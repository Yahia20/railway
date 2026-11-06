"""Generate n8n/workflows/13-chat-qa-v6.json. Edit the SQL here, never the JSON.

    python scripts/build_wf13_qa_v6.py
    python scripts/check_workflow_json.py n8n/workflows/13-chat-qa-v6.json

Workflow 10's shape and guards, pointed at the v6 scorecard (034): every 10 minutes, 23:00-03:59 Riyadh (gotcha
14), check the DeepSeek balance, ask v_qa6_gate whether v6 may spend, claim a few due chats with a fenced token,
grade them ONE AT A TIME through the worker (POST /qa/v6/evaluate), and store each answer plus its model_calls
rows. n8n writes; the worker only reads and asks (rule 11).

Workflow 10 (v4) stays as it is and keeps its own switch, so switching v6 off is `qa_config.v6_mode = 'off'` or
deactivating this workflow, and v4 is untouched either way.
"""
from __future__ import annotations

import json
from pathlib import Path

from build_wf10_qa_scorecard import KEY_HEADER, SQL_RECORD_STATUS, if_node, pg

OUT = Path(__file__).resolve().parents[1] / "n8n" / "workflows" / "13-chat-qa-v6.json"
JOB = "qa_chat_v6"

SQL_GATE = """-- multi-item: a plain read of one row.
-- A SEPARATE statement so it sees the balance just written. v_qa6_gate (034) is
-- every money rule of v_qa_gate on v6's own switch, qa_config.v6_mode.
SELECT mode, may_run, reason, balance_usd FROM v_qa6_gate;"""

SQL_BLOCKED = f"""-- lease-exempt: a run log entry; nothing was claimed.
-- 'skipped', not 'failed': no chat changed state, so the next tick picks the
-- queue up exactly where it was.
INSERT INTO job_runs (job_name, status, started_at, finished_at,
                      items_in, items_ok, items_failed, error, detail)
SELECT '{JOB}', 'skipped'::job_status, now(), now(),
       (SELECT count(*) FROM v_qa6_grade_due), 0, 0, $1::text,
       jsonb_build_object('gate', 'v_qa6_gate', 'reason', $1::text)
RETURNING run_id;"""

SQL_RETENTION = """-- lease-exempt: housekeeping on rows no run holds.
-- The 90-day purge blanks a conversation's text. The masked transcript, the
-- model answers and the alerts' explanations are that same text, so they go.
-- The score, the checks (statuses and line NUMBERS) and the loss reason stay:
-- they are facts about the agent, not the customer's words.
WITH gone AS (
  UPDATE qa_grades_v6 g
     SET transcript = NULL, answers = NULL,
         result = (g.result - 'alerts') || '{"purged": true}'::jsonb,
         updated_at = now()
  FROM interactions i
  WHERE i.interaction_id = g.interaction_id
    AND i.content_purged_at IS NOT NULL
    AND g.result IS NOT NULL AND NOT g.result ? 'purged'
  RETURNING g.interaction_id)
SELECT count(*)::int AS blanked FROM gone;"""

SQL_CLAIM = """-- fence-exempt: this statement IS the claim. INSERT ... ON CONFLICT DO UPDATE
-- locks the conflicting row and evaluates the WHERE below on its current
-- version, so two ticks cannot both take the same chat.
-- Up to qa_config.v6_batch due chats, oldest first, each with a fresh token.
-- The deal's stage is stamped AT CLAIM: a chat answered "not gradeable" must
-- still record which stage it was judged at, or the queue would offer it again
-- every tick.
INSERT INTO qa_grades_v6 (interaction_id, status, claimed_at, claim_token, deal_stage)
SELECT d.interaction_id, 'claimed', now(), gen_random_uuid(), d.deal_stage
  FROM v_qa6_grade_due d
 ORDER BY d.started_at
 LIMIT (SELECT value::int FROM qa_config WHERE key = 'v6_batch')
ON CONFLICT (interaction_id) DO UPDATE
   SET status = 'claimed', claimed_at = now(), claim_token = EXCLUDED.claim_token,
       deal_stage = EXCLUDED.deal_stage, updated_at = now()
 WHERE (qa_grades_v6.status = 'claimed'
        AND qa_grades_v6.claimed_at < now() - interval '30 minutes')
    OR (qa_grades_v6.status = 'failed' AND qa_grades_v6.attempts < 3
        AND qa_grades_v6.updated_at < now() - interval '1 hour')
    OR qa_grades_v6.status IN ('scored', 'not_gradeable')
RETURNING interaction_id::text, claim_token::text;"""

