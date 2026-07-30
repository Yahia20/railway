"""Metrics computed from metadata — never asked of a model.

Ask an LLM to count seconds and it guesses, and the guess changes between runs
of the same prompt. These numbers are arithmetic over timestamps, so they are
exact, free, and stable. They are handed to the judge in the METADATA block as
authoritative, and the prompt forbids recalculating them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from statistics import median

from ..sources.base import Conversation, Message

# Local business hours at the portal's timezone.
BUSINESS_START = time(9, 0)
BUSINESS_END = time(21, 0)

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
        after_hours=not (BUSINESS_START <= msgs[0].sent_at.timetz().replace(tzinfo=None) <= BUSINESS_END),
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

    local = started_at.timetz().replace(tzinfo=None)
    return ComputedMetrics(
        conversation_span_seconds=int(duration_seconds),
        after_hours=not (BUSINESS_START <= local <= BUSINESS_END),
        agent_talk_ratio=talk_ratio,
    )


def followup_history_block(promises: list[dict], later_contacts: list[dict]) -> str:
    """Render the FOLLOW-UP HISTORY block for the judge prompt.

    Returns the literal string 'unavailable' when we genuinely do not know, which
    the prompt reads as "score Module 4 null". That is the honest answer before
    the chats integration lands: for a call alone, we cannot see whether the
    agent followed up on WhatsApp afterwards.
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
        for c in later_contacts:
            lines.append(
                f"  - [{c['started_at']}] {c['channel']} by {c['by']}, "
                f"{c['hours_after']:.1f}h after this conversation: {c.get('first_message', '')}"
            )
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
