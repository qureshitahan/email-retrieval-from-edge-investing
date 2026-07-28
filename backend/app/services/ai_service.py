from __future__ import annotations

import re
from datetime import datetime
from html import unescape

from anthropic import Anthropic
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import EmailMessage
from app.services.graph_client import GraphClient, GraphAuthError
from app.services.relationship_context import (
    build_relationship_evidence,
    format_relationship_evidence,
)
# strip_html moved to text_utils so mail_readers can use it without importing this module
# (ai_service imports mail_readers, so the reverse direction would be circular).
# Re-exported here because existing callers import it from ai_service.
from app.services.text_utils import strip_html, strip_quoted_reply  # noqa: F401

MAX_MESSAGES_FOR_AI = 20
MAX_BODY_CHARS = 4000


class AIServiceError(Exception):
    pass


def _get_client() -> Anthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AIServiceError("ANTHROPIC_API_KEY is not configured in .env")
    return Anthropic(api_key=settings.anthropic_api_key)


def _get_contact(db: Session, contact_id: str) -> Contact:
    contact = (
        db.query(Contact)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
        .filter(Contact.id == contact_id)
        .one_or_none()
    )
    if not contact:
        raise AIServiceError("Contact not found")
    return contact


def _recent_messages(db: Session, contact_id: str, *, inbound: bool, limit: int) -> list[EmailMessage]:
    if limit <= 0:
        return []
    condition = (
        EmailMessage.direction == "inbound" if inbound else EmailMessage.direction != "inbound"
    )
    return (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id, condition)
        .order_by(EmailMessage.sent_datetime.desc())
        .limit(limit)
        .all()
    )


