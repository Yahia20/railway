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
* Decoder control tokens (`<lowercase_body>`) are sorted into three classes,
  because the shape alone says nothing about whether audio was lost:
  - ALLOWLISTED harmless markers (`HARMLESS_CONTROL_TOKENS`, currently only
    `<hesitation>`) are the one Tier-1 removal that leaves NO marker behind.
    They are not lost audio and not invalid speech — the words on either side
    are one continuous utterance — so a GAP there would falsely tell the judge
    a seam exists and would reject any genuine quote spanning it
    (`conversation_spans` treats GAP as a hard quote boundary). They are
    deleted with one adjacent space so the sentence closes up, counted as
    `control_tokens_removed`, and excluded from `tier1_chars_removed`, from
    the any-removal amber trigger, from loop detection, and — via
    `speech_chars()` — from the character-density and n-gram corroboration
    inputs, so a chatty decoder can never move a call's quality status. That
    exclusion survives merging: every op carries `control_chars`, the count of
    its own characters contributed by harmless control spans, so a control
    token glued to a real watermark still costs the watermark's characters and
    only the watermark's characters.
  - LOST-AUDIO markers (`LOST_AUDIO_CONTROL_TOKENS`: `<inaudible>`,
    `<inaudible_speech>`, `<noise>`, `<music>`, `<silence>`, `<unk>`) denote
    audio the decoder could not read. Deleting them silently would let a judge
    quote straight across unknown audio, so they are an ordinary Tier-1 GAP
    op (`control_token_gap_v1`): replaced by GAP, counted in
    `tier1_chars_removed`, and ambering the call through the normal
    any-removal trigger. They do NOT add to `invalid_seconds`: there are no
    token timestamps, and a marker's character length is uncorrelated with the
    audio behind it (`<silence>` is 9 characters whether it stands for one
    second or thirty), so attributing a proportional slice of the chunk's
    duration would fabricate an audio measurement. Their characters are the
    honest unit and that is what is counted.
  - UNKNOWN tokens (anything else of that shape) are NOT touched. Deleting a
    marker we cannot classify is the failure mode this rule exists to prevent,
    so the text is left exactly as the model produced it and an
    `unknown_control_token` flag is raised — amber through the existing
    any-flag path, no new status branch, and a human decides which list it
    belongs on.
  Because a harmless removal's replacement text is empty, `reconstruct()`
  walks the ledger offsets rather than searching for the marker.
* The call-level gate keeps broken transcripts away from the judge entirely:
  scoring an agent on a transcript that lost a minute of audio produces a
  number that looks fine and is wrong.

Policy changelog
----------------
asr-q1 -> asr-q2 (control-token handling). q1 deleted EVERY `<lowercase>` tag
without a marker, which silently erased `<inaudible>`/`<noise>`/`<silence>` and
let a judge quote evidence across audio nobody heard; their characters also
still fed the density and corroboration thresholds, and a control token merged
with a watermark was billed to `tier1_chars_removed`. q2 splits the shape into
an allowlist (deleted, status-neutral), a lost-audio list (GAP, Tier-1
characters, no invented seconds) and unknown (untouched + flagged), tracks
per-op `control_chars` so the harmless class stays neutral even when merged,
and measures density on `speech_chars()` instead of raw length. Any transcript
assessed under q1 must be re-assessed to be comparable with q2.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

GAP = "[[ASR_GAP]]"
POLICY_VERSION = "asr-q2"

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

# Model control tokens leaking into the transcript ("<hesitation>", observed
# on cohere-transcribe-arabic-07-2026 canaries). Both angle brackets and a
# short all-lowercase ASCII body are required: Arabic speech cannot produce
# this shape, and a lone "<" or ">" (a price range read aloud, a stray
# punctuation glitch) never matches.
CONTROL_TOKEN_PID = "control_token_v1"          # harmless: deleted, no marker
CONTROL_GAP_PID = "control_token_gap_v1"        # lost audio: replaced by GAP
RX_CONTROL_TOKEN = re.compile(r"<[a-z_]{2,20}>")
_WS_AFTER = re.compile(r"\s+")

