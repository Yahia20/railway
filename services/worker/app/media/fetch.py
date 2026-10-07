"""Download one chat file and keep it in the bucket.

One call, one URL. n8n decides which URLs and when, and records the outcome;
this module only answers "what happened when we tried". Every outcome is a
value, never an exception, because the caller has to write it down either way.

  stored        the bytes are in the bucket under their sha256 (new or already there)
  expired       the source says the file is gone (404 / 410) — retrying cannot help
  too_large     bigger than the cap; nothing kept
  not_a_file    the source answered 200 with an HTML or JSON page instead of a file
  not_allowed   the URL, or a redirect, left the allow-list
  failed        anything retryable: timeouts, 5xx, 429, the bucket refusing
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from . import links
from .s3 import S3Client, StorageUnavailable

log = logging.getLogger("worker.media")

# WhatsApp's own ceiling (documents 100 MB; video and audio 16 MB, images 5 MB).
# Set at the platform maximum on purpose: anything WhatsApp can carry, we keep,
# so `too_large` can only mean a file the channel itself could not have sent.
# The file is buffered ONCE (one bytearray, no join copy), so peak payload
# memory per fetch is this cap. Workflow 09 fetches one file at a time.
MAX_BYTES = int(os.getenv("MEDIA_MAX_BYTES", str(100 * 1024 * 1024)))   # 100 MiB
MAX_REDIRECTS = 3

# Where a redirect may land. A file host often hands out a CDN link; that link
# must still be somewhere we chose, never a private address someone wrote into
# a chat. Exact hosts or a leading-dot suffix.
REDIRECT_HOSTS = tuple(
    h.strip().lower() for h in os.getenv(
        "MEDIA_REDIRECT_HOSTS",
        "travelgate.bitrix24.ae,gupconnector.cultivbureau.com,filemanager.gupshup.io,"
        ".bitrix24.ae,.gupshup.io,.whatsapp.net,.fbsbx.com",
    ).split(",") if h.strip())


def _redirect_allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username:
        return False
    host = (parts.hostname or "").lower()
    return any(host == h or (h.startswith(".") and host.endswith(h)) for h in REDIRECT_HOSTS)


def object_key(sha256_hex: str) -> str:
    return f"sha256/{sha256_hex[:2]}/{sha256_hex}"


def receipt_key(url_hash: str) -> str:
    """Where the outcome of a successful fetch is written, keyed by the URL.

    n8n records every result, but the response can be lost after the bytes are
    already in the bucket (an n8n timeout, a restart, a DNS blip like
    2026-09-30). A customer link is dead by the time the retry comes, so
    without this the retry would report a file we hold as `expired`. The
    receipt lets the retry answer `stored` without touching the source.
    """
    return f"receipts/{url_hash[:2]}/{url_hash}.json"


# -- content sniffing ---------------------------------------------------------
# The Content-Type a source sends is a claim; the first bytes are evidence. An
# expired link that answers 200 with an HTML error page must never be stored
# as somebody's passport.

OFFICE = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def sniff(head: bytes, file_name: str | None = None) -> str | None:
    """MIME type from magic bytes. None = text-like (HTML/JSON/etc.)."""
    ext = (file_name or "").rsplit(".", 1)[-1].lower() if file_name and "." in file_name else ""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"#!AMR"):
        return "audio/amr"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head[4:8] == b"ftyp":
        # ISO-BMFF is a container family, not a video format: iPhone photos
        # (HEIC) and AVIF images use it too. Only known brands get a type.
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand.startswith(b"3gp") or brand.startswith(b"3g2"):
            return "video/3gpp"
        if brand in (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42",
                     b"avc1", b"dash", b"MSNV", b"M4V "):
            return "video/mp4"
        return "application/octet-stream"
    if head.startswith(b"PK\x03\x04"):
        return OFFICE.get(ext, "application/zip")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return {"doc": "application/msword", "xls": "application/vnd.ms-excel"}.get(
            ext, "application/x-ole-storage")
    stripped = head.lstrip()
    if not stripped or stripped[:1] in (b"<", b"{", b"["):
        return None
    return "application/octet-stream"


@dataclass
class FetchResult:
    outcome: str
    family: str | None = None
    http_status: int | None = None
    sha256: str | None = None
    bytes: int | None = None
    mime: str | None = None
    already_stored: bool | None = None
    redirected_to_host: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        # Exception text can quote a URL (redirect targets, transport errors).
        # Redacted here, once, so no error path can carry a REST token into the
        # response, the job row, or a log line.
        if self.error:
            self.error = links.redact(self.error)

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def fetch_and_store(
    url: str,
    *,
    store: S3Client,
    http: httpx.Client | None = None,
    file_name: str | None = None,
    max_bytes: int = MAX_BYTES,
) -> FetchResult:
    family = links.classify(url)
    if family is None:
        return FetchResult("not_allowed", error="url is not in an allowed family")

    receipt = receipt_key(links.url_hash(url))
    try:
        prior = store.get_json(receipt)
        # A receipt only counts when the bytes it names are really there. It is
        # written BEFORE the object (see below), so a crash between the two
        # leaves a receipt with no object, and that case must fetch again.
        if prior and prior.get("sha256") and store.exists(object_key(prior["sha256"])):
            return FetchResult("stored", family, 200, sha256=prior["sha256"],
                               bytes=prior.get("bytes"), mime=prior.get("mime"),
                               already_stored=True,
                               redirected_to_host=prior.get("redirected_to_host"))
    except (StorageUnavailable, httpx.HTTPError) as exc:
        return FetchResult("failed", family, error=f"bucket: {exc}"[:300])

    owns_client = http is None
    client = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        return _download_and_store(url, family, receipt, client, store, file_name, max_bytes)
    finally:
        if owns_client:
            client.close()


def _download_and_store(url, family, receipt, client, store, file_name, max_bytes) -> FetchResult:
    current = url
    redirected_host = None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream("GET", current, follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(current, r.headers.get("location", ""))
                    if not _redirect_allowed(target):
                        return FetchResult("not_allowed", family, r.status_code,
                                           error=f"redirect to {urlsplit(target).hostname}")
                    current = target
                    redirected_host = urlsplit(target).hostname
                    continue
                if r.status_code in (404, 410):
                    return FetchResult("expired", family, r.status_code,
                                       redirected_to_host=redirected_host)
                if r.status_code != 200:
                    return FetchResult("failed", family, r.status_code,
                                       redirected_to_host=redirected_host,
                                       error=f"source answered HTTP {r.status_code}")
                declared = r.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    return FetchResult("too_large", family, 200, bytes=int(declared),
                                       redirected_to_host=redirected_host)
                digest = hashlib.sha256()
                data = bytearray()
                for chunk in r.iter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        return FetchResult("too_large", family, 200,
                                           bytes=len(data) + len(chunk),
                                           redirected_to_host=redirected_host)
                    digest.update(chunk)
                    data += chunk
                break
        else:
            return FetchResult("failed", family, error="too many redirects")
    except httpx.HTTPError as exc:
        return FetchResult("failed", family, error=f"{type(exc).__name__}: {exc}"[:300])

    if not data:
        return FetchResult("not_a_file", family, 200, bytes=0,
                           redirected_to_host=redirected_host, error="empty body")
    mime = sniff(bytes(data[:64]), file_name)
    if mime is None:
        return FetchResult("not_a_file", family, 200, bytes=len(data),
                           redirected_to_host=redirected_host,
                           error="source sent a text page, not a file")
    sha = digest.hexdigest()
    key = object_key(sha)
    try:
        # RECEIPT FIRST, THEN BYTES. A PUT is atomic — the object is either
        # wholly there or absent — so with this order every crash point is
        # recoverable: no receipt means nothing was stored; a receipt whose
        # object is missing means fetch again; a receipt whose object exists
        # means done. The other order leaves a window where the bytes exist and
        # nothing records which job they belong to, which for a customer link
        # that has since died is a file we hold and can never find.
        store.put_json(receipt, {"sha256": sha, "bytes": len(data), "mime": mime,
                                 "family": family, "redirected_to_host": redirected_host})
        already = store.exists(key)
        if not already:
            store.put(key, data, sha256_hex=sha, content_type=mime)
    except (StorageUnavailable, httpx.HTTPError) as exc:
        return FetchResult("failed", family, 200, sha256=sha, bytes=len(data), mime=mime,
                           error=f"bucket: {exc}"[:300])
    return FetchResult("stored", family, 200, sha256=sha, bytes=len(data), mime=mime,
                       already_stored=already, redirected_to_host=redirected_host)
