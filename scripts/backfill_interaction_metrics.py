#!/usr/bin/env python3
"""Fill `interaction_metrics` for conversations judged before it had a writer.

    railway connect postgres --tunnel-only --port 55432
    export PGPASSWORD=...
    python scripts/backfill_interaction_metrics.py --port 55432 --apply

WHY THE TABLE IS EMPTY. `/chats/prepare` has always returned a `metrics`
object, but nothing stored it until 01d gained a `Store metrics` node. So
`v_agent_scorecard` — which LEFT JOINs `interaction_metrics` for
`avg_first_response_sec` — showed a blank response time for every agent, on
every row, for the whole life of the project.

WHY THIS IMPORTS THE WORKER'S OWN MODULE. Rule 3: response gaps, after-hours
and language match are computed in `evaluate/metrics.py` and nowhere else. A
backfill that re-derived them in SQL would be a second implementation, and the
two would disagree the first time a rule changed — which is precisely the bug
that made the retired 01b handler produce negative response times. This calls
the same function 01d calls, so a row written here and a row written by the
pipeline are the same row.

Only conversations that HAVE been judged are filled: the metrics for anything
else will be written by the pipeline when it judges them.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services" / "worker"))

SELECT_SQL = """
SELECT i.interaction_id,
       coalesce(jsonb_agg(
         jsonb_build_object(
           'seq', m.seq,
           -- Same relabel 01d's "Load thread" applies: an automation's turns
           -- are a bot's, not an agent's, and counting them as agent replies
           -- is what made the metrics wrong in the first place.
           'sender', CASE WHEN m.sender = 'agent' AND a.is_bot
                          THEN 'bot' ELSE m.sender::text END,
           'body', m.body,
           'sent_at', to_char(m.sent_at AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS') || '+00:00'
         ) ORDER BY m.sent_at, m.seq
       ) FILTER (WHERE m.message_id IS NOT NULL), '[]'::jsonb) AS messages
  FROM interactions i
  JOIN agent_evaluations e ON e.interaction_id = i.interaction_id
  LEFT JOIN chat_messages m ON m.interaction_id = i.interaction_id
  LEFT JOIN agents a        ON a.bitrix_user_id = m.sender_external_id
 WHERE i.channel <> 'phone_call'
   AND NOT EXISTS (SELECT 1 FROM interaction_metrics im
                    WHERE im.interaction_id = i.interaction_id)
 GROUP BY i.interaction_id
HAVING count(m.message_id) > 0
"""

UPSERT_SQL = """
INSERT INTO interaction_metrics (
  interaction_id, first_response_seconds, median_response_seconds,
  max_response_gap_seconds, agent_talk_ratio, customer_message_count,
  agent_message_count, conversation_span_seconds, after_hours,
  language_matched, computed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (interaction_id) DO UPDATE SET
  first_response_seconds    = EXCLUDED.first_response_seconds,
  median_response_seconds   = EXCLUDED.median_response_seconds,
  max_response_gap_seconds  = EXCLUDED.max_response_gap_seconds,
  agent_talk_ratio          = EXCLUDED.agent_talk_ratio,
  customer_message_count    = EXCLUDED.customer_message_count,
  agent_message_count       = EXCLUDED.agent_message_count,
  conversation_span_seconds = EXCLUDED.conversation_span_seconds,
  after_hours               = EXCLUDED.after_hours,
  language_matched          = EXCLUDED.language_matched,
  computed_at               = now()
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

    from app.evaluate import metrics as M
    from app.sources.base import Conversation, Message

    import psycopg
    from psycopg.rows import dict_row

    dsn = f"postgresql://postgres:{password}@127.0.0.1:{args.port}/{args.database}"
    with psycopg.connect(dsn, connect_timeout=20, autocommit=True,
                         row_factory=dict_row) as conn:
        todo = conn.execute(SELECT_SQL).fetchall()
        print(f"conversations judged but unmeasured: {len(todo)}")
        if not args.apply:
            print("\nCHECK ONLY — nothing written. Re-run with --apply.")
            return 0

        written = skipped = 0
        for row in todo:
            msgs = []
            for m in row["messages"]:
                try:
                    sent_at = datetime.fromisoformat(m["sent_at"])
                except ValueError:
                    continue
                sender = m["sender"] if m["sender"] in (
                    "customer", "agent", "bot", "system") else "unknown"
                msgs.append(Message(seq=m["seq"], sender=sender,
                                    body=m["body"] or "", sent_at=sent_at))
            if not msgs:
                skipped += 1
                continue
            conv = Conversation(
                external_id=str(row["interaction_id"]),
                external_source="bitrix_chat_api",
                channel="other",
                started_at=min(m.sent_at for m in msgs),
                messages=msgs,
            )
            c = M.compute_chat_metrics(conv)
            conn.execute(UPSERT_SQL, (
                row["interaction_id"], c.first_response_seconds,
                c.median_response_seconds, c.max_response_gap_seconds,
                c.agent_talk_ratio, c.customer_message_count,
                c.agent_message_count, c.conversation_span_seconds,
                c.after_hours, c.language_matched))
            written += 1

        print(f"written: {written}   skipped (no usable turns): {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