# The model card for cohere-transcribe-arabic-07-2026 documents NO control-token
# vocabulary at all (it only warns the model is "eager to transcribe, even
# non-speech sounds"), so nothing here can be justified from it. The allowlist is
# therefore exactly what was OBSERVED on the canaries — `<hesitation>` — and
# nothing is added to it on the strength of "it looks like filler": the cost of a
# wrong guess is a judge quoting across audio nobody heard.
HARMLESS_CONTROL_TOKENS = frozenset({"hesitation"})

# Markers whose ordinary ASR meaning is "audio here could not be read". These
# are missing audio, so they get a GAP and are billed as Tier-1 characters.
LOST_AUDIO_CONTROL_TOKENS = frozenset({
    "inaudible", "inaudible_speech", "noise", "music", "silence", "unk",
})

HARMLESS = "harmless"
LOST_AUDIO = "lost_audio"
UNKNOWN = "unknown"


def control_token_class(token: str) -> str | None:
    """HARMLESS / LOST_AUDIO / UNKNOWN for a control token, None if not one."""
    if not RX_CONTROL_TOKEN.fullmatch(token):
        return None
    body = token[1:-1]
    if body in HARMLESS_CONTROL_TOKENS:
        return HARMLESS
    if body in LOST_AUDIO_CONTROL_TOKENS:
        return LOST_AUDIO
    return UNKNOWN


