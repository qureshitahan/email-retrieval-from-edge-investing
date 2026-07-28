"""App-only (client credentials) Microsoft Graph sending.

The delegated flow in ``graph_client.py`` can only act as whoever signed in, and it signs in
against a single tenant (``AZURE_TENANT_ID``). That makes it structurally unable to send as a
mailbox that lives in a different Microsoft 365 tenant - the sign-in itself is refused with
AADSTS50020 before any Graph call happens.

This module covers that case. With the ``Mail.Send`` *application* permission and admin consent,
an app registration in the mailbox's own tenant can send as that mailbox with no interactive
sign-in at all:

    POST /users/{mailbox}/sendMail    Authorization: Bearer <app-only token>

App-only tokens carry granted permissions in the ``roles`` claim (delegated tokens use ``scp``),
so ``parse_token_roles`` is used to check ``Mail.Send`` before attempting a send. MSAL caches
tokens per application instance, and instances are cached here per (client_id, tenant_id), so
repeated sends reuse a token until it expires.

Nothing in this module reads or writes the database - app-only credentials come from config.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import httpx
import msal
import requests

from app.config import get_settings

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
REQUIRED_SEND_ROLE = "Mail.Send"


class GraphAppAuthError(Exception):
    pass


@dataclass(frozen=True)
class AppGraphCredentials:
    client_id: str
    tenant_id: str
    client_secret: str = field(repr=False, default="")

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.tenant_id and self.client_secret)

    @property
    def cache_key(self) -> tuple[str, str]:
        return (self.client_id, self.tenant_id)


def app_credentials_from_settings() -> AppGraphCredentials:
    settings = get_settings()
    return AppGraphCredentials(
        client_id=(settings.microsoft_client_id or "").strip(),
        tenant_id=(settings.microsoft_tenant_id or "").strip(),
        client_secret=(settings.microsoft_client_secret or "").strip(),
    )


def parse_token_roles(access_token: str) -> list[str]:
    """Application permissions granted to an app-only token (the ``roles`` claim)."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        roles = data.get("roles") or []
        if isinstance(roles, str):
            return [r for r in roles.split() if r]
        return [str(r) for r in roles]
    except Exception:
        return []


def _msal_http_client() -> requests.Session:
    """Bypass system proxy settings that can block login.microsoftonline.com."""
    session = requests.Session()
    session.trust_env = False
    return session


# Reused so MSAL's own token cache survives between sends.
_APP_CACHE: dict[tuple[str, str], msal.ConfidentialClientApplication] = {}


def _confidential_app(creds: AppGraphCredentials) -> msal.ConfidentialClientApplication:
    cached = _APP_CACHE.get(creds.cache_key)
    if cached is not None:
        return cached
    try:
        app = msal.ConfidentialClientApplication(
            creds.client_id,
            authority=f"https://login.microsoftonline.com/{creds.tenant_id}",
            client_credential=creds.client_secret,
            http_client=_msal_http_client(),
        )
    except requests.exceptions.RequestException as exc:
        raise GraphAppAuthError(
            "Could not reach Microsoft login service. Check internet access and disable "
            "VPN/proxy if enabled."
        ) from exc
    _APP_CACHE[creds.cache_key] = app
    return app


def _explain_token_error(creds: AppGraphCredentials, result: dict) -> str:
    description = str(result.get("error_description") or result.get("error") or "unknown error")
    if "AADSTS7000215" in description:
        return (
            f"Microsoft rejected the client secret for app {creds.client_id}. The secret is wrong "
            "or expired - create a new one under App registrations > Certificates & secrets and "
            "update MICROSOFT_CLIENT_SECRET."
        )
    if "AADSTS700016" in description or "AADSTS700027" in description:
        return (
            f"App {creds.client_id} was not found in tenant {creds.tenant_id}. Confirm "
            "MICROSOFT_CLIENT_ID and MICROSOFT_TENANT_ID belong to the same app registration."
        )
    if "AADSTS90002" in description or "AADSTS900023" in description:
        return (
            f"Tenant {creds.tenant_id} was not found. Check MICROSOFT_TENANT_ID against "
            "Microsoft Entra ID > Overview > Tenant ID."
        )
    return f"App-only token request failed for tenant {creds.tenant_id}: {description[:400]}"


