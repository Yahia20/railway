"""Archive every call row before 023 deletes it. RUN THIS FIRST.

023_remove_calls.sql drops `transcripts`, `call_ingest_jobs` and `asr_runs` and
deletes every conversation whose `external_source` is 'asterisk_drive', along
with the analyses, evaluations, metrics, requests and alert occurrences that
cascade from them. None of that can be recovered from the database afterwards.
This script is the only copy.

WHAT IT WRITES. One newline-delimited JSON file per table, plus a manifest with
the row counts and the query that produced each one. NDJSON rather than a
pg_dump because the point is to be READABLE in five years by someone who does
not have this schema to restore into — and because `pg_dump` is not on the PATH
on the machine this project is actually operated from.

WHERE IT WRITES. Outside the repo, and nowhere near it by default. These files
contain customer phone numbers and the transcribed words of real phone calls:
CLAUDE.md rule 7 says this repo is public, and the default output directory is
the separate calls archive for exactly that reason.

VERIFY BEFORE YOU DROP. The manifest's counts are what section 9 of the
migration prints back. If they disagree, something wrote to the database between
the two, and you stop.

    python scripts/dump_calls.py --out ../travelgate-calls-archive/data
    python scripts/dump_calls.py --out ... --verify      # re-count, change nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:                                            # pragma: no cover
    sys.exit("psycopg is required: pip install 'psycopg[binary]'")


# The order is the order a restore would need: parents before children, so a
# reader replaying these files does not have to work the dependencies out.
#
# `interactions` is filtered to the calls namespace. Everything below it is
# filtered THROUGH that same subquery rather than by its own guess at what a
# call is — one definition, in one place, or the archive and the deletion
# disagree about which rows were calls.
CALL_INTERACTIONS = """
  SELECT interaction_id FROM interactions WHERE external_source = 'asterisk_drive'
"""

TABLES: list[tuple[str, str]] = [
    ("interactions",
     "SELECT * FROM interactions WHERE external_source = 'asterisk_drive'"),
    ("transcripts",
     "SELECT * FROM transcripts"),
    ("call_ingest_jobs",
     "SELECT * FROM call_ingest_jobs"),
    ("asr_runs",
     "SELECT * FROM asr_runs"),
    ("interaction_analysis",
     f"SELECT * FROM interaction_analysis WHERE interaction_id IN ({CALL_INTERACTIONS})"),
    ("interaction_destinations",
     "SELECT d.* FROM interaction_destinations d "
     "JOIN interaction_analysis a USING (analysis_id) "
     f"WHERE a.interaction_id IN ({CALL_INTERACTIONS})"),
    ("interaction_requests",
     f"SELECT * FROM interaction_requests WHERE interaction_id IN ({CALL_INTERACTIONS})"),
    ("agent_evaluations",
     f"SELECT * FROM agent_evaluations WHERE interaction_id IN ({CALL_INTERACTIONS})"),
    ("interaction_metrics",
     f"SELECT * FROM interaction_metrics WHERE interaction_id IN ({CALL_INTERACTIONS})"),
    ("alert_occurrences",
     f"SELECT * FROM alert_occurrences WHERE interaction_id IN ({CALL_INTERACTIONS})"),
    ("follow_ups",
     f"SELECT * FROM follow_ups WHERE promised_in IN ({CALL_INTERACTIONS})"),
    # Both halves of what calls cost: the ASR runs and the judge calls attached
    # to a call conversation. `purpose = 'asr'` catches rows with no
    # interaction_id, which is how a batch-level ASR call is recorded.
    ("model_calls",
     "SELECT * FROM model_calls WHERE purpose = 'asr' "
     f"OR interaction_id IN ({CALL_INTERACTIONS})"),
    ("provider_budgets",
     "SELECT * FROM provider_budgets WHERE provider IN ('modal', 'cohere')"),
    ("provider_status",
     "SELECT * FROM provider_status WHERE provider IN ('modal', 'cohere')"),
]


def encode(value):
    """Types psycopg returns that `json` will not take.

    Decimal goes to str, not float: `asr_confidence` and every money column are
    exact in the database and would stop being exact here.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    raise TypeError(f"cannot archive {type(value).__name__}")


