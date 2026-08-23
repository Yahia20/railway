# PR2 (DB side) — evaluation statuses, the usable-score rule, and how a number gets published

**Files:** `db/migrations/014_evaluation_status.sql`,
`n8n/workflows/02-calls-ingest-evaluate.json` (nodes *Store evaluation*,
*Pass 2 usable?*, *Nothing to evaluate?*, the new *Store unscoreable outcome*
and *Unscoreable stored?*, and *Mark unscoreable*),
`scripts/sql/02_store_evaluation.sql`,
`scripts/sql/02_store_unscoreable_outcome.sql`,
`scripts/sql/02_mark_unscoreable.sql`,
`scripts/sql/acceptance_014_status.sql`

Nothing here has been applied or deployed. 014 has not been run against any
database and the live workflow has not been touched.

> This is the **consumer half** of PR2. The worker half — the prompt, the
> validator, the fingerprint capture — is `docs/PR2-judge-integrity.md` and is
> owned separately. This document only covers what the database and the
> reporting layer do with what the worker returns.

> **Round 4.** Three findings from the round-4 review are answered here and are
> flagged where they land:
>
> 1. **The judge-noise floor was being published as if it were a confidence
>    interval**, and used as the sole `band_stable` gate. A lower bound on the
>    uncertainty cannot prove a band is stable. §3, rule 2.
> 2. **The noise parameter was global while the means were version-grouped**, so
>    re-measuring after a prompt change would have retroactively re-stated the
>    uncertainty of every historical version group. It is now keyed by the
>    co-ordinate it was measured on. §3, rule 2.
> 3. **The `unscoreable` refusal was routed to dead-letter with no evaluation
>    row**, which made `unscoreable_count` unreachable for the commonest way a
>    call becomes unscoreable. The row is now stored first. §2.
>
> Plus apply-safety: 014 runs in one transaction, preserves the two views' owner
> and grants across the DROP, and no longer rewrites `updated_at` on the whole
> null-score history. §4.

> **D1 rollout rule.** Sol's "amber ASR is shadow-only" rule is folded into 014
> **before** it is applied anywhere: a call evaluation counts towards a
> published number only when its transcript came back `'green'`. §3, rule 4.

---

## 1 · What was wrong

`agent_evaluations` had one row per evaluated interaction and every consumer
read that row as *a score*. It is not. Pass 2 returns four different things:

| `contract_status` | means | is there a number? | retryable? |
|---|---|---|---|
| `ok` | the rubric was applied | yes | n/a |
| `contract_failed` | the response contradicted the rubric after the re-ask; the scoring engine refused to compute anything | no | **yes** — judge fault |
| `ungradeable` | too little of the rubric survived evidence checking to average | no | **no** — terminal data quality |
| `unscoreable` | there was nothing to evaluate; the transcript held less speech than the scoring minimum | no | **no** — terminal data quality |

Two consequences, both of which were live:

1. **The denominator and the numerator described different populations.**
   `v_agent_scorecard` reported `count(*)` as `evaluated_interactions` and
   `avg(final_score)` as `avg_score`. `count(*)` includes the null-score rows;
   `avg()` silently skips them. A day on which the ASR fell over read as
   *"40 interactions evaluated, average 78"* when the truth was *"12 scored,
   average 78, and 28 we could not grade"*. Nothing on the page said so.

2. **Status lived only inside `raw_response`.** A consumer that wanted to know
   whether a row carried a score had to reach into a jsonb blob and guess at
   the key. Most did not bother.

The reviewer's requirement (Sol, section 4):

> A score is usable only when `contract_status == "ok"`, `gradeable == true`,
> and `final_score != null`. Store status first-class in `agent_evaluations`,
> not only inside JSON. Report `scored_interactions = count(final_score)` and
> separate ungradeable/unscoreable counts.

---

## 2 · Evaluation statuses & the usable-score rule

### The rule, defined once

```sql
eval_score_is_usable(contract_status, gradeable, final_score)
  =  contract_status = 'ok' AND gradeable AND final_score IS NOT NULL
```

It is an `IMMUTABLE` SQL function, and it is the **only** place that rule is
written down. Every view calls it. `v_usable_evaluations` is the same rule in
row form for consumers that want the population rather than the predicate.

Restating the rule by hand is the failure mode this is built against: four
copies means one of them drifts, and the drift is silent — a report that
quietly includes ungradeable rows looks exactly like a report that does not.

It is deliberately **not** used in an index predicate. A functional index would
freeze the definition into stored index entries, and changing the function
would silently corrupt them.

### The five buckets

Every row lands in exactly one of these, and they sum to the row count:

```
scored_interactions      usable: ok + gradeable + a score
ungradeable_count        contract_status = 'ungradeable'
unscoreable_count        contract_status = 'unscoreable'
contract_failed_count    contract_status = 'contract_failed'   (expected: 0)
ok_without_score_count   'ok' but not usable — pre-013 history, or a writer
                         that does not send status
```

`contract_failed_count` should be **zero**: workflow 02 routes that status to
*Mark judge failed* and writes no row at all. A non-zero value means some other
writer stored a score the scoring engine explicitly refused to stand behind.

`unscoreable_count` should be **non-zero within days of rollout**. It is now
written from both directions — see below — and a persistent zero, on a queue
that shows dead-lettered `unscoreable:` jobs, means the store node is not
running. Acceptance query **R10** is the reconciliation.

`ok_without_score_count` should be **flat** after rollout. It is the backfilled
pre-013 history: rows with no score, for which we cannot retroactively say
whether they were ungradeable or unscoreable — that distinction did not exist
when they were written, and inventing it would put a measurement in the
database that nobody made. If it keeps growing, a writer is not sending status
(workflows 01 / 01b are the candidates — see §5).

Acceptance queries **R1** and **R2** in `scripts/sql/acceptance_014_status.sql`
assert the partition, globally and per scorecard row.

`amber_shadow_count` is **not** one of the five and is deliberately not in the
sum: a row excluded by rule 4 below was never in `evaluated_interactions` at
all. It is reported beside the buckets so the size of what was left out is on
the page, and adding it to the identity would make every row with a bad-ASR
call read `reconciles = false`.

### Ungradeable is terminal, not a judge failure

Workflow 02's *Pass 2 usable?* gate routes:

```
ok | ungradeable | unscoreable   -> Store evaluation   (row is written)
contract_failed | unknown status | missing pass2  -> Mark judge failed (retried)
```

