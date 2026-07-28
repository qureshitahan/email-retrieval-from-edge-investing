from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.sync import SyncRun
from app.schemas import SyncRunOut
from app.services.mail_readers import MailReadError
from app.services.mailboxes import Mailbox, MailboxConfigError, default_mailbox, get_mailbox
from app.services.sync_service import SyncService, run_sync_in_background

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

    active = service.get_active_run()
    if active:
        return active

    sync_run = service.start_inbox_sync() if inbox else service.start_full_sync()
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
def latest_sync_status(db: Session = Depends(get_db)):
    run = db.query(SyncRun).order_by(SyncRun.started_at.desc()).first()
    return run


@router.get("/runs", response_model=list[SyncRunOut])
def list_sync_runs(db: Session = Depends(get_db)):
    runs = db.query(SyncRun).order_by(SyncRun.started_at.desc()).limit(20).all()
    return runs
