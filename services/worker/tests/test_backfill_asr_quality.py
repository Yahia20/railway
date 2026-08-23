"""The ASR quality backfill for legacy transcripts.

The job exists to decide which of 400-odd pre-cleaner call transcripts the D1
rule may publish, so the only failure that matters is a row reaching `green`
that should not have. Every case here is either a shape the staging data
actually contains or a shape Sol's review names as fail-closed.

Nothing here touches Postgres: the driver is exercised against an in-memory
stand-in that enforces the two things the real statements enforce — a row
already carrying a status is not written again, and the reset only ever sees
rows carrying a `backfill` sub-object.

The policy version is never asserted as a literal: another agent moves it
(asr-q1 -> asr-q2) and the contract is that the backfill stamps whatever the
cleaner says at runtime, not a string this script remembers.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(ROOT / "services" / "worker"))
from app.asr import text_quality  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "backfill_asr_quality", ROOT / "scripts" / "backfill_asr_quality.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_asr_quality"] = module
    spec.loader.exec_module(module)
    return module


bf = _load()

POLICY = text_quality.POLICY_VERSION

# The exact top-level keys `cohere_arabic.transcribe_call` puts in
# `asr_metrics` today. A backfilled row must be indistinguishable in shape, so
# this list is the contract — if the ASR writer grows a key, this test is where
# the backfill finds out.
PRODUCTION_KEYS = {
    "chunks_total", "chunks_failed", "chunks_empty", "chars", "clean_chars",
    "chars_per_audio_sec", "max_token_run", "repetition_suspect",
    "asr_quality_status", "quality", "cleaning",
}

CLEAN_AR = ("السلام عليكم معك احمد من ترافل جيت كيف اقدر اساعدك اليوم "
            "ابغى عرض سعر لرحلة الى تركيا لعائلة من اربعة اشخاص "
            "تمام ابشر راح ارسل لك البرنامج والاسعار على الواتساب "
            "طيب شكرا جزيلا لك في امان الله")


def _seg(seq, start, end, text):
    return {"seq": seq, "start_sec": start, "end_sec": end,
            "speaker": "unknown", "text": text}


def _row(text=None, *, duration=60.0, segments=None, metrics=None, tid="t-1",
         full_text=None):
    """A legacy transcript row as the SELECT hands it over.

    Defaults mirror the staging data: one segment spanning the call, and a
    `full_text` built by the SAME expression `transcribe_call` uses, so the
    fixture is reconciled by construction. Pass `full_text=` explicitly to
    build a row where the two copies disagree.
    """
    if segments is None:
        segments = [_seg(0, 0, duration, text or "")]
    if full_text is None:
        full_text = " ".join(
            s.get("text") or "" for s in segments
            if isinstance(s, dict) and s.get("text")).strip()
    return {"transcript_id": tid, "duration_seconds": duration,
            "full_text": full_text, "segments": segments,
            "asr_metrics": {} if metrics is None else metrics}


# ── classification ──────────────────────────────────────────────────────────

def test_clean_legacy_call_is_green():
    v = bf.assess_row(_row(CLEAN_AR))
    assert v["status"] == "green"
    assert v["reasons"] == []
    assert v["chunk_source"] == "segments"


def test_contamination_ambers_and_is_not_removed_from_stored_text():
    """A watermark the cleaner would strip is enough for amber on its own —
    and the reason names it, so an operator does not have to diff two texts."""
    row = _row(CLEAN_AR + " eddirasa.com")
    v = bf.assess_row(row)
    assert v["status"] == "amber"
    assert "contamination_removed" in v["reasons"]
    assert v["cleaning"]["ops"], "the removal must be recorded in the ledger"
    # The job never rewrites text; the ledger's offsets index the stored text.
    op = v["cleaning"]["ops"][0]
    stored = row["segments"][0]["text"]
    assert stored[op["raw_start"]:op["raw_end"]] == op["removed_text"]


def test_decoder_loop_over_a_whole_long_chunk_is_red():
    """A hard loop charges the chunk's whole duration to invalid audio. On a
    legacy row a chunk can be the entire call, so the 60-second span rule
    fires — which is the fail-closed direction and is meant to."""
    v = bf.assess_row(_row(CLEAN_AR + " نعم" * 40, duration=180.0))
    assert v["status"] == "red"
    assert "invalid_span_60s_or_more" in v["reasons"]


def test_green_amber_red_are_the_only_statuses():
    for row in (_row(CLEAN_AR), _row(CLEAN_AR + " eddirasa.com"),
                _row("", duration=90.0)):
        assert bf.assess_row(row)["status"] in {"green", "amber", "red"}


# ── F6 · a partial transcript can never be certified ────────────────────────

def test_the_join_expression_matches_the_one_the_producer_uses():
    """`transcribe_call` builds full_text as
    `" ".join(s.text for s in segments if s.text).strip()`. Empty chunks
    contribute nothing at all — not an extra space — and the result is
    stripped. If that expression changes, the reconciliation below starts
    rejecting healthy rows, so it is pinned here."""
    assert bf._producer_join(["a", "", "b"]) == "a b"
    assert bf._producer_join(["", ""]) == ""
    assert bf._producer_join([" a ", "b"]) == "a  b"


def test_full_text_that_disagrees_with_the_segments_is_red():
    """The two stored copies of the transcript are the only independent record
    of how much speech there was. If they disagree, one of them has been
    truncated and there is no way from here to know which."""
    row = _row(segments=[_seg(0, 0, 60, CLEAN_AR)],
               full_text=CLEAN_AR + " ولا كلمة زيادة")
    v = bf.assess_row(row)
    assert v["status"] == "red"
    assert "text_join_mismatch" in v["reasons"]


def test_a_dropped_segment_is_caught_by_the_chunks_total_reconciliation():
    """The row records that the ASR run produced 3 chunks. Two are stored. The
    call is a third shorter than it should be and every gate below would have
    graded the remainder as if it were the whole conversation."""
    row = _row(segments=[_seg(0, 0, 40, CLEAN_AR), _seg(1, 40, 80, CLEAN_AR)],
               duration=120.0, metrics={"chunks_total": 3})
    v = bf.assess_row(row)
    assert v["status"] == "red"
    assert "segment_count_mismatch" in v["reasons"]


@pytest.mark.parametrize("segments", [
    # seq not contiguous from zero: a chunk is simply missing from the array.
    [_seg(0, 0, 40, CLEAN_AR), _seg(2, 40, 80, CLEAN_AR)],
    # start after end.
    [_seg(0, 40, 10, CLEAN_AR)],
    # overlapping spans: the durations no longer partition the call, so the
    # 20 % coverage rule would be measuring against the wrong denominator.
    [_seg(0, 0, 60, CLEAN_AR), _seg(1, 30, 90, CLEAN_AR)],
    # text is not a string.
    [{"seq": 0, "start_sec": 0, "end_sec": 60, "text": None}],
    # not an object at all.
    ["just a string"],
])
def test_a_malformed_segment_is_red_and_never_silently_filtered(segments):
    row = _row(segments=segments, duration=120.0)
    v = bf.assess_row(row)
    assert v["status"] == "red"
    assert "segments_inconsistent" in v["reasons"]


def test_a_transcript_with_no_segments_is_red_not_graded_as_one_chunk():
    """There is nothing to reconcile `full_text` against, and "cannot be
    reconciled" is the whole point of the rule."""
    v = bf.assess_row(_row(CLEAN_AR, segments=[]))
    assert v["status"] == "red"
    assert v["reasons"] == ["segments_missing"]


