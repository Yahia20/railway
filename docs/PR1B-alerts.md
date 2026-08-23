# PR1B — the score-free follow-up queue

**Files:** `db/migrations/013_alert_rules.sql`,
`n8n/workflows/02-calls-ingest-evaluate.json` (two new nodes),
`scripts/sql/02_evaluate_alert_rules.sql`,
`scripts/sql/02_reconcile_alert_evaluations.sql`,
`scripts/sql/acceptance_pr1b.sql`

Nothing here has been applied or deployed.

> **Revision 2 — the product changed.** This started as a *notification*
> feature: evaluate rules, POST a webhook, record whether the POST worked.
> Recordings reach us roughly **a day after the call**, so there is nothing
> real-time to notify anybody about. The webhook, the delivery recording and
> the `sent`/`failed` vocabulary are gone. What remains is a **queue that
> fills itself** and two views to read it from.
>
> The review also found three real problems that survive the redesign, all
> fixed here: `complaint_or_cancellation` was seeded as a high-confidence alert
> on a fact with no quote behind it; `promise_due` could never fire and would
> have missed its own window permanently even if it could; and two of the
> "tunable parameters" were decorative — the function hardcoded both gates and
> never read the keys.
>
> **Revision 3.** Reviewed again. PR1B could not roll out while PR1A was
> blocked, and three things here were part of that:
>
> 1. **Alert-queue persistence was best effort.** The node ran after the job was
>    terminal — and a terminal job is never recovered — while swallowing its own
>    errors into a leaf. Alert evaluation is now durable state
>    (`alerts_evaluated_at`), the node no longer swallows anything, and
>    `Reconcile alert evaluations` finishes whatever did not land. §5.
> 2. **013 was not safe on a database an earlier 013 had touched.** The seed's
>    `ON CONFLICT DO NOTHING` left `complaint_or_cancellation` at its old
>    `is_alert = true`, and both views changed column shape, which
>    `CREATE OR REPLACE VIEW` refuses. Both are handled explicitly now; the
>    preflight is in §8.
> 3. **The "every params key is read" test did not test that.** It was an
>    allowlist. §2, and §P of the acceptance file.

---

## 1 · Why no rule reads a score

The agent score is a judgement produced by a model against a rubric that is
still moving — prompt v3 changed it, the pass-1 validator changes it again. A
rule that fires on `final_score < 60` therefore fires on **prompt changes as
much as on reality**, and the first false positive teaches the sales floor to
ignore the queue. Once ignored it never comes back.

So every rule fires on *facts* from pass 1 — did the customer ask for a
product, did the agent promise something, what did they call about. Nothing
reads `agent_evaluations`.

### What "quote-validated" does and does not cover

The earlier version of this document, and of 013's header comment, said that
**every** fact used by every rule is one the pass-1 validator confirmed is
quoted verbatim. That was not true, and the exception was the rule most likely
to interrupt a supervisor.

- The two **hot-lead** rules *are* gated on the validator, because they assert
  that a customer said a specific sentence.
- `complaint_or_cancellation` is **not**, and cannot be: pass 1 v5 has no
  intent-evidence field, so the validator reports `null` for it and a gated
  rule would never fire. It asserts a *classification*, not a sentence — which
  is why it is now seeded as triage rather than as a finding (§3).
- `promise_open_or_overdue` reads `follow_ups`, which is materialised from
  validated promises upstream, but it is seeded inactive for unrelated reasons
  (§3).

Alerting also works when the judge does not: the alert node is reached from
**both** terminal transitions, `evaluated` and `judge_failed`, so a hot lead
with a verified quote reaches the queue even on a call whose pass 2 failed and
is waiting for a retry.

---

## 2 · Schema

### `alert_rules` — the catalogue

