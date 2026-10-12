"""Customer 360 console — the data behind /dashboard (the v4 chat scorecard).

The page (static/c360) holds nothing: it asks for WORKER_API_KEY and calls the
routes below, each answered live from the database, so tonight's grades are on
it tomorrow morning. This module only reads (rule 11).

    overview(start, end)         team KPIs, agents, customers, red flags, daily trend,
                                 loss reasons, chat types, lost ready customers, promises
    agent_chats(id, start, end)  one agent's graded chats
    customer(id)                 one customer's whole story: chats, deals, asks, promises
    search(q)                    customers by name, last digits of the phone, or Bitrix deal id
    chat_fragment(id)            one graded chat as HTML (app/qa/v4/render.py)
    promises(start, end)         every promise an agent made in a graded chat, broken first

Definitions that are made ONCE and read everywhere:
  * a period is [from, to) in Riyadh days on the chat's start;
  * an agent's score is the plain mean of their chat scores (handoff 5.8);
  * a deal is "won" when stage_semantic = 'S', and deals are NOT clipped to the
    period: a deal opened in one month and won in the next belongs to its owner;
  * "qualified" is the scorecard's own: a sales chat whose destination, date and
    travellers were all quoted from the customer and verified;
  * a promise is the scorecard's `promise` item (handoff 5.4b): kept = delivered,
    broken = not delivered though the agent had a real chance. It replaces the
    old `follow_ups` table here, which only pass 1 (switched off) ever filled.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db
from .qa.v4 import render, texts

RIYADH = timezone(timedelta(hours=3))
CUSTOMER_LIMIT = 1000
LIST_LIMIT = 200

SQL_SINCE = "SELECT value FROM qa_config WHERE key = 'since'"


def _period(col: str) -> str:
    return (f"{col} >= (%(start)s::date::timestamp AT TIME ZONE 'Asia/Riyadh') "
            f"AND {col} < (%(end)s::date::timestamp AT TIME ZONE 'Asia/Riyadh')")


_IN_PERIOD = _period("i.started_at")
_SCORED = f"g.status = 'scored' AND {_IN_PERIOD}"

SQL_TOTALS = f"""
SELECT count(*)                                      AS graded,
       round(avg(g.score), 1)                        AS avg_score,
       count(DISTINCT g.agent_id)                    AS agents,
       count(*) FILTER (WHERE g.red_flag IS NOT NULL) AS red_flags,
       count(*) FILTER (WHERE g.qualified)           AS qualified,
       count(*) FILTER (WHERE g.loss_reason = 'Booked') AS booked,
       max(g.evaluated_at)                           AS last_graded,
       (SELECT count(*) FROM v_qa_grade_due d WHERE {_period("d.started_at")}) AS waiting
FROM qa_grades g JOIN interactions i USING (interaction_id)
WHERE {_SCORED}
"""

SQL_AGENTS = f"""
SELECT g.agent_id::text, a.full_name, a.team,
       count(*)                                          AS chats,
       round(avg(g.score), 1)                            AS score,
       count(*) FILTER (WHERE g.red_flag IS NOT NULL)    AS red_flags,
       count(*) FILTER (WHERE g.qualified)               AS qualified,
       count(*) FILTER (WHERE g.loss_reason = 'Booked')  AS booked,
       percentile_cont(0.5) WITHIN GROUP (
         ORDER BY (g.detail->'metrics'->>'first_response_sec')::numeric) AS first_response_sec
