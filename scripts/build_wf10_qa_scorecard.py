"""Generate n8n/workflows/10-chat-qa-scorecard.json. Edit the SQL here, never the JSON.

    python scripts/build_wf10_qa_scorecard.py
    python scripts/check_workflow_json.py n8n/workflows/10-chat-qa-scorecard.json

Every 10 minutes, 23:00-03:59 Riyadh (gotcha 14: off-peak, and finished before
04:00): check the DeepSeek balance, ask v_qa_gate whether the QA scorecard may
spend, claim a few due chats with a fenced token, grade them ONE AT A TIME
through the worker (POST /qa/evaluate), and store each answer plus its three
model_calls rows. n8n writes; the worker only reads and asks (rule 11).
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "n8n" / "workflows" / "10-chat-qa-scorecard.json"
PG = {"postgres": {"id": "railway-pg", "name": "railway-pg"}}
KEY_HEADER = {"parameters": [{"name": "X-API-Key", "value": "={{ $env.WORKER_API_KEY }}"}]}

SQL_RECORD_STATUS = """-- lease-exempt: pipeline-wide state, not per-job work; the same single write
-- path into provider_status that 01d uses (the worker probes, n8n writes).
INSERT INTO provider_status
  (provider, checked_at, available, reason, balance_usd, spend_mtd_usd, raw)
SELECT e.key, now(),
       (e.value->>'available')::boolean,
       e.value->>'reason',
       nullif(e.value->>'balance_usd', '')::numeric,
       nullif(e.value->>'spend_mtd_usd', '')::numeric,
       coalesce(e.value->'raw', '{}'::jsonb)
  FROM jsonb_each($1::jsonb) AS e
ON CONFLICT (provider) DO UPDATE
   SET checked_at = EXCLUDED.checked_at, available = EXCLUDED.available,
       reason = EXCLUDED.reason, balance_usd = EXCLUDED.balance_usd,
       spend_mtd_usd = EXCLUDED.spend_mtd_usd, raw = EXCLUDED.raw
RETURNING provider, available;"""

SQL_GATE = """-- multi-item: a plain read of one row.
-- A SEPARATE statement so it sees the write above. v_qa_gate (030) holds the
-- policy: qa_config.mode, the monthly cap, a known and available balance, and
-- a probe younger than an hour. It does NOT read provider_budgets.enabled —
-- that switch keeps the old v7 judge (01d) off and must not stop this one.
SELECT mode, may_run, reason, balance_usd FROM v_qa_gate;"""

SQL_BLOCKED = """-- lease-exempt: a run log entry; nothing was claimed.
-- 'skipped', not 'failed': no chat changed state and the queue is exactly as
-- it was, so the next tick picks it up with nothing to backfill.
INSERT INTO job_runs (job_name, status, started_at, finished_at,
                      items_in, items_ok, items_failed, error, detail)
SELECT 'qa_chat', 'skipped'::job_status, now(), now(),
       (SELECT count(*) FROM v_qa_due), 0, 0, $1::text,
       jsonb_build_object('gate', 'v_qa_gate', 'reason', $1::text)
RETURNING run_id;"""

SQL_RETENTION = """-- lease-exempt: housekeeping on rows no run holds.
-- The 90-day purge blanks a conversation's text; the quotes stored in
-- qa_evaluations.items are that same text, so they go too. Score and the 15
-- item scores stay: they are numbers about the agent, not the customer's words.
WITH gone AS (
  UPDATE qa_evaluations q SET items = NULL, updated_at = now()
  FROM interactions i
  WHERE i.interaction_id = q.interaction_id
    AND i.content_purged_at IS NOT NULL AND q.items IS NOT NULL
  RETURNING q.interaction_id)
