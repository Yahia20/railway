"""Guards against the leniency the first real DeepSeek run exhibited.

Every case here is something the model actually did on call
q-3009-0500000000-20260701-170522, not a hypothetical.
"""
import pytest

# Two conversations long enough to clear the 100-character speech gate, so a
# test about contract handling is testing contract handling and not the gate.
REAL_SHORT_CALL = (
    "[00:00] AGENT: ألو السلام عليكم معك خالد من ترافل جيت\n"
    "[00:04] CUSTOMER: عليكم السلام، أبغى عرض لتركيا لعائلة أربعة أشخاص\n"
    "[00:18] AGENT: أبشر، أحسب لك العرض وأرسله لك على الواتساب\n"
)
REFUSAL_CALL = (
    "[00:01] CUSTOMER: لو سمحت أبغى تذكرة من الرياض إلى عدن الأسبوع الجاي\n"
    "[00:09] AGENT: ما عندنا رحلات إلى عدن، بس عندنا الرياض جدة والرياض دبي\n"
)

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


def _no_objections():
    """A Module 3 with nothing scored — the shape of a call with no pushback."""
    return dict.fromkeys(scoring.CRITERION_MAX["module3_objections"], None)


def test_stage_contradicting_a_null_offer_is_caught():
    """The real failure: stage_reached='offer_presented' while offer_completeness
    was null and the model's own notes said no offer was made."""
    modules = _modules(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": 20, "alternative_offer": None,
    }, module3_objections=_no_objections())
    problems = scoring.validate_stage_consistency({"stage_reached": "offer_presented"}, modules)
    assert len(problems) == 1
    assert "Pick one" in problems[0]


@pytest.mark.parametrize("stage", ["reception", "follow_up"])
def test_pre_offer_stages_are_consistent_with_a_null_offer(stage):
    modules = _modules(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": 20, "alternative_offer": None,
    }, module3_objections=_no_objections())
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
    }, module3_objections=_no_objections())
    problems = scoring.contract_violations({"stage_reached": "offer_presented"}, modules)
    assert len(problems) == 2


# ── the stage gate is per-field, and one field is deliberately outside it ───
#
# PR2 round 4. Step 0 used to name three of the four objections as requiring
# `negotiation` or later and say nothing about the fourth, and the judge
# generalised the rule to all four: on `174898da` it wrote "the conversation
# never reached a price offer or closing stage, so Modules 3, 4 and 5 were
# dropped" and returned `refusal_check` false with every objection null, on a
# call where visa assistance was refused four times. The prompt is the
# functional fix; this is the contract guard, and its shape has to match — a
# closed rule that applies only to the fields it names.

@pytest.mark.parametrize("criterion",
                         ["price_objection", "competitor_objection",
                          "thinking_time_objection"])
@pytest.mark.parametrize("stage", ["reception", "offer_presented"])
def test_a_pushback_objection_before_negotiation_is_a_violation(criterion, stage):
    """These three are all pushback on something the agent said, so none of
    them can exist before the agent said it and the customer argued with it."""
    breakdown = _no_objections()
    breakdown[criterion] = 25
    modules = _modules(module3_objections=breakdown)
    problems = scoring.validate_stage_consistency({"stage_reached": stage}, modules)
    assert len(problems) == 1
    assert criterion in problems[0]
    assert "negotiation or later" in problems[0]


@pytest.mark.parametrize("stage", ["reception", "offer_presented"])
@pytest.mark.parametrize("score", [0, 15, 25])
def test_an_unavailable_service_objection_before_negotiation_is_NOT_a_violation(
        stage, score):
    """The whole point of the round.

    A customer asks for something the agency does not sell and is turned away
    at the door. Nothing is ever quoted, so the call ends at `reception` — and
    that is the population this criterion exists to catch, not an edge case.
    Rejecting it here would re-ask the model until it reproduced the false
    negative the prompt was just changed to stop producing.
    """
    breakdown = _no_objections()
    breakdown["unavailable_service_objection"] = score
    modules = _modules(module3_objections=breakdown)
    assert scoring.validate_stage_consistency({"stage_reached": stage}, modules) == []
    assert scoring.hard_violations({"stage_reached": stage}, modules) == []


def test_the_refusal_link_still_holds_at_reception():
    """What is NOT relaxed. The stage gate is gone; the contract is not.

    `validate_refusal_link` is where `unavailable_service_objection` is checked,
    and it is checked at every stage, in both directions — which is exactly why
    it can be left out of the stage rule without leaving the criterion
    unguarded.
    """
    breakdown = _no_objections()
    modules = _modules(module3_objections=breakdown)
    modules["module3_objections"]["refusal_check"] = {
        "customer_requested_something_specific": True,
        "agent_refused_or_declared_unavailable": True,
        "refusal_quote": "ما عندنا رحلات إلى عدن",
    }
    problems = scoring.hard_violations({"stage_reached": "reception"}, modules)
    assert len(problems) == 1
    assert "must carry a number" in problems[0]


