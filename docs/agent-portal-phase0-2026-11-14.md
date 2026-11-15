# Agent portal: phase 0 verification and approval plan

Status at completion of phase 0: **planning only; awaiting the owner's approval**. No application, scoring,
workflow, account, database schema or production configuration was changed.
The owner's message supersedes the prompt's instruction to continue automatically.

Subsequent direction: the owner authorized continuing without new spending.
Proceed with local preparation and read-only verification; do not activate paid
models, provision compute or treat the mentioned $10 as permission to spend now.
The monthly table below remains a proposal, not an activated budget. See
`agent-portal-phase1-preparation-2026-11-14.md` for subsequent work.

## What was inspected

Read the complete supplied prompt (in Downloads), `CLAUDE.md` and
`docs/HANDOFF.md`. Fetched `origin`; local `main` and `origin/main` point to
`c3bea0f`. Inspected the current migrations, dashboard, v6 entry points, call
archive, private bake-off scripts/results and the Excel call rubric.

Live checks used the Railway API, n8n API, deployed worker source, the private
Postgres service's local socket, PBX documentation/journey API, and byte-range
GETs of all 770 stored WAV headers. Database inspection ran in read-only
transactions against **customer360**. No models were invoked and no paid compute
was provisioned. Raw API responses and inspection utilities stay in the ignored
`local-reports/` directory; this document contains aggregate information only.

## Verified numbers and corrections

The comparable database snapshot was **2026-11-14 09:44 UTC / 11:44 Cairo**.
Ingestion remained active: a subsequent read had 1,069 calls and 19 pending
downloads. These are moving counts, not inconsistent snapshots.

| Claim in prompt | Live result |
|---|---|
| v6: 126 scored chats, 18 agents | Confirmed; latest grade 2026-11-14 00:50:45 UTC |
| Existing dashboard reads retired v4 | Confirmed from the deployed `/app/app/dashboard.py`: reads `qa_grades`, no `qa_grades_v6` reference |
| Personal logins do not exist | Confirmed in current API routes and application schema; access remains the shared worker API key |
| 1,067 calls: 814 inbound / 253 outbound | Confirmed at the comparable snapshot; dates Nov 1–14 |
| 770 recordings stored, 17 pending | Confirmed; 1,338,933,080 stored bytes |
| 231 calls with PBX talk >=20 s, about 13 h | 231 and 13.124 **PBX talk-counter hours** confirmed; these do not measure the stored audio workload |
| 8 kHz mono audio | All 770 WAV headers verified: PCM, one channel, 8,000 Hz, 16 bit; no header-read errors |
| Audio workload for costing | **477 stored recordings >=20 s; 23.133 actual WAV hours**. All stored audio totals 23.245 h; 250 files are empty. Budget for **55 h/month and about 1,100 eligible calls**, rather than 30 h/month |
| Extension 143 holds 610 calls | Confirmed; matching account is QA-excluded |
| Inbound: 593 missing `ended_by_agent`, 219 different | 593 missing confirmed; **221 different** at the comparable snapshot, not 219; all other inbound rows differed |
| All 52 agents have no extension mapping | Confirmed: 52 agents, zero populated `phone_extension`; 3 QA-excluded accounts |
| Journey counts run into hundreds | Confirmed; maximum 1,264 |
| No call transcription/scoring pipeline | Confirmed in application schema and deployed API; only call log/download/archive objects exist |
| Minimum thresholds | Config is archive `on`, download duration >=30 s, future ASR talk threshold 20 s; `asr_min_talk_seconds` has no current consumer |

Some v6 results must not become employee averages: of 126 scored rows, 116 have
single-agent attribution and 10 are shared. Twenty-two rows have no numeric
score (21 single, one shared). The initial weekly averages therefore have at
most **95 single-agent numeric grades**, before date filtering. Show shared and
non-sales results separately, with coverage denominators.

