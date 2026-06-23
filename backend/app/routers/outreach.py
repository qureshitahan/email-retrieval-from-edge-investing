from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.outreach_service import (
    OutreachError,
    draft_to_dict,
    generate_draft_for_contact,
    generate_drafts_bulk,
    get_prompt_config,
    list_drafts,
    send_approved_drafts,
    send_draft,
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


class SingleGenerateRequest(BaseModel):
    custom_instructions: str | None = None


class DraftUpdate(BaseModel):
    subject: str | None = None
    body: str | None = None
    status: str | None = None


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
                db, payload.contact_ids[0], custom_instructions=payload.custom_instructions
            )
            return {"items": [draft_to_dict(draft)]}
        except OutreachError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    results = await generate_drafts_bulk(db, payload.contact_ids, custom_instructions=payload.custom_instructions)
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
    try:
        draft = await generate_draft_for_contact(db, contact_id, custom_instructions=instructions)
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


@router.post("/drafts/{draft_id}/send")
async def post_send_draft(draft_id: str, db: Session = Depends(get_db)):
    try:
        draft = await send_draft(db, draft_id)
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/drafts/send-approved")
async def post_send_approved(db: Session = Depends(get_db)):
    return {"results": await send_approved_drafts(db)}
