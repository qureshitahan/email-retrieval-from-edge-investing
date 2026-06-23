#!/usr/bin/env bash
# Deploy to existing Azure Web Apps using separate backend/frontend zips.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-edgeinvesting-email-contacts-rg}"
API_APP="${API_APP:-edgeinvesting-email-contacts-api}"
WEB_APP="${WEB_APP:-edgeinvesting-email-contacts-web}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v az &>/dev/null; then
  echo "Install Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli"
  exit 1
fi

az account show &>/dev/null || az login

API_ZIP="/tmp/edge-contacts-api.zip"
WEB_ZIP="/tmp/edge-contacts-web.zip"

bash "$ROOT/deploy/build-zips.sh" /tmp

echo "==> Setting startup commands..."
az webapp config set --resource-group "$RESOURCE_GROUP" --name "$API_APP" \
  --startup-file "bash startup.sh" -o none
az webapp config set --resource-group "$RESOURCE_GROUP" --name "$WEB_APP" \
  --startup-file "bash startup.sh" -o none

echo "==> Deploying API..."
az webapp deploy --resource-group "$RESOURCE_GROUP" --name "$API_APP" \
  --src-path "$API_ZIP" --type zip --async false

echo "==> Deploying Web..."
az webapp deploy --resource-group "$RESOURCE_GROUP" --name "$WEB_APP" \
  --src-path "$WEB_ZIP" --type zip --async false

echo ""
echo "Done. Open your web app URL from Azure Portal → Overview."
echo "API health: /api/v1/health"