def test_a_reception_refusal_that_is_scored_and_flagged_passes_every_check():
    """The `174898da` shape, answered the way round 4 requires."""
    breakdown = _no_objections()
    breakdown["unavailable_service_objection"] = 15
    modules = _modules(module2_offer={
        "attitude": 25, "offer_completeness": None,
        "value_selling": 20, "alternative_offer": None,
    }, module3_objections=breakdown)
    modules["module3_objections"]["refusal_check"] = {
        "customer_requested_something_specific": True,
        "agent_refused_or_declared_unavailable": True,
        "refusal_quote": "التأشيرات ما نشتغل فيها احنا نهائيا",
    }
    assert scoring.hard_violations({"stage_reached": "reception"}, modules) == []


def test_the_gated_list_is_the_three_the_reviewer_named():
    """Written down so a fourth entry needs a deliberate edit and a reason."""
    assert scoring.NEGOTIATION_GATED_OBJECTIONS == (
        "price_objection", "competitor_objection", "thinking_time_objection")
    assert "unavailable_service_objection" not in scoring.NEGOTIATION_GATED_OBJECTIONS
    assert scoring.PRE_NEGOTIATION_STAGES == frozenset({"reception", "offer_presented"})


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


# ── unscoreable transcripts ─────────────────────────────────────────────────
# Live on 2026-08-11: 17 of 20 calls came back from the ASR Space with empty
# text and confidence 0 under burst load, and every one was stored as
# final_score 0, "Below Average", gradeable. The judge cannot distinguish a
# missing transcript from a bad call — asked to grade nothing it returns zeros
# with full confidence.

