from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.contact import Contact, ContactEmailLink
from app.models.message import EmailMessage
from app.models.outreach import DraftRun, EmailDraft, OutreachPrompt
from app.services.ai_service import (
    _call_anthropic_async,
    _contact_messages,
    _get_contact,
    build_metadata_context,
)
from app.services.graph_client import GraphAuthError, GraphClient
from app.services.personal_brief import (
    format_personal_brief,
    format_their_own_words,
    get_personal_brief,
    prewarm_briefs,
)
from app.models.sender import SenderProfile
from app.services.relationship_context import build_relationship_evidence, format_relationship_evidence
from app.services.sender_profile import format_sender_profile, signature_for
from app.services.mail_sender import MailSendError, send_via_mailbox
from app.services.mailboxes import MailboxConfigError, default_mailbox, get_mailbox

DEFAULT_SYSTEM_PROMPT = (
    "You write outreach emails for Edge Investing / Galaxy Pharma.\n\n"
    "The email must read as though the sender has been paying attention to the recipient. A "
    "note that could have been sent to anybody is a failure even when every sentence in it is "
    "true. The brief includes a section headed WHAT THEY HAVE BEEN DOING, listing things the "
    "recipient has actually been up to, each one quoted from their own mail — leading with one "
    "of those is the single most important thing you do.\n\n"
    "That section is also a limit. Congratulate, acknowledge, or allude to nothing that is not "
    "in it. When it says nothing was verified, it means the mail contains no news about this "
    "person: open on the last real message instead, and never fall back on 'hope all is well "
    "at <company>' or an invented achievement. Congratulating someone on something that did "
    "not happen loses the relationship outright.\n\n"
    "The context labels every past message with who wrote it — either WE WROTE or THEY WROTE. "
    "Attributing our own words to the recipient, or theirs to us, produces an email that "
    "cannot be sent, so treat the distinction as the most important thing on the page after "
    "the brief itself.\n\n"
    "The brief also has a section headed WHO IS WRITING, holding the sender's own track record. "
    "Use it to earn the ask: a specific, quantified thing the sender has actually done is worth "
    "more than any adjective. One is enough — a paragraph of credentials reads as a CV, not a "
    "note. If that section says nothing is on file, make the ask on its own merits and claim no "
    "track record at all.\n\n"
    "Never invent facts. Do not state a job title, employer, deal, mutual contact, meeting, or "
    "commitment that is not present in the context. Never inflate a number from the sender's "
    "material — if it says $120M, it is not 'over $150M' and not 'more than $100M'.\n\n"
    "Never mention email volume, thread counts, or how long it has been since you last spoke. "
    "Those are internal metrics, not things one writes to a person."
)

