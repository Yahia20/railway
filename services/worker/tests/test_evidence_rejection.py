"""Criterion-level evidence enforcement.

The prompt has told the model since v1 that "a deduction with no quote is not a
finding — award the points instead". The code did not do that. It computed the
score first and then appended the evidence problems as warnings, so a deduction
resting on a quote nobody said still took the points off, and the "restored"
points existed only in a sentence in the prompt.

The second half of the same bug: the only evidence check that touched scoring
asked whether the MODULE was cited anywhere. One valid quote about a weak
greeting excused every other deduction in Module 1.

Every case below is written against a stub client. No test here talks to
DeepSeek.
"""
import pytest

from app.evaluate import judge, scoring


# ── fixtures ────────────────────────────────────────────────────────────────

CONVERSATION = (
    "[00:03] AGENT: السلام عليكم ترافل جيت مع خالد\n"
    "[00:07] CUSTOMER: عليكم السلام، أبغى عرض لتركيا لعائلة أربعة أشخاص\n"
    "[00:21] AGENT: أبشر، أحسب لك وأرسل لك العرض\n"
    "[01:40] CUSTOMER: تمام، جزاك الله خير\n"
)

# The two spans of a call whose middle was removed by the ASR cleaner. Nobody
# said them one after the other.
GAPPED = "AGENT: السعر ألفين ريال [[ASR_GAP]] CUSTOMER: خلاص ما عليه"


class StubClient:
    """A DeepSeek that answers from a script. Records what it was asked."""

    def __init__(self, *responses, model="stub-model"):
        self.model = model
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete_json(self, prompt, **_):
        self.prompts.append(prompt)
        payload = self._responses[min(len(self.prompts) - 1, len(self._responses) - 1)]
        # A fresh copy each call: run_pass2 mutates the payload it is given, and
        # a shared dict would let one call contaminate the next.
        import copy
        return copy.deepcopy(payload), {"prompt_tokens": 10, "completion_tokens": 5}


def _breakdowns(**overrides):
    """Full marks everywhere, then whatever the test wants changed."""
    modules = {}
    for key, caps in scoring.CRITERION_MAX.items():
        modules[key] = {"score": None, "breakdown": dict(caps)}
    for key, breakdown in overrides.items():
        modules[key] = {"score": None, "breakdown": breakdown}
    return modules


def _payload(modules=None, evidence=(), **extra):
    payload = {
        "schema_version": "1.0",
        "final_score": 99.9,                 # the model's arithmetic, discarded
        # Full marks on every module, so the call must have got far enough for
        # every module to be gradeable: price, competitor and thinking-time
        # objections all require `negotiation`, and a payload that scores them
        # at `reception` is a contract violation these tests are not about.
        "stage_reached": "negotiation",
        "modules": modules if modules is not None else _breakdowns(),
        "evidence": list(evidence),
        "notes": None,
    }
    payload.update(extra)
    return payload


def _run(payload, *more, conversation=CONVERSATION):
    client = StubClient(payload, *more)
    return judge.run_pass2(conversation, "call_transcript", client=client), client


# ── the decision table ──────────────────────────────────────────────────────

def test_valid_quote_for_the_exact_criterion_keeps_the_deduction():
    result, _ = _run(_payload(
        _breakdowns(module1_reception={
            "greeting": 25, "understanding_confirmation": 25,
            "missing_info_request": 0, "next_step_transition": 25}),
        evidence=[{"module": "module1_reception", "criterion": "missing_info_request",
                   "quote": "أبشر، أحسب لك وأرسل لك العرض", "speaker": "agent"}],
    ))
    assert result.evidence_rejected == []
    assert result.payload["modules"]["module1_reception"]["breakdown"]["missing_info_request"] == 0
    assert result.score.modules["module1_reception"] == 75.0


