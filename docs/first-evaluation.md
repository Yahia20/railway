# First evaluation — call `q-3009-0500000000-20260701-170522`

A live end-to-end test of the quality pipeline on one real call, run before any
of the two pending APIs exist. Purpose: prove the ASR works on real telephony
Arabic, and find out what the rubric does when it meets a real conversation.

It found a scoring bug worth fixing before go-live.

---

## What the recording is

| | |
|---|---|
| File | `q-3009-0500000000-20260701-170522-1782914722.226.wav` |
| Audio | 8 kHz, **mono**, 16-bit PCM, 8 min 20 s (500.32 s) |
| Agent extension | `3009` |
| Customer | `0500000000` → `+966500000000` (Saudi) |
| Started | 2026-07-01 17:05:22 (+03) |
| Transcription | Cohere Transcribe Arabic 07-2026, 13 silence-aligned chunks, **13/13 succeeded** |
| Diarization | **none** — see caveat |

### ASR verdict: good enough to score on

The transcript is coherent Gulf-dialect Arabic with correct proper nouns
(اسطنبول, طرابزون, بورصة, سبنجة) and correct numbers where they were repeated.
Two known errors: the company name renders as **"صوت الجيت"** (almost certainly
*TravelGate*), and there is one garbled stretch where the line genuinely dropped
— which both speakers comment on, so it is real, not an ASR artifact.

This is the model choice validated. Whisper large-v3 scores ~11 WER points worse
on Arabic and would not have held dialect this well.

### The one caveat that matters

The recording is **single-channel**. Nothing mechanically separates the agent
from the customer, so speaker attribution below is **inferred from content**, not
measured. Everything downstream of that inference inherits the uncertainty,
which is why the production prompt suppresses the ABSOLUTE RULES (anger,
ignoring the customer, defeatist language) whenever `diarization = none` — a
zero handed out on a guessed attribution is worse than a missing score.

**Cheapest fix, and it is free:** ask whoever runs the PBX to record two-channel
(Asterisk `MixMonitor` with the `r()`/`t()` options writes the inbound and
outbound legs separately). That makes attribution exact, makes `agent_talk_ratio`
computable, and costs one config change. Failing that, pyannote diarization is
~$0 self-hosted but adds a GPU dependency. **I'd push for the PBX change.**

---

## What actually happened on the call

A discovery call. Agent **خالد**, customer **أبو عبدالله**.

1. **00:00–00:35** Greeting with company name and own name, then football
   small-talk. Normal and expected in this market.
2. **00:35–02:35** Customer asks about **group tourism offers**. Agent
   establishes: 3 sub-groups, ~4–5 people each, all travelling together, each
   family separate. Asks about ages — youngest is 13, so no child pricing.
3. **02:35–04:00** Dates pinned down: end of July, ~12 days. Agent has to work
   for this; the customer confuses Hijri and Gregorian months and the agent
   patiently resolves it.
4. **04:00–05:50** Destination: Istanbul. Customer proposes splitting to
   Trabzon. **The agent corrects him — Trabzon is ~12 h by road, not 4** — and
   redirects to Sapanca and Bursa as day trips, then talks 12 days down to 10.
   This is the best moment on the call: real product knowledge, applied to the
   customer's actual situation.
5. **05:50–06:40** Customer asks for a car with driver. Line drops briefly.
6. **06:40–08:20** Agent confirms he'll check flights and hotels/apartments per
   group. Customer says start with 5 people. Agreed to continue on WhatsApp.

**No price was ever quoted. No offer was presented. No timeframe was given for
the quote.**

---

## The scores

Both columns are computed by `app/evaluate/scoring.py`, not by hand.

| Module | Weight | Production rubric | Source .docx as written |
|---|---|---|---|
| 1 · Reception | 15% | **90.0** | 90.0 |
| 2 · Offer | 25% | **80.0** | 65.0 |
| 3 · Objections | 25% | **null** — none arose | 100.0 *(automatic)* |
| 4 · Follow-up | 20% | **null** — not observable | 100.0 *(automatic)* |
| 5 · Closing | 15% | **null** — not reached | null |
| | | | |
| **Final** | | **83.8 · "Good"** | **87.9 · "Excellent"** |
| **Rubric exercised** | | **40%** | 85% |

