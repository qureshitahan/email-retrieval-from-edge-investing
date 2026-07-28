from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.ai_service import (
    AIServiceError,
    ai_status,
    classify_contact,
    generate_follow_up,
    generate_relationship_context,
    generate_summary,
    summarize_threads,
)

router = APIRouter(prefix="/contacts", tags=["ai"])


@router.get("/{contact_id}/ai/status")
def get_ai_status(contact_id: str, db: Session = Depends(get_db)):
    try:
        return ai_status(db, contact_id)
    except AIServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/summary")
async def post_ai_summary(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await generate_summary(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/relationship")
async def post_ai_relationship(
    contact_id: str,
    force: bool = Query(False),
    objective: str | None = Query(None, description="Optional campaign objective, e.g. 'board seat'"),
    db: Session = Depends(get_db),
):
    """Meaningful relationship insight instead of volume statistics."""
    try:
        return await generate_relationship_context(db, contact_id, force=force, objective=objective)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/follow-up")
async def post_ai_follow_up(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await generate_follow_up(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/classify")
async def post_ai_classify(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await classify_contact(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/summarize-threads")
async def post_ai_summarize_threads(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await summarize_threads(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
