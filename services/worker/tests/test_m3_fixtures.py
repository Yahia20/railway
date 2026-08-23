"""The M3 fixture file itself — schema, coverage, and gate clearance.

`unavailable_service_objection` is decided by the PROMPT, not by code, so these
cases cannot be asserted without calling the model: there is nothing in this
repository that can be given a transcript and asked whether an objection arose.
`scripts/compare_day.py --m3-fixtures` runs them against the live judge.

What CAN be checked offline is that the file is usable when that run happens —
that every day-13 flip is covered, that each case carries an assertable
expectation, and that no case would be refused by the speech gate before
reaching the model, which would silently turn a regression suite into fourteen
skipped rows.

It also checks that the file contains no real transcript text. The repository is
public and the day-13 corpus is personal data; the guard is a length ceiling and
a phone-number scan, which is not proof, but it catches a paste.
"""
import json
import re
from pathlib import Path

import pytest

from app.evaluate import judge
from app.main import MIN_SCOREABLE_CHARS, spoken_content

FIXTURES = Path(__file__).parent / "fixtures" / "m3_unavailable_service_cases.json"

# The seven day-13 flips the review named: four wrong additions, three correct
# drops. All seven must stay covered — that is what makes this a regression file
# rather than a set of examples.
DAY13_FLIPS = {"0aa8273b", "596c957a", "bb02b597", "bb68337f",
               "1583ff50", "303c297b", "eacba4ad"}


@pytest.fixture(scope="module")
def spec():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cases(spec):
    return spec["cases"]


@pytest.fixture(scope="module")
def prompt():
    """The pass-2 prompt the judge is currently configured to send."""
    return (Path(__file__).parents[1] / "app" / "prompts"
            / judge.PASS2_PROMPT_FILE).read_text(encoding="utf-8")


def test_every_day13_flip_is_covered(cases):
    assert {c["stands_for"] for c in cases} >= DAY13_FLIPS


def test_ids_are_unique(cases):
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("field", ["id", "pattern", "input_type", "conversation",
                                   "expect", "why", "expected_stage", "fragments"])
def test_every_case_is_complete(cases, field):
    for case in cases:
        assert case.get(field), f"{case.get('id')} is missing {field}"


def test_every_case_declares_the_stage_it_reaches(cases):
    """Rule 1 of the round-4 fixture policy.

    A fixture that does not say how far its conversation got cannot be checked
    for the thing round 3 discovered: that `unavailable_service_objection` was
    being dropped on stage grounds before Module 3 was read at all. The
    declaration is what makes the other stage rules assertable, and it is
    printed next to every run so a stage disagreement is diagnosable from the
    report instead of from a separate probe.
    """
    stages = {"reception", "offer_presented", "negotiation",
              "follow_up", "closing_attempted", "deal_closed"}
    for case in cases:
        assert case["expected_stage"] in stages, case["id"]


def test_every_expectation_is_assertable(cases):
    """A case whose expectation cannot be checked mechanically is a comment."""
    for case in cases:
        assert case["expect"]["unavailable_service_objection"] in ("null", "scored")
        assert isinstance(case["expect"]["refusal_check"], bool)
        # The two must agree, or the case contradicts the prompt's hard link.
        fires = case["expect"]["unavailable_service_objection"] == "scored"
        assert case["expect"]["refusal_check"] is fires, case["id"]


def test_the_suite_has_positive_controls(cases):
    """Without one, a prompt that never fires the objection scores 7/7.

    The exclusion list is a list of things that must NOT trigger the criterion.
    A judge that has stopped triggering it at all passes every negative case and
    has broken the rubric, so at least one case must require it to fire.
    """
    assert sum(1 for c in cases
               if c["expect"]["unavailable_service_objection"] == "scored") >= 2


def test_no_case_would_be_refused_by_the_speech_gate(cases):
    """A fixture under the gate never reaches the judge and proves nothing."""
    for case in cases:
        spoken = len(spoken_content(case["conversation"]))
        assert spoken >= MIN_SCOREABLE_CHARS, \
            f"{case['id']}: {spoken} spoken chars, gate is {MIN_SCOREABLE_CHARS}"


