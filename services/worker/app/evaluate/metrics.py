"""Metrics computed from metadata — never asked of a model.

Ask an LLM to count seconds and it guesses, and the guess changes between runs
of the same prompt. These numbers are arithmetic over timestamps, so they are
exact, free, and stable. They are handed to the judge in the METADATA block as
authoritative, and the prompt forbids recalculating them.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from statistics import median

from ..sources.base import Conversation, Message

# Local business hours at the portal's timezone.
BUSINESS_START = time(9, 0)
BUSINESS_END = time(21, 0)

# The offset those hours are expressed in. Saudi Arabia, matching
# DEFAULT_PHONE_REGION and PBX_TZ_OFFSET_HOURS.
#
# This has to be applied explicitly. A timestamp carries an offset, and reading
# the wall clock off it without converting means 19:24+00 is judged as 19:24 —
# inside business hours — when it is really 22:24 in Riyadh and squarely outside
# them. The Bitrix webhook sends +03:00, so the two agreed by accident and the
# error was invisible; the conversation API sends +00 and it is not.
BUSINESS_TZ = timezone(timedelta(hours=float(os.getenv("PORTAL_TZ_OFFSET_HOURS", "3"))))


def _local_time(dt: datetime) -> time:
    """Wall-clock time at the portal, whatever offset the source used."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BUSINESS_TZ).time()


def is_after_hours(dt: datetime) -> bool:
    return not (BUSINESS_START <= _local_time(dt) <= BUSINESS_END)

_ARABIC = range(0x0600, 0x0700)


@dataclass
class ComputedMetrics:
    first_response_seconds: int | None = None
    median_response_seconds: int | None = None
    max_response_gap_seconds: int | None = None
    customer_message_count: int = 0
    agent_message_count: int = 0
    bot_message_count: int = 0
    conversation_span_seconds: int | None = None
    after_hours: bool | None = None
    language_matched: bool | None = None
    agent_talk_ratio: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _is_arabic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(ord(c) in _ARABIC for c in letters) / len(letters) > 0.5


def _response_gaps(messages: list[Message]) -> list[float]:
    """Seconds between a customer message and the agent's next reply."""
    gaps, pending = [], None
    for m in messages:
        if m.sender == "customer":
            if pending is None:
                pending = m.sent_at
        elif m.sender == "agent" and pending is not None:
            gaps.append((m.sent_at - pending).total_seconds())
            pending = None
    return [g for g in gaps if g >= 0]


def compute_chat_metrics(conv: Conversation) -> ComputedMetrics:
    msgs = sorted(conv.messages, key=lambda m: m.sent_at)
    if not msgs:
        return ComputedMetrics()

    gaps = _response_gaps(msgs)
    customer = [m for m in msgs if m.sender == "customer"]
    agent = [m for m in msgs if m.sender == "agent"]

    matched = None
    if customer and agent:
        matched = _is_arabic(" ".join(m.body for m in customer)) == \
                  _is_arabic(" ".join(m.body for m in agent))

    return ComputedMetrics(
        first_response_seconds=int(gaps[0]) if gaps else None,
        median_response_seconds=int(median(gaps)) if gaps else None,
        max_response_gap_seconds=int(max(gaps)) if gaps else None,
        customer_message_count=len(customer),
        agent_message_count=len(agent),
        bot_message_count=sum(1 for m in msgs if m.sender == "bot"),
        conversation_span_seconds=int((msgs[-1].sent_at - msgs[0].sent_at).total_seconds()),
        after_hours=is_after_hours(msgs[0].sent_at),
        language_matched=matched,
    )


def compute_call_metrics(started_at: datetime, duration_seconds: float,
                         segments: list[dict] | None = None) -> ComputedMetrics:
    """Call metrics.

    `agent_talk_ratio` stays None unless the segments carry real speaker labels.
    With a mono recording and no diarization there is nothing to measure, and a
    fabricated ratio would be worse than a blank: it would look like data.
    """
    talk_ratio = None
    if segments:
        labelled = [s for s in segments if s.get("speaker") in ("agent", "customer")]
        if labelled:
            agent_time = sum(s["end_sec"] - s["start_sec"]
                             for s in labelled if s["speaker"] == "agent")
            total = sum(s["end_sec"] - s["start_sec"] for s in labelled)
            if total > 0:
                talk_ratio = round(agent_time / total, 3)

    return ComputedMetrics(
        conversation_span_seconds=int(duration_seconds),
        after_hours=is_after_hours(started_at),
        agent_talk_ratio=talk_ratio,
    )


