"""Retrospective ASR quality assessment for transcripts written before the
cleaner shipped (2026-08-13).

Migration 014 carries Sol's D1 rule: a call evaluation may be published only
when its transcript's `asr_metrics->>'asr_quality_status'` is exactly `green`.
Fail-closed, so the 400-odd transcripts that predate the cleaner — they have no
`asr_quality_status` at all — are shadow-only forever unless something assesses
them. Re-transcribing is not on the table (the audio costs money and some of it
is gone), so this job replays the SAME deterministic, text-only policy over the
text those rows already hold. The policy version is read from
`app.asr.text_quality.POLICY_VERSION` at runtime and stamped on every row: this
script never names a policy version of its own.

    py -3.13 scripts/backfill_asr_quality.py --dry-run
    py -3.13 scripts/backfill_asr_quality.py
    py -3.13 scripts/backfill_asr_quality.py --reset-backfilled   # undo

This is a one-off data job, not a migration: it writes no DDL, it is resumable,
and running it twice is a no-op. It lives beside the other operator scripts
(`compare_day.py`, `evaluate_call.py`) rather than in `db/migrations/`.

Order of operations (F7)
------------------------
`evaluate_alert_rules()` in `013_alert_rules.sql` coalesces a MISSING
`asr_quality_status` to `'green'` — deliberately, because an alert on a call
whose quality nobody recorded is still worth a human's attention. That default
is the opposite of D1's, and it means this job silently changes what the alert
rules would decide for every row it touches: a lead that would have raised
`hot_real_ask_uncommitted` under the coalesce stops raising it the moment the
row reads `amber`.

So RECONCILE FIRST, THEN BACKFILL. The job refuses to start while any terminal
`call_ingest_jobs` row still has `alerts_evaluated_at IS NULL` (the backlog
definition from `scripts/sql/02_reconcile_alert_evaluations.sql` and
`acceptance_pr1b.sql` I5) unless `--allow-backlog` is passed. It never inserts,
updates or deletes `alert_occurrences` and never calls `evaluate_alert_rules()`
or `reconcile_alert_evaluations()`; the occurrence count is read before and
after purely as evidence that it did not.

What it does NOT do, deliberately
---------------------------------
It does not rewrite `full_text` or `segments`. Two reasons, and the second is
the one that matters:

* The stored text of a legacy row is RAW — verified on staging, `full_text`
  equals the join of the segment texts and equals the legacy `chars` count, so
  nothing has been cleaned out of it yet. That equality is now CHECKED per row
  rather than assumed (see "Reconciliation" below).
* A `green` call has by definition had NOTHING removed (`any_removal` forces
  amber), so for exactly the rows the D1 rule lets through, clean text and raw
  text are the same string. Rewriting text would change nothing for green rows
  and would rewrite history for amber/red rows that stay shadow-only anyway —
  while destroying the raw text the ledger's offsets are recorded against.

So the cleaning ledger written here records offsets into the text that is
actually stored, which is the raw text. In a production row the ledger's
offsets refer to the raw chunk text and the STORED text is the clean one; here
they refer to the raw chunk text and the stored text is also the raw one.
Ops are recorded against raw in both cases. `backfill.text_rewritten` is
`false` on every row this job touches so the difference is never a guess.

Reconciliation (F6)
-------------------
A partial transcript that LOOKS whole is the one input that can certify a call
that should never be published, and nothing downstream can catch it: pass 2
scores the words it was given. So before any grading happens every segment is
validated and the array is reconciled against the two independent records of
what the transcript should contain:

* every element is an object, `text` is a string, `seq` is an integer,
  `start_sec`/`end_sec` are numbers with `start <= end`;
* `seq` runs contiguously from 0 in stored order, and the spans are monotone
  and non-overlapping (`start[i] >= end[i-1]`);
* `len(segments)` equals `chunks_total` when the row records one;
* `" ".join(s.text for s in segments if s.text).strip()` — character for
  character the expression `cohere_arabic.transcribe_call` uses to build
  `full_text` — equals the stored `full_text`.

Any failure is `red` with the reason named (`segments_inconsistent`,
`segment_count_mismatch`, `text_join_mismatch`, `segments_missing`) and can
never be green or amber. A row with no segment array is no longer graded from
`full_text` as a single chunk: there is nothing to reconcile the text against,
and "cannot be reconciled" is exactly the case this rule exists for.

What it writes
--------------
The same top-level `asr_metrics` shape `cohere_arabic.transcribe_call` writes
today, so a backfilled row is indistinguishable in shape from a live one, plus
one extra `backfill` sub-object carrying provenance, the human-readable reasons
behind the status, and the untouched original object for undo.

The write is one UPDATE per row that SETs the whole `asr_metrics` object, not a
`||` merge (F13). A merge cannot remove anything, so a legacy row carrying a
stale `quality`, `cleaning`, `clean_chars` or top-level `flags` from some
earlier experiment would keep it, sitting next to — and contradicting — the
status this job just wrote. The split is:

* MEASURED transport keys — `chunks_total`, `chunks_failed`, `chunks_empty`,
  `chars`, `max_token_run`, `repetition_suspect` — are PRESERVED verbatim when
  the row has them and fed INTO the assessment. They record what the original
  ASR run measured at transport time; that is not re-derivable from stored text
  and the measurement beats the replay.
* DERIVED keys — `quality`, `cleaning`, `clean_chars`, `chars_per_audio_sec`,
  `asr_quality_status`, and any stale top-level `flags` — are REPLACED
  wholesale by this run's values, atomically with the status. Note
  `chars_per_audio_sec` is derived, not measured: it is `chars / duration`, and
  a preserved `chars` with a recomputed ratio cannot disagree with itself.
* Any other key the row happens to carry is left alone.

`backfill.original` holds the entire pre-existing `asr_metrics` object, so
`--reset-backfilled` restores the exact pre-backfill state rather than
approximating it.

Fail-closed
-----------
`assess_call` is a gate, not a scorer, and every input this job cannot verify
resolves toward red. Empty text, no segments, an unreconcilable segment array,
a transport failure whose location is unknowable, a call with no duration to
measure coverage against — all red with a named reason. `green` is only ever
reached by a row that positively passed the gate on complete, reconciled
inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))

from app.asr import text_quality                            # noqa: E402
from app.asr.text_quality import ChunkQuality               # noqa: E402

# Keys the original ASR run measured at transport time. Not re-measurable from
# stored text, so an existing value always wins over a recomputed one.
# `chars_per_audio_sec` is deliberately NOT here: it is chars/duration, a
# derived ratio, and preserving it beside a recomputed `chars` would let the
# row contradict itself.
MEASURED_KEYS = ("chunks_total", "chunks_failed", "chunks_empty", "chars",
                 "max_token_run", "repetition_suspect")

# Keys this job owns outright: whatever the row held for them is replaced, in
# the same statement that writes the status, so nothing stale can survive to
# contradict it.
#
# The last four are not part of the live top-level shape — `flags` belongs
# under `cleaning`, and asr-q2's `control_token_gaps`, `unknown_control_tokens`
# and `speech_chars` belong under `quality`. They are listed because a legacy
# row can still be carrying one from an earlier experiment, and a stale
# top-level copy of a number that now lives inside `quality` is exactly the
# contradiction F13 is about. Being in this list means "dropped, never
# rewritten": `quality` and `cleaning.flags` are the authoritative homes.
DERIVED_KEYS = ("quality", "cleaning", "clean_chars", "chars_per_audio_sec",
                "asr_quality_status", "flags", "control_token_gaps",
                "unknown_control_tokens", "speech_chars")

# Everything this script may write. Used by the legacy reset path to work out
# which keys it added to a row written before `backfill.original` existed.
WRITTEN_KEYS = frozenset(MEASURED_KEYS) | frozenset(DERIVED_KEYS) | {"backfill"}

BACKFILL_SOURCE = "stored_text"
SCRIPT_NAME = "backfill_asr_quality.py"
SCRIPT_PATH = Path(__file__).resolve()

SELECT_SQL = """
SELECT transcript_id, duration_seconds, full_text, segments, asr_metrics
  FROM transcripts
 WHERE asr_metrics->>'asr_quality_status' IS NULL
 ORDER BY transcript_id
