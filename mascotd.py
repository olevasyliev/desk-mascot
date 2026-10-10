"""Desk Mascot daemon: Claude Code hook events -> mascot state, hotkeys -> actions, local STT/TTS.

Runs the face window on the main thread (pywebview needs Cocoa there), the HTTP/WebSocket server
and the whisper.cpp server in the background. No API keys, localhost only.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from aiohttp import web
from pynput import keyboard

ROOT = Path(__file__).resolve().parent
CFG = tomllib.loads((ROOT / "config.toml").read_text())
OVERRIDES = ROOT / "settings.json"  # written by the settings panel, wins over config.toml
TAB_CONFIG = Path.home() / ".warp" / "tab_configs" / "claude-mascot.toml"
try:
    for _sec, _vals in json.loads(OVERRIDES.read_text()).items():
        CFG.setdefault(_sec, {}).update(_vals)
except FileNotFoundError:
    pass
PORT = CFG["general"]["port"]
STT_PORT = CFG["stt"]["server_port"]
RATE = 16000
SESSION_TTL = 30 * 60
DONE_SECONDS = 3

# Display priority, highest first. Local states (listening etc.) always win.
PRIORITY = ["needs_you", "working", "thinking", "done", "idle", "sleep"]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Mascot:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}  # session_id -> {"state", "tool", "ts"}
        self.local = None  # listening | transcribing | speaking | None
        self.voice_on = CFG["voice"]["enabled"]
        self.flash = None  # (state, until) for one-off reactions: wake, shrug
        self.level = 0.0
        self.loop = None
        self.clients = set()
        self.last_sent = None
        self.say_proc = None
        self.last_prompt_sid = None

    # ---- state ----
    def snapshot(self):
        with self.lock:
            now = time.time()
            for sid in [s for s, v in self.sessions.items() if now - v["ts"] > SESSION_TTL]:
                del self.sessions[sid]
            for v in self.sessions.values():
                if v["state"] == "done" and now - v["ts"] > DONE_SECONDS:
                    v["state"] = "idle"
            live = [v for v in self.sessions.values() if v["state"] != "sleep"]
            tool = None
            if self.local:
                state = self.local
            elif self.flash and self.flash[1] > now:
                state = self.flash[0]
            elif live:
                top = min(live, key=lambda v: (PRIORITY.index(v["state"]), -v["ts"]))
                state, tool = top["state"], top.get("tool") if top["state"] == "working" else None
            else:
                state = "sleep" if self.sessions else "idle"
            busy = sum(1 for v in live if v["state"] in ("working", "thinking", "needs_you"))
            return {"state": state, "tool": tool, "sessions_busy": busy, "voice_on": self.voice_on}

    def push(self, force=False):
        snap = self.snapshot()
        if force or snap != self.last_sent:
            self.last_sent = snap
            self.broadcast(snap)

    def broadcast(self, msg):
        if self.loop:
            self.loop.call_soon_threadsafe(asyncio.ensure_future, self._send(json.dumps(msg)))

    async def _send(self, data):
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:
                self.clients.discard(ws)

    def set_local(self, state):
        with self.lock:
            self.local = state
        self.push()

    def do_flash(self, state, seconds=1.5):
        with self.lock:
            self.flash = (state, time.time() + seconds)
        self.push()

    # ---- hook events ----
    def on_event(self, ev):
        name = ev.get("hook_event_name", "")
        sid = ev.get("session_id", "?")
        log("event", sid[:8], name, ev.get("tool_name", ""), ev.get("notification_type", ""))
        with self.lock:
            s = self.sessions.setdefault(sid, {"state": "idle", "tool": None, "ts": time.time()})
            s["ts"] = time.time()
            if name == "SessionStart":
                s["state"] = "idle"
            elif name == "UserPromptSubmit":
                s["state"], s["tool"] = "thinking", None
                self.last_prompt_sid = sid
            elif name == "PreToolUse":
                tool = ev.get("tool_name", "")
                s["state"] = "needs_you" if tool == "AskUserQuestion" else "working"
                s["tool"] = tool.split("__")[-1][:18]
            elif name == "PostToolUse":
                s["state"] = "working"
            elif name == "Notification":
                if ev.get("notification_type") != "idle_prompt":
                    s["state"] = "needs_you"
            elif name == "Stop":
                s["state"], s["tool"] = "done", None
            elif name == "SessionEnd":
                s["state"] = "sleep"
        self.push()
        mine = CFG["voice"].get("scope", "all") == "all" or sid == self.last_prompt_sid
        if name == "Stop" and self.voice_on and mine:
            text = ev.get("last_assistant_message") or last_reply(ev.get("transcript_path"))
            speak(spoken_summary(text or ""))


M = Mascot()


# ---- spoken replies ----
def last_reply(path):
    try:
        lines = Path(path).read_text().splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "assistant":
            parts = rec.get("message", {}).get("content", [])
            text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
            if text.strip():
                return text
    return ""


def spoken_summary(text):
    cyr = bool(re.search(r"[а-яА-ЯёЁіїєІЇЄ]", text))
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"^\s*\|.*$", " ", t, flags=re.M)  # tables
    t = re.sub(r"`[^`]*`", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)  # links -> label
    t = re.sub(r"\S*/\S*[^\s.,!?;:]", " ", t)  # paths, urls (keep trailing punctuation)
    t = re.sub(r"[*_#>~]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    sentences = re.split(r"(?<=[.!?…])\s+", t)
    out = ""
    for s in sentences[:2]:
        if len(out) + len(s) > CFG["voice"]["max_chars"]:
            break
        out = (out + " " + s).strip()
    if len(out) < 8:
        out = "Готово." if cyr else "Done."
    return out


def speak(text):
    stop_speaking()
    cyr = bool(re.search(r"[а-яА-ЯёЁ]", text))
    voice = CFG["voice"]["voice_ru" if cyr else "voice_en"]
    log("say", voice or "(system voice)", repr(text))
    # Empty voice = the system Spoken Content voice; that's the only way to reach Siri/neural voices from `say`.
    M.say_proc = subprocess.Popen(["say", *(["-v", voice] if voice else []), text], stderr=subprocess.PIPE)
    M.set_local("speaking")

    def wait(p):
        err = p.communicate()[1]
        if p.returncode:
            log("say failed", p.returncode, err.decode()[:200])
        if M.say_proc is p:
            M.say_proc = None
            if M.local == "speaking":
                M.set_local(None)

    threading.Thread(target=wait, args=(M.say_proc,), daemon=True).start()


def stop_speaking():
    p = M.say_proc
    if p and p.poll() is None:
        p.terminate()
    M.say_proc = None
    if M.local == "speaking":
        M.set_local(None)


# ---- actions ----
def new_session():
    d = os.path.expanduser(CFG["general"]["default_dir"])
    if terminal() == "warp":
        subprocess.run(["open", "warp://tab_config/claude-mascot"])
    else:
        script = f'tell application "Terminal" to do script "cd {d!r} && claude"\ntell application "Terminal" to activate'
        subprocess.run(["osascript", "-e", script])
    log("new session")
    M.do_flash("wake", 1.5)


def write_tab_config():
    TAB_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    TAB_CONFIG.write_text('name = "Claude"\n[[panes]]\nid = "main"\ntype = "terminal"\n'
                          f'directory = "{CFG["general"]["default_dir"]}"\ncommands = ["claude"]\n')


# ---- settings panel ----
SETTINGS = [  # (section, key) the panel may change
    ("voice", "enabled"), ("voice", "scope"), ("voice", "voice_ru"), ("voice", "voice_en"),
    ("stt", "language"), ("stt", "auto_enter"), ("hotkeys", "push_to_talk"), ("general", "default_dir"),
]


def terminal():
    t = CFG["general"].get("terminal", "auto")
    if t == "auto":
        return "warp" if Path("/Applications/Warp.app").exists() else "terminal"
    return t


def installed_voices():
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    voices = {"ru": [], "en": []}
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s+(\w\w)_\w\w\s+#", line)
        if m and m.group(2) in voices:
            voices[m.group(2)].append(m.group(1).strip())
    return voices


def get_settings():
    return {"values": {f"{sec}.{key}": CFG[sec][key] for sec, key in SETTINGS}, "voices": installed_voices()}


def set_setting(path, value):
    sec, key = path.split(".")
    if (sec, key) not in SETTINGS:
        raise ValueError(path)
    CFG[sec][key] = value
    try:
        over = json.loads(OVERRIDES.read_text())
    except FileNotFoundError:
        over = {}
    over.setdefault(sec, {})[key] = value
    OVERRIDES.write_text(json.dumps(over, indent=2, ensure_ascii=False) + "\n")
    if path == "voice.enabled":
        with M.lock:
            M.voice_on = bool(value)
        if not value:
            stop_speaking()
    if path == "general.default_dir" and terminal() == "warp":
        write_tab_config()
    log("setting", path, "=", value)
    M.push(force=True)


def toggle_voice():
    set_setting("voice.enabled", not M.voice_on)


class Recorder:
    def __init__(self):
        self.chunks = []
        self.stream = None
        self.t0 = 0
        self.last_level = 0
        self.revealed = False

    def start(self, quiet=False):
        if self.stream:
            return
        self.chunks = []
        self.t0 = time.time()
        self.revealed = False
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", callback=self._cb)
        self.stream.start()
        if not quiet:
            self.reveal()

    def reveal(self):
        self.revealed = True
        stop_speaking()
        M.set_local("listening")

    def _cb(self, data, frames, t, status):
        self.chunks.append(data.copy())
        now = time.time()
        if self.revealed and now - self.last_level > 1 / 15:
            self.last_level = now
            M.broadcast({"level": float(min(1.0, np.sqrt(np.mean(data**2)) * 12))})

    def stop(self, cancel=False):
        if not self.stream:
            return
        self.stream.stop()
        self.stream.close()
        self.stream = None
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros((0, 1), "float32")
        secs = len(audio) / RATE
        if cancel or secs < CFG["stt"]["min_seconds"]:
            if self.revealed:
                M.set_local(None)
                if not cancel:
                    M.do_flash("shrug")
            return
        if not self.revealed:
            self.reveal()
        M.set_local("transcribing")
        threading.Thread(target=self._transcribe, args=(audio, secs), daemon=True).start()

    def _transcribe(self, audio, secs):
        t0 = time.time()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        text = transcribe_file(path)
        os.unlink(path)
        log(f"stt {secs:.1f}s audio -> {time.time() - t0:.2f}s: {text!r}")
        M.set_local(None)
        if text:
            paste(text)
        else:
            M.do_flash("shrug")


def transcribe_file(path):
    r = subprocess.run(
        ["curl", "-s", "--max-time", "60", f"http://127.0.0.1:{STT_PORT}/inference",
         "-F", f"file=@{path}", "-F", "response_format=json", "-F", f"language={CFG['stt']['language']}",
         "-F", "temperature=0.0"],
        capture_output=True, text=True,
    )
    try:
        text = json.loads(r.stdout).get("text", "")
    except Exception:
        log("stt error", r.stdout[:200], r.stderr[:200])
        return ""
    text = re.sub(r"\[[^\]]*\]|\([^)]*музык[^)]*\)", " ", text)  # [BLANK_AUDIO], (музыка)
    return re.sub(r"\s+", " ", text).strip()


kb = keyboard.Controller()
KEY_V = keyboard.KeyCode.from_vk(9)  # physical V key, works under any layout (Russian included)


def can_type():
    from ApplicationServices import AXIsProcessTrusted
    return bool(AXIsProcessTrusted())


def paste(text):
    if not can_type():
        # Without Accessibility macOS silently drops our ⌘V. Leave the text on the clipboard instead.
        subprocess.run(["pbcopy"], input=text.encode())
        log("no Accessibility permission: text left on the clipboard, press ⌘V")
        M.do_flash("copied", 2.5)
        return
    old = subprocess.run(["pbpaste"], capture_output=True).stdout
    subprocess.run(["pbcopy"], input=text.encode())
    time.sleep(0.05)
    with kb.pressed(keyboard.Key.cmd):
        kb.tap(KEY_V)
    if CFG["stt"]["auto_enter"]:
        time.sleep(0.1)
        kb.tap(keyboard.Key.enter)
    time.sleep(1.0)  # Warp reads the clipboard lazily; restoring too early pastes the wrong text
    subprocess.run(["pbcopy"], input=old)


# ---- hotkeys ----
REC = Recorder()
PTT_CHOICES = {"left_option": {"alt", "alt_l"}, "right_option": {"alt_r"}}
MODIFIERS = {"cmd", "cmd_r", "ctrl", "ctrl_r", "shift", "shift_r", "alt", "alt_l", "alt_r"}
HOLD_DELAY = 0.25  # a modifier tapped or used in a chord shorter than this never shows "listening"


def ptt_keys():
    keys = set()
    for k in CFG["hotkeys"]["push_to_talk"]:
        keys |= PTT_CHOICES.get(k, {k})
    return keys


def key_name(k):
    return getattr(k, "name", None) or getattr(k, "char", None)


class Hotkeys:
    def __init__(self):
        combos = {c: new_session for c in CFG["hotkeys"]["new_session"]}
        combos.update({c: toggle_voice for c in CFG["hotkeys"]["voice_toggle"]})
        self.combos = [keyboard.HotKey(keyboard.HotKey.parse(c), lambda f=f: threading.Thread(target=f, daemon=True).start())
                       for c, f in combos.items()]
        self.held = set()
        self.ptt_down = None  # key name currently held for PTT
        self.ptt_gen = 0

    def press(self, k):
        name = key_name(k)
        if self.ptt_down and name != self.ptt_down:
            # Another key while holding PTT: a chord like ⌥← or ⌃⌥N, not dictation.
            self.ptt_down = None
            REC.stop(cancel=True)
        elif name in ptt_keys() and self.ptt_down is None and not (self.held & MODIFIERS):
            self.ptt_down = name
            self.ptt_gen += 1
            REC.start(quiet=True)
            threading.Timer(HOLD_DELAY, self._reveal, args=(self.ptt_gen,)).start()
        self.held.add(name)
        ck = self.listener.canonical(k)
        for h in self.combos:
            h.press(ck)

    def _reveal(self, gen):
        if self.ptt_down and gen == self.ptt_gen:
            REC.reveal()

    def release(self, k):
        name = key_name(k)
        self.held.discard(name)
        if self.ptt_down and name == self.ptt_down:
            self.ptt_down = None
            REC.stop()
        ck = self.listener.canonical(k)
        for h in self.combos:
            h.release(ck)

    def run(self):
        self.listener = SafeListener(on_press=self.press, on_release=self.release)
        self.listener.start()


if sys.platform == "darwin":
    from Quartz import CGEventMaskBit, kCGEventFlagsChanged, kCGEventKeyDown, kCGEventKeyUp

    class SafeListener(keyboard.Listener):
        # pynput also taps system-defined (media/caps-lock) events and converts them with NSEvent on its own
        # thread; macOS 26 asserts that conversion is main-thread only and kills the process (crash 10.10).
        # We only need plain keys and modifiers.
        _EVENTS = CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventKeyUp) | CGEventMaskBit(kCGEventFlagsChanged)
else:
    SafeListener = keyboard.Listener


# ---- HTTP / WebSocket ----
async def h_event(req):
    try:
        ev = await req.json()
    except Exception:
        return web.Response(status=400)
    threading.Thread(target=M.on_event, args=(ev,), daemon=True).start()
    return web.Response(text="ok")


def action(fn):
    async def h(req):
        threading.Thread(target=fn, daemon=True).start()
        return web.Response(text="ok")
    return h


async def h_settings(req):
    if req.method == "POST":
        body = await req.json()
        try:
            set_setting(body["path"], body["value"])
        except (KeyError, ValueError):
            return web.Response(status=400)
    return web.json_response(get_settings())


async def h_test_voice(req):
    lang = (await req.json()).get("lang", "ru")
    text = "Привет! Так я звучу." if lang == "ru" else "Hi! This is how I sound."
    threading.Thread(target=speak, args=(text,), daemon=True).start()
    return web.Response(text="ok")


async def h_ws(req):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    M.clients.add(ws)
    await ws.send_str(json.dumps(M.snapshot()))
    async for _ in ws:
        pass
    M.clients.discard(ws)
    return ws


async def ticker():
    while True:
        await asyncio.sleep(1)
        M.push()


def serve():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    M.loop = loop
    app = web.Application()
    app.router.add_post("/event", h_event)
    app.router.add_post("/action/new_session", action(new_session))
    app.router.add_post("/action/ptt/start", action(REC.start))
    app.router.add_post("/action/ptt/stop", action(REC.stop))
    app.router.add_post("/action/voice/toggle", action(toggle_voice))
    app.router.add_get("/ws", h_ws)
    app.router.add_route("*", "/settings", h_settings)
    app.router.add_post("/voice/test", h_test_voice)
    app.router.add_get("/", lambda r: web.FileResponse(ROOT / "face" / "index.html"))
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", PORT).start())
    loop.create_task(ticker())
    if terminal() == "warp":
        write_tab_config()
    log(f"mascotd on http://127.0.0.1:{PORT}", "| accessibility:", "yes" if can_type() else "NO (paste falls back to clipboard)")
    loop.run_forever()


def start_stt():
    # A previous mascotd killed with SIGTERM leaves its whisper-server behind; replace it.
    subprocess.run(["pkill", "-f", f"whisper-server .*--port {STT_PORT}"], capture_output=True)
    time.sleep(0.2)
    model = ROOT / CFG["stt"]["model"]
    return subprocess.Popen(
        ["whisper-server", "-m", str(model), "--host", "127.0.0.1", "--port", str(STT_PORT),
         "-l", CFG["stt"]["language"], "-nt"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class WindowApi:
    """Called from the face page: grow the window for the settings panel, keeping the robot in place."""
    window = None

    def resize(self, width, height, dx):
        w = WindowApi.window
        w.resize(int(width), int(height))
        if dx:
            w.move(w.x + int(dx), w.y)


def main():
    headless = "--headless" in sys.argv
    stt = start_stt()
    threading.Thread(target=serve, daemon=True).start()
    hk = Hotkeys()
    hk.run()
    try:
        if headless:
            while True:
                time.sleep(3600)
        else:
            import webview
            time.sleep(0.3)
            win = webview.create_window("Mascot", f"http://127.0.0.1:{PORT}/", width=170, height=205,
                                        frameless=True, on_top=True, transparent=True, easy_drag=False,
                                        x=40, y=40, js_api=WindowApi())
            WindowApi.window = win
            webview.start()
    except KeyboardInterrupt:
        pass
    finally:
        stt.terminate()


if __name__ == "__main__":
    main()
