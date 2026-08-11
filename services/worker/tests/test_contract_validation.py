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


def test_a_real_short_call_still_reaches_the_judge(monkeypatch):
    """The floor must not swallow genuinely brief but real conversations."""
    from app.main import MIN_SCOREABLE_CHARS

    real = "ألو السلام عليكم -- عليكم السلام هلا معك خالد من ترافل جيت"
    assert len(real.strip()) >= MIN_SCOREABLE_CHARS


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

    real = "[00:00] ألو السلام عليكم -- عليكم السلام هلا معك خالد من ترافل جيت"
    assert len(spoken_content(real)) >= MIN_SCOREABLE_CHARS


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