def test_a_red_row_still_reports_what_could_be_observed():
    """The verdict is the finding; the counters are evidence for the operator
    who has to decide whether the row is worth re-transcribing."""
    v = bf.assess_row(_row(segments=[_seg(0, 0, 40, CLEAN_AR),
                                     _seg(2, 40, 80, CLEAN_AR)],
                           duration=120.0))
    assert v["computed"]["chars"] > 0
    assert v["computed"]["chunks_total"] == 2


# ── fail-closed ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("row, reason", [
    (_row("", duration=90.0), "empty_text_with_duration"),
    (_row("", duration=0.0, segments=[]), "segments_missing"),
    (_row(CLEAN_AR, duration=0.0), "unknown_duration"),
    (_row(CLEAN_AR, metrics={"chunks_total": 1, "chunks_failed": 1,
                             "chars": 0}), "transport_failure_no_chars"),
    (_row(CLEAN_AR, metrics={"chunks_total": 1, "chunks_failed": 3,
                             "chars": 200}), "chunks_failed_exceeds_segments"),
])
def test_broken_shapes_fail_closed_to_red_with_a_reason(row, reason):
    v = bf.assess_row(row)
    assert v["status"] == "red"
    assert v["reasons"] == [reason]


def test_an_unlocated_transport_failure_is_red_not_amber():
    """F14. The count of failed chunks is recorded, the location is not — on
    every row that exists today. Charging the loss to the longest chunk still
    produces a NUMBER, and a number is what green gets decided from. Fail
    closed and name it."""
    segments = [_seg(0, 0, 10, CLEAN_AR), _seg(1, 10, 120, CLEAN_AR)]
    v = bf.assess_row(_row(duration=120.0, segments=segments,
                           metrics={"chunks_total": 2, "chunks_failed": 1,
                                    "chars": 400}))
    assert v["status"] == "red"
    assert v["reasons"] == ["chunks_failed_unlocated"]