"""

# The whole object is written, not merged (see the module docstring, F13). The
# WHERE clause repeats the "still lacking a status" test so a row another
# process assessed between the SELECT and the UPDATE is left alone: the job is
# resumable AND safe to run twice at once.
UPDATE_SQL = """
UPDATE transcripts
   SET asr_metrics = %s::jsonb
 WHERE transcript_id = %s
   AND asr_metrics->>'asr_quality_status' IS NULL
"""

RESET_SELECT_SQL = """
SELECT transcript_id, asr_metrics
  FROM transcripts
 WHERE asr_metrics ? 'backfill'
 ORDER BY transcript_id
"""

RESET_UPDATE_SQL = """
UPDATE transcripts
   SET asr_metrics = %s::jsonb
 WHERE transcript_id = %s
   AND asr_metrics ? 'backfill'
"""

# The reconciliation backlog, in the words of acceptance_pr1b.sql I5 and
# scripts/sql/02_reconcile_alert_evaluations.sql. Copied rather than imported
# so the guard cannot drift silently from the definition it claims to use;
# acceptance is where the two are compared.
BACKLOG_SQL = """
SELECT count(*) FILTER (WHERE alerts_error IS NULL)     AS never_attempted,
       count(*) FILTER (WHERE alerts_error IS NOT NULL) AS failing,
       count(*)                                         AS total
  FROM call_ingest_jobs
 WHERE status IN ('evaluated','judge_failed','dead_letter')
   AND interaction_id      IS NOT NULL
   AND alerts_evaluated_at IS NULL
