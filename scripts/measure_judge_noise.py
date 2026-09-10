#!/usr/bin/env python3
"""Measure how much the judge disagrees with ITSELF, and record it.

    railway connect postgres --tunnel-only --port 55432
    export PGPASSWORD=... WORKER_API_KEY=...
    python scripts/measure_judge_noise.py --sample 60            # plan only
    python scripts/measure_judge_noise.py --sample 60 --apply

⚠ NOT YET RUNNABLE. This needs DeepSeek credit AND a clean night of judging
behind it, and at the time of writing the balance is -0.10 USD and 599 threads
are waiting. Written now, deliberately unrun. It refuses to start if the budget
gate says no, for the same reason every other spender does.

────────────────────────────────────────────────────────────────────────────
WHAT IT MEASURES, AND WHY THE SCORECARD NEEDS IT

`v_agent_scorecard.band_stable` is false for every agent today, for two
independent reasons: no agent has 30 usable scores yet, AND
`eval_noise_params` holds no measurement for the co-ordinate now shipping
(pass2-agent-quality-v6 / rubric 1.0.0 / deepseek-v4-flash / build a26a7955).
The second is the one this script fixes.

The question it answers is not "is the judge good" but "if I ask the same
judge the same question twice, how far apart are the two answers". That
distance is the floor under every confidence interval: an agent's mean cannot
be more precise than the instrument that produced it. Prior measurements on
this project, both with NO prompt change at all:

    2026-08-13  pass2-v3 / deepseek-chat      variance 188.70   11/68 bands flipped
    2026-08-24  pass2-v6 / stealth/ox-alpha   variance 159.20   20/58 bands flipped

Twenty of fifty-eight bands moved between two runs of the identical prompt.
That is what `band_stable` exists to stop being reported as a grade.

────────────────────────────────────────────────────────────────────────────
THE MEASUREMENT, EXACTLY

Two independent judgements of the SAME conversation, through the production
`/evaluate` endpoint — not a re-implementation of it, and not a local copy of
the prompts. `/evaluate` computes and returns; n8n is what writes
`agent_evaluations` (rule 11), so calling it twice stores nothing and cannot
disturb a live score.

    d_i        = score_run_a(i) - score_run_b(i)
    value      = var_samp(d)          <- what goes in eval_noise_params

`var_samp` of the PAIRED DIFFERENCE, matching both existing rows exactly: the
2026-08-24 note records "SD 12.62" against a stored value of 159.20, and
12.62² = 159.3. Do not "improve" this to Var(d)/2 on the argument that
Var(d) = 2σ² for independent runs. `eval_ci_half_width_95` consumes the stored
number as σ², so keeping Var(d) makes the published interval conservative by
√2 — and the two historical rows mean exactly this. Changing the definition
silently would make the new row incomparable with them while looking fine.

────────────────────────────────────────────────────────────────────────────
IT INSERTS. IT NEVER UPDATES.

`eval_noise_params` is keyed by the four version co-ordinates precisely so
that a re-measurement is a NEW ROW and the old one stays readable. Editing a
row to mean a new co-ordinate destroys the only record of what the previous
method's noise was, and 014 says so in the table comment. This script has no
UPDATE statement in it.

The co-ordinate is read back out of what the judge ACTUALLY returned, never
from configuration: `v_agent_scorecard` groups on the values stored in
`agent_evaluations`, so a row keyed to what we intended rather than what
happened would never be found by the lookup it exists to serve.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The same rule /evaluate applies: a score may be used only when all three
# agree. Anything else is a row recording why there is no number.
def usable(pass2: dict) -> bool:
    return (pass2.get("contract_status") == "ok"
            and bool(pass2.get("gradeable"))
            and pass2.get("final_score") is not None)


# A fixed, reproducible sample: the most recently judged chats, ordered by a
# stable key. Re-running with the same --sample picks the same conversations,
# so two measurements a month apart are comparable.
SAMPLE_SQL = """
SELECT i.interaction_id::text AS interaction_id,
       i.external_id,
       coalesce(jsonb_agg(
         jsonb_build_object(
           'seq', m.seq,
           -- The same relabel-and-redact 01d's "Load thread" applies (019).
           -- Measuring noise on an input production would never send would
           -- measure the wrong instrument.
           'sender', CASE WHEN m.sender = 'agent' AND a.is_bot
                          THEN 'bot' ELSE m.sender::text END,
           'body',   CASE WHEN m.sender = 'agent' AND a.is_bot
                          THEN '[automated system turn - content withheld]'
                          ELSE m.body END,
           'sent_at', to_char(m.sent_at AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS') || '+00:00'
         ) ORDER BY m.sent_at, m.seq
       ) FILTER (WHERE m.message_id IS NOT NULL), '[]'::jsonb) AS messages
  FROM interactions i
  JOIN agent_evaluations e ON e.interaction_id = i.interaction_id
  LEFT JOIN chat_messages m ON m.interaction_id = i.interaction_id
  LEFT JOIN agents a        ON a.bitrix_user_id = m.sender_external_id
 WHERE i.external_source = 'bitrix_chat_api'
   AND e.contract_status = 'ok'
   AND e.gradeable
   AND e.final_score IS NOT NULL
 GROUP BY i.interaction_id, i.external_id
HAVING count(m.message_id) > 0
 ORDER BY i.interaction_id
 LIMIT %s
"""

INSERT_SQL = """
INSERT INTO eval_noise_params
  (param_key, prompt_version, rubric_version, model, model_fingerprint,
   value, measured_on, source, notes)
VALUES ('repeat_run_variance', %s, %s, %s, %s, %s, current_date, %s, %s)
RETURNING param_key, prompt_version, model, model_fingerprint, value
"""


def worker_post(base: str, key: str, path: str, payload: dict,
                timeout: float = 300.0) -> dict:
    """httpx, not urllib.

    Found by running this: urllib validates against the Windows system store
    and rejects the worker's certificate chain as expired, on a machine where
    curl and httpx both accept it. httpx ships certifi, is already the client
    the worker itself uses, and so behaves the same wherever this is run from.
    """
    import httpx

    r = httpx.post(f"{base.rstrip('/')}{path}",
                   headers={"X-API-Key": key}, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def check_budget(base: str, key: str) -> None:
    """Refuse to spend if the pipeline itself would refuse to spend.

    Two runs over 60 conversations is ~120 judge calls. That is small, but it
    is not free, and a script that ignores the gate every workflow honours is
    the one that empties the account at 3am.
    """
    pre = worker_post(base, key, "/budget/preflight", {"providers": ["deepseek"]})
    d = pre["providers"]["deepseek"]
    if not d.get("may_run"):
        raise SystemExit(
            f"refusing to start: {d.get('blocked_reason')}\n"
            f"Top up DeepSeek and let a clean night of judging finish first — "
            f"a noise measurement taken while the judge is failing measures "
            f"the outage, not the judge.")
    print(f"budget ok — spent ${d['spend_mtd_usd']:.4f} of "
          f"${d['monthly_cap_usd']:.2f} this month")


def judge_once(base: str, key: str, conversation: str, label: str) -> dict:
    """One /evaluate call. pass 1 is skipped: this measures pass 2's spread."""
    return worker_post(base, key, "/evaluate", {
        "conversation": conversation,
        "input_type": "chat",
        "run_pass1": False,
        "run_pass2": True,
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=60,
                    help="conversations to re-judge twice (default 60, "
                         "matching the 2026-08-24 measurement)")
    ap.add_argument("--apply", action="store_true",
                    help="INSERT the measured row. Without it nothing is "
                         "written and nothing is spent.")
    ap.add_argument("--port", default=os.getenv("PGPORT", "55432"))
    ap.add_argument("--database", default="customer360")
    ap.add_argument("--out", help="write the raw pairs here for review")
    args = ap.parse_args()

    base = os.getenv("WORKER_URL",
                     "https://railway-production-d648.up.railway.app")
    key = os.getenv("WORKER_API_KEY")
    password = os.getenv("PGPASSWORD")
    if not key:
        raise SystemExit("WORKER_API_KEY is not set")
    if not password:
        raise SystemExit("PGPASSWORD is not set")

    import psycopg
    from psycopg.rows import dict_row

    dsn = f"postgresql://postgres:{password}@127.0.0.1:{args.port}/{args.database}"
    with psycopg.connect(dsn, connect_timeout=20, autocommit=True,
                         row_factory=dict_row) as conn:
        rows = conn.execute(SAMPLE_SQL, (args.sample,)).fetchall()
    print(f"sample: {len(rows)} already-judged conversations")
    if len(rows) < 30:
        print(f"\nWARNING: {len(rows)} is a thin sample for a variance. The two "
              f"historical measurements used 81 and 58 pairs. Let more threads "
              f"be judged before trusting this number.")
    if not args.apply:
        print("\nPLAN ONLY — nothing judged, nothing spent, nothing written.")
        print("Re-run with --apply once DeepSeek has credit.")
        return 0

    check_budget(base, key)

    pairs, coords, skipped = [], set(), 0
    for n, row in enumerate(rows, 1):
        lines = [f"[{m['sent_at']}] {m['sender'].upper()}: {m['body']}"
                 for m in row["messages"]]
        conversation = "\n".join(lines)
        try:
            a = judge_once(base, key, conversation, "a")
            # A pause between the two runs of a pair, so a provider-side cache
            # or a rate-limit burst cannot make run B a copy of run A. Two
            # identical answers would measure zero noise and publish every band.
            time.sleep(1.0)
            b = judge_once(base, key, conversation, "b")
        except Exception as exc:  # noqa: BLE001 - any transport failure is a skipped pair
            print(f"  [{n}/{len(rows)}] {row['external_id']}: judge error {exc}")
            skipped += 1
            continue

        pa, pb = a.get("pass2") or {}, b.get("pass2") or {}
        if not (usable(pa) and usable(pb)):
            skipped += 1
            continue

        # The co-ordinate the ROW will be keyed to, taken from what actually
        # came back. Both runs must agree, or the two halves of the pair were
        # not the same instrument and the difference is not noise.
        for p in (pa, pb):
            coords.add((p.get("prompt_version"), p.get("rubric_version"),
                        p.get("model"),
                        (p.get("usage") or {}).get("system_fingerprint")))

        d = float(pa["final_score"]) - float(pb["final_score"])
        pairs.append({"external_id": row["external_id"],
                      "a": float(pa["final_score"]),
                      "b": float(pb["final_score"]), "d": d})
        if n % 10 == 0:
            print(f"  [{n}/{len(rows)}] {len(pairs)} pairs so far")

    print(f"\nusable pairs: {len(pairs)}   skipped: {skipped}")
    if len(pairs) < 2:
        raise SystemExit("not enough usable pairs to compute a variance — "
                         "nothing written")
    if len(coords) != 1:
        raise SystemExit(
            "the runs did not share one version co-ordinate, so the spread is "
            "not repeat-run noise:\n  " +
            "\n  ".join(repr(c) for c in sorted(coords, key=str)) +
            "\nNothing written.")

    prompt_v, rubric_v, model, fingerprint = coords.pop()
    diffs = [p["d"] for p in pairs]
    variance = statistics.variance(diffs)
    sd = statistics.stdev(diffs)
    mae = sum(abs(x) for x in diffs) / len(diffs)

    print(f"\nco-ordinate : {prompt_v} / {rubric_v} / {model} / {fingerprint}")
    print(f"variance    : {variance:.2f}   <- eval_noise_params.value")
    print(f"SD          : {sd:.2f}")
    print(f"MAE         : {mae:.2f}")
    print(f"floor at 30 : ±{1.959964 * (variance / 30) ** 0.5:.2f} points")

    if args.out:
        Path(args.out).write_text(json.dumps(pairs, indent=1), encoding="utf-8")
        print(f"pairs written to {args.out}")

    source = (f"A/A re-measurement: {len(pairs)} conversations judged twice "
              f"through the production /evaluate endpoint at temperature 0")
    notes = (f"Variance of the per-call repeat-run difference, same definition "
             f"as the 2026-08-13 and 2026-08-24 rows. SD {sd:.2f}, "
             f"MAE {mae:.2f}, {skipped} pair(s) skipped as unusable. "
             f"Measured by scripts/measure_judge_noise.py.")

    with psycopg.connect(dsn, connect_timeout=20, autocommit=True,
                         row_factory=dict_row) as conn:
        # INSERT, never UPDATE: a re-measurement is a new row, and the previous
        # co-ordinate's number stays readable. If this raises a unique
        # violation, this co-ordinate has ALREADY been measured — decide
        # deliberately whether that is a re-run worth keeping, do not overwrite.
        written = conn.execute(INSERT_SQL, (
            prompt_v, rubric_v, model, fingerprint,
            round(variance, 2), source, notes)).fetchone()
    print(f"\nINSERTED: {written}")
    print("band_stable will now become true for any group with "
          "min_n_publish usable scores and an interval inside one band.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
