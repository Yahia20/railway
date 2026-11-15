"""Generate n8n/workflows/15-call-transcribe.json and 16-call-grade.json.
Edit the SQL here, never the JSON.

    python scripts/build_wf15_wf16_calls.py
    python scripts/check_workflow_json.py n8n/workflows/15-call-transcribe.json n8n/workflows/16-call-grade.json

15 · Calls · transcribe   every 10 min. Ask v_call_audio_gate (audio_mode, the lane's
                          10 USD lifetime cap, the anthropic gate) BEFORE claiming;
                          claim a couple of stored recordings; for each, POST
                          /calls/transcribe (the worker hands calls-audio a short-lived
                          URL, masks the words, asks Haiku which voice is the agent,
                          writes the stereo file) and store the transcript plus the
                          Haiku call's model_calls row. One run at a time: a run still
                          working makes the next tick claim nothing.
16 · Calls · grade        every 10 min, 23:00-03:59 Riyadh (DeepSeek off-peak, gotcha 14).
                          Probe the DeepSeek balance, ask v_call_grade_gate, claim
                          transcribed calls, POST /calls/qa/evaluate, store the grade
                          and every reading in model_calls (rule 12). Also the 90-day
                          purge of call text.

n8n writes; the worker only reads, asks and listens (rule 11).
"""
from __future__ import annotations

import json
from pathlib import Path

from build_wf10_qa_scorecard import KEY_HEADER, SQL_RECORD_STATUS, if_node, pg

ROOT = Path(__file__).resolve().parents[1] / "n8n" / "workflows"

SQL_CALLS = """-- lease-exempt: the money was spent whether or not this claim still owns the
-- call, and rule 12 says every model call lands in model_calls. UNIQUE makes a
-- re-store a no-op. Calls have no interactions row, so interaction_id is NULL.
-- multi-item: one worker answer per item.
WITH c AS (SELECT jsonb_array_elements(coalesce($1::jsonb->'calls', '[]'::jsonb)) AS x),
ins AS (
  INSERT INTO model_calls (interaction_id, purpose, provider, model, prompt_version, input_hash,
                           prompt_tokens, output_tokens, cached_tokens, cost_usd, priced_at_peak,
                           latency_ms, succeeded, error)
  SELECT NULL, x->>'purpose', x->>'provider', x->>'model', x->>'prompt_version',
         x->>'input_hash', (x->>'prompt_tokens')::int, (x->>'output_tokens')::int,
         (x->>'cached_tokens')::int, (x->>'cost_usd')::numeric,
         coalesce((x->>'priced_at_peak')::boolean, false), (x->>'latency_ms')::int,
         coalesce((x->>'succeeded')::boolean, true), x->>'error'
    FROM c
  ON CONFLICT (purpose, input_hash, prompt_version) DO NOTHING
  RETURNING call_id)
SELECT count(*)::int AS calls_stored FROM ins;"""

# ---------------------------------------------------------------- workflow 15

A_GATE = """-- multi-item: a plain read of one row.
SELECT mode, may_run, reason, spent_usd FROM v_call_audio_gate;"""

A_BLOCKED = """-- lease-exempt: a run log entry; nothing was claimed.
INSERT INTO job_runs (job_name, status, started_at, finished_at, items_in, items_ok, items_failed, error, detail)
SELECT 'call_transcribe', 'skipped'::job_status, now(), now(),
       (SELECT count(*) FROM v_call_audio_due), 0, 0, $1::text,
       jsonb_build_object('gate', 'v_call_audio_gate', 'reason', $1::text)
RETURNING run_id;"""

