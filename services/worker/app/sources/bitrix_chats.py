"""Chats from Bitrix24.

Two ways in, both landing on the same `Conversation`:

1. `BitrixWebhookSource.parse(payload)` — the push path. This is the shape
   already captured in `api_response.txt`; it is what the bot posts on every
   customer message.
2. `BitrixRestSource.fetch_since()` — the pull path, for backfill and for
   catching anything the webhook dropped.

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

        dialog_id = _pick(body, "dialog_id", "Dialog", "Dialoug")
        if not dialog_id:
            raise ValueError("payload has no dialog id under any known alias")

        deal = _pick(body, "deal_info", "deal_info_response.result", "DealInfo.result",
                     default={}) or {}
        source_id = str(deal.get("SOURCE_ID", ""))
        channel = _CHANNEL_MAP.get(source_id.split("|")[-1].upper(), "other")

        history = body.get("conversation_history") or []
        messages: list[Message] = []
        for i, item in enumerate(history, start=1):
            text = (item.get("message") or "").strip()
            if not text:
                continue
            messages.append(Message(
                seq=i,
                sender=_role(item.get("sender")),
                body=text,
                sent_at=_parse_ts(item.get("timestamp")),
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
            bitrix_deal_id=str(_pick(body, "crm_entity_id", "deal_id", "dealid") or "") or None,
            bitrix_contact_id=str(_pick(body, "contact_id", "ContactId") or "") or None,
            raw={"payload": payload, "deal_safe": safe_deal_fields(deal)},
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
