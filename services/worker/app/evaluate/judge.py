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
import threading
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from . import scoring

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

PASS1_VERSION = "pass1-customer-v5"
PASS2_VERSION = "pass2-agent-quality-v6"

# Version and file are kept together on purpose: bumping one and not the other
# stamps a version string on a score the other prompt produced, and every
# comparison built on that column silently compares the wrong two things.
#
# PR2 round 3: pass2 moved v4 → v5 for the M3 counterweight paragraph. The
# previous iteration edited pass2_agent_quality_v4.md in place and called the
# result "revision v4.1" inside an HTML comment, leaving production rows
# stamped `pass2-agent-quality-v4` for two materially different texts. That is
# the one thing a prompt version exists to prevent, so v4 is now frozen as
# history and every further edit gets a new file and a new label.
#
# PR2 round 4: pass2 moved v5 → v6 for the closed, field-specific rewrite of
# Step 0's MANDATORY CONSISTENCY block. v5 is frozen as the audited candidate
# it was — it is the text every score stamped `pass2-agent-quality-v5` came
# from, and the round-3 audit is a record of what it does.
PASS1_PROMPT_FILE = "pass1_customer_v5.md"
PASS2_PROMPT_FILE = "pass2_agent_quality_v6.md"

# The explicit model id, not the `deepseek-chat` alias.
#
# Verified against the live API and the docs on 2026-08-22:
#   - GET /models returns deepseek-v4-flash, deepseek-v4-pro and
#     deepseek-v4-flash-vision-exp. `deepseek-chat` is NOT among them.
#   - The Chat API reference lists exactly those three under `model`
#     ("Possible values: [deepseek-v4-flash, deepseek-v4-pro,
#     deepseek-v4-flash-vision-exp]"); `deepseek-chat` is absent, as it is from
#     the pricing table.
#   - The 2026-04-24 changelog: "The two legacy API model names, `deepseek-chat`
#     and `deepseek-reasoner`, will be discontinued in three months
#     (2026-07-24). During the current period, these two model names point to
#     the non-thinking mode and thinking mode of `deepseek-v4-flash`."
#     That date has passed; the alias still answers, on borrowed time.
# So this is a rename to the id the alias already resolves to, not a change of
# model — a request sent under either name comes back `"model":
# "deepseek-v4-flash"` with the same `system_fingerprint`.
DEFAULT_MODEL = "deepseek-v4-flash"

# ...but only with thinking OFF, which is the half of the rename that is easy
# to get wrong. `deepseek-chat` mapped to the NON-thinking mode; `thinking`
# defaults to enabled, so `deepseek-v4-flash` on its own is a different judge.
# Measured on the same one-line probe:
#
#   deepseek-chat                     prompt 34 tok, reasoning_tokens absent
#   deepseek-v4-flash (default)       prompt 112 tok, reasoning_tokens 28
#   deepseek-v4-flash + disabled      prompt 34 tok, reasoning_tokens absent
#
# Sending it explicitly keeps this rename behaviour-preserving and stops a
# future default flip from silently re-baselining every score. Set
# DEEPSEEK_THINKING=enabled to try thinking mode — a new baseline, not a
# tweak: re-run the A/A comparison before believing any score it produces.
DEFAULT_THINKING = "disabled"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Rate-limit handling. DeepSeek's paid endpoint has never returned 429 to this
# worker; OpenRouter's free models do, per minute, and a burst of six calls
# (the workflow's claim batch) is enough to trip it. These bound the wait so a
# single /evaluate cannot outlive n8n's 300-second node timeout: worst case is
# two waits of MAX_RATE_LIMIT_WAIT plus the model's own latency.
RATE_LIMIT_BACKOFF = 20.0
MAX_RATE_LIMIT_WAIT = 65.0


