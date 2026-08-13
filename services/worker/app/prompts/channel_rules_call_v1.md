=============================================================
INPUT TYPE — CALL TRANSCRIPT
=============================================================

The conversation is an automatic transcription of a recorded phone call. It is a
machine's guess at spoken Arabic, not a verbatim record. Grade accordingly.

TRANSCRIPT QUALITY
- Words may be wrong. Where a word is implausible in context (a brand name,
  a number, a city), assume transcription error rather than agent error.
- NEVER penalise the agent for a garbled phrase alone. If a deduction depends on
  a single unclear word, do not make it.
- Numbers are the least reliable part of a telephony transcript. Do not treat a
  transcribed price, date or headcount as certain unless it is repeated or
  confirmed by the other speaker. Record any such doubt in notes.
- The METADATA block gives `asr_confidence`. Below 0.75, lower your certainty
  across every module and say so in notes.
- The transcript may contain the marker `[[ASR_GAP]]`. It marks a place where
  unreliable machine output (transcription artifacts, a stuck decoder) was
  removed before you saw the text. It is NOT speech and nobody said it.
  * Never quote it as evidence.
  * Never draw a conclusion that connects the text before a gap to the text
    after it — the two may be unrelated moments of the call. A question before
    a gap was not necessarily answered by the sentence after it.
  * A gap is line/transcription quality, never agent behaviour. Do not score
    it against the agent; note it if it may have hidden something important.

SPEAKER ATTRIBUTION — READ CAREFULLY
The METADATA block states `diarization`:

- `dual_channel` / `pyannote` / `provider` — speakers are separated mechanically
  and the labels are reliable. Score normally.
- `none` — the recording is a single mixed channel with NO speaker separation.
  You must infer from context who is the agent and who is the customer.
  In this mode:
    * State your inference in notes, including how confident you are.
    * Do NOT apply any ABSOLUTE RULE (anger, ignoring the customer, defeatist
      language) unless the speaker is unambiguous from content. A zero handed
      out on a guessed attribution is worse than a missing score.
    * If a criterion turns on who said a specific line and you cannot tell,
      score that criterion `null` rather than guessing.

WHAT DOES NOT APPLY TO A CALL
- Message counts, "empty follow-up" messages and reply latency do not exist
  inside a call. Take them only from METADATA, never from the transcript.
- A phone call is continuous, so there is no in-conversation follow-up. Module 4
  is judged solely from the FOLLOW-UP HISTORY block. If that block is absent or
  `unavailable`, Module 4 = `null`.

WHAT IS SPECIFIC TO A CALL
- Greeting: an agent who states the company name and their own name at pickup
  satisfies both "introduced himself" and "proper greeting".
- Interruptions, dead air and line drops are line quality, not agent behaviour.
  Never score them against the agent. Do note them if they cut a sale short.
- Social opening (weather, football, family) is normal and expected in Gulf and
  Egyptian sales culture. Do not penalise it as time-wasting, and do not credit
  it as rapport unless the customer engages with it.
- If the call ends by agreeing to continue on another channel ("I'll send it on
  WhatsApp"), that is a next-step transition — and it creates a follow-up
  promise that Module 4 will be judged on later.
