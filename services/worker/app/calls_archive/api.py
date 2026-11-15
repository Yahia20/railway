"""HTTP endpoint for the call recordings archive.

  POST /calls/recording/store?uniqueid=<id>&day=<YYYY-MM-DD>
       body: the recording's raw bytes, exactly as the PBX sent them

Called by workflow 12 after it downloads one recording from the PBX. Always
200 with an `outcome` for anything about the bytes; 503 only when the bucket
is not configured, so the run fails loudly instead of recording a fake result.

The body is read as a stream and abandoned as soon as it passes the size cap,
so a request without Content-Length cannot make the worker buffer an
unbounded body. Hashing and the bucket's blocking HTTP calls run in a worker
thread: the event loop keeps serving every other route meanwhile.
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from starlette.concurrency import run_in_threadpool

from ..media.s3 import S3Client, S3Config, StorageUnavailable
from . import store as calls_store
from .attribution import reduce_journey

log = logging.getLogger("worker.calls")

# call-recordings is its own Railway bucket (the owner's choice, 2026-11-01),
# so its credentials arrive under their own prefix, next to chat-media's S3_*.
S3_PREFIX = "CALLS_S3_"

_client: S3Client | None = None
_lock = threading.Lock()
_uploads = threading.Lock()   # one upload at a time: the bucket sees one writer from us


def storage() -> S3Client:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                try:
                    _client = S3Client(S3Config.from_env(prefix=S3_PREFIX))
                except StorageUnavailable as exc:
                    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return _client


def _store_one(uniqueid: str, day: str, data: bytearray, client: S3Client) -> dict:
    """Runs in a worker thread. One upload at a time from this process."""
    with _uploads:
        return calls_store.store_recording(uniqueid, day, data, store=client).as_dict()


def build_router(require_api_key) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_api_key)])

    @router.post("/calls/recording/store")
    async def calls_recording_store(
        request: Request,
        uniqueid: str = Query(max_length=32),
        day: str = Query(max_length=10),
    ) -> dict:
        try:
            calls_store.object_key(uniqueid, day)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > calls_store.MAX_BYTES:
            return {"outcome": "too_large", "bytes": int(declared), "uniqueid": uniqueid}
        client = storage()
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > calls_store.MAX_BYTES:
                log.warning("call recording too large id=%s read=%d", uniqueid, len(data))
                return {"outcome": "too_large", "bytes": len(data), "uniqueid": uniqueid}
        out = await run_in_threadpool(_store_one, uniqueid, day, data, client)
        out["uniqueid"] = uniqueid
        log.info("call recording store id=%s outcome=%s bytes=%s", uniqueid, out["outcome"], out.get("bytes"))
        return out

    @router.post("/calls/journeys/reduce")
    def calls_journeys_reduce(body: dict) -> dict:
        """Workflow 14: reduce each fetched PBX journey to who answered.

        body: {"calls": [{"uniqueid": "...", "status": 200, "body": {...journey...}}]}
        A call whose PBX request failed comes back as `error`, so n8n can retry
        it rather than record a verdict about a response it never got.
        """
        calls = body.get("calls")
        if not isinstance(calls, list) or len(calls) > 100:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "calls must be a list of at most 100")
        out = []
        for c in calls:
            if not isinstance(c, dict) or not isinstance(c.get("uniqueid"), str):
                continue
            ok = c.get("status") == 200 and isinstance(c.get("body"), dict)
            if not ok:
                out.append({"uniqueid": c["uniqueid"], "error": f"PBX answered {c.get('status')!r}"})
                continue
            out.append({"uniqueid": c["uniqueid"], **reduce_journey(c["body"])})
        return {"calls": out}

    return router