The existing integrity assertions were executed unchanged, inside READ ONLY:
**0 failures, 2 warnings, 35 clean**. The warnings concern one legacy follow-up
history and 70 deals without owners; they were recorded, not repaired.

### n8n evidence

| Workflow | Live state | Schedule / evidence |
|---|---|---|
| 13, `Iwq9fkb31KQaLEaW` | Active | Every 10 min, 23:00–03:59 Asia/Riyadh. Latest saved execution `380082` succeeded; result and model-call storage nodes both ran without errors |
| 11, `Ak43sKCbbzB7Amgx` | Active | Every 15 min plus 23:59, Asia/Riyadh; current DB `last_index_at` advances |
| 12, `TNHUkGV6G1SkscrQ` | Active | Every 2 min; SQL enforces the 02:00–06:00 Riyadh download window; latest stored file 2026-11-14 00:30 UTC |

11/12 deliberately do not save successful scheduled executions. Their old saved
manual executions are **not** evidence that their schedules stopped. Current
database timestamps independently confirm activity. Their initial one-input
Postgres nodes lack `executeOnce`; do not change these existing workflows as
part of this proposal. New workflows will enforce it.

`v6_mode=on`, retired v4 `mode=off`, and no v6 chats were due when checked.
The daytime v6 budget gate was closed because the last nightly provider probe
was over an hour old; the latest nightly execution did successfully refresh it.
This is expected freshness protection, not an exhausted balance.

## Answering-agent discovery

The live PBX exposes OpenAPI at `/api/openapi.yaml`, including:

`GET /api/call-log/journey?linkedid=<linkedid>` (scope `cdr:read`).

It returns a CDR/CEL-derived timeline with `ANSWER`, `RING`, `TRANSFER`, etc.
The existing key successfully read **six** real call journeys without changing
n8n. Four were inbound: each had one distinct answering extension; **three of
those four differed from the extension on the call-list row**. Repeated ANSWER
events for the same extension were present. Many apparent TRANSFER events are
ring-group fan-out, so neither their count nor the last listed leg proves a
real transfer. Some answer durations are missing.

In the two outbound examples, the journey's `agent` on ANSWER was a long
external dial string. Do not treat that field as an employee extension for
outbound calls. Use the outbound origin mapping and direction-aware rules.

Conclusion: the API offers a promising attribution source; it is **not yet a
validated attribution algorithm**. Phase 1 must cross-check a larger sample,
resolve repeated events and genuine transfers, and have the owner approve the
extension-to-person mapping before writing/using it. Unknown or multiple
answering employees remain unattributed and never affect a person's average.
No vendor contact appears necessary for the basic journey endpoint.

## Proposed implementation after approval

1. **Attribution first.** Add a separate, generated workflow that reads journeys
   through n8n's allowed egress and stores provenance/confidence. Prepare the
   mapping privately for owner confirmation. Preserve workflows 11/12. Use only
   proven inbound ownership and mapped outbound origins for personal results.
2. **Measure audio on 20 calls.** Compare the existing Sortformer/Nemotron split,
   gap fill, overlap treatment and Cohere Arabic/Whisper transcription. Prefer
   Cohere if the new sample confirms the historical Arabic advantage. Compare
   word errors, wrong-speaker evidence, billed compute time and cold starts.
   No ASR or diarization deployment is chosen as already validated today.
3. **Batch audio processing.** Proposed host: Runpod Flex, zero active workers
   when idle, one bounded nightly batch. Diarize, provisionally transcribe each
   voice to identify roles, make one masked Haiku role decision, store agent-left /
   customer-right stereo, and reuse the transcript with the final labels. This
   avoids a dependency cycle and paying for transcription twice. Recognizing
   the agent voice does not identify which employee answered the phone.
4. **Call scorecard.** The Excel sheet has 16 items whose weights sum to 100.
   Submit a concrete observation-to-0–5 table and critical-error rules for owner
   approval. Three DeepSeek readings; code computes timings, applicability,
   scores and normalized total. Evidence must match the identified speaker's
   transcript. Use missing/uncertain results, never invented zeroes. Compare
   twice-graded results on 20 calls; investigate if >25% move by >10 points.
   Procedure/accuracy checks lacking authoritative company information require
   human review rather than a model guessing the policy.