def test_a_located_transport_failure_is_graded_against_its_own_chunk():
    """If a writer ever does record which sequences were lost, the gate can do
    its real job: charge those seconds and nothing else."""
    segments = [_seg(0, 0, 10, CLEAN_AR), _seg(1, 10, 120, CLEAN_AR)]
    v = bf.assess_row(_row(duration=120.0, segments=segments,
                           metrics={"chunks_total": 2, "chunks_failed": 1,
                                    "chars": 400, "failed_seqs": [1]}))
    assert v["status"] == "red"          # 110 of 120 seconds is over 20 %
    assert v["quality"]["invalid_seconds"] == pytest.approx(110.0)
    assert "chunks_failed_unlocated" not in v["reasons"]


def test_a_failed_seqs_list_that_does_not_match_the_count_is_unlocated():
    segments = [_seg(0, 0, 10, CLEAN_AR), _seg(1, 10, 120, CLEAN_AR)]
    for bad in ([1], [0, 1, 5], ["1"]):
        v = bf.assess_row(_row(
            duration=120.0, segments=segments,
            metrics={"chunks_total": 2, "chunks_failed": 2, "chars": 400,
                     "failed_seqs": bad}))
        assert v["reasons"] == ["chunks_failed_unlocated"], bad


def test_an_empty_chunk_ambers_but_never_invalidates_audio():
    """Silence read correctly is not lost audio — `transcribe_call` counts the
    two separately, and conflating them would redden ordinary calls."""
    segments = [_seg(0, 0, 60, CLEAN_AR), _seg(1, 60, 120, "")]
    v = bf.assess_row(_row(duration=120.0, segments=segments))
    assert v["status"] == "amber"
    assert "empty_chunks" in v["reasons"]
    assert v["quality"]["invalid_seconds"] == 0


# ── the written object ──────────────────────────────────────────────────────

def test_metrics_have_the_production_shape_plus_provenance():
    row = _row(CLEAN_AR)
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z",
                         "v4-trial")
    assert PRODUCTION_KEYS <= set(m)
    assert set(m) - PRODUCTION_KEYS == {"backfill"}
    b = m["backfill"]
    assert b["backfilled_at"] == "2026-08-23T10:00:00Z"
    assert b["backfill_source"] == "stored_text"
    assert b["backfill_script"] == "backfill_asr_quality.py@v4-trial"
    assert b["text_rewritten"] is False
    assert b["original"] == {}


