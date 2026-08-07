from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.ai_service import AIServiceError
from app.services.mailboxes import MailboxConfigError, load_mailboxes
from app.services.prioritization import prioritize_contacts
from app.services.outreach_service import (
    OutreachError,
    draft_to_dict,
    generate_draft_for_contact,
    generate_drafts_bulk,
    get_prompt_config,
    list_drafts,
    send_approved_drafts,
    send_draft,
    send_drafts,
    set_draft_mailbox,
    update_draft,
    update_prompt_config,
)

router = APIRouter(prefix="/outreach", tags=["outreach"])


class PromptUpdate(BaseModel):
    system_prompt: str | None = None
    user_prompt_template: str | None = None


class GenerateDraftsRequest(BaseModel):
    contact_ids: list[str] = []
    custom_instructions: str | None = None
    objective: str | None = None
    # Mailboxes the objective was searched across. Each draft is pinned to whichever of them
    # already corresponds with that contact, so a later "send all" needs no further input.
    mailbox_ids: list[str] = []


class SingleGenerateRequest(BaseModel):
    custom_instructions: str | None = None
    objective: str | None = None
    # Re-read the contact's mail instead of reusing the cached study of what they are doing.
    restudy: bool = False


class DraftUpdate(BaseModel):
    subject: str | None = None
    body: str | None = None
    status: str | None = None


class SendingMailboxIn(BaseModel):
    mailbox_id: str


class SendIn(BaseModel):
    mailbox_id: str | None = None


class PrioritizeRequest(BaseModel):
    objective: str
    contact_ids: list[str] = []
    # How deep to scan the contact base. Batched concurrently behind the scenes.
    limit: int = 200
    # Keep only the best N by rank. Preferred over min_score: scores are comparable within a
    # call but not across scan depths, so a fixed threshold can return an empty list.
    top_n: int | None = 50
    # Optional absolute floor on top of the rank cut.
    min_score: int | None = None
    # "Where should I look" - restrict candidates to people these mailboxes have emailed.
    mailbox_ids: list[str] = []


@router.get("/mailboxes")
def get_mailboxes():
    """Configured sending identities (no secrets)."""
    try:
        mailboxes = load_mailboxes()
    except MailboxConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"items": [m.public_dict() for m in mailboxes]}


@router.post("/prioritize")
async def post_prioritize(payload: PrioritizeRequest, db: Session = Depends(get_db)):
    """Rank contacts against an objective (e.g. "board seat"), best-first."""
    try:
        return await prioritize_contacts(
            db,
            objective=payload.objective,
            contact_ids=payload.contact_ids or None,
            limit=payload.limit,
            min_score=payload.min_score,
            top_n=payload.top_n,
            mailbox_ids=payload.mailbox_ids or None,
        )
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/prompt")
def get_prompt(db: Session = Depends(get_db)):
    return get_prompt_config(db)


@router.patch("/prompt")
def patch_prompt(payload: PromptUpdate, db: Session = Depends(get_db)):
    return update_prompt_config(
        db,
        system_prompt=payload.system_prompt,
        user_prompt_template=payload.user_prompt_template,
    )


@router.get("/drafts")
def get_drafts(status: str | None = None, db: Session = Depends(get_db)):
    drafts = list_drafts(db, status=status)
    return {"items": [draft_to_dict(d) for d in drafts]}


@router.post("/drafts/generate")
async def post_generate_drafts(payload: GenerateDraftsRequest, db: Session = Depends(get_db)):
    if not payload.contact_ids:
        raise HTTPException(status_code=400, detail="Select at least one contact")
    if len(payload.contact_ids) == 1:
        try:
            draft = await generate_draft_for_contact(
                db,
                payload.contact_ids[0],
                custom_instructions=payload.custom_instructions,
                objective=payload.objective,
                mailbox_ids=payload.mailbox_ids or None,
            )
            return {"items": [draft_to_dict(draft)]}
        except OutreachError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    results = await generate_drafts_bulk(
        db,
        payload.contact_ids,
        custom_instructions=payload.custom_instructions,
        objective=payload.objective,
        mailbox_ids=payload.mailbox_ids or None,
    )
    draft_ids = [r["draft_id"] for r in results if r.get("draft_id")]
    drafts = list_drafts(db)
    by_id = {d.id: d for d in drafts}
    items = [draft_to_dict(by_id[did]) for did in draft_ids if did in by_id]
    return {"results": results, "items": items}


@router.post("/contacts/{contact_id}/generate")
async def post_generate_for_contact(
    contact_id: str,
    payload: SingleGenerateRequest | None = None,
    db: Session = Depends(get_db),
):
    instructions = payload.custom_instructions if payload else None
    objective = payload.objective if payload else None
    try:
        draft = await generate_draft_for_contact(
            db,
            contact_id,
            custom_instructions=instructions,
            objective=objective,
            restudy=payload.restudy if payload else False,
        )
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/drafts/{draft_id}")
def patch_draft(draft_id: str, payload: DraftUpdate, db: Session = Depends(get_db)):
    try:
        draft = update_draft(db, draft_id, subject=payload.subject, body=payload.body, status=payload.status)
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: str, db: Session = Depends(get_db)):
    try:
        draft = update_draft(db, draft_id, subject=None, body=None, status="approved")
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/sending-mailbox")
def post_draft_sending_mailbox(draft_id: str, payload: SendingMailboxIn, db: Session = Depends(get_db)):
    try:
        draft = set_draft_mailbox(db, draft_id, payload.mailbox_id)
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/send")
async def post_send_draft(
    draft_id: str,
    payload: SendIn | None = None,
    db: Session = Depends(get_db),
):
    try:
        draft = await send_draft(db, draft_id, mailbox_id=payload.mailbox_id if payload else None)
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class SendBatchIn(BaseModel):
    draft_ids: list[str]
    # Omit to let each draft use the mailbox it was pinned to at generation time.
    mailbox_id: str | None = None


@router.post("/drafts/send-batch")
async def post_send_batch(payload: SendBatchIn, db: Session = Depends(get_db)):
    """Send the named drafts, each from its own mailbox unless one is forced."""
    try:
        results = await send_drafts(db, payload.draft_ids, mailbox_id=payload.mailbox_id)
    except OutreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sent = [r for r in results if r["status"] == "sent"]
    by_mailbox: dict[str, int] = {}
    for r in sent:
        key = r.get("mailbox_id") or "default"
        by_mailbox[key] = by_mailbox.get(key, 0) + 1
    return {
        "results": results,
        "sent": len(sent),
        "failed": len(results) - len(sent),
        "by_mailbox": by_mailbox,
    }


@router.post("/drafts/send-approved")
async def post_send_approved(payload: SendIn | None = None, db: Session = Depends(get_db)):
    return {"results": await send_approved_drafts(db, mailbox_id=payload.mailbox_id if payload else None)}