| column | meaning |
|---|---|
| `rule_code` (PK) | stable identifier |
| `rule_version` | bump to deliberately re-queue history under a new definition |
| `description` | what a human sees in `v_alert_queue` |
| `is_alert` | `false` records the occurrence as `suppressed` instead of queueing it — this is how you dry-run a rule against live traffic for a week and *measure its precision* before asking anyone to work its output |
| `active` | evaluate this rule at all |
| `params` | thresholds and gates, read by the function. Tuning a rule is an `UPDATE`, not a deploy |

Seeded with `ON CONFLICT DO NOTHING`, so re-running the migration never
overwrites a rule somebody tuned in production.

Every key in every seeded `params` object is a key the function actually reads.
A tunable nobody reads is worse than no tunable, because somebody will turn it
and believe the rule changed.

`scripts/sql/acceptance_pr1b.sql` used to "assert that mechanically" with a
hard-coded allowlist — `WHERE param_key NOT IN ('lead_temperatures', …)`. That
is a **spelling check**, not a proof: the allowlist is a copy of the intent, not
a reading of the function, and it would keep passing if every branch stopped
consulting `params` tomorrow. It is still there, relabelled as what it is, and
the actual proof is §P: eight blocks, one per declared key, each of which flips
the key on a fixture and asserts **the answer changes**. That is the B6 shape,
generalised — and it is the only kind of test that can tell a real parameter
from a decorative one.

### `call_ingest_jobs.alerts_evaluated_at` / `alerts_error`

Added by 013, on PR1A's table, because they describe the alert evaluation of one
job. `alerts_evaluated_at` is stamped in the **same statement** as a successful
`evaluate_alert_rules()` call, so it can never claim work that did not happen;
`alerts_error` is written by the reconciliation sweep from an exception handler,
so the *reason* survives even though the failed evaluation itself rolled back.
`alerts_evaluated_at IS NULL` on a terminal job is the follow-up queue's backlog,
and it is one query. See §5.

### `alert_occurrences` — one row per (rule, version, interaction, fact state)

`occurrence_hash` is what makes re-evaluation free. Workflow 02 re-runs the
rules every time a call reaches a terminal state — which happens again on every
judge retry — and the hash of the facts that fired the rule collapses those into
the row that already exists. Change the **facts** (a second promise appears, an
open promise goes from due-soon to overdue) and the hash changes, which is a
genuinely new thing to look at.

`delivery_status` is a **queue state, not a push state**:

| value | meaning |
|---|---|
| `pending` | in the follow-up queue, nobody has worked it |
| `acknowledged` | a human took it — `acknowledged_at`, `acknowledged_by`, `ack_note` |
| `suppressed` | the rule is in dry run (`is_alert = false`) |

There is no `sent` and no `failed`. Nothing is sent, so nothing can fail to
send. (013 carries an idempotent block that converts those two values and their
CHECK if a pre-release copy of the file ever reached a database. None has.)

---

## 3 · The four rules

`pass1_validation` is the object the worker's validator writes — it assigns it
**into** the payload (`judge.py: payload["pass1_validation"] = validation`) and
also lifts a copy out to `pass1.pass1_validation`; `Store pass1` merges the
sibling back in, so `interaction_analysis.raw_response->'pass1_validation'` is
present whichever side a future worker writes it to. A missing validation object
is treated as **false** wherever a rule is gated on it.

### `hot_real_ask_promised` — seeded active, `is_alert = true`
Fires when, in the raw pass-1 payload: `real_ask.is_real_inquiry = true`,
`commercial.lead_temperature` is `hot`, `pass1_validation.real_ask_quote_valid
= true`, at least one entry in `pass1_validation.promises` has
`quote_valid = true`, and `interactions.customer_phone_e164` is not null.

Meaning: a real, quotable inquiry where the agent promised to come back.
Somebody must actually come back.

### `hot_real_ask_uncommitted` — seeded active, `is_alert = true`
Same, but **zero** valid promises, and additionally requires
`asr_quality_status = 'green'`.

