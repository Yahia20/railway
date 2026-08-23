# Changes from `system prompt quality .docx`

The rubric in the .docx is the authority. Every module, criterion, point value
and weight is reproduced unchanged. This file lists the six changes made to turn
it into a production prompt, and why each one is necessary.

Rubric version: **1.0.0** · Prompt versions: **pass2-agent-quality-v6**,
**pass1-customer-v5** (prompt history in section 7 below)

---

## 1. `null` replaces automatic full marks for absent situations

**Source behaviour**
- Module 3: *"If NO objections appeared = 100 pts automatically"*
- Module 4: *"If customer replied and conversation continued = 100 pts automatically"*
- Module 2 C4: *"Customer did NOT reject = 25 pts automatically"*

**Problem.** Modules 3 and 4 carry **45% of the total weight**. A discovery call
where the customer never objected and never went quiet collects that entire 45%
at full marks for things the agent was never tested on. Two agents with
identical behaviour score differently based only on how agreeable their customer
was, and a weak call can outscore a strong one.

This is not hypothetical: it is exactly what happened on the first real call we
tested (see `docs/first-evaluation.md`), which reached **87.9 / "Excellent"**
without a price ever being quoted.

**Change.** An absent situation scores `null`, not 100. The final score is
computed over the weights that were actually exercised, and `weight_applied`
records the denominator. A conversation with `weight_applied` < 0.40 is reported
as ungradeable rather than given a misleading number.

## 2. Module 2 Criterion 2 can be `null`

**Source behaviour.** Offer completeness always scores; a missing price is 0.

**Problem.** The source rubric has a "not reached yet" escape for Module 5
(closing) but not for Module 2 (offer). A first call that correctly ends at
requirement-gathering is therefore scored as though the agent presented an offer
with no price, no hotel and no terms — a 0 for doing the right thing.

**Change.** If no offer was presented, Criterion 2 is `null`. Attitude
(Criterion 1) is always scored, because it applies to every conversation.

## 3. Module 4 is judged across the timeline, not inside the conversation

**Problem.** For a phone call, "did the agent follow up?" cannot be answered
from the call itself — a call is continuous by definition. Left as written,
every call scores 100 on 20% of the grade.

**Change.** Module 4 reads from a `FOLLOW-UP HISTORY` block assembled from the
`follow_ups` and `interactions` tables. If that block is unavailable, Module 4 is
`null`. This is the module that the Bitrix chats integration unlocks: once chats
and calls are in the same database, "he promised a quote on the call and sent it
on WhatsApp 3 hours later" becomes a measurable fact.

## 4. Every deduction must cite a quote

**Change.** Added an `evidence[]` array. Any criterion below full marks needs a
verbatim quote, the speaker and a timestamp. A deduction with no quote is
dropped and the points awarded.

**Why.** Coaching an agent with "your value selling was weak" changes nothing.
Coaching with "at 05:12 you said *'عرض كويس جدا'* without saying what makes it
good" changes behaviour. It also makes the evaluator auditable: you can check
whether the model actually read the conversation.

**How an omission is quoted (v4).** Most deductions are omissions, and an
omission has no words of its own — which for two versions left the rule
unanswerable and, in practice, unfollowed. The evidence rule now says what to
cite: either (a) the customer turn that made the missing action necessary, or
(b) the contiguous agent turn or the closing where it should have occurred,
with `effect` naming the absent action. Worked examples for
`missing_info_request`, `next_step_transition` and `value_selling`. **If no
valid anchor can be quoted, there is no finding: award full points.**

**When the code enforces it.** Not silently, and not first. Every below-cap
criterion lacking usable evidence is named back to the model in the single
correction re-ask — alongside its own previous JSON, because the API is
stateless — with two acceptable answers: anchor it, or restore it to its cap.
Only findings still unsupported after that are restored in code and recorded in
`evidence_rejected`. Restoring first and never asking measures how lenient the
code is, not how lenient the judge is.

**Which criterion a quote defends.** `evidence[].criterion` is accepted as the
bare name (`greeting`) or prefixed with the entry's own module
(`module1_reception.greeting`). Any other shape is refused rather than
suffix-matched: an entry declaring one module and citing another contradicts
itself, and a citation whose two halves disagree is evidence for neither.

