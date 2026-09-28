#!/usr/bin/env bash
# Detached launcher for the GE16 Graph & Vector Explorer local server.
# Called SYNCHRONOUSLY by the Start .app AppleScript so the server keeps
# running after the launcher exits (reparented to launchd). Backgrounding
# inside `do shell script` does NOT survive — this helper file is the fix.
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$SCRIPT_DIR"
PY="/usr/bin/python3"          # ABSOLUTE path: click path is not a login shell
PORT="8765"
LOG="/tmp/ge16-graph-explorer.log"

cd "$APP_DIR" || exit 1
# stop any previous instance of THIS server (same port + module pattern)
pkill -f "http.server $PORT" 2>/dev/null
pkill -f "serve_no_cache.py $PORT" 2>/dev/null
sleep 1

# serve with Cache-Control: no-store — python -m http.server sends no cache
# headers, so Safari shows a STALE index.html after updates (the "different
# index.html / no loading page" bug). The no-cache wrapper fixes that.
nohup "$PY" "$APP_DIR/serve_no_cache.py" "$PORT" > "$LOG" 2>&1 &

for i in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:$PORT/index.html" 2>/dev/null)
    if [ "$code" = "200" ]; then echo "up"; exit 0; fi
    sleep 0.5
done
echo "timeout"; exit 1
