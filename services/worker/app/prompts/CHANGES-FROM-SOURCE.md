# Changes from `system prompt quality .docx`

The rubric in the .docx is the authority. Every module, criterion, point value
and weight is reproduced unchanged. This file lists the six changes made to turn
it into a production prompt, and why each one is necessary.

Rubric version: **1.0.0** · Prompt version: **pass2-agent-quality-v1**

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
