#!/usr/bin/env python3
"""Does this Bitrix inbound webhook actually let workflow 04 do its job?

`app.sources.bitrix_chats --probe` predates workflow 04 and tests the chat-pull
methods (`imopenlines.*`, `im.dialog.*`). Workflow 04 uses neither. It calls
`crm.deal.list` and then `crm.contact.list`, and it is the only path that fills
`deals` and — through the phone on the contact — `customers`. A webhook can
exist, answer `profile.json` happily, and still be useless to 04 because the
CRM scope was never ticked when it was created.

That is a silent failure: n8n's HTTP node gets a 200 with an `error` key in the
body, and the workflow carries on with nothing.

    export BITRIX_PORTAL_DOMAIN=travelgate.bitrix24.ae
    export BITRIX_WEBHOOK_USER_ID=128
    export BITRIX_WEBHOOK_TOKEN=...
    python scripts/bitrix_probe.py

Prints nothing secret: the token is replaced with its length everywhere it
would otherwise appear, so the output is safe to paste into a ticket.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

# What workflow 04 needs, and what it needs it for. Ordered by the chain: a
# deal gives CONTACT_ID, the contact gives the phone, the phone is the only
# thing workflow 03 matches a customer on.
REQUIRED = [
    ("profile", {}, "the webhook itself works at all"),
    ("crm.deal.list", {"start": 0}, "04 'Fetch Bitrix deals' -> deals table"),
    ("crm.contact.list", {"start": 0}, "04 'Fetch Bitrix contacts' -> the phone"),
]
OPTIONAL = [
    ("scope", {}, "which permission groups this webhook was granted"),
    ("crm.deal.fields", {}, "field names, for DEAL_FIELD_ALLOWLIST"),
    ("user.get", {}, "agent names"),
]


def main() -> int:
    domain = os.getenv("BITRIX_PORTAL_DOMAIN", "").strip()
    user_id = os.getenv("BITRIX_WEBHOOK_USER_ID", "1").strip()
    token = os.getenv("BITRIX_WEBHOOK_TOKEN", "").strip()

    missing = [n for n, v in (("BITRIX_PORTAL_DOMAIN", domain),
                              ("BITRIX_WEBHOOK_TOKEN", token)) if not v]
    if missing:
        print("not set: " + ", ".join(missing), file=sys.stderr)
        return 2

    base = f"https://{domain}/rest/{user_id}/{token}"
    safe_base = f"https://{domain}/rest/{user_id}/<{len(token)}-char token>"
    print(f"portal : {safe_base}\n")

    failures = 0
    with httpx.Client(timeout=30.0) as client:
        for group, methods in (("REQUIRED", REQUIRED), ("OPTIONAL", OPTIONAL)):
            print(group)
            for method, params, why in methods:
                try:
                    r = client.post(f"{base}/{method}.json", json=params)
                    body = r.json()
                except Exception as exc:
                    print(f"  FAIL {method:22} transport: {type(exc).__name__}: {exc}")
                    failures += group == "REQUIRED"
                    continue

                if "error" in body:
                    err = body.get("error_description") or body["error"]
                    print(f"  FAIL {method:22} {body['error']}: {err}")
                    print(f"       ^ needed for: {why}")
                    failures += group == "REQUIRED"
                    continue

                result = body.get("result")
                if method == "scope":
                    print(f"  OK   {method:22} {', '.join(sorted(result or []))}")
                elif isinstance(result, list):
                    total = body.get("total", len(result))
                    keys = sorted(result[0])[:6] if result else []
                    print(f"  OK   {method:22} total={total} page={len(result)} "
                          f"keys[{', '.join(keys)}]")
                elif isinstance(result, dict):
                    print(f"  OK   {method:22} {len(result)} field(s)")
                else:
                    print(f"  OK   {method:22} {json.dumps(result)[:80]}")
            print()

    if failures:
        print(f"{failures} REQUIRED method(s) unavailable.\n"
              "If they say ACCESS_DENIED or 'Method not found', the webhook was\n"
              "created without the CRM scope. Bitrix does not let you add a scope\n"
              "to an existing webhook silently — open it, tick CRM under 'Assign\n"
              "permissions', and save. Workflow 04 writes nothing until this passes.")
        return 1

    print("every method workflow 04 needs is available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
