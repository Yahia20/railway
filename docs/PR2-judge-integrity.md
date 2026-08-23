# PR2 — judge integrity

Three things the judge claimed to do and did not actually do, plus the tool that
proves whether fixing them changed any real number.

Nothing in here changes a weight, a criterion, a point value or an enum. The
rubric is still 1.0.0. What changes is which of the model's assertions the code
is willing to believe.

---

## Round 4 — the stage gate, and a result that did not replicate

Round 3 shipped the counterweight, fixed `e779317b`, and left `174898da` a
false negative with the cause identified: Step 0, not Module 3. The fourth
review ruled on the fix. This section is what it turned into and what measuring
it showed.

| # | Ruling | What was done | Outcome |
|---|---|---|---|
| Q1 | **Replace the whole `MANDATORY CONSISTENCY` block with closed, field-specific rules; prompt is the fix, code is the contract guard.** | `pass2_agent_quality_v6.md`, the reviewer's text inserted verbatim where the old block was; `scoring.validate_stage_consistency` gates only the three named objections. | Shipped. `174898da` moved 0/3 → 2/3. The gate as a whole failed — see below. |
| Q2 | **Fixture policy: preserve stage shape, ASR damage and partial evidence.** | `m3_unavailable_service_cases.json` rebuilt, `fixture_policy` written into the file, six new offline tests enforce it. | Done. 14/14 ×3, all unanimous. |
| Q3 | **Freeze v5; ship v6 only after it passes ×3 on all 12 real cases, all M3 fixtures and both D6 directions.** | v5 frozen beside v4; the audit was run. | **It did not pass. v6 is not promoted.** |
| Q4 | **Set `DEEPSEEK_MODEL=deepseek-v4-flash` and `DEEPSEEK_THINKING=disabled` on Railway and in `railway_configure.py`.** | The script now writes both; `/ready` reports both; a test pins the script to the source defaults. | Done in the repo. **The Railway environment must still be updated at rollout** — see below. |

### The mechanism, and why the prompt is the only place it could be fixed

A round-3 diagnostic asked the judge directly what it did with `174898da`. Its
own `notes`: *"the conversation never reached a price offer or closing stage,
so Modules 3, 4 and 5 were dropped"*, with `refusal_check` false and all four
objections null — on a call where visa help was refused at every branch and
again when the customer offered to pay.

Module 3 was discarded on STAGE grounds before the exclusion list, its
counterweight, or the two-question test was ever consulted. The old block named
three of the four objections as requiring `negotiation` or later and said
nothing about the fourth, and the judge generalised. Every call in the
population this criterion exists to catch ends at `reception`, because nothing
is ever quoted — so the blind spot was the criterion's whole population, not an
edge of it.

v6 replaces that block with rules that apply only to the fields they name and
state that `unavailable_service_objection` is not stage-gated. The full text is
in `services/worker/app/prompts/CHANGES-FROM-SOURCE.md`. Replacement rather
than exemption is deliberate: round 3's appended paragraph enumerated Modules
2, 4 and 5 as `null`, the judge read the enumeration as an output template, and
Module 4 went null on a fixture untouched v4 scored 3/3.

**The code guard is narrower than the prompt on purpose.**
`validate_stage_consistency` enforces `negotiation` or later for
`price_objection`, `competitor_objection` and `thinking_time_objection` only.
It does not inspect `unavailable_service_objection`, and the docstring says
why: a contract violation is a re-ask, and a re-ask is a demand — encoding the
generalisation would make the code insist on the false negative the prompt was
changed to stop producing. The constraint that does hold on that criterion at
every stage, `refusal_check` true ⟺ a numeric score, is enforced by
`validate_refusal_link` exactly as before.

### The audit, ×3 — and the gate it failed

Full detail and every individual run in `scratchpad/day13/audit_v6/AUDIT.md`.

| requirement | result |
|---|---|
| 14+/14 M3 fixtures by majority | **PASS** — 14/14, all unanimous |
| `174898da` 3/3 scored | **2/3** — a real move; 0/3 under both v4.1 and v5 |
| `e779317b` 3/3 scored | **null 3/3** — and so is v5, measured the same day |
| no wrong majority among the 12 | **10/12** — `e779317b`, plus a new false positive on `f2657238` |
| D6 pair both 3/3 | **2/3, then 3/3** on a second independent triple; 6/6 on the inbound half |

Six of the fourteen fixtures now score Module 3 while the judge itself reports
`stage_reached: reception` — the combination v5 could not produce. The
mechanism is real and the prompt change addresses it. It is not enough to
promote v6:

- **`f2657238` is a new false positive and it belongs to v6.** A caller who had
  dialled the wrong company; the agent's clarification that his firm is not the
  one she means is now read as a service refusal, 2 runs of 3. v5 is null 3/3
  on it the same day. The fixture suite has boundary cases for HR routing,
  third-party bookings and call transfers, and they all held — it has no
  wrong-company case, and it needs one.
- **`174898da` is 2/3, not 3/3.**

### The finding that outranks the gate

`e779317b` is null 3/3 under v6. A v5 control was run the same day, through the
same code, the same `deepseek-v4-flash`, the same `system_fingerprint`
`a26a7955944dc5c60445bff77fac9c8e`, with an identical 10,999-token prompt on
every run and no correction re-ask. It is **null 3/3 as well** — against
`scored` 3/3 in the round-3 audit on byte-identical input.

So v6 did not break that call. **Round 3's headline claim about it does not
replicate.** Three unanimous runs on one afternoon were read as a stable
verdict and were not one.

Two consequences for how this pipeline is measured, both larger than the
prompt:

1. **A ×3 majority is a floor, not a proof.** §6 of this document already says
   never to publish a single call's score as a measurement; this extends it to
   a single call's *verdict* on a binary criterion.
2. **Every gate must include a control run of the previous prompt on the same
   day.** Comparing today's candidate against a stored result from another day
   attributes drift to the edit. The round-3 audit ran a v4 control for exactly
   this reason on the Module-4 fixture and was right to; round 4 ran one on
   three real calls, and it is the only reason the `e779317b` regression was
   not written up as v6's fault.

### Ops: the platform still has to be told

`scripts/railway_configure.py` now writes

```text
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_THINKING=disabled
```

and `test_the_platform_script_pins_the_same_model_and_thinking_as_the_source`
pins it to `judge.DEFAULT_MODEL` / `judge.DEFAULT_THINKING` so the two can only
drift on purpose. The script was **not run** as part of this round.

**At rollout the Railway environment must be updated.** An environment variable
beats every default in the source, so until the worker service's variables are
rewritten it is still asking for `deepseek-chat` — an alias whose stated
removal date, 2026-07-24, has passed, and which mapped to V4 Flash
*non-thinking* while an explicit v4 request defaults to thinking *enabled*.
Renaming without disabling thinking swaps the judge for a different one.
Verify on `/ready`, which reports `judge_model` and `judge_thinking`, and group
every rollout aggregate by `system_fingerprint`.

### Where v6 stands

Not promoted. `PASS2_VERSION` and `PASS2_PROMPT_FILE` point at v6 in the
repository so that the next audit measures the candidate under review, and v4
and v5 are frozen beside it as the texts their stored `prompt_version` rows
came from. Before anything is promoted, the two open items are the
`f2657238` false positive and a re-measurement of `e779317b` against a
same-day control.

---

## Round 3 — what the second review sent back

Iteration 2 was measured on the same day and reviewed again. Verdict: *"one more
targeted round. A1/A2/A4/A5 look shippable; M3 and consumer/version plumbing are
not ready."* Five items came back; this section is what each turned into, and
what measuring it actually showed.

