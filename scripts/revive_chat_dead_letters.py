#!/usr/bin/env python3
"""Re-open chat threads that were dead-lettered by the empty-judge bug.

    railway connect postgres --tunnel-only --port 55432
    export PGPASSWORD=...
    python scripts/revive_chat_dead_letters.py            # count only
    python scripts/revive_chat_dead_letters.py --apply

RUN THIS ONLY AFTER `DEEPSEEK_THINKING=disabled` IS LIVE ON THE WORKER.
Reviving first just re-burns the attempts against the same broken judge.

WHAT WENT WRONG. The worker was running with DEEPSEEK_THINKING=omit, which
DELETES the `thinking` field from the request rather than sending
{"type":"disabled"}. `deepseek-v4-flash` defaults to thinking ON, so every
judge call spent its entire 8,000-token budget on hidden reasoning and returned
an empty string. Measured on the real pass-1 prompt:

    omit      57.4s  finish_reason=length  content=''      8000 reasoning tokens
    disabled   4.0s  finish_reason=stop    2621 chars      837 completion tokens

That one variable produced 82 of the 135 dead letters directly ("model did not
return valid JSON ... got ''"), and the 57-second calls produced most of the
rest (18 x `timeout of 300000ms`, 28 x `socket hang up`).

WHAT IS NOT REVIVED. Two classes stay dead, on purpose:
  * 'unscoreable' -- terminal by design; re-asking a model that already
    answered can only manufacture a score.
  * the retired workflow-01 test fixtures, which are not production traffic.
Everything else was killed by infrastructure, not by the conversation.
"""
from __future__ import annotations

import argparse
import os
import sys

# The dry run must count EXACTLY the rows --apply rewrites, or the number it
# prints is about a different set than the one it is asking permission for.
# Both use REVIVABLE, and the WHERE clause exists once.
REVIVABLE = """
  status IN ('dead_letter', 'judge_failed')
  AND last_error IS NOT NULL
  AND last_error NOT LIKE 'not production traffic%'
"""

COUNT_SQL = f"""
SELECT count(*) FILTER (WHERE status = 'dead_letter')  AS dead,
       count(*) FILTER (WHERE status = 'judge_failed') AS failed,
       count(*) FILTER (WHERE {REVIVABLE})             AS revivable
  FROM chat_eval_jobs
"""

REVIVE_SQL = f"""
UPDATE chat_eval_jobs
   SET status          = 'pending',
       judge_attempts  = 0,
       claim_token     = NULL,
       claim_until     = NULL,
       claimed_at      = NULL,
       next_attempt_at = now(),
       last_error      = NULL,
       updated_at      = now()
 WHERE {REVIVABLE}
RETURNING interaction_id
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--port", default=os.getenv("PGPORT", "55432"))
    ap.add_argument("--database", default="customer360")
    args = ap.parse_args()

    password = os.getenv("PGPASSWORD")
    if not password:
        raise SystemExit("PGPASSWORD is not set")

    import psycopg

    dsn = f"postgresql://postgres:{password}@127.0.0.1:{args.port}/{args.database}"
    with psycopg.connect(dsn, connect_timeout=20, autocommit=True) as conn:
        dead, failed, revivable = conn.execute(COUNT_SQL).fetchone()
        print(f"dead_letter   : {dead}")
        print(f"judge_failed  : {failed}")
        print(f"WILL RE-OPEN  : {revivable}  "
              f"(of {dead + failed}; the rest are retired test fixtures)")
        if not args.apply:
            print("\nCHECK ONLY — nothing changed. Re-run with --apply.")
            return 0
        n = len(conn.execute(REVIVE_SQL).fetchall())
        print(f"\nre-opened {n} thread(s) as 'pending', attempts reset to 0.")
        print("They will be claimed on the next 23:00-03:59 Riyadh tick.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