def later_contact_line(entry: dict) -> str:
    """One `Subsequent contact` bullet, in the format production actually sends.

    THE AUTHORITATIVE RENDERER IS SQL, not this file: the block that reaches the
    judge in production is built by the `Build follow-up history` node of
    n8n/workflows/02-calls-ingest-evaluate.json, dumped for review to
    scripts/sql/02_build_follow_up_history.sql. This function exists so that
    Python-side callers emit the SAME bullet, and
    `scripts/compare_day.py:render_current_history` mirrors it field for field.
    `test_followup_history_block.py` fails if the two ever disagree.

    Every field here is load-bearing and each one fixes a measured defect. On
    day 13 four calls had later same-phone interactions in the database and all
    of them still scored Module 4 = null, because the old bullet
    (`{channel} by {by}`) rendered "phone_call by unknown" for essentially the
    whole corpus:

      * DIRECTION. A customer calling back in is not the agent following up, and
        Module 4 grades only what the AGENT did. With the direction unstated,
        `null` is the honest answer, and the model gave it.
      * THE HANDLER. Queue recordings deliberately carry `agent_id = NULL` - the
        extension in a queue filename is the QUEUE, not a person - so "no
        individual agent recorded (queue recording)" has to be distinguishable
        from "we do not know".
      * THE MESSAGE TEXT. Criterion 3 is follow-up MESSAGE QUALITY, 30 of the
        module's 100 points, and is unanswerable from a bullet without it.
    """
    channel = str(entry.get("channel") or "phone_call")

    if channel == "phone_call" and str(entry.get("kind") or "") == "q":
        direction = ("INBOUND: the customer called in, this is not an agent "
                     "follow-up")
    elif entry.get("direction"):
        direction = f"direction {entry['direction']}"
    else:
        direction = "direction not recorded"

    if entry.get("agent_name"):
        handler = str(entry["agent_name"])
    elif entry.get("is_bot_handled"):
        handler = "the qualification bot, not a human agent"
    elif str(entry.get("kind") or "") == "q":
        handler = "no individual agent recorded (queue recording)"
    else:
        handler = "not recorded"

    body = entry.get("first_message")
    message = f': "{str(body)[:300]}"' if body else ""
    hours = entry.get("hours_after")
    hours_text = f"{float(hours):.1f}" if isinstance(hours, (int, float)) else "?"

    return (f"  - [{entry.get('started_at', '?')}] {channel}, {direction}, "
            f"{hours_text}h after this conversation, handled by {handler}{message}")


def followup_history_block(promises: list[dict], later_contacts: list[dict]) -> str:
    """Render the FOLLOW-UP HISTORY block for the judge prompt.

    Returns the literal string 'unavailable' when we genuinely do not know, which
    the prompt reads as "score Module 4 null". That is the honest answer before
    the chats integration lands: for a call alone, we cannot see whether the
    agent followed up on WhatsApp afterwards.

    The `Subsequent contact` lines are rendered by `later_contact_line`, so this
    block and the SQL production actually runs cannot drift apart. The promises
    section has no SQL counterpart: it is derived from the conversation itself
    rather than from the customer's timeline.
    """
    if not promises and not later_contacts:
        return "unavailable"

    lines = []
    if promises:
        lines.append("Promises made by the agent in this conversation:")
        for p in promises:
            due = f" (due: {p['due_hint']})" if p.get("due_hint") else ""
            lines.append(f"  - [{p.get('timestamp', '?')}] \"{p['promise']}\"{due}")
    lines.append("")
    if later_contacts:
        lines.append("Subsequent contact with this customer:")
        lines.extend(later_contact_line(c) for c in
                     sorted(later_contacts,
                            key=lambda e: str(e.get("started_at") or "")))
    else:
        lines.append("Subsequent contact with this customer: NONE recorded.")
    return "\n".join(lines)


def hours_between(a: datetime, b: datetime) -> float:
    return round(abs((b - a).total_seconds()) / 3600, 2)


def followup_status(promised_at: datetime, due_at: datetime | None,
                    fulfilled_at: datetime | None) -> str:
    if fulfilled_at is None:
        if due_at and datetime.now(due_at.tzinfo) > due_at:
            return "missed"
        return "open"
    deadline = due_at or (promised_at + timedelta(hours=24))
    return "fulfilled" if fulfilled_at <= deadline else "late"
