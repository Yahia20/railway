"""The repeat runner and the D6 Module-4 fixture pair.

Two instruments, both of which report a verdict a decision gets made on:

- `--repeat N` runs each case N times and reports the majority. A single green
  run of a prompt fixture is inside the model's own spread — the day-13 A/A
  study moved 11 of 68 performance bands with no prompt change at all — so a
  suite that runs once tells you what the model said, not what the prompt does.
- `--m4-fixtures` is the PR1A rollout gate. Module 4 is 20% of the grade and
  cannot be observed inside one call, so it is scored from a follow-up-history
  block built by SQL. On day 13 all five calls that HAD a timeline scored
  Module 4 null, because the block that reached the prompt said only
  "phone_call by unknown". These fixtures check the rebuilt block is readable —
  and, more importantly, that an INBOUND callback from the customer is NOT
  credited to the agent.

Nothing here calls DeepSeek. The judging is stubbed; what is under test is the
majority arithmetic, the report, and the follow-up blocks the fixtures build.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "compare_day_repeat", ROOT / "scripts" / "compare_day.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cd = _load()


# ── majority arithmetic ─────────────────────────────────────────────────────

@pytest.mark.parametrize("outcomes,expected", [
    (["null", "null", "null"], "null"),
    (["scored", "scored", "scored"], "scored"),
    (["scored", "null", "null"], "null"),
    (["scored", "scored", "null"], "scored"),
    (["scored", "null"], "tie"),
    (["scored", "null", "ERROR"], "tie"),
    (["scored", "ERROR", "ERROR"], "scored"),
    (["ERROR", "ERROR"], "ERROR"),
])
def test_the_majority_is_the_majority(outcomes, expected):
    assert cd._majority(outcomes) == expected


def test_an_error_is_not_a_vote():
    """A rate limit is not the judge's opinion.

    Counting errors as a third outcome would let two 429s turn a unanimous
    verdict into a tie, and a tie reads as instability in the prompt.
    """
    assert cd._majority(["null", "ERROR", "ERROR"]) == "null"
    assert cd._majority(["null", "null", "ERROR"]) == "null"


# ── the report ──────────────────────────────────────────────────────────────

def _stub_outcomes(monkeypatch, scripted: dict[str, list[str]]):
    """Make judge_case_once replay a scripted outcome per case, in order."""
    counters: dict[str, int] = {}

    def fake(case, _client, _criterion):
        i = counters.get(case["id"], 0)
        counters[case["id"]] = i + 1
        outcome = scripted[case["id"]][i]
        return {"outcome": outcome, "value": 25 if outcome == "scored" else None,
                "flag": outcome == "scored", "final_score": 70.0,
                "contract_status": "ok", "quotes": [], "usage": {},
                "error": None}

    monkeypatch.setattr(cd, "judge_case_once", fake)
    monkeypatch.setattr(cd.judge, "DeepSeekClient", lambda *a, **k: object())


def test_a_two_one_split_is_reported_as_a_split_not_as_a_pass(tmp_path, monkeypatch):
    """The number that gets read off this is the verdict; the number that keeps
    it honest is how close the vote was. Both are printed."""
    cases = [
        {"id": "stable", "pattern": "p", "conversation": "c", "expect_outcome": "null"},
        {"id": "wobbly", "pattern": "p", "conversation": "c", "expect_outcome": "null"},
        {"id": "broken", "pattern": "p", "conversation": "c", "expect_outcome": "scored"},
    ]
    _stub_outcomes(monkeypatch, {
        "stable": ["null", "null", "null"],
        "wobbly": ["scored", "null", "null"],
        "broken": ["null", "null", "null"],
    })

    result = cd.run_repeated(cases, tmp_path, "suite", "m3", repeat=3, workers=1)

    assert result["passed"] == 2 and result["judged"] == 3
    by_id = {o["id"]: o for o in result["outcomes"]}
    assert by_id["stable"]["all_correct"] is True
    assert by_id["wobbly"]["majority_correct"] is True
    assert by_id["wobbly"]["all_correct"] is False        # 2-1, and it says so
    assert by_id["broken"]["majority_correct"] is False

    report = (tmp_path / "suite.md").read_text(encoding="utf-8")
    assert "2/3 correct by majority" in report
    assert "pass (majority only)" in report               # the 2-1
    assert "**FAIL**" in report
    # every individual run is in the file, not only the verdict
    assert report.count("- run 1:") == 3

    written = json.loads((tmp_path / "suite.json").read_text(encoding="utf-8"))
    assert [o["run_outcomes"] for o in written if o["id"] == "wobbly"] == \
        [["scored", "null", "null"]]


def test_a_case_with_no_expectation_is_reported_not_scored(tmp_path, monkeypatch):
    """`--repeat-ids` without `--expect` should still run and still report.

    Inventing a verdict for a case nobody has judged is worse than having none:
    it would count toward a pass rate that means nothing.
    """
    cases = [{"id": "unjudged", "pattern": "p", "conversation": "c",
              "expect_outcome": None}]
    _stub_outcomes(monkeypatch, {"unjudged": ["scored", "null"]})
    result = cd.run_repeated(cases, tmp_path, "suite", "m3", repeat=2, workers=1)
    assert result["judged"] == 0 and result["total"] == 1
    assert result["outcomes"][0]["majority"] == "tie"


def test_repeat_one_still_works(tmp_path, monkeypatch):
    cases = [{"id": "a", "pattern": "p", "conversation": "c", "expect_outcome": "null"}]
    _stub_outcomes(monkeypatch, {"a": ["null"]})
    result = cd.run_repeated(cases, tmp_path, "suite", "m3", repeat=1, workers=1)
    assert result["passed"] == 1
    assert result["outcomes"][0]["unanimous"] is True


# ── the M3 fixture cases feed the runner ────────────────────────────────────

def test_every_m3_fixture_reaches_the_runner_with_its_expectation():
    cases = cd.m3_fixture_cases()
    assert len(cases) == 14
    assert all(c["expect_outcome"] in ("scored", "null") for c in cases)
    assert {"e779317b", "174898da"} <= {c["stands_for"] for c in cases}


# ── D6: the Module-4 pair ───────────────────────────────────────────────────

def test_the_two_m4_cases_send_the_same_conversation():
    """The only variable is the history block.

    If the conversations differed too, a Module-4 difference could be caused by
    either, and the fixture would prove nothing about the block that day 13
    showed was unreadable.
    """
    outbound, inbound = cd.m4_fixture_cases()
    assert outbound["conversation"] == inbound["conversation"]
    assert outbound["followup_history"] != inbound["followup_history"]
    assert outbound["expect_outcome"] == "scored"
    assert inbound["expect_outcome"] == "null"

    # Everything else about the two cases is identical, or the pair is not a
    # pair: the only variable allowed is the direction and content of the block.
    differing = {k for k in set(outbound) | set(inbound)
                 if outbound.get(k) != inbound.get(k)}
    assert differing == {"id", "stands_for", "pattern", "followup_history",
                         "expect_outcome"}


def test_the_m4_pair_declares_the_stage_its_conversation_reaches():
    """Rule 1 of the round-4 fixture policy, applied to the D6 pair.

    Both halves send the same call — the agent quotes 14,000 and the customer
    goes away to think about it — so both reach `negotiation`, and a Module-4
    null on the inbound half has to be attributable to the history block rather
    than to a stage disagreement.
    """
    for case in cd.m4_fixture_cases():
        assert case["expected_stage"] == "negotiation"
        assert case["noisy"] is False


def test_the_m4_blocks_are_in_the_current_production_format():
    """Field for field the block `02_build_follow_up_history.sql` emits.

    A hand-typed lookalike would test the prompt against input production does
    not send, which is precisely the mistake `--history-format current` exists
    to prevent.
    """
    outbound, inbound = cd.m4_fixture_cases()

    for case in (outbound, inbound):
        block = case["followup_history"]
        assert block.startswith("Subsequent contact with this customer:")
        assert "h after this conversation, handled by " in block

    assert "direction outbound" in outbound["followup_history"]
    assert "whatsapp" in outbound["followup_history"]
    # criterion 3 is 30 of the module's 100 points and is unanswerable without
    # the message text, which is why the SQL now carries it
    assert 'قررتم شي؟"' in outbound["followup_history"]

    # The label the SQL emits verbatim for a queue recording, which is what the
    # judge has to read to know this was not the agent's doing.
    assert ("INBOUND: the customer called in, this is not an agent follow-up"
            in inbound["followup_history"])
    assert ("no individual agent recorded (queue recording)"
            in inbound["followup_history"])
    assert "direction" not in inbound["followup_history"].replace(
        "INBOUND: the customer called in, this is not an agent follow-up", "")


def test_the_m4_call_clears_the_speech_gate():
    """A fixture under the gate never reaches the judge and proves nothing."""
    from app.main import MIN_SCOREABLE_CHARS, spoken_content
    for case in cd.m4_fixture_cases():
        assert len(spoken_content(case["conversation"])) >= MIN_SCOREABLE_CHARS


def test_the_m4_call_contains_a_promise_to_follow_up():
    """Module 4 grades whether the agent came back. A call in which nothing was
    promised and nothing was left open makes 'no follow-up owed' a defensible
    null on BOTH cases, and the pair stops discriminating."""
    conversation = cd.m4_fixture_cases()[0]["conversation"]
    assert "بكرة" in conversation           # "tomorrow" — the promise
    assert "أرد عليك" in conversation       # the customer defers a decision


def test_module4_outcome_reads_the_module_score():
    assert cd._m4_outcome(
        {"modules": {"module4_followup": {"score": 85}}}) == ("scored", 85.0, None)
    assert cd._m4_outcome(
        {"modules": {"module4_followup": {"score": None}}}) == ("null", None, None)
    assert cd._m4_outcome({}) == ("null", None, None)


# ── selecting real cases by prefix ──────────────────────────────────────────

def _item(iid, conversation="[00:00] AGENT: مرحبا"):
    return {"interaction_id": iid, "conversation": conversation,
            "kind": "q", "asr_confidence": 1, "duration_seconds": 60,
            "diarization": "none", "channels": 1}


def test_real_cases_can_be_named_by_their_eight_character_prefix():
    """Every report names calls by the prefix. Requiring the full uuid is how a
    case gets silently dropped from a suite that then reports a clean pass."""
    items = [_item("e779317b-1111-2222-3333-444444444444"),
             _item("174898da-1111-2222-3333-444444444444")]
    cases = cd.real_cases_from_input(items, ["e779317b"], "stored",
                                     {"e779317b": "scored"})
    assert len(cases) == 1
    assert cases[0]["id"] == "e779317b"
    assert cases[0]["stands_for"].startswith("e779317b-")
    assert cases[0]["expect_outcome"] == "scored"


def test_an_unknown_id_is_a_failure_not_a_shrug():
    """Silently skipping it turns 12 cases into 11 and a green report."""
    with pytest.raises(SystemExit):
        cd.real_cases_from_input([_item("aaaaaaaa-1111-2222-3333-444444444444")],
                                 ["ffffffff"], "stored", None)
