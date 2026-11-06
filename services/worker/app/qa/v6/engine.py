"""One chat in, one v6 grade out: the trial's run_v6.py as a function.

    reading   DeepSeek, prompt.txt, three votes in parallel, thinking off. A quote not found in the message it
              names gets ONE bounded re-copy request, the same for every chat.
    alerts    DeepSeek, prompt_alerts.txt, one call with thinking on (off above 300 messages, where hidden
              thinking used the whole budget in the trial and returned nothing). Loss reason + alerts.
    score     scorecard.score_chat, the engine of record. The model never gives a number.

Nothing here writes (rule 11). The answer goes back to workflow 13, which stores the grade in qa_grades_v6 and
every paid call in model_calls (rule 12), including the calls of a chat whose grade then failed.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ...evaluate.judge import DeepSeekClient, JudgeError, cost_row
from ..common import RIYADH
from . import scorecard, transcript

PROMPT_VERSION = "qa-chat-v6.0"
ALERTS_PROMPT_VERSION = "qa-alerts-v3.0"
PURPOSE = "qa_chat_v6"
PURPOSE_RECOPY = "qa_chat_v6_recopy"
PURPOSE_ALERTS = "qa_alerts_v6"
VOTES = 3
MIN_VOTES = 2               # two readings are enough to grade; fewer fails the chat and it is retried later
MAX_TOKENS = 12000
LONG_CHAT = 300             # messages; above this the alerts call runs without thinking
ROLE = {"agent": "الموظف", "customer": "العميل", "bot": "البوت", "system": "نظام"}
LETTERS = "أبجدهوزحطي"
_HERE = Path(__file__).resolve().parent
_NOTE_MARKER = "# --- NOTE FOR THE SCORING CODE"
SYSTEM_PROMPT = (_HERE / "prompt.txt").read_text(encoding="utf-8").split(_NOTE_MARKER)[0].rstrip() + "\n"
ALERTS_PROMPT = (_HERE / "prompt_alerts.txt").read_text(encoding="utf-8")
ENGINE_VERSION = hashlib.sha256((_HERE / "scorecard.py").read_bytes()).hexdigest()[:16]
RECOPY_ASK = ("These quotes are not inside the message you named. Copy each one again EXACTLY from the transcript, "
              "character by character, or use null if no message fits. Return the whole JSON object again.\n")
_LINE_FOR = {"quote": "line", "source_quote": "line", "promise_quote": "line", "quote_a": "line_a", "quote_b": "line_b"}


def chat_lines(chat: dict) -> list[str]:
    order = chat.get("senders") or []
    many = len(order) > 1
    lines = []
    for i, m in enumerate(chat["messages"], 1):
        who = ROLE[m["role"]]
        if many and m["role"] == "agent" and m.get("sender") in order:
            who += f" ({LETTERS[order.index(m['sender']) % len(LETTERS)]})"
        lines.append(f"[{i}] {m['date']} {m['time']} {who}: {(m['reply_to'] + ' ') if m['reply_to'] else ''}{m['text'].strip()}")
    return lines


def _first_agent(chat: dict) -> int | None:
    return next((i for i, m in enumerate(chat["messages"], 1) if m["role"] == "agent"), None)


def vote_text(chat: dict) -> str:
    msgs = chat["messages"]
    qs = [i for i, m in enumerate(msgs, 1) if m["role"] == "customer" and re.search("[؟?]", m["text"])]
    obj = [i for i, m in enumerate(msgs, 1) if m["role"] == "customer" and re.search("غالي|عالي|ارخص|أرخص|اشاور|أشاور|بفكر|أفكر", m["text"])]
    first = _first_agent(chat)
    return "\n".join(["المحادثة:", ""] + chat_lines(chat) + ["", f"FIRST AGENT MESSAGE: {'[%d]' % first if first else '(none)'}",
                                                          f"CUSTOMER QUESTIONS: {qs or '(none)'}", f"OBJECTION LINES: {obj or '(none)'}"])


def alerts_text(chat: dict) -> str:
    first = _first_agent(chat)
    return ("المحادثة:\n\n" + "\n".join(chat_lines(chat))
            + f"\n\nFIRST AGENT MESSAGE: {'[%d]' % first if first else '(none)'}\n\nReturn ONLY the JSON object.")


def bad_quotes(node: Any, chat: dict, found: list | None = None) -> list[tuple[str, str, Any]]:
    """Every quote in an answer that is not inside the message it names (due dates are free text, not quotes)."""
    found = [] if found is None else found
    if isinstance(node, list):
        for x in node:
            bad_quotes(x, chat, found)
    elif isinstance(node, dict):
        for k, val in node.items():
            if isinstance(val, (dict, list)):
                bad_quotes(val, chat, found)
            elif k != "due_quote" and isinstance(val, str) and val and (k.endswith("quote") or k in ("quote_a", "quote_b")):
                ln = node.get(_LINE_FOR.get(k) or k.replace("_quote", "_line"))
                msgs = chat["messages"]
                text = ""
                if isinstance(ln, int) and not isinstance(ln, bool) and 1 <= ln <= len(msgs):
                    text = (msgs[ln - 1].get("reply_to") or "") + " " + msgs[ln - 1]["text"]
                if " ".join(val.split()) not in " ".join(text.split()):
                    found.append((k, val, ln))
    return found


def _timed(call: Callable[[], tuple[dict, dict]]) -> tuple[dict, dict, int]:
    t0 = time.monotonic()
    answer, usage = call()
    return answer, usage, int((time.monotonic() - t0) * 1000)


def _vote(chat: dict, text: str, base_hash: str, k: int, client: DeepSeekClient) -> tuple[dict | None, list[dict], str | None]:
    """One reading, plus its single re-copy when it misplaced quotes. Returns (answer, model_calls rows, error)."""
    calls: list[dict] = []
    try:
        answer, usage, ms = _timed(lambda: client.complete_json(text, temperature=0.0, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT))
    except JudgeError as exc:
        return None, calls, str(exc)
    calls.append(cost_row(PURPOSE, model=usage.get("model") or client.model, prompt_version=PROMPT_VERSION,
                          input_hash=f"{base_hash}:{k}", usage=usage, latency_ms=ms))
    bad = bad_quotes(answer, chat)
    if bad:
        ask = RECOPY_ASK + "\n".join(f"- {key} (line {ln}): {q}" for key, q, ln in bad[:30])
        follow_up = [{"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)},
                     {"role": "user", "content": ask}]
        try:
            again, usage2, ms2 = _timed(lambda: client.complete_json(text, temperature=0.0, max_tokens=MAX_TOKENS,
                                                                      system=SYSTEM_PROMPT, follow_up=follow_up))
            calls.append(cost_row(PURPOSE_RECOPY, model=usage2.get("model") or client.model, prompt_version=PROMPT_VERSION,
                                  input_hash=f"{base_hash}:{k}:recopy", usage=usage2, latency_ms=ms2))
            if len(bad_quotes(again, chat)) < len(bad):
                answer = again
        except JudgeError:
            pass                # the first reading stands; its misplaced quotes become "review" in the scorecard
    return answer, calls, None


def _alerts(chat: dict, base_hash: str, client: DeepSeekClient, plain: DeepSeekClient) -> tuple[dict, list[dict]]:
    """The loss reason and alerts. Thinking on as approved; once more with thinking off if that fails."""
    text = alerts_text(chat)
    first = plain if len(chat["messages"]) > LONG_CHAT else client
    errors = []
    for c in ((first, plain) if first is client else (plain,)):
        try:
            answer, usage, ms = _timed(lambda: c.complete_json(text, temperature=0.0, max_tokens=MAX_TOKENS,
                                                               system=ALERTS_PROMPT, json_mode=False))
        except JudgeError as exc:
            errors.append(str(exc))
            continue
        row = cost_row(PURPOSE_ALERTS, model=usage.get("model") or c.model, prompt_version=ALERTS_PROMPT_VERSION,
                       input_hash=f"{base_hash}:alerts:{c.thinking}", usage=usage, latency_ms=ms)
        return answer, [row]
    raise JudgeError("alerts call failed: " + " | ".join(errors))


def riyadh_now() -> datetime:
    return datetime.now(RIYADH).replace(tzinfo=None, second=0, microsecond=0)


def evaluate(interaction_id: str, *, client: DeepSeekClient | None = None, plain_client: DeepSeekClient | None = None,
             thinking_client: DeepSeekClient | None = None, bitrix: Any | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """Grade one chat. NotGradeable for a chat that must not be graded. JudgeError when fewer than two readings
    or no alerts answer came back; the calls that did happen are attached to the error for model_calls."""
    chat = transcript.build(interaction_id, bitrix)
    plain = plain_client or client or DeepSeekClient(thinking="disabled")
    thinking = thinking_client or DeepSeekClient(thinking="enabled")
    text = vote_text(chat)
    base_hash = hashlib.sha256((SYSTEM_PROMPT + "\n" + text).encode("utf-8")).hexdigest()

    with ThreadPoolExecutor(VOTES + 1) as pool:
        futures = [pool.submit(_vote, chat, text, base_hash, k, plain) for k in range(VOTES)]
        alert_future = pool.submit(_alerts, chat, base_hash, thinking, plain)
        results = [f.result() for f in futures]
        try:
            alerts, alert_calls = alert_future.result()
            alert_error = None
        except JudgeError as exc:
            alerts, alert_calls, alert_error = None, [], str(exc)

    calls = [row for _, rows, _ in results for row in rows] + alert_calls
    votes = [answer for answer, _, _ in results if answer is not None]
    if len(votes) < MIN_VOTES or alerts is None:
        reasons = [e for _, _, e in results if e] + ([alert_error] if alert_error else [])
        err = JudgeError(f"{len(votes)} of {VOTES} readings, alerts {'ok' if alerts is not None else 'failed'}: " + " | ".join(reasons)[:400])
        err.calls = calls       # type: ignore[attr-defined]  # paid for, so workflow 13 still records them
        raise err

    obs = now or riyadh_now()
    result = scorecard.score_chat(chat, votes, alerts, obs, obs.year)
    checks = result["checks"]
    return {
        "interaction_id": chat["id"],
        "agent_id": chat["agent_id"],
        "prompt_version": PROMPT_VERSION,
        "alerts_prompt_version": ALERTS_PROMPT_VERSION,
        "engine_version": ENGINE_VERSION,
        "model": calls[0]["model"] if calls else None,
        "votes": len(votes),
        "score": result["score"],
        "earned": result["earned"],
        "total": result["total"],
        "chat_type": result["type"],
        "title": result["title"],
        "outcome": chat["outcome"],
        "deal_stage": chat["deal_stage"],
        "attribution": chat["attribution"],
        "employee": chat["employee"],
        "loss_reason": result["loss_reason"],
        "reviews_open": sum(1 for c in checks.values() if c["status"] == "review"),
        "observed_at": obs.isoformat(),
        # Everything the weekly page needs to redraw this chat without the database: the scorecard's own result
        # and the masked transcript. Customer words, so workflow 13 blanks both with the 90-day purge.
        "result": result,
        "transcript": {"messages": chat["messages"], "agents": chat["agents"], "date": chat["date"],
                       "assigned_at": chat["assigned_at"], "calls": chat["calls"], "bitrix_read": chat["bitrix_read"],
                       "input_hash": chat["input_hash"]},
        "answers": {"votes": votes, "alerts": alerts},
        "cost_usd": round(sum(c["cost_usd"] or 0 for c in calls), 6),
        "calls": calls,
    }


__all__ = ["ALERTS_PROMPT_VERSION", "ENGINE_VERSION", "PROMPT_VERSION", "evaluate"]