def _retry_after_seconds(response) -> float | None:
    """The server's own answer to "when may I try again", in seconds.

    Accepts the numeric form only. The HTTP-date form is legal but no endpoint
    we talk to sends it, and guessing a clock skew is worse than falling back
    to our own backoff.
    """
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


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
    # "ok" | "contract_failed" | "ungradeable". None of the three is an
    # exception: the caller still gets the payload, the warnings and the reason,
    # and stores a row saying what could and could not be graded — which is
    # information. A 500 or a 422 in its place loses all of it.
    contract_status: str = "ok"
    evidence_rejected: list[dict[str, Any]] = field(default_factory=list)
    contract_violations: list[str] = field(default_factory=list)
    # Modules whose every deduction was discarded for want of evidence, so the
    # module scores `None` instead of being restored to a perfect 100.
    # `[{module, reason: "evidence_ungroundable", discarded_criteria: [...]}]`.
    ungradeable_modules: list[dict[str, Any]] = field(default_factory=list)
    # The weighted score of the breakdown the model last returned, taken the
    # instant BEFORE evidence enforcement touched it. Without it, a comparison
    # against an older prompt cannot tell "the new prompt judged differently"
    # from "the new code restored unsupported deductions" — the two land in one
    # number and the run measures nothing you can act on.
    pre_enforcement_score: float | None = None


