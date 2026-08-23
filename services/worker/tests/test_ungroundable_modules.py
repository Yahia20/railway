"""A module nobody could ground is null, not 100.

PR2 iteration 2. Enforcement restores an unsupported deduction to its cap so
the agent keeps points nobody could take away with evidence. Applied to a
module in which EVERY deduction was discarded, that rule published the opposite
of what it means: day 13, call `e5ab9937`, a 34-second call where the customer
asks for English and the agent says "one minute". The judge zeroed six criteria
across two modules and offered ONE quote for all six — a quote it had partly
translated, so the validator could not find it. All six deductions were
discarded, both modules went to full marks, and a call with 131 characters of
speech scored **100, Excellent**.

The distinction this file draws:

- an ISOLATED unsupported finding, in a module where some other deduction still
  stands → restore to cap, module keeps a number (unchanged behaviour);
- EVERY deduction in a module unsupported → the module was not graded at all,
  so it is `null` — out of numerator and denominator alike;
- too little rubric weight left after that → the whole call is ungradeable,
  `contract_status="ungradeable"`, `final_score=None`.

Every case runs against a stub client. Nothing here talks to DeepSeek.
"""
import pytest

from app.evaluate import judge, scoring

from test_evidence_rejection import StubClient, _breakdowns, _payload


# The e5ab9937 shape, shortened: a call that is almost entirely greeting, where
# the agent's only substantive act is asking the caller to hold.
SHORT_CALL = (
    "[00:00] يا هلا مساء الخير. ألو السلام عليكم. عليكم السلام. "
    "أكمل ألو من Travel Gate. English please. أظن. Can you speak English? One minute."
)

# What the judge offered for all six deductions. Genuine speech from the call —
# except that `أظن` has been rendered as "I think", so it is not verbatim and
# the validator is right to refuse it.
TRANSLATED_QUOTE = "English please. I think. Can you speak English? One minute."


def _run(payload, *more, conversation=SHORT_CALL):
    client = StubClient(payload, *more)
    return judge.run_pass2(conversation, "call_transcript", client=client), client


def _e5ab9937_payload():
    """Six deductions across two modules, one unfindable quote for all six."""
    modules = _breakdowns(
        module1_reception={"greeting": 0, "understanding_confirmation": 0,
                           "missing_info_request": 0, "next_step_transition": 0},
        module2_offer={"attitude": 0, "offer_completeness": None,
                       "value_selling": 0, "alternative_offer": None},
        module3_objections=dict.fromkeys(
            scoring.CRITERION_MAX["module3_objections"], None),
        module4_followup=dict.fromkeys(
            scoring.CRITERION_MAX["module4_followup"], None),
        module5_closing=dict.fromkeys(
            scoring.CRITERION_MAX["module5_closing"], None),
    )
    evidence = [
        {"module": module, "criterion": criterion, "quote": TRANSLATED_QUOTE}
        for module, criterion in (
            ("module1_reception", "greeting"),
            ("module1_reception", "understanding_confirmation"),
            ("module1_reception", "missing_info_request"),
            ("module1_reception", "next_step_transition"),
            ("module2_offer", "attitude"),
            ("module2_offer", "value_selling"),
        )
    ]
    return _payload(modules, evidence=evidence, stage_reached="reception")


def test_the_e5ab9937_shape_is_ungradeable_not_a_hundred():
    """The regression this rule exists for, end to end."""
    payload = _e5ab9937_payload()
    result, client = _run(payload, payload)          # unchanged after the re-ask

    assert len(client.prompts) == 2, "the judge must still be asked once first"
    assert len(result.evidence_rejected) == 6

    # Both modules struck out by name, with the criteria that could not be
    # grounded listed — this is what a human audits.
    assert result.ungradeable_modules == [
        {"module": "module1_reception", "reason": "evidence_ungroundable",
         "discarded_criteria": ["greeting", "missing_info_request",
                                "next_step_transition", "understanding_confirmation"]},
        {"module": "module2_offer", "reason": "evidence_ungroundable",
         "discarded_criteria": ["attitude", "value_selling"]},
    ]
    assert result.score.modules["module1_reception"] is None
    assert result.score.modules["module2_offer"] is None

    # The number that used to be 100.
    assert result.score.final_score is None
    assert result.score.performance_level is None
    assert result.score.gradeable is False
    assert result.score.weight_applied == 0.0
    assert result.contract_status == "ungradeable"
    assert result.payload["final_score"] is None
    assert result.payload["contract_status"] == "ungradeable"

    # Not a contract failure: the response never contradicted itself. It simply
    # could not support anything it said.
    assert result.contract_violations == []
    assert any("evidence_ungroundable" in w for w in result.warnings)


def test_the_stored_payload_and_the_notes_cannot_disagree():
    """`e5ab9937`'s stored row said 100 while its own notes said null.

    Whatever a consumer reads — the result object, the payload, or the module
    map inside the payload — it must get the same answer.
    """
    payload = _e5ab9937_payload()
    result, _ = _run(payload, payload)

    assert result.payload["final_score"] is result.score.final_score is None
    assert result.payload["weight_applied"] == result.score.weight_applied == 0.0
    for key in ("module1_reception", "module2_offer"):
        assert result.payload["modules"][key]["score"] is None


