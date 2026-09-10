"""The per-conversation drill-down panel.

Every other panel on /report is an aggregate. This one is the only place a
person can open a single conversation and read the verdict on it, which makes
it the only place two specific mistakes are visible:

  * a null module rendered as 0 — the failure the whole rubric was rebuilt to
    remove, and the one that makes a good agent look bad;
  * 50 conversations of 504 presented as though they were all of them.

It also carries real customer PII, so the auth and no-embedding rules that
already cover /report/data have to keep covering it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import db, report

REPO = Path(__file__).resolve().parents[3]
PAGE = REPO / "services" / "worker" / "app" / "static" / "report.html"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def renderer() -> str:
    """Just the drill-down code, so an assertion cannot pass on some other
    panel's markup elsewhere in the file."""
    return page().split("/* ---------- drill-down", 1)[1]


# ---------------------------------------------------------------------------
# Rule 2: null is not zero
# ---------------------------------------------------------------------------

def test_a_null_module_is_not_rendered_as_a_number():
    """A module scores null when the situation never arose and 0 when it arose
    and was handled badly.

    Live data has both right now: 50 of the 50 returned rows have a null
    m4_followup, and 5 have a genuine 0 on m3_objections. If the renderer
    coalesced null to 0, every one of those 50 would read as an agent who
    handled follow-up terribly, on conversations where follow-up never came up.
    """
    src = renderer()
    assert "function modCell" in src, "the module cell must be its own function"
    cell = src.split("function modCell", 1)[1].split("\nfunction ", 1)[0]

    assert "isNull" in cell, "the null case must be tested explicitly"
    assert "لم تنشأ" in cell, "a null module must say the situation never arose"
    assert '"—"' in cell or "'—'" in cell, "a null module must show an em-dash"

    # The specific bug: any of these turns null into 0 before it is rendered.
    for coercion in ("|| 0", "?? 0", "Number(v) || ", "parseFloat"):
        assert coercion not in cell, (
            f"{coercion!r} in modCell would render a null module as zero")


def test_zero_is_rendered_as_zero_and_not_muted_away():
    """The other half of the rule. A 0 is a real, bad score and must read as
    one — muting it or blanking it hides a genuine failure."""
    cell = renderer().split("function modCell", 1)[1].split("\nfunction ", 1)[0]
    # `zero` (the muted style) may only be applied on the null branch.
    m = re.search(r'isNull \?\s*" zero"\s*:\s*""', cell)
    assert m, ("the muted 'zero' class must be applied ONLY when the value is "
               "null; applying it to an actual 0 hides a real bad score")


def test_weight_applied_is_shown_beside_the_modules():
    """final_score is computed over weight_applied, not over all five modules.
    Without it a reader assumes every module was in play."""
    assert "weight_applied" in renderer(), (
        "the denominator the score was computed over must be visible")


# ---------------------------------------------------------------------------
# The cap is visible, not silent
# ---------------------------------------------------------------------------

def test_panel_reports_the_total_and_whether_it_truncated(monkeypatch):
    fake = [{"external_id": f"c{i}", "total_in_window": 504} for i in range(50)]
    monkeypatch.setattr(db, "rows", lambda sql, p=None: fake)

    out = report._conversations({"days": 30, "limit": 50})
    assert out["total_in_window"] == 504
    assert out["limit"] == 50
    assert out["truncated"] is True
    assert len(out["rows"]) == 50
    assert all("total_in_window" not in r for r in out["rows"]), (
        "the window total must not be repeated on every row")


def test_not_truncated_when_everything_fits(monkeypatch):
    fake = [{"external_id": "c1", "total_in_window": 1}]
    monkeypatch.setattr(db, "rows", lambda sql, p=None: fake)
    assert report._conversations({"days": 30, "limit": 50})["truncated"] is False


def test_empty_window_is_not_an_error(monkeypatch):
    monkeypatch.setattr(db, "rows", lambda sql, p=None: [])
    out = report._conversations({"days": 30, "limit": 50})
    assert out == {"rows": [], "total_in_window": 0, "limit": 50,
                   "truncated": False}


def test_page_says_out_loud_when_the_list_is_capped():
    src = renderer()
    assert "c.truncated" in src, "the page must react to the truncated flag"
    assert "total_in_window" in src, (
        "the page must show how many conversations the cap hid — a capped list "
        "that does not say so reads as the complete set")


