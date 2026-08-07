"""Score arithmetic and validation for the agent-quality rubric.

The model returns judgements. It does NOT return the final number: the weighted
score is recomputed here from the module scores, and the model's own arithmetic
is discarded. LLMs are reliable at "was there a price in this message" and
unreliable at multiplying by 0.25 and summing five terms — and a score that
drifts between runs of the same prompt destroys trust in the whole system.

Rubric version 1.0.0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RUBRIC_VERSION = "1.0.0"

WEIGHTS: dict[str, float] = {
    "module1_reception": 0.15,
    "module2_offer": 0.25,
    "module3_objections": 0.25,
    "module4_followup": 0.20,
    "module5_closing": 0.15,
}

# Below this share of the rubric, the remaining modules are too few to average
# into anything meaningful. Report ungradeable rather than a confident number
# built from one module.
MIN_WEIGHT_APPLIED = 0.40

# Max points per criterion, straight from the rubric. Used to rescale a module
# when some of its criteria are null.
CRITERION_MAX: dict[str, dict[str, int]] = {
    "module1_reception": {
        "greeting": 25, "understanding_confirmation": 25,
        "missing_info_request": 25, "next_step_transition": 25,
    },
    "module2_offer": {
        "attitude": 25, "offer_completeness": 25,
        "value_selling": 25, "alternative_offer": 25,
    },
    "module3_objections": {
        "price_objection": 25, "competitor_objection": 25,
        "thinking_time_objection": 25, "unavailable_service_objection": 25,
    },
    "module4_followup": {"timing": 40, "frequency": 30, "message_quality": 30},
    "module5_closing": {
        "payment_request": 30, "next_steps_confirmation": 20,
        "thank_you": 20, "booking_steps": 20, "service_review_request": 10,
    },
}


# Criteria that MAY be null, and the only situation that justifies it.
#
# Everything not listed here is always assessable and must carry a number. The
# first real judge run nulled `value_selling` on a call with plenty of value
# selling in it, which quietly removed the criterion from the denominator and
# pushed the module to 100. A permissive null is indistinguishable from a
# generous score, so the allowlist is enforced rather than requested.
NULLABLE_CRITERIA: dict[str, str] = {
    "module2_offer.offer_completeness": "no offer was presented",
    "module2_offer.alternative_offer": "the customer rejected nothing",
    "module3_objections.price_objection": "this objection did not arise",
    "module3_objections.competitor_objection": "this objection did not arise",
    "module3_objections.thinking_time_objection": "this objection did not arise",
    "module3_objections.unavailable_service_objection": "this objection did not arise",
    "module4_followup.timing": "no follow-up history was supplied",
    "module4_followup.frequency": "no follow-up history was supplied",
    "module4_followup.message_quality": "no follow-up history was supplied",
    "module5_closing.payment_request": "closing was never reached",
    "module5_closing.next_steps_confirmation": "closing was never reached",
    "module5_closing.thank_you": "the customer never approved",
    "module5_closing.booking_steps": "the customer never approved",
    "module5_closing.service_review_request": "the customer never approved",
}

# stage_reached values that assert an offer actually existed.
STAGES_IMPLYING_AN_OFFER = frozenset({
    "offer_presented", "negotiation", "closing_attempted", "deal_closed",
})


class RubricError(ValueError):
    """The model's output violates the rubric contract."""


@dataclass
class ScoreResult:
    final_score: float | None
    performance_level: str | None
    weight_applied: float
    modules: dict[str, float | None]
    gradeable: bool
    warnings: list[str] = field(default_factory=list)


