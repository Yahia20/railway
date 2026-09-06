"""The ASR batch's database side, now that it runs in the worker.

Modal used to open its own psycopg connection. It runs outside Railway, and
`postgres.railway.internal` is Railway's PRIVATE network, so the connection
never resolved: `[Errno -2] Name or service not known` on the first call. The
two ways out were exposing the database to the internet — which this project
forbids — or having Modal talk to the worker like everything else does.

What these tests protect is the part of that move that could go wrong silently:

  * the SQL must be the SAME SQL, not a tidier rewrite. It carries the lease
    fence that stops two systems paying to transcribe one call.
  * the claim predicate is one half of gotcha 13's boundary. Widen it and the
    same recording is transcribed twice.
  * the worker mints the lease token, not the caller.
  * a rejected lease fence is not a success.

No credentials and no database: the write layer is stubbed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import asr_jobs, db
from app.config import settings
from app.main import app

KEY = "asr-test-key"
MODAL_JOB = Path(__file__).resolve().parents[3] / "modal" / "transcribe_job.py"


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    return TestClient(app)


@pytest.fixture()
def calls(monkeypatch) -> list:
    """Capture every statement the endpoints would run."""
    seen: list = []

    def fake_write(sql, params=None):
        seen.append((sql, params))
        if "RETURNING" in sql.upper():
            return [{"uniqueid": "1755000000.1", "status": "transcribed"}]
        return []

    monkeypatch.setattr(db, "write", fake_write)
    return seen


H = {"X-API-Key": KEY}


# ---------------------------------------------------------------------------
# The SQL moved; it was not rewritten
# ---------------------------------------------------------------------------

def _sql_block(text: str, name: str) -> str | None:
    m = re.search(rf'^{name} = """(.*?)"""', text, re.S | re.M)
    return m.group(1) if m else None


@pytest.mark.parametrize("name", ["CLAIM_SQL", "STORE_SQL", "FAIL_SQL"])
def test_modal_no_longer_carries_its_own_copy_of_the_sql(name):
    """Two copies of a lease fence is two sets of rules to keep in step."""
    text = MODAL_JOB.read_text(encoding="utf-8")
    assert _sql_block(text, name) is None, (
        f"{name} is back in modal/transcribe_job.py; it belongs in "
        f"app/asr_jobs.py only")


def test_modal_opens_no_database_connection():
    """The whole point of the move. If psycopg or DATABASE_URL reappears here,
    so does the failure that started this."""
    text = MODAL_JOB.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#"))
    for forbidden in ("psycopg", "DATABASE_URL", "_connect("):
        assert forbidden not in code, (
            f"modal/transcribe_job.py references {forbidden!r} again")


# ---------------------------------------------------------------------------
# gotcha 13: the boundary between Modal and workflow 02
# ---------------------------------------------------------------------------

def test_claim_takes_only_the_states_modal_owns():
    """Modal owns discovered/asr_failed and leaves rows transcribed; workflow
    02 claims transcribed/judge_failed. Widen either side and the same call is
    transcribed twice and paid for twice."""
    sql = asr_jobs.CLAIM_SQL
    assert "j.status IN ('discovered', 'asr_failed')" in sql
    for owned_by_02 in ("'transcribed'", "'judge_failed'"):
        assert f"IN ({owned_by_02}" not in sql
    assert "FOR UPDATE SKIP LOCKED" in sql, (
        "a second run started by hand must walk past locked rows, not queue")


def test_claim_spends_an_attempt_and_takes_a_lease():
    sql = asr_jobs.CLAIM_SQL
    assert "asr_attempts = j.asr_attempts + 1" in sql
    assert "claim_until  = now() + (%(lease)s * interval '1 second')" in sql
    assert "j.asr_attempts < %(max_attempts)s" in sql


def test_store_leaves_the_row_transcribed_and_releases_the_lease():
    """The handoff protocol in one statement: n8n renews the lease because it
    judges next; Modal is finished, so it releases."""
    sql = asr_jobs.STORE_SQL
    assert "status         = 'transcribed'" in sql
    assert "claim_token    = NULL" in sql
    assert "AND j.claim_token = lease.claim_token" in sql, "the fence is gone"


def test_store_writes_the_same_namespace_as_workflow_02():
    """A different external_source turns one call into two half-filled rows."""
    assert "'asterisk_drive'" in asr_jobs.STORE_SQL


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/asr/run/start", "/asr/claim", "/asr/store", "/asr/fail",
    "/asr/release", "/asr/run/finish"])
def test_every_asr_endpoint_requires_the_key(client, path):
    assert client.post(path, json={}).status_code == 401


def test_the_worker_mints_the_lease_token(client, calls):
    """Not the caller: the token is the fence every later write is checked
    against, and a caller that could choose it could reuse another run's."""
    r = client.post("/asr/run/start", headers=H,
                    json={"run_id": "asr-x", "gpu": "A10G",
                          "model_version": "07-2026", "claim_token": "attacker"})
    assert r.status_code == 200
    token = r.json()["claim_token"]
    assert token != "attacker"
    assert len(token) == 36 and token.count("-") == 4


