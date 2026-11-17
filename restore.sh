#!/bin/sh
# ha-auto-restore launcher for Linux/macOS. No argument: "auto" (diagnose, then the steps in order).
DIR=$(cd "$(dirname "$0")" && pwd)
if [ $# -eq 0 ]; then set -- auto; fi
exec python3 "$DIR/ha_restore.py" "$@"