| # | Finding | What was done | Outcome |
|---|---|---|---|
| 1 | **The M3 exclusion list over-corrected.** Two genuine refusals were withdrawn on real calls: `e779317b` (destination refused, company-sold alternative offered) and `174898da` (visa assistance refused). | The reviewer's COUNTERWEIGHT paragraph, inserted verbatim after the exclusion list, plus five new fixtures. | `e779317b` fixed, 3/3. **`174898da` not fixed**, and the reason is elsewhere — see below. |
| 2 | **`revision: v4.1` is not a version.** Production rows recorded `pass2-agent-quality-v4` for two materially different texts. | New file `pass2_agent_quality_v5.md`, `PASS2_VERSION = "pass2-agent-quality-v5"`; v4 frozen as history. | Done. A test now requires the label to be derivable from the filename. |
| 3 | **`deepseek-chat` is a legacy alias; capture `system_fingerprint`.** | Default model is the explicit `deepseek-v4-flash` with `thinking: disabled`; `DEEPSEEK_MODEL` / `DEEPSEEK_THINKING` override; the fingerprint and the echoed model id are captured into `usage` on both passes. | Done, and verified against the live API. |
| 4 | **Consumer semantics for ungradeable rows.** | `/evaluate` returns `contract_status`, `gradeable` and `final_score` on every path including the pre-model refusal, and the usable-score rule is stated in one place. | Done. The DB/n8n half is another agent's. |
| 5 | **Audit ×3, and a D6 Module-4 fixture.** | `compare_day.py --repeat N`, `--m4-fixtures`, `--repeat-ids`, `--expect`. | Done. Results below; full detail in `scratchpad/day13/audit_v5/AUDIT.md`. |

### The model id, measured rather than assumed

Checked against the docs and against the live API on 2026-08-22:

- `GET /models` returns `deepseek-v4-flash`, `deepseek-v4-pro` and
  `deepseek-v4-flash-vision-exp`. **`deepseek-chat` is not among them.**
- The Chat API reference lists exactly those three under `model`; `deepseek-chat`
  is absent, as it is from the pricing table.
- The 2026-04-24 changelog: *"The two legacy API model names, `deepseek-chat`
  and `deepseek-reasoner`, will be discontinued in three months (2026-07-24).
  During the current period, these two model names point to the non-thinking
  mode and thinking mode of `deepseek-v4-flash`."* **That date has passed.** The
  alias still answers, which is why nothing will fail loudly when it stops.

So the docs do clearly say the alias is deprecated, and the rename is to the id
it already resolves to — not to a different model. Both names come back
`"model": "deepseek-v4-flash"` with the same `system_fingerprint`.

**The half that is easy to get wrong.** `deepseek-chat` mapped to the
NON-thinking mode, and `thinking` defaults to *enabled*. Renaming without
disabling it swaps the judge for a different one. Measured on one probe:

| request | prompt tokens | `reasoning_tokens` |
|---|---|---|
| `deepseek-chat` | 34 | absent |
| `deepseek-v4-flash`, defaults | 112 | 28 |
| `deepseek-v4-flash` + `thinking: {"type": "disabled"}` | 34 | absent |

`DEFAULT_THINKING = "disabled"` is therefore sent explicitly, so the rename is
behaviour-preserving and a future change of the vendor's default cannot
re-baseline every score silently. `DEEPSEEK_THINKING=enabled` is available and
is a new baseline, not a tuning knob: re-run the A/A comparison before believing
anything it produces.

> **Ops follow-up, outside this PR's files.** At the time of writing this
> section `scripts/railway_configure.py` still set
> `DEEPSEEK_MODEL=deepseek-chat` on the platform, and an environment variable
> beats every default in the source. **Closed in round 4** — the script now
> writes `deepseek-v4-flash` and `DEEPSEEK_THINKING=disabled`, and a test pins
> it to the source defaults. The Railway environment itself still has to be
> rewritten at rollout; check `/ready`, which reports `judge_model` and
> `judge_thinking`, before trusting the rename in production.

### `system_fingerprint`, and why it is stored

Scores are comparable only within one model and one backend build. DeepSeek
re-points aliases without notice — `deepseek-chat` has meant V3, then V3-0324,
then V3.1, then V4-flash — and every one of those transitions passed through
this pipeline as an unmarked change of judge. `pass1.usage.system_fingerprint`
and `pass2.usage.system_fingerprint` now carry the backend id, alongside
`usage.model` (what the API says answered) and `usage.model_requested` (what we
asked for; they differ whenever an alias is in play).

A two-call evaluation whose correction re-ask lands on a different backend
records `system_fingerprint_all: [a, b]` rather than keeping the last value.
Rare, and impossible to reconstruct afterwards.

**Group every aggregate by fingerprint.** An agent's month-over-month average
that straddles a change is measuring the vendor's release schedule.

### A score is usable only when three fields agree

`/evaluate`'s `pass2` block always carries `contract_status`, `gradeable` and
`final_score`, on every path — including the pre-model refusal, which used to
omit `ungradeable_modules` and `pre_enforcement_score` and made consumers branch
on a missing key. A score may be stored, averaged, reported or shown to an agent
**only** when:

```
contract_status == "ok"  AND  gradeable  AND  final_score is not None
```

| `contract_status` | `gradeable` | `final_score` | Meaning |
|---|---|---|---|
| `ok` | `true` | number | Scored. The only usable case. |
| `ok` | `false` | `null` | Cannot occur — `gradeable false` is reported as `ungradeable`. |
| `contract_failed` | `false` | `null` | The response still contradicted itself after one correction. `contract_violations` says what. |
| `ungradeable` | `false` | `null` | Too little of the rubric survived evidence enforcement to average. `ungradeable_modules` says which were struck. |
| `unscoreable` | `false` | `null` | Too little speech to grade; no model call was made. |

The three fields are redundant on purpose. `final_score` alone cannot separate
"no number because the call was empty" from "no number because the judge broke
its own contract", and every reporting bug this pipeline has had came from a
consumer inferring one from the other. None of these is a retryable fault:
re-running an ungradeable row can manufacture a score and wastes an attempt.
`pre_enforcement_score` is diagnostic only and must never be stored as a score.

### The audit: 14 fixtures ×3, 12 real calls ×3, D6 ×3

Full detail and every individual run in
`scratchpad/day13/audit_v5/AUDIT.md`. Summary:

| suite | result |
|---|---|
| 14 M3 fixtures | **14/14 by majority, all 14 unanimous** |
| 12 withdrawn real day-13 calls | **11/12 by majority**, 11 unanimous |
| `e779317b` (must score) | **scored 25, 3/3 — fixed** |
| `174898da` (must score) | **null 3/3 — NOT fixed** |
| D6 Module-4 pair | **2/2, both 3/3** |

The ten correct withdrawals stay withdrawn, unanimously, so the counterweight
did not loosen the criterion. Every run used fingerprint
`a26a7955944dc5c60445bff77fac9c8e`, so nothing in the audit is confounded by a
backend change mid-run. Estimated cost of the whole round: **≈ $1.41**.

### Why `174898da` is still wrong — and the stage exemption that was reverted

A diagnostic run asked the judge directly. Its own `notes` on that call:
*"the conversation never reached a price offer or closing stage, so Modules 3, 4
and 5 were dropped"*, with `customer_requested_something_specific: false`.

**The counterweight never got a hearing.** Module 3 was discarded on STAGE
grounds before the exclusion list was consulted at all. The mechanism is in Step
0's MANDATORY CONSISTENCY block:

> If `price_objection`, `competitor_objection`, or `thinking_time_objection` is
> non-null, `stage_reached` must be `negotiation` or later.

