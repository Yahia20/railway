"""One chat in, one scorecard grade out — handoff v2's calls 1 and 4, as a function.

    call 1   DeepSeek, `prompt.txt`, three runs in parallel  -> everything but q41
    call 4   Claude Sonnet 5.5, the same prompt               -> q41 only, when the
             chat did not end in a booking and qa_config says so

Calls 2 (q40) and 3 (coaching) are not here: their prompts did not arrive with
the handoff, and neither feeds the score.

Nothing here writes (rule 11). The result goes back to workflow 10, which
stores it in `qa_grades`, and every model call comes back as a `model_calls`
row (rule 12) — including a Sonnet call that was refused, because it was paid.
"""
from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ... import db
from ...evaluate.judge import DeepSeekClient, JudgeError, cost_row
from ...evaluate.metrics import compute_chat_metrics
from ...sources.base import Conversation, Message
from . import scorecard, transcript
from .claude import ClaudeClient, ClaudeStopped

PROMPT_VERSION = "qa-chat-v4.1"
PURPOSE = "qa_chat_v4"
PURPOSE_LOSS = "qa_loss_reason"
RUNS = 3
MAX_TOKENS = 6000

_PROMPT_FILE = Path(__file__).resolve().parent / "prompt.txt"
_NOTE_MARKER = "# --- NOTE FOR THE SCORING CODE"


def system_prompt(text: str | None = None) -> str:
    """The prompt file without its note for the scoring code (handoff: strip it)."""
    text = _PROMPT_FILE.read_text(encoding="utf-8") if text is None else text
    return text.split(_NOTE_MARKER, 1)[0].rstrip() + "\n"


SYSTEM_PROMPT = system_prompt()


@dataclass(frozen=True)
class Settings:
    """Where the loss reason comes from, read per chat so a switch needs no deploy."""

    loss_model: str = "deepseek"        # qa_config.loss_reason_model: 'sonnet' | 'deepseek'
    sonnet_votes: int = 1               # qa_config.sonnet_votes
    sonnet_gate_open: bool = False      # v_pipeline_gate for provider 'anthropic'
    sonnet_blocked: str | None = None   # why Sonnet is not used, when it is not

    @property
    def use_sonnet(self) -> bool:
        return self.loss_model == "sonnet" and self.sonnet_gate_open and self.sonnet_votes > 0


_SQL_SETTINGS = "SELECT key, value FROM qa_config WHERE key IN ('loss_reason_model', 'sonnet_votes')"
_SQL_ANTHROPIC_GATE = "SELECT may_run, reason FROM v_pipeline_gate WHERE provider = 'anthropic'"


def load_settings() -> Settings:
    cfg = {r["key"]: r["value"] for r in db.rows(_SQL_SETTINGS)}
    loss_model = cfg.get("loss_reason_model", "deepseek")
    try:
        votes = max(0, min(3, int(cfg.get("sonnet_votes", "1"))))
    except ValueError:
        votes = 1
    gate = db.one(_SQL_ANTHROPIC_GATE) or {}
    blocked = None
    if loss_model != "sonnet":
        blocked = "qa_config.loss_reason_model is not 'sonnet'"
    elif not gate.get("may_run"):
        blocked = gate.get("reason") or "no provider_budgets row for anthropic"
    return Settings(loss_model, votes, bool(gate.get("may_run")), blocked)


def _metrics(thread: dict[str, Any]) -> dict[str, int | None]:
    """Reply times, by the one implementation the project has (rule 3).
    Informational: response speed is not one of the nine items."""
    sender = {"customer": "customer", "agent": "agent", "bot": "bot", "system": "bot"}
    msgs = [Message(i, sender[m["role"]], m.get("body") or "", datetime.fromisoformat(m["at"]))
            for i, m in enumerate(thread["messages"], 1)]
    conv = Conversation(thread["id"], "bitrix_chat_api", "chat", msgs[0].sent_at, msgs) if msgs else None
    if conv is None:
        return {"first_response_sec": None, "median_response_sec": None}
    m = compute_chat_metrics(conv)
    return {"first_response_sec": m.first_response_seconds, "median_response_sec": m.median_response_seconds}


def _line_payload(chat: transcript.Chat, n: int | None) -> dict[str, Any] | None:
    line = chat.line(n)
    return None if line is None else {"line": line.n, "by": line.role, "text": line.text}


def _deepseek_runs(chat: transcript.Chat, text: str, base_hash: str,
                   client: DeepSeekClient) -> tuple[list[dict], list[scorecard.RunAssessment], list[dict]]:
    """Call 1, RUNS times in parallel: (answers, assessments, model_calls rows)."""
    def one(_: int) -> tuple[dict, dict, int]:
        t0 = time.monotonic()
        answer, usage = client.complete_json(text, temperature=0.0, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT)
        return answer, usage, int((time.monotonic() - t0) * 1000)

    with ThreadPoolExecutor(RUNS) as pool:
        results = list(pool.map(one, range(RUNS)))
    answers = [a for a, _, _ in results]
    # The same text is sent RUNS times on purpose; the run number keeps
    # model_calls' UNIQUE from collapsing the bills into one.
    calls = [cost_row(PURPOSE, model=usage.get("model") or client.model, prompt_version=PROMPT_VERSION,
                      input_hash=f"{base_hash}:{k}", usage=usage, latency_ms=ms)
             for k, (_, usage, ms) in enumerate(results)]
    return answers, [scorecard.assess_run(chat, a) for a in answers], calls


