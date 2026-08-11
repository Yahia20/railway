"""Drive the whole chats pipeline from the conversation simulator API.

    export SIM_BASE_URL=https://<tunnel>.trycloudflare.com
    export SIM_API_KEY=tg_...
    export DEEPSEEK_API_KEY=...

    python scripts/simulate_conversation.py --list
    python scripts/simulate_conversation.py <conversation_id>
    python scripts/simulate_conversation.py <conversation_id> --webhook

The simulator stands in for Bitrix while the client's IT team wires up the real
push. It exposes `/conversations`, `/conversations/{id}/messages`, `/deals` and
`/simulate/push`.

WHY THIS SCRIPT JOINS THE DEAL ITSELF
-------------------------------------
`/conversations/{id}/messages` returns message rows and no deal id, so on its own
it cannot tell you which sale a conversation belongs to. Every commercial number
this system produces — pipeline value, win rate, revenue per agent — is a join
from a conversation to a deal, so a transcript with no deal id is scoreable but
commercially blind.

`--deal-field` names where the deal id lives once the simulator carries it. Until
then this script falls back to `/deals`, matching on conversation id and then on
phone, and says which route it used. It never guesses: an unmatched conversation
is reported unmatched. See `resolve_deal_id`.

MODES
-----
default     fetch -> parse -> metrics -> pass 1 -> pass 2 -> score, all local.
            This is the same library code the n8n workflow calls, so the scores
            are real; only the transport differs.
--webhook   POST the assembled payload at the live n8n webhook instead, exactly
            as Bitrix will. Use scripts/n8n_smoke_test.py to watch it land.
--offline   stop before the two AI passes. Needs no DeepSeek key. Verifies the
            payload shape, the deal join and every computed metric.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))

from app.evaluate import judge                                  # noqa: E402
from app.evaluate.metrics import compute_chat_metrics           # noqa: E402
from app.normalize.phone import PhoneError, normalize_phone     # noqa: E402
from app.sources.bitrix_chats import BitrixWebhookSource        # noqa: E402

N8N_WEBHOOK = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-production-a685c.up.railway.app/webhook/travelgate/chat",
)


class Sim:
    """Thin client for the simulator API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0):
        self.c = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    def get(self, path: str, **params) -> Any:
        r = self.c.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    def conversations(self, limit: int = 20) -> list[dict]:
        return _rows(self.get("/conversations", limit=limit))

    def messages(self, conversation_id: str) -> dict:
        return self.get(f"/conversations/{conversation_id}/messages")

    def deals(self, limit: int = 200) -> list[dict]:
        return _rows(self.get("/deals", limit=limit))


