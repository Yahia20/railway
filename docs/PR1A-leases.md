# PR1A — leased, resumable call jobs

**Files:** `db/migrations/012_call_job_leases.sql`,
`n8n/workflows/02-calls-ingest-evaluate.json`,
`scripts/check_workflow_json.py`, `scripts/sql/02_*.sql`,
`scripts/sql/acceptance_pr1a.sql`

> **Rolls out with 013 and 014.** `db/migrations/014_evaluation_status.sql`
> lands in the same window (runbook §6 step 4) because it rebuilds the two
> reporting views over the table this workflow writes. Summary in §6b, full
> document in `docs/PR2-db-status.md`.

Nothing here has been applied or deployed. The migration has not been run
against any database and the live workflow has not been touched.

> **Revision 2.** The first version of this PR was reviewed and blocked. The
> lease fenced only the job-status updates, not the transcript / pass-1 /
> pass-2 / alert writes, and it was sized so that a batch could not finish
> inside it. Both are fixed here, along with the zero-row fall-throughs, the
> judge-attempt accounting, the renderer claim and the re-evaluation
> provenance. Section 8 records what is still known to be imperfect.
>
> **Revision 3.** Revision 2 was reviewed and blocked again, on two counts.
>
> 1. **The fences were not atomic.** Every durable write carried the token, but
>    it read it through an *unlocked* `SELECT` and then wrote. Recovery could
>    reclaim the row inside that window and the stale write still committed —
>    the fence was a check, not a lock. Every multi-statement fence now acquires
>    the job row with `SELECT … FOR UPDATE` **inside the same statement**, and
>    requires the lease to still be live. §2, "The fences are locks, not checks".
> 2. **Alert-queue persistence was best effort.** The alert node ran after the
>    job was terminal — and a terminal job is never recovered — while swallowing
>    its own errors into a leaf. Alert evaluation is now durable state on the
>    job row (`alerts_evaluated_at`), the node no longer swallows anything, and
>    a reconciliation sweep finishes whatever did not land. PR1B §5.
>
> Also in revision 3: an exhausted judge budget dead-letters explicitly instead
> of being parked for three hours (§10), `lease_shape` names `claimed_at` in
> both halves, and the runbook's post-deploy checks are ones that can actually
> fail.
>
> **Revision 4.** PR1A and PR1B were reviewed **merge-ready behind their staging
> gates**; 014 was blocked and is corrected in `docs/PR2-db-status.md`. What
> changed *here*: the workflow gained `Store unscoreable outcome` and
> `Unscoreable stored?` (§6b — the unscoreable refusal is now stored as an
> evaluation row before the job is terminalised, reversing revision 3's
> decision), A13 gained a PID-filtered waiter assertion and an isolation-level
> assertion and A13z is relabelled as the scaffold it is (§8), the runbook now
> opens with the reviewer's 12-step order and minimum no-go conditions verbatim
> (§6), and step 3 snapshots view ACLs as well as the queue and the constraints.

---

## 1 · What was wrong

Five defects, all in the calls pipeline, all of the kind that produce a number
rather than an error.

### D1 — the claim was not a claim

`Claim work` selected up to 25 rows and set `updated_at = now()`. That is a
timestamp, not ownership. The schedule fires every 15 minutes and a batch of 25
recordings routinely takes longer than that, so the next execution started
while the previous one was still working and **selected the same rows**: two
Cohere transcriptions of the same audio, two DeepSeek evaluations of the same
call, and two writers racing for one `transcripts` row.

### D2 — a judge retry re-ran ASR

Both `transcribed` and `judge_failed` were wired straight into
`Cohere Arabic ASR`. A job that already had a stored transcript and only needed
the judge run again therefore paid for a second transcription — and worse,
**overwrote the transcript the retry was supposed to be re-reading**, so the
retry was not a retry of the same input.

### D3 — the confidence gate ran before the red-quality gate

`Transcript usable?` asked `error || empty || confidence < 0.7` as one
condition, and only what survived it reached the `asr_quality_status === 'red'`
test. A transcript that was both red and low-confidence was therefore
classified as a plain ASR failure: never stored for audit, and retried up to
five times against audio whose acoustics cannot change.

### D4 — pass 2 could delete pass 1

`Evaluation ok?` required `pass1 && pass2`. When pass 2 failed, **nothing** was
stored — including a perfectly good pass 1 naming a hot lead with a
code-verified quote. The two passes exist as separate calls precisely so that
one cannot contaminate the other; gating one on the other threw away the half
that drives revenue in order to protect the half that drives coaching.

### D5 — one retry counter for two different failures

A single `retries` column could not tell "ASR failed five times" from "ASR
succeeded once and the judge failed four times". One bad prompt version could
dead-letter calls whose audio was fine.

### Plus: a lease with no recovery, and a queue that stopped on a quiet day

Adding in-flight statuses creates a new failure mode — a worker that dies
holding one. `Recover expired leases` exists for that. Separately, the claim
used to be chained behind `Register discoveries`; a Drive listing that returned
zero recordings emitted zero items, and the chain simply stopped, so **the
backlog did not drain on any day with no new recordings**. The claim now hangs
off its own branch from the trigger.

---

## 2 · What 012 adds, and how long a lease has to be

| Column | Why |
|---|---|
| `status` +`transcribing`, `evaluating` | "a worker holds this row right now". Without them a crashed execution is indistinguishable from a healthy one. |
| `claim_token uuid` | Lease identity. **Every durable write** carries `AND claim_token = $token`, so an execution that lost its lease cannot overwrite whoever picked the work up after it. |
| `claimed_at`, `claim_until` | When the lease was taken and when it lapses. |
| `next_attempt_at` | Earliest re-claim. The 45-minute cool-down after a failure is why one throttled ASR window cannot burn the retry budget of the whole backlog in an hour. |
| `asr_attempts`, `judge_attempts` | Separate budgets, counted where they are spent. |
| `retries` | **Kept.** Still incremented on failure so existing dashboards and the dead-letter runbook query keep working; no longer authoritative. |

012 also adds two CHECK constraints, because these are things every code path
already respects and the day one stops is the day you want to hear about it:

- `call_ingest_jobs_attempts_nonneg` — attempts and retries are never negative.
- `call_ingest_jobs_lease_shape` — a lease is a **three-column fact**, and
  **both halves name all three columns**. In flight ⇒ `claim_token`,
  `claimed_at` and `claim_until` are all set; not in flight ⇒ all three are
  null. A row in flight without a token can never be recovered: the sweep has
  nothing to clear. The first version omitted `claimed_at` from the
  not-in-flight half, which permitted `evaluated` + null token + a leftover
  `claimed_at` — exactly the shape a release path that forgot one line produces,
  i.e. the bug the constraint exists to catch. Every release path
  (`Mark evaluated`, `Mark judge failed`, `Mark ASR failed`, `Mark unscoreable`,
  `Dead-letter judge budget`, the recovery sweep) already clears all three.

  **Preflight.** The constraint is added with a name guard *and* a definition
  guard, so a database carrying the looser pre-release version gets it replaced
  rather than silently kept. The tightened version is validated against existing
  rows, so on such a database the `ALTER` fails if any non-in-flight row still
  carries `claimed_at`. 012 carries the query that finds them, and the `UPDATE`
  that clears them, commented out next to the constraint.

### The lease arithmetic

This is the part the first version got wrong, so it is written out.

n8n runs a node to completion **over every item** before the next node starts.
`Cohere Arabic ASR` batches at one item per 2.5 s with a **900 s** timeout;
`Two AI passes` has a 300 s timeout. So for a claimed batch of *N*, the last
item's worst case from the moment the lease was issued is

```
N × (900 + 2.5)   ASR for the whole batch
+ N × 300         judge for the whole batch
+ DB round trips
```

| N | lease needed (worst case) | old setting | new setting |
|---:|---:|---|---|
| 25 | 22 562 + 7 500 ≈ **30 062 s** | 900 s | — |
| 6 | 5 415 + 1 800 + ~60 ≈ **7 275 s** | — | 10 800 s (≈48% margin) |

The old `25 × 900 s` was not a small margin, it was a negative one: every item
after the first was unfenced *by construction*, and that is what made the
unfenced transcript write dangerous rather than merely untidy.

Two things keep the new number honest rather than just larger:

1. **`Claim work` takes 6, not 25.** Throughput is unaffected — executions
   overlap and the lease keeps them disjoint, so the ceiling is ~24 calls per
   hour against a corpus of well under a hundred calls a day.
2. **The lease is renewed as the item changes phase.** `Store call + transcript`
   renews it to 10 800 s when the transcript lands, and `Begin judge attempt`
   renews it to 3 600 s (6 × 300 s + margin) when evaluation starts. Each
   renewal only has to cover the phase in front of it, not the whole execution.

The cost is that a genuinely crashed execution leaves its rows unavailable for
up to three hours instead of fifteen minutes. That is the correct trade: a lease
that expires under a running worker causes double spend and racing writers,
which is the bug this PR exists to fix.

### The fences are locks, not checks

Carrying `claim_token` is necessary and was not sufficient. A fence shaped

```sql
WITH lease AS (SELECT 1 FROM call_ingest_jobs
                WHERE uniqueid = $1 AND claim_token = $2)
INSERT INTO … SELECT … FROM lease …
```

