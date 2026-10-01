#!/usr/bin/env python3
"""Generate n8n/workflows/09-chat-media-archive.json.

The workflow is mostly SQL, and SQL inside a JSON string cannot be reviewed.
So it lives here as plain text and the JSON is generated:

    python scripts/build_wf09_media_archive.py

Every run, once a minute, in this order — the order is the design:

  1. Take the run lease. One execution at a time, so the retention delete and
     a fetch of the same bytes can never interleave.
  2. Retention: drop references past the window, purge jobs nothing refers
     to, mark unreferenced objects `deleting`, ask the worker to delete them,
     stamp `deleted`. Runs BEFORE fetching, so a fetch in this run that lands
     on a just-deleted sha256 re-uploads it and re-marks it present.
  3. Discover: examine up to scan_batch unexamined messages, newest first
     (young customer links die first). Record every examined message, with or
     without links, so it is never parsed again.
  4. Claim up to fetch_batch jobs (token + deadline, SKIP LOCKED), youngest
     customer links first; reclaim expired claims.
  5. Fetch each through the worker, one at a time, and record the outcome
     fenced on the claim token.
  6. Release the run lease.

Execution data is NOT saved, success or error: claimed rows carry source URLs,
and bitrix_rest URLs carry a live REST token. The database rows are the record
(see v_media_health).
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "n8n" / "workflows" / "09-chat-media-archive.json"
PG = {"postgres": {"id": "railway-pg", "name": "railway-pg"}}   # stamped by n8n_deploy.py
PARSER_VERSION = "links-v1"

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_LEASE = r"""
-- lease-exempt: this statement IS the run lease; it writes nothing else.
-- Take the run lease, or learn that another run holds it, or that mode is off.
-- The UPDATE re-checks its WHERE after waiting on a concurrent writer, so two
-- executions starting together cannot both win.
WITH cfg AS (
  SELECT max(value) FILTER (WHERE key = 'mode')          AS mode,
         max(value) FILTER (WHERE key = 'scan_batch')    AS scan_batch,
         max(value) FILTER (WHERE key = 'fetch_batch')   AS fetch_batch,
         max(value) FILTER (WHERE key = 'max_attempts')  AS max_attempts,
         max(value) FILTER (WHERE key = 'retention_days') AS retention_days
  FROM media_archive_config
),
lease AS (
  UPDATE media_archive_config c
     SET value = (now() + interval '6 minutes')::text, updated_at = now()
   WHERE c.key = 'run_lease_until'
     AND c.value::timestamptz < now()
     AND (SELECT mode FROM cfg) = 'on'
  RETURNING 1
),
tok AS (
  UPDATE media_archive_config
     SET value = gen_random_uuid()::text, updated_at = now()
   WHERE key = 'run_lease_token' AND EXISTS (SELECT 1 FROM lease)
  RETURNING value
)
SELECT EXISTS (SELECT 1 FROM lease)                 AS acquired,
       (SELECT value FROM tok)                       AS run_token,
       cfg.mode,
       coalesce(cfg.scan_batch, '300')::int          AS scan_batch,
       coalesce(cfg.fetch_batch, '4')::int           AS fetch_batch,
       coalesce(cfg.max_attempts, '8')::int          AS max_attempts,
       coalesce(cfg.retention_days, '90')::int       AS retention_days