def test_the_snippets_are_synthetic_not_pasted_transcripts(cases):
    """A weak guard, deliberately kept: this repository is public.

    A real day-13 call runs to thousands of characters and its export carries
    phone numbers. Neither belongs here. This will not catch a careful paste —
    the real control is the rule, written at the top of the fixture file — but
    it catches the careless one.
    """
    for case in cases:
        assert len(case["conversation"]) < 900, case["id"]
        assert not re.search(r"\+?\d{9,}", case["conversation"]), case["id"]


def test_every_negative_case_names_the_exclusion_rule_it_tests(cases, prompt):
    """Each must point at a numbered rule in the prompt, so a failing case
    names the paragraph to fix rather than starting a re-read of Module 3."""
    for case in cases:
        if case["expect"]["unavailable_service_objection"] != "null":
            continue
        rule = case.get("exclusion_rule")
        assert isinstance(rule, int) and 1 <= rule <= 8, case["id"]
        assert f"\n{rule}. " in prompt, f"rule {rule} is not in the prompt"


def test_the_fixtures_run_against_the_prompt_the_judge_actually_uses(prompt):
    """The file the fixtures assert about must be the file production sends.

    v4.1 was edited into `pass2_agent_quality_v4.md` in place, so this test read
    one text while `PASS2_PROMPT_FILE` could have named another. Reading the
    prompt through `judge.PASS2_PROMPT_FILE` makes that impossible: point the
    judge somewhere else and these tests follow it.
    """
    assert judge.PASS2_PROMPT_FILE == "pass2_agent_quality_v6.md"
    assert judge.PASS2_VERSION == "pass2-agent-quality-v6"
    # The label a row is stamped with must be derivable from the filename, or
    # the two drift and nothing downstream can tell which text produced a score.
    assert judge.PASS2_VERSION == Path(judge.PASS2_PROMPT_FILE).stem.replace("_", "-")
    assert f"prompt_version: {judge.PASS2_VERSION}" in prompt


def test_the_prompt_carries_the_revision_the_fixtures_were_written_for(prompt):
    assert "revision: v6" in prompt
    assert "EXCLUSION LIST" in prompt


def test_the_prompt_carries_the_counterweight_verbatim(prompt):
    """The exact text the review required, not a paraphrase of it.

    Two false negatives on real calls were traced to the exclusion list being
    read as wider than it is. The counterweight is the correction, and a
    reworded correction is an untested one \u2014 so the sentences that carry the
    load are asserted word for word, whitespace-folded.
    """
    folded = " ".join(prompt.split())
    for sentence in (
        "COUNTERWEIGHT \u2014 these exclusions are narrow. An alternative does not "
        "erase a refusal.",
        "If the agent refused the requested tourism product but redirected the "
        "customer to another company-sold service, route, or destination, keep "
        "`agent_refused_or_declared_unavailable` true and keep "
        "`unavailable_service_objection` numeric; apply the 25/15/0 handling "
        "rubric below.",
        "Visa assistance, airport/ground transfers, and travel insurance ARE "
        "tourism products/services this company sells.",
        "Do not confuse an airport/ground transfer with transferring the phone "
        "call: only the latter administrative action is excluded by item 6.",
    ):
        assert sentence in folded, sentence[:60]


