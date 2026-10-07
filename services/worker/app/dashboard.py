"""The QA dashboard: employees, team, customers — read live from the database.

The screens follow the designs the owner already approved: the employee view is
the artifact "تقييم جودة الموظفين" (employee → chat → scorecard item →
question → evidence), and the team / customers tabs are the "أداء الفريق" and
"كل العملاء" screens of "ست شاشات TravelGate", cut down to what this database
actually holds. Nothing is precomputed: workflow 10 adds graded chats every
night and the next page load shows them, which is what "automatic" means here.

A period is [from, to) in Riyadh days on the chat's start. An employee's score
is the plain mean of their chat scores — the approved page's rule.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db

RIYADH = timezone(timedelta(hours=3))

SQL_SINCE = "SELECT value FROM qa_config WHERE key = 'since'"

_PERIOD = """
  i.started_at >= (%(start)s::date::timestamp AT TIME ZONE 'Asia/Riyadh')
  AND i.started_at < (%(end)s::date::timestamp AT TIME ZONE 'Asia/Riyadh')
"""

SQL_TOTALS = f"""
SELECT count(*) FILTER (WHERE q.status = 'scored')                   AS graded,
       round(avg(q.score) FILTER (WHERE q.status = 'scored'), 1)     AS avg_score,
       count(DISTINCT q.agent_id) FILTER (WHERE q.status = 'scored') AS agents,
       coalesce(sum(cardinality(q.critical)) FILTER (WHERE q.status = 'scored'), 0) AS critical,
       (SELECT count(*) FROM v_qa_due d
         WHERE d.started_at >= (%(start)s::date::timestamp AT TIME ZONE 'Asia/Riyadh')
           AND d.started_at <  (%(end)s::date::timestamp AT TIME ZONE 'Asia/Riyadh'))
                                                                     AS waiting,
       max(q.evaluated_at)                                           AS last_graded
FROM qa_evaluations q JOIN interactions i USING (interaction_id)
WHERE {_PERIOD}
"""

SQL_AGENTS = f"""
SELECT q.agent_id::text, a.full_name, a.team,
       count(*)                                  AS chats,
       round(avg(q.score), 1)                    AS score,
       sum(cardinality(q.critical))              AS critical
FROM qa_evaluations q
JOIN interactions i USING (interaction_id)
JOIN agents a ON a.agent_id = q.agent_id
WHERE q.status = 'scored' AND {_PERIOD}
GROUP BY q.agent_id, a.full_name, a.team
ORDER BY score DESC
"""

# Each of the 15 items on its own 0-5 scale, averaged over the chats where it
# applied. A JSON null (nothing in that item applied) is skipped, not zeroed.
SQL_AGENT_ITEMS = f"""
SELECT q.agent_id::text, c.key::int AS item, round(avg(c.value::numeric), 2) AS avg5,
       count(*) AS n
FROM qa_evaluations q
JOIN interactions i USING (interaction_id)
CROSS JOIN LATERAL jsonb_each_text(q.categories) AS c(key, value)
WHERE q.status = 'scored' AND c.value IS NOT NULL AND {_PERIOD}
GROUP BY 1, 2
"""

# How much of an agent's work the grade covers: every chat they handled in
# the period, against the ones that passed the filter and were graded.
SQL_AGENT_LOAD = f"""
SELECT i.agent_id::text, count(*) AS handled
FROM interactions i
WHERE i.external_source = 'bitrix_chat_api' AND i.agent_id IS NOT NULL AND {_PERIOD}
GROUP BY 1
"""

# Deals are not clipped to the period, for the reason report.py gives: a deal
# opened in June and won in September belongs to whoever won it.
SQL_AGENT_DEALS = """
SELECT agent_id::text,
       count(*)                                         AS deals,
       count(*) FILTER (WHERE stage_semantic = 'S')     AS won
