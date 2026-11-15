"""Generate n8n/workflows/14-call-journeys.json. Edit the SQL here, never the JSON.

    python scripts/build_wf14_call_journeys.py
    python scripts/check_workflow_json.py n8n/workflows/14-call-journeys.json

WHO ANSWERED each inbound call (migration 036). call_log.extension is the first
leg that rang, not the person who picked up; the PBX journey says who did.

Every 15 minutes (offset from workflow 11's quarter hours), while
call_pipeline_config.journey_mode = 'on': claim a few inbound calls whose
journey has not been read, GET each journey from the PBX one per second (only
n8n's static IPs are whitelisted, the same reason workflows 11/12 run here),
ask the worker to reduce them (POST /calls/journeys/reduce: pure, no database),
and store the reduction fenced on the claim token. Free: no model, no paid
compute. The person behind an extension is resolved live in v_call_owner.

The PBX key is read from call_archive_config at run time, the worker key from
n8n's environment; no secret lives in the workflow.
"""
from __future__ import annotations

import json
from pathlib import Path

from build_wf10_qa_scorecard import KEY_HEADER, if_node, pg

OUT = Path(__file__).resolve().parents[1] / "n8n" / "workflows" / "14-call-journeys.json"
JOB = "call_journeys"

SQL_CLAIM = """-- fence-exempt: this statement IS the claim. INSERT ... ON CONFLICT DO UPDATE
-- locks the conflicting row and re-checks the WHERE on its current version, so
-- two ticks cannot both take the same call.
-- Inbound calls long enough to have been answered (the archive's own minimum),
-- settled for 15 minutes so the journey is complete, newest first so the
-- portal fills from today backwards. Nothing at all while journey_mode is off.
WITH cfg AS (
  SELECT max(value) FILTER (WHERE key = 'journey_mode')            AS mode,
         max(value) FILTER (WHERE key = 'journey_batch')::int      AS batch,
         max(value) FILTER (WHERE key = 'since')::date             AS since,
         max(value) FILTER (WHERE key = 'max_attempts')::int       AS max_attempts
  FROM call_pipeline_config
),
arc AS (
  SELECT max(value) FILTER (WHERE key = 'pbx_base_url')                     AS base,
         max(value) FILTER (WHERE key = 'pbx_api_key')                      AS api_key,
         coalesce(max(value) FILTER (WHERE key = 'fetch_min_duration_seconds')::int, 30) AS min_seconds
  FROM call_archive_config
),
cand AS (
  SELECT l.uniqueid
  FROM call_log l
  LEFT JOIN call_journeys j USING (uniqueid)
  CROSS JOIN cfg CROSS JOIN arc
  WHERE cfg.mode = 'on'
    AND l.call_type = 'IN'
    AND l.linkedid IS NOT NULL
    AND l.calldate_local >= cfg.since
    AND l.calldate_utc < now() - interval '15 minutes'
    AND coalesce(l.duration_seconds, 0) >= arc.min_seconds
    AND (j.uniqueid IS NULL
         OR (j.status = 'retry_wait' AND j.next_attempt_at <= now())
         OR (j.status = 'claimed' AND j.claim_until < now() AND j.attempts < cfg.max_attempts))
  ORDER BY l.calldate_local DESC
  LIMIT (SELECT batch FROM cfg)
),
claimed AS (
  INSERT INTO call_journeys (uniqueid, status, attempts, claim_token, claim_until)
  SELECT uniqueid, 'claimed', 1, gen_random_uuid(), now() + interval '10 minutes' FROM cand
  ON CONFLICT (uniqueid) DO UPDATE
     SET status = 'claimed', attempts = call_journeys.attempts + 1,
         claim_token = EXCLUDED.claim_token, claim_until = EXCLUDED.claim_until, updated_at = now()
   WHERE (call_journeys.status = 'retry_wait' AND call_journeys.next_attempt_at <= now())
      OR (call_journeys.status = 'claimed' AND call_journeys.claim_until < now())
  RETURNING uniqueid, claim_token
)
SELECT c.uniqueid, c.claim_token::text AS claim_token, l.linkedid, arc.base, arc.api_key
FROM claimed c JOIN call_log l USING (uniqueid) CROSS JOIN arc;"""

