#!/usr/bin/env python3
"""Seed `agents` from a roster file, and mark which accounts are automations.

    railway connect postgres --tunnel-only --port 55432
    export PGPASSWORD=...
    python scripts/seed_agents.py --roster local-reports/agent_roster.json
    python scripts/seed_agents.py --roster local-reports/agent_roster.json --apply

WHY A FILE AND NOT A MIGRATION. The roster is 48 real people's names. This
repository is public (rule 7), so the names live in `local-reports/`, which is
gitignored, and the migration carries only the schema. It is also the more
useful shape: onboarding a salesperson is one line of JSON and a re-run, not a
new migration.

WHERE THE ROSTER COMES FROM. `user.get` is outside the webhook's scope, so the
portal will not name its own users. `crm.deal.list` returns ASSIGNED_BY_ID, and
a manual `DEAL_*.csv` export renders the same deal's owner as a display name;
joining the two on the deal id recovers both halves. `scripts/build_roster.py`
does that join and writes this file. Once these rows exist, ASSIGNED_BY_ID
alone is enough and the CSV is never needed again.

ROSTER FORMAT — {"<bitrix_user_id>": {"name": str, "is_bot": bool}}:

    {
      "98":    {"name": "Sales Person"},
      "20114": {"name": "Qualification Bot", "is_bot": true}
    }

`is_bot` is the only flag that matters and it is not cosmetic: it is what keeps
an automation out of `v_agent_scorecard`, and what makes 01d relabel its turns
so its text never reaches the judge as agent speech. Set it for anything that
posts into a thread without a person typing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", required=True, help="path to the roster JSON")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--port", default=os.getenv("PGPORT", "55432"))
    ap.add_argument("--database", default="customer360")
    args = ap.parse_args()

    with open(args.roster, encoding="utf-8") as fh:
        roster = json.load(fh)

    rows = []
    for uid, v in roster.items():
        name = (v.get("name") or "").strip() if isinstance(v, dict) else str(v).strip()
        if not name:
            print(f"  skip {uid}: no name")
            continue
        is_bot = bool(v.get("is_bot")) if isinstance(v, dict) else False
        rows.append((str(uid), name, is_bot, not is_bot))

    bots = [r[0] for r in rows if r[2]]
    print(f"roster        : {args.roster}")
    print(f"people        : {len(rows) - len(bots)}")
    print(f"automations   : {len(bots)}  (ids {', '.join(bots) or 'none'})")
    if not args.apply:
        print("\nCHECK ONLY — nothing written. Re-run with --apply.")
        return 0

    password = os.getenv("PGPASSWORD")
    if not password:
        raise SystemExit("PGPASSWORD is not set")

    import psycopg

    dsn = f"postgresql://postgres:{password}@127.0.0.1:{args.port}/{args.database}"
    with psycopg.connect(dsn, connect_timeout=20, autocommit=True) as conn:
        with conn.cursor() as cur:
            # executemany, not one statement per name: the round trips are the
            # whole cost, and ON CONFLICT keeps it re-runnable.
            cur.executemany(
                """
                INSERT INTO agents (bitrix_user_id, full_name, is_bot, is_active)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (bitrix_user_id) DO UPDATE
                   SET full_name  = EXCLUDED.full_name,
                       -- is_bot only ever goes ON, never off. A roster
                       -- rebuilt from Bitrix knows nothing about which
                       -- accounts are automations -- that is a human
                       -- judgement -- so a re-run must not silently
                       -- un-flag one and put its prompts back into the
                       -- judge's input. To clear it, do it deliberately:
                       --   UPDATE agents SET is_bot = false WHERE ...
                       is_bot     = agents.is_bot OR EXCLUDED.is_bot,
                       is_active  = NOT (agents.is_bot OR EXCLUDED.is_bot),
                       updated_at = now()
                """,
                rows,
            )
            print(f"upserted      : {len(rows)} agent(s)")
            cur.execute("SELECT link_agent_attribution()")
            print("attribution   :", json.dumps(cur.fetchone()[0], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
