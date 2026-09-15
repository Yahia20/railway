"""The rubric as CLOSED SETS — every criterion score is a sum of fixed constants.

WHY THIS FILE EXISTS. Until v7 the model was asked for a NUMBER per criterion:
`{"greeting": null}` came back as `{"greeting": 18}`. The rubric text says that
criterion is 10 + 10 + 5, so 18 is not a score the rubric can produce — but
nothing rejected it, and nothing could say which of the three items the model
thought it had seen. Two consequences, both visible in production rows:

  * RE-RUN DRIFT. The same conversation, the same prompt, temperature 0, scored
    differently across runs, because "how good was the greeting, out of 25" is a
    continuous judgement and a continuous judgement is where sampling noise
    lands. `v_quality_by_input` records the spread: 8.1 points over 5 chats.
  * MODEL DRIFT. deepseek-v4-flash and deepseek-v4-pro agreed on the FACTS of a
    conversation and still returned different numbers for it, so changing the
    model changed the score of work nobody had redone.

The fix is not a better adjective. It is to stop asking for the number. The
model reports WHAT IT OBSERVED — a boolean, or a label from a closed list — and
this table turns observations into points. Two models that agree on the facts
now produce the identical score, and a score that moves has a named item that
flipped.

WHAT IS NOT CHANGED: not one criterion, weight, cap or point value. Every
number below is transcribed from the rubric text that produced it, and
`test_rubric_items.py` asserts each criterion's items sum to its cap in
`scoring.CRITERION_MAX`. This file is a different ENCODING of the same rubric,
which is why `RUBRIC_VERSION` stays 1.0.0.

TWO ITEM KINDS, AND WHY BOTH.

  * `Flag` — worth its points or nothing. "Did the agent state a total price?"
  * `Choice` — one label from a closed list, each label carrying fixed points.

`Choice` exists because parts of the rubric are genuinely three-way: a payment
request is direct (30), indirect (15) or absent (0). Forcing that into booleans
would have changed the rubric. A pick from three labels is as reproducible as a
boolean and leaves the rubric alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Flag:
    """An item that is observed or not. Absent, unreadable or unquoted = 0."""
    points: int
    asks: str


@dataclass(frozen=True)
class Choice:
    """One label from a closed list. An unlisted label is a contract violation."""
    options: Mapping[str, int]
    asks: str

    @property
    def points(self) -> int:
        return max(self.options.values())


# ---------------------------------------------------------------------------
# The table. Read it against the prompt: the `asks` strings ARE the questions
# the prompt puts to the model, so changing the wording here without changing
# it there is a drift between what is asked and what is counted.
# ---------------------------------------------------------------------------

ITEMS: dict[str, dict[str, object]] = {
    "module1_reception": {
        "greeting": {
            "called_customer_by_name": Flag(10, "named the customer"),
            "introduced_self_by_name": Flag(10, "gave his or her own name"),
            "used_a_greeting": Flag(5, "opened with a greeting"),
        },
        "understanding_confirmation": {
            "named_the_request": Flag(10, "named the destination or the request"),
            "answered_the_question_asked": Flag(10, "answered what was asked, not something else"),
            "addressed_the_specifics": Flag(5, "engaged the specific details of the request"),
        },
        # 12 + 13, not 12.5 + 12.5: the rubric splits it unevenly and the split
        # is load-bearing — traveller count alone passes at 13, dates alone
        # does not. Do not tidy this into equal halves.
        "missing_info_request": {
            "dates_known_or_asked": Flag(12, "travel dates were already known, or were asked for"),
            "traveler_count_known_or_asked": Flag(13, "traveller count was already known, or was asked for"),
        },
        "next_step_transition": {
            "promised_a_quote": Flag(15, "promised to prepare or send something"),
            "gave_a_timeframe": Flag(10, "named an actual TIME, not a channel"),
        },
    },
    "module2_offer": {
        "attitude": {
            "professional_language": Choice({
                "throughout": 10,
                "one_lapse": 5,
                "repeated": 0,
            }, "professional and respectful language"),
            "difficult_customer": Choice({
                # The rubric awards the full 10 when the customer was never
                # difficult. That is deliberate: an agent cannot earn points for
                # calm under pressure that never came, and must not lose them.
                "never_difficult": 10,
                "stayed_calm": 10,
                "defensive": 5,
                "angry_or_ignored": 0,
            }, "handling a pushy or upset customer"),
            "no_defeatist_language": Flag(5, "used no 'nothing I can do' / 'impossible'"),
        },
        "offer_completeness": {
            "total_price": Flag(5, "a total price"),
            "package_contents": Flag(5, "what the package contains"),
            "hotel_name_and_rating": Flag(5, "the hotel name and its star rating"),
            "travel_dates": Flag(5, "the travel dates"),
            "booking_and_cancellation_terms": Flag(5, "booking and cancellation terms"),
        },
        "value_selling": {
            "stated_features": Flag(10, "said what the hotel or service actually offers"),
            "connected_to_need": Flag(10, "tied a feature to THIS customer's stated need"),
            "used_persuasion": Flag(5, "persuaded rather than listed"),
        },
        "alternative_offer": Choice({
            "understood_then_offered": 25,
            "offered_without_understanding": 15,
            "offered_nothing": 0,
        }, "what the agent did after the customer rejected something"),
    },
    "module3_objections": {
        "price_objection": {
            "asked_why_expensive": Flag(10, "asked why the customer finds it expensive"),
            "explained_value_vs_price": Flag(10, "explained value against the price"),
            "offered_discount_or_cheaper": Flag(5, "offered a discount or a cheaper option"),
        },
        "competitor_objection": {
            "asked_about_the_other_offer": Flag(10, "asked what the other offer contains"),
            "explained_the_difference": Flag(10, "explained how the two differ"),
            "offered_competitive_discount": Flag(5, "answered on price"),
        },
        "thinking_time_objection": {
            "asked_reason_for_hesitation": Flag(10, "asked what the hesitation is about"),
            "set_a_specific_followup_time": Flag(10, "named a specific time to come back"),
            "created_urgency": Flag(5, "gave a real reason not to wait"),
        },
        "unavailable_service_objection": Choice({
            "professional_with_alternative": 25,
            "apologised_no_alternative": 15,
            "bare_refusal": 0,
        }, "how the refusal was delivered"),
    },
    "module4_followup": {
        "timing": Choice({
            "within_24h": 40,
            "between_24_and_48h": 20,
            "after_48h": 0,
            "never": 0,
        }, "how long after the customer's last message the agent came back"),
        "frequency": Choice({
            "two_or_three": 30,
            "exactly_one": 15,
            "none": 0,
        }, "how many follow-ups there were"),
        "message_quality": {
            "asked_about_the_decision": Flag(15, "asked directly about the decision"),
            "reminded_of_value": Flag(15, "reminded the customer what they would get"),
        },
    },
    "module5_closing": {
        "payment_request": Choice({
            "direct": 30,
            "indirect": 15,
            "never": 0,
        }, "how the agent asked for payment"),
        "next_steps_confirmation": Choice({
            "explained": 20,
            "mentioned": 10,
            "none": 0,
        }, "what happens after payment"),
        "thank_you": Choice({
            "warm": 20,
            "brief": 10,
            "none": 0,
        }, "thanking and welcoming the customer"),
        "booking_steps": Choice({
            "full": 20,
            "partial": 10,
            "none": 0,
        }, "the post-payment booking steps"),
        "service_review_request": Flag(10, "asked for a rating or a review"),
    },
}


# CRITERIA WHOSE BEST ANSWER IS AN ABSENCE, AND THEREFORE CANNOT BE QUOTED.
#
# Rule 2 says no quote, no `true`, and `unquotable_positives` enforces it. That
# works for every criterion asking whether the agent DID something: there are
# words to point at, or there are not.
#
# `attitude` asks the opposite question. Its three top answers are
# "professional throughout" (no unprofessional turn), "the customer was never
# difficult" (no pressure to stay calm under) and "no defeatist language" (no
# such phrase). All three assert that something is NOT in the conversation, and
# an absence has no words of its own — demanding a quote for one is the same
# mistake as demanding a quote for a `false`, which is what this exemption list
# exists to stop repeating.
#
# Do not grow this list to silence an inconvenient reduction. The test is
# whether the BEST answer is the absence of a behaviour, not whether evidence
# happens to be hard to find.
ABSENCE_CRITERIA: frozenset[str] = frozenset({
    "module2_offer.attitude",
})


def _spec(module: str, criterion: str) -> object:
    try:
        return ITEMS[module][criterion]
    except KeyError as exc:                                    # pragma: no cover
        raise KeyError(f"no rubric items for {module}.{criterion}") from exc


def legal_values(module: str, criterion: str) -> frozenset[int]:
    """Every total this criterion's items can actually produce.

    Used to reject a free-typed number the rubric cannot generate. An 18 on a
    10/10/5 criterion is not a strict reading of anything — it is the model
    splitting a difference it was never asked to split.
    """
    spec = _spec(module, criterion)
    if isinstance(spec, Choice):
        return frozenset(spec.options.values())
    # A criterion that IS one item — module5's review request is the only one.
    if isinstance(spec, Flag):
        return frozenset({0, spec.points})

    totals = {0}
    for item in spec.values():                                 # type: ignore[union-attr]
        if isinstance(item, Flag):
            opts = (0, item.points)
        else:
            opts = tuple(sorted(set(item.options.values())))
        totals = {t + o for t in totals for o in opts}
    return frozenset(totals)


class ChecksError(ValueError):
    """The model's `checks` block does not match the rubric's closed sets."""


def score_criterion(module: str, criterion: str, checks: object) -> int:
    """Turn one criterion's observations into its points. No judgement here.

    `checks` is a bool-valued mapping for an item group, or a single label
    string for a `Choice`. Anything else raises: a criterion whose observations
    cannot be read must NOT quietly fall back to a number the model typed, or
    the whole guarantee this file exists for is off by one conversation and
    nobody can tell which.
    """
    spec = _spec(module, criterion)

    if isinstance(spec, Flag):
        if checks is True:
            return spec.points
        if checks in (False, None):
            return 0
        raise ChecksError(
            f"{module}.{criterion} = {checks!r}, expected true or false")

    if isinstance(spec, Choice):
        if not isinstance(checks, str):
            raise ChecksError(
                f"{module}.{criterion} expects one label from "
                f"{sorted(spec.options)}, got {type(checks).__name__}")
        if checks not in spec.options:
            raise ChecksError(
                f"{module}.{criterion} = {checks!r}, not one of {sorted(spec.options)}")
        return spec.options[checks]

    if not isinstance(checks, Mapping):
        raise ChecksError(
            f"{module}.{criterion} expects an object of named checks, "
            f"got {type(checks).__name__}")

    unknown = set(checks) - set(spec)                          # type: ignore[arg-type]
    if unknown:
        raise ChecksError(f"{module}.{criterion} has unknown checks: {sorted(unknown)}")

    total = 0
    for name, item in spec.items():                            # type: ignore[union-attr]
        value = checks.get(name)
        if isinstance(item, Choice):
            if not isinstance(value, str) or value not in item.options:
                raise ChecksError(
                    f"{module}.{criterion}.{name} = {value!r}, "
                    f"not one of {sorted(item.options)}")
            total += item.options[value]
            continue
        # A MISSING check is FALSE, not an error and not full marks. The rubric
        # has one tie-break and this is it: what was not observed did not
        # happen. A model that omits a key must never be rewarded for it.
        if value is True:
            total += item.points
        elif value not in (False, None):
            raise ChecksError(
                f"{module}.{criterion}.{name} = {value!r}, expected true or false")
    return total