ASSEMBLE_JS = r"""// Pair each PBX answer with the claim it was fetched for. The HTTP node emits
// exactly one item per input item, in order, even on error (onError continues),
// so index i of its output belongs to claim i.
const claims = $('Claim journeys').all().map(i => i.json);
const answers = $input.all();
const calls = claims.map((c, i) => {
  const a = (answers[i] && answers[i].json) || {};
  return { uniqueid: c.uniqueid, status: a.statusCode ?? null, body: a.body ?? null };
});
return [{ json: { calls, claims: claims.map(c => ({ uniqueid: c.uniqueid, claim_token: c.claim_token })) } }];"""

SQL_STORE = """-- Store the reductions, FENCED on the claim token: a late answer for a claim a
-- later tick took over matches nothing. A call the PBX did not answer for comes
-- back as `error` and waits (30 min, doubling) instead of getting a verdict.
-- A call missing from the worker's answer (the worker itself failed) is not
-- touched; its claim expires and the next tick retries it.
WITH r AS (SELECT x FROM jsonb_array_elements(coalesce($1::jsonb->'calls', '[]'::jsonb)) AS x),
t AS (SELECT x->>'uniqueid' AS uid, (x->>'claim_token')::uuid AS tok
      FROM jsonb_array_elements(coalesce($2::jsonb, '[]'::jsonb)) AS x),
cfg AS (SELECT max(value) FILTER (WHERE key = 'max_attempts')::int AS max_attempts FROM call_pipeline_config),
v AS (SELECT t.uid, t.tok, r.x, (r.x ? 'error') AS failed FROM r JOIN t ON t.uid = r.x->>'uniqueid'),
owned AS MATERIALIZED (
  SELECT q.uniqueid FROM call_journeys q JOIN v ON q.uniqueid = v.uid
  WHERE q.claim_token = v.tok AND q.status = 'claimed'
  FOR UPDATE OF q)
UPDATE call_journeys j SET
  status            = CASE WHEN NOT v.failed THEN 'read'
                           WHEN j.attempts >= cfg.max_attempts THEN 'failed'
                           ELSE 'retry_wait' END,
  verdict           = CASE WHEN v.failed THEN j.verdict ELSE v.x->>'verdict' END,
  answer_extension  = CASE WHEN v.failed THEN j.answer_extension ELSE v.x->>'answer_extension' END,
  answer_extensions = CASE WHEN v.failed THEN j.answer_extensions
                           ELSE ARRAY(SELECT jsonb_array_elements_text(coalesce(v.x->'answer_extensions', '[]'::jsonb))) END,
  events            = CASE WHEN v.failed THEN j.events ELSE v.x->'events' END,
  last_error        = CASE WHEN v.failed THEN left(v.x->>'error', 300) END,
  read_at           = CASE WHEN v.failed THEN j.read_at ELSE now() END,
  next_attempt_at   = CASE WHEN v.failed THEN now() + interval '30 minutes' * power(2, least(j.attempts, 4))
                           ELSE j.next_attempt_at END,
  claim_token = NULL, claim_until = NULL, updated_at = now()
FROM v JOIN owned ON owned.uniqueid = v.uid, cfg
WHERE j.uniqueid = v.uid AND j.claim_token = v.tok AND j.status = 'claimed'
RETURNING j.uniqueid, j.status;"""

SQL_LOG = f"""-- lease-exempt: housekeeping and a run log entry.
-- A claim that expired on its last allowed attempt can never be claimed again,
-- so it is closed as failed here instead of sitting 'claimed' forever.
WITH stale AS (
  UPDATE call_journeys SET status = 'failed', claim_token = NULL, claim_until = NULL,
         last_error = 'claim expired on the last attempt', updated_at = now()
   WHERE status = 'claimed' AND claim_until < now()
     AND attempts >= (SELECT max(value)::int FROM call_pipeline_config WHERE key = 'max_attempts')
  RETURNING 1)
INSERT INTO job_runs (job_name, status, started_at, finished_at, items_in, items_ok, items_failed, detail)
SELECT '{JOB}', 'succeeded'::job_status, now(), now(),
       count(*) FILTER (WHERE updated_at > now() - interval '10 minutes'),
       count(*) FILTER (WHERE status = 'read' AND updated_at > now() - interval '10 minutes'),
       count(*) FILTER (WHERE status IN ('retry_wait', 'failed') AND updated_at > now() - interval '10 minutes'),
       jsonb_build_object('closed_stale', (SELECT count(*) FROM stale),
                          'answered', count(*) FILTER (WHERE verdict = 'answered'),
                          'read', count(*) FILTER (WHERE status = 'read'))
  FROM call_journeys
RETURNING run_id;"""