SQL_STORE = """-- Store one answer, FENCED on the claim token: an answer whose claim was taken
-- over by a later tick matches nothing and changes nothing.
-- multi-item: one worker answer per item, each paired with its own claim.
-- The outcome is decided by the TYPE of `gradeable` (gotcha 15): a timeout or a
-- failed grade has no `gradeable` at all and is recorded as a failure with an
-- attempt spent, never read as a grade.
WITH r AS (SELECT $1::jsonb AS j, $2::uuid AS id, $3::uuid AS tok),
o AS (
  SELECT r.*, CASE WHEN jsonb_typeof(j->'gradeable') = 'boolean'
                    AND (j->>'gradeable')::boolean           THEN 'scored'
                   WHEN jsonb_typeof(j->'gradeable') = 'boolean' THEN 'not_gradeable'
                   ELSE 'failed' END AS outcome
  FROM r),
owned AS MATERIALIZED (
  SELECT q.interaction_id
  FROM qa_grades_v6 q, o
  WHERE q.interaction_id = o.id AND q.claim_token = o.tok AND q.status = 'claimed'
  FOR UPDATE OF q),
s AS (SELECT o.*, (o.outcome = 'scored') AS ok FROM o)
UPDATE qa_grades_v6 q SET
  status                = s.outcome,
  agent_id              = CASE WHEN s.ok THEN (s.j->>'agent_id')::uuid ELSE q.agent_id END,
  employee              = CASE WHEN s.ok THEN s.j->>'employee' ELSE q.employee END,
  attribution           = CASE WHEN s.ok THEN s.j->>'attribution' END,
  deal_stage            = coalesce(CASE WHEN s.ok THEN s.j->>'deal_stage' END, q.deal_stage),
  outcome               = CASE WHEN s.ok THEN s.j->>'outcome' END,
  prompt_version        = CASE WHEN s.ok THEN s.j->>'prompt_version' END,
  alerts_prompt_version = CASE WHEN s.ok THEN s.j->>'alerts_prompt_version' END,
  engine_version        = CASE WHEN s.ok THEN s.j->>'engine_version' END,
  model                 = CASE WHEN s.ok THEN s.j->>'model' END,
  votes                 = CASE WHEN s.ok THEN (s.j->>'votes')::smallint END,
  score                 = CASE WHEN s.ok THEN (s.j->>'score')::smallint END,
  earned                = CASE WHEN s.ok THEN (s.j->>'earned')::numeric END,
  total                 = CASE WHEN s.ok THEN (s.j->>'total')::numeric END,
  chat_type             = CASE WHEN s.ok THEN s.j->>'chat_type' END,
  title                 = CASE WHEN s.ok THEN s.j->>'title' END,
  loss_reason           = CASE WHEN s.ok THEN s.j->>'loss_reason' END,
  reviews_open          = CASE WHEN s.ok THEN (s.j->>'reviews_open')::smallint END,
  result                = CASE WHEN s.ok THEN s.j->'result' END,
  transcript            = CASE WHEN s.ok THEN s.j->'transcript' END,
  answers               = CASE WHEN s.ok THEN s.j->'answers' END,
  observed_at           = CASE WHEN s.ok THEN (s.j->>'observed_at')::timestamp END,
  cost_usd              = (s.j->>'cost_usd')::numeric,
  reason                = CASE s.outcome WHEN 'not_gradeable' THEN s.j->>'reason'
                                         WHEN 'failed' THEN left(coalesce(s.j->'error'->>'message', s.j::text), 500)
                          END,
  attempts              = CASE WHEN s.outcome = 'failed' THEN q.attempts + 1 ELSE 0 END,
  evaluated_at          = CASE WHEN s.outcome = 'failed' THEN q.evaluated_at ELSE now() END,
  claim_token           = NULL,
  updated_at            = now()
FROM s, owned
WHERE q.interaction_id = owned.interaction_id
RETURNING q.interaction_id::text, q.status;"""