def acquire_app_token(creds: AppGraphCredentials) -> str:
    """Client-credentials access token for Graph. Raises GraphAppAuthError with guidance."""
    if not creds.is_configured:
        raise GraphAppAuthError(
            "App-only Microsoft credentials are not configured. Set MICROSOFT_CLIENT_ID, "
            "MICROSOFT_CLIENT_SECRET and MICROSOFT_TENANT_ID in .env."
        )

    app = _confidential_app(creds)
    try:
        result = app.acquire_token_for_client(scopes=[GRAPH_DEFAULT_SCOPE])
    except requests.exceptions.RequestException as exc:
        raise GraphAppAuthError(
            "Could not reach Microsoft login service while requesting an app-only token. "
            f"Check network egress to login.microsoftonline.com: {exc}"
        ) from exc

    if not result or "access_token" not in result:
        raise GraphAppAuthError(_explain_token_error(creds, result or {}))
    return str(result["access_token"])


def check_send_permission(access_token: str, creds: AppGraphCredentials) -> None:
    """Fail early with a clear message when Mail.Send has not been consented."""
    roles = parse_token_roles(access_token)
    if REQUIRED_SEND_ROLE in roles:
        return
    granted = ", ".join(roles) or "(none)"
    raise GraphAppAuthError(
        f"App {creds.client_id} in tenant {creds.tenant_id} has no {REQUIRED_SEND_ROLE} "
        f"application permission (granted: {granted}). In Azure: App registrations > API "
        "permissions > Add a permission > Microsoft Graph > Application permissions > "
        f"{REQUIRED_SEND_ROLE}, then click 'Grant admin consent'."
    )


def _explain_send_error(
    creds: AppGraphCredentials, from_email: str, status_code: int, text: str
) -> str:
    body = text[:400]
    if status_code in (401, 403):
        return (
            f"Graph refused an app-only send as {from_email} ({status_code}). Grant the "
            f"{REQUIRED_SEND_ROLE} application permission to app {creds.client_id} in tenant "
            f"{creds.tenant_id} and click 'Grant admin consent'. If an application access policy "
            f"is in place, make sure it allows this app to access {from_email}. Details: {body}"
        )
    if status_code == 404:
        return (
            f"Graph could not find mailbox {from_email} in tenant {creds.tenant_id}. Confirm the "
            "address is a licensed Exchange Online mailbox in that tenant (not an alias or a "
            f"mail-enabled group). Details: {body}"
        )
    return f"Graph sendMail failed for {from_email} ({status_code}): {body}"


async def send_mail_as(
    creds: AppGraphCredentials,
    *,
    from_email: str,
    from_name: str | None,
    to_email: str,
    to_name: str | None,
    subject: str,
    body: str,
    content_type: str = "Text",
) -> None:
    """Send as ``from_email`` using app-only credentials. No user sign-in involved."""
    access_token = acquire_app_token(creds)
    check_send_permission(access_token, creds)

    recipient: dict = {"emailAddress": {"address": to_email}}
    if to_name:
        recipient["emailAddress"]["name"] = to_name

    sender: dict = {"emailAddress": {"address": from_email}}
    if from_name:
        sender["emailAddress"]["name"] = from_name

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": content_type, "content": body},
            "toRecipients": [recipient],
            "from": sender,
        },
        "saveToSentItems": True,
    }
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    endpoint = f"{GRAPH_BASE}/users/{from_email}/sendMail"

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        response = await client.post(endpoint, json=payload, headers=headers)

    if response.status_code >= 400:
        raise GraphAppAuthError(
            _explain_send_error(creds, from_email, response.status_code, response.text)
        )


async def verify_mailbox_access(creds: AppGraphCredentials, from_email: str) -> dict:
    """Read-only readiness check: token, Mail.Send role, and mailbox reachability.

    Sends nothing. Probes the mailbox through ``/mailFolders/inbox`` rather than the directory
    endpoint ``/users/{id}``: the mail endpoint is covered by the ``Mail.Read`` application
    permission this app already holds, whereas the directory endpoint needs ``User.Read.All``
    and returns Authorization_RequestDenied without it - which says nothing about whether
    sending works. The probe is informational; ``ready`` turns on ``Mail.Send`` alone, since
    that is the only permission ``sendMail`` actually requires.
    """
    access_token = acquire_app_token(creds)
    roles = parse_token_roles(access_token)
    has_mail_send = REQUIRED_SEND_ROLE in roles

    probe_url = f"{GRAPH_BASE}/users/{from_email}/mailFolders/inbox?$select=displayName,totalItemCount"
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.get(probe_url, headers={"Authorization": f"Bearer {access_token}"})

    reachable = response.status_code == 200
    return {
        "token_acquired": True,
        "roles": roles,
        "has_mail_send": has_mail_send,
        "ready": has_mail_send,
        "mailbox_probe_status": response.status_code,
        "mailbox_reachable": reachable,
        "mailbox": response.json() if reachable else None,
        "mailbox_error": None if reachable else response.text[:300],
    }