reads a snapshot and then lets go of it. Between that read and the write, the
recovery sweep can reclaim the row and a new worker can re-claim it — and the
stale write still commits, because nothing ever re-checked. That is a
read-then-write race with a real window in it, not a theoretical one: the window
is however long the surrounding upsert takes.

Every multi-statement fence now looks like this instead:

```sql
WITH lease AS MATERIALIZED (
  SELECT j.uniqueid, j.claim_token
  FROM call_ingest_jobs j
  WHERE j.uniqueid = $3 AND j.claim_token = $4::uuid
    AND j.status IN ('transcribing') AND j.claim_until > now()
  FOR UPDATE
), …
```

Three things make it work, and each is load-bearing:

- **`FOR UPDATE`** takes the row lock. If recovery holds it, this statement
  *blocks*; when it is released Postgres re-evaluates the qualification against
  the new row version (EvalPlanQual), finds the token gone, and yields no row.
  If this statement gets the lock first, recovery blocks and then re-checks its
  own `claim_until < now()` against what we committed.
- **`claim_until > now()`** makes an expired lease unusable *before* the sweep
  has got to it — otherwise the window between expiry and recovery is unfenced
  by definition. The cost is that a run which overshoots its lease throws its
  own work away instead of overwriting somebody else's; §2's ~48% margin is what
  keeps that rare.
- **Every dependent CTE reads `FROM lease`.** This is a Postgres rule, not a
  style choice: a data-modifying CTE cannot see the effects of its sibling CTEs,
  and all sub-statements share one snapshot, so a lock that merely stands
  *beside* the write guarantees nothing about the write. `AS MATERIALIZED` pins
  the evaluation so the CTE cannot be inlined into its readers.

A **single-statement** fence needs none of this — `UPDATE … WHERE claim_token =
$t` locks the row and re-evaluates its own predicate itself. Those statements say
so in a `-- fence-exempt:` comment, and the validator now requires one or the
other (§9).

### Locking order

Written down because the only way to get a deadlock out of this design is to
stop following it.

```
call_ingest_jobs  (always first, always by uniqueid)
      ↓
interactions → transcripts / interaction_analysis / agent_evaluations
      ↓
alert_occurrences
```

- **`Claim work`** takes many job rows, with `FOR UPDATE SKIP LOCKED` — it never
  waits for anything, so it can never be the blocked party in a cycle.
- **`Recover expired leases`** takes many job rows and waits, but touches *only*
  `call_ingest_jobs`.
- **Every durable write** takes exactly **one** job row, by primary key, and only
  then touches child tables. It never asks for a second job row while holding
  one, so it can never be the party that closes a cycle with recovery.
- **`Reconcile alert evaluations`** takes job rows with `FOR UPDATE SKIP
  LOCKED`, so it also never waits for a job row. It can wait on an
  `alert_occurrences` unique-index insert — but only for the same
  `(rule, version, interaction, hash)`, which implies the same job row, which it
  would have skipped.

The rule to keep: **take the job row first, take exactly one, and never wait for
a second one while holding it.**

### Attempt accounting

Attempts are counted **when the stage begins**, not on failure — a worker that
dies without writing anything still spent an attempt, and that is what stops a
poison row from being claimed forever.

`judge_attempts` is now spent in `Begin judge attempt`, **after** the transcript
quality gates. It used to be spent at the transcribe→evaluate handoff, before
them, so every red or low-confidence transcript burned one of the five judge
attempts without a judge request ever being made.

Backfill is conservative: `asr_failed` rows inherit `retries` into
`asr_attempts`, `judge_failed` rows into `judge_attempts`, rows already past
ASR get `asr_attempts = 1`, and everything else is left at zero rather than
guessed. Over-counting here would dead-letter healthy rows on their first real
failure. Every backfill is guarded on `= 0`, so a re-run cannot double-count.

The migration is idempotent: `ADD COLUMN IF NOT EXISTS`,
`CREATE INDEX IF NOT EXISTS`, constraint adds guarded on `pg_constraint`, and
the status CHECK is dropped by lookup (on the constraint whose only column is
`status`) before being re-added under the stable name
`call_ingest_jobs_status_check`. That lookup drops **every** single-column
`status` check, not only the one 008 created — which is why the runbook
snapshots the constraint list before applying anything.

---

## 3 · The workflow, before and after

### Before (19 nodes)

```
Every 15 min → List recent recordings → One item per recording
             → Register discoveries → Claim work → Cohere Arabic ASR
             → Transcript usable?  ──true──→ Store call + transcript
             │                                 → Link job transcribed
             │                                 → ASR quality red?
             │                                     ├─true→  Mark unscoreable
             │                                     └─false→ Build follow-up history
             │                                              → Two AI passes
             │                                              → Evaluation ok?
             │                                                  ├─true→  Store customer analysis
             │                                                  │        → Store evaluation
             │                                                  │        → Mark evaluated
             │                                                  └─false→ Mark judge failed
             └──false──→ Mark ASR failed
```

### After (35 nodes)

```
Every 15 min ┬→ List recent recordings → One item per recording → Register discoveries  (leaf)
             ├→ Reconcile alert evaluations  (leaf)
             └→ Recover expired leases → Claim work → Route by work stage
                    ├─[transcribe]→ Cohere Arabic ASR → Transcript returned?
                    │                   ├─true→ Store call + transcript → Transcript stored?
                    │                   │                                   ├─true→ ─────┐
                    │                   │                                   └─false→ (stop)
                    │                   └─false→ Mark ASR failed  (leaf)                 │
                    └─[evaluate]────────────────────────────────────────────────────────┤
                                                                                        ▼
                                                                       Load stored transcript
                                                                                        ▼
                                                                            ASR quality red?
                                                        ├─true→  Mark unscoreable  (leaf)
                                                        └─false→ Confidence usable?
                                                                    ├─false→ Mark ASR failed
                                                                    └─true→  Begin judge attempt
                                                                              → Judge attempt started?
                                                                                  ├─false→ Dead-letter judge budget  (leaf)
                                                                                  └─true→ Build follow-up history
                                                                                          → Prepare evaluation input
                                                                                          → Two AI passes
                                                                                          → Evaluation ok?
                                    ├─false→ Nothing to evaluate?                             │
                                    │           ├─true→  Store unscoreable outcome            │
                                    │           │          → Unscoreable stored?              │
                                    │           │              ├─false→ (stop)                │
                                    │           │              └─true→ Mark unscoreable (leaf)│
                                    │           └─false──────────────────────────────────────┤
                                    └─true→ Store pass1 → Pass 1 stored?                     │
                                                            ├─false→ (stop)                  │
                                                            └─true→ Pass 2 usable?           │
                                                                      ├─true→ Store evaluation → Mark evaluated ─┐
                                                                      └─false→ Mark judge failed ────────────────┤
                                                                                                                 ▼
                                                                                                       Job finalised?
                                                                                                 ├─false→ (stop)
                                                                                                 └─true→ Evaluate alert rules  (leaf)
```

Node **ids** are unchanged for every node that already existed. Two nodes were
renamed because their meaning changed:

| id | before | after |
|---|---|---|
| `transcript-usable` | `Transcript usable?` | `Transcript returned?` |
| `store-customer-analysis` | `Store customer analysis` | `Store pass1` |

**New:** `recover-expired-leases`, `route-by-work-stage`,
`load-stored-transcript`, `confidence-usable`, `prepare-evaluation-input`,
`pass2-usable`, `transcript-stored`, `begin-judge-attempt`,
`judge-attempt-started`, `pass1-stored`, `job-finalised`,
`evaluate-alert-rules`, `dead-letter-judge-budget`,
`reconcile-alert-evaluations`, `nothing-to-evaluate`,
`store-unscoreable-outcome`, `unscoreable-stored`.

`nothing-to-evaluate` came later, with 014: see §6b. It sits on the **false**
output of `Evaluation ok?` and peels off the worker's `unscoreable` refusal —
a 200 response with a `pass2` block and no `pass1` — so that a call containing
no speech is dead-lettered once instead of retried five times as a judge
failure.

`store-unscoreable-outcome` and `unscoreable-stored` are round 4, which reversed
the decision to write no evaluation row for that refusal. The store node is a
fifth fenced durable write (`AS MATERIALIZED` lease CTE, `FOR UPDATE`,
`claim_until > now()`, `RETURNING`) and the gate is a fifth zero-row hard stop,
with a leaf false output like the other four. `Mark unscoreable` now has two
callers and builds its `last_error` prefix from the item rather than hard-coding
`asr_quality_red:` — and on the pass-2 path that item is the stored row, not the
`/evaluate` response, because `Mark unscoreable` is shared with the ASR path
where `Two AI passes` never ran. That is why the store node returns
`notes AS unscoreable_reason`: the reason has to travel through the write.

The `dead-letter-judge-budget` and `reconcile-alert-evaluations` pair are
revision 3. `Dead-letter judge budget` hangs off the **false**
output of `Judge attempt started?`, which used to be a bare leaf (§10).
`Reconcile alert evaluations` hangs off the **trigger**, not off the claim
chain, because it has to run on a day with no claimable work — the same reason
the claim itself was moved off `Register discoveries`.

