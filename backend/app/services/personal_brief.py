"""What has this person actually been up to?

The relationship evidence in ``relationship_context`` describes *the relationship* — who
writes to whom, which threads are live, who owes a reply. That is enough to write a
competent follow-up, but it is not enough to write an email that opens with "congratulations
on closing the Aurora deal". For that you need to have read what the person themselves said
they were doing, and to be sure they really said it.

This module does that reading. It pulls the contact's own recent messages, fetches the full
bodies where the transport allows, and asks the model to extract concrete, dateable things
the person has been doing — a closed deal, a raise, a new role, a launch, a move.

The output is only useful if it is true. Congratulating someone on an achievement that never
happened is far worse than sending a plain note, so every extracted item carries a verbatim
quote and is discarded unless that quote is actually found in the source text. The model
therefore cannot introduce a fact; it can only point at one. See ``_verify_activity``.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import EmailMessage
from app.services.text_utils import strip_quoted_reply

# How much correspondence the study pass reads. Their own words carry nearly all the signal
# about what they are doing, so most of the budget is reserved for inbound messages; ours are
# included because a reply of ours often names the thing they told us about.
# The draft is only as good as the conversation behind it, so the study reads the whole recent
# exchange rather than a sample of it: at least ten full message bodies where that many exist,
# both sides, newest first. Their own words carry most of the signal about what they are doing,
# so most of the budget is reserved for inbound messages.
STUDY_MESSAGE_LIMIT = 18
THEIR_MESSAGE_QUOTA = 11
FULL_BODY_FETCH_LIMIT = 12
BODY_CHARS = 2200
PREVIEW_CHARS = 700

# Ceilings on how hard a batch may hit each upstream at once. A batch of twenty contacts wants
# 160 message bodies and twenty model calls; sent all at once that is a self-inflicted rate
# limit, so both are capped rather than left to fan out.
MAX_CONCURRENT_BODY_FETCHES = 8
MAX_CONCURRENT_STUDIES = 4

# Kept per event loop rather than as module globals. An asyncio primitive binds itself to the
# first loop that awaits it and then refuses every other one, which is invisible under the
# server (one loop for the process lifetime) and a hard failure in any script or test that
# calls asyncio.run more than once.
_SLOTS: dict[tuple[int, str], asyncio.Semaphore] = {}


def _slots(name: str, limit: int) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), name)
    semaphore = _SLOTS.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _SLOTS[key] = semaphore
    return semaphore

# An "achievement" from three years ago is history, not news. Anything older than this is kept
# as background but never offered as something to congratulate them on.
RECENT_MONTHS = 12
MAX_ACTIVITY_ITEMS = 5
MAX_FOCUS_ITEMS = 5

# Below this there is nothing to study, and a model asked to find achievements in two lines of
# "thanks, will do" will invent them.
MIN_MATERIAL_CHARS = 200

# A quote must survive whitespace and punctuation reflow to be recognised, but nothing more.
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
MIN_QUOTE_CHARS = 12


class PersonalBriefError(Exception):
    pass


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub(" ", (text or "").lower()).strip()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _recent_for_direction(db: Session, contact_id: str, *, inbound: bool, limit: int) -> list[EmailMessage]:
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


def _study_messages(db: Session, contact_id: str) -> list[EmailMessage]:
    """Recent correspondence, weighted towards what the contact themselves wrote.

    Whatever the inbound side does not use is handed back to the outbound side, so a contact
    who has never replied still gets a full window rather than a nearly empty one.
    """
    theirs = _recent_for_direction(db, contact_id, inbound=True, limit=THEIR_MESSAGE_QUOTA)
    ours = _recent_for_direction(
        db, contact_id, inbound=False, limit=STUDY_MESSAGE_LIMIT - len(theirs)
    )
    if len(theirs) + len(ours) < STUDY_MESSAGE_LIMIT:
        theirs = _recent_for_direction(
            db, contact_id, inbound=True, limit=STUDY_MESSAGE_LIMIT - len(ours)
        )
    merged = sorted(theirs + ours, key=lambda m: m.sent_datetime or datetime.min, reverse=True)
    return merged[:STUDY_MESSAGE_LIMIT]


async def gather_study_material(db: Session, contact_id: str) -> list[dict]:
    """The text the study pass reads, newest first.

    Full bodies are fetched for the most recent messages because the stored preview is often
    only the greeting — "Hi Dalbir, hope you are well" — and the thing worth congratulating
    somebody on is usually in the second paragraph. A body that will not fetch falls back to
    the stored preview rather than dropping the message.
    """
    from app.services.mail_readers import fetch_full_body

    messages = _study_messages(db, contact_id)

    # Fetched together rather than one after another. Each body is a separate round trip to
    # Graph, so reading eight of them in sequence cost about nine seconds per contact - which,
    # multiplied across a batch of drafts, was most of the wait.
    slots = _slots("fetch", MAX_CONCURRENT_BODY_FETCHES)

    async def body(message):
        async with slots:
            return await fetch_full_body(db, message)

    fetched = await asyncio.gather(
        *(body(message) for message in messages[:FULL_BODY_FETCH_LIMIT]),
        return_exceptions=True,
    )

    material: list[dict] = []
    for index, message in enumerate(messages):
        text = ""
        full_body = False
        if index < len(fetched):
            raw = fetched[index]
            if isinstance(raw, str) and raw:
                text = strip_quoted_reply(raw)[:BODY_CHARS]
                full_body = bool(text.strip())
        if not text:
            text = strip_quoted_reply(message.body_preview)[:PREVIEW_CHARS]
        if not text.strip():
            continue
        material.append(
            {
                "ref": f"m{index + 1}",
                "message_id": message.id,
                "direction": "them" if message.direction == "inbound" else "us",
                "subject": message.subject or "(no subject)",
                "sent_at": message.sent_datetime,
                "text": text.strip(),
                # Whether this is the whole message or only the stored preview, so the reviewer
                # can tell how deeply the conversation was actually read.
                "full_body": full_body,
            }
        )
    return material


def _material_block(material: list[dict]) -> str:
    lines: list[str] = []
    for item in material:
        who = "THEY WROTE" if item["direction"] == "them" else "WE WROTE"
        when = item["sent_at"].strftime("%Y-%m-%d") if item["sent_at"] else "unknown date"
        lines.append(
            f"[{item['ref']}] {when} · {who} · Subject: {item['subject']}\n{item['text']}"
        )
    return "\n\n".join(lines)


STUDY_SYSTEM_PROMPT = (
    "You are a research analyst. You read one person's correspondence and report, strictly "
    "factually, everything known about them that is worth referencing when writing to them.\n\n"
    "Your output is quoted back to that person by name, so a fabricated or embellished claim is "
    "the worst possible failure. Never guess. But also never come back empty-handed when the "
    "correspondence plainly says something about them: a stated role, a request they made, an "
    "offer they extended, a problem they are working on and an outstanding commitment are all "
    "real, referenceable facts even though none of them is an achievement.\n\n"
    "Absolute rules:\n"
    "- Every item must be supported by a quote copied character-for-character from the "
    "material. Do not paraphrase inside the quote, do not fix its spelling, do not join two "
    "separate sentences into one quote.\n"
    "- Report only what the text states. Do not infer a promotion from a changed signature, a "
    "closed deal from an attachment name, or a launch from an invitation.\n"
    "- The material labels each message THEY WROTE or WE WROTE. Never report something we did "
    "as something they did. An offer we made to them is not something they offered us.\n"
    "- Marketing blasts, newsletters, calendar invites and automated notifications describe an "
    "organisation, not this person. Ignore them.\n"
    "- Return JSON only. No prose, no markdown fence."
)

STUDY_USER_TEMPLATE = """Study {name}{company_clause} from the correspondence below.

