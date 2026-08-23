"""Which model answered, and under which backend configuration.

Scores are only comparable within one model and one backend build. DeepSeek
re-points its aliases without notice — `deepseek-chat` has meant V3, then
V3-0324, then V3.1, then the non-thinking mode of V4-flash — and every one of
those transitions passed through this pipeline as an unmarked change of judge.
An agent's month-over-month average that straddles one is measuring the
vendor's release schedule.

So three things are asserted here:

  1. the request names an explicit model id, never a legacy alias, and never
     silently switches thinking mode on;
  2. the response's `system_fingerprint` and its ECHOED `model` are carried into
     `usage` and out through `/evaluate`, so a change is visible downstream
     instead of being inferred from scores moving;
  3. a two-call evaluation that straddles a fingerprint change says so rather
     than reporting the last one it saw.

Nothing here talks to DeepSeek. The API behaviour these assertions encode was
measured against the live API on 2026-08-22 and is recorded in
docs/PR2-judge-integrity.md.
"""
from __future__ import annotations

import json

from pathlib import Path

import pytest

from app.evaluate import judge


# ── the request ─────────────────────────────────────────────────────────────

class _Recorder:
    """An httpx.Client stand-in that records the body and replays a response."""

    def __init__(self, *responses):
        self.bodies: list[dict] = []
        self._responses = list(responses)

    def post(self, _url, json=None):          # noqa: A002 - httpx's parameter name
        self.bodies.append(json)
        i = min(len(self.bodies) - 1, len(self._responses) - 1)
        return _Response(self._responses[i])


class _Response:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def _reply(content='{"ok": 1}', fingerprint="fp_aaa", model="deepseek-v4-flash",
           **usage):
    return {
        "model": model,
        "system_fingerprint": fingerprint,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, **usage},
    }


def _client(monkeypatch, *responses, **kwargs):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    for key, value in list(kwargs.pop("env", {}).items()):
        monkeypatch.setenv(key, value)
    client = judge.DeepSeekClient(**kwargs)
    client._client = _Recorder(*responses)
    return client


def test_the_default_model_is_an_explicit_id_not_a_legacy_alias():
    """`deepseek-chat` was scheduled for removal on 2026-07-24 and is absent
    from GET /models, the API reference and the pricing table. It still answers,
    which is exactly why nothing will fail loudly when it stops."""
    assert judge.DEFAULT_MODEL == "deepseek-v4-flash"
    assert "chat" not in judge.DEFAULT_MODEL


def test_thinking_is_disabled_by_default():
    """The alias mapped to the NON-thinking mode; `thinking` defaults to
    enabled. Renaming without this would have swapped the judge for a different
    one and re-baselined every score, with nothing in the diff to show it."""
    assert judge.DEFAULT_THINKING == "disabled"


def test_the_request_carries_the_model_and_the_thinking_mode(monkeypatch):
    client = _client(monkeypatch, _reply())
    client.complete_json("hello")

    body = client._client.bodies[0]
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_object"}


def test_the_env_overrides_the_default_model(monkeypatch):
    client = _client(monkeypatch, _reply(model="deepseek-v4-pro"),
                     env={"DEEPSEEK_MODEL": "deepseek-v4-pro"})
    client.complete_json("hello")
    assert client._client.bodies[0]["model"] == "deepseek-v4-pro"


def test_an_explicit_argument_beats_the_env(monkeypatch):
    """`main.py` passes `settings.deepseek_model`, which may be None. None must
    fall through to the env and then to the default — not become the string
    "None" in a request body."""
    client = _client(monkeypatch, _reply(), model=None,
                     env={"DEEPSEEK_MODEL": "deepseek-v4-pro"})
    assert client.model == "deepseek-v4-pro"

    explicit = _client(monkeypatch, _reply(), model="deepseek-v4-flash",
                       env={"DEEPSEEK_MODEL": "deepseek-v4-pro"})
    assert explicit.model == "deepseek-v4-flash"


# ── the response ────────────────────────────────────────────────────────────

