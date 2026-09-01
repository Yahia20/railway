-- 016 — the chat scoring queue covers the production API only.
--
-- WHAT 015 GOT WRONG. `v_chat_eval_due` accepted both chat namespaces:
--
--     WHERE i.external_source IN ('bitrix_chat_api', 'bitrix')
--
-- `bitrix` is workflow 01's namespace, and every one of the 15 rows in it is a
-- fixture from the 30 July bring-up: smoke-fa402bdd, TEST_claude_e2e_v4,
-- deal-TESTDEAL-777. They are also the OLDEST threads in the database, and the
-- queue is ordered oldest-idle-first, so they sort to the front — the first
-- fifteen judge calls of the first production run would have been spent
-- grading "أهلاً وسهلاً، معك أحمد. رحلات دبي تبدأ من 2500 ريال", a line
-- somebody typed by hand to test an insert.
--
-- Caught before the run, in a --dry-run that printed the queue head. That is
-- what the dry run is for.
--
-- WHY NOT FILTER ON THE NAME. Excluding `external_id LIKE 'TEST%'` would work
-- today and fail the first time a fixture is named something else. The real
-- distinction is the namespace: `bitrix_chat_api` is what the production chat
-- API writes through 01c, and it is the only source this workflow is for.
-- Threads that arrive through workflow 01 are scored by workflow 01, inline.
--
-- Idempotent: CREATE OR REPLACE VIEW. No data is touched — 015's queue rows
-- for the fixtures stay in chat_eval_jobs as 'pending' and simply stop being
-- claimable, which is the right record of what happened.

BEGIN;

CREATE OR REPLACE VIEW v_chat_eval_due AS
SELECT
  i.interaction_id,
  i.external_id,
  i.external_source,
  i.channel,
  i.message_count,
  i.customer_message_count,
  i.agent_message_count,
  m.last_message_at,
  round(extract(epoch FROM (now() - m.last_message_at)) / 86400.0, 2) AS idle_days
FROM interactions i
JOIN LATERAL (
  SELECT max(sent_at) AS last_message_at FROM chat_messages c
  WHERE c.interaction_id = i.interaction_id
) m ON true
WHERE i.external_source = 'bitrix_chat_api'
  AND m.last_message_at IS NOT NULL
  AND m.last_message_at < now() - interval '2 days'
  AND i.message_count >= 2
  AND i.agent_message_count > 0
  AND i.customer_message_count > 0;

COMMENT ON VIEW v_chat_eval_due IS
  'Chat threads from the production API, silent for 2 days and worth grading. '
  'The idle window is defined here only; workflow 01d and any backfill both '
  'read this view. The bitrix namespace is excluded: it holds workflow 01''s '
  'threads and the 30 July test fixtures.';

-- The fixtures were already registered by 015's first run. Retire them
-- explicitly rather than leaving rows that claim to be pending work nobody
-- will ever do.
UPDATE chat_eval_jobs j
   SET status     = 'dead_letter',
       last_error = 'not production traffic: workflow 01 test fixture, retired by 016',
       claim_token = NULL, claim_until = NULL, claimed_at = NULL,
       updated_at = now()
  FROM interactions i
 WHERE i.interaction_id = j.interaction_id
   AND i.external_source <> 'bitrix_chat_api'
   AND j.status IN ('pending', 'judge_failed');

COMMIT;
