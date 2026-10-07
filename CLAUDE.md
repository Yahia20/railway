# CLAUDE.md — read this before touching anything

TravelGate **Customer 360 & Sales Quality**. Takes Arabic customer conversations
(Bitrix24 chats, recorded phone calls), extracts what the customer wants, and
scores how well the agent handled it against a 5-module rubric.

Saudi Arabia (majority) and Egypt. Conversations are Arabic, Gulf and Egyptian
dialect. Full history and rationale: **`docs/HANDOFF.md`** — read it before
making decisions, not after.

---

## Status in one line

**Chats only, deployed, and running. The pipeline is complete end to end and
waiting on one thing: DeepSeek credit.**

```
worker      deployed, pass2-agent-quality-v7 live
database    001-024 applied, audit_data_integrity 0 failures / 36 clean
n8n         01c, 01d, 03, 04 active. 02 and 99 (calls) switched OFF
report      /report, 27 panels, channels ["chat"], errors none
blocked on  DeepSeek balance -0.10 USD. 1,008 threads waiting, attempts 0
```

### Calls were REMOVED, not paused — and the archive is the only copy

`../travelgate-calls-archive/` (its own git repo, commit `8b24085`) holds the
workflows, the Modal batch, the ASR chunker, `drive_calls.py`, `asr_jobs.py`,
the 02 SQL, migrations 008/010/012, the call channel-rules prompt, and
`shared-before/` — pre-edit copies of every shared file that was cut down
rather than deleted. Its README has the restore order.

**Why.** Not volume and not cost: **calls were never scoreable per agent.** All
1,119 recordings decode to extension `3009`, a QUEUE, so `agent_id` was NULL on
every call and ~800 of the 834 stored evaluations could not be attributed to
anybody — and every one of them was being aggregated under a heading that said
"agent performance". Shelving the schedule would not have fixed that; the rows
and the views stay. **It comes back when the PBX records the answering
extension, and not before.**

**APPLIED to production on 2026-09-15.** 4,327 rows archived first, then 830
call conversations deleted and three tables dropped. `audit_data_integrity`:
**0 failures, 36 clean**. Live counts now: 1,504 interactions (all chat),
37,672 messages, 42 evaluations, 1,008 threads due.

```bash
python scripts/dump_calls.py --out ../travelgate-calls-archive/data   # FIRST
psql ... -v ON_ERROR_STOP=1 -f db/migrations/023_remove_calls.sql
python scripts/dump_calls.py --out ../travelgate-calls-archive/data --verify
python scripts/audit_data_integrity.py --port 55432
```

**THREE DEPENDENCIES THE MIGRATION COULD NOT HAVE GUESSED**, each of which
failed a real run before the file was correct. None is visible in the migration
that created the object:

1. `call_ingest_jobs` REFERENCES `interactions` with **no ON DELETE CASCADE**,
   so deleting the conversations while that table stood failed on the FK.
2. `call_ingest_jobs` REFERENCES `asr_runs`, so the parent could not go first.
3. `v_spend_mtd` and `v_spend_by_component` read `asr_runs`, and
   `v_pipeline_gate` reads `v_spend_mtd` — **the budget gate every workflow
   asks before it claims work**. `v_alert_queue` and `v_alert_digest_daily`
   join `call_ingest_jobs` for one column, `uniqueid`.

All five are rebuilt with their **column lists unchanged**, because
`app/budget.py`, `/spend` and `/report` select them by name and a gate that
errors is a pipeline that stops. And 022's own `COMMIT` came in with its text
and split the migration in two halfway through — the half that failed was the
half that deletes rows.

**Live n8n: 02 (calls state machine) and 99 (calls ops report) are now OFF**,
backed up to `local-reports/n8n-backup-20260915/` first.
`scripts/n8n_deploy.py --deactivate` does it and never deletes — the execution
history is the record of what the lane did.

`023` rebuilds `v_usable_evaluations`, `v_agent_scorecard` and
`v_quality_by_input` without the `transcripts` join, drops
`eval_asr_input_is_eligible()`, re-creates 022's display layer on top, patches
`evaluate_alert_rules()` in place from `pg_get_functiondef`, deletes the
`asterisk_drive` rows, then drops `transcripts`, `call_ingest_jobs` and
`asr_runs`. **The enum values `phone_call` and `call_transcript` survive** —
removing them would rewrite two large tables under a lock — so three CHECK
constraints make them unusable instead.

`v_agent_scorecard.calls` is kept and hard-wired to `0`, and
`v_quality_by_input.diarization` is kept and always NULL, so the display layer
and `app/report.py` need no edit.

**A gap this surfaced, and FIXED: Module 4 had never been sent its input.**
01d never had a node that builds the follow-up-history block, so the judge was
never given one and correctly answered `null` — `m4_followup` was
not-applicable on **822 of 834** rows. Because `weight_applied` renormalises,
it did not look like a hole; it looked like conversations that needed no
follow-up. 20% of the rubric, silently absent.

The chain now runs end to end, and every link has a test:

```
01d "Load thread"      selects `later_interactions` — the customer's own
                       timeline, 14-day window, matched on customer_id and
                       falling back to the phone. ROWS, never a rendered string
/chats/prepare         renders them with `metrics.later_contact_line`
01d "Two AI passes"    forwards `followup_history` to /evaluate
build_pass2_prompt     substitutes it into {{FOLLOWUP_HISTORY}}
```

**Rule 2 reaches the block itself.** `later_interactions = []` means we searched
the whole timeline and found nothing — an agent who did not come back, which
SCORES zero. A *missing* field means nobody searched, which NULLS. Collapsing
the two is how the module went missing in the first place.

**The worker no longer writes to the database at all.** `writer()`/`write()`
and the write pool existed for one caller, `asr_jobs.py`, because Modal ran
outside Railway. Rule 11's exception went with the calls lane: n8n is now the
only writer, which is the property the split was always supposed to have.

### 024 — a deal now belongs to a CUSTOMER, not just to a conversation

`deals.customer_id` had existed since 003 and **never held a value**. Bitrix
links a deal to a CONTACT, workflow 04 fetched `CONTACT_ID` and dropped it, and
nothing resolved a Bitrix contact to our merged customer. So a customer's deals
were reachable only through their conversations, and a deal opened for someone
who never chatted was invisible on their profile.