Three of the four objections require an offer. The fourth is not mentioned, so
the judge generalises — reasonably. Module 3's own text has said since v3 that
Service Not Available *"does not require a previous agent offer"*, but that
sentence is ~300 lines further down and is never reached once the module has
been nulled.

That makes this a **systematic blind spot, not an edge case**: every call where
a customer asks for something the agency does not do and is turned away before
any price is quoted is a call where this criterion cannot fire. That is the
whole population the criterion exists to catch.

A one-paragraph exemption was written into the consistency block and measured.
It ended *"...even when `stage_reached` stays `reception` and Modules 2, 4 and 5
are all `null`."*

| case | v5 as shipped | + stage exemption | v4 control |
|---|---|---|---|
| `174898da` (must score) | null 3/3 ✗ | **scored 3/3** ✓ | — |
| `e779317b` (must score) | scored 3/3 ✓ | **null 2/3** ✗ | — |
| D6 `m4_outbound_agent_followup` (must score) | scored 3/3 ✓ | **null 2/3** ✗ | scored 3/3 ✓ |
| 14 M3 fixtures | 14/14 ✓ | 14/14 ✓ | — |

It fixed one regression and caused two. The v4 control settles the Module-4 one:
untouched v4 scores that fixture 3/3, so the null is attributable to the edit
and not to model spread. The likely mechanism is the wording rather than the
rule — an instruction that enumerates Modules 2, 4 and 5 as `null` reads as a
template, and Module 4 is precisely what went null.

**It was reverted.** `pass2_agent_quality_v5.md` on disk is the counterweight
and nothing else, byte-identical to the text the audit ran (sha256
`a7c74872eac34fdd2176f98fdc63a2cc593f0131b02c0acdd7078f6181af9b02`). The defect
stays open and needs its own round: wording that names no other module, a new
prompt version, and a full audit. **That round happened** — see "Round 4" at
the top of this document. `pass2_agent_quality_v6.md` replaces the block rather
than appending to it, and the guard was rewritten with it:
`tests/test_m3_fixtures.py::test_the_stage_block_is_closed_and_field_specific`
asserts v6's wording verbatim, and `::test_the_stage_block_names_no_other_module`
is the prohibition this one used to be.

### D6 — and the third renderer nobody was looking at

The Module-4 pair passed 2/2, both 3/3: an outbound agent WhatsApp follow-up is
scored, and a later INBOUND queue callback from the same customer is `null`. The
second half is the one that matters — nearly every recording in this corpus is a
queue recording, so a judge that credited an inbound callback as agent follow-up
would give away 20% of the grade for the customer's own effort as the normal
case, not the edge one.

Building the fixture surfaced a separate defect. The follow-up bullet was being
rendered in **three** places:

| renderer | who reads it | state before this round |
|---|---|---|
| `Build follow-up history` in the workflow JSON, dumped to `scripts/sql/02_build_follow_up_history.sql` | production | rebuilt — direction, queue-vs-unknown handler, message text |
| `compare_day.render_current_history` | `--history-format current`, the D6 fixtures | mirrors the SQL field for field |
| `metrics.followup_history_block` | nothing, today | **still the day-13 format**: `"{channel} by {by}"` |

The third had no callers, so nothing was broken and nothing failed. That is what
made it worth fixing rather than noting: it is a loaded footgun, one import away
from re-emitting `phone_call by unknown` into a prompt whose fixtures would keep
passing, because the fixtures go through the *other* copy. Its bullet now comes
from `metrics.later_contact_line`, and
`tests/test_followup_history_block.py` pins all three together — the two Python
renderers against each other line for line, and both against the format string
and the label literals read out of the dumped SQL. A drift in any one of them is
now a test failure.

Three renderers is still two too many. The SQL is the only one production runs,
so it stays authoritative; the Python copies exist because fixtures must be able
to build a block offline, and the tests are what keep that honest.

### Two lessons worth keeping

1. **A synthetic fixture that passes is not evidence the real call is fixed.**
   Round 2 was told *"both controls passed on clean synthetic text"*. Round 3's
   `visa_assistance_refusal` fixture — written specifically to close that gap —
   fires 3/3 while the real call it stands for stays null 3/3. The next fixture
   set should be built from the SHAPE of the real transcript: no offer stage,
   mid-turn ASR noise, an unquotable customer request.
2. **A prompt is one text.** An edit in Step 0 moved Module 4 in a fixture 500
   lines away. Nothing goes into a prompt without its own full audit, which is
   also why `--repeat 3` exists.

---

## Iteration 2 — what the day-13 review sent back

The first version of this PR was measured on one real day (81 calls, 2026-08-13)
and reviewed. The verdict was "sound direction, do not ship this version", on
five specific counts. This section is what changed in response; everything below
the next horizontal rule describes the design as it now stands.

| # | Finding | Fix |
|---|---|---|
| 1 | **79% of the enforcement effect was one call.** `e5ab9937` contributed +100 of the +126.3 total. Excluding it, mean enforcement movement over 74 calls was **+0.36** — and that one call's outcome, a 34-second conversation scored **100, Excellent**, was wrong in both directions. | All-module evidence failure becomes `evidence_ungroundable`: the module scores `null`, not its cap. |
| 2 | **"10 true fabrications" was not true.** `36c6d304` was genuine contiguous speech interrupted only by an inserted `[04:00]` — a validator false negative. | The validator normalises timestamps and speaker labels out of both sides before matching (`span-v2`). |
| 3 | **Module 3 got worse, not better.** Of v4's seven `unavailable_service_objection` flips, the three drops were right and **all four additions were wrong** — none was a refusal of a tourism product this company sells. | Prompt revision v4.1: a numbered exclusion list and a two-question test, plus nine regression fixtures. (Round 3 superseded this with `pass2-agent-quality-v5` and fourteen fixtures.) |
| 4 | **The speech gate.** Calls under 100 normalised characters produce scores that describe nothing. | `MIN_SCOREABLE_CHARS` default raised 20 → 100, and the count is now explicitly normalised. |
| 5 | **The prompt effect was never identified.** Mean +0.09 with MAE 10.0 and 15 band changes is the shape of run-to-run model noise, and pairing controls the call mix, not that noise. The report also claimed 17 band changes and printed 15. | `compare_day.py` gains the A/A machinery, the prompt switch, the current-format follow-up block, and prints every band change. |

### 1. A module nobody could ground is `null`, not 100

Restoring an unsupported deduction to its cap is right when the module still
contains a finding that stands: the judge over-reached on one criterion, and the
agent keeps those points. Applied to a module where **every** deduction was
discarded it says the opposite of what it means — the judge produced nothing
about that module that could be grounded, and full marks assert a perfect
performance on the strength of no evidence either way.

`e5ab9937` is the case. A 34-second call: the customer asks for English, the
agent says "one minute". The judge zeroed six criteria across Modules 1 and 2
and offered **one** quote for all six — having translated `أظن` into "I think"
inside it, so the validator could not find it. All six were discarded, both
modules went to 100, and the call scored **100, Excellent**. That is 1 call of
75 — but 6 of the 10 rejected findings, 150 of the 230 restored points, and 100
of the 126.3 enforcement score points.

The rule now:

| Situation | Module score | Call |
|---|---|---|
| Some deductions discarded, at least one still standing | number; discarded criteria restored to cap | scored |
| **Every** deduction in the module discarded | **`null`** — out of numerator *and* denominator | scored on what remains |
| Modules struck out leave `weight_applied` below `MIN_WEIGHT_APPLIED` (0.40) | — | **ungradeable**, `final_score = None` |