Meaning: the expensive one — the call ended with no next step and nothing else
in the pipeline will pick it up. The ASR-quality gate is not fussiness: on a red
or amber transcript "the agent promised nothing" may simply mean the promise was
in the audio we failed to decode, and accusing an agent of dropping a lead on
the strength of a bad microphone is how you lose the sales floor's trust in the
whole system.

### `complaint_or_cancellation` — seeded active, **`is_alert = false` (triage)**

Fires when `intent` is in `params->'intents'`, seeded `["complaint",
"cancellation"]`. These are exact values from pass 1's closed enum —
`price_inquiry, booking_request, availability_check, complaint, support,
modification, cancellation, general_info, other`
(`prompts/pass1_customer_v5.md`) — so this is set membership, never a substring
match on free text.

It is deliberately **not** gated on `intent_evidence_valid`, because pass 1 v5
has no intent-evidence field and a gated rule would never fire. That is exactly
why it does not go into anybody's working queue yet. Its description reads
*"Possible complaint/cancellation — review"*, not *"the customer called to
complain"*: the underlying claim is a model's classification with no quote
behind it, and the wording a supervisor reads should say so.

Seeded `is_alert = false`, it records `suppressed` occurrences that can be
sampled. `scripts/sql/acceptance_pr1b.sql` §B7 has the query. Read fifty by
hand; if the precision is good, flip `is_alert`. If it is not, the fix is a
quote in pass 1, not a lower bar here.

### `promise_open_or_overdue` — seeded **inactive**

Replaces `promise_due`, which had a fifth defect on top of the four documented
ones: it required `due_at > now()` inside a two-hour window, so **a sweep that
missed that window could never fire on that promise again**. With source data a
day old, that window is missed by definition.

The replacement fires per open `follow_ups` row for this interaction that is
either due within `params->>'due_within_hours'` (seeded 24) **or already
overdue** (`include_overdue`, seeded true). Undated promises are excluded by
default (`include_undated`, seeded false) and can be included with an `UPDATE`.
The overdue flag is part of the `occurrence_hash`, so a promise that was
"due soon" yesterday and is "overdue" today produces a new occurrence rather
than deduping against itself.

**It is seeded `active = false`, and activating it is not one switch.** All
three of the following must be true first:

1. **`follow_ups` rows must exist at all.** They are created by workflow 03
   (nightly), and workflow 03 is inactive. Nothing writes to the table today.
2. **Workflow 03's `Materialise promises` must stop excluding queue calls.** It
   requires `i.agent_id IS NOT NULL`, and `q` recordings have `agent_id = NULL`
   by design (the extension in a queue filename is the queue, not a person), so
   **no promise made on a queue-recorded call ever becomes a `follow_ups` row**.
   That is nearly the whole call corpus. Fixing it needs a decision about how a
   queue call gets attributed to a human; it is not in this PR.
   `acceptance_pr1b.sql` §B10 measures the size of the gap.
3. **Something must call `evaluate_alert_rules()` after materialisation.**
   Workflow 02 calls it when a *call* finalises; 03 runs nightly and creates
   `follow_ups` rows long after that. Nothing calls the function afterwards, so
   even with 1 and 2 fixed the rule would evaluate against a table that was
   empty when it was asked. What is missing is a scheduled sweep — roughly:

   ```sql
   SELECT o.rule_code, count(*)
     FROM follow_ups fu
    CROSS JOIN LATERAL evaluate_alert_rules(fu.promised_in) o
    WHERE fu.status = 'open'
      AND (fu.due_at < now() + interval '24 hours')
    GROUP BY 1;
   ```
   run once a day after 03. That sweep does not exist. **Activating workflow 03
   alone does not make this rule work** — this document used to imply it did.

Until all three land, the rule is inert *and inactive*, so it costs nothing and
claims nothing.

---

## 4 · `evaluate_alert_rules(p_interaction_id uuid)`

One call evaluates every **active** rule against one interaction, inserts what
fired with `ON CONFLICT DO NOTHING`, and returns **only the newly inserted
rows**. The caller therefore never has to ask "have I already handled this?" —
an empty result means everything that fired was already known. Re-running the
workflow over the same call is free and silent.

