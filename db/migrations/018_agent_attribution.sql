-- 018 — agent attribution: who handled the conversation.
--
-- THE HOLE THIS FILLS. `agents` held one row (the seeded bot) plus five stubs
-- named "Bitrix user 86". `interactions.agent_id` was NULL on 1,817 of 1,833
-- rows and `agent_evaluations.agent_id` on all of them, so:
--
--   * v_agent_scorecard returned nothing at all — it INNER JOINs agents on
--     agent_evaluations.agent_id, so a NULL there removes the row entirely;
--   * workflow 03's "Materialise promises" filters `i.agent_id IS NOT NULL`,
--     which matched zero rows, which is the whole reason `follow_ups` was 0
--     while pass 1 was extracting promises the entire time;
--   * module 4 of the rubric — 20% of the weight — was `not_applicable` on
--     814 of 826 evaluations.
--
-- In other words the "Sales Quality" half of this project produced no output,
-- and every symptom traced back to this one missing join.
--
-- WHY THE ROSTER IS INLINED HERE INSTEAD OF PULLED. `user.get` is outside the
-- webhook's scope (verified: {"error":"insufficient_scope"}), so the portal will
-- not name its own users. But `crm.deal.list` returns ASSIGNED_BY_ID, and the
-- manual CSV export renders the same deal's responsible person as a display
-- NAME. Joining the two on the deal id recovers the roster: 17,708 deals from
-- REST against 17,707 from the export, 17,575 joined, 48 distinct user ids,
-- every one resolving to a single name at confidence 1.00. That join is a
-- one-off — once these rows exist, ASSIGNED_BY_ID alone is enough forever.
--
-- If the scope is ever widened to include `user`, replace this seed with a
-- `user.get` pull and delete the CSV step. Nothing else has to change.

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1 · the roster
--
-- ON CONFLICT updates the NAME but never the flags a human may have corrected
-- by hand afterwards... except is_bot, which is not an opinion: user ids 30 and
-- 20114 are both "Travelgate AI" and must never appear in a human leaderboard.
-- v_agent_scorecard filters on is_bot = false and nothing else, so this column
-- is the only thing standing between the bot and the QA numbers.
--
-- is_active = false for user 1 (the vendor integration account): it is the integration
-- account, not a salesperson. It posts in 720 threads but only 950 messages —
-- about one per thread — so the attribution rule below deliberately ranks it
-- last and picks it only when no real agent spoke.
-- ---------------------------------------------------------------------------
-- The roster itself is NOT in this file. It is 48 real people's names and this
-- repository is public (rule 7), so it lives in `local-reports/agent_roster.json`,
-- which is gitignored, and is applied by:
--
--     python scripts/seed_agents.py --roster local-reports/agent_roster.json --apply
--
-- That is also the better shape for the long run: onboarding a salesperson is
-- one line of JSON and a re-run, not a new migration. The seed sets `is_bot`
-- from the same file, which is the flag that keeps an automation out of
-- v_agent_scorecard and stops its text reaching the judge as agent speech.

-- ---------------------------------------------------------------------------
-- 2 · deals carry the raw Bitrix user id
--
-- `deals.agent_id` is our uuid; a deal arriving from the nightly pull knows
-- only Bitrix's integer. Keeping the raw id means the mapping can be redone
-- after a roster correction without re-pulling 17,000 deals.
-- ---------------------------------------------------------------------------
ALTER TABLE deals ADD COLUMN IF NOT EXISTS assigned_by_id text;
COMMENT ON COLUMN deals.assigned_by_id IS
  'Bitrix ASSIGNED_BY_ID exactly as the CRM sent it. agent_id is the resolved '
  'FK; this is what it was resolved FROM, so a roster fix can be replayed.';
CREATE INDEX IF NOT EXISTS deals_assigned_by_idx ON deals (assigned_by_id)
  WHERE assigned_by_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3 · link_agent_attribution() — idempotent, and safe to run every night