**Removed:** `link-job-transcribed` (merged into `Store call + transcript`, see
below), and the three delivery nodes `alerts-to-send`, `notify-alerts`,
`record-alert-delivery` — see PR1B, there is no sender any more.

### Design points worth not re-litigating

**Every durable write is fenced — and the fence is a lock.** `Store call +
transcript`, `Store pass1`, `Store evaluation`, `Store unscoreable outcome` and
the alert evaluation each
acquire the job row with `SELECT … FOR UPDATE` on `uniqueid + claim_token +
in-flight status + live lease`, in the same statement as the write, and the
write reads *from* that locked row. A statement whose lease is gone — or was
taken away while it ran — writes nothing and returns nothing. Two earlier
versions of this reasoning were wrong and are recorded so they do not come back:
"the upsert is idempotent so a stale write only costs a wasted write" (it is
not: ASR is nondeterministic, model versions move, and the upsert stores new
text, new segments and new metrics), and "carrying the token is enough" (it is
not, without the lock — see §2, "The fences are locks, not checks").

**A zero-row fencing result is a hard stop.** Five IF gates — `Transcript
stored?`, `Judge attempt started?`, `Pass 1 stored?`, `Unscoreable stored?`,
`Job finalised?` — exist
only to turn "the statement matched nothing" into "this execution does nothing
else". Without them n8n's `{success:true}` placeholder is indistinguishable from
success: `Load stored transcript` would have run with an undefined
`interaction_id`, which is either a null-transcript route or a uuid cast error
depending on how n8n coerced the parameter that run. Every false output is a
leaf on purpose — the row belongs to somebody else now, and the right action is
to touch nothing.

**The handoff is atomic.** `Store call + transcript` upserts the interaction,
upserts the transcript, links `interaction_id` onto the job and renews the
lease in **one statement**, so a transcript can never exist without the job row
pointing at it. That is why `Link job transcribed` is gone. What that node did
that was *not* about the transcript — `status = 'evaluating'` and the judge
attempt — moved to `Begin judge attempt`, after the quality gates.

**Both paths converge at `Load stored transcript`.** The transcribe path *reads
its own transcript back* after storing it. That looks like an extra round trip
and it buys two things: the evaluate path never touches the ASR node, and there
is exactly **one** implementation of the dialogue format, so a judge retry
cannot score a differently-rendered transcript. See §4 for what "one
implementation" now actually means.

**Alerting hangs off a confirmed terminal transition, not canvas order.** The
alert branch used to be a sibling of the scoring branch under `Store pass1`,
relying on `executionOrder: v1` running the topmost branch first. That ordering
is real, but it does not help: a fenced `Mark evaluated` that updates **zero**
rows does not cancel its sibling, so an execution that had already lost the row
could still queue an alert. `Job finalised?` now sits after both `Mark
evaluated` and `Mark judge failed`, and only a genuinely returned row reaches
the alert node.

**`Prepare evaluation input` is the only node `Two AI passes` reads from.** The
body used to be assembled from five separate cross-node references, so one
broken reference produced a request that still *succeeded* with a field
missing. See §5.

**Cron stays `*/15`.** Unchanged.

---

## 4 · The renderer, and what "equivalent to `as_dialogue()`" means

`Load stored transcript` renders the stored segments into the `[mm:ss] TEXT`
form the judge prompt expects. The claim is that it is equivalent to
`CallTranscript.as_dialogue()` in `services/worker/app/asr/cohere_arabic.py` —
not similar to it, equivalent — because first run and judge retry must produce
the same bytes or every comparison between them measures the renderer.

The first version was **not** equivalent, in four ways that each change what
the judge reads:

| | `as_dialogue()` | first SQL version | now |
|---|---|---|---|
| order | iterates the segment **list** | `ORDER BY (s->>'seq')::int` — reorders when `seq` is missing (NULL sorts last) or non-monotonic, throws on a non-integer | `WITH ORDINALITY`, array order |
| empty text | skips `if not s.text` — falsey only | `btrim(text) <> ''` also drops whitespace-only segments | `coalesce(text,'') <> ''` |
| minutes | `{mm:02d}` — pads, never truncates | `lpad(x, 2, '0')` **truncates**: 6187 s rendered as `[10:07]` instead of `[103:07]` | `lpad(x, greatest(2, length(x)), '0')` |
| seconds cast | `int(start_sec)`, truncates toward zero | `floor()` | `trunc()` |

`floor` and `trunc` differ only for a negative `start_sec`, which ASR does not
emit — `Segment.start_sec` is an offset into the audio. It is `trunc` anyway,
because the cheapest way to keep a claim of equivalence true is to not leave a
known difference in it.

Golden cases for all four, plus known/unknown speakers and the empty
transcript, are in `scripts/sql/acceptance_pr1a.sql` **section A10**. They run
against fixtures with no tables involved, so they can be pasted into any psql
session.

---

## 5 · D6 — the missing follow-up history

**Observation.** Four calls in the day-13 data had later same-phone
interactions already in the database at the time they were evaluated, and still
scored Module 4 = `null` with "no follow-up history was supplied".

**The transport was not the problem.** Verified rather than assumed:

- `Build follow-up history` is a bare aggregate (`string_agg` with no
  `GROUP BY`), so it returns **exactly one row** per item — it can never hit
  the `{success:true}` empty-result path that gotcha 5 describes.
- `$json.history` in the next node is the identical pattern to workflow 01's
  `$json.newer` (`Newer messages arrived?` → `Still the latest?`), which is
  live and works.
- The paired-item chain back to `Claim work` passes only through Postgres nodes
  that carry `RETURNING`, so pairing survives. It has to: the transcript write
  sets `interaction_id` against `$('Claim work').item.json.uniqueid` on that
  same chain, and a broken pairing there would have cross-linked transcripts to
  the wrong calls — loudly, and it did not.

**The content was the problem.** The block reached the prompt and was
unreadable when it got there:

1. **Every line said `by unknown`.** `Store call + transcript` deliberately
   sets `agent_id = NULL` when `meta->>'kind' = 'q'`, because the extension in
   a queue filename is the *queue*, not a person — and `q` is nearly every
   recording in the corpus. `coalesce(a.full_name, 'unknown')` therefore
   rendered "unknown" almost always.
2. **Nothing stated the direction.** A customer calling back in is not an agent
   following up, and Module 4 scores only what the *agent* did. `interactions
   .direction` is never set for calls, so the block could not say which it was.
3. **No header, no message text.** It was a bare list of bullets. Criterion 3
   of Module 4 — follow-up *message quality*, 30 of the module's 100 points —
   was unanswerable from it, and the prompt's rule ("if the FOLLOW-UP HISTORY
   block is absent or marked `unavailable`, Module 4 = `null`") had nothing to
   recognise the block by.

Given "unknown person, direction unstated, no message", `null` is the honest
answer and the model gave it.

**Fix.** `Build follow-up history` now emits a labelled block: a `Subsequent
contact with this customer:` header, and per line the channel, the direction —
inferred as `INBOUND: the customer called in, this is not an agent follow-up`
when the later contact is a `q` queue recording — the hours elapsed, who
handled it (distinguishing *"no individual agent recorded (queue recording)"*
from *"not recorded"*), and the agent's first message where the channel has
message text. It is keyed on the stored `interaction_id` rather than the job's
filename metadata, which on a judge retry is a stale copy. And the value now
travels through `Prepare evaluation input`, so a dropped field would be visible
in one place instead of silently producing a valid request.

**It is compatible with the pass-2 v4 prompt, but it is NOT a mirror of
`metrics.followup_history_block()`, and this document used to say it was.** Two
real differences, both left as they are for now:

- the Python reference also lists the **current call's own promises** and
  distinguishes `NONE recorded` from unavailable; the SQL emits only subsequent
  contacts and returns the literal `unavailable` for an empty timeline;
- non-queue phone calls still have no direction, because call storage does not
  populate `interactions.direction` at all.

Before the new format is trusted for Module 4 scoring it needs a fixture that
proves v4 (a) scores an outbound chat follow-up and (b) does **not** read a
later inbound queue call as agent follow-up. That fixture is not in this PR.

> **D6 is a rollout gate, and it is not owned here.** The fixture belongs to the
> worker/prompt PR (`services/worker/**`, the pass-2 v4 prompt) — this PR only
> changes what the block *says*, not how the rubric reads it. PR1A can ship and
> be activated without it; what must not happen is Module 4 scores from the new
> block being treated as trustworthy before that fixture has run. Until then,
> read Module 4 on calls as provisional. Tracked in the runbook as
> post-activation gate, step 11.

### The silent-timeline question (open, deliberately not fixed here)

When the timeline is genuinely empty the query still returns the literal
`unavailable`, which the prompt reads as "Module 4 = null". Arguably a call
that is 14 days old with no subsequent contact of any kind is a *scored zero*,
not an unknown — and treating it as null is the same "absent situation scores
full marks" trap that rule 2 in `CLAUDE.md` exists to prevent, one level up.
Changing it would move real scores, so it is a product decision, not a bug fix,
and it is left alone in this PR.

---

## 6 · Rollout runbook

**The order matters and it does not start with the migration.** Applying 012
while the old workflow is still executing means the constraint drop, the
backfill and the new statuses all land under a writer that knows nothing about
them.

### The order, and the minimum no-go conditions