It reads `interaction_analysis.raw_response` (the pass-1 payload, the same shape
`v_real_asks` in 009 reads), `interactions`, `transcripts.asr_metrics` and
`follow_ups`. Nothing else.

`is_alert = false` produces the row with `delivery_status = 'suppressed'`, which
`v_alert_queue` excludes and `v_alert_digest_daily` still counts.

### The two gates are real now

`require_real_ask_quote_valid` and `require_valid_promise` were seeded as
tunable parameters while the function hardcoded both behaviours and never read
the keys. They are now resolved in a `LATERAL` next to each rule, and the same
resolved values drive the `WHERE` clause, the `occurrence_hash` and the
`fact_snapshot`:

| parameter | `true` (default) | `false` |
|---|---|---|
| `require_real_ask_quote_valid` | the real-ask quote must have passed validation | the rule fires on the extraction alone |
| `require_valid_promise` | count only promises whose quote passed validation | count every promise pass 1 extracted |

Turning `require_valid_promise` off does not weaken the rule into nonsense — it
changes *which population* it counts, on both sides at once: a call whose single
promise failed validation moves from `hot_real_ask_uncommitted` to
`hot_real_ask_promised`. That is the knob you want when measuring what the
validator is costing you. `acceptance_pr1b.sql` §B6 proves it moves.

---

## 5 · The workflow

```
… Store evaluation → Mark evaluated ─┐
                                     ├→ Job finalised? ──true──→ Evaluate alert rules  (leaf)
             … → Mark judge failed ──┘                 └─false→ (stop)

Every 15 min ──→ Reconcile alert evaluations  (leaf)
```

`Evaluate alert rules` runs the function, joins in the three things a human needs
that do not live on the occurrence (who the agent was, when the call was, which
recording it came from), stamps `alerts_evaluated_at`, and stops. Nothing is
POSTed anywhere.

### Why there is a second node

**This was a blocking review finding, and the fix is the interesting part.**

The first version ran the alert evaluation *after* the job became terminal, as a
leaf, with `onError: continueRegularOutput`. Every one of those three facts is
individually reasonable and together they lose data permanently:

- a terminal job is **never claimed again and never recovered** — the lease
  sweep only touches `transcribing` / `evaluating` — so nothing will ever come
  back to it;
- a leaf has nothing downstream to notice a zero result;
- `continueRegularOutput` turns a failure into a green execution.

So a dropped connection, a deadlock, or a bug in one rule's SQL silently deleted
that call's follow-up occurrences, and the run log said everything was fine.
"Best effort" is a fair description of the old behaviour and a bad property for
the thing whose entire job is to make sure a hot lead gets called back.

**The alternative we did not take.** Fold `evaluate_alert_rules()` into the same
statement as `Mark evaluated` / `Mark judge failed`: terminalize and insert the
occurrences atomically, return both. It is genuinely atomic — and it makes the
follow-up queue a *precondition for finishing a job*. One broken triage rule
would then roll back every terminalization, leave every job in `evaluating` until
its lease lapsed, and have the sweep re-queue each one for another DeepSeek run:
a cosmetic bug in the least important rule becomes a pipeline outage that spends
money every cycle. It also breaks the zero-row contract PR1A rests on — the
terminal node would emit one row per occurrence, so "no rule fired" and "the
lease is gone" would look identical to `Job finalised?`.

**What we did instead — durable state plus reconciliation.**

1. `alerts_evaluated_at` is stamped **in the same statement** as the evaluation.
   If the function raises, the statement aborts and the column stays null; there
   is no ordering in which the stamp can be a lie.
2. `Evaluate alert rules` no longer has an `onError` handler. A failure fails the
   execution, which is how anyone finds out.
