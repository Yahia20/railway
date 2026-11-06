"""One chat, read from the database and Bitrix, in the shape the v6 scorecard reads.

This is the trial's export (local-reports/v52-fix-2026-11-01/export_v6.py) as a function of one interaction, so
production builds the same text the approved trial was graded on: the same masking, the same quote splitting, the
same roles. Read-only: the database through app.db, Bitrix through crm.activity.list.

A chat is gradeable only when it can be judged fairly (owner decisions 2026-11-03):
  * its deal ended, won or lost; running deals are not graded,
  * the customer wrote something and a human agent replied,
  * the first human agent on it is not excluded from review (agents.qa_excluded, 034).
"""
from __future__ import annotations

import collections
import difflib
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any

from ... import db
from ..common import RIYADH
from ..engine import NotGradeable
from ..v4.transcript import ATTACHMENT_TYPES, mask, name_words

QUOTE = re.compile(r"^>>\s*(?:Bitrix agent:\s*)?(.*)$")
SPACE = re.compile(r"\s+")
STICKER = re.compile(r"^\[Unsupported message type: sticker\]")
REACTION = re.compile(r"^\[Unsupported message type: reaction\]")
NOT_A_SIGNATURE = r"حق|ان|إن|الان|الآن|في|على|كل|بس|يا|دايما|دائما|خطوة|لو|ما|من|كيف"
NOT_NAMES = {"the", "travel", "available", "a", "an", "my", "our", "your", "from", "only", "not", "for", "to"}
SIGNATURE = re.compile(rf"((?:معك|معاك|معكم|[Tt]his is)\s+)((?!(?:{NOT_A_SIGNATURE})\b)(?:(?!(?:من|في|from|at|in)\b)[^\s,،.!\-]+\s*){{1,3}})")
SALARY = re.compile(r"((?:الراتب|راتب|راتبي|مرتب|مرتبي|salary)\D{0,15})\d[\d,\.]*")
ATTACHMENT_TEXT = re.compile(r"\[Attachment:[^\]]*(?:\]|$)")     # a file name often holds the customer's name and booking code
PASSPORT = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,2}\d{6,9}(?![A-Za-z0-9])")

SYSTEM_TEXT = "[رسالة آلية - المحتوى محجوب]"
REACTION_TEXT = "[رد فعل بإيموجي]"
STICKER_TEXT = "[ملصق]"
FILE_TEXT = "[مرفق]"

_SQL_HEAD = """
SELECT i.interaction_id::text AS id, i.started_at, i.content_purged_at,
       d.stage_semantic, d.bitrix_deal_id, cu.display_name,
       coalesce(b.name, '') AS contact_first, coalesce(b.last_name, '') AS contact_last
FROM interactions i
LEFT JOIN deals d USING (deal_id)
LEFT JOIN bitrix_contacts b ON b.bitrix_contact_id = d.bitrix_contact_id
LEFT JOIN customers cu ON cu.customer_id = i.customer_id
WHERE i.interaction_id = %s AND i.external_source = 'bitrix_chat_api'
"""
_SQL_MESSAGES = """
SELECT m.sender::text AS sender, m.content_type, m.body, m.sent_at, m.sender_external_id AS ext,
       (SELECT cm.declared_type FROM chat_media cm WHERE cm.message_id = m.message_id ORDER BY cm.ordinal LIMIT 1) AS media_type,
       (SELECT cm.file_name FROM chat_media cm WHERE cm.message_id = m.message_id ORDER BY cm.ordinal LIMIT 1) AS file_name
FROM chat_messages m WHERE m.interaction_id = %s ORDER BY m.sent_at, m.seq
"""
_SQL_AGENTS = "SELECT bitrix_user_id, agent_id::text, full_name, is_bot, qa_excluded FROM agents WHERE bitrix_user_id IS NOT NULL"
# What this account usually signs with, from its own recent greetings. Two alphabets are kept apart: Habiba and
# حبيبة are one person.
_SQL_GREETINGS = """
SELECT body FROM chat_messages
WHERE sender = 'agent' AND sender_external_id = %s AND body IS NOT NULL AND sent_at > now() - interval '60 days'
ORDER BY sent_at DESC LIMIT 400
"""