def test_the_stage_block_is_closed_and_field_specific(prompt):
    """The round-4 fix, word for word — and the reverted wording kept out.

    Round 3 traced `174898da` to Step 0, not to Module 3. The MANDATORY
    CONSISTENCY list named three of the four objections as requiring
    `negotiation` or later, said nothing about the fourth, and the judge
    generalised the rule to all four; it said so in its own notes. Module 3 was
    dropped on stage grounds before the exclusion list or its counterweight was
    ever read — on exactly the population the criterion exists to catch.

    The first attempt at the fix was an exemption appended to the old block. It
    fixed `174898da` (0/3 → 3/3) and BROKE `e779317b` (3/3 → 1/3) and the D6
    Module-4 fixture (3/3 → 1/3, against 3/3 on untouched v4), and the likely
    mechanism was its wording: it ended "...even when `stage_reached` stays
    `reception` and Modules 2, 4 and 5 are all `null`", which reads as an
    output template, and Module 4 is the module that went null. It was
    reverted.

    v6 replaces the block instead of appending to it, with the reviewer's
    closed, field-specific rules — every rule applies only to the fields it
    names, and no other module is mentioned anywhere in it. That text is what
    the round-4 audit measured, so it is asserted verbatim (whitespace-folded):
    a reworded fix is an unaudited one.
    """
    block = prompt[prompt.index("MANDATORY CONSISTENCY"):
                   prompt.index("5. OBJECTIONS IDENTIFIED")]
    folded = " ".join(block.split())

    assert folded.startswith("MANDATORY CONSISTENCY — CLOSED, FIELD-SPECIFIC RULES:")
    for sentence in (
        "Determine each objection trigger before applying stage consistency. "
        "Apply every rule below only to the fields it names; do not extend a "
        "stage requirement from one objection to another.",
        "- If `price_objection`, `competitor_objection`, or "
        "`thinking_time_objection` is non-null, `stage_reached` must be "
        "`negotiation` or later.",
        "- `unavailable_service_objection` is NOT stage-gated. Always perform "
        "the SERVICE REFUSALS INVENTORY, including when no offer was stated. "
        "When the customer requested a qualifying tourism product/service and "
        "the agent categorically refused it, set "
        "`refusal_check.agent_refused_or_declared_unavailable` to true and "
        "score `unavailable_service_objection` 0, 15, or 25 even if "
        "`stage_reached` is `reception`. This objection does not itself "
        "advance `stage_reached`.",
        "- If the customer rejected a stated offer and `alternative_offer` is "
        "non-null, `stage_reached` must be `negotiation` or later.",
        "- `offer_presented` cannot coexist with detected post-offer price "
        "pushback.",
    ):
        assert sentence in folded, sentence[:70]


def test_the_stage_block_names_no_other_module(prompt):
    """The failure mode of the reverted attempt, written as a prohibition.

    An instruction that enumerates Modules 2, 4 and 5 as `null` was read as an
    output template by the judge, and Module 4 went null on a fixture untouched
    v4 scored 3/3. Whatever the next edit to this block is, it may not name a
    module it is not about.
    """
    block = prompt[prompt.index("MANDATORY CONSISTENCY"):
                   prompt.index("5. OBJECTIONS IDENTIFIED")]
    for foreign in ("Module 2", "Module 4", "Module 5", "module2_offer",
                    "module4_followup", "module5_closing"):
        assert foreign not in block, foreign
    assert "are all `null`" not in block


def test_the_counterweight_comes_after_the_exclusion_list(prompt):
    """Placement is the point: it qualifies the list, so it must follow it.

    Put before the list it reads as a general note the eight numbered
    exclusions then override, which is the failure it exists to fix.
    """
    assert prompt.index("EXCLUSION LIST") < prompt.index("COUNTERWEIGHT")
    assert prompt.index("COUNTERWEIGHT") < prompt.index("TEST BEFORE YOU FIRE IT")


def test_the_superseded_prompts_are_left_untouched_as_history():
    """A shipped prompt keeps its own text, or its stored scores lie.

    `agent_evaluations.prompt_version` says `pass2-agent-quality-v4` on every
    row scored before round 3 and `pass2-agent-quality-v5` on every row scored
    during round 3's audit. If either file acquires a later edit, the label
    points at a text that never produced those numbers — and v5 is also the
    frozen candidate the round-3 audit measured, so its stage block must still
    read the way that audit found it.
    """
    prompts = Path(__file__).parents[1] / "app" / "prompts"

    v4 = (prompts / "pass2_agent_quality_v4.md").read_text(encoding="utf-8")
    assert "COUNTERWEIGHT" not in v4
    assert "prompt_version: pass2-agent-quality-v4" in v4

    v5 = (prompts / "pass2_agent_quality_v5.md").read_text(encoding="utf-8")
    assert "prompt_version: pass2-agent-quality-v5" in v5
    assert "COUNTERWEIGHT" in v5
    v5_stage_block = v5[v5.index("MANDATORY CONSISTENCY"):
                        v5.index("5. OBJECTIONS IDENTIFIED")]
    assert "CLOSED, FIELD-SPECIFIC" not in v5_stage_block
    assert "unavailable_service_objection" not in v5_stage_block


