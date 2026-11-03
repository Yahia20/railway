"""Build the n8n SDK sources for workflows 11 and 12 (call recordings archive).

    python scripts/build_wf11_wf12_calls_archive.py OUT_DIR

Writes wf11.js and wf12.js (n8n Workflow SDK code, for the n8n MCP
`create_workflow_from_code`) and the SQL each Postgres node runs, so the SQL
can be read and reviewed as SQL. The SQL is embedded with json.dumps, never
hand-escaped.

  11 · Calls · index call log      every 15 min (+23:59): read the PBX call log
                                   for the last `index_window_minutes`, keep
                                   IN/OUT calls, upsert call_log, queue a
                                   download for each call with a recording.
  12 · Calls · download recordings every 2 min, inside the night window only:
                                   claim one job, download the WAV from the PBX
                                   (only n8n's static IPs are whitelisted), POST
                                   it to the worker which stores it in the
                                   call-recordings bucket, record the outcome.

Both do nothing while call_archive_config.mode = 'off'. No secret lives in the
workflow: the PBX key is read from call_archive_config at run time, the worker
key from n8n's environment.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

J = json.dumps
PG = "{ postgres: { id: 'nIS9u5rp51hgKccW', name: 'railway-pg (api)' } }"

READ_CONFIG = """-- Workflow 11 gate: nothing is read from the PBX unless mode = 'on'.
-- min_calldate is in the PBX's own clock (Riyadh wall time, no zone marker).
SELECT max(value) FILTER (WHERE key = 'pbx_base_url')  AS base,
       max(value) FILTER (WHERE key = 'pbx_api_key')   AS api_key,
       max(value) FILTER (WHERE key = 'index_max_pages')::int AS max_pages,
       to_char((now() AT TIME ZONE 'Asia/Riyadh')
               - make_interval(mins => max(value) FILTER (WHERE key = 'index_window_minutes')::int),
               'YYYY-MM-DD"T"HH24:MI:SS') AS min_calldate
