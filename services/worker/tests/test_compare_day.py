"""The comparison harness itself.

`compare_day.py` is the instrument the prompt-and-scoring decisions are read
off. An instrument nobody checks is worse than no instrument: a cache that can
answer a question it was never asked, or a delta that fuses two causes, does not
fail — it reports a confident number that means something else.

Nothing here calls DeepSeek or touches the network.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "compare_day", ROOT / "scripts" / "compare_day.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["compare_day"] = module
    spec.loader.exec_module(module)
    return module


cd = _load()


# ── fixtures ────────────────────────────────────────────────────────────────

def _item(**overrides):
    item = {
        "interaction_id": "11111111-2222-3333-4444-555555555555",
        "conversation": "[00:03] AGENT: السلام عليكم ترافل جيت\n"
                        "[00:07] CUSTOMER: أبغى عرض لتركيا لعائلة أربعة أشخاص",
        "kind": "q",
        "duration_seconds": 308.84,
        "asr_confidence": 1,
        "metadata": {"asr_confidence": 1, "diarization": "none",
                     "duration_seconds": 308.84, "channels": 1},
        "followup_history": None,
        "old": {"final_score": 70.0, "performance_level": "Good",
                "weight_applied": 0.75, "modules": {}, "pass2_payload": {}},
    }
    item.update(overrides)
    return item


# ── the cache key ───────────────────────────────────────────────────────────
# The old key was the interaction id plus two version labels. Everything below
# used to collide: same id, same labels, different question.

def test_the_cache_key_covers_every_effective_input():
    base = cd.cache_key(_item(), only_pass2=False)

    assert cd.cache_key(_item(conversation="something else entirely"),
                        only_pass2=False) != base
    assert cd.cache_key(_item(metadata={"asr_confidence": 0.4, "diarization": "none",
                                        "duration_seconds": 12.0, "channels": 2}),
                        only_pass2=False) != base
    assert cd.cache_key(_item(followup_history="Subsequent contact: ..."),
                        only_pass2=False) != base
    assert cd.cache_key(_item(kind="chat"), only_pass2=False) != base
    # A pass2-only smoke run must not poison a later full run: the cached entry
    # has no pass 1 in it, and reading it back reports pass-1 validation as
    # absent for the whole day.
    assert cd.cache_key(_item(), only_pass2=True) != base


def test_the_cache_key_is_stable_for_the_same_question():
    assert cd.cache_key(_item(), False) == cd.cache_key(_item(), False)
    # Fields that do not reach the judge must not split the cache.
    assert cd.cache_key(_item(agent_name="خالد"), False) == \
        cd.cache_key(_item(agent_name="سارة"), False)


def test_editing_a_prompt_file_invalidates_the_cache(tmp_path, monkeypatch):
    """A version label is a promise; the file contents are the measurement.

    Editing `pass2_agent_quality_v4.md` without bumping `PASS2_VERSION` is
    exactly what an iteration cycle does, and the old key could not see it.
    """
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "pass2.md").write_text("original", encoding="utf-8")

    monkeypatch.setattr(cd.judge, "PROMPT_DIR", prompts)
    before = cd._prompt_fingerprint()
    (prompts / "pass2.md").write_text("original, plus one new rule", encoding="utf-8")
    assert cd._prompt_fingerprint() != before


def test_the_cache_path_still_names_the_interaction():
    """Debuggability: a directory of opaque hashes cannot be inspected by id."""
    path = cd.cache_path(Path("out"), _item(), only_pass2=False)
    assert path.name.startswith("11111111-2222")
    assert path.suffix == ".json"


# ── the metadata block ──────────────────────────────────────────────────────

def test_nested_production_metadata_is_used_as_is():
    metadata, source, missing = cd.metadata_for(_item())
    assert source == "nested" and missing == []
    assert metadata == {"asr_confidence": 1, "diarization": "none",
                        "duration_seconds": 308.84, "channels": 1}


def test_a_missing_metadata_block_is_named_not_silently_emptied():
    """`{}` does not make the comparison neutral. It makes the new run answer a
    different question from the old one, invisibly."""
    metadata, source, missing = cd.metadata_for(_item(metadata=None))
    assert source == "rebuilt"
    assert metadata["asr_confidence"] == 1          # salvaged from the top level
    assert set(missing) == {"diarization", "channels"}

    _, source, missing = cd.metadata_for(
        {"interaction_id": "x", "conversation": "c"})
    assert source == "empty"
    assert set(missing) == set(cd.PRODUCTION_METADATA_FIELDS)


def test_a_dry_run_fails_when_the_metadata_block_is_absent(tmp_path, monkeypatch):
    import json
    bad = tmp_path / "in.json"
    bad.write_text(json.dumps([{"interaction_id": "a", "conversation": "x" * 80}]),
                   encoding="utf-8")
    argv = ["compare_day.py", "--dry-run", "--input", str(bad),
            "--out", str(tmp_path / "out")]

    monkeypatch.setattr(sys, "argv", argv)
    assert cd.main() == 3                                  # refused, not silent

    monkeypatch.setattr(sys, "argv", argv + ["--allow-incomplete-metadata"])
    assert cd.main() == 0                                  # accepted knowingly
    written = json.loads((tmp_path / "out" / "dry_run.json").read_text(encoding="utf-8"))
    assert written["items_without_nested_metadata"] == ["a"]
    assert written["metadata_ok"] is True


# ── the delta split ─────────────────────────────────────────────────────────

def _result(final, pre, **overrides):
    result = {
        "interaction_id": "11111111-2222-3333-4444-555555555555",
        "unscoreable": False, "error": None, "spoken_chars": 120,
        "metadata_source": "nested", "metadata_missing_fields": [],
        "pass1": None,
        "pass2": {
            "payload": {"modules": {}, "evidence": []},
            "final_score": final, "pre_enforcement_score": pre,
            "performance_level": "Good", "weight_applied": 0.75,
            "gradeable": True, "modules": {}, "warnings": [],
            "contract_status": "ok", "contract_violations": [],
            "evidence_rejected": [], "usage": {},
        },
    }
    result.update(overrides)
    return result


def test_the_delta_is_split_into_prompt_and_enforcement():
    """One number cannot be acted on: +15 is either a kinder prompt or a
    validator handing points back, and those call for opposite responses."""
    row = cd.build_row(_item(), _result(final=90.0, pre=75.0))

    assert row["old_final_score"] == 70.0
    assert row["pre_enforcement_score"] == 75.0
    assert row["score_delta"] == 20.0
    assert row["prompt_delta"] == 5.0            # the prompt judged 5 points kinder
    assert row["enforcement_delta"] == 15.0      # the code restored 15
    assert row["prompt_delta"] + row["enforcement_delta"] == row["score_delta"]


def test_the_split_is_absent_rather_than_guessed_when_a_side_is_missing():
    row = cd.build_row(_item(old={}), _result(final=90.0, pre=75.0))
    assert row["score_delta"] is None and row["prompt_delta"] is None
    assert row["enforcement_delta"] == 15.0      # this half is still knowable

    row = cd.build_row(_item(), _result(final=90.0, pre=None))
    assert row["score_delta"] == 20.0
    assert row["prompt_delta"] is None and row["enforcement_delta"] is None


# ── objection flips ─────────────────────────────────────────────────────────

def _with_objections(payload_values, evidence=()):
    return {"modules": {"module3_objections": {"breakdown": dict(payload_values)}},
            "evidence": list(evidence)}


def test_per_criterion_objection_flips_are_recorded_with_their_quotes():
    """The mean moves the same amount whether the prompt found a real objection
    it used to miss or invented one. Only the per-criterion flip can tell."""
    old = _with_objections({"price_objection": 25, "competitor_objection": None,
                            "thinking_time_objection": None,
                            "unavailable_service_objection": None})
    new = _with_objections(
        {"price_objection": None, "competitor_objection": None,
         "thinking_time_objection": 15, "unavailable_service_objection": None},
        evidence=[{"module": "module3_objections",
                   "criterion": "thinking_time_objection",
                   "quote": "تمام، جزاك الله خير"}])

    row = cd.build_row(
        _item(old={"final_score": 70.0, "modules": {}, "pass2_payload": old}),
        _result(final=70.0, pre=70.0,
                pass2={**_result(70.0, 70.0)["pass2"], "payload": new}))

    assert row["flip_thinking_time_objection"] == "null_to_numeric"
    assert row["quotes_thinking_time_objection"] == "تمام، جزاك الله خير"
    assert row["flip_price_objection"] == "numeric_to_null"
    assert row["flip_competitor_objection"] is None


def test_evidence_quotes_use_the_strict_criterion_pairing():
    """An entry naming another module must not be printed as this criterion's
    justification — the report would attribute a quote to a finding it never
    defended."""
    payload = _with_objections(
        {"thinking_time_objection": 15},
        evidence=[
            {"module": "module3_objections", "criterion": "module2_offer.thinking_time_objection",
             "quote": "wrong module"},
            {"module": "module3_objections", "criterion": "thinking_time_objection",
             "quote": "right one"},
        ])
    assert cd._evidence_quotes_for(payload, "thinking_time_objection") == ["right one"]


# ── the report renders ──────────────────────────────────────────────────────

def test_the_report_renders_every_new_section(tmp_path):
    items = [_item()]
    results = [_result(final=90.0, pre=75.0, pass2={
        **_result(90.0, 75.0)["pass2"],
        "evidence_rejected": [
            {"module": "module1_reception", "criterion": "missing_info_request",
             "reason": "no evidence cited for this criterion",
             "model_score": 0, "restored_to": 25, "quote": None},
            {"module": "module2_offer", "criterion": "value_selling",
             "reason": "quote not found in conversation: 'x'",
             "model_score": 10, "restored_to": 25, "quote": "never said"},
        ],
        "warnings": ["first response violated the rubric contract or cited no "
                     "evidence for 2 deduction(s); re-asked once"],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10,
                  "total_tokens": 30, "api_calls": 2},
    })]
    rows = [cd.build_row(items[0], results[0])]

    out = tmp_path / "report.md"
    cd.write_report(out, items, results, rows, 0.27, 1.10, 12.0)
    text = out.read_text(encoding="utf-8")

    assert "prompt_delta" in text and "enforcement_delta" in text
    assert "points restored" in text
    assert "nothing quoted" in text and "quote not in transcript" in text
    assert "Objection flips, per criterion" in text
    assert "Speech-gate sensitivity" in text
    assert "model calls: **2**" in text
    assert "1** needed a correction re-ask" in text


def test_the_usage_total_counts_both_calls_of_a_correction():
    results = [
        _result(90.0, 75.0, pass2={**_result(90.0, 75.0)["pass2"],
                                   "usage": {"prompt_tokens": 20, "api_calls": 2}}),
        _result(80.0, 80.0, pass2={**_result(80.0, 80.0)["pass2"],
                                   "usage": {"prompt_tokens": 10, "api_calls": 1}}),
    ]
    total = cd._usage_totals(results)
    assert total["prompt_tokens"] == 30
    assert total["api_calls"] == 3            # not 2 — the re-ask is counted


@pytest.mark.parametrize("reason,bucket", [
    ("no evidence cited for this criterion", "nothing quoted"),
    ("quote not found in conversation: 'x'", "quote not in transcript"),
    ("quote contains the ASR gap marker, which is not speech", "quoted the gap marker"),
    ("empty quote", "empty quote"),
])
def test_rejection_reasons_bucket_to_the_place_that_fixes_them(reason, bucket):
    assert cd._reason_class(reason) == bucket