def test_the_fingerprint_and_the_echoed_model_land_in_usage(monkeypatch):
    client = _client(monkeypatch, _reply(fingerprint="fp_123", model="deepseek-v4-flash"))
    _, usage = client.complete_json("hello")

    assert usage["system_fingerprint"] == "fp_123"
    # The model the API says answered, which is NOT always the one asked for.
    assert usage["model"] == "deepseek-v4-flash"
    assert usage["model_requested"] == "deepseek-v4-flash"
    assert usage["thinking"] == "disabled"
    # and the token counters are still there
    assert usage["prompt_tokens"] == 10


def test_an_alias_answer_records_both_names(monkeypatch):
    """Ask for an alias, get the real id back. Storing only the requested name
    is how a rename becomes invisible; storing only the echoed one loses what
    the config actually says."""
    client = _client(monkeypatch, _reply(model="deepseek-v4-flash"),
                     env={"DEEPSEEK_MODEL": "deepseek-chat"})
    _, usage = client.complete_json("hello")
    assert usage["model_requested"] == "deepseek-chat"
    assert usage["model"] == "deepseek-v4-flash"


def test_a_missing_fingerprint_is_recorded_as_null_not_dropped(monkeypatch):
    """A key that vanishes when the field is absent makes "we did not look"
    indistinguishable from "the API sent none"."""
    reply = _reply()
    del reply["system_fingerprint"]
    client = _client(monkeypatch, reply)
    _, usage = client.complete_json("hello")
    assert "system_fingerprint" in usage
    assert usage["system_fingerprint"] is None


# ── merging across the correction re-ask ────────────────────────────────────

def test_merging_sums_tokens_and_does_not_sum_identities():
    merged = judge._merge_usage(
        {"prompt_tokens": 10, "completion_tokens": 5,
         "system_fingerprint": "fp_a", "model": "deepseek-v4-flash"},
        {"prompt_tokens": 20, "completion_tokens": 7,
         "system_fingerprint": "fp_a", "model": "deepseek-v4-flash"},
    )
    assert merged["prompt_tokens"] == 30
    assert merged["completion_tokens"] == 12
    assert merged["api_calls"] == 2
    assert merged["system_fingerprint"] == "fp_a"
    assert "system_fingerprint_all" not in merged


def test_a_fingerprint_change_mid_evaluation_is_reported_not_hidden():
    """One score, two backends. Rare and worth knowing: the correction re-ask
    is where a deployment can land in the middle of a single evaluation, and
    "keep the last value" would quietly attribute the whole score to the second.
    """
    merged = judge._merge_usage(
        {"prompt_tokens": 10, "system_fingerprint": "fp_a"},
        {"prompt_tokens": 20, "system_fingerprint": "fp_b"},
    )
    assert merged["system_fingerprint_all"] == ["fp_a", "fp_b"]
    assert merged["prompt_tokens"] == 30


# ── end to end through run_pass1 / run_pass2 ────────────────────────────────

CONVERSATION = (
    "[00:03] AGENT: السلام عليكم ترافل جيت مع خالد\n"
    "[00:07] CUSTOMER: عليكم السلام، أبغى عرض لتركيا لعائلة أربعة أشخاص\n"
    "[00:21] AGENT: أبشر، أحسب لك وأرسل لك العرض\n"
)


class _StubClient:
    """Answers from a script, with a usage block shaped like the real one."""

    def __init__(self, *payloads, fingerprint="fp_live", model="deepseek-v4-flash"):
        self.model = model
        self.thinking = "disabled"
        self._payloads = list(payloads)
        self.calls = 0

    def complete_json(self, prompt, **_):
        import copy
        i = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return copy.deepcopy(self._payloads[i]), {
            "prompt_tokens": 10, "completion_tokens": 5,
            "system_fingerprint": "fp_live", "model": self.model,
            "model_requested": self.model, "thinking": self.thinking,
        }


def test_pass1_carries_the_fingerprint_out():
    client = _StubClient({"real_ask": {"is_real_inquiry": False, "evidence": []}})
    result = judge.run_pass1(CONVERSATION, client=client)
    assert result.usage["system_fingerprint"] == "fp_live"
    assert result.usage["model"] == "deepseek-v4-flash"


def test_pass2_carries_the_fingerprint_out():
    from app.evaluate import scoring

    modules = {key: {"score": None, "breakdown": dict(caps)}
               for key, caps in scoring.CRITERION_MAX.items()}
    payload = {"schema_version": "1.0", "stage_reached": "closing",
               "modules": modules, "evidence": []}
    client = _StubClient(payload, payload)
    result = judge.run_pass2(CONVERSATION, "call_transcript", client=client)
    assert result.usage["system_fingerprint"] == "fp_live"
    assert result.usage["model"] == "deepseek-v4-flash"
    assert result.usage["api_calls"] >= 1


