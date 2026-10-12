"""The chat scorecard, version 4 — the owner's handoff of 2026-10-11 (Mohamed Amin).

It replaces the 15-item v0.5 sheet (`app/qa/`, kept for the 297 trial grades it
produced) for grading agents. Nine weighted items, a loss reason, a red flag.

    transcript.py   the chat as the model reads it: numbered, masked, with hints
    evidence.py     a quote counts only inside the message it names, by the right side
    scorecard.py    the rules of handoff section 5: one run -> items, three runs -> grade
    claude.py       call 4, the loss reason from Claude Sonnet 5.5
    engine.py       load -> three DeepSeek runs (+ Sonnet) -> grade, nothing written
    texts.py        every Arabic label, shipped to the dashboard as `meta`
    render.py       the graded chat and the rules as HTML fragments

`qa-chat-v4.1` is the owner's v4 prompt plus two wording clarifications that
change no rule (see the note at the end of prompt.txt). Where the code is
stricter than the reference implementation it enforces a definition the
prompt already states, so the same answers can only give one grade:

  * a delivery, an answer or an offer mismatch must come AFTER what it answers;
  * a q36 money/booking error needs both of its sides verified, as defined;
  * a request line that only announces a question ("ابغى استفسر") is not a request;
  * a quote from a bot or system line never counts, for any field;
  * a label the list does not contain is no label;
  * a stated loss reason proven by a verified line outranks "No Response" votes,
    and so does a price/competitor objection in the customer's last message.
"""