def _rows(payload: Any) -> list[dict]:
    """The list inside whatever envelope the endpoint used.

    Written defensively because the simulator's list shape is not pinned down:
    it may return a bare array or wrap it under any of the keys below. Guessing
    wrong here would look like "no conversations exist".
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("conversations", "deals", "items", "results", "data", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _first(row: dict, *keys: str):
    for k in keys:
        v = row.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


# ---------------------------------------------------------------------------
# The deal join — the thing this script exists to add
# ---------------------------------------------------------------------------

def resolve_deal_id(conv_row: dict, msg_response: dict, deals: list[dict],
                    deal_field: str | None = None) -> tuple[str | None, str]:
    """Find this conversation's deal id. Returns (deal_id, how_we_got_it).

    Four routes, tried in order of how directly the source states the link. The
    route is returned alongside the id and printed, because a deal id matched on
    a phone number is a much weaker claim than one the API stated outright, and
    the difference must not vanish into a column that looks identical either way.
    """
    if deal_field:
        for src, where in ((msg_response, "messages"), (conv_row, "conversation")):
            value = (src or {}).get(deal_field)
            if value:
                return str(value), f"{deal_field} on the {where} response"

    for key in ("deal_id", "crm_entity_id", "dealid", "bitrix_deal_id"):
        for src, where in ((msg_response, "messages"), (conv_row, "conversation")):
            value = (src or {}).get(key)
            if value:
                return str(value), f"{key} on the {where} response"

    # Denormalised onto the message rows rather than the envelope.
    for m in (msg_response or {}).get("messages") or []:
        value = _first(m, "deal_id", "crm_entity_id")
        if value:
            return str(value), "deal_id on the message rows"

    conv_id = _first(conv_row, "conversation_id", "id")
    for d in deals:
        if conv_id and str(_first(d, "conversation_id", "chat_id", "dialog_id") or "") == str(conv_id):
            return str(_first(d, "deal_id", "id", "ID")), "/deals joined on conversation_id"

    phone = _norm(_first(conv_row, "phone", "customer_phone", "contact_phone"))
    if phone:
        hits = [d for d in deals
                if _norm(_first(d, "phone", "customer_phone", "contact_phone")) == phone]
        if len(hits) == 1:
            return str(_first(hits[0], "deal_id", "id", "ID")), "/deals joined on phone (weak)"
        if len(hits) > 1:
            # Two deals, one phone: a repeat customer. Picking either one attaches
            # this transcript to a sale it may have nothing to do with.
            return None, f"/deals phone match was ambiguous ({len(hits)} deals)"

    return None, "unmatched — no deal id in any field and no /deals row fits"


def _norm(phone: str | None) -> str | None:
    if not phone:
        return None
    try:
        return normalize_phone(phone, os.getenv("DEFAULT_PHONE_REGION", "SA"))
    except PhoneError:
        return None


# ---------------------------------------------------------------------------
# Simulator response -> the payload our pipeline consumes
# ---------------------------------------------------------------------------

def build_payload(conv_row: dict, msg_response: dict, deal_id: str | None,
                  deal_row: dict | None) -> dict:
    """The webhook payload, with the deal id present.

    Deliberately emitted in the shape Bitrix will send rather than the shape the
    simulator returned, so that what we exercise here is byte-for-byte what
    production will receive. The parser accepts both vocabularies; the point of
    normalising at this seam is that the n8n workflow downstream sees one shape.
    """
    conv_id = str(_first(conv_row, "conversation_id", "id") or
                  msg_response.get("conversation_id"))

    history = []
    for m in msg_response.get("messages") or []:
        history.append({
            "sender": _first(m, "sender_role", "sender", "role"),
            "sender_id": m.get("sender_id"),
            "message": _first(m, "content", "message", "text"),
            "timestamp": _first(m, "timestamp", "sent_at", "created_at"),
            "content_type": m.get("content_type", "text"),
        })

    payload: dict[str, Any] = {
        "dialog_id": conv_id,
        "conversation_id": conv_id,
        "message_count": msg_response.get("message_count", len(history)),
        "phone": _first(conv_row, "phone", "customer_phone", "contact_phone"),
        "contact_id": _first(conv_row, "contact_id", "customer_id"),
        # Stated outright here. The webhook can only infer it from the deal's
        # SOURCE_ID, which a dealless enquiry does not have.
        "channel": _first(conv_row, "channel", "source"),
        "conversation_history": history,
    }
    if deal_id:
        payload["crm_entity_type"] = "DEAL"
        payload["crm_entity_id"] = deal_id
        payload["deal_id"] = deal_id
    if deal_row:
        payload["deal_info"] = _deal_info(deal_row, deal_id)
    return payload


def _deal_info(deal_row: dict, deal_id: str | None) -> dict:
    """Map a simulator deal row onto the Bitrix field names the parser reads.

    Passed through a fixed key map rather than copied wholesale: rule 7 in
    CLAUDE.md exists because a raw Bitrix deal carries a UF_CRM_* field holding
    prose addressed to a bot, and `safe_deal_fields` can only filter fields it
    recognises. Anything not named here never reaches a model.
    """
    mapping = {
        "ID": ("deal_id", "id", "ID"),
        "TITLE": ("title", "name", "TITLE"),
        "STAGE_ID": ("stage", "stage_id", "STAGE_ID"),
        "OPPORTUNITY": ("amount", "value", "opportunity", "OPPORTUNITY"),
        "CURRENCY_ID": ("currency", "currency_id", "CURRENCY_ID"),
        "ASSIGNED_BY_ID": ("agent_id", "assigned_to", "ASSIGNED_BY_ID"),
        "SOURCE_ID": ("source", "channel", "SOURCE_ID"),
        "CONTACT_ID": ("contact_id", "customer_id", "CONTACT_ID"),
        "DATE_CREATE": ("created_at", "date_create", "DATE_CREATE"),
    }
    out = {k: _first(deal_row, *aliases) for k, aliases in mapping.items()}
    out = {k: v for k, v in out.items() if v is not None}
    out.setdefault("ID", deal_id)
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def show_ingest(conv, metrics, deal_id: str | None, route: str) -> None:
    notes = conv.raw.get("parse_notes", {})
    print("── ingest ───────────────────────────────────────────────")
    print(f"  external_id      : {conv.external_id}")
    print(f"  channel          : {conv.channel}")
    print(f"  deal_id          : {deal_id or '(none)'}   [{route}]")
    print(f"  contact_id       : {conv.bitrix_contact_id or '(none)'}")
    print(f"  phone (raw/e164) : {conv.customer_phone_raw or '-'} / "
          f"{_norm(conv.customer_phone_raw) or 'FAILED TO NORMALISE'}")
    print(f"  messages         : {len(conv.messages)} "
          f"(declared {notes.get('declared_message_count', '?')})")
    print(f"  bot only         : {conv.is_bot_only}")
    if notes.get("warning"):
        print(f"  ! {notes['warning']}")
    if notes.get("dropped_non_text"):
        print(f"  ! dropped non-text: {notes['dropped_non_text']}")

    print("\n── computed metrics (arithmetic, never the model) ───────")
    for k, v in metrics.as_dict().items():
        if v is not None:
            print(f"  {k:28s} {v}")


def show_scores(p1, p2) -> None:
    print("\n── pass 1 · what the customer wants ─────────────────────")
    print(json.dumps(p1.payload, ensure_ascii=False, indent=2))

    print("\n── pass 2 · how the agent handled it ────────────────────")
    print(f"  final_score      : {p2.score.final_score}")
    print(f"  performance_level: {p2.score.performance_level}")
    print(f"  weight_applied   : {p2.score.weight_applied}")
    print(f"  gradeable        : {p2.score.gradeable}")
    print(f"  stage_reached    : {p2.payload.get('stage_reached')}")
    for key, value in p2.score.modules.items():
        print(f"    {key:24s} {'null (did not arise)' if value is None else value}")
    if p2.warnings:
        print("\n  warnings:")
        for w in p2.warnings:
            print(f"    ! {w}")
    summary = p2.payload.get("summary") or {}
    if summary:
        print("\n  coaching:")
        print(json.dumps(summary, ensure_ascii=False, indent=4))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("conversation_id", nargs="?", help="omit with --list")
    ap.add_argument("--base-url", default=os.getenv("SIM_BASE_URL"))
    ap.add_argument("--api-key", default=os.getenv("SIM_API_KEY"))
    ap.add_argument("--list", action="store_true", help="list conversations and exit")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--all", action="store_true", help="run every listed conversation")
    ap.add_argument("--deal-field", default=os.getenv("SIM_DEAL_FIELD"),
                    help="field name carrying the deal id, once the API adds it")
    ap.add_argument("--webhook", action="store_true", help="POST to n8n instead of scoring locally")
    ap.add_argument("--offline", action="store_true", help="stop before the AI passes")
    ap.add_argument("--out", type=Path, help="write the full result as JSON")
    args = ap.parse_args()

    if not args.base_url or not args.api_key:
        print("SIM_BASE_URL and SIM_API_KEY must be set (or --base-url/--api-key)",
              file=sys.stderr)
        return 2

    sim = Sim(args.base_url, args.api_key)

    conversations = sim.conversations(limit=args.limit)
    if args.list or not (args.conversation_id or args.all):
        print(f"{len(conversations)} conversations at {args.base_url}\n")
        for row in conversations:
            cid = _first(row, "conversation_id", "id")
            print(f"  {cid}  {_first(row, 'phone', 'customer_phone') or '-':<16} "
                  f"{_first(row, 'message_count', 'messages') or '?'} msgs  "
                  f"{_first(row, 'channel', 'source') or '-'}")
        if not args.list:
            print("\nPass a conversation_id, or --all, to run the pipeline.")
        return 0

    try:
        deals = sim.deals()
    except httpx.HTTPError as exc:
        print(f"note: /deals unavailable ({exc}); deal join limited to the "
              f"conversation payload\n")
        deals = []

    targets = conversations if args.all else [
        next((c for c in conversations
              if str(_first(c, "conversation_id", "id")) == args.conversation_id),
             {"conversation_id": args.conversation_id})
    ]

    results, failures = [], 0
    for row in targets:
        cid = str(_first(row, "conversation_id", "id"))
        print("=" * 60)
        print(f"conversation {cid}")
        print("=" * 60)
        try:
            results.append(run_one(sim, row, deals, args))
        except Exception as exc:  # noqa: BLE001 — one bad conversation must not stop the batch
            failures += 1
            print(f"  FAILED: {type(exc).__name__}: {exc}")
        print()

    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        print(f"written: {args.out}")

    print(f"{len(results)} succeeded, {failures} failed")
    return 1 if failures else 0


def run_one(sim: Sim, conv_row: dict, deals: list[dict], args) -> dict:
    cid = str(_first(conv_row, "conversation_id", "id"))
    msg_response = sim.messages(cid)

    deal_id, route = resolve_deal_id(conv_row, msg_response, deals, args.deal_field)
    deal_row = next(
        (d for d in deals if str(_first(d, "deal_id", "id", "ID") or "") == str(deal_id)),
        None,
    ) if deal_id else None

    payload = build_payload(conv_row, msg_response, deal_id, deal_row)

    if args.webhook:
        r = httpx.post(N8N_WEBHOOK, json=payload, timeout=90.0)
        print(f"  POST {N8N_WEBHOOK} -> HTTP {r.status_code} {r.text[:80]!r}")
        print(f"  deal_id sent: {deal_id or '(none)'}  [{route}]")
        r.raise_for_status()
        return {"conversation_id": cid, "deal_id": deal_id, "posted": True,
                "status": r.status_code}

    conv = BitrixWebhookSource.parse(payload)
    metrics = compute_chat_metrics(conv)
    show_ingest(conv, metrics, deal_id, route)

    result: dict[str, Any] = {
        "conversation_id": cid,
        "deal_id": deal_id,
        "deal_id_route": route,
        "metrics": metrics.as_dict(),
        "parse_notes": conv.raw.get("parse_notes"),
        "payload": payload,
    }

    if conv.is_bot_only:
        # Rule: grading a human on the qualification bot's messages corrupts
        # every QA number, so the pipeline stops here in production too.
        print("\n  bot-only thread — excluded from agent scoring, as in production")
        result["scored"] = False
        return result

    if args.offline:
        print("\n  --offline: stopping before the two AI passes")
        result["scored"] = False
        return result

    if not os.getenv("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY is not set — rerun with --offline "
                           "to check ingest only")

    transcript = conv.transcript_text()
    metadata = {
        "channel": conv.channel,
        "started_at": conv.started_at.isoformat(),
        "deal_id": deal_id,
        **{k: v for k, v in metrics.as_dict().items() if v is not None},
    }

    client = judge.DeepSeekClient()
    p1 = judge.run_pass1(transcript, client=client)
    # followup_history stays unset: whether the agent followed up afterwards is
    # not visible in a single thread, and Module 4 must score null rather than
    # be guessed at.
    p2 = judge.run_pass2(transcript, "chat", metadata=metadata, client=client)
    show_scores(p1, p2)

    result.update({
        "scored": True,
        "pass1": p1.payload,
        "pass2": p2.payload,
        "score": {
            "final": p2.score.final_score,
            "level": p2.score.performance_level,
            "weight_applied": p2.score.weight_applied,
            "modules": p2.score.modules,
        },
        "warnings": p2.warnings,
        "usage": {"pass1": p1.usage, "pass2": p2.usage},
    })
    return result


if __name__ == "__main__":
    sys.exit(main())