You are producing the briefing someone reads immediately before writing to this person, so it
must answer two questions: what have they been doing, and what do we know about them.

Return exactly this JSON shape:

{{
  "activity": [
    {{
      "headline": "short phrase naming the thing they did, e.g. \\"closed the Aurora acquisition\\"",
      "detail": "one sentence of specifics - who, what, which company or deal",
      "kind": "deal|funding|role_change|launch|award|hiring|expansion|event|milestone|personal|other",
      "ref": "the [m#] marker of the message this came from",
      "quote": "verbatim sentence from that message that proves it",
      "who_said_it": "them|us"
    }}
  ],
  "about_them": [
    {{
      "headline": "short phrase, e.g. \\"offered to introduce us to his US partners\\"",
      "detail": "one sentence of specifics",
      "kind": "role|working_on|asked_us_for|offered_us|commitment|met_them|interest|constraint|other",
      "ref": "the [m#] marker of the message this came from",
      "quote": "verbatim sentence from that message that proves it",
      "who_said_it": "them|us"
    }}
  ],
  "focus": ["what they are actively working on or care about, in their words, 5 max"],
  "note": "one sentence on anything that changes how you would approach them, or \\"\\""
}}

"activity" is for things that HAPPENED and could be congratulated or acknowledged as news:
- Concrete events with a subject and an outcome. "Discussed the market" is not activity;
  "closed the Series B for Nexa Bio" is.