This is the round-4 reviewer's list, **quoted verbatim**. It supersedes any
earlier ordering in this document. The numbered steps below it are the detailed
procedure; where the two disagree, this list wins.

> 1. Fix 014 issues above.
> 2. On staging/restored copy: apply **012 → 013 → 014**, twice.
> 3. Run all schema checks plus A13a/A13b and A14/B11/B12.
> 4. Run the unscoreable end-to-end fixture: one stored status outcome, terminal job, one judge attempt, no retry.
> 5. Pass worker tests and **D6** before production activation.
> 6. Production: deactivate workflow 02, drain executions, snapshot queue/constraints/view ACLs.
> 7. Apply **012 → 013 → 014** atomically in order.
> 8. Publish workflow inactive; confirm validator, credentials and environment.
> 9. Set every alert rule to `is_alert=false`.
> 10. Execute controlled Step 8 test; restore Step 9b twice; pass all three reconciliation queries before dropping the backup.
> 11. Activate and observe two overlapping cron boundaries.
> 12. Keep rules suppressed for the dry-run week; enable individually only after precision review.
>
> Minimum no-go conditions: any A13 non-block/incorrect row count, any A14 lost stamp or head-of-line failure, D6 failure, nonzero schema invariants, unresolved view dependants/ACLs, unscoreable retrying or disappearing from outcome counts, schedule restore mismatch, or alert-reconciliation backlog after the sweep.

**Where each of those lives here.** Steps 1–5 are the staging half and have no
numbered section below, because until this revision there was no staging half:

| reviewer step | what to run |
|---|---|
| 1 | this revision |
| 2 | steps 4 and 5 below, **on staging**, run twice — all three migrations are idempotent and the second run is how you find out |
| 3 | `acceptance_pr1a.sql` §1–4 + **A13a/A13b**, `acceptance_pr1b.sql` §1–4 + **A14/B11/B12**, `acceptance_014_status.sql` §1–7 + R1–R11 |
| 4 | **`acceptance_014_status.sql` §F1** — the unscoreable end-to-end fixture. See below |
| 5 | worker test suite + **D6** (§5) |
| 6–7 | steps 1–4 below, on production. Step 3 now includes the **view ACL snapshot** (014 preflight P3), not only the queue and the constraints |
| 8 | step 6 below |
| 9 | step 7 below |
| 10 | steps 8, 9 and 9b below |
| 11 | step 10 below |
| 12 | step 11 below |

**Reviewer step 4 in full — the unscoreable end-to-end fixture.** This is the
only test that exercises the reversed round-4 decision end to end: worker
refusal → **stored evaluation row** → terminal job → no retry. The procedure and
the four assertions are §F1 of `scripts/sql/acceptance_014_status.sql`. In
short: register a fixture job whose transcript normalises to under 100
characters of speech but whose ASR quality is green, run one execution of
workflow 02 against staging, then prove all four of

1. **one stored status outcome** — exactly one `agent_evaluations` row,
   `contract_status = 'unscoreable'`, `gradeable` false, `final_score` null, the
   worker's reason in `notes`, and nothing score-shaped anywhere on the row;
2. **terminal job** — `dead_letter`, lease cleared, `last_error` starting
   `unscoreable: `;
3. **one judge attempt** — `judge_attempts = 1`, the single attempt stamped by
   `Begin judge attempt`, and `retries` unchanged;
4. **no retry** — a second execution 45+ minutes later leaves both of those
   untouched.

Any one of the four false is a no-go: it is the reviewer's
*"unscoreable retrying or disappearing from outcome counts"* condition, and
both halves of it are visible here.

### Step 1 — deactivate the old workflow

Deactivate **"02 · Calls v2 — state machine"** in n8n. Do not touch the
database yet.

### Step 2 — wait for every in-flight execution to finish

```
n8n → Executions → filter workflow 02, status "running"
```
Wait for the list to empty, or cancel what is left. An execution that survives
into step 4 will write against a schema it was not written for.

### Step 3 — snapshot the queue, and the constraints

```sql
SELECT status, count(*), sum(retries) FROM call_ingest_jobs
 GROUP BY status ORDER BY status;

SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
 WHERE conrelid = 'call_ingest_jobs'::regclass AND contype = 'c';
```

Keep both. The second one matters: 012 drops **every** single-column `status`
check, not only the one 008 created. If this list contains a `status` check
somebody added later, it is about to disappear, and this snapshot is the only
record that it existed.

**And snapshot the view ACLs.** Reviewer step 6 says
*"snapshot queue/constraints/view ACLs"*, and the third one is new in round 4:
013 and 014 both `DROP` and recreate views, and a dropped view takes its grants
with it. 014 replays owner and ACL automatically inside its own transaction,
013 does not, and neither leaves you a record. Run **preflight P3** in
`scripts/sql/acceptance_014_status.sql` and keep the output; extend the same
query to `v_alert_queue` / `v_alert_digest_daily` for 013's pair. Unresolved
view dependants **or ACLs** is a listed no-go condition.

**Preflight, only if any pre-release copy of 012 or 013 has ever been applied
to this database.** On a virgin database all three of these return nothing and
you can move on.

```sql
-- (a) 012 tightens lease_shape to name claimed_at in BOTH halves. The new
--     constraint is validated against existing rows, so these rows would make
--     the ALTER fail. They are leases nobody holds; clear them.
SELECT uniqueid, status, claim_token, claimed_at, claim_until
  FROM call_ingest_jobs
 WHERE status NOT IN ('transcribing','evaluating') AND claimed_at IS NOT NULL;

-- (b) 013 replaces both alert views with a DIFFERENT COLUMN SHAPE, so it drops
--     them first. Anything depending on them will make the DROP fail (which is
--     the outcome we want -- it fails loudly rather than cascading).
SELECT dependent_ns.nspname, dependent_view.relname
  FROM pg_depend d
  JOIN pg_rewrite r        ON r.oid = d.objid
  JOIN pg_class dependent_view ON dependent_view.oid = r.ev_class
  JOIN pg_class source_table   ON source_table.oid = d.refobjid
  JOIN pg_namespace dependent_ns ON dependent_ns.oid = dependent_view.relnamespace
 WHERE source_table.relname IN ('v_alert_queue','v_alert_digest_daily')
   AND dependent_view.relname NOT IN ('v_alert_queue','v_alert_digest_daily');

-- (c) 013's rule seed is ON CONFLICT DO NOTHING, so an already-seeded
--     complaint_or_cancellation keeps its old is_alert = true. 013 carries an
--     explicit, narrowed UPDATE that corrects it; note what it is now, so you
--     can tell a correction from a surprise afterwards.
SELECT rule_code, rule_version, is_alert, active, params
  FROM alert_rules ORDER BY rule_code;
```

### Step 4 — apply 012, then 013, then 014

There is no psql path to the Railway database from this machine, so the
migrations go in through a throwaway n8n workflow. (This is also why **A13 is a
staging test**: it needs two persistent sessions, and an n8n Postgres node
cannot give you one — see A13 in `scripts/sql/acceptance_pr1a.sql`.)

1. Create a new workflow with a **Webhook** node (`responseMode: onReceived`,
   a random path) wired to a **Postgres → Execute Query** node using the
   existing `railway-pg (api)` credential (id `eYWvPxQFAwKs0bOu`).
2. Paste `db/migrations/012_call_job_leases.sql`, `curl` the webhook once, read
   the execution log.
3. Replace the query with `db/migrations/013_alert_rules.sql`, `curl` again.
4. Replace the query with `db/migrations/014_evaluation_status.sql`, `curl`
   again. **Run preflights P1–P4 in `scripts/sql/acceptance_014_status.sql`
   first:**
   - **P1** — 014 drops and recreates `v_agent_scorecard` and
     `v_quality_by_input` (their column shape changes), with a plain `DROP` and
     never a `CASCADE`, so anything that depends on either view makes the
     migration fail rather than disappear. Expect zero rows.
   - **P2 / P2b / P2c** — the backfill population. **P2b** is the one that
     reconciles: it counts only the rows `v_agent_scorecard` can see (inner
     join to `interactions` and `agents`, `is_bot = false`), which is the
     like-for-like comparison the earlier revision got wrong. P2 is the whole
     table and P2c is the difference between them.
   - **P3** — view owner and ACLs, kept for comparison against acceptance
     section 7 afterwards.
   - **P4** — that the migration role owns `agent_evaluations` and both views.
     014 disables `t_eval_updated` around the backfill and re-owns the views;
     a role that cannot do either aborts the transaction halfway.

   014 wraps itself in `BEGIN; … COMMIT;`, so the n8n node's implicit
   transaction is belt and braces rather than the only thing protecting the
   view drop. Full rationale in `docs/PR2-db-status.md` §4.
5. **Delete the workflow.** A live webhook that runs arbitrary SQL is not
   something to leave switched on.

### Step 5 — verify the schema

Run sections 1–4 of `scripts/sql/acceptance_pr1a.sql` (columns, constraints,
backfill, indexes) and sections 1–4 of `scripts/sql/acceptance_pr1b.sql`
(rules, params, function, views, queue vocabulary), then sections 1–7 and
R1–R11 of `scripts/sql/acceptance_014_status.sql` (status columns, the
usable-score function, the two parameter tables, the co-ordinate binding, the
rebuilt views, the grants). All three migrations are idempotent — running them
twice is a safe way to confirm them, and reviewer step 2 requires exactly that
on staging.

