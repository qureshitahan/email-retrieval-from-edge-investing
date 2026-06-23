#!/usr/bin/env bash
# Build Azure deploy zips (backend-only + frontend-only).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-$HOME/Desktop}"

API_ZIP="$OUT_DIR/edge-contacts-api.zip"
WEB_ZIP="$OUT_DIR/edge-contacts-web.zip"

echo "==> Building API zip: $API_ZIP"
(cd "$ROOT/backend" && zip -r "$API_ZIP" . \
  -x ".venv/*" \
  -x "**/__pycache__/*" \
  -x "**/*.pyc")

echo "==> Building Web zip: $WEB_ZIP"
(cd "$ROOT/frontend" && zip -r "$WEB_ZIP" . \
  -x "node_modules/*" \
  -x ".next/*")

echo ""
echo "Done."
ls -lh "$API_ZIP" "$WEB_ZIP"
echo ""
echo "Upload edge-contacts-api.zip  -> edgeinvesting-email-contacts-api"
echo "Upload edge-contacts-web.zip  -> edgeinvesting-email-contacts-web"
echo "Startup command on BOTH apps:  bash startup.sh"
