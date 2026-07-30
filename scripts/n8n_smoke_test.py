"""Push a synthetic conversation through the live chats pipeline and verify it.

    export N8N_API_KEY=...
    python scripts/n8n_smoke_test.py

Verification goes through n8n's own execution record rather than a direct database
connection, because Postgres is on Railway's private network with public
networking OFF — which is how it should stay. n8n runs inside that network and
reports every node's input and output, so the execution log is both sufficient
and more honest: it shows what the pipeline actually did, not what the database
happens to contain.

The fixture is deliberately a COMPLETE sale — greeting by name, a priced offer
with hotel, dates and cancellation terms, a price objection with a competitor
comparison, a payment request, and a stated follow-up time. The real call we
tested was a discovery call that could only exercise 40% of the rubric, so it
proved nothing about modules 3, 4 and 5. This one exercises all five.
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
WEBHOOK = f"{N8N_BASE}/webhook/travelgate/chat"
WORKFLOW_ID = os.getenv("N8N_WORKFLOW_ID", "i6VM7qxmbYEDebDx")

START = datetime(2026, 7, 30, 10, 0, tzinfo=timezone(timedelta(hours=3)))

# (sender, text, minutes_after_start)
SCRIPT = [
    ("Customer", "السلام عليكم، عندكم عروض لتركيا لشهر أغسطس؟", 0),
    ("Agent",    "وعليكم السلام أستاذ أحمد، معك خالد من ترافل جيت. أهلاً بك! نعم عندنا عروض ممتازة لتركيا. كم عدد المسافرين ومتى تحبون السفر بالتحديد؟", 2),
    ("Customer", "أنا وزوجتي وطفلين، من ١٠ أغسطس لمدة أسبوع", 5),
    ("Agent",    "تمام أستاذ أحمد. عندي لك عرض عائلي ممتاز: فندق Ramada Merter ٤ نجوم في إسطنبول، ٧ ليالٍ من ١٠ إلى ١٧ أغسطس، شامل الإفطار والعشاء، مع استقبال من المطار وجولتين سياحيتين. السعر الإجمالي ١٨٥٠٠ ريال لكم الأربعة. الفندق قريب من المترو وفيه مسبح للأطفال، مناسب جداً لرحلة عائلية. الحجز يحتاج دفعة ٣٠٪ والإلغاء مجاني حتى ٧ أيام قبل السفر.", 9),
    ("Customer", "الصراحة السعر غالي شوي، لقيت عرض بـ١٥٠٠٠ من مكتب ثاني", 14),
    ("Agent",    "أفهمك أستاذ أحمد. ممكن تخبرني إيش يشمل العرض الثاني؟ لأن عرضنا يشمل الإفطار والعشاء والاستقبال وجولتين — لو حسبنا الوجبات لوحدها للأربعة سبعة أيام تقريباً ٢٥٠٠ ريال. وفندقنا ٤ نجوم مع مسبح أطفال. بس أقدر أنزل لك السعر إلى ١٧٢٠٠ ريال كعرض خاص، وأضيف لكم توصيل مجاني للمولات.", 17),
    ("Customer", "طيب معقول، خلينا نكمل", 22),
    ("Agent",    "ممتاز أستاذ أحمد! نحتاج دفعة ٥١٦٠ ريال لتأكيد الحجز، تقدر تحولها على الحساب أو عن طريق الرابط اللي أرسله لك الآن. بعد الدفع مباشرة أرسل لك تأكيد الفندق وتذاكر الطيران على الواتساب خلال ساعتين، وقبل السفر بيومين أرسل لك كل الفواتير والبرنامج التفصيلي.", 24),
    ("Customer", "تم التحويل", 31),
    ("Agent",    "وصلني التحويل، شكراً جزيلاً أستاذ أحمد وأهلاً وسهلاً بك في ترافل جيت! الحجز مؤكد الآن. سأرسل لك التأكيد والتذاكر خلال ساعتين كما وعدتك. وأتمنى بعد رحلتكم تقيّم خدمتنا وتشاركنا رأيك، يهمنا جداً.", 33),
]

EXPECTED_NODES = [
    "Bitrix webhook", "Land raw (idempotent)", "New payload?",
    "Parse & compute metrics", "Upsert interaction", "Insert messages (dedup)",
    "Human agent involved?", "Wait for thread to settle",
    "Newer messages arrived?", "Still the latest?", "Two AI passes",
    "Store customer analysis", "Store evaluation",
]


def payload(dialog_id: str) -> dict:
    return {
        "dialog_id": dialog_id,
        "crm_entity_type": "DEAL",
        "crm_entity_id": "99001",
        "contact_id": "99002",
        "phone": "+966500000001",
        "conversation_history": [
            {
                "sender": sender,
                "sender_id": "912" if sender == "Agent" else "4130",
                "message": text,
                "timestamp": (START + timedelta(minutes=mins)).isoformat(),
            }
            for sender, text, mins in SCRIPT
        ],
        "deal_info": {
            "ID": "99001",
            "TITLE": "Smoke test — Ahmed family Turkey",
            "SOURCE_ID": "54|WHATSAPP",
            "STAGE_ID": "C86:EXECUTING",
            "CURRENCY_ID": "SAR",
            "OPPORTUNITY": "17200.00",
            "ASSIGNED_BY_ID": "912",
        },
    }


class N8N:
    def __init__(self):
        key = os.getenv("N8N_API_KEY")
        if not key:
            raise SystemExit("N8N_API_KEY is not set")
        self.c = httpx.Client(base_url=N8N_BASE.rstrip("/") + "/api/v1",
                              headers={"X-N8N-API-KEY": key}, timeout=90.0)

    def latest_execution(self) -> dict | None:
        r = self.c.get("/executions",
                       params={"workflowId": WORKFLOW_ID, "limit": 1, "includeData": "true"})
        r.raise_for_status()
        data = r.json().get("data") or []
        return data[0] if data else None

    def execution(self, eid) -> dict:
        r = self.c.get(f"/executions/{eid}", params={"includeData": "true"})
        r.raise_for_status()
        return r.json()


def node_states(execution: dict) -> tuple[dict[str, str], dict]:
    result = (execution.get("data") or {}).get("resultData") or {}
    run = result.get("runData") or {}
    states, outputs = {}, {}
    for name, runs in run.items():
        first = runs[0] if runs else {}
        if first.get("error"):
            states[name] = "ERROR: " + str(first["error"].get("message"))[:160]
        else:
            states[name] = "ok"
            try:
                outputs[name] = first["data"]["main"][0][0]["json"]
            except (KeyError, IndexError, TypeError):
                pass
    if result.get("error"):
        err = result["error"]
        states.setdefault(err.get("node", {}).get("name", "?"),
                          "ERROR: " + str(err.get("message"))[:160])
    return states, outputs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=420, help="seconds to wait for completion")
    ap.add_argument("--dialog-id", default=None)
    args = ap.parse_args()

    dialog_id = args.dialog_id or f"smoke-{uuid.uuid4().hex[:8]}"
    body = payload(dialog_id)
    agents = sum(1 for m in body["conversation_history"] if m["sender"] == "Agent")

    print(f"dialog_id : {dialog_id}")
    print(f"messages  : {len(body['conversation_history'])} ({agents} from the agent)")
    print(f"posting to: {WEBHOOK}\n")

    n8n = N8N()
    before = n8n.latest_execution()
    before_id = before["id"] if before else None

    r = httpx.post(WEBHOOK, json=body, timeout=90.0)
    print(f"webhook -> HTTP {r.status_code} {r.text[:60]!r}")
    if r.status_code == 404:
        raise SystemExit("404 — workflow not published. Run n8n_setup.py --apply first.")
    r.raise_for_status()

    print(f"\nwaiting up to {args.wait}s for the execution to finish "
          "(1-minute settle window + two DeepSeek calls)")

    eid, deadline = None, time.time() + args.wait
    seen_states: dict[str, str] = {}
    while time.time() < deadline:
        latest = n8n.latest_execution()
        if latest and latest["id"] != before_id:
            eid = latest["id"]
            ex = n8n.execution(eid)
            states, outputs = node_states(ex)
            for name, st in states.items():
                if seen_states.get(name) != st:
                    mark = "  ok  " if st == "ok" else "  FAIL"
                    print(f"{mark} {name}" + ("" if st == "ok" else f"  :: {st[7:]}"))
                    seen_states[name] = st
            status = ex.get("status")
            if status in ("success", "error", "crashed", "canceled"):
                return report(ex, status, dialog_id)
        time.sleep(10)

    print("\ntimed out. Current execution status:", (n8n.execution(eid).get("status") if eid else "none"))
    print("If it is 'waiting', the Wait node has not resumed yet — re-run with --wait 2000.")
    return 1


def report(ex: dict, status: str, dialog_id: str) -> int:
    states, outputs = node_states(ex)
    print(f"\nexecution {ex.get('id')} finished: {status}\n")

    reached = [n for n in EXPECTED_NODES if n in states]
    missing = [n for n in EXPECTED_NODES if n not in states]
    failed = {n: s for n, s in states.items() if s != "ok"}

    print(f"nodes run   : {len(reached)}/{len(EXPECTED_NODES)}")
    if missing:
        print(f"not reached : {', '.join(missing)}")
    if failed:
        print("\nfailures:")
        for n, s in failed.items():
            print(f"  {n}: {s}")

    evaluate = outputs.get("Two AI passes")
    if evaluate and isinstance(evaluate.get("pass2"), dict):
        p2 = evaluate["pass2"]
        print("\n=== SCORED ===")
        print(f"  final_score      : {p2.get('final_score')}")
        print(f"  performance_level: {p2.get('performance_level')}")
        print(f"  weight_applied   : {p2.get('weight_applied')}")
        for k, v in (p2.get("modules") or {}).items():
            print(f"    {k:24s} {v}")
        summary = (p2.get("payload") or {}).get("summary") or {}
        if summary:
            print("\n  coaching:")
            for k, v in summary.items():
                print(f"    {k}: {v}")
        for w in p2.get("warnings") or []:
            print(f"  ! {w}")
        p1 = (evaluate.get("pass1") or {}).get("payload") or {}
        if p1:
            print(f"\n  customer: {(p1.get('customer') or {}).get('name')} · "
                  f"{p1.get('service')} · stage={(p1.get('commercial') or {}).get('buying_stage')}")

    ok = status == "success" and not failed
    print(f"\n{'PASS' if ok else 'FAIL'} — dialog_id {dialog_id}")
    if ok:
        print("Re-run to confirm idempotency: the second run must not duplicate rows.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
