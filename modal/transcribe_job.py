"""Nightly Arabic transcription batch, on Modal.

    modal deploy modal/transcribe_job.py        # installs the cron
    modal run modal/transcribe_job.py::main --limit 5 --dry-run   # try it first

WHY THIS IS NOT IN THE WORKER. Transcription used to run inside the Railway
worker. Loading a call into memory and decoding it took that container from
0.058 GB to 4.675 GB on 2026-08-13, and Railway bills reserved RAM by the
month whether or not you are using it — so two weeks of experiments cost about
$13.50/month for a job that runs for minutes. Modal bills by the second and
scales to zero, so the same work costs nothing between runs.

WHY A BATCH AND NOT A REQUEST PER CALL. Every run pays a fixed ~15 minutes of
provisioning, image pull and weight load before it transcribes anything. At 200
calls/day that overhead is 7.5 GPU-hours a month against 6.7 hours of actual
work — daily batching already costs more in warm-up than in transcription, and
per-call invocation would be absurd. One run a night, everything pending.

THE BOUNDARY WITH n8n. n8n discovers recordings and writes `call_ingest_jobs`
rows; this job owns every row in 'discovered' or 'asr_failed' and leaves them
'transcribed'; n8n claims them back from there and judges them. Both sides take
a lease before touching a row, because two systems that both claim a job both
transcribe it and both pay for it — the exact bug the lease was added to stop.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import modal

APP_NAME = "travelgate-asr"
MODEL_ID = "CohereLabs/cohere-transcribe-arabic-07-2026"
MODEL_VERSION = "07-2026"

# Pick by measured volume, not by feel. The break-evens from the hosting
# research: L4 up to ~60 audio-hours/month, A10 up to ~330, H100 above that.
# A10 covers 200 calls/day at 8 minutes each (800 audio-hours) — but only if
# RTFx really is ~120, which nothing has measured yet. `asr_runs.rtfx` is
# computed from the first real run and settles it.
GPU = os.environ.get("ASR_GPU", "A10G")
GPU_HOURLY_USD = float(os.environ.get("ASR_GPU_HOURLY_USD", "1.20"))

# 40 seconds, cut at the quietest frame nearby. The model has no timestamps, so
# offsets come from knowing where we cut; cutting on a fixed grid slices words
# in half and the model then guesses at both halves.
CHUNK_SECONDS = float(os.environ.get("ASR_CHUNK_SECONDS", "40"))

# A lease long enough that a slow batch does not have its own rows reclaimed
# underneath it, short enough that a crashed run frees them the same night.
LEASE_SECONDS = 6 * 3600

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch==2.5.1", "transformers==4.48.0", "accelerate==1.2.1",
        "numpy==2.2.1", "httpx==0.28.1",
        "google-api-python-client==2.156.0", "google-auth==2.37.0",
    )
    # The chunker and the WAV reader are already written and already tested in
    # the repo. Copying the module is how this job and the worker stay one
    # implementation instead of two that drift.
    .add_local_file(
        "services/worker/app/asr/cohere_arabic.py",
        "/root/cohere_arabic.py", copy=True,
    )
)

app = modal.App(APP_NAME, image=image)

# Weights are ~4 GB and take minutes to fetch. A Volume holds them between runs,
# and Modal's first 1 TiB of volume storage is free, so the cache costs nothing.
weights = modal.Volume.from_name("travelgate-asr-weights", create_if_missing=True)

SECRETS = [
    # WORKER_URL + WORKER_API_KEY. This job no longer touches Postgres: Modal
    # runs outside Railway, `postgres.railway.internal` is Railway's PRIVATE
    # network, and psycopg failed with "Name or service not known" on the very
    # first call. The alternative was exposing the database to the internet.
    modal.Secret.from_name("travelgate-worker"),
    modal.Secret.from_name("travelgate-drive"),   # GOOGLE_SERVICE_ACCOUNT_JSON
    modal.Secret.from_name("travelgate-hf"),      # HF_TOKEN — the model is gated
]


# ---------------------------------------------------------------------------
# The worker — every statement this job used to run itself
#
# The SQL did not change; it moved. services/worker/app/asr_jobs.py holds the
# same CLAIM / STORE / FAIL statements byte-for-byte, still fenced by the lease
# token this run holds, and the boundary of gotcha 13 is still one WHERE on each
# side. What changed is who executes them, and therefore who needs a database
# password: nobody outside Railway.
# ---------------------------------------------------------------------------

def _worker(path: str, payload: dict, timeout: float = 120.0) -> dict:
    """One call to the worker. Raises on anything that is not a 2xx.

    A failure here is not recoverable inside the batch — if the worker cannot
    be reached, the lease cannot be released either — so it propagates and the
    lease expires on its own. That is the behaviour the recovery sweep in
    workflow 02 already exists to handle.
    """
    import httpx

    base = os.environ["WORKER_URL"].rstrip("/")
    r = httpx.post(f"{base}{path}",
                   headers={"X-API-Key": os.environ["WORKER_API_KEY"]},
                   json=payload, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"worker {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def _drive_download(file_id: str, dest: str) -> str:
    """Pull one recording straight from Drive into the container.

    The audio never touches Railway. That is the point: routing it through the
    worker is what made the worker expensive, and a WAV moving between two
    clouds is also egress somebody pays for.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, drive.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------

