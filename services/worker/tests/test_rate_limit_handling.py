"""429 handling: a rate limit is a wait, not a transport blip.

Measured on OpenRouter's free `stealth/ox-alpha` on 2026-08-24, one day after
it became the production judge: the workflow claims six calls, each evaluation
makes two to three model calls, and the burst trips a per-minute limit. The
generic 1/2/4-second backoff expired inside the same closed window, so all
three attempts failed and n8n recorded one lost evaluation per burst — twice
in the first twelve production calls.

Two behaviours are asserted here:

  1. a 429 waits — honouring `Retry-After` when the server sends one, and
     capped so a single /evaluate cannot outlive n8n's 300-second node
     timeout;
  2. the client can pace itself, the same way `CohereAPIBackend` does for the
     5-requests/minute free ASR tier, because spacing requests out is what
     actually defeats a per-minute limit.

Both are off unless configured, so the DeepSeek path is untouched.
Nothing here sleeps for real: `time.sleep` is captured and asserted on.
"""
from __future__ import annotations

import httpx
import pytest

from app.evaluate import judge


class _Resp:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data or {
            "model": "stealth/ox-alpha",
            "choices": [{"message": {"content": '{"ok": 1}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._data


class _Seq:
    """Replays a fixed sequence of responses and counts the calls."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, _url, json=None):        # noqa: A002 - httpx's parameter name
        i = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[i]


@pytest.fixture
def slept(monkeypatch):
    """Capture every sleep the client asks for, without taking any."""
    waits: list[float] = []
    monkeypatch.setattr(judge.time, "sleep", waits.append)
    return waits


@pytest.fixture(autouse=True)
def _reset_pacing_clock():
    """The pacing clock is class-level on purpose; reset it between tests."""
    judge.DeepSeekClient._last_request = 0.0
    yield
    judge.DeepSeekClient._last_request = 0.0


def _client(monkeypatch, *responses, env=None):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    for key in ("DEEPSEEK_MODEL", "DEEPSEEK_THINKING", "DEEPSEEK_BASE_URL",
                "DEEPSEEK_REASONING_EFFORT", "JUDGE_MIN_REQUEST_INTERVAL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    client = judge.DeepSeekClient()
    client._client = _Seq(*responses)
    return client


# ── 429 backoff ─────────────────────────────────────────────────────────────

def test_a_429_then_success_is_retried_and_returns(monkeypatch, slept):
    client = _client(monkeypatch, _Resp(429), _Resp())
    payload, _ = client.complete_json("hello")
    assert payload == {"ok": 1}
    assert client._client.calls == 2


def test_the_429_wait_is_far_longer_than_the_transport_backoff(monkeypatch, slept):
    """1/2/4 seconds is what let all three attempts land in one closed window."""
    client = _client(monkeypatch, _Resp(429), _Resp())
    client.complete_json("hello")
    assert slept and slept[0] >= judge.RATE_LIMIT_BACKOFF


def test_retry_after_is_honoured_when_the_server_sends_one(monkeypatch, slept):
    client = _client(monkeypatch, _Resp(429, headers={"Retry-After": "37"}), _Resp())
    client.complete_json("hello")
    assert slept[0] == pytest.approx(37.0)


def test_a_wild_retry_after_is_capped_so_one_call_cannot_hang_the_node(monkeypatch, slept):
    """n8n gives the judge node 300 s; an honest 600-second Retry-After would
    otherwise turn one throttled call into a node timeout."""
    client = _client(monkeypatch, _Resp(429, headers={"Retry-After": "600"}), _Resp())
    client.complete_json("hello")
    assert slept[0] == judge.MAX_RATE_LIMIT_WAIT


def test_an_unparseable_retry_after_falls_back_to_our_own_backoff(monkeypatch, slept):
    client = _client(monkeypatch, _Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                     _Resp())
    client.complete_json("hello")
    assert slept[0] >= judge.RATE_LIMIT_BACKOFF


def test_a_sustained_429_still_raises_a_judge_error(monkeypatch, slept):
    """Exhausted retries must surface as JudgeError — /evaluate turns that into
    a 422 the workflow can retry, not a 500."""
    client = _client(monkeypatch, _Resp(429))
    with pytest.raises(judge.JudgeError, match="429|failed after"):
        client.complete_json("hello")


def test_the_last_attempt_does_not_sleep_before_giving_up(monkeypatch, slept):
    client = _client(monkeypatch, _Resp(429))
    with pytest.raises(judge.JudgeError):
        client.complete_json("hello", retries=2)
    # One wait between the two attempts, not one after the last.
    assert len([w for w in slept if w >= judge.RATE_LIMIT_BACKOFF]) == 1


# ── self-pacing ─────────────────────────────────────────────────────────────

def test_pacing_is_off_by_default_so_deepseek_is_unchanged(monkeypatch, slept):
    client = _client(monkeypatch, _Resp(), _Resp())
    client.complete_json("a")
    client.complete_json("b")
    assert slept == []


def test_pacing_spaces_consecutive_requests_when_configured(monkeypatch, slept):
    client = _client(monkeypatch, _Resp(), _Resp(),
                     env={"JUDGE_MIN_REQUEST_INTERVAL": "8"})
    client.complete_json("a")
    client.complete_json("b")
    # The first request goes out immediately; the second waits out the interval.
    assert len(slept) == 1
    assert 0 < slept[0] <= 8


def test_pacing_is_shared_across_instances(monkeypatch, slept):
    """The regression that let a 429 through 18 minutes after pacing shipped.

    `/evaluate` builds a fresh DeepSeekClient per request, so a per-instance
    clock paces one evaluation against itself and never against the concurrent
    evaluations that cause the burst. Two separate clients must pace against
    each other or the setting is decorative.
    """
    a = _client(monkeypatch, _Resp(), env={"JUDGE_MIN_REQUEST_INTERVAL": "9"})
    b = _client(monkeypatch, _Resp(), env={"JUDGE_MIN_REQUEST_INTERVAL": "9"})
    a.complete_json("first")
    b.complete_json("second")
    assert len(slept) == 1, "the second client ignored the first client's request"
    assert 0 < slept[0] <= 9


def test_pacing_and_a_429_compose(monkeypatch, slept):
    client = _client(monkeypatch, _Resp(429), _Resp(),
                     env={"JUDGE_MIN_REQUEST_INTERVAL": "5"})
    payload, _ = client.complete_json("hello")
    assert payload == {"ok": 1}
    assert any(w >= judge.RATE_LIMIT_BACKOFF for w in slept)
