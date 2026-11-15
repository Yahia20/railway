"""From the audio service's voices and words to a gradeable, masked call. Pure.

Everything here is arithmetic on timestamps or text (rule 3): masking, turns,
timings, the stereo file. The one judgement — which voice is the agent — is a
model call made by service.py with the dialog built here.
"""
from __future__ import annotations

import io
import re
import wave
from collections import defaultdict

import numpy as np

# Anything that could identify or charge a person never reaches a model or a
# page: card/passport/phone/booking numbers are digit runs, so every run of 4+
# digits (Latin or Arabic-Indic, spaces/dashes allowed inside) is masked.
DIGITS = re.compile(r"(?:[0-9٠-٩۰-۹][\s\-]?){4,}")
EMAIL = re.compile(r"\S+@\S+")
PAUSE_SPLITS_TURN = 2.0      # seconds of silence that start a new line even within one voice


def mask(text: str, names: list[str]) -> str:
    out = EMAIL.sub("[بريد]", text)
    out = DIGITS.sub("[رقم] ", out)
    for n in names:
        if n and len(n) >= 3:
            out = re.sub(re.escape(n), "[اسم]", out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()


def name_parts(customer_name: str | None) -> list[str]:
    """The PBX's caller name, split into the parts worth masking (3+ letters)."""
    if not customer_name:
        return []
    parts = [p for p in re.split(r"[\s\-_.]+", customer_name) if len(p) >= 3 and not p.isdigit()]
    return sorted(set(parts), key=len, reverse=True)


def turns(words: list[list]) -> list[dict]:
    """Consecutive words of one voice become one line; a long pause also breaks a line."""
    out: list[dict] = []
    for start, end, voice, word in words:
        last = out[-1] if out else None
        if last and last["voice"] == voice and start - last["end"] <= PAUSE_SPLITS_TURN:
            last["end"] = end
            last["text"] += word
        else:
            out.append({"voice": voice, "start": start, "end": end, "text": word})
    for t in out:
        t["text"] = t["text"].strip()
    return [t for t in out if t["text"]]


def dialog(lines: list[dict]) -> str:
    """What the agent-voice question sees: masked lines labelled A / B."""
    return "\n".join(f"{t['voice']}: {t['text']}" for t in lines)


def segments(lines: list[dict], agent_voice: str) -> list[dict]:
    """The transcript of record: numbered, speaker-labelled, masked. Evidence
    quotes from the grader must be found inside the segment they name."""
    return [{"id": str(i), "speaker": "agent" if t["voice"] == agent_voice else "customer",
             "start": round(t["start"], 2), "end": round(t["end"], 2), "text": t["text"]}
            for i, t in enumerate(lines, 1)]


def _union(intervals: list[tuple[float, float]]) -> float:
    total, cur_a, cur_b = 0.0, None, None
    for a, b in sorted(intervals):
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


def metrics(out: dict, agent_voice: str, segs: list[dict]) -> dict:
    """Timings from the diariser's raw speech, never from a model (rule 3).
    Overlap on mono audio is reported, never scored as an interruption."""
    raw = out.get("raw_segments") or []
    total = float(out.get("audio_seconds") or 0)
    spoken = out.get("speech_seconds") or {}
    customer_voice = "B" if agent_voice == "A" else "A"
    speech = _union([(a, b) for a, b, _ in raw])
    by_speaker: dict[str, list] = defaultdict(list)
    for a, b, s in raw:
        by_speaker[s].append((a, b))
    overlap = max(0.0, sum(_union(v) for v in by_speaker.values()) - speech)
    gaps, last = [], 0.0
    for a, b in sorted((a, b) for a, b, _ in raw):
        if a > last:
            gaps.append(a - last)
        last = max(last, b)
    agent_s = float(spoken.get(agent_voice, 0.0))
    customer_s = float(spoken.get(customer_voice, 0.0))
    first_agent = next((s["start"] for s in segs if s["speaker"] == "agent"), None)
    return {
        "audio_seconds": round(total, 1),
        "agent_speech_seconds": round(agent_s, 1),
        "customer_speech_seconds": round(customer_s, 1),
        "agent_talk_share": round(agent_s / (agent_s + customer_s), 3) if agent_s + customer_s else None,
        "silence_seconds": round(max(0.0, total - speech), 1),
        "longest_silence_seconds": round(max(gaps), 1) if gaps else 0.0,
        "overlap_seconds": round(overlap, 1),
        "agent_first_words_at": first_agent,
        "voices_heard": out.get("voices"),
    }


def stereo_wav(pcm: bytes, spans: list[list], agent_voice: str) -> bytes:
    """Left = agent, right = customer, same rate and length as the call.
    Each channel keeps the original audio only where its voice owns the moment
    (gap-filled spans), with a 20 ms ramp so cuts do not click."""
    with wave.open(io.BytesIO(pcm)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    n = len(x)
    k = max(1, int(0.02 * rate))
    chans = []
    for want_agent in (True, False):
        m = np.zeros(n, dtype=np.float32)
        for a, b, v in spans:
            if (v == agent_voice) == want_agent:
                m[int(a * rate):min(n, int(b * rate))] = 1.0
        m = np.clip(np.convolve(m, np.ones(k, dtype=np.float32) / k, mode="same"), 0, 1)
        chans.append(x * m)
    st = np.clip(np.stack(chans, 1), -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(st.tobytes())
    return buf.getvalue()