@dataclass
class Pass1Result:
    payload: dict[str, Any]
    prompt_version: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    input_hash: str = ""
    validation: dict[str, Any] = field(default_factory=dict)


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
    # Shared by every instance in the process: see _pace().
    _pace_lock = threading.Lock()
    _last_request = 0.0

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, timeout: float = 180.0,
                 thinking: str | None = None):
        key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise JudgeError("DEEPSEEK_API_KEY is not set")
        self.model = model or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL
        # DEEPSEEK_BASE_URL lets the SAME judge code point at another
        # OpenAI-compatible endpoint (e.g. OpenRouter) without touching the
        # call sites. When unset, the DeepSeek default holds.
        # DEEPSEEK_THINKING=omit drops the DeepSeek-specific `thinking` field
        # entirely — required for endpoints that reject unknown parameters.
        # DEEPSEEK_REASONING_EFFORT sends OpenRouter's unified reasoning
        # control ({"reasoning": {"effort": ...}}). Not optional decoration for
        # a reasoning model like stealth/ox-alpha: without it the model burned
        # the WHOLE 8000-token budget on 26k characters of hidden reasoning and
        # returned content=null on every real pass-2 prompt (measured
        # 2026-08-24); with effort=low the same prompt answered valid JSON in
        # 14s. Leave unset for DeepSeek, whose API rejects unknown fields.
        base_url = base_url or os.getenv("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE_URL
        self.thinking = thinking or os.getenv("DEEPSEEK_THINKING") or DEFAULT_THINKING
        self.reasoning_effort = os.getenv("DEEPSEEK_REASONING_EFFORT") or None
        # Kept for the synthetic fingerprint below: when a non-DeepSeek
        # endpoint returns no system_fingerprint, the endpoint host + routed
        # provider + reasoning effort ARE the backend identity, and losing them
        # makes the score-comparability coordinate unrecoverable from the row.
        self._endpoint_host = (base_url.split("://", 1)[-1].split("/", 1)[0]
                               if base_url else "")
        # Self-pacing, same intent as CohereAPIBackend: a per-minute rate limit
        # is defeated by spacing requests out, not by retrying into a window
        # that has not reopened. Default 0 = off, so the DeepSeek path is
        # unchanged; set JUDGE_MIN_REQUEST_INTERVAL for a rate-limited endpoint.
        self._min_interval = float(os.getenv("JUDGE_MIN_REQUEST_INTERVAL", "0"))
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=timeout,
        )

    def _pace(self) -> None:
        """Hold the next request back until `_min_interval` has passed.

        The clock is CLASS-level, not per-instance, and that is the whole
        point. `/evaluate` builds a fresh client per request, so per-instance
        state paces a single evaluation against itself and does nothing
        between the concurrent evaluations that actually cause the burst —
        measured 2026-08-24: a 429 arrived 18 minutes AFTER per-instance
        pacing went live, because the six requests racing each other each held
        their own untouched clock.

        FastAPI runs sync handlers on a thread pool, so the check-and-set must
        be atomic or two threads both read a stale timestamp and burst anyway.
        """
        if self._min_interval <= 0:
            return
        with DeepSeekClient._pace_lock:
            wait = self._min_interval - (time.monotonic() - DeepSeekClient._last_request)
            if wait > 0:
                time.sleep(wait)
            DeepSeekClient._last_request = time.monotonic()

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
            #
            # It is not, however, a guarantee: DeepSeek documents no seed, and
            # the A/A run on day 13 moved 11 of 68 performance bands with the
            # same prompt and temperature 0. `system_fingerprint` below is the
            # only handle we have on WHY — see the comment on its capture.
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "thinking": {"type": self.thinking},
        }
        if self.thinking == "omit":
            # Non-DeepSeek endpoint: the field is DeepSeek-specific and some
            # providers 400 on parameters they do not know.
            del body["thinking"]
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        last: Exception | None = None
        for attempt in range(retries):
            try:
                self._pace()
                r = self._client.post("/chat/completions", json=body)
                if r.status_code == 429:
                    # A rate limit is not a transport blip: the generic
                    # 1/2/4-second backoff below expires long before a
                    # per-minute window resets, so all three attempts burn
                    # inside the same closed window and the caller sees a hard
                    # failure. Measured on OpenRouter 2026-08-24: six 429s in
                    # a row, one evaluation lost per burst of six calls.
                    # Honour Retry-After when the server sends one, cap the
                    # wait so a single call cannot outlive n8n's 300 s node
                    # timeout, and only then fall through to the retry.
                    wait = _retry_after_seconds(r) or min(RATE_LIMIT_BACKOFF * (attempt + 1),
                                                          MAX_RATE_LIMIT_WAIT)
                    if attempt < retries - 1:
                        time.sleep(min(wait, MAX_RATE_LIMIT_WAIT))
                        continue
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                # The backend configuration the answer was produced by, plus the
                # model id the API ECHOED rather than the one we asked for —
                # they differ whenever an alias is in play, and the difference is
                # exactly what a stored `model` column needs to record.
                #
                # Scores are only comparable within one fingerprint. DeepSeek
                # ships silently: `deepseek-chat` was re-pointed at V3-0324, then
                # at V3.1, then at V4-flash, with nothing in our rows to mark the
                # boundary. A month-over-month agent average that straddles one
                # is measuring the vendor's release schedule.
                usage = dict(data.get("usage") or {})
                usage["system_fingerprint"] = data.get("system_fingerprint")
                if not usage["system_fingerprint"] and (
                        self.reasoning_effort or os.getenv("DEEPSEEK_BASE_URL")):
                    # OpenRouter-style endpoints return no system_fingerprint,
                    # but the routed provider and the reasoning effort change
                    # the judge's behaviour exactly the way a fingerprint flip
                    # does. Compose one so model_fingerprint stays a real
                    # comparability key instead of NULL (Sol, 2026-08-24).
                    usage["system_fingerprint"] = (
                        f"{self._endpoint_host}:{data.get('provider') or 'unknown'}"
                        f":effort={self.reasoning_effort or 'default'}")
                usage["model"] = data.get("model")
                usage["model_requested"] = self.model
                usage["thinking"] = self.thinking
                if self.reasoning_effort:
                    usage["reasoning_effort"] = self.reasoning_effort
                if content is None:
                    # A reasoning model that burned the whole token budget
                    # thinking returns content=null with finish_reason=length.
                    # Surface it as a retryable JudgeError instead of an
                    # AttributeError inside _extract_json.
                    raise JudgeError(
                        "model returned no content (finish_reason="
                        f"{data['choices'][0].get('finish_reason')!r}; likely the "
                        "whole max_tokens budget went to hidden reasoning)")
                return _extract_json(content), usage
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
        _load(PASS2_PROMPT_FILE)
        .replace("{{CHANNEL_RULES}}", channel_rules)
        .replace("{{METADATA}}", json.dumps(metadata or {}, ensure_ascii=False, indent=2))
        .replace("{{FOLLOWUP_HISTORY}}", followup_history or "unavailable")
        .replace("{{CONVERSATION}}", conversation)
    )