def test_the_counterweight_cases_are_all_present(cases):
    """The five the review asked for, by name and by expectation."""
    expected = {
        "destination_refusal_with_sold_redirect": "scored",
        "visa_assistance_refusal": "scored",
        "airport_transfer_refusal": "scored",
        "travel_insurance_refusal": "scored",
        "phone_call_transfer_refused": "null",
    }
    by_id = {c["id"]: c for c in cases}
    assert expected.keys() <= by_id.keys()
    for cid, want in expected.items():
        assert by_id[cid]["expect"]["unavailable_service_objection"] == want, cid


def test_the_two_known_regressions_are_covered(cases):
    """`e779317b` and `174898da` are the calls v4.1 got wrong, and they are the
    reason this round exists. A suite that stops covering them cannot fail for
    the reason it was built."""
    assert {"e779317b", "174898da"} <= {c["stands_for"] for c in cases}


def test_the_transfer_pair_is_a_pair(cases):
    """Airport transfer must fire; call transfer must not.

    Written as one test on purpose. Separately they can both pass under a judge
    that fires on the word "transfer" or on neither; together they only pass if
    it has read which kind of transfer was refused.
    """
    by_id = {c["id"]: c for c in cases}
    airport = by_id["airport_transfer_refusal"]["expect"]
    phone = by_id["phone_call_transfer_refused"]["expect"]
    assert airport["unavailable_service_objection"] == "scored"
    assert airport["refusal_check"] is True
    assert phone["unavailable_service_objection"] == "null"
    assert phone["refusal_check"] is False


# ── the round-4 fixture policy ──────────────────────────────────────────────
#
# Two rounds running, a synthetic control passed while the real call it stood
# for failed. Both times the fixture was a clean dialogue that exercised the
# rule, and both times the real transcript differed in SHAPE: it never reached
# an offer, it was damaged by ASR, and the customer's request was not sitting
# in it as one quotable sentence. The review's answer was to stop writing
# fixtures that are easier than production, and these tests are what stops the
# next author from tidying them back up.

OFFER_TOKENS = ("ريال", "ألف", "آلاف", "باكج", "شامل", "السعر", "سعر",
                "العرض", "عرض")

TURN = re.compile(r"^\[\d\d:\d\d\]\s+(?:AGENT|CUSTOMER):\s*(.*)$")


def _turns(conversation: str) -> list[str]:
    """What each speaker actually said, one entry per line of the transcript."""
    return [m.group(1) for line in conversation.split("\n")
            if (m := TURN.match(line))]


def _fires(case) -> bool:
    return case["expect"]["unavailable_service_objection"] == "scored"


def test_a_reception_refusal_case_is_never_cleaned_into_an_offer(cases):
    """Rule 1. The population this criterion exists to catch is the customer
    who asks for something the agency does not do and is turned away before
    anything is quoted — `174898da` and every call like it. A fixture for that
    shape that carries a price, a package or an offer is testing a different
    conversation, and it is the reason v5's `visa_assistance_refusal` fired on
    synthetic text while the real call stayed null 3/3.
    """
    reception_refusals = [c for c in cases
                          if _fires(c) and c["expected_stage"] == "reception"]
    assert len(reception_refusals) >= 4, "the shape under test has to be the majority"
    for case in reception_refusals:
        found = [t for t in OFFER_TOKENS if t in case["conversation"]]
        assert not found, f"{case['id']} mentions {found}"
        # and it stops shortly after the refusal rather than turning into a sale
        turns = _turns(case["conversation"])
        refusal = case["fragments"]["refusal"]
        after = turns[next(i for i, t in enumerate(turns) if refusal in t) + 1:]
        assert len(after) <= 4, f"{case['id']}: {len(after)} turns after the refusal"


