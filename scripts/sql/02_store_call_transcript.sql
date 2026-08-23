-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Store call + transcript'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ JSON.stringify($('Claim work').item.json.meta), JSON.stringify($('Cohere Arabic ASR').item.json), $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token, 10800 ] }}
-- Lease-fenced -- see the lease CTE below.
--
-- Three durable writes and one lease renewal that must not be able to happen
-- apart, so they are one statement:
--
--   1. upsert the interaction
--   2. upsert the transcript
--   3. link the stored interaction_id onto the job row
--   4. renew the lease, because the judge phase starts next
--
-- WHY THE FENCE. ASR is not deterministic and the model version moves. An
-- execution whose lease expired and whose row was recovered and re-claimed by a
-- newer worker used to be able to overwrite that worker's transcript here and
-- only find out at the terminal UPDATE, which reported "0 rows" to nobody. The
-- lease CTE is the whole guard: `r` selects FROM lease, so with no matching
-- lease row `r` is empty, `ins` is empty, the transcripts INSERT is empty, the
-- final UPDATE matches nothing, and the node returns zero rows. 'Transcript
-- stored?' turns that into a hard stop.
--
-- WHY THE HANDOFF IS HERE. The old 'Link job transcribed' node set
-- status = 'evaluating' and spent a judge attempt before the red/confidence
-- gates had run, so every unusable transcript burned one of the five judge
-- attempts without a judge ever being called. Linking and lease renewal happen
-- here (they are facts about the transcript); the status change and the judge
-- attempt happen in 'Begin judge attempt', after the quality gates.
--
-- ATOMIC AGAINST RECOVERY (round-2 blocker). The fence used to be an UNLOCKED
-- `SELECT 1 FROM call_ingest_jobs WHERE ... claim_token = $token`. That reads a
-- snapshot and then lets go: the recovery sweep could reclaim the row, and a
-- newer worker re-claim it, in the window between that read and the upsert
-- below, and the stale write still committed. The lease CTE now takes a real
-- row lock with FOR UPDATE, and requires the lease to still be LIVE
-- (claim_until > now()). Two orderings, both safe:
--
--   * recovery gets the lock first -> this statement BLOCKS on it, and when it
--     is released Postgres re-evaluates the qualification against the NEW row
--     version (EvalPlanQual). The token is gone, so the CTE yields no row, the
--     dependent write is empty, and the node returns zero rows.
--   * this statement gets the lock first -> recovery BLOCKS until we commit,
--     then re-checks its own `claim_until < now()` predicate against what we
--     wrote.
--
-- Postgres rule that dictates the SHAPE: a data-modifying CTE cannot see the
-- effects of its sibling CTEs, and sub-statements all run on one snapshot. So
-- the lock has to be what the write READS FROM, not a check standing beside it
-- -- every dependent CTE below selects (transitively) from `lease`, and the
-- final statement joins it. AS MATERIALIZED pins that: the CTE is evaluated
-- once, first, and cannot be inlined into the readers.
--
-- Locking order for the whole workflow is documented in docs/PR1A-leases.md
-- section 2 ("Locking order"). Short version: every statement takes
-- call_ingest_jobs FIRST and, apart from the claim and the sweep, takes exactly
-- ONE job row, by primary key; the claim uses SKIP LOCKED so it never waits.
WITH lease AS MATERIALIZED (
  SELECT j.uniqueid, j.claim_token
  FROM call_ingest_jobs j
  WHERE j.uniqueid    = $3
    AND j.claim_token = $4::uuid
    AND j.status IN ('transcribing')
    AND j.claim_until > now()
  FOR UPDATE
),
r AS (SELECT $1::jsonb AS meta, $2::jsonb AS tr FROM lease),
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
    claim_until    = now() + ($5 * interval '1 second'),
    last_error     = NULL,
    updated_at     = now()
FROM stored, lease
WHERE j.uniqueid    = lease.uniqueid
  AND j.claim_token = lease.claim_token
RETURNING j.uniqueid, j.interaction_id, j.status, j.judge_attempts;
