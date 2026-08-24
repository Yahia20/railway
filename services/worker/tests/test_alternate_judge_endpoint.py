"""The judge on a non-DeepSeek endpoint: base URL, request shape, identity.

Written for the 2026-08-24 swap to OpenRouter's `stealth/ox-alpha` and reviewed
by Sol the same day. What was measured against the live endpoint, and is
encoded here so it cannot regress silently:

- `reasoning: {enabled: false}` is HTTP 400 — reasoning is mandatory for the
  model; the only workable control is `reasoning: {effort: ...}`. Without it
  the model spent the WHOLE 8000-token budget on 26k characters of hidden
  reasoning and returned `content: null` with `finish_reason: "length"` on
  every real pass-2 prompt.
- OpenRouter returns no `system_fingerprint`, but the routed `provider` and
  the reasoning effort change the judge exactly the way a fingerprint flip
  does, so a synthetic one is composed rather than storing NULL.
- The D6 M4 fixtures measured 0/6 on this model (the customer's own inbound
  callback scored as a failed agent follow-up 3/3), hence JUDGE_M4_QUARANTINE.

Nothing here talks to any API.
"""
from __future__ import annotations

import pytest

from app.evaluate import judge, scoring

from test_evidence_rejection import StubClient, _breakdowns, _payload
from test_model_fingerprint import _Recorder, _reply


def _client(monkeypatch, *responses, env=None, **kwargs):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    for key in ("DEEPSEEK_MODEL", "DEEPSEEK_THINKING", "DEEPSEEK_BASE_URL",
                "DEEPSEEK_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    client = judge.DeepSeekClient(**kwargs)
    client._client = _Recorder(*responses)
    return client


# ── base URL precedence ─────────────────────────────────────────────────────

def test_base_url_env_overrides_the_deepseek_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://openrouter.ai/api/v1")
    client = judge.DeepSeekClient()
    assert str(client._client.base_url).startswith("https://openrouter.ai/api/v1")


def test_without_the_env_the_default_still_holds(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    client = judge.DeepSeekClient()
    assert str(client._client.base_url).startswith(judge.DEEPSEEK_BASE_URL)


def test_an_explicit_argument_beats_the_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://openrouter.ai/api/v1")
    client = judge.DeepSeekClient(base_url="https://example.test")
    assert str(client._client.base_url).startswith("https://example.test")


# ── request shape for a foreign endpoint ────────────────────────────────────

def test_thinking_omit_drops_the_deepseek_only_field(monkeypatch):
    client = _client(monkeypatch, _reply(), env={"DEEPSEEK_THINKING": "omit"})
    client.complete_json("hello")
    assert "thinking" not in client._client.bodies[0]


def test_reasoning_effort_env_adds_the_openrouter_parameter(monkeypatch):
    client = _client(monkeypatch, _reply(),
                     env={"DEEPSEEK_REASONING_EFFORT": "low"})
    client.complete_json("hello")
    assert client._client.bodies[0]["reasoning"] == {"effort": "low"}


def test_no_reasoning_field_without_the_env(monkeypatch):
    client = _client(monkeypatch, _reply())
    client.complete_json("hello")
    assert "reasoning" not in client._client.bodies[0]


# ── null content is an error, not an AttributeError ─────────────────────────

def test_null_content_raises_a_retryable_judge_error(monkeypatch):
    reply = _reply()
    reply["choices"][0]["message"]["content"] = None
    reply["choices"][0]["finish_reason"] = "length"
    client = _client(monkeypatch, reply)
    with pytest.raises(judge.JudgeError, match="no content"):
        client.complete_json("hello", retries=1)


# ── synthetic fingerprint ───────────────────────────────────────────────────

def test_missing_fingerprint_is_composed_from_host_provider_and_effort(monkeypatch):
    reply = _reply(fingerprint=None)
    reply["provider"] = "Stealth"
    client = _client(monkeypatch, reply,
                     env={"DEEPSEEK_BASE_URL": "https://openrouter.ai/api/v1",
                          "DEEPSEEK_REASONING_EFFORT": "low"})
    _, usage = client.complete_json("hello")
    assert usage["system_fingerprint"] == "openrouter.ai:Stealth:effort=low"
    assert usage["reasoning_effort"] == "low"


def test_a_real_fingerprint_is_never_overwritten(monkeypatch):
    client = _client(monkeypatch, _reply(fingerprint="fp_real"),
                     env={"DEEPSEEK_REASONING_EFFORT": "low"})
    _, usage = client.complete_json("hello")
    assert usage["system_fingerprint"] == "fp_real"


def test_deepseek_path_still_stores_null_when_the_api_sends_none(monkeypatch):
    """No env set + no fingerprint from the API -> NULL, exactly as before."""
    client = _client(monkeypatch, _reply(fingerprint=None))
    _, usage = client.complete_json("hello")
    assert usage["system_fingerprint"] is None


# ── the M4 quarantine ───────────────────────────────────────────────────────

HISTORY = "  - [2026-08-15 14:02+03] whatsapp by سارة, 19.5h after this call"

CONVERSATION = (
    "[00:00] ألو السلام عليكم أبغى أسأل عن باقة جورجيا. "
    "[00:20] عندنا باقة سبع ليال بستة آلاف ريال شاملة الطيران والفندق. "
    "[00:40] طيب أفكر وأرد عليكم. تمام في انتظارك."
)


def _clean_payload():
    """Contract-valid: full marks on M1/M2/M4 (no deductions, so no evidence
    is owed), M3/M5 legitimately null."""
    modules = _breakdowns(
        module3_objections=dict.fromkeys(
            scoring.CRITERION_MAX["module3_objections"], None),
        module5_closing=dict.fromkeys(
            scoring.CRITERION_MAX["module5_closing"], None),
    )
    return _payload(modules, stage_reached="negotiation")


def _run(monkeypatch, history, quarantine):
    if quarantine:
        monkeypatch.setenv("JUDGE_M4_QUARANTINE", "1")
    else:
        monkeypatch.delenv("JUDGE_M4_QUARANTINE", raising=False)
    payload = _clean_payload()
    client = StubClient(payload, payload)
    return judge.run_pass2(CONVERSATION, "call_transcript",
                           followup_history=history, client=client)


def test_quarantine_nulls_m4_when_a_history_was_sent(monkeypatch):
    result = _run(monkeypatch, HISTORY, quarantine=True)
    assert result.score.modules["module4_followup"] is None
    assert {"module": "module4_followup", "reason": "m4_model_quarantine",
            "discarded_criteria": []} in result.ungradeable_modules
    # The weight was recomputed, not merely the column blanked.
    assert result.score.weight_applied == pytest.approx(
        sum(w for k, w in scoring.WEIGHTS.items()
            if k not in ("module3_objections", "module4_followup", "module5_closing")))
    assert result.contract_status == "ok"
    assert result.score.final_score is not None


def test_quarantine_is_inert_without_a_history(monkeypatch):
    for history in (None, "", "unavailable"):
        result = _run(monkeypatch, history, quarantine=True)
        assert result.score.modules["module4_followup"] is not None, history
        assert all(e["reason"] != "m4_model_quarantine"
                   for e in result.ungradeable_modules)


def test_no_quarantine_without_the_env(monkeypatch):
    result = _run(monkeypatch, HISTORY, quarantine=False)
    assert result.score.modules["module4_followup"] is not None
    assert all(e["reason"] != "m4_model_quarantine"
               for e in result.ungradeable_modules)
