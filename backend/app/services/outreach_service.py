from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.contact import ContactEmailLink
from app.models.message import EmailMessage
from app.models.outreach import EmailDraft, OutreachPrompt
from app.services.ai_service import _call_anthropic, build_metadata_context, _contact_messages, _get_contact
from app.services.graph_client import GraphAuthError, GraphClient
from app.services.relationship_context import build_relationship_evidence, format_relationship_evidence
from app.services.mail_sender import MailSendError, send_via_mailbox
from app.services.mailboxes import MailboxConfigError, default_mailbox, get_mailbox

DEFAULT_SYSTEM_PROMPT = (
    "You write outreach emails for Edge Investing / Galaxy Pharma.\n\n"
    "The context you are given labels every past message with who wrote it — either WE WROTE "
    "or THEY WROTE. Read those labels carefully. Attributing our own words to the recipient, "
    "or theirs to us, produces an email that cannot be sent, so treat the distinction as the "
    "most important thing in the brief.\n\n"
    "Never invent facts. Do not state a job title, employer, deal, mutual contact, meeting, or "
    "commitment that is not present in the context. If you have nothing specific to reference, "
    "write a brief and honest note rather than a padded one.\n\n"
    "Never mention email volume, thread counts, or how long it has been since you last spoke. "
    "Those are internal metrics, not things one writes to a person."
)

DEFAULT_USER_PROMPT_TEMPLATE = """Draft a short outreach email to this contact.

How to use the context:
- Anchor the email on the most recent message THEY wrote, when there is one. That is the live
  thread in their mind. Continue it; do not restart the conversation.
- If they have never replied, do not imply a dialogue or shared history that did not happen.
- Reference at most one or two concrete specifics (a named deal, company, or workstream) that
  appear verbatim in the context. Precision beats warmth here.
- If the context is genuinely thin, keep the email short and general rather than inventing detail.

Requirements:
- Professional and warm, but direct. No filler openings.
- One clear ask, phrased as a low-friction next step.
- Body under 160 words. Shorter is better.
- No placeholders like [Name] or [Company] — use the real values or omit the sentence.
- Sign off as "Best regards" with no name (the sender adds their own signature).

{custom_instructions_block}

Return ONLY in this format, with nothing before or after:
Subject: <subject line>

<body paragraphs>

=== CONTEXT ===
{context}"""

# Stock prompts from earlier versions. A row still holding one of these has never been edited
# by the user, so it is safe to upgrade in place; anything else is a deliberate customisation
# and is left untouched.
_LEGACY_SYSTEM_PROMPTS = {
    (
        "You write professional, warm fundraising outreach emails for Edge Investing / Galaxy Pharma. "
        "Personalize based on prior correspondence. Never invent facts not supported by the contact data."
    ),
}

_LEGACY_USER_PROMPTS = {
    """Draft a fundraising outreach email to this contact.

Requirements:
- Professional, warm, personalized tone
- Reference prior correspondence naturally if any exists
- Clear call to action (call or meeting about a funding opportunity)
- Do not invent specific facts not supported by the data below
- Under 200 words for the body
- Sign off as "Best regards" without a name (the user will add their signature)

{custom_instructions_block}

Return ONLY in this format:
Subject: <subject line>

<body paragraphs>

Contact context:
{context}""",
}


class OutreachError(Exception):
    pass


_PREAMBLE_MARKERS = ("here is", "here's", "sure,", "certainly", "of course", "i've drafted", "draft:")