@dataclass
class LossDecision:
    """Who voted on the loss reason, and why. `votes` None = the DeepSeek runs."""

    votes: list[tuple[str | None, int | None]] | None = None
    source: str = "deepseek"
    note: str | None = None
    answers: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)


def _sonnet_votes(chat: transcript.Chat, text: str, base_hash: str, votes: int,
                  client: ClaudeClient) -> LossDecision:
    """Call 4. A failed vote is dropped, and its model_calls row still records what it cost."""
    def one(k: int):
        try:
            answer, usage = client.complete_json(SYSTEM_PROMPT, text)
            return k, answer, usage, None
        except ClaudeStopped as exc:
            return k, None, exc.usage, str(exc)
        except JudgeError as exc:
            return k, None, {}, str(exc)

    with ThreadPoolExecutor(votes) as pool:
        results = list(pool.map(one, range(votes)))
    decision = LossDecision(votes=[], source="sonnet")
    for k, answer, usage, error in results:
        decision.calls.append(cost_row(PURPOSE_LOSS, model=usage.get("model") or client.model,
                                       prompt_version=PROMPT_VERSION, input_hash=f"{base_hash}:sonnet:{k}",
                                       usage=usage, latency_ms=usage.get("latency_ms"),
                                       succeeded=error is None, error=error, provider="anthropic"))
        if answer is not None:
            q41 = answer.get("q41") if isinstance(answer.get("q41"), dict) else {}
            decision.votes.append(scorecard.loss_label(chat, q41))
            decision.answers.append({"q41": q41})
    return decision


def _decide_loss(chat: transcript.Chat, text: str, base_hash: str, runs: list[scorecard.RunAssessment],
                 settings: Settings, claude_factory: Callable[[], ClaudeClient]) -> LossDecision:
    """Sonnet votes when the switch, the spend gate and the chat allow it; otherwise,
    or when every Sonnet vote failed, the DeepSeek runs vote (and the note says why)."""
    if scorecard.pick_loss_label([(r.loss_label, r.loss_line) for r in runs])[0] == "Booked":
        return LossDecision(note="booked in the chat: no loss reason to ask for")
    if not settings.use_sonnet:
        return LossDecision(note=settings.sonnet_blocked)
    try:
        sonnet = _sonnet_votes(chat, text, base_hash, settings.sonnet_votes, claude_factory())
    except JudgeError as exc:                     # the switch is on, but no key or no package
        return LossDecision(note=f"Sonnet unavailable: {exc}")
    if sonnet.votes:
        return sonnet
    return LossDecision(note="every Sonnet vote failed; the DeepSeek runs voted instead",
                        answers=sonnet.answers, calls=sonnet.calls)


def _payload(thread: dict[str, Any], chat: transcript.Chat, grade: scorecard.Grade,
             loss: LossDecision, answers: list[dict], calls: list[dict]) -> dict[str, Any]:
    evidence_lines = {ln for it in grade.items.values() for ln in it.lines} | set(grade.followup_lines)
    return {
        "interaction_id": thread["id"],
        "agent_id": thread["agent_id"],
        "prompt_version": PROMPT_VERSION,
        "model": calls[0]["model"],
        "runs": RUNS,
        "score": grade.score,
        "items": {k: {"value": v.value, "lines": list(v.lines)} for k, v in grade.items.items()},
        "chat_type": grade.chat_type,
        "qualified": grade.qualified,
        "red_flag": grade.red_flag,
        "loss_reason": grade.loss_reason,
        "loss_reason_source": loss.source,
        "followup_attempts": len(grade.followup_lines),
        # Line numbers plus the masked text they point at. Customer words, so
        # workflow 10 blanks `detail` and `answers` with the 90-day purge.
        "detail": {
            "booking": {k: _line_payload(chat, n) for k, n in grade.booking.items()},
            "offer": _line_payload(chat, grade.offer_line),
            "loss_evidence": _line_payload(chat, grade.loss_line),
            "loss_note": loss.note,
            "followup_lines": grade.followup_lines,
            "evidence": {n: _line_payload(chat, n) for n in sorted(evidence_lines) if chat.line(n)},
            "metrics": _metrics(thread),
        },
        "answers": {"deepseek": answers, "sonnet": loss.answers},
        "cost_usd": round(sum(c["cost_usd"] or 0 for c in calls), 6),
        "calls": calls,
    }


def evaluate(thread: dict[str, Any], settings: Settings,
             deepseek: DeepSeekClient | None = None,
             claude_factory: Callable[[], ClaudeClient] = ClaudeClient) -> dict[str, Any]:
    """Grade one chat. Raises JudgeError only when call 1 cannot be answered."""
    chat = transcript.build(thread)
    text = chat.render()
    base_hash = hashlib.sha256((SYSTEM_PROMPT + "\n" + text).encode("utf-8")).hexdigest()
    answers, runs, calls = _deepseek_runs(chat, text, base_hash, deepseek or DeepSeekClient())
    loss = _decide_loss(chat, text, base_hash, runs, settings, claude_factory)
    grade = scorecard.combine(chat, runs, loss.votes)
    return _payload(thread, chat, grade, loss, answers, calls + loss.calls)


__all__ = ["PROMPT_VERSION", "Settings", "SYSTEM_PROMPT", "evaluate", "load_settings", "system_prompt"]
