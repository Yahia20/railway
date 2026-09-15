"""The report describes chats, and there is nothing else left to describe.

Calls were removed from the pipeline on 2026-09-14 — the lane, its tables, its
830 transcripts and its 1,064 terminal jobs. It was never the volume that made
them unusable: all 1,119 recordings decoded to extension 3009, a QUEUE, so no
call ever carried an `agent_id` and none ever could. For a month, 834
evaluations of which roughly 800 were unattributable sat under a headline about
agent performance.

These tests do NOT guard a feature flag. They guard the shape of the queries, so
that the day a second channel arrives the predicate is already there. The
version of this file that had no predicate at all is how 800 calls got counted
as agent performance in the first place.
"""
from __future__ import annotations

import re

import pytest

from app import report

SCORING_SQL = ("SQL_NULL_VS_ZERO", "SQL_CONVERSATIONS", "SQL_QUALITY_BY_INPUT")


@pytest.mark.parametrize("name", SCORING_SQL)
def test_every_scoring_panel_names_the_channel_it_counts(name):
    """`null_vs_zero` is the one that matters most: it is the panel that proves
    rule 2 is being honoured, and it reads `agent_evaluations` directly. An
    unfiltered version would describe whatever population happens to be in that
    table, under a page that says chats."""
    assert "input_type = 'chat'" in getattr(report, name)


def test_the_channel_predicate_is_a_literal_not_a_parameter():
    """A caller must not be able to widen it. `days` and `limit` come from the
    query string; the channel does not, and a `%(channel)s` here would put the
    report's scope in the hands of whoever types the URL."""
    assert "%(channel)s" not in report.SQL_CONVERSATIONS
    assert "%(input_type)s" not in report.SQL_CONVERSATIONS


def test_the_payload_declares_its_channels(monkeypatch):
    """A reader who assumes one channel and is shown another reads every count
    wrong. The page prints this, and a second entry is the signal that every
    'chats' label on it needs rewriting."""
    monkeypatch.setattr(report, "_panel", lambda *a, **k: None)
    assert report.build()["channels"] == ["chat"]


def test_no_panel_reads_a_call_table(monkeypatch):
    """The tables are dropped, so a surviving query would not fail quietly — it
    would take its whole panel down with `relation does not exist` and report an
    error next to a page that otherwise works. Better to never ask."""
    asked: list[str] = []
    monkeypatch.setattr(report, "_panel",
                        lambda name, fn, into, errors: asked.append(name))
    report.build()
    for gone in ("call_jobs", "stranded_calls", "asr_runs"):
        assert gone not in asked

    # The SQL only, not the prose: the module comment explains WHY the tables
    # are gone and naming them there is the point of it.
    text = open(report.__file__, encoding="utf-8").read()
    sql = " ".join(re.findall(r'"""(.*?)"""', text, re.S))
    for table in ("call_ingest_jobs", "transcripts", "asr_runs", "asr_confidence"):
        assert table not in sql, f"report.py still queries {table}"


def test_the_switch_is_gone_not_merely_defaulted_off():
    """`CALLS_ENABLED` existed for one day, between shelving calls and deleting
    them. A flag pointing at dropped tables is worse than no flag: it reads as a
    supported way back, and turning it on now produces errors, not calls."""
    assert not hasattr(report, "CALLS_ENABLED")