def test_missing_quote_discards_the_finding_and_restores_the_points():
    """The criterion goes back to its cap — and the MODULE goes to null.

    Both halves matter and they are not the same rule. Restoring the criterion
    to its cap and not to `null` keeps it in the denominator, so the agent is
    left whole instead of having the criterion quietly removed. But this module
    had exactly ONE deduction and nothing supported it, so the judge grounded
    nothing about Module 1 at all — publishing 100 for it would state a perfect
    reception on the strength of no finding either way. See
    `test_an_isolated_unsupported_finding_still_only_restores_to_cap` for the
    case where another deduction in the module survived.
    """
    result, _ = _run(_payload(
        _breakdowns(module1_reception={
            "greeting": 25, "understanding_confirmation": 25,
            "missing_info_request": 0, "next_step_transition": 25}),
        evidence=[],
    ))
    [rejection] = result.evidence_rejected
    assert rejection == {
        "module": "module1_reception", "criterion": "missing_info_request",
        "reason": "no evidence cited for this criterion",
        "model_score": 0, "restored_to": 25, "quote": None,
    }
    assert result.payload["modules"]["module1_reception"]["breakdown"]["missing_info_request"] == 25
    assert result.score.modules["module1_reception"] is None
    assert result.ungradeable_modules == [{
        "module": "module1_reception", "reason": "evidence_ungroundable",
        "discarded_criteria": ["missing_info_request"],
    }]
    # 0.85 of the rubric survives, so the call is still graded — on the modules
    # that were actually grounded.
    assert result.score.weight_applied == 0.85
    assert result.score.final_score == 100.0
    assert result.contract_status == "ok"
    assert result.score.gradeable is True


def test_fabricated_quote_discards_the_finding():
    result, _ = _run(_payload(
        # Everything else at its cap: the payload's stage is `negotiation`, so
        # a null offer_completeness would be its own contract violation and a
        # below-cap one its own unanchored deduction. Either would turn this
        # into a test of something other than quote validation.
        _breakdowns(module2_offer={
            **scoring.CRITERION_MAX["module2_offer"], "value_selling": 10}),
        evidence=[{"module": "module2_offer", "criterion": "value_selling",
                   "quote": "السعر ألفين دولار شامل كل شيء"}],     # never said
    ))
    [rejection] = result.evidence_rejected
    assert "not found in conversation" in rejection["reason"]
    assert rejection["quote"] == "السعر ألفين دولار شامل كل شيء"
    assert rejection["model_score"] == 10 and rejection["restored_to"] == 25


def test_a_quote_for_another_criterion_does_not_save_this_one():
    """The module-level bug, stated as a test.

    A real quote about the greeting used to excuse a deduction on a completely
    different criterion, because the check only asked whether the module was
    cited anywhere.
    """
    result, _ = _run(_payload(
        _breakdowns(module1_reception={
            "greeting": 10, "understanding_confirmation": 25,
            "missing_info_request": 0, "next_step_transition": 25}),
        evidence=[{"module": "module1_reception", "criterion": "greeting",
                   "quote": "السلام عليكم ترافل جيت مع خالد"}],
    ))
    rejected = {r["criterion"] for r in result.evidence_rejected}
    assert rejected == {"missing_info_request"}          # greeting kept its quote
    breakdown = result.payload["modules"]["module1_reception"]["breakdown"]
    assert breakdown["greeting"] == 10 and breakdown["missing_info_request"] == 25


def test_a_quote_stitched_across_an_asr_gap_is_not_evidence():
    result, _ = _run(
        _payload(
            _breakdowns(module2_offer={
                "attitude": 25, "offer_completeness": 5,
                "value_selling": 25, "alternative_offer": None}),
            evidence=[{"module": "module2_offer", "criterion": "offer_completeness",
                       "quote": "السعر ألفين ريال CUSTOMER: خلاص ما عليه"}],
        ),
        conversation=GAPPED,
    )
    [rejection] = result.evidence_rejected
    assert "not found in conversation" in rejection["reason"]


def test_the_gap_marker_itself_is_never_quotable():
    result, _ = _run(
        _payload(
            _breakdowns(module2_offer={
                "attitude": 25, "offer_completeness": 5,
                "value_selling": 25, "alternative_offer": None}),
            evidence=[{"module": "module2_offer", "criterion": "offer_completeness",
                       "quote": "السعر ألفين ريال [[ASR_GAP]]"}],
        ),
        conversation=GAPPED,
    )
    assert "ASR gap marker" in result.evidence_rejected[0]["reason"]


def test_a_criterion_prefixed_with_its_module_still_matches():
    """Models write `module1_reception.greeting` about a third of the time."""
    result, _ = _run(_payload(
        _breakdowns(module1_reception={
            "greeting": 10, "understanding_confirmation": 25,
            "missing_info_request": 25, "next_step_transition": 25}),
        evidence=[{"module": "module1_reception",
                   "criterion": "module1_reception.greeting",
                   "quote": "السلام عليكم ترافل جيت مع خالد"}],
    ))
    assert result.evidence_rejected == []