def test_one_scored_case_still_reaches_an_offer(cases):
    """Rule 1, the other way round.

    Round 4 removed a stage gate. A prompt that responded by treating Module 3
    as belonging to reception calls would pass every case above and be just as
    broken as v5 was, in the opposite direction — so one case that must fire
    has to be a genuine post-offer refusal that gets argued over.
    """
    assert any(_fires(c) and c["expected_stage"] == "negotiation" for c in cases)


def test_the_noisy_cases_carry_real_asr_damage(cases):
    """Rule 2. Truncation, misrecognition and a gap — not a tidy transcript.

    The blanket prohibition on `[[ASR_GAP]]` that used to live in this file was
    the wrong control: it guaranteed every fixture was cleaner than production.
    It is replaced by a requirement on the cases designated noisy, and by the
    matching assertion that the others are clean, because a suite where every
    case is damaged cannot tell a prompt failure from an ASR-handling one.
    """
    noisy = [c for c in cases if c.get("noisy")]
    assert len(noisy) >= 4
    for case in noisy:
        conversation = case["conversation"]
        assert "[[ASR_GAP]]" in conversation, case["id"]
        assert any(t.rstrip().endswith("-") for t in _turns(conversation)), \
            f"{case['id']}: no mid-turn truncation"
        misheard = case.get("misrecognitions") or []
        assert misheard, f"{case['id']}: noisy but declares no misrecognition"
        for token in misheard:
            assert token in conversation, f"{case['id']}: {token} is not in the text"

    for case in cases:
        if not case.get("noisy"):
            assert "[[ASR_GAP]]" not in case["conversation"], case["id"]


def test_no_fragment_bridges_the_asr_gap(cases):
    """Rule 2, the half that does the work.

    `[[ASR_GAP]]` marks removed machine output and is a hard boundary for
    evidence: `scoring.py` splits on it before matching any quote. A fixture
    whose request and refusal can be covered by one span either side of the gap
    is not testing that boundary. Here the gap sits between them, so a judge
    that wants one tidy sentence covering both has to invent it — and an
    invented quote is discarded along with the finding it supports.
    """
    for case in cases:
        spans = case["conversation"].split("[[ASR_GAP]]")
        for kind, fragment in case["fragments"].items():
            if fragment is None:
                continue
            assert sum(1 for s in spans if fragment in s) == 1, \
                f"{case['id']}: the {kind} fragment bridges the gap"


def test_the_request_and_the_refusal_are_separate_partial_fragments(cases):
    """Rule 3. Exact, contiguous, in different turns, and never a whole one.

    The two-question test — which product was asked for, and which turn refused
    it — has to be answerable from partial evidence, because on a real call it
    always is. A fragment that is an entire turn is a sentence written to be
    quoted, and a fixture built out of those tests the rule on input production
    never produces.
    """
    for case in cases:
        conversation, turns = case["conversation"], _turns(case["conversation"])
        fragments = case["fragments"]
        assert fragments.get("request"), case["id"]
        # Every case that must fire has to name the turn that refused. A case
        # that must NOT fire may still name one — on the exclusion cases the
        # agent does say no, to something that is not a product this company
        # sells, and that "no" is the thing the judge has to decline to score.
        if _fires(case):
            assert fragments.get("refusal"), case["id"]

        for kind, fragment in fragments.items():
            if fragment is None:
                continue
            assert fragment in conversation, f"{case['id']}: {kind} is not verbatim"
            holders = [t for t in turns if fragment in t]
            assert holders, f"{case['id']}: {kind} is not inside a single turn"
            assert all(t.strip() != fragment for t in holders), \
                f"{case['id']}: the {kind} fragment is a whole turn — too tidy"

        if fragments.get("refusal"):
            request_turn = next(t for t in turns if fragments["request"] in t)
            refusal_turn = next(t for t in turns if fragments["refusal"] in t)
            assert request_turn != refusal_turn, case["id"]
            assert conversation.index(fragments["request"]) < \
                conversation.index(fragments["refusal"]), case["id"]


def test_the_file_states_the_policy_it_is_built_to(spec):
    """The rules live next to the data, or the next author cannot follow them."""
    policy = spec["fixture_policy"]
    assert {"1_stage_shape", "2_asr_damage", "3_partial_evidence",
            "4_m4_pair"} <= policy.keys()