SQL_CALLS = """-- lease-exempt: the money was spent whether or not this claim still owns the
-- chat, and rule 12 says every judge call lands in model_calls. A failed grade
-- carries the calls that did come back, so they are stored too; UNIQUE below
-- makes a re-store a no-op.
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

SQL_LOG = f"""-- lease-exempt: a run log entry written once the loop is done.
INSERT INTO job_runs (job_name, status, started_at, finished_at,
                      items_in, items_ok, items_failed, cost_usd, detail)
SELECT '{JOB}', 'succeeded'::job_status, now(), now(),
       count(*) FILTER (WHERE updated_at > now() - interval '15 minutes'),
       count(*) FILTER (WHERE status IN ('scored', 'not_gradeable')
                          AND updated_at > now() - interval '15 minutes'),
       count(*) FILTER (WHERE status = 'failed' AND updated_at > now() - interval '15 minutes'),
       sum(cost_usd) FILTER (WHERE updated_at > now() - interval '15 minutes'),
       jsonb_build_object('due_left', (SELECT count(*) FROM v_qa6_grade_due))
  FROM qa_grades_v6
RETURNING run_id;"""

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
    pg("Read v6 gate", SQL_GATE, once=True, x=660, y=300),
    if_node("May we spend?", "={{ $json.may_run === true }}", 880, 300),
    pg("Log blocked run", SQL_BLOCKED, once=True,
       replace="={{ [ $('Read v6 gate').first().json.reason || 'blocked' ] }}", x=1100, y=480),
    pg("Retention", SQL_RETENTION, once=True, x=1100, y=300),
    pg("Claim chats", SQL_CLAIM, once=True, x=1320, y=300),
    if_node("Claimed one?", "={{ typeof $json.claim_token === 'string' && $json.claim_token.length > 0 }}",
            1540, 300),
    {"parameters": {"batchSize": 1, "options": {}}, "name": "Each chat",
     "type": "n8n-nodes-base.splitInBatches", "typeVersion": 3, "position": [1760, 300]},
    {"parameters": {"method": "POST", "url": "={{ $env.WORKER_URL }}/qa/v6/evaluate",
                    "sendHeaders": True, "headerParameters": KEY_HEADER, "sendBody": True,
                    "specifyBody": "json",
                    "jsonBody": "={{ JSON.stringify({ interaction_id: $json.interaction_id }) }}",
                    "options": {"timeout": 280000}},
     "name": "Grade through worker", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [1980, 200], "onError": "continueRegularOutput", "alwaysOutputData": True,
     "notes": "Three DeepSeek readings and the alerts call in parallel inside the worker: ~60-90 s. "
              "onError continues so a timeout becomes a recorded failure with an attempt spent, never a lost claim."},
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
    "Record provider status": {"main": [[{"node": "Read v6 gate", "type": "main", "index": 0}]]},
    "Read v6 gate": {"main": [[{"node": "May we spend?", "type": "main", "index": 0}]]},
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
    n["id"] = f"wf13-{i:02d}"

workflow = {
    "name": "13 · Chat QA scorecard v6",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1", "timezone": "Asia/Riyadh",
                 "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all",
                 "saveManualExecutions": True, "executionTimeout": 540},
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("wrote", OUT.relative_to(OUT.parents[2]))