FROM cfg;
"""

SQL_RETENTION = r"""
-- lease-exempt: runs only while this execution holds the run lease.
-- fence-exempt: it CLEARS claims of purged jobs and fences nothing. Claims are
-- only made and finished inside the execution that holds the run lease, so a
-- claim still marked here belongs to a dead execution whose answer never comes.
-- Retention. Same window and same anchor as purge_raw_content(): the
-- interaction's started_at. Runs here, not in workflow 04, so media expiry
-- does not depend on the chat purge succeeding (it failed every night from
-- 2026-09-16 until 025).
WITH cfg AS (SELECT $1::int AS days),
expired_refs AS (
  DELETE FROM chat_media cm
   USING chat_messages m, interactions i, cfg
   WHERE m.message_id = cm.message_id
     AND i.interaction_id = m.interaction_id
     AND (i.started_at < now() - make_interval(days => cfg.days)
          OR i.content_purged_at IS NOT NULL)
  RETURNING cm.media_id, cm.job_id
),
orphan_jobs AS (
  -- A job nothing refers to any more. Its URL goes too (it may carry a token).
  -- A claim in flight is fenced on its token and status, so its late answer
  -- simply matches nothing.
  --
  -- Every CTE here reads ONE snapshot: the references deleted just above are
  -- still visible to this NOT EXISTS, so they are excluded by id explicitly.
  UPDATE media_fetch_jobs j
     SET status = 'purged', sha256 = NULL, source_url = NULL,
         claim_token = NULL, claim_until = NULL, claimed_at = NULL
   WHERE j.status <> 'purged'
     AND NOT EXISTS (SELECT 1 FROM chat_media cm
                      WHERE cm.job_id = j.job_id
                        AND cm.media_id NOT IN (SELECT media_id FROM expired_refs))
  RETURNING j.url_hash
),
doomed AS (
  -- An object with no STORED job pointing at it. Jobs purged in this same
  -- statement still hold their old sha256 in this snapshot, so they are
  -- excluded explicitly.
  UPDATE media_objects o
     SET state = 'deleting'
   WHERE o.state = 'present'
     AND NOT EXISTS (
       SELECT 1 FROM media_fetch_jobs j
        WHERE j.sha256 = o.sha256 AND j.status = 'stored'
          AND j.url_hash NOT IN (SELECT url_hash FROM orphan_jobs))
  RETURNING o.sha256
)
SELECT
  (SELECT count(*) FROM expired_refs)::int AS refs_expired,
  -- Receipts still to delete: jobs purged now, plus purged jobs whose receipt
  -- delete was never confirmed (receipt_deleted_at stays NULL until it is).
  -- Bounded to the worker's limit; the rest go next run.
  coalesce((SELECT json_agg(url_hash) FROM (
      SELECT url_hash FROM orphan_jobs
      UNION
      SELECT url_hash FROM media_fetch_jobs
       WHERE status = 'purged' AND receipt_deleted_at IS NULL
      LIMIT 200) r), '[]'::json) AS receipts,
  coalesce((SELECT json_agg(sha256) FROM (
      SELECT sha256 FROM doomed
      UNION
      SELECT sha256 FROM media_objects WHERE state = 'deleting'   -- a previous run's leftovers
      LIMIT 50) d), '[]'::json) AS objects;
"""

SQL_MARK_DELETED = r"""
-- lease-exempt: writes media_objects and receipt stamps only, inside the run
-- lease; no claim is touched.
-- Record what the worker confirmed. Three things, each only on its own `ok`:
WITH r AS (SELECT $1::jsonb AS j),
objects_gone AS (
  -- Only if still 'deleting': an object a fetch re-uploaded in the meantime is
  -- 'present' again and stays so.
  UPDATE media_objects o
     SET state = 'deleted', deleted_at = now()
    FROM r, jsonb_array_elements(r.j->'objects') x
   WHERE o.sha256 = x->>'sha256' AND (x->>'ok')::boolean AND o.state = 'deleting'
  RETURNING o.sha256
),
receipts_gone AS (
  UPDATE media_fetch_jobs f
     SET receipt_deleted_at = now()
    FROM r, jsonb_array_elements(r.j->'receipts') x
   WHERE f.url_hash = x->>'url_hash' AND (x->>'ok')::boolean
     AND f.status = 'purged' AND f.receipt_deleted_at IS NULL
  RETURNING f.url_hash
),
receipted_bytes AS (
  -- Bytes a receipt named but no media_objects row knew: an upload whose
  -- answer never reached the database (round-3 review). Registered as
  -- 'deleting' so the next run removes them — unless a stored job now points
  -- at the same content, in which case they are live and left alone.
  INSERT INTO media_objects (sha256, bytes, mime, storage_key, state)
  SELECT DISTINCT ON (x->>'sha256')
         x->>'sha256', greatest(coalesce((x->>'bytes')::bigint, 1), 1),
         coalesce(x->>'mime', 'application/octet-stream'),
         'sha256/' || left(x->>'sha256', 2) || '/' || (x->>'sha256'), 'deleting'
  FROM r, jsonb_array_elements(r.j->'receipt_objects') x
  WHERE (x->>'sha256') ~ '^[0-9a-f]{64}$'
    AND NOT EXISTS (SELECT 1 FROM media_fetch_jobs f
                     WHERE f.sha256 = x->>'sha256' AND f.status = 'stored')
  ON CONFLICT (sha256) DO UPDATE SET state = 'deleting', deleted_at = NULL
   WHERE media_objects.state = 'deleted'
  RETURNING sha256
)
SELECT (SELECT count(*) FROM objects_gone)::int    AS objects_deleted,
       (SELECT count(*) FROM receipts_gone)::int   AS receipts_deleted,
       (SELECT count(*) FROM receipted_bytes)::int AS orphans_registered;
