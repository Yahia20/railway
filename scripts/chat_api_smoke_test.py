"""Prove workflow 01c stores the production chat payload, and stores it once.

    export N8N_API_KEY=...
    python scripts/chat_api_smoke_test.py

Posts the SAME batch twice, in the exact shape the production API sends: a flat
array of message rows, each one repeating dealid, crm_entity_id, contact_id,
created_at and updated_at. Then reads both executions out of n8n and checks

    delivery 1   raw is_new=true    messages_inserted = N
    delivery 2   raw is_new=false   messages_inserted = 0
    after both   message_count      = N          (not 2N)

The second delivery is the whole point. A pipeline that stores correctly once
and doubles on redelivery looks perfect in a single test.

Verification goes through n8n's own execution record rather than a direct
database connection, because Postgres is on Railway's private network with
public networking OFF — which is how it should stay. n8n runs inside that
network and reports every node's output, so the execution log shows what the
pipeline actually did, not what the database happens to contain.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

N8N_BASE = os.getenv("N8N_BASE_URL", "https://n8n-production-a685c.up.railway.app")
WEBHOOK = f"{N8N_BASE}/webhook/travelgate/chat-message"
WORKFLOW_NAME = "01c · Chats — store only (production API)"

RIYADH = timezone(timedelta(hours=3))
START = datetime(2026, 8, 20, 19, 28, 55, tzinfo=RIYADH)

# (sender_role, message, minutes_after_start)
SCRIPT = [
    ("Customer", "السلام عليكم، ممكن تفاصيل برنامج شرم الشيخ؟", 0),
    ("Agent", "وعليكم السلام، أهلاً بحضرتك. حاضر أبعتلك البرنامج كامل خلال دقايق.", 4),
    ("Customer", "تمام، في انتظارك", 6),
    ("Agent", "اتفضل استاذي الكريم البرنامج وفي انتظار رد حضرتك الكريم ان شاء الله", 41),
]

WEBHOOK_NODE = "Chat API webhook"
NORMALIZE_NODE = "Normalize and group"
RAW_NODE = "Land raw request"
MESSAGES_NODE = "Insert messages (dedup)"
COUNTERS_NODE = "Renumber and refresh counters"
PROCESSED_NODE = "Mark raw processed"

EXPECTED_NODES = [
    WEBHOOK_NODE, NORMALIZE_NODE, RAW_NODE, "Anything storable?",
    "Upsert conversations", MESSAGES_NODE, COUNTERS_NODE, PROCESSED_NODE,
]


def payload(conversation_id: str, deal_id: str, contact_id: str) -> list[dict]:
    """The production shape: a flat array, metadata repeated on every row."""
    updated = (START + timedelta(minutes=SCRIPT[-1][2])).isoformat()
    return [
        {
            "message": text,
            "dealid": deal_id,
            "crm_entity_id": deal_id,
            "contact_id": contact_id,
            "created_at": START.isoformat(),
            "updated_at": updated,
            "conversation_id": conversation_id,
            "sender_id": "86" if role == "Agent" else contact_id,
            "timestamp": (START + timedelta(minutes=mins)).isoformat(),
            "sender_role": role,
            "content_type": "text",
        }
        for role, text, mins in SCRIPT
    ]


class N8N:
    def __init__(self):
        key = os.getenv("N8N_API_KEY")
        if not key:
            raise SystemExit("N8N_API_KEY is not set")
        self.c = httpx.Client(base_url=N8N_BASE.rstrip("/") + "/api/v1",
                              headers={"X-N8N-API-KEY": key}, timeout=90.0)

    def workflow_id(self, name: str) -> str:
        r = self.c.get("/workflows", params={"limit": 250})
        r.raise_for_status()
        for w in r.json().get("data", []):
            if w["name"].strip() == name.strip():
                if not w.get("active"):
                    raise SystemExit(
                        f"{name!r} exists but is NOT active — n8n will not register its\n"
                        "webhook. Run: python scripts/n8n_deploy_chat_store.py --apply"
                    )
                return w["id"]
        raise SystemExit(
            f"no workflow named {name!r}.\n"
            "Run: python scripts/n8n_deploy_chat_store.py --apply"
        )

    def execution(self, eid) -> dict:
        r = self.c.get(f"/executions/{eid}", params={"includeData": "true"})
        r.raise_for_status()
        return r.json()

    def executions(self, workflow_id: str, limit: int = 20) -> list[dict]:
        r = self.c.get("/executions", params={"workflowId": workflow_id, "limit": limit})
        r.raise_for_status()
        return r.json().get("data") or []

    def find(self, workflow_id: str, conversation_id: str, skip: set) -> dict | None:
        """Match on OUR conversation_id, never on 'the most recent execution'.

        Anyone else testing the same webhook produces executions too, and
        latching onto the newest one reports someone else's run as ours.
        """
        for row in self.executions(workflow_id):
            if row["id"] in skip:
                continue
            ex = self.execution(row["id"])
            hook = ((ex.get("data") or {}).get("resultData", {})
                    .get("runData", {}).get(WEBHOOK_NODE))
            if not hook:
                continue
            try:
                body = hook[0]["data"]["main"][0][0]["json"]["body"]
            except (KeyError, IndexError, TypeError):
                continue
            rows = body if isinstance(body, list) else [body]
            if any(r.get("conversation_id") == conversation_id for r in rows if isinstance(r, dict)):
                return ex
        return None


def node_outputs(execution: dict) -> tuple[dict[str, str], dict]:
    result = (execution.get("data") or {}).get("resultData") or {}
    run = result.get("runData") or {}
    states, outputs = {}, {}
    for name, runs in run.items():
        first = runs[0] if runs else {}
        if first.get("error"):
            states[name] = "ERROR: " + str(first["error"].get("message"))[:200]
            continue
        states[name] = "ok"
        try:
            outputs[name] = first["data"]["main"][0][0]["json"]
        except (KeyError, IndexError, TypeError):
            outputs[name] = None
    if result.get("error"):
        err = result["error"]
        states.setdefault(err.get("node", {}).get("name", "?"),
                          "ERROR: " + str(err.get("message"))[:200])
    return states, outputs


def deliver(n8n: N8N, workflow_id: str, body: list[dict], conversation_id: str,
            seen: set, wait: int, label: str) -> tuple[dict[str, str], dict]:
    print(f"\n--- {label} ---")
    r = httpx.post(WEBHOOK, json=body, timeout=60.0)
    print(f"POST {WEBHOOK} -> HTTP {r.status_code}")
    if r.status_code == 404:
        raise SystemExit(
            "404 — nothing is registered on this path.\n"
            "Run: python scripts/n8n_deploy_chat_store.py --apply"
        )
    r.raise_for_status()

    deadline = time.time() + wait
    while time.time() < deadline:
        ex = n8n.find(workflow_id, conversation_id, seen)
        if ex and ex.get("finished"):
            seen.add(ex["id"])
            states, outputs = node_outputs(ex)
            print(f"execution {ex['id']}  status={ex.get('status')}")
            for name in EXPECTED_NODES:
                print(f"   {name:<32} {states.get(name, 'did not run')}")
            return states, outputs
        time.sleep(3)
    raise SystemExit(f"no finished execution for conversation {conversation_id} within {wait}s")


def check(label: str, actual, expected) -> bool:
    ok = actual == expected
    print(f"   [{'PASS' if ok else 'FAIL'}] {label}: got {actual!r}, expected {expected!r}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=90, help="seconds to wait per delivery")
    args = ap.parse_args()

    conversation_id = str(uuid.uuid4())
    deal_id = f"99{uuid.uuid4().int % 10000:04d}"
    contact_id = f"88{uuid.uuid4().int % 10000:04d}"
    body = payload(conversation_id, deal_id, contact_id)

    print(f"conversation_id : {conversation_id}")
    print(f"dealid          : {deal_id}   (repeated on all {len(body)} rows)")
    print(f"contact_id      : {contact_id} (repeated on all {len(body)} rows)")
    print(f"messages        : {len(body)}")

    n8n = N8N()
    workflow_id = n8n.workflow_id(WORKFLOW_NAME)
    print(f"workflow id     : {workflow_id}")

    seen: set = set()
    _, first = deliver(n8n, workflow_id, body, conversation_id, seen, args.wait,
                       "delivery 1 of 2 (new)")
    _, second = deliver(n8n, workflow_id, body, conversation_id, seen, args.wait,
                        "delivery 2 of 2 (identical redelivery)")

    n = len(body)
    print("\n--- assertions ---")
    results = [
        check("delivery 1 landed a new raw_events row",
              (first.get(RAW_NODE) or {}).get("is_new"), True),
        check("delivery 1 grouped into one conversation",
              ((first.get(NORMALIZE_NODE) or {}).get("stats") or {}).get("conversations"), 1),
        check("delivery 1 inserted every message",
              (first.get(MESSAGES_NODE) or {}).get("messages_inserted"), n),
        check("delivery 2 landed NO new raw_events row",
              (second.get(RAW_NODE) or {}).get("is_new"), False),
        check("delivery 2 inserted no messages",
              (second.get(MESSAGES_NODE) or {}).get("messages_inserted"), 0),
        check("message_count is N, not 2N",
              int((second.get(COUNTERS_NODE) or {}).get("message_count", -1)), n),
    ]

    counters = second.get(COUNTERS_NODE) or {}
    print("\nstored conversation:")
    for key in ("external_id", "external_deal_id", "external_contact_id",
                "message_count", "customer_message_count", "agent_message_count",
                "started_at", "ended_at"):
        print(f"   {key:<24} {counters.get(key)}")

    print()
    if all(results):
        print("PASS — the batch is stored once, and a redelivery changes nothing.")
        return 0
    print("FAIL — see the assertions above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