FROM deals WHERE agent_id IS NOT NULL
GROUP BY 1
"""

SQL_AGENT_CHATS = f"""
SELECT q.interaction_id::text, i.started_at, q.score, q.critical
FROM qa_evaluations q JOIN interactions i USING (interaction_id)
WHERE q.status = 'scored' AND q.agent_id = %(agent)s::uuid AND {_PERIOD}
ORDER BY i.started_at
"""

SQL_CUSTOMERS = f"""
SELECT c.customer_id::text,
       c.display_name,
       right(c.primary_phone_e164, 4)                                     AS phone_tail,
       count(*)                                                           AS chats,
       max(i.started_at)                                                  AS last_at,
       (array_agg(a.full_name ORDER BY i.started_at DESC))[1]             AS last_agent,
       (array_agg(q.score ORDER BY i.started_at DESC)
          FILTER (WHERE q.status = 'scored'))[1]                          AS last_score,
       (SELECT count(*) FROM deals d WHERE d.customer_id = c.customer_id) AS deals,
       (SELECT count(*) FROM deals d WHERE d.customer_id = c.customer_id
                                       AND d.stage_semantic = 'S')        AS won
FROM interactions i
JOIN customers c USING (customer_id)
LEFT JOIN agents a USING (agent_id)
LEFT JOIN qa_evaluations q USING (interaction_id)
WHERE i.external_source = 'bitrix_chat_api' AND {_PERIOD}
GROUP BY c.customer_id, c.display_name, c.primary_phone_e164
ORDER BY last_at DESC
LIMIT 1000
"""

SQL_ONE = """
SELECT interaction_id::text, status, score, critical, items, categories
FROM qa_evaluations WHERE interaction_id = %s::uuid
"""


def period(start: str | None, end: str | None) -> tuple[date, date]:
    """Default: from qa_config.since to tomorrow (Riyadh), i.e. everything graded."""
    today = datetime.now(RIYADH).date()
    s = date.fromisoformat(start) if start else date.fromisoformat(
        db.one(SQL_SINCE).get("value") or str(today - timedelta(days=7)))
    e = date.fromisoformat(end) if end else today + timedelta(days=1)
    if e <= s:
        raise ValueError("'to' must be after 'from'")
    return s, e


def build(start: str | None = None, end: str | None = None) -> dict[str, Any]:
    s, e = period(start, end)
    p = {"start": s.isoformat(), "end": e.isoformat()}
    agents = db.rows(SQL_AGENTS, p)
    items: dict[str, dict[str, Any]] = {}
    for r in db.rows(SQL_AGENT_ITEMS, p):
        items.setdefault(r["agent_id"], {})[str(r["item"])] = float(r["avg5"])
    load = {r["agent_id"]: r["handled"] for r in db.rows(SQL_AGENT_LOAD, p)}
    deals = {r["agent_id"]: r for r in db.rows(SQL_AGENT_DEALS)}
    for a in agents:
        a["score"] = float(a["score"]) if a["score"] is not None else None
        a["items"] = items.get(a["agent_id"], {})
        a["handled"] = load.get(a["agent_id"], 0)
        d = deals.get(a["agent_id"]) or {}
        a["deals"], a["won"] = d.get("deals", 0), d.get("won", 0)
    totals = db.one(SQL_TOTALS, p)
    customers = db.rows(SQL_CUSTOMERS, p)
    for c in customers:
        c["last_score"] = float(c["last_score"]) if c["last_score"] is not None else None
    return {"from": p["start"], "to": p["end"], "totals": totals,
            "agents": agents, "customers": customers}


def agent_chats(agent_id: str, start: str | None, end: str | None) -> list[dict[str, Any]]:
    s, e = period(start, end)
    rows = db.rows(SQL_AGENT_CHATS, {"agent": agent_id, "start": s.isoformat(), "end": e.isoformat()})
    for r in rows:
        r["score"] = float(r["score"])
    return rows


def chat_fragment(interaction_id: str, k: int = 1) -> str | None:
    """The graded chat as the approved page renders it, or None if not graded."""
    from .qa import engine, render

    row = db.one(SQL_ONE, (interaction_id,))
    if not row or row["status"] != "scored" or not row["items"]:
        return None
    try:
        thread = engine.load_thread(interaction_id, gate=False)
    except engine.NotGradeable:
        return None
    m = {"items": row["items"], "score": float(row["score"]), "critical": row["critical"]}
    return render.chat_html(k, thread, m)