- Most recent and most notable first, at most {max_items}.
- Leave it empty if the correspondence contains no such event. That is normal and expected.

"about_them" is for everything else the mail establishes about this person, and it should
almost never be empty when there is real correspondence. Include:
- their role, remit or company, where stated
- what they are working on or trying to solve
- anything they ASKED US for, and anything they OFFERED us - an introduction, a referral, a
  meeting, a document
- commitments either side made that are still open
- where and when we met them
- stated interests, preferences or constraints
At most {max_items} items, most useful for writing to them first.

Both lists follow the same rule: omit anything you cannot quote.

=== CORRESPONDENCE ===
{material}"""


def _json_object(raw: str) -> dict:
    """First JSON object in a model response, tolerating fences and stray prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end])


def _verify_activity(items: list, material: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Keep only the items whose quote genuinely appears in the correspondence.

    This is what makes the brief safe to put in front of a recipient. The model is free to
    choose *which* fact to surface, but it cannot introduce one: an item whose quote is not
    found in the material is dropped, and an item citing the wrong message is re-attributed to
    the message that actually contains the quote rather than being trusted or discarded.
    """
    if not isinstance(items, list):
        return []

    by_ref = {item["ref"]: item for item in material}
    normalized = [(item, _normalize(item["text"])) for item in material]
    now = _as_utc(now) or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=int(RECENT_MONTHS * 30.44))

    verified: list[dict] = []
    seen: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            continue
        quote = (entry.get("quote") or "").strip()
        headline = (entry.get("headline") or "").strip()
        if not headline or len(_normalize(quote)) < MIN_QUOTE_CHARS:
            continue

        needle = _normalize(quote)
        source = by_ref.get(str(entry.get("ref") or "").strip())
        if source is None or needle not in _normalize(source["text"]):
            source = next((item for item, text in normalized if needle in text), None)
        if source is None:
            continue  # quote is not in the correspondence — the item is unsupported

        key = _normalize(headline)
        if key in seen:
            continue
        seen.add(key)

        sent_at = _as_utc(source["sent_at"])
        verified.append(
            {
                "headline": headline,
                "detail": (entry.get("detail") or "").strip(),
                "kind": (entry.get("kind") or "other").strip().lower(),
                "quote": quote,
                # Attribution comes from the message we matched, never from the model: it is
                # the difference between "congratulations on your raise" and an embarrassment.
                "said_by": source["direction"],
                "source_subject": source["subject"],
                "source_message_id": source["message_id"],
                "source_date": sent_at.strftime("%Y-%m-%d") if sent_at else None,
                "is_recent": bool(sent_at and sent_at >= cutoff),
            }
        )
        if len(verified) >= MAX_ACTIVITY_ITEMS:
            break

    verified.sort(key=lambda item: (item["is_recent"], item["source_date"] or ""), reverse=True)
    return verified


def _clean_focus(values) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text.lower() not in {v.lower() for v in out}:
            out.append(text[:160])
        if len(out) >= MAX_FOCUS_ITEMS:
            break
    return out


# How many of the contact's own messages are kept verbatim in the brief. They are stored
# rather than re-fetched because the brief is cached: a regenerate would otherwise re-open
# every mailbox to read text we had already read.
KEEP_THEIR_WORDS = 5


def _their_words(material: list[dict]) -> list[dict]:
    theirs = [item for item in material if item["direction"] == "them"][:KEEP_THEIR_WORDS]
    return [
        {
            "date": item["sent_at"].strftime("%Y-%m-%d") if item["sent_at"] else None,
            "subject": item["subject"],
            "text": item["text"],
        }
        for item in theirs
    ]


def empty_brief(reason: str) -> dict:
    return {
        "activity": [],
        "about_them": [],
        "focus": [],
        "note": "",
        "reason": reason,
        "studied_messages": 0,
        "full_bodies_read": 0,
        "their_words": [],
        "generated_at": datetime.utcnow().isoformat(),
    }


async def extract_brief(contact: Contact, material: list[dict]) -> dict:
    """Turn gathered correspondence into a verified brief. Touches no database.

    Kept free of the session so a batch can run several of these at once: the model call is the
    slow part, and it is handed to a worker thread because the Anthropic client is synchronous
    and would otherwise block every other study in the batch.
    """
    from app.services.ai_service import _call_anthropic

    words = _their_words(material)
    if sum(len(item["text"]) for item in material) < MIN_MATERIAL_CHARS:
        return {**empty_brief("not enough correspondence to study"), "their_words": words}

    company = contact.company_name or contact.company_domain
    prompt = STUDY_USER_TEMPLATE.format(
        name=contact.full_name or contact.primary_email,
        company_clause=f" of {company}" if company else "",
        max_items=MAX_ACTIVITY_ITEMS,
        material=_material_block(material),
    )

    try:
        async with _slots("study", MAX_CONCURRENT_STUDIES):
            raw = await asyncio.to_thread(
                _call_anthropic, STUDY_SYSTEM_PROMPT, prompt, max_tokens=2000
            )
        payload = _json_object(raw)
    except Exception as exc:  # noqa: BLE001 - the draft is still writable without a brief
        return {**empty_brief(f"study pass unavailable: {exc}"), "their_words": words}

    proposed = payload.get("activity")
    activity = _verify_activity(proposed, material)

    # Verified separately, then de-duplicated against the activity list: the model routinely
    # reports the same fact in both, and seeing it twice on the card reads as a bug.
    proposed_about = payload.get("about_them")
    already = {_normalize(item["headline"]) for item in activity}
    about_them = [
        item
        for item in _verify_activity(proposed_about, material)
        if _normalize(item["headline"]) not in already
    ]

    dropped = 0
    for candidate, kept in ((proposed, activity), (proposed_about, about_them)):
        if isinstance(candidate, list):
            dropped += max(0, len(candidate) - len(kept))

    return {
        "activity": activity,
        "about_them": about_them,
        "focus": _clean_focus(payload.get("focus")),
        "note": str(payload.get("note") or "").strip()[:400],
        "reason": "" if (activity or about_them) else "nothing quotable in this correspondence",
        "studied_messages": len(material),
        "full_bodies_read": sum(1 for item in material if item.get("full_body")),
        "their_words": words,
        # Surfaced so a contact whose every proposed fact failed verification is visible as a
        # model problem rather than looking like a contact with nothing going on.
        "unverified_dropped": max(0, dropped),
        "generated_at": datetime.utcnow().isoformat(),
    }


async def build_personal_brief(db: Session, contact: Contact) -> dict:
    """Read the contact's recent mail and return verified facts about what they are doing.

    Never raises for want of material or a bad model response: the brief enriches an email that
    can still be written without it, so an empty brief is a normal outcome, not a failure.
    """
    material = await gather_study_material(db, contact.id)
    return await extract_brief(contact, material)


def _brief_is_usable(brief: dict | None) -> bool:
    return bool(brief) and isinstance(brief, dict) and "activity" in brief


def cached_brief(contact: Contact) -> dict | None:
    """The stored brief if it is still current, else None.

    Superseded as soon as the contact writes again: an opener built on the state of the
    relationship two messages ago reads as though nobody was paying attention, which is the
    exact failure this whole module exists to fix.
    """
    ctx: ContactContext | None = contact.context
    cached = ctx.ai_personal_brief if ctx else None
    if not _brief_is_usable(cached):
        return None

    generated = None
    raw_generated = cached.get("generated_at")
    if raw_generated:
        try:
            generated = datetime.fromisoformat(raw_generated)
        except ValueError:
            generated = None
    if generated and contact.last_contacted_at and contact.last_contacted_at > generated:
        return None
    return cached


def _store_brief(db: Session, contact: Contact, brief: dict) -> None:
    ctx = contact.context
    if ctx is None:
        ctx = ContactContext(contact_id=contact.id)
        db.add(ctx)
        db.flush()
        contact.context = ctx
    ctx.ai_personal_brief = brief
    ctx.updated_at = datetime.utcnow()


async def get_personal_brief(db: Session, contact: Contact, *, force: bool = False) -> dict:
    """Cached ``build_personal_brief``. Recomputed when new mail has arrived since.

    Drafting a batch of twenty and then regenerating three of them should not re-read twenty
    mailboxes, so the brief is stored beside the other AI context and reused until the contact
    writes again.
    """
    if not force:
        cached = cached_brief(contact)
        if cached is not None:
            return {**cached, "cached": True}

    brief = await build_personal_brief(db, contact)
    _store_brief(db, contact, brief)
    db.commit()
    return {**brief, "cached": False}


async def prewarm_briefs(db: Session, contacts: list[Contact], *, force: bool = False) -> int:
    """Study a whole batch of people at once, before any of them are written to.

    Drafting is sequential, one contact at a time, and that is fine for the writing itself. It
    is not fine for the study, which is dominated by waiting: eight message bodies and a model
    call per person, none of which needs the previous person to have finished. Running the
    batch's studies together turns that wait from N times one study into roughly one.

    Returns how many contacts were studied. Failures are not raised - a contact whose study
    fails simply has no brief, and drafting falls back to the last real message.
    """
    pending = [c for c in contacts if force or cached_brief(c) is None]
    if not pending:
        return 0

    # Material first, in the caller's coroutine: the queries run against the shared session, so
    # they must not be interleaved with each other's transactions.
    material_by_contact: list[tuple[Contact, list[dict]]] = []
    for contact in pending:
        material_by_contact.append((contact, await gather_study_material(db, contact.id)))

    briefs = await asyncio.gather(
        *(extract_brief(contact, material) for contact, material in material_by_contact),
        return_exceptions=True,
    )

    studied = 0
    for (contact, _), brief in zip(material_by_contact, briefs):
        if isinstance(brief, BaseException):
            continue
        _store_brief(db, contact, brief)
        studied += 1
    db.commit()
    return studied


def format_personal_brief(brief: dict | None) -> str:
    """Render the brief as a grounding block, stating plainly when there is nothing to use.

    The "nothing verified" wording is deliberate and load-bearing. Given a silent empty section
    a model will reach for a generic "hope things are going well at <company>"; told explicitly
    that no achievement was confirmed, it opens on the last real message instead.
    """
    if not brief or not isinstance(brief, dict):
        return "WHAT THEY HAVE BEEN DOING\n  Nothing verified — do not congratulate them on anything."

    def render(items: list[dict]) -> list[str]:
        out: list[str] = []
        for item in items:
            when = item.get("source_date") or "date unknown"
            who = "they told us" if item.get("said_by") == "them" else "we said this to them"
            age = "" if item.get("is_recent") else "  [OLDER THAN A YEAR - refer to it as past, not news]"
            out.append(f"  - {item['headline']} ({when}, {who}){age}")
            if item.get("detail"):
                out.append(f"      {item['detail']}")
            out.append(f"      Exact words: \"{item['quote']}\"")
        return out

    activity = brief.get("activity") or []
    about_them = brief.get("about_them") or []
    lines = ["WHAT THEY HAVE BEEN DOING (each line is quoted from the mail — safe to reference)"]

    if activity:
        lines += render(activity)
    else:
        lines.append(
            "  No news or achievement to congratulate them on. Do NOT invent one and do NOT "
            "write 'hope things are going well at <company>'."
        )

    if about_them:
        lines += [
            "",
            "WHAT ELSE WE KNOW ABOUT THEM (also quoted — open on one of these if there is no news)",
        ]
        lines += render(about_them)
    elif not activity:
        reason = brief.get("reason") or "nothing quotable in this correspondence"
        lines.append(
            f"  Nothing at all was verifiable about this person ({reason}). Open on the "
            "substance of the last real message instead."
        )

    focus = brief.get("focus") or []
    if focus:
        lines += ["", "WHAT THEY ARE WORKING ON"]
        lines += [f"  - {value}" for value in focus]

    if brief.get("note"):
        lines += ["", f"WORTH KNOWING: {brief['note']}"]

    return "\n".join(lines)


def format_their_own_words(brief: dict | None) -> str:
    """The contact's own recent messages in full, so the draft can echo their language.

    ``build_metadata_context`` shows short previews of both sides. That is enough to know a
    thread exists and not enough to write like someone who read it — the stored previews are
    frequently just the greeting, while the substance sits in the second paragraph.
    """
    words = (brief or {}).get("their_words") or []
    if not words:
        return ""
    lines = ["THEIR OWN RECENT MESSAGES IN FULL (newest first)"]
    for item in words:
        lines.append(f"  --- {item.get('date') or 'unknown date'} · Subject: {item.get('subject')} ---")
        lines.append(f"  {item.get('text') or ''}")
    return "\n".join(lines)
