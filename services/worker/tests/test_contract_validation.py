"""Guards against the leniency the first real DeepSeek run exhibited.

Every case here is something the model actually did on call
q-3009-0500000000-20260701-170522, not a hypothetical.
"""
import pytest

from app.evaluate import scoring


def _modules(**overrides):
    base = {
        k: {"breakdown": dict(scoring.CRITERION_MAX[k])}
        for k in scoring.WEIGHTS
    }
    for key, breakdown in overrides.items():
        base[key] = {"breakdown": breakdown}
    return base


def test_nulling_value_selling_is_rejected():
    """The real failure: value_selling nulled on a call full of value selling,
    which removed it from the denominator and pushed module 2 to 100."""
    modules = _modules(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": None, "alternative_offer": None,
    })
    problems = scoring.validate_nullability(modules)
    assert any("value_selling" in p for p in problems)
    assert not any("offer_completeness" in p for p in problems)   # legitimately nullable
    assert not any("alternative_offer" in p for p in problems)


def test_nulling_attitude_is_rejected():
    modules = _modules(module2_offer={
        "attitude": None, "offer_completeness": None,
        "value_selling": 20, "alternative_offer": None,
    })
    assert any("attitude" in p for p in scoring.validate_nullability(modules))


def test_nulling_any_reception_criterion_is_rejected():
    modules = _modules(module1_reception={
        "greeting": 25, "understanding_confirmation": 25,
        "missing_info_request": 25, "next_step_transition": None,
    })
    assert any("next_step_transition" in p for p in scoring.validate_nullability(modules))


def test_legitimate_nulls_pass():
    modules = _modules(
        module2_offer={"attitude": 20, "offer_completeness": None,
                       "value_selling": 20, "alternative_offer": None},
        module3_objections=dict.fromkeys(scoring.CRITERION_MAX["module3_objections"], None),
        module4_followup=dict.fromkeys(scoring.CRITERION_MAX["module4_followup"], None),
        module5_closing=dict.fromkeys(scoring.CRITERION_MAX["module5_closing"], None),
    )
    assert scoring.validate_nullability(modules) == []


def test_stage_contradicting_a_null_offer_is_caught():
    """The real failure: stage_reached='offer_presented' while offer_completeness
    was null and the model's own notes said no offer was made."""
    modules = _modules(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": 20, "alternative_offer": None,
    })
    problems = scoring.validate_stage_consistency({"stage_reached": "offer_presented"}, modules)
    assert len(problems) == 1
    assert "Pick one" in problems[0]


@pytest.mark.parametrize("stage", ["reception", "follow_up"])
def test_pre_offer_stages_are_consistent_with_a_null_offer(stage):
    modules = _modules(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": 20, "alternative_offer": None,
    })
    assert scoring.validate_stage_consistency({"stage_reached": stage}, modules) == []


def test_stage_with_a_real_offer_is_consistent():
    modules = _modules(module2_offer={
        "attitude": 25, "offer_completeness": 15,
        "value_selling": 20, "alternative_offer": 25,
    })
    assert scoring.validate_stage_consistency({"stage_reached": "negotiation"}, modules) == []


def test_contract_violations_aggregates_both_checks():
    modules = _modules(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": None, "alternative_offer": None,
    })
    problems = scoring.contract_violations({"stage_reached": "offer_presented"}, modules)
    assert len(problems) == 2


def test_out_of_range_criterion_is_a_contract_violation():
    """The real failure on the Jul-Aug chat batch: the model scored
    price_objection 50 against a cap of 25, on 4 of 25 conversations.

    Before this guard the value passed contract_violations untouched, so the
    re-ask never fired and compute() raised RubricError into a 500."""
    modules = _modules(module3_objections={
        "price_objection": 50, "competitor_objection": None,
        "thinking_time_objection": None, "unavailable_service_objection": None,
    })
    problems = scoring.validate_ranges(modules)
    assert any("price_objection" in p for p in problems)
    assert problems == scoring.contract_violations({"stage_reached": "negotiation"}, modules)


def test_ranges_accept_the_boundaries_and_nulls():
    modules = _modules(module3_objections={
        "price_objection": 25, "competitor_objection": 0,
        "thinking_time_objection": None, "unavailable_service_objection": None,
    })
    assert scoring.validate_ranges(modules) == []


def test_negative_and_non_numeric_criteria_are_rejected():
    modules = _modules(module3_objections={
        "price_objection": -5, "competitor_objection": "25",
        "thinking_time_objection": None, "unavailable_service_objection": None,
    })
    problems = scoring.validate_ranges(modules)
    assert any("price_objection" in p for p in problems)
    assert any("competitor_objection" in p for p in problems)


def test_unjustified_null_would_have_inflated_module2():
    """Demonstrates why the guard matters, in points.

    Same attitude score. Nulling the other three criteria turns 25/100 into
    a perfect module."""
    inflated = scoring.module_score("module2_offer", {
        "attitude": 25, "offer_completeness": None,
        "value_selling": None, "alternative_offer": None})
    honest = scoring.module_score("module2_offer", {
        "attitude": 25, "offer_completeness": None,
        "value_selling": 20, "alternative_offer": None})
    assert inflated == 100.0
    assert honest == 90.0


# ── evidence quoting ────────────────────────────────────────────────────────
# Observed on n8n execution 95, 2026-08-09: the model cited an offer by
# stitching the start and end of one agent message together and dropping the
# sentence between them, with no ellipsis. The splice reads as a single verbatim
# quote and every word in it is genuine, so nothing but a substring check
# catches it.

_AGENT_OFFER = (
    "فندق Ramada Merter ٤ نجوم في إسطنبول، ٧ ليالٍ من ١٠ إلى ١٧ أغسطس، "
    "شامل الإفطار والعشاء. "
    "الفندق قريب من المترو وفيه مسبح للأطفال، مناسب جداً لرحلة عائلية. "
    "الحجز يحتاج دفعة ٣٠٪ والإلغاء مجاني حتى ٧ أيام قبل السفر."
)


def test_spliced_quote_is_rejected():
    """Two real spans of one message, joined across an elided sentence."""
    spliced = (
        "فندق Ramada Merter ٤ نجوم في إسطنبول، ٧ ليالٍ من ١٠ إلى ١٧ أغسطس، "
        "شامل الإفطار والعشاء. "
        "الحجز يحتاج دفعة ٣٠٪ والإلغاء مجاني حتى ٧ أيام قبل السفر."
    )
    problems = scoring.validate_evidence({"evidence": [{"quote": spliced}]}, _AGENT_OFFER)
    assert any("evidence[0]" in p for p in problems)


def test_contiguous_quote_survives_whitespace_differences():
    """The guard must not fire on line wrapping — only on altered content."""
    quote = "الفندق قريب من  المترو وفيه مسبح للأطفال،\n  مناسب جداً لرحلة عائلية."
    assert scoring.validate_evidence({"evidence": [{"quote": quote}]}, _AGENT_OFFER) == []
