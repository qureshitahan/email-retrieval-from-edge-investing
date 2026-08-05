"""Objective-driven contact prioritisation.

The static ``fundraising_relevance_score`` answers "who do we talk to most, and does it look
like fundraising" — it cannot answer "who matters for a board seat". This module re-ranks a
candidate set against a stated objective, so typing an objective actually changes the order of
people you work through rather than only changing the wording of a draft.

Design notes:

- The existing score is used as a cheap prefilter, then the LLM judges the shortlist. Ranking
  every contact would be thousands of calls; ranking a shortlist is one.
- Candidates go to the model as compact factual briefs built from the database, never as raw
  mail, so the judgement is grounded and the prompt stays small enough to batch.
- Scoring is relative to the objective ONLY. A contact can be high-volume and still score low
  because they are irrelevant to what you are trying to do, which is the whole point.
- The model must return one entry per candidate; anything it omits or invents is reconciled
  against the shortlist so a malformed reply degrades to "unscored", never to a wrong order.
"""

from __future__ import annotations

import asyncio
import json
import re

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import EmailMessage
from app.services.ai_service import AIServiceError, _call_anthropic
from app.services.text_utils import is_trivial_preview, strip_quoted_reply

# How many candidates one LLM call judges. Kept modest for two reasons: the model attends to
# every candidate rather than skimming, and the JSON reply stays well inside SCORING_MAX_TOKENS.
# At 40 per batch the reply overran the token limit, was truncated mid-array, and every score
# silently came back null.
BATCH_SIZE = 20
# Room for BATCH_SIZE entries each carrying a sentence of justification, with headroom.
SCORING_MAX_TOKENS = 4000
# How deep into the contact base a single request scans. Batches run concurrently, so this
# is bounded by patience rather than by one prompt's size.
DEFAULT_SCAN = 200
MAX_SCAN = 600
SUBJECT_SAMPLES = 4
BRIEF_EXCERPT_CHARS = 200

SYSTEM_PROMPT = """You rank business contacts against a specific objective.

You are given an objective and a numbered list of contacts, each with factual evidence drawn
from real email correspondence. Score how useful each contact is FOR THAT OBJECTIVE.

Rules:
- Score 0-100. Judge relevance to the objective, not how much email there is. A contact with
  hundreds of messages who cannot help with the objective scores low; a contact with three
  messages who is exactly the right person scores high.
- Use only the evidence given. Never invent a role, company, or relationship.
- The reason must cite something specific from that contact's evidence and say why it bears on
  the objective. Never write a reason that would fit any contact.
- Never mention email counts, thread counts, or date ranges in the reason.
- Reply with ONLY a JSON array, no prose and no code fence, in this exact shape:
  [{"n": <candidate number>, "score": <0-100>, "reason": "<one sentence>"}]
- Include every candidate number exactly once."""

USER_TEMPLATE = """OBJECTIVE: {objective}

Rank these {count} contacts for that objective.

{candidates}"""


def _totals(db: Session, contact_id: str) -> tuple[int, int]:
    rows = (
        db.query(EmailMessage.direction, func.count(EmailMessage.id))
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id)
        .group_by(EmailMessage.direction)
        .all()
    )
    counts = {direction: count for direction, count in rows}
    return counts.get("inbound", 0), sum(v for k, v in counts.items() if k != "inbound")


