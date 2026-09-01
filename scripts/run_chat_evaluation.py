#!/usr/bin/env python3
"""Run workflow 01d's pipeline for a bounded number of threads.

WHY THIS EXISTS ALONGSIDE THE WORKFLOW. 01d is a schedule: switch it on and it
works through every due thread, one per tick, until there are none left. That
is the right shape for steady state and the wrong shape for the first run,
where the question is "does this produce sensible output on twenty real
conversations" and the answer needs to arrive before another two hundred and
fifty have been paid for.

IT IS NOT A SECOND IMPLEMENTATION. Every statement executed here is read out of
`n8n/workflows/01d-chats-evaluate.json` at run time — the same SQL the workflow
runs, in the same order, against the same lease — and both AI passes go through
the same `/evaluate` endpoint. If the workflow JSON changes, this changes with
it. A rewritten copy of the pipeline would prove nothing about the pipeline.

    railway connect postgres --tunnel-only --port 55441
    export PGPASSWORD=... WORKER_API_KEY=...
    python scripts/run_chat_evaluation.py --limit 20 --dry-run
    python scripts/run_chat_evaluation.py --limit 20
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WORKFLOW = Path("n8n/workflows/01d-chats-evaluate.json")
WORKER = os.getenv("WORKER_URL", "https://railway-production-d648.up.railway.app")


def load_queries() -> dict[str, str]:
    wf = json.load(io.open(WORKFLOW, encoding="utf-8"))
    return {n["name"]: n["parameters"]["query"]
            for n in wf["nodes"] if n["type"].endswith("postgres")}


def to_psycopg(sql: str, params: list):
    """n8n numbers parameters $1..$n and may repeat one; psycopg is positional."""
    bound: list = []

    def sub(m):
        bound.append(params[int(m.group(1)) - 1])
        return "%s"

    return re.sub(r"\$(\d+)", sub, sql), bound


def post(path: str, body: dict, timeout: int = 320) -> tuple[int, dict]:
    """curl, not urllib: this machine's Python certificate bundle has expired,
    and disabling verification to work around that is not a trade worth making
    for a script that carries an API key."""
    proc = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-w", "\n%{http_code}",
         "-X", "POST", "-H", f"X-API-Key: {os.environ['WORKER_API_KEY']}",
         "-H", "Content-Type: application/json",
         "--data-binary", "@-", f"{WORKER}{path}"],
        input=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        capture_output=True, timeout=timeout + 30)
    out = proc.stdout.decode("utf-8", "replace")
    body_text, _, code = out.rpartition("\n")
    try:
        return int(code.strip() or 0), json.loads(body_text or "{}")
    except (ValueError, json.JSONDecodeError):
        return int(code.strip() or 0), {"raw": body_text[:400]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--port", default=os.getenv("PGPORT", "55441"))
    ap.add_argument("--dry-run", action="store_true",
                    help="register and claim, but call no model and store nothing")
    args = ap.parse_args()

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    q = load_queries()
    import psycopg

    dsn = (f"postgresql://postgres:{os.environ['PGPASSWORD']}"
           f"@127.0.0.1:{args.port}/customer360")
    tally = {"evaluated": 0, "unscoreable": 0, "judge_failed": 0, "no_work": 0}

    with psycopg.connect(dsn, connect_timeout=20, autocommit=True) as conn:
        def run(name, params=None):
            sql, bound = to_psycopg(q[name], params or [])
            with conn.cursor() as cur:
                cur.execute(sql, bound or None)
                try:
                    cols = [d.name for d in cur.description] if cur.description else []
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
                except psycopg.ProgrammingError:
                    return []

        print("== Register due threads ==")
        print(f"   {len(run('Register due threads'))} thread(s) in the queue")
        print("== Recover expired leases ==")
        print(f"   {len(run('Recover expired leases'))} lease(s) reclaimed\n")

        for i in range(1, args.limit + 1):
            claimed = run("Claim work")
            if not claimed:
                print(f"[{i}] no work left"); tally["no_work"] += 1; break
            job = claimed[0]
            iid, token = job["interaction_id"], job["claim_token"]

            thread = run("Load thread", [iid])
            if not thread:
                run("Mark judge failed", [iid, token, "thread vanished"]); continue
            t = thread[0]

            code, prep = post("/chats/prepare", {
                "external_id": t["external_id"], "channel": t["channel"],
                "messages": t["messages"]}, timeout=60)

            label = f"[{i}] {t['external_id'][:8]}… {len(t['messages']):>3} msg"
            if code != 200 or not prep.get("should_evaluate"):
                why = ("every turn labelled agent" if prep.get("has_no_customer_turn")
                       else "bot-only thread" if prep.get("is_bot_only")
                       else f"prepare returned {code}")
                if args.dry_run:
                    print(f"{label}  DRY skip — {why}"); continue
                run("Mark unscoreable", [iid, token, why])
                tally["unscoreable"] += 1
                print(f"{label}  unscoreable — {why}")
                continue

            if args.dry_run:
                print(f"{label}  DRY would judge "
                      f"({len(prep['transcript_text'])} chars)")
                run("Mark judge failed", [iid, token, "dry run: lease released"])
                continue

            # ONE PASS PER REQUEST. Railway's edge proxy closes a request at
            # 300 seconds — measured 2026-09-01, nine of twelve threads came
            # back HTTP 502 at exactly 301s while the worker was still
            # computing, and the work was thrown away. A reasoning judge spends
            # about a minute on pass 1 and two to four on pass 2, so the two
            # together do not fit and each one alone does.
            #
            # The endpoint already supports this: run_pass1 / run_pass2 select
            # which halves to run, and the two responses carry disjoint keys,
            # so merging them client-side reconstructs exactly the body a
            # single call would have returned. The passes were always
            # independent — that is the point of them — so splitting the
            # transport changes nothing about the scoring.
            t0 = time.time()
            code1, a1 = post("/evaluate", {
                "conversation": prep["transcript_text"], "input_type": "chat",
                "metadata": prep["metrics"], "run_pass1": True, "run_pass2": False})
            code2, a2 = post("/evaluate", {
                "conversation": prep["transcript_text"], "input_type": "chat",
                "metadata": prep["metrics"], "run_pass1": False, "run_pass2": True})
            took = time.time() - t0
            code = code1 if code1 != 200 else code2
            ai = {**(a1 if code1 == 200 else {}), **(a2 if code2 == 200 else {})}
            if code1 != 200:
                ai.setdefault("_pass1_error", a1)
            if code2 != 200:
                ai.setdefault("_pass2_error", a2)

            p2 = ai.get("pass2") or {}
            if code != 200 or "pass1" not in ai or "pass2" not in ai \
                    or p2.get("contract_status") == "unscoreable":
                if p2.get("contract_status") == "unscoreable":
                    run("Mark unscoreable (refused)", [iid, token,
                        "judge refused: " + json.dumps(p2.get("warnings") or [])])
                    tally["unscoreable"] += 1
                    print(f"{label}  refused as unscoreable ({took:.0f}s)")
                else:
                    run("Mark judge failed", [iid, token,
                        f"HTTP {code}: {json.dumps(ai)[:300]}"])
                    tally["judge_failed"] += 1
                    print(f"{label}  JUDGE FAILED HTTP {code} ({took:.0f}s)")
                continue

            payload = json.dumps(ai, ensure_ascii=False)
            stored = run("Store pass1", [iid, payload, iid, token])
            if not stored:
                print(f"{label}  lease lost before store"); continue
            run("Store evaluation", [iid, payload, iid, token])
            deal = run("Assess deal", [iid, "{}", iid, token])
            run("Mark evaluated", [iid, token])
            tally["evaluated"] += 1

            score = p2.get("final_score")
            d = f" · deal {deal[0]['bitrix_deal_id']} → {deal[0]['ai_outcome']}" if deal else ""
            print(f"{label}  score {str(score):>6} "
                  f"{str(p2.get('performance_level') or '-'):<14}({took:.0f}s){d}")

    print("\n" + json.dumps(tally, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