DEFAULT_USER_PROMPT_TEMPLATE = """Draft a short outreach email to this contact.

Build the email in three beats, in this order:

1. THEM. Open on something specific about them, in this order of preference:
   a. something from WHAT THEY HAVE BEEN DOING — name the deal, company, role or launch. If it
      is recent and good news, congratulate them in one sentence and mean it; if it is older,
      refer to it as something you know about rather than as news.
   b. otherwise something from WHAT ELSE WE KNOW ABOUT THEM — an offer they made, a question
      they asked, what they are working on, where you met. These are just as personal; "you
      mentioned you could introduce me to your partners in the US" opens an email perfectly.
   c. only if both are empty, the substance of the most recent message THEY wrote — continue
      that thread rather than restarting the conversation.
   Use their own framing where a quote gives it to you. If they have never written to us at
   all, say plainly why you are reaching out. Never open with a generic pleasantry.

2. THE BRIDGE. One sentence that connects what they are doing to why you are writing now, and
   where the sender's own credibility belongs if it belongs anywhere. At most one concrete
   thing from WHO IS WRITING, chosen because it makes this particular ask reasonable to this
   particular person.

3. THE ASK. The purpose of the outreach, stated once, as a low-friction next step.

The SUBJECT LINE must be specific to this person and this ask — something they would open
because it is obviously not a mass send. Name the concrete thing: the company, the deal, the
introduction, the role. No more than about eight words. Never a generic label like "Quick
question", "Following up" or "Introduction".

Rules:
- Only reference specifics that appear in the context. Precision beats warmth.
- Do not stack achievements — theirs or the sender's. One of each, well chosen, beats three.
- Professional and warm, but direct. No filler openings, no flattery beyond the facts.
- Body under 160 words. Shorter is better.
- No placeholders like [Name] or [Company] — use the real values or omit the sentence.
- Sign off as "Best regards" with no name. The sender's signature block is appended
  afterwards, so writing a name here would produce two.

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
    # The stock prompt before the brief existed: correct about attribution, but it had nothing
    # to say about what the recipient had been doing, so drafts never opened on them.
    (
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
    ),
    # The stock prompt before the sender profile existed: it studied the recipient well but
    # knew nothing about who was writing, so every pitch read the same.
    (
        "You write outreach emails for Edge Investing / Galaxy Pharma.\n\n"
        "The email must read as though the sender has been paying attention to the recipient. A "
        "note that could have been sent to anybody is a failure even when every sentence in it is "
        "true. The brief includes a section headed WHAT THEY HAVE BEEN DOING, listing things the "
        "recipient has actually been up to, each one quoted from their own mail — leading with one "
        "of those is the single most important thing you do.\n\n"
        "That section is also a limit. Congratulate, acknowledge, or allude to nothing that is not "
        "in it. When it says nothing was verified, it means the mail contains no news about this "
        "person: open on the last real message instead, and never fall back on 'hope all is well "
        "at <company>' or an invented achievement. Congratulating someone on something that did "
        "not happen loses the relationship outright.\n\n"
        "The context labels every past message with who wrote it — either WE WROTE or THEY WROTE. "
        "Attributing our own words to the recipient, or theirs to us, produces an email that "
        "cannot be sent, so treat the distinction as the most important thing on the page after "
        "the brief itself.\n\n"
        "Never invent facts. Do not state a job title, employer, deal, mutual contact, meeting, or "
        "commitment that is not present in the context.\n\n"
        "Never mention email volume, thread counts, or how long it has been since you last spoke. "
        "Those are internal metrics, not things one writes to a person."
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
    """Draft a short outreach email to this contact.

Build the email in three beats, in this order:

1. THEM. Open on something specific they have been doing, taken from WHAT THEY HAVE BEEN
   DOING. Name the deal, company, role, or launch. If it is recent and good news, congratulate
   them in one sentence and mean it; if it is older, refer to it as something you know about
   rather than as news. Use their own framing where the quote gives it to you.
   If that section says nothing was verified, open instead on the substance of the most recent
   message THEY wrote — continue that thread rather than restarting the conversation. If they
   have never written to us at all, say plainly why you are reaching out. Never open with a
   generic pleasantry in either case.

2. THE BRIDGE. One sentence that connects what they are doing to why you are writing now. This
   is where the email earns the ask — the link should be real, not a pivot.

3. THE ASK. The purpose of the outreach, stated once, as a low-friction next step.

Rules:
- Only reference specifics that appear in the context. Precision beats warmth.
- Do not stack achievements. One opener, well chosen, beats three.
- Professional and warm, but direct. No filler openings, no flattery beyond the facts.
- Body under 160 words. Shorter is better.
- No placeholders like [Name] or [Company] — use the real values or omit the sentence.
- Sign off as "Best regards" with no name (the sender adds their own signature).

{custom_instructions_block}

Return ONLY in this format, with nothing before or after:
Subject: <subject line>

<body paragraphs>

=== CONTEXT ===
{context}""",
    """Draft a short outreach email to this contact.

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


