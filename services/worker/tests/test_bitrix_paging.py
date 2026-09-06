"""Bitrix list paging, and the bug it was written to kill.

Workflow 04 called `crm.deal.list` with `start: 0` and read one response. Bitrix
pages every `*.list` method at 50 rows and will not return more, so 04 imported
**50 of the 676** deals modified in the last week, every night, and logged
success — a short page and a small result set look identical from one response.
The same mistake sat one node later on `crm.contact.list`, where it dropped 450
of 500 contacts and left their conversations without the phone that workflow 03
needs to create a customer at all.

These run with no credentials: the transport is stubbed, so what is under test
is the paging loop, the stop conditions and the retry — not Bitrix.
"""
from __future__ import annotations

import httpx
import pytest

from app.sources.bitrix_chats import BitrixRestSource

PAGE = 50


def source(monkeypatch, pages, calls=None):
    """A BitrixRestSource whose `call` returns canned pages."""
    src = BitrixRestSource("example.bitrix24.ae", "tok", "128")
    seq = iter(pages)

    def fake_call(method, **params):
        if calls is not None:
            calls.append((method, params))
        nxt = next(seq)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(src, "call", fake_call)
    return src


def page(rows, start, total):
    """One Bitrix list response. `next` is absent on the final page — that
    absence is the only end-of-data signal Bitrix gives."""
    body = {"result": [{"ID": str(i)} for i in range(start, start + rows)],
            "total": total}
    if start + rows < total:
        body["next"] = start + rows
    return body


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------

def test_reads_every_page_not_just_the_first(monkeypatch):
    """676 deals in 50-row pages is 14 requests. Reading one gets 50."""
    total = 676
    pages = [page(min(PAGE, total - s), s, total) for s in range(0, total, PAGE)]
    calls: list = []
    src = source(monkeypatch, pages, calls)

    rows, reported, requests = src.list_all("crm.deal.list")

    assert len(rows) == 676, "this is the bug: a single page returns 50"
    assert reported == 676
    assert requests == 14
    # Each request must advance `start`, or the loop re-reads page one forever.
    assert [c[1]["start"] for c in calls] == list(range(0, 700, PAGE))


def test_stops_when_next_is_absent(monkeypatch):
    """Bitrix omits `next` on the last page. Nothing else says "done" — total
    can be stale on a filtered query."""
    src = source(monkeypatch, [page(50, 0, 60), page(10, 50, 60)])
    rows, total, requests = src.list_all("crm.contact.list")
    assert (len(rows), total, requests) == (60, 60, 2)


def test_a_single_short_page_is_one_request(monkeypatch):
    src = source(monkeypatch, [page(7, 0, 7)])
    rows, total, requests = src.list_all("crm.deal.list")
    assert (len(rows), total, requests) == (7, 7, 1)


def test_empty_result_terminates(monkeypatch):
    """A filter matching nothing must not loop on an empty page that still
    carries a `next`."""
    src = source(monkeypatch, [{"result": [], "total": 0, "next": 50}])
    rows, total, requests = src.list_all("crm.deal.list")
    assert (rows, total, requests) == ([], 0, 1)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def test_max_rows_stops_the_loop_and_is_reported(monkeypatch):
    """A filter that accidentally matches the whole CRM (17,651 deals) must
    cost a bounded number of requests. The caller sees the truncation by
    comparing len(rows) with the total."""
    total = 17_651
    pages = [page(PAGE, s, total) for s in range(0, total, PAGE)]
    src = source(monkeypatch, pages)

    rows, reported, requests = src.list_all("crm.deal.list", max_rows=120)

    assert len(rows) == 120
    assert reported == total
    assert requests == 3, "must stop as soon as max_rows is reached"
    assert len(rows) < reported, "truncation has to be visible to the caller"


def test_select_and_filter_are_sent_on_every_page(monkeypatch):
    """Rule 8: the allowlist is what keeps UF_CRM_1781281581 — prose addressed
    to a bot — out of the payload. Dropping `select` after page one would let
    it back in for pages 2..n."""
    calls: list = []
    src = source(monkeypatch, [page(50, 0, 60), page(10, 50, 60)], calls)
    src.list_all("crm.deal.list", select=["ID", "TITLE"],
                 filter={">DATE_MODIFY": "2026-08-30"})

    assert len(calls) == 2
    for _, params in calls:
        assert params["select"] == ["ID", "TITLE"]
        assert params["filter"] == {">DATE_MODIFY": "2026-08-30"}


