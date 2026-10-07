"""Load a finished QA trial run into qa_evaluations and model_calls (030).

The trial asked the model three times per chat from a laptop and kept the raw
answers. Re-asking from workflow 10 would pay a second time for answers we
already hold, so this stores them instead — through the SAME code workflow 10
uses (app.qa.engine / score_v05.majority), so a backfilled row and a nightly
row cannot differ in how they were scored.

    python scripts/qa_backfill_trial.py <trial_dir> --port 55437          # dry run
    python scripts/qa_backfill_trial.py <trial_dir> --port 55437 --apply

<trial_dir> holds sample.json (the threads) and runs/<run>/<id>.json (the raw
answers + usage). It is customer data and never belongs in this repo.

Writes from a laptop on purpose and only once: the worker stays read-only
(rule 11), and this is a one-off load of paid-for answers, like seed_agents.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "worker"))

import psycopg  # noqa: E402

from app.evaluate.judge import cost_row  # noqa: E402
from app.qa import engine  # noqa: E402
from app.qa.score_v05 import category_scores, majority, score_thread  # noqa: E402

RUNS = ("w1_flash_1", "w1_flash_2", "w1_flash_3")

SQL_EVAL = """
INSERT INTO qa_evaluations (interaction_id, status, agent_id, prompt_version, model, runs,
                            score, critical, items, categories, cost_usd, evaluated_at)
VALUES (%s, 'scored', %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, now())
ON CONFLICT (interaction_id) DO NOTHING
RETURNING interaction_id
"""

SQL_CALL = """
INSERT INTO model_calls (interaction_id, purpose, provider, model, prompt_version, input_hash,
                         prompt_tokens, output_tokens, cached_tokens, cost_usd, priced_at_peak,
                         latency_ms, succeeded, error)
VALUES (%(interaction_id)s, %(purpose)s, %(provider)s, %(model)s, %(prompt_version)s,
        %(input_hash)s, %(prompt_tokens)s, %(output_tokens)s, %(cached_tokens)s, %(cost_usd)s,
        %(priced_at_peak)s, %(latency_ms)s, %(succeeded)s, %(error)s)
ON CONFLICT (purpose, input_hash, prompt_version) DO NOTHING
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trial_dir", type=Path)
    ap.add_argument("--port", default="55437")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    threads = json.loads((a.trial_dir / "sample.json").read_text(encoding="utf-8"))["threads"]
    conn = psycopg.connect(host="127.0.0.1", port=a.port, user="postgres", dbname="customer360",
                           password=os.environ["PGPASSWORD"])
    inserted = calls = 0
    total_cost = 0.0
    with conn, conn.cursor() as cur:
        for t in threads:
            saved = [json.loads((a.trial_dir / "runs" / r / f"{t['id']}.json").read_text(encoding="utf-8"))
                     for r in RUNS]
            result = majority([score_thread(t, s["answer"]) for s in saved])
            items = {int(q): v for q, v in result["items"].items()}
            text = engine.request_text(t)
            base = hashlib.sha256((engine.SYSTEM_PROMPT + "\n" + text).encode("utf-8")).hexdigest()
            rows = [cost_row(engine.PURPOSE, model=s.get("model") or "deepseek-v4-flash",
                             prompt_version=engine.PROMPT_VERSION, input_hash=f"{base}:{k}",
                             usage=s["usage"]) for k, s in enumerate(saved)]
            cost = round(sum(r["cost_usd"] or 0 for r in rows), 6)
            total_cost += cost
            if not a.apply:
                continue
            cur.execute(SQL_EVAL, (t["id"], t["agent_id"], engine.PROMPT_VERSION, rows[0]["model"],
                                   len(RUNS), result["score"], sorted(result["critical"]),
                                   json.dumps({str(q): v for q, v in sorted(items.items())},
                                              ensure_ascii=False),
                                   json.dumps({str(n): v for n, v in category_scores(items).items()}),
                                   cost))
            if cur.fetchone():
                inserted += 1
                for r in rows:
                    cur.execute(SQL_CALL, {**r, "interaction_id": t["id"]})
                    calls += cur.rowcount
            # One chat per transaction: a dropped tunnel mid-run keeps what is
            # done, and ON CONFLICT DO NOTHING makes the re-run skip it.
            conn.commit()
        if not a.apply:
            conn.rollback()
    print(f"threads {len(threads)}  cost ${total_cost:.4f}  "
          + (f"inserted {inserted} evaluations, {calls} model_calls" if a.apply else "(dry run)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
