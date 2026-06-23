# Relationship Intelligence CRM

Internal CRM built from **Microsoft Outlook Sent Items** for `dbains@edgeinvesting.ca`. Extracts contacts, builds cheap relationship context from metadata, scores fundraising relevance, and exports to Excel.

## What it does (MVP 1)

- Microsoft Graph OAuth (delegated `Mail.Read`)
- Paginated import of ~25k Sent Items (metadata + `bodyPreview` only)
- Contact extraction from To / CC / BCC
- Deduplication by email
- Company inference from domain
- Rule-based context + fundraising relevance scoring
- **Excel / CSV export**
- **Minimal web UI** (contact table, filters, detail drawer, Outlook links)

## Prerequisites

- Python 3.12+ (3.11+ also works)
- Node.js 18+
- Azure AD app registration in the Edge Investing tenant

## Azure AD setup

1. Go to [Azure Portal → App registrations](https://portal.azure.com/#view/Microsoft_AAD_IAM/ActiveDirectoryMenuBlade/~/RegisteredApps) → **New registration**
2. Name: `Relationship CRM Local`
3. Supported account types: **Single tenant**
4. Redirect URI (Web): `http://localhost:8000/api/v1/auth/callback`
5. After creation, note **Application (client) ID** and **Directory (tenant) ID**
6. **Certificates & secrets** → New client secret → copy value
7. **API permissions** → Add delegated:
   - `Mail.Read`
   - `Mail.Send` (required to send outreach emails from the platform)
   - `User.Read`
   - `offline_access`
8. **Grant admin consent** if your tenant requires it

After adding `Mail.Send`, click **Reconnect Outlook** in the app so the new scope is granted.

Copy `.env.example` to `.env` at the project root and fill in values:

```bash
cp .env.example .env
```

## Run locally

### Backend

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

## Usage flow

1. Click **Connect Microsoft Outlook** → sign in as `dbains@edgeinvesting.ca`
2. Click **Sync Sent Items** (first run may take 15–30+ minutes for ~25k messages)
3. Browse contacts in the table; click a row for detail drawer
4. Use **Export Excel** to validate in spreadsheet
5. Click **Open in Outlook** on any row to view the original email

## Fundraising outreach (`/outreach`)

1. On **Contacts**, approve people you want to email (✓ button)
2. Open **Outreach** in the top nav
3. **Sync Inbox** once (detects who has/hasn't replied)
4. Filter by **No reply ≥ N days** if needed
5. Select contacts → add optional instructions → **Generate drafts**
6. Review each draft, edit subject/body, **Approve draft**
7. **Send via Outlook** (single) or **Send all approved** (bulk)

**Prompt template:** Click *Edit prompt template* to see/change the system + user prompts the LLM uses. Each draft stores the exact prompt used — click *Show prompt sent to LLM*.

Requires `Mail.Send` in Azure + **Reconnect Outlook** after adding the permission.

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/auth/login` | Start Microsoft OAuth |
| `GET /api/v1/auth/status` | Connection status |
| `POST /api/v1/sync/start` | Start full sync |
| `GET /api/v1/sync/status` | Latest sync progress |
| `GET /api/v1/contacts` | Paginated contact list |
| `GET /api/v1/contacts/{id}` | Contact detail |
| `GET /api/v1/export/contacts.xlsx` | Excel export |
| `GET /api/v1/export/contacts.csv` | CSV export |
| `POST /api/v1/sync/start-inbox` | Sync inbox for reply detection |
| `GET /api/v1/outreach/prompt` | View LLM prompt template |
| `POST /api/v1/outreach/drafts/generate` | Generate outreach drafts |
| `POST /api/v1/outreach/drafts/{id}/send` | Send draft via Outlook |

## Data storage

- SQLite database: `data/crm.db` (local) or `/home/data/crm.db` (Azure)
- OAuth tokens stored in the same database
- See **[docs/AZURE_DEPLOY.md](docs/AZURE_DEPLOY.md)** to host on Azure for your manager

## Deploy to Azure

Full step-by-step: **[docs/AZURE_DEPLOY.md](docs/AZURE_DEPLOY.md)**

1. Two Azure Web Apps: **Python API** + **Node frontend**
2. Add redirect URI: `https://YOUR-API.azurewebsites.net/api/v1/auth/callback`
3. Configure app settings (secrets, `FRONTEND_URL`, `NEXT_PUBLIC_API_BASE`)
4. Run `deploy/azure-deploy.sh` or use Azure Deployment Center
5. Share the **web** URL with your manager

## Internal domains excluded by default

- `edgeinvesting.ca`
- `galaxypharma.com`
- `galaxypharma.ca`

Edit `INTERNAL_DOMAINS` in `.env` to change.

## Roadmap

- **MVP 2** ✅ (partial): UI table, filters, detail drawer, Outlook links
- **MVP 3** ✅ (partial): Rule-based context + scoring
- **MVP 4**: On-demand Anthropic AI summary + follow-up draft + classify + deep thread summary
- **MVP 5**: Delta sync for incremental updates

## Security notes

- This is a local MVP. Do not deploy without hardening auth, HTTPS, and secret management.
- The app stores email previews locally. Delete `data/crm.db` when done.
- Only sign in with the intended mailbox.