SELECT count(*)::int AS blanked FROM gone;"""

SQL_CLAIM = """-- fence-exempt: this statement IS the claim. INSERT ... ON CONFLICT DO UPDATE
-- locks the conflicting row itself and evaluates the WHERE below on the locked,
-- current version, so two ticks cannot both take over the same stale row.
-- Claim up to qa_config.batch due chats, oldest first, each with a fresh
-- token. A row that is already claimed is taken over only once its claim is
-- 30 minutes stale (a crashed run); a failed one only after an hour, and only
-- while it has attempts left — the same rules v_qa_due states.
INSERT INTO qa_evaluations (interaction_id, status, claimed_at, claim_token)
SELECT d.interaction_id, 'claimed', now(), gen_random_uuid()
  FROM v_qa_due d
 ORDER BY d.started_at
 LIMIT (SELECT value::int FROM qa_config WHERE key = 'batch')
ON CONFLICT (interaction_id) DO UPDATE
   SET status = 'claimed', claimed_at = now(), claim_token = EXCLUDED.claim_token,
       updated_at = now()
 WHERE (qa_evaluations.status = 'claimed'
        AND qa_evaluations.claimed_at < now() - interval '30 minutes')
    OR (qa_evaluations.status = 'failed' AND qa_evaluations.attempts < 3
        AND qa_evaluations.updated_at < now() - interval '1 hour')
RETURNING interaction_id::text, claim_token::text;"""

SQL_STORE = """-- Store one answer, FENCED on the claim token: an answer whose claim was taken
-- over by a later tick matches nothing and changes nothing.
-- multi-item: one worker answer per item, each paired with its own claim.
-- The outcome is decided by the TYPE of `gradeable` (gotcha 15): an error item
-- from a timeout has no `gradeable` at all and is recorded as a failure with an
-- attempt spent, never read as a grade.
WITH r AS (SELECT $1::jsonb AS j, $2::uuid AS id, $3::uuid AS tok),
o AS (
  SELECT r.*, CASE WHEN jsonb_typeof(j->'gradeable') = 'boolean'
                    AND (j->>'gradeable')::boolean           THEN 'scored'
                   WHEN jsonb_typeof(j->'gradeable') = 'boolean' THEN 'not_gradeable'
                   ELSE 'failed' END AS outcome
  FROM r),
owned AS MATERIALIZED (
  -- The fence, LOCKED: the row is held from here to commit, so a takeover
  -- cannot slip in between this check and the write below.
  SELECT q.interaction_id
  FROM qa_evaluations q, o
  WHERE q.interaction_id = o.id AND q.claim_token = o.tok AND q.status = 'claimed'
  FOR UPDATE OF q)
UPDATE qa_evaluations q SET
  status         = o.outcome,
  agent_id       = coalesce((o.j->>'agent_id')::uuid, q.agent_id),
  prompt_version = o.j->>'prompt_version',
  model          = o.j->>'model',
  runs           = (o.j->>'runs')::smallint,
  score          = (o.j->>'score')::numeric,
  critical       = coalesce(ARRAY(SELECT x::smallint
                                    FROM jsonb_array_elements_text(o.j->'critical') AS x), '{}'),
  items          = CASE WHEN o.outcome = 'scored' THEN o.j->'items' END,
  categories     = CASE WHEN o.outcome = 'scored' THEN o.j->'categories' END,
  cost_usd       = (o.j->>'cost_usd')::numeric,
  reason         = CASE o.outcome WHEN 'not_gradeable' THEN o.j->>'reason'
                                  WHEN 'failed' THEN left(coalesce(o.j->'error'->>'message', o.j::text), 500)
                   END,
  attempts       = q.attempts + CASE WHEN o.outcome = 'failed' THEN 1 ELSE 0 END,
  evaluated_at   = CASE WHEN o.outcome = 'scored' THEN now() END,
  claim_token    = NULL,
  updated_at     = now()