3. `Reconcile alert evaluations` hangs off the **trigger** and calls
   `reconcile_alert_evaluations(200)`, which re-runs the rules for every terminal
   job whose `alerts_evaluated_at` is null. Re-running is free — the function
   inserts under `ON CONFLICT (rule_code, rule_version, interaction_id,
   occurrence_hash) DO NOTHING` — so the reconciliation is idempotent by
   construction, not by care.

It sits on the trigger rather than the claim chain for the same reason the claim
itself was moved there in PR1A: it has to run on a day with no claimable work.

`reconcile_alert_evaluations` is PL/pgSQL rather than one statement because one
statement over a batch has head-of-line blocking — a single interaction whose
data makes a rule raise would abort the whole batch, forever, and record nothing
about why. Each job gets its own `BEGIN … EXCEPTION` subtransaction: a poison row
writes its own `alerts_error`, is left with `alerts_evaluated_at` null so it is
retried, and the rest of the batch still lands. It takes its rows with
`FOR UPDATE SKIP LOCKED`, so it never waits for a job a live execution holds and
never joins the deadlock graph (PR1A §2, "Locking order").

The backlog is the monitor, and on a healthy pipeline it is zero after every
sweep:

```sql
SELECT count(*) FILTER (WHERE alerts_error IS NULL)     AS never_attempted,
       count(*) FILTER (WHERE alerts_error IS NOT NULL) AS failing,
       min(updated_at)                                  AS oldest
  FROM call_ingest_jobs
 WHERE status IN ('evaluated','judge_failed','dead_letter')
   AND interaction_id IS NOT NULL
   AND alerts_evaluated_at IS NULL;
```

**What this buys and what it costs.** Buys: no occurrence is lost by a transient
failure, and a permanent one is visible with its reason attached instead of being
a silent zero. Costs: alert evaluation is now *eventually* consistent — a job can
be terminal for up to one sweep interval before its occurrences exist. Given that
the recordings themselves arrive about a day late, fifteen minutes is not the
part of this pipeline anyone should be optimising.

`alwaysOutputData` stays on both nodes: an interaction that trips no rule, and an
empty reconciliation backlog, are the normal cases and the run log should say so.

### Why it hangs off `Job finalised?`

The first version fanned out from `Store pass1`: the scoring branch above, the
alert branch below, relying on `executionOrder: v1` running the topmost sibling
first so the job would be marked terminal before any webhook fired. The ordering
claim is true and it does not help. A **fenced** `Mark evaluated` that updates
zero rows — because this execution lost the lease — does not cancel its sibling
branch. A stale worker could therefore overwrite pass 1 and queue an alert after
it had stopped owning the row.

Now the alert node is reachable only through a gate that requires `Mark
evaluated` or `Mark judge failed` to have **returned a row**, which proves the
lease was held at that instant. The SQL adds a second fence for the gap between
the two statements: the job must still be in the terminal state this execution
just wrote, carrying no lease (`$3` is the status the terminal update RETURNed).
If somebody re-claimed the row in between, the node queues nothing.