Seven of the 014 checks must be **read**, not just run:

- **section 3b** — `t_eval_updated` is enabled again, and the backfill did not
  stamp migration day onto the null-score history.
- **section 4** — `gradeable = true AND final_score IS NULL` must be **0**.
- **section 5b** — the noise co-ordinate binding: 188.70 answers for
  `pass2-agent-quality-v3 / 1.0.0 / deepseek-chat` and **NULL** for the shipping
  co-ordinate. NULL there is correct, and it is why no band publishes yet.
- **section 5c** — the complete CI takes the larger of the observed spread and
  the noise floor, and returns NULL for an unmeasured co-ordinate.
- **section 7** — view owner and ACL identical to preflight P3.
- **R1 / R2 / R2b** — the counts query (`buckets_partition_total` true,
  `contract_failed` zero), per-agent reconciliation, and the scorecard total
  against P2b.
- **R10** — every `unscoreable:` dead-lettered job has exactly one stored
  `unscoreable` evaluation row.

**Expect every scorecard row to be unpublishable at this point.** `band_stable`
is false everywhere because the shipping prompt/model co-ordinate has no
measured noise floor. That is the designed behaviour (`docs/PR2-db-status.md`
§3, rule 2), not a broken view, and **R5** prints that exact reason per row.

Section 1 of `acceptance_pr1a.sql` now expects **eight** columns, not six:
`alerts_evaluated_at` and `alerts_error` come from 013 and live on
`call_ingest_jobs`, because that is the row whose alert evaluation they
describe. `acceptance_pr1b.sql` §P proves the rule parameters are actually read
by flipping them — run it on a copy or accept the fixture rows it rolls back.

### Step 6 — publish the new workflow, **inactive**

`n8n/workflows/02-calls-ingest-evaluate.json` is a mirror of the live workflow.
Credentials are bound by internal id (gotcha 8) and the ids in this file are
the real ones, so the update path is the n8n API with a
`scripts/n8n_setup.py`-style patch — **not** an import through the UI, which
would leave every node's credential unset.

Before pushing:

```bash
pip install sqlglot                      # optional, enables the SQL parse
python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json
```

Expect `0 error(s), 0 warning(s)`.

After pushing, with the workflow still inactive, confirm in the UI that all
**sixteen** Postgres nodes show the `railway-pg (api)` credential and that
`WORKER_URL` / `WORKER_API_KEY` are set on the n8n service.
**`ALERT_WEBHOOK_URL` is no longer used and can be removed** — there is no
sender.

### Step 7 — put every rule in dry run

```sql
UPDATE alert_rules SET is_alert = false;
```

Occurrences are still recorded, as `suppressed`, and `v_alert_digest_daily`
still counts them. Nobody is asked to work a queue whose precision has not been
measured yet. (`complaint_or_cancellation` and `promise_open_or_overdue` are
seeded this way already; this covers the two hot-lead rules.)

### Step 8 — one controlled transcription and one controlled judge retry

Pick two rows by hand and give the workflow exactly them. **Back up the real
schedule first** — the park below overwrites `next_attempt_at` for the whole
retryable queue, and without this the cool-downs that keep a throttled ASR
provider from burning the backlog are simply gone.

**8.0a — an existing backup table is a HARD STOP.** This step used to begin
`DROP TABLE IF EXISTS tmp_pr1a_next_attempt_backup`. If a previous rollout was
interrupted anywhere between step 8 and step 9b, that table is the **only**
surviving copy of the real schedule, and dropping it destroys every cool-down in
the queue and immediately replaces the backup with a snapshot of the
already-parked queue — silently, and with nothing left to restore from. Never
drop it blind.

```sql
-- 8.0a does one already exist?
SELECT to_regclass('tmp_pr1a_next_attempt_backup') AS must_be_null;
```

- **NULL** → continue to 8.0b.
- **NOT NULL** → **stop.** Do not drop it and do not overwrite it. It is either a
  leftover from an interrupted rollout — in which case it is the only record of
  the real schedule — or somebody else's table wearing the same name. Inspect it
  before you decide which:

  ```sql
  SELECT count(*) AS rows, min(next_attempt_at), max(next_attempt_at)
    FROM tmp_pr1a_next_attempt_backup;

  SELECT j.uniqueid, j.status,
         j.next_attempt_at AS live, b.next_attempt_at AS backed_up
    FROM tmp_pr1a_next_attempt_backup b
    JOIN call_ingest_jobs j ON j.uniqueid = b.uniqueid
   WHERE j.next_attempt_at IS DISTINCT FROM b.next_attempt_at
   LIMIT 20;
  ```

  Rows from the second query mean **the queue is still parked** from the
  interrupted run: run step 9b with *this* table first, verify it, drop it, and
  only then start step 8 over. If you conclude the table is stale, **rename it,
  never drop it**:

  ```sql
  ALTER TABLE tmp_pr1a_next_attempt_backup
    RENAME TO tmp_pr1a_next_attempt_backup_20260822_1830;
  ```

**8.0b — save.** A REAL table, not TEMP: the n8n Postgres node runs on a pooled
connection and a temp table would not survive to step 9b. Plain `CREATE TABLE`,
no `IF NOT EXISTS` — a table that appeared between 8.0a and here must fail this
step loudly rather than be silently reused.

```sql
CREATE TABLE tmp_pr1a_next_attempt_backup AS
SELECT uniqueid, next_attempt_at
  FROM call_ingest_jobs
 WHERE status IN ('discovered','transcribed','asr_failed','judge_failed');
```

**8.0c — it is not a backup until you have looked at it.** The two counts must
be **equal**. `rows_backed_up = 0` is only correct if the retryable queue is
genuinely empty, and the second query is what tells you which of the two you are
looking at. Do not run the park until they match.

```sql
SELECT count(*) AS rows_backed_up,
       count(*) FILTER (WHERE next_attempt_at IS NULL) AS null_schedule
  FROM tmp_pr1a_next_attempt_backup;

SELECT count(*) AS must_equal_rows_backed_up
  FROM call_ingest_jobs
 WHERE status IN ('discovered','transcribed','asr_failed','judge_failed');
```

```sql
-- everything else parked out of reach
UPDATE call_ingest_jobs SET next_attempt_at = now() + interval '2 hours'
 WHERE status IN ('discovered','transcribed','asr_failed','judge_failed');

-- one fresh transcription
UPDATE call_ingest_jobs SET next_attempt_at = now()
 WHERE uniqueid = '<a discovered uniqueid>';

-- one stored-transcript judge retry
UPDATE call_ingest_jobs
   SET status = 'judge_failed', judge_attempts = 0, next_attempt_at = now(),
       claim_token = NULL, claimed_at = NULL, claim_until = NULL
 WHERE uniqueid = '<an evaluated uniqueid>';
```

Trigger the workflow manually once.

### Step 9 — inspect

- **Job state.** Both rows `evaluated`, `claim_token`/`claimed_at`/`claim_until`
  all null, `last_error` null.
- **Attempts.** The retry row shows `judge_attempts = 1`, not 2 — the claim
  spent it and `Begin judge attempt` must not have spent a second one.
- **Transcript identity.** The retry's `transcripts.transcribed_at` is
  unchanged, and `Cohere Arabic ASR` does not appear in that item's run at all.
- **Provenance.** `interaction_analysis.prompt_version` / `model` and
  `agent_evaluations.prompt_version` / `rubric_version` / `model` match the
  worker that just ran, not the previous one.
- **Alert evaluation landed.** Both rows show `alerts_evaluated_at` set, and
  `alerts_error` null:
  ```sql
  SELECT uniqueid, status, alerts_evaluated_at, alerts_error
    FROM call_ingest_jobs WHERE uniqueid IN ('<a>','<b>');
  ```
- **Invariants.** Run I1–I5 in `scripts/sql/acceptance_pr1a.sql`. All five
  return zero rows.
- **Occurrences.** `SELECT * FROM alert_occurrences` — every row `suppressed`
  at this stage.

### Step 9b — restore the schedule you parked

**Do this before activating.** Until it runs, the entire retryable queue is
sitting two hours in the future and the first activated run will look
deceptively quiet.

The restore is **idempotent**: it only touches rows that still differ from the
backup, so running it twice is a no-op the second time and running it after a
partially applied restore finishes the job. Run it **twice** — the second run
returning zero rows is the idempotence check, and it costs nothing.

```sql
UPDATE call_ingest_jobs j
   SET next_attempt_at = b.next_attempt_at
  FROM tmp_pr1a_next_attempt_backup b
 WHERE b.uniqueid = j.uniqueid
   AND j.next_attempt_at IS DISTINCT FROM b.next_attempt_at
RETURNING j.uniqueid, j.status, j.next_attempt_at;
-- first run: the parked rows. second run: ZERO rows.
```

Then verify, in this order. **Do not drop the backup table until all three
pass** — until then it is still the only copy of the schedule.