A_CLAIM = """-- fence-exempt: this statement IS the claim. INSERT ... ON CONFLICT DO UPDATE
-- locks the conflicting row and re-checks the WHERE on its current version.
-- Newest calls first, so the portal fills from today backwards. Nothing at all
-- while another run still holds a live claim: calls-audio takes one recording
-- at a time, and two runs would only queue inside it and time out.
WITH busy AS (
  SELECT 1 FROM call_processing_jobs WHERE status = 'claimed' AND claim_until >= now()
),
cand AS (
  SELECT d.uniqueid FROM v_call_audio_due d
  WHERE NOT EXISTS (SELECT 1 FROM busy)
  ORDER BY d.calldate_local DESC
  LIMIT (SELECT value::int FROM call_pipeline_config WHERE key = 'audio_batch')
)
INSERT INTO call_processing_jobs (uniqueid, status, attempts, claim_token, claim_until)
SELECT uniqueid, 'claimed', 1, gen_random_uuid(), now() + interval '20 minutes' FROM cand
ON CONFLICT (uniqueid) DO UPDATE
   SET status = 'claimed', attempts = call_processing_jobs.attempts + 1,
       claim_token = EXCLUDED.claim_token, claim_until = EXCLUDED.claim_until, updated_at = now()
 WHERE (call_processing_jobs.status = 'retry_wait' AND call_processing_jobs.next_attempt_at <= now())
    OR (call_processing_jobs.status = 'claimed' AND call_processing_jobs.claim_until < now())
RETURNING uniqueid, claim_token::text AS claim_token;"""

A_STORE = """-- Store one transcription, FENCED on the claim token.
-- multi-item: one worker answer per item, each paired with its own claim.
-- The outcome is read from the answer's `outcome` TEXT (gotcha 15): a timeout or
-- an error item has none and is recorded as a failure with an attempt spent.
-- compute_seconds ADDS UP across attempts: it is what Railway billed.
WITH r AS (SELECT $1::jsonb AS j, $2::text AS uid, $3::uuid AS tok),
o AS (
  SELECT r.*, CASE WHEN j->>'outcome' IN ('transcribed', 'empty', 'review') THEN j->>'outcome'
                   WHEN j->>'outcome' = 'no_recording' THEN 'failed_final'
                   ELSE 'failed' END AS outcome,
         (SELECT value::int FROM call_pipeline_config WHERE key = 'max_attempts') AS max_attempts
  FROM r),
owned AS MATERIALIZED (
  SELECT q.uniqueid FROM call_processing_jobs q, o
  WHERE q.uniqueid = o.uid AND q.claim_token = o.tok AND q.status = 'claimed'
  FOR UPDATE OF q)
UPDATE call_processing_jobs q SET
  status          = CASE o.outcome WHEN 'failed_final' THEN 'failed'
                                   WHEN 'failed' THEN CASE WHEN q.attempts >= o.max_attempts THEN 'failed' ELSE 'retry_wait' END
                                   ELSE o.outcome END,
  audio_seconds   = coalesce(nullif(o.j->>'audio_seconds', '')::numeric, q.audio_seconds),
  speech_seconds  = coalesce(nullif(o.j->>'speech_seconds', '')::numeric, q.speech_seconds),
  compute_seconds = coalesce(q.compute_seconds, 0) + coalesce(nullif(o.j->>'compute_seconds', '')::numeric, 0),
  stereo_key      = CASE WHEN o.outcome = 'transcribed' THEN o.j->>'stereo_key' ELSE q.stereo_key END,
  agent_voice     = CASE WHEN o.outcome = 'transcribed' THEN o.j->>'agent_voice' ELSE q.agent_voice END,
  transcript      = CASE WHEN o.outcome = 'transcribed' THEN o.j->'transcript' ELSE q.transcript END,
  metrics         = CASE WHEN o.outcome = 'transcribed' THEN o.j->'metrics' ELSE q.metrics END,
  model_versions  = coalesce(o.j->'models', q.model_versions),
  last_error      = CASE WHEN o.outcome LIKE 'failed%' THEN left(coalesce(o.j->>'error', o.j->>'message', o.j::text), 400)
                         WHEN o.outcome = 'review' THEN o.j->>'reason' END,
  next_attempt_at = CASE WHEN o.outcome = 'failed' THEN now() + interval '30 minutes' * power(2, least(q.attempts, 4))
                         ELSE now() END,
  claim_token = NULL, claim_until = NULL, updated_at = now()
FROM o, owned
WHERE q.uniqueid = owned.uniqueid
RETURNING q.uniqueid, q.status;"""