"""

ALERT_OCCURRENCE_SQL = "SELECT count(*) AS n FROM alert_occurrences"

EXIT_OK = 0
EXIT_NO_URL = 2
EXIT_BACKLOG = 3


# ---------------------------------------------------------------------------
# Version stamp
# ---------------------------------------------------------------------------

def script_sha256() -> str:
    """sha256 of this file's bytes.

    The git stamp below says which branch or commit the operator *thought* they
    were running; this says what actually ran. It is immutable, survives a
    dirty tree, and is the only provenance field that cannot be wrong.
    """
    try:
        return hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest()
    except OSError:
        return "unknown"


def script_version() -> str:
    """`<sha>` when the tree is clean, else the branch name.

    A commit sha on a dirty tree would name a commit that does not contain the
    script that ran, which is worse than no sha at all. Kept alongside
    `script_sha256()` because a branch name is what an operator recognises.
    """
    try:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, timeout=15)
        if dirty.returncode == 0 and not dirty.stdout.strip():
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 cwd=ROOT, capture_output=True, text=True,
                                 timeout=15)
            if sha.returncode == 0 and sha.stdout.strip():
                return sha.stdout.strip()
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=ROOT, capture_output=True, text=True,
                                timeout=15)
        if branch.returncode == 0 and branch.stdout.strip():
            return branch.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _control_only(pattern_id: str) -> bool:
    """The cleaner's own predicate, never a local re-implementation.

    Deliberately NOT a `getattr` with a fallback. asr-q2 has two control-token
    pattern ids that differ in exactly the way that matters —
    `control_token_v1` is harmless machine punctuation, `control_token_gap_v1`
    stands for LOST AUDIO and is Tier-1 counted — and any convention-based
    stand-in (say, "the id contains `control_token`") would call the second one
    harmless and quietly stop the backfill from reporting
    `contamination_removed` on a row that lost speech. If this attribute ever
    disappears the job must fail loudly at import, not guess.
    """
    return text_quality._control_only(pattern_id)


# ---------------------------------------------------------------------------
# Segment validation and reconciliation (F6)
# ---------------------------------------------------------------------------

def _producer_join(texts: list[str]) -> str:
    """`full_text` exactly as `cohere_arabic.transcribe_call` builds it:

        full_text = " ".join(s.text for s in segments if s.text).strip()

    Falsy texts are dropped (so an empty chunk contributes no double space),
    the rest are joined by one space, and the result is stripped. Mirrored here
    rather than imported because importing `cohere_arabic` drags in the gradio
    backend; the expression is one line and this test is what catches it
    changing.
    """
    return " ".join(t for t in texts if t).strip()


def _as_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_segments(segments, full_text: str, existing: dict
                      ) -> tuple[list[str], list[str], list[float], list[int]]:
    """Validate every segment and reconcile the array with the row's own
    records of what it should contain.

    Returns `(problems, texts, durations, seqs)`. A non-empty `problems` list
    means the row is unreconcilable and must be red; the other three are
    returned anyway, best effort, so the observed counters can still be
    reported on the red row.
    """
    problems: list[str] = []
    if not isinstance(segments, list) or not segments:
        return ["segments_missing"], [], [], []

    texts: list[str] = []
    durations: list[float] = []
    seqs: list[int] = []
    structural = False
    prev_end: float | None = None

    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            structural = True
            texts.append("")
            durations.append(0.0)
            seqs.append(i)
            continue

        text = seg.get("text")
        if not isinstance(text, str):
            # A null or a number where the transcript should be. Whatever it
            # is, it is not speech, and grading around it would silently
            # shorten the call.
            structural = True
            text = ""

        seq = seg.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            structural = True
            seq = i

        start = _as_float(seg.get("start_sec"))
        end = _as_float(seg.get("end_sec"))
        if start is None or end is None or end < start:
            structural = True
            start = 0.0 if start is None else start
            end = start if end is None or end < start else end

        if prev_end is not None and start + 1e-6 < prev_end:
            # Overlapping or out-of-order spans: the durations no longer add up
            # to a partition of the call, so `invalid_seconds` and the 20 %
            # coverage rule are measuring against a denominator that is wrong.
            structural = True
        prev_end = end

        texts.append(text)
        durations.append(max(0.0, end - start))
        seqs.append(seq)

    if seqs != list(range(len(segments))):
        # Contiguous from zero, in stored order. `transcribe_call` writes
        # `seq=i` over `range(len(cuts) - 1)`, so a gap means a chunk is simply
        # missing from the array and the text is short by however long it was.
        structural = True

    if structural:
        problems.append("segments_inconsistent")

    declared_total = existing.get("chunks_total")
    if isinstance(declared_total, int) and not isinstance(declared_total, bool):
        if declared_total != len(segments):
            problems.append("segment_count_mismatch")

    if _producer_join(texts) != (full_text or ""):
        # The two stored copies of the transcript disagree. One of them has
        # been edited, truncated or written by something that is not the ASR
        # writer, and there is no way from here to know which.
        problems.append("text_join_mismatch")

    return problems, texts, durations, seqs


# ---------------------------------------------------------------------------
# Assessment of one row
# ---------------------------------------------------------------------------

def _red(reasons: list[str] | str, computed: dict, chunk_source: str) -> dict:
    """A fail-closed verdict: the gate was never reached, so the `quality`
    block records zeroes rather than measurements it did not make. The reasons
    are the finding; the counts are whatever could be observed."""
    if isinstance(reasons, str):
        reasons = [reasons]
    quality = {
        "status": "red",
        "invalid_seconds": 0.0,
        "longest_invalid_span_seconds": 0.0,
        "tier1_chars_removed": 0,
        "control_tokens_removed": 0,
        "control_token_gaps": 0,
        "unknown_control_tokens": 0,
        "speech_chars": 0,
        "quality_policy_version": text_quality.POLICY_VERSION,
    }
    return {"status": "red", "quality": quality, "reasons": list(reasons),
            "cleaning": {"version": text_quality.POLICY_VERSION,
                         "ops": [], "flags": []},
            "chunk_source": chunk_source, "computed": computed}


def _observed(texts: list[str], duration: float, existing: dict) -> dict:
    """The production metric keys, with any value the original run MEASURED
    taking precedence and every DERIVED value recomputed from this run."""
    raw_full = _producer_join(texts)
    raw_chars = int(existing["chars"]) if "chars" in existing else len(raw_full)
    empty = int(existing["chunks_empty"]) if "chunks_empty" in existing \
        else sum(1 for t in texts if not t.strip())
    token_run = int(existing["max_token_run"]) if "max_token_run" in existing \
        else _max_token_run(raw_full)
    suspect = bool(existing["repetition_suspect"]) \
        if "repetition_suspect" in existing else token_run >= 6
    return {
        "chunks_total": int(existing.get("chunks_total") or len(texts)),
        "chunks_failed": int(existing.get("chunks_failed") or 0),
        "chunks_empty": empty,
        "chars": raw_chars,
        "clean_chars": 0,
        # Derived, always recomputed: chars/duration, from whichever `chars`
        # this row is actually carrying.
        "chars_per_audio_sec": round(raw_chars / duration, 2) if duration
        else 0.0,
        "max_token_run": token_run,
        "repetition_suspect": suspect,
    }


def _reasons(quality: dict, chunk_results: list[ChunkQuality],
             invalid: list[bool], duration: float, density: float,
             clean_chars: int, speech: int, chunks_empty: int) -> list[str]:
    """Why this row landed where it did, in the gate's own terms.

    `assess_call` returns a status and the numbers behind it but not which
    trigger fired, and an operator reading a red row needs the trigger. These
    conditions mirror the ones in `assess_call`; they are read off the same
    inputs and numbers it was given, never recomputed from a different source.

    `speech` and `density` are the gate's OWN denominators, not raw character
    counts: asr-q2 measures both against `text_quality.speech_chars()` — raw
    length minus the harmless control tokens inside it — so that a chatty
    decoder cannot move a status. Re-deriving a denominator here would let the
    reasons disagree with the status they are supposed to explain, which is
    worse than having no reasons at all.
    """
    out: list[str] = []
    tier1 = quality["tier1_chars_removed"]
    if clean_chars < 20:
        out.append("clean_text_under_20_chars")
    if duration and quality["invalid_seconds"] > 0.20 * duration:
        out.append("invalid_audio_over_20pct")
    if quality["longest_invalid_span_seconds"] >= 60.0:
        out.append("invalid_span_60s_or_more")
    if tier1 >= 40 and speech and tier1 >= 0.25 * speech:
        out.append("contamination_over_25pct_of_text")

    if quality["invalid_seconds"] > 0:
        out.append("invalid_audio")
    if any(cq.warn_run and not inv for cq, inv in zip(chunk_results, invalid)):
        out.append("token_run_6_15")
    if any(not _control_only(op["pattern_id"])
           for cq in chunk_results for op in cq.ops):
        out.append("contamination_removed")
    if any(cq.flags for cq in chunk_results):
        out.append("tier2_flag")
    if duration >= 30 and density < 3:
        out.append("char_density_under_3_per_sec")
    if chunks_empty > 0:
        out.append("empty_chunks")
    if density > 22 and not any(cq.hard_loop for cq in chunk_results):
        out.append("char_density_over_22_per_sec")
    return out


def _declared_failed_seqs(existing: dict, n_segments: int) -> set[int] | None:
    """The EXACT sequences a transport failure hit, or None if unrecorded.

    `transcribe_call` knows which chunks failed — it holds `failed_seqs` while
    it runs — but it only ever persisted the COUNT. If a future writer starts
    recording the list, this reads it; for every row that exists today the
    answer is None, and F14 says an unlocated failure is red.
    """
    raw = existing.get("failed_seqs")
    if not isinstance(raw, list) or not raw:
        return None
    out: set[int] = set()
    for v in raw:
        if not isinstance(v, int) or isinstance(v, bool):
            return None
        if not 0 <= v < n_segments:
            return None
        out.add(v)
    return out or None


def assess_row(row: dict) -> dict:
    """Replay the cleaner's gate over one stored transcript. Never raises.

    Returns {status, quality, cleaning, reasons, computed, chunk_source}.
    """
    duration = float(row.get("duration_seconds") or 0.0)
    full_text = row.get("full_text") or ""
    segments = row.get("segments")
    existing = row.get("asr_metrics") or {}
    if not isinstance(existing, dict):
        existing = {}

    problems, texts, durations, seqs = validate_segments(
        segments, full_text, existing)
    chunk_source = "segments" if texts else "none"
    observed = _observed(texts, duration, existing)

    if problems:
        # Unreconcilable. Never green, never amber, and the reason names which
        # of the three records disagreed.
        return _red(problems, observed, chunk_source)

    if not "".join(texts).strip():
        # Nothing was transcribed at all. With audio behind it that is a
        # transport failure however the row got here; without, there is no
        # call to grade. Either way it is not gradeable.
        return _red("empty_text_with_duration" if duration > 0
                    else "empty_transcript", observed, chunk_source)

    if duration <= 0:
        # No audio length means no denominator: `invalid_seconds`, the 20%
        # coverage rule and the density floors all silently evaluate to
        # "fine". A row that cannot be measured must not be certified.
        return _red("unknown_duration", observed, chunk_source)

    # Transport failures. The count is recorded; the location is not, on every
    # row that exists today. Guessing which chunks were lost — even
    # pessimistically — still produces a NUMBER, and a number is what green is
    # decided from. Fail closed and name it instead (F14).
    failed_seqs: set[int] = set()
    declared_failed = observed["chunks_failed"]
    if declared_failed > 0:
        if observed["chars"] == 0:
            return _red("transport_failure_no_chars", observed, chunk_source)
        if declared_failed > len(texts):
            return _red("chunks_failed_exceeds_segments", observed,
                        chunk_source)
        located = _declared_failed_seqs(existing, len(texts))
        if located is None or len(located) != declared_failed:
            return _red("chunks_failed_unlocated", observed, chunk_source)
        failed_seqs = located

    chunk_results = [text_quality.clean_chunk(t, seq=q)
                     for t, q in zip(texts, seqs)]
    clean_full = " ".join(cq.clean_text for cq in chunk_results
                          if cq.clean_text).strip()
    clean_chars = len(clean_full.replace(text_quality.GAP, ""))

    raw_chars = observed["chars"]
    # An empty chunk is silence or music read correctly, NOT a lost chunk
    # (`transcribe_call` counts the two separately and only failures move
    # `confidence`). It still ambers the call; it never invalidates audio.
    chunks_empty = observed["chunks_empty"]

    quality = text_quality.assess_call(
        chunk_results,
        chunk_durations=durations,
        failed_seqs=failed_seqs,
        total_duration=duration,
        clean_chars=clean_chars,
        raw_chars=raw_chars,
        chunks_empty=chunks_empty,
    )

    # The gate's own denominator (asr-q2 `speech_chars`: raw length minus the
    # harmless control tokens inside it), taken from the block it just
    # returned, so the reasons cannot drift from the status.
    speech = int(quality.get("speech_chars",
                             text_quality.speech_chars(chunk_results,
                                                       raw_chars)))
    density = (speech / duration) if duration else 0.0
    invalid = [i in failed_seqs or cq.hard_loop
               or (cq.warn_run > 0 and (cq.ngram_fraction >= 0.45
                                        or density > 22))
               for i, cq in enumerate(chunk_results)]
    reasons = _reasons(quality, chunk_results, invalid, duration, density,
                       clean_chars, speech, chunks_empty)

    return {
        "status": quality["status"],
        "quality": quality,
        "cleaning": {
            "version": text_quality.POLICY_VERSION,
            "ops": [op for cq in chunk_results for op in cq.ops],
            "flags": [f for cq in chunk_results for f in cq.flags],
        },
        "reasons": reasons,
        "chunk_source": chunk_source,
        "computed": {**observed, "clean_chars": clean_chars},
    }


def _max_token_run(text: str) -> int:
    """Same measure `cohere_arabic._max_token_run` records, duplicated here so
    the backfill does not depend on the ASR module's private helpers."""
    best = run = 0
    prev = None
    for tok in text.split():
        run = run + 1 if tok == prev else 1
        prev = tok
        best = max(best, run)
    return best