def sender_profile_for(db: Session, mailbox_id: str | None):
    """The profile of whichever mailbox this draft will go out from, if there is one."""
    if not mailbox_id:
        return None
    return (
        db.query(SenderProfile)
        .filter(SenderProfile.mailbox_id == mailbox_id)
        .one_or_none()
    )


def append_signature(body: str, signature: str) -> str:
    """Put the sender's signature at the end of a draft, replacing a bare sign-off.

    The model is told to end on "Best regards" with no name; leaving that above a full block
    produces two sign-offs. Anything the model already wrote below it is dropped rather than
    kept, because that is invariably a hallucinated name.
    """
    text = (body or "").rstrip()
    signature = (signature or "").strip()
    if not signature:
        return text

    if _normalize_signature(signature) and _normalize_signature(signature) in _normalize_signature(text):
        return text  # already signed, e.g. a regenerate over an edited draft

    lines = text.split("\n")
    for index in range(len(lines) - 1, max(len(lines) - 5, -1), -1):
        stripped = lines[index].strip().rstrip(",.").lower()
        if stripped in _SIGN_OFFS:
            return "\n".join(lines[:index]).rstrip() + f"\n\n{signature}"
    return text + f"\n\n{signature}"


_SIGN_OFFS = {
    "best regards",
    "best",
    "kind regards",
    "regards",
    "warm regards",
    "sincerely",
    "thanks",
    "thank you",
    "cheers",
    "many thanks",
}