def _control_only(pattern_id: str) -> bool:
    """True when a (possibly merged) removal span is nothing but HARMLESS
    control tokens — i.e. a removal that is status-neutral and marker-free.
    A lost-audio control token is deliberately NOT control-only: it is a normal
    Tier-1 GAP removal and must read as one everywhere (including the backfill,
    which calls this to decide whether contamination was removed)."""
    return all(p == CONTROL_TOKEN_PID for p in pattern_id.split("+"))

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
    control_tokens: int = 0    # HARMLESS control tokens removed (status-neutral)
    control_chars: int = 0     # raw chars those removals consumed (incl. 1 space)
    control_gaps: int = 0      # lost-audio control tokens replaced by GAP
    unknown_control_tokens: int = 0   # unclassified tokens left in the text


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
    # Corroboration reads SPEECH only. A control token is a machine marker, not
    # a word, and 20 of them in a chunk would otherwise push repeated-n-gram
    # coverage past the corroboration threshold and charge the chunk's whole
    # duration to invalid_seconds.
    speech_norms = [n for t, n in zip(tokens, norms)
                    if not RX_CONTROL_TOKEN.fullmatch(t)]

    # (start, end, pattern_id, control_chars) — the 4th element is how many of
    # the span's characters belong to HARMLESS control tokens and must stay out
    # of every status calculation, even after the span is merged with a real
    # contamination span.
    spans: list[tuple[int, int, str, int]] = []
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
        if RX_CONTROL_TOKEN.fullmatch(tokens[i]):
            # A repeated control token of ANY class is a marker stream, not a
            # decoder looping on speech, so it never trips loop detection: a
            # chatty "<hesitation>" would charge the chunk's whole duration to
            # invalid_seconds for punctuation, and a run of "<inaudible>" is
            # already answered below by a GAP and its Tier-1 characters.
            i = j
            continue
        if run >= HARD_RUN:
            hard_loop = True
            spans.append((offs[i + KEEP_OCCURRENCES][0], offs[j - 1][1],
                          f"hard_token_run_{run}", 0))
        elif WARN_RUN_LO <= run < HARD_RUN and not _is_numeric_token(norms[i]):
            warn_run = max(warn_run, run)
            flags.append({"flag": "token_run_6_15", "run": run,
                          "token": tokens[i]})
        i = j if j > i else i + 1

    # URL-garbage chains, BEFORE the narrower eddirasa regex.
    for m in _URL_CANDIDATE.finditer(text):
        if _is_url_garbage(m.group()):
            spans.append((m.start(), m.end(), "url_garbage_v1", 0))

    for pid, rx in (("qusay_credit_v1", RX_QUSAY_CREDIT),
                    ("eddirasa_domain_v1", RX_EDDIRASA),
                    ("atsign_blank_v1", RX_ATSIGN_BLANK)):
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), pid, 0))

    # Control tokens, by class (see the module docstring).
    control_tokens = 0          # harmless: deleted, marker-free, status-neutral
    control_chars = 0           # raw chars those deletions consume
    gap_tokens = 0              # lost audio: replaced by GAP, billed as Tier 1
    unknown: dict[str, int] = {}
    for m in RX_CONTROL_TOKEN.finditer(text):
        kind = control_token_class(m.group())
        if kind is LOST_AUDIO:
            # Missing audio. Exactly a Tier-1 removal: the judge must see the
            # seam. No whitespace is swallowed — GAP takes the token's place
            # and the surrounding spacing is already correct.
            gap_tokens += 1
            spans.append((m.start(), m.end(), CONTROL_GAP_PID, 0))
            continue
        if kind is UNKNOWN:
            # Unclassified: never deleted, never gapped, only surfaced.
            unknown[m.group()] = unknown.get(m.group(), 0) + 1
            continue
        # Harmless. The span swallows the whitespace on one side so the
        # sentence closes up instead of keeping a doubled space where the token
        # was; trailing is preferred so a token at the very end still collapses.
        s, e = m.start(), m.end()
        after = _WS_AFTER.match(text, e)
        if after:
            e = after.end()
        elif not RX_CONTROL_TOKEN.match(text, e):
            # Nothing to eat on the right, so take the space on the left —
            # unless the next token is another control token, which will take
            # its own right-hand space (or become a GAP that needs the space).
            # Eating both sides of a "<a><b>" pair would glue the surrounding
            # words together.
            while s > 0 and text[s - 1].isspace():
                s -= 1
        control_tokens += 1
        control_chars += e - s
        spans.append((s, e, CONTROL_TOKEN_PID, e - s))

    for tok in sorted(unknown):
        flags.append({"flag": "unknown_control_token", "token": tok,
                      "count": unknown[tok]})

    # Merge overlaps/adjacency (whitespace-only separation) into one span so
    # a chain of watermarks becomes one gap, not a stutter of gaps.
    spans.sort()
    merged: list[list] = []
    for s, e, pid, cc in spans:
        if merged and s <= merged[-1][1] or (
                merged and not text[merged[-1][1]:s].strip()):
            merged[-1][1] = max(merged[-1][1], e)
            if pid not in merged[-1][2].split("+"):
                merged[-1][2] += "+" + pid
            # Harmless-control characters stay attributed to their own
            # sub-spans, so merging never launders them into Tier 1.
            merged[-1][3] += cc
        else:
            merged.append([s, e, pid, cc])

    # A harmless span swallows an adjacent space so the sentence closes up. If
    # that span then merges into one that emits GAP — a lost-audio marker or a
    # watermark right next to it — the swallowed space would be eaten by a
    # replacement that does not close the sentence up, gluing GAP to the
    # neighbouring word ("...[[ASR_GAP]]واضح") and breaking the marker's job as
    # a visible seam. Give the whitespace back to the clean text.
    for span in merged:
        if _control_only(span[2]):
            continue
        while span[0] < span[1] and text[span[1] - 1].isspace():
            span[1] -= 1
            span[3] = max(0, span[3] - 1)
        while span[0] < span[1] and text[span[0]].isspace():
            span[0] += 1
            span[3] = max(0, span[3] - 1)

    ops: list[dict] = []
    clean = text
    for s, e, pid, cc in reversed(merged):
        # Harmless-control-only spans leave nothing behind (see module
        # docstring); a span that merged with real contamination — or with a
        # lost-audio marker — still gets its GAP.
        repl = "" if _control_only(pid) else GAP
        ops.append({"pattern_id": pid, "raw_start": s, "raw_end": e,
                    "removed_text": text[s:e], "replacement_text": repl,
                    # chars of this op that are harmless control tokens and
                    # must not count as contamination anywhere downstream
                    "control_chars": min(cc, e - s)})
        clean = clean[:s] + repl + clean[e:]
    ops.reverse()
    # Re-read from the ops rather than trusting the running total: the
    # whitespace give-back above can hand a character back to the clean text
    # after it was counted, and `speech_chars()` must subtract exactly what was
    # actually removed or the density denominator drifts.
    control_chars = sum(op["control_chars"] for op in ops)

    # Tier-2 flags from raw offsets, suppressed where a removal already covers
    # the span (a name inside a removed credit line is not separate news).
    for pid, rx in TIER2:
        for m in rx.finditer(text):
            if any(s <= m.start() < e or s < m.end() <= e
                   for s, e, _, _ in merged):
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
        ngram_fraction=round(repeated_ngram_fraction(speech_norms), 3),
        control_tokens=control_tokens,
        control_chars=control_chars,
        control_gaps=gap_tokens,
        unknown_control_tokens=sum(unknown.values()),
    )


