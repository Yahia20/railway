"""HTTP endpoints for the media archive.

Machine side (workflow 09 calls these; n8n records every answer):
  POST /media/fetch    download one URL into the bucket
  POST /media/delete   remove objects and receipts whose last reference expired

Reader side lives in `reader.py`.
"""
from __future__ import annotations

import logging
import threading

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import fetch, links
from .s3 import S3Client, S3Config, StorageUnavailable

log = logging.getLogger("worker.media")

# httpx logs every request at INFO with the FULL URL, and the worker's root
# logger is INFO. A bitrix_rest source URL carries a live Bitrix REST token, so
# that one line would copy the credential into Railway's logs on every voice
# note. Requests are logged by this module instead, by url_hash.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_client: S3Client | None = None
_lock = threading.Lock()

# ONE bucket operation at a time in this process: a delete and a fetch of the
# same bytes must never interleave. Workflow 09 already runs one execution at a
# time, but an n8n HTTP timeout lets it move on while this process is still
# deleting — and a fetch that saw the old object, skipped its upload, and was
# then deleted underneath would be a stored file with no bytes. Railway runs
# one replica of this service; more replicas would need a bucket-level fence.
_bucket_ops = threading.Lock()

MAX_DELETE_OBJECTS = 50
MAX_DELETE_RECEIPTS = 200


def storage() -> S3Client:
    """The bucket client, built on first use so the worker boots without one."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                try:
                    _client = S3Client(S3Config.from_env())
                except StorageUnavailable as exc:
                    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return _client


# Request models live at module scope. Declared inside build_router() they are
# local classes, and with `from __future__ import annotations` FastAPI cannot
# resolve the string annotation — it then treats `req` as a missing QUERY
# parameter and answers 422 to every valid body (Codex/astra review, round 3).

class FetchRequest(BaseModel):
    url: str = Field(max_length=4096)
    file_name: str | None = Field(default=None, max_length=255)


class DeleteRequest(BaseModel):
    sha256: list[str] = Field(default_factory=list, max_length=MAX_DELETE_OBJECTS)
    url_hashes: list[str] = Field(default_factory=list, max_length=MAX_DELETE_RECEIPTS)


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def build_router(require_api_key) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_api_key)])

    @router.post("/media/fetch")
    def media_fetch(req: FetchRequest) -> dict:
        """One download. Always 200 with an `outcome` — a failed download is a
        result to record, not an error to retry at the HTTP layer. Only a
        missing bucket configuration is a 503, because then nothing can work.

        The response never echoes the URL: for `bitrix_rest` it carries a live
        REST token."""
        store = storage()
        with _bucket_ops:
            result = fetch.fetch_and_store(req.url, store=store, file_name=req.file_name)
        out = result.as_dict()
        out["url_hash"] = links.url_hash(req.url)
        log.info("media fetch %s family=%s outcome=%s http=%s bytes=%s",
                 out["url_hash"][:12], out.get("family"), out["outcome"],
                 out.get("http_status"), out.get("bytes"))
        return out

    @router.post("/media/delete")
    def media_delete(req: DeleteRequest) -> dict:
        """Delete objects, and the receipts of purged jobs. Idempotent: a key
        that is already gone counts as deleted. Answers per key, so n8n stamps
        only what really went.

        A receipt is READ before it is deleted. If it names bytes, those bytes
        are returned in `receipt_objects`: they were uploaded by a fetch whose
        answer never reached the database, so no media_objects row exists for
        them, and deleting the receipt would otherwise orphan them in the
        bucket forever. n8n registers them for deletion on the next run."""
        store = storage()
        objects: list[dict] = []
        receipts: list[dict] = []
        receipt_objects: list[dict] = []
        with _bucket_ops:
            for sha in req.sha256:
                if not _is_hex64(sha):
                    objects.append({"sha256": sha, "ok": False, "error": "not a sha256"})
                    continue
                try:
                    store.delete(fetch.object_key(sha))
                    objects.append({"sha256": sha, "ok": True})
                except (StorageUnavailable, httpx.HTTPError) as exc:
                    objects.append({"sha256": sha, "ok": False, "error": str(exc)[:200]})
            for uh in req.url_hashes:
                if not _is_hex64(uh):
                    receipts.append({"url_hash": uh, "ok": False, "error": "not a url hash"})
                    continue
                try:
                    prior = store.get_json(fetch.receipt_key(uh))
                    if prior and _is_hex64(str(prior.get("sha256", ""))):
                        receipt_objects.append({
                            "sha256": prior["sha256"], "bytes": prior.get("bytes"),
                            "mime": prior.get("mime")})
                    store.delete(fetch.receipt_key(uh))
                    receipts.append({"url_hash": uh, "ok": True})
                except (StorageUnavailable, httpx.HTTPError) as exc:
                    receipts.append({"url_hash": uh, "ok": False, "error": str(exc)[:200]})
        return {"objects": objects, "receipts": receipts, "receipt_objects": receipt_objects}

    return router
