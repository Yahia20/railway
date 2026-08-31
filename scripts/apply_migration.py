#!/usr/bin/env python3
"""Apply one migration file to `customer360` over the Railway tunnel.

The database has no public TCP proxy, so migrations have historically been
pasted into the Railway console by hand — which is how 011 ended up applied in
the live database but recorded nowhere, and how a paste arriving mangled as
`^[[200~psql` became a debugging session. This runs the file as written.

Every migration in `db/migrations/` manages its own transaction, or is written
to be idempotent, or both. This script does not add one: wrapping a file that
already says BEGIN/COMMIT would nest, and silently rolling back a file designed
to commit in stages is worse than either.

    railway connect postgres --tunnel-only --port 55440
    export PGPASSWORD=...
    python scripts/apply_migration.py db/migrations/015_*.sql --check
    python scripts/apply_migration.py db/migrations/015_*.sql --apply
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--apply", action="store_true",
                    help="run it. Without this the file is only parsed and described.")
    ap.add_argument("--port", default=os.getenv("PGPORT", "55440"))
    ap.add_argument("--database", default="customer360")
    args = ap.parse_args()

    sql = io.open(args.path, encoding="utf-8").read()
    print(f"file      : {args.path}")
    print(f"size      : {len(sql)} chars")
    print(f"database  : {args.database} @ 127.0.0.1:{args.port}")
    print(f"own txn   : {'yes' if 'BEGIN;' in sql else 'no'}")

    if not args.apply:
        print("\nCHECK ONLY — nothing run. Re-run with --apply.")
        return 0

    password = os.getenv("PGPASSWORD")
    if not password:
        raise SystemExit("PGPASSWORD is not set")

    import psycopg

    dsn = f"postgresql://postgres:{password}@127.0.0.1:{args.port}/{args.database}"
    with psycopg.connect(dsn, connect_timeout=20, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print("\nAPPLIED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
