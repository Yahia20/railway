#!/usr/bin/env python3
"""Backfill `interactions.customer_phone_e164` from a Bitrix deal export.

WHY THIS EXISTS. The production chat API does not send a phone number, and
RESOLVE (workflow 03) matches customers on the phone and nothing else. So every
chat thread is an island: the same person's five conversations cannot be
recognised as one customer, `customers` stays empty, and every per-customer
number downstream — deals per customer, lifetime value, average score — has
nothing to group by.

The CSV export closes that for the threads it covers. It is a ONE-OFF repair,
not a pipeline: it is a snapshot with a date on it, and re-running it a month
from now will not know about anything that happened since. The permanent fix is
the chat API sending `phone` on every message, and this script is what makes
the data usable until it does.

SCOPE: LINKING ONLY. Four columns are read — the deal id, the contact id, and
two phone columns. Nothing else. The export also carries `Lead Temperature`,
`Bot Confidence Score`, `Customer Intent Level`, `Lead Score` and a dozen
similar fields written by another system's AI, and those are exactly the
judgements this project exists to produce from the conversation itself, with
evidence. Importing somebody else's unverifiable version of them would put two
disagreeing answers in one database with nothing to say which is which.

MATCHING. By Bitrix deal id first, then by Bitrix contact id. Both are natural
keys the chat API already sends on every message and 01c already stores, so
neither is a guess. Deal id first because it is the narrower of the two: one
deal is one request, while a contact can have several.

NEVER OVERWRITES. Only rows where `customer_phone_e164` IS NULL are touched. A
phone already on a row came from the source that owns the conversation, and a
CSV exported on one afternoon does not get to overrule it.

    python scripts/import_bitrix_phones.py DEAL_*.csv                 # dry run
    python scripts/import_bitrix_phones.py DEAL_*.csv --apply
    python scripts/import_bitrix_phones.py DEAL_*.csv --apply --port 55432

The database is reached over the Railway tunnel:

    railway connect postgres --tunnel-only --port 55432
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path

# The repo's own normaliser, so an imported number is identical to one that
# arrived through the pipeline. A second implementation here would drift, and
# two spellings of one phone number are two customers.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "worker"))
from app.normalize.phone import try_normalize  # noqa: E402

# Read in this order, first non-empty wins. `Contact: Work Phone` is the
# fullest column in the observed export (482/486) despite the name — Bitrix
# files a WhatsApp contact's number there.
PHONE_COLUMNS = (
    "Contact: Work Phone",
    "Phone original",
    "Contact: Mobile",
    "phone number",
)
DEAL_COLUMN = "ID"
CONTACT_COLUMN = "Contact: ID"

DEFAULT_REGION = os.getenv("DEFAULT_PHONE_REGION", "SA")


def clean_source_number(raw: str) -> list[str]:
    """Candidate spellings of one number, best first.

    THIS BELONGS TO THE EXPORT, NOT TO THE PIPELINE. Every quirk handled here
    is a way THIS spreadsheet writes phone numbers, so it is cleaned up on the
    way in rather than by teaching `normalize/phone.py` to be lenient — that
    module is what the live ingest uses, and loosening it would loosen every
    path at once.

    Two quirks, observed in the 29 Aug export:

    1. Two numbers in one cell: `966537842747, +20537487893`. Only the first is
       the customer's; the second repeats across dozens of rows and is an
       office line.
    2. An international number with the `+` dropped: `971508163059` (UAE),
       `447424545785` (UK), `923289123105` (Pakistan). Asked to read these as
       Saudi NATIONAL numbers the normaliser correctly refuses — 12 digits
       where it wants 9.

    Re-adding the `+` is NOT the guess gotcha 9 forbids. That rule is about a
    number with NO country code, where choosing one merges two different
    people; here the country code is present in the digits and only the plus is
    missing. A bare national number is still returned unchanged and still
    refused.
    """
    if not raw:
        return []
    # The customer's own number is the first; the rest is whatever else the
    # CRM row accumulated.
    head = raw.replace(";", ",").replace("/", ",").split(",")[0]
    head = head.strip()
    digits = "".join(ch for ch in head if ch.isdigit())
    candidates = [head]

    if not head.startswith("+") and digits:
        if head.startswith("00"):
            candidates.append("+" + digits[2:])
        # 10-15 digits not starting with a trunk 0 is the shape of an
        # international number missing its plus. Shorter than 10 is a national
        # number and must stay ambiguous.
        elif not digits.startswith("0") and 10 <= len(digits) <= 15:
            candidates.append("+" + digits)

    return candidates


def read_export(path: Path) -> tuple[dict[str, str], dict[str, str], Counter]:
    """(deal id -> phone), (contact id -> phone), and why rows were skipped.

    Bitrix exports UTF-8 with a BOM and semicolon delimiters. The 366-column
    rows blow past csv's default field limit, hence the raise.
    """
    csv.field_size_limit(10 ** 7)
    by_deal: dict[str, str] = {}
    by_contact: dict[str, str] = {}
    stats: Counter = Counter()

    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        missing = [c for c in (DEAL_COLUMN, CONTACT_COLUMN) if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path.name} has no {missing} column — is this a deal export?")

        for row in reader:
            stats["rows"] += 1
            raw = next((row[c].strip() for c in PHONE_COLUMNS
                        if row.get(c) and row[c].strip()), "")
            if not raw:
                stats["no_phone_in_row"] += 1
                continue

            phone = error = None
            for candidate in clean_source_number(raw):
                phone, error = try_normalize(candidate, DEFAULT_REGION)
                if phone:
                    break
            if not phone:
                # Deliberate, not a bug to route around: a bare Egyptian
                # national number cannot be told from a Saudi one, and guessing
                # +966 merges two different people. A null phone is
                # recoverable; a wrong-country match is not.
                stats["unnormalisable"] += 1
                stats[f"  eg. {error}"] += 1
                continue

            deal = (row.get(DEAL_COLUMN) or "").strip()
            contact = (row.get(CONTACT_COLUMN) or "").strip()
            if deal:
                by_deal[deal] = phone
            if contact:
                # First deal wins. A contact with two deals normally repeats one
                # number; when it does not, the alternative is picking the last
                # row read, which is no better and is order-dependent.
                by_contact.setdefault(contact, phone)

    return by_deal, by_contact, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path", type=Path)
    ap.add_argument("--apply", action="store_true",
                    help="write. Without this it reports and changes nothing.")
    ap.add_argument("--port", default=os.getenv("PGPORT", "55432"),
                    help="local port of the railway tunnel (default 55432)")
    ap.add_argument("--database", default="customer360")
    args = ap.parse_args()

    if not args.csv_path.exists():
        raise SystemExit(f"no such file: {args.csv_path}")

    by_deal, by_contact, stats = read_export(args.csv_path)
    print(f"export      : {args.csv_path.name}")
    print(f"rows        : {stats['rows']}")
    print(f"deal keys   : {len(by_deal)}")
    print(f"contact keys: {len(by_contact)}")
    for reason, n in stats.most_common():
        if reason != "rows":
            print(f"  skipped   : {n}  ({reason})")

    password = os.getenv("PGPASSWORD")
    if not password:
        raise SystemExit("PGPASSWORD is not set — read it from Railway → postgres → Variables")

    import psycopg

    dsn = f"postgresql://postgres:{password}@127.0.0.1:{args.port}/{args.database}"
    with psycopg.connect(dsn, connect_timeout=20) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("""
                SELECT interaction_id, external_deal_id, external_contact_id
                FROM interactions
                WHERE customer_phone_e164 IS NULL
                  AND external_source = 'bitrix_chat_api'
            """)
            targets = cur.fetchall()

            updates, by_key = [], Counter()
            for interaction_id, deal, contact in targets:
                phone = by_deal.get((deal or "").strip())
                key = "deal"
                if not phone:
                    phone = by_contact.get((contact or "").strip())
                    key = "contact"
                if phone:
                    updates.append((phone, interaction_id))
                    by_key[key] += 1

            print()
            print(f"chat threads with no phone : {len(targets)}")
            print(f"matched by deal id         : {by_key['deal']}")
            print(f"matched by contact id      : {by_key['contact']}")
            print(f"total to fill              : {len(updates)}"
                  f"  ({len(updates) * 100 // max(len(targets), 1)}%)")
            print(f"left without a phone       : {len(targets) - len(updates)}")

            if not args.apply:
                print("\nDRY RUN — nothing written. Re-run with --apply.")
                conn.rollback()
                return 0

            # customer_phone_raw is filled alongside, and says where the value
            # came from. A number that appeared in a conversation and one that
            # was imported from a spreadsheet are different kinds of fact, and
            # six months from now nothing else will record which this was.
            cur.executemany("""
                UPDATE interactions
                   SET customer_phone_e164 = %s,
                       customer_phone_raw  = coalesce(customer_phone_raw,
                                                      'bitrix_csv_import'),
                       updated_at          = now()
                 WHERE interaction_id      = %s
                   AND customer_phone_e164 IS NULL
            """, updates)
            written = cur.rowcount
            conn.commit()
            print(f"\nAPPLIED — {len(updates)} rows updated "
                  f"(driver reported {written}).")

            cur.execute("""
                SELECT count(*) FILTER (WHERE customer_phone_e164 IS NOT NULL),
                       count(*)
                FROM interactions WHERE external_source = 'bitrix_chat_api'
            """)
            have, total = cur.fetchone()
            print(f"chat threads with a phone now: {have}/{total}")
            print("\nRESOLVE (workflow 03) is what turns these into customers. "
                  "It is currently OFF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