def test_the_policy_version_is_read_from_the_cleaner_never_hardcoded():
    """F15. The other half of this change moves POLICY_VERSION; the backfill
    must stamp whatever the module says at the moment it runs, so the script
    must not contain a policy-version literal at all."""
    row = _row(CLEAN_AR)
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z", "v")
    assert m["backfill"]["quality_policy_version"] == POLICY
    assert m["quality"]["quality_policy_version"] == POLICY
    assert m["cleaning"]["version"] == POLICY
    src = bf.SCRIPT_PATH.read_text(encoding="utf-8")
    assert not re.search(r"""['"]asr-q\d+['"]""", src)


def test_an_immutable_script_hash_is_stamped():
    """A branch name says what the operator thought they ran. This says what
    actually ran, and cannot be wrong on a dirty tree."""
    row = _row(CLEAN_AR)
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z", "v")
    sha = m["backfill"]["script_sha256"]
    assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)
    assert sha == bf.script_sha256()


def test_measured_transport_keys_are_preserved_verbatim():
    """The pre-cleaner rows carry what the original ASR run measured at
    transport time. That is not re-derivable from stored text, so the replay
    must keep it and never over-write it."""
    legacy = {"chars": 999, "chunks_total": 1, "chunks_failed": 0,
              "chunks_empty": 0, "max_token_run": 2,
              "repetition_suspect": False}
    row = _row(CLEAN_AR, metrics=dict(legacy))
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z", "v")
    for key, value in legacy.items():
        assert m[key] == value, f"{key} was over-written"
    assert set(m["backfill"]["preserved_keys"]) == set(legacy)


def test_stale_derived_keys_are_replaced_not_left_to_contradict_the_status():
    """F13. A `||` merge cannot remove anything, so a legacy row carrying a
    `quality` block or a `clean_chars` from some earlier experiment would keep
    it sitting next to — and disagreeing with — the status just written."""
    stale = {"chars": 999, "chunks_total": 1,
             "clean_chars": 1, "chars_per_audio_sec": 99.9,
             "quality": {"status": "green", "invalid_seconds": 0.0},
             "cleaning": {"version": "ancient", "ops": [], "flags": []},
             "flags": [{"flag": "left_over"}]}
    row = _row(CLEAN_AR, metrics=dict(stale))
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z", "v")
    assert m["quality"]["quality_policy_version"] == POLICY
    assert m["cleaning"]["version"] == POLICY
    assert m["clean_chars"] == len(CLEAN_AR)
    # chars_per_audio_sec is DERIVED (chars/duration), so it is recomputed from
    # the preserved `chars` rather than kept — the row cannot disagree with
    # itself about its own ratio.
    assert m["chars"] == 999
    assert m["chars_per_audio_sec"] == pytest.approx(round(999 / 60.0, 2))
    # A top-level `flags` is not part of the live shape; `cleaning.flags` is
    # the ledger, so the stale one is dropped rather than rewritten.
    assert "flags" not in m
    assert set(m["backfill"]["replaced_keys"]) == {
        "clean_chars", "chars_per_audio_sec", "quality", "cleaning", "flags"}


def test_an_unrecognised_key_is_left_alone():
    row = _row(CLEAN_AR, metrics={"some_future_key": {"a": 1}})
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z", "v")
    assert m["some_future_key"] == {"a": 1}


def test_metrics_are_json_serialisable_without_escaping_arabic():
    row = _row(CLEAN_AR + " eddirasa.com")
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z", "v")
    assert "eddirasa" in json.dumps(m, ensure_ascii=False)


# ── the undo ────────────────────────────────────────────────────────────────

def test_restore_returns_the_original_object_verbatim():
    original = {"chars": 999, "chunks_total": 1, "chunks_failed": 0,
                "chunks_empty": 0, "max_token_run": 2,
                "repetition_suspect": False, "chars_per_audio_sec": 16.65}
    row = _row(CLEAN_AR, metrics=dict(original))
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z", "v")
    restored, how = bf.restore_metrics(m)
    assert how == "original"
    assert restored == original