As of revision 3 that second fence is a **lock**, not a check — `SELECT … FOR
UPDATE`, the same treatment every other durable write got (PR1A §2, "The fences
are locks, not checks"). Both the evaluation and the stamp read `FROM` that
locked row, because a data-modifying CTE cannot see the effects of a sibling and
a lock standing beside a write guarantees nothing about it. One shape detail
worth knowing if you ever edit that statement: the CTE that *calls*
`evaluate_alert_rules()` is not a data-modifying CTE — it is a `SELECT` over a
writing function — so unlike an `INSERT`/`UPDATE` CTE it is **not** guaranteed
to run if nothing reads it. The final `SELECT` must keep referencing it. The
`LEFT JOIN` is what lets a call that trips no rule still return its stamp row,
so zero rows from this node means one thing only: the fence failed.

---

## 6 · Reading the queue

### `v_alert_queue` — the working list

Everything in `pending`, newest first, with `customer_phone_e164`, `agent_name`,
`uniqueid`, `started_at`, and the fact columns lifted out of the snapshot
(`summary_ar`, `products`, `real_ask_quote`, `lead_temperature`,
`promise_overdue`) so nobody has to dig through JSON to make a phone call.

`fact_snapshot` is whatever the rule that fired chose to record, and it is what
the occurrence row stores — so reading it a week later shows the facts as they
were when the rule fired, not as they are now.

Working an item is an `UPDATE`, not a webhook:

```sql
UPDATE alert_occurrences
   SET delivery_status = 'acknowledged',
       acknowledged_at = now(),
       acknowledged_by = '<name>',
       ack_note        = '<what happened>'
 WHERE occurrence_id = '<occurrence_id>';
```

### `v_alert_digest_daily` — the once-a-day read

One row per **Riyadh day** per rule: `occurrences`, `pending`, `acknowledged`,
`suppressed`, and a `pending_items` jsonb array carrying phone, summary,
products, quote, promise text, due date and overdue flag per occurrence.

Intended for Metabase, or for a nightly workflow that renders one message per
day. It is a view, not a job: whoever reads it decides when, and re-reading it
changes nothing.

`alert_day` is the day the occurrence was **queued**, not the day of the call.
Recordings land about a day late and the person working the list cares about
what arrived on their desk today; each item carries the call's own `started_at`.
`Asia/Riyadh` is +03 with no DST, the same offset the worker's
`PORTAL_TZ_OFFSET_HOURS` defaults to.

```sql
SELECT alert_day, rule_code, pending, jsonb_array_length(pending_items) AS items
  FROM v_alert_digest_daily
 WHERE alert_day >= (now() AT TIME ZONE 'Asia/Riyadh')::date - 7
 ORDER BY alert_day DESC, rule_code;
```

`agent_name` is `unassigned` for every queue (`q`) recording, because `agent_id`
is deliberately null for those. That is honest, not a bug: see PR1A §5.

---

## 7 · Adding a rule

1. `INSERT INTO alert_rules (...) VALUES (...)` with `is_alert = false`.
2. Add a `UNION ALL` branch to `evaluate_alert_rules`, following the shape of
   the existing four: five columns, in order — `rule_code`, `rule_version`,
   `delivery_status` (`CASE WHEN r.is_alert THEN 'pending' ELSE 'suppressed'
   END`), `occurrence_hash`, `fact_snapshot`.
3. The `occurrence_hash` must include **every fact whose change should
   re-queue** and nothing else. Too much in it and the rule re-fires on noise;
   too little and a genuinely new situation is deduped away as already-seen.
4. Gate on the relevant `pass1_validation` flag if the rule asserts that a
   customer or agent *said* something. If it only asserts a classification, do
   not — and then seed it `is_alert = false` and hedge its wording, as
   `complaint_or_cancellation` does.
5. Every `params` key you add must be read by the branch you added — and you
   must **prove** it, not assert it. Add the key to the spelling check in §2 of
   `acceptance_pr1b.sql`, then add a block to §P that flips the key on a fixture
   and shows the answer changing. The allowlist alone would pass on a parameter
   the function ignores; that is exactly how the last two decorative parameters
   survived review.
6. Run it against history before switching it on:
   ```sql
   SELECT o.rule_code, count(*) FROM interactions i
   CROSS JOIN LATERAL evaluate_alert_rules(i.interaction_id) o
   WHERE i.started_at > now() - interval '30 days'
   GROUP BY 1;
   ```
   (this inserts suppressed rows — do it on a copy, or accept the backfill).
7. `UPDATE alert_rules SET is_alert = true WHERE rule_code = '…';`

Changing what an existing rule *means* — not just its threshold — is a
`rule_version` bump, which re-queues history under the new definition instead of
silently deduping against occurrences that meant something else.

---

## 8 · Rollout

Order: **013 after 012**, then the workflow. The full sequence, including
draining the old workflow first and the rollback, is in PR1A §6–7. The
PR1B-specific parts of it:

- **Step 5 verification** — `scripts/sql/acceptance_pr1b.sql` §1–4: the four
  rules with the seeded `is_alert`/`active` states above, zero unread `params`
  keys, the function, both views, and the three-value `delivery_status` CHECK.
- **Step 7** — `UPDATE alert_rules SET is_alert = false;` puts the two hot-lead
  rules into dry run alongside the two that are seeded that way. Let it collect
  for a week, then

  ```sql
  SELECT rule_code, count(*), min(created_at), max(created_at)
    FROM alert_occurrences GROUP BY rule_code ORDER BY 2 DESC;
  ```

  and read a sample of `fact_snapshot` before flipping `is_alert` back on.
- **`ALERT_WEBHOOK_URL` is no longer used.** Remove it from the n8n service.

### Preflight, if any pre-release copy of 013 has ever been applied

On a virgin database this section is a no-op and the migration is plainly
idempotent: the functions are `CREATE OR REPLACE`, the tables and columns are
`IF NOT EXISTS`, the seed is `ON CONFLICT DO NOTHING`. On a database that has
seen an earlier 013, "idempotent" was doing more work than it could carry, in
two specific places.

**The seed cannot correct a rule it is not allowed to overwrite.**
`ON CONFLICT DO NOTHING` is there so re-running the migration never clobbers a
rule somebody tuned in production — and that is also why an already-seeded
`complaint_or_cancellation` kept `is_alert = true`, which is precisely the value
this revision exists to change. 013 now carries an explicit `UPDATE` for it,
narrowed three ways so it cannot clobber deliberate tuning: one named rule, only
at `rule_version = 1`, and only when it is not already correct. It is a one-way
correction of a pre-release seed. **If you turned that rule on deliberately,
turn it back on afterwards and bump `rule_version`** so the next migration leaves
it alone. Note the before state:

```sql
SELECT rule_code, rule_version, is_alert, active, params
  FROM alert_rules ORDER BY rule_code;
```

The same block fills in a **missing** `require_asr_quality` key on
`hot_real_ask_uncommitted` with `'{…}'::jsonb || params`, which gives the stored
value precedence — a tuned value survives, only an absent key is added.

**Both views change column shape, so they are dropped and recreated.**
`CREATE OR REPLACE VIEW` can only append trailing columns; it refuses — "cannot
change name of view column" — when a column is removed, renamed, retyped or
reordered, and the pre-release `v_alert_queue` carried the push-era delivery
columns and no `promise_overdue`. On such a database a replace fails and leaves
the migration half-applied. 013 now does `DROP VIEW IF EXISTS` first. Nothing
should depend on either view; check before applying, because a dependency makes
the `DROP` fail (deliberately — no `CASCADE`):

```sql
SELECT dependent_view.relname
  FROM pg_depend d
  JOIN pg_rewrite r            ON r.oid = d.objid
  JOIN pg_class dependent_view ON dependent_view.oid = r.ev_class
  JOIN pg_class source_table   ON source_table.oid = d.refobjid
 WHERE source_table.relname IN ('v_alert_queue','v_alert_digest_daily')
   AND dependent_view.relname NOT IN ('v_alert_queue','v_alert_digest_daily');
```

Anything reading these views — a Metabase question, a saved query — is
unaffected by the drop-and-recreate as long as it reads them by name.

### After activation: watch the backlog, not the queue

The queue filling up is the feature. The thing to watch is the **backlog of
calls whose rules never ran**, which should be zero after every sweep:

```sql
SELECT count(*) FILTER (WHERE alerts_error IS NULL)     AS never_attempted,
       count(*) FILTER (WHERE alerts_error IS NOT NULL) AS failing,
       min(updated_at)                                  AS oldest
  FROM call_ingest_jobs
 WHERE status IN ('evaluated','judge_failed','dead_letter')
   AND interaction_id IS NOT NULL
   AND alerts_evaluated_at IS NULL;
```

`failing > 0` means `reconcile_alert_evaluations()` caught a real error and
recorded it — read `alerts_error`. A `never_attempted` count that grows without
`failing` growing means the reconciliation node itself is not running.

---

## 9 · Acceptance tests

Full SQL in `scripts/sql/acceptance_pr1b.sql`. Every behavioural test builds its
own interaction and rolls it back.

**None of it has been run.** The file says so in its opening lines and that is
still true — nothing in PR1A or PR1B has touched a database. §1–4 and §P are
runbook step 5; B11–B12 are rollout gates, run on staging or a restored copy
after runbook step 4, with the full block in `acceptance_pr1a.sql` A14.

The previous version's B1 read "call the function twice on the newest row in
`v_real_asks`; the second call must return zero". That passes with **0 then 0**
whenever the chosen row trips no rule — i.e. it passes when the rules are
completely broken. Every test below therefore starts from a fixture whose facts
make exactly one named rule fire, and the tests that assert a rule must *not*
fire clear `alert_occurrences` for that interaction first, so that ordinary
deduplication cannot masquerade as a working gate.

| | test |
|---|---|
| **B1** | A rule fires exactly once — and it definitely fires the first time. First call returns exactly `hot_real_ask_promised / pending`; second returns zero; one occurrence row. |
| **B2** | Changing the facts re-queues: a second valid promise produces a different `occurrence_hash`. |
| **B3** | An unvalidated quote never reaches the queue. Clean occurrence table first. |
| **B4** | A red transcript produces no "uncommitted" alert — asserting **both** halves: green fires, red does not. |
| **B5** | Suppression records evidence without filling the queue: `suppressed`, absent from `v_alert_queue`, still counted by `v_alert_digest_daily`. |
| **B6** | The two gates are real: flipping `require_valid_promise` moves the same call from `hot_real_ask_uncommitted` to `hot_real_ask_promised`. |
| **B7** | `complaint_or_cancellation` is triage: on a freshly seeded database its occurrence is `suppressed`, plus the sampling query to measure precision before enabling it. |
| **B8** | The queue survives a pass-2 failure: `judge_failed` jobs may legitimately have occurrences. |
| **B9** | A stale worker cannot queue an alert — cross-referenced to `acceptance_pr1a.sql` A3e rather than duplicated. |
| **B10** | `promise_open_or_overdue` is inactive on purpose, and the three reasons are measurable: `follow_ups` is empty, the null-agent exclusion is sized, and no sweep exists. |
| **B11** | **An injected alert-function failure loses nothing — and it is inspectable.** Full block in `acceptance_pr1a.sql` A14: one transaction driven by `SAVEPOINT`s, so `ROLLBACK TO SAVEPOINT` undoes only the failed call and leaves the fixture in place to be read. A **positive control** first (real function → one occurrence, stamp set), because otherwise the next step "passes" whenever the *fence* fails to match. Then break `evaluate_alert_rules()` inside the transaction, run the alert node's statement, confirm it **raises** — and then actually `SELECT alerts_evaluated_at IS NULL` and `alerts_error` on the surviving fixture row. The earlier revision aborted and rolled back the fixture along with the failure, so it could assert nothing at all. |
| **B12** | **A poison row does not block the batch, and says why.** The poison is **data, not a broken function**: `"maybe"` where `evaluate_alert_rules()` casts to boolean, so the *real* function raises 22P02 for one interaction only — a globally broken function would take the healthy job down with it and could not show this at all. The poison job carries an **older `updated_at`** than a healthy one, because the sweep works oldest-first and head-of-line blocking is only tested if the poison is in front. `reconcile_alert_evaluations()` must not raise: `error_text` for the poison, `alerts_error` written, `alerts_evaluated_at` left null so it is retried — and the healthy job behind it stamped in the same batch. |
| **§P** | **Every declared `params` key is read** — eight blocks, one per key, each flipping the key on a fixture and asserting the result changes. This replaces the allowlist in §2, which is kept but relabelled as the spelling check it always was. |