def _contact_messages(db: Session, contact_id: str) -> list[EmailMessage]:
    """Recent messages for a contact, with both directions guaranteed to be represented.

    A plain "most recent N" window silently drops the contact's own replies whenever we have
    just sent a burst — and on a long relationship that can mean the model sees only our side
    and concludes they never answered. Half the budget is reserved per direction, and whatever
    one side does not use is given back to the other.
    """
    half = max(1, MAX_MESSAGES_FOR_AI // 2)
    inbound = _recent_messages(db, contact_id, inbound=True, limit=half)
    outbound = _recent_messages(
        db, contact_id, inbound=False, limit=MAX_MESSAGES_FOR_AI - len(inbound)
    )
    if len(inbound) + len(outbound) < MAX_MESSAGES_FOR_AI:
        inbound = _recent_messages(
            db, contact_id, inbound=True, limit=MAX_MESSAGES_FOR_AI - len(outbound)
        )
    merged = sorted(inbound + outbound, key=lambda m: m.sent_datetime, reverse=True)
    return merged[:MAX_MESSAGES_FOR_AI]


def _direction_totals(contact: Contact) -> tuple[int, int]:
    """(inbound, outbound) across the WHOLE relationship, independent of the shown window."""
    inbound = outbound = 0
    for link in contact.email_links or []:
        message = link.message
        if message is None:
            continue
        if message.direction == "inbound":
            inbound += 1
        else:
            outbound += 1
    return inbound, outbound


def _needs_refresh(context: ContactContext | None, contact: Contact, field: str) -> bool:
    if not context:
        return True
    generated_at = context.ai_summary_generated_at
    if not getattr(context, field, None):
        return True
    if not generated_at or not contact.last_contacted_at:
        return False
    return contact.last_contacted_at > generated_at


def build_metadata_context(contact: Contact, messages: list[EmailMessage]) -> str:
    """Grounding block for a contact's correspondence.

    Every message is explicitly attributed to the sender. This matters: the message list
    contains both directions once an inbox sync has run, and labelling the whole set as
    "sent" made the model read the contact's own replies as ours.
    """
    context = contact.context
    lines = [
        f"Contact: {contact.full_name} <{contact.primary_email}>",
        f"Company: {contact.company_name or 'Unknown'} ({contact.company_domain or 'n/a'})",
        f"First contacted: {contact.first_contacted_at}",
        f"Last contacted: {contact.last_contacted_at}",
        f"Fundraising score: {contact.fundraising_relevance_score} ({contact.fundraising_relevance_tier})",
        f"Detected topics: {', '.join(context.detected_topics or []) if context else 'none'}",
    ]

    shown_inbound = [m for m in messages if m.direction == "inbound"]
    shown_outbound = [m for m in messages if m.direction != "inbound"]

    # Reciprocity must be judged on the whole relationship. Counting only the shown window
    # produced "they have never replied" for a contact who had replied 222 times, simply
    # because our most recent messages filled the window.
    total_inbound, total_outbound = _direction_totals(contact)
    if total_inbound + total_outbound == 0:
        total_inbound, total_outbound = len(shown_inbound), len(shown_outbound)

    lines.append(
        f"Correspondence to date: we sent {total_outbound}, they sent {total_inbound}"
        + (" — they have never replied" if total_inbound == 0 else "")
    )
    if len(messages) < total_inbound + total_outbound:
        lines.append(
            f"(Excerpt below shows the {len(messages)} most recent messages — "
            f"{len(shown_outbound)} from us, {len(shown_inbound)} from them — not the full history.)"
        )

    lines += [
        "",
        "MESSAGE HISTORY (newest first). Each line states who wrote it — do not confuse the two:",
    ]
    for msg in messages:
        who = "THEY WROTE" if msg.direction == "inbound" else "WE WROTE"
        excerpt = strip_quoted_reply(msg.body_preview) or "(empty)"
        lines.append(
            f"- [{msg.sent_datetime:%Y-%m-%d}] {who} · Subject: {msg.subject or '(no subject)'}\n"
            f"  {excerpt}"
        )
    return "\n".join(lines)


async def build_full_context(db: Session, contact_id: str) -> str:
    """Metadata context plus the full bodies of the most recent messages.

    Bodies are fetched through each message's own mailbox transport, so this works for app-only
    mailboxes that have no interactive sign-in. It used to go straight to the delegated
    ``/me/messages/{id}`` endpoint and raised "Not authenticated" for every app-only mailbox.

    A body that cannot be fetched is skipped rather than fatal — the stored previews in the
    metadata block still describe the relationship, so a partial result beats an error.
    """
    from app.services.mail_readers import fetch_full_body

    contact = _get_contact(db, contact_id)
    messages = _contact_messages(db, contact_id)
    base = build_metadata_context(contact, messages)

    body_sections: list[str] = []
    for msg in messages[:10]:
        raw = await fetch_full_body(db, msg)
        if not raw:
            continue
        who = "THEY WROTE" if msg.direction == "inbound" else "WE WROTE"
        body_sections.append(
            f"--- Full body [{msg.sent_datetime:%Y-%m-%d}] {who} · {msg.subject} ---\n"
            f"{raw[:MAX_BODY_CHARS]}"
        )

    if body_sections:
        return base + "\n\nFull message bodies (truncated):\n" + "\n\n".join(body_sections)
    return base


def _call_anthropic(system: str, user_prompt: str) -> str:
    settings = get_settings()
    client = _get_client()
    fallbacks = [
        settings.anthropic_model,
        "claude-haiku-4-5",
        "claude-sonnet-4-20250514",
        "claude-3-haiku-20240307",
    ]
    seen: set[str] = set()
    last_error: Exception | None = None
    for model in fallbacks:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1500,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
            parts = [block.text for block in response.content if block.type == "text"]
            return "\n".join(parts).strip()
        except Exception as exc:
            last_error = exc
            continue
    raise AIServiceError(f"Anthropic API failed for all models: {last_error}")


def _ensure_context_row(db: Session, contact: Contact) -> ContactContext:
    if contact.context:
        return contact.context
    ctx = ContactContext(contact_id=contact.id)
    db.add(ctx)
    db.flush()
    contact.context = ctx
    return ctx


async def generate_summary(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_summary and not _needs_refresh(ctx, contact, "ai_summary"):
        return {"summary": ctx.ai_summary, "cached": True, "generated_at": ctx.ai_summary_generated_at}

    messages = _contact_messages(db, contact_id)
    prompt_context = build_metadata_context(contact, messages)
    summary = _call_anthropic(
        "You are a relationship intelligence assistant for Edge Investing / Galaxy Pharma fundraising and business development.",
        f"""Based on sent email metadata below, write a concise relationship summary covering:
- Who is this person and what company are they associated with?
- Why do we know them?
- What was discussed (best inference from subjects/previews)?
- Last meaningful status
- Useful for: fundraising, pharma/healthcare, board, networking, or other?
- Suggested next action

Keep it practical and under 300 words. If information is thin, say so clearly.

{prompt_context}""",
    )
    ctx.ai_summary = summary
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"summary": summary, "cached": False, "generated_at": ctx.ai_summary_generated_at}


RELATIONSHIP_SYSTEM_PROMPT = (
    "You are a relationship intelligence analyst for Edge Investing / Galaxy Pharma. You read "
    "correspondence evidence and explain what a relationship actually is, so a busy principal "
    "can decide whether and how to approach someone.\n\n"
    "Absolute rules:\n"
    "- Ground every claim in the evidence provided. Never invent employers, titles, deals, or events.\n"
    "- Do NOT lead with volume statistics. Counts like 'exchanged 64 emails across 21 threads' are "
    "not insight and must never be the substance of your answer. Use numbers only where they change "
    "a decision (for example: they have never replied, or they reply within a day).\n"
    "- Distinguish who wrote what. The evidence labels each message as from them or from us.\n"
    "- When the evidence is thin, say so plainly instead of padding."
)

RELATIONSHIP_USER_TEMPLATE = """Analyse this relationship from the evidence below.

Write these sections, using the exact headings, in plain prose (no bullet-point padding):

WHO THEY ARE
Their apparent role, organisation, and what they do — inferred only from the evidence. Say
"unclear from correspondence" where it genuinely is.

WHAT WE'VE WORKED ON TOGETHER
The substance: specific deals, transactions, companies, mandates, or workstreams named in the
threads. Reference actual subjects and what was said. This is the most important section.

HOW THE RELATIONSHIP ACTUALLY STANDS
Is this warm and reciprocal, one-sided, or dormant? Who owes whom a reply? Ground this in the
direction pattern and reply behaviour, not raw totals.

SHARED CONNECTIONS
Who else sits on these threads and what that implies about how we're connected. Omit the
section entirely if there are none.

WHY THEY MATTER{objective_clause}
The concrete reason to prioritise or deprioritise this person, and the single most sensible
next step. Be decisive.

Keep the whole thing under 320 words.

=== EVIDENCE ===
{evidence}"""


async def generate_relationship_context(
    db: Session,
    contact_id: str,
    *,
    force: bool = False,
    objective: str | None = None,
) -> dict:
    """Meaningful relationship insight, replacing bare volume statistics.

    ``objective`` optionally steers the "why they matter" judgement toward a campaign goal
    (for example "board seat"), matching how the objective flow prioritises contacts.
    """
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)

    # Objective-specific answers are not interchangeable, so only reuse an unscoped cache.
    if not force and not objective and ctx.ai_relationship_context:
        if not _needs_refresh(ctx, contact, "ai_relationship_context"):
            return {
                "relationship_context": ctx.ai_relationship_context,
                "cached": True,
                "generated_at": ctx.ai_summary_generated_at,
            }

    evidence = build_relationship_evidence(db, contact)
    if evidence["cadence"]["total_messages"] == 0:
        raise AIServiceError("No messages found for this contact — run a sync first")

    objective_clause = f" (in relation to this objective: {objective.strip()})" if objective else ""
    user_prompt = RELATIONSHIP_USER_TEMPLATE.format(
        objective_clause=objective_clause,
        evidence=format_relationship_evidence(evidence),
    )

    insight = _call_anthropic(RELATIONSHIP_SYSTEM_PROMPT, user_prompt)

    if not objective:
        ctx.ai_relationship_context = insight
        ctx.ai_summary_generated_at = datetime.utcnow()
        ctx.ai_model_used = get_settings().anthropic_model
        ctx.updated_at = datetime.utcnow()
        db.commit()

    return {
        "relationship_context": insight,
        "cached": False,
        "objective": objective,
        "generated_at": ctx.ai_summary_generated_at,
        "evidence": evidence,
    }


