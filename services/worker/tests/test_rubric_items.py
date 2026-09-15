"""The rubric's closed sets, and the prompt that asks for them.

Every test here exists because the guarantee v7 sells — same facts, same score,
on any model, on any run — is only as good as the agreement between three
things that live in different files:

    rubric_items.ITEMS       what a check is worth
    scoring.CRITERION_MAX    what a criterion is worth
    pass2_agent_quality_v7   what the model is asked

Any two of them can be changed without the third noticing. These tests are what
notices.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.evaluate import judge, scoring
from app.evaluate.rubric_items import (
    ITEMS, Choice, ChecksError, Flag, legal_values, score_criterion,
)

PROMPT = (Path(judge.__file__).parent.parent / "prompts" / judge.PASS2_PROMPT_FILE
          ).read_text(encoding="utf-8")


# ── the table agrees with the rubric it encodes ─────────────────────────────

def test_every_criterion_in_the_rubric_has_items():
    """A criterion with a cap but no items would silently keep the old
    free-number behaviour while every other criterion is pinned — the worst
    possible state, because the report would look uniform and not be."""
    assert {m: set(c) for m, c in ITEMS.items()} == {
        m: set(c) for m, c in scoring.CRITERION_MAX.items()
    }


@pytest.mark.parametrize("module,criterion", [
    (m, c) for m, crits in ITEMS.items() for c in crits
])
def test_items_sum_to_the_criterion_cap(module, criterion):
    """All items true must be exactly the cap — never more, never less.

    Less means an agent who did everything the rubric asks cannot reach full
    marks. More means the cap is not the cap, and `validate_ranges` starts
    rejecting perfect answers.
    """
    assert max(legal_values(module, criterion)) == scoring.CRITERION_MAX[module][criterion]


@pytest.mark.parametrize("module,criterion", [
    (m, c) for m, crits in ITEMS.items() for c in crits
])
def test_zero_is_always_reachable(module, criterion):
    """An agent who did none of it scores 0, not the lowest partial credit."""
    assert 0 in legal_values(module, criterion)


def test_no_criterion_can_produce_a_value_outside_its_cap():
    for module, crits in ITEMS.items():
        for criterion in crits:
            cap = scoring.CRITERION_MAX[module][criterion]
            assert all(0 <= v <= cap for v in legal_values(module, criterion))


# ── the tie-break: what is not observed did not happen ──────────────────────

def test_an_omitted_check_is_false_not_full_marks():
    """Rule 1 of the Determinism Contract, enforced rather than requested.

    A model that drops a key must never gain by it. If an omission defaulted to
    true, the cheapest way to a high score would be to emit less JSON.
    """
    assert score_criterion("module1_reception", "greeting", {}) == 0
    assert score_criterion("module1_reception", "greeting",
                           {"used_a_greeting": True}) == 5


def test_a_false_check_earns_nothing():
    assert score_criterion("module2_offer", "offer_completeness", {
        "total_price": True, "package_contents": False,
        "hotel_name_and_rating": False, "travel_dates": False,
        "booking_and_cancellation_terms": None,
    }) == 5


def test_an_unknown_check_name_is_refused():
    """Not ignored. A misspelled key that scored 0 silently would look exactly
    like an agent who did not do the thing."""
    with pytest.raises(ChecksError, match="unknown checks"):
        score_criterion("module1_reception", "greeting",
                        {"called_customer_by_name": True, "said_hello": True})


def test_a_number_where_a_boolean_belongs_is_refused():
    with pytest.raises(ChecksError, match="expected true or false"):
        score_criterion("module1_reception", "greeting",
                        {"called_customer_by_name": 10})


def test_an_unlisted_label_is_refused():
    with pytest.raises(ChecksError, match="not one of"):
        score_criterion("module5_closing", "payment_request", "sort_of")


def test_the_menu_itself_is_refused_as_a_value():
    """The prompt prints `"direct | indirect | never"` as a menu. A model that
    echoes the menu instead of choosing from it must fail loudly — it is the
    single most likely schema mistake and it means nothing was decided."""
    with pytest.raises(ChecksError):
        score_criterion("module5_closing", "payment_request", "direct | indirect | never")


# ── the resolver, end to end ────────────────────────────────────────────────

def _module(checks):
    return {"module1_reception": {"weight": 0.15, "checks": checks}}


def test_checks_beat_the_number_the_model_typed_beside_them():
    """The whole point. If `breakdown` could win, nothing changed in v7."""
    block = {"weight": 0.15,
             "breakdown": {"greeting": 22, "understanding_confirmation": 19,
                           "missing_info_request": 7, "next_step_transition": 3},
             "checks": {"greeting": {"called_customer_by_name": True,
                                     "introduced_self_by_name": True,
                                     "used_a_greeting": True}}}
    resolved, problems = scoring.resolve_checks("module1_reception", block)
    assert problems == []
    assert resolved["greeting"] == 25
    # criteria with no checks keep what the model gave — a v6 response still scores
    assert resolved["understanding_confirmation"] == 19


def test_a_null_criterion_survives_the_resolver_as_null():
    """Rule 2 of CLAUDE.md at its narrowest point: a situation that never arose
    must not arrive at the scorer as a zero."""
    block = {"weight": 0.25, "checks": {"offer_completeness": None}}
    resolved, problems = scoring.resolve_checks("module2_offer", block)
    assert problems == []
    assert resolved["offer_completeness"] is None


def test_malformed_checks_are_reported_not_swallowed():
    block = {"weight": 0.15, "checks": {"greeting": "excellent"}}
    _, problems = scoring.resolve_checks("module1_reception", block)
    assert problems and "greeting" in problems[0]


def test_a_response_with_no_checks_is_untouched():
    """v6 rows and any backfill that replays them must score identically."""
    block = {"weight": 0.15, "breakdown": {"greeting": 15}}
    resolved, problems = scoring.resolve_checks("module1_reception", block)
    assert problems == []
    assert resolved == {"greeting": 15}


# ── illegal totals are refused whether or not checks were sent ──────────────

def test_a_score_the_rubric_cannot_add_up_to_is_a_contract_violation():
    """18 out of 25 on a 10/10/5 criterion is the drift this release removes."""
    modules = {"module1_reception": {"breakdown": {"greeting": 18}}}
    problems = scoring.validate_legal_values(modules)
    assert any("greeting" in p and "18" in p for p in problems)


@pytest.mark.parametrize("value", [0, 5, 10, 15, 20, 25])
def test_every_reachable_greeting_total_is_accepted(value):
    modules = {"module1_reception": {"breakdown": {"greeting": value}}}
    assert scoring.validate_legal_values(modules) == []


def test_null_is_not_an_illegal_value():
    modules = {"module2_offer": {"breakdown": {"offer_completeness": None}}}
    assert scoring.validate_legal_values(modules) == []


def test_illegal_values_reach_the_re_ask():
    modules = {"module1_reception": {"breakdown": {"greeting": 18}}}
    violations = scoring.contract_violations({"stage_reached": "reception"}, modules)
    assert any("greeting" in v for v in violations)


# ── the prompt asks for exactly what the table counts ───────────────────────

def test_the_prompt_names_every_check_the_table_knows():
    """The drift this catches: renaming a check in code, shipping, and having
    the model keep sending the old key — which then reads as `false` under Rule
    1 and quietly zeroes a criterion for every conversation."""
    missing = []
    for module, crits in ITEMS.items():
        for criterion, spec in crits.items():
            if f"`{criterion}`" not in PROMPT and f'"{criterion}"' not in PROMPT:
                missing.append(f"{module}.{criterion}")
                continue
            if isinstance(spec, Flag):
                continue
            names = spec.options if isinstance(spec, Choice) else spec
            for name in names:
                if name not in PROMPT:
                    missing.append(f"{module}.{criterion}.{name}")
    assert missing == []


def test_the_prompt_asks_for_no_criterion_the_table_cannot_count():
    """The mirror: a check named in the prompt but absent from the table would
    be an answer nobody reads, and the criterion would score as if the agent
    never did it."""
    known = {n for crits in ITEMS.values() for c, spec in crits.items()
             for n in ([c] + ([] if isinstance(spec, Flag)
                              else list(spec.options if isinstance(spec, Choice) else spec)))}
    block = PROMPT[PROMPT.index("THE OBSERVATION SHEET"):
                   PROMPT.index("WHAT HAPPENS TO YOUR ANSWERS")]
    quoted = set()
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("`") and line.count("`") >= 2:
            quoted.add(line.split("`")[1])
        elif ":  true / false" in line or ":  one label" in line:
            quoted.add(line.split(":")[0].strip())
    assert quoted - known == set()


def test_the_prompt_does_not_show_the_model_the_point_values():
    """A question whose price is known is a question that can be answered
    strategically. The Observation Sheet prints the questions and no numbers."""
    sheet = PROMPT[PROMPT.index("THE OBSERVATION SHEET"):
                   PROMPT.index("WHAT HAPPENS TO YOUR ANSWERS")]
    priced = re.findall(r"\d+\s*(?:pts|points|marks)", sheet)
    assert priced == []
    # nor a bare cap beside a criterion: "out of 25" is the same leak
    assert not re.search(r"out of\s*\d", sheet)


def test_the_prompt_forbids_the_model_from_emitting_breakdown():
    assert "Do not emit `breakdown`" in PROMPT


def test_the_prompt_carries_the_six_rules():
    for rule in ("RULE 1 — THE DEFAULT IS THE ONE THAT AWARDS NOTHING",
                 "RULE 2 — NO QUOTE, NO `true`",
                 "RULE 3 — READ THE WORDS, NOT THE CONVERSATION",
                 "RULE 4 — EACH QUESTION IS ANSWERED ALONE",
                 "RULE 5 — `null` MEANS THE QUESTION COULD NOT BE ASKED",
                 "RULE 6 — YOUR ARITHMETIC IS DISCARDED"):
        assert rule in PROMPT, rule


def test_the_prompt_translates_the_carried_over_point_language():
    """v7 keeps v6's calibrated passages verbatim, and those still say "0, 15 or
    25". Without the bridging note the model is handed two vocabularies and one
    of them asks for a number."""
    assert "READING THE OLDER PASSAGES BELOW" in PROMPT
    assert "must NOT be null" in PROMPT


# ── the bug the first real v7 run found ─────────────────────────────────────

def _v7_response():
    """A CORRECT v7 response: every observation answered, no `breakdown`.

    This is what the model actually returned on the first live run against a
    real conversation. The prompt forbids `breakdown`, so a v7 response that
    carries one is the broken case, not this.
    """
    def group(module, criterion, value):
        spec = ITEMS[module][criterion]
        if isinstance(spec, Flag):
            return False
        if isinstance(spec, Choice):
            return sorted(spec.options)[0]
        return {name: (value if isinstance(item, Flag) else sorted(item.options)[0])
                for name, item in spec.items()}

    return {"modules": {
        module: {"weight": scoring.WEIGHTS[module],
                 "checks": {c: group(module, c, True) for c in criteria}}
        for module, criteria in ITEMS.items()
    }}


def test_a_correct_v7_response_is_not_a_contract_violation():
    """THE REGRESSION. `validate_completeness` was written for v6 and demands a
    `breakdown` object. v7 tells the model not to send one. So a perfect
    response — thirty observations, every quote valid — was rejected five times
    over as "breakdown is missing", re-asked once, rejected again, and stored
    as `contract_failed` with a null score. Measured on a real conversation
    through OpenRouter before this test existed.
    """
    payload = _v7_response()
    problems = scoring.materialise_checks(payload["modules"])
    assert problems == []
    violations = scoring.contract_violations(payload, payload["modules"])
    assert not [v for v in violations if "breakdown is missing" in v], violations


def test_materialise_fills_breakdown_for_every_module():
    """Everything downstream — the validators, the stored row, the report —
    reads `breakdown`. It must exist before any of them look."""
    payload = _v7_response()
    scoring.materialise_checks(payload["modules"])
    for module, criteria in ITEMS.items():
        breakdown = payload["modules"][module]["breakdown"]
        assert set(breakdown) == set(criteria), module
        assert all(isinstance(v, int) for v in breakdown.values()), module


def test_materialise_is_idempotent():
    """`compute()` resolves checks again. Running it twice must not double a
    score or drift one."""
    payload = _v7_response()
    scoring.materialise_checks(payload["modules"])
    first = {m: dict(b["breakdown"]) for m, b in payload["modules"].items()}
    scoring.materialise_checks(payload["modules"])
    assert {m: dict(b["breakdown"]) for m, b in payload["modules"].items()} == first


def test_a_v6_response_is_left_alone():
    """No `checks` means nothing to materialise. A replay of a stored v6 row
    must score exactly as it did."""
    payload = {"modules": {"module1_reception": {"breakdown": {"greeting": 15}}}}
    assert scoring.materialise_checks(payload["modules"]) == []
    assert payload["modules"]["module1_reception"]["breakdown"] == {"greeting": 15}


# ── ambiguities the prompt must settle, because the model otherwise settles
#    them differently on different runs ─────────────────────────────────────

def test_the_prompt_decides_what_counts_as_a_greeting():
    """MEASURED, not hypothetical. deepseek-chat-v3.1 at temperature 0 answered
    `used_a_greeting` true on one run and false on the next, on the single word
    "اهلين" — five points of swing with nothing else different between them.

    Rule 1 says doubt resolves down, but doubt the rubric could have removed
    should not reach Rule 1 at all: the tie-break is a floor, not a substitute
    for a decision. Every ambiguity this demo turns up gets decided here.
    """
    assert "اهلين" in PROMPT
    assert "A GREETING IS A GREETING HOWEVER SHORT OR INFORMAL" in PROMPT
    # and the other half of the decision: what does NOT count
    for counterexample in ("تمام", "أوكي", "أبشر"):
        assert counterexample in PROMPT, counterexample


def test_the_prompt_decides_when_a_followup_was_owed():
    """The biggest measured drift in the rubric, and the one worth the most.

    Same model, temperature 0, same conversation, same follow-up block: one run
    nulled Module 4 and the next scored it 0. Twenty points of weight moving on
    an unstated rule. The queue only ever hands the judge a thread that has been
    silent for three days, so "nothing was owed yet" is never true — but the
    prompt did not say so, and the model filled the gap differently each time.
    """
    assert "THREE STATES, AND ONLY THE FIRST IS `null`" in PROMPT
    assert "WHEN A FOLLOW-UP WAS OWED IS NOT YOUR DECISION" in PROMPT
    # the specific case that produced the split: the customer promised to return
    assert "خليني" in PROMPT


def test_an_empty_chat_search_is_never_scored_as_a_missed_followup():
    """The agent may have PHONED. We would not see it.

    Only chat is searched — the telephone lane was removed from this system
    altogether — so an agent who called the customer and closed the sale
    produces exactly the same empty block as one who forgot them. Scoring that
    as zero punishes whoever works the phone hardest, for a gap in our data
    collection rather than anything they did.
    """
    assert "NO CHAT FOLLOW-UP RECORDED" in PROMPT
    assert "it is NOT the same as" in PROMPT or "NOT the same as" in PROMPT
    assert "invisible to this system" in PROMPT


def test_the_prompt_quotes_the_sentence_the_renderer_actually_emits():
    """The model matches on this string. If the renderer and the rubric word it
    differently, the model matches neither and falls back to guessing."""
    from app.evaluate import metrics
    rendered = metrics.followup_history_block(later_contacts=[])
    assert "NO CHAT FOLLOW-UP RECORDED" in rendered
    assert "NO CHAT FOLLOW-UP RECORDED" in PROMPT
    # and the renderer must carry the caveat, not only the prompt
    assert "phone call" in rendered


def test_every_check_measured_drifting_has_been_decided():
    """One section, not five scattered paragraphs.

    Each of these was observed disagreeing with ITSELF across two runs of the
    same model at temperature 0. Rule 1 (doubt resolves down) is the floor, but
    a check whose bar the rubric never states is not doubt to the model — it is
    a bar the model sets, and it sets it differently each time. The fix is to
    state the bar.
    """
    assert "WHAT COUNTS — the checks that have actually drifted, decided" in PROMPT
    for check in ("answered_the_question_asked",
                  "dates_known_or_asked",
                  "traveler_count_known_or_asked",
                  "used_a_greeting",
                  "used_persuasion"):
        assert check in PROMPT, check
    # and the catch-all, so a check NOT on the list still has one answer
    assert "Rule 1 applies and the answer is `false`" in PROMPT


# ── evidence points the other way under v7 ──────────────────────────────────

CONV = ("[10:00] CUSTOMER: السلام عليكم، ابغى عرض لتركيا\n"
        "[10:19] AGENT: اهلين\n"
        "[10:19] AGENT: كم عدد الاشخاص؟\n")


def _module2(value_selling_all_false=True, quote=None):
    """module2 as a v7 response: attitude clean, value_selling all false."""
    modules = {"module2_offer": {"weight": 0.25, "checks": {
        "attitude": {"professional_language": "throughout",
                     "difficult_customer": "never_difficult",
                     "no_defeatist_language": True},
        "offer_completeness": None,
        "value_selling": {"stated_features": not value_selling_all_false,
                          "connected_to_need": False, "used_persuasion": False},
        "alternative_offer": None,
    }}}
    payload = {"modules": modules}
    if quote is not None:
        payload["evidence"] = [{"module": "module2_offer", "criterion": "value_selling",
                                "quote": quote}]
    scoring.materialise_checks(modules)
    return payload, modules


def test_a_false_never_needs_a_quote():
    """THE REGRESSION, measured on a real run.

    A model correctly answered all three `value_selling` checks false on an
    agent who sold nothing, attached a clumsy quote joining three messages to
    say so, and the quote was rejected. The v6 machinery read that as "an
    unsupported deduction", made the whole module ungroundable, nulled a module
    whose `attitude` had scored a clean 25, and dropped the conversation below
    the 40% floor — for reporting, correctly, that something did not happen.
    """
    payload, modules = _module2(quote="اهلين\nكم عدد الاشخاص؟\nتمام ابشر")
    assert scoring.unsupported_criteria(payload, modules, CONV) == []
    problems = scoring.unquotable_positives(payload, modules, CONV)
    assert [p["criterion"] for p in problems] == []


def test_an_unquotable_true_is_reduced_not_restored():
    """Rule 2 enforced. The only safe direction for an unproven claim is down:
    the worst case is an agent who did something good and whose judge could not
    quote it, and that costs points rather than inventing them."""
    payload, modules = _module2(value_selling_all_false=False, quote="words never said")
    problems = scoring.unquotable_positives(payload, modules, CONV)
    assert len(problems) == 1
    assert problems[0]["criterion"] == "value_selling"
    assert problems[0]["model_score"] == 10
    assert problems[0]["reduced_to"] == 0
    scoring.apply_unquotable_positives(modules, problems)
    assert modules["module2_offer"]["breakdown"]["value_selling"] == 0


def test_a_valid_quote_keeps_the_observation():
    payload, modules = _module2(value_selling_all_false=False, quote="كم عدد الاشخاص؟")
    assert scoring.unquotable_positives(payload, modules, CONV) == []


def test_a_choice_is_reduced_to_its_lowest_label_not_to_zero():
    """`difficult_customer` bottoms out at 0 but `timing` bottoms out at 0 via
    "never" — the floor is whatever the closed list can actually produce, which
    is not always what a bare 0 would mean."""
    modules = {"module5_closing": {"weight": 0.15, "checks": {
        "payment_request": "direct", "next_steps_confirmation": "none",
        "thank_you": "none", "booking_steps": "none",
        "service_review_request": False,
    }}}
    payload = {"modules": modules}
    scoring.materialise_checks(modules)
    problems = scoring.unquotable_positives(payload, modules, CONV)
    assert [p["criterion"] for p in problems] == ["payment_request"]
    assert problems[0]["reduced_to"] == 0


def test_v6_modules_still_get_the_v6_rule():
    """A stored v6 response has no `checks`, so the deduction-based rule must
    still apply to it exactly as before."""
    modules = {"module1_reception": {"breakdown": {"greeting": 5}}}
    payload = {"modules": modules}
    assert scoring.unquotable_positives(payload, modules, CONV) == []
    assert [u["criterion"] for u in
            scoring.unsupported_criteria(payload, modules, CONV)] == ["greeting"]


def test_the_correction_names_both_kinds_of_anchoring_problem():
    payload, modules = _module2(value_selling_all_false=False, quote="not in there")
    problems = scoring.criterion_evidence_problems(payload, modules, CONV)
    assert any("Rule 2" in p for p in problems)


def test_the_absence_exemption_is_narrow_and_justified():
    """One criterion, and it earns it.

    `attitude`'s three top answers are "no unprofessional turn", "the customer
    was never difficult" and "no defeatist language". All three assert that
    something is NOT in the conversation, and an absence has no words to quote.
    Every other criterion asks whether the agent DID something, which either
    has words or does not.

    This list must not grow to silence an inconvenient reduction. If it does,
    Rule 2 stops being enforced anywhere it is uncomfortable — which is
    everywhere that matters.
    """
    from app.evaluate.rubric_items import ABSENCE_CRITERIA
    assert ABSENCE_CRITERIA == frozenset({"module2_offer.attitude"})
    for entry in ABSENCE_CRITERIA:
        module, criterion = entry.split(".")
        assert criterion in ITEMS[module], entry
