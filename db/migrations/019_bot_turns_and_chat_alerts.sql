-- 019 — the automation posting as an agent, and alerts for chats.
--
-- ═══════════════════════════════════════════════════════════════════════════
-- 1 · AN AUTOMATION WAS BEING GRADED AS A SALESPERSON
--
-- Bitrix user 1 (the vendor integration account) has 1,157 turns stored with
-- sender = 'agent'. They are not messages. They are the PROMPT that some other
-- automation sends to its own model, saved into the thread verbatim:
--
--   "The client has not responded. Based on the conversation context,
--    generate a short, natural follow-up message in Saudi Arabic that
--    encourages the client to continue the conversation. Guidelines: ..."
--
-- Three separate harms, all of which were live:
--
--   a) PROMPT INJECTION INTO OUR OWN JUDGE. That text went into pass 1 and
--      pass 2 as an agent turn. It is instructions addressed to a model,
--      inside the input of a model — exactly what rule 8 forbids for
--      UF_CRM_1781281581, arriving through a door nobody was watching.
--   b) It was attributed to a person. That account sat at the top of
--      v_agent_scorecard with 7 evaluations and the worst average in the
--      company (21.9), which is a number about a cron job filed under a name.
--   c) It corrupts every timing metric: 1,157 turns that were never sent to a
--      customer counted as agent replies.
--
-- THE FIX IS ONE FLAG, not a filter buried in SQL. `agents.is_bot` already
-- means "never grade this as a human" and v_agent_scorecard already honours
-- it. Marking user 1 is therefore the whole change here, and marking the NEXT
-- automation somebody wires up is a one-line UPDATE with no deploy:
--
--   UPDATE agents SET is_bot = true WHERE bitrix_user_id = '<id>';
--
-- Workflow 01d's "Load thread" relabels turns from any is_bot agent to
-- sender = 'bot' so they stop reaching the judge as agent speech. It reads the
-- flag, so that UPDATE is genuinely all there is to do.
-- ═══════════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL lock_timeout = '5s';

-- A REPAIR FOR THE DATABASE THAT ALREADY EXISTS, not the source of truth.
--
-- 018 used to inline the roster; it no longer does, because 48 real names must
-- not sit in a public repository (rule 7). So on a FRESH database this UPDATE
-- matches nothing — the row does not exist yet — and that is correct, not a
-- bug: `local-reports/agent_roster.json` carries `"is_bot": true` for this id
-- and `scripts/seed_agents.py` applies it. The seed also refuses to turn the
-- flag back off, so neither order of operations can lose it.
--
-- This statement exists so that the one database already carrying the row gets
-- flagged without waiting for a seed run.
UPDATE agents
   SET is_bot     = true,
       is_active  = false,
       updated_at = now()
 WHERE bitrix_user_id = '1'
   AND is_bot = false;

COMMENT ON COLUMN agents.is_bot IS
  'Never grade this account as a human. v_agent_scorecard filters on it and '
  '01d''s "Load thread" relabels its turns to sender=bot so they never reach '
  'the judge as agent speech. Set it for any automation that posts into a '
  'thread — that is the entire procedure, no deploy required.';

-- ---------------------------------------------------------------------------
-- 2 · link_agent_attribution() — a bot-only thread belongs to nobody
--
-- The previous version fell back to the deal's owner whenever no human turn
-- was found. With user 1 now flagged, 88 threads have agent turns that are ALL
-- automation, and that fallback would hand each one to whichever salesperson
-- happens to own the deal — grading a person on a conversation they never
-- took part in, which is the precise failure this whole file exists to stop.
--
-- The fallback now applies only where the thread has no identifiable agent
-- turn AT ALL. A thread whose only agent turns are a bot's keeps agent_id
-- NULL, and /chats/prepare then reports is_bot_only and refuses to score it.
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
  n_cleared int;
