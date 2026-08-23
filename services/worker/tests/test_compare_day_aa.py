"""The A/A machinery, the prompt switch, and the follow-up-history format.

PR2 iteration 2. The day-13 review's central objection was that a prompt effect
with mean +0.09 and MAE 10.0 is not a prompt effect at all until the model's own
run-to-run spread has been measured and subtracted. That measurement is what
`--pass1-prompt` / `--pass2-prompt` (the A/A baseline) and `--aa-compare` (the
arithmetic) exist for, and it is only trustworthy if:

- picking a different prompt file actually changes the cache key, or the A run
  reads back the B run's cached answers and the variance floor comes out as
  zero;
- the version label follows the file, or every output row is stamped with the
  wrong prompt;
- `--history-format current` cannot silently send the old block.

Nothing here calls DeepSeek or touches the network.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "compare_day_aa", ROOT / "scripts" / "compare_day.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["compare_day_aa"] = module
    spec.loader.exec_module(module)
    return module


cd = _load()


@pytest.fixture(autouse=True)
def _restore_prompt_choice():
    """`select_prompts` mutates module state on the judge. Put it back."""
    j = cd.judge
    saved = (j.PASS1_PROMPT_FILE, j.PASS2_PROMPT_FILE,
             j.PASS1_VERSION, j.PASS2_VERSION)
    yield
    (j.PASS1_PROMPT_FILE, j.PASS2_PROMPT_FILE,
     j.PASS1_VERSION, j.PASS2_VERSION) = saved


def _item(**overrides):
    item = {
        "interaction_id": "11111111-2222-3333-4444-555555555555",
        "conversation": "[00:03] AGENT: السلام عليكم ترافل جيت\n"
                        "[00:07] CUSTOMER: أبغى عرض لتركيا لعائلة أربعة أشخاص",
        "kind": "q",
        "metadata": {"asr_confidence": 1, "diarization": "none",
                     "duration_seconds": 308.84, "channels": 1},
        "followup_history": None,
    }
    item.update(overrides)
    return item


# ── choosing a prompt ───────────────────────────────────────────────────────

def test_the_version_label_is_derived_from_the_filename():
    assert cd.version_label_for("pass1_customer_v4.md") == "pass1-customer-v4"
    assert cd.version_label_for("pass2_agent_quality_v3.md") == "pass2-agent-quality-v3"
    # The derivation must agree with what the judge hard-codes today, or an
    # A/A run and a normal run label the same prompt two different ways.
    assert cd.version_label_for(cd.judge.PASS1_PROMPT_FILE) == cd.judge.PASS1_VERSION
    assert cd.version_label_for(cd.judge.PASS2_PROMPT_FILE) == cd.judge.PASS2_VERSION


def test_choosing_a_prompt_moves_the_version_label_with_it():
    chosen = cd.select_prompts(Path("pass1_customer_v4.md"),
                               Path("pass2_agent_quality_v3.md"))
    assert chosen == {"pass1": "pass1_customer_v4.md",
                      "pass2": "pass2_agent_quality_v3.md"}
    assert cd.judge.PASS1_VERSION == "pass1-customer-v4"
    assert cd.judge.PASS2_VERSION == "pass2-agent-quality-v3"


def test_choosing_nothing_leaves_the_current_prompts_alone():
    before = (cd.judge.PASS1_PROMPT_FILE, cd.judge.PASS2_PROMPT_FILE)
    cd.select_prompts(None, None)
    assert (cd.judge.PASS1_PROMPT_FILE, cd.judge.PASS2_PROMPT_FILE) == before


def test_the_cache_key_sees_which_prompt_file_was_chosen():
    """The all-prompts fingerprint cannot: every version lives in one directory.

    Without this the A/A run reads back the B run's cached answers, Var(A) comes
    out equal to Var(B), the noise share reads 100%, and the conclusion is drawn
    from a cache hit.
    """
    item = _item()
    v4 = cd.cache_key(item, only_pass2=False)
    cd.select_prompts(None, Path("pass2_agent_quality_v3.md"))
    v3 = cd.cache_key(item, only_pass2=False)
    assert v4 != v3


def test_the_cache_key_still_sees_an_edited_prompt(tmp_path, monkeypatch):
    """A prompt edited without a version bump is what an iteration cycle does."""
    item = _item()
    before = cd.cache_key(item, only_pass2=False)
    real = cd.judge.PROMPT_DIR / cd.judge.PASS2_PROMPT_FILE
    original = real.read_bytes()
    try:
        real.write_bytes(original + b"\n<!-- one more sentence -->\n")
        assert cd.cache_key(item, only_pass2=False) != before
    finally:
        real.write_bytes(original)


def test_a_prompt_outside_the_prompts_directory_is_refused(tmp_path):
    """It would compose against channel_rules_*.md it cannot see."""
    stray = tmp_path / "pass2_agent_quality_v9.md"
    stray.write_text("hello", encoding="utf-8")
    with pytest.raises(SystemExit):
        cd.select_prompts(None, stray)


def test_a_missing_prompt_is_refused():
    with pytest.raises(SystemExit):
        cd.select_prompts(None, Path("pass2_agent_quality_v99.md"))


# ── follow-up history format ────────────────────────────────────────────────

LATER = [{
    "started_at": "2026-08-13 14:52", "channel": "phone_call", "kind": "q",
    "hours_after": 6.4, "first_message": None,
}, {
    "started_at": "2026-08-14 09:10", "channel": "whatsapp", "direction": "outbound",
    "agent_name": "خالد", "hours_after": 24.9, "first_message": "أرسلت لك العرض",
}]


def test_the_current_format_names_direction_and_handler():
    """The three things day 13 proved the old block could not say."""
    block = cd.render_current_history(LATER)
    assert block.startswith("Subsequent contact with this customer:")
    assert "INBOUND: the customer called in, this is not an agent follow-up" in block
    assert "no individual agent recorded (queue recording)" in block
    assert "handled by خالد" in block
    assert '"أرسلت لك العرض"' in block


def test_current_format_renders_from_later_interactions():
    text, source = cd.followup_source_for(
        _item(later_interactions=LATER), "current")
    assert source == "current"
    assert "Subsequent contact" in text


def test_current_format_falls_back_and_says_so():
    """The day-13 export: a stored string and no per-interaction rows.

    Silently sending the old block under a flag that promises the new one is
    how Module 4 comes to look tested when it was not.
    """
    stored = "  - [2026-08-13T14:52:55+00:00] phone_call by unknown, 6.4h after"
    text, source = cd.followup_source_for(
        _item(followup_history=stored, followup_history_now=stored), "current")
    assert source == "fallback-stored"
    assert text == stored


def test_a_precomputed_current_block_is_used_when_it_differs():
    text, source = cd.followup_source_for(
        _item(followup_history="old string",
              followup_history_now="Subsequent contact with this customer:\n  - new"),
        "current")
    assert source == "current-precomputed"
    assert text.startswith("Subsequent contact")


def test_none_is_still_the_literal_unavailable():
    """A Python None in the template reads as the word 'None' — a follow-up
    history the model would then try to grade."""
    for fmt in ("stored", "current"):
        assert cd.followup_for(_item(followup_history=None), fmt) == "unavailable"


def test_the_history_format_is_in_the_cache_key():
    item = _item(followup_history="old", later_interactions=LATER)
    assert cd.cache_key(item, False, "stored") != cd.cache_key(item, False, "current")


# ── the A/A arithmetic ──────────────────────────────────────────────────────

def _write_run(directory: Path, rows: list[dict]) -> None:
    import csv
    directory.mkdir(parents=True, exist_ok=True)
    fields = ["interaction_id", "old_final_score", "pre_enforcement_score",
              "new_final_score", "old_performance_level",
              "new_performance_level", "pre_enforcement_performance_level"]
    with (directory / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def _row(iid, old, pre, old_band="Good", pre_band="Good"):
    return {"interaction_id": iid, "old_final_score": old,
            "pre_enforcement_score": pre, "new_final_score": pre,
            "old_performance_level": old_band,
            "new_performance_level": pre_band,
            "pre_enforcement_performance_level": pre_band}


def test_identical_runs_report_all_of_the_spread_as_noise(tmp_path):
    """If A and B move identically, the prompt explains nothing.

    This is the outcome the metric exists to be able to state: noise share 1.0,
    prompt-attributable RMS 0, bias 0.
    """
    rows = [_row(f"id{i:02d}", 50.0, 50.0 + (i % 7) * 5) for i in range(20)]
    _write_run(tmp_path / "a", rows)
    _write_run(tmp_path / "b", rows)
    m = cd.aa_compare(tmp_path / "a", tmp_path / "b", tmp_path / "b" / "aa_report.md")

    assert m["n_paired"] == 20
    assert m["noise_share"] == pytest.approx(1.0)
    assert m["prompt_attributable_rms"] == pytest.approx(0.0)
    assert m["prompt_bias"] == pytest.approx(0.0)


def test_a_wider_b_run_leaves_prompt_attributable_movement(tmp_path):
    a = [_row(f"id{i:02d}", 50.0, 50.0 + (1 if i % 2 else -1)) for i in range(20)]
    b = [_row(f"id{i:02d}", 50.0, 50.0 + (10 if i % 2 else -10)) for i in range(20)]
    _write_run(tmp_path / "a", a)
    _write_run(tmp_path / "b", b)
    m = cd.aa_compare(tmp_path / "a", tmp_path / "b", tmp_path / "b" / "aa_report.md")

    assert m["var_a"] == pytest.approx(1.0)
    assert m["var_b"] == pytest.approx(100.0)
    assert m["noise_share"] == pytest.approx(0.01)
    assert m["prompt_attributable_rms"] == pytest.approx(99.0 ** 0.5)
    assert m["mae_a"] == pytest.approx(1.0)
    assert m["rmse_b"] == pytest.approx(10.0)


def test_a_shifted_b_run_is_bias_not_noise(tmp_path):
    """Same spread, moved up 8 points: the prompt grades kinder, full stop."""
    a = [_row(f"id{i:02d}", 50.0, 50.0 + (1 if i % 2 else -1)) for i in range(20)]
    b = [_row(f"id{i:02d}", 50.0, 58.0 + (1 if i % 2 else -1)) for i in range(20)]
    _write_run(tmp_path / "a", a)
    _write_run(tmp_path / "b", b)
    m = cd.aa_compare(tmp_path / "a", tmp_path / "b", tmp_path / "b" / "aa_report.md")

    assert m["prompt_bias"] == pytest.approx(8.0)
    assert m["noise_share"] == pytest.approx(1.0)
    assert m["prompt_attributable_rms"] == pytest.approx(0.0)


def test_band_flips_are_counted_on_both_sides(tmp_path):
    a = [_row("id1", 50.0, 50.0, "Average", "Average"),
         _row("id2", 50.0, 90.0, "Average", "Excellent")]
    b = [_row("id1", 50.0, 90.0, "Average", "Excellent"),
         _row("id2", 50.0, 90.0, "Average", "Excellent")]
    _write_run(tmp_path / "a", a)
    _write_run(tmp_path / "b", b)
    m = cd.aa_compare(tmp_path / "a", tmp_path / "b", tmp_path / "b" / "aa_report.md")
    assert (m["band_flips_a_pre"], m["band_flips_b_pre"]) == (1, 2)


def test_rows_without_a_comparable_old_score_are_excluded(tmp_path):
    """A pair whose stored old score differs is not the same comparison."""
    _write_run(tmp_path / "a", [_row("id1", 50.0, 55.0), _row("id2", 50.0, 55.0)])
    _write_run(tmp_path / "b", [_row("id1", 50.0, 60.0), _row("id2", 70.0, 60.0)])
    m = cd.aa_compare(tmp_path / "a", tmp_path / "b", tmp_path / "b" / "aa_report.md")
    assert m["n_paired"] == 1


def test_the_report_and_the_metrics_json_are_both_written(tmp_path):
    rows = [_row(f"id{i}", 50.0, 55.0) for i in range(5)]
    _write_run(tmp_path / "a", rows)
    _write_run(tmp_path / "b", rows)
    cd.aa_compare(tmp_path / "a", tmp_path / "b", tmp_path / "b" / "aa_report.md")

    text = (tmp_path / "b" / "aa_report.md").read_text(encoding="utf-8")
    assert "noise share" in text and "prompt-attributable RMS" in text
    saved = json.loads((tmp_path / "b" / "aa_metrics.json").read_text(encoding="utf-8"))
    assert saved["n_paired"] == 5


def test_a_missing_run_directory_is_a_clear_refusal(tmp_path):
    with pytest.raises(SystemExit):
        cd.aa_compare(tmp_path / "nope", tmp_path / "b", tmp_path / "aa.md")


# ── the M3 fixture file is where the runner expects it ──────────────────────

def test_the_m3_fixture_file_is_findable_from_the_script():
    assert cd.M3_FIXTURES.exists()
    assert json.loads(cd.M3_FIXTURES.read_text(encoding="utf-8"))["cases"]
