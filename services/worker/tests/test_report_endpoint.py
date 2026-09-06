"""The reporting endpoint: auth, read-only discipline, and partial failure.

Every assertion here is a way this endpoint could quietly become the wrong kind
of thing:

  * the page leaking customer data to anyone who knows the URL, because the
    obvious way to authenticate a browser navigation is a query-string key;
  * a report query that writes, because SELECT and UPDATE are one typo apart
    and this is the only code in the worker that talks to the database at all;
  * one broken panel returning 500 for the whole report, so a database a
    migration behind looks like a dead pipeline;
  * `days` taken from the query string straight into an interval.

Runs with no credentials and no database.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import db, report
from app.config import settings
from app.main import app

KEY = "test-key-for-report"


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_page_needs_no_key_and_carries_no_data(client):
    """The shell is public on purpose — it holds nothing worth protecting."""
    res = client.get("/report")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-frame-options"] == "DENY"


def test_page_ships_no_embedded_rows(client):
    """If a future change ever bakes data into the shell, this fails. The page
    must fetch through the authenticated endpoint, never carry results."""
    html = client.get("/report").text
    # The panel containers are empty in the source; the only thing that fills
    # them is the fetch. Any literal row payload would have to appear here.
    assert "interaction_id" not in html
    assert '"data"' not in html


def test_data_requires_the_key(client):
    assert client.get("/report/data").status_code == 401
    assert client.get("/report/data", headers={"X-API-Key": "wrong"}).status_code == 401


def test_data_does_not_accept_a_query_string_key(client):
    """A key in the URL lands in browser history, proxy logs and Referer
    headers. The endpoint must not honour one."""
    res = client.get(f"/report/data?key={KEY}&api_key={KEY}")
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Read-only discipline
# ---------------------------------------------------------------------------

WRITES = re.compile(
    r"\b(insert\s+into|update\s+\w|delete\s+from|drop\s|truncate\s|alter\s+table|"
    r"create\s+(table|view|index)|grant\s|copy\s)\b", re.I)


def _sql_constants() -> dict[str, str]:
    return {name: value for name, value in vars(report).items()
            if name.startswith("SQL_") and isinstance(value, str)}


def test_there_are_sql_constants_to_check():
    """Guards the two tests below from passing vacuously if the constants are
    ever renamed."""
    assert len(_sql_constants()) >= 10


@pytest.mark.parametrize("name", sorted(_sql_constants()))
def test_every_report_query_is_read_only(name):
    sql = _sql_constants()[name]
    assert not WRITES.search(sql), f"{name} is not a pure SELECT"
    assert sql.lstrip().upper().startswith("SELECT"), f"{name} does not start with SELECT"


@pytest.mark.parametrize("name", sorted(_sql_constants()))
def test_no_query_interpolates_its_parameters(name):
    """psycopg placeholders only. An f-string or % formatting in these
    constants would be an injection point reachable from the query string."""
    sql = _sql_constants()[name]
    assert "{" not in sql and "}" not in sql, f"{name} looks like an f-string"
    for token in re.findall(r"%\(?\w*\)?[a-z]?", sql):
        assert re.fullmatch(r"%\((days|limit)\)s", token), f"{name}: unexpected {token!r}"


def test_pool_configuration_is_read_only():
    """The guarantee is enforced on the connection, not just by convention in
    the SQL above."""
    source = (db.__file__ and open(db.__file__, encoding="utf-8").read()) or ""
    assert "default_transaction_read_only = on" in source
    assert "statement_timeout" in source


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------

def test_no_database_is_503_not_500(client, monkeypatch):
    """A worker with no DATABASE_URL must say so, not crash. This is how the
    tests and any local run see it."""
    monkeypatch.setattr(settings, "database_url", None, raising=False)
    monkeypatch.setattr(db, "_pool", None, raising=False)
    res = client.get("/report/data", headers={"X-API-Key": KEY})
    assert res.status_code == 503
    assert "DATABASE_URL" in res.text


def test_one_broken_panel_does_not_break_the_report(monkeypatch):
    """A database one migration behind is missing `interaction_requests`. The
    panels that do work must still answer, and the one that does not must be
    named rather than silently empty."""
    calls = {"n": 0}

    def flaky(sql, params=None):
        calls["n"] += 1
        if "interaction_requests" in sql:
            raise RuntimeError('relation "interaction_requests" does not exist')
        return [{"ok": True}]

    monkeypatch.setattr(report.db, "rows", flaky)
    monkeypatch.setattr(report.db, "one", lambda sql, params=None: {"ok": True})

    out = report.build(days=7, limit=5)
    assert out["errors"], "a failing panel must be reported"
    assert "unlogged_requests" in out["errors"]
    assert out["data"]["unlogged_requests"] is None
    # ...and the rest still answered.
    assert out["data"]["verdicts"] == [{"ok": True}]
    assert out["data"]["chat_jobs"] == [{"ok": True}]


def test_healthy_report_has_an_empty_errors_object(monkeypatch):
    """The page renders `errors`, so it must be present and empty on success —
    a missing key and an empty one would render the same and mean different
    things."""
    monkeypatch.setattr(report.db, "rows", lambda sql, params=None: [])
    monkeypatch.setattr(report.db, "one", lambda sql, params=None: {})
    out = report.build()
    assert out["errors"] == {}
    assert "generated_at" in out


# ---------------------------------------------------------------------------
# Input bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given,expected_days", [
    (0, 1), (-5, 1), (1, 1), (30, 30), (365, 365), (99999, 365)])
def test_days_is_clamped(client, monkeypatch, given, expected_days):
    seen = {}

    def fake_build(days, limit):
        seen["days"], seen["limit"] = days, limit
        return {"data": {}, "errors": {}}

    monkeypatch.setattr(report, "build", lambda days, limit: fake_build(days, limit))
    client.get(f"/report/data?days={given}", headers={"X-API-Key": KEY})
    assert seen["days"] == expected_days


@pytest.mark.parametrize("given,expected_limit", [(0, 1), (50, 50), (10_000, 500)])
def test_limit_is_clamped(client, monkeypatch, given, expected_limit):
    seen = {}
    monkeypatch.setattr(report, "build",
                        lambda days, limit: (seen.update(limit=limit), {"data": {}, "errors": {}})[1])
    client.get(f"/report/data?limit={given}", headers={"X-API-Key": KEY})
    assert seen["limit"] == expected_limit


def test_non_numeric_days_is_rejected_not_coerced(client):
    """FastAPI types the parameter, so this is a 422 and never reaches SQL."""
    res = client.get("/report/data?days=1;DROP", headers={"X-API-Key": KEY})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# The pool's configure contract
#
# This section exists because of a production incident. `configure` ran two
# plain SETs, each of which opens an implicit transaction, so every connection
# went back to the pool INTRANS. psycopg_pool discards such a connection and
# reconnects, forever — /report/data hung for the full pool timeout on every
# request while the log filled with
#
#     connection left in status INTRANS by configure function: discarded
#
# Nothing caught it before deploy: the pool is lazy, so no test and no import
# ever reached this code path. These tests reach it without a database.
# ---------------------------------------------------------------------------

class FakeConn:
    """Records what configure() does, in order."""

    def __init__(self) -> None:
        self.autocommit = False
        self.statements: list[str] = []
        self.autocommit_set_after: int | None = None

    def __setattr__(self, name, value):
        if name == "autocommit" and getattr(self, "statements", None) is not None:
            object.__setattr__(self, "autocommit_set_after", len(self.statements))
        object.__setattr__(self, name, value)

    def execute(self, sql: str):
        self.statements.append(sql)
        return self


def _capture_configure(monkeypatch):
    """Build the pool against a stubbed ConnectionPool and return the real
    `configure` callable the module passed in."""
    captured: dict = {}

    class StubPool:
        def __init__(self, **kw):
            captured.update(kw)

        def open(self):
            pass

    import psycopg_pool
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", StubPool)
    monkeypatch.setattr(settings, "database_url", "postgresql://stub/db", raising=False)
    monkeypatch.setattr(db, "_pool", None, raising=False)
    db.get_pool()
    return captured


def test_configure_sets_autocommit_before_any_statement(monkeypatch):
    """The whole incident in one assertion: autocommit must be on BEFORE the
    SETs, or each SET leaves the connection in a transaction."""
    captured = _capture_configure(monkeypatch)
    conn = FakeConn()
    captured["configure"](conn)

    assert conn.autocommit is True, "configure must put the connection in autocommit"
    assert conn.autocommit_set_after == 0, (
        "autocommit was set after %d statement(s); it must come first, or those "
        "statements open a transaction the pool then discards"
        % conn.autocommit_set_after)


def test_configure_applies_both_session_settings(monkeypatch):
    captured = _capture_configure(monkeypatch)
    conn = FakeConn()
    captured["configure"](conn)

    joined = " ".join(conn.statements).lower()
    assert "default_transaction_read_only = on" in joined
    assert "statement_timeout" in joined


def test_pool_waits_a_bounded_time_for_a_connection(monkeypatch):
    """The report runs sixteen queries. At psycopg's 30-second default, a pool
    that cannot connect holds one request for eight minutes."""
    captured = _capture_configure(monkeypatch)
    assert captured["timeout"] <= 15, "pool timeout must be short enough to fail fast"
    assert captured["open"] is False, "the pool must not connect at import time"
