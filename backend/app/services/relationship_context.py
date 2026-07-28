"""Structured relationship evidence for a contact.

Everything here is computed from the database - no LLM calls - so it is cheap enough to
run per contact and safe to feed to a model as grounding. The point is to replace bare
volume stats ("exchanged 64 emails") with facts that actually characterise a relationship:

- who talks to whom, and whether the contact ever replies (reciprocity)
- how quickly they reply, and who sent the last word
- the threads that carry real content, with quoted history stripped
- shared connections: other people who appear on the same threads
- concrete deal/topic signals drawn from subjects

``build_relationship_evidence`` returns a dict; ``format_relationship_evidence`` renders it
as the grounding block for a prompt.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.contact import Contact, ContactEmailLink
from app.models.message import EmailMessage
from app.services.text_utils import is_trivial_preview, strip_quoted_reply

MAX_THREAD_HIGHLIGHTS = 6
MAX_SHARED_CONNECTIONS = 8
MAX_SUBJECT_SAMPLES = 12
PREVIEW_CHARS = 420


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _direction_of(message: EmailMessage) -> str:
    return "inbound" if message.direction == "inbound" else "outbound"


def _contact_all_messages(db: Session, contact_id: str) -> list[EmailMessage]:
    return (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id)
        .order_by(EmailMessage.sent_datetime.asc())
        .all()
    )


def _reply_latency_days(outbound: list[EmailMessage], inbound: list[EmailMessage]) -> float | None:
    """Median days between one of our messages and the contact's next reply."""
    if not outbound or not inbound:
        return None
    inbound_times = sorted(t for m in inbound if (t := _as_utc(m.sent_datetime)))
    gaps: list[float] = []
    for out in outbound:
        sent = _as_utc(out.sent_datetime)
        if sent is None:
            continue
        reply = next((t for t in inbound_times if t > sent), None)
        if reply is not None:
            gaps.append((reply - sent).total_seconds() / 86400.0)
    if not gaps:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    return round(gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2, 1)


def _shared_connections(db: Session, contact_id: str, conversation_ids: list[str]) -> list[dict]:
    """Other contacts who appear on the same conversations - i.e. mutual connections."""
    if not conversation_ids:
        return []

    rows = (
        db.query(Contact.full_name, Contact.primary_email, Contact.company_name, EmailMessage.conversation_id)
        .join(ContactEmailLink, ContactEmailLink.contact_id == Contact.id)
        .join(EmailMessage, EmailMessage.id == ContactEmailLink.email_message_id)
        .filter(
            EmailMessage.conversation_id.in_(conversation_ids),
            Contact.id != contact_id,
            Contact.is_internal.is_(False),
            Contact.is_excluded.is_(False),
        )
        .all()
    )

    by_email: dict[str, dict] = {}
    for full_name, email, company, conversation_id in rows:
        entry = by_email.setdefault(
            email,
            {"name": full_name or email, "email": email, "company": company, "threads": set()},
        )
        entry["threads"].add(conversation_id)

    ranked = sorted(by_email.values(), key=lambda e: len(e["threads"]), reverse=True)
    return [
        {"name": e["name"], "email": e["email"], "company": e["company"], "shared_threads": len(e["threads"])}
        for e in ranked[:MAX_SHARED_CONNECTIONS]
    ]


def _thread_highlights(messages: list[EmailMessage]) -> list[dict]:
    """Threads with real content, newest first, quoted history removed."""
    by_conversation: dict[str, list[EmailMessage]] = {}
    for message in messages:
        key = message.conversation_id or f"single:{message.id}"
        by_conversation.setdefault(key, []).append(message)

    threads: list[dict] = []
    for conversation_id, thread_messages in by_conversation.items():
        ordered = sorted(thread_messages, key=lambda m: m.sent_datetime)
        substantive = [m for m in ordered if not is_trivial_preview(m.body_preview)]
        anchor = (substantive or ordered)[-1]
        last = ordered[-1]
        threads.append(
            {
                "subject": anchor.subject or last.subject or "(no subject)",
                "conversation_id": conversation_id,
                "message_count": len(ordered),
                "first_at": ordered[0].sent_datetime,
                "last_at": last.sent_datetime,
                "last_direction": _direction_of(last),
                "inbound_count": sum(1 for m in ordered if _direction_of(m) == "inbound"),
                "outbound_count": sum(1 for m in ordered if _direction_of(m) == "outbound"),
                "has_attachments": any(m.has_attachments for m in ordered),
                "excerpt": strip_quoted_reply(anchor.body_preview)[:PREVIEW_CHARS],
                "excerpt_direction": _direction_of(anchor),
                "excerpt_at": anchor.sent_datetime,
            }
        )

    threads.sort(key=lambda t: (t["last_at"] is not None, t["last_at"]), reverse=True)
    return threads[:MAX_THREAD_HIGHLIGHTS]


