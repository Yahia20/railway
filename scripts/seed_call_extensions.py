#!/usr/bin/env python3
"""Write the owner-approved extension -> employee mapping (migration 036).

    python scripts/seed_call_extensions.py --map local-reports/call_extension_map.json          # dry run, prints SQL
    python scripts/seed_call_extensions.py --map local-reports/call_extension_map.json --apply  # runs it

The map holds real staff names, so it lives in local-reports/ (git-ignored, rule
7), like agent_roster.json. Format:

    {"approved_on": "2026-11-15",
     "extensions": {
        "125": {"agent_id": "<uuid of an existing agents row>"},
        "109": {"pbx_only": true, "name": "Name as the PBX shows it"}
     }}

`agent_id` maps the extension to an existing employee. `pbx_only` creates the
employee first (no Bitrix account: calls only, no chats). Idempotent: a re-run
updates the mapping, never duplicates a person, and never touches an extension
that is not in the file. Applied through `railway ssh` into the postgres
service, so no tunnel and no password on this machine.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

EXT = re.compile(r"^[0-9]{2,6}$")


def lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_sql(spec: dict) -> str:
    day = spec["approved_on"]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise SystemExit("approved_on must be YYYY-MM-DD")
    out = ["BEGIN;", "SET LOCAL lock_timeout = '5s';"]
    for ext, v in sorted(spec["extensions"].items()):
        if not EXT.match(ext):
            raise SystemExit(f"not an extension: {ext!r}")
        if v.get("pbx_only"):
            name = v["name"].strip()
            if not name:
                raise SystemExit(f"{ext}: pbx_only needs a name")
            # A PBX-only person is keyed by their extension, so a re-run finds them again.
            out.append(f"""INSERT INTO agents (full_name, phone_extension, team)
SELECT {lit(name)}, {lit(ext)}, 'calls'
WHERE NOT EXISTS (SELECT 1 FROM agents WHERE phone_extension = {lit(ext)});""")
            target = f"(SELECT agent_id FROM agents WHERE phone_extension = {lit(ext)})"
            provenance = "pbx_only_employee"
        else:
            agent = str(uuid.UUID(v["agent_id"]))
            target = f"{lit(agent)}::uuid"
            provenance = "pbx_name_matches_bitrix"
        out.append(f"""INSERT INTO call_agent_mappings (extension, agent_id, approved, approved_at, provenance)
VALUES ({lit(ext)}, {target}, true, {lit(day)}::timestamptz, {lit(provenance)})
ON CONFLICT (extension) DO UPDATE
   SET agent_id = EXCLUDED.agent_id, approved = true, approved_at = EXCLUDED.approved_at,
       provenance = EXCLUDED.provenance, updated_at = now();""")
    out.append("SELECT count(*) AS mapped FROM call_agent_mappings WHERE approved;")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    sql = build_sql(json.loads(Path(args.map).read_text(encoding="utf-8")))
    if not args.apply:
        print(sql)
        return 0
    p = subprocess.run(
        ["railway", "ssh", "--service", "postgres", "--", "psql", "-h", "/var/run/postgresql",
         "-U", "postgres", "-d", "customer360", "-X", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, text=True, encoding="utf-8", capture_output=True,
        env={**__import__("os").environ, "MSYS_NO_PATHCONV": "1"})
    print(p.stdout[-2000:])
    if p.returncode:
        print(p.stderr[-2000:], file=sys.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