# ---------------------------------------------------------------------------
# Retry
#
# Workflow 04 runs once, at 03:20. A page that fails on a transient blip takes
# the whole night's pull with it, and a real 502 was seen within a handful of
# manual runs.
# ---------------------------------------------------------------------------

def test_transient_failure_is_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    src = source(monkeypatch, [
        RuntimeError("bitrix crm.deal.list: QUERY_LIMIT_EXCEEDED"),
        page(3, 0, 3),
    ])
    rows, _, requests = src.list_all("crm.deal.list")
    assert len(rows) == 3
    assert requests == 1, "a retried page is still one page"


def test_transport_error_is_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    src = source(monkeypatch, [httpx.ConnectError("reset"), page(2, 0, 2)])
    rows, _, _ = src.list_all("crm.deal.list")
    assert len(rows) == 2


def test_a_permanent_error_is_not_retried(monkeypatch):
    """insufficient_scope will not fix itself in 1.5 seconds. Retrying it four
    times just delays a clear failure by six."""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    attempts: list = []
    src = source(monkeypatch, [
        RuntimeError("bitrix crm.deal.list: insufficient_scope"),
        page(1, 0, 1), page(1, 0, 1), page(1, 0, 1),
    ], attempts)

    with pytest.raises(RuntimeError, match="insufficient_scope"):
        src.list_all("crm.deal.list")
    assert len(attempts) == 1


def test_retries_are_finite(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    err = RuntimeError("bitrix crm.deal.list: QUERY_LIMIT_EXCEEDED")
    src = source(monkeypatch, [err] * 10)
    with pytest.raises(RuntimeError):
        src.list_all("crm.deal.list")


# ---------------------------------------------------------------------------
# The endpoints, and the workflow that calls them
# ---------------------------------------------------------------------------

def test_endpoints_require_the_api_key(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    # monkeypatch, not assignment: `settings` is a module-level singleton, and
    # assigning to it here leaked a fake key into every later test in the
    # session — twelve unrelated failures in test_chats_prepare.
    monkeypatch.setattr(settings, "worker_api_key", "k", raising=False)
    c = TestClient(app)
    assert c.post("/bitrix/deals", json={}).status_code == 401
    assert c.post("/bitrix/contacts", json={}).status_code == 401


def test_no_contact_ids_makes_no_bitrix_call(monkeypatch):
    """An empty id list is the normal case on a quiet night. It must not become
    an unfiltered request for all 23,754 contacts."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "worker_api_key", "k", raising=False)
    c = TestClient(app)
    r = c.post("/bitrix/contacts", headers={"X-API-Key": "k"},
               json={"contact_ids": []})
    assert r.status_code == 200
    assert r.json() == {"result": [], "total": 0, "fetched": 0,
                        "truncated": False, "requests": 0}


def test_workflow_04_no_longer_calls_bitrix_directly():
    """The paging fix is only real if the workflow stops bypassing it."""
    import json
    from pathlib import Path

    wf = json.loads((Path(__file__).resolve().parents[3] / "n8n" / "workflows"
                     / "04-nightly-housekeeping.json").read_text(encoding="utf-8"))
    urls = [n["parameters"].get("url", "") for n in wf["nodes"]
            if n["type"].endswith("httpRequest")]

    assert not [u for u in urls if "bitrix24" in u or "crm." in u], (
        "workflow 04 calls Bitrix directly again; paging lives in the worker")
    assert any(u.endswith("/bitrix/deals") for u in urls)
    assert any(u.endswith("/bitrix/contacts") for u in urls)

    for n in wf["nodes"]:
        if n["type"].endswith("httpRequest") and "/bitrix/" in n["parameters"].get("url", ""):
            hdrs = n["parameters"]["headerParameters"]["parameters"]
            assert any(h["name"] == "X-API-Key" for h in hdrs), \
                f"{n['name']} does not send the worker API key"