5. **Personal portal.** Extend the approved Arabic RTL C360 design, mobile/light/
   dark, with agent, leader and manager accounts. Read v6 without changing its
   scoring. Agents see their own data; leaders/managers see everyone. Include
   weekly averages, coverage, evidence, flags, audio playback, unattributed
   calls and pipeline/spend health. Obtain an export of the leaders' reference
   artifact before reproducing any parts unavailable in the repository.
6. **Stage, test, then launch.** New idempotent migrations start at 035, use
   `lock_timeout`, and add integrity assertions. Generated workflows have claim
   fencing, retries/dead letters, cost accounting and modes initially `off`.
   Test cross-account access at API level, attribution exclusions, speaker
   evidence, retention, resume/idempotency, and closed-gate/no-claim behavior.
   Run worker tests, workflow validator and integrity checks; exercise staging
   before production. Provide account handoff and single-line off switches.

### Authentication boundary

Keep the score reader and n8n writer separate. Add a dedicated `portal_auth`
schema and login role with DML grants **only** on its users, sessions, reset
tokens and auth audit/rate-limit tables. It gets no score-table writes, no
schema creation, no membership in the n8n writer role, and no elevated functions.
A separate score-reader credential has SELECT grants only on required data.
Use Argon2id hashes, opaque revocable sessions, Secure/httpOnly/SameSite cookies,
CSRF protection, login throttling and admin create/disable/reset. Authorization
comes from the server-side session's agent identity, not a request parameter.
An admin-generated one-use reset avoids a new email subscription in v1.

Audio links are short-lived and issued only after ownership/role checks.
Mask names, staff signatures, passports, cards and file names before text-model
requests. Extend the existing 90-day text/evidence purge to the new transcripts.
Do not invent consent or assume a new audio processor is already approved;
confirm the existing voice-data processing arrangement before activation.

## Monthly costs

All amounts are USD before tax. These are **planning allowances**, not measured
costs of an unbuilt pipeline. No supplier was subscribed or charged for a new
service in phase 0.

### Measured Railway baseline

Railway reports this workspace is **PRO**. API usage over the last seven days,
extrapolated to 30 days using the published container rates:

| Existing service | Monthly run rate |
|---|---:|
| n8n | $7.03 |
| Postgres, including its volume | $4.19 |
| Production worker | $1.58 |
| Worker staging | $0.48 |
| Passport MRZ service, included conservatively | $1.73 |
| Total project resources | **$15.01** |

The 14-day run rate is $15.29, supporting the baseline. No chat7-api resource
usage appeared in the returned window. Railway Pro is a **$20 minimum including
$20 usage**, not $20 plus the above $15.01. This proposal allocates the whole
Pro minimum to this stack; unrelated projects and Bitrix/PBX licence contracts
are outside the site budget. Check aggregate workspace usage before launch if
other projects consume the same included credit.

