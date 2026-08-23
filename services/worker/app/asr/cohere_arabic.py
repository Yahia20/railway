"""Arabic speech-to-text via Cohere Transcribe Arabic (CohereLabs/cohere-transcribe-arabic-07-2026).

The model is a 2B conformer encoder-decoder, Apache-2.0, and is the strongest
open Arabic ASR available: WER 25.87 on the HF Arabic leaderboard vs. ~37 for
Whisper large-v3, and it handles dialect and Arabic-English code-switching,
which is what actually occurs on these calls.

Two things it does NOT do, both of which the pipeline has to compensate for:

* **No timestamps.** We get them by chunking the audio ourselves and recording
  each chunk's offset. That is what `chunk_at_silences` is for.
* **No speaker diarization.** On a mono recording this is the weak link in the
  entire quality pipeline — see the note on `Transcription.diarization`.

Three backends, same interface. Pick with ASR_BACKEND.
"""
from __future__ import annotations

import os
import wave
from dataclasses import dataclass, field
from typing import Literal

MODEL_ID = "CohereLabs/cohere-transcribe-arabic-07-2026"
MODEL_VERSION = "07-2026"

FRAME_MS = 20


@dataclass
class Segment:
    seq: int
    start_sec: float
    end_sec: float
    text: str
    speaker: str = "unknown"
    confidence: float | None = None


@dataclass
class Transcription:
    full_text: str
    segments: list[Segment]
    language: str = "ar"
    provider: str = "cohere-transcribe-arabic"
    model_version: str = MODEL_VERSION
    duration_seconds: float | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None
    confidence: float | None = None

    # 'none' means nothing separated the speakers and every agent score below
    # rests on the judge inferring who spoke. It is stored on the transcript row
    # and shown in v_quality_by_input so the weakness stays visible instead of
    # quietly becoming an assumption.
    diarization: Literal["none", "dual_channel", "pyannote", "provider", "manual"] = "none"
    speaker_map: dict = field(default_factory=dict)

    # Honest health measurements (see transcribe_call). `confidence` above is
    # only chunk return rate; these say what actually came back and whether it
    # looks like speech or like a decoder stuck in a loop.
    metrics: dict = field(default_factory=dict)

    def as_dialogue(self) -> str:
        """Timestamped rendering for the judge prompt.

        When nothing separated the speakers, no speaker label is emitted at all.
        Writing 'UNKNOWN:' on every line would invite the model to treat the
        label as meaningful; an unlabelled transcript plus the call rules block
        states the situation honestly.
        """
        out = []
        for s in self.segments:
            if not s.text:
                continue
            mm, ss = divmod(int(s.start_sec), 60)
            label = "" if s.speaker == "unknown" else f" {s.speaker.upper()}:"
            out.append(f"[{mm:02d}:{ss:02d}]{label} {s.text}")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Audio handling. Deliberately uses only the stdlib `wave` module plus numpy:
# Asterisk writes 8 kHz 16-bit PCM, which needs no ffmpeg to read, and adding
# ffmpeg to the Railway image for no reason costs build time on every deploy.
# ---------------------------------------------------------------------------

def read_pcm(path: str):
    import numpy as np

    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {w.getsampwidth() * 8}-bit")
        rate, channels = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype("int16")
    return pcm, rate, channels


