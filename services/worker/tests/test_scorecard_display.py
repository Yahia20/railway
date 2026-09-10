"""A score is always shown, and never shown naked.

The owner decided — with the statistical caveats explained and understood —
that a visible provisional number labelled honestly beats a blank cell. These
tests guard the two halves of that decision, which pull in opposite directions:

  * the number must never be suppressed again, AND
  * it must never appear without its sample size and the interim-method
    caveat.

They also guard the way it was implemented, because the tempting shortcuts
(min_n_publish = 0, deleting band_stable, inserting a fabricated
eval_noise_params row) all produce a scorecard that looks identical to this one
and quietly destroy the machinery that will answer "is this trustworthy" once
the A/A re-measurement is finally taken.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "db" / "migrations" / "022_scorecard_display_layer.sql"
REPORT_PY = REPO / "services" / "worker" / "app" / "report.py"
REPORT_HTML = REPO / "services" / "worker" / "app" / "static" / "report.html"
AUDIT = REPO / "scripts" / "audit_data_integrity.py"


def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def sql_body() -> str:
    """The migration without its commentary.

    This file explains at length what it deliberately does NOT do, naming the
    shortcuts it avoids. Those sentences must not read as the shortcuts being
    present.
    """
    return "\n".join(l for l in sql().splitlines()
                     if not l.strip().startswith("--"))


# ---------------------------------------------------------------------------
# The machinery underneath is untouched
# ---------------------------------------------------------------------------

def test_migration_does_not_weaken_the_publication_machinery():
    body = sql_body()
    assert "min_n_publish" not in body or "UPDATE eval_report_params" not in body, (
        "min_n_publish must stay 30 — it becomes the threshold that picks a "
        "LABEL, not a gate that hides the number")
    assert "UPDATE eval_report_params" not in body
    assert "DELETE FROM eval_report_params" not in body
    assert "INSERT INTO eval_noise_params" not in body, (
        "inserting a noise row would fabricate the measurement that decides "
        "what is publishable — the one thing that must never be invented")
    assert "UPDATE eval_noise_params" not in body
    assert "DROP VIEW" not in body.upper(), (
        "the base views are wrapped, not replaced: a rewrite is a 300-line "
        "copy that drifts, and it discards 014's grants and ownership")


def test_migration_does_not_edit_014():
    """014 is the honest machinery. It must be readable as originally written."""
    m014 = (REPO / "db" / "migrations" / "014_evaluation_status.sql").read_text(
        encoding="utf-8")
    assert "band_stable" in m014, "014 still defines band_stable"
    assert "v_agent_scorecard_display" not in m014, (
        "the display layer must live in 022, not be back-ported into 014")


def test_migration_declares_lock_timeout():
    assert "lock_timeout" in sql_body(), (
        "every migration here fails fast rather than fighting live ingestion")


def test_band_stable_is_an_input_not_a_gate():
    """It must be READ by the label and never redefined or dropped."""
    body = sql_body()
    assert "band_stable" in body, "the label must consume band_stable"
    assert "AS band_stable" not in body, (
        "022 must not redefine band_stable — it reads the base view's value")


# ---------------------------------------------------------------------------
# The number is always there, and never alone
# ---------------------------------------------------------------------------

def test_display_view_packages_value_with_its_caveats():
    body = sql_body()
    for key in ("'value'", "'n_usable'", "'label'", "'is_provisional'",
                "'method'", "'headline'"):
        assert key in body, f"score_display must carry {key}"


def test_headline_string_carries_the_label():
    """A caller that renders only `headline` still cannot lose the caveat."""
    body = sql_body()
    head = body.split("'headline'", 1)[1].split(") AS score_display", 1)[0]
    assert "eval_confidence_label" in head, (
        "the one-string form must include the label, or rendering it alone "
        "shows a naked number")


def test_label_covers_all_four_states():
    body = sql_body()
    for state in ("no evaluations yet", "provisional", "published"):
        assert state in body, f"missing label state: {state}"
    assert "margin of error not yet measured" in body, (
        "n above threshold but unmeasured noise is its own state — it is not "
        "the same as 'too few evaluations'")


def test_method_label_says_the_method_will_change():
    body = sql_body()
    assert "will change" in body, (
        "the owner must be able to see that today's scores are not comparable "
        "with tomorrow's method")


# ---------------------------------------------------------------------------
# The report cannot show the number without the label
# ---------------------------------------------------------------------------

def test_report_reads_the_display_views():
    src = REPORT_PY.read_text(encoding="utf-8")
    assert "v_agent_scorecard_display" in src
    assert "v_quality_by_input_display" in src
    assert not re.search(r"FROM v_agent_scorecard\s*$", src, re.M), (
        "the human-facing panel must read the display view, not the base one")


def test_report_payload_has_no_bare_mean():
    """A sibling key can be dropped in a renderer; an object member cannot.

    This is the assertion that makes the label impossible to lose by accident.
    """
    src = REPORT_PY.read_text(encoding="utf-8")
    scorecard = src.split("SQL_SCORECARD = ", 1)[1].split('"""', 2)[1]
    assert "score_display" in scorecard
    assert not re.search(r"\bavg_score\b", scorecard), (
        "avg_score must NOT be a bare payload key — the only way to the "
        "headline number is through score_display, which carries the label")
    assert "n_usable" in scorecard, (
        "n_usable is the mean's real denominator; evaluated_interactions is not")
    assert "method_label" in scorecard, (
        "two rows for one agent are two scoring methods, not a duplicate")


