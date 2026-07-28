"""Mailbox listing with live readiness.

Separate from the outreach router because the contacts page needs it too: the UI shows a mailbox
dropdown instead of a "Connect Microsoft Outlook" button, so it has to know which identities are
usable without any user action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.mailbox_status import all_mailbox_statuses, clear_cache

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


@router.get("")
def get_mailboxes(
    refresh: bool = Query(False, description="Bypass the readiness cache and re-check now"),
    db: Session = Depends(get_db),
):
    """Configured mailboxes with live send/read readiness. Never returns secrets."""
    if refresh:
        clear_cache()
    return all_mailbox_statuses(db, use_cache=not refresh)