def test_restore_reconstructs_a_row_written_before_original_existed():
    """The 406 rows already on staging were written by the merge-only version,
    which recorded `preserved_keys` but not `original`. Dropping every key this
    script can write and putting the preserved ones back is exact for them —
    their pre-existing keys were measured ones or nothing at all."""
    original = {"chars": 999, "chunks_total": 1, "chunks_failed": 0,
                "chunks_empty": 0, "max_token_run": 2,
                "repetition_suspect": False, "chars_per_audio_sec": 16.65}
    legacy_written = dict(original)
    legacy_written.update({
        "clean_chars": 200, "asr_quality_status": "amber",
        "quality": {"status": "amber"},
        "cleaning": {"version": "asr-q1", "ops": [], "flags": []},
        "backfill": {"backfilled_at": "x",
                     "preserved_keys": list(original)},
    })
    restored, how = bf.restore_metrics(legacy_written)
    assert how == "legacy"
    assert restored == original


def test_restore_of_an_empty_legacy_row_returns_an_empty_object():
    written = {"chars": 10, "chunks_total": 1, "asr_quality_status": "green",
               "quality": {}, "cleaning": {}, "clean_chars": 10,
               "chars_per_audio_sec": 1.0, "chunks_failed": 0,
               "chunks_empty": 0, "max_token_run": 1,
               "repetition_suspect": False,
               "backfill": {"backfilled_at": "x"}}
    restored, how = bf.restore_metrics(written)
    assert how == "legacy"
    assert restored == {}


# ── the driver: resumability, idempotency, undo ─────────────────────────────

class _Cursor:
    def __init__(self, store):
        self.store = store
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        head = sql.strip().upper()
        backfilled = "? 'BACKFILL'" in head
        if head.startswith("SELECT"):
            if backfilled:
                rows = [r for r in self.store if "backfill" in r["asr_metrics"]]
            else:
                rows = [r for r in self.store
                        if "asr_quality_status" not in r["asr_metrics"]]
            rows.sort(key=lambda r: r["transcript_id"])
            if "LIMIT" in head:
                rows = rows[:int(sql.rsplit("LIMIT", 1)[1])]
            self._rows = [dict(r) for r in rows]
            self.rowcount = len(self._rows)
            return
        metrics, tid = json.loads(params[0]), params[1]
        for r in self.store:
            if r["transcript_id"] != tid:
                continue
            # The real WHERE clauses: still status-less for the write, still
            # carrying provenance for the reset.
            ok = ("backfill" in r["asr_metrics"] if backfilled
                  else "asr_quality_status" not in r["asr_metrics"])
            if ok:
                # SET, not merge — the whole object is replaced.
                r["asr_metrics"] = metrics
                self.rowcount = 1
                return
        self.rowcount = 0

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, store):
        self.store = store
        self.commits = 0

    def cursor(self):
        return _Cursor(self.store)

    def commit(self):
        self.commits += 1


def _store():
    return [_row(CLEAN_AR, tid=f"t-{i}") for i in range(5)] + [
        _row(CLEAN_AR + " eddirasa.com", tid="t-5"),
        _row("", duration=90.0, tid="t-6"),
    ]


def test_dry_run_writes_nothing_and_reports_the_distribution(capsys):
    store = _store()
    conn = _Conn(store)
    result = bf.run(conn, dry_run=True, limit=None, batch=100,
                    version="v4-trial")
    assert result["updated"] == 0
    assert result["counts"] == {"green": 5, "amber": 1, "red": 1}
    assert all("asr_quality_status" not in r["asr_metrics"] for r in store)
    assert conn.commits == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert POLICY in out


def test_second_run_is_a_no_op(capsys):
    store = _store()
    first = bf.run(_Conn(store), dry_run=False, limit=None, batch=100,
                   version="v4-trial")
    assert first["updated"] == 7
    assert all(r["asr_metrics"]["asr_quality_status"] for r in store)

    second = bf.run(_Conn(store), dry_run=False, limit=None, batch=100,
                    version="v4-trial")
    assert second["seen"] == 0
    assert second["updated"] == 0
    capsys.readouterr()


def test_rerunning_does_not_disturb_what_the_first_run_wrote(capsys):
    store = _store()
    bf.run(_Conn(store), dry_run=False, limit=None, batch=100,
           version="v4-trial")
    before = json.dumps(store, sort_keys=True, default=str)
    bf.run(_Conn(store), dry_run=False, limit=None, batch=100,
           version="v4-trial")
    assert json.dumps(store, sort_keys=True, default=str) == before
    capsys.readouterr()