def _intent_evidence_quotes(payload: dict) -> list[Any] | None:
    """Whatever this schema calls the quote behind `intent`, or None if it has none.

    pass1 v5 has no intent-evidence field. The probe is here so that adding one
    starts validating it automatically instead of silently reporting `null`
    forever — the alert rules key on `intent_evidence_valid`, and a field that
    is never checked but always says null is worse than one that is absent.
    """
    for key in ("intent_evidence", "intent_quote"):
        value = payload.get(key)
        if isinstance(value, list):
            return [q.get("quote") if isinstance(q, dict) else q for q in value]
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [value.get("quote")]
    intent = payload.get("intent")
    if isinstance(intent, dict) and "evidence" in intent:
        value = intent["evidence"]
        if isinstance(value, list):
            return [q.get("quote") if isinstance(q, dict) else q for q in value]
        return [value]
    return None


def validate_pass1(payload: dict, conversation: str) -> dict[str, Any]:
    """Check pass-1's quoted fields against the conversation. Never edits them.

    Alerts are built on `real_ask` and on `promises_made_by_agent`: one puts a
    salesperson on the phone, the other becomes a row in `follow_ups` and later
    an accusation that an agent broke a promise. Both rest on a quote the model
    supplies and nothing verified — pass 2 has checked its quotes since day one
    and pass 1 never did.

    The model's own fields are left exactly as returned. Overwriting them would
    destroy the evidence needed to tell a hallucination from a validator bug;
    the verdict goes alongside, under `pass1_validation`.

    `null` means the field was absent, not that it passed.
    """
    haystacks, folded = scoring.conversation_spans(conversation)

    def ok(quote: Any) -> bool:
        return scoring.quote_problem(
            quote if isinstance(quote, str) else None, haystacks, folded) is None

    real_ask = payload.get("real_ask")
    real_ask_valid: bool | None = None
    if isinstance(real_ask, dict):
        quotes = [e.get("quote") if isinstance(e, dict) else e
                  for e in (real_ask.get("evidence") or [])]
        if quotes:
            # Every quote offered must hold up: one fabricated line among three
            # is still a fabricated line, and this flag gates a real phone call.
            real_ask_valid = all(ok(q) for q in quotes)
        elif real_ask.get("is_real_inquiry"):
            real_ask_valid = False            # claimed true, quoted nothing
    elif isinstance(real_ask, bool):
        real_ask_valid = None                 # older shape, carries no quote

    promises = []
    raw_promises = payload.get("promises_made_by_agent")
    if isinstance(raw_promises, list):
        for i, item in enumerate(raw_promises):
            if isinstance(item, dict):
                # `promise` is the schema field. `quote` is a legacy shape and
                # is consulted only when `promise` is ABSENT — falling back on a
                # *present but empty* `promise` let {"promise": "", "quote": ...}
                # validate the wrong string and report the promise as checked.
                quote = item["promise"] if "promise" in item else item.get("quote")
            else:
                quote = item
            promises.append({"index": i, "quote_valid": ok(quote)})

    intent_quotes = _intent_evidence_quotes(payload)
    intent_valid = None if intent_quotes is None else all(ok(q) for q in intent_quotes)

    return {
        "real_ask_quote_valid": real_ask_valid,
        "promises": promises,
        "intent_evidence_valid": intent_valid,
        "validator_version": scoring.VALIDATOR_VERSION,
    }


def run_pass1(conversation: str, client: DeepSeekClient | None = None) -> Pass1Result:
    """Extract the customer's request. Never mentions the agent's performance."""
    client = client or DeepSeekClient()
    prompt = _load(PASS1_PROMPT_FILE).replace("{{CONVERSATION}}", conversation)
    payload, usage = client.complete_json(prompt)
    validation = validate_pass1(payload, conversation)
    payload["pass1_validation"] = validation
    return Pass1Result(
        payload=payload,
        prompt_version=PASS1_VERSION,
        model=client.model,
        usage=usage,
        input_hash=_hash_input(PASS1_VERSION, conversation),
        validation=validation,
    )


