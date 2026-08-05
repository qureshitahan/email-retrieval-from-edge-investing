"""Per-mailbox inbox/sent readers.

The original sync could only read the signed-in user's own mail (``/me/...``). To reach three
mailboxes on three different transports, each mailbox gets a reader exposing the same two calls:

    fetch_sent_page(cursor)  -> (items, next_cursor)
    fetch_inbox_page(cursor) -> (items, next_cursor)

Every reader yields items in **Microsoft Graph message shape**, because ``upsert_message`` already
parses that shape. The Gmail reader therefore translates IMAP/RFC-822 messages into the same dict
rather than the sync service learning a second format. ``cursor`` is opaque: a Graph
``@odata.nextLink`` URL for Graph readers, an integer offset for IMAP. ``None`` means "start", and a
``None`` next_cursor means "no more pages".

Transport notes:
- delegated Graph : reads ``/me/...`` with the stored interactive sign-in. One mailbox only.
- app-only Graph  : reads ``/users/{address}/...``; needs the Mail.Read application permission.
- Gmail IMAP      : same app password used for SMTP sending; needs IMAP enabled on the account.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from app.services.graph_app_client import (
    GRAPH_BASE,
    GraphAppAuthError,
    acquire_app_token,
    parse_token_roles,
)
from app.services.graph_client import (
    INBOX_MESSAGE_SELECT,
    MESSAGE_SELECT,
    GraphAuthError,
    GraphClient,
)
from app.services.mailboxes import (
    PROVIDER_GMAIL,
    PROVIDER_GRAPH,
    PROVIDER_GRAPH_APP,
    Mailbox,
    get_mailbox,
)
from app.services.text_utils import strip_html

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993
IMAP_PAGE_SIZE = 100
IMAP_TIMEOUT_SECONDS = 60
PREVIEW_CHARS = 2000

# Gmail exposes sent mail under a localised special-use folder; \Sent is the reliable selector.
GMAIL_SENT_CANDIDATES = ("[Gmail]/Sent Mail", "[Google Mail]/Sent Mail", "Sent")


class MailReadError(Exception):
    pass


class MailReader(Protocol):
    mailbox: Mailbox

    async def fetch_sent_page(self, cursor: str | None) -> tuple[list[dict], str | None]: ...

    async def fetch_inbox_page(self, cursor: str | None) -> tuple[list[dict], str | None]: ...


# --------------------------------------------------------------------------------------
# Microsoft Graph - delegated (the interactive sign-in)
# --------------------------------------------------------------------------------------


class DelegatedGraphReader:
    """Reads the signed-in user's own mailbox. Requires a stored OAuth session."""

    def __init__(self, db: Session, mailbox: Mailbox):
        self.mailbox = mailbox
        self.client = GraphClient(db)

    async def fetch_sent_page(self, cursor: str | None) -> tuple[list[dict], str | None]:
        try:
            page = await self.client.fetch_messages_page(cursor)
        except GraphAuthError as exc:
            raise MailReadError(
                f"{self.mailbox.from_email}: {exc} Sign in to this mailbox from the app first."
            ) from exc
        return page.get("value", []), page.get("@odata.nextLink")

    async def fetch_inbox_page(self, cursor: str | None) -> tuple[list[dict], str | None]:
        try:
            page = await self.client.fetch_inbox_page(cursor)
        except GraphAuthError as exc:
            raise MailReadError(
                f"{self.mailbox.from_email}: {exc} Sign in to this mailbox from the app first."
            ) from exc
        return page.get("value", []), page.get("@odata.nextLink")


# --------------------------------------------------------------------------------------
# Microsoft Graph - app-only (no sign-in)
# --------------------------------------------------------------------------------------


class AppGraphReader:
    """Reads /users/{address}/... with client-credentials. No interactive sign-in."""

    def __init__(self, mailbox: Mailbox):
        self.mailbox = mailbox
        self.creds = mailbox.app_credentials

    def _token(self) -> str:
        try:
            token = acquire_app_token(self.creds)
        except GraphAppAuthError as exc:
            raise MailReadError(f"{self.mailbox.from_email}: {exc}") from exc
        if "Mail.Read" not in parse_token_roles(token):
            raise MailReadError(
                f"{self.mailbox.from_email}: app {self.creds.client_id} lacks the Mail.Read "
                "application permission. In Azure: App registrations > API permissions > "
                "Microsoft Graph > Application permissions > Mail.Read, then 'Grant admin consent'."
            )
        return token

    async def _page(self, url: str) -> tuple[list[dict], str | None]:
        headers = {"Authorization": f"Bearer {self._token()}"}
        for _ in range(5):
            async with httpx.AsyncClient(timeout=120.0, trust_env=False) as http:
                response = await http.get(url, headers=headers)
                if response.status_code == 429:
                    await asyncio.sleep(int(response.headers.get("Retry-After", "5")))
                    continue
                if response.status_code >= 400:
                    raise MailReadError(
                        f"{self.mailbox.from_email}: Graph read failed "
                        f"({response.status_code}): {response.text[:300]}"
                    )
                payload = response.json()
                return payload.get("value", []), payload.get("@odata.nextLink")
        raise MailReadError(f"{self.mailbox.from_email}: Graph rate limit exceeded after retries")

    # Newest first, deliberately. Ascending order means a 14,000-message mailbox spends its
    # first several minutes importing the oldest mail in the account, so the contacts you
    # actually care about appear last. Descending puts this month's correspondence on screen
    # within seconds, and a sync that is interrupted has still delivered the useful part.
    async def fetch_sent_page(self, cursor: str | None) -> tuple[list[dict], str | None]:
        url = cursor or (
            f"{GRAPH_BASE}/users/{self.mailbox.from_email}/mailFolders/sentitems/messages"
            f"?$select={MESSAGE_SELECT}&$orderby=sentDateTime desc&$top=100"
        )
        return await self._page(url)

    async def fetch_inbox_page(self, cursor: str | None) -> tuple[list[dict], str | None]:
        url = cursor or (
            f"{GRAPH_BASE}/users/{self.mailbox.from_email}/mailFolders/inbox/messages"
            f"?$select={INBOX_MESSAGE_SELECT}&$orderby=receivedDateTime desc&$top=100"
        )
        return await self._page(url)