def build_candidate_brief(db: Session, contact: Contact) -> str:
    """Compact, factual evidence for one contact. No email volume framing."""
    inbound, outbound = _totals(db, contact.id)
    context: ContactContext | None = contact.context

    parts = [
        f"{contact.full_name or contact.primary_email} <{contact.primary_email}>",
        f"  Company: {contact.company_name or 'unknown'}"
        + (f" ({contact.company_domain})" if contact.company_domain else ""),
    ]
    if contact.contact_type:
        parts.append(f"  Categorised as: {contact.contact_type}")
    if context and context.detected_topics:
        parts.append(f"  Topics discussed: {', '.join(context.detected_topics[:6])}")

    # Deliberately qualitative. Handing the model a reply count invites it to quote the number
    # back in its reason, which is the "exchanged 64 emails" failure mode all over again.
    if inbound == 0:
        reciprocity = "one-way so far — they have never replied to us"
    elif inbound >= max(3, outbound // 4):
        reciprocity = "genuinely two-way — they reply and engage"
    else:
        reciprocity = "mostly us — they have replied only occasionally"
    parts.append(f"  Reciprocity: {reciprocity}")

    subjects = [
        subject
        for (subject,) in db.query(EmailMessage.subject)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact.id, EmailMessage.subject.isnot(None))
        .order_by(EmailMessage.sent_datetime.desc())
        .limit(20)
        .all()
    ]
    seen: list[str] = []
    for subject in subjects:
        cleaned = re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", subject, flags=re.I).strip()
        if cleaned and cleaned.lower() not in {s.lower() for s in seen}:
            seen.append(cleaned)
        if len(seen) >= SUBJECT_SAMPLES:
            break
    if seen:
        parts.append("  Recent threads: " + "; ".join(seen))

    # Their most recent substantive message says more about them than any metadata.
    last_inbound = (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact.id, EmailMessage.direction == "inbound")
        .order_by(EmailMessage.sent_datetime.desc())
        .limit(6)
        .all()
    )
    excerpt = next(
        (
            strip_quoted_reply(m.body_preview)[:BRIEF_EXCERPT_CHARS]
            for m in last_inbound
            if not is_trivial_preview(m.body_preview)
        ),
        None,
    )
    if excerpt:
        parts.append(f'  They most recently wrote: "{excerpt}"')

    return "\n".join(parts)


def shortlist_candidates(
    db: Session,
    contact_ids: list[str] | None,
    limit: int,
    mailbox_ids: list[str] | None = None,
) -> list[Contact]:
    """Contacts to judge: an explicit set, or the strongest existing candidates.

    ``mailbox_ids`` answers "where should I look" - only people the chosen mailboxes have
    actually corresponded with are considered.
    """
    query = db.query(Contact).options(joinedload(Contact.context))
    if contact_ids:
        return query.filter(Contact.id.in_(contact_ids)).all()

    query = query.filter(
        Contact.is_internal.is_(False),
        Contact.is_excluded.is_(False),
        Contact.email_count > 0,
    )

    if mailbox_ids:
        from_selected = (
            db.query(ContactEmailLink.id)
            .join(EmailMessage, EmailMessage.id == ContactEmailLink.email_message_id)
            .filter(
                ContactEmailLink.contact_id == Contact.id,
                EmailMessage.mailbox_id.in_(mailbox_ids),
            )
            .exists()
        )
        query = query.filter(from_selected)

    return (
        query.order_by(
            Contact.fundraising_relevance_score.desc(),
            Contact.email_count.desc(),
        )
        .limit(limit)
        .all()
    )


def _parse_rankings(raw: str, count: int) -> dict[int, dict]:
    """Pull the JSON array out of the reply, tolerating fences or stray prose."""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("["):
        match = re.search(r"\[.*\]", text, re.S)
        if match:
            text = match.group(0)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # A reply cut off by the token limit is valid up to the truncation point. Recover the
        # complete objects rather than throwing away every score in the batch.
        parsed = []
        for match in re.finditer(r"\{[^{}]*\}", text, re.S):
            try:
                parsed.append(json.loads(match.group(0)))
            except json.JSONDecodeError:
                continue
    if not isinstance(parsed, list):
        return {}

    out: dict[int, dict] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            n = int(entry.get("n"))
            score = int(round(float(entry.get("score"))))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= count:
            continue
        reason = str(entry.get("reason") or "").strip()
        out[n] = {"score": max(0, min(100, score)), "reason": reason}
    return out