The decision, recorded: **`contract_status` gains a third value,
`"ungradeable"`**, rather than leaving these rows `"ok"` with a null score. Two
reasons. The column already carries a third value in production —
`"unscoreable"`, written by the speech gate in `main.py` — so consumers already
branch on more than two. And the exact failure the review caught was a row whose
own notes said the score was null while its stored arithmetic said 100: a state
meaning "no number" has to be visible in the column a dashboard filters on, not
inferable from `final_score IS NULL`.

**No existing key changes name or meaning.** `/evaluate` gains one additive key,
`pass2.ungradeable_modules` — `[{module, reason: "evidence_ungroundable",
discarded_criteria: [...]}]` — and every module-level reason is also written into
`pass2.warnings`, which is where an operator reads it. `gradeable` already
existed and is already `false` on these rows.

`services/worker/app/evaluate/scoring.py`: `deducted_criteria()` (taken **before**
enforcement mutates the breakdown — afterwards the question cannot be answered),
`ungroundable_modules()`, and `compute(modules, ungradeable_modules=…)`.
`judge.run_pass2` sequences them, after the correction re-ask as before.

**No workflow change is needed and none was made.** The n8n judge-failure branch
(`02-calls-ingest-evaluate.json`, "Two AI passes") tests
`(contract_status || 'ok') !== 'contract_failed'`, so an `ungradeable` response
takes the normal store path and writes a row with `final_score = null` — which is
what should happen. The call was looked at and could not be graded, and that is
worth recording; `agent_evaluations.final_score` is nullable and the existing
`gradeable=false` path already produced exactly this row. `scripts/evaluate_call.py`
prints any non-`ok` status as "NOT GRADED" and needed no change either.

### 2. The validator judges content, not rendering

A transcript is rendered with `[04:00]` segment markers and `AGENT:` /
`CUSTOMER:` labels **inserted between words that were spoken contiguously**. A
verbatim quote of that speech contains no marker; the haystack still did; so the
quote did not match. `36c6d304`: 190 characters of real agent speech about
arranging the hotel, tours, transfers and visa, rejected as fabricated —
restoring 20 points to an agent on a finding that was correct.

`scoring.strip_transcript_furniture()` removes timestamps and speaker labels and
collapses whitespace runs, and is applied to **both** the haystack spans and the
quote before matching. This is strictly a concession about rendering:

- a quote of words nobody said still fails — including `e5ab9937`'s translated
  one, which is a content difference, not a rendering one;
- `[[ASR_GAP]]` is still a hard boundary. The split happens **before**
  normalisation, so stripping timestamps can never dissolve the seam it protects;
- a quote consisting only of furniture (`[04:00]`) is refused with its own reason
  — it appears in every call and proves nothing.

`VALIDATOR_VERSION` moves `span-v1` → **`span-v2`**. A changed matcher under an
unchanged label makes every stored `pass1_validation` row uninterpretable, and
the version is in `compare_day.py`'s cache key.

The same function is now the speech gate's definition of speech —
`main.spoken_content` delegates to it — so there is one answer to "what was
actually said" rather than two that can disagree.

### 3. Module 3 — the exclusion list

The review checked all seven `unavailable_service_objection` flips v4 produced on
day 13. **Three drops correct, four additions wrong** — and the four were wrong in
one consistent way: the agent said no to something that is not a tourism product
this company sells.

- `0aa8273b` — routing a job applicant to the HR portal.
- `596c957a` — cannot control airline pricing, *while still selling alternatives*.
- `bb02b597` — no branch in Jeddah.
- `bb68337f` — denying the price is high / that a cheaper option exists. That is
  the agent's **answer to a price objection**; scoring it here counted one
  customer sentence twice and took Module 3 to zero from both ends.

`pass2_agent_quality_v4.md` gains a numbered **exclusion list** — jobs and HR,
offices and branches, support for a booking made elsewhere, prices the agent does
not set, denying the price is high, refusing a discount or favour, a person, a
pleasantry — and a test that must be answerable from the transcript before the
objection may fire: *name the tourism product the customer asked to buy, and the
agent turn that declared THAT product unavailable*. The same limits are mirrored
into the Step-0 refusals inventory that feeds the criterion, because the hard
link means an over-full inventory forces an over-fired objection.

**The file name and `PASS2_VERSION` do not change** — this was iteration 2's
reasoning, and **round 3 reversed it.** The argument was that the rubric,
criteria, enums and JSON schema were untouched and that `compare_day.py` keys
its cache on prompt **contents**, so an edit invalidates every cached answer
without a version bump; the two texts were distinguished by a `revision: v4.1`
line in the front matter. But the cache is not the consumer that matters. See
"Round 3" above: the exclusion list and the counterweight moved into
`pass2_agent_quality_v5.md` under the label `pass2-agent-quality-v5`, and
"Round 4" for `pass2_agent_quality_v6.md` under `pass2-agent-quality-v6`, which
is what `PASS2_VERSION` points at now. `pass2_agent_quality_v4.md` and
`pass2_agent_quality_v5.md` are both frozen as the texts their stored
`prompt_version` rows came from.

**Regression fixtures**:
`services/worker/tests/fixtures/m3_unavailable_service_cases.json` — one case per
day-13 flip, plus **two positive controls**. The controls are not optional: all
seven flip cases say "must NOT fire", so a prompt that had stopped scoring the
criterion altogether would pass 7 of 7 while having broken the rubric. Round 3
added five more, for fourteen in total: the two real v4.1 false negatives, the
two products the counterweight names, and a `phone_call_transfer_refused`
boundary control paired with `airport_transfer_refusal` so that a judge firing
on the English word "transfer" — or on neither sense of it — fails.

Every snippet is **synthetic**. This repository is public and the day-13 corpus is
PDPL-protected personal data, so each case reproduces the *pattern* of a real
misjudgement and none of its text. `tests/test_m3_fixtures.py` checks the file
offline — coverage of all seven flips, an assertable expectation on each, that
every case clears the speech gate, and that each negative case names the numbered
prompt rule it tests. Whether the objection actually fires is decided by the
prompt, not by code, so it is checked against the live judge by
`compare_day.py --m3-fixtures`, which writes `m3_fixtures.md`.

### 4. The speech gate is 100

`MIN_SCOREABLE_CHARS` default **20 → 100**, still env-overridable, and the count
is of **normalised** characters — timestamps, speaker labels and whitespace runs
removed — which the refusal reason now says in words.

On day 13 that refuses 11 of 81 rather than 6. Two of the five newly refused
carried a stored score, both 36.9, on transcripts that were greeting and dead-air
fragments; `e2daa006` — whose entire transcript is `هلا صباح الخير هلا صباح الخير`,
29 characters — scored 0.0 in one run and 33.1 in another, the same call twice
with no agent behaviour in between. Those were not measurements.

A duration floor was considered and rejected: duration counts silence, hold music
and IVR routing, none of which is a conversation.

This does make new scores incomparable with old ones below 100 characters. That
is the intent — those old scores described nothing — but it is a deployment
decision, so it stays an env var and is recorded here rather than living only in
a changed constant.

### 5. Measuring the prompt against the model's own noise

The day-13 run reported `prompt_delta` mean **+0.09**, MAE **10.0**, RMSE
**17.34**, and 15 of 74 calls changing performance band before enforcement. Those
numbers together identify nothing: pairing controls *which calls* are compared,
not how differently DeepSeek answers the same call twice. So `compare_day.py`
gains the ability to run the **old prompts through the new code** and measure
that floor.

```
A_i = A-run pre-enforcement − stored old score     # old prompts, new code
B_i = B-run pre-enforcement − stored old score     # new prompts, new code
```

Pre-enforcement on both sides on purpose: enforcement is a code effect and
belongs to neither prompt.