_CORRECTION_TEMPLATE = """

=============================================================
CORRECTION — YOUR PREVIOUS RESPONSE VIOLATED THE CONTRACT
=============================================================

This is the response you returned for this conversation:

{previous}

It had these problems:

{problems}

Re-score the SAME conversation, fixing exactly those problems. Change nothing
else. A criterion is `null` only when the situation genuinely did not arise —
never because it is hard to judge.

For every criterion named above as lacking evidence, you have exactly two
acceptable answers: either add ONE valid evidence entry naming this exact module
and criterion — verbatim, contiguous, and following the omission-anchor rule
when the finding is that something did not happen — or restore that criterion to
its cap. A deduction you cannot anchor in the customer's or the agent's own
words is not a finding.

Return the complete corrected JSON: every module, every breakdown, every
criterion key, not just the parts you changed.
"""


# Identity, not arithmetic: summing these would be nonsense and overwriting
# them loses the only evidence that an evaluation straddled a backend change.
_USAGE_IDENTITY_FIELDS = ("system_fingerprint", "model", "model_requested", "thinking",
                          "reasoning_effort")


def _merge_usage(*usages: dict[str, Any]) -> dict[str, Any]:
    """Sum token counters across the calls one evaluation actually made.

    The correction re-ask used to overwrite the first attempt's usage, so the
    cost report undercounted precisely the conversations that cost most —
    the ones that needed two calls. Non-numeric fields keep the last value seen.

    The identity fields are the exception. An evaluation is one or two API
    calls, and if the fingerprint changed between them the two halves of a
    single score came from different backends — worth knowing and impossible to
    reconstruct later, so a disagreement is recorded as
    `system_fingerprint_all` rather than resolved by taking the last one.
    """
    total: dict[str, Any] = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if key in _USAGE_IDENTITY_FIELDS:
                total[key] = value
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                total[key] = value
            else:
                total[key] = total.get(key, 0) + value if isinstance(
                    total.get(key, 0), (int, float)) else value
    for key in _USAGE_IDENTITY_FIELDS:
        seen = [u.get(key) for u in usages if u is not None and key in u]
        distinct = list(dict.fromkeys(seen))
        if len(distinct) > 1:
            total[f"{key}_all"] = distinct
    total["api_calls"] = sum(1 for u in usages if u is not None)
    return total


