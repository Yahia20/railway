"""Wire up n8n workflow 01 with no clicking: credentials, nodes, activation.

    export N8N_API_KEY=...        # n8n > Settings > n8n API > Create an API key
    export PGPASSWORD=...         # Railway > postgres > Variables
    export WORKER_API_KEY=...     # Railway > worker service > Variables
    python scripts/n8n_setup.py --apply --test-wait

Without --apply it reports what it would do and changes nothing.

Why a script rather than the UI: n8n binds credentials to nodes by internal ID,
and an imported workflow JSON can only carry a placeholder. Every import
therefore leaves every node with a broken credential reference that must be
re-picked by hand. Creating the credentials through the API returns their real
IDs, which can be written straight onto the nodes — repeatable, and it edits the
workflow that is already there rather than adding another copy.

Note on the n8n public API: it can create credentials but cannot LIST them, so
this creates its own rather than trying to find yours. n8n allows duplicate
credential names; delete the hand-made ones afterwards if you prefer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_FILE = ROOT / "n8n" / "workflows" / "01-chats-ingest-evaluate.json"

N8N_BASE = os.getenv("N8N_BASE_URL", "https://n8n-production-a685c.up.railway.app")
WORKER_INTERNAL = os.getenv("WORKER_URL", "http://railway-15c718f0.railway.internal:8000")

PG_CRED_NAME = "railway-pg (api)"
HDR_CRED_NAME = "worker-api (api)"


class N8N:
    def __init__(self, base: str, api_key: str):
        self.c = httpx.Client(
            base_url=base.rstrip("/") + "/api/v1",
            headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=60.0,
        )

    def _check(self, r: httpx.Response):
        if r.status_code >= 400:
            raise SystemExit(
                f"n8n {r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:400]}"
            )
        return r.json() if r.content else {}

    def workflows(self) -> list[dict]:
        return self._check(self.c.get("/workflows", params={"limit": 250})).get("data", [])

    def workflow(self, wid: str) -> dict:
        return self._check(self.c.get(f"/workflows/{wid}"))

    def update_workflow(self, wid: str, body: dict) -> dict:
        return self._check(self.c.put(f"/workflows/{wid}", json=body))

    def activate(self, wid: str) -> dict:
        return self._check(self.c.post(f"/workflows/{wid}/activate"))

    def create_credential(self, name: str, ctype: str, data: dict) -> dict:
        return self._check(
            self.c.post("/credentials", json={"name": name, "type": ctype, "data": data})
        )


def build_nodes(local: dict, cred_ids: dict[str, dict], test_wait: bool) -> list[dict]:
    """Take the committed workflow and stamp the real credential IDs onto it."""
    nodes = json.loads(json.dumps(local["nodes"]))          # deep copy
    stamped = 0
    for node in nodes:
        creds = node.get("credentials") or {}
        for ctype in list(creds):
            if ctype in cred_ids:
                creds[ctype] = cred_ids[ctype]
                stamped += 1
        # Keep HTTP nodes pointed at the configured worker address.
        if node["type"] == "n8n-nodes-base.httpRequest":
            url = node["parameters"].get("url", "")
            if ":8000" in url:
                node["parameters"]["url"] = WORKER_INTERNAL.rstrip("/") + url.split(":8000", 1)[1]
        if test_wait and node["type"] == "n8n-nodes-base.wait":
            node["parameters"] = {"amount": 1, "unit": "minutes"}
    print(f"   stamped {stamped} credential references across {len(nodes)} nodes")
    return nodes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--test-wait", action="store_true",
                    help="set the settle Wait node to 1 minute for a first run")
    ap.add_argument("--workflow-name", default="01 · Chats — ingest & evaluate")
    args = ap.parse_args()

    missing = [v for v in ("N8N_API_KEY", "PGPASSWORD", "WORKER_API_KEY") if not os.getenv(v)]
    if missing:
        raise SystemExit("not set: " + ", ".join(missing))

    local = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    n8n = N8N(N8N_BASE, os.environ["N8N_API_KEY"])

    print(f"n8n     {N8N_BASE}")
    print(f"worker  {WORKER_INTERNAL}\n")

    print("1. locating the workflow")
    all_wf = n8n.workflows()
    matches = [w for w in all_wf if w["name"].strip() == args.workflow_name.strip()]
    if not matches:
        raise SystemExit(
            f"no workflow named {args.workflow_name!r}.\nFound: {[w['name'] for w in all_wf]}"
        )
    wf = matches[0]
    print(f"   {wf['name']}   id={wf['id']}   active={wf.get('active')}")

    if not args.apply:
        print(f"\ndry run — would create 2 credentials, rewrite {len(local['nodes'])} "
              "nodes and activate. Re-run with --apply")
        return 0

    print("\n2. creating credentials")
    pg = n8n.create_credential(PG_CRED_NAME, "postgres", {
        "host": "postgres.railway.internal",
        "port": 5432,
        # NOT 'railway': n8n owns railway/public and already has an `agents`
        # table that would collide with ours.
        "database": "customer360",
        "user": "postgres",
        "password": os.environ["PGPASSWORD"],
        "ssl": "disable",
        "allowUnauthorizedCerts": False,
    })
    print(f"   postgres        id={pg['id']}   database=customer360")

    hdr = n8n.create_credential(HDR_CRED_NAME, "httpHeaderAuth", {
        "name": "X-API-Key",
        "value": os.environ["WORKER_API_KEY"],
    })
    print(f"   httpHeaderAuth  id={hdr['id']}")

    cred_ids = {
        "postgres": {"id": pg["id"], "name": PG_CRED_NAME},
        "httpHeaderAuth": {"id": hdr["id"], "name": HDR_CRED_NAME},
    }

    print("\n3. rewriting the workflow in place")
    nodes = build_nodes(local, cred_ids, args.test_wait)
    if args.test_wait:
        print("   Wait node set to 1 minute (put it back to 30 after testing)")

    n8n.update_workflow(wf["id"], {
        "name": local["name"],
        "nodes": nodes,
        "connections": local["connections"],
        "settings": local.get("settings", {"executionOrder": "v1"}),
    })
    print("   updated")

    print("\n4. activating")
    n8n.activate(wf["id"])
    print(f"   active={n8n.workflow(wf['id']).get('active')}")

    print(f"\nwebhook live at  {N8N_BASE}/webhook/travelgate/chat")
    print("next:  python scripts/n8n_smoke_test.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
