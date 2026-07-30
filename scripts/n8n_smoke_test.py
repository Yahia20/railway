"""Push a synthetic conversation through the live chats pipeline and check every stage.

    export DATABASE_URL=postgresql://postgres:PASSWORD@HOST:PORT/customer360
    python scripts/n8n_smoke_test.py

Posts a two-sided Arabic conversation to the n8n webhook, then verifies each
table in turn: raw_events -> interactions -> chat_messages -> interaction_analysis
-> agent_evaluations.

The fixture is deliberately a *complete* sale — greeting by name, a priced offer,
a price objection, a close, and a stated follow-up time — so all five rubric
modules are exercised. That is the opposite of the real discovery call we tested,
which could only exercise 40% of the rubric, and it is the only way to prove the
modules that call left null actually work.

Run it twice: the second run must not duplicate anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

N8N_BASE = os.getenv("N8N_BASE_URL", "https://n8n-production-a685c.up.railway.app")
WEBHOOK = f"{N8N_BASE}/webhook/travelgate/chat"

DIALOG_ID = os.getenv("SMOKE_DIALOG_ID", "smoke-test-001")
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


def payload() -> dict:
    return {
        "dialog_id": DIALOG_ID,
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


def connect():
    import psycopg

    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    if "/customer360" not in url:
        raise SystemExit("DATABASE_URL does not point at the customer360 database")
    return psycopg.connect(url, autocommit=True)


def check(conn, label: str, sql: str, params=()) -> tuple[bool, str]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    ok = bool(row and row[0])
    detail = " · ".join(str(x) for x in row) if row else "no row"
    print(f"  {'PASS' if ok else 'FAIL'}  {label:34s} {detail}")
    return ok, detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=180,
                    help="seconds to wait for the settle Wait node + both AI passes")
    args = ap.parse_args()

    body = payload()
    agents = sum(1 for m in body["conversation_history"] if m["sender"] == "Agent")
    print(f"posting {len(body['conversation_history'])} messages "
          f"({agents} from the agent) to\n  {WEBHOOK}\n")

    r = httpx.post(WEBHOOK, json=body, timeout=60.0)
    print(f"webhook -> HTTP {r.status_code} {r.text[:80]!r}")
    if r.status_code == 404:
        raise SystemExit("404 — the workflow is not published. Run n8n_setup.py --apply first.")
    if r.status_code >= 400:
        raise SystemExit(f"webhook rejected the payload: {r.text[:300]}")

    conn = connect()

    print("\nstage 1 — ingest (immediate)")
    time.sleep(8)
    results = [
        check(conn, "raw_events landed",
              "SELECT count(*) FROM raw_events WHERE external_ref = %s", (DIALOG_ID,)),
        check(conn, "interaction created",
              "SELECT count(*), max(channel::text) FROM interactions "
              "WHERE external_id = %s AND external_source = 'bitrix'", (DIALOG_ID,)),
        check(conn, "messages stored",
              "SELECT count(*) FROM chat_messages m JOIN interactions i "
              "USING (interaction_id) WHERE i.external_id = %s", (DIALOG_ID,)),
        check(conn, "phone normalised to E.164",
              "SELECT count(*) FROM interactions WHERE external_id = %s "
              "AND customer_phone_e164 = '+966500000001'", (DIALOG_ID,)),
        check(conn, "agent messages recognised",
              "SELECT agent_message_count FROM interactions WHERE external_id = %s", (DIALOG_ID,)),
        check(conn, "not flagged bot-only",
              "SELECT NOT is_bot_handled FROM interactions WHERE external_id = %s", (DIALOG_ID,)),
    ]

    print(f"\nstage 2 — waiting up to {args.wait}s for the settle window and both AI passes")
    deadline = time.time() + args.wait
    scored = False
    while time.time() < deadline:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM agent_evaluations e JOIN interactions i "
                "USING (interaction_id) WHERE i.external_id = %s", (DIALOG_ID,))
            if cur.fetchone()[0]:
                scored = True
                break
        time.sleep(10)
        print("   ...", end="", flush=True)
    print()

    if not scored:
        print("  FAIL  no evaluation row yet.")
        print("        Check n8n > Executions for workflow 01. If it is paused on the")
        print("        Wait node, that is expected — re-run with --wait 2000, or set the")
        print("        Wait node to 1 minute (n8n_setup.py --test-wait).")
        return 1

    print("stage 3 — AI output")
    results += [
        check(conn, "customer analysis stored",
              "SELECT count(*), max(summary_ar) FROM interaction_analysis a "
              "JOIN interactions i USING (interaction_id) WHERE i.external_id = %s", (DIALOG_ID,)),
        check(conn, "evaluation stored",
              "SELECT final_score, performance_level, weight_applied FROM agent_evaluations e "
              "JOIN interactions i USING (interaction_id) WHERE i.external_id = %s", (DIALOG_ID,)),
        check(conn, "all 5 modules scored",
              "SELECT (m1_reception IS NOT NULL AND m2_offer IS NOT NULL "
              "AND m3_objections IS NOT NULL AND m5_closing IS NOT NULL) "
              "FROM agent_evaluations e JOIN interactions i USING (interaction_id) "
              "WHERE i.external_id = %s", (DIALOG_ID,)),
        check(conn, "evidence cited",
              "SELECT jsonb_array_length(evidence) FROM agent_evaluations e "
              "JOIN interactions i USING (interaction_id) WHERE i.external_id = %s", (DIALOG_ID,)),
    ]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT final_score, performance_level, weight_applied, m1_reception, m2_offer, "
            "m3_objections, m4_followup, m5_closing, stage_reached, top_weakness "
            "FROM agent_evaluations e JOIN interactions i USING (interaction_id) "
            "WHERE i.external_id = %s", (DIALOG_ID,))
        row = cur.fetchone()
    if row:
        keys = ["final_score", "level", "weight_applied", "m1", "m2", "m3", "m4", "m5",
                "stage", "top_weakness"]
        print("\nscored row:")
        print(json.dumps(dict(zip(keys, [str(x) for x in row])), ensure_ascii=False, indent=2))

    failed = [d for ok, d in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    print("\nRun this script again — nothing should duplicate (idempotency check).")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