def run_pass2(conversation: str, input_type: Literal["chat", "call_transcript"],
              metadata: dict | None = None, followup_history: str | None = None,
              client: DeepSeekClient | None = None) -> Pass2Result:
    """Score the agent against the rubric, then recompute the arithmetic locally.

    A response that breaks the rubric contract — nulling or omitting an
    always-assessable criterion, claiming a stage its own scores contradict, or
    deducting points it cannot quote — is re-asked ONCE with the specific
    problems named and its own previous JSON quoted back to it (the API is
    stateless; "your previous response" means nothing without it). Repairing
    such a response locally would mean inventing a score the model never gave.

    The order is deliberate. Unsupported deductions are named in that same
    single re-ask, BEFORE any of them are restored, so the model gets one chance
    to produce the anchoring quote or withdraw the finding itself. Only findings
    still unsupported after the correction have their points handed back. The
    previous order — enforce first, never ask — restored every unanchored
    omission finding unasked, which measures the code's leniency rather than the
    judge's.

    Three outcomes, all of them returned rather than raised:

    - `contract_status="ok"` — scored, with any still-unsupported findings
      discarded and listed in `evidence_rejected`.
    - `contract_status="contract_failed"` — the response still contradicts
      itself, still nulls an always-assessable criterion, or still omits part of
      the rubric after the correction. No score, no partial denominator, and the
      violations are named.
    - `contract_status="ungradeable"`, `gradeable=False`, `final_score=None` —
      too little of the rubric survived to average into anything. Reached when
      whole modules were struck out as `evidence_ungroundable` (below), and in
      theory when the model's own legitimate nulls leave under the 0.40 floor.

    Only structurally unusable output (unparseable, or no `modules`) raises.

    **Ungroundable modules.** An isolated unsupported finding restores to its
    cap: the judge over-reached on one criterion and the agent keeps those
    points. A module in which EVERY deduction was discarded is different — the
    judge produced nothing about that module that could be grounded, and
    restoring all of it says "perfect" on the strength of nothing. Those modules
    score `None`, out of numerator and denominator alike, and if what remains is
    under `MIN_WEIGHT_APPLIED` the whole call is ungradeable. Day 13,
    `e5ab9937`: one mistranslated quote offered for six deductions across two
    modules, and a 34-second call scored **100**. It now scores nothing.
    """
    client = client or DeepSeekClient()
    prompt = build_pass2_prompt(conversation, input_type, metadata, followup_history)
    input_hash = _hash_input(PASS2_VERSION, scoring.RUBRIC_VERSION, input_type, conversation)

    payload, first_usage = client.complete_json(prompt)
    usage = _merge_usage(first_usage)
    modules = payload.get("modules")
    if not isinstance(modules, dict):
        raise JudgeError("response has no 'modules' object")

    # Both classes of problem go into the ONE correction: structural contract
    # violations, and every below-cap criterion that cites no usable quote.
    violations = scoring.contract_violations(payload, modules)
    evidence_problems = scoring.criterion_evidence_problems(payload, modules, conversation)

    retried = False
    if violations or evidence_problems:
        retried = True
        previous = json.dumps(payload, ensure_ascii=False, indent=2)
        corrected = prompt + _CORRECTION_TEMPLATE.format(
            previous=previous,
            problems="\n".join(f"  - {v}" for v in violations + evidence_problems),
        )
        payload, second_usage = client.complete_json(corrected)
        usage = _merge_usage(first_usage, second_usage)
        modules = payload.get("modules")
        if not isinstance(modules, dict):
            raise JudgeError("response has no 'modules' object after correction")
        violations = scoring.contract_violations(payload, modules)

    warnings: list[str] = []
    if retried:
        warnings.append(
            f"first response violated the rubric contract or cited no evidence for "
            f"{len(evidence_problems)} deduction(s); re-asked once"
        )

    # A response that still contradicts itself after being told exactly what
    # contradicts is not a score with a caveat. It asserts a refusal happened
    # and did not happen, a stage its own criteria deny, or a rubric with pieces
    # missing; picking one would mean inventing a judgement the model never
    # made. Return the evidence of the failure and no number.
    hard = scoring.hard_violations(payload, modules)
    if hard:
        warnings += [f"UNRESOLVED after retry: {v}" for v in hard]
        # No partial weight, no partial module scores. A denominator built from
        # whichever criteria survived is a number that looks meaningful, gets
        # stored by n8n, and averages into an agent's month — while the reason
        # it is wrong sits in a warnings array nobody aggregates. The raw
        # breakdown stays inside `payload` for forensics and nowhere else.
        module_scores = {k: None for k in scoring.WEIGHTS}
        payload["final_score"] = None
        payload["performance_level"] = None
        payload["weight_applied"] = 0.0
        payload["contract_status"] = "contract_failed"
        payload["contract_violations"] = hard
        payload["evidence_rejected"] = []
        payload["ungradeable_modules"] = []
        return Pass2Result(
            payload=payload,
            score=scoring.ScoreResult(None, None, 0.0, module_scores, False, warnings),
            prompt_version=PASS2_VERSION,
            rubric_version=scoring.RUBRIC_VERSION,
            model=client.model,
            warnings=warnings,
            usage=usage,
            input_hash=input_hash,
            contract_status="contract_failed",
            contract_violations=hard,
        )

    warnings += [f"UNRESOLVED after retry: {v}" for v in violations]

    # The score of what the model actually returned, before we touch it. This is
    # the only place it exists: enforcement mutates the breakdown in place.
    try:
        pre_enforcement_score = scoring.compute(modules).final_score
    except scoring.RubricError:
        pre_enforcement_score = None

    # Which criteria the model actually deducted on, taken BEFORE enforcement
    # rewrites the discarded ones to their caps. Afterwards the question "was
    # anything in this module still standing" cannot be answered.
    deductions_before = scoring.deducted_criteria(modules)

    # Enforce the evidence rule the prompt has always stated, on the findings
    # that survived the correction: a below-cap criterion with no valid quote
    # for THAT criterion is not a finding, so the points go back and the score
    # is recomputed over the corrected breakdown. Order matters — this used to
    # run after `compute`, so a discarded finding kept its deduction and only
    # added a warning.
    rejected = scoring.enforce_criterion_evidence(payload, modules, conversation)

    # A module whose every deduction was discarded was not graded at all. Null
    # it rather than hand it a perfect score built on nothing.
    ungroundable = scoring.ungroundable_modules(deductions_before, rejected)
    ungradeable_modules = [
        {
            "module": module,
            "reason": "evidence_ungroundable",
            "discarded_criteria": sorted(deductions_before[module]),
        }
        for module in ungroundable
    ]

    # JUDGE_M4_QUARANTINE: strike Module 4 whenever a follow-up history block
    # was actually sent. Measured 2026-08-24 on the D6 fixtures: the ox-alpha
    # judge scored the customer's OWN inbound callback as a failed agent
    # follow-up (M4=0) three runs out of three, and missed the real outbound
    # follow-up two out of three — with the rule already stated in the prompt.
    # A judge that punishes agents for customers calling back cannot grade this
    # module, so the module is null (out of numerator AND denominator, weight
    # recomputed by scoring.compute), not zero and not trusted. Off by default;
    # set the env only for models with a demonstrated M4 failure.
    quarantine = set()
    if os.getenv("JUDGE_M4_QUARANTINE"):
        history = (followup_history or "").strip()
        if history and history != "unavailable" and "module4_followup" not in ungroundable:
            quarantine = {"module4_followup"}
            ungradeable_modules.append({
                "module": "module4_followup",
                "reason": "m4_model_quarantine",
                "discarded_criteria": [],
            })
            warnings.append(
                "module4_followup: m4_model_quarantine — a follow-up history was "
                "sent and this judge model mis-scores M4 (D6 0/6); module nulled, "
                "weight recomputed")

    result = scoring.compute(modules, ungradeable_modules=set(ungroundable) | quarantine)

    warnings += result.warnings
    warnings += scoring.validate_evidence(payload, conversation)
    warnings += scoring.require_evidence_for_deductions(payload, result.modules)
    warnings += [
        f"{r['module']}.{r['criterion']}: {r['reason']} — finding discarded, "
        f"{r['model_score']} restored to {r['restored_to']}"
        for r in rejected
    ]
    warnings += [
        f"{entry['module']}: evidence_ungroundable — every deduction in this "
        f"module ({', '.join(entry['discarded_criteria'])}) was discarded for "
        f"want of a valid quote, so the module is null, not 100"
        for entry in ungradeable_modules
    ]

    # `gradeable=False` here means too little of the rubric survived to average.
    # It is not "ok with a caveat": `contract_status` says so by name, because
    # the day-13 failure was precisely a row whose notes said null and whose
    # stored arithmetic said 100, and nothing in the status column disagreed.
    contract_status = "ok" if result.gradeable else "ungradeable"

    # Overwrite whatever the model computed. Ours is the number that gets stored.
    payload["final_score"] = result.final_score
    payload["performance_level"] = result.performance_level
    payload["weight_applied"] = result.weight_applied
    payload["contract_status"] = contract_status
    payload["contract_violations"] = violations
    payload["evidence_rejected"] = rejected
    payload["ungradeable_modules"] = ungradeable_modules
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
        input_hash=input_hash,
        contract_status=contract_status,
        evidence_rejected=rejected,
        contract_violations=violations,
        ungradeable_modules=ungradeable_modules,
        pre_enforcement_score=pre_enforcement_score,
    )