"""

SQL_DISCOVER = r"""
-- Messages the archive has not examined, newest first: a customer link is
-- worth most in its first minutes. Every row comes back, link or not, because
-- "examined, nothing here" is recorded too.
SELECT m.message_id::text AS message_id, m.body, m.content_type,
       m.created_at AS seen_at
FROM chat_messages m
JOIN interactions i ON i.interaction_id = m.interaction_id
WHERE i.external_source = 'bitrix_chat_api'
  AND i.content_purged_at IS NULL
  AND i.started_at >= now() - make_interval(days => $2::int)
  AND NOT EXISTS (SELECT 1 FROM chat_media_scan s WHERE s.message_id = m.message_id)
ORDER BY m.message_id DESC
LIMIT $1::int;
"""

SQL_PERSIST = r"""
-- lease-exempt: inserts new rows; on an existing job it only lowers
-- first_seen_at or revives a PURGED one, which no claim can hold. Run-leased.
-- Record what discovery found, in one statement. url_hash is computed HERE
-- (pgcrypto) so it is byte-for-byte the sha256 the worker computes in Python.
WITH d AS (SELECT $1::jsonb AS j),
scan_in AS (
  SELECT (s->>'message_id')::bigint AS message_id, (s->>'links_found')::int AS links_found
  FROM d, jsonb_array_elements(d.j->'scans') s
  WHERE EXISTS (SELECT 1 FROM chat_messages m WHERE m.message_id = (s->>'message_id')::bigint)
),
scans AS (
  INSERT INTO chat_media_scan (message_id, links_found, parser)
  SELECT message_id, links_found, $2 FROM scan_in
  ON CONFLICT (message_id) DO NOTHING
  RETURNING message_id
),
refs AS (
  SELECT (r->>'message_id')::bigint AS message_id, (r->>'ordinal')::int AS ordinal,
         r->>'url' AS url, encode(digest(r->>'url', 'sha256'), 'hex') AS url_hash,
         r->>'family' AS family, nullif(r->>'file_name', '') AS file_name,
         nullif(r->>'declared_type', '') AS declared_type,
         (r->>'seen_at')::timestamptz AS seen_at
  FROM d, jsonb_array_elements(d.j->'refs') r
  WHERE (r->>'message_id')::bigint IN (SELECT message_id FROM scans)
),
one_per_url AS (
  -- ON CONFLICT DO UPDATE may not touch one row twice in a statement.
  SELECT DISTINCT ON (url_hash) url_hash, url, family,
         min(seen_at) OVER (PARTITION BY url_hash) AS first_seen_at
  FROM refs ORDER BY url_hash
),
jobs AS (
  INSERT INTO media_fetch_jobs (url_hash, family, source_url, first_seen_at)
  SELECT url_hash, family, url, first_seen_at FROM one_per_url
  ON CONFLICT (url_hash) DO UPDATE SET
    -- earliest observation wins; a second delivery never makes a link look younger
    first_seen_at = LEAST(media_fetch_jobs.first_seen_at, EXCLUDED.first_seen_at),
    -- a URL whose old references all expired and that is sent again is worth fetching again
    status = CASE WHEN media_fetch_jobs.status = 'purged' THEN 'pending' ELSE media_fetch_jobs.status END,
    attempts = CASE WHEN media_fetch_jobs.status = 'purged' THEN 0 ELSE media_fetch_jobs.attempts END,
    next_attempt_at = CASE WHEN media_fetch_jobs.status = 'purged' THEN now() ELSE media_fetch_jobs.next_attempt_at END,
    source_url = CASE WHEN media_fetch_jobs.status = 'purged' THEN EXCLUDED.source_url
                      ELSE media_fetch_jobs.source_url END,
    -- A revived job will write a NEW receipt; the old "receipt deleted" stamp
    -- must not stop that one from being cleaned up when it is purged again.
    receipt_deleted_at = CASE WHEN media_fetch_jobs.status = 'purged' THEN NULL
                              ELSE media_fetch_jobs.receipt_deleted_at END
  RETURNING job_id, url_hash
),
ins AS (
  INSERT INTO chat_media (message_id, ordinal, job_id, declared_type, file_name)
  SELECT r.message_id, r.ordinal, j.job_id, r.declared_type, r.file_name
  FROM refs r JOIN jobs j ON j.url_hash = r.url_hash
  ON CONFLICT (message_id, ordinal) DO NOTHING
  RETURNING media_id
)
SELECT (SELECT count(*) FROM scans)::int AS scanned,
       (SELECT count(*) FROM ins)::int   AS references_added,
       (SELECT count(*) FROM jobs)::int  AS jobs_touched;