def test_a_legitimate_null_stays_null_and_is_not_restored():
    """Enforcement must never touch the not-applicable path.

    Restoring a null to its cap would be the source rubric's automatic full
    marks coming back in through the side door.
    """
    result, _ = _run(_payload(_breakdowns(
        module3_objections=dict.fromkeys(scoring.CRITERION_MAX["module3_objections"], None),
    )))
    assert result.evidence_rejected == []
    assert result.score.modules["module3_objections"] is None
    assert result.score.weight_applied == 0.75


def test_restored_points_change_the_stored_score():
    """The whole point, in numbers: the same response, scored two ways."""
    modules = _breakdowns(module2_offer={
        "attitude": 25, "offer_completeness": 0,
        "value_selling": 0, "alternative_offer": None})
    before = scoring.compute(modules).final_score

    result, _ = _run(_payload(modules, evidence=[]))
    assert before == 83.3                       # unsupported findings counted
    assert result.score.final_score == 100.0    # unsupported findings discarded
    assert len(result.evidence_rejected) == 2


# ── a thin denominator is a failure, not a thin score ───────────────────────
# This test used to assert the opposite: that nulls the model refused to
# withdraw produced `contract_status="ok"` with `weight_applied=0.15` and a
# module breakdown n8n would happily store. That blessed the leniency path.
# An unjustified null shrinks the denominator, so a response that keeps one
# after being told exactly which one is a response whose score is inflated by a
# known amount — and 0.15 of the rubric is not "thin", it is four fifths of the
# rubric missing.

def test_unresolved_nullability_is_a_contract_failure_not_a_thin_score():
    thin = _payload(_breakdowns(
        module2_offer=dict.fromkeys(scoring.CRITERION_MAX["module2_offer"], None),
        module3_objections=dict.fromkeys(scoring.CRITERION_MAX["module3_objections"], None),
        module4_followup=dict.fromkeys(scoring.CRITERION_MAX["module4_followup"], None),
        module5_closing=dict.fromkeys(scoring.CRITERION_MAX["module5_closing"], None),
    ))
    result, client = _run(thin, thin)

    assert len(client.prompts) == 2                       # asked again, once
    assert result.contract_status == "contract_failed"
    assert result.score.final_score is None
    assert result.score.gradeable is False
    assert any("attitude" in v for v in result.contract_violations)
    assert any("UNRESOLVED after retry" in w for w in result.warnings)
    # The evaluation itself is still there to look at — the raw breakdown is
    # kept inside the payload, which is what forensics needs and what a
    # dashboard never reads.
    assert result.payload["modules"]["module1_reception"]["breakdown"]["greeting"] == 25


def test_a_contract_failure_publishes_no_partial_denominator():
    """Module 1 is perfect and could be scored. It must not be published.

    A `weight_applied` of 0.15 with one real module score attached is the shape
    of a meaningful result. n8n stores it, the agent's month averages it, and
    the reason it is wrong lives in a warnings array nobody aggregates.
    """
    thin = _payload(_breakdowns(
        module2_offer=dict.fromkeys(scoring.CRITERION_MAX["module2_offer"], None),
        module3_objections=dict.fromkeys(scoring.CRITERION_MAX["module3_objections"], None),
        module4_followup=dict.fromkeys(scoring.CRITERION_MAX["module4_followup"], None),
        module5_closing=dict.fromkeys(scoring.CRITERION_MAX["module5_closing"], None),
    ))
    result, _ = _run(thin, thin)

    assert result.score.weight_applied == 0.0
    assert result.payload["weight_applied"] == 0.0
    assert set(result.score.modules) == set(scoring.WEIGHTS)
    assert all(v is None for v in result.score.modules.values())


def test_an_omitted_criterion_key_is_a_contract_violation():
    """Dropping the key is strictly cheaper for a lenient judge than nulling it.

    Same effect on the denominator, and until `validate_completeness` existed,
    no check fired at all: `breakdown.get(name, "missing")` read an absent key
    as fine.
    """
    modules = _breakdowns(module2_offer={
        "attitude": 25, "offer_completeness": 25, "alternative_offer": None})
    problems = scoring.validate_completeness(modules)
    assert len(problems) == 1
    assert "module2_offer.value_selling is missing" in problems[0]
    assert problems == [p for p in scoring.contract_violations(_payload(modules), modules)
                        if "missing from the breakdown" in p]


