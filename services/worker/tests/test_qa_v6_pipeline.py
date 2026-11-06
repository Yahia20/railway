"""The v6 scorecard's way into production: the chat built from the database, the DeepSeek calls, the endpoint,
migration 034 and workflow 13 agreeing with each other.

The database, DeepSeek and Bitrix are stand-ins written here; the scorecard, the transcript builder, the engine and
the endpoint are the real code. No credentials.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.evaluate import judge
from app.main import app
from app.qa.engine import NotGradeable
from app.qa.v6 import engine, transcript

REPO = Path(__file__).resolve().parents[3]
IID = "00000000-0000-0000-0000-000000000601"
AID = "00000000-0000-0000-0000-0000000000a1"
AGENT, BOT = "501", "20114"
NOW = datetime(2026, 11, 3, 12, 0)


def utc(day: int, hh: int, mm: int) -> datetime:
    return datetime(2026, 10, day, hh - 3, mm, tzinfo=timezone.utc)      # Riyadh hh:mm


def row(sender, at, body, ext=None, ctype="text", media=None, file_name=None):
    return {"sender": sender, "content_type": ctype, "body": body, "sent_at": at, "ext": ext,
            "media_type": media, "file_name": file_name}


MESSAGES = [
    row("customer", utc(20, 11, 0), "السلام عليكم ابي رحلة تركيا لشخصين 5 يناير"),
    row("agent", utc(20, 11, 1), "هلا! فريقنا راح يتواصل معك", ext=BOT),
    row("agent", utc(20, 11, 10), "السلام عليكم معك سارة من ترافل جيت", ext=AGENT),
    row("agent", utc(20, 11, 12), "السعر 3000 ريال للشخص", ext=AGENT),
    row("customer", utc(20, 11, 15), "[Attachment: Ali Hassan AB12345.pdf]", ctype="document", media="document",
        file_name="Ali Hassan AB12345.pdf"),
    row("customer", utc(20, 11, 16), "[Unsupported message type: reaction]", ctype="reaction"),
    row("customer", utc(20, 11, 20), "تمام شكرا يا Ali"),
]
HEAD = {"id": IID, "started_at": utc(20, 11, 0), "content_purged_at": None, "stage_semantic": "F",
        "bitrix_deal_id": "100", "display_name": "Ali Hassan", "contact_first": "", "contact_last": ""}
AGENTS = [{"bitrix_user_id": AGENT, "agent_id": AID, "full_name": "Agent A", "is_bot": False, "qa_excluded": False},
          {"bitrix_user_id": BOT, "agent_id": None, "full_name": "Travelgate AI", "is_bot": True, "qa_excluded": False}]

ANSWER = {"chat_type": {"type": "sales", "type_quote": "ابي رحلة تركيا", "type_line": 1, "other_kind": None},
          "scope": {"scope": "in"},
          "booking": {"destination_quote": "تركيا", "destination_line": 1, "date_quote": "5 يناير", "date_line": 1,
                      "travellers_quote": "لشخصين", "travellers_line": 1},
          "probes": [], "offer": {"offer_quote": "السعر 3000 ريال", "offer_line": 4, "attachment_unclear": False},
          "followup": [], "closure": {}, "q18": {}, "promises": [], "requests": [], "objections": [],
          "q13": {}, "q15": {}, "q16": {}, "q34": {}, "q26": {}, "q36": {}}
ALERTS = {"loss_reason": {"reason": "No Response"}, "alerts": []}
USAGE = {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_cache_hit_tokens": 900, "model": "deepseek-flash"}


class FakeDB:
    def __init__(self, head=HEAD, agents=AGENTS, messages=MESSAGES, greetings=({"body": "معك سارة"},)):
        # copies, so a test that changes a deal or an agent cannot leak into the next test
        self.head, self.agents, self.messages = copy.deepcopy(head), copy.deepcopy(agents), copy.deepcopy(messages)
        self.greetings = list(greetings)

    def one(self, sql, params=None):
        assert "FROM interactions i" in sql
        return copy.deepcopy(self.head)

    def rows(self, sql, params=None):
        if "FROM agents WHERE bitrix_user_id IS NOT NULL" in sql:
            return copy.deepcopy(self.agents)
        if "FROM chat_messages m WHERE m.interaction_id" in sql:
            return copy.deepcopy(self.messages)
        if "sender_external_id = %s AND body" in sql:
            return list(self.greetings)
        raise AssertionError(sql)


class FakeClient:
    """Answers like DeepSeek: a reading for the scorecard prompt, an alerts answer for the alerts prompt."""

    def __init__(self, thinking="disabled", votes=None, fail_votes=0, fail_alerts=False):
        self.model, self.thinking = "deepseek-v4-flash", thinking
        self.votes = list(votes or [])
        self.fail_votes, self.fail_alerts = fail_votes, fail_alerts
        self.sent = []

    def complete_json(self, text, temperature=0.0, max_tokens=8000, retries=3, system=None, follow_up=None, json_mode=True):
        self.sent.append({"system": system, "follow_up": follow_up, "json_mode": json_mode})
        if system == engine.ALERTS_PROMPT:
            if self.fail_alerts:
                raise judge.JudgeError("no content")
            return copy.deepcopy(ALERTS), dict(USAGE)
        if follow_up:
            return copy.deepcopy(ANSWER), dict(USAGE)
        if self.fail_votes:
            self.fail_votes -= 1
            raise judge.JudgeError("timeout")
        return copy.deepcopy(self.votes.pop(0) if self.votes else ANSWER), dict(USAGE)


class FakeBitrix:
    def __init__(self, fail=False):
        self.fail = fail

    def call(self, method, **params):
        if self.fail:
            raise RuntimeError("bitrix down")
        assert method == "crm.activity.list" and params["filter"]["OWNER_ID"] == "100"
        return {"result": [
            {"PROVIDER_ID": "CRM_TASKS_TASK", "TYPE_ID": "6", "CREATED": "2026-10-20T11:05:00+03:00", "RESPONSIBLE_ID": "501"},
            {"PROVIDER_ID": "VOXIMPLANT_CALL", "TYPE_ID": "2", "CREATED": "2026-10-21T12:00:00+03:00", "RESPONSIBLE_ID": "501"}]}


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(transcript, "db", db)
    return db


def grade(client=None, thinking=None, bitrix=None):
    client = client or FakeClient()
    return engine.evaluate(IID, plain_client=client, thinking_client=thinking or FakeClient("enabled"),
                           bitrix=bitrix, now=NOW)


# ---- the chat as the scorecard reads it
def test_transcript_masks_the_customer_and_never_sends_a_file_name(fake_db):
    chat = transcript.build(IID)
    text = json.dumps(chat["messages"], ensure_ascii=False)
    assert "Ali" not in text and "AB12345" not in text
    assert [m["text"] for m in chat["messages"][4:6]] == ["[مرفق] ملف pdf", "[رد فعل بإيموجي]"]
    assert chat["messages"][1]["role"] == "bot" and chat["messages"][5]["role"] == "system"
    assert "[اسم الموظف]" in chat["messages"][2]["text"] and "سارة" not in text
    assert chat["employee"] == "Agent A" and chat["attribution"] == "single" and chat["outcome"] == "lost"
    assert chat["messages"][0]["at"] == "2026-10-20T11:00"


@pytest.mark.parametrize("change, reason", [
    (lambda db: db.head.update(stage_semantic="P"), "still open"),
    (lambda db: db.head.update(stage_semantic=None), "no deal"),
    (lambda db: db.head.update(content_purged_at=NOW), "purged"),
    (lambda db: setattr(db, "messages", [m for m in MESSAGES if m["sender"] != "customer"]), "customer never wrote"),
    (lambda db: setattr(db, "messages", [m for m in MESSAGES if m["ext"] != AGENT]), "no human agent"),
    (lambda db: db.agents[0].update(qa_excluded=True), "excluded"),
])
def test_chats_that_must_not_be_graded(fake_db, change, reason):
    change(fake_db)
    with pytest.raises(NotGradeable, match=reason):
        transcript.build(IID)


def test_an_agent_the_roster_does_not_name_shows_the_name_they_sign_with_and_their_id(fake_db):
    fake_db.agents = [a for a in AGENTS if a["bitrix_user_id"] != AGENT]
    assert transcript.build(IID)["employee"] == "سارة (مستخدم 501)"


def test_bitrix_gives_the_assignment_time_and_call_records(fake_db):
    chat = transcript.build(IID, FakeBitrix())
    assert chat["assigned_at"] == "2026-10-20T11:05" and chat["calls"] == ["2026-10-21T12:00"] and chat["bitrix_read"]
    down = transcript.build(IID, FakeBitrix(fail=True))
    assert down["assigned_at"] is None and down["calls"] == [] and down["bitrix_read"] is False


# ---- the model calls
def test_a_grade_carries_every_paid_call_and_everything_the_page_needs(fake_db):
    out = grade()
    assert out["score"] is not None and out["votes"] == 3 and out["outcome"] == "lost"
    assert [c["purpose"] for c in out["calls"]].count(engine.PURPOSE) == 3
    assert [c["purpose"] for c in out["calls"]].count(engine.PURPOSE_ALERTS) == 1
    assert len({c["input_hash"] for c in out["calls"]}) == len(out["calls"]), "UNIQUE in model_calls would drop a paid call"
    assert out["engine_version"] == engine.ENGINE_VERSION and out["prompt_version"] == engine.PROMPT_VERSION
    assert out["result"]["checks"]["offer"]["status"] == "yes"
    assert out["transcript"]["messages"] and out["answers"]["alerts"] == ALERTS


def test_alerts_run_with_thinking_and_without_json_mode_as_approved(fake_db):
    thinking = FakeClient("enabled")
    grade(thinking=thinking)
    assert thinking.sent and all(s["system"] == engine.ALERTS_PROMPT and s["json_mode"] is False for s in thinking.sent)


def test_a_misplaced_quote_gets_one_recopy_and_the_better_answer_is_kept(fake_db):
    wrong = copy.deepcopy(ANSWER)
    wrong["offer"]["offer_line"] = 1
    out = grade(FakeClient(votes=[wrong, ANSWER, ANSWER]))
    purposes = [c["purpose"] for c in out["calls"]]
    assert purposes.count(engine.PURPOSE_RECOPY) == 1
    assert all(v["offer"]["offer_line"] == 4 for v in out["answers"]["votes"])


def test_alerts_fall_back_to_thinking_off_once(fake_db):
    out = grade(thinking=FakeClient("enabled", fail_alerts=True))
    alert = [c for c in out["calls"] if c["purpose"] == engine.PURPOSE_ALERTS]
    assert len(alert) == 1 and alert[0]["input_hash"].endswith(":alerts:disabled")


def test_two_readings_are_enough_and_one_is_not(fake_db):
    assert grade(FakeClient(fail_votes=1))["votes"] == 2
    with pytest.raises(judge.JudgeError) as err:
        grade(FakeClient(fail_votes=2))
    assert [c["purpose"] for c in err.value.calls].count(engine.PURPOSE) == 1, "the reading that came back was paid for"


def test_the_prompt_files_are_the_approved_ones():
    trial = REPO / "local-reports" / "v52-fix-2026-11-01"
    if not trial.exists():
        pytest.skip("the trial folder is git-ignored and lives on one machine")
    assert (trial / "prompt_v6.txt").read_bytes() == (REPO / "services/worker/app/qa/v6/prompt.txt").read_bytes()
    assert (trial / "prompt_alerts_v3.txt").read_bytes() == (REPO / "services/worker/app/qa/v6/prompt_alerts.txt").read_bytes()


# ---- the judge client changes v6 needs, without moving pass 1/2 or v4
def test_follow_up_turns_and_plain_mode_reach_the_request(monkeypatch):
    sent = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '```json\n{"a": 1}\n``` done'}}], "usage": {}}

    c = judge.DeepSeekClient(api_key="k")
    monkeypatch.setattr(c._client, "post", lambda path, json: sent.append(json) or Resp())
    answer, _ = c.complete_json("chat", system="rules", follow_up=[{"role": "assistant", "content": "{}"},
                                                                 {"role": "user", "content": "again"}], json_mode=False)
    assert answer == {"a": 1}
    assert [m["role"] for m in sent[0]["messages"]] == ["system", "user", "assistant", "user"]
    assert "response_format" not in sent[0]


def test_json_mode_stays_strict():
    with pytest.raises(judge.JudgeError):
        judge._extract_json('{"a": 1} done')
    with pytest.raises(judge.JudgeError):
        judge._extract_first_json("no object here")


# ---- the endpoint
@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setattr(settings, "worker_api_key", "test-key", raising=False)
    return TestClient(app)


def test_endpoint_not_gradeable_is_an_answer(api, monkeypatch):
    def nope(_id, **_):
        raise NotGradeable("the deal is still open")

    monkeypatch.setattr(engine, "evaluate", nope)
    r = api.post("/qa/v6/evaluate", headers={"X-API-Key": "test-key"}, json={"interaction_id": IID})
    assert r.status_code == 200 and r.json()["gradeable"] is False


def test_endpoint_failure_keeps_the_paid_calls_and_has_no_gradeable(api, monkeypatch):
    def boom(_id, **_):
        err = judge.JudgeError("1 of 3 readings")
        err.calls = [{"purpose": engine.PURPOSE}]
        raise err

    monkeypatch.setattr(engine, "evaluate", boom)
    r = api.post("/qa/v6/evaluate", headers={"X-API-Key": "test-key"}, json={"interaction_id": IID})
    body = r.json()
    assert r.status_code == 200 and "gradeable" not in body and body["calls"] == [{"purpose": engine.PURPOSE}]


# ---- migration 034 and workflow 13 agree with the code
MIGRATION = (REPO / "db" / "migrations" / "034_qa_scorecard_v6.sql").read_text(encoding="utf-8")
WF = json.loads((REPO / "n8n" / "workflows" / "13-chat-qa-v6.json").read_text(encoding="utf-8"))
SQL = {n["name"]: n["parameters"].get("query", "") for n in WF["nodes"]}


def test_workflow_file_is_what_the_generator_writes():
    script = REPO / "scripts" / "build_wf13_qa_v6.py"
    before = (REPO / "n8n" / "workflows" / "13-chat-qa-v6.json").read_text(encoding="utf-8")
    subprocess.run([sys.executable, str(script)], check=True, capture_output=True, cwd=REPO)
    assert (REPO / "n8n" / "workflows" / "13-chat-qa-v6.json").read_text(encoding="utf-8") == before


def test_every_field_the_store_reads_is_in_the_payload(fake_db):
    payload = {"gradeable": True, **grade(), "reason": "", "error": {}}
    read = set(re.findall(r"s\.j->>?'(\w+)'", SQL["Store result"]))
    assert read and read <= set(payload), read - set(payload)


def test_every_column_the_store_writes_exists_and_every_status_is_allowed():
    table = MIGRATION[MIGRATION.index("CREATE TABLE IF NOT EXISTS qa_grades_v6"):MIGRATION.index("CREATE INDEX")]
    written = set(re.findall(r"^\s+(\w+)\s+=", SQL["Store result"].split("UPDATE qa_grades_v6 q SET")[1], re.M))
    assert written and all(re.search(rf"^\s+{c}\s", table, re.M) for c in written), written
    allowed = set(re.search(r"status IN \(([^)]*)\)", table).group(1).replace("'", "").replace(" ", "").split(","))
    assert {"claimed", "scored", "not_gradeable", "failed"} == allowed


def test_workflow_uses_v6_objects_only_and_the_route_exists():
    joined = re.sub(r"--[^\n]*", "", "\n".join(SQL.values()))      # what runs, not what the comments mention
    assert "v_qa6_grade_due" in joined and "v_qa6_gate" in joined and "qa_grades_v6" in joined
    assert not re.search(r"\bqa_grades\b(?!_v6)", joined) and "v_qa_gate" not in joined
    url = next(n["parameters"]["url"] for n in WF["nodes"] if n["name"] == "Grade through worker")
    assert url.endswith("/qa/v6/evaluate") and "/qa/v6/evaluate" in {r.path for r in app.routes}
    for view in ("v_qa6_grade_due", "v_qa6_gate"):
        assert f"CREATE OR REPLACE VIEW {view}" in MIGRATION
    assert "('v6_mode',  'off')" in MIGRATION, "v6 must ship switched off"


def test_queue_grades_only_ended_deals_and_waits_a_day():
    view = MIGRATION[MIGRATION.index("CREATE OR REPLACE VIEW v_qa6_grade_due"):]
    assert "d.stage_semantic IN ('S', 'F')" in view and "interval '24 hours'" in view and "qa_excluded" in view
