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
        "numpy==2.2.1", "psycopg[binary]==3.2.3",
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
    modal.Secret.from_name("travelgate-db"),      # DATABASE_URL
    modal.Secret.from_name("travelgate-drive"),   # GOOGLE_SERVICE_ACCOUNT_JSON
    modal.Secret.from_name("travelgate-hf"),      # HF_TOKEN — the model is gated
]


# ---------------------------------------------------------------------------
# Database — every statement fenced by the lease this run holds
# ---------------------------------------------------------------------------

def _connect():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


CLAIM_SQL = """
-- Take a bounded batch of untranscribed calls and stamp this run on them.
--
-- FOR UPDATE SKIP LOCKED, not a plain FOR UPDATE: a second run started by hand
-- from the dashboard walks past locked rows instead of queueing behind them.
--
-- ATTEMPTS ARE SPENT AT CLAIM TIME. A run that dies before it can record
-- anything has still spent an attempt, and that is the only version of this
-- counter a crash cannot reset — three failures and the row is dead-lettered
-- and visible, rather than retried nightly forever at full GPU price.
WITH picked AS (
  SELECT j.uniqueid
  FROM call_ingest_jobs j
  WHERE j.claim_until IS NULL
    AND j.meta <> '{}'::jsonb
    AND j.status IN ('discovered', 'asr_failed')
    AND j.asr_attempts < %(max_attempts)s
  ORDER BY j.discovered_at
  LIMIT %(limit)s
  FOR UPDATE SKIP LOCKED
)
UPDATE call_ingest_jobs j
   SET status       = 'transcribing',
       claim_token  = %(token)s::uuid,
       claim_until  = now() + (%(lease)s * interval '1 second'),
       claimed_at   = now(),
       asr_attempts = j.asr_attempts + 1,
       asr_run_id   = %(run_id)s,
       updated_at   = now()
  FROM picked p
 WHERE j.uniqueid = p.uniqueid
RETURNING j.uniqueid, j.filename, j.audio_uri, j.meta;
"""

STORE_SQL = """
-- Lifted VERBATIM from workflow 02's "Store call + transcript" node, which
-- was reviewed four times and whose lease fence is the reason two writers
-- cannot overwrite each other. Copying it is deliberate: a second, simpler
-- version of this statement is a second set of rules to keep in step, and the
-- namespace alone ('asterisk_drive') decides whether a call is one row or two
-- half-filled ones.
--
-- ONE DIFFERENCE, AT THE END. n8n renews the lease here because its next node
-- is the judge. Modal is finished, so it sets status='transcribed' and RELEASES
-- the lease — that is the whole handoff protocol between the two systems.
WITH lease AS MATERIALIZED (
  SELECT j.uniqueid, j.claim_token
  FROM call_ingest_jobs j
  WHERE j.uniqueid    = %(uniqueid)s
    AND j.claim_token = %(token)s::uuid
    AND j.status IN ('transcribing')
    AND j.claim_until > now()
  FOR UPDATE
),
r AS (SELECT %(meta)s::jsonb AS meta, %(tr)s::jsonb AS tr FROM lease),
ins AS (
  INSERT INTO interactions (
    external_source, external_id, channel, started_at, duration_seconds,
    customer_phone_raw, customer_phone_e164, agent_id, handled_by
  )
  SELECT 'asterisk_drive', meta->>'uniqueid', 'phone_call'::channel,
         (meta->>'started_at')::timestamptz,
         round((tr->>'duration_seconds')::numeric)::int,
         meta->>'customer_phone_raw', meta->>'customer_phone_e164',
         -- 'q' recordings carry the QUEUE extension (3009), not a person.
         -- Attributing them to an agent row makes every scorecard wrong.
         CASE WHEN meta->>'kind' = 'q' THEN NULL
              ELSE (SELECT agent_id FROM agents WHERE phone_extension = meta->>'agent_extension') END,
         'agent'::speaker_role
  FROM r
  ON CONFLICT (external_source, external_id) DO UPDATE SET updated_at = now()
  RETURNING interaction_id
),
stored AS (
  INSERT INTO transcripts (
    interaction_id, audio_uri, duration_seconds, sample_rate_hz, channels,
    asr_provider, asr_model_version, asr_confidence, language,
    full_text, segments, diarization, asr_metrics
  )
  SELECT ins.interaction_id, r.meta->>'audio_uri',
         (r.tr->>'duration_seconds')::numeric,
         (r.tr->>'sample_rate_hz')::int, (r.tr->>'channels')::int,
         r.tr->>'provider', r.tr->>'model_version',
         (r.tr->>'confidence')::numeric, 'ar',
         r.tr->>'full_text', coalesce(r.tr->'segments', '[]'::jsonb),
         r.tr->>'diarization', coalesce(r.tr->'asr_metrics', '{}'::jsonb)
  FROM ins, r
  -- A re-transcription replaces EVERY value that came out of ASR, not just the
  -- text. Leaving asr_provider / asr_model_version / duration behind next to
  -- new segments produces a row that says it was produced by a run that did
  -- not produce it, which is the version of this bug that survives review.
  ON CONFLICT (interaction_id) DO UPDATE SET
    audio_uri         = EXCLUDED.audio_uri,
    duration_seconds  = EXCLUDED.duration_seconds,
    sample_rate_hz    = EXCLUDED.sample_rate_hz,
    channels          = EXCLUDED.channels,
    asr_provider      = EXCLUDED.asr_provider,
    asr_model_version = EXCLUDED.asr_model_version,
    asr_confidence    = EXCLUDED.asr_confidence,
    language          = EXCLUDED.language,
    full_text         = EXCLUDED.full_text,
    segments          = EXCLUDED.segments,
    diarization       = EXCLUDED.diarization,
    asr_metrics       = EXCLUDED.asr_metrics,
    transcribed_at    = now()
  RETURNING interaction_id
)
UPDATE call_ingest_jobs j
SET interaction_id = stored.interaction_id,
    status         = 'transcribed',
    claim_token    = NULL,
    claim_until    = NULL,
    claimed_at     = NULL,
    last_error     = NULL,
    updated_at     = now()
FROM stored, lease
WHERE j.uniqueid    = lease.uniqueid
  AND j.claim_token = lease.claim_token
RETURNING j.uniqueid, j.interaction_id, j.status;
"""