def split_quote(body: str, earlier: list[str]) -> tuple[str | None, str]:
    """A reply arrives as '>>Bitrix agent: <quoted text><reply>' with no separator. The quoted text is an earlier
    message of the same chat, so the longest earlier message the remainder starts with is the quote."""
    m = QUOTE.match(body)
    if not m:
        return None, body
    flat = SPACE.sub(" ", m.group(1)).strip()
    best = ""
    for p in earlier:
        q = SPACE.sub(" ", p).strip()
        if len(q) >= 3 and flat.startswith(q) and len(q) > len(best):
            best = q
    if best:
        return f"(replying to: {best})", flat[len(best):].strip()
    # the quoted message may itself have been cut at 239 characters: take the longest shared start
    for p in earlier:
        q = SPACE.sub(" ", p).strip()
        k = 0
        while k < min(len(q), len(flat)) and q[k] == flat[k]:
            k += 1
        if k >= 12 and k > len(best):
            best = flat[:k]
    if best:
        return f"(replying to: {best})", flat[len(best):].strip()
    return "(replying to: an earlier message)", flat


def mask_more(text: str) -> str:
    """On top of the production masker: staff signatures, file names, passport numbers and salaries."""
    text = SIGNATURE.sub(lambda m: m.group(1) + "[اسم الموظف] ", text)
    text = ATTACHMENT_TEXT.sub(FILE_TEXT, text)
    text = PASSPORT.sub("[رقم]", text)
    return SALARY.sub(lambda m: m.group(1) + "[مبلغ]", text)


def signature_word(text: str | None) -> str | None:
    m = SIGNATURE.search(text or "")
    if not m:
        return None
    word = m.group(2).split()[0]
    return None if word.lower() in NOT_NAMES else word


def is_latin(word: str) -> bool:
    return bool(re.match(r"[A-Za-z]", word))


def signed_by_someone_else(word: str, account_name: str | None, latin_usual: str | None, arabic_usual: str | None) -> bool:
    """An account signed with a different name, in the same alphabet. Spelling is forgiven (Merna / Mirna)."""
    close = lambda a, b: difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.6
    if is_latin(word):
        known = [k for k in (account_name or "").split() if is_latin(k)] or ([latin_usual] if latin_usual else [])
        return bool(known) and not any(close(word, k) for k in known)
    return bool(arabic_usual) and not close(word, arabic_usual)


def usual_signatures(ext: str) -> tuple[str | None, str | None]:
    """(latin, arabic) name this account most often signs its greetings with."""
    counts = collections.Counter(w for r in db.rows(_SQL_GREETINGS, (ext,)) if (w := signature_word(r["body"])))
    pick = lambda latin: next((w for w, _ in counts.most_common() if is_latin(w) == latin), None)
    return pick(True), pick(False)


def _local(at: datetime) -> datetime:
    return at.astimezone(RIYADH)