A_LOG = """-- lease-exempt: a run log entry written once the loop is done.
INSERT INTO job_runs (job_name, status, started_at, finished_at, items_in, items_ok, items_failed, detail)
SELECT 'call_transcribe', 'succeeded'::job_status, now(), now(),
       count(*) FILTER (WHERE updated_at > now() - interval '30 minutes'),
       count(*) FILTER (WHERE status IN ('transcribed', 'empty', 'review') AND updated_at > now() - interval '30 minutes'),
       count(*) FILTER (WHERE status IN ('retry_wait', 'failed') AND updated_at > now() - interval '30 minutes'),
       jsonb_build_object('due_left', (SELECT count(*) FROM v_call_audio_due),
                          'spent_usd', (SELECT spent_usd FROM v_call_lane_spend))
  FROM call_processing_jobs
RETURNING run_id;"""

wf15_nodes = [
    {"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 */10 * * * *"}]}},
     "name": "Every 10 min", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 300]},
    pg("Read audio gate", A_GATE, once=True, x=220, y=300),
    if_node("May we spend?", "={{ $json.may_run === true }}", 440, 300),
    pg("Log blocked run", A_BLOCKED, once=True,
       replace="={{ [ $('Read audio gate').first().json.reason || 'blocked' ] }}", x=660, y=480),
    pg("Claim recordings", A_CLAIM, once=True, x=660, y=300),
    if_node("Claimed one?", "={{ typeof $json.claim_token === 'string' && $json.claim_token.length > 0 }}",
            880, 300),
    {"parameters": {"batchSize": 1, "options": {}}, "name": "Each call",
     "type": "n8n-nodes-base.splitInBatches", "typeVersion": 3, "position": [1100, 300]},
    {"parameters": {"method": "POST", "url": "={{ $env.WORKER_URL }}/calls/transcribe",
                    "sendHeaders": True, "headerParameters": KEY_HEADER, "sendBody": True,
                    "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ uniqueid: $json.uniqueid }) }}",
                    "options": {"timeout": 900000}},
     "name": "Transcribe through worker", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [1320, 200], "onError": "continueRegularOutput", "alwaysOutputData": True,
     "notes": "calls-audio may be waking from app sleeping (model load ~1 min) and then listens at "
              "about real time on CPU. onError continues so a timeout becomes a recorded failure."},
    pg("Store transcript", A_STORE,
       replace="={{ [ JSON.stringify($json), $('Each call').item.json.uniqueid, "
               "$('Each call').item.json.claim_token ] }}", x=1540, y=200),
    pg("Store model calls", SQL_CALLS,
       replace="={{ [ JSON.stringify($('Transcribe through worker').item.json) ] }}", x=1760, y=200,
       notes="Reads the worker answer by node name (gotcha 5)."),
    pg("Log run", A_LOG, once=True, x=1320, y=440),
]
wf15_conn = {
    "Every 10 min": {"main": [[{"node": "Read audio gate", "type": "main", "index": 0}]]},
    "Read audio gate": {"main": [[{"node": "May we spend?", "type": "main", "index": 0}]]},
    "May we spend?": {"main": [[{"node": "Claim recordings", "type": "main", "index": 0}],
                               [{"node": "Log blocked run", "type": "main", "index": 0}]]},
    "Claim recordings": {"main": [[{"node": "Claimed one?", "type": "main", "index": 0}]]},
    "Claimed one?": {"main": [[{"node": "Each call", "type": "main", "index": 0}], []]},
    "Each call": {"main": [[{"node": "Log run", "type": "main", "index": 0}],
                           [{"node": "Transcribe through worker", "type": "main", "index": 0}]]},
    "Transcribe through worker": {"main": [[{"node": "Store transcript", "type": "main", "index": 0}]]},
    "Store transcript": {"main": [[{"node": "Store model calls", "type": "main", "index": 0}]]},
    "Store model calls": {"main": [[{"node": "Each call", "type": "main", "index": 0}]]},
}

