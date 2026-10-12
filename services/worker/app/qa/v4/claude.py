"""Call 4: the loss reason (q41) from Claude Sonnet 5.5.

Why a second vendor for one field (handoff section 2): on the owner's 50 test
chats Sonnet and DeepSeek disagreed on 11 loss reasons and Sonnet was right in
9 — DeepSeek labels a chat with a clearly stated reason ("والله الصراحه عاليه")
as "No Response". Only `q41` is read from this answer; everything else comes
from the three DeepSeek runs.

The request is the owner's tested one: the same system prompt as call 1 (cached),
the same chat, effort `medium`, no temperature (Sonnet 5.5 rejects non-default
sampling). `fallbacks: "default"` is Anthropic's server-side retry on a safety
decline; it only fires for categories a travel chat should never touch, and a
final refusal is a failed vote, never a label.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from ...evaluate.judge import JudgeError

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 16000
EFFORT = "medium"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TIMEOUT_SECONDS = 180.0

# USD per million tokens, Anthropic first-party API (Sonnet 5.5, read 2026-10-11).
# A 5-minute cache write costs 1.25x input; a cache read 0.1x.
PRICING = {MODEL: {"input": 2.00, "cache_write": 2.50, "cache_read": 0.20, "output": 10.00}}


def first_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in the text. Sonnet sometimes appends a second one
    (2 of 50 runs in the owner's test); everything after the first is ignored."""
    start = text.find("{")
    if start < 0:
        raise JudgeError(f"no JSON object in the answer: {text[:200]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise JudgeError(f"answer is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise JudgeError("answer is not a JSON object")
    return obj


def cost_usd(usage: dict[str, Any], model: str) -> float | None:
    """Dollars for one call from the reported usage; None for an unpriced model."""
    price = PRICING.get(model)
    if not price:
        return None
    dollars = (usage.get("input_tokens", 0) * price["input"]
               + usage.get("cache_creation_input_tokens", 0) * price["cache_write"]
               + usage.get("cache_read_input_tokens", 0) * price["cache_read"]
               + usage.get("output_tokens", 0) * price["output"]) / 1e6
    return round(dollars, 6)


class ClaudeStopped(JudgeError):
    """The call returned, but not with a usable answer (refusal, truncation).
    The tokens were spent, so the usage travels with the error to model_calls."""

    def __init__(self, message: str, usage: dict[str, Any]):
        super().__init__(message)
        self.usage = usage


class ClaudeClient:
    """One JSON answer per call. Import is lazy so a worker without the key
    (or without the package) still serves everything else."""

    def __init__(self, api_key: str | None = None, model: str = MODEL):
        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise JudgeError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self.model = model
        self._client = anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=2)
        self._errors = (anthropic.APIError,)

    def complete_json(self, system: str, user: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(answer, usage). `usage` carries the token counts in the shape
        `judge.cost_row` reads, plus `cost_usd` priced here."""
        t0 = time.monotonic()
        try:
            resp = self._client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                output_config={"effort": EFFORT},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user + "\n\nReturn ONLY the JSON object."}],
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except self._errors as exc:
            raise JudgeError(f"Claude call failed: {exc}") from exc

        u = resp.usage
        raw = {k: int(getattr(u, k, 0) or 0) for k in
               ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
        usage = {
            "model": resp.model,
            "prompt_tokens": raw["input_tokens"] + raw["cache_creation_input_tokens"]
                             + raw["cache_read_input_tokens"],
            "completion_tokens": raw["output_tokens"],
            "prompt_cache_hit_tokens": raw["cache_read_input_tokens"],
            # A server-side fallback answers under another model id; price it as
            # the model we asked for when that id is not in the table.
            "cost_usd": cost_usd(raw, resp.model if resp.model in PRICING else self.model),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "stop_reason": resp.stop_reason,
        }
        if resp.stop_reason != "end_turn":
            raise ClaudeStopped(f"Claude stopped with {resp.stop_reason!r}", usage)
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return first_json_object(text), usage
