# CLAUDE.md — read this before touching anything

TravelGate **Customer 360 & Sales Quality**. Takes Arabic customer conversations
(Bitrix24 chats, recorded phone calls), extracts what the customer wants, and
scores how well the agent handled it against a 5-module rubric.

Saudi Arabia (majority) and Egypt. Conversations are Arabic, Gulf and Egyptian
dialect. Full history and rationale: **`docs/HANDOFF.md`** — read it before
making decisions, not after.

---

## Status in one line

**Chats work end to end and are live.** Calls are built but never run — no Google
Drive credentials.

**Bitrix is now sending real data** to `/webhook/travelgate/chat-message`, which
is served by workflow **01c, n8n id `H7r5YWGJ3nNVA99Z`**, deployed from this repo
on 2026-08-23 and verified live. It stores and does not score.

A second, hand-built 6-node handler (`5iGvWrBUoWckBU6b`, "01c · Chats ·
per-message store (Bitrix)") held that path first. It is **deactivated, not
deleted** — keep it as the rollback. Do not reactivate it without reading
`docs/HANDOFF.md`: it computed `first_response_seconds` in SQL as
`min(agent) - min(customer)` (negative whenever the agent opens), invented
`now()` for unparseable timestamps, created stub `deals`/`agents` rows, dropped
empty attachment turns, and numbered `seq` with `count(*)+1`, which collided for
two messages in the same second.

Its rows were migrated from `external_source = 'bitrix'` to `'bitrix_chat_api'`
so threads would not split across the two namespaces. The 16 older rows still
under `'bitrix'` belong to workflow 01 and were deliberately left alone.

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

---

## Commands

```bash
# tests — no credentials needed (37 pass; 4 skip without fixtures)
cd services/worker && pytest tests/ -q

# what is configured, live
curl -H "X-API-Key: $WORKER_API_KEY" https://railway-production-d648.up.railway.app/ready

# read/set Railway config without the CLI
export RAILWAY_TOKEN=...
python scripts/railway_api.py info
python scripts/railway_configure.py --apply     # needs DEEPSEEK_API_KEY, PGPASSWORD

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
db/migrations/         001-007, already applied to Railway
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
n8n/workflows/         01 chats (live), 01c store-only chat API,
                       02 calls (untested), 03 nightly
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