async def generate_follow_up(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_follow_up_draft and not _needs_refresh(ctx, contact, "ai_follow_up_draft"):
        return {"draft": ctx.ai_follow_up_draft, "cached": True, "generated_at": ctx.ai_summary_generated_at}

    messages = _contact_messages(db, contact_id)
    prompt_context = build_metadata_context(contact, messages)
    if ctx.ai_summary:
        prompt_context += f"\n\nExisting AI summary:\n{ctx.ai_summary}"

    draft = _call_anthropic(
        "You write professional, warm follow-up emails for Edge Investing / Galaxy Pharma.",
        f"""Draft a short follow-up email to this contact based on the relationship history below.
- Professional but personable tone
- Reference the last conversation naturally
- Clear call to action (call, meeting, or next step)
- Under 150 words
- Do not invent specific facts not supported by the data
- Sign off as "Best regards" without a name (user will add signature)

Return only the email body (Subject line on first line as "Subject: ...").

{prompt_context}""",
    )
    ctx.ai_follow_up_draft = draft
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"draft": draft, "cached": False, "generated_at": ctx.ai_summary_generated_at}


async def classify_contact(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_contact_classification and not _needs_refresh(ctx, contact, "ai_contact_classification"):
        return {"classification": ctx.ai_contact_classification, "cached": True}

    messages = _contact_messages(db, contact_id)
    prompt_context = build_metadata_context(contact, messages)
    raw = _call_anthropic(
        "You classify business contacts for a healthcare investment firm.",
        f"""Classify this contact. Reply in this exact JSON format only (no markdown):
{{"contact_type": "investor|family_office|pharma|healthcare|advisor|vendor|legal|board|intro|other", "confidence": "high|medium|low", "reason": "one sentence"}}

{prompt_context}""",
    )
    import json

    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        classification = json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError):
        classification = {"contact_type": "other", "confidence": "low", "reason": raw[:200]}

    ctx.ai_contact_classification = classification
    if classification.get("contact_type"):
        contact.contact_type = classification["contact_type"]
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"classification": classification, "cached": False}