def reconstruct(clean_text: str, ops: list[dict]) -> str:
    """Rebuild the raw chunk text from clean text + ledger. Tested contract:
    reconstruct(clean_chunk(raw).clean_text, ops) == raw, char for char.

    Driven by the recorded raw offsets, not by searching for the marker: a
    control-token removal replaces its span with nothing, and an empty needle
    is not findable. Ops are in raw order and non-overlapping, so walking them
    once while tracking how far the clean text has shifted is exact.
    """
    out: list[str] = []
    cursor = 0      # position in clean_text
    shift = 0       # raw offset - clean offset, accumulated over prior ops
    for op in ops:
        repl = op["replacement_text"]
        start = op["raw_start"] - shift
        end = start + len(repl)
        if start < cursor or clean_text[start:end] != repl:
            raise ValueError("ledger does not match clean text")
        out.append(clean_text[cursor:start])
        out.append(op["removed_text"])
        cursor = end
        shift += (op["raw_end"] - op["raw_start"]) - len(repl)
    out.append(clean_text[cursor:])
    return "".join(out)


# --- Call-level gate. ------------------------------------------------------

def speech_chars(chunk_results: list["ChunkQuality"], raw_chars: int) -> int:
    """Raw characters minus the ones that were never speech.

    `raw_chars` is the length of the text the model returned, and harmless
    control tokens are machine punctuation inside it. Leaving them in the
    denominator lets a chatty decoder move `chars_per_audio_sec` and therefore
    the density thresholds, which is exactly the kind of status movement the
    control-token rule exists to prevent. Lost-audio markers are NOT subtracted:
    they stand for audio that really was there, and the Tier-1 ratio they are
    compared against is measured on the same text.

    Exposed so callers that mirror the gate's arithmetic (the backfill's
    `_reasons`) can use the identical denominator instead of re-deriving one.
    """
    return max(0, raw_chars - sum(cq.control_chars for cq in chunk_results))


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
    speech = speech_chars(chunk_results, raw_chars)
    density = (speech / total_duration) if total_duration else 0.0
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

    # Harmless control tokens are machine punctuation, not contamination and
    # not lost speech, so they are excluded from BOTH status triggers: they
    # neither push tier1_chars toward red nor trip the any-removal amber.
    # Otherwise a decoder that says "<hesitation>" once would amber every call
    # it touches and amber would stop meaning anything. Subtracting each op's
    # own `control_chars` rather than skipping whole control-only ops is what
    # keeps that true for a token MERGED into a watermark span: the watermark's
    # characters are still charged, the token's are not.
    tier1_chars = sum(
        len(op["removed_text"]) - op.get("control_chars", 0)
        for cq in chunk_results for op in cq.ops
        if "hard_token_run" not in op["pattern_id"])
    control_tokens = sum(cq.control_tokens for cq in chunk_results)
    control_gaps = sum(cq.control_gaps for cq in chunk_results)
    unknown_control = sum(cq.unknown_control_tokens for cq in chunk_results)
    any_removal = any(not _control_only(op["pattern_id"])
                      for cq in chunk_results for op in cq.ops)
    any_flag = any(cq.flags for cq in chunk_results)
    warn_only = any(cq.warn_run and not inv
                    for cq, inv in zip(chunk_results, invalid))

    red = (clean_chars < 20
           or (total_duration and invalid_sec > 0.20 * total_duration)
           or longest_invalid >= 60.0
           or (tier1_chars >= 40 and speech and tier1_chars >= 0.25 * speech))
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
        "control_tokens_removed": control_tokens,
        # Lost-audio markers: GAPped, Tier-1 counted, never turned into seconds
        # (no token timestamps exist to attribute duration to them).
        "control_token_gaps": control_gaps,
        # Tokens of the control shape that are on neither list: left in the
        # text verbatim and flagged, so a human classifies them.
        "unknown_control_tokens": unknown_control,
        # Denominator actually used for the density thresholds above.
        "speech_chars": speech,
        "quality_policy_version": POLICY_VERSION,
    }
