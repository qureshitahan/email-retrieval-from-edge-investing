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

MAX_MESSAGES_FOR_AI = 20
MAX_BODY_CHARS = 4000


class AIServiceError(Exception):
    pass


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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


def _contact_messages(db: Session, contact_id: str) -> list[EmailMessage]:
    return (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id)
        .order_by(EmailMessage.sent_datetime.desc())
        .limit(MAX_MESSAGES_FOR_AI)
        .all()
    )


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
    context = contact.context
    lines = [
        f"Contact: {contact.full_name} <{contact.primary_email}>",
        f"Company: {contact.company_name or 'Unknown'} ({contact.company_domain or 'n/a'})",
        f"First contacted: {contact.first_contacted_at}",
        f"Last contacted: {contact.last_contacted_at}",
        f"Email count: {contact.email_count}, Thread count: {contact.thread_count}",
        f"Fundraising score: {contact.fundraising_relevance_score} ({contact.fundraising_relevance_tier})",
        f"Detected topics: {', '.join(context.detected_topics or []) if context else 'none'}",
        f"Auto context: {context.auto_context_detailed if context else 'n/a'}",
        "",
        "Sent email history (newest first):",
    ]
    for msg in messages:
        lines.append(
            f"- [{msg.sent_datetime:%Y-%m-%d}] Subject: {msg.subject or '(no subject)'}\n"
            f"  Preview: {msg.body_preview or '(empty)'}"
        )
    return "\n".join(lines)


async def build_full_context(db: Session, contact_id: str) -> str:
    contact = _get_contact(db, contact_id)
    messages = _contact_messages(db, contact_id)
    base = build_metadata_context(contact, messages)

    graph = GraphClient(db)
    try:
        graph.ensure_access_token()
    except GraphAuthError as exc:
        raise AIServiceError(str(exc)) from exc

    body_sections: list[str] = []
    for msg in messages[:10]:
        try:
            body_data = await graph.fetch_message_body(msg.graph_message_id)
            body_content = body_data.get("body", {})
            raw = body_content.get("content", "")
            if body_content.get("contentType") == "html":
                raw = strip_html(raw)
            else:
                raw = raw.strip()
            if raw:
                truncated = raw[:MAX_BODY_CHARS]
                body_sections.append(
                    f"--- Full body [{msg.sent_datetime:%Y-%m-%d}] {msg.subject} ---\n{truncated}"
                )
        except Exception:
            continue

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
        "You summarize email thread history for fundraising and BD relationship management.",
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
