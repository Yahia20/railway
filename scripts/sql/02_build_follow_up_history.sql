-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Build follow-up history'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $json.interaction_id ] }}
-- Everything the judge is allowed to treat as FACT about follow-up, rendered
-- as a labelled block. Module 4 is 20% of the agent's grade and cannot be
-- observed inside a single phone call, so it is scored across the customer's
-- timeline instead -- this query is that timeline.
--
-- WHAT WAS WRONG BEFORE. On day 13, four calls had later same-phone
-- interactions already in the database and still scored Module 4 = null with
-- "no follow-up history was supplied". The block reached the prompt; it was
-- unreadable when it got there:
--
--   1. Every line said "by unknown". Store call + transcript deliberately sets
--      agent_id = NULL for 'q' recordings, because the extension in a queue
--      filename is the QUEUE, not a person -- and 'q' is nearly every recording
--      we have. So coalesce(a.full_name, 'unknown') rendered "unknown" for
--      essentially the whole corpus.
--   2. Nothing stated the DIRECTION of the later contact. A customer calling
--      back in is not an agent following up, and Module 4 scores only what the
--      AGENT did. Given "unknown, direction unstated", null is the honest
--      answer, and the model gave it.
--   3. No header and no message text. The block was a bare list of bullets, so
--      criterion 3 (follow-up MESSAGE QUALITY, 30 of the module's 100 points)
--      was unanswerable from it, and the prompt's rule -- "if the FOLLOW-UP
--      HISTORY block is absent ... Module 4 = null" -- had nothing to
--      recognise it by.
--
-- The block now names the direction explicitly, distinguishes "no individual
-- agent recorded (queue recording)" from "we do not know", and carries the
-- agent's first message for channels that have message text.
--
-- Keyed on the stored interaction row rather than on the job's filename
-- metadata: the interaction row is what the rest of the pipeline joins on, and
-- on the evaluate path (a judge retry) the filename metadata is a stale copy.
--
-- Returns exactly one row, always. The literal 'unavailable' means "we cannot
-- see", which the pass-2 prompt reads as "Module 4 = null". Note that an empty
-- timeline still produces the same word -- see docs/PR1A-leases.md, "the
-- silent-timeline question", for why turning that into a scored zero is a
-- product decision and not a bug fix.
SELECT coalesce((
  SELECT 'Subsequent contact with this customer:' || E'\n'
         || string_agg(x.line, E'\n' ORDER BY x.started_at)
  FROM (
    SELECT n.started_at,
           format('  - [%s] %s, %s, %sh after this conversation, handled by %s%s',
                  to_char(n.started_at, 'YYYY-MM-DD HH24:MI'),
                  n.channel::text,
                  CASE
                    WHEN n.channel = 'phone_call' AND jj.meta->>'kind' = 'q'
                      THEN 'INBOUND: the customer called in, this is not an agent follow-up'
                    WHEN n.direction IS NOT NULL
                      THEN 'direction ' || n.direction::text
                    ELSE 'direction not recorded'
                  END,
                  round((extract(epoch from (n.started_at - me.started_at)) / 3600.0)::numeric, 1),
                  CASE
                    WHEN a.full_name IS NOT NULL THEN a.full_name
                    WHEN n.is_bot_handled        THEN 'the qualification bot, not a human agent'
                    WHEN jj.meta->>'kind' = 'q'  THEN 'no individual agent recorded (queue recording)'
                    ELSE 'not recorded'
                  END,
                  coalesce(': "' || left(msg.body, 300) || '"', '')) AS line
    FROM interactions me
    JOIN interactions n
      ON  n.customer_phone_e164 = me.customer_phone_e164
      AND n.interaction_id     <> me.interaction_id
      AND n.started_at          > me.started_at
      AND n.started_at          < me.started_at + interval '14 days'
    LEFT JOIN agents a            ON a.agent_id        = n.agent_id
    LEFT JOIN call_ingest_jobs jj ON jj.interaction_id = n.interaction_id
    LEFT JOIN LATERAL (
      SELECT cm.body FROM chat_messages cm
      WHERE cm.interaction_id = n.interaction_id AND cm.sender = 'agent'
      ORDER BY cm.seq LIMIT 1
    ) msg ON true
    WHERE me.interaction_id = $1::uuid
      AND me.customer_phone_e164 IS NOT NULL
  ) x
), 'unavailable') AS followup_history;
