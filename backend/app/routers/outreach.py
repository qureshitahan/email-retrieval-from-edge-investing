from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.outreach import DraftRun
from app.services.ai_service import AIServiceError
from app.services.mailboxes import MailboxConfigError, load_mailboxes
from app.services.objective_planner import build_objective_plan
from app.services.prioritization import prioritize_contacts
from app.services.outreach_service import (
    DraftRunAlreadyActive,
    OutreachError,
    create_draft_run,
    draft_run_people,
    draft_run_to_dict,
    draft_to_dict,
    drafts_by_ids,
    generate_draft_for_contact,
    generate_drafts_bulk,
    get_prompt_config,
    latest_draft_run,
    list_drafts,
    run_draft_job,
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


class SelectionReason(BaseModel):
    contact_id: str
    reason: str | None = None
    score: int | None = None


class GenerateDraftsRequest(BaseModel):
    contact_ids: list[str] = []
    custom_instructions: str | None = None
    objective: str | None = None
    # Mailboxes the objective was searched across. Each draft is pinned to whichever of them
    # already corresponds with that contact, so a later "send all" needs no further input.
    mailbox_ids: list[str] = []
    # Why the ranker picked each of these people, so the review card can show it.
    reasons: list[SelectionReason] = []


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


class ObjectivePlanRequest(BaseModel):
    objective: str


class PlanAnswer(BaseModel):
    question: str
    answer: str


class ObjectivePlan(BaseModel):
    objective: str | None = None
    questions: list[PlanAnswer] = []
    looking_for: str | None = None
    avoid: str | None = None


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
    # The answers to the planning questions, as edited by the user. Scored alongside the
    # objective so the shortlist reflects what they actually meant by it.
    plan: ObjectivePlan | None = None


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
            plan=payload.plan.model_dump() if payload.plan else None,
        )
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/objective/plan")
async def post_objective_plan(payload: ObjectivePlanRequest):
    """Questions worth answering before searching, each with a proposed answer.

    Answered by the model so the default path stays one click; everything it proposes is a
    suggestion the user edits before it is used.
    """
    if not payload.objective.strip():
        raise HTTPException(status_code=400, detail="Describe what you want to accomplish first")
    try:
        mailboxes = load_mailboxes()
        who = ", ".join(m.label for m in mailboxes)
    except MailboxConfigError:
        who = ""
    return await build_objective_plan(payload.objective, who)


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
        selections={r.contact_id: {"reason": r.reason, "score": r.score} for r in payload.reasons},
    )
    draft_ids = [r["draft_id"] for r in results if r.get("draft_id")]
    drafts = list_drafts(db)
    by_id = {d.id: d for d in drafts}
    items = [draft_to_dict(by_id[did]) for did in draft_ids if did in by_id]
    return {"results": results, "items": items}


@router.post("/drafts/start")
def post_start_drafting(
    payload: GenerateDraftsRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Queue a bulk drafting run and return immediately.

    Writing a batch takes minutes; a single request that long is closed by Azure's front end
    at 230 seconds, which the browser reports as "Failed to fetch" even though the server is
    still working. The client polls ``/drafts/runs/{id}`` instead.
    """
    if not payload.contact_ids:
        raise HTTPException(status_code=400, detail="Select at least one contact")
    try:
        run = create_draft_run(
            db,
            payload.contact_ids,
            custom_instructions=payload.custom_instructions,
            objective=payload.objective,
            mailbox_ids=payload.mailbox_ids or None,
            selections={
                r.contact_id: {"reason": r.reason, "score": r.score} for r in payload.reasons
            },
        )
    except DraftRunAlreadyActive as exc:
        # Not an error worth stopping for: hand back the run in progress so the page attaches
        # to it rather than starting a competing one.
        return {
            **draft_run_to_dict(exc.run),
            "people": draft_run_people(db, exc.run),
            "already_running": True,
        }
    except OutreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    background_tasks.add_task(run_draft_job, SessionLocal, run.id)
    # The list ships with the response so the queue is on screen before any work is done.
    return {**draft_run_to_dict(run), "people": draft_run_people(db, run), "already_running": False}


@router.get("/drafts/runs/latest")
def get_latest_draft_run(db: Session = Depends(get_db)):
    """The most recent run, so a reloaded page can rejoin one already in flight."""
    run = latest_draft_run(db)
    if run is None:
        return None
    return {
        **draft_run_to_dict(run),
        "people": draft_run_people(db, run),
        "items": [draft_to_dict(d) for d in drafts_by_ids(db, run.draft_ids or [])],
    }


@router.get("/drafts/runs/{run_id}")
def get_draft_run(run_id: str, db: Session = Depends(get_db)):
    """Progress plus the drafts finished so far, so they appear as they are written."""
    run = db.query(DraftRun).filter(DraftRun.id == run_id).one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Drafting run not found")
    return {
        **draft_run_to_dict(run),
        "people": draft_run_people(db, run),
        "items": [draft_to_dict(d) for d in drafts_by_ids(db, run.draft_ids or [])],
    }


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
