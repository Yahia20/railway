-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Register discoveries'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $json.external_id, $json.filename, $json.audio_uri, JSON.stringify($json) ] }}
-- lease-exempt: this is the row's FIRST appearance. There is no lease to
-- carry yet, and the ON CONFLICT clause only refreshes Drive metadata on a
-- row whose meta is still '{}' -- it can never touch status or a lease.
INSERT INTO call_ingest_jobs (uniqueid, filename, audio_uri, meta, status, last_error)
VALUES ($1, $2, $3, $4::jsonb,
        CASE WHEN coalesce(($4::jsonb->>'size_bytes')::bigint, 999999) < 1024
             THEN 'dead_letter' ELSE 'discovered' END,
        CASE WHEN coalesce(($4::jsonb->>'size_bytes')::bigint, 999999) < 1024
             THEN 'audio_too_small: WAV under 1KB is a header with no audio' END)
ON CONFLICT (uniqueid) DO UPDATE
  SET filename  = EXCLUDED.filename,
      audio_uri = EXCLUDED.audio_uri,
      meta      = EXCLUDED.meta,
      updated_at = now()
  WHERE call_ingest_jobs.meta = '{}'::jsonb
RETURNING uniqueid, status, last_error;