# ---------------------------------------------------------------------------
# The row's new asr_metrics
# ---------------------------------------------------------------------------

def build_metrics(row: dict, verdict: dict, now: str, version: str,
                  sha256: str | None = None) -> dict:
    """The COMPLETE `asr_metrics` object to write (F13).

    Measured transport keys survive verbatim; every derived key is replaced by
    this run's value in the same statement as the status, so nothing stale can
    outlive it; anything else the row carries is left alone; and the original
    object is kept under `backfill.original` so the write is exactly
    reversible.
    """
    existing = row.get("asr_metrics") or {}
    if not isinstance(existing, dict):
        existing = {}

    computed = verdict.get("computed") or {}
    fresh = {
        "chunks_total": computed.get("chunks_total", 0),
        "chunks_failed": computed.get("chunks_failed", 0),
        "chunks_empty": computed.get("chunks_empty", 0),
        "chars": computed.get("chars", 0),
        "clean_chars": computed.get("clean_chars", 0),
        "chars_per_audio_sec": computed.get("chars_per_audio_sec", 0.0),
        "max_token_run": computed.get("max_token_run", 0),
        "repetition_suspect": computed.get("repetition_suspect", False),
        "asr_quality_status": verdict["status"],
        "quality": verdict["quality"],
        "cleaning": verdict["cleaning"],
    }

    preserved = [k for k in MEASURED_KEYS if k in existing]
    replaced = [k for k in DERIVED_KEYS if k in existing]

    # Start from whatever the row already had, minus the keys this job owns,
    # so an unrecognised key survives untouched...
    metrics = {k: v for k, v in existing.items() if k not in WRITTEN_KEYS}
    # ...then the measured keys, existing value winning...
    for key in MEASURED_KEYS:
        metrics[key] = existing[key] if key in existing else fresh[key]
    # ...then every derived key, this run's value always winning. `flags` is in
    # DERIVED_KEYS but is not part of the live shape, so a stale one is dropped
    # rather than rewritten: `cleaning.flags` is the ledger.
    for key in ("clean_chars", "chars_per_audio_sec", "asr_quality_status",
                "quality", "cleaning"):
        metrics[key] = fresh[key]

    backfill = {
        "quality_policy_version": text_quality.POLICY_VERSION,
        "backfilled_at": now,
        "backfill_source": BACKFILL_SOURCE,
        "backfill_script": f"{SCRIPT_NAME}@{version}",
        "script_sha256": script_sha256() if sha256 is None else sha256,
        "chunk_source": verdict.get("chunk_source", "segments"),
        # The stored text is left exactly as it was; see the module docstring.
        "text_rewritten": False,
        "reasons": verdict.get("reasons", []),
        # The undo record. `--reset-backfilled` restores this verbatim, so the
        # pre-backfill state is recovered rather than reconstructed.
        "original": existing,
    }
    if preserved:
        backfill["preserved_keys"] = preserved
    if replaced:
        backfill["replaced_keys"] = replaced
    metrics["backfill"] = backfill
    return metrics