FROM qa_grades g
JOIN interactions i USING (interaction_id)
JOIN agents a ON a.agent_id = g.agent_id
WHERE {_SCORED}
GROUP BY g.agent_id, a.full_name, a.team
ORDER BY score DESC NULLS LAST
"""

# Each item's mean value (0..1) over the chats where it applied. An item that
# did not apply is absent from `items`, so it is skipped, never zeroed.
SQL_AGENT_ITEMS = f"""
SELECT g.agent_id::text, it.key, round(avg((it.value->>'value')::numeric), 3) AS value, count(*) AS n
FROM qa_grades g
JOIN interactions i USING (interaction_id)
CROSS JOIN LATERAL jsonb_each(g.items) AS it(key, value)
WHERE {_SCORED}
GROUP BY 1, 2
"""

SQL_AGENT_LOSSES = f"""
SELECT g.agent_id::text, g.loss_reason, count(*) AS chats
FROM qa_grades g JOIN interactions i USING (interaction_id)
WHERE {_SCORED} AND g.loss_reason IS NOT NULL
GROUP BY 1, 2
"""

# Everything the agent handled in the period, graded or not: the denominator
# that says how much of their work the score covers.
SQL_AGENT_LOAD = f"""
SELECT i.agent_id::text, count(*) AS handled, count(DISTINCT i.customer_id) AS customers
FROM interactions i
WHERE i.external_source = 'bitrix_chat_api' AND i.agent_id IS NOT NULL AND {_IN_PERIOD}
GROUP BY 1
"""

SQL_AGENT_DEALS = """
SELECT agent_id::text,
       count(*)                                                  AS deals,
       count(*) FILTER (WHERE stage_semantic = 'S')              AS won,
       count(*) FILTER (WHERE stage_semantic = 'F')              AS lost,
       count(*) FILTER (WHERE stage_semantic = 'P')              AS open,
       round(sum(amount) FILTER (WHERE stage_semantic = 'S'), 2) AS won_amount
FROM deals WHERE agent_id IS NOT NULL
GROUP BY 1
"""

SQL_DAILY = f"""
SELECT (i.started_at AT TIME ZONE 'Asia/Riyadh')::date::text AS day,
       count(*) AS graded, round(avg(g.score), 1) AS avg_score
FROM qa_grades g JOIN interactions i USING (interaction_id)
WHERE {_SCORED}
GROUP BY 1 ORDER BY 1
"""

SQL_LOSS_MIX = f"""
SELECT g.loss_reason, count(*) AS chats
FROM qa_grades g JOIN interactions i USING (interaction_id)
WHERE {_SCORED} AND g.loss_reason IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC
"""

SQL_CHAT_TYPES = f"""
SELECT coalesce(g.chat_type, 'unknown') AS chat_type, count(*) AS chats
FROM qa_grades g JOIN interactions i USING (interaction_id)
WHERE {_SCORED}
GROUP BY 1 ORDER BY 2 DESC
"""

_CHAT_ROW = """
SELECT g.interaction_id::text, i.started_at, g.score, g.red_flag, g.loss_reason, g.qualified,
       g.chat_type, a.full_name AS agent_name, i.external_deal_id AS deal_id,
       i.customer_id::text, c.display_name, right(c.primary_phone_e164, 4) AS phone_tail,
       d.stage_semantic
FROM qa_grades g
JOIN interactions i USING (interaction_id)
LEFT JOIN agents a ON a.agent_id = g.agent_id
LEFT JOIN customers c ON c.customer_id = i.customer_id
LEFT JOIN deals d ON d.deal_id = i.deal_id
"""

SQL_RED_FLAG_CHATS = f"""{_CHAT_ROW}
WHERE {_SCORED} AND g.red_flag IS NOT NULL
ORDER BY i.started_at DESC LIMIT {LIST_LIMIT}
"""

# Customers who told us everything needed to sell (qualified) and did not book:
# the list a sales lead works from. Their deal's stage travels with them,
# because "lost in Bitrix" and "still open in Bitrix" are different calls.
SQL_QUALIFIED_LOST = f"""{_CHAT_ROW}
WHERE {_SCORED} AND g.qualified AND g.loss_reason IS DISTINCT FROM 'Booked'
ORDER BY i.started_at DESC LIMIT {LIST_LIMIT}
"""

SQL_AGENT_CHATS = f"""{_CHAT_ROW}
WHERE {_SCORED} AND g.agent_id = %(agent)s::uuid
ORDER BY i.started_at
"""

SQL_CUSTOMERS = f"""
SELECT c.customer_id::text,
       c.display_name,
       right(c.primary_phone_e164, 4)                                     AS phone_tail,
       count(*)                                                           AS chats,
       max(i.started_at)                                                  AS last_at,
       (array_agg(a.full_name ORDER BY i.started_at DESC))[1]             AS last_agent,
       -- One observation, not a mean: a customer has one to five chats, and a
       -- "customer average" is a mean of one dressed up as a statistic.
       (array_agg(g.score ORDER BY i.started_at DESC)
          FILTER (WHERE g.status = 'scored' AND g.score IS NOT NULL))[1]  AS last_score,
       bool_or(g.qualified)                                               AS qualified,
       coalesce(ds.deals, 0)                                              AS deals,
       coalesce(ds.won, 0)                                                AS won,
       ds.won_amount
