"""Acceptance tests for the "study the person" pass.

The critical property under test is that the brief cannot introduce a fact. Everything the
model proposes is checked back against the correspondence, and anything it cannot point at is
dropped — because the output of this module ends up in a sentence that begins "congratulations
on", addressed to a real person.

Run: python scripts/test_personal_brief.py   (no API key needed; the model is not called)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.personal_brief import (  # noqa: E402
    _json_object,
    _their_words,
    _verify_activity,
    empty_brief,
    format_personal_brief,
    format_their_own_words,
)

PASSED = 0
FAILED = 0


def check(label: str, actual, expected) -> None:
    global PASSED, FAILED
    if actual == expected:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}\n         expected: {expected!r}\n         got:      {actual!r}")


def ok(label: str, condition: bool) -> None:
    check(label, bool(condition), True)


NOW = datetime(2026, 8, 7, tzinfo=timezone.utc)


def material(*, days_ago: int = 30, direction: str = "them", text: str | None = None) -> list[dict]:
    return [
        {
            "ref": "m1",
            "message_id": "msg-1",
            "direction": direction,
            "subject": "Aurora update",
            "sent_at": NOW - timedelta(days=days_ago),
            "text": text
            or "Hi Dalbir, good news - we finally closed the Aurora acquisition last Thursday "
            "after nine months. Next up is the Series B.",
        },
        {
            "ref": "m2",
            "message_id": "msg-2",
            "direction": "us",
            "subject": "Re: Aurora update",
            "sent_at": NOW - timedelta(days=29),
            "text": "That is excellent news, congratulations to the whole team.",
        },
    ]


print("=== quote verification: the model cannot introduce a fact ===")

supported = _verify_activity(
    [
        {
            "headline": "closed the Aurora acquisition",
            "detail": "Nine-month process, signed last Thursday.",
            "kind": "deal",
            "ref": "m1",
            "quote": "we finally closed the Aurora acquisition last Thursday",
            "who_said_it": "them",
        }
    ],
    material(),
    now=NOW,
)
check("a quoted fact survives", len(supported), 1)
check("headline is kept", supported[0]["headline"], "closed the Aurora acquisition")
check("attribution comes from the matched message", supported[0]["said_by"], "them")
check("source date is recorded", supported[0]["source_date"], "2026-07-08")

invented = _verify_activity(
    [
        {
            "headline": "raised a $200m fund",
            "detail": "Closed a new growth fund.",
            "kind": "funding",
            "ref": "m1",
            "quote": "we just closed our $200m growth fund",
            "who_said_it": "them",
        }
    ],
    material(),
    now=NOW,
)
check("an invented fact is dropped", invented, [])

embellished = _verify_activity(
    [
        {
            "headline": "closed the Aurora acquisition for $400m",
            "detail": "A $400m deal.",
            "kind": "deal",
            # Real event, but the quote has a number that is not in the mail.
            "quote": "we finally closed the Aurora acquisition for $400m last Thursday",
            "ref": "m1",
        }
    ],
    material(),
    now=NOW,
)
check("an embellished quote is dropped", embellished, [])

mixed = _verify_activity(
    [
        {"headline": "closed Aurora", "quote": "closed the Aurora acquisition last Thursday", "ref": "m1"},
        {"headline": "joined the board of Nexa", "quote": "I joined the Nexa board", "ref": "m1"},
    ],
    material(),
    now=NOW,
)
check("a real item survives alongside a fabricated one", [i["headline"] for i in mixed], ["closed Aurora"])

print()
print("=== robustness of the match ===")

reflowed = _verify_activity(
    [{"headline": "closed Aurora", "quote": "we  finally CLOSED the Aurora\nacquisition,  last Thursday!", "ref": "m1"}],
    material(),
    now=NOW,
)
check("whitespace, case and punctuation drift still match", len(reflowed), 1)

wrong_ref = _verify_activity(
    [{"headline": "closed Aurora", "quote": "closed the Aurora acquisition last Thursday", "ref": "m9"}],
    material(),
    now=NOW,
)
check("a quote citing the wrong message is re-attributed, not lost", len(wrong_ref), 1)
check("re-attribution finds the true source", wrong_ref[0]["source_message_id"], "msg-1")

too_short = _verify_activity([{"headline": "closed Aurora", "quote": "closed", "ref": "m1"}], material(), now=NOW)
check("a quote too short to be evidence is rejected", too_short, [])

no_headline = _verify_activity(
    [{"headline": "", "quote": "we finally closed the Aurora acquisition last Thursday", "ref": "m1"}],
    material(),
    now=NOW,
)
check("an item with no headline is rejected", no_headline, [])

duplicates = _verify_activity(
    [
        {"headline": "Closed Aurora", "quote": "closed the Aurora acquisition last Thursday", "ref": "m1"},
        {"headline": "closed aurora", "quote": "we finally closed the Aurora acquisition", "ref": "m1"},
    ],
    material(),
    now=NOW,
)
check("the same fact twice is reported once", len(duplicates), 1)

check("a non-list from the model is survivable", _verify_activity("nonsense", material(), now=NOW), [])
check("junk entries are skipped", _verify_activity([None, 42, "x"], material(), now=NOW), [])

print()
print("=== recency: old news must not be congratulated as new ===")

fresh = _verify_activity(
    [{"headline": "closed Aurora", "quote": "closed the Aurora acquisition last Thursday", "ref": "m1"}],
    material(days_ago=30),
    now=NOW,
)
check("a month-old event is recent", fresh[0]["is_recent"], True)

stale = _verify_activity(
    [{"headline": "closed Aurora", "quote": "closed the Aurora acquisition last Thursday", "ref": "m1"}],
    material(days_ago=900),
    now=NOW,
)
check("a two-year-old event is not recent", stale[0]["is_recent"], False)
ok("stale items are flagged in the prompt block", "OLDER THAN A YEAR" in format_personal_brief({"activity": stale}))

print()
print("=== attribution: never congratulate them on something we did ===")

ours = _verify_activity(
    [
        {
            "headline": "congratulated them",
            "quote": "That is excellent news, congratulations to the whole team",
            "ref": "m1",  # model blames the wrong message; the text is really ours
            "who_said_it": "them",
        }
    ],
    material(),
    now=NOW,
)
check("a quote from our own mail is labelled as ours", ours[0]["said_by"], "us")
ok("the prompt block says who said it", "we said this to them" in format_personal_brief({"activity": ours}))

print()
print("=== the empty case is explicit, not silent ===")

blank = format_personal_brief(empty_brief("not enough correspondence to study"))
ok("says there is no news to congratulate them on", "No news or achievement" in blank)
ok("forbids inventing one", "Do NOT invent one" in blank)
ok("names the generic opener it must not use", "hope things are going well" in blank)
ok("says nothing at all was verifiable", "Nothing at all was verifiable" in blank)
ok("points at the fallback opener", "last real message" in blank)
ok("a None brief still produces a safe block", "Nothing verified" in format_personal_brief(None))
ok("a junk brief still produces a safe block", "Nothing verified" in format_personal_brief("garbage"))

# The whole point of the widening: a contact with no *news* but with something else on record
# must still give the draft a real opener rather than falling through to the empty case.
no_news_but_known = format_personal_brief(
    {
        "activity": [],
        "about_them": [
            {
                "headline": "offered to introduce us to his US partners",
                "detail": "Said he could connect us with his partners in the USA.",
                "quote": "You mentioned you could introduce me to your partners in USA.",
                "said_by": "them",
                "source_date": "2026-05-04",
                "is_recent": True,
            }
        ],
    }
)
ok("a contact with no news still gets an opener", "offered to introduce us" in no_news_but_known)
ok("the second section is labelled for that use", "WHAT ELSE WE KNOW ABOUT THEM" in no_news_but_known)
ok("and it is not reported as nothing known",
   "Nothing at all was verifiable" not in no_news_but_known)

print()
print("=== rendering ===")

brief = {
    "activity": supported,
    "focus": ["Series B for Aurora", "US expansion"],
    "note": "Prefers a short call over email.",
}
block = format_personal_brief(brief)
ok("headline appears", "closed the Aurora acquisition" in block)
ok("exact words appear so the model can echo them", "Exact words:" in block)
ok("focus appears", "US expansion" in block)
ok("note appears", "Prefers a short call" in block)

print()
print("=== their own words are carried in the brief, not refetched ===")

words = _their_words(material())
check("only their side is kept", [w["subject"] for w in words], ["Aurora update"])
check("dates are kept", words[0]["date"], "2026-07-08")
rendered = format_their_own_words({"their_words": words})
ok("their full text is rendered", "nine months" in rendered)
check("no inbound mail renders nothing", format_their_own_words({"their_words": []}), "")
check("a missing brief renders nothing", format_their_own_words(None), "")

print()
print("=== model output parsing ===")

check("plain JSON", _json_object('{"activity": []}'), {"activity": []})
check("fenced JSON", _json_object('```json\n{"activity": []}\n```'), {"activity": []})
check("JSON with prose around it", _json_object('Sure!\n{"activity": []}\nHope that helps'), {"activity": []})
try:
    _json_object("no json here")
    ok("non-JSON raises", False)
except ValueError:
    ok("non-JSON raises", True)

print()
print(f"{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
