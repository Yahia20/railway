"""The QA scorecard: the 15-item chat check from the company's QA sheet.

Approved 2026-10-02 on 50 chats (artifact "تقييم جودة الموظفين") and trialled
on the 297 chats of the week 2026-09-27 → 10-04. The modules here are that
run's code moved in unchanged — `common`, `rules`, `rules_v1`, `score`,
`score_v02` … `score_v05`, `evidence`, `texts`, `texts_en` — with only their
imports made relative. Each `score_vNN` builds on the one before it, so the
version numbers are the history of the rubric, not dead files: v0.5 calls v0.4
calls v0.3 calls v0.2.

The model answers only the questions that need reading (`prompt_v05.txt`), by
COPYING sentences. Everything with a timestamp or a word list — reply speed,
greeting, closing, reassurance, follow-up — is decided here in code, and every
quote the model returns is checked against the chat before it counts.

`engine.py` is the only new code: load one chat from the database, ask three
times, take the per-question majority, score.
"""