# ---------------------------------------------------------------- workflow 16

G_GATE = """-- multi-item: a plain read of one row.
-- A SEPARATE statement so it sees the balance just written.
SELECT mode, may_run, reason, balance_usd FROM v_call_grade_gate;"""

G_BLOCKED = """-- lease-exempt: a run log entry; nothing was claimed.
INSERT INTO job_runs (job_name, status, started_at, finished_at, items_in, items_ok, items_failed, error, detail)
SELECT 'call_grade', 'skipped'::job_status, now(), now(),
       (SELECT count(*) FROM v_call_grade_due), 0, 0, $1::text,
       jsonb_build_object('gate', 'v_call_grade_gate', 'reason', $1::text)
RETURNING run_id;"""

G_RETENTION = """-- lease-exempt: housekeeping on rows no run holds.
-- 90 days after the call, the words go: transcript and the model's readings.
-- The score, the items (statuses and segment NUMBERS) and the timings stay:
-- they are facts about the agent, not the customer's words. The stereo file is
-- left for the bucket's own lifecycle rule.
WITH old AS (
  SELECT j.uniqueid FROM call_processing_jobs j JOIN call_log l USING (uniqueid)
  WHERE l.calldate_local < (now() AT TIME ZONE 'Asia/Riyadh') - interval '90 days'
    AND j.content_purged_at IS NULL AND j.status NOT IN ('claimed', 'grading')),
jobs AS (
  UPDATE call_processing_jobs j SET transcript = NULL, content_purged_at = now(), updated_at = now()
  FROM old WHERE j.uniqueid = old.uniqueid RETURNING j.uniqueid),
grades AS (
  UPDATE call_grades g SET answers = NULL,
         critical_errors = (SELECT coalesce(jsonb_agg(e - 'reason'), '[]'::jsonb)
                              FROM jsonb_array_elements(coalesce(g.critical_errors, '[]'::jsonb)) e)
  FROM jobs WHERE g.uniqueid = jobs.uniqueid RETURNING g.uniqueid)
SELECT (SELECT count(*) FROM jobs)::int AS blanked, (SELECT count(*) FROM grades)::int AS grades_blanked;"""

G_CLAIM = """-- Claim up to grade_batch transcribed calls with a fresh token each.
-- Lock first, then update (FOR UPDATE SKIP LOCKED), so two ticks never take the
-- same call.
WITH cand AS (
  SELECT j.uniqueid FROM call_processing_jobs j
  JOIN v_call_grade_due d USING (uniqueid)
  ORDER BY d.calldate_local DESC
  LIMIT (SELECT value::int FROM call_pipeline_config WHERE key = 'grade_batch')
  FOR UPDATE OF j SKIP LOCKED)
UPDATE call_processing_jobs j
   SET status = 'grading', claim_token = gen_random_uuid(),
       claim_until = now() + interval '15 minutes', attempts = j.attempts + 1, updated_at = now()
  FROM cand WHERE j.uniqueid = cand.uniqueid
RETURNING j.uniqueid, j.claim_token::text AS claim_token;"""