def test_an_omitted_module_is_a_contract_violation():
    modules = _breakdowns()
    del modules["module4_followup"]
    problems = scoring.validate_completeness(modules)
    assert len(problems) == 1
    assert "module4_followup is missing from `modules`" in problems[0]


def test_an_omitted_module_survives_the_retry_as_contract_failed():
    incomplete = _payload(_breakdowns())
    del incomplete["modules"]["module5_closing"]
    result, client = _run(incomplete, incomplete)

    assert len(client.prompts) == 2
    assert result.contract_status == "contract_failed"
    assert any("module5_closing" in v for v in result.contract_violations)
    assert result.score.weight_applied == 0.0


def test_the_correction_reask_happens_exactly_once():
    good = _payload()
    bad = _payload(_breakdowns(
        module2_offer={"attitude": None, "offer_completeness": None,
                       "value_selling": None, "alternative_offer": None}))
    result, client = _run(bad, good)

    assert len(client.prompts) == 2
    assert "CORRECTION" in client.prompts[1]
    assert "attitude" in client.prompts[1]
    assert any("re-asked once" in w for w in result.warnings)
    assert result.contract_status == "ok"


# ── the re-ask comes BEFORE the restoration ─────────────────────────────────
# The order was the whole finding of the PR2 review. Enforcement used to run
# only after the single correction, so the correction never mentioned evidence
# and the judge was never given the chance to support or withdraw a finding
# before its points were handed back. On the day-13 replay that restored 82
# omission findings without asking once — which measures the code's leniency,
# not the judge's.

def _weak(**overrides):
    """A breakdown with one unsupported deduction on `missing_info_request`."""
    return _payload(_breakdowns(module1_reception={
        "greeting": 25, "understanding_confirmation": 25,
        "missing_info_request": 0, "next_step_transition": 25}), **overrides)


def test_an_unsupported_deduction_triggers_the_correction_before_restoring():
    result, client = _run(_weak(), _weak())

    assert len(client.prompts) == 2
    correction = client.prompts[1]
    assert "CORRECTION" in correction
    assert "module1_reception.missing_info_request is scored 0" in correction
    assert "no evidence cited for this criterion" in correction
    assert "omission-anchor rule" in correction
    assert "restore the criterion to its cap of 25" in correction
    # Only after the model declined to fix it are the points handed back.
    assert [r["criterion"] for r in result.evidence_rejected] == ["missing_info_request"]


def test_the_model_can_rescue_its_own_finding_in_the_correction():
    """The re-ask is a real chance, not a formality: a genuine omission has an
    anchor the model can quote, and quoting it keeps the deduction."""
    anchored = _weak(evidence=[
        {"module": "module1_reception", "criterion": "missing_info_request",
         "quote": "أبشر، أحسب لك وأرسل لك العرض", "speaker": "agent",
         "effect": "moved on without asking for travel dates"},
    ])
    result, client = _run(_weak(), anchored)

    assert len(client.prompts) == 2
    assert result.evidence_rejected == []            # nothing restored
    assert result.payload["modules"]["module1_reception"]["breakdown"][
        "missing_info_request"] == 0
    assert result.score.modules["module1_reception"] == 75.0


def test_the_correction_prompt_carries_the_previous_response_json():
    """The API is stateless. "Your previous response" names nothing unless the
    response is in the request."""
    result, client = _run(_weak(), _weak())

    correction = client.prompts[1]
    assert "This is the response you returned" in correction
    assert '"missing_info_request": 0' in correction
    assert '"stage_reached": "negotiation"' in correction
    assert result.contract_status == "ok"


def test_usage_is_aggregated_across_both_calls():
    """The correction re-ask used to overwrite the first attempt's usage, so the
    cost report undercounted precisely the conversations that cost most."""
    one, client_one = _run(_payload())                     # no correction needed
    two, client_two = _run(_weak(), _weak())               # two calls

    assert len(client_one.prompts) == 1 and len(client_two.prompts) == 2
    assert one.usage["prompt_tokens"] == 10
    assert one.usage["api_calls"] == 1
    assert two.usage["prompt_tokens"] == 20                # 10 + 10, not 10
    assert two.usage["completion_tokens"] == 10
    assert two.usage["api_calls"] == 2