```sql
-- (i) every backed-up row matches the live row again
SELECT count(*) AS must_be_zero
  FROM call_ingest_jobs j
  JOIN tmp_pr1a_next_attempt_backup b ON b.uniqueid = j.uniqueid
 WHERE j.next_attempt_at IS DISTINCT FROM b.next_attempt_at;

-- (ii) nothing that was backed up has disappeared in the meantime.
--      A row deleted between step 8 and now is listed here so you SEE it.
SELECT b.uniqueid
  FROM tmp_pr1a_next_attempt_backup b
  LEFT JOIN call_ingest_jobs j ON j.uniqueid = b.uniqueid
 WHERE j.uniqueid IS NULL;

-- (iii) nothing retryable is still parked by step 8. The park was +2 hours and
--       the real cool-downs are 45 minutes, so anything past 90 minutes is
--       either a leftover park or a cool-down you can name.
SELECT uniqueid, status, next_attempt_at
  FROM call_ingest_jobs
 WHERE status IN ('discovered','transcribed','asr_failed','judge_failed')
   AND next_attempt_at > now() + interval '90 minutes'
 ORDER BY next_attempt_at;
```

```sql
-- only after (i)-(iii) have passed
DROP TABLE tmp_pr1a_next_attempt_backup;
```

The two rows you deliberately re-opened in step 8 are `evaluated` by now, so
they are not in the backup table, are not counted by (iii), and are not
affected. Do not activate while (iii) returns rows you cannot explain.

### Step 10 — activate, and watch two overlapping cron boundaries

Activate. Watch at least **two** `*/15` boundaries where an execution is still
running when the next one starts — that is the case the whole PR is about.

The previous version of this step asked for `count(DISTINCT claim_token) > 1`
**grouped by `uniqueid`**. `uniqueid` is the primary key, so each group is one
row and each row has one current token: that query cannot return anything, ever,
however broken the claim is. It is replaced by checks that can actually fail.

```sql
-- T1 . one token per ROW, not one token per BATCH.
-- gen_random_uuid() is volatile and is evaluated per row in the claim's
-- UPDATE. If somebody ever hoists it into the CTE, every row in a batch shares
-- a token, and one execution's terminal write then unfences all six. This is
-- the query that notices.
SELECT claim_token, count(*) AS rows_sharing_it,
       array_agg(uniqueid ORDER BY uniqueid)
  FROM call_ingest_jobs
 WHERE claim_token IS NOT NULL
 GROUP BY claim_token HAVING count(*) > 1;                   -- expect none

-- T2 . one execution claims at most `batch size` rows.
-- Every row claimed by one statement shares claimed_at to the microsecond
-- (now() is transaction time), so this groups by execution. A group larger than
-- 6 means either LIMIT stopped working or 'Claim work' lost `executeOnce` and
-- ran once per item of the recovery sweep's output.
SELECT claimed_at, count(*) AS claimed_together
  FROM call_ingest_jobs
 WHERE claimed_at > now() - interval '2 hours'
 GROUP BY claimed_at HAVING count(*) > 6;                    -- expect none

-- T3 . nothing is being re-transcribed by an execution that does not hold it.
-- A transcript may only be written by an execution holding the row in
-- 'transcribing'. This is a sampled check, not a proof -- the row can move
-- between the write and your look -- so treat a single hit as "re-run it", and
-- a repeatable hit as a finding.
SELECT j.uniqueid, j.status, j.claimed_at, t.transcribed_at, j.asr_attempts
  FROM transcripts t
  JOIN call_ingest_jobs j ON j.interaction_id = t.interaction_id
 WHERE t.transcribed_at > now() - interval '15 minutes'
   AND (j.status <> 'transcribing' OR t.transcribed_at < j.claimed_at)
 ORDER BY t.transcribed_at DESC;

-- T4 . the follow-up queue is not silently falling behind (PR1B).
-- On a healthy pipeline this drains to zero on every sweep. `failing` > 0 means
-- reconcile_alert_evaluations() is catching a real error -- read alerts_error.
SELECT count(*) FILTER (WHERE alerts_error IS NULL)     AS never_attempted,
       count(*) FILTER (WHERE alerts_error IS NOT NULL) AS failing,
       min(updated_at)                                  AS oldest
  FROM call_ingest_jobs
 WHERE status IN ('evaluated','judge_failed','dead_letter')
   AND interaction_id IS NOT NULL
   AND alerts_evaluated_at IS NULL;

-- T5 . the five invariants I1-I5 stay clean.
```

The direct proof that two executions cannot share a row is **A13**, not a
monitoring query: it is a two-session test, run deliberately on staging or a
restored copy, not watched for. It cannot be run through n8n — see the A13
header in `scripts/sql/acceptance_pr1a.sql` for why pooled Postgres nodes cannot
hold a transaction open across two nodes.

Then let it run a week with rules suppressed, read a sample of
`fact_snapshot`, and only then `UPDATE alert_rules SET is_alert = true` on the
two hot-lead rules.

### Step 11 — the gates that are still open after activation

Activation is not the end of the rollout. Two things are deliberately not
blockers for switching the workflow on, and are equally deliberately not
finished:

- **D6's scoring fixture** (§5). Owned by the worker/prompt PR, not this one.
  Until it has run, Module 4 on calls is provisional — do not build coaching
  on it and do not report it as a trend.
- **Rule precision** (PR1B §8). Every rule stays `is_alert = false` until a week
  of suppressed occurrences has been sampled by hand.

---

## 6b · Evaluation statuses & the usable-score rule (014)

**Short form. The full document is `docs/PR2-db-status.md`; the SQL is
`db/migrations/014_evaluation_status.sql` and
`scripts/sql/acceptance_014_status.sql`.** It is summarised here because it
lands in the same rollout, on the same table the runbook above touches.

**Not every `agent_evaluations` row is a score.** Pass 2 returns four things:

| `contract_status` | number? | what workflow 02 does |
|---|---|---|
| `ok` | yes | store |
| `ungradeable` — too little of the rubric survived evidence checking | no | **store** (terminal data quality, never retried) |
| `unscoreable` — there was nothing to grade | no | **store, then dead-letter.** `Store evaluation` when it arrives beside a pass 1; `Store unscoreable outcome` → `Mark unscoreable` when the worker refused before pass 1 ran. Terminal either way, and counted either way |
| `contract_failed` — the response contradicted the rubric | no | `judge_failed`, retried; **no row written** |

**A score is usable only when** `contract_status = 'ok'` **and** `gradeable`
**and** `final_score IS NOT NULL`. That rule is written down exactly once, as
`eval_score_is_usable()`, and every view calls it. Do not restate it anywhere.

014 lifts those statuses out of `raw_response` into real columns
(`contract_status`, `gradeable`, `ungradeable_modules`, `evidence_rejected`,
`model_fingerprint`) and rebuilds `v_agent_scorecard` / `v_quality_by_input` so
that the counts and the averages describe the same rows. The old views did not:
`count(*)` counted ungradeable rows in the denominator while `avg(final_score)`
silently skipped them, so a bad ASR day read as *"40 evaluated, average 78"*
when it was *"12 scored, average 78, and 28 we could not grade"*.

Two reporting rules come with it, and they are not optional:

- **N ≥ 30, and never compare across prompt versions.** The day-13 A/A run —
  same prompts, same code, same 81 calls, judged twice — has repeat-run
  variance **188.70**, so a mean moves by `±26.9/√N` at 95% for no reason at
  all (±4.92 at N=30, ±2.99 at N=81). Below 30 usable scores, a mean is not a
  measurement. And prompt-attributable RMS was **14.15 points** — a whole band
  — so `v_agent_scorecard` groups by `prompt_version`, `rubric_version`,
  `model` and `model_fingerprint`, and one agent gets **one row per version
  co-ordinate**. Rows whose version columns differ are separate measurements.
  A **fingerprint change is a version change.**

- **A band is published only when its COMPLETE interval stays inside it.**
  Round 4 blocked the first version of this: it published the judge-noise floor
  as if it were the confidence interval and gated on it, which is a lower bound
  on the uncertainty being used as proof of stability. The interval is now
  `z₉₅ × √(max(sample_var, noise_var) / N)` — the observed between-call spread,
  floored at the judge noise, `max()` and not a sum because the judge noise is
  already inside the observed spread. `ci95_half_width` is the gate;
  `noise_floor_half_width` is published beside it as the floor alone.
  `band_stable` is false unless N ≥ 30 and both ends of that interval fall in
  the same band. 11 of 68 bands flipped in the A/A run with no prompt change; a
  band straddling a boundary is a coin flip shown to a person about their own
  work.

- **The noise measurement is versioned, and the shipping co-ordinate has none
  yet.** Round 4's other blocking finding: the noise parameter was global while
  the means were version-grouped, so re-measuring after a prompt change would
  retroactively re-state the uncertainty of every historical group.
  `eval_noise_params` is now keyed by the same four co-ordinates the views group
  by (NULL fingerprint = wildcard), and 188.70 is bound to
  `pass2-agent-quality-v3 / 1.0.0 / deepseek-chat`, where it was measured. The
  worker ships whatever `judge.PASS2_VERSION` currently is (v6 as this is
  written) against `deepseek-v4-flash`, so **after
  rollout `band_stable` is false everywhere** until the A/A run is repeated on
  the shipping co-ordinate and its variance inserted. Fail closed: a missing
  parameter hides bands rather than inventing certainty. The co-ordinate-free
  constants (`z_95`, `min_n_publish`) live in `eval_report_params`.

