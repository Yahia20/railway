"""The ASR batch's database side, moved out of Modal and into the worker.

WHY IT MOVED. `modal/transcribe_job.py` connected to Postgres directly. Modal
runs outside Railway and `postgres.railway.internal` is Railway's private
network, so it never resolved — `[Errno -2] Name or service not known` on the
very first call. The two ways out were opening the database to the public
internet, or having Modal talk to the worker like everything else does. This is
the second one: nothing new is exposed, the API key is the only credential that
leaves Modal, and the database stays private.

THE SQL IS MOVED, NOT REWRITTEN. Every statement below is byte-for-byte what
Modal ran, which is itself lifted from workflow 02's audited nodes. A second,
tidier version of a lease fence is a second set of rules to keep in step, and
the fence is the only thing stopping two systems paying to transcribe the same
call — gotcha 13.

THE BOUNDARY IS UNCHANGED. This claims only `discovered` and `asr_failed` and
leaves rows `transcribed`; workflow 02 claims `transcribed` and `judge_failed`.
Widen either side and the same call is transcribed twice and paid for twice.
"""
from __future__ import annotations

import uuid

from . import db

# Long enough that a slow batch does not have its own rows reclaimed underneath
# it, short enough that a crashed run frees them the same night. Matches the
# value the Modal job used when it held the connection itself.
LEASE_SECONDS = 6 * 3600


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

# A dry run must not keep the rows it looked at, and must give back the attempt
# it spent — otherwise `--dry-run` slowly dead-letters the backlog it was meant
# to inspect safely.
RELEASE_SQL = """
UPDATE call_ingest_jobs
   SET status = 'discovered', claim_token = NULL, claim_until = NULL,
       claimed_at = NULL, asr_attempts = GREATEST(asr_attempts - 1, 0),
       updated_at = now()
 WHERE claim_token = %(token)s::uuid
RETURNING uniqueid, status;
"""


def start_run(run_id: str, gpu: str, model_version: str) -> dict:
    """Open an asr_runs row and mint the lease token for this batch."""
    db.write("INSERT INTO asr_runs (run_id, gpu, model_version, status) "
             "VALUES (%s, %s, %s, 'running') ON CONFLICT (run_id) DO NOTHING",
             (run_id, gpu, model_version))
    return {"run_id": run_id, "claim_token": str(uuid.uuid4())}


def claim(run_id: str, claim_token: str, limit: int, max_attempts: int) -> list[dict]:
    rows = db.write(CLAIM_SQL, {
        "limit": limit, "max_attempts": max_attempts,
        "token": claim_token, "lease": LEASE_SECONDS, "run_id": run_id})
    db.write("UPDATE asr_runs SET claimed = %s WHERE run_id = %s",
             (len(rows), run_id))
    return rows


def store(uniqueid: str, claim_token: str, meta: str, transcript: str) -> list[dict]:
    return db.write(STORE_SQL, {"uniqueid": uniqueid, "token": claim_token,
                                "meta": meta, "tr": transcript})


def fail(uniqueid: str, claim_token: str, error: str, max_attempts: int) -> list[dict]:
    return db.write(FAIL_SQL, {"uniqueid": uniqueid, "token": claim_token,
                               "error": error, "max_attempts": max_attempts})


def release(claim_token: str) -> list[dict]:
    return db.write(RELEASE_SQL, {"token": claim_token})


def finish_run(run_id: str, status: str, processed: int, failed: int,
               audio_seconds: float, gpu_seconds: float,
               est_cost_usd: float | None, error: str | None = None) -> list[dict]:
    """Close the run. `rtfx` is a generated column — it is computed from these
    two numbers, never supplied, which is what makes it a measurement."""
    return db.write(
        "UPDATE asr_runs SET status=%s, finished_at=now(), processed=%s,"
        " failed=%s, audio_seconds=%s, gpu_seconds=%s, est_cost_usd=%s,"
        " error=%s WHERE run_id=%s"
        " RETURNING run_id, status, processed, failed, rtfx, est_cost_usd",
        (status, processed, failed, round(audio_seconds, 1),
         round(gpu_seconds, 1), est_cost_usd, error, run_id))
