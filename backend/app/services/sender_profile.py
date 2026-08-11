"""The sender's side of a draft: who is writing, and what they can credibly claim.

``personal_brief`` studies the recipient so an email can open on them. This studies the sender
so the middle of the email is worth reading. Without it every draft pitches the same abstract
thing — "we are raising a round" — regardless of who is sending it, which is precisely the gap
between a mail merge and a note from a person with a track record.

Proof points are extracted from uploaded documents under the same rule the recipient brief
uses: each one must be quotable from the document it came from. A résumé is the sender's own
claim rather than an independently verified fact, but it must at least be *their* claim and not
the model's embellishment of it — "raised $120M" must not become "raised over $150M".
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from sqlalchemy.orm import Session, joinedload

from app.models.sender import SenderDocument, SenderProfile
from app.services.document_text import guess_kind

MAX_POINTS_PER_DOCUMENT = 14
MAX_PROFILE_POINTS = 24
MAX_PROFILE_KEYWORDS = 40
MAX_KEYWORDS_PER_DOCUMENT = 18
# What the draft prompt sees. More than this crowds out the recipient, who matters more.
POINTS_IN_DRAFT_PROMPT = 10
EXTRACT_CHARS = 24_000
MAX_CONCURRENT_EXTRACTIONS = 3

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_SLOTS: dict[tuple[int, str], asyncio.Semaphore] = {}


def _slots(name: str, limit: int) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), name)
    semaphore = _SLOTS.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _SLOTS[key] = semaphore
    return semaphore


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub(" ", (text or "").lower()).strip()


EXTRACT_SYSTEM_PROMPT = (
    "You read one professional document about a person and pull out the specific, checkable "
    "things it says they have done.\n\n"
    "Everything you return is quoted back at real investors and executives in that person's own "
    "outreach, so an inflated number or an achievement they did not claim is a serious error.\n\n"
    "Rules:\n"
    "- Every proof point must be supported by text that appears in the document. Do not round "
    "numbers, do not merge two roles into one claim, do not upgrade 'supported' into 'led'.\n"
    "- Prefer quantified outcomes: capital raised, deals closed, revenue or EBITDA moved, exits, "
    "multiples, headcount, years. A number is what makes a claim land.\n"
    "- Each point must stand alone when read in an email. 'Strong leadership skills' is not a "
    "proof point; 'Raised $70M in senior debt and family office equity at Amenity Health Care' is.\n"
    "- Keywords are the domains this person can credibly speak to, in lower case.\n"
    "- Return JSON only. No prose, no markdown fence."
)

EXTRACT_USER_TEMPLATE = """Pull the proof points out of this document about {name}.

Return exactly this JSON shape:

{{
  "summary": "one sentence on what this document is",
  "kind": "resume|bio|deal_sheet|case_study|other",
  "proof_points": [
    {{"text": "the claim, written so it could be dropped into an email as-is",
      "quote": "the words in the document that support it"}}
  ],
  "keywords": ["domain this person can speak to", "..."],
  "title": "their current job title if the document states one, else \\"\\"",
  "company": "their current company if the document states one, else \\"\\"",
  "positioning": "one sentence on what they are doing now, if the document says, else \\"\\""
}}

At most {max_points} proof points, strongest and most quantified first.
At most {max_keywords} keywords.

