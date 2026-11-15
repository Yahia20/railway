# Proposed call observations to 0–5 scale

Approved by the owner on 2026-11-14 in the implementation conversation;
scoring mode remains off until validation. Source weights are the
16-item Call QA sheet. Chat v6 is unchanged.

Each item has five equally weighted observable checks in
`services/worker/app/call_qa/rubric.json`. A check is true, false, not applicable,
or unobservable. A positive quote must occur verbatim in the indicated speaker's
specific transcript segment; customer evidence cannot establish agent behavior.
Failure to observe an expected act after reviewing the complete relevant context
may support false; failed transcription or invalid evidence cannot.

Three independent readings vote on each check. At least two valid agreeing
readings are required; disagreement without a majority requires review.

| Passed out of five applicable checks | Score |
|---|---:|
| 0 | 0 |
| 1 | 1 |
| 2 | 2 |
| 3 | 3 |
| 4 | 4 |
| 5 | 5 |

When fewer checks apply, score = floor(5 * passed / applicable + 0.5).
An entirely inapplicable item is null and its weight is excluded from the total.
An unobservable check makes the item pending review, not a failed check or a
silently omitted check. Weighted total = 100 * sum(weight * score / 5) /
sum(applicable scored weights); show assessed weight and pending items beside it.
No score is published with zero assessed weight.

Applicability is justified by observed context. Name use is inapplicable if no
reliable name is available. Objections are inapplicable when none arose. Closing
may be inapplicable when a disconnection prevented it. Accuracy and company
procedures require an approved reference; without one they require review.
Do not infer tone/prosody or factual correctness from an ASR transcript alone.

Timings come from raw audio segmentation/timestamps, never from a model. Gap-filled
playback masks are not a VAD clock. Mono overlap that cannot be separated reliably
is flagged for review; do not impose an automatic interruption deduction.

Critical errors are separate review flags: misinformation, unauthorized promise,
financial/ticketing error, privacy breach or serious procedure violation. Require
verified evidence, and an authoritative reference where relevant. A flag does
not automatically zero a call.
