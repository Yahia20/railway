"""Tests for the rubric arithmetic.

The behaviour these lock down is the difference between the source .docx and the
production rubric, so they are the tests that matter most: if `null` handling
regresses, scores silently inflate and nobody notices until an agent disputes one.
"""
import pytest

from app.evaluate import scoring


def full(module_key):
    return {k: v for k, v in scoring.CRITERION_MAX[module_key].items()}


def test_all_modules_perfect_scores_100():
    modules = {k: {"breakdown": full(k)} for k in scoring.WEIGHTS}
    r = scoring.compute(modules)
    assert r.final_score == 100.0
    assert r.weight_applied == 1.0
    assert r.performance_level == "Excellent"


def test_null_criterion_is_dropped_not_zeroed():
    """A module with one null criterion is scored over the rest, not penalised."""
    breakdown = full("module1_reception")
    breakdown["next_step_transition"] = None
    # 75 of 75 possible -> 100, not 75.
    assert scoring.module_score("module1_reception", breakdown) == 100.0


def test_partial_criterion_rescales_over_scored_criteria_only():
    breakdown = {"greeting": 25, "understanding_confirmation": 25,
                 "missing_info_request": 25, "next_step_transition": 15}
    # 90 of 100
    assert scoring.module_score("module1_reception", breakdown) == 90.0


def test_module_with_all_criteria_null_is_none():
    breakdown = {k: None for k in scoring.CRITERION_MAX["module3_objections"]}
    assert scoring.module_score("module3_objections", breakdown) is None


def test_absent_modules_renormalise_the_denominator():
    """The headline change from the source rubric.

    Modules 3 and 4 are 45% of the weight. When neither situation arose they
    must drop out of the denominator rather than score 100.
    """
    modules = {
        "module1_reception": {"breakdown": {"greeting": 25, "understanding_confirmation": 25,
                                            "missing_info_request": 25,
                                            "next_step_transition": 15}},   # 90
        "module2_offer": {"breakdown": {"attitude": 20, "offer_completeness": None,
                                        "value_selling": 20, "alternative_offer": None}},  # 40/50 -> 80
        "module3_objections": {"breakdown": dict.fromkeys(
            scoring.CRITERION_MAX["module3_objections"], None)},
        "module4_followup": {"breakdown": dict.fromkeys(
            scoring.CRITERION_MAX["module4_followup"], None)},
        "module5_closing": {"breakdown": dict.fromkeys(
            scoring.CRITERION_MAX["module5_closing"], None)},
    }
    r = scoring.compute(modules)
    assert r.modules["module3_objections"] is None
    assert r.modules["module4_followup"] is None
    assert r.weight_applied == 0.40                      # 0.15 + 0.25
    # (90*0.15 + 80*0.25) / 0.40 = 83.75 -> 83.8
    assert r.final_score == 83.8
    assert r.gradeable is True


def test_source_rubric_behaviour_would_have_inflated_this_call():
    """Documents the bug the change fixes, using the real first call.

    Under the source .docx, absent objections and absent follow-up each score
    100 automatically. Same agent behaviour, 4 points higher, and the difference
    lands in the 'Excellent' band.
    """
    as_written = {
        "module1_reception": {"breakdown": {"greeting": 25, "understanding_confirmation": 25,
                                            "missing_info_request": 25, "next_step_transition": 15}},
        "module2_offer": {"breakdown": {"attitude": 20, "offer_completeness": 0,
                                        "value_selling": 20, "alternative_offer": 25}},
        "module3_objections": {"breakdown": dict.fromkeys(
            scoring.CRITERION_MAX["module3_objections"], 25)},          # "100 automatically"
        "module4_followup": {"breakdown": {"timing": 40, "frequency": 30,
                                           "message_quality": 30}},     # "100 automatically"
        "module5_closing": {"breakdown": dict.fromkeys(
            scoring.CRITERION_MAX["module5_closing"], None)},
    }
    r = scoring.compute(as_written)
    assert r.final_score == 87.9
    assert r.performance_level == "Excellent"            # for a call with no price quoted