def test_the_pre_enforcement_score_is_recorded_separately():
    """Without it, a prompt change and a code change land in one delta."""
    result, _ = _run(_weak(), _weak())

    assert result.pre_enforcement_score == 96.2           # what the model returned
    assert result.score.final_score == 100.0             # after restoration
    assert result.pre_enforcement_score != result.score.final_score


# ── evidence must not contradict itself about where it came from ────────────

@pytest.mark.parametrize("criterion", [
    "module2_offer.greeting",          # names a different module
    "garbage.greeting",                # names nothing
    "module1_reception.extra.greeting",  # deeper dot path
    ".greeting",                       # empty prefix
    "greeting.",                       # empty criterion
])
def test_a_contradictory_criterion_prefix_does_not_rescue_a_deduction(criterion):
    """Suffix-matching accepted any prefix after the final dot, so an entry
    declaring one module and citing another rescued the deduction anyway. A
    citation whose two halves disagree is evidence for neither half."""
    modules = _breakdowns(module1_reception={
        "greeting": 10, "understanding_confirmation": 25,
        "missing_info_request": 25, "next_step_transition": 25})
    rejected = scoring.enforce_criterion_evidence(
        {"evidence": [{"module": "module1_reception", "criterion": criterion,
                       "quote": "السلام عليكم ترافل جيت مع خالد"}]},
        modules, CONVERSATION)

    assert [r["criterion"] for r in rejected] == ["greeting"]
    assert modules["module1_reception"]["breakdown"]["greeting"] == 25


def test_the_two_accepted_spellings_both_still_work():
    for criterion in ("greeting", " Greeting ", "module1_reception.greeting"):
        modules = _breakdowns(module1_reception={
            "greeting": 10, "understanding_confirmation": 25,
            "missing_info_request": 25, "next_step_transition": 25})
        rejected = scoring.enforce_criterion_evidence(
            {"evidence": [{"module": "module1_reception", "criterion": criterion,
                           "quote": "السلام عليكم ترافل جيت مع خالد"}]},
            modules, CONVERSATION)
        assert rejected == [], criterion


def test_evidence_criterion_key_normalises_and_refuses():
    assert scoring.evidence_criterion_key("module1_reception", " Greeting ") == \
        ("module1_reception", "greeting")
    assert scoring.evidence_criterion_key(
        "Module1_Reception", "module1_reception.greeting") == \
        ("module1_reception", "greeting")
    assert scoring.evidence_criterion_key("module1_reception", "module2_offer.greeting") is None
    assert scoring.evidence_criterion_key("module1_reception", None) is None
    assert scoring.evidence_criterion_key(None, "greeting") == ("", "greeting")


# ── unit level ──────────────────────────────────────────────────────────────

def test_enforce_is_a_no_op_on_a_perfect_breakdown():
    modules = _breakdowns()
    assert scoring.enforce_criterion_evidence({"evidence": []}, modules, CONVERSATION) == []
    assert modules["module1_reception"]["breakdown"]["greeting"] == 25


def test_enforce_ignores_non_numeric_criteria():
    """`validate_ranges` owns those, and it fires before this ever runs."""
    modules = _breakdowns(module3_objections={
        "price_objection": "25", "competitor_objection": None,
        "thinking_time_objection": None, "unavailable_service_objection": None})
    assert scoring.enforce_criterion_evidence({"evidence": []}, modules, CONVERSATION) == []
    assert modules["module3_objections"]["breakdown"]["price_objection"] == "25"


@pytest.mark.parametrize("evidence", [
    [{"module": "module1_reception", "criterion": "greeting", "quote": ""}],
    [{"module": "module1_reception", "criterion": "greeting", "quote": None}],
    [{"module": "module1_reception", "criterion": None, "quote": "السلام عليكم ترافل جيت مع خالد"}],
    ["not a dict"],
])
def test_unusable_evidence_entries_do_not_rescue_a_deduction(evidence):
    modules = _breakdowns(module1_reception={
        "greeting": 5, "understanding_confirmation": 25,
        "missing_info_request": 25, "next_step_transition": 25})
    rejected = scoring.enforce_criterion_evidence(
        {"evidence": evidence}, modules, CONVERSATION)
    assert [r["criterion"] for r in rejected] == ["greeting"]
