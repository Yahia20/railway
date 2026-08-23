-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Mark unscoreable'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token, (() => { const j = $json || {}; if (j.asr_quality) return 'asr_quality_red: ' + JSON.stringify(j.asr_quality); if (j.unscoreable_reason) return 'unscoreable: ' + j.unscoreable_reason; if (j.contract_status === 'unscoreable') return 'unscoreable: the stored evaluation row carried no stated reason'; return 'unscoreable: no reason available -- neither an ASR quality verdict nor a stored unscoreable evaluation reached this node'; })() ] }}
-- fence-exempt: single-statement UPDATE. The fence IS the statement's own
-- WHERE clause, so the row is locked and the predicate re-evaluated against
-- the latest committed version by the UPDATE itself (EvalPlanQual). There is
-- no read-then-write window to widen, and adding FOR UPDATE would only take
-- the same lock twice.
-- TWO WAYS IN, ONE MEANING: there is nothing here that can be scored, and
-- asking again would not change that. Both are terminal dead_letter, and
-- neither is ever retried.
--
--   1. 'ASR quality red?' -- too much of the audio is unaccounted for
--      (failed chunks, decoder loops, contamination-dominated text) to
--      score an agent on what remains. The transcript IS stored for audit.
--      Re-transcribing the same audio reproduces the same acoustics.
--   2. 'Nothing to evaluate?' -> 'Store unscoreable outcome' -> here. Pass 2
--      came back with contract_status = 'unscoreable': the worker refused
--      BEFORE any model call because the transcript held less speech than
--      the scoring minimum. Re-asking a model that was never asked cannot
--      help, and routing it to judge_failed (which is what happened before)
--      burned a judge attempt every 45 minutes until the budget ran out and
--      the row dead-lettered anyway -- five wasted retries to reach the same
--      place. The agent_evaluations row IS written, one node upstream, and
--      only then is the job terminalised here (round-4 review): the outcome
--      belongs in unscoreable_count whether or not pass 1 ran.
--
-- WHY THE REASON IS A PARAMETER. $3 used to be the ASR gate detail with
-- 'asr_quality_red: ' hard-coded in front of it. With a second entry path
-- that prefix would be a lie on every unscoreable row, so the caller now
-- builds the whole string and the SQL only truncates it. The expression
-- reads ONLY $json and the claim node -- both branches run the claim, and
-- neither branch may reach into a node the other one ran (the validator's
-- branch-isolation rule; 'Two AI passes' has no run data on the ASR path).
-- On the pass-2 path $json is no longer the /evaluate response: it is the row
-- RETURNed by 'Store unscoreable outcome', which is why that node returns
-- `notes AS unscoreable_reason` -- the reason has to travel through the write
-- rather than around it.
--
-- Guarded on claim_token: an execution whose lease expired must not be able to
-- dead-letter a row somebody else has since picked up and may be about to
-- evaluate successfully. Status IN ('transcribing','evaluating') covers both
-- entries: the ASR path arrives holding a 'transcribing' lease, the pass-2
-- path an 'evaluating' one stamped by 'Begin judge attempt'.
UPDATE call_ingest_jobs
SET status      = 'dead_letter',
    last_error  = left($3, 500),
    claim_token = NULL,
    claimed_at  = NULL,
    claim_until = NULL,
    updated_at  = now()
WHERE uniqueid = $1
  AND claim_token = $2::uuid
  AND status IN ('transcribing', 'evaluating')
RETURNING uniqueid, status;