--
-- THREE SOURCES, IN ORDER OF TRUTHFULNESS:
--
--   a) chat_messages.sender_external_id — who actually typed. 11,849 of 11,932
--      agent turns carry it, across 966 of 987 threads. This is the real
--      answer: a thread can be handled by two people and the CRM's single
--      "responsible" field cannot express that, so the agent with the most
--      turns wins and ties break to whoever spoke first.
--   b) the deal's ASSIGNED_BY_ID — used only where (a) is silent.
--   c) nothing. Calls stay NULL on purpose; see the note at the bottom.
--
-- agent_evaluations.agent_id is set LAST, from interactions, because that is
-- the column v_agent_scorecard actually joins on. Setting one without the
-- other leaves the scorecard exactly as empty as it was.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION link_agent_attribution()
RETURNS jsonb
LANGUAGE plpgsql
AS $fn$
DECLARE
  n_deals   int;
  n_msg     int;
  n_fallbk  int;
  n_eval    int;
BEGIN
  -- a) resolve the deal's raw Bitrix id to our agent uuid
  UPDATE deals d
     SET agent_id = a.agent_id, updated_at = now()
    FROM agents a
   WHERE a.bitrix_user_id = d.assigned_by_id
     AND d.agent_id IS DISTINCT FROM a.agent_id;
  GET DIAGNOSTICS n_deals = ROW_COUNT;

  -- b) the agent who actually typed in the thread
  WITH turns AS (
    SELECT cm.interaction_id,
           cm.sender_external_id AS ext,
           count(*)      AS turns,
           min(cm.sent_at) AS first_at
      FROM chat_messages cm
     WHERE cm.sender = 'agent'
       AND cm.sender_external_id IS NOT NULL
     GROUP BY 1, 2
  ), pick AS (
    SELECT DISTINCT ON (t.interaction_id) t.interaction_id, t.ext
      FROM turns t
      JOIN agents a ON a.bitrix_user_id = t.ext
     WHERE a.is_bot = false
     -- is_active DESC puts the integration account last without excluding it:
     -- a thread only it touched is still attributable, just not to a human.
     ORDER BY t.interaction_id, a.is_active DESC, t.turns DESC, t.first_at ASC
  )
  UPDATE interactions i
     SET agent_id = a.agent_id, updated_at = now()
    FROM pick p
    JOIN agents a ON a.bitrix_user_id = p.ext
   WHERE i.interaction_id = p.interaction_id
     AND i.agent_id IS DISTINCT FROM a.agent_id;
  GET DIAGNOSTICS n_msg = ROW_COUNT;

  -- c) fall back to the deal's owner where nobody identifiable typed
  UPDATE interactions i
     SET agent_id = d.agent_id, updated_at = now()
    FROM deals d
   WHERE d.deal_id = i.deal_id
     AND i.agent_id IS NULL
     AND d.agent_id IS NOT NULL;
  GET DIAGNOSTICS n_fallbk = ROW_COUNT;

  -- d) carry it onto the evaluations, which is what the scorecard joins
  UPDATE agent_evaluations e
     SET agent_id = i.agent_id, updated_at = now()
    FROM interactions i
   WHERE i.interaction_id = e.interaction_id
     AND i.agent_id IS NOT NULL
     AND e.agent_id IS DISTINCT FROM i.agent_id;
  GET DIAGNOSTICS n_eval = ROW_COUNT;

  RETURN jsonb_build_object(
    'deals_linked',        n_deals,
    'threads_by_message',  n_msg,
    'threads_by_deal',     n_fallbk,
    'evaluations_linked',  n_eval);
END
$fn$;

COMMENT ON FUNCTION link_agent_attribution() IS
  'Resolve who handled each conversation, from the per-message Bitrix user id '
  'first and the deal owner second, then carry it onto agent_evaluations. '
  'Idempotent: every UPDATE is guarded by IS DISTINCT FROM.';

COMMIT;

-- ---------------------------------------------------------------------------
-- CALLS ARE DELIBERATELY NOT ATTRIBUTED.
--
-- All 1,119 recordings on Drive decode to the same agent_extension, "3009".
-- That is a queue, not a person, so there is no honest way to say which agent
-- handled a call. `agents.phone_extension` exists for exactly this mapping and
-- is left empty rather than filled with a guess: attributing 830 calls to one
-- invented agent would produce a scorecard that looks complete and is fiction.
--
-- The fix is not ours. The PBX has to record the answering extension in the
-- filename (or record two channels, which also fixes diarization — gotcha 10).
-- Until then chats carry the sales-quality numbers and calls do not.
-- ---------------------------------------------------------------------------