Retrying an ungradeable call re-asks a model that already answered. It spends
one of five judge attempts, and the only way it can "succeed" is by
manufacturing a score that the evidence does not support. The row is stored
instead, with `final_score` null, `gradeable` false and the status in its own
column, so it is *counted as ungradeable* rather than averaged or silently
dropped from a denominator.

An **unknown** status fails closed and is retried. A worker newer than this
workflow must not have its new status stored as if it meant `ok` — and 014's
`CHECK` would reject it anyway, so failing at the gate gives a retry and a
readable `last_error` instead of a hard SQL error mid-batch.

A **missing** `contract_status` is treated as `ok`: an older worker simply did
not send the field, and 014 gives the column the same default.

### `unscoreable` is stored, then terminalised — never retried

**Round 4 reversed the previous decision here.** The earlier revision routed the
worker's pre-pass-1 refusal straight to dead-letter and wrote no evaluation row,
on the reasoning that "there is no evaluation". The reviewer disagreed, and was
right: the worker deliberately returns a **complete, storable pass-2 refusal**
(`services/worker/app/main.py`, `_unscoreable()`) carrying `rubric_version`,
`prompt_version`, a named model placeholder, `contract_status = 'unscoreable'`,
`gradeable = false`, `final_score = null` and the reason in `warnings[0]`.
Dropping it hid the **worst input-quality cases** — the ones most worth seeing —
from both reporting views, and left `unscoreable_count` populated by nothing.
Storage policy must not depend on whether pass 1 happened to run.

The worker's refusal short-circuits `/evaluate` **before** pass 1 — the
transcript held less speech than the scoring minimum, so there is nothing for
either pass to read. The response is a 200 carrying a `pass2` block and **no
`pass1`**.

`Evaluation ok?` tests `!error && !!pass1`, so it takes its **false** output on
a response that is not an error at all, and that output used to go straight to
`Mark judge failed`. The row was then retried: one judge attempt every 45
minutes until the budget of five was gone, and then `dead_letter` anyway. Five
model calls that were never going to be made, to reach the same state — and
Sol's rule is explicit that this class is terminal, not retryable.

The path is now four nodes:

```
Evaluation ok? (false)
  -> Nothing to evaluate?          contract_status = 'unscoreable' and no error?
       true  -> Store unscoreable outcome   one agent_evaluations row, fenced
              -> Unscoreable stored?        zero rows = hard stop
              -> Mark unscoreable           terminal dead_letter, no retry
       false -> Mark judge failed           unchanged, still retried
```

- **`Store unscoreable outcome`** writes one row: `contract_status`
  `'unscoreable'`, `gradeable` false, `final_score` null, `prompt_version` /
  `rubric_version` / `model` taken from the response itself, the reason in
  `notes`, and every score-shaped column explicitly null or empty. It is
  lease-fenced exactly like `Store evaluation` — `AS MATERIALIZED` lease CTE,
  `FOR UPDATE`, `claim_until > now()`, `RETURNING`. Acceptance query **R11**
  asserts that nothing score-shaped got in.
- **`Unscoreable stored?`** turns a zero-row fencing result into a hard stop.
  Zero rows means the lease is gone and somebody else owns the job; we do not
  go on to dead-letter a row we no longer hold. The false output is a leaf,
  exactly like `Pass 1 stored?` and `Transcript stored?`.
- **`Mark unscoreable`** then dead-letters: lease cleared, `last_error` =
  `unscoreable: ` plus the worker's own reason, no retry.

**Why the store node comes first.** A job terminalised before the row is written
would, on a crash in between, be a `dead_letter` with no evaluation — exactly
the state this change removes. Writing first and terminalising second means the
worst case is a stored outcome whose job is still `evaluating`, which the
recovery sweep and the queue already know how to finish.

`Mark unscoreable` now has two callers and reads its reason from `$json`. On the
ASR path `$json` is the quality verdict; on the pass-2 path it is the row
returned by `Store unscoreable outcome`, which is why that node returns
`notes AS unscoreable_reason`. It **cannot** read `$('Two AI passes')` — the
node is shared with the ASR-quality path, where that node never ran, and the
validator's branch-isolation rule forbids it. The reason therefore travels
*through* the write rather than around it. The lease fence, the token guard and
`status IN ('transcribing','evaluating')` are unchanged and cover both entries:
the ASR path arrives holding a `transcribing` lease, the pass-2 path an
`evaluating` one.

**Two prefixes, two different outcomes.** `asr_quality_red:` is the ASR gate —
too much of the audio is unaccounted for to score anybody on what is left. That
path stores a *transcript* and no evaluation, because there was never a pass 2
to record. `unscoreable:` is the pass-2 refusal, and it now always has an
evaluation row beside it. **R9** and **R10** keep them apart.

### The invariant both store nodes maintain

`gradeable` is written as *what the worker said* **AND** *a score actually
arrived*:

```sql
coalesce((d->'pass2'->>'gradeable')::boolean, true)
  AND (d->'pass2'->>'final_score') IS NOT NULL
```