@app.function(
    gpu=GPU,
    image=image,
    secrets=SECRETS,
    volumes={"/weights": weights},
    timeout=6 * 3600,
    # 23:30 Riyadh, after n8n's 23:00 discovery pass has registered the day's
    # recordings and before the 01:00 judging window opens on what we produce.
    schedule=modal.Cron("30 23 * * *", timezone="Asia/Riyadh"),
)
def transcribe_batch(limit: int = 500, max_attempts: int = 3,
                     dry_run: bool = False) -> dict:
    import sys
    sys.path.insert(0, "/root")
    import cohere_arabic as ca  # the repo's chunker and WAV reader

    run_id = f"asr-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()

    # The lease token is minted by the worker, not here. It is the fence every
    # later write is checked against, and a caller that chose it could reuse
    # another run's.
    token = _worker("/asr/run/start", {
        "run_id": run_id, "gpu": GPU, "model_version": MODEL_VERSION,
    })["claim_token"]

    claimed = _worker("/asr/claim", {
        "run_id": run_id, "claim_token": token,
        "limit": limit, "max_attempts": max_attempts,
    })
    rows = [(r["uniqueid"], r["filename"], r["audio_uri"], r["meta"])
            for r in claimed["recordings"]]
    print(f"[{run_id}] claimed {len(rows)} recordings")

    if dry_run or not rows:
        # A dry run must not keep the rows it looked at, and must give back the
        # attempt it spent — otherwise --dry-run slowly dead-letters the very
        # backlog it exists to inspect safely.
        if dry_run and rows:
            released = _worker("/asr/release", {"claim_token": token})["released"]
            print(f"[{run_id}] released {released} back to discovered")
        _worker("/asr/run/finish", {"run_id": run_id, "status": "succeeded",
                                    "gpu_seconds": time.monotonic() - started})
        return {"run_id": run_id, "claimed": len(rows), "dry_run": dry_run}

    # One model per container, loaded once. Loading per call would pay the
    # weight-load cost on every recording instead of once per batch.
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
    os.environ.setdefault("HF_HOME", "/weights/hf")
    processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        MODEL_ID, trust_remote_code=True, torch_dtype=torch.bfloat16).to("cuda")

    processed = failed = 0
    audio_seconds = 0.0

    for uniqueid, filename, audio_uri, meta in rows:
        try:
            local = f"/tmp/{uniqueid}.wav"
            file_id = (audio_uri or "").replace("drive://", "")
            _drive_download(file_id, local)

            pcm, rate, channels = ca.read_pcm(local)
            duration = len(pcm) / rate
            audio_seconds += duration

            cuts = ca.chunk_at_silences(pcm, rate, target_sec=CHUNK_SECONDS)
            segments, texts, missing = [], [], 0
            for seq, (a, b) in enumerate(zip(cuts, cuts[1:])):
                import numpy as np
                audio = pcm[a:b].astype(np.float32) / 32768.0
                if rate != 16000:
                    idx = np.linspace(0, len(audio) - 1, int(len(audio) * 16000 / rate))
                    audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
                inputs = processor(audio, sampling_rate=16000,
                                   return_tensors="pt").to("cuda")
                with torch.no_grad():
                    ids = model.generate(**inputs, max_new_tokens=440)
                text = processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
                if text is None:
                    missing += 1
                    continue
                segments.append({"seq": seq, "start_sec": a / rate,
                                 "end_sec": b / rate, "text": text,
                                 "speaker": "unknown"})
                texts.append(text)

            # Chunk return rate, the same honest measure the worker records: it
            # says how much of the audio came back, not how good it was.
            confidence = round(1.0 - missing / max(1, len(cuts) - 1), 2)
            transcript = {
                "duration_seconds": round(duration, 2),
                "sample_rate_hz": rate,
                "channels": channels,
                "provider": "cohere-transcribe-arabic",
                "model_version": MODEL_VERSION,
                "confidence": confidence,
                "full_text": "\n".join(texts),
                "segments": segments,
                # Mono, and nothing separated the speakers. Saying so is what
                # makes pass 2 suppress the absolute rules instead of handing
                # out zeros on a guessed attribution.
                "diarization": "none",
                "asr_metrics": {
                    "chunks": len(cuts) - 1, "chunks_missing": missing,
                    "asr_quality_status": "green" if confidence >= 0.7 else "red",
                    "run_id": run_id,
                },
            }
            result = _worker("/asr/store", {
                "uniqueid": uniqueid, "claim_token": token,
                "meta": meta or {}, "transcript": transcript,
            })
            if not result.get("stored"):
                # The lease fence rejected the write: it expired, or another run
                # reclaimed the row. The GPU time is spent either way, but this
                # must not be counted as a success.
                failed += 1
                print(f"[{run_id}] {uniqueid}: lease lost, transcript discarded")
                continue
            processed += 1
            print(f"[{run_id}] {uniqueid}: {duration:.0f}s, {len(texts)} chunks, "
                  f"confidence {confidence}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            try:
                _worker("/asr/fail", {
                    "uniqueid": uniqueid, "claim_token": token,
                    "max_attempts": max_attempts,
                    "error": f"{type(exc).__name__}: {exc}"})
            except Exception as report_exc:   # noqa: BLE001
                # Could not even record the failure. The lease expires on its
                # own and workflow 02's recovery sweep reopens the row.
                print(f"[{run_id}] {uniqueid} FAILED and could not be recorded: "
                      f"{report_exc}")
            print(f"[{run_id}] {uniqueid} FAILED: {exc}")
        finally:
            try:
                os.remove(local)
            except OSError:
                pass

    gpu_seconds = time.monotonic() - started
    run = _worker("/asr/run/finish", {
        "run_id": run_id,
        "status": "succeeded" if not failed else "partial",
        "processed": processed, "failed": failed,
        "audio_seconds": round(audio_seconds, 1),
        "gpu_seconds": round(gpu_seconds, 1),
        "est_cost_usd": round(gpu_seconds / 3600 * GPU_HOURLY_USD, 4),
    }).get("run") or {}

    # rtfx comes back COMPUTED — it is a generated column, so this is the first
    # measurement of the number every cost estimate in this project assumed.
    rtfx = float(run.get("rtfx") or 0) or (audio_seconds / gpu_seconds if gpu_seconds else 0)
    print(f"[{run_id}] done: {processed} ok, {failed} failed, "
          f"{audio_seconds/3600:.2f} audio-hours in {gpu_seconds/3600:.2f} GPU-hours "
          f"(RTFx {rtfx:.0f}, ${gpu_seconds/3600*GPU_HOURLY_USD:.2f})")
    return {"run_id": run_id, "processed": processed, "failed": failed,
            "audio_seconds": audio_seconds, "gpu_seconds": gpu_seconds, "rtfx": rtfx}


@app.local_entrypoint()
def main(limit: int = 500, dry_run: bool = False):
    """`modal run modal/transcribe_job.py::main --limit 5 --dry-run` first.

    The dry run claims and releases without loading the model, which proves the
    database boundary works before any GPU time is spent on it.
    """
    print(transcribe_batch.remote(limit=limit, dry_run=dry_run))