def chunk_at_silences(pcm, rate: int, target_sec: float = 40.0, search_sec: float = 6.0):
    """Cut boundaries at the quietest frame near each nominal cut.

    Cutting on a fixed grid slices words in half and the model then guesses at
    both halves. Cutting at a local energy minimum costs one pass over the
    signal and removes that whole class of error.
    """
    import numpy as np

    n = int(rate * FRAME_MS / 1000)
    usable = (len(pcm) // n) * n
    rms = np.sqrt((pcm[:usable].reshape(-1, n).astype(np.float64) ** 2).mean(axis=1))
    fps = rate / n

    cuts, pos, total = [0], target_sec, len(pcm) / rate
    while pos < total - 10:
        lo = int(max(0, pos - search_sec) * fps)
        hi = int(min(len(rms), (pos + search_sec) * fps))
        if hi <= lo:
            break
        quietest = lo + int(np.argmin(rms[lo:hi]))
        cuts.append(quietest * n)
        pos = quietest / fps + target_sec
    cuts.append(len(pcm))
    return cuts


def _write_chunk(pcm, rate: int, path: str) -> str:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class SpaceBackend:
    """Calls the public HF Space. Free and needs no credentials, but it is a
    shared community GPU: rate-limited, queued, and it can be taken down without
    notice. Fine for evaluation and backfill spikes; not for production SLA."""

    def __init__(self, space: str = MODEL_ID):
        from gradio_client import Client

        self._client = Client(space)

    def transcribe_file(self, path: str) -> str:
        from gradio_client import handle_file

        return (self._client.predict(
            audio_path=handle_file(path), language="ar", api_name="/transcribe"
        ) or "").strip()


class LocalBackend:
    """Runs the weights in-process. No per-minute cost and no rate limit, but it
    needs ~5 GB RAM and is slow without a GPU. This is the production backend if
    call volume is high enough to beat an API bill."""

    def __init__(self, model_id: str = MODEL_ID, device: str | None = None):
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_id,
            trust_remote_code=True,
            torch_dtype=torch.bfloat16 if self.device == "cuda" else torch.float32,
        ).to(self.device)

    def transcribe_file(self, path: str) -> str:
        import numpy as np
        import torch

        pcm, rate, _ = read_pcm(path)
        audio = pcm.astype(np.float32) / 32768.0
        if rate != 16000:                       # the processor resamples, but be explicit
            idx = np.linspace(0, len(audio) - 1, int(len(audio) * 16000 / rate))
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        inputs = self.processor(audio, sampling_rate=16000, return_tensors="pt").to(self.device)
        with torch.no_grad():
            ids = self.model.generate(**inputs, max_new_tokens=440)
        return self.processor.batch_decode(ids, skip_special_tokens=True)[0].strip()


class CohereAPIBackend:
    """Cohere's hosted copy of the same weights. Lowest ops burden.

    Endpoint verified against docs.cohere.com/reference/create-audio-transcription
    (2026-08-11): POST /v2/audio/transcriptions, multipart with file/model/language,
    transcription comes back in `text`.

    Free keys are limited to 5 requests/minute, so the backend paces itself:
    one request per ASR_MIN_REQUEST_INTERVAL seconds (default 12) and a backoff
    retry on 429/5xx. Without the pacing a two-chunk call bursts past the limit
    and the second chunk dies — which the caller records as a silent empty
    segment, the exact failure mode that poisoned the first live batch.
    """

    # This backend retries transport errors itself (with rate-limit pacing),
    # so the generic outer retry in _transcribe_with_retry must not wrap it —
    # nested loops multiplied to 12 requests per chunk worst case, and burned
    # free-tier quota exactly when the API was already refusing.
    handles_transport_retries = True

    def __init__(self, api_key: str, base_url: str = "https://api.cohere.com"):
        import threading

        import httpx

        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=300.0,
        )
        self._min_interval = float(os.getenv("ASR_MIN_REQUEST_INTERVAL", "12"))
        self._last_request = 0.0
        # FastAPI runs sync handlers on a thread pool: two concurrent requests
        # would both read a stale _last_request and burst past the 5/min limit
        # unless the pacing check-and-set is atomic.
        self._pace_lock = threading.Lock()

    def transcribe_file(self, path: str) -> str:
        import time

        import httpx

        for attempt in range(3):
            with self._pace_lock:
                wait = self._min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                self._last_request = time.monotonic()

            with open(path, "rb") as fh:
                # The API rejects the request unless model/language appear
                # BEFORE the file part in the multipart body (verified live,
                # 400 otherwise). httpx encodes `data` fields before `files`,
                # which satisfies this — do not swap to requests-style ordering.
                r = self._client.post(
                    "/v2/audio/transcriptions",
                    files={"file": (os.path.basename(path), fh, "audio/wav")},
                    data={"model": MODEL_ID.split("/")[-1], "language": "ar"},
                )
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 2:
                    r.raise_for_status()
                retry_after = r.headers.get("Retry-After")
                try:
                    pause = max(float(retry_after), 1.0) if retry_after else 15.0 * (attempt + 1)
                except ValueError:
                    pause = 15.0 * (attempt + 1)
                time.sleep(pause)
                continue
            r.raise_for_status()
            payload = r.json()
            return (payload.get("text") or payload.get("transcript") or "").strip()
        raise httpx.HTTPStatusError("unreachable", request=r.request, response=r)