def restore_metrics(existing: dict) -> tuple[dict, str]:
    """The pre-backfill `asr_metrics` for a row this job wrote.

    Returns `(metrics, how)`. `how` is `"original"` when the row carries
    `backfill.original` — exact, and the only path a fresh run produces — or
    `"legacy"` for rows written before that key existed, where the original is
    reconstructed from `backfill.preserved_keys`: every key this script can
    write is dropped, and the ones the earlier run recorded as pre-existing are
    put back. That reconstruction is exact for a row whose only pre-existing
    keys were measured ones, which is every legacy row on staging (183 with the
    seven transport keys, 223 with an empty object).
    """
    backfill = existing.get("backfill")
    if not isinstance(backfill, dict):
        return dict(existing), "legacy"

    original = backfill.get("original")
    if isinstance(original, dict):
        return dict(original), "original"

    preserved = backfill.get("preserved_keys")
    preserved = preserved if isinstance(preserved, list) else []
    metrics = {k: v for k, v in existing.items() if k not in WRITTEN_KEYS}
    for key in preserved:
        if isinstance(key, str) and key in existing:
            metrics[key] = existing[key]
    return metrics, "legacy"


# ---------------------------------------------------------------------------
# Alert-reconciliation guard (F7)
# ---------------------------------------------------------------------------

