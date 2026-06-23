#!/bin/bash
set -e
cd "$(dirname "$0")"
export HOSTNAME=0.0.0.0
export PORT="${PORT:-8080}"
# Oryx already runs npm install + build during deploy — only rebuild if missing
if [ ! -d .next ]; then
  npm install
  npm run build
fi
exec npx next start -H "$HOSTNAME" -p "$PORT"
