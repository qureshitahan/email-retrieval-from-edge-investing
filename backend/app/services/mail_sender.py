"""Provider-aware outbound send.

Dispatches a message to the right transport for a configured mailbox:

- ``gmail``               → SMTP over STARTTLS using the mailbox's app password
- ``microsoft_graph``     → Graph sendMail using the connected (delegated) OAuth session
- ``microsoft_graph_app`` → Graph sendMail using app-only client credentials, no sign-in

Delegated Graph note: a delegated token can only ``/me/sendMail`` as the signed-in user. When
the requested mailbox is a *different* address, we fall back to ``/users/{address}/sendMail``,
which needs Send-As rights on that mailbox. If those rights are absent, Graph returns 403 and we
surface an actionable message rather than a bare HTTP error. A mailbox in a different tenant
cannot be reached this way at all — use ``microsoft_graph_app`` for those.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

import httpx
from sqlalchemy.orm import Session

from app.services.graph_app_client import GraphAppAuthError, send_mail_as
from app.services.graph_client import GRAPH_BASE, GraphAuthError, GraphClient, parse_token_scopes
from app.services.mailboxes import PROVIDER_GMAIL, PROVIDER_GRAPH, PROVIDER_GRAPH_APP, Mailbox

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
SMTP_TIMEOUT_SECONDS = 60


class MailSendError(Exception):
    pass


def _format_sender(mailbox: Mailbox) -> str:
    if mailbox.from_name:
        return formataddr((mailbox.from_name, mailbox.from_email))
    return mailbox.from_email


def _send_gmail_blocking(mailbox: Mailbox, *, to_email: str, to_name: str | None, subject: str, body: str) -> None:
    if not mailbox.gmail_app_password:
        raise MailSendError(
            f"Mailbox {mailbox.id!r} ({mailbox.from_email}) has no gmail_app_password configured. "
            "Add one to OUTREACH_MAILBOXES in .env."
        )

    message = EmailMessage()
    message["From"] = _format_sender(mailbox)
    message["To"] = formataddr((to_name, to_email)) if to_name else to_email
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            smtp.login(mailbox.from_email, mailbox.gmail_app_password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailSendError(
            f"Gmail rejected the app password for {mailbox.from_email}. Confirm the account has "
            "2-Step Verification on and regenerate the app password at "
            f"https://myaccount.google.com/apppasswords ({exc.smtp_code})."
        ) from exc
    except smtplib.SMTPException as exc:
        raise MailSendError(f"Gmail SMTP send failed for {mailbox.from_email}: {exc}") from exc
    except OSError as exc:
        raise MailSendError(
            f"Could not reach {GMAIL_SMTP_HOST}:{GMAIL_SMTP_PORT} for {mailbox.from_email}. "
            f"Check network/firewall egress on port {GMAIL_SMTP_PORT}: {exc}"
        ) from exc


async def _send_gmail(mailbox: Mailbox, *, to_email: str, to_name: str | None, subject: str, body: str) -> None:
    # smtplib is blocking — keep the event loop free.
    await asyncio.to_thread(
        _send_gmail_blocking,
        mailbox,
        to_email=to_email,
        to_name=to_name,
        subject=subject,
        body=body,
    )


async def _send_graph(
    db: Session,
    mailbox: Mailbox,
    *,
    to_email: str,
    to_name: str | None,
    subject: str,
    body: str,
) -> None:
    client = GraphClient(db)
    try:
        access_token = client.ensure_access_token()
    except GraphAuthError as exc:
        raise MailSendError(str(exc)) from exc

    if "Mail.Send" not in parse_token_scopes(access_token):
        raise MailSendError(
            "The connected Outlook session does not include Mail.Send. Click Reconnect Outlook "
            "and accept all permissions."
        )

    token_row = client.get_token_row()
    signed_in = (token_row.user_email or "").strip().lower() if token_row else ""
    sends_as_self = signed_in == mailbox.from_email

    endpoint = f"{GRAPH_BASE}/me/sendMail" if sends_as_self else f"{GRAPH_BASE}/users/{mailbox.from_email}/sendMail"

    recipient: dict = {"emailAddress": {"address": to_email}}
    if to_name:
        recipient["emailAddress"]["name"] = to_name

    message: dict = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [recipient],
    }
    if not sends_as_self:
        # Ask Graph to stamp the intended identity; requires Send-As on that mailbox.
        message["from"] = {"emailAddress": {"address": mailbox.from_email}}

    payload = {"message": message, "saveToSentItems": True}
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as http:
        response = await http.post(endpoint, json=payload, headers=headers)

    if response.status_code in (401, 403):
        if sends_as_self:
            raise MailSendError(
                "Outlook refused the send. Reconnect Outlook after granting Mail.Send in Azure AD."
            )
        raise MailSendError(
            f"Signed in as {signed_in or 'an unknown account'} but mailbox {mailbox.id!r} sends as "
            f"{mailbox.from_email}. Either sign in as {mailbox.from_email}, or grant that account "
            "Send-As rights to the signed-in user in Exchange admin."
        )
    if response.status_code >= 400:
        raise MailSendError(
            f"Graph sendMail failed for {mailbox.from_email} ({response.status_code}): {response.text[:400]}"
        )


async def _send_graph_app(
    mailbox: Mailbox,
    *,
    to_email: str,
    to_name: str | None,
    subject: str,
    body: str,
) -> None:
    try:
        await send_mail_as(
            mailbox.app_credentials,
            from_email=mailbox.from_email,
            from_name=mailbox.from_name,
            to_email=to_email,
            to_name=to_name,
            subject=subject,
            body=body,
        )
    except GraphAppAuthError as exc:
        raise MailSendError(str(exc)) from exc


async def send_via_mailbox(
    db: Session,
    mailbox: Mailbox,
    *,
    to_email: str,
    to_name: str | None,
    subject: str,
    body: str,
) -> None:
    """Send one message as ``mailbox``. Raises MailSendError with an actionable message."""
    if not subject or not body:
        raise MailSendError("Cannot send: subject and body are both required")

    if mailbox.provider == PROVIDER_GMAIL:
        await _send_gmail(mailbox, to_email=to_email, to_name=to_name, subject=subject, body=body)
    elif mailbox.provider == PROVIDER_GRAPH_APP:
        await _send_graph_app(mailbox, to_email=to_email, to_name=to_name, subject=subject, body=body)
    elif mailbox.provider == PROVIDER_GRAPH:
        await _send_graph(db, mailbox, to_email=to_email, to_name=to_name, subject=subject, body=body)
    else:
        raise MailSendError(f"Mailbox {mailbox.id!r} has unsupported provider {mailbox.provider!r}")
