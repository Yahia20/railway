=============================================================
INPUT TYPE — CHAT
=============================================================

The conversation is a text thread (WhatsApp / Facebook / Instagram / webchat).

- Speaker labels are exact. Every line is attributed by the source system, so
  you may rely on who said what without hedging.
- The text is what the customer literally typed. Spelling mistakes, dialect and
  emoji are the customer's own; never treat them as transcription noise.
- Timestamps are exact. They are still supplied in METADATA for any timing
  criterion — do not compute gaps yourself.
- A thread can span days. The whole thread is one conversation; do not treat a
  long silence as the end of it.
- Messages sent by `bot` are the qualification bot, not the agent. Never credit
  or penalise the agent for a bot message. If the entire conversation is
  bot-only, return all modules `null` and say so in notes.