def _normalize_signature(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def build_draft_context(
    db: Session,
    contact,
    messages,
    brief: dict | None = None,
    sender=None,
    *,
    sender_name: str | None = None,
    sender_missing: bool = False,
) -> str:
    """Grounding for a draft, ordered by how much it shapes the email.

    The recipient brief goes first because it decides the opening line, which is what
    determines whether the email reads as written to this person or to a list. The sender
    profile follows, because it decides whether the middle of the email is worth reading — a
    draft that knows everything about its recipient and nothing about its sender still opens
    well and then pitches like a form letter. Relationship evidence comes next (it separates
    the last message *from them* from the last *from us*, so the draft continues the right
    conversation), then their own recent messages in full, then the labelled log of both sides.

    ``brief`` and ``sender`` are optional so the older callers that only have a contact and a
    message list keep working; without them the context is what it was before.
    """
    sections: list[str] = []
    if brief is not None:
        sections.append(format_personal_brief(brief))
    if sender is not None or sender_missing:
        sections.append(format_sender_profile(sender, sender_name))
    sections.append(format_relationship_evidence(build_relationship_evidence(db, contact)))
    own_words = format_their_own_words(brief)
    if own_words:
        sections.append(own_words)
    if messages:
        sections.append(build_metadata_context(contact, messages))
    return "\n\n".join(sections)


def _contacts_for(db: Session, contact_ids: list[str]) -> list:
    """Approved contacts from a batch, with their AI context loaded.

    Unapproved ids are dropped rather than reported: ``generate_draft_for_contact`` raises the
    real error for them a moment later, and studying someone who is about to be rejected is
    wasted work.
    """
    if not contact_ids:
        return []
    return (
        db.query(Contact)
        .options(joinedload(Contact.context))
        .filter(Contact.id.in_(contact_ids), Contact.review_status == "approved")
        .all()
    )


def summarize_personalization(brief: dict | None) -> dict:
    """What the sender is shown about why the email opens the way it does.

    Deliberately excludes ``their_words``: the card needs the evidence for the opening line,
    not a copy of the recipient's inbox. Each item keeps its quote and date so a claim in the
    email can be checked against the mail it came from without leaving the review screen.
    """
    brief = brief or {}

    def strip(items) -> list[dict]:
        return [
            {
                "headline": item.get("headline"),
                "detail": item.get("detail"),
                "quote": item.get("quote"),
                "date": item.get("source_date"),
                "said_by": item.get("said_by"),
                "is_recent": item.get("is_recent", False),
                "source_subject": item.get("source_subject"),
            }
            for item in (items or [])
        ]

    return {
        "activity": strip(brief.get("activity")),
        "about_them": strip(brief.get("about_them")),
        "focus": brief.get("focus") or [],
        "note": brief.get("note") or "",
        "studied_messages": brief.get("studied_messages", 0),
        "full_bodies_read": brief.get("full_bodies_read", 0),
        "reason": brief.get("reason") or "",
    }


async def generate_draft_for_contact(
    db: Session,
    contact_id: str,
    *,
    custom_instructions: str | None = None,
    objective: str | None = None,
    mailbox_ids: list[str] | None = None,
    restudy: bool = False,
) -> EmailDraft:
    contact = _get_contact(db, contact_id)
    if contact.review_status != "approved":
        raise OutreachError(f"Contact {contact.primary_email} is not approved for outreach")

    prompt_row = get_or_create_prompt(db)
    messages = _contact_messages(db, contact_id)
    # Read what this person has been doing before writing to them. Cached per contact and
    # refreshed when they write again, so a bulk run studies each person once.
    brief = await get_personal_brief(db, contact, force=restudy)

    # Decide the sending identity *before* writing, not after: the email argues from that
    # sender's track record and is signed by them, so choosing the mailbox afterwards would
    # mean a Galaxy pitch going out over an Edge Investing signature.
    existing_draft = (
        db.query(EmailDraft)
        .filter(EmailDraft.contact_id == contact_id, EmailDraft.status.in_(["draft", "approved"]))
        .order_by(EmailDraft.created_at.desc())
        .first()
    )
    mailbox_id = (existing_draft.sending_mailbox_id if existing_draft else None) or resolve_source_mailbox(
        db, contact_id, mailbox_ids
    )
    sender = sender_profile_for(db, mailbox_id)
    sender_name = None
    if mailbox_id:
        try:
            mailbox = get_mailbox(mailbox_id)
            sender_name = (mailbox.from_name or "").strip() or None
        except MailboxConfigError:
            mailbox = None

    context = build_draft_context(
        db,
        contact,
        messages,
        brief,
        sender,
        sender_name=sender_name,
        sender_missing=bool(mailbox_id),
    )

    instructions = custom_instructions
    if objective and objective.strip():
        goal = f"The purpose of this outreach: {objective.strip()}"
        instructions = f"{goal}\n{custom_instructions.strip()}" if custom_instructions else goal

    user_prompt = build_user_prompt(prompt_row.user_prompt_template, context, instructions)

    # Off the event loop: a blocking call here freezes progress polls, every other request,
    # and the health check Azure uses to decide whether the container is alive.
    raw = await _call_anthropic_async(prompt_row.system_prompt, user_prompt)
    subject, body = _parse_draft_response(raw)
    body = append_signature(body, signature_for(sender, sender_name))

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
    draft.personalization = summarize_personalization(brief)
    draft.error_message = None
    draft.updated_at = datetime.utcnow()

    # Route the reply back through the mailbox that already holds this relationship, so a
    # bulk send needs no per-draft decision. An identity chosen by hand is left alone; either
    # way it matches the profile and signature the email was written from above.
    draft.sending_mailbox_id = draft.sending_mailbox_id or mailbox_id

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
    restudy: bool = False,
) -> list[dict]:
    # Study everyone first, together. Writing the emails one after another is fine — it is fast
    # and each one needs the previous commit — but making each contact wait for the previous
    # contact's mailbox reads and study call is what made a batch of twenty feel broken.
    await prewarm_briefs(db, _contacts_for(db, contact_ids), force=restudy)

    results: list[dict] = []
    for contact_id in contact_ids:
        try:
            draft = await generate_draft_for_contact(
                db,
                contact_id,
                custom_instructions=custom_instructions,
                objective=objective,
                mailbox_ids=mailbox_ids,
                # The prewarm above already honoured ``restudy``. Passing it on would study
                # every contact a second time, at full cost, for an identical answer.
                restudy=False,
            )
            results.append({"contact_id": contact_id, "draft_id": draft.id, "status": "ok"})
        except Exception as exc:
            results.append({"contact_id": contact_id, "status": "error", "error": str(exc)})
    return results


