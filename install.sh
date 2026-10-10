#!/bin/bash
# One-command setup for Desk Mascot on macOS (Apple Silicon).
#   ./install.sh            install everything and start the mascot
#   MODEL=small ./install.sh   use a smaller, faster, less accurate speech model (~190 MB instead of ~550 MB)
set -euo pipefail
cd "$(dirname "$0")"

[ "$(uname)" = Darwin ] || { echo "macOS only."; exit 1; }
command -v brew >/dev/null || { echo "Homebrew is required: https://brew.sh"; exit 1; }
command -v claude >/dev/null || echo "warning: Claude Code CLI not found in PATH (https://claude.com/claude-code)"

echo "==> Homebrew packages (whisper.cpp, Python 3.12)"
brew list whisper-cpp >/dev/null 2>&1 || brew install whisper-cpp
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
PY="$(brew --prefix python@3.12)/bin/python3.12"

echo "==> Python environment"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

case "${MODEL:-turbo}" in
  turbo) FILE=ggml-large-v3-turbo-q5_0.bin ;;
  small) FILE=ggml-small-q5_1.bin ;;
  *) echo "MODEL must be turbo or small"; exit 1 ;;
esac
mkdir -p models
if [ ! -f "models/$FILE" ]; then
  echo "==> Downloading speech model $FILE (runs locally, nothing leaves your Mac)"
  curl -L --fail -o "models/$FILE.part" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$FILE"
  mv "models/$FILE.part" "models/$FILE"
fi
if [ "$FILE" != ggml-large-v3-turbo-q5_0.bin ] && [ ! -f settings.json ]; then
  printf '{\n  "stt": {\n    "model": "models/%s"\n  }\n}\n' "$FILE" > settings.json
fi

echo "==> Claude Code hooks and login item"
chmod +x run.sh hooks/mascot-hook.sh uninstall.sh
.venv/bin/python install.py "$@"

echo "==> Starting the mascot"
pkill -f "$PWD/mascotd.py" 2>/dev/null || true
sleep 1
open "Desk Mascot.app"
echo
echo "Done. Hold left Option and speak; Ctrl+Option+N opens a new Claude session; click the robot for settings."
