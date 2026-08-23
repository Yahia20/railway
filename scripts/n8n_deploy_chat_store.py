"""Deploy and activate workflow 01c — the store-only chat ingest.

    export N8N_API_KEY=...        # n8n > Settings > n8n API > Create an API key
    export PGPASSWORD=...         # Railway > postgres > Variables
    python scripts/n8n_deploy_chat_store.py --apply

Without --apply it reports what it would do and changes nothing.

This is the fix for

    The requested webhook "POST travelgate/chat-message" is not registered.
    The workflow must be active for a production URL to run successfully.

That error does not mean the URL is wrong. It means no ACTIVE workflow in n8n
owns that path — the workflow exists in this repo and had never been imported.
n8n registers a production webhook only while its workflow is active, so
importing without activating leaves the sender with the same message.

Only one active workflow may own a path. 01b claims the same one and is a
different pipeline (it scores as well as stores), so this script refuses to
activate over an active 01b unless --take-over says to deactivate it first.

Why a script rather than the UI: n8n binds credentials to nodes by internal ID,
and an imported workflow JSON can only carry a placeholder, so every import
leaves every Postgres node broken. Creating the credential through the API
returns its real ID, which is written straight onto the nodes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_FILE = ROOT / "n8n" / "workflows" / "01c-chats-store-only.json"

N8N_BASE = os.getenv("N8N_BASE_URL", "https://n8n-production-a685c.up.railway.app")
WEBHOOK_PATH = "travelgate/chat-message"
PG_CRED_NAME = "railway-pg (01c)"


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

    def create_workflow(self, body: dict) -> dict:
        return self._check(self.c.post("/workflows", json=body))

    def update_workflow(self, wid: str, body: dict) -> dict:
        return self._check(self.c.put(f"/workflows/{wid}", json=body))

    def activate(self, wid: str) -> dict:
        return self._check(self.c.post(f"/workflows/{wid}/activate"))

    def deactivate(self, wid: str) -> dict:
        return self._check(self.c.post(f"/workflows/{wid}/deactivate"))

    def create_credential(self, name: str, ctype: str, data: dict) -> dict:
        return self._check(
            self.c.post("/credentials", json={"name": name, "type": ctype, "data": data})
        )


def webhook_paths(workflow: dict) -> set[str]:
    return {
        str(n.get("parameters", {}).get("path", "")).strip("/")
        for n in workflow.get("nodes") or []
        if n.get("type") == "n8n-nodes-base.webhook"
    }


def stamp_credentials(nodes: list[dict], pg_cred: dict) -> list[dict]:
    """Replace the placeholder credential reference with the real one."""
    nodes = json.loads(json.dumps(nodes))          # deep copy
    stamped = 0
    for node in nodes:
        if "postgres" in (node.get("credentials") or {}):
            node["credentials"]["postgres"] = pg_cred
            stamped += 1
    print(f"   stamped the Postgres credential onto {stamped} of {len(nodes)} nodes")
    return nodes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--take-over", action="store_true",
                    help=f"deactivate any other workflow already serving {WEBHOOK_PATH}")
    ap.add_argument("--credential-id",
                    help="reuse an existing n8n Postgres credential instead of creating one "
                         "(the public API cannot list credentials, so it cannot find yours)")
    args = ap.parse_args()

    if not os.getenv("N8N_API_KEY"):
        raise SystemExit("N8N_API_KEY is not set")
    if not (args.credential_id or os.getenv("PGPASSWORD")):
        raise SystemExit("set PGPASSWORD, or pass --credential-id to reuse an existing one")

    local = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    n8n = N8N(N8N_BASE, os.environ["N8N_API_KEY"])

    print(f"n8n       {N8N_BASE}")
    print(f"workflow  {local['name']}")
    print(f"path      {WEBHOOK_PATH}\n")

    print("1. checking who owns the webhook path")
    existing = None
    conflicts = []
    for summary in n8n.workflows():
        full = summary if summary.get("nodes") else n8n.workflow(summary["id"])
        if full["name"].strip() == local["name"].strip():
            existing = full
        elif WEBHOOK_PATH in webhook_paths(full):
            conflicts.append(full)

    for w in conflicts:
        state = "ACTIVE" if w.get("active") else "inactive"
        print(f"   also claims it: {w['name']!r} ({state})")
    blocking = [w for w in conflicts if w.get("active")]
    if blocking and not args.take_over:
        raise SystemExit(
            "\n".join(
                [f"\n{len(blocking)} active workflow(s) already serve /{WEBHOOK_PATH}:"]
                + [f"  - {w['name']} (id={w['id']})" for w in blocking]
                + ["", "n8n registers a path to one active workflow only. Re-run with",
                   "--take-over to deactivate them, or change this workflow's path."]
            )
        )
    if existing:
        print(f"   found existing: id={existing['id']} active={existing.get('active')}")
    else:
        print("   not deployed yet — will be created")

    if not args.apply:
        print(f"\ndry run — would {'update' if existing else 'create'} "
              f"{len(local['nodes'])} nodes and activate. Re-run with --apply")
        return 0

    print("\n2. credential")
    if args.credential_id:
        pg_cred = {"id": args.credential_id, "name": PG_CRED_NAME}
        print(f"   reusing id={args.credential_id}")
    else:
        created = n8n.create_credential(PG_CRED_NAME, "postgres", {
            "host": "postgres.railway.internal",
            "port": 5432,
            # NOT 'railway': n8n owns railway/public and already has an `agents`
            # table of its own that ours would collide with.
            "database": "customer360",
            "user": "postgres",
            "password": os.environ["PGPASSWORD"],
            "ssl": "disable",
            "allowUnauthorizedCerts": False,
        })
        pg_cred = {"id": created["id"], "name": PG_CRED_NAME}
        print(f"   created id={created['id']}  database=customer360")

    body = {
        "name": local["name"],
        "nodes": stamp_credentials(local["nodes"], pg_cred),
        "connections": local["connections"],
        "settings": local.get("settings", {"executionOrder": "v1"}),
    }

    print("\n3. writing the workflow")
    if existing:
        wf = n8n.update_workflow(existing["id"], body)
        print(f"   updated id={wf.get('id', existing['id'])}")
        wid = existing["id"]
    else:
        wf = n8n.create_workflow(body)
        wid = wf["id"]
        print(f"   created id={wid}")

    if blocking:
        print("\n4. releasing the path")
        for w in blocking:
            n8n.deactivate(w["id"])
            print(f"   deactivated {w['name']!r}")

    print("\n5. activating")
    n8n.activate(wid)
    active = n8n.workflow(wid).get("active")
    print(f"   active={active}")
    if not active:
        raise SystemExit("n8n accepted the activate call but the workflow is not active")

    print(f"\nwebhook live at  {N8N_BASE}/webhook/{WEBHOOK_PATH}")
    print("verify:  python scripts/chat_api_smoke_test.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