FAIL_SQL = """
-- Retryable up to the attempt ceiling, then dead-lettered and visible.
UPDATE call_ingest_jobs
   SET status = CASE WHEN asr_attempts >= %(max_attempts)s
                     THEN 'dead_letter' ELSE 'asr_failed' END,
       claim_token = NULL, claim_until = NULL, claimed_at = NULL,
       last_error = left(%(error)s, 500), updated_at = now()
 WHERE uniqueid = %(uniqueid)s AND claim_token = %(token)s::uuid
RETURNING uniqueid, status;
"""


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
    token = str(uuid.uuid4())
    started = time.monotonic()
    conn = _connect()

    conn.execute(
        "INSERT INTO asr_runs (run_id, gpu, model_version, status) "
        "VALUES (%s, %s, %s, 'running')", (run_id, GPU, MODEL_VERSION))

    rows = conn.execute(CLAIM_SQL, {
        "limit": limit, "max_attempts": max_attempts, "token": token,
        "lease": LEASE_SECONDS, "run_id": run_id}).fetchall()
    conn.execute("UPDATE asr_runs SET claimed = %s WHERE run_id = %s",
                 (len(rows), run_id))
    print(f"[{run_id}] claimed {len(rows)} recordings")

    if dry_run or not rows:
        conn.execute(
            "UPDATE asr_runs SET status='succeeded', finished_at=now() WHERE run_id=%s",
            (run_id,))
        # A dry run must not keep the rows it looked at.
        if dry_run and rows:
            conn.execute(
                "UPDATE call_ingest_jobs SET status='discovered', claim_token=NULL,"
                " claim_until=NULL, claimed_at=NULL, asr_attempts=asr_attempts-1"
                " WHERE claim_token = %s::uuid", (token,))
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
            conn.execute(STORE_SQL, {
                "meta": json.dumps(meta or {}, ensure_ascii=False),
                "tr": json.dumps(transcript, ensure_ascii=False),
                "uniqueid": uniqueid, "token": token,
            })
            processed += 1
            print(f"[{run_id}] {uniqueid}: {duration:.0f}s, {len(texts)} chunks, "
                  f"confidence {confidence}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            conn.execute(FAIL_SQL, {"uniqueid": uniqueid, "token": token,
                                    "max_attempts": max_attempts,
                                    "error": f"{type(exc).__name__}: {exc}"})
            print(f"[{run_id}] {uniqueid} FAILED: {exc}")
        finally:
            try:
                os.remove(local)
            except OSError:
                pass

    gpu_seconds = time.monotonic() - started
    conn.execute(
        "UPDATE asr_runs SET status=%s, finished_at=now(), processed=%s, failed=%s,"
        " audio_seconds=%s, gpu_seconds=%s, est_cost_usd=%s WHERE run_id=%s",
        ("succeeded" if not failed else "partial", processed, failed,
         round(audio_seconds, 1), round(gpu_seconds, 1),
         round(gpu_seconds / 3600 * GPU_HOURLY_USD, 4), run_id))

    rtfx = audio_seconds / gpu_seconds if gpu_seconds else 0
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
