#!/bin/sh
# Idempotent daemon launcher: the daemon itself holds a flock and exits
# immediately when another instance is already running.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
nohup python3 "$DIR/daemon.py" >/dev/null 2>&1 &
exit 0
