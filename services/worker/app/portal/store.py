"""Separate credentials; never fall back to the worker/n8n database role."""
import os
from contextlib import contextmanager

from fastapi import HTTPException


@contextmanager
def connection(*, auth=False):
    import psycopg
    from psycopg.rows import dict_row
    name = 'PORTAL_AUTH_DATABASE_URL' if auth else 'PORTAL_READ_DATABASE_URL'
    url = os.getenv(name)
    if not url:
        raise HTTPException(503, 'Portal is not configured')
    options = '-c statement_timeout=10000'
    if not auth:
        options += ' -c default_transaction_read_only=on'
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options=options) as conn:
        yield conn


def rows(sql, params=()):
    with connection() as conn:
        return conn.execute(sql, params).fetchall()
