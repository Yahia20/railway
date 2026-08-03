# HANDOFF — TravelGate Customer 360 & Sales Quality

Complete context for someone (or some model) picking this up cold. Updated
2026-07-30 after the chats pipeline was verified live. No secrets in this file —
see [§10](#10-secrets) for where each one lives.

**If you are Claude Code and just opened this folder: `CLAUDE.md` has the rules
and gotchas in short form. This file has the why.**

---

## 1 · What this is

A pipeline that takes **customer conversations** — WhatsApp/Facebook chats from
Bitrix24, and recorded phone calls — and does two things with each one:

1. **Extracts what the customer wants** (destination, dates, headcount, budget,
   buying stage) → feeds sales and forecasting.
2. **Scores how well the agent handled it** against a 5-module rubric → feeds QA
   and coaching.

Company: a tourism/travel agency operating in **Saudi Arabia** (majority) and
Egypt. Conversations are in **Arabic**, mostly Gulf and Egyptian dialect.

### The one rule that shapes everything

These are **two separate model calls with two separate prompts that never see
each other's output.** If a single prompt scored the agent *and* extracted the
customer's budget, the two contaminate each other: an angry customer drags down
the agent's score, and a high-scoring agent inflates the sales forecast. Two
calls cost marginally more and produce numbers you can trust.

---

## 2 · Live infrastructure

Everything is on one Railway project, `handsome-amazement`.

| | |
|---|---|
| Railway project | `9323ae43-5cbc-4a68-8fd0-5070f19ab85e` |
| Environment | `228ae89f-369b-4724-92e8-f5c4d59ce30a` (production) |
| Repo | https://github.com/Yahia20/railway — **public** |

### Services

| Service | ID | Status | Address |
|---|---|---|---|
| **postgres** | `ff1994f9-dff2-4993-a672-f7827b036096` | Online | `postgres.railway.internal:5432` |
| **n8n** | `4d9835af-fec9-40e1-a598-bf3efe1fd1df` | Online | https://n8n-production-a685c.up.railway.app |
| **railway** (the worker) | `15c718f0-28b3-4017-a979-7e2fec274f83` | Online | https://railway-production-d648.up.railway.app<br>private: `railway-15c718f0.railway.internal:8000` |

The worker service is confusingly *named* `railway` because it was created from a
repo called `railway`. Its private hostname is derived from that, so it is
`railway-15c718f0.railway.internal`, **not** `worker.railway.internal`.

### External services

- **DeepSeek** (`deepseek-chat`) — the judge and the extractor
- **Cohere Transcribe Arabic** (`CohereLabs/cohere-transcribe-arabic-07-2026`) — ASR
- **Bitrix24** — `cultiv.bitrix24.com`, source of chats
- **Google Drive** — where call recordings live (not yet wired)

---

## 3 · Database

⚠️ **The schema is in a database named `customer360`, NOT `railway`.**

This matters enormously. n8n stores its own **114 tables** in `railway/public`,
and one of them is called **`agents`** — the exact name our schema uses.
Installing into `railway` would have collided head-on. Separate database, same
Postgres instance: no collision, independent lifecycle, one backup covers both.

**Verified live:** 25 tables, 4 views, 12 enums, 32 foreign keys.

### Table groups

- **People:** `customers`, `customer_identities`, `agents`, `destinations`, `destination_aliases`
- **Conversations:** `raw_events`, `interactions`, `chat_messages`, `transcripts`
- **AI output:** `interaction_analysis` (pass 1), `agent_evaluations` (pass 2), `interaction_destinations`, `interaction_metrics`
- **Commercial:** `deals`, `deal_stage_history`, `vouchers`, `voucher_redemptions`, `loyalty_ledger`, `crm_field_map`
- **Ops:** `follow_ups`, `consents`, `customer_metrics`, `job_runs`, `model_calls`, `schema_migrations`

### Views
`v_agent_scorecard`, `v_quality_by_input`, `v_followup_discipline`, `v_funnel`

### Design decisions worth not re-litigating

- **`customer_id` is a UUID surrogate key.** Phone stays the natural key you
  *match* on (in `customer_identities`) but never the key you *join* on — phone
  numbers get reassigned.
- **`chat_messages` has a unique key on `(interaction_id, sender, sent_at, body_hash)`.**
  The Bitrix webhook resends the *entire* thread on every message; without this,
  five webhooks become five copies of every message.
- **Module scores in `agent_evaluations` are NULLABLE.** `null` means "this
  situation never arose", which is different from `0`. See §5.
- **`loyalty_ledger` is an immutable event log.** Balance is always `SUM(points)`,
  never a stored counter — so a wrong number can't quietly become permanent.
- **`customer_metrics` is fully derived**, rebuilt nightly by
  `rebuild_customer_metrics()`. Safe to truncate and recompute at any time.
- **Migrations** live in `db/migrations/001..007`. `db/migrate.sh` tracks applied
  files in `schema_migrations`, so it's safe to re-run.

---

## 4 · The repo

```
db/migrations/            001-007, applied to Railway already
db/migrate.sh             idempotent runner
services/worker/
  app/
    serve.py              entrypoint — reads PORT, binds dual-stack (see §7)
    main.py               FastAPI: /health /ready /chats/parse
                          /calls/parse-name /calls/transcribe /evaluate /score/recompute
    config.py             all config from env; validate_for() fails fast
    sources/
      base.py             Conversation / CallRecording + Protocols — THE seam
      bitrix_chats.py     webhook parser (verified against real payload)
      drive_calls.py      PBX filename decoding + Drive listing
      mock.py             fixture-backed sources for local dev
    asr/cohere_arabic.py  silence-aligned chunking; 3 backends (space/local/cohere_api)
    evaluate/
      judge.py            the two DeepSeek passes + retry-on-contract-violation
      scoring.py          weights, null handling, evidence validation
      metrics.py          everything computed rather than judged
    normalize/phone.py    E.164
    prompts/              THE RUBRIC — see §5
  tests/                  37 tests, all passing
n8n/workflows/            01 chats, 02 calls, 03 nightly
scripts/
  railway_api.py          read Railway config without the CLI
  railway_configure.py    set all service variables reproducibly
  evaluate_call.py        run the two passes on a stored transcript
  build_report.py         render an evaluation as HTML
docs/
  HANDOFF.md              this file
  bitrix-integration-spec.md   forward this to the Bitrix IT team
  first-evaluation.md     the real call, scored, and the bugs it found
fixtures/README.md        how to populate local test data (data itself gitignored)
```

### Running it locally

```bash
cd services/worker
pip install -r requirements.txt
pytest tests/ -q          # 37 pass; 4 skip without fixtures
python -m app.serve
```

---

## 5 · The rubric — the most important part

`services/worker/app/prompts/pass2_agent_quality_v1.md` reproduces the client's
`system prompt quality .docx` **criterion for criterion**. Every weight and point
value is unchanged:

| Module | Weight |
|---|---|
| 1 · Reception quality | 15% |
| 2 · Offer quality | 25% |
| 3 · Objection handling | 25% |
| 4 · Follow-up | 20% |
| 5 · Closing | 15% |

Six documented changes are in `prompts/CHANGES-FROM-SOURCE.md`. **One of them
matters more than everything else in this project:**

### `null` instead of automatic full marks

The source rubric says:
- Module 3: *"If NO objections appeared = 100 pts automatically"*
- Module 4: *"If customer replied and conversation continued = 100 pts automatically"*

Those two modules are **45% of the total weight.** A discovery call where the
customer never objected and never went quiet collects that entire 45% at full
marks for things the agent was never tested on.

**This is not hypothetical.** The first real call we scored came out at
**87.9 / "Excellent"** — on a call where **no price was ever quoted, no offer was
presented, and no timeframe was given.**

So: an absent situation scores `null`, not 100. `final_score` is computed over
the weights actually exercised, and **`weight_applied` is stored beside every
score**. The same call now reports **73.8 on 40% of the rubric** — an honest
statement rather than a flattering one.

Locked by `tests/test_scoring.py::test_source_rubric_behaviour_would_have_inflated_this_call`.

**If the business prefers the original behaviour**, one constant in `scoring.py`
reverts it. But review `weight_applied` across the first ~50 conversations first
— if most calls exercise only 40%, the real answer is a separate discovery-call
rubric with different weights.

### Which criteria may be `null` — enforced allowlist

Only these, and only for the stated reason. Everything else is always scored:
`module2.offer_completeness` (no offer), `module2.alternative_offer` (nothing
rejected), all of `module3` (that objection didn't arise), all of `module4` (no
follow-up history), all of `module5` (closing not reached).

`module1.*`, `module2.attitude` and `module2.value_selling` are **always
assessable**. A null there is a contract violation and the response is re-asked.

### Never asked of a model

Response times, durations, message counts, talk ratio, after-hours, language
match. All computed in `metrics.py` from timestamps and handed to the judge as
authoritative. Ask an LLM to count seconds and it guesses, and the guess changes
between runs of the same prompt.

### The scoring engine does the arithmetic, not the model

`judge.py` discards whatever `final_score` the model reports and recomputes it
from the criterion breakdown. It also:
- verifies every quote in `evidence[]` appears **verbatim** in the conversation
  (guards against fabricated citations)
- rejects unjustified `null`s and stage/score contradictions, then **re-asks once**
  with the specific problem named

---

## 6 · Results so far

### ASR is validated

Test call: `q-3009-05XXXXXXXX-20260701-170522-1782914722.226.wav`
8 kHz **mono**, 16-bit PCM, 8 min 20 s. Cohere Transcribe Arabic returned
**13/13 chunks**, coherent Gulf dialect, correct proper nouns
(اسطنبول, طرابزون, بورصة, سبنجة). Whisper large-v3 is ~11 WER points worse on
Arabic and would not have held dialect this well.

### The evaluation

A discovery call. Agent خالد, customer أبو عبدالله. Group travel for 3 families,
Istanbul, 10 days, end of July. The agent correctly told the customer Trabzon is
~12h by road (not 4) and restructured to Sapanca/Bursa day trips — genuinely good
product knowledge. **But: no price quoted, no offer presented, and no timeframe
given for the quote.**

| | M1 | M2 | M3 | M4 | M5 | Final |
|---|---|---|---|---|---|---|
| Human (me) | 90 | 80 | null | null | null | **83.8** Good |
| DeepSeek, 1st attempt | 100 | 100 | null | null | null | **100.0** Excellent ❌ |
| DeepSeek, current | 80 | 70 | null | null | null | **73.8** Good ✅ |

`weight_applied = 0.40`. Cost ≈ 11.5k tokens per conversation across both passes.

### Three prompt bugs the live run exposed, all fixed

1. **Pass 1 named the agent as the customer** — returned `customer.name: "خالد"`,
   who is the *agent*. On an unlabelled mono transcript it grabbed the first name
   it heard. Unfixed this mints fake customer records that real people then get
   merged onto. Now returns أبو عبدالله correctly.
2. **Unjustified `null` bought a perfect module** — it nulled `value_selling` and
   `alternative_offer`, leaving only `attitude: 25/25`, so Module 2 scored 100.
3. **It contradicted itself** — `stage_reached: "offer_presented"` while its own
   notes said no offer was presented.

Each is now locked by a test in `tests/test_contract_validation.py`.

### One residual, unfixed

The model wrote `next_step_transition: 10` while its own evidence text said
*"...(15 pts) but no timeframe given (0 pts). Total 15/25."* Field and rationale
disagree by 5 points. LLMs judge well and transcribe their own numbers badly.
Watch it over the first 50 evaluations; the fix is to have the model emit
sub-points and sum them locally, as module totals already are.

### The biggest known weakness

**The recording is single-channel.** Nothing mechanically separates agent from
customer, so speaker attribution is **inferred from content**. The prompt
suppresses the ABSOLUTE RULES (anger, ignoring the customer, defeatist language)
whenever `diarization = none`, because a zero handed out on a guessed attribution
is worse than a missing score.

**Free fix, and it's the highest-value ask in this project:** have whoever runs
the PBX record **two channels** (Asterisk `MixMonitor` with `r()`/`t()` writes
the legs separately). That makes attribution exact and `agent_talk_ratio`
computable, for one config change.

---

## 7 · Gotchas that cost real time

Do not rediscover these.

### Railway runs the start command WITHOUT a shell

Four failed deploys came from this one fact. `--port $PORT` and
`--port ${PORT:-8000}` are passed to the process as **literal text**:

```
Error: Invalid value for '--port': '${PORT:-8000}' is not a valid integer.
```

Setting the `PORT` variable did nothing, because the expansion that would have
read it never ran. `app/serve.py` reads `PORT` in Python. **Never put shell
syntax in a Railway start command.**

### Railway's private network is IPv6-only

A service bound to `0.0.0.0` is unreachable at `<service>.railway.internal`,
regardless of what the healthcheck says. But the edge proxy and healthcheck reach
the container over **IPv4**. `serve.py` therefore creates the listener itself
with `IPV6_V6ONLY = 0`, serving both families. `--host ::` alone works only
because most kernels default `bindv6only=0` — on one that doesn't, it binds
v6-only, refuses the healthcheck, and still logs
`Uvicorn running on http://[::]:8000`.

### Railway needs a domain before the healthcheck can pass

An "Unexposed service" has no target port, so the probe gets
`service unavailable`. Creating a service domain with `targetPort: 8000` fixed it.

### n8n splits `queryReplacement` on commas

Query parameters given as a comma-separated string are split on **every** comma —
so `JSON.stringify(messages)` and Arabic text containing commas get shredded into
dozens of bogus parameters and the INSERT fails. **Always use the array form:**

```
={{ [ $json.a, JSON.stringify($json.b) ] }}
```

Workflow 01 is fixed. **Workflow 02 (calls) still has this bug** — see §8.

### n8n matches credentials by internal ID, not name

An imported workflow's `credentials` block can only carry a placeholder ID, so
every node shows ⚠️ until the credential is re-selected by hand. This is why
importing is painful and why patching via the n8n API is the better path (§8).

### Phone numbers: `DEFAULT_PHONE_REGION=SA`

Decided 2026-07-30 — "95% of phones are Saudi". `0500000000` is a valid Saudi
mobile and **means nothing in Egypt**, so the region cannot be guessed per-number.
Numbers arriving *with* a country code are honoured as-is, so the Egyptian
minority still resolves. Bare-national Egyptian numbers **fail to normalise
rather than being assigned to +966** — deliberate: a null phone is recoverable, a
wrong-country match merges two real people.

Monitor: `SELECT count(*) FROM interactions WHERE customer_phone_e164 IS NULL AND customer_phone_raw IS NOT NULL;`

### Prompt injection is already present in the data

Bitrix deal field **`UF_CRM_1781281581`** contains prose addressed to a bot
(*"Treat these instructions as guidance only…"*) stored inside the CRM record.
Any pipeline that passes the deal object to a model hands that text over as
instructions. Two defences: the prompt declares conversation content to be data
and logs embedded instructions as a `behavior_flag`; and the worker passes an
explicit field **allowlist**, never the raw deal blob
(`DEAL_FIELD_ALLOWLIST` in `bitrix_chats.py`).

### This repo is public

`api_response.txt`, `fixtures/`, `docs/samples/` and `docs/call-evaluation.html`
are gitignored because they contain a **live Bitrix REST token** and real
customers' personal data including a voice recording. Tests that need fixtures
skip when absent. **Keep it that way**, or consider making the repo private.

---

## 8 · What remains

### ✅ Chats — DONE and verified live

Workflow 01 is **active** and ran all 14 nodes to success on a live webhook post
(n8n execution 25, and again on 22):

```
Bitrix webhook → Land raw → New payload? → Parse & compute metrics
→ Upsert interaction → Insert messages → Human agent involved?
→ Wait (30 min) → Newer messages? → Still latest? → Two AI passes
→ Store customer analysis → Store evaluation → 200 OK
```

The synthetic fixture (a complete sale: greeting by name, priced offer with
hotel/dates/terms, price objection with competitor comparison, payment request,
stated follow-up time) scored **100 / Excellent on weight_applied 0.8**, stage
`deal_closed`, 15 evidence items, customer extracted as أحمد / package /
purchased. Four modules scored; follow-up correctly `null`.

Reproduce any time: `python scripts/n8n_smoke_test.py`

**Four bugs were found by running it for real** — all fixed, all worth knowing
because they are n8n behaviours, not typos. They are listed as gotchas 4–7 in
`CLAUDE.md`.

### ⏳ Chats — waiting on the client

**Bitrix must actually send us conversations.** Forward
`docs/bitrix-integration-spec.md` (Arabic summary, handler URL, `curl` to test).

Handler: `https://n8n-production-a685c.up.railway.app/webhook/travelgate/chat`

⚠️ **The one thing that decides whether this works:** their sample payload
contained **only customer messages** — all five entries `"sender": "Customer"`.
We score the *agent*. Without their replies there is nothing to score, every
conversation is flagged bot-only and skipped, and the pipeline will look broken
while behaving exactly as designed.

They also need `OnImOpenLinesMessageAdd` (or to send the full session history on
close). None of the three events currently on their outbound webhook carries a
human agent's reply.

### ❌ Calls — built, never run

Workflow 02 is **inactive and untested**. It needs:

1. **A Google Drive service account** — JSON key plus the recordings folder ID,
   with the folder shared to the service account's email. Then set
   `DRIVE_CALLS_FOLDER_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` on the worker.
2. A first run against a real recording. The ASR itself is already proven (13/13
   chunks on the sample call), and the same comma/jsonb parameter fixes from
   workflow 01 have been applied — but **nothing in workflow 02 has ever
   executed**, so treat it as unverified.
3. **Two-channel recording from the PBX** — free, one config change, and it turns
   speaker attribution from inferred into measured. Highest-value ask available.

### ❌ Nightly (workflow 03) — inactive

Fixed and ready, but there is nothing to aggregate until real conversations flow.
Activate it once they do.

### 🔒 Security — do soon

- **Rotate the Bitrix REST token** (`v7yx…`). Highest priority: it was in a
  folder headed for a public repo.
- Rotate the DeepSeek API key and the Postgres password — both appeared in a chat
  transcript.
- Postgres public networking has already been turned **off**. Keep it off; use
  `scripts/railway_api.py` or n8n for database access.

## 9 · Open decisions

| # | Decision | Why it matters |
|---|---|---|
| 1 | **Bitrix or DeepSeek as source of truth?** | The Bitrix bot already writes nationality, city, language, lead temperature and a rolling Arabic summary into `UF_CRM_*` fields. DeepSeek extracts the same things. Pick one, or you get two answers with no tie-breaker. |
| 2 | **Which privacy regime?** | Egyptian and Saudi PDPL differ, and you are storing **voice recordings**. `customers.residence_country` decides which applies; `consents` (with `kind='call_recording'`) must satisfy both if you operate in both. |
| 3 | **Name precedence** | Deal 13682 is titled *"Ahmed Foad"* while its own comments describe *"العميلة جنة"* — a different person and a different gender. Current order: CRM contact → deal title → AI-extracted, recorded in `customer_identities`. Confirm. |
| 4 | **Map the 43 `UF_CRM_*` fields** | Their codes are unix timestamps, so nobody can read them. 6 are mapped in `crm_field_map`; the rest are unknown. Do it while someone still remembers. |
| 5 | **Discovery vs. closing rubric** | See §5. Let ~50 scored conversations decide, not an assumption. |

---

## 10 · Secrets

**None are in this file or the repo.** Where each lives:

| Secret | Where to get it |
|---|---|
| Postgres password | Railway → postgres → Variables → `PGPASSWORD` |
| `DATABASE_URL` | Railway → worker → Variables (points at `customer360`) |
| `WORKER_API_KEY` | Railway → worker (service `railway`) → Variables |
| `DEEPSEEK_API_KEY` | Railway → worker → Variables |
| `BITRIX_WEBHOOK_SECRET` | Railway → worker → Variables (generated; give to Bitrix IT) |
| Bitrix REST token | Bitrix24 → inbound webhook. **Rotate the current one.** |
| Railway project token | You hold it. Used by `scripts/railway_api.py` via `RAILWAY_TOKEN`. |

Read them without the CLI:

```bash
export RAILWAY_TOKEN=<project token>
python scripts/railway_api.py info
python scripts/railway_api.py vars railway
python scripts/railway_api.py raw railway WORKER_API_KEY
```

Set them all reproducibly:

```bash
export RAILWAY_TOKEN=... DEEPSEEK_API_KEY=... PGPASSWORD=...
python scripts/railway_configure.py            # dry run
python scripts/railway_configure.py --apply
```

---

## 11 · Verify the current state in 60 seconds

```bash
# 1. worker alive
curl https://railway-production-d648.up.railway.app/health
# -> {"status":"ok","version":"1.0.0"}

# 2. what is configured
curl -H "X-API-Key: $WORKER_API_KEY"   https://railway-production-d648.up.railway.app/ready
# -> database: ready, judge: ready
#    chats_source: missing BITRIX_WEBHOOK_TOKEN  (only needed for backfill pull)
#    calls_source: missing DRIVE_* vars          (expected — calls not wired)

# 3. tests
cd services/worker && pytest tests/ -q          # 37 passed

# 4. the whole chats pipeline, live
export N8N_API_KEY=...
python scripts/n8n_smoke_test.py                # expect PASS, 13/13 nodes
```

If step 4 passes, everything on our side works and the only thing missing is
real data from Bitrix.

**Status:** database live and migrated; worker deployed and healthy with DeepSeek
and Postgres connected; ASR and the two-pass evaluation proven on a real call;
**chats workflow verified end to end and active**; calls workflow built but never
executed; Bitrix not yet sending.