| Metric | Formula | Reading |
|---|---|---|
| noise share | `min(1, Var(A)/Var(B))` | the share of B's spread the model reproduces with no prompt change at all. At 1.0 the prompt explains nothing. |
| prompt-attributable RMS | `sqrt(max(0, Var(B) − Var(A)))` | points of movement left once A's spread is removed. |
| prompt bias | `mean(B) − mean(A)` | whether the new prompt grades higher or lower. |
| band-flip rate | A vs B, before and after enforcement | against the observed 15/74. |

New flags:

| Flag | Meaning |
|---|---|
| `--pass1-prompt` / `--pass2-prompt` | Run a named prompt file from `app/prompts/` instead of the current one. The version label is **derived from the filename** (`pass1_customer_v4.md` → `pass1-customer-v4`) — a label typed by hand is one that eventually names the wrong file, and it is stamped on every output row. A file outside the prompts directory is refused: the prompt composes against `channel_rules_*.md` loaded from there. |
| `--history-format {stored,current}` | `stored` sends the block production sent at the time. `current` re-renders it the way `scripts/sql/02_build_follow_up_history.sql` renders it today, from `later_interactions` on the item — direction stated, queue recording distinguished from an unknown agent, first agent message carried. |
| `--aa-compare A_DIR` | Compares a previous run directory (A) against `--out` (B) and writes `aa_report.md` + `aa_metrics.json`. Reads `comparison.csv` from both. No model calls, no `--input` needed. |
| `--m3-fixtures` | Runs the Module 3 regression fixtures through the live judge; writes `m3_fixtures.md` + `.json`. Add `--repeat 3` for the per-case majority. |
| `--m4-fixtures` | **Round 3.** The D6 Module-4 follow-up pair; writes `m4_fixtures.md` + `.json`. |

Three supporting fixes, each of which would otherwise have produced a confident
wrong answer:

- **The cache key can now see which prompt file was chosen.** The all-prompts
  fingerprint hashes every `*.md` in the prompts directory, and every version
  lives in that one directory — so an A/A run and a B run hashed identically
  under it, and A would have read back B's cached answers. Var(A) would have
  equalled Var(B), the noise share would have read 100%, and the conclusion would
  have been drawn from a cache hit. The two chosen files are now hashed by name
  **and** by content.
- **`--history-format current` cannot fall back silently.** When the export
  carries no `later_interactions` rows there is nothing to re-render, so the
  stored string is sent — and that is counted, printed to stderr, written into
  `dry_run.json` as `history_format_honoured`, and stated at the top of
  `report.md`. Sending the old block under a flag promising the new one is how
  Module 4 comes to look tested when it was not. **The day-13 export is exactly
  this case**: 5 of 81 rows carry a `followup_history` string and a
  `followup_history_now` copy identical to it, and no per-interaction rows at
  all. Module 4 remains untested by that input, and the run says so.
- **Every band change is listed.** The report said 17 and printed 15 because the
  list was truncated at 15; the two omitted ids, `5e2a7743` and `bb68337f`, were
  invisible to the review that had to explain them. A count that does not match
  its own list is worse than no list. The pre-enforcement band-change count is
  reported alongside, because that is the number the A/A run compares against.

### What iteration 2 measured

Three runs over the same 81 day-13 calls, all through the new code. `runAA` is
the A/A baseline (old prompts `pass1_customer_v4` / `pass2_agent_quality_v3`);
`run2` is the candidate (v5 / v4.1). Full write-up in the run directory's
`SUMMARY2.md`; totals ≈ $0.98 of a $2.50 budget.

**The noise floor is real and it is large.** Re-running the OLD prompts through
the same code moved scores by MAE **6.81**, RMSE **13.95**, and flipped **11 of
68 performance bands** — with no prompt change at all. The original report's
"15 of 74 band changes" was never the prompt's number; roughly two thirds of it
is what DeepSeek does when asked the same question twice.

| | A = runAA (old prompts) | B = run2 (new prompts) |
|---|---|---|
| mean | +2.45 | +5.04 |
| variance | 188.70 | 388.95 |
| MAE / RMSE | 6.81 / 13.95 | 13.35 / 20.35 |
| band flips, pre-enforcement | 11/68 | 24/68 |

Noise share **48.5%**, prompt-attributable RMS **14.15** points, prompt bias
**+2.58**. So the prompt effect is outside the noise — but "outside the noise" is
not "better", and nothing in this data is ground truth.

**The integrity fixes did what they were for.**

- Discarded findings **10 → 2**, restored points **230 → 30**, mean
  `enforcement_delta` **+1.68 → −0.01**. Almost all of that is the validator
  normalisation, not the prompt.
- `enforcement_delta` is **no longer one-signed**: striking a module out removes
  it from the weighted average, so it can lower a score (`596c957a`, −6.2). The
  report generator's claim that enforcement "can only ever move a score UP" was
  corrected.
- **`e5ab9937` scores 36.9**, not 100 and not 0 — and the ungroundable rule was
  not what fixed it. With `span-v2` the judge no longer produces an unfindable
  citation, so nothing is discarded. A1 remains the guard against the next such
  call; on this day its contribution was zero. Said plainly, because the first
  version of this PR claimed an enforcement effect that was one call.
- Ungradeable calls: **0 in run2**, 2 in runAA. `ffb8da52` there is the edge to
  watch — a real 4 608-character call whose model nulls had already left
  `weight_applied` at 0.40, so striking one module took it under the floor.