def performance_level(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 55:
        return "Average"
    return "Below Average"


def module_score(module_key: str, breakdown: dict[str, Any]) -> float | None:
    """Rescale a module to 0-100 over the criteria that were actually scored.

    A null criterion is dropped from both numerator and denominator. Treating it
    as zero would punish the agent for a situation that never arose — which is
    the single most common way a rubric like this produces unfair numbers.
    """
    maxima = CRITERION_MAX[module_key]
    earned = possible = 0.0

    for name, cap in maxima.items():
        value = breakdown.get(name)
        if value is None:
            continue
        if not isinstance(value, (int, float)):
            raise RubricError(f"{module_key}.{name} is {value!r}, expected a number or null")
        if not 0 <= value <= cap:
            raise RubricError(f"{module_key}.{name} = {value}, outside 0..{cap}")
        earned += float(value)
        possible += cap

    if possible == 0:
        return None
    return round(earned / possible * 100, 2)


def compute(modules: dict[str, dict[str, Any]]) -> ScoreResult:
    """Recompute every module score and the weighted final from the breakdowns."""
    warnings: list[str] = []
    scores: dict[str, float | None] = {}

    for key in WEIGHTS:
        block = modules.get(key) or {}
        breakdown = block.get("breakdown") or {}
        derived = module_score(key, breakdown)

        # If the model reported a module score, trust the breakdown over it, but
        # say so — a large gap usually means the model misread a criterion cap.
        stated = block.get("score")
        if stated is not None and derived is not None and abs(float(stated) - derived) > 1.0:
            warnings.append(
                f"{key}: model said {float(stated):.1f}, breakdown gives {derived:.1f}; used breakdown"
            )
        scores[key] = derived

    weight_applied = round(sum(w for k, w in WEIGHTS.items() if scores[k] is not None), 3)

    if weight_applied < MIN_WEIGHT_APPLIED:
        warnings.append(
            f"only {weight_applied:.0%} of the rubric was exercised "
            f"(minimum {MIN_WEIGHT_APPLIED:.0%}); not gradeable"
        )
        return ScoreResult(None, None, weight_applied, scores, False, warnings)

    total = sum(scores[k] * w for k, w in WEIGHTS.items() if scores[k] is not None)
    final = round(total / weight_applied, 1)
    return ScoreResult(final, performance_level(final), weight_applied, scores, True, warnings)


def validate_evidence(payload: dict, conversation_text: str) -> list[str]:
    """Every quote in `evidence` must actually appear in the conversation.

    This is the cheapest available guard against a fabricated citation, and it
    is worth running on every evaluation: a hallucinated quote in a coaching
    report is worse than no report, because the agent can prove it wrong and
    then discounts every future score.
    """
    problems: list[str] = []
    haystack = " ".join(conversation_text.split())

    for i, item in enumerate(payload.get("evidence") or []):
        quote = (item.get("quote") or "").strip()
        if not quote:
            problems.append(f"evidence[{i}]: empty quote")
            continue
        if " ".join(quote.split()) not in haystack:
            problems.append(f"evidence[{i}]: quote not found in conversation: {quote[:60]!r}")
    return problems


def require_evidence_for_deductions(payload: dict, scores: dict[str, float | None]) -> list[str]:
    """A module below full marks with no evidence cited is unusable for coaching."""
    cited = {e.get("module") for e in (payload.get("evidence") or [])}
    return [
        f"{key} scored {value:.1f} but cites no evidence"
        for key, value in scores.items()
        if value is not None and value < 100 and key not in cited
    ]


def validate_nullability(modules: dict[str, Any]) -> list[str]:
    """Reject `null` on criteria that are always assessable.

    Nulling a criterion removes it from the denominator, so an unjustified null
    silently inflates the module. This is the single easiest way for a lenient
    judge to hand out marks it never actually awarded.
    """
    problems = []
    for module_key, caps in CRITERION_MAX.items():
        breakdown = (modules.get(module_key) or {}).get("breakdown") or {}
        for name in caps:
            if breakdown.get(name, "missing") is None and \
                    f"{module_key}.{name}" not in NULLABLE_CRITERIA:
                problems.append(
                    f"{module_key}.{name} is null but is always assessable — "
                    f"score it 0-{caps[name]}"
                )
    return problems


def validate_stage_consistency(payload: dict, modules: dict[str, Any]) -> list[str]:
    """`stage_reached` must agree with whether an offer was actually scored.

    The first real run reported `stage_reached: offer_presented` while nulling
    offer completeness *and* stating in its own notes that no offer was made.
    Two contradictory answers in one response means one of them is noise.
    """
    stage = payload.get("stage_reached")
    if stage not in STAGES_IMPLYING_AN_OFFER:
        return []

    completeness = ((modules.get("module2_offer") or {}).get("breakdown") or {}) \
        .get("offer_completeness", "missing")
    if completeness is None:
        return [
            f"stage_reached='{stage}' claims an offer was presented, but "
            f"module2_offer.offer_completeness is null (no offer). Pick one."
        ]
    return []


def validate_ranges(modules: dict[str, Any]) -> list[str]:
    """Reject criterion values outside their 0..cap range.

    A model that returns 50 for a criterion capped at 25 has misread the rubric,
    usually by scoring against a 100-point scale it invented. Clamping to the cap
    would fabricate a score the model never gave, so this is a re-ask, not a
    repair — the same reasoning as every other check here.

    Without this, `module_score` raises RubricError later and the whole request
    fails: observed on 4 of 25 real conversations, all with
    `module3_objections.price_objection = 50`.
    """
    problems = []
    for module_key, caps in CRITERION_MAX.items():
        breakdown = (modules.get(module_key) or {}).get("breakdown") or {}
        for name, cap in caps.items():
            value = breakdown.get(name)
            if value is None or name not in breakdown:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                problems.append(
                    f"{module_key}.{name} is {value!r}, expected a number 0-{cap} or null"
                )
            elif not 0 <= value <= cap:
                problems.append(
                    f"{module_key}.{name} = {value}, outside its range — "
                    f"score it 0-{cap}, not on a 0-100 scale"
                )
    return problems


def contract_violations(payload: dict, modules: dict[str, Any]) -> list[str]:
    """Every check that means the response should be re-asked, not repaired."""
    return (validate_nullability(modules)
            + validate_ranges(modules)
            + validate_stage_consistency(payload, modules))
