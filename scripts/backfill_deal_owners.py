#!/usr/bin/env python3
"""Fill `deals.assigned_by_id` from Bitrix, then resolve every agent link.

    railway connect postgres --tunnel-only --port 55432
    export PGPASSWORD=... BITRIX_PORTAL_DOMAIN=travelgate.bitrix24.ae
    export BITRIX_WEBHOOK_USER_ID=128 BITRIX_WEBHOOK_TOKEN=...
    python scripts/backfill_deal_owners.py --apply

WHY THIS EXISTS. Workflow 04 pulls only deals modified in the last few days, so
the ~17,000 historical rows would never learn who owns them. This walks the
whole `crm.deal.list` once and writes ASSIGNED_BY_ID onto every deal we hold,
then calls `link_agent_attribution()` (migration 018) to turn those raw Bitrix
ids into agent_id on deals, interactions and agent_evaluations.

Safe to re-run: the UPDATE is keyed on bitrix_deal_id and the function is
idempotent. Nothing here creates a deal — a Bitrix id we do not already hold is
skipped, because inventing a deal row with no stage and no amount pollutes
every funnel report (the same rule 01c follows).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request


def bitrix_base() -> str:
    domain = os.getenv("BITRIX_PORTAL_DOMAIN")
    user = os.getenv("BITRIX_WEBHOOK_USER_ID")
    token = os.getenv("BITRIX_WEBHOOK_TOKEN")
    if not (domain and user and token):
        raise SystemExit("set BITRIX_PORTAL_DOMAIN, BITRIX_WEBHOOK_USER_ID, "
                         "BITRIX_WEBHOOK_TOKEN")
    return f"https://{domain}/rest/{user}/{token}"


def fetch_all_owners(base: str) -> dict[str, str]:
    """deal id -> ASSIGNED_BY_ID, every page.

    Bitrix caps a page at 50 whatever you ask for, and returns `next` until it
    does not. Paging on the cursor rather than a counter is what stops the
    silent truncation that made workflow 04 import 50 of 709 deals a night.
    """
    out: dict[str, str] = {}
    start, pages = 0, 0
    while True:
        body = json.dumps({"select": ["ID", "ASSIGNED_BY_ID"],
                           "order": {"ID": "ASC"}, "start": start}).encode()
        req = urllib.request.Request(f"{base}/crm.deal.list.json", data=body,
                                     headers={"Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read())
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))
        for row in d.get("result", []):
            out[str(row["ID"])] = str(row.get("ASSIGNED_BY_ID") or "")
        pages += 1
        if pages % 50 == 0:
            print(f"  {pages} pages, {len(out)} deals", flush=True)
        nxt = d.get("next")
        if nxt is None:
            return out
        start = nxt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--port", default=os.getenv("PGPORT", "55432"))
    ap.add_argument("--database", default="customer360")
    ap.add_argument("--cache", help="read/write the REST result here instead of "
                                    "re-walking 355 pages")
    args = ap.parse_args()

    if args.cache and os.path.exists(args.cache):
        owners = json.load(open(args.cache, encoding="utf-8"))
        print(f"owners from cache: {len(owners)}")
    else:
        owners = fetch_all_owners(bitrix_base())
        print(f"owners from Bitrix: {len(owners)}")
        if args.cache:
            json.dump(owners, open(args.cache, "w"), indent=0)

    print(f"distinct assignees: {len(set(owners.values()))}")
    if not args.apply:
        print("\nCHECK ONLY — nothing written. Re-run with --apply.")
        return 0

    password = os.getenv("PGPASSWORD")
    if not password:
        raise SystemExit("PGPASSWORD is not set")

    import psycopg

    dsn = f"postgresql://postgres:{password}@127.0.0.1:{args.port}/{args.database}"
    pairs = [(k, v) for k, v in owners.items() if v]

    with psycopg.connect(dsn, connect_timeout=20, autocommit=True) as conn:
        with conn.cursor() as cur:
            # One temp table and one UPDATE ... FROM, not 17,000 statements:
            # the round trips are the whole cost at this size.
            #
            # No ON COMMIT DROP: this connection is autocommit, so the CREATE
            # commits on its own and the table would be gone before the COPY.
            # A temp table dies with the session regardless.
            cur.execute("DROP TABLE IF EXISTS _owner")
            cur.execute("CREATE TEMP TABLE _owner (bitrix_deal_id text PRIMARY KEY, "
                        "assigned_by_id text)")
            with cur.copy("COPY _owner (bitrix_deal_id, assigned_by_id) FROM STDIN") as cp:
                for k, v in pairs:
                    cp.write_row((k, v))
            cur.execute("""
                UPDATE deals d
                   SET assigned_by_id = o.assigned_by_id, updated_at = now()
                  FROM _owner o
                 WHERE o.bitrix_deal_id = d.bitrix_deal_id
                   AND d.assigned_by_id IS DISTINCT FROM o.assigned_by_id
            """)
            print(f"deals given an owner: {cur.rowcount}")
            cur.execute("SELECT link_agent_attribution()")
            print("link_agent_attribution():",
                  json.dumps(cur.fetchone()[0], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
