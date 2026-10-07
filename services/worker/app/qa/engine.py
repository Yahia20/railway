"""One chat in, one QA scorecard result out — the trial run, as a function.

load_thread()   the chat as the trial's extract.py built it, read-only
request_text()  exactly the user turn the trial sent: numbered lines + the
                line lists code chose (questions, unavailable, objections)
evaluate()      three model runs, per-question majority, score — the same
                score_v05.majority the approved page was built from

Nothing here writes. The result goes back to n8n, which stores it (rule 11),
and every one of the three calls comes back as a `model_calls` row (rule 12).
"""
from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import db
from ..evaluate.judge import DeepSeekClient, cost_row
from .common import render_transcript
from .score_v02 import hint_text
from .score_v05 import category_scores, majority, score_thread

PROMPT_VERSION = "qa-chat-v0.5"
PURPOSE = "qa_chat"
RUNS = 3
MAX_TOKENS = 6000
SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompt_v05.txt").read_text(encoding="utf-8")

# The trial's sample filter, kept as the definition of "a chat there is
# something to grade in": a human agent who is not a bot, both sides wrote at
# least twice, 4-60 lines. v_qa_due (migration 030) applies the same filter.
MIN_SIDE_MESSAGES = 2
MIN_MESSAGES, MAX_MESSAGES = 4, 60


class NotGradeable(LookupError):
    """The chat is missing, has no human agent, or is outside the filter."""


_SQL_THREAD = """
SELECT i.interaction_id::text, i.agent_id::text, i.customer_id::text, i.customer_phone_e164,
       i.started_at, i.ended_at, i.message_count, i.customer_message_count,
       i.agent_message_count, a.full_name, a.is_bot, a.is_active
FROM interactions i JOIN agents a USING (agent_id)
WHERE i.interaction_id = %s
"""

_SQL_MESSAGES = """
SELECT seq, sender::text, content_type, body, sent_at, sender_external_id
FROM chat_messages WHERE interaction_id = %s ORDER BY sent_at, seq
"""

_SQL_SYSTEM_SENDERS = "SELECT bitrix_user_id FROM agents WHERE is_bot"

_SQL_NAME = "SELECT display_name, name_source FROM customers WHERE customer_id = %s"

# Agent messages to the same customer in OTHER threads, up to 4 days after
# this one ended: item 27 (came back as promised within 3 days) reads them.
_SQL_LATER = """
SELECT m.sent_at, m.sender_external_id FROM chat_messages m
JOIN interactions o ON o.interaction_id = m.interaction_id
WHERE o.interaction_id <> %s AND m.sender = 'agent'
  AND ((%s::uuid IS NOT NULL AND o.customer_id = %s::uuid)
       OR (%s::uuid IS NULL AND o.customer_phone_e164 = %s))
  AND m.sent_at > %s AND m.sent_at < %s + interval '4 days'
ORDER BY m.sent_at
"""


def load_thread(interaction_id: str, *, gate: bool = True) -> dict[str, Any]:
    """The chat in the shape the scoring modules read (the trial's sample.json row).

    `gate=False` is for showing a chat that was already graded: it may have
    grown past 60 lines since, or its agent left, and the page must still open.
    """
    head = db.one(_SQL_THREAD, (interaction_id,))
    if not head:
        raise NotGradeable("no such conversation, or it has no agent")
    if gate and (head["is_bot"] or not head["is_active"]):
        raise NotGradeable("the agent is a bot or inactive")
    if gate and ((head["customer_message_count"] or 0) < MIN_SIDE_MESSAGES
                 or (head["agent_message_count"] or 0) < MIN_SIDE_MESSAGES
                 or not MIN_MESSAGES <= (head["message_count"] or 0) <= MAX_MESSAGES):
        raise NotGradeable("outside the sample filter (both sides x2, 4-60 lines)")

    system_ids = {r["bitrix_user_id"] for r in db.rows(_SQL_SYSTEM_SENDERS)}

    def is_system(ext: str | None) -> bool:
        return ext == "1" or ext in system_ids

    msgs = []
    for r in db.rows(_SQL_MESSAGES, (interaction_id,)):
        role = r["sender"]
        if role == "agent" and is_system(r["sender_external_id"]):
            role = "system"
        msgs.append({"role": role, "type": r["content_type"], "body": r["body"] or "",
                     "at": r["sent_at"].isoformat(),
                     "agent_ext": r["sender_external_id"] if r["sender"] == "agent" else None})

    name = None
    if head["customer_id"]:
        n = db.one(_SQL_NAME, (head["customer_id"],))
        if n.get("display_name"):
            name = {"name": n["display_name"], "source": n["name_source"]}

    cid, phone = head["customer_id"], head["customer_phone_e164"]
    later = [{"at": r["sent_at"].isoformat(), "agent_ext": r["sender_external_id"]}
             for r in db.rows(_SQL_LATER, (interaction_id, cid, cid, cid, phone,
                                           head["started_at"], head["ended_at"]))
             if not is_system(r["sender_external_id"])]

    return {"id": head["interaction_id"], "agent": head["full_name"], "agent_id": head["agent_id"],
            "started_at": head["started_at"].isoformat(), "ended_at": head["ended_at"].isoformat(),
            "customer_name": name, "messages": msgs, "later_agent_msgs": later}


def request_text(thread: dict[str, Any]) -> str:
    return "المحادثة:\n\n" + render_transcript(thread) + hint_text(thread)


def evaluate(thread: dict[str, Any], client: DeepSeekClient | None = None) -> dict[str, Any]:
    """Ask RUNS times, score each answer, keep the per-question majority."""
    client = client or DeepSeekClient()
    text = request_text(thread)
    base_hash = hashlib.sha256((SYSTEM_PROMPT + "\n" + text).encode("utf-8")).hexdigest()

    def one_run(k: int) -> tuple[dict, dict, int]:
        t0 = time.monotonic()
        answer, usage = client.complete_json(text, temperature=0.0, max_tokens=MAX_TOKENS,
                                             system=SYSTEM_PROMPT)
        return answer, usage, int((time.monotonic() - t0) * 1000)

    with ThreadPoolExecutor(RUNS) as pool:
        runs = list(pool.map(one_run, range(RUNS)))

    result = majority([score_thread(thread, answer) for answer, _, _ in runs])
    items = {int(q): r for q, r in result["items"].items()}
    calls = [cost_row(PURPOSE, model=usage.get("model") or client.model,
                      prompt_version=PROMPT_VERSION,
                      # The same text is sent three times on purpose; the run
                      # number keeps model_calls' UNIQUE from collapsing them.
                      input_hash=f"{base_hash}:{k}", usage=usage, latency_ms=ms)
             for k, (_, usage, ms) in enumerate(runs)]
    return {
        "interaction_id": thread["id"],
        "agent_id": thread["agent_id"],
        "prompt_version": PROMPT_VERSION,
        "model": calls[0]["model"],
        "runs": RUNS,
        "score": result["score"],
        "critical": sorted(result["critical"]),
        "items": {str(q): r for q, r in sorted(items.items())},
        "categories": {str(n): v for n, v in category_scores(items).items()},
        "cost_usd": round(sum(c["cost_usd"] or 0 for c in calls), 6),
        "calls": calls,
    }