def test_limit_leaves_the_rest_for_the_next_run(capsys):
    store = _store()
    first = bf.run(_Conn(store), dry_run=False, limit=3, batch=100,
                   version="v4-trial")
    assert first["updated"] == 3
    remaining = [r for r in store
                 if "asr_quality_status" not in r["asr_metrics"]]
    assert len(remaining) == 4

    second = bf.run(_Conn(store), dry_run=False, limit=None, batch=100,
                    version="v4-trial")
    assert second["updated"] == 4
    assert all("asr_quality_status" in r["asr_metrics"] for r in store)
    capsys.readouterr()


def test_batch_commits_as_it_goes_so_an_interrupted_run_keeps_its_work(capsys):
    conn = _Conn(_store())
    bf.run(conn, dry_run=False, limit=None, batch=2, version="v4-trial")
    # 3 mid-run commits at rows 2/4/6, plus the final one.
    assert conn.commits == 4
    capsys.readouterr()


def test_reset_restores_the_exact_pre_backfill_state(capsys):
    store = _store()
    # Half the rows carry the transport measurements, as on staging.
    for r in store[:3]:
        r["asr_metrics"] = {"chars": len(CLEAN_AR), "chunks_total": 1,
                            "chunks_failed": 0, "chunks_empty": 0,
                            "max_token_run": 1, "repetition_suspect": False,
                            "chars_per_audio_sec": 3.05}
    before = json.dumps(store, sort_keys=True, default=str)

    bf.run(_Conn(store), dry_run=False, limit=None, batch=100, version="v")
    assert all("backfill" in r["asr_metrics"] for r in store)

    out = bf.reset(_Conn(store), dry_run=False, batch=100)
    assert out["restored"] == 7
    assert out["how"] == {"original": 7}
    assert json.dumps(store, sort_keys=True, default=str) == before
    capsys.readouterr()


def test_reset_dry_run_writes_nothing(capsys):
    store = _store()
    bf.run(_Conn(store), dry_run=False, limit=None, batch=100, version="v")
    snapshot = json.dumps(store, sort_keys=True, default=str)
    out = bf.reset(_Conn(store), dry_run=True, batch=100)
    assert out["seen"] == 7 and out["restored"] == 0
    assert json.dumps(store, sort_keys=True, default=str) == snapshot
    capsys.readouterr()


def test_reset_leaves_rows_the_live_pipeline_assessed_alone(capsys):
    """Scope is `asr_metrics ? 'backfill'`, so a row the cleaner wrote is never
    in range no matter what its status is."""
    store = _store()
    live = _row(CLEAN_AR, tid="t-live")
    live["asr_metrics"] = {"asr_quality_status": "green", "chars": 100}
    store.append(live)
    bf.run(_Conn(store), dry_run=False, limit=None, batch=100, version="v")
    bf.reset(_Conn(store), dry_run=False, batch=100)
    assert live["asr_metrics"] == {"asr_quality_status": "green", "chars": 100}
    capsys.readouterr()


def test_reset_then_backfill_reproduces_the_same_distribution(capsys):
    store = _store()
    first = bf.run(_Conn(store), dry_run=False, limit=None, batch=100,
                   version="v")
    bf.reset(_Conn(store), dry_run=False, batch=100)
    second = bf.run(_Conn(store), dry_run=False, limit=None, batch=100,
                    version="v")
    assert second["counts"] == first["counts"]
    assert second["updated"] == first["updated"]
    capsys.readouterr()


# ── F7 · the alert-reconciliation guard ─────────────────────────────────────

class _ScalarCursor:
    def __init__(self, rows):
        self.rows = rows
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._row = self.rows["backlog"] if "alerts_evaluated_at" in sql \
            else self.rows["occurrences"]

    def fetchone(self):
        return self._row