def _riyadh_text(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(RIYADH).strftime("%Y-%m-%dT%H:%M")


def bitrix_activities(deal_id: str | None, client: Any | None) -> list[dict] | None:
    """The deal's activities, oldest first. None when Bitrix could not be read: the scorecard then has no
    assignment time and no call records, which it treats as unknown, never as absent."""
    if not deal_id or client is None:
        return None
    try:
        data = client.call("crm.activity.list", filter={"OWNER_TYPE_ID": 2, "OWNER_ID": deal_id},
                           select=["PROVIDER_ID", "TYPE_ID", "CREATED", "RESPONSIBLE_ID"], order={"CREATED": "ASC"})
    except Exception:
        return None
    return data.get("result") or []


def build(interaction_id: str, bitrix: Any | None = None) -> dict[str, Any]:
    """The chat the scorecard reads. Raises NotGradeable with the reason when it must not be graded."""
    head = db.one(_SQL_HEAD, (interaction_id,))
    if not head:
        raise NotGradeable("no such chat conversation")
    if head["content_purged_at"]:
        raise NotGradeable("the conversation text was purged")
    if head["stage_semantic"] not in ("S", "F"):
        raise NotGradeable("the deal is still open" if head["stage_semantic"] else "no deal is linked")

    staff = {r["bitrix_user_id"]: r for r in db.rows(_SQL_AGENTS)}
    bots = {ext for ext, r in staff.items() if r["is_bot"]}
    rows = db.rows(_SQL_MESSAGES, (interaction_id,))

    def role_of(r: dict) -> str:
        if r["sender"] == "agent" and r["ext"] == "1":
            return "system"
        if r["sender"] == "agent" and r["ext"] in bots:
            return "bot"
        return r["sender"]

    speaking = [role_of(r) for r in rows
                if not (r["content_type"] == "reaction" or REACTION.match((r["body"] or "").strip()))]
    if "customer" not in speaking:
        raise NotGradeable("the customer never wrote")
    if "agent" not in speaking:
        raise NotGradeable("no human agent replied")

    words = name_words({"name": " ".join(x for x in (head["display_name"], head["contact_first"], head["contact_last"]) if x)})
    msgs: list[dict[str, Any]] = []
    senders: list[str] = []
    signed = None
    for r in rows:
        role, raw_body = role_of(r), (r["body"] or "").strip()
        reply_to, text, attachment = None, "", None
        if r["content_type"] == "reaction" or REACTION.match(raw_body):
            role, text = "system", REACTION_TEXT
        elif role == "system":
            text = SYSTEM_TEXT
        elif r["content_type"] == "sticker" or STICKER.match(raw_body):
            text = STICKER_TEXT
        elif r["content_type"] in ATTACHMENT_TYPES:
            attachment = r["media_type"] or r["content_type"]
            text = FILE_TEXT
            if attachment == "document" and r["file_name"]:     # the kind of file, never its name
                text = f"{FILE_TEXT} ملف {os.path.splitext(r['file_name'])[1].lstrip('.').lower()}".rstrip()
        else:
            if role == "agent":
                signed = signed or signature_word(raw_body)
            reply_to, text = split_quote(mask_more(mask(raw_body, words)), [x["text"] for x in msgs])
            text = text or FILE_TEXT
        local = _local(r["sent_at"])
        if role == "agent" and r["ext"] not in senders:
            senders.append(r["ext"])
        msgs.append({"role": role, "time": local.strftime("%H:%M"), "reply_to": reply_to, "text": text,
                     "date": local.strftime("%m-%d"), "at": local.strftime("%Y-%m-%dT%H:%M"),
                     "sender": r["ext"] if role == "agent" else None, "attachment": attachment})

    first = staff.get(senders[0]) or {}
    if first.get("qa_excluded"):
        raise NotGradeable("the agent is excluded from QA review")
    latin_usual, arabic_usual = usual_signatures(senders[0])
    if first.get("full_name"):
        employee = first["full_name"]
    else:                       # not on the roster yet: the name the account signs with, and its Bitrix id
        employee = f"{arabic_usual or latin_usual} (مستخدم {senders[0]})" if (arabic_usual or latin_usual) else f"مستخدم {senders[0]}"
    if len(senders) > 1:
        attribution = "shared"
    elif signed and signed_by_someone_else(signed, first.get("full_name"), latin_usual, arabic_usual):
        attribution = "uncertain"
    else:
        attribution = "single"

    acts = bitrix_activities(head["bitrix_deal_id"], bitrix)
    assigned_at, calls = None, []
    for a in acts or []:
        if str(a.get("TYPE_ID")) == "2":
            calls.append(_riyadh_text(a["CREATED"]))
        elif a.get("PROVIDER_ID") == "CRM_TASKS_TASK" and assigned_at is None and str(a.get("RESPONSIBLE_ID")) == senders[0]:
            assigned_at = _riyadh_text(a["CREATED"])

    return {"id": head["id"], "employee": employee,
            "agents": [(staff.get(x) or {}).get("full_name") or f"مستخدم {x}" for x in senders],
            "senders": senders, "agent_id": first.get("agent_id"),
            "date": _local(head["started_at"]).strftime("%Y-%m-%d"),
            "outcome": "won" if head["stage_semantic"] == "S" else "lost", "deal_stage": head["stage_semantic"],
            "assigned_at": assigned_at, "calls": calls, "bitrix_read": acts is not None,
            "attribution": attribution, "messages": msgs,
            "input_hash": hashlib.sha256(json.dumps(msgs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]}
