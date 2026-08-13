"""Release-1 ASR text quality: contamination removal, loop truncation, gate.

Cohere Transcribe Arabic hallucinates its training-data watermarks into real
calls — "التفريغ والتدقيق قصي البياتي" and eddirasa.com URL chains appeared in
~20% of one measured day (2026-08-09, 23/112 calls) — and its decoder loops on
hold tones (one token repeated up to 487 times). Both were reaching the judge
and being quoted as if the customer said them.

Design agreed with a second model over four adversarial rounds and validated
on a 10-call trial before shipping. The rules that look oddly specific are
each the survivor of a measured failure:

* Removal is precision-first: only exact known watermark families and
  URL-garbage chains are removed. Generic words that also appear in the
  watermarks ("الترجمة" — customers really discuss document translation) and
  bare person names are never touched.
* Loop truncation keeps the FIRST THREE occurrences: in a real call the loop
  "لا ×190" began as the customer's genuine refusal — an answer module 3
  needs — before the decoder derailed. Cutting from the 4th occurrence keeps
  the refusal and drops the noise. Runs of 4-5 are untouched entirely: the
  trial data shows real speech reaches 5 ("أيوه أيوه أيوه أيوه", city names,
  phone digits read aloud).
* Every removal is recorded with its exact offsets and literal text, and
  replaced by GAP so the judge sees that something was cut. The raw text is
  reconstructible character-for-character from clean text + the ledger —
  tested, not assumed.
* The call-level gate keeps broken transcripts away from the judge entirely:
  scoring an agent on a transcript that lost a minute of audio produces a
  number that looks fine and is wrong.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

GAP = "[[ASR_GAP]]"
POLICY_VERSION = "asr-q1"

_AR = "ء-ي"

# --- Tier 1: removed. Exact known contamination families only. -------------

RX_QUSAY_CREDIT = re.compile(
    rf"(?<![{_AR}])"
    r"التفريغ\s*(?:و|وال)?\s*التدقيق"
    r"(?:\s*(?:من|بواسط[ةه]|اعداد)\s*)?"
    r"\s+قص[يى]\s+البيات[يى]"
    # The two fake co-credit names are removed ONLY when attached to the
    # credit line ("..عبد الناصر عاشور" — the transcript writes double dots).
    # Standing alone they could be real people and are Tier 2.
    r"(?:(?:\s*[-.،,:]{1,3}\s*|\s+)"
    r"(?:عبد\s+الناصر\s+عاشور|جواد\s+الخفا[جح][يى])){0,2}"
    rf"(?![{_AR}])")

RX_EDDIRASA = re.compile(
    rf"(?:(?<![{_AR}])موقع\s+الدراس[ةه]\s+الجزائر[يى]"
    r"\s*[:：\-–—]?\s*)?"
    r"(?:https?\s*:\s*//\s*)?"
    r"(?:www\s*\.\s*)?"
    r"eddirasa\s*\.\s*com"
    r"(?:\s*\.\s*[a-z]{2,3})*"
    r"(?:\s*/\s*[^\s،؛!?]*)?", re.IGNORECASE)

# Literal blank-marker the ASR emits ("@@@فراغ"). The at-signs carry the
# precision; the bare word فراغ is ordinary Arabic and must survive.
RX_ATSIGN_BLANK = re.compile(rf"(?<!@)@{{3,}}\s*فراغ(?![{_AR}])")

# --- URL-garbage chains (found by the 10-call trial, not the design). ------
# Hallucinated domains loop INSIDE one token ("...uk.uk.uk.uk..."), which
# token-level repetition detection cannot see. Runs before RX_EDDIRASA so the
# narrow regex never bites mid-chain and leaves fragments.

_URL_CANDIDATE = re.compile(
    r"(?:https?\s*:\s*//\s*)?[A-Za-z0-9_\-]+(?:\s*\.\s*[A-Za-z0-9_\-]+){2,}"
    r"(?:\s*/\s*[^\s،؛!?]*)?")
_CONTAMINATION_LABELS = ("eddirasa", "fontsalon")


def _is_url_garbage(candidate: str) -> bool:
    flat = re.sub(r"\s+", "", candidate).lower()
    dots = flat.count(".")
    if any(lbl in flat for lbl in _CONTAMINATION_LABELS) and dots >= 1:
        return True
    if dots >= 5 and len(flat) >= 25:
        return True
    if dots >= 3:
        labels = [p for p in re.split(r"[./:]+", flat) if p]
        for lbl in set(labels):
            if 2 <= len(lbl) <= 4 and labels.count(lbl) >= 3:
                return True
    return False


# --- Tier 2: flagged, never removed. ---------------------------------------

RX_SITE_LABEL_ONLY = re.compile(
    rf"(?<![{_AR}])موقع\s+الدراس[ةه]\s+الجزائر[يى](?![{_AR}])")
RX_GENERIC_CREDIT = re.compile(
    rf"(?<![{_AR}])(?:الترجم[ةه]|التفريغ|التدقيق)"
    rf"(?:\s*(?:و|وال)\s*(?:الترجم[ةه]|التفريغ|التدقيق)){{1,2}}"
    rf"(?:\s+(?:بواسط[ةه]|اعداد|من))?"
    rf"(?:\s+[{_AR}]{{2,}}){{1,4}}")
RX_SUSPECT_NAMES = re.compile(
    rf"(?<![{_AR}])(?:عبد\s+الناصر\s+عاشور|جواد\s+الخفا[جح][يى])(?![{_AR}])")

TIER2 = (("site_label_only", RX_SITE_LABEL_ONLY),
         ("generic_credit", RX_GENERIC_CREDIT),
         ("suspect_name", RX_SUSPECT_NAMES))

# --- Loop detection on normalized tokens. ----------------------------------
# Normalization is for COMPARISON only (the emitted text is never altered by
# it): punctuation stripped, alef variants and ى folded, diacritics removed.
# No fuzzy equivalence — "نع" and "نعم" are different tokens on purpose; real
# short Arabic words would merge otherwise.

_DIACRITICS = dict.fromkeys(map(ord, "ًٌٍَُِّْـ"), None)
_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي"})
_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)

# Spoken digits produce genuine runs (phone numbers read aloud: "9 9 9 9",
# "ثلاثه ثلاثه ثلاثه ثلاثه"), so numeric tokens skip the 6-15 warning band.
_DIGIT_WORDS = frozenset(
    "صفر واحد اثنين اتنين ثلاثه ثلاثة تلاته تلاتة اربعه اربعة خمسه خمسة "
    "سته ستة سبعه سبعة ثمانيه ثمانية تمانيه تمانية تسعه تسعة عشره عشرة".split())

HARD_RUN = 16       # >= this: decoder loop, no real speech reaches it
WARN_RUN_LO = 6     # 6..15: warn only — measured gray zone
KEEP_OCCURRENCES = 3


def _norm_token(tok: str) -> str:
    t = unicodedata.normalize("NFKC", tok)
    t = _EDGE_PUNCT.sub("", t)
    return t.translate(_DIACRITICS).translate(_FOLD).lower()


def _is_numeric_token(norm: str) -> bool:
    if not norm:
        return False
    return norm in _DIGIT_WORDS or all(c.isdigit() for c in norm)


def repeated_ngram_fraction(norm_tokens: list[str]) -> float:
    """Fraction of tokens covered by 2-5-token n-grams occurring >= 3 times.
    Corroborates a 6-15 run; never a trigger on its own in release 1."""
    n = len(norm_tokens)
    if n < 20:
        return 0.0
    counts: dict[tuple, int] = {}
    for size in range(2, 6):
        for i in range(n - size + 1):
            g = tuple(norm_tokens[i:i + size])
            counts[g] = counts.get(g, 0) + 1
    covered = [False] * n
    for size in range(2, 6):
        for i in range(n - size + 1):
            if counts[tuple(norm_tokens[i:i + size])] >= 3:
                for k in range(i, i + size):
                    covered[k] = True
    return sum(covered) / n


# --- The per-chunk pipeline. -----------------------------------------------

@dataclass
class ChunkQuality:
    clean_text: str
    ops: list[dict] = field(default_factory=list)      # removals, raw offsets
    flags: list[dict] = field(default_factory=list)    # Tier-2 + warn runs
    hard_loop: bool = False
    warn_run: int = 0          # largest uncorroborated 6-15 run
    ngram_fraction: float = 0.0


def clean_chunk(text: str, seq: int | None = None) -> ChunkQuality:
    """Clean one chunk's raw text. Returns clean text + a reversible ledger.

    Removal spans are collected against the ORIGINAL text, merged, and
    applied back-to-front, so every op's offsets refer to the raw string and
    reconstruct() can rebuild it exactly.
    """
    tokens = text.split()
    offs: list[tuple[int, int]] = []
    pos = 0
    for t in tokens:
        s = text.index(t, pos)
        offs.append((s, s + len(t)))
        pos = s + len(t)
    norms = [_norm_token(t) for t in tokens]

    spans: list[tuple[int, int, str]] = []   # (start, end, pattern_id)
    flags: list[dict] = []
    hard_loop = False
    warn_run = 0

    # Loops (normalized-token runs). Cut occurrence 4 .. end of the maximal
    # run only — the trial showed coherent genuine text AFTER loops, and the
    # first occurrences are the real answer the loop grew out of.
    i = 0
    while i < len(norms):
        j = i
        while j < len(norms) and norms[j] == norms[i] and norms[i]:
            j += 1
        run = j - i
        if run >= HARD_RUN:
            hard_loop = True
            spans.append((offs[i + KEEP_OCCURRENCES][0], offs[j - 1][1],
                          f"hard_token_run_{run}"))
        elif WARN_RUN_LO <= run < HARD_RUN and not _is_numeric_token(norms[i]):
            warn_run = max(warn_run, run)
            flags.append({"flag": "token_run_6_15", "run": run,
                          "token": tokens[i]})
        i = j if j > i else i + 1

    # URL-garbage chains, BEFORE the narrower eddirasa regex.
    for m in _URL_CANDIDATE.finditer(text):
        if _is_url_garbage(m.group()):
            spans.append((m.start(), m.end(), "url_garbage_v1"))

    for pid, rx in (("qusay_credit_v1", RX_QUSAY_CREDIT),
                    ("eddirasa_domain_v1", RX_EDDIRASA),
                    ("atsign_blank_v1", RX_ATSIGN_BLANK)):
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), pid))

    # Merge overlaps/adjacency (whitespace-only separation) into one span so
    # a chain of watermarks becomes one gap, not a stutter of gaps.
    spans.sort()
    merged: list[list] = []
    for s, e, pid in spans:
        if merged and s <= merged[-1][1] or (
                merged and not text[merged[-1][1]:s].strip()):
            merged[-1][1] = max(merged[-1][1], e)
            if pid not in merged[-1][2]:
                merged[-1][2] += "+" + pid
        else:
            merged.append([s, e, pid])

    ops: list[dict] = []
    clean = text
    for s, e, pid in reversed(merged):
        ops.append({"pattern_id": pid, "raw_start": s, "raw_end": e,
                    "removed_text": text[s:e], "replacement_text": GAP})
        clean = clean[:s] + GAP + clean[e:]
    ops.reverse()

    # Tier-2 flags from raw offsets, suppressed where a removal already covers
    # the span (a name inside a removed credit line is not separate news).
    for pid, rx in TIER2:
        for m in rx.finditer(text):
            if any(s <= m.start() < e or s < m.end() <= e for s, e, _ in merged):
                continue
            flags.append({"flag": pid,
                          "text": text[m.start():m.end()][:80]})

    if seq is not None:
        for op in ops:
            op["segment_seq"] = seq
        for f in flags:
            f["segment_seq"] = seq

    return ChunkQuality(
        clean_text=clean,
        ops=ops,
        flags=flags,
        hard_loop=hard_loop,
        warn_run=warn_run,
        ngram_fraction=round(repeated_ngram_fraction(norms), 3),
    )


def reconstruct(clean_text: str, ops: list[dict]) -> str:
    """Rebuild the raw chunk text from clean text + ledger. Tested contract:
    reconstruct(clean_chunk(raw).clean_text, ops) == raw, char for char."""
    out = clean_text
    for op in ops:  # ops are in raw order; gaps appear in the same order
        idx = out.find(op["replacement_text"])
        if idx < 0:
            raise ValueError("ledger does not match clean text")
        out = out[:idx] + op["removed_text"] + out[idx + len(op["replacement_text"]):]
    return out


# --- Call-level gate. ------------------------------------------------------

def assess_call(chunk_results: list[ChunkQuality],
                chunk_durations: list[float],
                failed_seqs: set[int],
                total_duration: float,
                clean_chars: int,
                raw_chars: int,
                chunks_empty: int) -> dict:
    """green / amber / red per the agreed release-1 gate.

    An invalid chunk is transport-failed, hard-looped, or a 6-15 run
    corroborated by n-gram coverage or extreme density. A loop chunk's whole
    duration counts invalid — there are no token timestamps to know where in
    the audio the decoder derailed — even though its retained text is kept.
    Red means the judge never sees the call: too much of the audio is
    unaccounted for to score a human being on what remains.
    """
    density = (raw_chars / total_duration) if total_duration else 0.0
    invalid: list[bool] = []
    for idx, cq in enumerate(chunk_results):
        corroborated = cq.warn_run > 0 and (
            cq.ngram_fraction >= 0.45 or density > 22)
        invalid.append(idx in failed_seqs or cq.hard_loop or corroborated)

    invalid_sec = sum(d for d, bad in zip(chunk_durations, invalid) if bad)
    longest_invalid = 0.0
    run = 0.0
    for d, bad in zip(chunk_durations, invalid):
        run = run + d if bad else 0.0
        longest_invalid = max(longest_invalid, run)

    tier1_chars = sum(
        len(op["removed_text"]) for cq in chunk_results for op in cq.ops
        if "hard_token_run" not in op["pattern_id"])
    any_removal = any(cq.ops for cq in chunk_results)
    any_flag = any(cq.flags for cq in chunk_results)
    warn_only = any(cq.warn_run and not inv
                    for cq, inv in zip(chunk_results, invalid))

    red = (clean_chars < 20
           or (total_duration and invalid_sec > 0.20 * total_duration)
           or longest_invalid >= 60.0
           or (tier1_chars >= 40 and raw_chars and tier1_chars >= 0.25 * raw_chars))
    amber = (invalid_sec > 0
             or warn_only
             or any_removal
             or any_flag
             or (total_duration >= 30 and density < 3)
             or chunks_empty > 0
             or (density > 22 and not any(cq.hard_loop for cq in chunk_results)))

    status = "red" if red else ("amber" if amber else "green")
    return {
        "status": status,
        "invalid_seconds": round(invalid_sec, 2),
        "longest_invalid_span_seconds": round(longest_invalid, 2),
        "tier1_chars_removed": tier1_chars,
        "quality_policy_version": POLICY_VERSION,
    }
