from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.message import EmailMessage
from app.models.sync import SyncRun
from app.schemas import SyncRunOut
from app.services.mail_readers import MailReadError
from app.services.mailboxes import Mailbox, MailboxConfigError, default_mailbox, get_mailbox
from app.services.sync_service import (
    SyncAlreadyRunning,
    SyncService,
    run_sync_in_background,
)

router = APIRouter(prefix="/sync", tags=["sync"])


def _db_factory():
    return SessionLocal()


def _resolve_mailbox(mailbox_id: str | None) -> Mailbox:
    """The mailbox to sync. Falls back to the first configured one when unnamed."""
    try:
        mailbox = get_mailbox(mailbox_id) if mailbox_id else default_mailbox()
    except MailboxConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if mailbox is None:
        raise HTTPException(
            status_code=400,
            detail="No mailboxes configured. Set OUTREACH_MAILBOXES in the backend .env.",
        )
    return mailbox


def _run_reprocess() -> None:
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent.parent / "scripts" / "reprocess_contacts.py"
    subprocess.run([sys.executable, str(script)], check=True)


@router.post("/reprocess-contacts")
def reprocess_contacts(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_reprocess)
    return {"status": "started", "message": "Re-extracting contacts from imported messages"}


def _start(
    db: Session,
    background_tasks: BackgroundTasks,
    mailbox_id: str | None,
    *,
    inbox: bool,
) -> SyncRun:
    mailbox = _resolve_mailbox(mailbox_id)
    try:
        service = SyncService(db, mailbox)
    except MailReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    already_running = service.get_active_run() is not None
    try:
        sync_run = service.start_inbox_sync() if inbox else service.start_full_sync()
    except SyncAlreadyRunning as exc:
        # 409: the request was valid, another mailbox just holds the write lock.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Only attach a worker for a genuinely new run, or a second worker would race the first.
    if not already_running:
        background_tasks.add_task(run_sync_in_background, _db_factory, sync_run.id)
    return sync_run


@router.post("/start-inbox", response_model=SyncRunOut)
def start_inbox_sync(
    background_tasks: BackgroundTasks,
    mailbox_id: str | None = Query(None, description="Mailbox to sync; defaults to the first one"),
    db: Session = Depends(get_db),
):
    return _start(db, background_tasks, mailbox_id, inbox=True)


@router.post("/start", response_model=SyncRunOut)
def start_sync(
    background_tasks: BackgroundTasks,
    mailbox_id: str | None = Query(None, description="Mailbox to sync; defaults to the first one"),
    db: Session = Depends(get_db),
):
    return _start(db, background_tasks, mailbox_id, inbox=False)


@router.get("/status", response_model=SyncRunOut | None)
def latest_sync_status(
    mailbox_id: str | None = Query(None, description="Scope to one mailbox's runs"),
    db: Session = Depends(get_db),
):
    """Latest run, optionally for one mailbox.

    Without the filter the UI showed another mailbox's progress next to the mailbox you had
    selected, which read as "your sync is running" when it was not.
    """
    query = db.query(SyncRun)
    if mailbox_id:
        query = query.filter(SyncRun.mailbox_id == mailbox_id)
    return query.order_by(SyncRun.started_at.desc()).first()


class BackfillRequest(BaseModel):
    mailbox_id: str


@router.post("/backfill-mailbox")
def backfill_mailbox(payload: BackfillRequest, db: Session = Depends(get_db)):
    """Attribute pre-multi-mailbox rows to a mailbox.

    Messages imported before ``mailbox_id`` existed have NULL there, so they vanish the moment
    the contact list is filtered by mailbox even though the contacts are still present. Those
    rows all came from the one Outlook account that could be connected at the time; naming
    which mailbox that was is a decision for the caller, not a guess this code should make.
    """
    mailbox = _resolve_mailbox(payload.mailbox_id)

    updated = (
        db.query(EmailMessage)
        .filter(EmailMessage.mailbox_id.is_(None))
        .update({EmailMessage.mailbox_id: mailbox.id}, synchronize_session=False)
    )
    runs_updated = (
        db.query(SyncRun)
        .filter(SyncRun.mailbox_id.is_(None))
        .update({SyncRun.mailbox_id: mailbox.id}, synchronize_session=False)
    )
    db.commit()
    return {
        "mailbox_id": mailbox.id,
        "from_email": mailbox.from_email,
        "messages_updated": updated,
        "sync_runs_updated": runs_updated,
        "messages_still_unattributed": db.query(EmailMessage)
        .filter(EmailMessage.mailbox_id.is_(None))
        .count(),
    }


@router.get("/runs", response_model=list[SyncRunOut])
def list_sync_runs(db: Session = Depends(get_db)):
    runs = db.query(SyncRun).order_by(SyncRun.started_at.desc()).limit(20).all()
    return runs
