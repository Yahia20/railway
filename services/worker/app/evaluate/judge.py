"""The two AI passes, against DeepSeek.

Pass 1 extracts what the customer wants. Pass 2 scores the agent. They are
separate API calls with separate prompts and neither sees the other's output.
That separation is the most important rule in this stage: a single prompt doing
both lets an angry customer drag down the agent's score and lets a strong agent
inflate the sales forecast, and you cannot tell afterwards which happened.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from . import scoring

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

PASS1_VERSION = "pass1-customer-v4"
PASS2_VERSION = "pass2-agent-quality-v3"

DEFAULT_MODEL = "deepseek-chat"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class JudgeError(RuntimeError):
    pass


@dataclass
class Pass2Result:
    payload: dict[str, Any]
    score: scoring.ScoreResult
    prompt_version: str
    rubric_version: str
    model: str
    warnings: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    input_hash: str = ""


@dataclass
class Pass1Result:
    payload: dict[str, Any]
    prompt_version: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    input_hash: str = ""


def _load(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _extract_json(text: str) -> dict:
    """Parse the model's reply as JSON, tolerating a stray code fence.

    Anything beyond stripping a fence is refused on purpose: silently repairing
    malformed output hides a prompt that has started drifting, and you find out
    weeks later from bad numbers instead of immediately from a failed row.
    """
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"model did not return valid JSON: {exc}; got {cleaned[:300]!r}") from exc


def _hash_input(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class DeepSeekClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 base_url: str = DEEPSEEK_BASE_URL, timeout: float = 180.0):
        key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise JudgeError("DEEPSEEK_API_KEY is not set")
        self.model = model
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=timeout,
        )

    def complete_json(self, prompt: str, temperature: float = 0.0,
                      max_tokens: int = 8000, retries: int = 3) -> tuple[dict, dict]:
        # 8000 is a mitigation for long calls whose pass-2 JSON overran 4096
        # and died truncated 3/3; the real fix is segmenting long transcripts.
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            # Deterministic on purpose. A QA score that changes when you re-run
            # the same conversation is not a measurement, and agents will notice
            # the inconsistency long before management does.
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self._client.post("/chat/completions", json=body)
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                return _extract_json(content), data.get("usage", {})
            except (httpx.HTTPError, KeyError, JudgeError) as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise JudgeError(f"DeepSeek call failed after {retries} attempts: {last}")


def build_pass2_prompt(conversation: str, input_type: Literal["chat", "call_transcript"],
                       metadata: dict | None = None,
                       followup_history: str | None = None) -> str:
    channel_rules = _load(
        "channel_rules_call_v1.md" if input_type == "call_transcript"
        else "channel_rules_chat_v1.md"
    )
    return (
        _load("pass2_agent_quality_v3.md")
        .replace("{{CHANNEL_RULES}}", channel_rules)
        .replace("{{METADATA}}", json.dumps(metadata or {}, ensure_ascii=False, indent=2))
        .replace("{{FOLLOWUP_HISTORY}}", followup_history or "unavailable")
        .replace("{{CONVERSATION}}", conversation)
    )


def run_pass1(conversation: str, client: DeepSeekClient | None = None) -> Pass1Result:
    """Extract the customer's request. Never mentions the agent's performance."""
    client = client or DeepSeekClient()
    prompt = _load("pass1_customer_v4.md").replace("{{CONVERSATION}}", conversation)
    payload, usage = client.complete_json(prompt)
    return Pass1Result(
        payload=payload,
        prompt_version=PASS1_VERSION,
        model=client.model,
        usage=usage,
        input_hash=_hash_input(PASS1_VERSION, conversation),
    )


_CORRECTION_TEMPLATE = """

=============================================================
CORRECTION — YOUR PREVIOUS RESPONSE VIOLATED THE CONTRACT
=============================================================

Your last answer had these problems:

{problems}

Re-score the SAME conversation, fixing exactly those problems. Change nothing
else. A criterion is `null` only when the situation genuinely did not arise —
never because it is hard to judge.
"""


def run_pass2(conversation: str, input_type: Literal["chat", "call_transcript"],
              metadata: dict | None = None, followup_history: str | None = None,
              client: DeepSeekClient | None = None) -> Pass2Result:
    """Score the agent against the rubric, then recompute the arithmetic locally.

    A response that breaks the rubric contract — nulling an always-assessable
    criterion, or claiming a stage its own scores contradict — is re-asked once
    with the specific problems named. Repairing such a response locally would
    mean inventing a score the model never gave.
    """
    client = client or DeepSeekClient()
    prompt = build_pass2_prompt(conversation, input_type, metadata, followup_history)

    payload, usage = client.complete_json(prompt)
    modules = payload.get("modules")
    if not isinstance(modules, dict):
        raise JudgeError("response has no 'modules' object")

    violations = scoring.contract_violations(payload, modules)
    retried = False
    if violations:
        retried = True
        corrected = prompt + _CORRECTION_TEMPLATE.format(
            problems="\n".join(f"  - {v}" for v in violations)
        )
        payload, usage = client.complete_json(corrected)
        modules = payload.get("modules")
        if not isinstance(modules, dict):
            raise JudgeError("response has no 'modules' object after correction")
        violations = scoring.contract_violations(payload, modules)

    result = scoring.compute(modules)

    warnings = list(result.warnings)
    warnings += scoring.validate_evidence(payload, conversation)
    warnings += scoring.require_evidence_for_deductions(payload, result.modules)
    if retried:
        warnings.append("first response violated the rubric contract; re-asked once")
    warnings += [f"UNRESOLVED after retry: {v}" for v in violations]

    # Overwrite whatever the model computed. Ours is the number that gets stored.
    payload["final_score"] = result.final_score
    payload["performance_level"] = result.performance_level
    payload["weight_applied"] = result.weight_applied
    for key, value in result.modules.items():
        payload.setdefault("modules", {}).setdefault(key, {})["score"] = value

    return Pass2Result(
        payload=payload,
        score=result,
        prompt_version=PASS2_VERSION,
        rubric_version=scoring.RUBRIC_VERSION,
        model=client.model,
        warnings=warnings,
        usage=usage,
        input_hash=_hash_input(PASS2_VERSION, scoring.RUBRIC_VERSION, input_type, conversation),
    )
