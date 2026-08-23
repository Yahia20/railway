"""Score arithmetic and validation for the agent-quality rubric.

The model returns judgements. It does NOT return the final number: the weighted
score is recomputed here from the module scores, and the model's own arithmetic
is discarded. LLMs are reliable at "was there a price in this message" and
unreliable at multiplying by 0.25 and summing five terms — and a score that
drifts between runs of the same prompt destroys trust in the whole system.

Rubric version 1.0.0.
"""
from __future__ import annotations

import re
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

# stage_reached values that assert the customer never got as far as bargaining.
# `offer_presented` is in both sets: a price was stated, nobody has pushed back
# on it yet.
PRE_NEGOTIATION_STAGES = frozenset({"reception", "offer_presented"})

# The three objections that cannot exist before an offer has been argued over.
# `unavailable_service_objection` is deliberately NOT one of them; the reasoning
# is in validate_stage_consistency, and it is the whole point of PR2 round 4.
NEGOTIATION_GATED_OBJECTIONS = (
    "price_objection",
    "competitor_objection",
    "thinking_time_objection",
)


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


def compute(modules: dict[str, dict[str, Any]],
            ungradeable_modules: Any = ()) -> ScoreResult:
    """Recompute every module score and the weighted final from the breakdowns.

    `ungradeable_modules` names modules whose breakdown exists but cannot be
    believed — every deduction in them was discarded for want of evidence, so
    restoring them all to their caps would publish a perfect module score on
    the strength of nothing at all. Those modules score `None`: dropped from
    the numerator AND the denominator, exactly like a criterion that never
    arose. See `judge.run_pass2` for who decides membership.
    """
    ungradeable = set(ungradeable_modules or ())
    warnings: list[str] = []
    scores: dict[str, float | None] = {}

    for key in WEIGHTS:
        block = modules.get(key) or {}
        breakdown = block.get("breakdown") or {}
        if key in ungradeable:
            scores[key] = None
            continue
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


_AR_FOLD = str.maketrans({
    # alef family, ya/alef-maqsura, ta-marbuta/ha — the spelling axes ASR is
    # least consistent on. Folding both sides lets a genuine quote survive a
    # one-glyph transcription drift while a fabricated one still fails.
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ة": "ه",
    "ـ": None,  # tatweel
})
_AR_DIACRITICS = dict.fromkeys(map(ord, "ًٌٍَُِّْ"), None)


def _fold_arabic(text: str) -> str:
    return " ".join(text.translate(_AR_FOLD).translate(_AR_DIACRITICS).split())


VALIDATOR_VERSION = "span-v2"

# ── transcript furniture ────────────────────────────────────────────────────
# A rendered transcript carries scaffolding that nobody said: `[04:00]` segment
# markers and `AGENT:` / `CUSTOMER:` speaker labels. The renderer inserts them
# between words that WERE spoken contiguously, so a genuinely verbatim quote
# spanning two rendered segments contains no marker and therefore did not match
# the haystack, which still did. Day 13, `36c6d304`: 190 characters of real,
# contiguous agent speech rejected as fabricated because a `[04:00]` had been
# inserted into the middle of it.
#
# So both sides are normalised before matching: the marker is removed from the
# haystack and from the quote. This makes the validator strictly more
# permissive about RENDERING and no more permissive about CONTENT — a
# fabricated quote is still fabricated, and `[[ASR_GAP]]` is still a hard
# boundary that no quote may cross (see `conversation_spans`).
_TIMESTAMP = re.compile(r"\[\d{1,3}:\d{2}(?::\d{2})?\]")
_SPEAKER_LABEL = re.compile(
    r"(?:AGENT|CUSTOMER|BOT|SYSTEM|SPEAKER(?:_\d+)?)\s*:", re.IGNORECASE
)


def strip_transcript_furniture(text: str) -> str:
    """Timestamps and speaker labels out, whitespace runs collapsed.

    The one definition of "what was actually said", shared by quote matching
    and by the speech gate in `main.py`. Two definitions would mean a call the
    gate calls empty and the validator calls quotable, or the reverse.
    """
    without = _SPEAKER_LABEL.sub(" ", _TIMESTAMP.sub(" ", text or ""))
    return " ".join(without.split())


