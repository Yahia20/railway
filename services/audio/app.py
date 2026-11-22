"""calls-audio: the CPU service that listens to call recordings.

    POST /process   {"url": "<short-lived GET URL of one WAV>"}   X-API-Key required
    GET  /health

Called by workflow 15 over Railway's private network. It holds no database
credentials and no bucket keys: the worker hands it a URL that works for a few
minutes, and it returns voices and words (pipeline.process). It writes nothing
anywhere. One recording at a time, so memory stays flat and the bill is
compute that actually ran.

The models live in ONE child process, never in this one, and the child is
shut down after IDLE_UNLOAD_SECONDS (600) with no work. Railway bills RAM that
is held, busy or not (10 USD per GB-month): with the models cached in the
server process, one Whisper run kept 2.75 GB for two days at 0 CPU, and Cohere
peaks at 6-8 GB, which is 40-80 USD a month standing still. Freeing a cache
inside a live Python process does not reliably give the memory back to the
OS; ending the process does. The first job after an unload pays the model
load from the image's disk. App sleeping (railway.json) is the second line:
with the child gone this process sends nothing, so Railway can stop it too.
"""
from __future__ import annotations

import hmac
import logging
import os
import socket
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing as mp

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("calls-audio")
app = FastAPI(title="calls-audio", docs_url=None, redoc_url=None)
_one = threading.Lock()
MAX_BYTES = 80 * 1024 * 1024        # ~40 min of 8 kHz PCM; the archive's own cap is lower
ALLOWED_PREFIX = os.getenv("ALLOWED_URL_PREFIX", "https://")
IDLE_UNLOAD_SECONDS = float(os.getenv("IDLE_UNLOAD_SECONDS", "600"))

_pool: ProcessPoolExecutor | None = None     # guarded by _one
_last_used = time.monotonic()


def _child_init() -> None:
    logging.basicConfig(level=logging.INFO)


def _process_in_child(data: bytes) -> dict:
    import pipeline                       # imported only here: torch never enters the server process
    return pipeline.process(data)


def _child() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"),
                                    initializer=_child_init)
    return _pool


def _unload() -> bool:
    """End the child and with it every model. Caller holds _one."""
    global _pool
    if _pool is None:
        return False
    pool, _pool = _pool, None
    pool.shutdown(wait=True, cancel_futures=True)
    return True


def _idle_watch() -> None:
    while True:
        time.sleep(30)
        if time.monotonic() - _last_used < IDLE_UNLOAD_SECONDS or not _one.acquire(blocking=False):
            continue
        try:
            if time.monotonic() - _last_used >= IDLE_UNLOAD_SECONDS and _unload():
                log.info("idle %ss: models unloaded", int(IDLE_UNLOAD_SECONDS))
        finally:
            _one.release()


class Job(BaseModel):
    url: str = Field(min_length=10, max_length=4000)


def _check(key: str | None) -> None:
    want = os.getenv("AUDIO_API_KEY", "")
    if not want or not key or not hmac.compare_digest(key, want):
        raise HTTPException(401, "bad key")


def _run(url: str) -> dict:
    global _last_used
    with _one:
        try:
            return _run_locked(url)
        finally:
            _last_used = time.monotonic()


def _run_locked(url: str) -> dict:
    global _pool
    t = time.monotonic()
    with httpx.Client(timeout=60, follow_redirects=False) as c:
        r = c.get(url)
    if r.status_code != 200:
        return {"error": f"recording fetch answered {r.status_code}"}
    if len(r.content) > MAX_BYTES:
        return {"error": "recording too large"}
    fetched = time.monotonic() - t
    try:
        out = _child().submit(_process_in_child, r.content).result()
    except ValueError as exc:          # not 16-bit PCM WAV
        return {"error": f"unreadable audio: {exc}"}
    except BrokenProcessPool:          # the child died (out of memory): the next job starts a fresh one
        _pool = None
        return {"error": "audio worker process died"}
    out["fetch_seconds"] = round(fetched, 2)
    log.info("processed audio=%ss compute=%ss words=%s",
             out.get("audio_seconds"), out.get("compute_seconds"), len(out.get("words") or []))
    return out


@app.get("/health")
def health() -> dict:
    return {"ok": True, "models_loaded": _pool is not None}


@app.post("/process")
async def process(job: Job, x_api_key: str | None = Header(default=None)) -> dict:
    _check(x_api_key)
    if not job.url.startswith(ALLOWED_PREFIX):
        raise HTTPException(422, "url not allowed")
    return await run_in_threadpool(_run, job.url)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=_idle_watch, name="idle-unload", daemon=True).start()
    port = int(os.getenv("PORT") or 8000)
    # Railway's private network is IPv6-only and its healthcheck is IPv4
    # (gotcha 2): bind one socket that takes both.
    sock = socket.create_server(("::", port), family=socket.AF_INET6, dualstack_ipv6=True)
    config = uvicorn.Config(app, log_level="info", timeout_keep_alive=75)
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