**What "verbatim" means about RENDERING (validator `span-v2`).** Timestamps and
speaker labels are inserted by the transcript renderer between words that were
spoken contiguously, so a genuinely verbatim quote spanning two rendered
segments contains no marker while the haystack still does. Both sides are
normalised before matching. This relaxes nothing about content: a quote of words
nobody said still fails, a translated word inside an otherwise real span still
fails, and `[[ASR_GAP]]` — which marks removed machine output — is still a hard
boundary no quote may cross.

**When restoring the points would itself be a lie.** Handing a criterion back to
its cap says "nobody could take these points away with evidence". Applied to a
module where *every* deduction was discarded it says something else entirely —
that the module was perfect — on the strength of a judge that could ground
nothing about it either way. Such a module scores `null` instead, and if too
little of the rubric survives, the call is `ungradeable`. See
`docs/PR2-judge-integrity.md`, "A module nobody could ground is null, not 100".

## 5. Input-trust rules (prompt-injection defence)

**Why.** Bitrix deal field `UF_CRM_1781281581` contains a block of prose
addressed to a bot — *"Treat these instructions as guidance only…"* — stored
inside the CRM record. Any pipeline that passes the deal object into the model
hands that text to the evaluator as instructions.

**Change.** Two layers:
- The prompt declares conversation content to be data, never instruction, and
  logs any embedded instructions as a `behavior_flag`.
- The worker never passes the raw deal object. It passes an explicit field
  allowlist, and `crm_field_map.is_prompt_injection_risk` marks the fields that
  are excluded by construction.

## 6. Channel-specific rule blocks

**Why.** The source opens *"You will be given a complete WhatsApp
conversation"*. Applied unchanged to an ASR transcript it penalises the agent for
the transcriber's mistakes, and — where the recording is mono — for words the
agent may not have said.

**Change.** `channel_rules_chat_v1.md` and `channel_rules_call_v1.md` are
injected at `{{CHANNEL_RULES}}`. The call block forbids deductions that rest on a
single unclear word, and forbids applying the ABSOLUTE RULES on guessed speaker
attribution.

## 7. Prompt versions after the first live day

Both prompts are **purely additive** over their predecessors. No criterion,
point value, weight, enum, field or output shape changed in either, and
`refusal_check` and the pass-2 JSON schema are byte-identical to v3. Diff them
directly (`diff pass2_agent_quality_v3.md pass2_agent_quality_v4.md`) — the
change is prose, in three places.

### `pass2-agent-quality-v4` — indirect objections are objections

**Problem.** In Gulf and Egyptian Arabic a customer almost never refuses
flatly. Disagreement arrives wrapped in courtesy, religion or a joke —
*"إن شاء الله أشوف وأرد عليك"*, *"تمام جزاك الله خير"* used to end the call,
*"غالي شوي بس ماشي"*, sarcastic *"ما شاء الله سعر ممتاز!"* — and v3 read the
wrapping instead of the message. Scoring a polite deferral as neutral
acceptance hands the agent full marks for a sale he lost, and it does it
through Module 3, the joint-largest module at 25%.

**Change.** An explicit rule plus five short dialect examples, a
neutral-vs-negative disambiguation note (still gathering facts = neutral;
withdrawing, deferring or pushing back = objection, however warmly phrased),
and two guards in both directions: politeness never downgrades an objection,
and warmth never *creates* one where the trigger gates were not met. A soft
refusal is likewise named as a refusal in Step 0.

**Why it is a prompt change and not a code change.** Whether a sentence is an
objection is a judgement about meaning, which is what the model is for. What
the code can check — that the flag and the score agree — is checked in
`scoring.validate_refusal_link`, and that check is now part of
`contract_violations`.

**Corrected in review — the rule cut too far.** Two paragraphs of the above were
**replaced**, because as first written they made non-purchase itself into
evidence of an objection:

- *"تمام، جزاك الله خير" said as a closing move right after a price ... read it
  as Need Time to Think* — deleted. A closing thanks is now explicitly
  **neutral** unless the customer's words *also* express deferral,
  reconsideration, future response, unwillingness, comparison, or pushback.
  *"خليني أفكر وأرجعلك"* carries that signal; *"جزاك الله خير"* on its own does
  not, however the conversation ended.
- *"If you conclude that NO objection arose ... re-read the customer's last three
  messages before writing `null`"* — this primed the model to keep looking until
  it found one whenever an offer did not convert. It now names the textual
  signals to re-read for and says plainly: **keep all objection fields `null`
  when no trigger is present.**