def conversation_spans(conversation_text: str) -> tuple[list[str], list[str]]:
    """The conversation split into the spans a quote may be matched inside.

    `[[ASR_GAP]]` marks removed machine output, so the text on either side of it
    may be two unrelated moments of the call. Splitting here is what stops a
    "verbatim" quote from being stitched across the seam — and it happens BEFORE
    furniture is stripped, so normalising timestamps can never dissolve that
    boundary. Returned twice: with furniture removed and whitespace flattened,
    and again with Arabic orthography folded.
    """
    spans = [s for s in (conversation_text or "").split("[[ASR_GAP]]") if s.strip()]
    flat = [strip_transcript_furniture(s) for s in spans]
    return flat, [_fold_arabic(s) for s in flat]


def quote_problem(quote: str | None, haystacks: list[str],
                  folded_haystacks: list[str]) -> str | None:
    """`None` when the quote is a genuine contiguous citation, else the reason.

    The single place that decides whether a quote is real. Pass-1 field
    validation, pass-2 evidence warnings and criterion-level evidence
    enforcement all route through here, so a quote can never be acceptable to
    one caller and fabricated to another.
    """
    text = (quote or "").strip()
    if not text:
        return "empty quote"
    if "ASR_GAP" in text:
        return ("quote contains the ASR gap marker, "
                "which is not speech and cannot be cited")
    flat = strip_transcript_furniture(text)
    if not flat:
        return ("quote is only transcript furniture (a timestamp or a speaker "
                "label), not speech")
    if any(flat in h for h in haystacks):
        return None
    if any(_fold_arabic(flat) in h for h in folded_haystacks):
        return None
    return f"quote not found in conversation: {text[:60]!r}"


def quote_is_valid(quote: str | None, conversation_text: str) -> bool:
    """One-shot convenience wrapper. Prefer the span form in a loop."""
    return quote_problem(quote, *conversation_spans(conversation_text)) is None


def validate_evidence(payload: dict, conversation_text: str) -> list[str]:
    """Every quote in `evidence` must actually appear in the conversation.

    This is the cheapest available guard against a fabricated citation, and it
    is worth running on every evaluation: a hallucinated quote in a coaching
    report is worse than no report, because the agent can prove it wrong and
    then discounts every future score.

    Matching is exact first, then retried with Arabic orthography folded
    (hamza/alef forms, ya vs alef maqsura, ta marbuta vs ha, diacritics,
    tatweel) — ASR spells these inconsistently, and rejecting a real quote
    over a hamza teaches the judge that citing evidence is pointless.

    Call transcripts may contain [[ASR_GAP]] markers where contaminated or
    looped ASR output was removed. A quote must match inside ONE uninterrupted
    span: matching across a gap would accept a "quote" stitched together from
    two unrelated sentences that merely became adjacent after cleaning, and
    the marker itself is never quotable.
    """
    problems: list[str] = []
    haystacks, folded_haystacks = conversation_spans(conversation_text)

    for i, item in enumerate(payload.get("evidence") or []):
        reason = quote_problem((item or {}).get("quote"), haystacks, folded_haystacks)
        if reason:
            problems.append(f"evidence[{i}]: {reason}")
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


def validate_completeness(modules: dict[str, Any]) -> list[str]:
    """Every module, every `breakdown`, every criterion key must be present.

    `validate_nullability` only sees a criterion the model actually emitted:
    `breakdown.get(name, "missing")` treats an absent key as fine. Omitting the
    key is strictly better for a lenient judge than nulling it — same effect on
    the denominator, and no check fires. A response that silently drops
    `value_selling` scores Module 2 over three criteria instead of four and
    nobody can tell from the stored row.

    So absence is a contract violation with exactly the weight of an
    unjustified null: named in the re-ask, and fatal if it survives it.
    """
    problems: list[str] = []
    for module_key, caps in CRITERION_MAX.items():
        block = modules.get(module_key)
        if not isinstance(block, dict):
            problems.append(
                f"{module_key} is missing from `modules` — every module must be "
                f"present with a full breakdown, even when every criterion is null"
            )
            continue
        breakdown = block.get("breakdown")
        if not isinstance(breakdown, dict):
            problems.append(
                f"{module_key}.breakdown is missing or is not an object — it must "
                f"list all {len(caps)} criteria: {', '.join(caps)}"
            )
            continue
        for name, cap in caps.items():
            if name not in breakdown:
                problems.append(
                    f"{module_key}.{name} is missing from the breakdown — every "
                    f"criterion must be present, scored 0-{cap} or explicitly null"
                )
    return problems


