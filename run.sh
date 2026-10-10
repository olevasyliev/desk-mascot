#!/bin/bash
# Start the mascot (face window + hotkeys + local STT). Restarts it after a crash;
# a normal stop (Ctrl+C, pkill/SIGTERM) ends the loop.
cd "$(dirname "$0")" || exit 1
crashes=0
while true; do
  .venv/bin/python "$PWD/mascotd.py" "$@"
  code=$?
  case $code in 0|130|143) exit $code ;; esac
  crashes=$((crashes + 1))
  echo "$(date +%T) mascotd exited with $code, restart #$crashes"
  [ $crashes -ge 5 ] && { echo "too many crashes, giving up"; exit $code; }
  sleep 2
done