Silence, a customer who simply did not buy, and terminal courtesy are not
objections. Inventing one costs the agent up to 25% of the grade for something
that never happened, which is the same failure as the leniency this prompt was
written to fix, pointed the other way. `compare_day.py` reports per-criterion
`null → numeric` flips — `thinking_time_objection` flagged — with the quotes,
so the rule's real firing rate is measured rather than assumed.

### `pass2-agent-quality-v4`, revision v4.1 — the Service Not Available exclusion list

**Problem.** v4 widened objection detection, and one criterion over-fired as a
result. Measured on day 13 (81 calls), `unavailable_service_objection` produced
seven changes against the stored v3 scores: three withdrawals, all correct, and
**four new claims, all wrong**. Every one of the four was the same mistake — the
agent said no to something that is not a tourism product this company sells:

| call | what the agent refused | belongs under |
|---|---|---|
| `0aa8273b` | directed a job applicant to the HR portal | nothing — not a sale at all |
| `596c957a` | cannot control airline pricing, *while still selling alternatives* | nothing — the service was offered |
| `bb02b597` | no branch in Jeddah | nothing — premises are not a product |
| `bb68337f` | denied the price was high and that a cheaper option existed | Price Too Expensive |

`bb68337f` is the expensive one: it scored the same customer sentence twice,
once as a price objection at 0 and once as an unavailability objection at 0,
taking Module 3 from 100 to 0 and the call from 81.4 to 42.9.

**Change.** A numbered **exclusion list** in Module 3, each entry drawn from one
of those failures: jobs and HR routing; offices, branches and locations; support
for a booking made with someone else; prices the agent does not set while still
selling; denying the price is high or that a cheaper option exists; refusing a
discount, transfer or favour; a person rather than a service; a pleasantry. Plus
a test that must be answerable **from the transcript** before the objection may
fire — *name the tourism product the customer asked to buy, and the agent turn
that declared THAT product unavailable* — and the same limits mirrored into the
Step-0 SERVICE REFUSALS INVENTORY, since the hard link between the inventory and
the criterion means an over-full inventory forces an over-fired objection.

**Why the filename and version did not change — and why that was wrong.** The
argument at the time: no criterion, weight, enum or output field moved, the JSON
schema and `refusal_check` were untouched, a `revision: v4.1` line in the front
matter distinguished the texts, and `compare_day.py` keys its cache on prompt
**contents**, so the edit invalidated every cached answer without a version
bump.

That argument is wrong, and the round-3 review said so. `compare_day.py`'s cache
is not the consumer that matters; `agent_evaluations.prompt_version` is, and it
stamped `pass2-agent-quality-v4` on scores produced by two materially different
texts. Nothing downstream — no month-over-month average, no A/B, no incident
investigation — can separate them. "The rubric did not change" is not the test.
The test is whether the same input can now produce a different score, and
v4.1 was written precisely because it does. **Any edit to a shipped prompt file
gets a new file and a new version label. v4.1's text lives on in
`pass2_agent_quality_v5.md`; `pass2_agent_quality_v4.md` is frozen as the text
its stored rows were scored against.**

**How it is regression-tested.** Whether an objection fires is decided by the
prompt, so no unit test can assert it.
`services/worker/tests/fixtures/m3_unavailable_service_cases.json` holds one
case per day-13 flip plus **two positive controls** — without those, a prompt
that had stopped scoring the criterion entirely would pass all seven negative
cases. `compare_day.py --m3-fixtures` runs them against the live judge;
`tests/test_m3_fixtures.py` checks the file itself offline. Every snippet is
synthetic: this repository is public and the day-13 corpus is PDPL-protected
personal data, so each case reproduces the pattern of a real misjudgement and
none of its text.

### `pass2-agent-quality-v5` — an alternative does not erase a refusal

**Problem.** v4.1's exclusion list over-corrected. Re-running day 13 with it,
`unavailable_service_objection` went from 23 scored to 11: no new claims and
**twelve withdrawals**, of which ten were right and two were wrong.

| call | what the agent refused | why the withdrawal was wrong |
|---|---|---|
| `e779317b` | Turkey group programmes, offering an Azerbaijan programme instead | a redirect is the HANDLING of a refusal, not evidence it never happened. Cost the agent 94.2 → 69.8 |
| `174898da` | help with a UK student visa, at any branch, including when the customer offered to pay | a visa is named as a tourism product in the prompt's own trigger. Nulling Module 3 removed it from the denominator and RAISED the call 47.7 → 88.8 |