def validate_stage_consistency(payload: dict, modules: dict[str, Any]) -> list[str]:
    """`stage_reached` must agree with what was actually scored. Two rules.

    **A stage that claims an offer, against a null offer.** The first real run
    reported `stage_reached: offer_presented` while nulling offer completeness
    *and* stating in its own notes that no offer was made. Two contradictory
    answers in one response means one of them is noise.

    **A pre-negotiation stage, against an objection that requires one.** Price,
    competitor and thinking-time objections are all pushback on something the
    agent said. None of them can exist before the agent said it, so a response
    that scores one of them at `reception` has contradicted itself.

    `unavailable_service_objection` is DELIBERATELY not inspected here, and the
    omission is the finding of PR2 round 4 rather than an oversight. That
    criterion fires on a request the agent refused, which needs no offer and no
    bargaining: the customer asks for something the agency does not do and is
    turned away at the door, and the call ends at `reception`. Stage-gating it
    is exactly what made `174898da` a false negative for two rounds — the judge
    generalised the three-objection rule to all four and dropped Module 3 on
    stage grounds before the exclusion list was ever read. Encoding the same
    generalisation here would re-ask the model until it reproduced the defect,
    which is worse than not checking: a re-ask is a demand, not a question.

    The constraint that DOES hold on that criterion at every stage —
    `refusal_check.agent_refused_or_declared_unavailable` true if and only if
    `unavailable_service_objection` is numeric — is enforced by
    `validate_refusal_link()`, and it is enforced there precisely because it is
    the check that does not depend on how far the conversation got.
    """
    problems = []
    stage = payload.get("stage_reached")

    if stage in STAGES_IMPLYING_AN_OFFER:
        completeness = ((modules.get("module2_offer") or {}).get("breakdown") or {}) \
            .get("offer_completeness", "missing")
        if completeness is None:
            problems.append(
                f"stage_reached='{stage}' claims an offer was presented, but "
                f"module2_offer.offer_completeness is null (no offer). Pick one."
            )

    if stage in PRE_NEGOTIATION_STAGES:
        breakdown = (
            (modules.get("module3_objections") or {}).get("breakdown") or {}
        )
        for criterion in NEGOTIATION_GATED_OBJECTIONS:
            if breakdown.get(criterion) is not None:
                problems.append(
                    f"module3_objections.{criterion} is non-null, so "
                    "stage_reached must be negotiation or later"
                )

    return problems


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


def validate_refusal_link(modules: dict[str, Any]) -> list[str]:
    """`refusal_check` and `unavailable_service_objection` must agree.

    The prompt declares the link a contract violation in words — "setting the
    flag true and the objection null is a contract violation" — and then nothing
    checked it. Both directions are wrong and both were observed:

    - flag true, objection `null`: the model inventoried a categorical refusal
      in Step 0 and then dropped Module 3 out of the grade entirely, which is
      25% of the weight removed by the same response that found the failure.
    - flag false, objection scored: the objection was fired without the trigger
      the gates require, so the agent is being marked on an event the model
      itself says did not happen.

    Either way the response contradicts itself and one of the two answers is
    noise; which one cannot be decided here, so it is re-asked, not repaired.
    """
    block = modules.get("module3_objections") or {}
    check = block.get("refusal_check")
    if not isinstance(check, dict):
        return []

    refused = check.get("agent_refused_or_declared_unavailable")
    if not isinstance(refused, bool):
        return []

    breakdown = block.get("breakdown") or {}
    scored = breakdown.get("unavailable_service_objection")

    if refused and scored is None:
        return [
            "module3_objections.refusal_check.agent_refused_or_declared_unavailable "
            "is true, so unavailable_service_objection must carry a number "
            "(0, 15 or 25) — it is null. Pick one: either the agent refused and "
            "the objection is scored, or there was no categorical refusal and "
            "the flag is false."
        ]
    if not refused and scored is not None:
        return [
            "module3_objections.unavailable_service_objection is scored "
            f"{scored} but refusal_check.agent_refused_or_declared_unavailable "
            "is false. An objection cannot be scored without the refusal that "
            "triggers it. Pick one."
        ]
    return []