def test_an_isolated_unsupported_finding_still_only_restores_to_cap():
    """One unanchored deduction among several supported ones is not a failure.

    The judge over-reached on one criterion; the agent keeps those points and
    the module keeps its number. Nulling here would throw away three findings
    that WERE grounded.
    """
    modules = _breakdowns(module1_reception={
        "greeting": 0,                       # supported below
        "understanding_confirmation": 0,     # unsupported
        "missing_info_request": 25, "next_step_transition": 25})
    result, _ = _run(_payload(modules, evidence=[
        {"module": "module1_reception", "criterion": "greeting",
         "quote": "يا هلا مساء الخير"},
        {"module": "module1_reception", "criterion": "understanding_confirmation",
         "quote": "لم يذكرها أحد قط"},
    ], stage_reached="negotiation"), )

    assert [r["criterion"] for r in result.evidence_rejected] == \
        ["understanding_confirmation"]
    assert result.ungradeable_modules == []
    # greeting 0 stands, understanding_confirmation restored to 25.
    assert result.score.modules["module1_reception"] == 75.0
    assert result.contract_status == "ok"
    assert result.score.gradeable is True


def test_a_module_at_full_marks_is_never_ungroundable():
    """No deduction means nothing to ground. Full marks need no quote."""
    result, _ = _run(_payload(_breakdowns(), stage_reached="negotiation"))
    assert result.ungradeable_modules == []
    assert result.score.final_score == 100.0
    assert result.contract_status == "ok"


def test_one_ungroundable_module_still_leaves_a_gradeable_call():
    """Module 1 is 15% of the rubric. Losing it does not lose the call."""
    result, _ = _run(_payload(_breakdowns(module1_reception={
        "greeting": 0, "understanding_confirmation": 0,
        "missing_info_request": 0, "next_step_transition": 0}),
        evidence=[], stage_reached="negotiation"))

    assert [e["module"] for e in result.ungradeable_modules] == ["module1_reception"]
    assert result.score.modules["module1_reception"] is None
    assert result.score.weight_applied == 0.85
    assert result.score.final_score == 100.0     # the other four are untouched
    assert result.contract_status == "ok"


def test_a_module_rescued_in_the_correction_is_not_ungroundable():
    """The re-ask comes first, and a judge that anchors its finding keeps it."""
    broken = _payload(_breakdowns(module1_reception={
        "greeting": 5, "understanding_confirmation": 25,
        "missing_info_request": 25, "next_step_transition": 25}),
        evidence=[], stage_reached="negotiation")
    fixed = _payload(_breakdowns(module1_reception={
        "greeting": 5, "understanding_confirmation": 25,
        "missing_info_request": 25, "next_step_transition": 25}),
        evidence=[{"module": "module1_reception", "criterion": "greeting",
                   "quote": "يا هلا مساء الخير"}], stage_reached="negotiation")

    result, client = _run(broken, fixed)
    assert len(client.prompts) == 2
    assert result.evidence_rejected == []
    assert result.ungradeable_modules == []
    assert result.score.modules["module1_reception"] == 80.0


def test_ungroundable_modules_is_pure():
    """The helper decides membership from two inputs and touches nothing."""
    before = {"module1_reception": {"greeting", "next_step_transition"},
              "module2_offer": {"value_selling"}}
    rejected = [
        {"module": "module1_reception", "criterion": "greeting"},
        {"module": "module2_offer", "criterion": "value_selling"},
    ]
    # Module 1 keeps a standing deduction; Module 2 has nothing left.
    assert scoring.ungroundable_modules(before, rejected) == ["module2_offer"]

    rejected.append({"module": "module1_reception", "criterion": "next_step_transition"})
    assert scoring.ungroundable_modules(before, rejected) == \
        ["module1_reception", "module2_offer"]


def test_deducted_criteria_ignores_nulls_and_full_marks():
    modules = _breakdowns(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": 10, "alternative_offer": 0})
    assert scoring.deducted_criteria(modules) == {
        "module2_offer": {"value_selling", "alternative_offer"}}


def test_deducted_criteria_must_be_taken_before_enforcement():
    """Enforcement rewrites the breakdown, so order is not a style choice."""
    modules = _breakdowns(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": 10, "alternative_offer": None})
    before = scoring.deducted_criteria(modules)
    scoring.enforce_criterion_evidence({"evidence": []}, modules, SHORT_CALL)
    assert before == {"module2_offer": {"value_selling"}}
    assert scoring.deducted_criteria(modules) == {}      # nothing left to see


@pytest.mark.parametrize("status_field", ["contract_status", "final_score"])
def test_the_evaluate_response_keeps_its_keys(monkeypatch, status_field):
    """`/evaluate` gains a key and loses none — n8n reads by name."""
    from fastapi.testclient import TestClient
    from app import main

    payload = _e5ab9937_payload()
    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    monkeypatch.setattr(main.judge, "DeepSeekClient",
                        lambda *a, **kw: StubClient(payload, payload))
    monkeypatch.setattr(main, "MIN_SCOREABLE_CHARS", 20)

    r = TestClient(main.app).post(
        "/evaluate",
        json={"conversation": SHORT_CALL, "input_type": "call_transcript",
              "run_pass1": False},
        headers={"X-API-Key": "k"},
    )
    assert r.status_code == 200
    p2 = r.json()["pass2"]
    for key in ("payload", "final_score", "performance_level", "weight_applied",
                "gradeable", "modules", "warnings", "prompt_version", "model",
                "usage", "input_hash", "contract_status", "contract_violations",
                "evidence_rejected", "ungradeable_modules"):
        assert key in p2, key
    assert p2["contract_status"] == "ungradeable"
    assert p2[status_field] in ("ungradeable", None)
    assert [e["module"] for e in p2["ungradeable_modules"]] == \
        ["module1_reception", "module2_offer"]
