from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.message import EmailMessage
from app.models.sync import SyncRun
from app.services.contact_pipeline import (
    process_inbound_sender,
    process_message_recipients,
    rebuild_contact_aggregates,
)
from app.services.mail_readers import MailReader, reader_for
from app.services.mailboxes import Mailbox, default_mailbox
from app.services.text_utils import normalize_email, normalize_subject, parse_display_name


def _serialize_recipients(recipients: list[dict] | None) -> list[dict]:
    serialized: list[dict] = []
    for recipient in recipients or []:
        name, email = parse_display_name(recipient)
        if email:
            serialized.append({"name": name, "address": email})
    return serialized


def upsert_message(
    db: Session,
    item: dict,
    *,
    direction: str = "outbound",
    mailbox_id: str | None = None,
) -> tuple[EmailMessage, bool]:
    graph_id = item["id"]
    existing = db.query(EmailMessage).filter(EmailMessage.graph_message_id == graph_id).one_or_none()
    if existing:
        # A message already imported without attribution belongs to whichever mailbox first
        # reads it; backfill rather than leaving the column NULL forever.
        if mailbox_id and existing.mailbox_id is None:
            existing.mailbox_id = mailbox_id
        return existing, False

    sender = item.get("sender") or item.get("from") or {}
    _, sender_email = parse_display_name(sender)

    if direction == "inbound":
        dt_raw = item.get("receivedDateTime") or item.get("sentDateTime")
    else:
        dt_raw = item["sentDateTime"]
    sent_dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))

    message = EmailMessage(
        graph_message_id=graph_id,
        internet_message_id=item.get("internetMessageId"),
        conversation_id=item.get("conversationId"),
        sent_datetime=sent_dt,
        subject=item.get("subject"),
        subject_normalized=normalize_subject(item.get("subject")),
        body_preview=item.get("bodyPreview"),
        outlook_weblink=item.get("webLink"),
        has_attachments=bool(item.get("hasAttachments")),
        importance=item.get("importance"),
        categories=item.get("categories") or [],
        sender_email=normalize_email(sender_email),
        raw_from=sender,
        raw_to=_serialize_recipients(item.get("toRecipients")),
        raw_cc=_serialize_recipients(item.get("ccRecipients")),
        raw_bcc=_serialize_recipients(item.get("bccRecipients")),
        direction=direction,
        mailbox_id=mailbox_id,
    )
    db.add(message)
    db.flush()
    return message, True


class SyncService:
    BATCH_AGGREGATE_SIZE = 250

    def __init__(self, db: Session, mailbox: Mailbox | None = None):
        self.db = db
        # No mailbox named -> first configured one, preserving single-mailbox behaviour.
        self.mailbox = mailbox or default_mailbox()
        self.reader: MailReader | None = reader_for(db, self.mailbox) if self.mailbox else None

    @property
    def mailbox_id(self) -> str | None:
        return self.mailbox.id if self.mailbox else None

    def _require_reader(self) -> MailReader:
        if self.reader is None:
            raise RuntimeError(
                "No mailbox configured. Set OUTREACH_MAILBOXES in .env before syncing."
            )
        return self.reader

    def get_active_run(self) -> SyncRun | None:
        return (
            self.db.query(SyncRun)
            .filter(SyncRun.status == "running")
            .order_by(SyncRun.started_at.desc())
            .first()
        )

    async def _run(self, sync_run_id: str, *, inbound: bool) -> None:
        """Page through one folder of one mailbox. Sent and inbox differ only in the reader
        call, the message direction, and which contact-extraction step applies."""
        reader = self._require_reader()
        sync_run = self.db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        touched_contact_ids: set[str] = set()
        cursor = sync_run.checkpoint_url
        messages_new = sync_run.messages_new
        messages_fetched = sync_run.messages_fetched
        direction = "inbound" if inbound else "outbound"

        try:
            while True:
                if inbound:
                    values, next_cursor = await reader.fetch_inbox_page(cursor)
                else:
                    values, next_cursor = await reader.fetch_sent_page(cursor)

                batch_touched: list[str] = []
                for item in values:
                    message, is_new = upsert_message(
                        self.db, item, direction=direction, mailbox_id=self.mailbox_id
                    )
                    messages_fetched += 1
                    if is_new:
                        messages_new += 1
                        if inbound:
                            batch_touched.extend(process_inbound_sender(self.db, message))
                        else:
                            batch_touched.extend(process_message_recipients(self.db, message))

                self.db.commit()
                touched_contact_ids.update(batch_touched)

                if len(touched_contact_ids) >= self.BATCH_AGGREGATE_SIZE:
                    rebuild_contact_aggregates(self.db, list(touched_contact_ids))
                    touched_contact_ids.clear()

                sync_run.messages_fetched = messages_fetched
                sync_run.messages_new = messages_new
                sync_run.checkpoint_url = next_cursor
                self.db.commit()

                cursor = next_cursor
                if not cursor:
                    break

            updated_count = (
                rebuild_contact_aggregates(self.db, list(touched_contact_ids))
                if touched_contact_ids
                else 0
            )

            sync_run.status = "completed"
            sync_run.completed_at = datetime.utcnow()
            sync_run.contacts_updated = updated_count
            self.db.commit()
        except Exception as exc:
            sync_run.status = "failed"
            sync_run.error_message = str(exc)
            sync_run.completed_at = datetime.utcnow()
            self.db.commit()
            raise

    async def run_full_sync(self, sync_run_id: str) -> None:
        await self._run(sync_run_id, inbound=False)

    async def run_inbox_sync(self, sync_run_id: str) -> None:
        await self._run(sync_run_id, inbound=True)

    def _start(self, sync_type: str) -> SyncRun:
        active = self.get_active_run()
        if active:
            return active
        sync_run = SyncRun(sync_type=sync_type, status="running", mailbox_id=self.mailbox_id)
        self.db.add(sync_run)
        self.db.commit()
        self.db.refresh(sync_run)
        return sync_run

    def start_inbox_sync(self) -> SyncRun:
        return self._start("inbox")

    def start_full_sync(self) -> SyncRun:
        return self._start("full")


async def run_sync_in_background(db_factory, sync_run_id: str) -> None:
    from app.services.mailboxes import get_mailbox

    db = db_factory()
    try:
        sync_run = db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        try:
            # Resolve the mailbox from the run so a background task syncs what was requested.
            mailbox = get_mailbox(sync_run.mailbox_id) if sync_run.mailbox_id else None
            service = SyncService(db, mailbox)
        except Exception as exc:
            # Setup failures (mailbox removed from config, unsupported provider) must still mark
            # the run finished - a run left at "running" blocks every later sync.
            sync_run.status = "failed"
            sync_run.error_message = str(exc)
            sync_run.completed_at = datetime.utcnow()
            db.commit()
            raise

        if sync_run.sync_type == "inbox":
            await service.run_inbox_sync(sync_run_id)
        else:
            await service.run_full_sync(sync_run_id)
    finally:
        db.close()