### Module 1 — Reception, 90

- Greeting **25/25**: used the customer's name (*"استاذ ابو عبدالله"*), gave his
  own name and the company at pickup (*"...مع خالد"*), proper *السلام عليكم*.
- Understanding **25/25**: engaged the group-travel request directly and
  answered the discount question honestly (ready-made packages are built for two).
- Missing info **25/25**: neither dates nor headcount were supplied, and he
  asked for both, plus ages.
- Next step **15/25**: said he'd prepare the quote, **never said when**. This is
  the only deduction, and it is the one thing he could have fixed in five seconds.

### Module 2 — Offer, 80 (scored on 2 of 4 criteria)

- Attitude **20/25**. Calm, respectful, handled the dropped line well. Deducted
  5 for correcting the customer with *"أنت حتى غلطان"* — "you're even wrong".
  He **was** right, and the customer conceded gracefully. Borderline; flagged
  because a coachable phrasing habit is worth surfacing, not because the call
  was unprofessional. **This deduction rests on inferred speaker attribution.**
- Offer completeness **null**. No offer was presented — correct for a first call.
- Value selling **20/25**. Strong on features-to-needs (the Trabzon correction,
  restructuring the itinerary around the group). Weak on persuasion: *"عرض كويس
  جدا"* is a claim, not a reason. No urgency, no value framing.
- Alternative-when-rejected **null**. The customer rejected an *itinerary idea*,
  not an offer. Judgement call — the criterion sits inside "Offer Quality" and I
  read it as requiring an actual offer. Scoring it instead would give 25 and
  raise the final to 87.9.

### Modules 3, 4, 5 — null

- **3**: no objection arose. Asking for a discount before any price exists is
  not a price objection.
- **4**: a phone call is continuous; there is no follow-up inside it. Whether he
  sent the quote on WhatsApp is **exactly what the Bitrix chats API will tell
  us** — until then this is honestly unknowable.
- **5**: the call ended at requirement-gathering. Closing was never attempted.

---

## The finding: the rubric inflates discovery calls by ~4 points

Applied exactly as written, the source rubric awards this call:

- **100/100 on objection handling** because the customer never objected
- **100/100 on follow-up** because the customer was still replying

That is **45% of the total weight given away for situations that never arose**,
and it lands the call in **"Excellent"** — a call where no price was quoted, no
offer was made, and no timeframe was promised.

The distortion is not a rounding issue. It means:

- Agents with agreeable customers outscore agents with difficult ones, for
  reasons unrelated to skill.
- The 25% weight on objection handling — jointly the largest module — mostly
  does not measure anything on first calls.
- A genuinely weak call can outrank a strong one.

**Fix, already implemented:** absent situations score `null`, not 100. The final
is computed over the weights actually exercised, and `weight_applied` is stored
next to every score. This call reports **83.8, on 40% of the rubric** — which is
an honest statement: *on what we could measure, he did well; most of the rubric
was never tested.*

Locked down by `tests/test_scoring.py::test_source_rubric_behaviour_would_have_inflated_this_call`,
so the behaviour cannot silently regress.

**This is a business decision, not just a technical one.** If you prefer the
original behaviour, one constant in `scoring.py` reverts it. But I'd recommend
against it, and I'd recommend reviewing `weight_applied` across the first ~50
scored conversations to see whether a separate discovery-call rubric is worth
building.

---

## Coaching output for خالد

- **أفضل ما فعله:** معرفة ممتازة بالمنتج — صحّح للعميل أن طرابزون بعيدة جدًا
  وأعاد بناء البرنامج حول إسطنبول مع جولات في سبنجة وبورصة، وهذا وفّر على
  العميل يومًا كاملًا في الطريق.
- **أهم نقطة ضعف:** لم يحدد موعدًا لإرسال العرض. قال إنه سيجهّز عرضًا، ولم
  يقل متى.
- **التوصية:** اختم كل مكالمة بجملة واحدة محددة — *"سأرسل لك العرض على واتساب
  خلال ساعتين"*. هذا يبني التزامًا قابلًا للقياس، ويمنع ضياع العميل في
  الانتظار.

