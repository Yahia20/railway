"""The QA scorecard (app/qa), its endpoint, and the dashboard that reads it.

What each test guards is a way the move from the trial run into the worker
could change a number without anybody seeing it:

  * the request the model receives — the approved system prompt plus the
    numbered chat and the code-chosen line lists, nothing else;
  * three runs and a per-question majority, each run its own model_calls row
    (one shared input_hash would let UNIQUE swallow two of the three bills);
  * the same thread and the same answers giving the same score as the trial;
  * migration 030's queue filter agreeing with the engine's;
  * the dashboard shell carrying no data and every data route needing the key.

The parity run against the 297 real trial chats (40/40 identical threads,
scores and items) was done on 2026-10-07 against the live database and is not
repeatable here: that data is customer data and never enters this repo.
Runs with no credentials and no database.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.evaluate import judge
from app.main import app
from app.qa import engine, render
from app.qa.score_v05 import CATEGORIES, score_thread

KEY = "test-key-for-qa"
REPO = Path(__file__).resolve().parents[3]

# A made-up chat, Gulf dialect, shaped like the trial's sample.json rows.
THREAD = {
    "id": "00000000-0000-0000-0000-000000000001", "agent": "Test Agent",
    "agent_id": "00000000-0000-0000-0000-0000000000aa",
    "started_at": "2026-10-01T10:00:00+03:00", "ended_at": "2026-10-01T10:30:00+03:00",
    "customer_name": None, "later_agent_msgs": [],
    "messages": [
        {"role": "customer", "type": "text", "body": "السلام عليكم ابي رحلة لروسيا شخصين 12 اكتوبر",
         "at": "2026-10-01T10:00:00+03:00", "agent_ext": None},
        {"role": "agent", "type": "text", "body": "وعليكم السلام، معك سارة من ترافل جيت، أبشر",
         "at": "2026-10-01T10:03:00+03:00", "agent_ext": "501"},
        {"role": "agent", "type": "text", "body": "حضرتك تبي روسيا لشخصين صحيح؟",
         "at": "2026-10-01T10:04:00+03:00", "agent_ext": "501"},
        {"role": "customer", "type": "text", "body": "ايوه كم السعر؟",
         "at": "2026-10-01T10:05:00+03:00", "agent_ext": None},
        {"role": "agent", "type": "text", "body": "السعر 9000 ريال للشخصين، هبعتلك العرض خلال ساعة",
         "at": "2026-10-01T10:07:00+03:00", "agent_ext": "501"},
        {"role": "agent", "type": "text", "body": "تحت أمرك في أي وقت",
         "at": "2026-10-01T10:08:00+03:00", "agent_ext": "501"},
    ],
}
ANSWER = {
    "booking": {"destination_quote": "روسيا", "date_quote": "12 اكتوبر", "travellers_quote": "شخصين"},
    "q2": {"answer_quote": "حضرتك تبي روسيا لشخصين صحيح؟"}, "q3": {"answer_quote": None},
    "q13": {"violation_quote": None}, "q15": {"violation_quote": None}, "q16": {"violation_quote": None},
    "q18": {"answer_quote": None}, "q24": {}, "q25": {"4": "السعر 9000 ريال للشخصين"},
    "q26": {"quote_a": None, "quote_b": None}, "q30": {"option_quotes": []}, "q32": {},
    "q34": {"violation_quote": None}, "q35": {"violation_quote": None},
    "q36": {"quote_a": None, "quote_b": None}, "q37": {"violation_quote": None},
    "q38": {"violation_quote": None},
}


class StubClient:
    model = "deepseek-v4-flash"

    def __init__(self, answers=None):
        self.answers = list(answers or [ANSWER] * 3)
        self.calls = []

    def complete_json(self, prompt, temperature=0.0, max_tokens=8000, retries=3, system=None):
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens,
                           "temperature": temperature})
        usage = {"prompt_tokens": 2742, "prompt_cache_hit_tokens": 1444,
                 "prompt_cache_miss_tokens": 1298, "completion_tokens": 320, "model": "deepseek-flash"}
        return self.answers.pop(0), usage


def test_the_request_is_the_approved_prompt_plus_the_numbered_chat():
    text = engine.request_text(THREAD)
    assert text.startswith("المحادثة:\n\n[1] ")
    assert "CUSTOMER QUESTIONS: [4]" in text, "the line list is chosen by code, not the model"
    assert engine.SYSTEM_PROMPT.startswith("You are a quality checker for a travel company")
    assert "UNAVAILABLE LINES" in engine.SYSTEM_PROMPT


def test_three_runs_majority_and_three_billable_rows():
    client = StubClient()
    r = engine.evaluate(THREAD, client)
    assert len(client.calls) == 3
    assert all(c["system"] == engine.SYSTEM_PROMPT and c["temperature"] == 0.0 for c in client.calls)
    assert len({c["input_hash"] for c in r["calls"]}) == 3, "UNIQUE would drop two of three bills"
    assert {c["purpose"] for c in r["calls"]} == {"qa_chat"}
    assert all(c["cost_usd"] for c in r["calls"]), "the echoed model name must be priced"
    assert r["score"] == score_thread(THREAD, ANSWER)["score"]
    assert r["critical"] == []
    assert set(r["categories"]) == {str(n) for n, *_ in CATEGORIES}


def test_runs_that_disagree_fall_to_the_majority():
    rude = dict(ANSWER, q16={"violation_quote": "تحت أمرك في أي وقت"})
    one_rude = engine.evaluate(THREAD, StubClient([rude, ANSWER, ANSWER]))
    two_rude = engine.evaluate(THREAD, StubClient([rude, rude, ANSWER]))
    assert one_rude["items"]["16"]["ans"] == "yes"
    assert two_rude["items"]["16"]["ans"] == "no" and 16 in two_rude["critical"]


def test_a_quote_that_is_not_in_the_chat_does_not_count():
    """The model copies; code checks the copy. An invented recap is a 'no'."""
    invented = dict(ANSWER, q2={"answer_quote": "حضرتك تبي باريس لثلاثة؟"})
    assert score_thread(THREAD, invented)["items"][2]["ans"] == "no"


def test_system_prompt_is_optional_and_off_by_default(monkeypatch):
    sent = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

    c = judge.DeepSeekClient(api_key="k")
    monkeypatch.setattr(c._client, "post", lambda path, json: sent.append(json) or Resp())
    c.complete_json("chat")
    c.complete_json("chat", system="rules")
    assert [m["role"] for m in sent[0]["messages"]] == ["user"], "pass 1/2 request unchanged"
    assert [m["role"] for m in sent[1]["messages"]] == ["system", "user"]


def test_migration_queue_uses_the_engines_filter():
    sql = (REPO / "db" / "migrations" / "030_qa_scorecard.sql").read_text(encoding="utf-8")
    assert f"message_count BETWEEN {engine.MIN_MESSAGES} AND {engine.MAX_MESSAGES}" in sql
    assert f"customer_message_count >= {engine.MIN_SIDE_MESSAGES}" in sql
    assert f"agent_message_count >= {engine.MIN_SIDE_MESSAGES}" in sql
    assert "interval '3 days'" in sql, "item 27 needs three days to have passed"


def test_rendered_chat_masks_numbers_and_shows_every_applied_item():
    m = {"items": score_thread(THREAD, ANSWER)["items"], "score": 80.0, "critical": []}
    html = render.chat_html(1, dict(THREAD, messages=THREAD["messages"] + [
        {"role": "customer", "type": "text", "body": "رقمي 0551234567",
         "at": "2026-10-01T10:09:00+03:00", "agent_ext": None}]), m)
    assert "0551234567" not in html and "[رقم]" in html
    assert "التحية الصح" in html
    assert not re.search(r"\d{9,}", html)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    return TestClient(app)


H = {"X-API-Key": KEY}


def test_not_gradeable_is_an_answer_not_an_error(client, monkeypatch):
    def nope(_id, **_):
        raise engine.NotGradeable("outside the sample filter")

    monkeypatch.setattr(engine, "load_thread", nope)
    r = client.post("/qa/evaluate", headers=H, json={"interaction_id": THREAD["id"]})
    assert r.status_code == 200 and r.json()["gradeable"] is False


def test_a_failed_model_call_is_a_502(client, monkeypatch):
    monkeypatch.setattr(engine, "load_thread", lambda _id, **_: THREAD)

    def boom(_t, _c=None):
        raise judge.JudgeError("DeepSeek call failed after 3 attempts")

    monkeypatch.setattr(engine, "evaluate", boom)
    r = client.post("/qa/evaluate", headers=H, json={"interaction_id": THREAD["id"]})
    assert r.status_code == 502


def test_dashboard_shell_is_public_and_empty_data_is_keyed(client, monkeypatch):
    page = client.get("/dashboard")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    # The shell's script names fields; what it must never carry is a value —
    # a conversation id or a phone number baked into the HTML.
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", page.text)
    assert not re.search(r"\d{9,}", page.text)
    for path in ("/dashboard/data", f"/dashboard/agent/{THREAD['agent_id']}/chats",
                 f"/dashboard/chat/{THREAD['id']}", "/dashboard/rules", "/qa/evaluate"):
        res = client.post(path, json={}) if path == "/qa/evaluate" else client.get(path)
        assert res.status_code == 401, path


def test_an_ungraded_chat_is_a_404(client, monkeypatch):
    monkeypatch.setattr(db, "one", lambda sql, params=None: {})
    assert client.get(f"/dashboard/chat/{THREAD['id']}", headers=H).status_code == 404