# Violations that make the whole evaluation untrustworthy rather than merely
# lenient: the response asserts two contradictory facts about the same
# conversation, a number that cannot be scored at all, or a denominator that is
# missing pieces of the rubric. After the one correction re-ask these end the
# evaluation with `contract_failed` instead of producing a score built on a
# self-contradiction or on a silently shortened rubric.
#
# Nullability and completeness used to be "soft": named in the re-ask, then
# scored anyway if the model repeated them. That is the leniency path — an
# unjustified null and an omitted key both shrink the denominator, so a response
# that keeps them after being told exactly which ones is a response whose score
# is inflated by a known amount. Publishing it as a number with a warning
# attached means the warning lives in a jsonb blob and the number lives in every
# dashboard.
def hard_violations(payload: dict, modules: dict[str, Any]) -> list[str]:
    return (validate_completeness(modules)
            + validate_nullability(modules)
            + validate_ranges(modules)
            + validate_stage_consistency(payload, modules)
            + validate_refusal_link(modules))


def contract_violations(payload: dict, modules: dict[str, Any]) -> list[str]:
    """Every check that means the response should be re-asked, not repaired.

    Identical to `hard_violations` by design: anything worth a correction re-ask
    is worth refusing to score if the correction does not fix it. The two names
    are kept apart because they are asked at different moments — before the
    re-ask and after it — and collapsing them would hide which one a caller
    means.
    """
    return hard_violations(payload, modules)


# ── criterion-level evidence enforcement ────────────────────────────────────
# The prompt has always said an unsupported deduction is discarded and the
# points restored. The code only ever appended a warning, so the deduction kept
# its points off the score and the "restored" points existed in prose alone.
# Worse, the only check that looked at evidence at all asked whether the MODULE
# was cited anywhere — one valid quote about the greeting excused every other
# deduction in Module 1.


def evidence_criterion_key(module_field: Any, criterion_field: Any) -> tuple[str, str] | None:
    """The (module, criterion) an evidence entry defends, or None if it names none.

    Only two spellings are accepted, both of which say the same thing twice:

    - the bare criterion — `greeting`, ` Greeting `;
    - the criterion prefixed with the entry's OWN module — `module1_reception.greeting`.

    Anything else is refused rather than salvaged. The previous version took the
    text after the final dot, so an entry declaring `module="module1_reception"`
    and `criterion="module2_offer.greeting"` — or `"garbage.greeting"` — rescued
    Module 1's greeting deduction while contradicting itself about where the
    quote came from. A citation whose two halves disagree is not evidence for
    either half, and suffix-matching turned that contradiction into a free pass.
    """
    if not isinstance(criterion_field, str):
        return None
    module = str(module_field or "").strip().lower()
    text = criterion_field.strip().lower()
    if not text:
        return None
    if "." in text:
        prefix, _, name = text.rpartition(".")
        # Exact `<same-module>.<criterion>` only: no other prefix, no deeper
        # dot path, and never a prefix naming a module other than this entry's.
        if not name or prefix != module:
            return None
    else:
        name = text
    return module, name


def unsupported_criteria(payload: dict, modules: dict[str, Any],
                        conversation_text: str) -> list[dict[str, Any]]:
    """Every below-cap criterion whose exact quote is missing or fake. Mutates nothing.

    The read-only half of evidence enforcement, split out so the judge can ask
    the question *before* deciding what to do about the answer. Restoring a
    deduction is the last resort, not the first move: the model gets one chance
    to produce the anchor or withdraw the finding, and that chance requires
    knowing which criteria are unsupported without having already overwritten
    them.

    One record per unsupported finding: which criterion, why, what the model
    gave it, what it would be restored to, and the quote that failed if there
    was one.
    """
    haystacks, folded = conversation_spans(conversation_text)

    # (module, criterion) -> the quotes offered for it, in output order.
    offered: dict[tuple[str, str], list[str]] = {}
    for item in payload.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        key = evidence_criterion_key(item.get("module"), item.get("criterion"))
        if key is None:
            continue                          # contradictory or unnamed: not evidence
        offered.setdefault(key, []).append(item.get("quote"))

    unsupported: list[dict[str, Any]] = []
    for module_key, caps in CRITERION_MAX.items():
        block = modules.get(module_key)
        if not isinstance(block, dict):
            continue
        breakdown = block.get("breakdown")
        if not isinstance(breakdown, dict):
            continue

        for criterion, cap in caps.items():
            value = breakdown.get(criterion)
            if value is None or criterion not in breakdown:
                continue                      # null is the not-applicable path
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue                      # validate_ranges owns this
            if value >= cap:
                continue                      # nothing was deducted

            quotes = offered.get((module_key.lower(), criterion), [])
            reasons = [quote_problem(q, haystacks, folded) for q in quotes]
            if any(r is None for r in reasons):
                continue                      # at least one quote holds up

            if not quotes:
                reason, quote = "no evidence cited for this criterion", None
            else:
                reason, quote = reasons[0], quotes[0]

            unsupported.append({
                "module": module_key,
                "criterion": criterion,
                "reason": reason,
                "model_score": value,
                "restored_to": cap,
                "quote": quote,
            })
    return unsupported


