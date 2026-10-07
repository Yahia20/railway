"""Pull a stored conversation back out, with its files in place.

  GET /conversations/{interaction_id}           one stored thread, exactly as stored
  GET /conversations/by-deal/{bitrix_deal_id}   every stored thread of a Bitrix deal
  GET /conversations                            the page that renders either

WHAT "AS STORED" MEANS, AND WHAT IT DOES NOT.
  * Order is stored thread order (sent_at, then message id). The chat API's
    `timestamp` is processing time at the source, not the moment the customer
    pressed send, so this is the order we received, not a verified send order.
  * A deal's threads are returned SEPARATELY. Since 2026-09-23 the API delivers
    each message twice on two conversation ids of the same deal. Merging them
    is a later, versioned step with its own fixture set (Codex/astra review
    2026-10-01); until then nothing is hidden. Each thread carries a
    `feed_hint` that is explicitly a guess.
  * `text` is the human part of a message: for an attachment, its caption with
    the file link removed; for anything else, the body with the Bitrix REST
    token removed. Ordinary links people typed are left alone.

ACCESS. The caller has the worker key (as for /report). A file is reached only
through its message here, never by hash, and every link handed out is a
presigned bucket URL that lives at most 5 minutes and never past the
attachment's own retention date. Links are transferable until they expire;
that is accepted for the owner-only reader (review C4).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from .. import db
from . import links
from .s3 import PRESIGN_SECONDS, StorageUnavailable

log = logging.getLogger("worker.media.reader")

SCHEMA_VERSION = 1


def _now() -> datetime:
    """The reader's one clock. Tests drive it; nothing else reads the time."""
    return datetime.now(timezone.utc)
MAX_MESSAGES_PER_THREAD = 3000
MAX_THREADS_PER_DEAL = 20
DEFAULT_RETENTION_DAYS = 90

