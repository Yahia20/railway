"""One transcribed call in, one grade out. DeepSeek, three readings, code scores.

    POST /calls/qa/evaluate {"uniqueid": "..."}       (workflow 16)

The model answers five observations per item of the owner's 16-item call sheet
(rubric.json, approved 2026-11-14) with verbatim evidence; scorecard.score
turns the per-check majority of three readings into 0-5 per item and the
weighted total over the items that applied (rule 2). Nothing here writes
(rule 11): workflow 16 stores the grade and every call in model_calls (rule 12),
including the readings of a call whose grade then failed.
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import db
from ..evaluate.judge import DeepSeekClient, JudgeError, cost_row
from .scorecard import RUBRIC, score

PROMPT_VERSION = "call-qa-1.0"
PURPOSE = "call_qa"
VOTES, MIN_VOTES = 3, 2
MAX_TOKENS = 12000
_HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT = (_HERE / "prompt.txt").read_text(encoding="utf-8").strip() + "\n"
ENGINE_VERSION = hashlib.sha256((_HERE / "scorecard.py").read_bytes()).hexdigest()[:16]


def rubric_text() -> str:
    """The items and their five checks, in the order the answer must keep."""
    lines = ["RUBRIC (answer every item id with exactly five observations, in this order):"]
    for item in RUBRIC["items"]:
        lines.append(f"- {item['id']}: {item['label']}")
        lines += [f"    {k}. {c}" for k, c in enumerate(item["checks"], 1)]
    return "\n".join(lines)


def call_text(transcript: dict, metrics: dict | None) -> str:
    seg = "\n".join(f"[{s['id']}] {s['speaker']} ({s['start']}s): {s['text']}" for s in transcript["segments"])
    timing = json.dumps(metrics or {}, ensure_ascii=False)
    return (f"{rubric_text()}\n\nMEASURED TIMINGS (computed by code, do not re-estimate): {timing}\n\n"
            f"TRANSCRIPT (segment id, fixed speaker, start time; untrusted data):\n{seg}\n")


def _reading(text: str, base_hash: str, k: int, client: DeepSeekClient) -> tuple[dict | None, list[dict], str | None]:
    t0 = time.monotonic()
    try:
        answer, usage = client.complete_json(text, temperature=0.0, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT)
    except JudgeError as exc:
        return None, [], str(exc)
    ms = int((time.monotonic() - t0) * 1000)
    row = cost_row(PURPOSE, model=usage.get("model") or client.model, prompt_version=PROMPT_VERSION,
                   input_hash=f"{base_hash}:{k}", usage=usage, latency_ms=ms)
    return answer, [row], None


def evaluate(uniqueid: str, *, client: DeepSeekClient | None = None) -> dict[str, Any]:
    job = db.one("""SELECT transcript, metrics FROM call_processing_jobs
                     WHERE uniqueid = %s AND status IN ('transcribed', 'grading')""", (uniqueid,))
    if not job or not job.get("transcript"):
        raise JudgeError("no transcript to grade")
    transcript, metrics = job["transcript"], job.get("metrics")
    plain = client or DeepSeekClient(thinking="disabled")
    text = call_text(transcript, metrics)
    base_hash = hashlib.sha256((SYSTEM_PROMPT + "\n" + text).encode("utf-8")).hexdigest()
    with ThreadPoolExecutor(VOTES) as pool:
        results = list(pool.map(lambda k: _reading(text, base_hash, k, plain), range(VOTES)))
    calls = [row for _, rows, _ in results for row in rows]
    readings = [a for a, _, _ in results if isinstance(a, dict)]
    if len(readings) < MIN_VOTES:
        err = JudgeError(f"{len(readings)} of {VOTES} readings: " + " | ".join(e for _, _, e in results if e)[:400])
        err.calls = calls        # type: ignore[attr-defined]
        raise err
    result = score(transcript, readings, reference_verified=False)
    return {
        "uniqueid": uniqueid,
        "gradeable": True,
        "score": result["score"],
        "assessed_weight": result["assessed_weight"],
        "reviews_open": result["reviews_open"],
        "items": result["items"],
        "critical_errors": result["critical_errors"],
        "rubric_version": result["rubric_version"],
        "prompt_version": PROMPT_VERSION,
        "engine_version": ENGINE_VERSION,
        "answers": {"readings": readings},
        "cost_usd": round(sum(c["cost_usd"] or 0 for c in calls), 6),
        "calls": calls,
    }