---

---

## The real DeepSeek run — and the three prompt bugs it found

The scores above are mine, applied by hand against the rubric. Running the same
transcript through the actual judge produced something different, and the
difference was the most useful output of the whole exercise.

| Run | M1 | M2 | Final | Verdict |
|---|---|---|---|---|
| Hand-scored (mine) | 90 | 80 | **83.8** | Good |
| DeepSeek, first attempt | 100 | 100 | **100.0** | Excellent |
| DeepSeek, after fixes | 80 | 70 | **73.8** | Good |

### Bug 1 — pass 1 named the agent as the customer

The extraction returned `customer.name: "خالد"`. خالد is the **agent**. On an
unlabelled mono transcript the model latched onto the first name it heard.

Left alone this is severe: it mints a customer record for a person who does not
exist, and identity resolution then merges real customers onto it.

**Fix.** `pass1_customer_v1.md` now tells the model how to tell the two apart —
whoever answers with a company name is the agent, whoever asks for prices is the
customer — and to return `null` rather than guess. It now correctly returns
**أبو عبدالله**.

### Bug 2 — unjustified `null` silently awarded a perfect module

The first run nulled `value_selling` and `alternative_offer`, leaving only
`attitude: 25/25`. Module 2 therefore scored **100** — on a call with no offer.

Nulling a criterion removes it from the denominator, so a permissive null is
indistinguishable from a generous score. This is the easiest way for a lenient
judge to hand out marks nobody earned.

**Fix.** `scoring.NULLABLE_CRITERIA` is an enforced allowlist. `module1.*`,
`attitude` and `value_selling` are always assessable; a null there is a contract
violation and the response is **re-asked once** with the specific problem named.

### Bug 3 — the model contradicted itself

It reported `stage_reached: "offer_presented"` while simultaneously nulling
offer completeness *and* writing in its own notes that no offer was presented.
It also narrated a `weight_applied` of 0.15 while its own module scores implied
0.40.

**Fix.** `validate_stage_consistency()` rejects a stage that claims an offer the
scores say never existed. And the prompt now forbids the model from computing
the final score at all — that arithmetic was always done locally, so its
narration could only ever be noise.

### The over-correction, and what it teaches

My first fix zeroed too hard: the judge dropped `next_step_transition` and
`value_selling` to **0**, reading "no timeframe given" as "no next step" and
"no price quoted" as "no value selling". That produced 59.4, which is as wrong
as 100 in the other direction.

Both criteria now state explicitly that their sub-points score independently,
and that value selling does not require a formal offer. The rubric's own
structure was fine — the prompt had to say out loud what a human reader infers.

### Residual disagreement, and why it is acceptable

DeepSeek now says **73.8 "Good"**; I said **83.8 "Good"**. Same band, ten points
apart, on two defensible judgement calls:

- It deducted 5 on `understanding_confirmation` for an early misread. I did not.
- It gave `value_selling` 10 (features only); I gave 20 (features *and*
  needs-connection, for the Trabzon restructure).

That is normal judge-vs-human variance. What matters is that both land in the
same band and both name the same top weakness — **no timeframe on the quote**.

### One residual inconsistency, unfixed

The model wrote `next_step_transition: 10` in the breakdown while its own
evidence text says *"Promised to send quote on WhatsApp (15 pts) but no
timeframe given (0 pts). Total 15/25."* The field and the rationale disagree by
5 points.

This is a known weakness — LLMs are reliable at judging and unreliable at
transcribing their own judgement into a number. It is worth watching across the
first 50 evaluations; if it recurs, the fix is to have the model emit the
sub-points (`quote_promised`, `timeframe_given`) and sum them locally, exactly
as the module totals are already handled.

---

## Caveats on this evaluation

1. **Speaker attribution is inferred**, not measured — see the top of this file.
2. **One call is not a sample.** Nothing here should change policy until ~50
   conversations have been scored.
3. **Cost.** ~11.5k tokens per conversation across both passes. On DeepSeek that
   is a fraction of a cent; at 1,000 conversations/month it stays negligible.
