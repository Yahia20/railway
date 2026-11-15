"""calls-audio: the CPU service that listens to call recordings.

    POST /process   {"url": "<short-lived GET URL of one WAV>"}   X-API-Key required
    GET  /health

Called by workflow 15 over Railway's private network. It holds no database
credentials and no bucket keys: the worker hands it a URL that works for a few
minutes, and it returns voices and words (pipeline.process). It writes nothing
anywhere. One recording at a time, so memory stays flat and the bill is
compute that actually ran.

Railway's app sleeping stops this container when idle, so it costs nothing
between nightly batches; the first request after a sleep pays the model load.
"""
from __future__ import annotations

import hmac
import logging
import os
import socket
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import pipeline

log = logging.getLogger("calls-audio")
app = FastAPI(title="calls-audio", docs_url=None, redoc_url=None)
_one = threading.Lock()
MAX_BYTES = 80 * 1024 * 1024        # ~40 min of 8 kHz PCM; the archive's own cap is lower
ALLOWED_PREFIX = os.getenv("ALLOWED_URL_PREFIX", "https://")


class Job(BaseModel):
    url: str = Field(min_length=10, max_length=4000)


def _check(key: str | None) -> None:
    want = os.getenv("AUDIO_API_KEY", "")
    if not want or not key or not hmac.compare_digest(key, want):
        raise HTTPException(401, "bad key")


def _run(url: str) -> dict:
    with _one:
        t = time.monotonic()
        with httpx.Client(timeout=60, follow_redirects=False) as c:
            r = c.get(url)
        if r.status_code != 200:
            return {"error": f"recording fetch answered {r.status_code}"}
        if len(r.content) > MAX_BYTES:
            return {"error": "recording too large"}
        fetched = time.monotonic() - t
        try:
            out = pipeline.process(r.content)
        except ValueError as exc:          # not 16-bit PCM WAV
            return {"error": f"unreadable audio: {exc}"}
        out["fetch_seconds"] = round(fetched, 2)
        log.info("processed audio=%ss compute=%ss words=%s",
                 out.get("audio_seconds"), out.get("compute_seconds"), len(out.get("words") or []))
        return out


@app.get("/health")
def health() -> dict:
    return {"ok": True, "models_loaded": pipeline.diariser.cache_info().currsize > 0}


@app.post("/process")
async def process(job: Job, x_api_key: str | None = Header(default=None)) -> dict:
    _check(x_api_key)
    if not job.url.startswith(ALLOWED_PREFIX):
        raise HTTPException(422, "url not allowed")
    return await run_in_threadpool(_run, job.url)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("PORT") or 8000)
    # Railway's private network is IPv6-only and its healthcheck is IPv4
    # (gotcha 2): bind one socket that takes both.
    sock = socket.create_server(("::", port), family=socket.AF_INET6, dualstack_ipv6=True)
    config = uvicorn.Config(app, log_level="info", timeout_keep_alive=75)
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