Both are the shape the two positive controls already covered, and both controls
passed — on clean synthetic text. That is the whole lesson: a fixture that only
ever runs on tidy input tests the rule, not the margin.

**Change 1 — the counterweight (Module 3, immediately after the exclusion
list). This is the only change v5 ships.** Text supplied verbatim by the round-3 reviewer and inserted unedited:
an alternative does not erase a refusal, so a redirect to another company-sold
service, route or destination keeps `agent_refused_or_declared_unavailable` true
and `unavailable_service_objection` numeric; and visa assistance,
airport/ground transfers and travel insurance ARE products this company sells,
with the trap named — an airport transfer is not a phone-call transfer, and only
the latter is excluded by item 6. Placement matters: it qualifies the list, so
it follows it. Put before, it reads as a general note the eight numbered
exclusions then override.

**Change 2 — a stage exemption, written, measured, and REVERTED in the same
round.** Recorded here because the measurement is the useful part and the next
attempt should start from it, not from scratch.

The counterweight alone fixed `e779317b` (3/3 scored, 25) and did **not** fix
`174898da` (0/3). The judge said why in its own `notes`: *"the conversation
never reached an offer or closing stage, so Modules 3, 4 and 5 were dropped."*
The counterweight never got a hearing, because Module 3 had already been
discarded on stage grounds. Step 0's MANDATORY CONSISTENCY list requires
`negotiation` or later for objections 1–3, says nothing about objection 4, and
the judge generalises it to all four — reasonably. Module 3's own text has said
since v3 that Service Not Available *"does not require a previous agent offer"*,
but that sentence sits 300 lines further down and is never reached once the
module has been nulled.

So a paragraph was added to the consistency list naming the exception. It
ended: *"...carries a scored Module 3 even when `stage_reached` stays
`reception` and Modules 2, 4 and 5 are all `null`."* Measured over three runs
per case:

| case | counterweight only | + stage exemption | v4 control |
|---|---|---|---|
| `174898da` (must score) | null 3/3 ✗ | **scored 3/3** ✓ | — |
| `e779317b` (must score) | scored 3/3 ✓ | **null 2/3** ✗ | — |
| D6 `m4_outbound_agent_followup` (must score) | scored 3/3 ✓ | **null 2/3** ✗ | scored 3/3 ✓ |
| 14 M3 fixtures | 14/14 ✓ | 14/14 ✓ | — |

It fixed one regression and caused two, and the v4 control rules out model
noise on the Module-4 one: untouched v4 scores that fixture 3/3. The most
likely mechanism is the wording, not the rule — a sentence that enumerates
Modules 2, 4 and 5 as `null` inside an instruction reads as a template, and
Module 4 is the module that went null. **It was reverted.** `v5` on disk is the
counterweight and nothing else, byte-identical to the text the audit ran.

The underlying defect is real and still open at v5. The next attempt needs
wording that does not name other modules, a new prompt version, and a full
audit of its own — which is `pass2-agent-quality-v6`, below. The prohibition
this paragraph used to be enforced by is now
`tests/test_m3_fixtures.py::test_the_stage_block_names_no_other_module`, beside
`::test_the_stage_block_is_closed_and_field_specific`, which asserts v6's
replacement text verbatim.

**How it is regression-tested.** Whether an objection fires is decided by the
prompt, so no unit test can assert it.
`services/worker/tests/fixtures/m3_unavailable_service_cases.json` now holds
fourteen cases: one per day-13 flip, two positive controls, the two real v4.1
false negatives, the two products the counterweight names, and a
`phone_call_transfer_refused` boundary control that must still stay `null` —
paired with `airport_transfer_refusal` so that a judge firing on the word
"transfer", or on neither sense of it, fails. `compare_day.py --m3-fixtures
--repeat 3` runs them against the live judge three times and reports the
per-case majority, because a single green run sits inside a noise floor that the
A/A study measured at 11 band flips in 68 calls with no prompt change at all.
`tests/test_m3_fixtures.py` checks the file offline, and asserts the
counterweight sentences word for word: a reworded correction is an untested one.
Every snippet is synthetic — this repository is public and the day-13 corpus is
PDPL-protected personal data — so each case reproduces the pattern of a real
misjudgement and none of its text.

### `pass2-agent-quality-v6` — the stage rules are closed and field-specific