FROM interactions i
JOIN customers c USING (customer_id)
LEFT JOIN agents a USING (agent_id)
LEFT JOIN qa_grades g USING (interaction_id)
LEFT JOIN v_customer_deal_summary ds ON ds.customer_id = c.customer_id
WHERE i.external_source = 'bitrix_chat_api' AND c.is_merged_into IS NULL AND {_IN_PERIOD}
GROUP BY c.customer_id, c.display_name, c.primary_phone_e164, ds.deals, ds.won, ds.won_amount
ORDER BY last_at DESC
LIMIT {CUSTOMER_LIMIT}
"""

# One promise per graded chat where the `promise` item applied. Its first
# evidence line is the promise, the second (when kept) the delivery; the text is
# the masked line the grade was made from.
_PROMISE_ROW = """
SELECT g.interaction_id::text, i.started_at, i.customer_id::text,
       (g.items->'promise'->>'value')::numeric = 1                                      AS kept,
       g.detail->'evidence'->(g.items->'promise'->'lines'->>0)->>'text'                 AS promise_text,
       g.detail->'evidence'->(g.items->'promise'->'lines'->>1)->>'text'                 AS delivery_text,
       a.full_name AS agent_name, c.display_name, right(c.primary_phone_e164, 4) AS phone_tail
FROM qa_grades g
JOIN interactions i USING (interaction_id)
LEFT JOIN agents a ON a.agent_id = g.agent_id
LEFT JOIN customers c ON c.customer_id = i.customer_id
WHERE g.status = 'scored' AND g.items ? 'promise'
"""
SQL_PROMISES = f"""{_PROMISE_ROW} AND {_IN_PERIOD}
ORDER BY kept, i.started_at DESC
"""
SQL_CUSTOMER_PROMISES = f"""{_PROMISE_ROW} AND i.customer_id = %s::uuid
ORDER BY i.started_at DESC
"""

SQL_CHAT = """
SELECT interaction_id::text, status, score, items, chat_type, qualified, red_flag,
       loss_reason, loss_reason_source, followup_attempts, detail
FROM qa_grades WHERE interaction_id = %s::uuid
"""

SQL_CUSTOMER = """
SELECT customer_id::text, display_name, name_source, primary_phone_e164 AS phone,
       residence_city AS city, nationality, first_seen_at, last_seen_at,
       is_merged_into::text AS merged_into
FROM customers WHERE customer_id = %s::uuid
"""

SQL_CUSTOMER_CHATS = """
SELECT i.interaction_id::text, i.started_at, i.ended_at, i.message_count,
       i.external_deal_id AS deal_id, i.agent_id::text, a.full_name AS agent_name,
       g.status AS qa_status, g.score, g.red_flag, g.loss_reason, g.qualified,
       g.detail->'booking' AS booking, ia.summary_ar
