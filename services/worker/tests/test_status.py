"""/status: what is broken in the pipeline, and why.

Each case is a state the pipeline was actually in. The page exists because the
2026-10-05 deals outage ran two nights with nothing saying so; if it ever reads
that state as healthy again, the page is decoration.

Runs with no credentials and no database: `db.one` / `db.rows` are stubbed per
query, so what is under test is the reading of the numbers, not Postgres.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, status
from app.config import settings
from app.main import app

KEY = "test-key-for-status"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

HEALTHY = {
    status.SQL_INGEST: {"last_in": NOW, "age": timedelta(seconds=20), "n24": 2336,
                        "rejected24": 0, "stuck24": 0},
    status.SQL_MEDIA: {"stored": 2507, "queued": 0, "customer_links_at_risk": 0,
                       "last_scan_at": NOW, "scan_age": timedelta(seconds=30), "mode": "on"},
    status.SQL_DEALS: {"last_write": NOW, "age": timedelta(hours=9), "newest_deal": NOW,
                       "with_deal_id": 270, "deal_missing": 6},
    status.SQL_IDENTITY: {"total": 300, "with_phone": 260, "phone_unlinked": 0,
                          "last_customer_write": NOW},
    status.SQL_GATE: {"may_run": True, "reason": None, "balance_usd": 18.7},
    status.SQL_JUDGE: {"pending": 40, "dead24": 0, "evaluated24": 120, "last_eval": NOW,
                       "last_error": None},
    status.SQL_RETENTION: {"overdue": 0, "oldest": None},
}
EMPTY_LISTS = {status.SQL_MEDIA_REJECTED_24H: [], status.SQL_ROSTER: []}


def stub(monkeypatch, **override):
    """Answer each check's SQL from HEALTHY, with named overrides merged in."""
    one = {sql: dict(row) for sql, row in HEALTHY.items()}
    for name, patch in override.items():
        one[getattr(status, name)].update(patch)
    monkeypatch.setattr(db, "one", lambda sql, params=None: one[sql])
    monkeypatch.setattr(db, "rows", lambda sql, params=None: EMPTY_LISTS[sql])


def by_key(result):
    return {c["key"]: c for c in result["checks"]}


def test_a_healthy_pipeline_is_all_ok(monkeypatch):
    stub(monkeypatch)
    r = status.build()
    assert r["overall"] == "ok"
    assert {c["status"] for c in r["checks"]} == {"ok"}


def test_the_october_deals_outage_is_red(monkeypatch):
    """Measured 2026-10-07: 04 ran on time, but 225 of 288 conversations had no
    deal because the capped pull kept old deals. 'It ran' is not 'it worked'."""
    stub(monkeypatch, SQL_DEALS={"with_deal_id": 288, "deal_missing": 225},
         SQL_IDENTITY={"total": 319, "with_phone": 146})
    r = status.build()
    checks = by_key(r)
    assert r["overall"] == "fail"
    assert checks["deals"]["status"] == "fail"
    assert "225 من 288" in checks["deals"]["what"]
    assert checks["identity"]["status"] == "warn", "the missing phones follow from the deals"
    assert r["checks"][0]["key"] == "deals", "worst first"


def test_a_judge_switched_off_by_hand_is_off_not_ok_and_not_fail(monkeypatch):
    """Off for three weeks with 599 waiting. Green would hide it; red would
    cry wolf about a decision somebody made on purpose."""
    stub(monkeypatch, SQL_GATE={"may_run": False, "reason": "disabled in provider_budgets"},
         SQL_JUDGE={"pending": 599, "evaluated24": 0})
    judge = by_key(status.build())["judge"]
    assert judge["status"] == "off"
    assert "enabled = true" in judge["fix"]


def test_an_open_gate_with_nothing_judged_is_a_failure(monkeypatch):
    stub(monkeypatch, SQL_JUDGE={"pending": 599, "evaluated24": 0})
    assert by_key(status.build())["judge"]["status"] == "fail"


def test_messages_received_but_not_stored_fail_even_when_traffic_is_live(monkeypatch):
    stub(monkeypatch, SQL_INGEST={"stuck24": 3})
    assert by_key(status.build())["ingest"]["status"] == "fail"


def test_a_stopped_media_archive_fails(monkeypatch):
    """Customer links die in ~20 minutes; ten without a scan is files lost."""
    stub(monkeypatch, SQL_MEDIA={"scan_age": timedelta(minutes=11)})
    assert by_key(status.build())["media"]["status"] == "fail"


def test_one_broken_check_does_not_hide_the_others(monkeypatch):
    stub(monkeypatch)

    def boom():
        raise RuntimeError('relation "v_media_health" does not exist')

    monkeypatch.setattr(status, "CHECKS", [boom, status.check_ingest])
    r = status.build()
    assert [c["status"] for c in r["checks"]] == ["error", "ok"]
    assert "v_media_health" in r["checks"][0]["why"]


def test_every_check_query_is_read_only():
    sqls = [v for k, v in vars(status).items() if k.startswith("SQL_")]
    assert sqls
    for sql in sqls:
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP)\b", sql, re.I), sql


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    return TestClient(app)


def test_page_is_public_and_data_needs_the_key(client, monkeypatch):
    page = client.get("/status")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/status/data").status_code == 401

    stub(monkeypatch)
    r = client.get("/status/data", headers={"X-API-Key": KEY})
    assert r.status_code == 200
    assert r.json()["overall"] == "ok"