**Problem.** v5 fixed one of the two false negatives and left `174898da`
untouched, and round 3 found out why by asking the judge. Its own `notes` on
that call: *"the conversation never reached a price offer or closing stage, so
Modules 3, 4 and 5 were dropped"*, with
`customer_requested_something_specific: false` and every objection null — on a
call where help with a UK student visa was refused at every branch and again
when the customer offered to pay.

**The counterweight never got a hearing.** Module 3 was discarded on STAGE
grounds before the exclusion list was consulted at all, and the mechanism is in
Step 0, not in Module 3. The old `MANDATORY CONSISTENCY` block read:

> - If `price_objection`, `competitor_objection`, or `thinking_time_objection`
>   is non-null, `stage_reached` must be `negotiation` or later.

Three of the four objections require an offer to have happened. The fourth is
not mentioned, so the judge generalises the rule to all four — which is a
reasonable reading of a list that names three of four siblings. Module 3's own
text has said since v3 that Service Not Available *"does not require a previous
agent offer or customer pushback"*, but that sentence is ~300 lines further
down and is never reached once the module has been nulled.

This is not an edge case. A call where the customer asks for something the
agency does not do and is turned away before anything is quoted IS the
population the criterion exists to catch, and every one of those ends at
`reception`.

**Change — the entire block is REPLACED, not appended to.** Text supplied
verbatim by the round-4 reviewer:

```md
MANDATORY CONSISTENCY — CLOSED, FIELD-SPECIFIC RULES:

Determine each objection trigger before applying stage consistency. Apply every
rule below only to the fields it names; do not extend a stage requirement from
one objection to another.

- If `price_objection`, `competitor_objection`, or
  `thinking_time_objection` is non-null, `stage_reached` must be
  `negotiation` or later.
- `unavailable_service_objection` is NOT stage-gated. Always perform the
  SERVICE REFUSALS INVENTORY, including when no offer was stated. When the
  customer requested a qualifying tourism product/service and the agent
  categorically refused it, set
  `refusal_check.agent_refused_or_declared_unavailable` to true and score
  `unavailable_service_objection` 0, 15, or 25 even if `stage_reached` is
  `reception`. This objection does not itself advance `stage_reached`.
- If the customer rejected a stated offer and `alternative_offer` is non-null,
  `stage_reached` must be `negotiation` or later.
- `offer_presented` cannot coexist with detected post-offer price pushback.
```

Replacement rather than exemption is the point. Round 3's attempt appended a
paragraph that ended *"...even when `stage_reached` stays `reception` and
Modules 2, 4 and 5 are all `null`"*; the judge read the enumeration as an
output template and Module 4 went null on a fixture untouched v4 scored 3/3.
The replacement names no other module anywhere, and
`tests/test_m3_fixtures.py::test_the_stage_block_names_no_other_module` fails
if one reappears.

Nothing else moved. The Module-3 trigger, the numbered exclusion list, the
COUNTERWEIGHT paragraph and the `refusal_check` hard link are byte-identical to
v5, as are every criterion, weight, enum and output field. `v5` is frozen as
the audited candidate it was, beside `v4`.

**The matching code change is deliberately narrower than the prompt.**
`scoring.validate_stage_consistency` now enforces `negotiation` or later for
`price_objection`, `competitor_objection` and `thinking_time_objection` — and
says in its docstring why it does not touch `unavailable_service_objection`.
Encoding the generalisation in code would re-ask the model until it reproduced
the very false negative the prompt was changed to stop producing. The link that
does hold on that criterion at every stage — `refusal_check` true if and only
if the objection is numeric — is enforced by `validate_refusal_link`, which is
where it always was.

**Measured, and it did not clear its gate.** Full detail in
`scratchpad/day13/audit_v6/AUDIT.md`:

| suite | result |
|---|---|
| 14 M3 fixtures ×3 | 14/14, all unanimous — and six of them score Module 3 at `stage_reached: reception`, which v5 could not do |
| `174898da` (must score) | **scored 2/3**, flag true — 0/3 under v4.1 and under v5 |
| `f2657238` (must be null) | **scored 2/3 — a new false positive**, a wrong-company call read as a refusal |
| `e779317b` (must score) | null 3/3 — but v5 measured the same day is ALSO null 3/3 on it, against `scored` 3/3 in the round-3 audit on byte-identical input |
| D6 Module-4 pair ×3, twice | 2/3 then 3/3 on the outbound half; 6/6 on the inbound half |

