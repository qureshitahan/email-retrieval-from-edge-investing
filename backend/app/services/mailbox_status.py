"""Live readiness of each configured mailbox.

The UI needs to show three mailboxes as *already connected* rather than asking the user to run an
OAuth ceremony. Whether that is true differs by transport:

- ``microsoft_graph_app`` - ask Microsoft for an app-only token and read the ``roles`` claim.
  Nothing to connect: if ``Mail.Send`` is consented, the mailbox is ready permanently.
- ``gmail`` - ready as soon as an app password is configured. We deliberately do NOT run an SMTP
  login here; this runs on every page load and Google throttles repeated auth attempts.
- ``microsoft_graph`` - delegated, so it depends on a stored sign-in. This is the only transport
  that can report ``needs_signin``.

Results are cached briefly so repeated page loads do not re-hit login.microsoftonline.com. MSAL
also caches the token itself, so a cache miss is usually cheap.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.models.sync import AuthToken
from app.services.graph_app_client import (
    GraphAppAuthError,
    acquire_app_token,
    parse_token_roles,
)
from app.services.graph_client import parse_token_scopes
from app.services.mailboxes import (
    PROVIDER_GMAIL,
    PROVIDER_GRAPH,
    PROVIDER_GRAPH_APP,
    Mailbox,
    MailboxConfigError,
    load_mailboxes,
)

CACHE_TTL_SECONDS = 60

# mailbox id -> (checked_at, payload)
_CACHE: dict[str, tuple[float, dict]] = {}

STATUS_READY = "ready"
STATUS_NEEDS_SIGNIN = "needs_signin"
STATUS_NEEDS_CONSENT = "needs_consent"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_ERROR = "error"


def clear_cache() -> None:
    _CACHE.clear()


def _app_only_status(mailbox: Mailbox) -> dict:
    creds = mailbox.app_credentials
    if not creds.is_configured:
        return {
            "status": STATUS_NOT_CONFIGURED,
            "can_send": False,
            "can_read": False,
            "detail": (
                "App-only credentials are missing. Set MICROSOFT_CLIENT_ID, "
                "MICROSOFT_CLIENT_SECRET and MICROSOFT_TENANT_ID."
            ),
        }

    try:
        roles = parse_token_roles(acquire_app_token(creds))
    except GraphAppAuthError as exc:
        return {"status": STATUS_ERROR, "can_send": False, "can_read": False, "detail": str(exc)}

    can_send = "Mail.Send" in roles
    can_read = "Mail.Read" in roles
    if can_send:
        return {
            "status": STATUS_READY,
            "can_send": True,
            "can_read": can_read,
            "detail": f"Connected app-only in tenant {creds.tenant_id}. No sign-in needed.",
        }
    return {
        "status": STATUS_NEEDS_CONSENT,
        "can_send": False,
        "can_read": can_read,
        "detail": (
            f"App {creds.client_id} has no Mail.Send application permission "
            f"(granted: {', '.join(roles) or 'none'}). In Azure: App registrations > API "
            "permissions > Microsoft Graph > Application permissions > Mail.Send, then "
            "'Grant admin consent'."
        ),
    }


def _gmail_status(mailbox: Mailbox) -> dict:
    if not mailbox.gmail_app_password:
        return {
            "status": STATUS_NOT_CONFIGURED,
            "can_send": False,
            "can_read": False,
            "detail": "No gmail_app_password set for this mailbox in OUTREACH_MAILBOXES.",
        }
    return {
        "status": STATUS_READY,
        "can_send": True,
        # One app password covers both transports: SMTP for sending, IMAP for reading. We do not
        # log in here to confirm - Google throttles repeated auth attempts and this runs on every
        # page load - so a disabled-IMAP account surfaces its error at sync time instead.
        "can_read": True,
        "detail": "Connected via Gmail app password (SMTP send, IMAP read). No sign-in needed.",
    }


def _delegated_status(db: Session, mailbox: Mailbox) -> dict:
    token_row = db.query(AuthToken).order_by(AuthToken.updated_at.desc()).first()
    if token_row is None:
        return {
            "status": STATUS_NEEDS_SIGNIN,
            "can_send": False,
            "can_read": False,
            "detail": (
                "This app registration only has DELEGATED Graph permissions, so it needs one "
                "interactive sign-in and none is stored yet. Click the sign-in button above. To "
                "remove the sign-in step permanently, add the Mail.Read + Mail.Send APPLICATION "
                "permissions in Azure, grant admin consent, then set this mailbox's provider to "
                "microsoft_graph_app."
            ),
        }

    scopes = parse_token_scopes(token_row.access_token or "")
    signed_in = (token_row.user_email or "").strip().lower()
    matches = not signed_in or signed_in == mailbox.from_email
    return {
        "status": STATUS_READY if "Mail.Send" in scopes else STATUS_NEEDS_SIGNIN,
        "can_send": "Mail.Send" in scopes,
        "can_read": "Mail.Read" in scopes,
        "detail": (
            f"Signed in as {signed_in or 'unknown'}."
            + ("" if matches else f" Sending as {mailbox.from_email} needs Send-As rights.")
            + ("" if "Mail.Send" in scopes else " Mail.Send is missing from the session.")
        ),
    }


def mailbox_status(db: Session, mailbox: Mailbox, *, use_cache: bool = True) -> dict:
    cached = _CACHE.get(mailbox.id) if use_cache else None
    if cached and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    if mailbox.provider == PROVIDER_GRAPH_APP:
        result = _app_only_status(mailbox)
    elif mailbox.provider == PROVIDER_GMAIL:
        result = _gmail_status(mailbox)
    elif mailbox.provider == PROVIDER_GRAPH:
        result = _delegated_status(db, mailbox)
    else:
        result = {
            "status": STATUS_ERROR,
            "can_send": False,
            "can_read": False,
            "detail": f"Unsupported provider {mailbox.provider!r}",
        }

    payload = {
        **mailbox.public_dict(),
        **result,
        # True when the mailbox needs no user action at all - what the UI calls "connected".
        "connected": result["status"] == STATUS_READY,
        "needs_signin": result["status"] == STATUS_NEEDS_SIGNIN,
        "requires_interactive_auth": mailbox.provider == PROVIDER_GRAPH,
    }
    _CACHE[mailbox.id] = (time.monotonic(), payload)
    return payload


def all_mailbox_statuses(db: Session, *, use_cache: bool = True) -> dict:
    """Every configured mailbox with live readiness. Never raises on config errors."""
    try:
        mailboxes = load_mailboxes()
    except MailboxConfigError as exc:
        return {"items": [], "config_error": str(exc)}

    items = [mailbox_status(db, m, use_cache=use_cache) for m in mailboxes]
    return {
        "items": items,
        "config_error": None,
        "sendable": [i["id"] for i in items if i["can_send"]],
    }