"""

SQL_CLAIM = r"""
-- Reclaim claims whose deadline passed (their execution died), then claim a
-- batch. Order: customer links seen in the last 25 minutes first, oldest of
-- those first (earliest deadline); then durable links; then customer links
-- old enough that they are probably dead, last.
WITH reclaimed AS (
  UPDATE media_fetch_jobs
     SET status = 'retry_wait', claim_token = NULL, claim_until = NULL, claimed_at = NULL,
         next_attempt_at = now(), last_outcome = 'claim_expired'
   WHERE status = 'fetching' AND claim_until < now()
  RETURNING job_id
),
picked AS (
  SELECT j.job_id
  FROM media_fetch_jobs j
  WHERE j.status IN ('pending', 'retry_wait')
    AND j.next_attempt_at <= now()
    AND j.source_url IS NOT NULL
    AND EXISTS (SELECT 1 FROM chat_media cm WHERE cm.job_id = j.job_id)
    AND j.job_id NOT IN (SELECT job_id FROM reclaimed)
  ORDER BY CASE WHEN j.family = 'gupconnector' AND j.first_seen_at > now() - interval '25 minutes' THEN 0
                WHEN j.family = 'gupconnector' THEN 2
                ELSE 1 END,
           j.first_seen_at
  LIMIT $1::int
  FOR UPDATE SKIP LOCKED
),
claimed AS (
  UPDATE media_fetch_jobs j
     SET status = 'fetching', claim_token = gen_random_uuid(), claimed_at = now(),
         -- One claim must outlive the whole serial batch (3 x 75 s + margin)
         -- and still expire before the next run can take the lease (6 min).
         claim_until = now() + interval '5 minutes', attempts = j.attempts + 1
    FROM picked
   WHERE j.job_id = picked.job_id
  RETURNING j.job_id::text AS job_id, j.claim_token::text AS claim_token,
            j.source_url, j.family, j.attempts
)
SELECT c.*,
       (SELECT cm.file_name FROM chat_media cm
         WHERE cm.job_id = c.job_id::uuid AND cm.file_name IS NOT NULL LIMIT 1) AS file_name