=== DOCUMENT: {filename} ===
{content}"""


def _json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end])


def verify_points(items, document_text: str, *, source: str) -> list[dict]:
    """Keep only proof points whose supporting quote really appears in the document.

    Same guard as the recipient brief, for the same reason: the model chooses which claim to
    surface, it does not get to author one. A résumé that says "$120M" cannot become "$150M"
    because the inflated quote will not be found in the text.
    """
    if not isinstance(items, list):
        return []

    haystack = _normalize(document_text)
    kept: list[dict] = []
    seen: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            continue
        text = (entry.get("text") or "").strip()
        quote = (entry.get("quote") or "").strip()
        if len(text) < 12 or len(_normalize(quote)) < 12:
            continue
        if _normalize(quote) not in haystack:
            continue
        key = _normalize(text)
        if key in seen:
            continue
        seen.add(key)
        kept.append({"text": text[:400], "quote": quote[:400], "source": source})
        if len(kept) >= MAX_POINTS_PER_DOCUMENT:
            break
    return kept


def _clean_keywords(values, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    lowered: set[str] = set()
    for value in values:
        text = str(value).strip().lower()
        if not text or len(text) > 60 or text in lowered:
            continue
        lowered.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


async def analyse_document(name: str, filename: str, content: str) -> dict:
    """Extract proof points from one document. Never raises — text alone is still useful."""
    from app.services.ai_service import _call_anthropic

    prompt = EXTRACT_USER_TEMPLATE.format(
        name=name or "this person",
        filename=filename,
        max_points=MAX_POINTS_PER_DOCUMENT,
        max_keywords=MAX_KEYWORDS_PER_DOCUMENT,
        content=content[:EXTRACT_CHARS],
    )
    try:
        async with _slots("extract", MAX_CONCURRENT_EXTRACTIONS):
            raw = await asyncio.to_thread(
                _call_anthropic, EXTRACT_SYSTEM_PROMPT, prompt, max_tokens=3000
            )
        payload = _json_object(raw)
    except Exception as exc:  # noqa: BLE001 - the document is still stored and readable
        return {
            "status": "text_only",
            "error_message": f"Could not read proof points from this document: {exc}",
            "proof_points": [],
            "keywords": [],
            "summary": None,
            "fields": {},
        }

    points = verify_points(payload.get("proof_points"), content, source=filename)
    return {
        "status": "ready" if points else "text_only",
        "error_message": None if points else "No quotable proof points were found in this document.",
        "proof_points": points,
        "keywords": _clean_keywords(payload.get("keywords"), MAX_KEYWORDS_PER_DOCUMENT),
        "summary": str(payload.get("summary") or "").strip()[:400] or None,
        "kind": str(payload.get("kind") or "").strip().lower() or guess_kind(filename, content),
        "fields": {
            "title": str(payload.get("title") or "").strip()[:256],
            "company": str(payload.get("company") or "").strip()[:256],
            "positioning": str(payload.get("positioning") or "").strip()[:600],
        },
    }


# This tool has exactly one user, so his identity is known and there is no reason to make him
# type it into three separate forms before the first draft is any good. Seeded on creation
# only: everything here stays editable, and an edit is never overwritten.
DALBIR = {
    "display_name": "Dalbir Bains",
    "title": "CEO",
    "phone": "+1 646 957 7762",
    "linkedin_url": "https://www.linkedin.com/in/dalbir-bains/",
}

# Company and website follow the mailbox — outreach from Galaxy must not sign as Edge.
_BY_DOMAIN = {
    # No website given for Edge Investing, so none is set rather than guessed — an invented URL
    # in a signature is worse than no URL.
    "edgeinvesting.ca": {"company": "Edge Investing"},
    "galaxypharma.net": {"company": "Galaxy Pharma", "website": "galaxypharma.net"},
    "tekhqs.ai": {"company": "Tekhqs", "website": "tekhqs.com"},
    "tekhqs.com": {"company": "Tekhqs", "website": "tekhqs.com"},
}


def seed_values_for(mailbox_id: str) -> dict:
    """Defaults for a brand-new profile, keyed off the mailbox's own address."""
    try:
        from app.services.mailboxes import get_mailbox

        mailbox = get_mailbox(mailbox_id)
    except Exception:  # noqa: BLE001 - a missing mailbox just means no seed
        return {}
    if mailbox is None or not mailbox.from_email:
        return {}

    domain = mailbox.from_email.strip().lower().rsplit("@", 1)[-1]
    company = _BY_DOMAIN.get(domain)
    if company is None:
        return dict(DALBIR)
    return {**DALBIR, **company}


