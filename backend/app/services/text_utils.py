from __future__ import annotations

import re
from html import unescape

RE_PREFIX = re.compile(r"^(re|fw|fwd|aw|sv):\s*", re.IGNORECASE)


def strip_html(html: str | None) -> str:
    """HTML mail body -> readable plain text."""
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()

NOISE_EMAIL_PATTERNS = (
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "mailer-daemon@",
    "postmaster@",
    "notifications@",
    "newsletter@",
    "bounce@",
    "automated@",
)

TRIVIAL_PREVIEW_PATTERNS = (
    r"^thanks[\s!.&,-]*$",
    r"^thank you[\s!.&,-]*$",
    r"^sounds good[\s!.&,-]*$",
    r"^yes[\s!.&,-]*$",
    r"^ok[\s!.&,-]*$",
    r"^okay[\s!.&,-]*$",
    r"^got it[\s!.&,-]*$",
    r"^perfect[\s!.&,-]*$",
    r"^will do[\s!.&,-]*$",
    r"^thanks\s*&\s*regards[\s!.&,-]*$",
)


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    return email.strip().lower()


def extract_domain(email: str) -> str | None:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        return None
    return normalized.split("@", 1)[1]


def normalize_subject(subject: str | None) -> str:
    if not subject:
        return ""
    value = subject.strip()
    while True:
        updated = RE_PREFIX.sub("", value).strip()
        if updated == value:
            break
        value = updated
    return value.lower()


def parse_display_name(recipient: dict) -> tuple[str | None, str | None]:
    # Graph API format: {"emailAddress": {"name": "...", "address": "..."}}
    email_address = recipient.get("emailAddress") or {}
    email = normalize_email(email_address.get("address"))
    name = (email_address.get("name") or "").strip() or None
    if email:
        return name, email
    # Serialized DB format: {"name": "...", "address": "..."}
    email = normalize_email(recipient.get("address"))
    name = (recipient.get("name") or "").strip() or None
    return name, email


def is_noise_email(email: str) -> bool:
    lower = email.lower()
    return any(pattern in lower for pattern in NOISE_EMAIL_PATTERNS)


def is_trivial_preview(preview: str | None) -> bool:
    if not preview:
        return True
    text = preview.strip()
    if len(text) < 40:
        for pattern in TRIVIAL_PREVIEW_PATTERNS:
            if re.match(pattern, text, re.IGNORECASE):
                return True
    return False


# Markers that begin a quoted reply chain. Graph's bodyPreview frequently runs the new
# message straight into the quoted history, which makes the previous email look like the
# current one. Cutting at the first marker keeps only what the sender actually wrote.
QUOTED_REPLY_MARKERS = (
    r"-{2,}\s*original message\s*-{2,}",
    r"-{2,}\s*forwarded message\s*-{2,}",
    r"\bon\s.{3,80}\swrote:",
    r"\bfrom:\s.{1,120}?\bsent:\s",
    r"\bfrom:\s.{1,120}?\bto:\s",
    r"_{10,}",
    r"\bsent from my \w+",
    r"\bget outlook for \w+",
)
_QUOTED_REPLY_RE = re.compile("|".join(QUOTED_REPLY_MARKERS), re.IGNORECASE | re.DOTALL)


def strip_quoted_reply(text: str | None) -> str:
    """Return only the newly written portion of an email body/preview.

    Drops quoted history, ``>`` quote lines, and trailing mobile signatures. Falls back to
    the original text when stripping would leave nothing useful, so we never lose content.
    """
    if not text:
        return ""

    cleaned = text.replace("\r\n", "\n").strip()
    match = _QUOTED_REPLY_RE.search(cleaned)
    if match and match.start() > 0:
        cleaned = cleaned[: match.start()].strip()

    kept = [line for line in cleaned.split("\n") if not line.lstrip().startswith(">")]
    cleaned = "\n".join(kept).strip()
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # If the message was *entirely* quoted material, keep the original rather than nothing.
    if len(cleaned) < 2:
        return re.sub(r"\s+", " ", text).strip()
    return cleaned


def format_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    local = re.sub(r"[._-]+", " ", local)
    return local.title()