The `e779317b` line is the one to read twice. A v5 control run the same day,
through the same code, the same model id and the same `system_fingerprint`,
with an identical 10,999-token prompt and no correction re-ask, reversed a
unanimous verdict from the round-3 audit. **v6 did not break that call; round
3's headline claim about it does not replicate.** Three runs were treated as a
stable verdict and were not one, and every future gate should include a control
run of the previous prompt on the same day rather than comparing against a
stored result from another one.

**How it is regression-tested.** The fixture file was rebuilt this round rather
than added to, under the reviewer's fixture policy — the rules are written into
`m3_unavailable_service_cases.json` itself under `fixture_policy` and each is
enforced offline by `tests/test_m3_fixtures.py`:

1. **Stage shape.** Every case declares `expected_stage`. Five of the six cases
   that must fire now stop at `reception` with no price, package or offer
   anywhere in them — the shape `174898da` has and every clean v5 fixture
   lacked. One scored case was moved the other way, to a post-offer refusal at
   `negotiation`, so a prompt that merely relocated the blind spot fails.
2. **ASR damage.** Five cases carry a mid-turn truncation, a declared
   misrecognised token and an `[[ASR_GAP]]` between the request and the
   refusal. The blanket prohibition on the gap marker was removed: it
   guaranteed every fixture was cleaner than production, which is the failure
   two rounds running.
3. **Partial evidence.** `fragments.request` and `fragments.refusal` are exact
   contiguous spans, in different turns, never a whole turn, and never spanning
   the gap. A judge that wants one tidy sentence covering both has to invent
   it, and an invented quote is discarded with the finding it supports.
4. **The D6 pair** is built in `compare_day.m4_fixture_cases` through
   `render_current_history`, declares its stage, and is asserted to differ
   between its two halves in nothing but the history block.

`compare_day.py` now prints the `stage_reached` each run chose beside its
outcome, so a stage-gating failure is visible in the report instead of needing
a diagnostic run of its own.

### `pass1-customer-v5` — quotes are verified, so say so

**Problem.** `real_ask` and `promises_made_by_agent` drive real actions: one
puts a salesperson on the phone, the other becomes a row in `follow_ups` and
later an accusation that an agent broke a promise. Pass 2 has checked its
quotes since day one; pass 1 never did. Re-running one real day found
**3 fabricated `real_ask` quotes and 5 fabricated promise quotes** in already
stored production rows.

**Change.** Two additions. First, a clarification that a quote must be one
uninterrupted verbatim span — never joined across two parts of a message,
never trimmed in the middle, never quoted across an `[[ASR_GAP]]` marker — and
that these quotes are now checked in code. Second, the same
indirect-negativity note as pass 2, applied to the fields that already exist
for it: `objections[].kind` and `lead_temperature`. Reading courtesy as
enthusiasm is what puts dead leads at the top of the follow-up queue.

**Corrected in review, in step with pass 2.** The same over-reach was fixed
here: a bare closing *"تمام، جزاك الله خير"* no longer maps to `need_time`, and
`objections` stays **empty** when no trigger is present. The
`lead_temperature` note now cuts both ways — an *explicit* deferral means the
customer is `warm` or `cold` rather than `hot`, but a closing thanks on its own
is not a deferral and must not cool a customer who was otherwise ready to move.
Both passes must read the same phrase the same way, or `objections[]` and
Module 3 disagree about the same conversation and nothing downstream can tell
which is right.

**The verdict never overwrites the model.** `judge.validate_pass1` adds
`pass1_validation` alongside the model's own fields and changes none of them —
overwriting would destroy the evidence needed to tell a hallucination from a
validator bug. `null` in that block means the field was absent, not that it
passed.

---

## Deliberately NOT changed

- No weight was altered. 15 / 25 / 25 / 20 / 15 as written.
- No criterion or point value was altered.
- The performance-level bands are unchanged.
- The Arabic-language requirement for `summary` fields is unchanged.
- The two-pass separation (customer extraction vs. agent scoring never in one
  call) is preserved from the existing build spec.

## Open question for the business

The source rubric weights **objection handling at 25%** — the joint-largest
module. On a healthy first call there is often nothing to object to. Once ~50
conversations are scored, check what fraction actually exercise Module 3. If it
is low, the choice is either to keep `null` handling (current behaviour) or to
split the rubric into a **discovery-call rubric** and a **closing-call rubric**
with different weights. The data should decide this, not an assumption.