def _parse_draft_response(text: str) -> tuple[str, str]:
    """Split a model response into (subject, body).

    Tolerates a leading conversational preamble, markdown fences, and bolded labels. Falls
    back to using the first line as the subject only when it looks like a subject rather
    than prose, so a stray sentence never silently becomes the subject line.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    lines = cleaned.splitlines()

    for i, line in enumerate(lines):
        # Handles "Subject:", "**Subject:**", "### Subject:" etc.
        stripped = line.strip().lstrip("#*> ").rstrip("*").strip()
        if stripped.lower().startswith("subject:"):
            subject = stripped.split(":", 1)[1].strip().strip("*").strip()
            body = "\n".join(lines[i + 1 :]).strip()
            # Strips "Body:", "**Body:**", "*Body*:" — asterisks may sit either side of the colon.
            body = re.sub(r"^\**\s*body\s*\**\s*:\s*\**\s*", "", body, flags=re.IGNORECASE).strip()
            return subject, body

    # No explicit Subject: label. Drop any preamble line, then take the first short,
    # non-terminated line as the subject — long or punctuated lines are prose, not subjects.
    candidates = [ln for ln in lines if ln.strip()]
    if candidates and any(candidates[0].strip().lower().startswith(m) for m in _PREAMBLE_MARKERS):
        candidates = candidates[1:]
    if not candidates:
        return "", cleaned

    first = candidates[0].strip()
    if len(first) <= 120 and not first.endswith((".", "!", "?")):
        return first, "\n".join(candidates[1:]).strip()

    # Whole response is prose — keep it all as the body and let the caller/user set a subject.
    return "", "\n".join(candidates).strip()


def get_or_create_prompt(db: Session) -> OutreachPrompt:
    row = db.query(OutreachPrompt).filter(OutreachPrompt.id == "default").one_or_none()
    if row:
        # Upgrade never-edited stock prompts; preserve anything the user has customised.
        upgraded = False
        if (row.system_prompt or "").strip() in {p.strip() for p in _LEGACY_SYSTEM_PROMPTS}:
            row.system_prompt = DEFAULT_SYSTEM_PROMPT
            upgraded = True
        if (row.user_prompt_template or "").strip() in {p.strip() for p in _LEGACY_USER_PROMPTS}:
            row.user_prompt_template = DEFAULT_USER_PROMPT_TEMPLATE
            upgraded = True
        if upgraded:
            row.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
        return row
    row = OutreachPrompt(
        id="default",
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        user_prompt_template=DEFAULT_USER_PROMPT_TEMPLATE,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_prompt_config(db: Session) -> dict:
    row = get_or_create_prompt(db)
    return {
        "system_prompt": row.system_prompt,
        "user_prompt_template": row.user_prompt_template,
        "updated_at": row.updated_at,
    }


def update_prompt_config(db: Session, *, system_prompt: str | None, user_prompt_template: str | None) -> dict:
    row = get_or_create_prompt(db)
    if system_prompt is not None:
        row.system_prompt = system_prompt
    if user_prompt_template is not None:
        row.user_prompt_template = user_prompt_template
    row.updated_at = datetime.utcnow()
    db.commit()
    return get_prompt_config(db)


def build_user_prompt(
    template: str,
    context: str,
    custom_instructions: str | None,
) -> str:
    block = ""
    if custom_instructions and custom_instructions.strip():
        block = f"Additional instructions from the user:\n{custom_instructions.strip()}\n"
    return template.replace("{custom_instructions_block}", block).replace("{context}", context)


def build_draft_context(db: Session, contact, messages) -> str:
    """Grounding for a draft: relationship evidence first, then the labelled message log.

    The evidence block separates the last message *from them* and the last message *from us*,
    which is what the draft needs to continue the right conversation.
    """
    sections = [format_relationship_evidence(build_relationship_evidence(db, contact))]
    if messages:
        sections.append(build_metadata_context(contact, messages))
    return "\n\n".join(sections)


async def generate_draft_for_contact(
    db: Session,
    contact_id: str,
    *,
    custom_instructions: str | None = None,
    objective: str | None = None,
    mailbox_ids: list[str] | None = None,
) -> EmailDraft:
    contact = _get_contact(db, contact_id)
    if contact.review_status != "approved":
        raise OutreachError(f"Contact {contact.primary_email} is not approved for outreach")

    prompt_row = get_or_create_prompt(db)
    messages = _contact_messages(db, contact_id)
    context = build_draft_context(db, contact, messages)

    instructions = custom_instructions
    if objective and objective.strip():
        goal = f"The purpose of this outreach: {objective.strip()}"
        instructions = f"{goal}\n{custom_instructions.strip()}" if custom_instructions else goal

    user_prompt = build_user_prompt(prompt_row.user_prompt_template, context, instructions)

    raw = _call_anthropic(prompt_row.system_prompt, user_prompt)
    subject, body = _parse_draft_response(raw)

    existing = (
        db.query(EmailDraft)
        .filter(EmailDraft.contact_id == contact_id, EmailDraft.status.in_(["draft", "approved"]))
        .order_by(EmailDraft.created_at.desc())
        .first()
    )
    draft = existing or EmailDraft(contact_id=contact_id)
    draft.subject = subject
    draft.body = body
    draft.status = "draft"
    draft.custom_instructions = instructions
    draft.system_prompt = prompt_row.system_prompt
    draft.user_prompt = user_prompt
    draft.error_message = None
    draft.updated_at = datetime.utcnow()

    # Route the reply back through the mailbox that already holds this relationship, so a
    # bulk send needs no per-draft decision. An identity chosen by hand is left alone.
    if not draft.sending_mailbox_id:
        draft.sending_mailbox_id = resolve_source_mailbox(db, contact_id, mailbox_ids)

    if not existing:
        db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


async def generate_drafts_bulk(
    db: Session,
    contact_ids: list[str],
    *,
    custom_instructions: str | None = None,
    objective: str | None = None,
    mailbox_ids: list[str] | None = None,
) -> list[dict]:
    results: list[dict] = []
    for contact_id in contact_ids:
        try:
            draft = await generate_draft_for_contact(
                db,
                contact_id,
                custom_instructions=custom_instructions,
                objective=objective,
                mailbox_ids=mailbox_ids,
            )
            results.append({"contact_id": contact_id, "draft_id": draft.id, "status": "ok"})
        except Exception as exc:
            results.append({"contact_id": contact_id, "status": "error", "error": str(exc)})
    return results


def list_drafts(db: Session, status: str | None = None) -> list[EmailDraft]:
    query = (
        db.query(EmailDraft)
        .options(joinedload(EmailDraft.contact))
        .order_by(EmailDraft.updated_at.desc())
    )
    if status:
        query = query.filter(EmailDraft.status == status)
    return query.all()


def draft_to_dict(draft: EmailDraft) -> dict:
    contact = draft.contact
    return {
        "id": draft.id,
        "contact_id": draft.contact_id,
        "contact_name": contact.full_name if contact else None,
        "contact_email": contact.primary_email if contact else None,
        "list_number": contact.list_number if contact else None,
        "subject": draft.subject,
        "body": draft.body,
        "status": draft.status,
        "sending_mailbox_id": draft.sending_mailbox_id,
        "custom_instructions": draft.custom_instructions,
        "system_prompt": draft.system_prompt,
        "user_prompt": draft.user_prompt,
        "error_message": draft.error_message,
        "sent_at": draft.sent_at,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


def update_draft(db: Session, draft_id: str, *, subject: str | None, body: str | None, status: str | None) -> EmailDraft:
    draft = db.query(EmailDraft).filter(EmailDraft.id == draft_id).one_or_none()
    if not draft:
        raise OutreachError("Draft not found")
    if subject is not None:
        draft.subject = subject
    if body is not None:
        draft.body = body
    if status is not None:
        draft.status = status
    draft.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(draft)
    return draft


def set_draft_mailbox(db: Session, draft_id: str, mailbox_id: str) -> EmailDraft:
    """Pin which configured mailbox a draft will send from."""
    draft = db.query(EmailDraft).filter(EmailDraft.id == draft_id).one_or_none()
    if not draft:
        raise OutreachError("Draft not found")
    try:
        mailbox = get_mailbox(mailbox_id)
    except MailboxConfigError as exc:
        raise OutreachError(str(exc)) from exc
    if not mailbox.can_send:
        raise OutreachError(f"Mailbox {mailbox.id!r} is not able to send (missing credentials)")
    draft.sending_mailbox_id = mailbox.id
    draft.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(draft)
    return draft


def resolve_source_mailbox(
    db: Session, contact_id: str, allowed_mailbox_ids: list[str] | None = None
) -> str | None:
    """Which mailbox should write to this contact: the one that already knows them.

    Replying from the address a relationship actually lives on is what the recipient expects,
    so a contact reached through the Galaxy mailbox is written to from Galaxy, not from
    whichever mailbox happens to be first in the config.

    Chosen by message volume, with the most recent contact breaking ties. Restricted to
    ``allowed_mailbox_ids`` when the caller searched a specific set - if only one mailbox was
    searched, every draft comes from it, which is the single-mailbox case.
    """
    query = (
        db.query(
            EmailMessage.mailbox_id,
            func.count(EmailMessage.id).label("n"),
            func.max(EmailMessage.sent_datetime).label("latest"),
        )
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id, EmailMessage.mailbox_id.isnot(None))
        .group_by(EmailMessage.mailbox_id)
    )
    if allowed_mailbox_ids:
        query = query.filter(EmailMessage.mailbox_id.in_(allowed_mailbox_ids))

    rows = query.all()
    sendable = []
    for mailbox_id, count, latest in rows:
        try:
            mailbox = get_mailbox(mailbox_id)
        except MailboxConfigError:
            continue  # configured away since the mail was imported
        if mailbox.can_send:
            sendable.append((count, latest, mailbox_id))

    if sendable:
        sendable.sort(key=lambda row: (row[0], row[1] or datetime.min), reverse=True)
        return sendable[0][2]

    # No correspondence in the searched mailboxes: fall back to the only one searched, else
    # leave it unset so the existing default applies.
    if allowed_mailbox_ids and len(allowed_mailbox_ids) == 1:
        return allowed_mailbox_ids[0]
    return None


def _resolve_send_mailbox(draft: EmailDraft, mailbox_id: str | None):
    """Pick the mailbox for a send: explicit arg → pinned on draft → first configured.

    Returns None when no mailboxes are configured at all, which keeps the original
    single-account Outlook send path working unchanged.
    """
    chosen = mailbox_id or draft.sending_mailbox_id
    if chosen:
        return get_mailbox(chosen)
    return default_mailbox()


async def send_draft(db: Session, draft_id: str, *, mailbox_id: str | None = None) -> EmailDraft:
    draft = (
        db.query(EmailDraft)
        .options(joinedload(EmailDraft.contact))
        .filter(EmailDraft.id == draft_id)
        .one_or_none()
    )
    if not draft:
        raise OutreachError("Draft not found")
    if draft.status == "sent":
        raise OutreachError("Draft already sent")
    contact = draft.contact
    if not contact:
        raise OutreachError("Contact not found")
    if not draft.subject or not draft.body:
        raise OutreachError("Draft is missing subject or body")

    try:
        mailbox = _resolve_send_mailbox(draft, mailbox_id)
    except MailboxConfigError as exc:
        draft.error_message = str(exc)
        draft.updated_at = datetime.utcnow()
        db.commit()
        raise OutreachError(str(exc)) from exc

    try:
        if mailbox is None:
            # No OUTREACH_MAILBOXES configured — original behaviour: send as the signed-in user.
            graph = GraphClient(db)
            await graph.send_mail(
                to_email=contact.primary_email,
                to_name=contact.full_name,
                subject=draft.subject,
                body=draft.body,
            )
        else:
            await send_via_mailbox(
                db,
                mailbox,
                to_email=contact.primary_email,
                to_name=contact.full_name,
                subject=draft.subject,
                body=draft.body,
            )
            draft.sending_mailbox_id = mailbox.id
    except (GraphAuthError, MailSendError) as exc:
        draft.error_message = str(exc)
        draft.updated_at = datetime.utcnow()
        db.commit()
        raise OutreachError(str(exc)) from exc

    draft.status = "sent"
    draft.sent_at = datetime.utcnow()
    draft.error_message = None
    draft.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(draft)
    return draft


async def send_drafts(
    db: Session, draft_ids: list[str], *, mailbox_id: str | None = None
) -> list[dict]:
    """Send a specific set of drafts, each from its own pinned mailbox.

    ``mailbox_id`` overrides every draft and is for the case where the sender genuinely wants
    one identity for the whole batch. Left unset - the normal path - each draft goes out from
    the mailbox that already corresponds with its recipient.
    """
    if not draft_ids:
        raise OutreachError("No drafts selected")

    drafts = db.query(EmailDraft).filter(EmailDraft.id.in_(draft_ids)).all()
    found = {d.id for d in drafts}

    results: list[dict] = []
    for draft_id in draft_ids:
        if draft_id not in found:
            results.append({"draft_id": draft_id, "status": "error", "error": "Draft not found"})
            continue
        try:
            sent = await send_draft(db, draft_id, mailbox_id=mailbox_id)
            results.append(
                {
                    "draft_id": draft_id,
                    "status": "sent",
                    "mailbox_id": sent.sending_mailbox_id,
                    "to": sent.contact.primary_email if sent.contact else None,
                }
            )
        except Exception as exc:
            # One bad recipient must not stop the rest of the batch.
            results.append({"draft_id": draft_id, "status": "error", "error": str(exc)})
    return results


async def send_approved_drafts(db: Session, *, mailbox_id: str | None = None) -> list[dict]:
    drafts = db.query(EmailDraft).filter(EmailDraft.status == "approved").all()
    results: list[dict] = []
    for draft in drafts:
        try:
            sent = await send_draft(db, draft.id, mailbox_id=mailbox_id)
            results.append({"draft_id": draft.id, "status": "sent", "mailbox_id": sent.sending_mailbox_id})
        except Exception as exc:
            results.append({"draft_id": draft.id, "status": "error", "error": str(exc)})
    return results