**The `unscoreable` retry loop is closed, and the outcome is recorded.** The
worker's refusal short-circuits `/evaluate` *before* pass 1 runs, so the
response is a 200 carrying a `pass2` block and no `pass1`. `Evaluation ok?`
tests `!error && !!pass1`, so it took its false output and the row went to
`Mark judge failed` — one judge attempt every 45 minutes until the budget of
five was gone, then `dead_letter`. Five model calls that were never going to be
made, to reach the same state.

A new IF, **`Nothing to evaluate?`**, sits on that false output. Round 4
reversed what happens next: the earlier revision dead-lettered the job and wrote
**no** evaluation row, which made `unscoreable_count` unreachable for the
commonest way a call becomes unscoreable and hid the worst input-quality cases
from every report. The path is now

```
Nothing to evaluate? (true)
  -> Store unscoreable outcome   one fenced agent_evaluations row:
                                 contract_status 'unscoreable', gradeable false,
                                 final_score null, the worker's reason in notes
  -> Unscoreable stored?         zero rows (lease lost) = hard stop
  -> Mark unscoreable            terminal dead_letter, lease cleared, no retry
```

The row is written **before** the job is terminalised: a crash in between leaves
a stored outcome on a job the sweep can still finish, where the other order
would leave a `dead_letter` with no evaluation — the exact state this change
removes. Errors, 422s and a missing pass 1 with no explanation still go to
`Mark judge failed` and are still retried. Reviewer step 4 (§6) is the
end-to-end fixture for all of it.

One known gap remains, recorded in `docs/PR2-db-status.md` §5: workflows 01 /
01b (chats) still write evaluations without a status, and those nodes are not
owned by this PR.

---

## 7 · Rollback

**Re-enabling the old workflow while rows sit in `transcribing` or `evaluating`
is unsafe.** The old claim does not know those statuses: those rows become
invisible to it *permanently*, and the old workflow re-transcribes every judge
retry (D2) on everything else. Drain first.

1. Deactivate the new workflow. Wait for its executions to finish or cancel
   them.
2. Force the sweep to reclaim everything in flight:
   ```sql
   UPDATE call_ingest_jobs SET claim_until = now() - interval '1 second'
    WHERE status IN ('transcribing','evaluating');
   ```
   then run `scripts/sql/02_recover_expired_leases.sql` with `$1 = 0, $2 = 5`.
   Rows come back as `asr_failed` / `judge_failed` / `dead_letter`, all of which
   the old workflow understands.
3. Prove the drain finished — this must be **zero** before anything is
   reactivated:
   ```sql
   SELECT count(*) FROM call_ingest_jobs
    WHERE status IN ('transcribing','evaluating');
   ```
4. Reactivate the old workflow.
5. **Do not roll back 012 or 013.** Both are additive; the extra columns,
   statuses, constraints, tables and views are invisible to the old workflow,
   and dropping them destroys the attempt history that the next attempt at this
   deploy needs. If the rules must stop evaluating: `UPDATE alert_rules SET
   active = false;`.

One caveat to state plainly: the `dead_letter` rows the new workflow created
with `asr_quality_red:` reasons stay dead-lettered under the old workflow too.
That is correct — the audio has not improved — but it means the rollback is not
a perfect undo, and nothing about it is.

---

## 8 · Acceptance tests

Full SQL in `scripts/sql/acceptance_pr1a.sql`. Every mutating test creates its
own fixture rows and rolls them back; none of them expires a live lease. Two
exceptions: **A13**, which needs two sessions to see each other, and **A16/F1**,
which needs a real workflow execution to make the write it proves. Both fixtures
are **committed** and removed by an explicit cleanup at the end of their section
— which is also why both are staging / restored-copy tests and not production
ones.

**None of these has been run.** Nothing in this PR has touched a database:
`acceptance_pr1a.sql` and `acceptance_pr1b.sql` both say so in their opening
lines and it is still true. A12's workflow half, **A13 in full** and **A14 in
full** cannot be run until step 4 of the runbook has applied the migrations
somewhere — staging or a restored copy — and they are rollout gates, not
optional extras. A13 additionally needs **psql**, which this machine does not
have against Railway (step 4), and its fixture is **committed** rather than
rolled back, so it also carries a mandatory cleanup. Anyone reading this section
as "the tests pass" is reading it wrong.