def _score_batch(objective: str, briefs: list[str]) -> dict[int, dict]:
    """One LLM call over one batch. Pure strings in, so it is safe off the main thread."""
    raw = _call_anthropic(
        SYSTEM_PROMPT,
        USER_TEMPLATE.format(objective=objective, count=len(briefs), candidates="\n\n".join(briefs)),
        max_tokens=SCORING_MAX_TOKENS,
    )
    return _parse_rankings(raw, len(briefs))


async def prioritize_contacts(
    db: Session,
    *,
    objective: str,
    contact_ids: list[str] | None = None,
    limit: int = DEFAULT_SCAN,
    min_score: int | None = None,
    top_n: int | None = None,
    mailbox_ids: list[str] | None = None,
) -> dict:
    """Rank contacts for an objective, best-first.

    Scans in concurrent batches so the shortlist can span hundreds of people rather than one
    prompt's worth. Contacts are judged regardless of review status - the point is to find who
    matters for the objective first, and approve them afterwards.
    """
    objective = (objective or "").strip()
    if not objective:
        raise AIServiceError("An objective is required to prioritise contacts")

    limit = max(1, min(limit, MAX_SCAN))
    candidates = shortlist_candidates(db, contact_ids, limit, mailbox_ids)
    if not candidates:
        return {
            "objective": objective,
            "items": [],
            "scored": 0,
            "scanned": 0,
            "batches": 0,
            "failed_batches": 0,
        }

    # Briefs are built here, on the thread that owns the session: the DB session is not
    # thread-safe, so only the model calls are allowed to fan out.
    briefs = [build_candidate_brief(db, contact) for contact in candidates]

    batches: list[tuple[list[Contact], list[str]]] = []
    for start in range(0, len(candidates), BATCH_SIZE):
        chunk = candidates[start : start + BATCH_SIZE]
        chunk_briefs = [
            f"[{i}] {briefs[start + offset]}" for i, offset in enumerate(range(len(chunk)), start=1)
        ]
        batches.append((chunk, chunk_briefs))

    scored_batches = await asyncio.gather(
        *(asyncio.to_thread(_score_batch, objective, chunk_briefs) for _, chunk_briefs in batches),
        return_exceptions=True,
    )

    items: list[dict] = []
    failed_batches = 0
    for (chunk, _), rankings in zip(batches, scored_batches):
        # A failed batch leaves its candidates unscored rather than losing them entirely.
        if isinstance(rankings, BaseException):
            failed_batches += 1
            rankings = {}
        for i, contact in enumerate(chunk, start=1):
            ranked = rankings.get(i)
            items.append(
                {
                    "contact_id": contact.id,
                    "list_number": contact.list_number,
                    "full_name": contact.full_name,
                    "primary_email": contact.primary_email,
                    "company_name": contact.company_name,
                    "review_status": contact.review_status,
                    "baseline_score": contact.fundraising_relevance_score,
                    # None (not 0) when the model skipped it, so the UI can say "unscored"
                    # instead of implying the contact was judged irrelevant.
                    "objective_score": ranked["score"] if ranked else None,
                    "reason": ranked["reason"] if ranked else None,
                }
            )

    # Unscored candidates sort last rather than as zeros.
    items.sort(key=lambda item: (item["objective_score"] is None, -(item["objective_score"] or 0)))

    scanned = len(items)

    # Trim by rank first. Scores are only comparable within one call - the model marks more
    # harshly when a batch holds 40 candidates than 20 - so an absolute cut-off silently
    # returns nothing at larger scan depths. Rank stays meaningful either way, which is why
    # top_n is the primary control and min_score is opt-in.
    if top_n is not None and top_n > 0:
        items = items[:top_n]
    if min_score is not None and min_score > 0:
        items = [
            i for i in items if i["objective_score"] is not None and i["objective_score"] >= min_score
        ]

    return {
        "objective": objective,
        "items": items,
        "scored": sum(1 for i in items if i["objective_score"] is not None),
        "scanned": scanned,
        "batches": len(batches),
        "failed_batches": failed_batches,
    }