FROM claimed c;
"""

SQL_RECORD = r"""
-- Write one fetch outcome, FENCED: only the claim that is still current may
-- finish the job. A late answer from an expired claim matches nothing.
-- multi-item: one fetch result per item, each paired with its own claim.
WITH a AS (SELECT $1::jsonb AS j, $2::int AS max_attempts),
v AS (
  SELECT (j->'job'->>'job_id')::uuid      AS job_id,
         (j->'job'->>'claim_token')::uuid AS tok,
         (j->'job'->>'attempts')::int     AS attempts,
         j->'res'                         AS res,
         -- An answer counts only if it is one of the worker's outcome words.
         -- An error item (timeout, 503) has no outcome and is `no_answer`.
         CASE WHEN j->'res'->>'outcome' IN ('stored','expired','too_large','not_a_file',
                                            'not_allowed','failed')
              THEN j->'res'->>'outcome' ELSE 'no_answer' END AS outcome,
         max_attempts
  FROM a
),
owned AS MATERIALIZED (
  -- The fence, LOCKED: the job row is held from here to commit, so nothing
  -- can reclaim and re-claim it between this check and the write below.
  SELECT j.job_id
  FROM media_fetch_jobs j, v
  WHERE j.job_id = v.job_id AND j.claim_token = v.tok AND j.status = 'fetching'
    AND j.claim_until > now()          -- an elapsed claim may not finish, reclaimed or not
  FOR UPDATE OF j
),
obj AS (
  INSERT INTO media_objects (sha256, bytes, mime, storage_key)
  SELECT res->>'sha256', (res->>'bytes')::bigint, coalesce(res->>'mime', 'application/octet-stream'),
         'sha256/' || left(res->>'sha256', 2) || '/' || (res->>'sha256')
  FROM v
  WHERE outcome = 'stored' AND (res->>'sha256') ~ '^[0-9a-f]{64}$'
    AND EXISTS (SELECT 1 FROM owned)
  ON CONFLICT (sha256) DO UPDATE SET state = 'present', deleted_at = NULL
  RETURNING sha256
),
decided AS (
  -- The new status, decided once. Everything else follows from it, so the
  -- URL can never be dropped from a job that is going to need it again.
  SELECT v.*,
         CASE
           WHEN v.outcome = 'stored' AND EXISTS (SELECT 1 FROM obj)  THEN 'stored'
           WHEN v.outcome = 'expired'                                THEN 'recovery_pending'
           WHEN v.outcome IN ('too_large','not_a_file','not_allowed') THEN 'rejected'
           WHEN v.outcome = 'failed' AND v.attempts >= v.max_attempts THEN 'rejected'
           ELSE 'retry_wait'
         END AS new_status
  FROM v
),
upd AS (
  UPDATE media_fetch_jobs j SET
    status = d.new_status,
    sha256 = CASE WHEN d.new_status = 'stored' THEN d.res->>'sha256' END,
    -- The URL goes the moment the job can never use it again (it may carry a
    -- REST token). Only retry_wait keeps it.
    source_url = CASE WHEN d.new_status = 'retry_wait' THEN j.source_url END,
    -- no_answer (the worker never answered: timeout, 503, no bucket) is an
    -- infrastructure pause, not a verdict on the file: it gives the attempt
    -- back and waits 5 minutes, so a long outage cannot reject good files.
    attempts = CASE WHEN d.outcome = 'no_answer' THEN greatest(j.attempts - 1, 0)
                    ELSE j.attempts END,
    http_status  = nullif(d.res->>'http_status', '')::int,
    last_outcome = CASE WHEN d.new_status = 'rejected' AND d.outcome = 'failed'
                        THEN 'gave_up_after_' || d.attempts ELSE d.outcome END,
    last_error   = left(coalesce(d.res->>'error', d.res->>'message'), 300),
    -- 30 s, 1, 2, 4 … capped at 30 min. A one-minute schedule rounds up.
    next_attempt_at = CASE WHEN d.outcome = 'no_answer' THEN now() + interval '5 minutes'
                           ELSE now() + LEAST(interval '30 minutes',
                                interval '30 seconds' * power(2, LEAST(d.attempts - 1, 6))) END,
    claim_token = NULL, claim_until = NULL, claimed_at = NULL
  FROM decided d
  WHERE j.job_id = d.job_id AND j.job_id IN (SELECT job_id FROM owned)
    AND j.claim_token = d.tok AND j.status = 'fetching'
  RETURNING j.job_id::text AS job_id, j.status, j.last_outcome
)
SELECT coalesce((SELECT job_id FROM upd), (SELECT job_id::text FROM v)) AS job_id,
       (SELECT status FROM upd)          AS status,
       (SELECT last_outcome FROM upd)    AS outcome,
       EXISTS (SELECT 1 FROM upd)        AS fenced_write_applied;
