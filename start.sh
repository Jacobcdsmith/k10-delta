#!/usr/bin/env bash
# K10-Δ persistent launcher
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

KILLED=0
for p in host.py ota_server.py; do
    pkill -f "$p" 2>/dev/null && KILLED=1
done
[ "$KILLED" -eq 1 ] && sleep 1

source "$DIR/.env"
rm -f /tmp/k10_host.log /tmp/k10_ota.log
setsid bash -c "source '$DIR/.env' && cd '$DIR' && exec python host.py" > /tmp/k10_host.log 2>&1 &
setsid bash -c "cd '$DIR' && exec python ota_server.py" > /tmp/k10_ota.log 2>&1 &
echo "$!" > /tmp/k10_ota.pid

echo "Starting K10-Δ — wait 15s for boot..."
tail -f /tmp/k10_host.log 2>/dev/null &
TAILPID=$!
sleep 15
kill $TAILPID 2>/dev/null
echo ""
echo "=== Boot complete ==="
echo "Dashboard: http://localhost:8765"
echo "OTA:       http://localhost:8080/manifest"