**Module 3: fixed, with a new margin failure.** All **7 of 7** named day-13 flips
are correct in run2, and the fixtures pass **9 of 9** including both positive
controls. Twelve objections were withdrawn overall; nine are right, one is model
variance — and **two are wrong**: `e779317b` (destination refused, alternative
offered — which the prompt says still earns the 25) and `174898da` (a visa
refusal, and visas are on the prompt's own product list). Both pass as synthetic
fixtures and fail on messy ASR, so the two-question test needs a counterweight
line before this ships: *a refusal WITH an alternative still counts*, and visas
named in the test itself.

That withdrawal pattern is also where the +2.58 bias comes from: ten of the
twelve had been scored 0 or 15, and dropping a criterion nulls it out of the
denominator rather than scoring it zero, so the weighted average rises. The
drift is arithmetic, not generosity.

**Module 4 remains untested.** The day-13 export carries no `later_interactions`
rows, so `--history-format current` fell back on all five items that have a
history — which the run states rather than hides. Testing Module 4 needs a fresh
export.

---

## The three problems

### 1. "A deduction with no quote is dropped and the points awarded" — it wasn't

`prompts/CHANGES-FROM-SOURCE.md` §4 has promised this since the first version,
and the prompt says it to the model on every call. The code appended a warning
and kept the deduction. The points were restored in prose only, and the score
that reached the database still carried the unsupported finding.

Worse, the only check that looked at evidence at all asked whether the **module**
was cited anywhere. One valid quote about the greeting excused every other
deduction in Module 1 — and Module 1 has four criteria.

Re-running one real day (81 conversations, day 13) through enforcement found
**84 unsupported findings across 34 conversations**, worth **2090 raw criterion
points**. The most-discarded criterion was
`module1_reception.missing_info_request` at 30, then `module2_offer.value_selling`
at 22.

⚠️ **Read those as an upper bound, not a result.** That replay restored every
one of them without asking the judge a single question — enforcement ran after a
correction re-ask that never mentioned evidence. The judge is now asked first
(see "Ask before you restore" below), so a real omission with an anchorable quote
survives instead of being handed back. The number worth reporting is the
post-correction one, and `compare_day.py` reports it separately as
`enforcement_delta`.

### 2. Pass 1 never checked its own quotes

Pass 2 has validated evidence quotes since day one. Pass 1 never did — and pass 1
is the pass whose output *does something*: `real_ask` puts a salesperson on the
phone, and `promises_made_by_agent` becomes a row in `follow_ups` and later an
accusation that an agent broke a promise.

The same day-13 replay found **3 fabricated `real_ask` quotes and 5 fabricated
promise quotes** already sitting in stored production rows.

### 3. A self-contradicting response became a 422, losing the evidence

`refusal_check` and `unavailable_service_objection` can contradict each other in
both directions, and both were observed live. The prompt called the link a
contract violation in words; nothing checked it. When other contradictions *were*
caught, the request died as a 422 — so the one artefact that showed what the
model did wrong was thrown away, and the row was never written at all.

---

## Ask before you restore

The order is the whole design, and it was wrong in the first draft of this PR.

Enforcement used to run **after** the single correction re-ask, and the re-ask
only ever listed structural contract violations. So the judge was never told
which of its deductions had no quote behind them — the points were simply handed
back. On the day-13 replay that restored **82 omission findings unasked**, for a
mean enforcement delta of **+15.3**. That number measures how lenient the *code*
is, not how lenient the *judge* is, and it would have been read as the latter.

The flow now is:

1. `scoring.criterion_evidence_problems()` — non-mutating — lists every
   below-cap criterion with missing or unusable evidence.
2. Those problems are appended to the structural `contract_violations` and go
   into the **one** correction re-ask, which also carries the model's own
   previous JSON. (The API is stateless: "your previous response" named nothing
   until this PR put the response in the request.)
3. Each named criterion gets one instruction with exactly two acceptable
   answers: *add one valid evidence entry naming this exact module and criterion
   — using the omission-anchor rule where the finding is an absence — or restore
   the criterion to its cap.*
4. Revalidate once.
5. **Only then** are still-unsupported findings mutated to their caps and
   recorded in `evidence_rejected`.

A genuine omission has an anchor the judge can quote, so it survives with a
reviewable citation. One it invented does not, so it is restored. The +15.3 is
therefore an **upper bound** on the enforcement effect, not the expected one —
and `compare_day.py` reports the two halves separately so the claim can be
checked rather than assumed.

### The omission anchor

Most deductions are omissions, and an omission has no words of its own. The
evidence rule in the pass-2 prompt now says what to quote: either the
customer turn that made the missing action necessary, or the contiguous agent
turn (or the closing) where it should have occurred, with `effect` naming the
absent action. Worked examples for `missing_info_request`,
`next_step_transition` and `value_selling`. **If no valid anchor can be quoted,
there is no finding: award full points.**

---

## The decision table

Applied to each criterion **after** the single correction re-ask, which has
already named it.

| Situation | Result | Why |
|---|---|---|
| Below cap, valid quote naming that exact criterion | **keep the deduction** | The finding is supported. |
| Below cap, quote missing / fabricated / stitched across `[[ASR_GAP]]` / cites a different criterion | **discard: criterion → its cap**, recorded in `evidence_rejected` | No evidence, no finding. Restoring to the **cap** and not to `null` matters: `null` would drop it from the denominator and *inflate* the module, turning a failed audit into a higher score. |
| `null`, and on the `NULLABLE_CRITERIA` allowlist | **stays `null`** | The situation genuinely never arose. Unchanged behaviour. |
| `null`, not on the allowlist | re-ask; then **`contract_failed`** | An unjustified `null` is indistinguishable from a generous score. **Changed in review:** this used to be scored anyway with a warning. |
| Criterion key, `breakdown` or whole module **absent** | re-ask; then **`contract_failed`** | **New.** Omitting the key was strictly cheaper for a lenient judge than nulling it: identical effect on the denominator, and until `validate_completeness` existed no check fired at all. |
| At full marks | untouched | Nothing was deducted, so there is nothing to support. |

### Which criterion a quote defends

`evidence[].criterion` is accepted in exactly two spellings, both of which say
the same thing twice:

- the bare criterion — `greeting`;
- the criterion prefixed with the entry's **own** module —
  `module1_reception.greeting`.

Every other shape is refused. The previous version took the text after the final
dot, so an entry declaring `module="module1_reception"` and
`criterion="module2_offer.greeting"` — or `"garbage.greeting"` — rescued Module
1's greeting deduction while contradicting itself about where the quote came
from. A citation whose two halves disagree is evidence for neither half.

### At the response level

| Situation | `contract_status` | `final_score` | `weight_applied` | HTTP |
|---|---|---|---|---|
| Normal | `ok` | number | number | 200 |
| HARD violation still present after the re-ask — an unjustified `null`, a missing module/criterion/breakdown, `refusal_check` ↔ `unavailable_service_objection` either direction, `stage_reached` contradicting applicability, or a value outside its range | `contract_failed`, `gradeable=false`, `contract_violations=[…]` | `null` | **`0.0`** | **200** |
| Every deduction in one or more modules discarded, leaving under 40% of the rubric (**iteration 2**) | `ungradeable`, `gradeable=false`, `ungradeable_modules=[…]` | `null` | number (what survived) | 200 |
| Below the spoken-content gate (`MIN_SCOREABLE_CHARS`, default **100** since iteration 2) | `unscoreable` | `null` | `0.0` | 200 |
| Unparseable JSON, or no `modules`, after retry | — | — | — | **422** |

**No partial denominator on a contract failure.** Top-level `weight_applied` is
`0.0` and **every** top-level module score is `None`. The raw model breakdown is
retained inside `payload` for forensics and nowhere else. Publishing
`weight_applied: 0.15` with one real module score attached produced something
n8n stores and an agent's month averages, while the reason it is wrong lived in
a warnings array nobody aggregates.

The old `contract_status="ok", gradeable=false` row is gone from this table on
purpose, twice over. It was originally reachable only through malformed or
missing criteria, which are now `contract_failed`. Iteration 2 then gave the
remaining path a name: a response whose modules were struck out as
`evidence_ungroundable` can legitimately fall below the 0.40 floor without
contradicting itself anywhere, and that state is now `contract_status =
"ungradeable"` rather than `"ok"` with a null score. "No number" belongs in the
column a dashboard filters on.

**A contract failure is a returned result, not an exception.** The caller gets
the payload, the warnings and the named reason, and stores a row saying this
conversation could not be graded — which is information. A 422 in its place
loses all of it. 422 now means exactly one thing: the output was not usable JSON.

---

## New response keys

All additive. Every key n8n already reads — `pass1.payload`, `pass2.payload`,
`pass2.final_score`, `pass2.modules`, `pass2.warnings`, `pass2.gradeable`,
`pass2.weight_applied`, `pass2.performance_level`, `pass2.prompt_version` —
keeps its name, position and meaning.

On `pass2` (and mirrored inside `pass2.payload`):

| Key | Type | Notes |
|---|---|---|
| `contract_status` | `"ok"` \| `"contract_failed"` \| `"unscoreable"` | Always present. |
| `contract_violations` | `list[str]` | Always present, often empty. |
| `evidence_rejected` | `list[dict]` | Always present, often empty. One entry per discarded finding: `{module, criterion, reason, model_score, restored_to, quote}`. |

`Pass2Result` additionally carries `pre_enforcement_score` — the weighted score
of the breakdown the model last returned, taken the instant **before** evidence
enforcement touched it. It is not published on the HTTP response (n8n has no use
for it); it exists so `compare_day.py` can separate a prompt effect from a code
effect, which is impossible once enforcement has mutated the breakdown in place.

`pass2.usage` is now the **sum across both calls** when a correction re-ask
fired, plus an `api_calls` counter. The re-ask used to overwrite the first
attempt's usage, so the cost report undercounted precisely the conversations
that cost most.

On `pass1`:

| Key | Type | Notes |
|---|---|---|
| `pass1_validation` | `dict` | Also inside `pass1.payload`; lifted out so alert rules need not reach through a jsonb blob. `{real_ask_quote_valid: bool\|null, promises: [{index, quote_valid}], intent_evidence_valid: bool\|null, validator_version: "span-v2"}`. |

Two things to know about `pass1_validation`:

- **`null` means the field was absent, not that it passed.** `real_ask_quote_valid`
  is `null` when `real_ask` carried no quote at all, `false` when it claimed a
  real inquiry and quoted nothing, or quoted something not in the conversation.
- **The model's own fields are never overwritten.** The verdict goes alongside.
  Overwriting would destroy the evidence needed to tell a hallucination from a
  validator bug.

`intent_evidence_valid` is `null` today because the pass-1 schema has no intent
evidence field. The probe is written so that adding one starts validating it
automatically — a field that is never checked but always reports `null` is worse
than one that is absent, and the alert rules key on this name.

### One validator, one definition of "verbatim"

Pass-1 field validation, pass-2 evidence warnings and criterion-level
enforcement all route through `scoring.quote_problem`. A quote can never be
acceptable to one caller and fabricated to another. It enforces:

- one **uninterrupted** span — `[[ASR_GAP]]` marks removed machine output, so
  text either side of it may be two unrelated moments of the call, and a quote
  matched across the seam is stitched, not cited;
- the marker itself is never quotable;
- exact match first, then retried with Arabic orthography folded (hamza/alef
  forms, ya vs alef maqsura, ta marbuta vs ha, diacritics, tatweel). ASR spells
  these inconsistently, and rejecting a real quote over a hamza teaches the
  judge that citing evidence is pointless;
- **since iteration 2 (`span-v2`)**: timestamps and speaker labels are stripped
  from BOTH sides before matching, so a verbatim quote spanning two rendered
  segments matches. Rendering, not content — a quote of words nobody said still
  fails, and the `[[ASR_GAP]]` split still happens first.

### Prompts

`pass1-customer-v5` and `pass2-agent-quality-v5`. Prose only: no criterion,
weight, enum or output field changed, and pass 2's `refusal_check` and JSON
schema are byte-identical to v3. The substance is in
`prompts/CHANGES-FROM-SOURCE.md` §7 and §8, the v4.1 exclusion list is described
under "Module 3 — the exclusion list" above, and the v5 counterweight under
"Round 3".

**v4.1 was a mistake in bookkeeping, and v5 is the correction.** Iteration 2
edited `pass2_agent_quality_v4.md` in place and distinguished the texts with a
`revision:` line in an HTML comment. The argument was that the rubric had not
changed and that `compare_day.py` keys its cache on prompt contents. But
`compare_day.py`'s cache is not the consumer that matters —
`agent_evaluations.prompt_version` is, and it stamped `pass2-agent-quality-v4`
on scores produced by two texts that answer differently. The test is not "did
the rubric change"; it is "can the same input now produce a different score",
and v4.1 existed because it can. **Any edit to a shipped prompt file gets a new
file and a new label.** `pass2_agent_quality_v4.md` is frozen as the text its
stored rows were scored against.

Both prompts learn that indirect, polite and sarcastic Gulf/Egyptian objections
are objections — and, after review, both learn the limit of that rule. Two
paragraphs were **replaced, not added**:

- The rule making terminal courtesy after a price sufficient for *Need Time to
  Think* is gone. In its place: *do not infer an objection from non-purchase,
  silence, or terminal courtesy alone; a closing thanks remains neutral unless
  the customer's words also express deferral, reconsideration, future response,
  unwillingness, comparison, or pushback.*
- The "re-read the last three messages before writing `null`" nudge primed the
  model to find an objection whenever an offer did not convert. It now says to
  re-read for those specific textual signals **and to keep the objection fields
  `null` when no trigger is present**.

Non-purchase is not evidence of an objection, and Module 3 is 25% of an agent's
grade. The same correction is applied to `pass1_customer_v5.md`, including its
`lead_temperature` note: an explicit deferral cools a lead, a bare closing
thanks does not.

Pass 2 also gains the **omission-anchor** paragraph in the evidence rule
(described above), which is what makes the "ask before you restore" flow
answerable rather than a formality.

---

## Running `compare_day.py`

Re-scores stored evaluations with the current judge and puts old and new side by
side. It calls the same `run_pass1`, `run_pass2` and `MIN_SCOREABLE_CHARS` gate
that `/evaluate` calls — never a re-implementation — so a difference it reports
is a real difference in production behaviour.

```bash
# validate the input and print counts. No network, no key needed.
python scripts/compare_day.py --dry-run \
    --input day13/compare_input.json --out day13/dryrun

# one conversation, to see the shape and the cost before committing to a day
DEEPSEEK_API_KEY=... python scripts/compare_day.py \
    --input day13/compare_input.json --out day13/run1 --limit 1

# the full day
DEEPSEEK_API_KEY=... python scripts/compare_day.py \
    --input day13/compare_input.json --out day13/run1 --workers 2
```

| Flag | Meaning |
|---|---|
| `--limit N` | Only the first N items. |
| `--workers N` | Parallel evaluations, default 2. Backs off with jitter on 429 and 5xx. |
| `--only-pass2` | Skip pass 1. Halves the cost when only the rubric changed. |
| `--dry-run` | Validate input, print counts, write `dry_run.json`. No model call. |
| `--refresh` | Ignore the on-disk cache. |
| `--allow-incomplete-metadata` | Proceed when an item carries no nested production `metadata` block. Without it the run **refuses** (exit 3). |
| `--price-in` / `--price-out` | USD per 1M tokens for the cost estimate. |
| `--pass1-prompt` / `--pass2-prompt` | **Iteration 2.** Run a named prompt file from `app/prompts/` instead of the current one; the version label is derived from the filename. This is what makes the A/A baseline possible. |
| `--history-format {stored,current}` | **Iteration 2.** Which follow-up-history format to send. Falls back to the stored string, loudly, when the input carries no `later_interactions` rows. |
| `--aa-compare A_DIR` | **Iteration 2.** Variance metrics for A (that directory) vs B (`--out`); writes `aa_report.md` + `aa_metrics.json`. No model calls. |
| `--m3-fixtures` | **Iteration 2.** Run the Module 3 exclusion fixtures against the live judge; writes `m3_fixtures.md` + `.json`. |
| `--repeat N` | **Round 3.** Run every fixture or named case N times and report the per-case majority alongside all N outputs. A single green run of a prompt fixture sits inside the model's own spread: the A/A study moved 11 of 68 bands with no prompt change at all. |
| `--m4-fixtures` | **Round 3.** The D6 Module-4 pair — two synthetic calls differing only in their FOLLOW-UP HISTORY block, rendered in the current production format. One outbound agent follow-up (Module 4 must score) and one inbound queue callback (Module 4 must be null). |
| `--repeat-ids ID,...` | **Round 3.** Re-run named interaction ids from `--input` through the live judge and report the majority. Ids may be the 8-character prefix the reports use; an id that matches nothing is a hard failure, not a skip. |
| `--expect FILE` | **Round 3.** `{id: "scored"\|"null"}` for the `--repeat-ids` cases, so the run reports a verdict rather than only an outcome. Without it the cases run and are reported as unjudged. |

```bash
# the A/A baseline: OLD prompts through the NEW code
DEEPSEEK_API_KEY=... python scripts/compare_day.py     --input day13/compare_input.json --out day13/runAA --workers 2     --pass1-prompt pass1_customer_v4.md --pass2-prompt pass2_agent_quality_v3.md

# what the prompt change is worth once the model's own spread is removed
python scripts/compare_day.py --out day13/run2 --aa-compare day13/runAA

# the Module 3 regression suite
DEEPSEEK_API_KEY=... python scripts/compare_day.py --out day13/run2 --m3-fixtures

# round 3: every suite, three runs each, with verdicts on the real calls
DEEPSEEK_API_KEY=... python scripts/compare_day.py \
    --out day13/audit_v5 --input day13/compare_input.json \
    --m3-fixtures --m4-fixtures --repeat-ids e779317b,174898da,... \
    --expect day13/audit_v5/expect_real.json --repeat 3 \
    --history-format current
```

Each suite writes `<name>.md` (the majority table plus every individual run) and
`<name>.json`. The command exits non-zero if any case with an expectation is
wrong by majority, so it can gate a rollout.

Outputs under `--out`:

- **`new_results.jsonl`** — full new pass1/pass2 payloads, one object per line.
- **`comparison.csv`** — one row per conversation: old vs new final score and
  per-module scores, null counts, gradeable, contract status, evidence-rejected
  count and restored points, refusal/objection consistency old and new,
  `real_ask` old and new, the pass-1 validation flags, performance-level change
  and the unscoreable flag — plus, since iteration 2, `ungradeable_modules`,
  `ungradeable_modules_count`, `pre_enforcement_performance_level` and
  `followup_history_source`. Written with a UTF-8 BOM so Excel does not turn
  Arabic agent names into mojibake.
- **`aa_report.md`** / **`aa_metrics.json`** — written by `--aa-compare`:
  noise share, prompt-attributable RMS, prompt bias, MAE and RMSE for both runs,
  band-flip counts before and after enforcement, and the fifteen conversations
  where A and B disagree most.
- **`m3_fixtures.md`** / **`m3_fixtures.json`** — written by `--m3-fixtures`:
  pass/fail per Module 3 case, with the evidence the judge quoted for each
  failure.
- **`report.md`** — counts; the score delta **split by cause**; per-module
  deltas; findings discarded with points restored and rejection reasons per
  criterion, plus the quotes that failed; contract failures; per-criterion
  objection flips with their evidence; the speech-gate sensitivity table;
  pass-1 quotes that failed validation, each with the quote; the ten largest
  score deltas with the evidence behind each; and token/cost totals.

### The delta is split by cause

A single "new − old" number cannot be acted on: the same +15 mean is either a
prompt that judges more kindly or a validator that hands points back, and those
call for opposite responses. The report gives three:

| Component | Definition | What it measures |
|---|---|---|
| `prompt_delta` | `pre_enforcement_score − old_score` | What the new **prompt** judged differently. |
| `enforcement_delta` | `final_score − pre_enforcement_score` | What this PR's **evidence rule** handed back. Can only ever be ≥ 0. |
| `score_delta` | `final_score − old_score` | The two together. |

### Objection flips are reported per criterion

The mean number of scored Module 3 criteria moves identically whether the prompt
found a real objection it used to miss or invented one that was never raised, so
it is kept only as a coarse warning. The measurement is the per-criterion
`null → numeric` flip count and its share of comparable conversations, with the
evidence quotes printed — `thinking_time_objection` flagged, because "terminal
courtesy = Need Time to Think" was the rule most likely to fire on a customer
who simply said thank you and hung up. That rule is now replaced (below).

### The metadata block is required, not defaulted

The script used to send `{}` unless the export carried a nested `metadata`
object. The prompt calls that block "computed, authoritative — do not
recalculate" and scores several criteria as unmeasurable when a number it needs
is absent, so an empty block does not make the comparison neutral: it makes the
new run answer a different question from the old one, on a difference nothing in
the output recorded.

Each item is now classified `nested` (the exact production block —
`asr_confidence`, `diarization`, `duration_seconds`, `channels`), `rebuilt`
(reconstructed from top-level export columns, with the unfillable fields named),
or `empty`. Anything but `nested` makes `--dry-run` **exit 3** and a live run
refuse, unless `--allow-incomplete-metadata` is passed. All 81 day-13 rows are
`nested`.

### Caching

Keyed on a canonical SHA-256 of **every effective input**: conversation,
metadata block and its source, follow-up history, input type, `--only-pass2`,
prompt versions, the **contents** of every prompt file, rubric version,
validator version, model, and `MIN_SCOREABLE_CHARS`. The filename keeps the
interaction id so the directory is still greppable.

The old key was the interaction id plus two version labels. It could not see a
prompt edited without a version bump — exactly what an iteration cycle does —
and a `--only-pass2` smoke run left entries with no pass 1 in them that a later
full run read back, reporting pass-1 validation as absent for the whole day.
A cache that can answer a question it was never asked is worse than no cache.

**Input** is a JSON array; each item carries the conversation exactly as
production sent it plus the `old` block read back out of `agent_evaluations`.
Missing `old` fields are tolerated — 7 of the 81 day-13 rows had never been
scored at all, and they still re-run and report, just without a delta.

Two production behaviours the script reproduces deliberately, because getting
either wrong would make the comparison lie:

- `followup_history: null` is sent as the literal string `"unavailable"`. A
  Python `None` formatted into the template reads as the word `"None"` — a
  follow-up history the model would then try to grade.
- `kind` is a PBX/Bitrix code, not a channel name. The day-13 export is all
  `'q'` (queue call). Channel is inferred from whether the row carries audio
  evidence, because picking wrong swaps the `{{CHANNEL_RULES}}` block and
  changes what the judge is allowed to deduct for.

---

## The short-garbage call, and where the gate ended up

Day 13 contains a five-second call whose entire transcript is
`مساء الخير معكرونة من شركة` — 34 characters of speech. Under the original
20-character gate it cleared. The old judge scored it **0 on every criterion
with no evidence anywhere**; under evidence enforcement every one of those
unsupported zeros is restored and it becomes **100**. Neither number describes
anything that happened on that call.

The first version of this PR deliberately did **not** move the gate, on the
grounds that `MIN_SCOREABLE_CHARS` is what every stored score on this rubric was
gated at. It shipped two things instead:

1. **`MIN_SCOREABLE_CHARS` as an env var** (`services/worker/app/main.py`), so
   the choice is a recorded deployment decision rather than a constant that
   quietly changed.
2. **A sensitivity table in `compare_day.py`** — how many conversations fall
   under 20 / 50 / 100 / 200 spoken characters, with the mean old and new score
   of each group and the ten shortest conversations individually.

On the day-13 input the counts are **6 / 9 / 11 / 24** of 81.

**Iteration 2 made the choice from that table: the default is now 100**, refusing
11 of 81 rather than 6, and the count is of normalised characters (see
"The speech gate is 100" above). The five newly refused between 50 and 100
characters are greeting and dead-air fragments; two carried a stored score, both
36.9. `e2daa006` — 29 characters, `هلا صباح الخير هلا صباح الخير` — scored 0.0 in
one run and 33.1 in another with no agent behaviour in between. The env var
stays, and the sensitivity table stays, so the next move is made the same way.

---

### Cost

The one-item live smoke on the final code cost **$0.0064** (14,701 prompt +
2,171 completion tokens across 2 model calls: pass 1 and pass 2, no correction
re-ask needed). A full 81-conversation day is roughly **$0.50** at the default
rates, plus whatever share of conversations need a correction re-ask — each of
those adds one more pass-2 call, and those calls are now counted in the report's
`model calls` line instead of overwriting the first attempt's usage. The rates
are flags, not constants — they have moved more than once — and every money
figure in the report is labelled an estimate. Token counts are measured; money
is not.
