# Deploy to Azure App Service

Host the CRM for your manager using **two Azure Web Apps** (recommended):

| App | Stack | Example URL |
|-----|-------|-------------|
| **edgeinvesting-email-contacts-api** | Python 3.12 + FastAPI | `https://edgeinvesting-email-contacts-api.azurewebsites.net` |
| **edgeinvesting-email-contacts-web** | Node 20 + Next.js | `https://edgeinvesting-email-contacts-web.azurewebsites.net` |

The manager uses the **web** URL. The **api** URL handles Outlook OAuth, sync, and sending.

---

## Prerequisites

- Azure subscription (same tenant as Edge Investing)
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) installed (`az login`)
- Your existing App Registration (**Edge Investing - Email Contact Retrieval**)
- Git repo pushed to GitHub (optional but easiest for deploy)

---

## Step 1 — Update Azure AD app (production URLs)

In [Azure Portal → App registrations → Edge Investing - Email Contact Retrieval](https://portal.azure.com):

### Redirect URIs (Web)

Add **both** (keep localhost for your dev machine):

```
https://edgeinvesting-email-contacts-api.azurewebsites.net/api/v1/auth/callback
http://localhost:8000/api/v1/auth/callback
```

Replace `edgeinvesting-email-contacts-api` with your actual API app name if you chose a different one.

### API permissions (already done)

- Mail.Read, Mail.Send, User.Read — admin consent granted

---

## Step 2 — Create Azure resources (CLI)

Pick a region close to you (e.g. `canadacentral`) and unique names:

```bash
RESOURCE_GROUP="edgeinvesting-email-contacts-rg"
LOCATION="canadacentral"
API_APP="edgeinvesting-email-contacts-api"      # must be globally unique
WEB_APP="edgeinvesting-email-contacts-web"      # must be globally unique
PLAN="edgeinvesting-email-contacts-plan"

az group create --name $RESOURCE_GROUP --location $LOCATION

az appservice plan create \
  --name $PLAN \
  --resource-group $RESOURCE_GROUP \
  --sku B1 \
  --is-linux

# API (Python)
az webapp create \
  --resource-group $RESOURCE_GROUP \
  --plan $PLAN \
  --name $API_APP \
  --runtime "PYTHON:3.12"

# Web (Node)
az webapp create \
  --resource-group $RESOURCE_GROUP \
  --plan $PLAN \
  --name $WEB_APP \
  --runtime "NODE:20-lts"
```

**B1** (~$13/mo per plan; one plan can host both apps) is enough for a small team. Use **S1** if sync runs feel slow.

Enable **Always On** (keeps SQLite + background sync stable):

```bash
az webapp config set --resource-group $RESOURCE_GROUP --name $API_APP --always-on true
az webapp config set --resource-group $RESOURCE_GROUP --name $WEB_APP --always-on true
```

---

## Step 3 — Configure API app settings

Replace values with yours. Get the client secret from Azure AD → Certificates & secrets.

```bash
API_URL="https://${API_APP}.azurewebsites.net"
WEB_URL="https://${WEB_APP}.azurewebsites.net"

az webapp config appsettings set --resource-group $RESOURCE_GROUP --name $API_APP --settings \
  AZURE_CLIENT_ID="fc47853d-e5a8-40f8-b7a2-1655d512c17b" \
  AZURE_CLIENT_SECRET="YOUR_CLIENT_SECRET" \
  AZURE_TENANT_ID="852ddb55-084f-4b90-8e78-3a154cdef606" \
  AZURE_REDIRECT_URI="${API_URL}/api/v1/auth/callback" \
  FRONTEND_URL="${WEB_URL}" \
  CORS_ORIGINS="${WEB_URL}" \
  GRAPH_SCOPES="Mail.Read,Mail.Send,User.Read" \
  ANTHROPIC_API_KEY="YOUR_ANTHROPIC_KEY" \
  ANTHROPIC_MODEL="claude-sonnet-4-6" \
  DATABASE_URL="sqlite:////home/data/crm.db" \
  SCM_DO_BUILD_DURING_DEPLOYMENT="true"
```

Set startup command:

```bash
az webapp config set --resource-group $RESOURCE_GROUP --name $API_APP \
  --startup-file "bash startup.sh"
```

Health check (optional):

```bash
az webapp config set --resource-group $RESOURCE_GROUP --name $API_APP \
  --generic-configurations '{"healthCheckPath": "/api/v1/health"}'
```

---

## Step 4 — Configure Web app settings

`NEXT_PUBLIC_API_BASE` must be set **before** the build runs:

```bash
az webapp config appsettings set --resource-group $RESOURCE_GROUP --name $WEB_APP --settings \
  NEXT_PUBLIC_API_BASE="https://${API_APP}.azurewebsites.net/api/v1" \
  SCM_DO_BUILD_DURING_DEPLOYMENT="true"
```

Startup command:

```bash
az webapp config set --resource-group $RESOURCE_GROUP --name $WEB_APP \
  --startup-file "bash startup.sh"
```

---

## Step 5 — Deploy code

Use **two separate zips** (backend and frontend at zip root). Azure Oryx needs
`requirements.txt` or `package.json` at the top level of each upload.

### Build zips (Mac)

```bash
cd "/path/to/Email Retrieval from Edge Investing"
bash deploy/build-zips.sh ~/Desktop
```

Creates:

| Zip | Upload to |
|-----|-----------|
| `edge-contacts-api.zip` | **edgeinvesting-email-contacts-api** |
| `edge-contacts-web.zip` | **edgeinvesting-email-contacts-web** |

**Startup command on both apps:** `bash startup.sh`

### Option A — Azure Portal (Zip Push Deploy)

1. Open each Web App → **Development Tools** → **Advanced Tools** → **Go**
2. Navigate to `/ZipDeployUI` in the browser URL
3. Upload the matching zip → **Deploy** (5–10 min each)

Set `NEXT_PUBLIC_API_BASE` on the **web** app **before** deploying the web zip
(Oryx bakes it into the build).

### Option B — Azure CLI

```bash
bash deploy/azure-deploy.sh
```

### Option C — GitHub Actions (recommended for updates)

1. Push repo to GitHub
2. Azure Portal → each Web App → **Deployment Center** → GitHub
3. Set startup to `bash startup.sh` on each app
4. Web app: `NEXT_PUBLIC_API_BASE` must exist before first build

---

## Step 6 — First use (manager flow)

1. Open `https://edgeinvesting-email-contacts-web.azurewebsites.net`
2. **Connect Microsoft Outlook** → sign in as `dbains@edgeinvesting.ca`
3. **Sync Sent Items** (first run: 15–30+ min for ~13k emails)
4. Review contacts → approve → **Outreach** to send

---

## Custom domain (optional)

Map e.g. `crm.edgeinvesting.ca` to the **web** app:

```bash
az webapp config hostname add --resource-group $RESOURCE_GROUP --webapp-name $WEB_APP --hostname crm.edgeinvesting.ca
```

Then update:

- `FRONTEND_URL` and `CORS_ORIGINS` on API app
- `NEXT_PUBLIC_API_BASE` on Web app (rebuild required)
- Azure AD redirect URI if API also gets a custom domain

---

## Data & backups

- SQLite file lives at `/home/data/crm.db` on the API app (persists across restarts)
- **Back up regularly**: download via Kudu (`https://edgeinvesting-email-contacts-api.scm.azurewebsites.net` → Debug console → `/home/data/crm.db`)
- For heavy production use later, migrate to **Azure Database for PostgreSQL**

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| OAuth redirect error | Redirect URI in Azure AD must exactly match `AZURE_REDIRECT_URI` |
| CORS error in browser | Set `CORS_ORIGINS` to exact web URL (https, no trailing slash) |
| API calls fail on web | Rebuild web app after setting `NEXT_PUBLIC_API_BASE` |
| Mail.Send fails | Reconnect Outlook after granting Mail.Send |
| Sync times out | Normal on first run; check API logs. Always On must be enabled |
| Empty contacts after deploy | Run Sync Sent Items — DB starts fresh on new deploy unless you restore `crm.db` |

### View logs

```bash
az webapp log tail --resource-group $RESOURCE_GROUP --name $API_APP
az webapp log tail --resource-group $RESOURCE_GROUP --name $WEB_APP
```

---

## Security notes for production

- Restrict who can open the web URL (Azure App Service **Authentication** / IP restrictions, or private network)
- Rotate `AZURE_CLIENT_SECRET` periodically; update app settings when you do
- Never commit `.env` or secrets to git
- This app stores email metadata locally — treat the API app as sensitive

---

## Cost estimate (small team)

| Resource | Approx. |
|----------|---------|
| B1 App Service Plan (2 apps on 1 plan) | ~$13–25 USD/mo |
| Anthropic API | Pay per AI draft/summary |
| Microsoft Graph | Included with M365 |

---

## Quick checklist

- [ ] Azure AD redirect URI added for production API
- [ ] API app settings configured (secrets, FRONTEND_URL, CORS)
- [ ] Web app `NEXT_PUBLIC_API_BASE` set, then deployed/built
- [ ] Always On enabled on both apps
- [ ] Manager can open web URL and connect Outlook
- [ ] First sync completed