# A job whose process died leaves a row saying "running" forever. Anything older than this with
# no progress is treated as dead so the next request is not refused on its behalf.
STALE_DRAFT_RUN_MINUTES = 30


class DraftRunAlreadyActive(OutreachError):
    def __init__(self, run: DraftRun) -> None:
        super().__init__(
            f"A drafting run is already in progress ({run.completed}/{run.total} done)"
        )
        self.run = run


def draft_run_to_dict(run: DraftRun) -> dict:
    done = run.completed + run.failed
    return {
        "id": run.id,
        "status": run.status,
        "phase": run.phase,
        "total": run.total,
        "completed": run.completed,
        "failed": run.failed,
        "done": done,
        "percent": int(round(100 * done / run.total)) if run.total else 0,
        "current_label": run.current_label,
        "objective": run.objective,
        "draft_ids": run.draft_ids or [],
        "errors": run.errors or [],
        "error_message": run.error_message,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "completed_at": run.completed_at,
    }


def draft_run_people(db: Session, run: DraftRun) -> list[dict]:
    """Per-person state for the progress list: who is written, who is next, who failed.

    A percentage tells you how much is left but not whose email exists yet, which is the thing
    worth knowing when a batch is part way through. Derived from what has actually been
    written rather than from a counter, so it stays honest if a run is interrupted.
    """
    contact_ids = list(run.contact_ids or [])
    if not contact_ids:
        return []

    drafted: dict[str, str] = {}
    for draft in drafts_by_ids(db, run.draft_ids or []):
        drafted[draft.contact_id] = draft.id

    failures = {
        entry.get("contact_id"): entry.get("error")
        for entry in (run.errors or [])
        if isinstance(entry, dict)
    }

    names = {
        contact.id: (contact.full_name or contact.primary_email)
        for contact in db.query(Contact).filter(Contact.id.in_(contact_ids)).all()
    }

    # Contacts are written in the order they were queued, so the first unfinished one is the
    # one in hand. Only meaningful while the run is live.
    finished = run.completed + run.failed
    people: list[dict] = []
    for index, contact_id in enumerate(contact_ids):
        if contact_id in failures:
            status = "failed"
        elif contact_id in drafted:
            status = "done"
        elif run.status == "running" and index == finished and run.phase == "writing":
            status = "writing"
        elif run.status == "running":
            status = "pending"
        else:
            status = "skipped"
        people.append(
            {
                "contact_id": contact_id,
                "name": names.get(contact_id, contact_id),
                "status": status,
                "draft_id": drafted.get(contact_id),
                "error": failures.get(contact_id),
            }
        )
    return people


def reap_stale_draft_runs(db: Session, *, older_than: timedelta | None = None) -> int:
    """Close out runs whose process is gone.

    Without this a restart mid-batch — a deploy, a container recycle — leaves a row at
    "running" and every later attempt is rejected as a duplicate of a job that is not there.
    """
    cutoff = datetime.utcnow() - (older_than or timedelta(minutes=STALE_DRAFT_RUN_MINUTES))
    stale = (
        db.query(DraftRun)
        .filter(DraftRun.status == "running", DraftRun.updated_at < cutoff)
        .all()
    )
    for run in stale:
        run.status = "failed"
        run.error_message = "Interrupted — the server restarted while this run was in progress."
        run.completed_at = datetime.utcnow()
    if stale:
        db.commit()
    return len(stale)


def active_draft_run(db: Session) -> DraftRun | None:
    reap_stale_draft_runs(db)
    return (
        db.query(DraftRun)
        .filter(DraftRun.status == "running")
        .order_by(DraftRun.started_at.desc())
        .first()
    )