def test_sql_respects_both_days_and_limit():
    sql = report.SQL_CONVERSATIONS
    assert "%(days)s" in sql, "the panel must honour the window parameter"
    assert "%(limit)s" in sql, "an uncapped drill-down can return every row"
    assert "LIMIT" in sql.upper()


# ---------------------------------------------------------------------------
# It fails like every other panel: named, not fatal
# ---------------------------------------------------------------------------

def test_a_broken_panel_lands_in_errors_and_leaves_the_rest(monkeypatch):
    """A drill-down that throws must not blank a page whose other 20 panels
    are fine — that is the difference between one broken query and a dead
    pipeline."""
    def flaky(sql, params=None):
        if "total_in_window" in sql:
            raise RuntimeError("boom")
        return [{"ok": True}]

    monkeypatch.setattr(report.db, "rows", flaky)
    monkeypatch.setattr(report.db, "one", lambda sql, params=None: {"ok": True})

    out = report.build(days=30, limit=5)
    assert "conversations" in out["errors"], (
        "a failing panel must name itself in errors")
    assert "boom" in out["errors"]["conversations"]
    assert out["data"]["conversations"] is None
    assert len(out["data"]) > 1, "the other panels must still be present"


def test_panel_is_registered_in_build():
    src = (REPO / "services" / "worker" / "app" / "report.py").read_text(encoding="utf-8")
    assert '_panel("conversations"' in src, (
        "it must go through _panel, or a failure takes the whole report down")


def test_panel_reads_through_the_read_only_pool():
    """Rule 11. `test_asr_jobs_is_the_only_module_that_writes` enforces this
    across the worker; this pins it on the new code specifically."""
    src = (REPO / "services" / "worker" / "app" / "report.py").read_text(encoding="utf-8")
    fn = src.split("def _conversations", 1)[1].split("\nSQL_", 1)[0]
    assert "db.rows(" in fn
    for forbidden in ("db.write", "db.writer", "db.get_write_pool"):
        assert forbidden not in fn, f"{forbidden} is not allowed here"


# ---------------------------------------------------------------------------
# It must not look like it was bolted on
# ---------------------------------------------------------------------------

def test_the_stylesheet_was_not_touched():
    """The panel had to reuse what is already there. A new colour, radius or
    shadow would make it obvious which panel arrived later."""
    import subprocess
    head = subprocess.run(
        ["git", "show", "HEAD:services/worker/app/static/report.html"],
        capture_output=True, text=True, encoding="utf-8", cwd=REPO).stdout
    if not head:
        pytest.skip("no git object for the page")
    now_style = page().split("<style>", 1)[1].split("</style>", 1)[0]
    head_style = head.split("<style>", 1)[1].split("</style>", 1)[0]
    assert now_style == head_style, (
        "the <style> block changed — this panel must add no CSS at all")


def test_renderer_uses_only_classes_that_already_exist():
    style = page().split("<style>", 1)[1].split("</style>", 1)[0]
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", style))
    used = set()
    for m in re.finditer(r'class="([a-z ]+)"', renderer()):
        used.update(m.group(1).split())
    unknown = sorted(u for u in used if u not in defined)
    assert unknown == [], f"new CSS classes with no rule behind them: {unknown}"


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------

def test_no_conversation_data_is_baked_into_the_shell():
    """The page holds no data and fetches it with the key. This panel exposes
    Arabic summaries of real conversations, so that has to keep being true."""
    html = page()
    shell = html.split("<script>", 1)[0]
    for leak in ("summary_ar", "top_strength", "external_deal_id"):
        assert leak not in shell, f"{leak} appears in the HTML shell"


def test_summary_and_free_text_are_escaped():
    """Arabic summaries are customer words echoed into the DOM."""
    src = renderer()
    detail = src.split("function convDetail", 1)[1].split("\nfunction ", 1)[0]
    for field in ("summary_ar", "top_strength", "top_weakness",
                  "top_recommendation"):
        assert field in detail
    assert "innerHTML" not in detail, (
        "convDetail builds a string; it must not assign unescaped innerHTML")
    assert detail.count("esc(") >= 4, "free text must go through esc()"
