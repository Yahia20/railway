#!/usr/bin/env python3
"""Build the agent roster by joining Bitrix REST against a manual CSV export.

    export BITRIX_PORTAL_DOMAIN=travelgate.bitrix24.ae
    export BITRIX_WEBHOOK_USER_ID=128 BITRIX_WEBHOOK_TOKEN=...
    python scripts/build_roster.py --csv DEAL_20260907_*.csv \
                                  --out local-reports/agent_roster.json

WHY A JOIN AND NOT A LOOKUP. The portal will not name its own users: the
webhook holds scope `crm` only, and `user.get` answers `insufficient_scope`.
Neither available source is sufficient alone —

  * `crm.deal.list` returns ASSIGNED_BY_ID, the numeric id, and no name.
  * a manual `DEAL_*.csv` export renders the same deal's owner as a display
    NAME, and its "Responsible ID" column is a custom field (nearly always
    'user_1'), so it carries no usable id.

— but both carry the deal id, so joining on that recovers both halves. Measured
on the 2026-09-07 export: 17,708 deals from REST against 17,707 from the CSV,
17,575 joined, 48 distinct user ids, every one resolving to a single name at
confidence 1.00.

THIS IS A ONE-OFF. Once `agents` holds these rows, ASSIGNED_BY_ID alone is
enough forever and no further export is needed. Re-run it only to onboard a
batch of new staff, or drop the CSV entirely and hand-edit the roster file —
it is just JSON.

OUTPUT is the format `scripts/seed_agents.py` reads:

    {"98": {"name": "Sales Person"}, "20114": {"name": "Bot", "is_bot": true}}

`is_bot` is preserved from an existing --out file if one is there, because that
flag is a human judgement about an account and must not be lost to a re-run.
The output lands in `local-reports/`, which is gitignored: these are real
people's names and this repository is public (rule 7).
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import sys
import time
import urllib.request

csv.field_size_limit(10_000_000)

# Column 0 is the deal id and column 10 the responsible person's display name
# in Bitrix's standard deal export. Both are positional because the header has
# duplicate names ("Stage" and "Destination Country" each appear twice), so
# looking a column up by name is ambiguous.
COL_DEAL_ID = 0
COL_RESPONSIBLE = 10


def bitrix_base() -> str:
    domain = os.getenv("BITRIX_PORTAL_DOMAIN")
    user = os.getenv("BITRIX_WEBHOOK_USER_ID")
    token = os.getenv("BITRIX_WEBHOOK_TOKEN")
    if not (domain and user and token):
        raise SystemExit("set BITRIX_PORTAL_DOMAIN, BITRIX_WEBHOOK_USER_ID, "
                         "BITRIX_WEBHOOK_TOKEN")
    return f"https://{domain}/rest/{user}/{token}"


def fetch_owners(base: str) -> dict[str, str]:
    """deal id -> ASSIGNED_BY_ID, every page. Bitrix caps a page at 50."""
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
        if d.get("next") is None:
            return out
        start = d["next"]


def read_names(path: str) -> dict[str, str]:
    """deal id -> responsible display name, from the manual export."""
    names: dict[str, str] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh, delimiter=";", quotechar='"')
        next(rd, None)                                   # header
        for row in rd:
            if len(row) > COL_RESPONSIBLE:
                names[row[COL_DEAL_ID].strip()] = row[COL_RESPONSIBLE].strip()
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True,
                    help="the DEAL_*.csv export (a glob is accepted)")
    ap.add_argument("--out", default="local-reports/agent_roster.json")
    ap.add_argument("--owners-cache",
                    help="read/write the REST result here instead of re-walking")
    ap.add_argument("--min-confidence", type=float, default=0.95,
                    help="below this an id maps to several names and is reported")
    args = ap.parse_args()

    matches = sorted(glob.glob(args.csv))
    if not matches:
        raise SystemExit(f"no CSV matched {args.csv!r}")
    csv_path = matches[-1]

    if args.owners_cache and os.path.exists(args.owners_cache):
        owners = json.load(open(args.owners_cache, encoding="utf-8"))
        print(f"owners (cached): {len(owners)}")
    else:
        owners = fetch_owners(bitrix_base())
        print(f"owners (REST)  : {len(owners)}")
        if args.owners_cache:
            json.dump(owners, open(args.owners_cache, "w"), indent=0)

    names = read_names(csv_path)
    print(f"names  (CSV)   : {len(names)}  from {os.path.basename(csv_path)}")

    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    joined = 0
    for deal_id, uid in owners.items():
        name = names.get(deal_id)
        if uid and name:
            votes[uid][name] += 1
            joined += 1
    print(f"joined         : {joined}")

    # Preserve is_bot: it is a human judgement about an account, and a re-run
    # that silently un-flagged an automation would put it straight back into
    # the scorecard and its prompts back into the judge's input.
    previous: dict = {}
    if os.path.exists(args.out):
        previous = json.load(open(args.out, encoding="utf-8"))

    roster, ambiguous = {}, []
    for uid, counter in votes.items():
        name, top = counter.most_common(1)[0]
        total = sum(counter.values())
        entry = {"name": name}
        if (previous.get(uid) or {}).get("is_bot"):
            entry["is_bot"] = True
        roster[uid] = entry
        if top / total < args.min_confidence:
            ambiguous.append((uid, counter.most_common(4)))

    for uid, entry in previous.items():
        if uid not in roster:
            roster[uid] = entry                          # keep hand-added rows

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(roster, fh, ensure_ascii=False, indent=1)

    bots = [u for u, e in roster.items() if e.get("is_bot")]
    print(f"\nwrote {args.out}: {len(roster)} agent(s), {len(bots)} flagged is_bot")
    if ambiguous:
        print("\nAMBIGUOUS — one id, several names (a rename or a shared login):")
        for uid, top in ambiguous:
            print(f"  {uid}: {top}")
    print("\nReview the file, flag any automation with \"is_bot\": true, then:")
    print(f"  python scripts/seed_agents.py --roster {args.out} --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