FROM interactions i
LEFT JOIN agents a USING (agent_id)
LEFT JOIN qa_grades g USING (interaction_id)
LEFT JOIN interaction_analysis ia USING (interaction_id)
WHERE i.customer_id = %s::uuid
ORDER BY i.started_at DESC
"""

SQL_CUSTOMER_DEALS = """
SELECT d.bitrix_deal_id AS deal_id, d.title, d.stage_id, d.stage_semantic, d.amount, d.currency,
       d.service::text, d.created_at_src, d.close_date, d.modified_at_src, a.full_name AS agent_name
FROM deals d LEFT JOIN agents a ON a.agent_id = d.agent_id
WHERE d.customer_id = %s::uuid
ORDER BY d.created_at_src DESC NULLS LAST
"""

_SEARCH = """
SELECT c.customer_id::text, c.display_name, right(c.primary_phone_e164, 4) AS phone_tail,
       c.last_seen_at,
       (SELECT count(*) FROM interactions i WHERE i.customer_id = c.customer_id) AS chats,
       (SELECT count(*) FROM deals d WHERE d.customer_id = c.customer_id)        AS deals
FROM customers c
WHERE c.is_merged_into IS NULL AND {where}
ORDER BY c.last_seen_at DESC NULLS LAST
LIMIT 30
"""
SQL_SEARCH_DIGITS = _SEARCH.format(where="""(
    c.primary_phone_e164 LIKE '%%' || %(q)s
    OR EXISTS (SELECT 1 FROM deals d WHERE d.customer_id = c.customer_id AND d.bitrix_deal_id = %(q)s))""")
SQL_SEARCH_NAME = _SEARCH.format(where="c.display_name ILIKE '%%' || %(q)s || '%%'")


# ---------------------------------------------------------------------------

def period(start: str | None, end: str | None) -> dict[str, str]:
    """Default: from qa_config.since to tomorrow (Riyadh) — everything graded."""
    today = datetime.now(RIYADH).date()
    s = date.fromisoformat(start) if start else date.fromisoformat(
        (db.one(SQL_SINCE) or {}).get("value") or str(today - timedelta(days=7)))
    e = date.fromisoformat(end) if end else today + timedelta(days=1)
    if e <= s:
        raise ValueError("'end' must be after 'start'")
    return {"start": s.isoformat(), "end": e.isoformat()}


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _by_agent(rows: list[dict], key: str = "agent_id") -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def _agents(p: dict[str, str]) -> list[dict[str, Any]]:
    agents = db.rows(SQL_AGENTS, p)
    items = _by_agent(db.rows(SQL_AGENT_ITEMS, p))
    losses = _by_agent(db.rows(SQL_AGENT_LOSSES, p))
    load = {r["agent_id"]: r for r in db.rows(SQL_AGENT_LOAD, p)}
    deals = {r["agent_id"]: r for r in db.rows(SQL_AGENT_DEALS)}
    for a in agents:
        aid = a["agent_id"]
        a["score"] = _num(a["score"])
        a["first_response_sec"] = _num(a["first_response_sec"])
        a["items"] = {r["key"]: float(r["value"]) for r in items.get(aid, [])}
        a["items_n"] = {r["key"]: r["n"] for r in items.get(aid, [])}   # chats where it applied
        a["losses"] = {r["loss_reason"]: r["chats"] for r in losses.get(aid, [])}
        a["handled"] = (load.get(aid) or {}).get("handled", 0)
        a["customers"] = (load.get(aid) or {}).get("customers", 0)
        d = deals.get(aid) or {}
        a.update(deals=d.get("deals", 0), won=d.get("won", 0), lost=d.get("lost", 0),
                 open=d.get("open", 0), won_amount=_num(d.get("won_amount")))
    return agents


def _scores_to_float(rows: list[dict], *keys: str) -> list[dict]:
    for r in rows:
        for k in keys:
            r[k] = _num(r.get(k))
    return rows


def overview(start: str | None = None, end: str | None = None) -> dict[str, Any]:
    p = period(start, end)
    totals = db.one(SQL_TOTALS, p) or {}
    totals["avg_score"] = _num(totals.get("avg_score"))
    agents = _agents(p)
    totals["handled"] = sum(a["handled"] for a in agents)
    broken = [x for x in db.rows(SQL_PROMISES, p) if not x["kept"]]
    return {
        "from": p["start"], "to": p["end"],
        "meta": texts.meta(),
        "totals": totals,
        "agents": agents,
        "customers": _scores_to_float(db.rows(SQL_CUSTOMERS, p), "last_score", "won_amount"),
        "red_flag_chats": _scores_to_float(db.rows(SQL_RED_FLAG_CHATS, p), "score"),
        "qualified_lost": _scores_to_float(db.rows(SQL_QUALIFIED_LOST, p), "score"),
        "daily": _scores_to_float(db.rows(SQL_DAILY, p), "avg_score"),
        "loss_reasons": db.rows(SQL_LOSS_MIX, p),
        "chat_types": db.rows(SQL_CHAT_TYPES, p),
        "broken_promises": broken,
    }


def agent_chats(agent_id: str, start: str | None, end: str | None) -> list[dict[str, Any]]:
    p = {**period(start, end), "agent": agent_id}
    return _scores_to_float(db.rows(SQL_AGENT_CHATS, p), "score")


def customer(customer_id: str) -> dict[str, Any] | None:
    c = db.one(SQL_CUSTOMER, (customer_id,))
    if not c:
        return None
    if c["merged_into"]:
        return {"customer": c}
    chats = _scores_to_float(db.rows(SQL_CUSTOMER_CHATS, (customer_id,)), "score")
    deals = _scores_to_float(db.rows(SQL_CUSTOMER_DEALS, (customer_id,)), "amount")
    promises = db.rows(SQL_CUSTOMER_PROMISES, (customer_id,))
    graded = [x for x in chats if x["qa_status"] == "scored"]
    won = [d for d in deals if d["stage_semantic"] == "S"]
    asks = [{"interaction_id": x["interaction_id"], "started_at": x["started_at"],
             **{k: (x["booking"].get(k) or {}).get("text") for k in texts.BOOKING}}
            for x in chats if x.get("booking") and any(x["booking"].values())]
    return {
        "customer": c,
        "summary": {
            "chats": len(chats), "graded": len(graded),
            "last_score": next((x["score"] for x in graded if x["score"] is not None), None),
            "red_flags": sum(1 for x in graded if x["red_flag"]),
            "deals": len(deals), "won": len(won),
            "won_amount": sum(d["amount"] or 0 for d in won) or None,
            "broken_promises": sum(1 for x in promises if not x["kept"]),
            "agents": sorted({x["agent_name"] for x in chats if x["agent_name"]}),
        },
        "chats": [{k: v for k, v in x.items() if k != "booking"} for x in chats],
        "deals": deals,
        "asks": asks,
        "promises": promises,
        "meta": texts.meta(),
    }


def search(q: str) -> list[dict[str, Any]]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    digits = re.sub(r"\D", "", q)
    if re.fullmatch(r"\+?[\d\s-]+", q) and digits:
        return db.rows(SQL_SEARCH_DIGITS, {"q": digits})
    # LIKE wildcards in a name are literal characters, not patterns.
    return db.rows(SQL_SEARCH_NAME, {"q": re.sub(r"([%_\\])", r"\\\1", q)})


def chat_fragment(interaction_id: str) -> str | None:
    row = db.one(SQL_CHAT, (interaction_id,))
    if not row or row["status"] != "scored":
        return None
    return render.chat_html(row)


def rules_fragment() -> str:
    return render.rules_html()


def promises(start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Every promise in the period's graded chats, broken first."""
    rows = db.rows(SQL_PROMISES, period(start, end))
    kept = sum(1 for x in rows if x["kept"])
    return {"totals": {"promises": len(rows), "kept": kept, "broken": len(rows) - kept},
            "rows": rows}