def test_evaluate_exposes_the_fingerprint_on_both_passes(monkeypatch):
    """The whole point of capturing it: a consumer can group by it.

    Reaching into `pass2.payload` for it would not do — the payload is the
    model's own JSON and the fingerprint is not something the model said.
    """
    from fastapi.testclient import TestClient

    from app import main
    from app.evaluate import scoring

    modules = {key: {"score": None, "breakdown": dict(caps)}
               for key, caps in scoring.CRITERION_MAX.items()}
    payload = {"schema_version": "1.0", "stage_reached": "closing",
               "modules": modules, "evidence": []}

    monkeypatch.setattr(main.settings, "worker_api_key", "k")
    monkeypatch.setattr(main.settings, "deepseek_api_key", "key")
    monkeypatch.setattr(
        main.judge, "DeepSeekClient",
        lambda *a, **kw: _StubClient(
            {"real_ask": {"is_real_inquiry": False, "evidence": []}},
            payload, payload))

    body = {"conversation": CONVERSATION * 3, "input_type": "call_transcript"}
    response = TestClient(main.app).post(
        "/evaluate", json=body, headers={"X-API-Key": "k"})
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["pass1"]["usage"]["system_fingerprint"] == "fp_live"
    assert out["pass2"]["usage"]["system_fingerprint"] == "fp_live"
    assert out["pass1"]["usage"]["model"] == "deepseek-v4-flash"
    assert out["pass2"]["usage"]["model"] == "deepseek-v4-flash"


def test_ready_reports_the_model_it_will_actually_ask_for(monkeypatch):
    """A stale DEEPSEEK_MODEL on the platform beats every default in the source
    and is invisible in a diff. /ready is where that gets caught."""
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main.settings, "worker_api_key", "k")
    monkeypatch.setattr(main.settings, "deepseek_model", None)
    response = TestClient(main.app).get("/ready", headers={"X-API-Key": "k"})
    assert response.json()["judge_model"] == judge.DEFAULT_MODEL

    monkeypatch.setattr(main.settings, "deepseek_model", "deepseek-chat")
    response = TestClient(main.app).get("/ready", headers={"X-API-Key": "k"})
    assert response.json()["judge_model"] == "deepseek-chat"


def test_ready_reports_the_thinking_mode_as_well(monkeypatch):
    """The half that is easy to get wrong.

    `deepseek-chat` resolved to V4 Flash NON-thinking, while an explicit v4
    request defaults to thinking ENABLED. A rename that reports only the model
    would show green on /ready while having quietly swapped the judge, and
    every score after it would be on a different baseline.
    """
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main.settings, "worker_api_key", "k")
    monkeypatch.setattr(main.settings, "deepseek_thinking", None)
    body = TestClient(main.app).get("/ready", headers={"X-API-Key": "k"}).json()
    assert body["judge_thinking"] == judge.DEFAULT_THINKING == "disabled"

    monkeypatch.setattr(main.settings, "deepseek_thinking", "enabled")
    body = TestClient(main.app).get("/ready", headers={"X-API-Key": "k"}).json()
    assert body["judge_thinking"] == "enabled"


def test_the_platform_script_pins_the_same_model_and_thinking_as_the_source():
    """An environment variable beats every default in the source.

    `scripts/railway_configure.py` writes the worker's Railway variables, and
    for the whole of round 3 it still wrote `DEEPSEEK_MODEL=deepseek-chat` — so
    the rename was true in the repository and false in production, and the only
    place the difference was visible was /ready. Pinning it here means the two
    can only drift on purpose.

    This asserts the TEXT of the script. Running it would talk to Railway.
    """
    script = (Path(__file__).resolve().parents[3] / "scripts"
              / "railway_configure.py").read_text(encoding="utf-8")
    assert f'"DEEPSEEK_MODEL": "{judge.DEFAULT_MODEL}"' in script
    assert f'"DEEPSEEK_THINKING": "{judge.DEFAULT_THINKING}"' in script
    assert '"deepseek-chat"' not in script
