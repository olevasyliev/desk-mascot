# Desk Mascot — Phase 1 spec (software only)

The original spec this was built from. The README describes what actually shipped.

A desk companion for Claude Code. It runs on the Mac with no extra hardware and **no Anthropic API key**: it drives the
normal `claude` CLI under the existing subscription. Phase 2 moves the face and buttons onto an ESP32 board without changing
the Mac side.

## Goal

1. One key opens a new Claude Code session.
2. Hold another key and speak; the text goes into the active session.
3. A small on-screen mascot reacts to what Claude is doing.
4. Optionally, it reads a short part of the reply aloud.

## Constraints

- No API keys and no cloud calls. Speech-to-text and text-to-speech run locally. Only localhost traffic.
- Must never slow down or break Claude Code: if the mascot is not running, hooks exit 0 in under 100 ms and do nothing.
- Merge into `~/.claude/settings.json` without touching existing hooks.
- macOS 26, Apple Silicon, Homebrew.

## Architecture

```
 hotkeys / (phase 2: device buttons)
        │
        ▼
 ┌──────────────── mascotd (Python, 127.0.0.1:7788) ────────────────┐
 │  state machine · actions · STT · TTS · WebSocket /ws state feed   │
 └──────────────▲───────────────────────────────┬───────────────────┘
                │ POST /event (from hooks)      │ WebSocket state
   Claude Code hooks (curl, fire-and-forget)     ▼
                                       face window (phase 1) / ESP32 (phase 2)
```

Components:

| Part | What it is |
|---|---|
| `mascotd` | Long-running daemon: HTTP + WebSocket server, global hotkeys, recorder, STT, TTS. Started by a launchd agent at login. |
| `hooks/mascot-hook.sh` | One script for all hook events: reads the hook JSON from stdin, `curl --max-time 0.3` POSTs it to `/event`, always exits 0. |
| `face/` | HTML/CSS/JS face in a frameless, always-on-top, transparent window (pywebview), ~160×160, draggable, corner of the screen. |
| `install.py` | Installs dependencies, the whisper model, the launchd agent, and merges hooks into settings. `--uninstall` reverses it. |
| `config.toml` | Hotkeys, terminal app, default project dir, voice on/off, auto-Enter, whisper model, TTS voices. |

## Actions

### A. New session (default hotkey `⌃⌥N`, phase 2: button 1)
- Opens a new terminal tab/window in `default_dir` and runs `claude`.
- Terminal is configurable: `Terminal` (AppleScript `do script`, the reliable default) or `Warp`. Warp has no clean
  "run command in new tab" AppleScript, so use a Warp launch configuration or a URI if one works; if neither works reliably,
  document it and fall back to Terminal.
- Mascot: short "wake up" animation, then `idle`.

### B. Push-to-talk (default: hold right `⌥`, phase 2: button 2)
- Key down → start recording from the default mic (16 kHz mono). Mascot `listening`, with the mouth reacting to input level.
- Key up → stop and transcribe locally with **whisper.cpp** (`brew install whisper-cpp`, Metal), multilingual model
  (`small` by default, configurable). Mascot `transcribing`.
- Languages: Russian and English, auto-detect; mixed RU+EN tech speech must come out usable.
- Paste into the frontmost window: save clipboard → set text → `⌘V` → restore clipboard. `auto_enter` (default off)
  presses Return after pasting.
- Under 0.4 s of audio or empty transcript → ignore, mascot shrugs.
- Latency target: under 2 s from key-up to pasted text for a 10 s phrase (measure and report the real number).
- First, check whether Claude Code has its own built-in voice dictation. If it does and it's good, action B can just press
  its key, and local STT stays as the fallback.

### C. Voice toggle (default `⌃⌥V`, phase 2: long-press button 1)
- Turns spoken replies on/off; the mascot shows a small speaker icon when on.

## Mascot states (driven by Claude Code hooks)

| Hook event | State | Face |
|---|---|---|
| `SessionStart` | `idle` | blinks, looks around now and then |
| `UserPromptSubmit` | `thinking` | eyes up, antenna light pulses |
| `PreToolUse` | `working` | busy eyes; tool name as a tiny caption (Bash, Edit, Read…) |
| `PostToolUse` | `working` | stays until the next event |
| `Notification` (permission / waiting for input) | `needs_you` | waves, antenna blinks orange; until the next event |
| `Stop` | `done` | happy face 3 s → `idle` |
| `SessionEnd` | `sleep` if no sessions are left | eyes closed |
| (local) recording / STT | `listening` / `transcribing` | see action B |
| (local) speaking | `speaking` | mouth animates while `say` runs |

Several sessions at once: track by `session_id`. Priority: `needs_you` > `working`/`thinking` > `done` > `idle`.
Show a small counter when more than one session is busy. A session with no events for 30 min is dropped.

## Spoken reply (optional, off by default)

- On `Stop`, read the last assistant message from `transcript_path` (JSONL).
- Strip code blocks, tables, paths and markdown. Take the first 1–2 sentences, at most ~200 characters.
- Speak with macOS `say`: a Russian voice if the text is Cyrillic (e.g. Milena), otherwise an English one. Voices are set
  in config.
- No LLM summarisation (that would need a key). If the reply is mostly code, say "Done" or "Готово" instead.
- New speech interrupts old speech; push-to-talk interrupts speech immediately.

## Local API (the contract for the phase 2 device)

- `POST /event`: raw hook JSON (from hooks).
- `POST /action/new_session`, `POST /action/ptt/start`, `POST /action/ptt/stop`, `POST /action/voice/toggle`.
- `GET /ws`: WebSocket. Sends `{"state","tool","sessions_busy","voice_on","level"}` on every change, plus `level`
  (mic level 0–1) at ~15 Hz while listening.
- Binds to 127.0.0.1 only in phase 1. Phase 2 opens it to the LAN with a shared token from `.env`.

## Permissions to expect

Microphone (recording), Accessibility (global hotkeys, pasting with ⌘V), Automation for Terminal/Warp (opening tabs).
`install.py` prints which ones to grant and where.

## Acceptance criteria

1. The hotkey opens a new terminal tab running `claude` in `default_dir`.
2. Holding PTT and saying "покажи статус гита и последние три коммита" puts that text into the Claude prompt, readable,
   in under 2 s (real number reported).
3. While Claude runs a tool, the face shows `working` with the tool name; when it asks for permission it shows `needs_you`;
   when it finishes it shows `done` and then `idle`.
4. With voice on, a short reply is read aloud in the right language; with voice off, nothing is spoken.
5. With `mascotd` stopped, Claude Code works as before and hooks add under 100 ms (measured).
6. The existing `PostToolUse` hook still runs; `install.py --uninstall` removes only our entries.
7. A screen recording of the full loop: new session → dictate → face reacts → spoken reply.

## Out of scope for phase 1

ESP32 firmware, 3D-printed case, wake word ("Hey Claude"), LLM summaries, Windows/Linux, multiple Macs.

## Phase 2 hardware (Amazon.es, prices as seen 10.10.2026)

- Face + buttons: M5StickS3 (ESP32-S3, screen, buttons), ~€39, B0GT7N11B4.
- Alternative face: Waveshare ESP32-S3 1.28" round touch LCD, ~€35, B0CM68M8LR.
- Buttons only, works with phase 1 today: 3-key USB macro pad, ~€7, B0B4XLG9F6 (map keys to the hotkeys above).