nodes = [
    {"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 7,22,37,52 * * * *"}]}},
     "name": "Every 15 min", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
     "position": [0, 300],
     "notes": "Offset from workflow 11 (:00/:15/:30/:45) so the PBX never sees both at once."},
    pg("Claim journeys", SQL_CLAIM, once=True, x=220, y=300),
    if_node("Claimed one?", "={{ typeof $json.claim_token === 'string' && $json.claim_token.length > 0 }}",
            440, 300),
    {"parameters": {"method": "GET", "url": "={{ $json.base }}/api/call-log/journey",
                    "sendQuery": True, "queryParameters": {"parameters": [
                        {"name": "linkedid", "value": "={{ $json.linkedid }}"}]},
                    "sendHeaders": True, "headerParameters": {"parameters": [
                        {"name": "X-API-Key", "value": "={{ $json.api_key }}"}]},
                    "options": {"allowUnauthorizedCerts": True, "timeout": 20000,
                                "batching": {"batch": {"batchSize": 1, "batchInterval": 1000}},
                                "response": {"response": {"fullResponse": True, "neverError": True,
                                                          "responseFormat": "json"}}}},
     "name": "Read journey from PBX", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [660, 300], "onError": "continueRegularOutput", "alwaysOutputData": True,
     "notes": "One request per second: the vendor asked us to be gentle with the live PBX."},
    {"parameters": {"mode": "runOnceForAllItems", "jsCode": ASSEMBLE_JS},
     "name": "Pair with claims", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [880, 300]},
    {"parameters": {"method": "POST", "url": "={{ $env.WORKER_URL }}/calls/journeys/reduce",
                    "sendHeaders": True, "headerParameters": KEY_HEADER, "sendBody": True,
                    "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ calls: $json.calls }) }}",
                    "options": {"timeout": 30000}},
     "name": "Reduce in worker", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [1100, 300], "onError": "continueRegularOutput", "alwaysOutputData": True,
     "executeOnce": True},
    pg("Store journeys", SQL_STORE, once=True,
       replace="={{ [ JSON.stringify($json), JSON.stringify($('Pair with claims').first().json.claims) ] }}",
       x=1320, y=300),
    pg("Log run", SQL_LOG, once=True, x=1540, y=300),
]

connections = {
    "Every 15 min": {"main": [[{"node": "Claim journeys", "type": "main", "index": 0}]]},
    "Claim journeys": {"main": [[{"node": "Claimed one?", "type": "main", "index": 0}]]},
    "Claimed one?": {"main": [[{"node": "Read journey from PBX", "type": "main", "index": 0}], []]},
    "Read journey from PBX": {"main": [[{"node": "Pair with claims", "type": "main", "index": 0}]]},
    "Pair with claims": {"main": [[{"node": "Reduce in worker", "type": "main", "index": 0}]]},
    "Reduce in worker": {"main": [[{"node": "Store journeys", "type": "main", "index": 0}]]},
    "Store journeys": {"main": [[{"node": "Log run", "type": "main", "index": 0}]]},
}

for i, n in enumerate(nodes, 1):
    n["id"] = f"wf14-{i:02d}"

workflow = {
    "name": "14 · Calls · who answered (PBX journey)",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1", "timezone": "Asia/Riyadh",
                 "saveDataSuccessExecution": "none", "saveDataErrorExecution": "all",
                 "saveManualExecutions": True, "executionTimeout": 300},
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("wrote", OUT.relative_to(OUT.parents[2]))