def test_store_reports_a_rejected_lease_as_not_stored(client, monkeypatch):
    """An expired or stolen lease makes the UPDATE match no rows. That is not
    an error — and it must not read as success either, or a batch reports work
    it did not keep."""
    monkeypatch.setattr(db, "write", lambda sql, params=None: [])
    r = client.post("/asr/store", headers=H, json={
        "uniqueid": "1755000000.1", "claim_token": "t",
        "meta": {"uniqueid": "1755000000.1"}, "transcript": {"full_text": "x"}})
    assert r.status_code == 200
    assert r.json() == {"stored": False, "updated": []}


def test_store_serialises_jsonb_here_not_at_the_caller(client, calls):
    """meta and transcript arrive as objects and are encoded by the worker, so
    a caller cannot decide how this database's jsonb columns are written —
    ensure_ascii=False keeps the Arabic readable in the row."""
    client.post("/asr/store", headers=H, json={
        "uniqueid": "u1", "claim_token": "t",
        "meta": {"kind": "q"}, "transcript": {"full_text": "مرحبا"}})
    _, params = calls[-1]
    assert isinstance(params["meta"], str) and isinstance(params["tr"], str)
    assert "مرحبا" in params["tr"], "Arabic was escaped instead of stored as text"
    assert json.loads(params["meta"]) == {"kind": "q"}


def test_release_gives_back_the_attempt_it_spent(client, calls):
    """Otherwise --dry-run slowly dead-letters the backlog it exists to inspect
    safely."""
    client.post("/asr/release", headers=H, json={"claim_token": "t"})
    sql, _ = calls[-1]
    assert "asr_attempts = GREATEST(asr_attempts - 1, 0)" in sql
    assert "status = 'discovered'" in sql


def test_finish_returns_the_measured_rtfx(client, monkeypatch):
    """rtfx is a generated column. Every Modal cost figure in this project
    assumes 120 and nothing has ever observed it."""
    monkeypatch.setattr(db, "write", lambda sql, params=None: [
        {"run_id": "asr-x", "status": "succeeded", "processed": 5,
         "failed": 0, "rtfx": 96.4, "est_cost_usd": 0.12}])
    r = client.post("/asr/run/finish", headers=H, json={
        "run_id": "asr-x", "status": "succeeded", "processed": 5,
        "audio_seconds": 1200, "gpu_seconds": 12.4})
    assert r.json()["run"]["rtfx"] == 96.4


def test_finish_rejects_a_status_the_check_constraint_forbids(client):
    """asr_runs.status CHECK IN ('running','succeeded','failed','partial')."""
    r = client.post("/asr/run/finish", headers=H,
                    json={"run_id": "asr-x", "status": "done"})
    assert r.status_code == 422


def test_claim_limit_is_bounded(client):
    r = client.post("/asr/claim", headers=H, json={
        "run_id": "asr-x", "claim_token": "t", "limit": 100000})
    assert r.status_code == 422


def test_no_database_is_503_not_500(client, monkeypatch):
    def boom(sql, params=None):
        raise db.DatabaseUnavailable("DATABASE_URL not configured")

    monkeypatch.setattr(db, "write", boom)
    r = client.post("/asr/claim", headers=H,
                    json={"run_id": "asr-x", "claim_token": "t"})
    assert r.status_code == 503
    assert "DATABASE_URL" in r.text


# ---------------------------------------------------------------------------
# The writer is a separate, named path
# ---------------------------------------------------------------------------

def test_reports_still_cannot_write():
    """`cursor`/`rows`/`one` stay read-only; only `writer`/`write` may write,
    and they are reachable only by name."""
    source = Path(db.__file__).read_text(encoding="utf-8")
    assert "def writer(" in source and "def write(" in source
    assert "default_transaction_read_only = on" in source
    assert "if read_only:" in source, (
        "the read-only SET must be conditional on the pool, not removed")


def test_asr_jobs_is_the_only_module_that_writes():
    worker_app = Path(db.__file__).parent
    writers = [p.name for p in worker_app.glob("*.py")
               if re.search(r"\bdb\.write\b|\bdb\.writer\b",
                            p.read_text(encoding="utf-8"))]
    assert sorted(writers) == ["asr_jobs.py"], (
        f"{writers} write to the database; only asr_jobs.py may")
