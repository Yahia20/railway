#!/usr/bin/env python3
"""Push repo workflows to the live n8n, stamping the real credential id on.

    export N8N_API_KEY=...
    python scripts/n8n_deploy.py --list                 # what is live, and its id
    python scripts/n8n_deploy.py 01d 04                 # update/create, stay inactive
    python scripts/n8n_deploy.py 01d --activate
    python scripts/n8n_deploy.py --backup-only          # snapshot the live copies

WHY THIS EXISTS. n8n binds credentials by INTERNAL ID (CLAUDE.md gotcha 8), so a
workflow JSON in git can only ever carry a placeholder, and importing one by hand
leaves every Postgres node broken with no error until it runs. This stamps the
live id on the way out. It is also the only place that knows which repo file
corresponds to which live workflow — that mapping used to live in somebody's
head, and getting it wrong overwrites the wrong production workflow.

ALWAYS BACKS UP FIRST. Every live copy it is about to touch is written to
`local-reports/n8n-backup-<date>/` before anything is sent. That directory is
gitignored; the backup is for the next ten minutes, not for history.

Activation is deliberately separate. Deploying a workflow and switching it on
are different decisions, and the second one costs money.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import httpx

BASE = os.getenv("N8N_BASE_URL", "https://n8n-production-a685c.up.railway.app") + "/api/v1"
REPO = Path(__file__).resolve().parent.parent
WF_DIR = REPO / "n8n" / "workflows"

# Live Postgres credential. Not a secret — it is an internal reference with no
# password in it, and it is meaningless outside this one n8n instance.
PG_CRED = {
    "id": os.getenv("N8N_PG_CREDENTIAL_ID", "eYWvPxQFAwKs0bOu"),
    "name": os.getenv("N8N_PG_CREDENTIAL_NAME", "railway-pg (api)"),
}

# repo stem -> live workflow id. None means "create it".
TARGETS: dict[str, str | None] = {
    "01c-chats-store-only": "H7r5YWGJ3nNVA99Z",
    "01d-chats-evaluate": "P1zSFsw16wmV28YF",
    "02-calls-v2-state-machine": "Q3ARdzVsO3Z8bcWr",
    "03-nightly-resolve-and-aggregate": "sUnNPv6Ucye6Gsii",
    "04-nightly-housekeeping": "z60SxzoYmKOLsH4S",
}


def client() -> httpx.Client:
    key = os.environ.get("N8N_API_KEY")
    if not key:
        raise SystemExit("N8N_API_KEY is not set")
    return httpx.Client(headers={"X-N8N-API-KEY": key}, timeout=120)


def resolve(name: str) -> str:
    """Accept '01d', '01d-chats-evaluate' or the full filename."""
    stems = list(TARGETS)
    exact = [s for s in stems if s == name or s == name.removesuffix(".json")]
    if exact:
        return exact[0]
    prefix = [s for s in stems if s.startswith(name)]
    if len(prefix) == 1:
        return prefix[0]
    raise SystemExit(f"{name!r} matches {prefix or 'nothing'}; be more specific")


def backup(cl: httpx.Client, ids: list[str]) -> Path:
    out = REPO / "local-reports" / f"n8n-backup-{date.today():%Y%m%d}"
    out.mkdir(parents=True, exist_ok=True)
    for wid in ids:
        r = cl.get(f"{BASE}/workflows/{wid}")
        if r.status_code >= 300:
            print(f"  backup {wid}: HTTP {r.status_code} — not backed up")
            continue
        w = r.json()
        (out / f"{wid}.json").write_text(
            json.dumps(w, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  backed up {wid}  {w['name'][:46]:48s} active={w['active']}")
    return out


def deploy(cl: httpx.Client, stem: str, activate: bool) -> None:
    wf = json.loads((WF_DIR / f"{stem}.json").read_text(encoding="utf-8"))
    stamped = 0
    for node in wf["nodes"]:
        if node["type"].endswith(".postgres"):
            node["credentials"] = {"postgres": dict(PG_CRED)}
            stamped += 1

    # The public API accepts exactly these four keys on write. Sending `id`,
    # `active`, `tags` or `versionId` is rejected outright, which is why the
    # payload is rebuilt rather than passed through.
    body = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": wf.get("settings", {"executionOrder": "v1"}),
    }
    wid = TARGETS[stem]
    r = (cl.put(f"{BASE}/workflows/{wid}", json=body) if wid
         else cl.post(f"{BASE}/workflows", json=body))
    if r.status_code >= 300:
        print(f"FAIL  {stem}: HTTP {r.status_code} {r.text[:300]}")
        return
    got = r.json()
    print(f"OK    {stem:34s} {'updated' if wid else 'created':8s} id={got['id']}"
          f"  nodes={len(got['nodes'])}  creds={stamped}  active={got.get('active')}")
    if not wid:
        print(f"      ^ new id — add it to TARGETS in {Path(__file__).name}")

    if activate:
        a = cl.post(f"{BASE}/workflows/{got['id']}/activate")
        print(f"      activate: {'ON' if a.status_code < 300 and a.json().get('active') else a.text[:160]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workflows", nargs="*", help="repo stems or prefixes, e.g. 01d 04")
    ap.add_argument("--activate", action="store_true", help="switch on after deploying")
    ap.add_argument("--list", action="store_true", help="show live workflows and ids")
    ap.add_argument("--backup-only", action="store_true")
    args = ap.parse_args()

    with client() as cl:
        if args.list:
            for w in sorted(cl.get(f"{BASE}/workflows", params={"limit": 250}).json()["data"],
                            key=lambda x: (not x["active"], x["name"])):
                print(f"  {'ON ' if w['active'] else 'off'}  {w['id']:22s} {w['name'][:60]}")
            return 0

        stems = [resolve(n) for n in args.workflows] or list(TARGETS)
        live = [TARGETS[s] for s in stems if TARGETS[s]]
        print(f"backup -> {backup(cl, live)}\n" if live else "")
        if args.backup_only:
            return 0
        for stem in stems:
            deploy(cl, stem, args.activate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
