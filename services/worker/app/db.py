"""Read-only database access for the worker.

WHY THIS DID NOT EXIST BEFORE. Every write in this system goes through an n8n
Postgres node: n8n owns the transaction, the retry and the lease, and the worker
is a pure function it calls. That split is deliberate and this module does not
change it — nothing here writes.

WHAT IT IS FOR. The reporting endpoint needs to read thirteen views that already
exist in the database. Shipping those numbers back through n8n would mean a
workflow whose only job is to forward SELECT results to a browser.

THREE GUARANTEES, EACH ENFORCED HERE RATHER THAN TRUSTED:

  read-only    every connection sets `default_transaction_read_only`, so a typo
               in a report query fails instead of writing. The report path can
               never be the thing that corrupts a score.
  bounded      `statement_timeout` caps a query that hits a bad plan, and the
               pool caps concurrency. A report someone reloads impatiently must
               not starve the judge of connections.
  lazy         the pool opens on first use, not at import. The worker boots and
               answers its healthcheck with no database configured at all —
               which is exactly how the tests run it.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

log = logging.getLogger("worker.db")

# A report query that has not answered in ten seconds is a bug, not slow data.
# The whole dashboard is counts over tables with the right indexes.
STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "10000"))

# How long to wait for a connection out of the pool before giving up. See the
# `timeout=` note in _build_pool: this bounds the whole report, not one query.
CONNECT_TIMEOUT_S = float(os.getenv("DB_CONNECT_TIMEOUT_S", "10"))

_pool: Any = None
_lock = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """DATABASE_URL is not set, or psycopg could not open a pool."""


def _build_pool() -> Any:
    if not settings.database_url:
        raise DatabaseUnavailable("DATABASE_URL not configured")
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:  # pragma: no cover - psycopg is a hard dependency
        raise DatabaseUnavailable(f"psycopg not installed: {exc}") from exc

    def configure(conn: Any) -> None:
        """Session settings for every pooled connection.

        AUTOCOMMIT FIRST, AND NOT AS A STYLE CHOICE. psycopg_pool requires a
        configure function to hand the connection back idle. A plain
        `conn.execute("SET ...")` opens an implicit transaction and leaves it
        open, so the pool rejects the connection with

            connection left in status INTRANS by configure function: discarded

        and retries forever — every request then blocks for the pool timeout
        and the endpoint hangs rather than failing. Setting autocommit first
        makes each SET its own transaction and leaves nothing open. (Caught in
        production, not in review: the pool is lazy, so nothing touches this
        path until the first real report request.)

        Both settings are SESSION scope, so they outlive the statement that set
        them and cover every query this connection later runs.
        """
        conn.autocommit = True
        # Belt and braces: the role may already be read-only, but this endpoint
        # must be read-only regardless of how the database is provisioned.
        conn.execute("SET default_transaction_read_only = on")
        conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")

    pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row},
        configure=configure,
        # Fail fast. The default is 30 seconds, and the report runs sixteen
        # queries: a pool that cannot hand out connections would otherwise hold
        # a single request for eight minutes before answering. Ten seconds is
        # long enough for a cold connection over Railway's private network and
        # short enough that a broken pool reports itself immediately.
        timeout=CONNECT_TIMEOUT_S,
        # Do not connect at construction time. A database that is briefly down
        # must not stop the worker from starting and serving /health.
        open=False,
        name="worker-readonly",
    )
    pool.open()
    return pool


def get_pool() -> Any:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


@contextmanager
def cursor() -> Iterator[Any]:
    """A read-only cursor returning dict rows.

    A pool that cannot produce a connection is a database problem, not a query
    problem, so it surfaces as DatabaseUnavailable — which `report._panel` lets
    through to become one 503 instead of sixteen identical panel errors behind
    a 200.
    """
    try:
        from psycopg_pool import PoolTimeout
    except ImportError:  # pragma: no cover - psycopg is a hard dependency
        PoolTimeout = ()  # type: ignore[assignment]

    pool = get_pool()
    try:
        conn_ctx = pool.connection()
    except PoolTimeout as exc:  # pragma: no cover - needs a broken database
        raise DatabaseUnavailable(f"no database connection: {exc}") from exc

    try:
        with conn_ctx as conn:
            with conn.cursor() as cur:
                yield cur
    except PoolTimeout as exc:
        raise DatabaseUnavailable(f"no database connection: {exc}") from exc


def rows(sql: str, params: Any = None) -> list[dict]:
    """Run one SELECT and return every row as a dict."""
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def one(sql: str, params: Any = None) -> dict:
    """Run one SELECT expected to produce a single row. `{}` if it produced none."""
    result = rows(sql, params)
    return result[0] if result else {}


def close() -> None:
    """Release the pool. Called from the FastAPI shutdown hook."""
    global _pool
    with _lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception as exc:  # pragma: no cover - shutdown best effort
                log.warning("closing db pool: %s", exc)
            _pool = None