"""

SQL_RELEASE = r"""
-- lease-exempt: this releases the run lease, fenced on the run token below.
-- Release the run lease, but only our own: a run that overran its lease must
-- not release the lease a later run now holds.
UPDATE media_archive_config c
   SET value = '1970-01-01T00:00:00Z', updated_at = now()
 WHERE c.key = 'run_lease_until'
   AND EXISTS (SELECT 1 FROM media_archive_config t
                WHERE t.key = 'run_lease_token' AND t.value = $1)
RETURNING c.key;
"""

# ---------------------------------------------------------------------------
# Discovery parser — the JavaScript twin of app/media/links.py.
# tests/test_workflow_09.py runs this exact code and compares it with Python.
# ---------------------------------------------------------------------------

JS_PARSE = r"""
// Turn examined messages into scan records and attachment references.
// MUST AGREE with services/worker/app/media/links.py (test_workflow_09.py).
const FAMILIES = [
  ['bitrix_rest',  'travelgate.bitrix24.ae',        /^\/rest\/\d+\/[^/]+\/download\//],
  ['bitrix_short', 'travelgate.bitrix24.ae',        /^\/~[A-Za-z0-9]+$/],
  ['gupconnector', 'gupconnector.cultivbureau.com', /^\/connector\/gupshup-media\//],
  ['gupshup',      'filemanager.gupshup.io',        /^\/wa\//],
];
const URL_RE = /https?:\/\/[^\s<>"']+/g;
const TRAIL_RE = /[.,;:!?)\]}>'"،]+$/;
const ATTACH_RE = /^\[Attachment:\s*([^\]\n]{1,255})\]\s*$/;

function classify(url) {
  // Read the RAW authority, not URL's normalised view: `:443` must be
  // accepted and any other port, or credentials, refused — as urlsplit does.
  const m = /^https:\/\/([^/?#]*)(\/[^?#]*)?/i.exec(url);
  if (!m) return null;
  const authority = m[1];
  if (authority.includes('@')) return null;
  const hp = /^([^:]+)(?::(\d*))?$/.exec(authority);
  if (!hp) return null;
  if (hp[2] !== undefined && hp[2] !== '443') return null;
  const host = hp[1].toLowerCase();
  const path = m[2] || '';
  for (const [family, fhost, re] of FAMILIES) {
    if (host === fhost && re.test(path)) return family;
  }
  return null;
}

function findLinks(body) {
  const seen = new Set();
  const out = [];
  for (const raw of String(body || '').match(URL_RE) || []) {
    const url = raw.replace(TRAIL_RE, '');
    const family = classify(url);
    if (family && !seen.has(url)) { seen.add(url); out.push({ url, family }); }
  }
  return out;
}

function attachmentName(body) {
  for (const line of String(body || '').split(/\r?\n/)) {
    const m = ATTACH_RE.exec(line.trim());
    if (m) return m[1].trim();
  }
  return null;
}

const scans = [];
const refs = [];
for (const item of $input.all()) {
  const row = item.json || {};
  if (row.message_id === undefined || row.message_id === null) continue;  // alwaysOutputData's empty item
  const found = findLinks(row.body);
  scans.push({ message_id: String(row.message_id), links_found: found.length });
  const name = attachmentName(row.body);
  found.forEach((l, i) => refs.push({
    message_id: String(row.message_id), ordinal: i, url: l.url, family: l.family,
    file_name: found.length === 1 ? name : null,
    declared_type: row.content_type || null,
    seen_at: row.seen_at,
  }));
}
return [{ json: { scans, refs, scanned: scans.length, links: refs.length } }];
"""

# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def pg(name, nid, query, x, y, *, replacement=None, execute_once=False, always=False, notes=None):
    node = {
        "parameters": {"operation": "executeQuery", "query": query.strip() + "\n",
                       "options": ({"queryReplacement": replacement} if replacement else {})},
        "id": nid, "name": name, "type": "n8n-nodes-base.postgres", "typeVersion": 2.5,
        "position": [x, y], "credentials": PG,
    }
    if execute_once:
        node["executeOnce"] = True
    if always:
        node["alwaysOutputData"] = True
    if notes:
        node["notes"] = notes
    return node


def gate(name, nid, left, x, y, *, op="true"):
    """A boolean IF. `left` is an expression; true goes to output 0."""
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": nid + "-c", "leftValue": left, "rightValue": "",
                    "operator": {"type": "boolean", "operation": op, "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": nid, "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [x, y],
    }


LEASE = "$('Take run lease').first().json"

nodes = [
    {"parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}},
     "id": "every-minute", "name": "Every minute", "type": "n8n-nodes-base.scheduleTrigger",
     "typeVersion": 1.2, "position": [0, 300]},

    pg("Take run lease", "lease", SQL_LEASE, 220, 300, execute_once=True,
       notes="mode lives in media_archive_config. Deployed 'off'."),
    gate("Lease taken?", "lease-ok", f"={{{{ {LEASE}.acquired === true }}}}", 440, 300),

    pg("Retention", "retention", SQL_RETENTION, 660, 300, execute_once=True,
       replacement=f"={{{{ [ {LEASE}.retention_days ] }}}}"),
    gate("Anything to delete?", "del-any",
         "={{ ($json.objects || []).length + ($json.receipts || []).length > 0 }}", 880, 300),
    {"parameters": {
        "method": "POST", "url": "={{ $env.WORKER_URL }}/media/delete",
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "X-API-Key", "value": "={{ $env.WORKER_API_KEY }}"}]},
        "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ sha256: $('Retention').first().json.objects || [], "
                    "url_hashes: $('Retention').first().json.receipts || [] }) }}",
        "options": {"timeout": 120000}},
     "id": "delete", "name": "Delete from bucket", "type": "n8n-nodes-base.httpRequest",
     "typeVersion": 4.2, "position": [1100, 200], "onError": "continueRegularOutput",
     "executeOnce": True},
    gate("Delete answered?", "del-ok",
         "={{ Array.isArray($json.objects) && Array.isArray($json.receipts) }}", 1320, 200),
    pg("Stamp deleted", "stamp-deleted", SQL_MARK_DELETED, 1540, 120, execute_once=True, always=True,
       replacement="={{ [ JSON.stringify({ objects: $json.objects, receipts: $json.receipts, "
                   "receipt_objects: $json.receipt_objects || [] }) ] }}",
       notes="Only what the worker reported ok. Anything else stays pending for the next run."),

    pg("Find unexamined messages", "discover", SQL_DISCOVER, 1760, 300, execute_once=True, always=True,
       replacement=f"={{{{ [ {LEASE}.scan_batch, {LEASE}.retention_days ] }}}}"),
    {"parameters": {"mode": "runOnceForAllItems", "jsCode": JS_PARSE.strip() + chr(10)},
     "id": "parse", "name": "Parse links", "type": "n8n-nodes-base.code", "typeVersion": 2,
     "position": [1980, 300]},
    pg("Record discoveries", "persist", SQL_PERSIST, 2200, 300, execute_once=True, always=True,
       replacement=f"={{{{ [ JSON.stringify($('Parse links').first().json), '{PARSER_VERSION}' ] }}}}"),

    pg("Claim downloads", "claim", SQL_CLAIM, 2420, 300, execute_once=True, always=True,
       replacement=f"={{{{ [ {LEASE}.fetch_batch ] }}}}"),
    gate("Claimed one?", "claimed", "={{ typeof $json.claim_token === 'string' && $json.claim_token.length > 0 }}",
         2640, 300),
    # ONE DOWNLOAD AT A TIME, for real. The HTTP node's own batching only
    # spaces out the STARTS of its requests and then awaits them together, so
    # it would run every claimed download at once (round-3 review). A loop
    # hands the HTTP node one item per iteration and waits for its answer.
    {"parameters": {"batchSize": 1, "options": {}},
     "id": "each", "name": "Each download", "type": "n8n-nodes-base.splitInBatches",
     "typeVersion": 3, "position": [2860, 300]},
    {"parameters": {
        "method": "POST", "url": "={{ $env.WORKER_URL }}/media/fetch",
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "X-API-Key", "value": "={{ $env.WORKER_API_KEY }}"}]},
        "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ url: $json.source_url, file_name: $json.file_name || null }) }}",
        "options": {"timeout": 75000}},
     "id": "fetch", "name": "Download through worker", "type": "n8n-nodes-base.httpRequest",
     "typeVersion": 4.2, "position": [3080, 200], "onError": "continueRegularOutput",
     "notes": "75 s cap x fetch_batch 3 stays inside the 300 s execution timeout and the 5-minute "
              "claim. onError continues so a timeout becomes a recorded 'no_answer', never a lost claim."},
    pg("Record outcome", "record", SQL_RECORD, 3300, 200,
       replacement=("={{ [ JSON.stringify({ job: $('Each download').item.json, res: $json }), "
                    f"{LEASE}.max_attempts ] }}}}")),

    pg("Release run lease", "release", SQL_RELEASE, 3520, 400, execute_once=True, always=True,
       replacement=f"={{{{ [ {LEASE}.run_token ] }}}}"),
]

connections = {
    "Every minute": {"main": [[{"node": "Take run lease", "type": "main", "index": 0}]]},
    "Take run lease": {"main": [[{"node": "Lease taken?", "type": "main", "index": 0}]]},
    "Lease taken?": {"main": [[{"node": "Retention", "type": "main", "index": 0}], []]},
    "Retention": {"main": [[{"node": "Anything to delete?", "type": "main", "index": 0}]]},
    "Anything to delete?": {"main": [[{"node": "Delete from bucket", "type": "main", "index": 0}],
                                     [{"node": "Find unexamined messages", "type": "main", "index": 0}]]},
    "Delete from bucket": {"main": [[{"node": "Delete answered?", "type": "main", "index": 0}]]},
    # A delete that did not answer may still be running in the worker. No
    # fetch may start in this run: release the lease and let the next run
    # (and the worker's bucket lock) take it from there.
    "Delete answered?": {"main": [[{"node": "Stamp deleted", "type": "main", "index": 0}],
                                  [{"node": "Release run lease", "type": "main", "index": 0}]]},
    "Stamp deleted": {"main": [[{"node": "Find unexamined messages", "type": "main", "index": 0}]]},
    "Find unexamined messages": {"main": [[{"node": "Parse links", "type": "main", "index": 0}]]},
    "Parse links": {"main": [[{"node": "Record discoveries", "type": "main", "index": 0}]]},
    "Record discoveries": {"main": [[{"node": "Claim downloads", "type": "main", "index": 0}]]},
    "Claim downloads": {"main": [[{"node": "Claimed one?", "type": "main", "index": 0}]]},
    "Claimed one?": {"main": [[{"node": "Each download", "type": "main", "index": 0}],
                              [{"node": "Release run lease", "type": "main", "index": 0}]]},
    # splitInBatches v3: output 0 = done, output 1 = loop
    "Each download": {"main": [[{"node": "Release run lease", "type": "main", "index": 0}],
                               [{"node": "Download through worker", "type": "main", "index": 0}]]},
    "Download through worker": {"main": [[{"node": "Record outcome", "type": "main", "index": 0}]]},
    "Record outcome": {"main": [[{"node": "Each download", "type": "main", "index": 0}]]},
}

workflow = {
    "name": "09 · Chat media archive",
    "nodes": nodes,
    "connections": connections,
    "settings": {
        "executionOrder": "v1",
        "timezone": "Asia/Riyadh",
        # Execution data holds source URLs, and bitrix_rest URLs hold a live
        # REST token. The database is the record; see v_media_health.
        "saveDataSuccessExecution": "none",
        "saveDataErrorExecution": "none",
        "saveManualExecutions": False,
        "saveExecutionProgress": False,
        "executionTimeout": 300,
    },
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(REPO)}  ({len(nodes)} nodes)")
