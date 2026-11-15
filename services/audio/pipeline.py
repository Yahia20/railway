"""One call recording -> two voices, words with times. CPU only, no database.

The recipe the 2026-11-11 bake-off chose (scripts kept in local-reports/):

1. 8 kHz PCM -> 16 kHz float (linear interpolation is enough for both models).
2. NVIDIA Sortformer v2.1 (streaming 4-speaker, offline setting from its card)
   finds who speaks when. Keep the two voices that talk longest.
3. Gap fill: every moment of the call goes to the nearest of the two voices in
   time, so a word the diariser missed still lands in a channel (70% -> 80% of
   speech on the right channel, measured on synthetic calls with known truth).
4. faster-whisper large-v3-turbo transcribes the whole call ONCE with word
   timestamps; each word is placed by time onto voice A or B. (Transcribing
   each voice separately pays twice and loses the context across turns.)

Voice A is whoever speaks first. Which voice is the AGENT is not decided here:
it is one question to a model per call, asked by the worker (/calls/prepare),
which also masks the text and builds the stereo file. This service only
listens; it holds no credentials and is handed short-lived URLs.
"""
from __future__ import annotations

import io
import os
import tempfile
import time
import wave
from collections import defaultdict
from functools import lru_cache

import numpy as np

SR = 16000
DIAR_MODEL = "nvidia/diar_streaming_sortformer_4spk-v2.1"
ASR_MODEL = os.getenv("ASR_MODEL", "large-v3-turbo")
THREADS = int(os.getenv("CPU_THREADS", "8"))


def read_pcm(data: bytes) -> tuple[np.ndarray, int]:
    """WAV bytes -> (mono float32 in [-1, 1], sample rate). PCM 16-bit only."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {8 * w.getsampwidth()}-bit")
        rate, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, rate


def to_16k(x: np.ndarray, rate: int) -> np.ndarray:
    if rate == SR or len(x) == 0:
        return x.astype(np.float32)
    n = int(len(x) * SR / rate)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


@lru_cache(maxsize=1)
def diariser():
    import torch
    from nemo.collections.asr.models import SortformerEncLabelModel
    torch.set_num_threads(THREADS)
    m = SortformerEncLabelModel.from_pretrained(DIAR_MODEL, map_location="cpu")
    m.eval()
    sm = m.sortformer_modules   # the model card's offline (30.4 s latency) setting
    sm.chunk_len, sm.chunk_right_context, sm.fifo_len, sm.spkcache_update_period = 340, 40, 40, 300
    return m


@lru_cache(maxsize=1)
def recogniser():
    from faster_whisper import WhisperModel
    return WhisperModel(ASR_MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS)


def diarise(x16: np.ndarray) -> list[list]:
    """[[start, end, speaker], ...] in seconds, raw from Sortformer."""
    import soundfile as sf
    import torch
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "call.wav")
        sf.write(path, x16, SR, subtype="PCM_16")
        with torch.inference_mode():
            segs = diariser().diarize(audio=[path], batch_size=1)
    out = []
    for s in segs[0]:
        a, b, spk = (s.split()[:3] if isinstance(s, str) else (s[0], s[1], s[2]))
        out.append([float(a), float(b), str(spk)])
    return out


def fill_two(segs: list[list], total: float) -> list[list]:
    """Keep the two longest voices, label them A (speaks first) and B, and give
    every moment of the call to the nearest of the two. Returns contiguous
    [[start, end, 'A'|'B'], ...] covering 0..total."""
    dur, first = defaultdict(float), {}
    for a, b, s in segs:
        dur[s] += b - a
        first[s] = min(first.get(s, a), a)
    keep = sorted([s for s, _ in sorted(dur.items(), key=lambda z: -z[1])[:2]], key=lambda s: first[s])
    label = {s: "AB"[i] for i, s in enumerate(keep)}
    S = sorted(x for x in segs if x[2] in label)
    out = []
    for i, (a, b, s) in enumerate(S):
        lo = 0.0 if i == 0 else ((S[i - 1][1] + a) / 2 if S[i - 1][1] < a else a)
        hi = total if i == len(S) - 1 else ((b + S[i + 1][0]) / 2 if b < S[i + 1][0] else b)
        if hi > lo:
            out.append([round(lo, 2), round(hi, 2), label[s]])
    merged: list[list] = []
    for seg in out:   # neighbours of the same voice become one span
        if merged and merged[-1][2] == seg[2] and seg[0] <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], seg[1])
        else:
            merged.append(seg)
    return merged


def speech_spans(segs: list[list]) -> dict[str, float]:
    """Seconds each raw voice actually spoke (before the fill): for metrics."""
    dur: dict[str, float] = defaultdict(float)
    for a, b, s in segs:
        dur[s] += b - a
    return dict(dur)


def transcribe(x16: np.ndarray) -> list[list]:
    """[[start, end, word], ...] for the whole call, Arabic forced."""
    segments, _ = recogniser().transcribe(
        x16, language="ar", word_timestamps=True, beam_size=1,
        vad_filter=True, condition_on_previous_text=False)
    return [[round(w.start, 2), round(w.end, 2), w.word] for s in segments for w in (s.words or [])]


def voice_at(spans: list[list], a: float, b: float) -> str | None:
    best, who = 0.0, None
    for s0, s1, v in spans:
        o = min(b, s1) - max(a, s0)
        if o > best:
            best, who = o, v
    return who


def process(data: bytes) -> dict:
    """The whole service in one call. Timings are measured, never estimated."""
    t0 = time.monotonic()
    x, rate = read_pcm(data)
    total = len(x) / rate if rate else 0.0
    if total < 1.0:
        return {"empty": True, "audio_seconds": round(total, 2), "compute_seconds": 0.0}
    x16 = to_16k(x, rate)
    raw = diarise(x16)
    t1 = time.monotonic()
    spans = fill_two(raw, total) if raw else []
    words = transcribe(x16)
    t2 = time.monotonic()
    placed = [[a, b, voice_at(spans, a, b) or "A", w] for a, b, w in words]
    # speech actually heard per voice, mapped from raw speaker ids to A/B
    labels = {}
    for a, b, v in spans:
        for r0, r1, s in raw:
            if min(b, r1) - max(a, r0) > 0 and s not in labels:
                labels[s] = v
    spoken = defaultdict(float)
    for s, d in speech_spans(raw).items():
        if s in labels:
            spoken[labels[s]] += d
    return {
        "empty": not words,
        "audio_seconds": round(total, 2),
        "voices": len({s for _, _, s in raw}),
        "spans": spans,                 # A/B ownership of every moment (gap-filled)
        "raw_segments": raw,            # what the diariser actually heard
        "words": placed,                # [start, end, 'A'|'B', word]
        "speech_seconds": {k: round(v, 2) for k, v in spoken.items()},
        "compute_seconds": round(t2 - t0, 2),
        "timing": {"diarise": round(t1 - t0, 2), "transcribe": round(t2 - t1, 2)},
        "models": {"diariser": DIAR_MODEL, "asr": f"faster-whisper {ASR_MODEL} int8 beam1 vad"},
    }