A response claiming `gradeable = true` with a null `final_score` is
self-contradictory, and storing it that way would create a row in an
"ok, graded, but no number" state that no bucket covers. This keeps the
invariant 014's backfill establishes: **gradeable implies `final_score IS NOT
NULL`**. `Store unscoreable outcome` uses the same expression rather than a
hard-coded `false`, so there is one rule and not two. Acceptance query 4
asserts it.

`model_fingerprint` is read from `pass2.usage.system_fingerprint`, falling back
to `pass2.system_fingerprint` and `pass2.model_fingerprint` — the field has
moved once already, and a fingerprint silently landing as null would look
exactly like a provider that does not send one. On an unscoreable row it is
legitimately null: no model was called, so there is no fingerprint to capture.

---

## 3 · Reporting rules

These are not style preferences. Each one exists because the data already
violated it.

### Rule 1 — never compare means across prompt versions

`v_agent_scorecard` groups by `agent_id` **and** `prompt_version`,
`rubric_version`, `model`, `model_fingerprint`. One agent gets **one row per
version co-ordinate**.

The day-13 data put the prompt-attributable RMS at **14.15 points** — a whole
performance band. A mean that mixes two prompt versions is the mean of two
different measurements. Grouping by version makes a prompt change show up as a
new row starting at N=1, rather than as a mysterious drift in an old one.

Two rows are comparable only if all four version columns match. Acceptance
query **R6** lists every agent whose history spans a version change: for those,
`sum(n_usable)` across rows is **not** a valid N.

A **fingerprint change is a version change.** DeepSeek documents no seed and
does not promise a stable model behind an alias; the returned
`system_fingerprint` is the only signal that the thing being measured moved.
Query **R7** watches for `distinct_fingerprints` going from 1 to 2.

### Rule 2 — the interval is complete, and the noise floor is versioned

**Round 4 blocked the first version of this rule on two counts.** Both are
fixed here, and both changed the answer the view gives.

**(a) The floor is not the interval.** From the day-13 A/A run — the same
prompts, the same code, the same 81 calls, judged twice — the variance of the
repeat-run difference is **188.70**, which gives

```
judge-noise floor = 1.96 × √(188.70 / N) = ±26.9/√N
```

| N | noise floor |
|---|---|
| 30 | ±4.92 |
| 81 | ±2.99 |
| 182 | ±2.00 |

The first draft published *that* as `ci_half_width_95` and gated `band_stable`
on it. It is a **lower bound on the uncertainty**, not a confidence interval: it
captures how much the judge moves when asked twice, and says nothing about
*which calls an agent happened to take that month*. Passing a lower-bound test
does not prove a band is stable. The draft's own documentation admitted the
omission while the SQL went on gating on it.

The standard error is now built from both:

```
se               = √( max(sample_var, noise_var) / N )
ci95_half_width  = z₉₅ × se
```

where `sample_var` is that group's observed `var_samp()` over its usable
scores. **`max()`, not a sum** — the judge's run-to-run noise is already inside
the observed spread of real scores, so adding them would count it twice. Taking
the larger keeps the floor for a group whose scores happen to sit close together
by luck, and lets the real spread dominate whenever it exceeds the floor, which
on live data it usually will.

The columns are named for what they are:

| column | what it is |
|---|---|
| `noise_floor_half_width` | the judge-repeat floor alone — informational |
| `ci95_half_width` | the complete interval — **this is the gate** |
| `score_sample_variance` | the observed `var_samp` behind it |
| `noise_variance` | the measured floor variance for this co-ordinate |

Acceptance query **R5b** prints the gap between the two half-widths per row:
that gap is exactly how much the first draft understated every interval.

**(b) The noise measurement belongs to the co-ordinate it was measured on.**
The first draft held **one global** `repeat_run_variance` row while the views
grouped means by four version columns. Re-measuring the noise after a prompt or
model change — which is precisely when you must re-measure it — would then have
retroactively re-stated the uncertainty of every historical version group with a
number that was never measured on it.

`eval_noise_params` is now keyed by `(param_key, prompt_version,
rubric_version, model, model_fingerprint)`, with a **NULL fingerprint meaning
wildcard** and an exact-fingerprint row winning over it. Each view row looks the
variance up on **its own grouping co-ordinate**.

The seeded row is bound to where 188.70 actually came from:

| column | value |
|---|---|
| `prompt_version` | `pass2-agent-quality-v3` (the A/A ran the old pass-2 prompt on both sides; pass 1 was `pass1-customer-v4`) |
| `rubric_version` | `1.0.0` |
| `model` | `deepseek-chat` (the legacy alias in use that day) |
| `model_fingerprint` | NULL — none was captured then |
| `value` | 188.70, measured 2026-08-13 |

**No match is not zero.** The lookup returns NULL, both half-widths return NULL,
and `band_stable` reads false.

> **Read this before asking why nothing is publishable after rollout.** The
> worker ships whatever `judge.PASS2_VERSION` currently is —
> `pass2-agent-quality-v6` as this is written, and it has moved twice since the
> A/A run — against `deepseek-v4-flash`. That
> co-ordinate has **no measured noise floor**, so every scorecard row will read
> `noise_variance` NULL, `ci95_half_width` NULL and `band_stable` false until
> somebody repeats the A/A run on the shipping prompt and model and `INSERT`s
> the resulting variance. That is intended and it is the honest behaviour: the
> alternative is reusing a two-prompt-generations-old measurement as if it
> described the judge that is actually running. Acceptance query **R5** prints
> exactly this reason on every row.

The co-ordinate-free constants live in a **second** table, `eval_report_params`,
because they do not depend on which prompt or model produced a score:

| `param_key` | value | why |
|---|---|---|
| `z_95` | 1.959964 | normal, two-sided |
| `min_n_publish` | 30 | reviewer decision |

**Below N = 30 a mean is not a measurement** and no band is published. At N=30
the noise floor *alone* is ±4.9 points — a third of a band.

Both tables exist rather than literals in the view text for the same reason
`alert_rules` holds thresholds: re-measuring is an `INSERT`, not a deploy. A
re-measurement is always a **new row on a new co-ordinate** — never an edit of
an old row to mean something else.

**Honest about the statistics, still.** 188.70 is the variance of the
*difference* between two runs, which for independent runs is about twice the
variance of one run. Used directly as a per-call variance it makes the floor
roughly √2 **wider** than a single run's judge noise. That is the direction to
be wrong in. And a floor is still only a floor: nothing here models systematic
semantic error, which survives repetition entirely.

If a parameter row is missing, the functions return NULL and every band gate
reads false. **Fail closed:** removing a constant hides bands, it does not
invent certainty.

### Rule 3 — a band is published only when the interval stays inside it

`band_stable` is true only when *all* of:

- `n_usable >= min_n_publish`
- there is a mean at all
- `ci95_half_width` is not NULL — i.e. this co-ordinate has a measured floor
- `eval_performance_band(mean − ci95) = eval_performance_band(mean + ci95)`

and the whole expression is `coalesce(..., false)`, so a missing parameter row
cannot make it NULL and slip past a truthiness test somewhere downstream.

Bands are 85 / 70 / 55, mirroring `performance_level()` in
`services/worker/app/evaluate/scoring.py`. SQL cannot import Python, so
acceptance query **R4** pins the two copies against each other. The band is
computed from the **unrounded** mean: rounding before the comparison is how a
mean lands on the wrong side of 85.

Why the gate exists: **11 of 68 bands flipped in the A/A run with no prompt
change at all.** A band whose interval straddles a boundary is a coin flip
presented as a grade — and it is presented to a person about their own work.

Query **R5** is the "what may I show today" query: it prints every scorecard
row with `publishable` and the reason it is or is not.

### Rule 4 — amber ASR is shadow-only

Sol's D1 rollout rule. A call evaluation reaches a published number only when
its transcript's `asr_metrics->>'asr_quality_status'` is exactly `'green'`;
chats are always eligible. Amber, red, an unrecognised status, a transcript with
no `asr_metrics` and a call with no transcript row at all are **all excluded**
from `evaluated_interactions`, every average, `n_usable`, both half-widths and
`band_stable` in `v_agent_scorecard` and `v_quality_by_input`, and from
`v_usable_evaluations`. The rows are still written and still stored — *shadow*
means excluded from the reporting layer, not deleted — so shadow analysis reads
`agent_evaluations` directly. The rule is one function,
`eval_asr_input_is_eligible(input_type, asr_quality_status)`, for the same
reason `eval_score_is_usable()` is one function: three hand-written copies is
two chances to drift silently. Each view still writes out Sol's
`LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id`, which can
match at most one row because `transcripts.interaction_id` is `NOT NULL UNIQUE`
(`003_interactions.sql`) — there is no "current transcript" to choose, and
acceptance checks **6c/6d** fail loudly if a later migration relaxes that.

The rationale is that `eval_score_is_usable()` cannot see this failure. An amber
transcript returns text for every chunk, so nothing upstream refuses it and pass
2 applies the whole rubric — to the words it was given rather than the words
that were spoken. The output is a perfectly well-formed score about a
conversation that did not happen that way, and averaging it puts ASR error into
a number presented to a person about their own work. Note this defaults the
**opposite** way to `evaluate_alert_rules()` in `013_alert_rules.sql`, which
coalesces a missing status to `'green'`: an alert on a call whose quality nobody
recorded is still worth a human's attention, whereas a *grade* on one is not, so
the scorecard fails closed and unknown means excluded. The cost is made visible
rather than absorbed — `amber_shadow_count` and `amber_shadow_usable_count` sit
beside the buckets on every row (they are **outside** the five-bucket partition,
which still covers `evaluated_interactions` exactly), and both views take their
group universe **before** the filter, so an agent whose entire month was amber
appears reading `evaluated_interactions = 0` instead of vanishing. Preflight
**P2c** prices the exclusion before you apply anything; **R2c**, **R2d** and
**P6** are the after-queries.

---

## 4 · Rollout

014 slots into the PR1A runbook (`docs/PR1A-leases.md` §6) as **step 4c**, and
into the acceptance sweep at **step 5**. Order:

```
012_call_job_leases.sql   ->  013_alert_rules.sql  ->  014_evaluation_status.sql
```

014 must come **after** the workflow is deactivated and drained (PR1A steps 1–2)
for the same reason 012 does: it drops and recreates two views, and the old
workflow writes rows that the new views count.

### It runs in one transaction

The file opens with `BEGIN;` and ends with `COMMIT;`. It drops two views and
recreates them; a half-applied 014 leaves the reporting layer with **no
scorecard at all**. If the runner already wraps the script in a transaction —
n8n's Postgres node sends a multi-statement query as one implicit transaction —
the explicit `BEGIN` is harmless. If it does not (`psql -f`, which autocommits
statement by statement), the explicit `BEGIN` is the only thing making this
safe. Re-running the whole file is still safe: `ADD COLUMN IF NOT EXISTS`,
guarded constraint adds, a naturally re-runnable backfill,
`CREATE OR REPLACE FUNCTION`, `ON CONFLICT DO NOTHING` seeds.

### Preflight

- **P1 — dependants.** 014 `DROP`s `v_agent_scorecard` and `v_quality_by_input`
  — their column shape changes, and `CREATE OR REPLACE` can only append
  columns. The `DROP` is a plain `DROP`, never `CASCADE`: if anything depends on
  either view the migration **must fail loudly** rather than silently delete
  somebody's report. Expect zero rows.
- **P2 / P2b / P2c — the backfill population, like for like.** The earlier
  revision compared `count(*) − count(final_score)` over the **whole table**
  against `ok_without_score_count` summed over the scorecard. Those are
  different populations and the claim was wrong: the scorecard inner-joins
  `interactions` and `agents` and filters `is_bot = false`, so a null-agent row,
  a bot row or an orphaned interaction is simply not in it. **P2b** computes the
  numbers over exactly the rows the scorecard sees — including rule 4's ASR
  gate, so it also reports `asr_shadowed`, which must match
  `sum(amber_shadow_count)` — and it is the one that must match
  `sum(ok_without_score_count)` (query **R2b**). **P2c** counts the remainder so
  the gap is a number somebody looked at rather than a discrepancy somebody
  explains away later; since rule 4 it also breaks the call population down by
  ASR status, which is how you find out **before** applying 014 how much of the
  call history stops being published the moment it lands. If that is most of the
  corpus, the answer is to re-transcribe, not to weaken the rule.
- **P3 — view owner and ACLs.** `DROP` + `CREATE` makes **new objects**, and new
  objects have no grants. `CREATE OR REPLACE` would have preserved them; `DROP`
  does not, so a reporting role that could `SELECT` these views this morning
  loses that privilege silently and finds out from a broken dashboard. 014
  snapshots owner and full ACL into a temp table before the `DROP` and replays
  them after the `CREATE` (including `WITH GRANT OPTION` and `PUBLIC` grants),
  inside the same transaction — but a temp table is not a record. Run P3, keep
  the output, and compare it to **section 7** afterwards.
- **P4 — can the migration role do this?** 014 disables and re-enables
  `t_eval_updated` around the backfill (needs ownership of `agent_evaluations`)
  and re-owns the two views. `can_set_owner` must be true on all three; a false
  means the transaction will abort halfway, which is safe but avoidable.

### Order of operations inside 014

1. add the five columns
2. add the named `contract_status` CHECK
3. **disable `t_eval_updated`**, backfill `gradeable = false` where
   `final_score IS NULL`, **re-enable it**
4. index `(contract_status, gradeable)`
5. define `eval_score_is_usable`, `eval_performance_band`, `v_usable_evaluations`
6. create and seed `eval_report_params` and `eval_noise_params`, define
   `eval_report_param`, `eval_noise_param`,
   `eval_noise_floor_half_width_95`, `eval_ci_half_width_95`
7. snapshot view owner/ACLs, drop and recreate the two reporting views, replay
   owner and grants, drop the snapshot

**Why step 3 disables the trigger.** `t_eval_updated` (004_ai.sql) rewrites
`updated_at` on every `UPDATE`. An unguarded backfill would stamp migration day
onto the whole null-score history and destroy the only record of when each
evaluation was actually last written — which is the column §6 reads to tell
"pre-013 history" from "a writer that is still not sending status". Nothing else
in the transaction writes to `agent_evaluations`, the trigger is re-enabled
immediately, and a failure anywhere below rolls the disable back with everything
else. Acceptance check **3b** asserts the trigger is back on and that the
history was not stamped.

### Acceptance

Run `scripts/sql/acceptance_014_status.sql`, sections 1–7 and R1–R11, alongside
the PR1A/PR1B sweeps. The ones that must be **looked at**, not just run:

- **4** — `gradeable = true AND final_score IS NULL` must be **0**.
- **3b** — the trigger is enabled again, and the history was not re-stamped.
- **6b** — `eval_asr_input_is_eligible()` fails closed: only literally
  `'green'` passes on a call, and NULL / unknown / red / a missing transcript
  all read false.
- **6c / 6d** — one transcript per interaction, so the D1 join cannot fan an
  evaluation out. Zero here means the views are wrong, not the check.
- **5b** — the co-ordinate binding: 188.70 answers for
  `pass2-agent-quality-v3 / 1.0.0 / deepseek-chat` and returns **NULL** for the
  shipping co-ordinate. NULL there is correct.
- **5c** — the complete CI takes the larger of the two variances, and returns
  NULL for an unmeasured co-ordinate.
- **7** — the two views' owner and ACL match preflight P3 exactly.
- **R1** — the counts query. `buckets_partition_total` must be true;
  `contract_failed` must be 0.
- **R2 / R2b** — per-agent reconciliation, and the scorecard total against P2b.
- **R2c / R2d** — rule 4 priced per agent, and the groups that now exist
  only in shadow (`evaluated_interactions = 0`). Rows in **R2d** are not errors;
  they are people about whose month the reporting layer is correctly silent, and
  call audio worth re-running.
- **P6** — the D1 fixture. `BEGIN ... ROLLBACK`, builds its own agent,
  interactions, transcripts and evaluations, depends on no production data, and
  proves all four cases at once: chat present, green call present, amber call
  absent, missing `asr_metrics` absent — with all four rows carrying a
  *usable* score, so it can only pass because of rule 4.
- **R10** — every `unscoreable:` dead-lettered job has exactly one stored
  `unscoreable` evaluation row.
- **F1** — the unscoreable fixture, run as **SQL integration, not end-to-end
  n8n** (round-5 review finding F16): one stored status outcome, a terminal
  job, one judge attempt, no retry. The four statements workflow 02 sends
  (`02_claim_work` → `02_begin_judge_attempt` → `02_store_unscoreable_outcome`
  → `02_mark_unscoreable`) go straight at the database, in the workflow's order
  and with the workflow's parameters, so what is proved is the **SQL
  contract**. Node wiring, the IF branches, the expression that builds the
  reason string and the credential the nodes run under are all outside it —
  the true end-to-end is **G2**, running the workflow against staging.

### Rollback

014 is additive to the table and destructive only to two views. There is
nothing to undo on `agent_evaluations`: the columns are invisible to any writer
that does not name them, and dropping them would destroy the status history the
next attempt needs. To restore the old reporting shape, re-apply the
`v_agent_scorecard` / `v_quality_by_input` definitions from
`db/migrations/007_views.sql` — but understand that doing so restores the
counting bug in §1 along with them, **and** that it will not restore the grants:
re-`GRANT` from the P3 snapshot by hand.

---

## 5 · What this does not fix

- **Workflows 01 and 01b (chats) do not write status.** Their
  `Store evaluation` nodes predate 014 and are outside this PR's ownership.
  Their rows land as `contract_status = 'ok'`, `gradeable = true` — and if such
  a row has a null `final_score`, it lands in the "ok, graded, no number" state
  that the call path is careful to avoid, and shows up in
  `ok_without_score_count` growing after rollout. Acceptance query 4 and
  preflight P2b are how you find out. Fixing those two nodes is the obvious
  follow-up and is a copy of the five expressions in
  `scripts/sql/02_store_evaluation.sql`.

- **A re-evaluation that comes back `unscoreable` overwrites a real score.**
  `Store unscoreable outcome` does `ON CONFLICT (interaction_id) DO UPDATE` and
  clears every score-derived column, matching what `Store evaluation` already
  does for the same statuses arriving on the pass-2 path. The reasoning is that
  the honest record is the current one: if the stored transcript no longer holds
  enough speech to grade, a score computed from an older transcript is not
  describing anything that exists. It is still the one destructive edge of
  storing the outcome, it needs a re-transcription to happen at all, and the
  alternative — leaving old module scores beside a null `final_score` — is the
  half-one-run-half-another row the store nodes exist to prevent. Recorded, not
  hidden.

- **`model_fingerprint` is the last fingerprint seen, and the disagreement is
  not stored.** An evaluation is one or two API calls (the contract re-ask is
  the second), and the worker records a mismatch between them as
  `usage.system_fingerprint_all`. `Store evaluation` writes
  `usage.system_fingerprint` — the last value — and the `_all` list is written
  nowhere, because `raw_response` holds `pass2.payload`, not `pass2.usage`. A
  score whose two halves came from different backends is therefore grouped
  under one of them. Storing the usage blob (token counts included, which are
  also currently dropped) is a small follow-up on the same node.

- **Nothing is publishable until the noise is re-measured on the shipping
  co-ordinate.** This is by design (§3, rule 2) but it is a real gap in
  capability, not just a caveat: until somebody repeats the day-13 A/A run on
  the shipping pass-2 prompt / `deepseek-v4-flash` and inserts the variance,
  `band_stable` is false everywhere and the scorecard publishes no bands at all.
  That work belongs to the worker PR's evaluation harness
  (`scripts/compare_day.py --repeat`), and it is the last gate between this
  migration and a scorecard anybody may show to an agent.

- **The noise constant is a floor, not the whole uncertainty.** Taking
  `max(sample_var, noise_var)` fixes the "lower bound used as a proof" defect,
  but the sample variance is itself estimated from the same N, and neither term
  models systematic semantic error — which, as Sol noted, survives
  self-consistency averaging entirely.

- **`v_usable_evaluations` expands `SELECT e.*` at creation time.** Any later
  migration that adds a column to `agent_evaluations` must re-run 014 (or
  re-create that view) for the column to appear there.

- **The D1 rule judges the transcript, not the score.** A `'green'` transcript
  can still be wrong, and rule 4 says nothing about that; it removes only the
  cases the ASR itself flagged. It also cannot recover an amber call: the fix is
  re-transcription, which is out of this PR's scope.

- **Rule 4 changes what the historical scorecard says**, without changing a
  single stored row. Numbers published before 014 included bad-ASR calls and
  numbers published after do not, so a figure quoted from last month will not
  reproduce. Preflight **P2c** is the record of how big that shift is; take it
  before applying, and keep it.

---

## 6 · After rollout: what to watch, and where from

Four questions, four queries, run for the first fortnight.

**Is a writer still not sending status?** `ok_without_score_count` is supposed
to be frozen history. Anything landing in it *after* migration day is a live
writer that does not set `contract_status` — workflows 01 / 01b are the
candidates. This is the query the trigger-disable in §4 exists to keep
meaningful: without it every historical row would carry migration day's
`updated_at` and this would be unreadable.

```sql
SELECT updated_at::date AS last_written, count(*)
  FROM agent_evaluations
 WHERE contract_status = 'ok'
   AND NOT eval_score_is_usable(contract_status, gradeable, final_score)
 GROUP BY 1 ORDER BY 1 DESC;
