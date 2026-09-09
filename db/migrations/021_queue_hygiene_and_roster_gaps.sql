-- 021 — stop the report counting retired work, and notice new staff.
--
-- ---------------------------------------------------------------------------
-- 1 · the retired namespace is still in the judging queue
--
-- `chat_eval_jobs` holds 13 rows whose interaction is `external_source =
-- 'bitrix'` — the namespace of workflow 01, the inline-scoring handler retired
-- by migration 016. Four of them say `status = 'evaluated'` and have no
-- `agent_evaluations` row at all: their pass 1 ran on 2026-07-30 under
-- `pass1-customer-v1` and pass 2 never stored anything.
--
-- WHY IT MATTERS RATHER THAN BEING UNTIDY. `/report`'s chat_jobs panel counts
-- these rows, so it reported 40 threads evaluated when 27 were. A person
-- reading that number is being told the pipeline did 48% more work than it
-- did, and the four that claim success while holding no result are the worst
-- kind of wrong: they look like the healthy state.
--
-- SAFE TO DELETE, PERMANENTLY. `v_chat_eval_due` filters
-- `external_source = 'bitrix_chat_api'` (016, unchanged by 017), so these rows
-- can never be registered again. Deleting a queue row touches nothing else:
-- the interaction, its analysis and any evaluation are separate tables and
-- keep their rows. This removes bookkeeping about work that is not ours, not
-- the record of the work itself.
-- ---------------------------------------------------------------------------

BEGIN;

SET LOCAL lock_timeout = '5s';

DELETE FROM chat_eval_jobs j
 USING interactions i
 WHERE i.interaction_id = j.interaction_id
   AND i.external_source <> 'bitrix_chat_api';

-- ---------------------------------------------------------------------------
-- 2 · v_roster_gaps — the one manual step Bitrix leaves us
--
-- Everything about a deal is automated: `crm.deal.list` pages nightly through
-- workflow 04 and carries ASSIGNED_BY_ID, so a deal's owner arrives on its
-- own. The single thing the portal will not give up is the owner's NAME —
-- `user.get` answers `insufficient_scope` and the webhook holds `crm` only.
--
-- So a salesperson who joins after the roster was built shows up as a Bitrix
-- user id nobody has named, their deals get no agent_id, and they are missing
-- from every per-agent report while looking exactly like a customer who has no
-- deals. This view is what turns that from a silent hole into a task:
--
--     python scripts/seed_agents.py --roster local-reports/agent_roster.json --apply
--
-- after adding one line to the roster file. `/report` reads this view, so the
-- prompt to do it appears the morning after the first deal is assigned.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_roster_gaps AS
SELECT d.assigned_by_id            AS bitrix_user_id,
       count(*)                    AS deals,
       min(d.created_at_src)       AS first_deal_at,
       max(d.created_at_src)       AS latest_deal_at
  FROM deals d
 WHERE d.assigned_by_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM agents a
                    WHERE a.bitrix_user_id = d.assigned_by_id)
 GROUP BY d.assigned_by_id
 ORDER BY count(*) DESC;

COMMENT ON VIEW v_roster_gaps IS
  'Bitrix users who own deals but have no agents row — new staff. The only '
  'part of the Bitrix integration that is not automated, because user.get is '
  'outside the webhook scope. Fix by adding a line to the roster file and '
  're-running scripts/seed_agents.py.';

COMMIT;