def test_too_thin_to_grade_returns_none_not_a_number():
    modules = {k: {"breakdown": dict.fromkeys(scoring.CRITERION_MAX[k], None)}
               for k in scoring.WEIGHTS}
    modules["module1_reception"] = {"breakdown": full("module1_reception")}
    r = scoring.compute(modules)                          # only 0.15 exercised
    assert r.gradeable is False
    assert r.final_score is None
    assert r.performance_level is None
    assert any("not gradeable" in w for w in r.warnings)


def test_criterion_above_its_cap_is_rejected():
    with pytest.raises(scoring.RubricError):
        scoring.module_score("module1_reception", {"greeting": 40})


def test_performance_bands():
    assert scoring.performance_level(85) == "Excellent"
    assert scoring.performance_level(84.9) == "Good"
    assert scoring.performance_level(70) == "Good"
    assert scoring.performance_level(69.9) == "Average"
    assert scoring.performance_level(55) == "Average"
    assert scoring.performance_level(54.9) == "Below Average"


def test_evidence_quote_must_appear_in_the_conversation():
    conversation = "[00:12] AGENT: أنا بظبط لك عرض كويس جدا"
    payload = {"evidence": [
        {"module": "module2_offer", "quote": "أنا بظبط لك عرض كويس جدا"},
        {"module": "module2_offer", "quote": "the price is $2000"},      # fabricated
    ]}
    problems = scoring.validate_evidence(payload, conversation)
    assert len(problems) == 1
    assert "not found" in problems[0]


def test_deduction_without_evidence_is_flagged():
    problems = scoring.require_evidence_for_deductions(
        {"evidence": []}, {"module1_reception": 90.0, "module2_offer": 100.0}
    )
    assert len(problems) == 1
    assert "module1_reception" in problems[0]


# ── the span validator, used by both passes ─────────────────────────────────
# `quote_problem` is the one place that decides whether a quote is real. Pass-1
# field validation, pass-2 evidence warnings and criterion-level enforcement all
# route through it, so a quote can never be genuine to one caller and fabricated
# to another.

def test_quote_problem_reports_why_not_just_that():
    spans = scoring.conversation_spans("AGENT: أرسل لك العرض [[ASR_GAP]] بكرة الصبح")

    assert scoring.quote_problem("أرسل لك العرض", *spans) is None
    assert scoring.quote_problem("", *spans) == "empty quote"
    assert scoring.quote_problem(None, *spans) == "empty quote"
    assert "ASR gap marker" in scoring.quote_problem("العرض [[ASR_GAP]]", *spans)
    # Across the seam: both halves are real, the sentence is not.
    assert "not found" in scoring.quote_problem("أرسل لك العرض بكرة الصبح", *spans)


def test_quote_is_valid_folds_arabic_orthography():
    assert scoring.quote_is_valid("احسب لك العرض", "AGENT: أحسب لك العرض")
    assert not scoring.quote_is_valid("أحسب لك الفيزا", "AGENT: أحسب لك العرض")


def test_an_unsupported_deduction_is_restored_to_its_cap_not_to_null():
    """Null would drop the criterion from the denominator and inflate the
    module — the source rubric's automatic full marks coming back in through
    the side door. The cap leaves the agent whole and the denominator honest."""
    modules = {"module1_reception": {"breakdown": {
        "greeting": 25, "understanding_confirmation": 25,
        "missing_info_request": 0, "next_step_transition": 25}}}

    rejected = scoring.enforce_criterion_evidence({"evidence": []}, modules, "لا شيء")
    breakdown = modules["module1_reception"]["breakdown"]

    assert breakdown["missing_info_request"] == 25
    assert rejected[0]["restored_to"] == 25 and rejected[0]["model_score"] == 0
    assert scoring.module_score("module1_reception", breakdown) == 100.0
