# TravelGate · Customer 360 + Sales Quality

Chats and calls land in one Railway Postgres, get transcribed and scored by two
independent AI passes, and roll up into per-customer and per-agent views.

**Status: everything except the two data sources is built and tested.** The day
the chats API and the calls folder are handed over, go-live is configuration —
not development. See [Go-live](#go-live) for the exact remaining steps.

---

## Architecture

Everything runs in one Railway project, on Railway's private network.

```
Bitrix webhook ─┐
                ├─→ n8n ─→ worker (FastAPI) ─→ Postgres
Drive recordings┘           │
                            ├─ Cohere Transcribe Arabic  (calls → text)
                            └─ DeepSeek ×2               (customer / agent)
```

| Service | What it is | Why |
|---|---|---|
| **Postgres** | Railway plugin | 24 tables, 4 views, the warehouse |
| **n8n** | Railway template, self-hosted | Scheduling, retries, branching, and a UI a business user can read |
| **worker** | This repo, `services/worker` | ASR chunking and rubric arithmetic belong in tested Python, not in n8n nodes |

Every n8n node is a single HTTP call to the worker. n8n never does arithmetic
that ends up in a score.

---

## Layout

```
db/migrations/          001-007, run in order
services/worker/
  app/
    sources/            ← the seam the two pending APIs plug into
      base.py             Conversation / CallRecording + Protocols
      bitrix_chats.py     webhook parser (verified) + REST puller (unverified)
      drive_calls.py      PBX filename decoding + Drive listing
    asr/cohere_arabic.py  silence-aligned chunking, 3 backends
    evaluate/
      judge.py            the two DeepSeek passes
      scoring.py          weights, null handling, evidence validation
      metrics.py          everything computed rather than judged
    normalize/phone.py    E.164
    prompts/              ← the rubric, versioned
    main.py               FastAPI
  tests/                  26 tests, all passing
n8n/workflows/          3 importable workflows
docs/first-evaluation.md  the end-to-end test on a real call
```

---

## The rubric

`app/prompts/pass2_agent_quality_v1.md` is your `system prompt quality .docx`,
reproduced criterion for criterion. Every weight and point value is unchanged.

Six production changes are listed and justified in
[`CHANGES-FROM-SOURCE.md`](services/worker/app/prompts/CHANGES-FROM-SOURCE.md).
The one that matters:

> **Absent situations score `null`, not automatic full marks.** As written, a
> discovery call collects 45% of the rubric free — 100 for objection handling
> because nobody objected, 100 for follow-up because the customer was still
> replying. The first real call we tested scored **87.9 "Excellent"** that way,
> having never quoted a price.

Final scores are computed over the weights actually exercised, and
`weight_applied` is stored beside every score. **83.8 on 40% of the rubric** is
an honest number; 87.9 is not.

### Two passes, never one

Pass 1 extracts what the customer wants. Pass 2 scores the agent. Separate
prompts, separate API calls, neither sees the other's output. A single prompt
doing both lets an angry customer drag down the agent's score and lets a strong
agent inflate the forecast — and you cannot tell afterwards which happened.

### Never asked of a model

Response times, durations, message counts, talk ratio, after-hours,
language-match. All computed in `metrics.py` from timestamps and passed to the
judge as authoritative. Ask an LLM to count seconds and it guesses, and the
guess changes between runs.

---

## Local development

```bash
cd services/worker
pip install -r requirements.txt
cp ../../.env.example .env        # fill in DEEPSEEK_API_KEY at minimum
pytest tests/ -q                  # 26 passing, no credentials needed
uvicorn app.main:app --reload
```

Verify the Bitrix parser against the captured payload:

```bash
python -m app.sources.bitrix_chats --parse ../../api_response.txt
# chat15556 · facebook · 5 messages · bot_only=True
```

`bot_only=True` is correct and important: that thread never had a human agent,
so it must never reach agent scoring.

---

## Go-live

### 1 · Postgres — ✅ done

Applied 2026-07-29 to project `9323ae43…`. **25 tables, 4 views, 12 enums,
32 foreign keys.** Verified live: Arabic name folding collapses `القاهرة`/`القاهره`,
`rebuild_customer_metrics()` runs, the bot agent row is seeded.

⚠️ **The schema lives in a database called `customer360`, not `railway`.**
n8n owns `railway/public` with 114 tables of its own — one of which is named
`agents` and would have collided with ours. Separate database, same instance:
no collision, independent lifecycle, one backup covers both.

To re-run or extend:

```bash
bash db/migrate.sh "postgresql://postgres:PASSWORD@HOST:PORT/customer360"
```

`migrate.sh` tracks applied files in `schema_migrations`, so it is safe to
re-run. Turn public networking back off on the Postgres service when finished.

### 2 · n8n

Deploy the Railway n8n template into the same project, then set
`WORKER_URL=http://worker.railway.internal:8000` and `WORKER_API_KEY`.
Import the three workflows from `n8n/workflows/`. Create two credentials:
`railway-pg` (Postgres) and `drive-sa` (Google service account).

Add a Railway **volume** mounted at `/tmp/customer360` on **both** n8n and the
worker, so call audio passes by path instead of base64 over HTTP.

### 3 · Worker

```bash
cd services/worker && railway up
```

Set the variables from `.env.example`. Then confirm:

```bash
curl -H "X-API-Key: $WORKER_API_KEY" https://<worker>/ready
```

Every capability reports `ready` or names the exact missing variable.

### 4 · The two pending sources

**Chats — Bitrix.** The push path is already written and verified against your
real payload; point the Bitrix outbound webhook at n8n's
`/webhook/travelgate/chat`. For backfill, probe which REST methods your portal
exposes before anything depends on them:

```bash
python -m app.sources.bitrix_chats --probe
```

**Calls — Drive.** Create a service account, share the recordings folder with
its email, set `DRIVE_CALLS_FOLDER_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON`.
Filename decoding is already verified against a real recording.

---

## Open decisions

These are cheap now and expensive once data exists.

| # | Decision | Why it blocks |
|---|---|---|
| 1 | ~~`DEFAULT_PHONE_REGION`~~ — **decided: `SA`** (2026-07-29, "95% Saudi") | Settled. Numbers arriving with a country code are honoured as-is, so the Egyptian minority still resolves. Bare-national Egyptian numbers will fail to normalise rather than be mis-assigned to +966 — recoverable, unlike a silent wrong-country merge. |
| 2 | **Ask the PBX team for two-channel recording** | Free, one config change, and it makes agent-vs-customer attribution exact instead of inferred. Currently the largest source of uncertainty in every call score. |
| 3 | **Bitrix or DeepSeek as source of truth?** | Your Bitrix bot already writes nationality, city, language, lead temperature and a rolling Arabic summary. DeepSeek is about to extract the same things. Pick one, or you get two answers with no tie-breaker. |
| 4 | **Which privacy regime?** | Egyptian and Saudi PDPL differ, and you are storing voice recordings. `customers.residence_country` decides which applies; `consents` must satisfy both if you operate in both. |
| 5 | **Name precedence** | Deal 13682 is titled *"Ahmed Foad"* while its own comments describe *"العميلة جنة"*. Order is currently CRM contact → deal title → AI-extracted, recorded in `customer_identities`. Confirm. |

Already handled, no decision needed: deal field `UF_CRM_1781281581` contains
prose addressed to a bot. It is on a hard deny-list and the worker passes an
explicit field allowlist, never the raw deal object.
