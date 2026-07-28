"""Outreach mailbox registry.

Sending identities are declared in the OUTREACH_MAILBOXES env var as a JSON array.
Three providers are supported:

- ``microsoft_graph``     — sends through the connected Outlook OAuth session (delegated)
- ``microsoft_graph_app`` — sends app-only via client credentials, no sign-in required, so it
                            works for a mailbox in a different tenant than AZURE_TENANT_ID
- ``gmail``               — sends over SMTP using a Google app password

App-only entries inherit MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET / MICROSOFT_TENANT_ID
from config, and may override any of them per mailbox with "client_id" / "client_secret" /
"tenant_id" when several mailboxes live in different tenants.

The registry is read-only config; nothing here touches the database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.config import get_settings
from app.services.graph_app_client import AppGraphCredentials, app_credentials_from_settings

PROVIDER_GRAPH = "microsoft_graph"
PROVIDER_GRAPH_APP = "microsoft_graph_app"
PROVIDER_GMAIL = "gmail"
SUPPORTED_PROVIDERS = (PROVIDER_GRAPH, PROVIDER_GRAPH_APP, PROVIDER_GMAIL)


class MailboxConfigError(Exception):
    pass


@dataclass(frozen=True)
class Mailbox:
    id: str
    label: str
    provider: str
    from_email: str
    from_name: str | None = None
    gmail_app_password: str | None = field(default=None, repr=False)
    # App-only Graph overrides; when unset the MICROSOFT_* settings are used.
    client_id: str | None = None
    tenant_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)

    @property
    def app_credentials(self) -> AppGraphCredentials:
        """App-only credentials for this mailbox: per-entry overrides over the global ones."""
        base = app_credentials_from_settings()
        return AppGraphCredentials(
            client_id=self.client_id or base.client_id,
            tenant_id=self.tenant_id or base.tenant_id,
            client_secret=self.client_secret or base.client_secret,
        )

    @property
    def can_send(self) -> bool:
        if self.provider == PROVIDER_GMAIL:
            return bool(self.gmail_app_password)
        if self.provider == PROVIDER_GRAPH_APP:
            return self.app_credentials.is_configured
        return True

    @property
    def auth_hint(self) -> str:
        """Short, non-secret explanation of what this mailbox needs to be able to send."""
        if self.provider == PROVIDER_GMAIL:
            return "Gmail SMTP app password" if self.can_send else "Missing gmail_app_password"
        if self.provider == PROVIDER_GRAPH_APP:
            if self.can_send:
                return f"Microsoft app-only (tenant {self.app_credentials.tenant_id})"
            return "Missing MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET / MICROSOFT_TENANT_ID"
        return "Uses the connected Outlook sign-in"

    def public_dict(self) -> dict:
        """Serializable view with no secrets — safe to return over the API."""
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "from_email": self.from_email,
            "from_name": self.from_name,
            "can_send": self.can_send,
            "auth_hint": self.auth_hint,
        }


def _parse_entry(entry: dict, index: int) -> Mailbox:
    if not isinstance(entry, dict):
        raise MailboxConfigError(f"OUTREACH_MAILBOXES[{index}] must be an object")

    missing = [key for key in ("id", "provider", "from_email") if not entry.get(key)]
    if missing:
        raise MailboxConfigError(
            f"OUTREACH_MAILBOXES[{index}] is missing required field(s): {', '.join(missing)}"
        )

    provider = str(entry["provider"]).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise MailboxConfigError(
            f"OUTREACH_MAILBOXES[{index}] has unsupported provider {provider!r}. "
            f"Expected one of: {', '.join(SUPPORTED_PROVIDERS)}"
        )

    from_email = str(entry["from_email"]).strip().lower()
    return Mailbox(
        id=str(entry["id"]).strip(),
        label=str(entry.get("label") or from_email).strip(),
        provider=provider,
        from_email=from_email,
        from_name=(str(entry["from_name"]).strip() if entry.get("from_name") else None),
        gmail_app_password=(
            str(entry["gmail_app_password"]).replace(" ", "") if entry.get("gmail_app_password") else None
        ),
        client_id=(str(entry["client_id"]).strip() if entry.get("client_id") else None),
        tenant_id=(str(entry["tenant_id"]).strip() if entry.get("tenant_id") else None),
        client_secret=(str(entry["client_secret"]).strip() if entry.get("client_secret") else None),
    )


def load_mailboxes() -> list[Mailbox]:
    """Parse OUTREACH_MAILBOXES. Returns [] when unset so existing flows keep working."""
    raw = (get_settings().outreach_mailboxes or "").strip()
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MailboxConfigError(f"OUTREACH_MAILBOXES is not valid JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise MailboxConfigError("OUTREACH_MAILBOXES must be a JSON array")

    mailboxes = [_parse_entry(entry, i) for i, entry in enumerate(parsed)]

    seen: set[str] = set()
    for mailbox in mailboxes:
        if mailbox.id in seen:
            raise MailboxConfigError(f"OUTREACH_MAILBOXES has duplicate id {mailbox.id!r}")
        seen.add(mailbox.id)

    return mailboxes


def get_mailbox(mailbox_id: str) -> Mailbox:
    for mailbox in load_mailboxes():
        if mailbox.id == mailbox_id:
            return mailbox
    known = ", ".join(m.id for m in load_mailboxes()) or "(none configured)"
    raise MailboxConfigError(f"Unknown mailbox {mailbox_id!r}. Configured mailboxes: {known}")


def default_mailbox() -> Mailbox | None:
    """First sendable mailbox, used when a caller does not name one."""
    for mailbox in load_mailboxes():
        if mailbox.can_send:
            return mailbox
    return None