Source: [Railway plans and resource rates](https://docs.railway.com/pricing/plans).

### Proposed monthly operating envelope: 55 source-audio hours

| Component | Monthly allocation | Basis and uncertainty |
|---|---:|---|
| Railway: existing services, portal/auth, staging and audio buckets | **$20.00** | Reuse existing worker/database; allow about $2 portal growth and up to $2 bucket growth inside the roughly $5 baseline headroom. Recheck actual usage before launch |
| Runpod Flex diarization + two-channel ASR, including start/idle overhead | **$6–9** | Current 24 GB L4/A5000/3090 class is $0.75/GPU-h. Up to 110 channel-hours/month; $9 accommodates about 12 billed GPU-hours. Requires measured ASR roughly 10–20x real time, with remaining time for split/start overhead. Slower results require repricing before launch |
| Persistent model-weight cache | **$1.40** | Proposed 20 GB network volume at $0.07/GB-month; no persistent customer audio there |
| DeepSeek, existing chat grading plus call grading/retries | **$5.00 total ceiling** | Keep the existing shared $5 cap; reserve approximately $2 for chats and $3 for calls. Call token usage is unmeasured, so reserve work per lane and stop/defer if insufficient |
| One Haiku voice-role decision per call | **$0.50** | About 1,100 calls. Private bake-off has 16 recorded decisions, total $0.0030213, mean $0.0001888; projected $0.21 before headroom. Respect the existing Anthropic gate as well as this smaller lane allocation |
| Additional domain / login provider / email subscription | **$0** | Existing Railway URL and own account/session tables; admin-assisted reset |
| Unallocated safety reserve | **$3.00** | Small resource drift/retries; does not authorize bypassing any cap |
| **Total planning envelope** | **$35.90–38.90/month** | Conditional on the audio benchmark and Railway staying within its allocation |

Compute source: [Runpod GPU prices](https://www.runpod.io/pricing) and
[Flex billing](https://docs.runpod.io/serverless/pricing). The $0.75 rate is
for the **serverless** 24 GB class, not the dedicated-Pod rate. Weight-cache
storage is separate. No always-on GPU is included.

At 55 h/month, source PCM is 3.168 GB/month and same-rate stereo is 6.336 GB/month.
Keeping both for a full year without deleting existing recordings reaches about
114 GB, or $1.71/month at year end. That fits the $2 bucket allowance above;
service upload egress also consumes some Railway headroom. Bucket GET operations
and bucket egress are free. Longer retention keeps increasing storage; this
document does not authorize deleting existing recordings.
[Bucket billing](https://docs.railway.com/storage-buckets/billing).

Current v6 production accounting shows $0.429168 on 126 stored grades, with
$0.431471 across 511 model calls including retry/other attempts. No recent calls
have missing prices. Approximately $1.5–2/month for current chats is reasonable;
the proposed call rubric has not been measured. Published current Flash rates
are $0.003 cached input / $0.15 fresh input / $0.60 output per million off-peak
tokens. Repricing the stored November token totals yields about $0.36847,
different from the stored historic-price accounting. Reconcile price tables
before new spend; leave scoring unchanged.
[DeepSeek prices and off-peak window](https://api-docs.deepseek.com/quick_start/pricing/).
Haiku pricing reference: [Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/overview).

No free trial/Modal credit or temporary ASR promotion is required by this budget.
The historical bake-off's 6–8 s/call, 95% agreement and DER figures could not be
independently reproduced from the available stored result files in phase 0.
Its summary has **16** role decisions, whereas the prompt says 14/15 correct
and also mentions two misses. Those accuracy claims are inconsistent and need
the planned labeled sample, not an assumed success rate.

## Approval boundary and remaining measurements

Approve the approach and **$40/month maximum**, with a **$5 one-time maximum
for model backfill/tests**, before implementation starts. GPU tests must also
fit the approved monthly compute envelope. These are caps, not a claim that all
historical calls can already be processed for $5. Projected model backfill at
the provisional per-call allowance is roughly $1.4–2; actual token usage is
measured on the first sample before committing the remainder.

Budget checks must reserve estimated cost before claiming work and reconcile
actual usage, including failures, after completion. n8n persists each model
call; audio jobs need explicit compute usage records too. The shared DeepSeek
gate must not let calls exhaust chat capacity. New schedules explicitly use
Asia/Riyadh, and text grading remains 23:00–03:59.

The owner still approves the real extension mapping and the observation-to-score
table when they are concrete. Seek separate authorization before changing another
developer's live workflows, deleting existing rows/files/workflows, rotating keys,
changing chat scoring, contacting a vendor, or exceeding either money limit.
If the benchmark cannot cover the full 55 h workload under $40, report that
before activation; do not silently omit calls to make the budget appear to fit.

**Stop here. No phase 1 work is authorized by this document.**