def make_backend(kind: str | None = None):
    kind = (kind or os.getenv("ASR_BACKEND", "space")).lower()
    if kind == "space":
        return SpaceBackend()
    if kind == "local":
        return LocalBackend()
    if kind == "cohere_api":
        return CohereAPIBackend(os.environ["COHERE_API_KEY"])
    raise ValueError(f"unknown ASR_BACKEND {kind!r}; expected space|local|cohere_api")


_backend_cache: dict[str, object] = {}


def shared_backend(kind: str | None = None):
    """One backend instance per process, built lazily and reused.

    A gradio Client spawns background threads that are never joined; building
    one per request leaks them until the container hits its thread limit and
    every request dies with "RuntimeError: can't start new thread" (took the
    worker down after ~200 calls on 2026-08-12). On any error the cached
    instance is dropped, so a wedged client (e.g. after the Space restarts)
    costs one failed call, not a restart."""
    key = (kind or os.getenv("ASR_BACKEND", "space")).lower()
    if key not in _backend_cache:
        _backend_cache[key] = make_backend(key)
    return _backend_cache[key]


def drop_shared_backend(kind: str | None = None) -> None:
    key = (kind or os.getenv("ASR_BACKEND", "space")).lower()
    _backend_cache.pop(key, None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# The free Space is a shared community GPU. Under a burst it rejects requests
# rather than queueing them, so a batch that would succeed one at a time comes
# back mostly empty — 17 of 20 calls on 2026-08-11, each of which had
# transcribed fine on its own moments earlier. Retrying with a widening gap
# turns most of those into successes and costs nothing when the first try works.
ASR_RETRIES = 3
ASR_BACKOFF_SEC = 2.0


def _transcribe_with_retry(backend, chunk_path: str) -> str | None:
    """The chunk's text, or None if every attempt failed.

    None and "" are kept distinct on purpose: "" is a chunk of silence the model
    read correctly, None is a chunk nobody read. Only the second is a failure,
    and only failures may move `confidence`.
    """
    import time

    if getattr(backend, "handles_transport_retries", False):
        # The backend already paces and retries transport errors internally;
        # wrapping it again multiplies the attempts (3×4 = 12 requests per
        # chunk at worst) and turns one throttled minute into a burned quota.
        try:
            return backend.transcribe_file(chunk_path)
        except Exception:                       # noqa: BLE001
            return None

    for attempt in range(ASR_RETRIES):
        try:
            return backend.transcribe_file(chunk_path)
        except Exception:                       # noqa: BLE001
            if attempt == ASR_RETRIES - 1:
                return None
            time.sleep(ASR_BACKOFF_SEC * (2 ** attempt))
    return None


def _max_token_run(text: str) -> int:
    """Longest run of one token repeated consecutively. ASR decoders that get
    stuck emit the same word dozens of times; real Gulf/Egyptian speech rarely
    repeats a token more than a handful of times in a row."""
    best = run = 0
    prev = None
    for tok in text.split():
        run = run + 1 if tok == prev else 1
        prev = tok
        best = max(best, run)
    return best


def transcribe_call(path: str, backend=None, work_dir: str | None = None,
                    target_sec: float = 40.0) -> Transcription:
    """Transcribe one call recording into timestamped segments."""
    import tempfile

    owns_backend = backend is None
    backend = backend or shared_backend()
    work_dir = work_dir or tempfile.mkdtemp(prefix="asr_")
    os.makedirs(work_dir, exist_ok=True)

    pcm, rate, channels = read_pcm(path)
    cuts = chunk_at_silences(pcm, rate, target_sec=target_sec)

    segments, failures, empty_ok = [], 0, 0
    failed_seqs: set[int] = set()
    for i in range(len(cuts) - 1):
        start, end = cuts[i], cuts[i + 1]
        chunk_path = _write_chunk(pcm[start:end], rate,
                                  os.path.join(work_dir, f"chunk_{i:04d}.wav"))
        text = _transcribe_with_retry(backend, chunk_path)
        if text is None:
            if owns_backend:
                # The cached client may be wedged (Space restarted, dead
                # session); rebuild rather than fail every future call.
                drop_shared_backend()
                backend = shared_backend()
            text, failures = "", failures + 1
            failed_seqs.add(i)
        elif not text:
            empty_ok += 1
        segments.append(Segment(seq=i, start_sec=round(start / rate, 2),
                                end_sec=round(end / rate, 2), text=text))

    total = len(segments) or 1
    raw_full_text = " ".join(s.text for s in segments if s.text).strip()
    duration = round(len(pcm) / rate, 2)
    token_run = _max_token_run(raw_full_text)

    # Contamination removal + loop truncation, per chunk, before anything
    # downstream sees the text. Segments carry CLEAN text from here on; the
    # removed literals live in the metrics ledger, reconstructible exactly.
    from . import text_quality

    chunk_results = []
    for s in segments:
        cq = text_quality.clean_chunk(s.text, seq=s.seq)
        s.text = cq.clean_text
        chunk_results.append(cq)
    full_text = " ".join(s.text for s in segments if s.text).strip()
    clean_chars = len(full_text.replace(text_quality.GAP, ""))

    quality = text_quality.assess_call(
        chunk_results,
        chunk_durations=[s.end_sec - s.start_sec for s in segments],
        failed_seqs=failed_seqs,
        total_duration=duration,
        clean_chars=clean_chars,
        raw_chars=len(raw_full_text),
        chunks_empty=empty_ok,
    )

    return Transcription(
        full_text=full_text,
        segments=segments,
        duration_seconds=duration,
        sample_rate_hz=rate,
        channels=channels,
        # Not a model confidence — the model does not emit one. This is the
        # share of chunks that came back at all, which is what we can actually
        # measure. Named honestly so nobody reads it as acoustic certainty.
        confidence=round((total - failures) / total, 2),
        diarization="none",
        metrics={
            "chunks_total": total,
            "chunks_failed": failures,
            # Chunks the backend answered with nothing — silence or music read
            # correctly. Distinct from failed: only failed moves `confidence`.
            "chunks_empty": empty_ok,
            "chars": len(raw_full_text),
            "clean_chars": clean_chars,
            # Arabic phone speech lands very roughly at 2-15 chars/sec of
            # audio; near-zero on a long call means lost speech even when
            # every chunk "succeeded".
            "chars_per_audio_sec": round(len(raw_full_text) / duration, 2) if duration else 0.0,
            "max_token_run": token_run,
            "repetition_suspect": token_run >= 6,
            "asr_quality_status": quality["status"],
            "quality": quality,
            "cleaning": {
                "version": text_quality.POLICY_VERSION,
                "ops": [op for cq in chunk_results for op in cq.ops],
                "flags": [f for cq in chunk_results for f in cq.flags],
            },
        },
    )