FROM call_archive_config
HAVING max(value) FILTER (WHERE key = 'mode') = 'on';"""

KEEP_JS = r"""// Keep customer calls only (IN / OUT). INTERNAL extension-to-extension calls
// are not stored at all (owner's decision, 2026-11-01). Pages overlap across
// runs on purpose; the last copy of a uniqueid wins and the upsert is idempotent.
const min = $('Read config').first().json.min_calldate;
const seen = new Map();
let pages = 0;
for (const it of $input.all()) {
  pages++;
  const calls = (it.json && Array.isArray(it.json.calls)) ? it.json.calls : [];
  for (const c of calls) {
    if (!c || !c.uniqueid || !c.calldate) continue;
    if (c.call_type !== 'IN' && c.call_type !== 'OUT') continue;
    if (c.calldate < min) continue;
    seen.set(String(c.uniqueid), c);
  }
}
return [{ json: { rows: Array.from(seen.values()), pages, min_calldate: min } }];"""

UPSERT = """-- Upsert the customer calls of this run, and queue a download for every call
-- that has a recording file. Idempotent: re-seeing a call refreshes its row
-- and never queues it twice.
--
-- ONE BAD ROW MUST NOT SINK THE BATCH. Every cast below is guarded: a field
-- the PBX sends empty, negative or in an odd shape becomes NULL instead of
-- aborting the statement (which would lose every good call of the run too).
-- A row whose calldate cannot be read at all cannot be kept (calldate_local
-- is NOT NULL) and is counted in `skipped`. A call whose uniqueid the worker
-- would refuse as an object key is indexed but never queued, so it cannot
-- burn retries against a 422.
WITH inp AS (
  SELECT r FROM jsonb_array_elements($1::jsonb) AS r
),
src AS (
  SELECT r FROM inp
  WHERE r->>'uniqueid' IS NOT NULL
    AND r->>'call_type' IN ('IN', 'OUT')
    AND r->>'calldate' ~ '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])[T ]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$'
),
up AS (
  INSERT INTO call_log (uniqueid, linkedid, calldate_local, calldate_utc, call_type, route, app,
                        src, dst, phone_number, customer_name, extension, agent_name,
                        duration_seconds, talk_seconds, disposition, status, canonical_status,
                        ended_by, ended_by_agent, call_journey_count, is_supervision,
                        recording_file, recording_path, raw)
  SELECT r->>'uniqueid', r->>'linkedid',
         (r->>'calldate')::timestamp,
         (r->>'calldate')::timestamp AT TIME ZONE 'Asia/Riyadh',
         r->>'call_type', r->>'route', r->>'app', r->>'src', r->>'dst',
         r->>'phone_number', r->>'customer_name', r->>'extension', r->>'agent_name',
         CASE WHEN r->>'duration' ~ '^[0-9]{1,9}$' THEN (r->>'duration')::int END,
         CASE WHEN r->>'talk'     ~ '^[0-9]{1,9}$' THEN (r->>'talk')::int END,
         r->>'disposition', r->>'status', r->>'canonical_status',
         r->>'ended_by', r->>'ended_by_agent',
         CASE WHEN r->>'call_journey_count' ~ '^[0-9]{1,9}$' THEN (r->>'call_journey_count')::int END,
         CASE WHEN jsonb_typeof(r->'is_supervision') = 'boolean' THEN (r->'is_supervision')::boolean END,
         NULLIF(r->>'recording_file', ''), NULLIF(r->>'recording_path', ''), r
  FROM src
  ON CONFLICT (uniqueid) DO UPDATE SET
    last_seen_at     = now(),
    raw              = EXCLUDED.raw,
    extension        = EXCLUDED.extension,
    agent_name       = EXCLUDED.agent_name,
    duration_seconds = EXCLUDED.duration_seconds,
    talk_seconds     = EXCLUDED.talk_seconds,
    disposition      = EXCLUDED.disposition,
    status           = EXCLUDED.status,
    canonical_status = EXCLUDED.canonical_status,
    ended_by         = EXCLUDED.ended_by,
    ended_by_agent   = EXCLUDED.ended_by_agent,
    recording_file   = coalesce(EXCLUDED.recording_file, call_log.recording_file),
    recording_path   = coalesce(EXCLUDED.recording_path, call_log.recording_path)
  RETURNING uniqueid, recording_file, duration_seconds, (xmax = 0) AS inserted
),
-- TOO SHORT TO MATTER (owner, 2026-11-03): a call whose whole duration is
-- under fetch_min_duration_seconds cannot hold a conversation worth keeping,
-- so it is indexed but never downloaded. Duration, not `talk`: the PBX's talk
-- counter is wrong for queue calls (it counts the first ringing agent's leg),
-- so 51 of the first 149 recordings had talk < 20 s yet minutes of real audio.
-- A call re-seen later with a longer duration is queued then.
jobs AS (
  INSERT INTO call_recording_jobs (uniqueid)
  SELECT uniqueid FROM up
  WHERE recording_file IS NOT NULL
    AND uniqueid ~ '^[0-9]{6,12}[.][0-9]{1,9}$'   -- the worker's object-key rule
    AND coalesce(duration_seconds, 0) >= coalesce(
          (SELECT value::int FROM call_archive_config WHERE key = 'fetch_min_duration_seconds'), 30)
  ON CONFLICT (uniqueid) DO NOTHING
  RETURNING uniqueid
)
SELECT (SELECT count(*) FROM up)                AS upserted,
       (SELECT count(*) FROM up WHERE inserted) AS new_calls,
       (SELECT count(*) FROM jobs)              AS new_jobs,
       (SELECT count(*) FROM inp) - (SELECT count(*) FROM src) AS skipped,
       (SELECT coalesce(jsonb_agg(r->>'uniqueid'), '[]'::jsonb)
          FROM (SELECT r FROM inp EXCEPT SELECT r FROM src LIMIT 5) x) AS skipped_ids;"""

CLAIM = """-- Workflow 12: claim ONE recording to download, only when mode = 'on', the
-- Riyadh clock is inside the night window, and NO other download is in
-- flight. A run that is still downloading when the next 2-minute tick fires
-- makes that tick claim nothing, so the PBX never serves two of our
-- downloads at once (the owner's "one call at a time").
--
-- A claim whose deadline passed (its execution died) counts as a failed
-- attempt: it waits out the same backoff as any other failure and becomes
-- `failed` at max_attempts, so a job that kills its execution every time
-- cannot loop forever. Sibling CTEs share one snapshot, so a lease expired
-- here is no longer "busy" (busy reads claim_until, not just status) but the
-- job itself is only claimable again after its backoff.
WITH cfg AS (
  SELECT max(value) FILTER (WHERE key = 'mode')                    AS mode,
         max(value) FILTER (WHERE key = 'pbx_base_url')            AS base,
         max(value) FILTER (WHERE key = 'pbx_api_key')             AS api_key,
         max(value) FILTER (WHERE key = 'fetch_window_start')::int AS ws,
         max(value) FILTER (WHERE key = 'fetch_window_end')::int   AS we,
         coalesce(max(value) FILTER (WHERE key = 'max_attempts')::int, 5) AS max_attempts,
         coalesce(max(value) FILTER (WHERE key = 'fetch_min_duration_seconds')::int, 30) AS min_duration
  FROM call_archive_config
),
gate AS (
  SELECT * FROM cfg
  WHERE mode = 'on'
    AND extract(hour FROM now() AT TIME ZONE 'Asia/Riyadh') >= ws
    AND extract(hour FROM now() AT TIME ZONE 'Asia/Riyadh') <  we
),
reclaimed AS (
  UPDATE call_recording_jobs j
     SET status = CASE WHEN j.attempts >= (SELECT max_attempts FROM cfg) THEN 'failed' ELSE 'retry_wait' END,
         claim_token = NULL, claim_until = NULL, claimed_at = NULL,
         last_outcome = 'claim_expired',
         last_error = 'the run that claimed it never reported back before its lease ran out',
         next_attempt_at = now() + interval '10 minutes' * power(2, least(j.attempts, 4))
   WHERE j.status = 'fetching' AND j.claim_until < now()
  RETURNING j.uniqueid
),
busy AS (
  SELECT 1 FROM call_recording_jobs WHERE status = 'fetching' AND claim_until >= now()
),
picked AS (
  SELECT j.uniqueid
  FROM call_recording_jobs j
  WHERE EXISTS (SELECT 1 FROM gate)
    AND NOT EXISTS (SELECT 1 FROM busy)
    AND j.status IN ('pending', 'retry_wait')
    AND j.attempts < (SELECT max_attempts FROM cfg)
    AND j.next_attempt_at <= now()
    AND EXISTS (SELECT 1 FROM call_log l
                WHERE l.uniqueid = j.uniqueid
                  AND coalesce(l.duration_seconds, 0) >= (SELECT min_duration FROM cfg))
  ORDER BY j.next_attempt_at, j.uniqueid
  LIMIT 1
  FOR UPDATE SKIP LOCKED
),
claimed AS (
  UPDATE call_recording_jobs j
     SET status = 'fetching', claim_token = gen_random_uuid(), claimed_at = now(),
         claim_until = now() + interval '5 minutes', attempts = j.attempts + 1
    FROM picked
   WHERE j.uniqueid = picked.uniqueid
  RETURNING j.uniqueid, j.claim_token, j.attempts
)
SELECT c.uniqueid, c.claim_token, c.attempts,
       to_char(l.calldate_local, 'YYYY-MM-DD') AS day,
       g.base, g.api_key,
       (SELECT count(*) FROM reclaimed) AS reclaimed
FROM claimed c
JOIN call_log l USING (uniqueid)
CROSS JOIN gate g;"""

RECORD = """-- Write one download outcome, FENCED: only the claim that is still current
-- may finish the job; a late answer from an expired claim matches nothing.
WITH a AS (SELECT $1::jsonb AS j),
v AS (
  SELECT j->'job'->>'uniqueid'            AS uid,
         (j->'job'->>'claim_token')::uuid AS tok,
         (j->'job'->>'attempts')::int     AS attempts,
         j->'res'                         AS res,
         CASE WHEN j->'res'->>'outcome' IN ('stored', 'not_audio', 'too_large', 'failed')
                THEN j->'res'->>'outcome'
              WHEN j->'res'->>'statusCode' = '404' THEN 'missing'
              ELSE 'no_answer' END        AS outcome,
         (SELECT value::int FROM call_archive_config WHERE key = 'max_attempts') AS max_attempts
  FROM a
)
UPDATE call_recording_jobs t SET
  status = CASE v.outcome
             WHEN 'stored'    THEN 'stored'
             WHEN 'missing'   THEN 'missing'
             WHEN 'not_audio' THEN 'rejected'
             WHEN 'too_large' THEN 'rejected'
             ELSE CASE WHEN v.attempts >= v.max_attempts THEN 'failed' ELSE 'retry_wait' END
           END,
  claim_token = NULL, claim_until = NULL, claimed_at = NULL,
  sha256      = CASE WHEN v.outcome = 'stored' THEN v.res->>'sha256'          ELSE t.sha256 END,
  bytes       = CASE WHEN v.outcome = 'stored' THEN (v.res->>'bytes')::bigint ELSE t.bytes END,
  mime        = CASE WHEN v.outcome = 'stored' THEN v.res->>'mime'            ELSE t.mime END,
  storage_key = CASE WHEN v.outcome = 'stored' THEN v.res->>'storage_key'     ELSE t.storage_key END,
  stored_at   = CASE WHEN v.outcome = 'stored' THEN now()                     ELSE t.stored_at END,
  http_status = CASE WHEN v.outcome IN ('stored', 'not_audio', 'too_large') THEN 200
                     WHEN v.res->>'statusCode' ~ '^[0-9]+$' THEN (v.res->>'statusCode')::int
                     ELSE NULL END,
  last_outcome = v.outcome,
  last_error   = left(coalesce(v.res->>'error', v.res->>'message', v.res->>'detail'), 300),
  next_attempt_at = CASE WHEN v.outcome IN ('failed', 'no_answer')
                         THEN now() + interval '10 minutes' * power(2, least(v.attempts, 4))
                         ELSE t.next_attempt_at END
FROM v
WHERE t.uniqueid = v.uid AND t.claim_token = v.tok AND t.status = 'fetching'
RETURNING t.uniqueid, t.status, t.last_outcome, t.bytes;"""

CLAIM_REF = "$('Claim one').first().json"
READ_REF = "$('Read config').first().json"

WF11 = """import { workflow, node, trigger, ifElse, expr } from '@n8n/workflow-sdk';

const every15 = trigger({
  type: 'n8n-nodes-base.scheduleTrigger',
  version: 1.3,
  config: {
    name: 'Every 15 min + 23:59',
    parameters: { rule: { interval: [
      { field: 'cronExpression', expression: '0 */15 * * * *' },
      { field: 'cronExpression', expression: '0 59 23 * * *' }
    ] } }
  }
});

const readConfig = node({
  type: 'n8n-nodes-base.postgres',
  version: 2.7,
  config: {
    name: 'Read config',
    parameters: { operation: 'executeQuery', query: __READ_CONFIG__ },
    credentials: __PG__
  }
});

const modeOn = ifElse({
  version: 2.3,
  config: {
    name: 'Mode on?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
        conditions: [{ leftValue: expr('{{ $json.min_calldate }}'), operator: { type: 'string', operation: 'notEmpty', singleValue: true }, rightValue: '' }],
        combinator: 'and'
      }
    }
  }
});