G_STORE = """-- Store one grade, FENCED on the claim token.
-- multi-item: one worker answer per item, each paired with its own claim.
-- `gradeable` must be a real boolean true (gotcha 15); anything else is a failed
-- attempt that goes back to `transcribed` after a backoff, or to `failed`.
WITH r AS (SELECT $1::jsonb AS j, $2::text AS uid, $3::uuid AS tok),
o AS (SELECT r.*, (jsonb_typeof(j->'gradeable') = 'boolean' AND (j->>'gradeable')::boolean) AS ok,
             (SELECT value::int FROM call_pipeline_config WHERE key = 'max_attempts') AS max_attempts
      FROM r),
owned AS MATERIALIZED (
  SELECT q.uniqueid FROM call_processing_jobs q, o
  WHERE q.uniqueid = o.uid AND q.claim_token = o.tok AND q.status = 'grading'
  FOR UPDATE OF q),
grade AS (
  INSERT INTO call_grades (uniqueid, score, assessed_weight, reviews_open, items, critical_errors,
                           answers, rubric_version, prompt_version, cost_usd, evaluated_at)
  SELECT o.uid, (o.j->>'score')::numeric, (o.j->>'assessed_weight')::numeric,
         coalesce((o.j->>'reviews_open')::smallint, 0), o.j->'items', o.j->'critical_errors',
         o.j->'answers', o.j->>'rubric_version', o.j->>'prompt_version',
         coalesce((o.j->>'cost_usd')::numeric, 0), now()
  FROM o JOIN owned ON owned.uniqueid = o.uid WHERE o.ok
  ON CONFLICT (uniqueid) DO UPDATE SET
    score = EXCLUDED.score, assessed_weight = EXCLUDED.assessed_weight, reviews_open = EXCLUDED.reviews_open,
    items = EXCLUDED.items, critical_errors = EXCLUDED.critical_errors, answers = EXCLUDED.answers,
    rubric_version = EXCLUDED.rubric_version, prompt_version = EXCLUDED.prompt_version,
    cost_usd = EXCLUDED.cost_usd, evaluated_at = EXCLUDED.evaluated_at
  RETURNING uniqueid)
UPDATE call_processing_jobs q SET
  status = CASE WHEN o.ok THEN 'graded'
                WHEN q.attempts >= o.max_attempts THEN 'failed' ELSE 'transcribed' END,
  last_error = CASE WHEN o.ok THEN NULL ELSE left(coalesce(o.j->>'error', o.j::text), 400) END,
  next_attempt_at = CASE WHEN o.ok THEN q.next_attempt_at
                         ELSE now() + interval '1 hour' * power(2, least(q.attempts, 3)) END,
  claim_token = NULL, claim_until = NULL, updated_at = now()
FROM o, owned
WHERE q.uniqueid = owned.uniqueid
RETURNING q.uniqueid, q.status, (SELECT count(*) FROM grade) AS stored;"""

G_LOG = """-- lease-exempt: a run log entry written once the loop is done.
INSERT INTO job_runs (job_name, status, started_at, finished_at, items_in, items_ok, items_failed, cost_usd, detail)
SELECT 'call_grade', 'succeeded'::job_status, now(), now(),
       count(*) FILTER (WHERE j.updated_at > now() - interval '15 minutes'),
       count(*) FILTER (WHERE j.status = 'graded' AND j.updated_at > now() - interval '15 minutes'),
       count(*) FILTER (WHERE j.status = 'failed' AND j.updated_at > now() - interval '15 minutes'),
       (SELECT sum(cost_usd) FROM call_grades WHERE evaluated_at > now() - interval '15 minutes'),
       jsonb_build_object('due_left', (SELECT count(*) FROM v_call_grade_due),
                          'spent_usd', (SELECT spent_usd FROM v_call_lane_spend))
  FROM call_processing_jobs j
RETURNING run_id;"""