def alert_backlog(conn) -> dict:
    """Terminal call_ingest_jobs whose alert evaluation never landed."""
    with conn.cursor() as cur:
        cur.execute(BACKLOG_SQL)
        row = cur.fetchone()
    if row is None:
        return {"never_attempted": 0, "failing": 0, "total": 0}
    if isinstance(row, dict):
        return {k: int(row.get(k) or 0)
                for k in ("never_attempted", "failing", "total")}
    return {"never_attempted": int(row[0]), "failing": int(row[1]),
            "total": int(row[2])}


def alert_occurrence_count(conn) -> int:
    """Rows in `alert_occurrences`. Read-only, and read only as evidence: this
    job must leave the number exactly where it found it."""
    with conn.cursor() as cur:
        cur.execute(ALERT_OCCURRENCE_SQL)
        row = cur.fetchone()
    if row is None:
        return 0
    return int(row["n"] if isinstance(row, dict) else row[0])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _summary(counts: Counter, reasons: Counter, total: int, title: str) -> str:
    lines = [f"\n{title}", "-" * len(title),
             f"{'status':<10}{'rows':>7}{'share':>9}"]
    for st in ("green", "amber", "red"):
        n = counts.get(st, 0)
        share = f"{(100.0 * n / total):.1f}%" if total else "-"
        lines.append(f"{st:<10}{n:>7}{share:>9}")
    lines.append(f"{'TOTAL':<10}{total:>7}")
    if reasons:
        lines += ["", "top reasons", "-----------"]
        for reason, n in reasons.most_common(12):
            lines.append(f"  {reason:<36}{n:>6}")
    return "\n".join(lines)