const pull = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.4,
  config: {
    name: 'Pull call log',
    parameters: {
      method: 'GET',
      url: expr('{{ $json.base }}/api/call-log'),
      sendQuery: true,
      specifyQuery: 'keypair',
      queryParameters: { parameters: [{ name: 'limit', value: '100' }] },
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [{ name: 'X-API-Key', value: expr('{{ $json.api_key }}') }] },
      options: {
        allowUnauthorizedCerts: true,
        timeout: 30000,
        pagination: { pagination: {
          paginationMode: 'updateAParameterInEachRequest',
          parameters: { parameters: [{ type: 'qs', name: 'cursor', value: expr('{{ $response.body.next_cursor }}') }] },
          paginationCompleteWhen: 'other',
          completeExpression: __COMPLETE__,
          limitPagesFetched: true,
          maxRequests: __MAXREQ__,
          requestInterval: 1000
        } }
      }
    }
  }
});

const keep = node({
  type: 'n8n-nodes-base.code',
  version: 2,
  config: {
    name: 'Keep customer calls',
    parameters: { mode: 'runOnceForAllItems', jsCode: __KEEP_JS__ }
  }
});

const upsert = node({
  type: 'n8n-nodes-base.postgres',
  version: 2.7,
  config: {
    name: 'Upsert calls + queue downloads',
    parameters: {
      operation: 'executeQuery',
      query: __UPSERT__,
      options: { queryReplacement: expr('{{ [ JSON.stringify($json.rows) ] }}') }
    },
    credentials: __PG__
  }
});

