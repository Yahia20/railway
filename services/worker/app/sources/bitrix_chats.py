"""Chats from Bitrix24.

Two ways in, both landing on the same `Conversation`:

1. `BitrixWebhookSource.parse(payload)` — the push path. This is the shape
   already captured in `api_response.txt`; it is what the bot posts on every
   customer message.
2. `BitrixRestSource.fetch_since()` — the pull path, for backfill and for
   catching anything the webhook dropped.

There are two inbound vocabularies for the same conversation and both land here:

    webhook            conversation API
    ----------------   ----------------
    dialog_id          conversation_id
    conversation_history  messages
    [].sender          [].sender_role
    [].message         [].content
    [].timestamp       [].timestamp   + [].content_type

Normalising them in this file is deliberate. Nothing downstream should have to
know which endpoint a conversation arrived through, and a second parser would
drift from this one the first time a field is added.

The webhook payload has three traps, all handled here:

* It resends the ENTIRE `conversation_history` every time. Ingest is therefore
  a full-thread upsert, never an append. Deduplication happens on the message
  hash in Postgres.
* It carries three identical copies of the deal object (`deal_info`,
  `deal_info_response.result`, `DealInfo.result`) and doubled keys
  (`dialog_id`/`Dialog`/`Dialoug`, `is_extranet`/`is_extrant`). `_pick` walks a
  fixed precedence list so two developers cannot read two different fields.
* Deal field `UF_CRM_1781281581` contains prose addressed to a bot. It is on the
  deny-list below and is never allowed near a model.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from .base import Conversation, Message, SpeakerRole

# Bitrix SOURCE_ID prefix -> our channel enum.
_CHANNEL_MAP = {
    "FACEBOOK": "facebook",
    "INSTAGRAM": "instagram",
    "WHATSAPP": "whatsapp",
    "TELEGRAM": "telegram",
    "LIVECHAT": "webchat",
    "OPENLINE": "webchat",
}

# Fields that must never be included in any LLM input, whatever the caller does.
# Mirrors crm_field_map.is_prompt_injection_risk; duplicated here so the
# guarantee holds even if the database row is missing.
DENY_FIELDS = frozenset({"UF_CRM_1781281581"})

# The only deal fields the extraction prompt is allowed to see. An allowlist,
# not a deny-list, because new UF_CRM_* fields appear without warning.
DEAL_FIELD_ALLOWLIST = frozenset({
    "ID", "TITLE", "STAGE_ID", "STAGE_SEMANTIC_ID", "CATEGORY_ID",
    "OPPORTUNITY", "CURRENCY_ID", "CONTACT_ID", "ASSIGNED_BY_ID",
    "SOURCE_ID", "DATE_CREATE", "DATE_MODIFY", "BEGINDATE", "CLOSEDATE",
})

# Field aliases, in precedence order. See the two-vocabularies table at the top.
_ID_KEYS = ("dialog_id", "Dialog", "Dialoug", "conversation_id", "id")
_HISTORY_KEYS = ("conversation_history", "messages", "ConversationHistory")
_TEXT_KEYS = ("message", "content", "text")
_SENDER_KEYS = ("sender", "sender_role", "role")
_TS_KEYS = ("timestamp", "sent_at", "created_at", "DATE_CREATE")
_DEAL_ID_KEYS = ("crm_entity_id", "deal_id", "dealid", "DealId", "bitrix_deal_id")

# `content_type` values that carry words a rubric can be scored on. An image, a
# file card or a system join notice is counted and dropped rather than passed on
# as an empty turn: an empty turn still sits between a question and its answer,
# so keeping it would corrupt every response-gap metric in metrics.py.
_SCOREABLE_CONTENT_TYPES = frozenset({"", "text", "plain", "message"})


def _pick(payload: dict, *paths: str, default=None):
    """First non-empty value along a fixed precedence list of dotted paths."""
    for path in paths:
        cur: Any = payload
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if cur not in (None, "", [], {}):
            return cur
    return default


def _role(sender: str | None, is_bot_flag: bool = False) -> SpeakerRole:
    s = (sender or "").strip().lower()
    if s in ("customer", "client", "user"):
        return "customer"
    if s == "bot" or is_bot_flag:
        return "bot"
    if s in ("agent", "operator", "manager", "employee"):
        return "agent"
    if s == "system":
        return "system"
    return "unknown"


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _deal_id(body: dict, history: list, deal: dict) -> str | None:
    """The deal this conversation belongs to, from whichever field carries it.

    Four places, in descending order of how directly they say it:

    1. the envelope — `crm_entity_id` (webhook) or `deal_id` (conversation API);
    2. a message row — the conversation API denormalises conversation-level
       columns onto every message, so a `/messages` response can carry the deal
       id even when its envelope does not;
    3. `deal_info.ID` — present whenever the deal object was attached at all.

    Returns None rather than guessing when none of them is set. A conversation
    with no deal is a real and common state (an enquiry that never converted);
    inventing a link would attach that enquiry's transcript to a stranger's sale.
    """
    found = _pick(body, *_DEAL_ID_KEYS)
    if not found:
        for item in history or []:
            if isinstance(item, dict):
                found = _pick(item, *_DEAL_ID_KEYS)
                if found:
                    break
    if not found:
        found = deal.get("ID")
    return str(found) if found else None


def _parse_notes(body: dict, history: list, dropped: list[str]) -> dict:
    """What this payload did not tell us straight, recorded next to the result.

    `message_count` is the source's own count of the thread. When it exceeds the
    number of messages actually delivered, the response is paginated or trimmed
    and we are holding a FRAGMENT. Scoring a fragment produces a confident number
    about a conversation nobody read — Module 5 in particular scores near zero on
    any thread cut off before the close. Surface it; never score around it.
    """
    declared = body.get("message_count")
    notes: dict[str, Any] = {
        "declared_message_count": declared,
        "received_message_count": len(history or []),
    }
    if isinstance(declared, int) and declared > len(history or []):
        notes["truncated"] = True
        notes["warning"] = (
            f"source declares {declared} messages but sent "
            f"{len(history or [])}; this thread is incomplete"
        )
    if dropped:
        notes["dropped_non_text"] = dropped
    return notes


def safe_deal_fields(deal: dict) -> dict:
    """The deal object reduced to what a model may safely be shown."""
    return {
        k: v for k, v in (deal or {}).items()
        if k in DEAL_FIELD_ALLOWLIST and k not in DENY_FIELDS
    }


class BitrixWebhookSource:
    """Parses the push payload. Stateless — no network, no credentials."""

    name = "bitrix"

    @staticmethod
    def parse(payload: dict) -> Conversation:
        body = payload.get("body", payload)

        dialog_id = _pick(body, *_ID_KEYS)
        if not dialog_id:
            raise ValueError("payload has no dialog id under any known alias")

        deal = _pick(body, "deal_info", "deal_info_response.result", "DealInfo.result",
                     default={}) or {}
        # The webhook only states the channel inside the deal's SOURCE_ID. The
        # conversation API states it outright, and often has no deal attached at
        # all — without this fallback every dealless thread lands as 'other',
        # which silently removes it from per-channel reporting.
        source_id = str(deal.get("SOURCE_ID")
                        or _pick(body, "channel", "source", default="") or "")
        channel = _CHANNEL_MAP.get(source_id.split("|")[-1].upper(), "other")

        history = _pick(body, *_HISTORY_KEYS, default=[]) or []
        messages: list[Message] = []
        dropped: list[str] = []
        for item in history:
            ctype = str(item.get("content_type") or "").strip().lower()
            text = str(_pick(item, *_TEXT_KEYS, default="") or "").strip()
            if ctype not in _SCOREABLE_CONTENT_TYPES or not text:
                dropped.append(ctype or "empty")
                continue
            messages.append(Message(
                seq=len(messages) + 1,
                sender=_role(_pick(item, *_SENDER_KEYS), bool(item.get("is_bot"))),
                body=text,
                sent_at=_parse_ts(_pick(item, *_TS_KEYS)),
            ))

        started = messages[0].sent_at if messages else _parse_ts(deal.get("DATE_CREATE"))
        ended = messages[-1].sent_at if messages else None

        return Conversation(
            external_id=str(dialog_id),
            external_source="bitrix",
            channel=channel,
            started_at=started,
            ended_at=ended,
            messages=messages,
            customer_phone_raw=_pick(body, "phone", "customer_phone", "user_phone"),
            agent_external_id=str(deal.get("ASSIGNED_BY_ID") or "") or None,
            bitrix_deal_id=_deal_id(body, history, deal),
            bitrix_contact_id=str(_pick(body, "contact_id", "ContactId") or "") or None,
            raw={
                "payload": payload,
                "deal_safe": safe_deal_fields(deal),
                "parse_notes": _parse_notes(body, history, dropped),
            },
        )


class BitrixRestSource:
    """Pull path, for backfill and gap-filling.

    NOTE: the exact method names below follow the documented Bitrix24 REST API
    (`imopenlines.*` / `im.dialog.messages.get`). They are unverified against
    your portal — nobody has run them against cultiv.bitrix24.com yet. Run
    `python -m app.sources.bitrix_chats --probe` once you have the inbound
    webhook URL; it reports which methods your portal actually exposes before
    anything depends on them.
    """

    name = "bitrix"

    def __init__(self, portal_domain: str, webhook_token: str, user_id: str = "1"):
        self.base = f"https://{portal_domain}/rest/{user_id}/{webhook_token}"
        self._client = httpx.Client(timeout=30.0)

    def call(self, method: str, **params) -> dict:
        r = self._client.post(f"{self.base}/{method}.json", json=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"bitrix {method}: {data.get('error_description', data['error'])}")
        return data

    def probe(self) -> dict[str, str]:
        """Which of the methods we need does this portal actually expose?"""
        wanted = [
            "imopenlines.session.history.get",
            "im.dialog.messages.get",
            "im.chat.get",
            "crm.deal.get",
            "user.get",
        ]
        out = {}
        for m in wanted:
            try:
                self.call(m)
                out[m] = "ok"
            except Exception as exc:  # noqa: BLE001 - we are probing on purpose
                out[m] = f"unavailable: {str(exc)[:120]}"
        return out

    def fetch_one(self, external_id: str) -> Conversation | None:
        raise NotImplementedError(
            "Pull path is not wired until the portal probe confirms which "
            "history method is available. Use BitrixWebhookSource meanwhile."
        )

    def fetch_since(self, since: datetime, limit: int = 500) -> Iterator[Conversation]:
        raise NotImplementedError(
            "Pull path is not wired until the portal probe confirms which "
            "history method is available. Use BitrixWebhookSource meanwhile."
        )


if __name__ == "__main__":  # pragma: no cover - operational helper
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Bitrix source helper")
    ap.add_argument("--probe", action="store_true", help="report which REST methods exist")
    ap.add_argument("--parse", metavar="FILE", help="parse a saved webhook payload")
    args = ap.parse_args()

    if args.probe:
        src = BitrixRestSource(
            os.environ["BITRIX_PORTAL_DOMAIN"], os.environ["BITRIX_WEBHOOK_TOKEN"]
        )
        print(json.dumps(src.probe(), indent=2))
    elif args.parse:
        with open(args.parse, encoding="utf-8") as fh:
            raw = json.load(fh)
        conv = BitrixWebhookSource.parse(raw[0] if isinstance(raw, list) else raw)
        print(f"{conv.external_id} · {conv.channel} · {len(conv.messages)} messages "
              f"· bot_only={conv.is_bot_only}")
        print(conv.transcript_text())