| where | what |
|---|---|
| `deals.bitrix_contact_id` | 04 stores the field it was already fetching |
| `customer_identities` kind `bitrix_contact_id` | built from the 1,509 threads that carry both ids, `method = exact_crm_id`, confidence **0.95 not 1.00** — it is an assertion about a CRM record, inherited, not an exact phone match |
| `link_deal_customers()` | contact first, conversation as fallback, idempotent |
| `v_customer_deal_summary` | one definition of "won" (`stage_semantic = 'S'`) |

**It runs in 03, not 04, and that is the whole point of where it sits.** The
contact→customer mapping is built from `interactions.customer_id`, which 03's
resolver writes at 03:40. Running it in 04 at 03:20 would work off yesterday's
resolution and be permanently one night behind — the exact bug 03's own
resolver shipped with.

First run: **1,042 of 1,268 deals** now carry a customer. `linked_by_contact`
was 0 because `bitrix_contact_id` is still empty; it fills on 04's next nightly
pull and takes over from the conversation fallback.

### 027 — a customer has a NAME, and /report answers the sales question

**Written as 025, renumbered to 027** (origin/main took 025/026 on 2026-10-01).
027, 028 (deal-title noise) and 029 (trip columns) are **applied to the
database**; 743 customers carry a name. Workflows 01d/03/04 still need
`python scripts/n8n_deploy.py 01d 03 04 --activate` — `bitrix_contacts` stays
empty until 04 runs with the widened contact selector.

`customers.display_name` has existed since 002 and **never held a value**, and
neither has `customers.name_source` beside it. 002 did not miss the problem — it
wrote the CHECK `name_source IN ('crm_contact','deal_title','ai_extracted',
'manual')`, which is a PRECEDENCE, with a comment naming the exact conflict it
was built for. The column list was right. Nothing was wired to it. So every
customer page, dashboard row and follow-up queue identified a human being by
their phone number.

**All three sources were already here, each dropped at a different step.**

| source | where it was lost |
|---|---|
| `crm_contact` | `/bitrix/contacts` asked for `select=["ID","PHONE"]`. NAME arrives NULL rather than as an error (gotcha 16), and 04's pairing node reads `c.PHONE` and drops the rest of the object |
| `deal_title` | `deals.title` often IS the name and since 024 a deal knows its customer — but a title is free text, so **`p_use_deal_title` defaults FALSE**. Look at `v_customer_name_candidates` before switching it on; it is an argument, not a deploy |
| `ai_extracted` | pass 1 fills `customer.name` and `interaction_analysis.customer_name` was **in no INSERT in any workflow**. Stored in `raw_response` and unreadable |

**Rank-ordered, and it can only ever promote.** `name_source_rank` is a table,
not a CASE, so changing which source wins is an UPDATE. `manual` is 0 and
outranks everything automated permanently. A name pass 1 itself flagged in
`uncertain_fields` is never promoted — the prompt is explicit that a wrong name
creates a person who does not exist and identity resolution then merges real
people onto them.

**`resolve_customer_names()` runs in 03, not 04, for the same reason
`link_deal_customers()` does.** Both sources are keyed on `customer_id`, which
03 writes at 03:40. In 04 at 03:20 it would work off yesterday's resolution and
be permanently one night behind.

**`bitrix_contacts` is new and holds what Bitrix SAID**, separate from
`display_name` which is what we concluded — so a disagreement survives and a
wrong name is auditable. 04's selector widened from "contacts missing a phone"
to "contacts we still need"; a contact the CRM genuinely has no name for is
retried **monthly, not nightly**, or it would occupy the 500-id budget forever.

**The trap this walked around:** `interaction_destinations` (004) has no writer
either, exactly like `display_name`. A real-ask panel built on it would report
zero forever and look like a finding. `interaction_requests.destination` is the
only destination this system records.

#### /report now answers the commercial question, not only the quality one

Seven new panels: `real_ask_funnel`, `agent_commercial`, `service_mix`,
`customers`, `name_coverage`, `followup_totals`, `followups`.

**A real ask is mechanical, and defined once.** Destination AND travellers AND
date — three fields present or not, so it cannot drift between runs the way a
model's opinion of "serious" does (rule 3). `_REAL_ASK` is bound into all three
queries that count it; a second copy is how the agent table and the headline
start disagreeing by one. Requests with `evidence_valid = false` are excluded
(rule 9).

**Rule 2, moved out of the rubric and into a sales report.** `real_asks` can
only be counted on a conversation pass 1 has read. Printing "2" against 35
customers reports 33 time-wasters where the truth is 31 were never looked at, so
`analysed` travels in the same row, the page renders **"2 من 4 مقروء"**, and an
agent with nothing analysed gets a dash — never a zero.

**The customer row carries `last_score`, not an average.** A customer has one to
five conversations, so a "customer average" is a mean of one dressed as a
statistic — 022's failure with a smaller n and no display view to qualify it. A
single `final_score` needs no denominator.

`test_customer_names_and_real_ask.py` (41 assertions) pins all of it: the
contact select against the SQL that reads it, the never-demote guard on all
three UPDATEs, that nothing may come between `Pair contact to phone` and
`Normalise phones` (gotcha 5), and that the real-ask denominator is visible.
**700 tests pass.**

### 030 — the QA scorecard replaces v7 for grading agents, and /dashboard shows it

**The owner chose the company's QA sheet (15 items) over v7's 5 modules** on
2026-10-07. It was approved on 50 chats (artifact "تقييم جودة الموظفين",
2026-10-02), trialled on all 297 chats of 2026-09-27 → 10-04 (18 agents,
average 54.9, 2 critical errors, $0.30), and those 297 results are in the
database. The trial's code lives in `app/qa/` **unchanged** — imports made
relative, nothing else — and was checked against the trial: 40/40 threads
loaded identically from the database, same score and same 15 items; 30/30
chats render byte-for-byte as on the approved page.