def dsn(args) -> str:
    if args.dsn:
        return args.dsn
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    password = os.getenv("PGPASSWORD")
    if not password:
        sys.exit("set PGPASSWORD (the railway tunnel prints it) or pass --dsn")
    return (f"host={args.host} port={args.port} dbname={args.dbname} "
            f"user={args.user} password={password}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="../travelgate-calls-archive/data",
                   help="directory to write into. Keep it OUT of this repo.")
    p.add_argument("--verify", action="store_true",
                   help="count rows and compare with an existing manifest; write nothing")
    p.add_argument("--dsn")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="55432")
    p.add_argument("--dbname", default="customer360")
    p.add_argument("--user", default="postgres")
    args = p.parse_args()

    out = Path(args.out)
    manifest_path = out / "manifest.json"

    with psycopg.connect(dsn(args), row_factory=dict_row) as conn:
        # This script must never be the thing that changes the database it is
        # archiving. Read-only is set on the connection, not remembered.
        conn.execute("SET default_transaction_read_only = on")
        conn.execute("SET statement_timeout = '30min'")

        if conn.execute("SELECT current_database()").fetchone()["current_database"] != args.dbname:
            sys.exit(f"connected to the wrong database; expected {args.dbname}")

        counts: dict[str, int] = {}
        if not args.verify:
            out.mkdir(parents=True, exist_ok=True)

        for table, query in TABLES:
            exists = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{table}",)
            ).fetchone()["ok"]
            if not exists:
                print(f"  {table:26} — table is already gone, skipping")
                counts[table] = None
                continue

            rows = conn.execute(query).fetchall()
            counts[table] = len(rows)
            if args.verify:
                print(f"  {table:26} {len(rows):>7} rows")
                continue

            target = out / f"{table}.ndjson"
            with open(target, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False, default=encode) + "\n")
            print(f"  {table:26} {len(rows):>7} rows -> {target}")

    if args.verify:
        if not manifest_path.exists():
            sys.exit("no manifest to verify against; run without --verify first")
        old = json.loads(manifest_path.read_text(encoding="utf-8"))["counts"]
        drift = {t: (old.get(t), counts.get(t))
                 for t in counts if old.get(t) != counts.get(t)}

        if not drift:
            print("\nverified: every count matches the manifest. 023 has NOT "
                  "run yet, and the archive is current.")
            return 0

        # TWO VERY DIFFERENT KINDS OF DRIFT, AND CONFLATING THEM IS USELESS.
        #
        # This check was written to answer "did anything write to the database
        # between the dump and the migration". But it is the same command you
        # reach for AFTERWARDS, to confirm the migration did what it said — and
        # run then, every count is zero or the table is gone. An unqualified
        # "something wrote, re-dump before running 023" is both alarming and
        # exactly backwards, which is what it printed on the real run.
        removed = {t: v for t, v in drift.items()
                   if v[1] in (0, None) and (v[0] or 0) > 0}
        unexpected = {t: v for t, v in drift.items() if t not in removed}

        if unexpected:
            print("\nCOUNTS MOVED IN A WAY 023 DOES NOT EXPLAIN:")
            for table, (was, now) in unexpected.items():
                print(f"  {table}: archived {was}, database now has {now}")
            sys.exit("something else wrote to the database. Re-dump before "
                     "running 023.")

        print("\n023 HAS RUN. Every archived table is now empty or dropped:")
        for table, (was, now) in sorted(removed.items()):
            print(f"  {table:26} {was:>6} archived  ->  "
                  + ("table dropped" if now is None else f"{now} rows left"))
        total = sum(v[0] for v in removed.values())
        print(f"\nThe archive is the only copy of those {total:,} rows: "
              f"{out.resolve()}")
        return 0

    manifest = {
        "taken_at": datetime.now().astimezone().isoformat(),
        "database": args.dbname,
        "counts": counts,
        "queries": dict(TABLES),
        "restores_into": "db/migrations/008, 010, 012 (in the calls archive)",
        "note": "Customer phone numbers and transcribed call audio. Do not "
                "commit these files to a public repository.",
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nmanifest -> {manifest_path}")
    print("Now run:  psql ... -v ON_ERROR_STOP=1 -f db/migrations/023_remove_calls.sql")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