def run(conn, *, dry_run: bool, limit: int | None, batch: int,
        version: str, out=None) -> dict:
    # Resolved here, not in the signature: a default bound at import time
    # would keep writing to the real stdout when a caller has replaced it.
    out = sys.stdout if out is None else out
    now = datetime.now(timezone.utc).isoformat()
    sha256 = script_sha256()
    counts: Counter = Counter()
    reasons: Counter = Counter()
    sources: Counter = Counter()
    updated = 0
    seen = 0
    samples: dict[str, dict] = {}

    sql = SELECT_SQL + ("" if limit is None else f" LIMIT {int(limit)}")
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    with conn.cursor() as cur:
        for row in rows:
            seen += 1
            verdict = assess_row(row)
            counts[verdict["status"]] += 1
            sources[verdict.get("chunk_source", "segments")] += 1
            for reason in verdict.get("reasons", []):
                reasons[reason] += 1
            samples.setdefault(verdict["status"], {
                "transcript_id": str(row["transcript_id"]),
                "reasons": verdict.get("reasons", []),
            })
            if dry_run:
                continue
            metrics = build_metrics(row, verdict, now, version, sha256)
            cur.execute(UPDATE_SQL,
                        (json.dumps(metrics, ensure_ascii=False),
                         row["transcript_id"]))
            updated += cur.rowcount
            if updated and updated % batch == 0:
                conn.commit()
                print(f"  committed {updated} rows", file=out, flush=True)
    if not dry_run:
        conn.commit()

    title = ("DRY RUN — would write" if dry_run
             else f"BACKFILL — wrote {updated} rows")
    print(_summary(counts, reasons, seen, title), file=out)
    print("\nchunk source: "
          + ", ".join(f"{k}={v}" for k, v in sorted(sources.items())),
          file=out)
    print(f"policy version: {text_quality.POLICY_VERSION}   "
          f"script sha256: {sha256[:16]}", file=out)
    return {"seen": seen, "updated": updated, "counts": dict(counts),
            "reasons": dict(reasons), "samples": samples,
            "policy_version": text_quality.POLICY_VERSION,
            "script_sha256": sha256}