FROM o, owned
WHERE q.interaction_id = owned.interaction_id
RETURNING q.interaction_id::text, q.status;"""

SQL_CALLS = """-- lease-exempt: the money was spent whether or not this claim still owns the
-- chat, and rule 12 says every judge call lands in model_calls; UNIQUE below
-- dedupes a re-store, and the rows describe the calls, not the chat's grade.
-- Rule 12: every judge call lands in model_calls. Three per chat, one per run;
-- UNIQUE (purpose, input_hash, prompt_version) makes a re-store a no-op.
-- multi-item: one worker answer per item.
WITH c AS (SELECT jsonb_array_elements(coalesce($1::jsonb->'calls', '[]'::jsonb)) AS x),
ins AS (
  INSERT INTO model_calls (interaction_id, purpose, provider, model, prompt_version, input_hash,
                           prompt_tokens, output_tokens, cached_tokens, cost_usd, priced_at_peak,
                           latency_ms, succeeded, error)
  SELECT $2::uuid, x->>'purpose', x->>'provider', x->>'model', x->>'prompt_version',
         x->>'input_hash', (x->>'prompt_tokens')::int, (x->>'output_tokens')::int,
         (x->>'cached_tokens')::int, (x->>'cost_usd')::numeric,
         (x->>'priced_at_peak')::boolean, (x->>'latency_ms')::int,
         coalesce((x->>'succeeded')::boolean, true), x->>'error'
    FROM c
  ON CONFLICT (purpose, input_hash, prompt_version) DO NOTHING
  RETURNING call_id)
SELECT count(*)::int AS calls_stored FROM ins;"""

SQL_LOG = """-- lease-exempt: a run log entry written once the loop is done.
INSERT INTO job_runs (job_name, status, started_at, finished_at,
                      items_in, items_ok, items_failed, cost_usd, detail)
SELECT 'qa_chat', 'succeeded'::job_status, now(), now(),
       count(*) FILTER (WHERE updated_at > now() - interval '15 minutes'),
       count(*) FILTER (WHERE status IN ('scored', 'not_gradeable')
                          AND updated_at > now() - interval '15 minutes'),
       count(*) FILTER (WHERE status = 'failed' AND updated_at > now() - interval '15 minutes'),
       sum(cost_usd) FILTER (WHERE updated_at > now() - interval '15 minutes'),
       jsonb_build_object('due_left', (SELECT count(*) FROM v_qa_due))
  FROM qa_evaluations
