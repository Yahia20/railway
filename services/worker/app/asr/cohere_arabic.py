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
    """Cohere's hosted copy of the same weights. Lowest ops burden and a real
    SLA. Confirm the endpoint against docs.cohere.com before relying on it —
    the path below is not verified against a live key."""

    def __init__(self, api_key: str, base_url: str = "https://api.cohere.com"):
        import httpx

        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=300.0,
        )

    def transcribe_file(self, path: str) -> str:
        with open(path, "rb") as fh:
            r = self._client.post(
                "/v2/transcribe",
                files={"file": (os.path.basename(path), fh, "audio/wav")},
                data={"model": "cohere-transcribe-arabic-07-2026", "language": "ar"},
            )
        r.raise_for_status()
        payload = r.json()
        return (payload.get("text") or payload.get("transcript") or "").strip()


def make_backend(kind: str | None = None):
    kind = (kind or os.getenv("ASR_BACKEND", "space")).lower()
    if kind == "space":
        return SpaceBackend()
    if kind == "local":
        return LocalBackend()
    if kind == "cohere_api":
        return CohereAPIBackend(os.environ["COHERE_API_KEY"])
    raise ValueError(f"unknown ASR_BACKEND {kind!r}; expected space|local|cohere_api")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def transcribe_call(path: str, backend=None, work_dir: str | None = None,
                    target_sec: float = 40.0) -> Transcription:
    """Transcribe one call recording into timestamped segments."""
    import tempfile

    backend = backend or make_backend()
    work_dir = work_dir or tempfile.mkdtemp(prefix="asr_")
    os.makedirs(work_dir, exist_ok=True)

    pcm, rate, channels = read_pcm(path)
    cuts = chunk_at_silences(pcm, rate, target_sec=target_sec)

    segments, failures = [], 0
    for i in range(len(cuts) - 1):
        start, end = cuts[i], cuts[i + 1]
        chunk_path = _write_chunk(pcm[start:end], rate,
                                  os.path.join(work_dir, f"chunk_{i:04d}.wav"))
        try:
            text = backend.transcribe_file(chunk_path)
        except Exception:                       # noqa: BLE001
            text, failures = "", failures + 1
        segments.append(Segment(seq=i, start_sec=round(start / rate, 2),
                                end_sec=round(end / rate, 2), text=text))

    total = len(segments) or 1
    return Transcription(
        full_text=" ".join(s.text for s in segments if s.text).strip(),
        segments=segments,
        duration_seconds=round(len(pcm) / rate, 2),
        sample_rate_hz=rate,
        channels=channels,
        # Not a model confidence — the model does not emit one. This is the
        # share of chunks that came back at all, which is what we can actually
        # measure. Named honestly so nobody reads it as acoustic certainty.
        confidence=round((total - failures) / total, 2),
        diarization="none",
    )
