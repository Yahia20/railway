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

    # `pass1_validation` is inside the payload above too. Printed again here
    # because an unverified real_ask quote is the one line in this output a
    # human must not scroll past: it means the follow-up task built on it is
    # citing something the customer never said.
    validation = p1.validation
    bad_promises = [p["index"] for p in validation.get("promises") or []
                    if not p["quote_valid"]]
    if validation.get("real_ask_quote_valid") is False or bad_promises:
        print("\n  ! QUOTES FAILED VALIDATION "
              f"(validator {validation.get('validator_version')}):")
        if validation.get("real_ask_quote_valid") is False:
            print("    ! real_ask cites a quote that is not in the conversation")
        for index in bad_promises:
            print(f"    ! promises_made_by_agent[{index}] quote not found")

    print("\n── pass 2 · agent quality ───────────────────────────────")
    # followup_history is deliberately omitted: for a lone call we genuinely
    # cannot see whether the agent followed up, so Module 4 must score null.
    p2 = judge.run_pass2(conversation, "call_transcript", metadata=metadata, client=client)

    # A self-contradicting response is now a returned result, not an exception,
    # so `final_score: None` below would otherwise be the only clue that this
    # conversation was never actually graded. Say it in words instead.
    if p2.contract_status != "ok":
        print(f"contract_status  : {p2.contract_status.upper()} — NOT GRADED")
        for violation in p2.contract_violations:
            print(f"    ! {violation}")
    else:
        print(f"contract_status  : {p2.contract_status}")

    print(f"final_score      : {p2.score.final_score}")
    # What the model's own breakdown scored before evidence enforcement touched
    # it. Printed beside the final so the two causes of a moved number stay
    # separable: the judge judged differently, or the code restored a deduction.
    if p2.pre_enforcement_score is not None and             p2.pre_enforcement_score != p2.score.final_score:
        print(f"  (before evidence enforcement: {p2.pre_enforcement_score})")
    print(f"performance_level: {p2.score.performance_level}")
    print(f"weight_applied   : {p2.score.weight_applied}")
    print(f"gradeable        : {p2.score.gradeable}")
    print(f"stage_reached    : {p2.payload.get('stage_reached')}")
    for key, value in p2.score.modules.items():
        print(f"  {key:24s} {value}")
    # Each of these is a deduction the model made and could not support, so the
    # criterion went back to its cap and the score above was recomputed without
    # it. Listing them is the audit trail for why an agent's number went up.
    if p2.evidence_rejected:
        print(f"\nfindings discarded for want of evidence ({len(p2.evidence_rejected)}):")
        for rejection in p2.evidence_rejected:
            print(f"  ! {rejection['module']}.{rejection['criterion']}: "
                  f"{rejection['model_score']} → {rejection['restored_to']} "
                  f"({rejection['reason']})")
            if rejection.get("quote"):
                print(f"      offered: {rejection['quote'][:120]}")

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
             "pass1_validation": p1.validation,
             "score": {"final": p2.score.final_score,
                       "pre_enforcement": p2.pre_enforcement_score,
                       "level": p2.score.performance_level,
                       "weight_applied": p2.score.weight_applied,
                       "modules": p2.score.modules},
             "contract_status": p2.contract_status,
             "contract_violations": p2.contract_violations,
             "evidence_rejected": p2.evidence_rejected,
             "warnings": p2.warnings, "usage": usage},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
