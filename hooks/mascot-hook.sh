#!/bin/bash
# Forwards a Claude Code hook event to mascotd. Never blocks or fails Claude:
# connection refused returns instantly, and every exit is 0.
curl -s --max-time 0.3 -X POST -H 'Content-Type: application/json' \
  --data-binary @- "http://127.0.0.1:${MASCOT_PORT:-7788}/event" >/dev/null 2>&1
exit 0