| | test |
|---|---|
| **A1** | Two claims cannot take the same row. Run the body of `02_claim_work.sql` in two sessions (session 1 inside an open transaction). Session 2 returns a disjoint set and does not block. |
| **A2** | A claimed row is invisible to the claim. |
| **A3** | **A lost lease cannot write — to anything.** Five sub-tests, one per durable write: job status, transcript+link, pass 1, pass 2, alert evaluation. Each must report zero rows under a wrong token, *and* the target table must be unchanged afterwards. **A3f** adds the expired-but-not-yet-swept case: the *correct* token against a row whose `claim_until` has passed must also write nothing. |
| **A4** | Recovery reopens a dead lease and only dead-letters at the cap — against three purpose-built fixture rows (`ACC-A4-*`), not against whatever happens to be in flight. |
| **A5** | A judge retry does not re-transcribe, and spends exactly one judge attempt. |
| **A6** | A red transcript is stored, then dead-lettered, **without** spending a judge attempt. |
| **A7** | Pass 1 survives a pass-2 failure. |
| **A8** | A quiet day still drains the backlog (`/calls/list` returning `count: 0`). |
| **A9** | `python scripts/check_workflow_json.py …` exits 0. |
| **A10** | **Golden renderer cases** — nine fixtures covering unknown/known speaker, whitespace-only text, empty and absent text, missing and out-of-order `seq`, a call past 99 minutes, fractional seconds, and the empty transcript. |
| **A11** | **The lease covers the batch.** A mixed six-item batch (three heading for ASR, three for the judge): six distinct tokens, `claim_until - now() ≥ 2h59m`, `asr_attempts = 3` and `judge_attempts = 3`. Then push `claimed_at` back 50 minutes — well past the old 900 s lease — and confirm neither a second claim nor the recovery sweep touches those rows. |
| **A12** | **A zero-row handoff is a hard stop.** Both halves: SQL (every fenced write returns zero rows under a wrong token; `Begin judge attempt` also returns zero when the judge budget is exhausted) and workflow (steal the token mid-execution, confirm `Transcript stored?` takes its false output and nothing after it runs). **A12c** covers the new dead-letter path: an exhausted budget with the right token must produce `dead_letter` + `judge_budget_exhausted_before_handoff` and a cleared lease; the same node with a stale token must produce `UPDATE 0` and leave the row alone. |
| **A13** | **The recovery-versus-upsert race, in two REAL sessions** — two psql terminals. Not n8n: its Postgres nodes are pooled, so `BEGIN` in one node and `COMMIT` in another are not guaranteed the same session. The fixture lease is **live for 60 seconds**, and the two halves disagree about it *only* through `now()`, which is transaction-start time: a transaction that began inside the window sees a live lease, one that began after it sees an expired one, and neither has to falsify the row. **A13a** — the writer begins inside the window, its fence passes, it renews `claim_until` in-statement and holds the row `FOR UPDATE`; recovery then begins *after* expiry, qualifies, and must **block**; after the writer commits, recovery's `EvalPlanQual` recheck sees the renewed lease and must touch **zero rows**. **A13b** — the mirror: recovery wins the lock first, the writer (whose own `now()` is inside the window, so it qualifies and blocks rather than being filtered out) must return **zero rows** on recheck, with `transcripts` untouched. Under the old unlocked fence A13b does not block — it overwrites. Every step that must land inside or outside the window carries a `window_ok` assertion, so a missed window reads as *invalid run*, never as a pass. **Round-4 hardening:** each half records the backend PID of the session that is supposed to block (`pg_backend_pid()`) and the other session filters `pg_stat_activity` by **that pid** — "exactly one lock waiter in this database" was satisfiable by any unrelated waiter on a shared staging copy — and both sessions assert `SHOW transaction_isolation` = `read committed`, because every claim the test makes about rechecking and re-qualifying is READ COMMITTED behaviour and a REPEATABLE READ server would end the run in a 40001 that proves nothing. **A13z is a scaffold, not a test:** it opens two persistent psql sessions and contains a literal `...` where the test goes; it runs no assertions and cannot pass or fail. A13a/A13b, read by a human, remain the gate. |
| **A14** | **An injected alert-function failure loses nothing — and it is inspectable.** One transaction driven by `SAVEPOINT`s, because `ROLLBACK TO SAVEPOINT` undoes only the failed call and leaves the fixture in place, which is what lets the stamp actually be `SELECT`ed afterwards. **0** — positive control with the real function (one occurrence, stamp set), so step 1 cannot "pass" because the *fence* failed to match. **1** — break `evaluate_alert_rules()` inside the transaction, run the alert node's statement: it must **raise**, and after `ROLLBACK TO SAVEPOINT` the row must still show `alerts_evaluated_at IS NULL`, `alerts_error IS NULL`, zero occurrences. **2** — a **poison row** (bad *data*, real function: `"maybe"` where a boolean is cast, so 22P02 fires for that interaction only) with an *older* `updated_at` than a healthy job: `reconcile_alert_evaluations()` must not raise, must return `error_text` for the poison and stamp the healthy job behind it. **3** — the stamp is set, occurrences exist, and a second sweep returns nothing for the healthy job. |
| **A15** | **Counts and averages describe the same rows (014).** `scripts/sql/acceptance_014_status.sql`. **R1** is the counts query — total vs scored vs ungradeable vs unscoreable vs contract_failed vs ok-without-score, plus `buckets_partition_total`, which must be true: the five buckets partition the table exactly. `contract_failed` must be **0** (workflow 02 writes no row for it). **R2** repeats the reconciliation per scorecard row, and **R2b** ties the scorecard's total to preflight **P2b** — the like-for-like population, which the earlier revision got wrong by comparing the whole table to a view that inner-joins `agents` and excludes bots. **Section 4** asserts the invariant the backfill and both store nodes maintain: zero rows with `gradeable = true AND final_score IS NULL`. **Section 3b** asserts the backfill did not re-stamp `updated_at` and that `t_eval_updated` is enabled again. **Section 5b** asserts the noise co-ordinate binding — 188.70 answers for `pass2-agent-quality-v3 / 1.0.0 / deepseek-chat` and **NULL** for the shipping co-ordinate. **Section 5c** asserts the complete CI takes the larger of the observed spread and the noise floor, and fails closed on an unmeasured co-ordinate. **Section 7** compares view owner and ACL against preflight P3, because DROP+CREATE makes new objects with no grants. **R4** pins the SQL band boundaries (85/70/55) against `performance_level()` in the worker. **R5** prints exactly which agent means are publishable today and why the rest are not; **R5b** prints how much wider the honest interval is than the noise floor the first draft gated on. **R9** is the queue side of the `unscoreable` routing fix: every job whose `last_error` starts `unscoreable: ` must be **dead_letter** with `judge_attempts = 1` — a row sitting in `judge_failed`, or one that reached `dead_letter` with 5 attempts, means the response is being retried as a judge fault again. **R10/R10b** reconcile the two places `unscoreable` lands: every dead-lettered `unscoreable:` job must now have exactly one stored `unscoreable` evaluation row, and the pass-1-succeeded variant is counted once and separately. **R11** asserts nothing score-shaped got onto an unscoreable row. |
| **A16** | **The unscoreable end-to-end fixture** (`acceptance_014_status.sql` §F1) — reviewer step 4, and the only test that exercises the reversed round-4 decision end to end. A fixture call whose transcript normalises to under 100 characters of speech but whose ASR quality is green, run through one execution of workflow 02 on staging. All four must hold: **one stored status outcome** (exactly one row, `unscoreable`, `gradeable` false, `final_score` null, the worker's reason in `notes`, nothing score-shaped), **terminal job** (`dead_letter`, lease cleared, `last_error` starting `unscoreable: `), **one judge attempt** (`judge_attempts = 1`, `retries` unchanged), and **no retry** (a second execution 45+ minutes later changes neither). Its fixture is **committed** and carries a mandatory cleanup, like A13's. |

---

## 9 · The validator

`scripts/check_workflow_json.py` gained three checks, each of which would have
caught one of this PR's blocking defects before review:

- **`check_returning_gates`** — a conditional `RETURNING` mutation that feeds a
  node reading `$json` must feed an IF/Switch first. This is the zero-row
  fall-through as a lint rule. Feeding a node that reads only
  `$('Named node')` or constants is fine and reported as a note.
- **`check_lease_fencing`** — a statement that writes durable data (including
  via `evaluate_alert_rules()` or `reconcile_alert_evaluations()`, which write
  behind a `SELECT`) must mention `claim_token`. Four statements legitimately
  cannot: the discovery INSERT, the recovery sweep, the claim itself and the
  reconciliation sweep. They opt out with a `-- lease-exempt: <reason>` comment
  **inside the SQL**, so the exception and its justification live in the same
  place as the code they excuse.
- **The fence must LOCK** (revision 3) — carrying `claim_token` is not enough.
  A fenced write must contain `FOR UPDATE`, or declare
  `-- fence-exempt: <reason>`. The exemption that is actually used is
  "single-statement UPDATE": `UPDATE … WHERE claim_token = $t` locks the row and
  re-evaluates its own predicate itself, so there is no read-then-write window
  to widen. Everything with a CTE chain has to take the lock explicitly. This is
  the check that turns "the fence is a check, not a lock" from a review finding
  into a lint error.

Current output on this workflow: **`0 error(s), 0 warning(s)`** — 35 nodes,
39 edges, 17 queries parsed by sqlglot, 7 gated conditional-RETURNING edges,
11 fenced writes — 5 locked with `FOR UPDATE`, 6 declared `fence-exempt` — and
4 declared `lease-exempt`. 16 branch/output pairs checked for cross-branch
reads.

The cross-branch check earned its keep during this revision: the first draft of
`Dead-letter judge budget` explained itself by naming `$('Two AI passes')` **in
a SQL comment**, and the validator failed the workflow for it. SQL comments are
not stripped before that check on purpose — an expression is an expression
wherever it appears, and n8n would have resolved it.

---

## 10 · Known residual risks

- **A batch of six is fenced for three hours.** A genuinely crashed execution
  therefore parks its rows for up to that long before the sweep reopens them.
  Accepted deliberately (§2); the alternative is a lease that can expire under
  a running worker.
- ~~**`Begin judge attempt` can return zero rows on an exhausted judge budget**~~
  — **fixed in revision 3.** It used to park the row for three hours, have the
  sweep re-queue it as ASR work, and burn the *ASR* budget re-transcribing audio
  for a judge that could never run. The false output of `Judge attempt started?`
  now goes to `Dead-letter judge budget`: one fenced statement whose `WHERE`
  requires **both** the original token and `judge_attempts >= cap`, so the
  exhausted-budget case dead-letters immediately with
  `judge_budget_exhausted_before_handoff`, and the other cause of a zero row — a
  stale token — matches nothing and stays the leaf it should be. It reads
  nothing from the judge branch, which is why it could not simply reuse
  `Mark judge failed`. Covered by A12c.
- **A durable write whose lease expired mid-flight now throws its own work
  away.** The fences require `claim_until > now()`, so an execution that
  overshoots its lease returns zero rows rather than overwriting whoever holds
  the row next. That is the intended trade — the alternative is the race this
  revision exists to close — but it does mean a slow batch loses work that used
  to (unsafely) land. The ~48% lease margin in §2 is what keeps it rare, and
  I4/A11 are what tell you if it stops being rare.
- **A blocked durable write waits on the recovery sweep's lock.** `FOR UPDATE`
  means a statement can now wait where it previously raced. The wait is bounded
  by one short `UPDATE` on `call_ingest_jobs` and the locking order in §2 has no
  cycle in it, but "n8n node took 40 s" is a new shape of slow that did not
  exist before. `statement_timeout` is not set on the Postgres credential; if
  that ever becomes a problem, that is the knob.
- **Alert evaluation is eventually-consistent, not atomic** (PR1B §5). A job can
  be terminal for up to one sweep interval before its occurrences exist. This is
  deliberate — folding the rules into the terminal update would let one broken
  rule roll back every terminalization — but it means "evaluated" and "in the
  follow-up queue" are not the same instant, and a digest read in that window is
  short. T4 in step 10 is the monitor.
- **A low-confidence transcript found on the *evaluate* path spends a judge
  attempt but is re-queued as `asr_failed`.** The accounting is off by one for
  that row. It needs a job that cannot exist yet (a `transcribed` row whose
  stored confidence is below the floor), so it is recorded rather than papered
  over.
- **Two connections feed `Load stored transcript`.** n8n runs the downstream
  chain once per incoming branch. Correct, but the execution log shows two runs
  of each downstream node on a mixed batch.
- **The follow-up history block is not a mirror of
  `metrics.followup_history_block()`** and needs a scoring fixture before it is
  trusted for Module 4. See §5 — that fixture (D6) is a rollout gate owned by the
  worker PR, tracked as runbook step 11.
- **Workflows 01 and 03 have the same missing-`RETURNING` defect** — the
  validator reports 2 errors in each. Out of scope here; workflow 01 is
  single-item so its pairing is not actually at risk, workflow 03's is.

- **No band is publishable until the judge noise is re-measured on the shipping
  co-ordinate.** 014 binds the day-13 variance to
  `pass2-agent-quality-v3 / 1.0.0 / deepseek-chat`, and the worker ships the
  current pass-2 prompt against `deepseek-v4-flash`, so `band_stable` is
  false on every scorecard row after rollout. Deliberate and fail-closed (§6b),
  but it means the scorecard shows counts and means and **no grades** until
  somebody repeats the A/A run — worker-PR work, and the last gate between this
  rollout and a scorecard anybody may show to an agent. `acceptance_014_status`
  **R5** prints that reason on every row so it cannot be mistaken for a bug.

- **A re-evaluation that comes back `unscoreable` overwrites a previously stored
  score.** `Store unscoreable outcome` upserts and clears the score columns,
  matching what `Store evaluation` already does for the same status on the
  pass-2 path. It needs a re-transcription to happen at all, and the alternative
  — old module scores sitting beside a null `final_score` — is worse. Recorded
  in `docs/PR2-db-status.md` §5.