```

Everything on or before migration day is the backfilled history. Any later date
is a bug, and the date tells you when it started.

**Is `unscoreable` being stored and terminalised, exactly once each?** R9, R10
and R10b. A dead-lettered `unscoreable:` job with no evaluation row means the
store node did not run; an `unscoreable` row on a `judge_failed` job means the
routing regressed and the response is being retried as a judge fault again.

**Has a version co-ordinate moved?** R6 and R7. A new `prompt_version`,
`model` or `model_fingerprint` starts a fresh scorecard row at N=1 with **no**
measured noise floor, so it publishes nothing until it is measured. That is the
system working; the thing to watch is that somebody notices and schedules the
A/A run rather than wondering why the dashboard emptied.

**How much is the D1 rule costing?** R2c and R2d. `amber_shadow_count` rising
faster than `evaluated_interactions` is an ASR regression showing up in the
reporting layer rather than being absorbed by it, and R2d lists the agents who
have dropped out of reporting entirely. `amber_shadow_usable_count` is the size
of the prize for re-transcribing: those are the rows that would join `n_usable`
if the audio came back green. The one thing that must not happen quietly is the
rule being relaxed to make a dashboard look fuller.

## 7 · F2 backfill — retrospective ASR quality for transcripts that predate the cleaner

The D1 rule in §2 fails closed on a transcript with no
`asr_metrics->>'asr_quality_status'`, and the cleaner only started writing one
on 2026-08-13. On the staging copy that left **402 of 662** call evaluations
attached to a transcript with no status at all — shadow-only forever, not
because the audio was bad but because nobody had looked. Sol's G1 condition for
production is that they be assessed first.

`scripts/backfill_asr_quality.py` replays the existing policy
(`services/worker/app/asr/text_quality.py`, `assess_call`) over the text those
rows already hold. The policy is text-only and deterministic, so the replay is
the same judgement the live pipeline would have made — it is not a second,
laxer rule invented for old data. The **policy version is read from
`text_quality.POLICY_VERSION` at runtime and stamped on every row**; the script
contains no version literal of its own, so when the cleaner moved from `asr-q1`
to `asr-q2` the provenance moved with it and a row written under either can be
told apart without guessing.

### Run reconcile first, then backfill

`evaluate_alert_rules()` (`013_alert_rules.sql`, `base` CTE) coalesces a
**missing** `asr_quality_status` to `'green'` — deliberately, because an alert
on a call whose quality nobody recorded is still worth a human's attention.
That default is the opposite of D1's, and it means this job silently changes
what the alert rules would decide for every row it touches: a lead that raises
`hot_real_ask_uncommitted` under the coalesce stops raising it the moment the
transcript reads `amber`.

Whether that is a loss depends entirely on whether the alert was already
evaluated. A call reconciled *before* the backfill keeps the occurrence it
earned; a call still sitting in the backlog would be evaluated *after* the
status landed and would never raise it at all — the same lead, silently
dropped, for no reason but the order two jobs happened to run in.

So the job **refuses to start** while the reconciliation backlog is non-empty:

```
REFUSING TO RUN: 686 terminal call_ingest_jobs still have alerts_evaluated_at IS NULL
```

and exits **3**. The backlog is the same population as
`scripts/sql/02_reconcile_alert_evaluations.sql` and `acceptance_pr1b.sql`
**I5**: `status IN ('evaluated','judge_failed','dead_letter')`,
`interaction_id IS NOT NULL`, `alerts_evaluated_at IS NULL`. Clear it with
`SELECT * FROM reconcile_alert_evaluations(500);`, repeated until it returns no
rows — it is idempotent, `evaluate_alert_rules()` deduplicates on the
occurrence hash — then run the backfill.

`--allow-backlog` overrides the guard and prints a warning instead. It is for a
database where reconciliation is *structurally* impossible, not for impatience.

The job itself never INSERTs, UPDATEs or DELETEs `alert_occurrences`, never
calls `evaluate_alert_rules()` and never calls `reconcile_alert_evaluations()`.
It reads `count(*) FROM alert_occurrences` immediately before and after its
work, prints both, and returns non-zero if they differ:

```
alert_occurrences: 8 before, 8 after (unchanged)
```

That is the **R-check**. Run it by hand either side of the job as well — steps
2 and 6 below — so the evidence does not depend on the job that is being
checked.

### The operator sequence for production

```bash
# 0 · clear the alert reconciliation backlog (repeat until it returns no rows)
psql "$DATABASE_URL" -c "SELECT * FROM reconcile_alert_evaluations(500);"

