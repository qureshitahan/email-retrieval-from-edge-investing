"""Sender profiles: who is writing, and the documents that back what they claim."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.sender import SenderDocument, SenderProfile
from app.services.document_text import (
    SUPPORTED_EXTENSIONS,
    DocumentTextError,
    extract_text,
    guess_kind,
)
from app.services.mailboxes import MailboxConfigError, get_mailbox, load_mailboxes
from app.services.sender_profile import (
    analyse_document,
    document_to_dict,
    get_or_create_profile,
    profile_to_dict,
    rebuild_profile_points,
    signature_for,
)

router = APIRouter(prefix="/senders", tags=["senders"])


class ProfileUpdate(BaseModel):
    display_name: str | None = None
    title: str | None = None
    company: str | None = None
    positioning: str | None = None
    linkedin_url: str | None = None
    phone: str | None = None
    website: str | None = None
    signature: str | None = None
    # Full replacement of the point list, so the UI can reorder, edit and pin in one call.
    proof_points: list[dict] | None = None
    keywords: list[str] | None = None


def _known_mailbox(mailbox_id: str):
    try:
        mailbox = get_mailbox(mailbox_id)
    except MailboxConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if mailbox is None:
        raise HTTPException(status_code=404, detail=f"No mailbox called {mailbox_id!r}")
    return mailbox


@router.get("")
def list_sender_profiles(db: Session = Depends(get_db)):
    """One entry per configured mailbox, whether or not a profile has been filled in yet."""
    try:
        mailboxes = load_mailboxes()
    except MailboxConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    items = []
    for mailbox in mailboxes:
        profile = get_or_create_profile(db, mailbox.id)
        items.append(
            {
                **profile_to_dict(profile, include_documents=False),
                "label": mailbox.label,
                "from_email": mailbox.from_email,
                "from_name": mailbox.from_name,
                "document_count": len(profile.documents),
                "effective_signature": signature_for(profile, mailbox.from_name),
            }
        )
    return {"items": items, "supported_extensions": list(SUPPORTED_EXTENSIONS)}


@router.get("/{mailbox_id}")
def get_sender_profile(mailbox_id: str, db: Session = Depends(get_db)):
    mailbox = _known_mailbox(mailbox_id)
    profile = get_or_create_profile(db, mailbox_id)
    return {
        **profile_to_dict(profile),
        "label": mailbox.label,
        "from_email": mailbox.from_email,
        "from_name": mailbox.from_name,
        "effective_signature": signature_for(profile, mailbox.from_name),
        "supported_extensions": list(SUPPORTED_EXTENSIONS),
    }


@router.patch("/{mailbox_id}")
def patch_sender_profile(mailbox_id: str, payload: ProfileUpdate, db: Session = Depends(get_db)):
    mailbox = _known_mailbox(mailbox_id)
    profile = get_or_create_profile(db, mailbox_id)

    for field in (
        "display_name",
        "title",
        "company",
        "positioning",
        "linkedin_url",
        "phone",
        "website",
        "signature",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(profile, field, value.strip() or None)

    if payload.proof_points is not None:
        # Hand-edited points are pinned so a later re-index cannot drop them.
        cleaned = []
        for point in payload.proof_points:
            text = str(point.get("text") or "").strip()
            if not text:
                continue
            cleaned.append(
                {
                    "text": text[:400],
                    "quote": str(point.get("quote") or "").strip()[:400],
                    "source": str(point.get("source") or "typed by hand")[:200],
                    "pinned": bool(point.get("pinned", True)),
                }
            )
        profile.proof_points = cleaned
    if payload.keywords is not None:
        profile.keywords = [k.strip().lower() for k in payload.keywords if k and k.strip()][:60]

    profile.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(profile)
    return {
        **profile_to_dict(profile),
        "label": mailbox.label,
        "from_email": mailbox.from_email,
        "effective_signature": signature_for(profile, mailbox.from_name),
    }


@router.post("/{mailbox_id}/documents")
async def upload_sender_document(
    mailbox_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a résumé, bio or deal sheet and pull the proof points out of it.

    Parsing failures are reported as 400 with the reason, because "nothing happened" after
    dropping a file is the worst possible outcome here — the user cannot tell whether it worked.
    """
    mailbox = _known_mailbox(mailbox_id)
    profile = get_or_create_profile(db, mailbox_id)

    data = await file.read()
    try:
        text, truncated = extract_text(file.filename or "", data)
    except DocumentTextError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    analysis = await analyse_document(
        profile.display_name or mailbox.from_name or mailbox.from_email,
        file.filename or "document",
        text,
    )

    document = SenderDocument(
        mailbox_id=mailbox_id,
        filename=(file.filename or "document")[:512],
        kind=analysis.get("kind") or guess_kind(file.filename or "", text),
        content_text=text,
        char_count=len(text),
        proof_points=analysis["proof_points"],
        keywords=analysis["keywords"],
        summary=analysis["summary"],
        status=analysis["status"],
        error_message=analysis["error_message"],
    )
    db.add(document)
    db.commit()

    # Fill the profile's identity fields from the first document that states them, rather than
    # making the user retype what the résumé already says. Never overwrites a typed value.
    fields = analysis.get("fields") or {}
    changed = False
    for attribute in ("title", "company", "positioning"):
        if not getattr(profile, attribute) and fields.get(attribute):
            setattr(profile, attribute, fields[attribute])
            changed = True
    if not profile.display_name and (mailbox.from_name or "").strip():
        profile.display_name = mailbox.from_name.strip()
        changed = True
    if changed:
        db.commit()

    db.refresh(profile)
    rebuild_profile_points(db, profile)
    db.refresh(document)
    return {
        "document": document_to_dict(document),
        "profile": profile_to_dict(profile),
        "truncated": truncated,
    }


@router.delete("/{mailbox_id}/documents/{document_id}")
def delete_sender_document(mailbox_id: str, document_id: str, db: Session = Depends(get_db)):
    _known_mailbox(mailbox_id)
    document = (
        db.query(SenderDocument)
        .filter(SenderDocument.id == document_id, SenderDocument.mailbox_id == mailbox_id)
        .one_or_none()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    db.delete(document)
    db.commit()

    profile = get_or_create_profile(db, mailbox_id)
    rebuild_profile_points(db, profile)
    return {"deleted": document_id, "profile": profile_to_dict(profile)}


@router.post("/{mailbox_id}/reindex")
async def reindex_sender_documents(mailbox_id: str, db: Session = Depends(get_db)):
    """Re-run proof-point extraction over every stored document.

    Useful after the extraction prompt changes, and the reason the document text is kept: the
    originals do not have to be uploaded again.
    """
    mailbox = _known_mailbox(mailbox_id)
    profile = get_or_create_profile(db, mailbox_id)

    reindexed = 0
    for document in list(profile.documents):
        if not document.content_text:
            continue
        analysis = await analyse_document(
            profile.display_name or mailbox.from_name or mailbox.from_email,
            document.filename,
            document.content_text,
        )
        document.proof_points = analysis["proof_points"]
        document.keywords = analysis["keywords"]
        document.summary = analysis["summary"]
        document.status = analysis["status"]
        document.error_message = analysis["error_message"]
        reindexed += 1
    db.commit()
    db.refresh(profile)
    rebuild_profile_points(db, profile)
    return {"reindexed": reindexed, "profile": profile_to_dict(profile)}
