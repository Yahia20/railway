"""Run the real two-pass evaluation on a stored transcript.

    python scripts/evaluate_call.py docs/samples/call-1782914722-transcript.json

Reads DEEPSEEK_API_KEY from the environment. This is the same code path the n8n
workflow drives in production — the only difference is that the transcript comes
from a file instead of straight out of ASR.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))

from app.asr.cohere_arabic import Segment, Transcription   # noqa: E402
from app.evaluate import judge                             # noqa: E402


def load(path: Path) -> Transcription:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [
        Segment(seq=s["idx"], start_sec=s["start_sec"], end_sec=s["end_sec"],
                text=s["text"], speaker="unknown")
        for s in data["segments"]
    ]
    ok = sum(1 for s in data["segments"] if not s.get("error"))
    return Transcription(
        full_text=" ".join(s.text for s in segments if s.text),
        segments=segments,
        duration_seconds=data["duration_sec"],
        sample_rate_hz=data["sample_rate"],
        channels=1,
        confidence=round(ok / max(len(segments), 1), 2),
        diarization="none",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY is not set", file=sys.stderr)
        return 2

    tr = load(args.transcript)
    conversation = tr.as_dialogue()
    metadata = {
        "channel": "phone_call",
        "duration_seconds": tr.duration_seconds,
        "sample_rate_hz": tr.sample_rate_hz,
        "channels": tr.channels,
        "asr_confidence": tr.confidence,
        "asr_provider": tr.provider,
        "diarization": tr.diarization,
        "agent_extension": "3009",
        "customer_phone_e164": "+966500000000",
        "started_at": "2026-07-01T17:05:22+03:00",
    }

    client = judge.DeepSeekClient()
    print(f"transcript: {len(conversation)} chars, {len(tr.segments)} segments")
    print(f"model: {client.model}\n")

    print("── pass 1 · customer extraction ─────────────────────────")
    p1 = judge.run_pass1(conversation, client=client)
    print(json.dumps(p1.payload, ensure_ascii=False, indent=2))

    print("\n── pass 2 · agent quality ───────────────────────────────")
    # followup_history is deliberately omitted: for a lone call we genuinely
    # cannot see whether the agent followed up, so Module 4 must score null.
    p2 = judge.run_pass2(conversation, "call_transcript", metadata=metadata, client=client)

    print(f"final_score      : {p2.score.final_score}")
    print(f"performance_level: {p2.score.performance_level}")
    print(f"weight_applied   : {p2.score.weight_applied}")
    print(f"gradeable        : {p2.score.gradeable}")
    print(f"stage_reached    : {p2.payload.get('stage_reached')}")
    for key, value in p2.score.modules.items():
        print(f"  {key:24s} {value}")
    if p2.warnings:
        print("\nwarnings:")
        for w in p2.warnings:
            print(f"  ! {w}")

    print("\nsummary:")
    print(json.dumps(p2.payload.get("summary", {}), ensure_ascii=False, indent=2))
    print(f"\nnotes: {p2.payload.get('notes')}")

    usage = {"pass1": p1.usage, "pass2": p2.usage}
    print(f"\ntokens: {json.dumps(usage)}")

    if args.out:
        args.out.write_text(json.dumps(
            {"pass1": p1.payload, "pass2": p2.payload,
             "score": {"final": p2.score.final_score,
                       "level": p2.score.performance_level,
                       "weight_applied": p2.score.weight_applied,
                       "modules": p2.score.modules},
             "warnings": p2.warnings, "usage": usage},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