class _ScalarConn:
    def __init__(self, backlog, occurrences):
        self.rows = {"backlog": backlog, "occurrences": occurrences}

    def cursor(self):
        return _ScalarCursor(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_backlog_query_reads_the_three_counters():
    conn = _ScalarConn({"never_attempted": 4, "failing": 1, "total": 5},
                       {"n": 12})
    assert bf.alert_backlog(conn) == {"never_attempted": 4, "failing": 1,
                                      "total": 5}
    assert bf.alert_occurrence_count(conn) == 12


def test_the_backlog_query_uses_the_reconciliation_definition():
    """Same population as scripts/sql/02_reconcile_alert_evaluations.sql and
    acceptance_pr1b I5: terminal, has an interaction, never stamped."""
    sql = " ".join(bf.BACKLOG_SQL.split())
    assert "status IN ('evaluated','judge_failed','dead_letter')" in sql
    assert "interaction_id IS NOT NULL" in sql
    assert "alerts_evaluated_at IS NULL" in sql


def test_the_script_never_writes_to_the_alert_tables():
    """F7(b). The backfill changes what the rules WOULD decide; it must not
    also decide anything itself. Checked over every SQL constant the module
    defines, so a new statement cannot be added without this noticing — prose
    that tells the operator to run the reconcile sweep is not a statement."""
    statements = {name: value for name, value in vars(bf).items()
                  if name.isupper() and isinstance(value, str)
                  and ("SELECT" in value or "UPDATE" in value)}
    assert statements, "the SQL constants moved; this test is now blind"
    for name, sql in statements.items():
        flat = " ".join(sql.split()).lower()
        if "alert" not in flat:
            continue
        assert not any(w in flat for w in ("insert", "update ", "delete")), name
        assert not any(fn in flat for fn in ("evaluate_alert_rules",
                                             "reconcile_alert_evaluations")), name
    assert "alert_occurrences" in bf.ALERT_OCCURRENCE_SQL
    assert bf.ALERT_OCCURRENCE_SQL.strip().lower().startswith("select count")


def test_main_refuses_to_run_while_reconciliation_is_behind(monkeypatch,
                                                            capsys):
    conn = _ScalarConn({"never_attempted": 686, "failing": 0, "total": 686},
                       {"n": 0})
    called = []
    monkeypatch.setattr(bf, "run", lambda *a, **k: called.append(1))
    rc = bf.main(["--database-url", "postgresql://x", "--dry-run"],
                 connect=lambda url: conn)
    assert rc == bf.EXIT_BACKLOG
    assert not called, "it must not touch a single row"
    assert "REFUSING TO RUN" in capsys.readouterr().err


def test_allow_backlog_overrides_the_guard(monkeypatch, capsys):
    conn = _ScalarConn({"never_attempted": 3, "failing": 0, "total": 3},
                       {"n": 0})
    called = []
    monkeypatch.setattr(bf, "run", lambda *a, **k: called.append(1))
    rc = bf.main(["--database-url", "postgresql://x", "--dry-run",
                  "--allow-backlog"], connect=lambda url: conn)
    assert rc == bf.EXIT_OK
    assert called
    assert "WARNING" in capsys.readouterr().err


def test_a_zero_backlog_lets_the_job_through(monkeypatch, capsys):
    conn = _ScalarConn({"never_attempted": 0, "failing": 0, "total": 0},
                       {"n": 41})
    called = []
    monkeypatch.setattr(bf, "run", lambda *a, **k: called.append(1))
    assert bf.main(["--database-url", "postgresql://x"],
                   connect=lambda url: conn) == bf.EXIT_OK
    assert called
    assert "41 before, 41 after (unchanged)" in capsys.readouterr().out


def test_a_changed_occurrence_count_is_reported_as_a_failure(monkeypatch,
                                                             capsys):
    """R-check. The job never writes the alert tables, so a change here means
    something else did while it ran — and the operator must see it."""
    conn = _ScalarConn({"never_attempted": 0, "failing": 0, "total": 0},
                       {"n": 41})

    def _bump(*a, **k):
        conn.rows["occurrences"] = {"n": 42}

    monkeypatch.setattr(bf, "run", _bump)
    assert bf.main(["--database-url", "postgresql://x"],
                   connect=lambda url: conn) != bf.EXIT_OK
    assert "CHANGED" in capsys.readouterr().out


def test_the_reset_path_does_not_need_a_reconciled_backlog(monkeypatch):
    """The undo only ever REMOVES amber/red, which restores 012's
    coalesce-to-green default rather than diverging from it."""
    conn = _ScalarConn({"never_attempted": 686, "failing": 0, "total": 686},
                       {"n": 0})
    called = []
    monkeypatch.setattr(bf, "reset", lambda *a, **k: called.append(1))
    assert bf.main(["--database-url", "postgresql://x", "--reset-backfilled"],
                   connect=lambda url: conn) == bf.EXIT_OK
    assert called


# ── asr-q2 alignment: the replay must speak the cleaner's dialect ───────────

def test_the_reasons_use_the_gates_own_speech_denominator():
    """asr-q2 measures density against `speech_chars()` — raw length minus the
    harmless control tokens inside it — precisely so a chatty decoder cannot
    move a status. If `_reasons` re-derived a denominator from `chars` it would
    report a density trigger the gate never fired, on a green row."""
    short = "نعم اوكي تمام شكرا لك يا اخي في امان الله كلام طيب جدا"
    padded = short + " " + " ".join(["<hesitation>"] * 40)
    row = _row(padded, duration=10.0)
    v = bf.assess_row(row)
    speech = v["quality"]["speech_chars"]
    assert speech < len(padded), "the tokens must leave the denominator"
    assert len(padded) / 10.0 > 22, "the fixture must be dense on RAW chars"
    assert speech / 10.0 <= 22, "and sparse on speech chars"
    assert "char_density_over_22_per_sec" not in v["reasons"]
    assert v["status"] == "green"


def test_a_lost_audio_marker_is_not_mistaken_for_harmless_punctuation():
    """`control_token_gap_v1` stands for audio nobody heard: it is a normal
    Tier-1 GAP removal, and a convention-based `_control_only` stand-in that
    matched on the substring `control_token` would call it status-neutral and
    silently drop `contamination_removed` from a row that lost speech."""
    v = bf.assess_row(_row(CLEAN_AR + " <inaudible>"))
    assert v["status"] in {"amber", "red"}
    assert "contamination_removed" in v["reasons"]
    assert any(op["pattern_id"] == text_quality.CONTROL_GAP_PID
               for op in v["cleaning"]["ops"])
    assert v["quality"]["control_token_gaps"] == 1


def test_the_control_only_predicate_is_the_cleaners_own():
    assert bf._control_only is not text_quality._control_only  # a thin wrapper
    assert bf._control_only(text_quality.CONTROL_TOKEN_PID) is True
    assert bf._control_only(text_quality.CONTROL_GAP_PID) is False


def test_a_red_row_carries_the_same_quality_keys_as_a_graded_one():
    """An operator reading `quality` should not have to know whether the gate
    was reached; a fail-closed verdict reports zeroes, not absences."""
    graded = bf.assess_row(_row(CLEAN_AR))["quality"]
    failed = bf.assess_row(_row(CLEAN_AR, segments=[]))["quality"]
    assert set(failed) == set(graded)
    assert {"control_token_gaps", "unknown_control_tokens",
            "speech_chars"} <= set(graded)


def test_a_stale_top_level_copy_of_a_quality_number_is_dropped():
    """asr-q2 put `speech_chars`, `control_token_gaps` and
    `unknown_control_tokens` inside `quality`. A legacy row carrying a
    top-level copy would be a second, older answer to the same question."""
    row = _row(CLEAN_AR, metrics={"speech_chars": 1, "control_token_gaps": 9,
                                  "unknown_control_tokens": 9})
    m = bf.build_metrics(row, bf.assess_row(row), "2026-08-23T10:00:00Z", "v")
    for key in ("speech_chars", "control_token_gaps",
                "unknown_control_tokens"):
        assert key not in m, key
    assert m["quality"]["speech_chars"] == len(CLEAN_AR)
    # ...and the undo still puts them back.
    restored, how = bf.restore_metrics(m)
    assert how == "original" and restored == row["asr_metrics"]
