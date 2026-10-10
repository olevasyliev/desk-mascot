"""Install / uninstall the Desk Mascot integration (run by install.sh / uninstall.sh).

  install.py               merge hooks into ~/.claude/settings.json, build the login app, add it to login items
  install.py --rebuild-app rebuild the login app too (macOS then asks for its permissions again)
  install.py --uninstall   remove only our hook entries, the login item, the app and the Warp tab config

Existing hooks are left untouched; a backup of settings.json is written before any change.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOOK = str(ROOT / "hooks" / "mascot-hook.sh")
SETTINGS = Path.home() / ".claude" / "settings.json"
TAB_CONFIG = Path.home() / ".warp" / "tab_configs" / "claude-mascot.toml"
APP = ROOT / "Desk Mascot.app"
LOG = Path.home() / "Library" / "Logs" / "desk-mascot.log"
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd"]
TOOL_EVENTS = {"PreToolUse", "PostToolUse"}


def osa(script):
    subprocess.run(["osascript", "-e", script], capture_output=True)


def ours(entry):
    return any(h.get("command", "").endswith("mascot-hook.sh") for h in entry.get("hooks", []))


def load():
    return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}


def save(data):
    if SETTINGS.exists():
        backup = SETTINGS.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(SETTINGS, backup)
        print("backup:", backup)
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def install():
    os.chmod(HOOK, 0o755)
    data = load()
    hooks = data.setdefault("hooks", {})
    for ev in EVENTS:
        entries = [e for e in hooks.get(ev, []) if not ours(e)]
        entry = {"hooks": [{"type": "command", "command": HOOK, "timeout": 2}]}
        if ev in TOOL_EVENTS:
            entry = {"matcher": "*", **entry}
        hooks[ev] = entries + [entry]
    save(data)
    print("hooks installed for:", ", ".join(EVENTS))

    # Start at login through a tiny AppleScript app. A launchd agent can't do it: macOS blocks background
    # services from ~/Desktop. The app is the process macOS asks about Desktop, Microphone and Accessibility.
    # Rebuilding re-signs the app, and macOS forgets the permissions granted to it, so keep an existing one.
    if not APP.exists() or "--rebuild-app" in sys.argv:
        shutil.rmtree(APP, ignore_errors=True)
        subprocess.run(["osacompile", "-o", str(APP), "-e",
                        f'do shell script quoted form of "{ROOT / "run.sh"}" & " >> " & quoted form of "{LOG}" & " 2>&1"'],
                       check=True)
        plist = str(APP / "Contents" / "Info.plist")
        for key, kind, value in [("LSUIElement", "-bool", "YES"), ("CFBundleName", "-string", "Desk Mascot"),
                                 ("CFBundleIdentifier", "-string", "io.github.desk-mascot")]:
            subprocess.run(["plutil", "-replace", key, kind, value, plist], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(APP)], check=True)
    osa(f'tell application "System Events" to delete (every login item whose name is "{APP.stem}")')
    osa(f'tell application "System Events" to make login item at end with properties {{path:"{APP}", hidden:true}}')
    print("login item:", APP, "log:", LOG)
    print("\nOn first use macOS asks for Microphone. Also enable the app in System Settings > Privacy & Security >")
    print("Accessibility and Input Monitoring (it may be listed as \"Desk Mascot\" or \"applet\").")


def uninstall():
    data = load()
    hooks = data.get("hooks", {})
    for ev in list(hooks):
        hooks[ev] = [e for e in hooks[ev] if not ours(e)]
        if not hooks[ev]:
            del hooks[ev]
    save(data)
    TAB_CONFIG.unlink(missing_ok=True)
    osa(f'tell application "System Events" to delete (every login item whose name is "{APP.stem}")')
    shutil.rmtree(APP, ignore_errors=True)
    print("removed mascot hooks, tab config and login item")


if __name__ == "__main__":
    uninstall() if "--uninstall" in sys.argv else install()