wf16_nodes = [
    {"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "*/10 23,0-3 * * *"}]}},
     "name": "Every 10 min, 23:00-04:00", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
     "position": [0, 300],
     "notes": "Asia/Riyadh (settings.timezone). Outside DeepSeek's peak; ends before 04:00 (gotcha 14)."},
    {"parameters": {"method": "POST", "url": "={{ $env.WORKER_URL }}/budget/preflight",
                    "sendHeaders": True, "headerParameters": KEY_HEADER, "sendBody": True,
                    "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ providers: [\"deepseek\"] }) }}",
                    "options": {"timeout": 20000}},
     "name": "Check budget", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [220, 300], "onError": "continueRegularOutput", "alwaysOutputData": True},
    pg("Record provider status", SQL_RECORD_STATUS, once=True,
       replace="={{ [ JSON.stringify($json.providers || {}) ] }}", x=440, y=300),
    pg("Read grade gate", G_GATE, once=True, x=660, y=300),
    if_node("May we spend?", "={{ $json.may_run === true }}", 880, 300),
    pg("Log blocked run", G_BLOCKED, once=True,
       replace="={{ [ $('Read grade gate').first().json.reason || 'blocked' ] }}", x=1100, y=480),
    pg("Retention", G_RETENTION, once=True, x=1100, y=300),
    pg("Claim calls", G_CLAIM, once=True, x=1320, y=300),
    if_node("Claimed one?", "={{ typeof $json.claim_token === 'string' && $json.claim_token.length > 0 }}",
            1540, 300),
    {"parameters": {"batchSize": 1, "options": {}}, "name": "Each call",
     "type": "n8n-nodes-base.splitInBatches", "typeVersion": 3, "position": [1760, 300]},
    {"parameters": {"method": "POST", "url": "={{ $env.WORKER_URL }}/calls/qa/evaluate",
                    "sendHeaders": True, "headerParameters": KEY_HEADER, "sendBody": True,
                    "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ uniqueid: $json.uniqueid }) }}",
                    "options": {"timeout": 280000}},
     "name": "Grade through worker", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [1980, 200], "onError": "continueRegularOutput", "alwaysOutputData": True},
    pg("Store grade", G_STORE,
       replace="={{ [ JSON.stringify($json), $('Each call').item.json.uniqueid, "
               "$('Each call').item.json.claim_token ] }}", x=2200, y=200),
    pg("Store model calls", SQL_CALLS,
       replace="={{ [ JSON.stringify($('Grade through worker').item.json) ] }}", x=2420, y=200,
       notes="Reads the worker answer by node name (gotcha 5)."),
    pg("Log run", G_LOG, once=True, x=1980, y=440),
]
wf16_conn = {
    "Every 10 min, 23:00-04:00": {"main": [[{"node": "Check budget", "type": "main", "index": 0}]]},
    "Check budget": {"main": [[{"node": "Record provider status", "type": "main", "index": 0}]]},
    "Record provider status": {"main": [[{"node": "Read grade gate", "type": "main", "index": 0}]]},
    "Read grade gate": {"main": [[{"node": "May we spend?", "type": "main", "index": 0}]]},
    "May we spend?": {"main": [[{"node": "Retention", "type": "main", "index": 0}],
                               [{"node": "Log blocked run", "type": "main", "index": 0}]]},
    "Retention": {"main": [[{"node": "Claim calls", "type": "main", "index": 0}]]},
    "Claim calls": {"main": [[{"node": "Claimed one?", "type": "main", "index": 0}]]},
    "Claimed one?": {"main": [[{"node": "Each call", "type": "main", "index": 0}], []]},
    "Each call": {"main": [[{"node": "Log run", "type": "main", "index": 0}],
                           [{"node": "Grade through worker", "type": "main", "index": 0}]]},
    "Grade through worker": {"main": [[{"node": "Store grade", "type": "main", "index": 0}]]},
    "Store grade": {"main": [[{"node": "Store model calls", "type": "main", "index": 0}]]},
    "Store model calls": {"main": [[{"node": "Each call", "type": "main", "index": 0}]]},
}


def build(name: str, nodes: list, conn: dict, prefix: str, timeout: int) -> dict:
    for i, n in enumerate(nodes, 1):
        n["id"] = f"{prefix}-{i:02d}"
    return {"name": name, "nodes": nodes, "connections": conn,
            "settings": {"executionOrder": "v1", "timezone": "Asia/Riyadh",
                         "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all",
                         "saveManualExecutions": True, "executionTimeout": timeout}}


if __name__ == "__main__":
    for fname, wf in (("15-call-transcribe.json", build("15 · Calls · transcribe (two voices)", wf15_nodes, wf15_conn, "wf15", 2400)),
                      ("16-call-grade.json", build("16 · Calls · grade (16-item sheet)", wf16_nodes, wf16_conn, "wf16", 1500))):
        out = ROOT / fname
        out.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print("wrote", out.relative_to(ROOT.parents[1]))