| | |
|---|---|
| `app/qa/` | `score_v05` → v04 → v03 → v02 is the rubric's history, not dead code. The model only COPIES quotes (`prompt_v05.txt`); times, word lists and the arithmetic are code |
| `POST /qa/evaluate` | three runs in parallel, per-question majority, returns the result + three `model_calls` rows. Writes nothing |
| `qa_evaluations` | one row per chat: `claimed` → `scored` / `not_gradeable` / `failed` (3 attempts). Quotes blanked when the chat is purged |
| `v_qa_due` | the trial's filter: human agent, both sides ×2, 4–60 lines, quiet 3 days (item 27 needs them) |
| `v_qa_gate` | every money rule of v_pipeline_gate **except** `provider_budgets.enabled`, which keeps v7 (01d) OFF and must not stop this. Switch: `qa_config.mode` |
| workflow 10 | every 10 min 23:00–03:59, fenced claim token, one chat at a time. Generated by `scripts/build_wf10_qa_scorecard.py` — edit the SQL there |
| `/dashboard` | employees (the approved page, one chat at a time), team (15 items 0–5), customers. Live reads: tonight's grades are there tomorrow |

**Not deployed to n8n yet**: needs `N8N_API_KEY`. Then
`python scripts/n8n_deploy.py 10 --activate` and
`UPDATE qa_config SET value = 'on' WHERE key = 'mode';`.
DeepSeek now echoes `deepseek-flash` for a v4-flash request; it is priced, or
every call would store `cost_usd = NULL` and vanish from the cap.

### The judge reports observations, not scores — `pass2-agent-quality-v7`

v6 asked for a number per criterion and got `{"greeting": 18}` on a criterion
that is 10 + 10 + 5 — a score the rubric cannot produce, and nothing rejected
it. Measured: `v_quality_by_input` records a spread of **8.1 points over 5
chats and 17.4 over 10**, at temperature 0 on one prompt; and v4-flash and
v4-pro returned different numbers for conversations they agreed about.

v7 removes the judgement rather than the noise. The model answers booleans and
closed labels; `app/evaluate/rubric_items.py` turns them into points.

| | |
|---|---|
| **Not one criterion, weight or cap changed** | `rubric_version` stays `1.0.0` — a different ENCODING of the same rubric |
| **Every v2–v6 calibration carried over verbatim** | trigger gates, the Module-3 exclusion list, the counterweight, the polite/sarcastic catalogue |
| **The tie-break is one rule** | what was not observed did not happen: a missing check is `false`, never full marks |
| **`validate_legal_values`** | rejects any total the rubric's own items cannot add up to, with or without `checks` |
| **Backwards compatible** | a response with no `checks` scores exactly as before, so v6 rows and replays are untouched |

`tests/test_rubric_items.py` pins the item table to `CRITERION_MAX` and to the
prompt text, in both directions — a check renamed in one and not the other
fails the suite rather than silently zeroing a criterion.

**MEASURED, on five written conversations through OpenRouter** (real judge
path, real prompt, real scoring engine — not a reimplementation):

```
deepseek-chat-v3.1  run 1   45.5   59.2   92.8
deepseek-chat-v3.1  run 2   45.5   59.2   92.8    ← identical, every time
google/gemini-2.5   run 1   45.5   75.6   ...     ← another vendor, same answer
openai/gpt-4o-mini          contract failed, no score published
```

**Re-run drift is gone.** What v7 does NOT fully solve is cross-model
agreement: deepseek and gemini agree exactly on some conversations and differ
on others. The difference is now *readable* — the comparison is per
OBSERVATION, so you see which boolean flipped rather than two numbers.

**gpt-4o-mini failing is the system working.** It fired three objections on a
conversation that never reached negotiation and scored a service refusal its
own `refusal_check` said had not happened. The contract caught all of it and
published nothing. A weak model produces no number here, loudly — not a wrong
one, quietly.

**Every ambiguity the run exposed is now decided in the prompt**, in one
section (`WHAT COUNTS`), because Rule 1 is a floor and not a substitute for a
decision: a check whose bar the rubric never states is a bar the model sets,
differently each time. Five so far — the greeting, whether the agent answered
the question, what counts as a date, what counts as a traveller count, and
what counts as persuasion. Expect more, and add them there.

### What runs, and when (all times Asia/Riyadh — n8n's GENERIC_TIMEZONE)

| | workflow | n8n id | when |
|---|---|---|---|
| live | **01c** chats store-only | `H7r5YWGJ3nNVA99Z` | every Bitrix message, ~1,300/day |
| live | **01d** chat scoring | `P1zSFsw16wmV28YF` | every 10 min, 23:00–03:59 |
| live | **04** housekeeping | `z60SxzoYmKOLsH4S` | 03:20 daily |
| live | **03** identity + promises | `sUnNPv6Ucye6Gsii` | 03:40 daily |
| built, not deployed | **09** chat media archive | — | every minute, idle while `media_archive_config.mode = 'off'` |

**09 keeps the files chats refer to** (customer photos/PDFs die on a ~20-minute
gupconnector link). Generated by `scripts/build_wf09_media_archive.py` — edit
the SQL there, never the JSON. Worker side is `app/media/`; the reader is
`GET /conversations` (page) and `/conversations/by-deal/{id}` (JSON). Its real
SQL runs end to end against an empty `_scratch` database with
`scripts/check_media_archive_sql.py`. Design, limits and go-live steps:
**`docs/chat-media-archive.md`**. Needs migrations 025 and 026 and a private
Railway Bucket.

The night window is a discount, not a preference — see gotcha 14. 03 runs
*after* 04 because 04 is what fetches the phones 03 matches on.

**Workflow 01** (`i6VM7qxmbYEDebDx`) is still active and has had zero executions
in weeks. It listens on `/webhook/travelgate/chat`, a different path from 01c's
`/webhook/travelgate/chat-message`, and scores inline into the `bitrix`
namespace which `v_chat_eval_due` deliberately excludes. Harmless; left alone.

