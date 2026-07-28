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

import json
import re

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import EmailMessage
from app.services.ai_service import AIServiceError, _call_anthropic
from app.services.text_utils import is_trivial_preview, strip_quoted_reply

# Upper bound on how many candidates go to the model in one call.
MAX_CANDIDATES = 40
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


def shortlist_candidates(db: Session, contact_ids: list[str] | None, limit: int) -> list[Contact]:
    """Contacts to judge: an explicit set, or the strongest existing candidates."""
    query = db.query(Contact).options(joinedload(Contact.context))
    if contact_ids:
        return query.filter(Contact.id.in_(contact_ids)).all()

    return (
        query.filter(
            Contact.is_internal.is_(False),
            Contact.is_excluded.is_(False),
            Contact.email_count > 0,
        )
        .order_by(
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
        return {}
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


def prioritize_contacts(
    db: Session,
    *,
    objective: str,
    contact_ids: list[str] | None = None,
    limit: int = MAX_CANDIDATES,
) -> dict:
    """Rank contacts for an objective. Returns candidates ordered best-first."""
    objective = (objective or "").strip()
    if not objective:
        raise AIServiceError("An objective is required to prioritise contacts")

    limit = max(1, min(limit, MAX_CANDIDATES))
    candidates = shortlist_candidates(db, contact_ids, limit)
    if not candidates:
        return {"objective": objective, "items": [], "scored": 0}

    briefs = [
        f"[{i}] {build_candidate_brief(db, contact)}"
        for i, contact in enumerate(candidates, start=1)
    ]
    raw = _call_anthropic(
        SYSTEM_PROMPT,
        USER_TEMPLATE.format(
            objective=objective, count=len(candidates), candidates="\n\n".join(briefs)
        ),
    )
    rankings = _parse_rankings(raw, len(candidates))

    items = []
    for i, contact in enumerate(candidates, start=1):
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
    return {
        "objective": objective,
        "items": items,
        "scored": sum(1 for item in items if item["objective_score"] is not None),
    }