RETURNING run_id;"""


def pg(name, query, *, replace=None, once=False, always=True, notes=None, x=0, y=0):
    params = {"operation": "executeQuery", "query": query, "options": {}}
    if replace:
        params["options"]["queryReplacement"] = replace
    n = {"parameters": params, "name": name, "type": "n8n-nodes-base.postgres",
         "typeVersion": 2.5, "position": [x, y], "credentials": PG}
    if once:
        n["executeOnce"] = True
    if always:
        n["alwaysOutputData"] = True
    if notes:
        n["notes"] = notes
    return n


def if_node(name, expr, x, y):
    return {"parameters": {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": name.lower().replace(" ", "-").replace("?", ""), "leftValue": expr,
                        "rightValue": "", "operator": {"type": "boolean", "operation": "true",
                                                       "singleValue": True}}],
        "combinator": "and"}, "options": {}},
        "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [x, y]}


nodes = [
    {"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "*/10 23,0-3 * * *"}]}},
     "name": "Every 10 min, 23:00-04:00", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
     "position": [0, 300],
     "notes": "Asia/Riyadh (settings.timezone). 23:00-03:59 local is entirely outside DeepSeek's "
              "peak (01-04 and 06-10 UTC), which halves the bill, and it ends before 04:00."},
    {"parameters": {"method": "POST", "url": "={{ $env.WORKER_URL }}/budget/preflight",
                    "sendHeaders": True, "headerParameters": KEY_HEADER, "sendBody": True,
                    "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ providers: [\"deepseek\"] }) }}",
                    "options": {"timeout": 20000}},
     "name": "Check budget", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [220, 300], "onError": "continueRegularOutput", "alwaysOutputData": True},
    pg("Record provider status", SQL_RECORD_STATUS, once=True,
       replace="={{ [ JSON.stringify($json.providers || {}) ] }}", x=440, y=300),
    pg("Read QA gate", SQL_GATE, once=True, x=660, y=300),
    if_node("May we spend?", "={{ $json.may_run === true }}", 880, 300),
    pg("Log blocked run", SQL_BLOCKED, once=True,
       replace="={{ [ $('Read QA gate').first().json.reason || 'blocked' ] }}", x=1100, y=480),
    pg("Retention", SQL_RETENTION, once=True, x=1100, y=300),
    pg("Claim chats", SQL_CLAIM, once=True, x=1320, y=300),
    if_node("Claimed one?", "={{ typeof $json.claim_token === 'string' && $json.claim_token.length > 0 }}",
            1540, 300),
    {"parameters": {"batchSize": 1, "options": {}}, "name": "Each chat",
     "type": "n8n-nodes-base.splitInBatches", "typeVersion": 3, "position": [1760, 300]},
    {"parameters": {"method": "POST", "url": "={{ $env.WORKER_URL }}/qa/evaluate",
                    "sendHeaders": True, "headerParameters": KEY_HEADER, "sendBody": True,
                    "specifyBody": "json",
                    "jsonBody": "={{ JSON.stringify({ interaction_id: $json.interaction_id }) }}",
                    "options": {"timeout": 240000}},
     "name": "Grade through worker", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [1980, 200], "onError": "continueRegularOutput", "alwaysOutputData": True,
     "notes": "Three model runs in parallel inside the worker, ~10-30 s. onError continues so a "
              "timeout becomes a recorded failure with an attempt spent, never a lost claim."},
    pg("Store result", SQL_STORE,
       replace="={{ [ JSON.stringify($json), $('Each chat').item.json.interaction_id, "
               "$('Each chat').item.json.claim_token ] }}", x=2200, y=200),
    pg("Store model calls", SQL_CALLS,
       replace="={{ [ JSON.stringify($('Grade through worker').item.json), "
               "$('Each chat').item.json.interaction_id ] }}", x=2420, y=200,
       notes="Reads the worker answer by node name: after a Postgres node $json is that node's "
             "own result (gotcha 5)."),
    pg("Log run", SQL_LOG, once=True, x=1980, y=440),
]

connections = {
    "Every 10 min, 23:00-04:00": {"main": [[{"node": "Check budget", "type": "main", "index": 0}]]},
    "Check budget": {"main": [[{"node": "Record provider status", "type": "main", "index": 0}]]},
    "Record provider status": {"main": [[{"node": "Read QA gate", "type": "main", "index": 0}]]},
    "Read QA gate": {"main": [[{"node": "May we spend?", "type": "main", "index": 0}]]},
    "May we spend?": {"main": [[{"node": "Retention", "type": "main", "index": 0}],
                               [{"node": "Log blocked run", "type": "main", "index": 0}]]},
    "Retention": {"main": [[{"node": "Claim chats", "type": "main", "index": 0}]]},
    "Claim chats": {"main": [[{"node": "Claimed one?", "type": "main", "index": 0}]]},
    "Claimed one?": {"main": [[{"node": "Each chat", "type": "main", "index": 0}], []]},
    "Each chat": {"main": [[{"node": "Log run", "type": "main", "index": 0}],
                           [{"node": "Grade through worker", "type": "main", "index": 0}]]},
    "Grade through worker": {"main": [[{"node": "Store result", "type": "main", "index": 0}]]},
    "Store result": {"main": [[{"node": "Store model calls", "type": "main", "index": 0}]]},
    "Store model calls": {"main": [[{"node": "Each chat", "type": "main", "index": 0}]]},
}

for i, n in enumerate(nodes, 1):
    n["id"] = f"wf10-{i:02d}"

workflow = {
    "name": "10 · Chat QA scorecard",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1", "timezone": "Asia/Riyadh",
                 "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all",
                 "saveManualExecutions": True, "executionTimeout": 540},
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("wrote", OUT.relative_to(OUT.parents[2]))