export default workflow('calls-11-index', '11 · Calls · index call log')
  .add(every15)
  .to(readConfig)
  .to(modeOn.onTrue(pull.to(keep.to(upsert))));
"""

WF12 = """import { workflow, node, trigger, ifElse, expr } from '@n8n/workflow-sdk';

const every2 = trigger({
  type: 'n8n-nodes-base.scheduleTrigger',
  version: 1.3,
  config: {
    name: 'Every 2 min',
    parameters: { rule: { interval: [{ field: 'cronExpression', expression: '0 */2 * * * *' }] } }
  }
});

const claim = node({
  type: 'n8n-nodes-base.postgres',
  version: 2.7,
  config: {
    name: 'Claim one',
    parameters: { operation: 'executeQuery', query: __CLAIM__ },
    credentials: __PG__
  }
});

const claimedOne = ifElse({
  version: 2.3,
  config: {
    name: 'Claimed one?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
        conditions: [{ leftValue: expr('{{ $json.uniqueid }}'), operator: { type: 'string', operation: 'notEmpty', singleValue: true }, rightValue: '' }],
        combinator: 'and'
      }
    }
  }
});

const download = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.4,
  config: {
    name: 'Download from PBX',
    onError: 'continueRegularOutput',
    parameters: {
      method: 'GET',
      url: expr('{{ $json.base }}/api/call-log/recording/{{ $json.uniqueid }}'),
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [{ name: 'X-API-Key', value: expr('{{ $json.api_key }}') }] },
      options: {
        allowUnauthorizedCerts: true,
        timeout: 120000,
        response: { response: { fullResponse: true, neverError: true, responseFormat: 'file', outputPropertyName: 'data' } }
      }
    }
  }
});

