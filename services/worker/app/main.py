"""HTTP surface for the worker. n8n orchestrates; this service does the work.

The split is deliberate. n8n is good at scheduling, retries, branching and
showing a business user what ran. It is bad at 400 lines of ASR chunking and
rubric arithmetic, which belong in tested Python. So every n8n node here is a
single HTTP call to one of these endpoints.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .asr import cohere_arabic
from .config import settings
from .evaluate import judge, metrics, scoring
from .normalize.phone import try_normalize
from .sources.bitrix_chats import BitrixWebhookSource
from .sources.drive_calls import RecordingNameError, parse_recording_name

log = logging.getLogger("worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="Customer 360 worker", version="1.0.0")


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Shared-secret auth. The worker is reachable on Railway's private network,
    but n8n workflows get exported, shared and pasted into chats — so the
    endpoint is never left open on the assumption the network protects it."""
    if not settings.worker_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WORKER_API_KEY not configured")
    if x_api_key != settings.worker_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing X-API-Key")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ParseChatRequest(BaseModel):
    payload: dict[str, Any]


class TranscribeRequest(BaseModel):
    audio_path: str = Field(description="local path, or drive://<fileId>")
    filename: str | None = Field(default=None, description="for PBX metadata parsing")


class EvaluateRequest(BaseModel):
    conversation: str
    input_type: Literal["chat", "call_transcript"]
    metadata: dict[str, Any] = Field(default_factory=dict)
    followup_history: str | None = None
    run_pass1: bool = True
    run_pass2: bool = True


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    """Liveness only — no dependency checks, so Railway does not restart the
    container because DeepSeek had a slow minute."""
    return {"status": "ok", "version": app.version}


@app.get("/ready", dependencies=[Depends(require_api_key)])
def ready() -> dict:
    """What is actually configured. The go-live checklist reads this."""
    def state(*caps: str) -> str:
        try:
            settings.validate_for(*caps)
            return "ready"
        except RuntimeError as exc:
            return str(exc)

    return {
        "database": state("db"),
        "judge": state("judge"),
        "chats_source": state("chats"),
        "calls_source": state("calls"),
        "asr_backend": settings.asr_backend,
        "rubric_version": scoring.RUBRIC_VERSION,
        "prompt_versions": {"pass1": judge.PASS1_VERSION, "pass2": judge.PASS2_VERSION},
        "default_phone_region": settings.default_phone_region,
    }


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

@app.post("/chats/parse", dependencies=[Depends(require_api_key)])
def parse_chat(req: ParseChatRequest) -> dict:
    """Normalise a Bitrix webhook payload into our conversation shape.

    Returns the computed metrics alongside, because those must be calculated
    from timestamps here and handed to the judge — never inferred by the model.
    """
    try:
        conv = BitrixWebhookSource.parse(req.payload)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    phone, phone_error = try_normalize(conv.customer_phone_raw, settings.default_phone_region)
    computed = metrics.compute_chat_metrics(conv)

    return {
        "external_id": conv.external_id,
        "external_source": conv.external_source,
        "channel": conv.channel,
        "started_at": conv.started_at.isoformat(),
        "ended_at": conv.ended_at.isoformat() if conv.ended_at else None,
        "customer_phone_e164": phone,
        "phone_error": phone_error,
        "bitrix_deal_id": conv.bitrix_deal_id,
        "bitrix_contact_id": conv.bitrix_contact_id,
        "agent_external_id": conv.agent_external_id,
        "is_bot_only": conv.is_bot_only,
        # Bot-only threads must never reach agent scoring: the bot qualifies the
        # customer before a human joins, and grading humans on bot messages makes
        # every QA number wrong.
        "should_evaluate": not conv.is_bot_only or settings.score_bot_only_conversations,
        "messages": [
            {"seq": m.seq, "sender": m.sender, "body": m.body, "sent_at": m.sent_at.isoformat()}
            for m in conv.messages
        ],
        "transcript_text": conv.transcript_text(),
        "metrics": computed.as_dict(),
    }


