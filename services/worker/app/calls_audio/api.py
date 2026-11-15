"""HTTP for the calls lane after the archive: transcribe, then grade.

Both answer 200 even when the work failed, with an `outcome`/`error` and the
model calls that did happen, so workflows 15/16 record the money spent
(rule 12) and spend an attempt, instead of reading a 500 as nothing happened.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException

from ..evaluate.judge import JudgeError

log = logging.getLogger("worker.calls_audio")
UNIQUEID = re.compile(r"^[0-9]{6,12}[.][0-9]{1,9}$")


def _uid(body: dict) -> str:
    uid = body.get("uniqueid")
    if not isinstance(uid, str) or not UNIQUEID.match(uid):
        raise HTTPException(422, "uniqueid")
    return uid


def build_router(require_api_key) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_api_key)])

    @router.post("/calls/transcribe")
    def calls_transcribe(body: dict) -> dict:
        from .service import TranscribeError, transcribe
        uid = _uid(body)
        try:
            return {"uniqueid": uid, **transcribe(uid)}
        except TranscribeError as exc:
            log.warning("transcribe %s failed: %s", uid, exc)
            return {"uniqueid": uid, "outcome": "failed", "error": str(exc)[:400], "calls": exc.calls}
        except Exception as exc:                       # network, model, bucket: retry later
            log.exception("transcribe %s crashed", uid)
            return {"uniqueid": uid, "outcome": "failed", "error": f"{type(exc).__name__}: {exc}"[:400], "calls": []}

    @router.post("/calls/qa/evaluate")
    def calls_qa_evaluate(body: dict) -> dict:
        from ..call_qa.engine import evaluate
        uid = _uid(body)
        try:
            return evaluate(uid)
        except JudgeError as exc:
            return {"uniqueid": uid, "error": str(exc)[:400], "calls": getattr(exc, "calls", [])}

    return router