A second, hand-built 6-node handler (`5iGvWrBUoWckBU6b`, "01c · Chats ·
per-message store (Bitrix)") held the chat path first. It is **deactivated, not
deleted** — keep it as the rollback. Do not reactivate it without reading
`docs/HANDOFF.md`: it computed `first_response_seconds` in SQL as
`min(agent) - min(customer)` (negative whenever the agent opens), invented
`now()` for unparseable timestamps, created stub `deals`/`agents` rows, dropped
empty attachment turns, and numbered `seq` with `count(*)+1`, which collided for
two messages in the same second.

Its rows were migrated from `external_source = 'bitrix'` to `'bitrix_chat_api'`
so threads would not split across the two namespaces. The 16 older rows still
under `'bitrix'` belong to workflow 01 and were deliberately left alone.

### Where things stand — 2026-09-07, measured, not assumed

Open `/report` first (below). Everything here was checked against the live n8n
API, the live database and the live Bitrix portal on the night of 06→07 Sept.

**The pipeline ran end to end for the first time.** 01d judged at 00:15 Riyadh,
inside the window, `priced_at_peak = 0`, and wrote the first ever rows to
`model_calls` and `interaction_requests`. One conversation costs **$0.008**
(pass1 $0.0029 + pass2 $0.0051), and DeepSeek's prefix cache served 10,624 of
10,732 prompt tokens — the static prompt is essentially free after the first
call.

**Fixed and live this session**

| | what was wrong |
|---|---|
| timezone | `GENERIC_TIMEZONE` is not set on the n8n service and only 04 declared a zone, so 01d/02/03 resolved `23:00` in n8n's own default. Every scheduled workflow now pins `settings.timezone: Asia/Riyadh`. |
| Bitrix paging | 04 sent `start: 0` once. Bitrix pages at 50, so it imported **50 of 709** deals a night and logged success. Paging moved to the worker (`POST /bitrix/deals`, `/bitrix/contacts`). |
| `deals` never filled | The upsert violated `is_closed NOT NULL` (CLOSED was in no select list), then `stage_semantic` CHECK, then `deals_origin_ck` — three constraints, all silent because the node is `onError: continueRegularOutput`. |
| health check | `job_runs.status` is an enum; an unqualified CASE yields text. The node that reports whether anything got judged was the only one that could not run. |
| timeouts became verdicts | `Prepare chat input` is `continueRegularOutput`, so a timeout left `should_evaluate` undefined and `Scoreable?` routed to **terminal** `unscoreable`. 20 threads written off permanently by a slow HTTP call. Gated; the 20 were revived (5 genuine ones left alone). |

### 2026-09-07, second pass — the judge bug found, agents attributed

**THE JUDGE WAS RETURNING AN EMPTY STRING, and one variable caused it.**
`DEEPSEEK_THINKING=omit` on the worker **deletes** the `thinking` field from
the request instead of sending `{"type":"disabled"}`. `deepseek-v4-flash`
defaults to thinking ON, so every judge call burned its whole 8,000-token
budget on hidden reasoning and returned `''`. Measured on the real pass-1
prompt:

```
omit      57.4s  finish_reason=length  content=''            8000 reasoning tokens
disabled   4.0s  finish_reason=stop    2621 chars of JSON     837 completion tokens
```

That is 82 of the 135 chat dead-letters directly ("model did not return valid
JSON … got `''`"), plus the 18 × `timeout of 300000ms` and 28 × `socket hang
up` that 57-second calls produce. It is also **6× the cost**: 1,953 output
tokens per conversation with thinking off against 11,863 with it on, so
$0.0013/conversation not $0.0080. `DEFAULT_THINKING` in `judge.py` has always
been `"disabled"` — the env var was overriding the correct default with a value
that means "send nothing".

**Fixed in the repo, applied to the database, NOT yet deployed** (the deploy and
the Railway variable were both blocked by a permission classifier — run them by
hand):

| | what |
|---|---|
| **018** | agent roster + attribution. Applied. |
| 03 | resolver split into 3 statements; `link_agent_attribution()` added; `alwaysOutputData` on the nightly mutations |
| 04 | `deals.assigned_by_id` written from `ASSIGNED_BY_ID` |
| 01d | new fenced `Store metrics` node — `interaction_metrics` had no writer at all |

```bash
railway variables --service railway --set DEEPSEEK_THINKING=disabled
python scripts/n8n_deploy.py 03 04 01d --activate
python scripts/revive_chat_dead_letters.py --apply      # AFTER the variable is live
```

### 2026-09-09, fourth pass — the money gate, and production

**DeepSeek is out of credit: balance −0.10 USD, `is_available: false`.** The
judge cannot run at all. Nothing in the pipeline knew that, and 599 threads sat
`pending` — the next window would have claimed them ten at a time, failed every
call, incremented `judge_attempts` and dead-lettered the whole queue inside
three nights, for a reason with nothing to do with the conversations in it.

**The rule now enforced everywhere: stop BEFORE claiming, not after failing.**
An unclaimed job keeps its status, its attempt count and its place in the
queue, so an outage of any length costs nothing and loses nothing, and work
resumes on its own when the money returns. No backfill, no manual step.

```
Check budget ──▶ Record provider status ──▶ Read budget gate ──▶ May we spend?
                                                                  │        └── false ──▶ Log blocked run  (job_runs, status 'skipped')
                                                                  └── true ──▶ Register/Claim …
```

Two statements, not one: a parameterised query may carry only one command, and
a CTE beside the INSERT would read the pre-update snapshot — **the same bug 03
shipped**.

| where | what |
|---|---|
| `020` | `provider_budgets` / `provider_status`, `v_spend_mtd`, `v_pipeline_gate`, `v_spend_by_component` |
| `app/budget.py` | one probe per provider; `/budget/preflight`, `/budget/spend` |
| `/spend` | **the spend report page** — where the money went, and why work is stopped |
| `/asr/claim` | **the Modal 30 USD hard cap**, enforced in the worker |

**The Modal cap is in the worker on purpose.** Modal reaches the database only
through the worker, so that is the one chokepoint every batch must pass. A cap
in `modal/transcribe_job.py` would be advisory — a redeploy or a hand-run
`modal run --limit 500` would step straight past it. The claim is also trimmed
to what the remaining budget can pay for, so one large batch cannot vault the
ceiling in a single go.

**The gate fails CLOSED** for any provider whose balance we can check and have
not. An unchecked DeepSeek is exactly the state that would have emptied the
queue.

**Adding a provider** is a `_probe_<name>()` function, a `PROBES` entry and a
row in `provider_budgets`. **Changing a cap is an UPDATE, not a deploy.**

**Data integrity is now checked, not assumed.** `scripts/audit_data_integrity.py`
holds 24 assertions about what the numbers MEAN — an evaluation attached to no
agent, a request counted twice, a rate over the wrong denominator. It found a
real one: 13 retired-namespace rows were still in the judging queue, four
claiming success with no evaluation, so `/report` said **40 threads evaluated
when 27 were**. Cleaned in `021`. Run it after any migration; it is currently
**0 failures**.

`interaction_metrics` was backfilled for all 42 judged conversations using the
worker's own `compute_chat_metrics` (rule 3 — never a second implementation),
so `v_agent_scorecard.avg_first_response_sec` is populated for the first time:
the spread runs from 85 seconds to 19 hours.

**Bitrix is fully automated except one thing.** `crm.deal.list` pages nightly
and carries `ASSIGNED_BY_ID`, so deals, stages, amounts and owners all arrive
on their own. The only manual step is naming a NEW member of staff, because
`user.get` is outside the webhook's scope — `v_roster_gaps` (and a `/report`
panel) names any Bitrix user id owning deals with no `agents` row. Fix with one
line in `local-reports/agent_roster.json` and `scripts/seed_agents.py --apply`.

**Changing the call source: `docs/CHANGING_THE_CALL_SOURCE.md`.** Modal's audio
fetch is now scheme-dispatched (`drive://`, `https://`, `s3://`), so a recorder
that can produce a signed URL needs no new code at all.

### 2026-09-09, third pass — production hardening

**Nothing from the second pass was deployed**, so the empty-judge bug ran two
more nights: chat dead-letters went **135 → 332**. The three commands below are
still the whole unblock.

**`executeOnce` was missing on every whole-table node, and that is systemic.**
An n8n Postgres node runs its query **once per input item**. A statement with
no `$N` placeholder is whole-table work, so it ran once per row the upstream
node returned. Measured: `job_runs` holds 2,630 rows, of which **2,622 are one
`nightly_health` row written 2,622 times in a single night**. The night before
it was 6, the night before that 1 — it scales with the data.

Worst instance: 01d's `Claim work` has `LIMIT 10` and is third in a
Register → Recover → Claim chain, so it claimed **ten jobs per registered
thread** instead of ten per tick. Hundreds of concurrent judge calls against a
0.5 GB worker is where the `socket hang up` and `timeout of 300000ms` dead
letters came from — a second, independent cause alongside the thinking bug.
Workflow 02 always had this right; 01d lost it when it was derived from 02.

`check_workflow_json.py` now has a `check_execute_once` rule so this cannot
regress, and it found the two 01d cases on its first run.

**An automation was being graded as a salesperson, and injecting into the judge.**
Bitrix user 1 has 1,157 turns stored as `sender = 'agent'`: 294 copies of the
PROMPT it sends its own model ("generate a short, natural follow-up message in
Saudi Arabic …") and 863 bare 👍 reactions. Neither was ever sent to a
customer. That text went into pass 1 and pass 2 as agent speech — rule 8's
prompt-injection failure through a door nobody was watching — and the account
sat top of `v_agent_scorecard` with the worst average in the company (21.9).

Fixed by one flag: `agents.is_bot`. 01d's `Load thread` relabels such turns to
`bot` **and withholds the body** — relabelling alone was not enough, because
`transcript_text()` renders bot turns verbatim. The turn is kept, not deleted,
so the response gap it sits in stays the length it really was.

**Silencing the next automation is one line, no deploy:**
```sql
UPDATE agents SET is_bot = true WHERE bitrix_user_id = '<id>';
```

**Alerts only ever ran for calls.** `evaluate_alert_rules(uuid)` was always
channel-agnostic but only 02 called it, so all 25 occurrences were
`phone_call`. With calls paused the follow-up queue was completely dark. 01d
now evaluates them with the same stamp-and-fence discipline, the 40 already-
judged threads were backfilled (5 occurrences fired), and
`v_chat_alerts_pending` + a `/report` panel make a dropped tick visible.

**Also fixed:** 03's step 1b was stamping `exact_phone, confidence 1.00` onto
every customer with a phone rather than only those it matched — fabricated rows
in the one table that makes a bad merge discoverable. `deals.source_channel`
and `deals.assigned_by_id` now get written from fields that were already being
fetched and dropped. `Store metrics` fails soft, because a failure there would
cost a second paid judge run for an answer already stored.

**The roster is out of the repo.** 018 no longer inlines 48 real names (rule
7). `local-reports/agent_roster.json` is gitignored; `scripts/build_roster.py`
rebuilds it from REST ⋈ CSV and `scripts/seed_agents.py` applies it. The seed
turns `is_bot` **on but never off**, so a rebuild cannot un-flag an automation.
Onboarding someone is now one line of JSON, not a migration.

**Agent attribution now exists.** `user.get` really is refused
(`insufficient_scope`), but `crm.deal.list` returns `ASSIGNED_BY_ID` and the
manual `DEAL_*.csv` export renders the same deal's owner as a NAME. Joining the
two on the deal id recovered **48 users, every one at confidence 1.00** —
17,708 deals from REST against 17,707 from the export, 17,575 joined. `scripts/seed_agents.py` seeds them from the gitignored roster;
`link_agent_attribution()` then attributes threads from
`chat_messages.sender_external_id` (who actually typed) and falls back to the
deal owner. Result: **970 of 987 chat threads and 673 deals** carry an agent.
`scripts/backfill_deal_owners.py` replays the whole thing.

Ids **30 and 20114** are both "Travelgate AI" — flagged `is_bot`, which is the
only thing keeping the bot out of `v_agent_scorecard`. Id **1** is "Cultiv
Developer", the integration account: `is_active = false`, and the attribution
rule ranks it last so it only wins a thread no human touched.

**Still open**

1. **The calls source has been dry since 2026-08-19.** Drive holds 1,119 files
   / 1,064 unique recordings and the newest is 19 days old. All 1,064 are
   already registered and terminal, so nothing is stranded — this is upstream.
   Ask whether the PBX stopped uploading or the folder/credential changed.
2. **Calls cannot be scored per agent, at all.** All 1,119 filenames decode to
   `agent_extension = 3009` — a queue, not a person. 616 of the 623 promises
   pass 1 has extracted sit in call transcripts and can never become
   `follow_ups` rows. The PBX must record the answering extension; two channels
   would fix diarization at the same time (gotcha 10). Until then **chats carry
   the sales-quality numbers and calls do not**.
3. **Modal has $0.99 left of its $30/month free credit** and no *scheduled* run
   has fired yet (the one `asr_runs` row is a manual run: claimed 0, 0.7
   GPU-seconds). Add a payment method or ASR stops the moment calls resume.
4. **RTFx is still unmeasured** — blocked on 1.
5. **DPA / PDPL** before customer audio leaves for any processor. `consents` is
   still empty and call recordings are voice data.
6. **Drive's own retention** — `purge_raw_content` blanks call text after a
   year. If Drive deletes the WAV sooner, that conversation is gone from the
   world.
7. **Live n8n holds far more than this repo**: a WhatsApp/Gupshup platform, a
   popup-form → CRM flow, and `vbttYDc7KoYBgaTW "Bitrix24 - Store Chat
   Messages"` (active, writes its own `messages` table, not ours). None of them
   touch `customer360`, but do not assume this repo is the whole portal.

**Bitrix, as it actually is.** Portal `travelgate.bitrix24.ae`, webhook user
128 (`ADMIN: true`), scope `crm` only. 17,651 deals and 23,754 contacts;
709 modified in the last 7 days. `.env.example` still says
`cultiv.bitrix24.com` — that is stale. `python scripts/bitrix_probe.py` tests
exactly what workflow 04 calls.

---

## Rules that must not be broken

These are not style preferences. Each one exists because breaking it produces
numbers that look fine and are wrong.

1. **Two AI passes, never one.** Pass 1 extracts the customer's request; pass 2
   scores the agent. Separate prompts, separate API calls, neither sees the
   other's output. Merge them and an angry customer drags down the agent's score
   while a strong agent inflates the sales forecast — and you cannot tell which
   happened afterwards.

2. **`null` is not `0`.** A module scores `null` when the situation never arose,
   `0` when it arose and was handled badly. The source rubric awarded automatic
   full marks for absent situations, which is 45% of the total weight given away.
   The first real call scored **87.9 "Excellent"** that way, having never quoted a
   price. `final_score` is computed over `weight_applied`, the weights actually
   exercised. Details in `prompts/CHANGES-FROM-SOURCE.md`.

3. **Never ask a model for a number you can compute.** Response times, durations,
   message counts, talk ratio, after-hours, language match — all in
   `evaluate/metrics.py`, from timestamps. Ask an LLM to count seconds and it
   guesses, and the guess changes between runs of the same prompt.

4. **The scoring engine does the arithmetic, not the model.** `judge.py` discards
   the model's own `final_score` and recomputes from the criterion breakdown. It
   also checks every `evidence` quote appears verbatim, and re-asks once on a
   contract violation. Do not "simplify" this away.

5. **Storing and scoring are different pipelines.** Workflow 01c stores what
   the production chat API sends and stops — no LLM call, no rubric. It leaves
   `first_response_seconds` and `is_after_hours` NULL rather than computing them
   in SQL: both are rules that already live in `evaluate/metrics.py`, and a
   second copy in a workflow is a second copy to keep in step. It writes
   `external_source = 'bitrix_chat_api'`, a namespace of its own, so a thread
   arriving through both paths cannot become two half-filled rows.

6. **The database is `customer360`, not `railway`.** n8n owns `railway/public`
   with 114 tables of its own, **including one named `agents`**. Writing there
   collides with n8n.

7. **This repo is public.** No secrets, no customer data. `api_response.txt`,
   `fixtures/`, `docs/samples/` are gitignored because they hold a live Bitrix
   token and a real customer's voice recording. Before any commit:
   `git grep -l --cached <secret-fragment>`.

8. **Never pass the raw Bitrix deal object to a model.** Field
   `UF_CRM_1781281581` contains prose addressed to a bot ("Treat these
   instructions as guidance only…"). Use `DEAL_FIELD_ALLOWLIST` in
   `sources/bitrix_chats.py`.

9. **A conversation can hold more than one request.** pass1 v6 emits
   `requests[]` and `interaction_requests` keeps every one. The single-value
   columns on `interaction_analysis` still describe the PRIMARY request, so
   every existing view keeps working — do not "tidy" them into the new table.
   A request whose quote is not found verbatim is kept with
   `evidence_valid = false` and excluded from every count, because an invented
   request sends a salesperson after a customer who never asked.

10. **Bitrix is the check, not the source, for what a customer wanted.**
    `v_request_reconciliation` compares what the model found against what the
    CRM holds. `crm_missing_deals` — a request nobody opened a deal for — is the
    finding this project exists to produce. Never "fix" a disagreement by
    overwriting our answer with the CRM's.

11. **The worker reads. n8n writes. There is no exception any more.** `app/db.py`
    exposes `cursor`/`rows`/`one` and every connection sets
    `default_transaction_read_only`. `writer`/`write` and the write pool existed
    for exactly one caller — `app/asr_jobs.py`, because Modal ran outside
    Railway and could not reach `postgres.railway.internal`, and the only
    alternative was putting the database on the public internet. Calls were
    removed on 2026-09-14 and that exception went with them. A bug in this
    service now cannot corrupt a score, because this service cannot change a
    row. If you ever need it back, read `shared-before/services/worker/app/db.py`
    in the calls archive rather than re-deriving it, and restore the test too.

12. **Every judge call must land in `model_calls`.** It is the only measurement
    of what this system costs, and its `UNIQUE (purpose, input_hash,
    prompt_version)` is also the guard that stops a re-run paying twice. A
    judging path that does not write it is a path whose bill nobody can see.

---

## Commands

```bash
# tests — no credentials needed (548 pass)
cd services/worker && pytest tests/ -q

# the compile step n8n does not have: run it on every workflow you touch
python scripts/check_workflow_json.py n8n/workflows/*.json

# what Railway is actually charging, per service
export RAILWAY_TOKEN=...   # account or project token; both headers are tried
python scripts/railway_usage.py

# what is configured, live
curl -H "X-API-Key: $WORKER_API_KEY" https://railway-production-d648.up.railway.app/ready

# THE SPEND REPORT. Where the money went, and why work is stopped if it is.
#   https://railway-production-d648.up.railway.app/spend
curl -H "X-API-Key: $WORKER_API_KEY" https://railway-production-d648.up.railway.app/budget/spend

# Is the pipeline allowed to spend right now? (this is what the workflows ask)
curl -X POST -H "X-API-Key: $WORKER_API_KEY" -H 'Content-Type: application/json'      -d '{"providers":["deepseek","modal"]}'      https://railway-production-d648.up.railway.app/budget/preflight

# Do the numbers MEAN what the reports say? Run after every migration.
export PGPASSWORD=...
python scripts/audit_data_integrity.py --port 55432

# Roster: name a new member of staff (the one manual Bitrix step)
#   edit local-reports/agent_roster.json, then:
python scripts/seed_agents.py --roster local-reports/agent_roster.json --apply

# THE REPORT. Open in a browser and paste WORKER_API_KEY when it asks — the page
# holds no data and fetches /report/data itself, because a browser cannot set a
# header on a navigation and a key in the URL lands in history and proxy logs.
#   https://railway-production-d648.up.railway.app/report
# Twenty-seven panels, each separately fallible; `errors` is present and empty when
# healthy. crm_missing_deals leads. `build_dashboard_data.py` and
# `build_crm_pages_data.py` are superseded — they wrote a JSON file by hand next
# to a static page and nothing scheduled ever ran them.
curl -H "X-API-Key: $WORKER_API_KEY"   'https://railway-production-d648.up.railway.app/report/data?days=30'

# read/set Railway config without the CLI
export RAILWAY_TOKEN=...
python scripts/railway_api.py info
python scripts/railway_configure.py --apply     # needs DEEPSEEK_API_KEY, PGPASSWORD

# reach the database — public networking is OFF and must stay off. The CLI
# opens an SSH tunnel and PRINTS the password; nothing else has to know it.
railway connect postgres --tunnel-only --port 55432    # leave running
export PGPASSWORD=... PGCLIENTENCODING=UTF8
psql -h 127.0.0.1 -p 55432 -U postgres -d customer360  # NOT the `railway` db

# apply a migration. lock_timeout is in the file: an ADD COLUMN on `interactions`
# fights live ingestion and deadlocks, so it fails fast and you simply re-run.
psql -h 127.0.0.1 -p 55432 -U postgres -d customer360      -v ON_ERROR_STOP=1 -f db/migrations/017_*.sql

# deploy workflows: backs up the live copies, stamps the real credential id
export N8N_API_KEY=...
python scripts/n8n_deploy.py --list          # what is live, and its id
python scripts/n8n_deploy.py 01d 04          # deploy, leave switched off
python scripts/n8n_deploy.py 01d --activate

# does the Bitrix webhook let workflow 04 do its job? (the older
# `app.sources.bitrix_chats --probe` tests the chat-pull methods, which 04
# does not use — this one tests crm.deal.list and crm.contact.list)
export BITRIX_PORTAL_DOMAIN=travelgate.bitrix24.ae BITRIX_WEBHOOK_USER_ID=128
export BITRIX_WEBHOOK_TOKEN=...
python scripts/bitrix_probe.py

# rewrite + activate the n8n chats workflow (edits in place, no clicking)
export N8N_API_KEY=... PGPASSWORD=... WORKER_API_KEY=...
python scripts/n8n_setup.py --apply             # add --test-wait for a 1-min settle

# deploy + activate 01c, the store-only handler for the production chat API
# (this is what /webhook/travelgate/chat-message answers; it does NOT score)
export N8N_API_KEY=... PGPASSWORD=...
python scripts/n8n_deploy_chat_store.py --apply   # --take-over if 01b holds the path
python scripts/chat_api_smoke_test.py             # posts the same batch TWICE

# end-to-end: posts a synthetic sale to the live webhook, verifies every node
python scripts/n8n_smoke_test.py

# drive the pipeline from the conversation simulator API
export SIM_BASE_URL=https://<tunnel>.trycloudflare.com SIM_API_KEY=tg_...
python scripts/simulate_conversation.py --list          # what is in there
python scripts/simulate_conversation.py <id> --offline  # ingest only, no key
DEEPSEEK_API_KEY=... python scripts/simulate_conversation.py <id>   # real scores
python scripts/simulate_conversation.py <id> --webhook  # POST at live n8n
```

---

## Layout

```
db/migrations/         001-029 APPLIED to Railway (verified 2026-10-07).
                       023 removed the calls lane; 024 linked deals to
                       customers; 025 retention without transcripts; 026 chat
                       media archive; 027 customer names; 028 deal-title noise;
                       029 trip columns. 027-029 were written as 025-027 and
                       renumbered when origin/main took those numbers.
services/worker/app/
  serve.py             entrypoint — see gotcha 1 and 2 below
  main.py              FastAPI
  sources/base.py      Conversation — the seam the chat APIs plug into
  sources/bitrix_chats.py   webhook parser, verified against the real payload
  evaluate/judge.py         the two DeepSeek passes
  evaluate/scoring.py       weights, null handling, evidence validation
  evaluate/rubric_items.py  v7: the rubric as closed sets — observations to points
  prompts/                  THE RUBRIC — treat as source code, version it
  media/                    chat file archive: links (URL rules), fetch, s3
                            (stdlib SigV4, AWS test vectors), api, reader
n8n/workflows/         01 chats, 01c store-only, 01d chat scoring,
                       03 nightly identity, 04 nightly housekeeping —
                       ALL FOUR DEPLOYED AND ACTIVE. 02 (calls) is in the
                       archive and switched OFF in live n8n.
scripts/               railway_api, railway_configure, n8n_setup, n8n_smoke_test
docs/HANDOFF.md        full context
docs/bitrix-integration-spec.md   forward to the client's IT team
```

---

## Gotchas — each cost a failed deploy or a wrong result

**1 · Railway runs start commands without a shell.** `--port $PORT` and
`${PORT:-8000}` arrive at the process as literal text:
`Error: Invalid value for '--port': '${PORT:-8000}' is not a valid integer.`
Never put shell syntax in a Railway start command. `serve.py` reads env itself.

**2 · Railway has two networks with different address families.** The private
network (`*.railway.internal`) is IPv6-only; the edge proxy and healthcheck come
in over IPv4. `--host ::` is not sufficient — whether that socket accepts IPv4
depends on the kernel's `bindv6only`, and a v6-only socket refuses the healthcheck
while still logging `Uvicorn running on http://[::]:8000`. `serve.py` creates the
socket with `IPV6_V6ONLY = 0` explicitly.

**3 · A service with no domain fails its healthcheck.** No domain means no target
port, so the probe gets `service unavailable`. Create a service domain with
`targetPort` set.

**4 · n8n splits query parameters on commas.** A comma-separated
`queryReplacement` shreds `JSON.stringify(...)` and Arabic text into dozens of
bogus parameters. **Always use the array form**, and for anything large pass one
`jsonb` parameter and extract in SQL:
```
={{ [ $json.id, JSON.stringify($json.big) ] }}
```

**5 · `$json` after a Postgres node is `{success:true}`.** The n8n Postgres node
returns that, not your data. Any node chained after one must reference the source
by name — `$('Two AI passes').item.json` — or it silently reads nothing. This
caused three separate confusing failures.

**6 · Postgres returns `bigserial` and `count(*)` as STRINGS.** Number-typed IF
conditions fail with `'6' is a string but was expecting a number`. Coerce
explicitly.

**7 · `ON CONFLICT DO NOTHING` returning no rows also yields `{success:true}`** —
indistinguishable from a real row. Return an explicit `is_new` boolean instead of
inferring from row count.

**8 · n8n binds credentials by internal ID.** An imported workflow JSON can only
carry a placeholder, so every import leaves every node broken. Use
`scripts/n8n_setup.py`, which creates credentials via the API and stamps the real
IDs on. Do not tell anyone to re-pick them by hand.

**9 · `DEFAULT_PHONE_REGION=SA`, decided, not assumed.** `0500000000` is a valid
Saudi mobile and means nothing in Egypt. Numbers arriving with a country code are
honoured as-is. Bare-national Egyptian numbers **fail to normalise rather than
being assigned to +966** — deliberate: a null phone is recoverable, a
wrong-country match merges two real people.

**11 · A timestamp's offset is not decoration — convert before comparing.**
`after_hours` used to read the wall clock straight off `sent_at` and drop the
offset. The Bitrix webhook sends `+03:00`, which is already Riyadh local, so the
bug agreed with the truth on every payload we had and stayed invisible. The
conversation API sends `+00`, and `19:24+00` — 22:24 in Riyadh, plainly after
hours — came back as *within* business hours. `metrics.is_after_hours` now
converts to `PORTAL_TZ_OFFSET_HOURS` (default 3) first. Any new source that
sends UTC would have hit this.


**12 · A `respondToWebhook` node is not a guarantee — a Wait node outranks it.**
With `executionOrder: v1`, n8n runs sibling branches in canvas order, topmost
first, and runs each depth-first to its end. Workflow 01 fanned out to `200 OK`
(y=480) and to the ingest chain (y=300), so the chain went first, parked at the
30-minute Wait, and the responder was never reached: Bitrix got no answer at all
and would have timed out and retried. It stayed hidden because the Wait used to
be short enough that the whole workflow finished inside the sender's timeout.
Fixed by moving the acknowledgement into the webhook node itself —
`responseMode: onReceived` — which cannot be reordered by dragging a box.
**For any fire-and-forget ingest webhook, use `onReceived`, not a responder
node.** Workflow 01b already did.


**14 · The judging window is a discount, not a preference.** DeepSeek peak is
01:00–04:00 and 06:00–10:00 UTC Mon–Fri, where every rate doubles. n8n runs on
Asia/Riyadh, so the crons sit at 23:00–03:59 local and must END before 04:00.
Moving a schedule an hour later doubles the model bill for identical work;
`tests/test_workflow_017_changes.py` fails if any cron drifts into the window.

**And a cron with no timezone is not a time.** n8n resolves an unqualified cron
in `GENERIC_TIMEZONE`, which **is not set on the n8n service** — so until
2026-09-06 only workflow 04 declared a zone and 01d, 02 and 03 ran on n8n's own
default, hours away from where they were written and mostly inside peak. The
cron text never changed, so every test above passed while the bill doubled.
Every scheduled workflow now carries `settings.timezone: Asia/Riyadh` and a test
asserts it for anything with a `scheduleTrigger`. Never rely on the platform
default; the histogram on `/report` is the only place the mistake is visible
after the fact, because it plots `model_calls` by Riyadh hour against
`priced_at_peak`.


**15 · `onError: continueRegularOutput` turns a failed call into DATA, and the
next node reads it as an answer.** Three separate bugs on the first live night,
all of this shape. `Prepare chat input` timed out, so `should_evaluate` was
undefined, so `Scoreable?` read "not true" and wrote the **terminal** state
`unscoreable` — a slow HTTP call became "this conversation has nothing worth
grading, permanently". `Upsert deals` violated three constraints in a row and
the workflow carried on to the next node looking healthy. The setting is right
for a nightly job that must not abort halfway; what is missing each time is a
gate that asks *did this node actually answer* before anything reads its
output. Check the TYPE, not truthiness: `typeof x === 'boolean'` passes a real
`false` through and an error item does not.

**16 · A field the API was never asked for arrives NULL, not as an error.**
`Upsert deals` read `d->>'CLOSED'` and CLOSED was in no select list, so
`is_closed` — `NOT NULL DEFAULT false` — got an explicit NULL, which overrides a
DEFAULT rather than falling back to it. Every row of every batch failed, and
`deals` had never held a single row from the nightly pull.
`test_deal_select_covers_every_field_the_sql_reads` parses `d->>'FIELD'` out of
the SQL and fails if it is not in `DEAL_SELECT`.
---

## Working style for this project

- **Verify, don't assume.** "It's done" has been wrong twice here. Run the smoke
  test, read the n8n execution log, query the database. Report what you actually
  observed.
- **The prompts are source code.** Changing wording changes scores. Version them,
  and add a test to `tests/test_contract_validation.py` for any failure mode you
  fix — every test in there is something a model actually did.
- **One real conversation beats ten synthetic ones** for finding rubric problems.
  The first live run found three prompt bugs in ten minutes.
- Don't add dependencies casually. There is deliberately no ffmpeg: Asterisk
  writes 8 kHz PCM WAV, which the stdlib `wave` module reads.