BEGIN
  -- a) resolve the deal's raw Bitrix id to our agent uuid.
  --
  --    LEFT JOIN, so a deal reassigned to somebody who is not in the roster
  --    yet has its agent_id CLEARED rather than left pointing at the previous
  --    owner. An inner join here silently kept the old name on the deal, and
  --    step (c) then credited a conversation to a person who had handed it
  --    over — a stale attribution reads exactly like a correct one.
  UPDATE deals d
     SET agent_id = a.agent_id, updated_at = now()
    FROM deals src
    LEFT JOIN agents a ON a.bitrix_user_id = src.assigned_by_id
   WHERE d.deal_id = src.deal_id
     AND src.assigned_by_id IS NOT NULL
     AND d.agent_id IS DISTINCT FROM a.agent_id;
  GET DIAGNOSTICS n_deals = ROW_COUNT;

  -- b) the HUMAN who actually typed in the thread
  WITH turns AS (
    SELECT cm.interaction_id,
           cm.sender_external_id AS ext,
           count(*)        AS turns,
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
     ORDER BY t.interaction_id, t.turns DESC, t.first_at ASC
  )
  UPDATE interactions i
     SET agent_id = a.agent_id, updated_at = now()
    FROM pick p
    JOIN agents a ON a.bitrix_user_id = p.ext
   WHERE i.interaction_id = p.interaction_id
     AND i.agent_id IS DISTINCT FROM a.agent_id;
  GET DIAGNOSTICS n_msg = ROW_COUNT;

  -- b2) un-attribute anything a bot-only thread was previously given.
  --     Idempotent and self-correcting: flag an automation today and tomorrow
  --     night's run takes its threads off the human it was wrongly charged to.
  UPDATE interactions i
     SET agent_id = NULL, updated_at = now()
   WHERE i.agent_id IS NOT NULL
     AND EXISTS (SELECT 1 FROM chat_messages cm
                  WHERE cm.interaction_id = i.interaction_id
                    AND cm.sender = 'agent'
                    AND cm.sender_external_id IS NOT NULL)
     AND NOT EXISTS (SELECT 1
                       FROM chat_messages cm
                       JOIN agents a ON a.bitrix_user_id = cm.sender_external_id
                      WHERE cm.interaction_id = i.interaction_id
                        AND cm.sender = 'agent'
                        AND a.is_bot = false);
  GET DIAGNOSTICS n_cleared = ROW_COUNT;

  -- c) the deal's owner, ONLY where nobody identifiable typed at all
  UPDATE interactions i
     SET agent_id = d.agent_id, updated_at = now()
    FROM deals d
   WHERE d.deal_id = i.deal_id
     AND i.agent_id IS NULL
     AND d.agent_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM chat_messages cm
                      WHERE cm.interaction_id = i.interaction_id
                        AND cm.sender = 'agent'
                        AND cm.sender_external_id IS NOT NULL);
  GET DIAGNOSTICS n_fallbk = ROW_COUNT;

  -- d) carry it onto the evaluations, which is what the scorecard joins.
  --    Also clears a stale attribution, so b2 reaches the scorecard too.
  UPDATE agent_evaluations e
     SET agent_id = i.agent_id, updated_at = now()
    FROM interactions i
   WHERE i.interaction_id = e.interaction_id
     AND e.agent_id IS DISTINCT FROM i.agent_id;
  GET DIAGNOSTICS n_eval = ROW_COUNT;

  RETURN jsonb_build_object(
    'deals_linked',        n_deals,
    'threads_by_message',  n_msg,
    'threads_cleared_bot', n_cleared,
    'threads_by_deal',     n_fallbk,
    'evaluations_linked',  n_eval);
END
$fn$;

-- ---------------------------------------------------------------------------
-- 3 · alerts for chats
--
-- `evaluate_alert_rules(uuid)` has always been channel-agnostic — it reads
-- interaction_analysis and interactions, neither of which knows what a call
-- is. Only workflow 02 ever called it, so every alert in the system was a call
-- alert: all 25 occurrences on record are `phone_call`. With calls paused,
-- that means the follow-up queue is dark.
--
-- 01d gets the same stamp-and-evaluate node 02 uses, and needs the same column
-- to record that it ran. Without the stamp a transient failure after the job
-- is terminal loses that thread's occurrences permanently and silently, which
-- is the bug 013 fixed on the calls side.
-- ---------------------------------------------------------------------------
ALTER TABLE chat_eval_jobs
  ADD COLUMN IF NOT EXISTS alerts_evaluated_at timestamptz;

COMMENT ON COLUMN chat_eval_jobs.alerts_evaluated_at IS
  'When alert rules were evaluated for this thread. NULL after a successful '
  'judge means the alert pass still owes work — v_chat_alerts_pending lists '
  'exactly those, so a failure is visible instead of lost.';

CREATE INDEX IF NOT EXISTS idx_chat_eval_jobs_alerts_pending
  ON chat_eval_jobs (updated_at)
  WHERE status = 'evaluated' AND alerts_evaluated_at IS NULL;

-- The backlog view: threads judged but never alert-evaluated. A leaf node that
-- fails leaves its row here rather than nowhere.
CREATE OR REPLACE VIEW v_chat_alerts_pending AS
SELECT j.interaction_id,
       j.updated_at AS evaluated_at,
       i.external_id,
       i.started_at
  FROM chat_eval_jobs j
  JOIN interactions i ON i.interaction_id = j.interaction_id
 WHERE j.status = 'evaluated'
   AND j.alerts_evaluated_at IS NULL
 ORDER BY j.updated_at;

COMMENT ON VIEW v_chat_alerts_pending IS
  'Chat threads that were judged but whose alert rules never ran. Should be '
  'empty; a non-empty result is work that was dropped, not work not yet due.';

COMMIT;