# --------------------------------------------------------------------------------------
# Gmail over IMAP
# --------------------------------------------------------------------------------------


def _decode(value: str | None) -> str:
    """RFC 2047 header -> plain text, never raising on malformed input."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _addresses(message: Message, header: str) -> list[dict]:
    raw = message.get_all(header) or []
    out: list[dict] = []
    for name, address in getaddresses(raw):
        if address:
            out.append({"emailAddress": {"name": _decode(name) or None, "address": address}})
    return out


def _body_preview(message: Message) -> str:
    """First readable text/plain part, falling back to stripped HTML."""
    candidates: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() != "text":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if part.get_content_subtype() == "plain":
                return text[:PREVIEW_CHARS]
            candidates.append(text)
    else:
        try:
            payload = message.get_payload(decode=True)
        except Exception:
            payload = None
        if payload:
            charset = message.get_content_charset() or "utf-8"
            candidates.append(payload.decode(charset, errors="replace"))

    if not candidates:
        return ""
    import re

    stripped = re.sub(r"<[^>]+>", " ", candidates[0])
    return re.sub(r"\s+", " ", stripped).strip()[:PREVIEW_CHARS]


def _has_attachments(message: Message) -> bool:
    if not message.is_multipart():
        return False
    return any(part.get_content_disposition() == "attachment" for part in message.walk())


def _to_graph_shape(mailbox: Mailbox, raw: bytes, *, inbound: bool) -> dict | None:
    """Translate one RFC-822 message into the Graph dict shape upsert_message expects."""
    try:
        message = email.message_from_bytes(raw)
    except Exception:
        return None

    message_id = (message.get("Message-ID") or "").strip()
    if not message_id:
        return None

    try:
        sent = parsedate_to_datetime(message.get("Date"))
    except Exception:
        return None
    if sent is None:
        return None
    iso = sent.isoformat()

    senders = _addresses(message, "From")
    sender = senders[0] if senders else {"emailAddress": {"address": mailbox.from_email}}

    # Gmail threads by References/In-Reply-To; the first reference is the thread root and so
    # makes a stable conversation key. Both headers are absent on a thread-starting message,
    # in which case the message is its own conversation.
    references = (message.get("References") or "").split()
    conversation_id = (
        references[0].strip()
        if references
        else (message.get("In-Reply-To") or "").strip() or message_id
    )

    item: dict = {
        # Namespaced so an IMAP id can never collide with a Graph id in the unique column.
        "id": f"imap:{mailbox.id}:{message_id}",
        "internetMessageId": message_id,
        "conversationId": conversation_id,
        "subject": _decode(message.get("Subject")),
        "bodyPreview": _body_preview(message),
        "webLink": None,
        "hasAttachments": _has_attachments(message),
        "importance": "normal",
        "categories": [],
        "from": sender,
        "sender": sender,
        "toRecipients": _addresses(message, "To"),
        "ccRecipients": _addresses(message, "Cc"),
        "bccRecipients": _addresses(message, "Bcc"),
    }
    if inbound:
        item["receivedDateTime"] = iso
    item["sentDateTime"] = iso
    return item


class GmailImapReader:
    """Reads a Gmail mailbox over IMAP using the same app password used for SMTP sending."""

    def __init__(self, mailbox: Mailbox):
        self.mailbox = mailbox

    def _connect(self) -> imaplib.IMAP4_SSL:
        if not self.mailbox.gmail_app_password:
            raise MailReadError(
                f"{self.mailbox.from_email} has no gmail_app_password configured in "
                "OUTREACH_MAILBOXES."
            )
        try:
            imap = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, timeout=IMAP_TIMEOUT_SECONDS)
            imap.login(self.mailbox.from_email, self.mailbox.gmail_app_password)
            return imap
        except imaplib.IMAP4.error as exc:
            raise MailReadError(
                f"Gmail refused IMAP for {self.mailbox.from_email}: {exc}. Enable IMAP under "
                "Gmail > Settings > Forwarding and POP/IMAP, and confirm the app password is valid."
            ) from exc
        except OSError as exc:
            raise MailReadError(
                f"Could not reach {GMAIL_IMAP_HOST}:{GMAIL_IMAP_PORT} for "
                f"{self.mailbox.from_email}: {exc}"
            ) from exc

    def _select_folder(self, imap: imaplib.IMAP4_SSL, *, sent: bool) -> int:
        names = GMAIL_SENT_CANDIDATES if sent else ("INBOX",)
        last_error = ""
        for name in names:
            status, data = imap.select(f'"{name}"', readonly=True)
            if status == "OK":
                return int(data[0]) if data and data[0] else 0
            last_error = f"{name}: {data}"
        raise MailReadError(
            f"{self.mailbox.from_email}: could not open "
            f"{'Sent Mail' if sent else 'INBOX'} ({last_error})"
        )

    def _fetch_blocking(self, offset: int, *, sent: bool) -> tuple[list[dict], str | None]:
        imap = self._connect()
        try:
            total = self._select_folder(imap, sent=sent)
            if total == 0 or offset >= total:
                return [], None

            # Newest first, matching the Graph readers. IMAP sequence numbers run 1..total
            # with the newest last, so walk down from the top: offset counts how many of the
            # newest messages have already been consumed.
            end = total - offset
            start = max(1, end - IMAP_PAGE_SIZE + 1)
            if end < 1:
                return [], None
            status, data = imap.fetch(f"{start}:{end}", "(RFC822)")
            if status != "OK":
                raise MailReadError(f"{self.mailbox.from_email}: IMAP fetch failed ({status})")

            items: list[dict] = []
            for entry in data or []:
                # imaplib returns alternating tuples and closing-paren bytes.
                if not isinstance(entry, tuple) or len(entry) < 2:
                    continue
                shaped = _to_graph_shape(self.mailbox, entry[1], inbound=not sent)
                if shaped:
                    items.append(shaped)
            # Server returns the range ascending; flip so the caller sees newest first too.
            items.reverse()

            consumed = offset + (end - start + 1)
            next_cursor = str(consumed) if consumed < total else None
            return items, next_cursor
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    async def _fetch(self, cursor: str | None, *, sent: bool) -> tuple[list[dict], str | None]:
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        # imaplib is blocking - keep the event loop free.
        return await asyncio.to_thread(self._fetch_blocking, offset, sent=sent)

    async def fetch_sent_page(self, cursor: str | None) -> tuple[list[dict], str | None]:
        return await self._fetch(cursor, sent=True)

    async def fetch_inbox_page(self, cursor: str | None) -> tuple[list[dict], str | None]:
        return await self._fetch(cursor, sent=False)


# --------------------------------------------------------------------------------------


async def fetch_full_body(db: Session, message) -> str | None:
    """Full plain-text body for one stored message, via that message's own transport.

    Returns None when the body cannot be fetched, and never raises: callers use this to enrich
    a prompt, so a mailbox that cannot serve bodies should degrade to the stored preview rather
    than fail the whole request.

    Gmail returns None by design. Its stored preview is already up to 2000 characters (IMAP
    gives us the whole message at sync time, unlike Graph's short bodyPreview), so a refetch
    would mean an IMAP connection and a Message-ID search for very little gain.
    """
    mailbox_id = getattr(message, "mailbox_id", None)
    graph_id = getattr(message, "graph_message_id", "") or ""

    mailbox = None
    if mailbox_id:
        try:
            mailbox = get_mailbox(mailbox_id)
        except Exception:
            mailbox = None

    # Unattributed rows predate multi-mailbox sync and came from the delegated account.
    provider = mailbox.provider if mailbox else PROVIDER_GRAPH

    if provider == PROVIDER_GMAIL or graph_id.startswith("imap:"):
        return None

    select = "?$select=subject,body,sentDateTime"
    try:
        if provider == PROVIDER_GRAPH_APP and mailbox is not None:
            token = acquire_app_token(mailbox.app_credentials)
            url = f"{GRAPH_BASE}/users/{mailbox.from_email}/messages/{graph_id}{select}"
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as http:
                response = await http.get(url, headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                return None
            payload = response.json()
        else:
            payload = await GraphClient(db).fetch_message_body(graph_id)
    except (GraphAppAuthError, GraphAuthError, httpx.HTTPError, Exception):
        return None

    body = payload.get("body") or {}
    raw = body.get("content") or ""
    if body.get("contentType") == "html":
        raw = strip_html(raw)
    return raw.strip() or None


def reader_for(db: Session, mailbox: Mailbox) -> MailReader:
    """The right reader for a mailbox's transport."""
    if mailbox.provider == PROVIDER_GRAPH_APP:
        return AppGraphReader(mailbox)
    if mailbox.provider == PROVIDER_GMAIL:
        return GmailImapReader(mailbox)
    if mailbox.provider == PROVIDER_GRAPH:
        return DelegatedGraphReader(db, mailbox)
    raise MailReadError(f"Mailbox {mailbox.id!r} has unsupported provider {mailbox.provider!r}")