# 1 · confirm it is clear -- this must print 0
psql "$DATABASE_URL" -c "
  SELECT count(*) FROM call_ingest_jobs
   WHERE status IN ('evaluated','judge_failed','dead_letter')
     AND interaction_id IS NOT NULL AND alerts_evaluated_at IS NULL;"

# 2 · R-check, BEFORE
psql "$DATABASE_URL" -c "SELECT count(*) FROM alert_occurrences;"

# 3 · distribution only, writes nothing
py -3.13 scripts/backfill_asr_quality.py --dry-run

# 4 · the real thing; DATABASE_URL comes from the environment, never the command line
py -3.13 scripts/backfill_asr_quality.py

# 5 · idempotency: this must report "wrote 0 rows"
py -3.13 scripts/backfill_asr_quality.py

# 6 · R-check, AFTER -- must equal step 2
psql "$DATABASE_URL" -c "SELECT count(*) FROM alert_occurrences;"

# 7 · the point of the exercise: no call evaluation is status-less any more
psql "$DATABASE_URL" -c "
  SELECT coalesce(t.asr_metrics->>'asr_quality_status','<missing>') AS status,
         count(*)
    FROM agent_evaluations e
    LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id
   WHERE e.input_type = 'call_transcript'
   GROUP BY 1 ORDER BY 1;"