def reset(conn, *, dry_run: bool, batch: int, out=None) -> dict:
    """Undo: put back the `asr_metrics` of every row this job wrote.

    Touches ONLY rows carrying a `backfill` sub-object, so a row the live
    pipeline assessed is never in scope no matter what its status is.
    """
    out = sys.stdout if out is None else out
    with conn.cursor() as cur:
        cur.execute(RESET_SELECT_SQL)
        rows = cur.fetchall()

    how: Counter = Counter()
    restored = 0
    with conn.cursor() as cur:
        for row in rows:
            existing = row["asr_metrics"] or {}
            if not isinstance(existing, dict):
                existing = {}
            metrics, source = restore_metrics(existing)
            how[source] += 1
            if dry_run:
                continue
            cur.execute(RESET_UPDATE_SQL,
                        (json.dumps(metrics, ensure_ascii=False),
                         row["transcript_id"]))
            restored += cur.rowcount
            if restored and restored % batch == 0:
                conn.commit()
                print(f"  committed {restored} rows", file=out, flush=True)
    if not dry_run:
        conn.commit()

    verb = "would restore" if dry_run else f"restored {restored}"
    print(f"\nRESET — {verb} of {len(rows)} backfilled rows "
          f"({', '.join(f'{k}={v}' for k, v in sorted(how.items())) or 'none'})",
          file=out)
    return {"seen": len(rows), "restored": restored, "how": dict(how)}


def _connect(url: str):
    """The real connection. Split out so `main` has one seam a test can hold
    without a hidden command-line flag."""
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(url, row_factory=dict_row)


def main(argv: list[str] | None = None, connect=None) -> int:
    ap = argparse.ArgumentParser(
        description="Backfill ASR quality status onto legacy transcripts.")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="Postgres URL; defaults to $DATABASE_URL.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Assess and print the distribution; write nothing.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Assess at most N rows (still resumable).")
    ap.add_argument("--batch", type=int, default=100,
                    help="Commit every N updated rows (default 100).")
    ap.add_argument("--script-version", default=None,
                    help="Override the provenance stamp (default: git).")
    ap.add_argument("--allow-backlog", action="store_true",
                    help="Run even though alert reconciliation is behind. "
                         "Read the F7 note in docs/PR2-db-status.md first.")
    ap.add_argument("--reset-backfilled", action="store_true",
                    help="Maintenance: restore the pre-backfill asr_metrics "
                         "of every row this job wrote, then exit.")
    args = ap.parse_args(argv)

    if not args.database_url:
        print("no database URL: pass --database-url or set DATABASE_URL",
              file=sys.stderr)
        return EXIT_NO_URL

    version = args.script_version or script_version()
    with (connect or _connect)(args.database_url) as conn:
        if args.reset_backfilled:
            # No backlog guard here: the reset only ever REMOVES amber/red
            # statuses, which restores 013's coalesce-to-green default rather
            # than diverging from it.
            reset(conn, dry_run=args.dry_run, batch=args.batch)
            return EXIT_OK

        backlog = alert_backlog(conn)
        if backlog["total"] and not args.allow_backlog:
            print(
                f"REFUSING TO RUN: {backlog['total']} terminal call_ingest_jobs "
                f"still have alerts_evaluated_at IS NULL "
                f"({backlog['never_attempted']} never attempted, "
                f"{backlog['failing']} failing).\n"
                "evaluate_alert_rules() coalesces a missing asr_quality_status "
                "to 'green' (013_alert_rules.sql), so backfilling amber/red "
                "onto rows whose alerts have not been evaluated yet would "
                "silently suppress the uncommitted-lead rule for them.\n"
                "Reconcile first:  SELECT * FROM reconcile_alert_evaluations(500);\n"
                "then re-run this job. Pass --allow-backlog to override.",
                file=sys.stderr)
            return EXIT_BACKLOG
        if backlog["total"]:
            print(f"WARNING: proceeding with {backlog['total']} unreconciled "
                  f"terminal jobs (--allow-backlog).", file=sys.stderr)

        before = alert_occurrence_count(conn)
        run(conn, dry_run=args.dry_run, limit=args.limit, batch=args.batch,
            version=version)
        after = alert_occurrence_count(conn)
        print(f"alert_occurrences: {before} before, {after} after "
              f"({'unchanged' if before == after else 'CHANGED — investigate'})")
        if before != after:
            return EXIT_BACKLOG
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