def latest_draft_run(db: Session) -> DraftRun | None:
    reap_stale_draft_runs(db)
    return db.query(DraftRun).order_by(DraftRun.started_at.desc()).first()


def create_draft_run(
    db: Session,
    contact_ids: list[str],
    *,
    custom_instructions: str | None = None,
    objective: str | None = None,
    mailbox_ids: list[str] | None = None,
) -> DraftRun:
    """Queue a bulk drafting job. Raises if one is already running.

    Two concurrent runs over overlapping contacts would fight over the same draft rows, so the
    caller is handed the existing run instead of quietly starting a second one.
    """
    if not contact_ids:
        raise OutreachError("Select at least one contact")

    existing = active_draft_run(db)
    if existing is not None:
        raise DraftRunAlreadyActive(existing)

    run = DraftRun(
        status="running",
        phase="studying",
        total=len(contact_ids),
        contact_ids=list(contact_ids),
        mailbox_ids=list(mailbox_ids or []),
        objective=objective,
        custom_instructions=custom_instructions,
        draft_ids=[],
        errors=[],
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


async def run_draft_job(db_factory, run_id: str) -> None:
    """Execute a queued drafting run. Owns its own session — it outlives the request.

    Progress is committed after every contact so the polling endpoint reflects real work, and
    so a run interrupted halfway leaves its finished drafts behind rather than losing them.
    """
    db = db_factory()
    try:
        run = db.query(DraftRun).filter(DraftRun.id == run_id).one_or_none()
        if run is None:
            return
        contact_ids = list(run.contact_ids or [])

        try:
            contacts = _contacts_for(db, contact_ids)
            by_id = {c.id: c for c in contacts}

            run.phase = "studying"
            run.current_label = f"Reading recent mail for {len(contacts)} people"
            run.updated_at = datetime.utcnow()
            db.commit()

            await prewarm_briefs(db, contacts)

            run.phase = "writing"
            db.commit()

            draft_ids: list[str] = []
            errors: list[dict] = []
            for contact_id in contact_ids:
                contact = by_id.get(contact_id)
                run.current_label = (
                    contact.full_name or contact.primary_email if contact else contact_id
                )
                run.updated_at = datetime.utcnow()
                db.commit()
                try:
                    draft = await generate_draft_for_contact(
                        db,
                        contact_id,
                        custom_instructions=run.custom_instructions,
                        objective=run.objective,
                        mailbox_ids=run.mailbox_ids or None,
                    )
                    draft_ids.append(draft.id)
                    run.completed += 1
                except Exception as exc:  # noqa: BLE001 - one bad contact must not stop the batch
                    errors.append({"contact_id": contact_id, "error": str(exc)})
                    run.failed += 1
                # Reassigned rather than appended: SQLAlchemy only notices a JSON column
                # changing when the attribute itself is set.
                run.draft_ids = list(draft_ids)
                run.errors = list(errors)
                run.updated_at = datetime.utcnow()
                db.commit()

            run.status = "completed"
            run.phase = "done"
            run.current_label = None
            run.completed_at = datetime.utcnow()
            db.commit()
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.error_message = str(exc)
            run.completed_at = datetime.utcnow()
            db.commit()
            raise
    finally:
        db.close()


def drafts_by_ids(db: Session, draft_ids: list[str]) -> list[EmailDraft]:
    """The named drafts, in the order requested, so the UI can show them as they appear."""
    if not draft_ids:
        return []
    rows = (
        db.query(EmailDraft)
        .options(joinedload(EmailDraft.contact))
        .filter(EmailDraft.id.in_(draft_ids))
        .all()
    )
    by_id = {row.id: row for row in rows}
    return [by_id[draft_id] for draft_id in draft_ids if draft_id in by_id]


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
        "personalization": draft.personalization,
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