```

`--limit N` stops after N rows, `--batch N` changes the commit size (default
100), `--database-url` overrides `$DATABASE_URL`, `--script-version` overrides
the git provenance stamp. Every run selects only rows still lacking
`asr_quality_status` and the UPDATE re-tests that condition in its `WHERE`, so
the job is resumable, safe to interrupt, and a second run reports
`wrote 0 rows`. It is a data job, not a migration: no DDL, no number in
`db/migrations/`.

Exit codes: **0** fine, **2** no database URL, **3** the reconciliation guard
tripped or `alert_occurrences` changed under the run.

### Undo — `--reset-backfilled`

```bash
py -3.13 scripts/backfill_asr_quality.py --reset-backfilled --dry-run
py -3.13 scripts/backfill_asr_quality.py --reset-backfilled
```

Restores the pre-backfill `asr_metrics` of every row **this job wrote**, then
exits. Scope is exactly `WHERE asr_metrics ? 'backfill'`, so a row the live
pipeline assessed is never in range whatever its status is. Each row is
restored from its own `backfill.original` — the untouched object, kept verbatim
— so this is a restore, not a reconstruction, and it makes the whole job
re-runnable against a database that has already had it.

Rows written by the first, merge-only version of the script carry no
`original`; for those the reset falls back to dropping every key the script can
write and putting back the ones that run listed in `backfill.preserved_keys`.
Both paths were exercised on staging and both restored all 406 rows to a state
**byte-identical** to an independent pre-backfill copy of the database, checked
with `dblink` across all 686 transcripts: 0 rows differing.

No backlog guard on this path: the reset only ever *removes* amber and red,
which restores 013's coalesce-to-green default rather than diverging from it.

### Reconciliation — a partial transcript is never certified

A transcript that lost a chunk still *looks* whole, and nothing downstream can
tell: pass 2 applies the full rubric to whatever words it is handed and returns
a well-formed number about a conversation that did not happen that way. So
before any grading happens the segment array is validated element by element
and reconciled against the two independent records of what the transcript
should contain:

* every element is an object; `text` is a string; `seq` is an integer;
  `start_sec`/`end_sec` are numbers with `start <= end`;
* `seq` runs contiguously from 0 in stored order — `transcribe_call` writes
  `seq=i` over `range(len(cuts)-1)`, so a gap means a chunk is missing from the
  array and the call is short by however long that chunk was;
* spans are monotone and non-overlapping (`start[i] >= end[i-1]`), otherwise
  the durations no longer partition the call and `invalid_seconds`, the 20 %
  coverage rule and both density floors are measuring against a denominator
  that is wrong;
* `len(segments)` equals `chunks_total` when the row records one;
* `" ".join(s.text for s in segments if s.text).strip()` — character for
  character the expression `cohere_arabic.transcribe_call` uses to build
  `full_text` — equals the stored `full_text`.

Any failure is `red` with the reason named — `segments_inconsistent`,
`segment_count_mismatch`, `text_join_mismatch`, `segments_missing` — and can
never be green or amber. Malformed segments are **never silently filtered**,
which is what the first version did.

A row with **no** segment array is now red (`segments_missing`) rather than
graded as one chunk spanning the call: there is nothing to reconcile the text
against, and "cannot be reconciled" is precisely the case this rule exists for.

### What it writes

The whole `asr_metrics` object, SET in **one UPDATE per row**, not a `||`
merge. A merge cannot remove anything, so a legacy row carrying a stale
`quality`, `cleaning`, `clean_chars`, `chars_per_audio_sec` or top-level
`flags` from some earlier experiment would keep it, sitting beside — and
contradicting — the status just written. The split:

* **MEASURED transport keys are preserved verbatim** — `chunks_total`,
  `chunks_failed`, `chunks_empty`, `chars`, `max_token_run`,
  `repetition_suspect` (183 of 406 rows on staging carry them). They record
  what the original ASR run measured at transport time; that is not
  re-derivable from stored text, so the measurement beats the replay and is fed
  *into* the assessment. Listed in `backfill.preserved_keys`.
* **DERIVED keys are replaced wholesale**, atomically with the status —
  `quality`, `cleaning`, `clean_chars`, `chars_per_audio_sec`,
  `asr_quality_status`, and any stale top-level `flags`, `speech_chars`,
  `control_token_gaps` or `unknown_control_tokens`. Listed in
  `backfill.replaced_keys`.
* Any other key the row happens to carry is left alone.
* `backfill.original` holds the entire pre-existing object, so the write is
  exactly reversible.

Two of those deserve their own line. `chars_per_audio_sec` is **derived, not
measured**: it is `chars / duration`, and preserving an old ratio beside a
preserved `chars` would let the row disagree with itself. And a stale
top-level `flags`, `speech_chars`, `control_token_gaps` or
`unknown_control_tokens` is **dropped rather than rewritten** — none of the
four is part of the live top-level shape (`cleaning.flags` and `quality` are
their authoritative homes), so re-writing one would invent a second, older
answer to a question `quality` already answers.

The resulting top-level keys are exactly the ones
`cohere_arabic.transcribe_call` writes today — `chunks_total`, `chunks_failed`,
`chunks_empty`, `chars`, `clean_chars`, `chars_per_audio_sec`, `max_token_run`,
`repetition_suspect`, `asr_quality_status`, `quality`, `cleaning` — plus the
one extra `backfill` sub-object, which is the only way to tell a backfilled row
from a live one.

`full_text` and `segments` are **not** rewritten, and `backfill.text_rewritten`
is `false` on every row to say so. A `green` call has by definition had nothing
removed — `any_removal` forces amber — so for exactly the rows D1 publishes,
clean text and raw text are the same string. Rewriting would change nothing
where it matters and would destroy the raw text the cleaning ledger's offsets
are recorded against on the rows where it does not.

### Provenance keys

```json
"backfill": {
  "quality_policy_version": "asr-q2",
  "backfilled_at": "2026-08-23T19:31:44.812706+00:00",
  "backfill_source": "stored_text",
  "backfill_script": "backfill_asr_quality.py@v4-trial",
  "script_sha256": "fbb771b597b827c0…",
  "chunk_source": "segments",
  "text_rewritten": false,
  "reasons": ["contamination_removed"],
  "preserved_keys": ["chunks_total", "chunks_failed", "..."],
  "replaced_keys": ["chars_per_audio_sec"],
  "original": { "chars": 1004, "chunks_total": 2, "...": "..." }
}
```

`quality_policy_version` is whatever `text_quality.POLICY_VERSION` said when
the job ran. `script_sha256` is the sha256 of the script file's own bytes: the
git stamp in `backfill_script` says which branch or commit the operator
*thought* they were running, and is a branch name rather than a sha whenever
the tree is dirty, but the hash says what actually ran and cannot be wrong. A
single distinct `script_sha256` across the corpus is the check that one job,
not two versions of one, wrote it. `reasons` names the gate triggers that
fired, so a red row explains itself without a diff.

### Fail-closed

`assess_call` is a gate, not a scorer, and everything the replay cannot verify
resolves toward red, never green:

| reason | what it means |
| --- | --- |
| `segments_missing` | no segment array to reconcile the text against |
| `segments_inconsistent` | a segment is malformed, out of order, or overlapping |
| `segment_count_mismatch` | fewer (or more) segments than `chunks_total` says |
| `text_join_mismatch` | `full_text` and the segment texts disagree |
| `empty_transcript` / `empty_text_with_duration` | no speech, with or without audio behind it |
| `unknown_duration` | no denominator, so the coverage rule and both density floors silently evaluate to "fine" |
| `transport_failure_no_chars` | a chunk was lost and nothing came back at all |
| `chunks_failed_exceeds_segments` | more failures recorded than there are chunks |
| `chunks_failed_unlocated` | a chunk was lost and **which one is not recorded** |

The last is Sol's F14 and it is the one behaviour change an operator will
notice. A recorded `chunks_failed` count says how many chunks were lost but not
which, and the first version assumed the worst compatible case — the longest
chunks — so the coverage gate saw the largest hole the count could justify.
That is still a *guess*, a guess produces a number, and a number is what
`green` gets decided from. So an unlocated transport failure is now red with
its own name. If a writer ever starts recording the sequences (`asr_metrics`
key `failed_seqs`, a list of integers matching the count — `transcribe_call`
holds exactly this while it runs and simply never persisted it), the gate uses
it and charges those chunks and nothing else. No row on staging has one, and no
row on staging has `chunks_failed > 0`, so this moved nothing there — it is a
rule for the corpus, not for the sample.

An **empty** chunk is not a failed one: silence read correctly ambers the call
and never invalidates audio, exactly as `transcribe_call` counts it.

### Staying in step with the cleaner

The replay must speak the cleaner's dialect exactly, or the row it writes is a
different judgement wearing the same field names. Two couplings are load-bearing
and both are pinned by tests:

* **The denominator.** asr-q2 measures character density and the Tier-1
  contamination ratio against `text_quality.speech_chars()` — raw length minus
  the harmless control tokens inside it — precisely so a chatty decoder cannot
  move a status. The backfill's `reasons` take that number out of the `quality`
  block the gate just returned rather than re-deriving one from `chars`;
  otherwise a green row could carry a density reason the gate never fired.
* **The control-token predicate.** `control_token_v1` (harmless, deleted, no
  marker) and `control_token_gap_v1` (lost audio, GAPped, Tier-1 counted) are
  one substring apart and mean opposite things. The backfill calls
  `text_quality._control_only` directly, with no local fallback: a
  convention-based stand-in would read a lost-audio removal as status-neutral
  and drop `contamination_removed` from a row that lost speech.

### Staging result

Verified end to end after the `asr-q2` cleaner change landed (allowlisted
`<hesitation>`, lost-audio markers to `[[ASR_GAP]]`, density on
`speech_chars`), on a database put back to its pre-backfill state with
`--reset-backfilled` first, so these are a fresh replay and not a re-reading of
the earlier run.

Dry run and real run agree: **334 green / 55 amber / 17 red** over all 406
status-less transcripts. Restricted to the 402 that carry a call evaluation it
is **334 / 55 / 13**, matching Sol's read-only replay exactly. The four extra
reds are transcripts no evaluation ever pointed at — three
`clean_text_under_20_chars`, one `unknown_duration`.

**Every delta against the earlier 334/55/13 run is zero, and that is a
measurement rather than an assumption.** The four changes that could have moved
rows moved none of these rows, each for a checked reason:

* **asr-q2 control tokens.** The only control-token shape anywhere in the
  corpus is `<hesitation>` — 46 occurrences across 28 transcripts, 27 of them
  inside the backfilled 406. asr-q2 classifies it HARMLESS and keeps it
  status-neutral, exactly as asr-q1 did. Across all 406 rows:
  `control_token_gaps` **0**, `unknown_control_tokens` **0** — `<inaudible>`,
  `<silence>`, `<noise>`, `<music>`, `<unk>` appear zero times, so no row
  gained an `[[ASR_GAP]]` and no row moved to amber or red because of one.
* **The `speech_chars` denominator.** It differs from `chars` only by those 27
  harmless tokens, which is far too small to cross the 3/sec or 22/sec floors
  on any row here; the one `char_density_over_22_per_sec` and four
  `char_density_under_3_per_sec` rows are the same rows as before.
* **F14 unlocated transport failures.** `chunks_failed > 0` on **0** of the 406
  rows, so nothing could become `chunks_failed_unlocated`.
* **F6 reconciliation.** All 406 rows pass every check: 0 malformed segments, 0
  `seq` gaps, 0 overlapping spans, 0 count mismatches against `chunks_total`, 0
  `full_text` / segment-join mismatches, 0 rows with an empty segment array.
  The claim the first version's docstring *asserted* ("verified on staging,
  `full_text` equals the join of the segment texts") is now **performed per
  row, every run**.

Everything else held:

* `<missing>` is **0** for `input_type = 'call_transcript'`: **576 green, 73
  amber, 13 red**, with `call_no_transcript` 0 and `call_status_unknown_value`
  0. Before the run the same query read 242 / 18 / 0 with **402 missing**.
* `v_quality_by_input` for `call_transcript` moved from **242 usable call rows
  to 576**, and `amber_shadow_count` fell from **420 to 86**. The chat row and
  the two degenerate one-row groups are unchanged.
* Reconciliation backlog **686 → 0** before the run (four
  `reconcile_alert_evaluations(500)` sweeps; 8 occurrences recorded, 0 errors),
  and **0** after.
* `alert_occurrences` **8 before, 8 after** — unchanged across the reset, the
  dry run, the real run and the second run.
* Provenance on all 406 rows: `quality_policy_version = asr-q2` in `backfill`,
  in `quality` and in `cleaning.version`; exactly **one** distinct
  `script_sha256`; `backfill.original` present on every row and equal, key for
  key, to the independent pre-backfill copy of the database; **0** rows
  carrying a stray top-level `flags`, `speech_chars`, `control_token_gaps` or
  `unknown_control_tokens`.