const gotFile = ifElse({
  version: 2.3,
  config: {
    name: 'Got the file?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
        conditions: [{ leftValue: expr('{{ $json.statusCode }}'), operator: { type: 'number', operation: 'equals' }, rightValue: 200 }],
        combinator: 'and'
      }
    }
  }
});

const storeIt = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.4,
  config: {
    name: 'Store in bucket (worker)',
    onError: 'continueRegularOutput',
    parameters: {
      method: 'POST',
      url: expr('{{ $env.WORKER_URL }}/calls/recording/store'),
      sendQuery: true,
      specifyQuery: 'keypair',
      queryParameters: { parameters: [
        { name: 'uniqueid', value: __UID__ },
        { name: 'day', value: __DAY__ }
      ] },
      sendHeaders: true,
      specifyHeaders: 'keypair',
      headerParameters: { parameters: [{ name: 'X-API-Key', value: expr('{{ $env.WORKER_API_KEY }}') }] },
      sendBody: true,
      contentType: 'binaryData',
      inputDataFieldName: 'data',
      options: { timeout: 120000, response: { response: { neverError: true, responseFormat: 'json' } } }
    }
  }
});

const record = node({
  type: 'n8n-nodes-base.postgres',
  version: 2.7,
  config: {
    name: 'Record outcome',
    parameters: {
      operation: 'executeQuery',
      query: __RECORD__,
      options: { queryReplacement: __RECORD_PARAM__ }
    },
    credentials: __PG__
  }
});

export default workflow('calls-12-download', '12 · Calls · download recordings')
  .add(every2)
  .to(claim)
  .to(claimedOne.onTrue(download.to(gotFile.onTrue(storeIt.to(record)).onFalse(record))));
"""


def ex(body: str) -> str:
    """An SDK expr('{{ ... }}') call with the body embedded as a JS string."""
    return "expr(" + J("{{ " + body + " }}") + ")"


def build() -> dict[str, str]:
    complete = ex("!$response.body.next_cursor || !$response.body.calls || "
                  "$response.body.calls.length === 0 || "
                  "$response.body.calls[$response.body.calls.length - 1].calldate < "
                  + READ_REF + ".min_calldate")
    wf11 = (WF11.replace("__READ_CONFIG__", J(READ_CONFIG)).replace("__UPSERT__", J(UPSERT))
                .replace("__KEEP_JS__", J(KEEP_JS)).replace("__PG__", PG)
                .replace("__COMPLETE__", complete).replace("__MAXREQ__", ex(READ_REF + ".max_pages")))
    record_param = ex("[ JSON.stringify({ job: { uniqueid: " + CLAIM_REF + ".uniqueid, claim_token: "
                      + CLAIM_REF + ".claim_token, attempts: " + CLAIM_REF + ".attempts }, res: $json }) ]")
    wf12 = (WF12.replace("__CLAIM__", J(CLAIM)).replace("__RECORD__", J(RECORD)).replace("__PG__", PG)
                .replace("__UID__", ex(CLAIM_REF + ".uniqueid")).replace("__DAY__", ex(CLAIM_REF + ".day"))
                .replace("__RECORD_PARAM__", record_param))
    return {"wf11.js": wf11, "wf12.js": wf12, "read_config.sql": READ_CONFIG, "upsert.sql": UPSERT,
            "claim.sql": CLAIM, "record.sql": RECORD}


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for name, text in build().items():
        (out / name).write_text(text, encoding="utf-8")
        print("wrote", out / name, len(text))
