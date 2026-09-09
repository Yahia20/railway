# CLAUDE.md — read this before touching anything

TravelGate **Customer 360 & Sales Quality**. Takes Arabic customer conversations
(Bitrix24 chats, recorded phone calls), extracts what the customer wants, and
scores how well the agent handled it against a 5-module rubric.

Saudi Arabia (majority) and Egypt. Conversations are Arabic, Gulf and Egyptian
dialect. Full history and rationale: **`docs/HANDOFF.md`** — read it before
making decisions, not after.

---

## Status in one line

**Everything is deployed and correct. It is stopped, on purpose, because
DeepSeek has no credit.** Balance −0.10 USD, `is_available: false`. Top the
account up and the pipeline resumes on its own — 599 threads are waiting,
`judge_attempts = 0`, nothing lost. **There is no command to run afterwards.**

Check it in one call:

```bash
curl -s https://railway-production-d648.up.railway.app/spend        # the page
```

**Calls are paused by you, not broken.** Drive's newest recording is
2026-08-19; all 1,064 are processed and terminal. Nothing is stranded. The
lane needs no code change to resume — but see the two things to insist on with
the new recorder in `docs/CHANGING_THE_CALL_SOURCE.md`, because **calls cannot
currently be scored per agent at all**: every recording decodes to extension
`3009`, which is a queue and not a person.

**Modal is deployed** (`travelgate-asr`, cron 23:30) and capped at a hard
30 USD/month enforced in the worker, not in the batch. It has $0.99 of free
credit left, so add a payment method before calls resume.

**The judging queue is production-only and honest**: 599 pending, 27 evaluated,
6 unscoreable. It said 40 evaluated until `021` removed 13 rows belonging to
the retired workflow-01 namespace, four of which claimed success while holding
no evaluation.

Before trusting any number on `/report`, run:

```bash
python scripts/audit_data_integrity.py --port 55432   # 0 failures as of 2026-09-09
```

### What runs, and when (all times Asia/Riyadh — n8n's GENERIC_TIMEZONE)

| | workflow | n8n id | when |
|---|---|---|---|
| live | **01c** chats store-only | `H7r5YWGJ3nNVA99Z` | every Bitrix message, ~1,300/day |
| live | **02** Calls v2 — discovery | `Q3ARdzVsO3Z8bcWr` | 23:00 daily |
| live | **01d** chat scoring | `P1zSFsw16wmV28YF` | every 10 min, 23:00–03:59 |
| live | **02** Calls v2 — judging | (same workflow) | every 10 min, 23:00–03:59 |
| live | **04** housekeeping | `z60SxzoYmKOLsH4S` | 03:20 daily |
| live | **03** identity + promises | `sUnNPv6Ucye6Gsii` | 03:40 daily |
| live | **Modal** ASR batch | `travelgate-asr` | 23:30 daily |

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

11. **The worker reads; n8n writes — with one named exception.** `app/db.py`
    exposes `cursor`/`rows`/`one`, which set `default_transaction_read_only`,
    and `writer`/`write`, which do not. Only `app/asr_jobs.py` may use the
    second pair, and a test enforces that. The exception exists because Modal
    runs outside Railway and cannot reach `postgres.railway.internal` at all;
    the alternative was putting the database on the public internet. Do not
    widen it, and do not merge the two pools into one with a flag — a flag can
    be defaulted wrong and reads identically at the call site.

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
# Sixteen panels, each separately fallible; `errors` is present and empty when
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

# the Modal transcription batch — DEPLOYED, cron 23:30 Riyadh. It reaches the
# database through the WORKER, not directly: Modal runs outside Railway and
# postgres.railway.internal is Railway's private network. Secrets:
# travelgate-worker (WORKER_URL + WORKER_API_KEY), travelgate-drive, travelgate-hf.
modal profile activate dstravelgate
modal run modal/transcribe_job.py::main --limit 5 --dry-run   # claims + releases
modal deploy modal/transcribe_job.py                          # installs the cron

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

# score a stored transcript directly
DEEPSEEK_API_KEY=... python scripts/evaluate_call.py docs/samples/<file>.json

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
db/migrations/         001-021 all applied to Railway (018 attribution, 019 bots+alerts,
                       020 spend governance, 021 queue hygiene)
services/worker/app/
  serve.py             entrypoint — see gotcha 1 and 2 below
  main.py              FastAPI
  sources/base.py      Conversation / CallRecording — the seam the APIs plug into
  sources/bitrix_chats.py   webhook parser, verified against the real payload
  sources/drive_calls.py    PBX filename decoding
  asr/cohere_arabic.py      silence-aligned chunking, 3 backends
  evaluate/judge.py         the two DeepSeek passes
  evaluate/scoring.py       weights, null handling, evidence validation
  prompts/                  THE RUBRIC — treat as source code, version it
n8n/workflows/         01 chats (live), 01c store-only chat API (live),
                       01d chat scoring (off), 02-calls-v2-state-machine
                       (the colleague's, discovery live), 03 nightly (off),
                       04 nightly housekeeping (new)
modal/transcribe_job.py  the nightly ASR batch — replaces ASR in the worker
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

**10 · Call recordings are mono.** Nothing separates agent from customer, so
speaker attribution is inferred from content and the prompt suppresses the
absolute rules when `diarization = none`. The fix is free and not ours: ask the
PBX team to record two channels.

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

**13 · Two systems must never claim the same job.** Transcription moved out of
the worker into a Modal batch (017). Modal owns `call_ingest_jobs` rows in
`discovered`/`asr_failed` and leaves them `transcribed`; workflow 02 claims them
back only from `transcribed`/`judge_failed`. That boundary lives in exactly two
places — the `WHERE` in 02's `Claim work` and the `WHERE` in `CLAIM_SQL` in
`modal/transcribe_job.py`. Widen either one and the same call is transcribed
twice and paid for twice, which is the bug the lease was added to stop in the
first place. Modal also writes `external_source = 'asterisk_drive'`, the same
namespace 02 uses; a different one would turn one call into two half-filled
rows.

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