def get_or_create_profile(db: Session, mailbox_id: str) -> SenderProfile:
    profile = (
        db.query(SenderProfile)
        .options(joinedload(SenderProfile.documents))
        .filter(SenderProfile.mailbox_id == mailbox_id)
        .one_or_none()
    )
    if profile is None:
        profile = SenderProfile(mailbox_id=mailbox_id, **seed_values_for(mailbox_id))
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def rebuild_profile_points(db: Session, profile: SenderProfile) -> SenderProfile:
    """Merge every document's proof points into the profile, best-evidenced first.

    Points the user pinned or typed by hand are kept at the top and never dropped: the whole
    point of an editable profile is that a human can overrule the extraction.
    """
    pinned = [
        point
        for point in (profile.proof_points or [])
        if isinstance(point, dict) and point.get("pinned")
    ]
    seen = {_normalize(point.get("text", "")) for point in pinned}

    merged: list[dict] = list(pinned)
    keywords: list[str] = []
    documents = sorted(
        profile.documents, key=lambda d: (len(d.proof_points or []), d.uploaded_at), reverse=True
    )
    for document in documents:
        for point in document.proof_points or []:
            key = _normalize(point.get("text", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append({**point, "pinned": False})
            if len(merged) >= MAX_PROFILE_POINTS:
                break
        for keyword in document.keywords or []:
            if keyword not in keywords:
                keywords.append(keyword)
        if len(merged) >= MAX_PROFILE_POINTS:
            break

    profile.proof_points = merged[:MAX_PROFILE_POINTS]
    profile.keywords = keywords[:MAX_PROFILE_KEYWORDS]
    profile.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(profile)
    return profile


def profile_to_dict(profile: SenderProfile, *, include_documents: bool = True) -> dict:
    data = {
        "mailbox_id": profile.mailbox_id,
        "display_name": profile.display_name,
        "title": profile.title,
        "company": profile.company,
        "positioning": profile.positioning,
        "linkedin_url": profile.linkedin_url,
        "phone": profile.phone,
        "website": profile.website,
        "signature": profile.signature,
        "proof_points": profile.proof_points or [],
        "keywords": profile.keywords or [],
        "updated_at": profile.updated_at,
        # True as soon as anything on this profile reaches a real email. Identity fields count:
        # they build the signature that gets appended, so reporting "not set up yet" while
        # signing outgoing mail with them would be a lie the user only discovers after sending.
        "is_configured": bool(
            (profile.proof_points or [])
            or profile.positioning
            or profile.signature
            or profile.display_name
            or profile.title
            or profile.company
        ),
    }
    if include_documents:
        data["documents"] = [document_to_dict(d) for d in sorted(
            profile.documents, key=lambda d: d.uploaded_at, reverse=True
        )]
    return data


def document_to_dict(document: SenderDocument) -> dict:
    return {
        "id": document.id,
        "mailbox_id": document.mailbox_id,
        "filename": document.filename,
        "kind": document.kind,
        "char_count": document.char_count,
        "proof_point_count": len(document.proof_points or []),
        "proof_points": document.proof_points or [],
        "keywords": document.keywords or [],
        "summary": document.summary,
        "status": document.status,
        "error_message": document.error_message,
        "uploaded_at": document.uploaded_at,
    }


# The fields that make a signature worth appending. The mailbox's own from_name is not one of
# them: it is available for every mailbox whether or not anyone has set a profile up.
_SIGNATURE_FIELDS = ("display_name", "title", "company", "phone", "linkedin_url", "website")


def default_signature(profile: SenderProfile, fallback_name: str | None = None) -> str:
    """A signature built from the profile fields, used when none was typed by hand."""
    name = profile.display_name or fallback_name
    lines = [line for line in [name] if line]
    role = ", ".join(part for part in [profile.title, profile.company] if part)
    if role:
        lines.append(role)
    if profile.phone:
        lines.append(profile.phone)
    if profile.linkedin_url:
        lines.append(profile.linkedin_url)
    if profile.website:
        lines.append(profile.website)
    return "\n".join(lines)


def signature_for(profile: SenderProfile | None, fallback_name: str | None = None) -> str:
    """The block to append to a draft, or "" to leave the draft's own sign-off alone.

    Nothing is appended until a profile has actually been filled in. Outlook and Gmail add the
    user's real signature on send, so replacing "Best regards" with a bare name taken from the
    mailbox config would leave every unconfigured mailbox sending *less* than it does today.
    """
    if profile is None:
        return ""
    typed = (profile.signature or "").strip()
    if typed:
        return typed
    if not any(getattr(profile, field, None) for field in _SIGNATURE_FIELDS):
        return ""
    return default_signature(profile, fallback_name)


def format_sender_profile(profile: SenderProfile | None, fallback_name: str | None = None) -> str:
    """The sender block for a draft prompt.

    States plainly when there is nothing on file. A model given an empty section will fall back
    on generic authority claims — "we are a leading platform" — which is the sender-side version
    of the same failure the recipient brief guards against.
    """
    if profile is None or not (profile.proof_points or profile.positioning or profile.title):
        return (
            "WHO IS WRITING\n"
            "  Nothing on file about the sender. Do NOT claim a track record, a fund size, a "
            "number of deals, or any credential. Make the ask on its own merits."
        )

    lines = ["WHO IS WRITING (their own material — safe to draw on)"]
    name = profile.display_name or fallback_name
    if name:
        role = ", ".join(part for part in [profile.title, profile.company] if part)
        lines.append(f"  {name}" + (f" — {role}" if role else ""))
    elif profile.title or profile.company:
        lines.append(f"  {', '.join(part for part in [profile.title, profile.company] if part)}")
    if profile.positioning:
        lines.append(f"  Currently: {profile.positioning}")

    points = (profile.proof_points or [])[:POINTS_IN_DRAFT_PROMPT]
    if points:
        lines += ["", "  WHAT THEY HAVE ACTUALLY DONE (use at most one, only if it earns the ask)"]
        for point in points:
            lines.append(f"    - {point.get('text')}")

    if profile.keywords:
        lines += ["", f"  Credible on: {', '.join(profile.keywords[:14])}"]

    return "\n".join(lines)