def build_relationship_evidence(db: Session, contact: Contact) -> dict:
    """Collect grounded relationship facts for one contact."""
    messages = _contact_all_messages(db, contact.id)
    outbound = [m for m in messages if _direction_of(m) == "outbound"]
    inbound = [m for m in messages if _direction_of(m) == "inbound"]

    conversation_ids = sorted({m.conversation_id for m in messages if m.conversation_id})
    last_message = messages[-1] if messages else None
    last_inbound = inbound[-1] if inbound else None
    last_outbound = outbound[-1] if outbound else None

    subjects = [m.subject for m in messages if m.subject]
    subject_counts = Counter(subjects)

    return {
        "contact": {
            "name": contact.full_name,
            "email": contact.primary_email,
            "company": contact.company_name,
            "domain": contact.company_domain,
            "contact_type": contact.contact_type,
            "is_personal_email": contact.is_personal_email,
        },
        "cadence": {
            "total_messages": len(messages),
            "we_sent": len(outbound),
            "they_sent": len(inbound),
            "threads": len(conversation_ids) or (1 if messages else 0),
            "first_at": messages[0].sent_datetime if messages else None,
            "last_at": last_message.sent_datetime if last_message else None,
            "last_direction": _direction_of(last_message) if last_message else None,
            "they_ever_replied": bool(inbound),
            "median_reply_days": _reply_latency_days(outbound, inbound),
            "awaiting_reply": bool(contact.awaiting_reply),
            "days_since_outreach": contact.days_since_outreach,
            "attachments_exchanged": sum(1 for m in messages if m.has_attachments),
        },
        "last_inbound": (
            {
                "subject": last_inbound.subject,
                "at": last_inbound.sent_datetime,
                "excerpt": strip_quoted_reply(last_inbound.body_preview)[:PREVIEW_CHARS],
            }
            if last_inbound
            else None
        ),
        "last_outbound": (
            {
                "subject": last_outbound.subject,
                "at": last_outbound.sent_datetime,
                "excerpt": strip_quoted_reply(last_outbound.body_preview)[:PREVIEW_CHARS],
            }
            if last_outbound
            else None
        ),
        "threads": _thread_highlights(messages),
        "recurring_subjects": [s for s, n in subject_counts.most_common(MAX_SUBJECT_SAMPLES) if n > 1],
        "shared_connections": _shared_connections(db, contact.id, conversation_ids),
    }


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%b %Y") if value else "unknown"


def _fmt_day(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "unknown date"


def format_relationship_evidence(evidence: dict) -> str:
    """Render evidence as a grounding block. Directions are always explicit."""
    contact = evidence["contact"]
    cadence = evidence["cadence"]

    lines: list[str] = [
        "CONTACT",
        f"  Name: {contact['name'] or '(unknown)'} <{contact['email']}>",
        f"  Company: {contact['company'] or 'Unknown'}"
        + (f" ({contact['domain']})" if contact["domain"] else "")
        + ("  [personal email address]" if contact["is_personal_email"] else ""),
    ]
    if contact["contact_type"]:
        lines.append(f"  Previously categorised as: {contact['contact_type']}")

    reciprocity = (
        "never replied to us" if not cadence["they_ever_replied"] else f"replied {cadence['they_sent']} time(s)"
    )
    lines += [
        "",
        "CORRESPONDENCE PATTERN",
        f"  Window: {_fmt_date(cadence['first_at'])} -> {_fmt_date(cadence['last_at'])}",
        f"  We sent {cadence['we_sent']}; they sent {cadence['they_sent']} ({reciprocity})",
        f"  Threads: {cadence['threads']}; attachments exchanged: {cadence['attachments_exchanged']}",
        f"  Last message was {cadence['last_direction'] or 'unknown'}",
    ]
    if cadence["median_reply_days"] is not None:
        lines.append(f"  Typical reply time: about {cadence['median_reply_days']} day(s)")
    if cadence["awaiting_reply"] and cadence["days_since_outreach"] is not None:
        lines.append(f"  Currently awaiting their reply for {cadence['days_since_outreach']} day(s)")

    if evidence["last_inbound"]:
        item = evidence["last_inbound"]
        lines += [
            "",
            f"MOST RECENT MESSAGE *FROM THEM* ({_fmt_day(item['at'])})",
            f"  Subject: {item['subject'] or '(no subject)'}",
            f"  They wrote: {item['excerpt'] or '(no readable text)'}",
        ]
    else:
        lines += ["", "MOST RECENT MESSAGE *FROM THEM*: none - they have never written to us"]

    if evidence["last_outbound"]:
        item = evidence["last_outbound"]
        lines += [
            "",
            f"MOST RECENT MESSAGE *FROM US* ({_fmt_day(item['at'])})",
            f"  Subject: {item['subject'] or '(no subject)'}",
            f"  We wrote: {item['excerpt'] or '(no readable text)'}",
        ]

    if evidence["threads"]:
        lines += ["", "THREADS (newest first)"]
        for thread in evidence["threads"]:
            who = "they" if thread["excerpt_direction"] == "inbound" else "we"
            lines.append(
                f"  - \"{thread['subject']}\" | {thread['message_count']} msg"
                f" ({thread['outbound_count']} from us / {thread['inbound_count']} from them)"
                f" | {_fmt_date(thread['first_at'])}->{_fmt_date(thread['last_at'])}"
                f" | last was {thread['last_direction']}"
                + ("  [has attachments]" if thread["has_attachments"] else "")
            )
            if thread["excerpt"]:
                lines.append(f"      {who} wrote ({_fmt_day(thread['excerpt_at'])}): {thread['excerpt']}")

    if evidence["recurring_subjects"]:
        lines += ["", "RECURRING SUBJECTS (repeat threads - likely ongoing workstreams)"]
        lines += [f"  - {s}" for s in evidence["recurring_subjects"]]

    if evidence["shared_connections"]:
        lines += ["", "SHARED CONNECTIONS (people on the same threads as this contact)"]
        for person in evidence["shared_connections"]:
            company = f" - {person['company']}" if person["company"] else ""
            lines.append(
                f"  - {person['name']} <{person['email']}>{company}"
                f" | {person['shared_threads']} shared thread(s)"
            )

    return "\n".join(lines)
