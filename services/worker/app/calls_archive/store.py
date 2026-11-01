"""Keep one call recording in the call-recordings bucket.

n8n has already downloaded the bytes from the PBX (only n8n's IPs are
whitelisted there) and hands them over. This module answers what happened;
every outcome is a value, never an exception, because n8n writes it down
either way.

  stored      the bytes are in the bucket under calls/<yyyy>/<mm>/<dd>/<id>.wav
  not_audio   the bytes are not WAV/OGG/MP3/AMR (an HTML error page, an empty body)
  too_large   bigger than the cap; nothing kept
  failed      the bucket refused; retryable

The key is the call's own id under its day, so storing the same call twice
writes the same object: a retry after a lost answer cannot create a second copy.

WHAT `stored` DESCRIBES. The sha256, size and type in a `stored` answer are
always those of the object that is in the bucket afterwards, because n8n
writes them down as the archive's record of that object. When the key is
already taken (a retry after an answer was lost), the existing object is read
back and compared:

  same bytes          nothing is written; already_stored = true
  existing is longer  kept; the answer describes IT, not the new bytes. A
                      recording only grows, so the shorter copy is the
                      truncated one.
  new is longer       the new bytes replace it; replaced = true
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date

import httpx

from ..media.fetch import sniff
from ..media.s3 import S3Client, StorageUnavailable

log = logging.getLogger("worker.calls")

# ~1 MB per minute of 8 kHz PCM. 200 MiB is over three hours of audio; a call
# longer than that is a stuck channel, not a conversation worth scoring.
MAX_BYTES = int(os.getenv("CALLS_MAX_BYTES", str(200 * 1024 * 1024)))

AUDIO_MIMES = frozenset({"audio/wav", "audio/ogg", "audio/mpeg", "audio/amr"})
_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
UNIQUEID = re.compile(r"^[0-9]{6,12}\.[0-9]{1,9}$")


@dataclass
class StoreResult:
    outcome: str
    sha256: str | None = None
    bytes: int | None = None
    mime: str | None = None
    storage_key: str | None = None
    already_stored: bool | None = None
    replaced: bool | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def object_key(uniqueid: str, day: str) -> str:
    """calls/<yyyy>/<mm>/<dd>/<uniqueid>.wav, `day` = the call's local date."""
    m = _DAY.fullmatch(day or "")
    if not m:
        raise ValueError("day must be YYYY-MM-DD")
    try:
        date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise ValueError("day must be a real calendar date") from None
    if not UNIQUEID.fullmatch(uniqueid or ""):
        raise ValueError("uniqueid must look like 1793546440.46642")
    return f"calls/{m.group(1)}/{m.group(2)}/{m.group(3)}/{uniqueid}.wav"


def store_recording(uniqueid: str, day: str, data: bytes, *, store: S3Client,
                    max_bytes: int = MAX_BYTES) -> StoreResult:
    try:
        key = object_key(uniqueid, day)
    except ValueError as exc:
        return StoreResult("failed", error=str(exc))
    if len(data) > max_bytes:
        return StoreResult("too_large", bytes=len(data))
    if not data:
        return StoreResult("not_audio", bytes=0, error="empty body")
    mime = sniff(bytes(data[:64]), f"{uniqueid}.wav")
    if mime not in AUDIO_MIMES:
        return StoreResult("not_audio", bytes=len(data),
                           error=f"first bytes are {mime or 'text'}, not audio")
    sha = hashlib.sha256(data).hexdigest()
    try:
        existing = store.get_bytes(key) if store.exists(key) else None
        if existing is not None:
            old_sha = hashlib.sha256(existing).hexdigest()
            if old_sha == sha or len(existing) >= len(data):
                old_mime = sniff(bytes(existing[:64]), f"{uniqueid}.wav") or mime
                log.info("call recording kept id=%s bytes=%d same=%s", uniqueid, len(existing), old_sha == sha)
                return StoreResult("stored", sha256=old_sha, bytes=len(existing), mime=old_mime,
                                   storage_key=key, already_stored=True)
        store.put(key, data, sha256_hex=sha, content_type=mime)
    except (StorageUnavailable, httpx.HTTPError) as exc:
        return StoreResult("failed", sha256=sha, bytes=len(data), mime=mime,
                           error=f"bucket: {exc}"[:300])
    replaced = existing is not None
    log.info("call recording stored id=%s bytes=%d mime=%s replaced=%s", uniqueid, len(data), mime, replaced)
    return StoreResult("stored", sha256=sha, bytes=len(data), mime=mime,
                       storage_key=key, already_stored=False, replaced=replaced or None)