# What the correction re-ask tells the model to do about each unsupported
# criterion. Both branches are legitimate answers and the model is told so: a
# real omission has an anchor it can quote, and one it invented does not.
_EVIDENCE_FIX = (
    "Either add one valid evidence entry naming this exact module and criterion "
    "(for an omission, use the omission-anchor rule: quote the customer turn that "
    "made the action necessary, or the contiguous agent turn or closing where it "
    "should have occurred, and state the absent action in `effect`) — or restore "
    "the criterion to its cap of {cap}."
)


def criterion_evidence_problems(payload: dict, modules: dict[str, Any],
                                conversation_text: str) -> list[str]:
    """The unsupported deductions, phrased as problems for the correction re-ask.

    Non-mutating on purpose. Enforcement used to run only *after* the single
    re-ask, so the re-ask never mentioned evidence and the judge was never given
    the chance to support or withdraw a finding before its points were handed
    back. Restoration then happened silently and en masse — on the day-13 replay,
    82 omission findings restored without the model being asked once.
    """
    return [
        f"{u['module']}.{u['criterion']} is scored {u['model_score']} "
        f"(below its cap of {u['restored_to']}) but {u['reason']}. "
        + _EVIDENCE_FIX.format(cap=u["restored_to"])
        for u in unsupported_criteria(payload, modules, conversation_text)
    ]


def deducted_criteria(modules: dict[str, Any]) -> dict[str, set[str]]:
    """Every below-cap, numeric criterion, per module. Mutates nothing.

    Must be taken BEFORE `enforce_criterion_evidence` runs, because enforcement
    rewrites the discarded criteria to their caps and the question "did this
    module have any supported finding left" becomes unanswerable afterwards.
    """
    found: dict[str, set[str]] = {}
    for module_key, caps in CRITERION_MAX.items():
        block = modules.get(module_key)
        if not isinstance(block, dict):
            continue
        breakdown = block.get("breakdown")
        if not isinstance(breakdown, dict):
            continue
        for criterion, cap in caps.items():
            value = breakdown.get(criterion)
            if value is None or criterion not in breakdown:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value < cap:
                found.setdefault(module_key, set()).add(criterion)
    return found


def ungroundable_modules(modules_before: dict[str, set[str]],
                         rejected: list[dict[str, Any]]) -> list[str]:
    """Modules in which EVERY deduction was discarded for want of evidence.

    Isolated unsupported findings still restore to their caps: one unanchored
    deduction among four supported ones is a judge that over-reached on one
    criterion, and the agent keeps those points. But a module in which nothing
    the judge said could be grounded is not a module the judge graded — and
    restoring all of it to full marks turns a total grounding failure into
    "Excellent". Day 13, `e5ab9937`: one mistranslated quote offered for six
    deductions across two modules, and a 34-second call scored 100.

    `None` for that module instead: dropped from numerator and denominator, so
    the answer is "we cannot say" rather than "perfect".
    """
    discarded: dict[str, set[str]] = {}
    for record in rejected:
        discarded.setdefault(str(record.get("module")), set()).add(
            str(record.get("criterion")))
    return sorted(
        module for module, deductions in modules_before.items()
        if deductions and deductions <= discarded.get(module, set())
    )


def enforce_criterion_evidence(payload: dict, modules: dict[str, Any],
                               conversation_text: str) -> list[dict[str, Any]]:
    """Discard every below-cap criterion whose exact quote is missing or fake.

    Mutates `modules` in place — the discarded criterion is set to its cap, NOT
    to null. Null would drop it from the denominator and quietly inflate the
    module; the cap says what the rule says: with no evidence there is no
    finding, so the agent keeps the points.

    Evidence must name the same criterion it defends. Module-level matching is
    what made this check toothless, and a quote proving a weak greeting says
    nothing about whether the agent asked for the travel dates.

    Runs only after the correction re-ask has already named these criteria to
    the model. Returns one record per discarded finding, for the caller to store
    and for a human to audit.
    """
    rejected = unsupported_criteria(payload, modules, conversation_text)
    for record in rejected:
        modules[record["module"]]["breakdown"][record["criterion"]] = record["restored_to"]
    return rejected
