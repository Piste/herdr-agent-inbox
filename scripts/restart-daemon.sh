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
  # Only kill a process that really is our daemon — the recorded pid may
  # have been recycled by the OS after a crash.
  if ps -p "$PID" -o command= 2>/dev/null | grep -q "daemon\.py"; then
    kill "$PID" 2>/dev/null
    # Wait for it to release the flock instead of racing a fixed sleep.
    i=0
    while [ "$i" -lt 25 ] && kill -0 "$PID" 2>/dev/null; do
      sleep 0.2
      i=$((i + 1))
    done
  fi
fi
nohup python3 "$DIR/daemon.py" >/dev/null 2>&1 &
exit 0