@app.post("/calls/parse-name", dependencies=[Depends(require_api_key)])
def parse_call_name(filename: str) -> dict:
    try:
        meta = parse_recording_name(filename, settings.pbx_tz_offset_hours)
    except RecordingNameError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    phone, phone_error = try_normalize(meta["customer_phone_raw"], settings.default_phone_region)
    meta["started_at"] = meta["started_at"].isoformat()
    meta["customer_phone_e164"] = phone
    meta["phone_error"] = phone_error
    return meta


@app.post("/calls/transcribe", dependencies=[Depends(require_api_key)])
def transcribe(req: TranscribeRequest) -> dict:
    path = req.audio_path
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"audio not found: {path}")

    result = cohere_arabic.transcribe_call(
        path, work_dir=settings.work_dir, target_sec=settings.asr_chunk_seconds
    )
    return {
        "full_text": result.full_text,
        "dialogue": result.as_dialogue(),
        "language": result.language,
        "provider": result.provider,
        "model_version": result.model_version,
        "duration_seconds": result.duration_seconds,
        "sample_rate_hz": result.sample_rate_hz,
        "channels": result.channels,
        "confidence": result.confidence,
        "diarization": result.diarization,
        "segments": [
            {"seq": s.seq, "start_sec": s.start_sec, "end_sec": s.end_sec,
             "speaker": s.speaker, "text": s.text}
            for s in result.segments
        ],
        # Loud, structured, and carried all the way to the evaluation row.
        # Without diarization every agent score rests on the judge inferring who
        # spoke, and that must never become an invisible assumption.
        "warnings": (
            ["no speaker diarization: agent/customer attribution is inferred by the "
             "judge from content, not measured. Absolute rules (anger, ignoring the "
             "customer, defeatist language) are suppressed in this mode."]
            if result.diarization == "none" else []
        ),
    }


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

@app.post("/evaluate", dependencies=[Depends(require_api_key)])
def evaluate(req: EvaluateRequest) -> dict:
    settings.validate_for("judge")
    client = judge.DeepSeekClient(settings.deepseek_api_key, settings.deepseek_model)

    out: dict[str, Any] = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "rubric_version": scoring.RUBRIC_VERSION,
    }

    if req.run_pass1:
        p1 = judge.run_pass1(req.conversation, client=client)
        out["pass1"] = {
            "payload": p1.payload, "prompt_version": p1.prompt_version,
            "model": p1.model, "usage": p1.usage, "input_hash": p1.input_hash,
        }

    if req.run_pass2:
        p2 = judge.run_pass2(
            req.conversation, req.input_type,
            metadata=req.metadata, followup_history=req.followup_history, client=client,
        )
        out["pass2"] = {
            "payload": p2.payload,
            "final_score": p2.score.final_score,
            "performance_level": p2.score.performance_level,
            "weight_applied": p2.score.weight_applied,
            "gradeable": p2.score.gradeable,
            "modules": p2.score.modules,
            "warnings": p2.warnings,
            "prompt_version": p2.prompt_version,
            "model": p2.model,
            "usage": p2.usage,
            "input_hash": p2.input_hash,
        }

    return out


@app.post("/score/recompute", dependencies=[Depends(require_api_key)])
def recompute(modules: dict[str, Any]) -> dict:
    """Re-run the weighting on stored module breakdowns, no model call.

    Use this after a rubric revision to rescore history without paying for
    re-evaluation, and to prove a score is reproducible from its breakdown.
    """
    result = scoring.compute(modules)
    return {
        "final_score": result.final_score,
        "performance_level": result.performance_level,
        "weight_applied": result.weight_applied,
        "modules": result.modules,
        "gradeable": result.gradeable,
        "warnings": result.warnings,
    }