def test_quality_payload_has_no_bare_mean():
    src = REPORT_PY.read_text(encoding="utf-8")
    q = src.split("SQL_QUALITY_BY_INPUT = ", 1)[1].split('"""', 2)[1]
    assert "score_display" in q
    assert not re.search(r"\bavg_score\b", q)


def test_report_page_renders_the_object_not_a_bare_number():
    html = REPORT_HTML.read_text(encoding="utf-8")
    assert "score_display" in html
    assert "r.avg_score" not in html, (
        "the page must not reach for a bare avg_score — it is no longer sent, "
        "so this would render an empty cell and lose the score entirely")
    assert "method_label" in html, "the method column keeps duplicate rows explicable"


# ---------------------------------------------------------------------------
# A provisional score may be displayed. It must not be acted on.
# ---------------------------------------------------------------------------

def test_no_n8n_workflow_consumes_a_mean():
    """The main risk of this change: a number that was suppressed is now
    visible, and something automated inherits it.

    Verified in the database too (pg_depend shows nothing depends on either
    view, and evaluate_alert_rules reads no agent mean). This guards the other
    half: that no workflow starts reading one later.
    """
    offenders = []
    for path in sorted((REPO / "n8n" / "workflows").glob("*.json")):
        wf = json.loads(path.read_text(encoding="utf-8"))
        for node in wf["nodes"]:
            q = (node.get("parameters") or {}).get("query", "")
            body = "\n".join(l for l in q.splitlines()
                             if not l.strip().startswith("--"))
            for needle in ("v_agent_scorecard", "v_quality_by_input",
                           "avg_score", "score_display"):
                if needle in body:
                    offenders.append(f"{path.name}::{node['name']} reads {needle}")
    assert offenders == [], (
        "a workflow now consumes an agent mean. A provisional score must not "
        "drive anything automated — gate it on is_provisional = false "
        "explicitly, and say so in the handover. Offenders: " + str(offenders))


def test_is_provisional_exists_for_consumers_to_gate_on():
    body = sql_body()
    assert "eval_is_provisional" in body
    assert "is_provisional" in REPORT_PY.read_text(encoding="utf-8"), (
        "the payload must carry the flag, so any future consumer has "
        "something to refuse on without re-deriving the rule")


# ---------------------------------------------------------------------------
# The audit enforces this against live data, not just against the source
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("check_id", [
    "mean-without-sample-size",
    "mean-without-label",
    "mean-suppressed",
    "version-coordinate-collapsed",
    "published-without-measured-noise",
])
def test_audit_asserts_the_display_rule(check_id):
    assert check_id in AUDIT.read_text(encoding="utf-8"), (
        f"audit_data_integrity.py must check {check_id} against the live "
        f"database — a view can be replaced without touching this repo")