KIND_BY_TYPE = {"image": "image", "audio": "audio", "video": "video", "document": "document",
                "sticker": "image", "file": "document"}

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-([0-9a-f])[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


SQL_INTERACTION = """
SELECT i.interaction_id::text AS interaction_id, i.external_id, i.external_deal_id,
       i.external_contact_id, i.started_at, i.ended_at, i.content_purged_at,
       i.message_count
FROM interactions i
WHERE i.interaction_id = %(iid)s::uuid AND i.external_source = 'bitrix_chat_api'
"""

SQL_DEAL_INTERACTIONS = """
SELECT i.interaction_id::text AS interaction_id, i.external_id, i.external_deal_id,
       i.external_contact_id, i.started_at, i.ended_at, i.content_purged_at,
       i.message_count
FROM interactions i
WHERE i.external_deal_id = %(deal)s AND i.external_source = 'bitrix_chat_api'
ORDER BY i.started_at, i.interaction_id
LIMIT %(limit)s
"""

SQL_MESSAGES = """
SELECT m.message_id::text AS message_id, m.seq, m.sender::text AS sender,
       m.sender_external_id, m.content_type, m.body, m.sent_at, m.created_at AS received_at
FROM chat_messages m
WHERE m.interaction_id = %(iid)s::uuid
ORDER BY m.sent_at, m.message_id
LIMIT %(limit)s
"""

SQL_MEDIA = """
SELECT cm.media_id::text AS media_id, cm.message_id::text AS message_id, cm.ordinal,
       cm.declared_type, cm.file_name, j.status, j.family, j.last_outcome,
       o.sha256, o.bytes, o.mime, o.storage_key, o.state AS object_state
FROM chat_media cm
JOIN media_fetch_jobs j ON j.job_id = cm.job_id
LEFT JOIN media_objects o ON o.sha256 = j.sha256
WHERE cm.message_id IN (SELECT message_id FROM chat_messages WHERE interaction_id = %(iid)s::uuid)
ORDER BY cm.message_id, cm.ordinal
"""

SQL_RETENTION = "SELECT value FROM media_archive_config WHERE key = 'retention_days'"

SQL_MEDIA_ONE = """
SELECT cm.media_id::text AS media_id, cm.file_name, cm.declared_type, j.status,
       o.bytes, o.mime, o.storage_key, o.state AS object_state,
       i.started_at, i.content_purged_at
FROM chat_media cm
JOIN chat_messages m     ON m.message_id = cm.message_id
JOIN interactions i      ON i.interaction_id = m.interaction_id
JOIN media_fetch_jobs j  ON j.job_id = cm.job_id
LEFT JOIN media_objects o ON o.sha256 = j.sha256
WHERE cm.media_id = %(mid)s::uuid AND i.external_source = 'bitrix_chat_api'
"""


# -- helpers ---------------------------------------------------------------------

def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def feed_hint(conversation_id: str | None) -> dict:
    """A labelled GUESS at which delivery path a thread came from. The two
    duplicate feeds were seen to use UUID v4 and v5 conversation ids; nothing
    has verified which sender is which (review C7)."""
    m = UUID_RE.match(conversation_id or "")
    value = f"uuid_v{m.group(1)}" if m else "other"
    return {"value": value, "basis": "conversation_id_pattern", "verified": False}


def _retention_days() -> int:
    try:
        row = db.one(SQL_RETENTION)
        return int(row.get("value") or DEFAULT_RETENTION_DAYS)
    except Exception:   # before 026 is applied the table does not exist
        return DEFAULT_RETENTION_DAYS


def _media_rows(iid: str) -> tuple[dict[str, list[dict]], str]:
    """Archived attachments by message id, and whether the archive exists."""
    try:
        rows = db.rows(SQL_MEDIA, {"iid": iid})
    except Exception as exc:
        # UndefinedTable before 026; anything else is reported, not hidden.
        if "does not exist" in str(exc):
            return {}, "not_installed"
        log.warning("media query failed for %s: %s", iid, exc)
        return {}, "error"
    by_message: dict[str, list[dict]] = {}
    for r in rows:
        by_message.setdefault(r["message_id"], []).append(r)
    return by_message, "ok"


def _presign(store, row: dict, deadline: datetime | None) -> str | None:
    """A link for one stored file, valid min(5 minutes, time left before the
    reference's retention date) — measured NOW, at signing. Computing the
    lifetime earlier and signing later lets a slow request hand out a link
    that outlives the reference (round-3 review)."""
    if store is None or row.get("object_state") != "present" or not row.get("storage_key"):
        return None
    if deadline is None:      # no retention date known: fail closed, no link
        return None
    left = (deadline - _now()).total_seconds()
    expires_in = int(min(PRESIGN_SECONDS, left))
    if expires_in < 1:
        return None
    name = row.get("file_name") or f"{row['media_id']}"
    return store.presign(row["storage_key"], content_type=row.get("mime"),
                         filename=name, expires=expires_in)


def _kind(declared: str | None, mime: str | None) -> str:
    if declared in KIND_BY_TYPE:
        return KIND_BY_TYPE[declared]
    if mime:
        top = mime.split("/", 1)[0]
        if top in ("image", "audio", "video"):
            return top
    return "document"


def _render_message(m: dict, archived: list[dict], store, deadline: datetime | None,
                    reference_alive: bool) -> dict:
    body = m.get("body") or ""
    found = links.find_links(body)
    media: list[dict] = []
    if archived:
        for a in archived:
            stored = a["status"] == "stored" and a.get("object_state") == "present"
            entry = {
                "id": a["media_id"], "ordinal": a["ordinal"],
                "kind": _kind(a.get("declared_type"), a.get("mime")),
                "name": a.get("file_name"), "mime": a.get("mime"),
                "bytes": int(a["bytes"]) if a.get("bytes") is not None else None,
                "status": a["status"] if a.get("object_state") != "deleted" else "purged",
                "access_url": None, "reason": None,
            }
            if not reference_alive:
                entry["status"], entry["reason"] = "purged", "past the retention date"
            elif stored:
                url = _presign(store, a, deadline)
                entry["access_url"] = url
                entry["reason"] = None if url else "storage not configured on the worker"
            else:
                entry["reason"] = {
                    "pending": "not downloaded yet", "fetching": "downloading now",
                    "retry_wait": "download failed, will retry",
                    "recovery_pending": "the source link was dead before it was saved",
                    "rejected": a.get("last_outcome") or "rejected",
                }.get(a["status"], a["status"])
            media.append(entry)
    else:
        # Not examined by the archive yet (or the archive is not installed).
        # Public links without a credential are shown as-is so the reader is
        # useful from day one; a link carrying a REST token never is.
        for i, link in enumerate(found):
            public = link.family in ("bitrix_short", "gupshup")
            media.append({
                "id": None, "ordinal": i,
                "kind": _kind(m.get("content_type"), None),
                "name": links.attachment_name(body), "mime": None, "bytes": None,
                "status": "not_archived",
                "access_url": link.url if public else None,
                "reason": None if public else "not archived, and the source link is "
                          + ("short-lived" if link.family == "gupconnector" else "credential-bearing"),
            })
    return {
        "id": m["message_id"], "seq": m["seq"],
        "at": _iso(m["sent_at"]), "received_at": _iso(m["received_at"]),
        "sender": {"role": m["sender"], "external_id": m.get("sender_external_id")},
        "declared_type": m.get("content_type"),
        "text": links.caption(body) if found else links.redact(body),
        "media": media,
    }


def _thread(meta: dict, store, retention_days: int, now: datetime) -> dict:
    iid = meta["interaction_id"]
    started = meta.get("started_at")
    expires_at = (started + timedelta(days=retention_days)) if isinstance(started, datetime) else None
    # Fail closed: a thread with no start date has no retention date, so it
    # is treated as expired rather than as forever.
    alive = expires_at is not None and expires_at > _now() and meta.get("content_purged_at") is None

    out = {
        "interaction_id": iid,
        "conversation_id": meta.get("external_id"),
        "deal_id": meta.get("external_deal_id"),
        "contact_id": meta.get("external_contact_id"),
        "started_at": _iso(started), "ended_at": _iso(meta.get("ended_at")),
        "retention_until": _iso(expires_at),
        "content_status": "available" if alive else "purged",
        "order": "stored_thread_order",
        "time_basis": "source_processing_time",
        "feed_hint": feed_hint(meta.get("external_id")),
        "messages": [], "truncated": False, "archive": "ok",
    }
    if not alive:
        return out
    msgs = db.rows(SQL_MESSAGES, {"iid": iid, "limit": MAX_MESSAGES_PER_THREAD + 1})
    out["truncated"] = len(msgs) > MAX_MESSAGES_PER_THREAD
    media, out["archive"] = _media_rows(iid)
    out["messages"] = [
        _render_message(m, media.get(m["message_id"], []), store, expires_at, alive)
        for m in msgs[:MAX_MESSAGES_PER_THREAD]
    ]
    return out


def _store_or_none():
    from .api import storage   # lazy: the reader must work with no bucket configured
    try:
        return storage()
    except HTTPException:
        return None


# -- routes ----------------------------------------------------------------------

def build_router(require_api_key) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_api_key)])

    @router.get("/conversations/by-deal/{deal_id}")
    def conversation_by_deal(deal_id: str) -> dict:
        if not re.fullmatch(r"\d{1,12}", deal_id):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "deal id must be digits")
        metas = db.rows(SQL_DEAL_INTERACTIONS, {"deal": deal_id, "limit": MAX_THREADS_PER_DEAL + 1})
        if not metas:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no stored conversation for this deal")
        store, days, now = _store_or_none(), _retention_days(), _now()
        return {
            "schema_version": SCHEMA_VERSION, "view": "source_threads", "deal_id": deal_id,
            "merged": False,
            "note": "Each stored thread is shown separately; duplicate deliveries are not merged yet.",
            "threads": [_thread(m, store, days, now) for m in metas[:MAX_THREADS_PER_DEAL]],
            "threads_truncated": len(metas) > MAX_THREADS_PER_DEAL,
        }

    @router.get("/conversations/media/{media_id}/link")
    def media_link(media_id: str) -> dict:
        """A fresh link for one attachment. The page asks for it when a link it
        was given has expired — an image scrolled to, or a voice note played,
        more than five minutes after the conversation was opened. Same checks
        as the conversation itself: through its message, within retention."""
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", media_id):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a media id")
        try:
            row = db.one(SQL_MEDIA_ONE, {"mid": media_id})
        except Exception as exc:
            if "does not exist" in str(exc):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "media archive not installed") from exc
            raise
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such attachment")
        started = row.get("started_at")
        deadline = started + timedelta(days=_retention_days()) if isinstance(started, datetime) else None
        if row.get("content_purged_at") is not None or deadline is None or deadline <= _now():
            raise HTTPException(status.HTTP_410_GONE, "past the retention date")
        if row.get("status") != "stored" or row.get("object_state") != "present":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "this file was not saved")
        url = _presign(_store_or_none(), row, deadline)
        if not url:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage not configured")
        return {"media_id": media_id, "access_url": url}

    @router.get("/conversations/{interaction_id}")
    def conversation(interaction_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", interaction_id):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not an interaction id")
        meta = db.one(SQL_INTERACTION, {"iid": interaction_id})
        if not meta:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such stored conversation")
        store, days, now = _store_or_none(), _retention_days(), _now()
        return {"schema_version": SCHEMA_VERSION, "view": "source_thread",
                "threads": [_thread(meta, store, days, now)]}

    return router
