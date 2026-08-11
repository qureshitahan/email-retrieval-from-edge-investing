"""Turn a one-line objective into the criteria that actually decide who belongs on the list.

"Raise capital — investors who could fund or open doors" is a sentence, not a filter. Two
people can both match it while only one is worth writing to, because the things that really
decide it — what stage, what cheque size, whether an introduction counts as much as a cheque,
who to leave out — live in the user's head and never reach the ranker.

This asks those questions and answers them itself, so the default run is one click. Every
answer is a proposal the user can edit, and the edited answers are what the ranking is scored
against.
"""

from __future__ import annotations

import asyncio
import json
import re

MAX_QUESTIONS = 4
ANSWER_CHARS = 300

PLAN_SYSTEM_PROMPT = (
    "You help a business owner turn a vague outreach goal into the specific criteria that "
    "decide who should be contacted.\n\n"
    "Ask the few questions whose answers would most change the shortlist, and answer each one "
    "yourself with the most likely answer for this goal, so the user only has to correct you "
    "rather than fill in a form.\n\n"
    "Rules:\n"
    "- Questions must be about WHO to contact, never about how to write the email.\n"
    "- Each question must be one a reasonable person could answer differently, and where the "
    "difference would change who appears. 'What is your goal?' is useless; 'Are you looking for "
    "cheque-writers, or people who can introduce you to them?' is not.\n"
    "- Your proposed answer must be concrete and usable as a filter, not a restatement of the "
    "question.\n"
    "- Return JSON only. No prose, no markdown fence."
)

PLAN_USER_TEMPLATE = """The objective is: {objective}

The mailboxes being searched belong to: {who}

Return exactly this JSON shape:

{{
  "questions": [
    {{
      "question": "the question, in plain language, addressed to the user",
      "answer": "your proposed answer - concrete, and usable to decide who qualifies",
      "why": "half a sentence on what this changes about the shortlist"
    }}
  ],
  "looking_for": "one sentence describing the ideal person for this objective",
  "avoid": "one sentence describing who should NOT appear, or \\"\\""
}}

At most {max_questions} questions, most decisive first."""


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


def empty_plan(objective: str) -> dict:
    return {"objective": objective, "questions": [], "looking_for": "", "avoid": ""}


async def build_objective_plan(objective: str, who: str = "") -> dict:
    """Questions and proposed answers for an objective. Never raises — a plan is optional."""
    from app.services.ai_service import _call_anthropic

    objective = (objective or "").strip()
    if not objective:
        return empty_plan(objective)

    prompt = PLAN_USER_TEMPLATE.format(
        objective=objective,
        who=who or "the person running this outreach",
        max_questions=MAX_QUESTIONS,
    )
    try:
        raw = await asyncio.to_thread(_call_anthropic, PLAN_SYSTEM_PROMPT, prompt, max_tokens=1600)
        payload = _json_object(raw)
    except Exception:  # noqa: BLE001 - searching without a plan is the old behaviour
        return empty_plan(objective)

    questions: list[dict] = []
    for entry in payload.get("questions") or []:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        if not question or not answer:
            continue
        questions.append(
            {
                "question": question[:240],
                "answer": answer[:ANSWER_CHARS],
                "why": str(entry.get("why") or "").strip()[:200],
            }
        )
        if len(questions) >= MAX_QUESTIONS:
            break

    return {
        "objective": objective,
        "questions": questions,
        "looking_for": str(payload.get("looking_for") or "").strip()[:400],
        "avoid": str(payload.get("avoid") or "").strip()[:400],
    }


def format_plan(plan: dict | None) -> str:
    """The plan as a scoring block. Empty string when there is no plan, so ranking is unchanged."""
    if not plan or not isinstance(plan, dict):
        return ""
    lines: list[str] = []
    if plan.get("looking_for"):
        lines.append(f"WHO QUALIFIES: {plan['looking_for']}")
    if plan.get("avoid"):
        lines.append(f"WHO DOES NOT: {plan['avoid']}")

    answered = [q for q in (plan.get("questions") or []) if q.get("answer")]
    if answered:
        lines.append("")
        lines.append("THE USER HAS SPECIFIED:")
        for item in answered:
            lines.append(f"  - {item['question']}")
            lines.append(f"    {item['answer']}")

    if not lines:
        return ""
    lines.append("")
    lines.append(
        "Score against these as well as the objective. A contact who fits the objective in "
        "general but fails what the user specified scores low, and your reason must say which "
        "criterion decided it."
    )
    return "\n".join(lines)
