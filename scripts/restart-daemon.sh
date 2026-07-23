#!/bin/sh
DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Must match daemon.py: state dir lives beside the session socket.
SOCK="${HERDR_SOCKET_PATH:-$HOME/.config/herdr/herdr.sock}"
STATE="$(dirname "$SOCK")/agent-inbox-state"
PID=$(head -n1 "$STATE/daemon.lock" 2>/dev/null)
case "$PID" in
  ''|*[!0-9]*) PID=$(pgrep -f "$DIR/daemon.py" | head -n1) ;;
esac
if [ -n "$PID" ]; then
  kill "$PID" 2>/dev/null
  sleep 1
fi
nohup python3 "$DIR/daemon.py" >/dev/null 2>&1 &
exit 0