async def summarize_threads(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_summary and ctx.ai_model_used and ctx.ai_model_used.endswith("-threads"):
        if not _needs_refresh(ctx, contact, "ai_summary"):
            return {"summary": ctx.ai_summary, "cached": True, "generated_at": ctx.ai_summary_generated_at}

    full_context = await build_full_context(db, contact_id)
    summary = _call_anthropic(
        "You summarize email thread history for fundraising and BD relationship management. "
        "The grounding states who wrote each message - treat that as the most important thing "
        "on the page and never attribute their words to us or ours to them. Never open with, or "
        "add a header for, message/thread counts: the reader can already see those, and they say "
        "nothing about the relationship. Describe what was actually discussed instead.",
        f"""Provide a detailed thread-by-thread summary of the relationship with this contact.
Include:
- Each major conversation theme
- Key decisions or open items
- Relationship trajectory over time
- Fundraising / pharma / BD relevance
- Recommended next action

Use full message content where available. Under 500 words.

{full_context}""",
    )
    ctx.ai_summary = summary
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model + "-threads"
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"summary": summary, "cached": False, "generated_at": ctx.ai_summary_generated_at}


def ai_status(db: Session, contact_id: str) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = contact.context
    return {
        "has_summary": bool(ctx and ctx.ai_summary),
        "has_follow_up": bool(ctx and ctx.ai_follow_up_draft),
        "has_classification": bool(ctx and ctx.ai_contact_classification),
        "summary_generated_at": ctx.ai_summary_generated_at if ctx else None,
        "model_used": ctx.ai_model_used if ctx else None,
        "needs_refresh": _needs_refresh(ctx, contact, "ai_summary") if ctx else True,
    }