import pytest


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "ألو", "السلام عليكم"])
def test_short_or_empty_transcripts_are_refused_not_scored(text, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    r = TestClient(main.app).post(
        "/evaluate",
        json={"conversation": text, "input_type": "call_transcript"},
        headers={"X-API-Key": "k"},
    )
    assert r.status_code == 200
    p2 = r.json()["pass2"]
    assert p2["gradeable"] is False
    assert p2["final_score"] is None          # never 0 — 0 means "did it badly"
    assert "not a badly handled one" in p2["warnings"][0]


def test_every_pass2_path_carries_the_same_status_keys(monkeypatch):
    """A consumer must never have to branch on a key being absent.

    The usable-score rule is `contract_status == "ok" AND gradeable AND
    final_score is not None`, and it is only checkable if all three are present
    on every path. The pre-model refusal used to omit `ungradeable_modules`,
    so a consumer reading it either crashed or defaulted -- and the default
    that costs money is reading a missing score as a zero.
    """
    from fastapi.testclient import TestClient

    from app import main
    from app.evaluate import scoring

    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    client = TestClient(main.app)

    refused = client.post(
        "/evaluate", json={"conversation": "ألو", "input_type": "call_transcript"},
        headers={"X-API-Key": "k"}).json()["pass2"]
    assert refused["contract_status"] == "unscoreable"

    modules = {key: {"score": None, "breakdown": dict(caps)}
               for key, caps in scoring.CRITERION_MAX.items()}
    payload = {"schema_version": "1.0", "stage_reached": "closing",
               "modules": modules, "evidence": []}
    monkeypatch.setattr(main.judge, "DeepSeekClient",
                        lambda *a, **kw: _Stub(payload, payload))
    scored = client.post(
        "/evaluate",
        json={"conversation": REAL_SHORT_CALL, "input_type": "call_transcript",
              "run_pass1": False},
        headers={"X-API-Key": "k"}).json()["pass2"]
    assert scored["contract_status"] == "ok"

    # Same keys, whatever happened.
    assert set(refused) == set(scored), set(refused) ^ set(scored)
    for block in (refused, scored):
        for key in ("contract_status", "gradeable", "final_score",
                    "ungradeable_modules", "contract_violations",
                    "evidence_rejected", "prompt_version", "model"):
            assert key in block, key

    # And the rule itself agrees with the status on both.
    def usable(p2):
        return (p2["contract_status"] == "ok" and p2["gradeable"]
                and p2["final_score"] is not None)

    assert usable(scored) is True
    assert usable(refused) is False


def test_a_real_short_call_still_reaches_the_judge(monkeypatch):
    """The floor must not swallow genuinely brief but real conversations.

    Raised to 100 normalised characters in PR2 iteration 2. A greeting plus one
    stated requirement plus one agent commitment clears it; a greeting alone
    does not, which is the whole point of the move.
    """
    from app.main import MIN_SCOREABLE_CHARS, spoken_content

    assert len(spoken_content(REAL_SHORT_CALL)) >= MIN_SCOREABLE_CHARS


def test_failed_chunk_is_distinct_from_a_silent_one():
    """None means nobody read the chunk; '' means it was read and was silent."""
    from app.asr.cohere_arabic import _transcribe_with_retry

    class Silent:
        def transcribe_file(self, p): return ""

    class Broken:
        def transcribe_file(self, p): raise RuntimeError("429 rate limited")

    assert _transcribe_with_retry(Silent(), "x") == ""
    assert _transcribe_with_retry(Broken(), "x") is None


def test_retry_recovers_a_chunk_that_fails_once():
    from app.asr.cohere_arabic import _transcribe_with_retry

    class Flaky:
        def __init__(self): self.n = 0
        def transcribe_file(self, p):
            self.n += 1
            if self.n < 2: raise RuntimeError("429")
            return "نعم تفضل"

    assert _transcribe_with_retry(Flaky(), "x") == "نعم تفضل"


# ── the floor must measure speech, not scaffolding ──────────────────────────
# Live 2026-08-11: "[00:00] ألو السلام عليكم" — a hangup, sixteen characters of
# Arabic — cleared a twenty character floor because the timestamp counted
# towards it, and was scored 33.1 "Below Average". The caller hung up; the
# agent did nothing wrong.

def test_timestamps_and_speaker_labels_do_not_count_as_speech():
    from app.main import spoken_content

    assert spoken_content("[00:00] ألو السلام عليكم") == "ألو السلام عليكم"
    assert spoken_content("[00:00] AGENT: ألو") == "ألو"
    assert spoken_content("[01:23:45] CUSTOMER: نعم") == "نعم"
    assert spoken_content("[00:00] " + chr(10) + "[00:12] ") == ""


def test_a_hangup_is_refused_not_scored(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    r = TestClient(main.app).post(
        "/evaluate",
        json={"conversation": "[00:00] ألو السلام عليكم", "input_type": "call_transcript"},
        headers={"X-API-Key": "k"},
    )
    p2 = r.json()["pass2"]
    assert p2["gradeable"] is False and p2["final_score"] is None


def test_a_real_conversation_is_not_swallowed_by_the_floor():
    from app.main import spoken_content, MIN_SCOREABLE_CHARS

    assert len(spoken_content(REAL_SHORT_CALL)) >= MIN_SCOREABLE_CHARS


def test_the_gate_counts_normalised_speech_not_rendering():
    """Padding a transcript with segment markers must not talk it past the gate."""
    from app.main import spoken_content

    padded = "".join(f"[{m:02d}:00] AGENT: هلا " for m in range(12))
    assert len(spoken_content(padded)) < 100        # 12 x "هلا " is 47 chars of speech
    assert "[" not in spoken_content(padded)
    assert "AGENT" not in spoken_content(padded)


def test_the_gate_reason_says_the_count_is_normalised(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    p2 = TestClient(main.app).post(
        "/evaluate",
        json={"conversation": "[00:00] AGENT: هلا", "input_type": "call_transcript"},
        headers={"X-API-Key": "k"},
    ).json()["pass2"]
    assert "normalised characters of speech" in p2["warnings"][0]
    assert "timestamps, speaker labels and whitespace runs removed" in p2["warnings"][0]


def test_a_refusal_still_fills_the_not_null_columns(monkeypatch):
    """agent_evaluations.model is NOT NULL; a refusal must still be storable."""
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    p2 = TestClient(main.app).post(
        "/evaluate", json={"conversation": "", "input_type": "call_transcript"},
        headers={"X-API-Key": "k"},
    ).json()["pass2"]

    for column in ("model", "prompt_version"):
        assert p2[column], f"{column} would violate NOT NULL"
    assert p2["final_score"] is None          # nullable, and must stay null
    assert p2["gradeable"] is False


# ── the refusal ↔ objection link ────────────────────────────────────────────
# pass2 v3 declares this a contract violation in words — "setting the flag true
# and the objection null is a contract violation" — and nothing checked it. Both
# directions remove a real judgement: one drops 25% of the weight out of the
# grade, the other marks the agent on an event the same response says never
# happened.

def _module3(refused, scored, **rest):
    modules = _modules(module3_objections={
        "price_objection": None, "competitor_objection": None,
        "thinking_time_objection": None, "unavailable_service_objection": scored,
    })
    modules["module3_objections"]["refusal_check"] = {
        "customer_requested_something_specific": True,
        "agent_refused_or_declared_unavailable": refused,
        "refusal_quote": "ما عندنا رحلات إلى عدن",
        **rest,
    }
    return modules


def test_a_refusal_with_a_null_objection_is_a_contract_violation():
    problems = scoring.validate_refusal_link(_module3(True, None))
    assert len(problems) == 1
    assert "must carry a number" in problems[0]


def test_an_objection_scored_without_a_refusal_is_a_contract_violation():
    problems = scoring.validate_refusal_link(_module3(False, 15))
    assert len(problems) == 1
    assert "cannot be scored without the refusal" in problems[0]


@pytest.mark.parametrize("refused,scored", [(True, 0), (True, 15), (True, 25),
                                            (False, None)])
def test_agreeing_answers_pass(refused, scored):
    assert scoring.validate_refusal_link(_module3(refused, scored)) == []


def test_a_missing_refusal_check_block_is_not_a_violation():
    """Older stored payloads and pass2 v1/v2 have no such field. Absence is not
    a contradiction — there is only one answer, so nothing can disagree."""
    modules = _modules()
    assert scoring.validate_refusal_link(modules) == []
    modules["module3_objections"]["refusal_check"] = {"refusal_quote": None}
    assert scoring.validate_refusal_link(modules) == []


def test_the_link_is_part_of_contract_violations():
    modules = _module3(True, None)
    problems = scoring.contract_violations({"stage_reached": "reception"}, modules)
    assert any("refusal_check" in p for p in problems)
    assert problems == scoring.hard_violations({"stage_reached": "reception"}, modules)


# ── a self-contradicting response returns, it does not raise ────────────────
# 422 used to be the answer to "the model contradicted itself", which loses the
# payload, the reason and the row. It now means only "the output was not usable
# JSON". A contradiction comes back 200 with no score and the violations named.

class _Stub:
    def __init__(self, *responses, model="stub-model"):
        self.model = model
        self._responses = list(responses)
        self.prompts = []

    def complete_json(self, prompt, **_):
        import copy
        self.prompts.append(prompt)
        i = min(len(self.prompts) - 1, len(self._responses) - 1)
        return copy.deepcopy(self._responses[i]), {"prompt_tokens": 1, "completion_tokens": 1}


def _contradictory():
    return {"schema_version": "1.0", "stage_reached": "reception",
            "modules": _module3(True, None), "evidence": []}


def test_an_unresolved_contradiction_is_returned_not_raised():
    from app.evaluate import judge

    payload = _contradictory()
    client = _Stub(payload, payload)
    result = judge.run_pass2("[00:01] AGENT: ما عندنا رحلات إلى عدن", "call_transcript",
                             client=client)

    assert len(client.prompts) == 2                  # asked again, once
    assert result.contract_status == "contract_failed"
    assert result.score.final_score is None
    assert result.score.gradeable is False
    assert result.payload["final_score"] is None
    assert result.payload["contract_status"] == "contract_failed"
    assert any("refusal_check" in v for v in result.contract_violations)
    # The evaluation itself is still there to look at.
    assert result.payload["modules"]["module3_objections"]["refusal_check"]


def test_a_contract_failure_is_http_200_with_a_status(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    payload = _contradictory()
    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    monkeypatch.setattr(main.judge, "DeepSeekClient",
                        lambda *a, **kw: _Stub(payload, payload))

    r = TestClient(main.app).post(
        "/evaluate",
        json={"conversation": REFUSAL_CALL,
              "input_type": "call_transcript", "run_pass1": False},
        headers={"X-API-Key": "k"},
    )
    assert r.status_code == 200
    p2 = r.json()["pass2"]
    assert p2["contract_status"] == "contract_failed"
    assert p2["final_score"] is None and p2["gradeable"] is False
    assert p2["contract_violations"] and p2["evidence_rejected"] == []
    for column in ("model", "prompt_version"):
        assert p2[column], f"{column} would violate NOT NULL"


def test_structurally_unusable_json_is_still_a_422(monkeypatch):
    """The one thing that is not a result: output with no `modules` at all."""
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    monkeypatch.setattr(main.judge, "DeepSeekClient",
                        lambda *a, **kw: _Stub({"sorry": "I cannot do that"}))

    r = TestClient(main.app).post(
        "/evaluate",
        json={"conversation": REAL_SHORT_CALL,
              "input_type": "call_transcript", "run_pass1": False},
        headers={"X-API-Key": "k"},
    )
    assert r.status_code == 422
    assert "structurally unusable" in r.json()["detail"]
