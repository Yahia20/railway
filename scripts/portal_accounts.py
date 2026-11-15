#!/usr/bin/env python3
"""Create portal accounts in bulk (migration 035). Prints nothing secret.

    python scripts/portal_accounts.py --out local-reports/portal-accounts.csv                     # dry run
    python scripts/portal_accounts.py --out local-reports/portal-accounts.csv --manager yahia --apply

Creates one `agent` account for every active employee who has something to see
(a v6 chat grade or an approved call extension), plus the named manager
accounts, each with a one-time temporary password that must be changed at the
first sign-in. Existing usernames are left exactly as they are, so a re-run
only adds people who joined since.

The usernames and temporary passwords go to --out, which must be under
local-reports/ (git-ignored, rule 7): hand each person their own line. Later
accounts and resets are done from the portal's own "الحسابات" tab.

Runs through `railway ssh` into the postgres service: no tunnel, no password
on this machine. Passwords are hashed here (scrypt, app/portal/passwords.py),
so only hashes cross the wire.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))
from app.portal.passwords import password_hash  # noqa: E402

ENV = {**os.environ, "MSYS_NO_PATHCONV": "1"}
PSQL = ["railway", "ssh", "--service", "postgres", "--", "psql", "-h", "/var/run/postgresql",
        "-U", "postgres", "-d", "customer360", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-f", "-"]

WHO = """SELECT json_agg(json_build_object('agent_id', a.agent_id, 'name', a.full_name) ORDER BY a.full_name)
FROM agents a
WHERE a.is_active AND NOT a.is_bot AND NOT a.qa_excluded
  AND (EXISTS (SELECT 1 FROM qa_grades_v6 g WHERE g.agent_id = a.agent_id)
       OR EXISTS (SELECT 1 FROM call_agent_mappings m WHERE m.agent_id = a.agent_id AND m.approved))
  AND NOT EXISTS (SELECT 1 FROM portal_auth.users u WHERE u.agent_id = a.agent_id);"""


def psql(sql: str) -> str:
    p = subprocess.run(PSQL, input=sql, text=True, encoding="utf-8", capture_output=True, env=ENV)
    if p.returncode:
        raise SystemExit(p.stderr[-1500:])
    return p.stdout


def username(name: str, taken: set[str]) -> str:
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = ".".join(re.findall(r"[a-z0-9]+", ascii_)[:2]) or "employee"
    if len(base) < 3:
        base = f"{base}.tg"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}{n}", n + 1
    taken.add(out)
    return out


def lit(v: str | None) -> str:
    return "NULL" if v is None else "'" + v.replace("'", "''") + "'"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--manager", action="append", default=[], help="username of a manager account")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    if (ROOT / "local-reports") not in out.parents:
        raise SystemExit("--out must be inside local-reports/ (git-ignored)")

    taken = set(json.loads(psql("SELECT coalesce(json_agg(username), '[]') FROM portal_auth.users;").strip() or "[]"))
    people = json.loads(psql(WHO).strip() or "null") or []
    accounts = [{"username": m.lower(), "name": "(manager)", "role": "manager", "agent_id": None}
                for m in args.manager if m.lower() not in taken]
    taken |= {a["username"] for a in accounts}
    accounts += [{"username": username(p["name"], taken), "name": p["name"], "role": "agent",
                  "agent_id": p["agent_id"]} for p in people]
    print(f"{len(accounts)} new account(s): {sum(a['role'] == 'manager' for a in accounts)} manager, "
          f"{sum(a['role'] == 'agent' for a in accounts)} employee")
    if not args.apply or not accounts:
        return 0

    for a in accounts:
        a["password"] = secrets.token_urlsafe(12)
    sql = ["BEGIN;"] + [
        f"""INSERT INTO portal_auth.users (username, password_hash, role, agent_id)
VALUES ({lit(a['username'])}, {lit(password_hash(a['password']))}, {lit(a['role'])}, {lit(a['agent_id'])}::uuid)
ON CONFLICT (username) DO NOTHING;""" for a in accounts] + [
        "INSERT INTO portal_auth.audit (action) VALUES ('bulk_create_accounts');", "COMMIT;"]
    psql("\n".join(sql))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if fh.tell() == 0:
            w.writerow(["username", "name", "role", "temporary_password"])
        for a in accounts:
            w.writerow([a["username"], a["name"], a["role"], a["password"]])
    print("written to", out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
