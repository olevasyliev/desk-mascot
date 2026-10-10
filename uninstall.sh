#!/bin/bash
# Remove Desk Mascot's hooks, login item and app. Leaves this folder (and the model) in place.
cd "$(dirname "$0")"
pkill -f "$PWD/mascotd.py" 2>/dev/null
pkill -f "whisper-server .*$PWD/models" 2>/dev/null
.venv/bin/python install.py --uninstall 2>/dev/null || python3 install.py --uninstall
