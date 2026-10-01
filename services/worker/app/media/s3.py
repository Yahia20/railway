"""A minimal S3 client: PUT, HEAD, DELETE and presigned GET, signed with AWS
Signature Version 4.

Written against the standard library and the `httpx` the worker already ships,
instead of adding boto3 (~80 MB installed) for four calls. The signing is the
part that has to be exactly right, so it is tested against the worked examples
AWS publishes for SigV4 (tests/test_media_s3.py).

Railway buckets are S3-compatible, private, region `auto`, and use
virtual-hosted addressing (`https://<bucket>.<endpoint-host>/<key>`). Older
buckets want path style (`https://<endpoint-host>/<bucket>/<key>`); the
credentials tab says which, and S3_URL_STYLE selects it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import quote, urlsplit

import httpx

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED = "UNSIGNED-PAYLOAD"

# Presigned GETs are handed to a browser. Long enough to open a conversation
# and play a voice note; short enough that a link pasted into a chat is dead
# before anyone else reads it.
PRESIGN_SECONDS = 300


class StorageUnavailable(RuntimeError):
    """The bucket is not configured, or answered something we cannot use."""


def _uri_encode(value: str, *, keep_slash: bool) -> str:
    """S3's own encoding rule: everything except A-Z a-z 0-9 - _ . ~ is
    percent-encoded, and '/' survives only in the object path."""
    return quote(value, safe="-_.~/" if keep_slash else "-_.~")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str = "s3") -> bytes:
    k = _hmac(("AWS4" + secret).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


@dataclass(frozen=True)
class S3Config:
    endpoint: str          # https://t3.storageapi.dev
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"
    url_style: str = "virtual"   # or "path"

    @classmethod
    def from_env(cls) -> "S3Config":
        """Read the bucket's credentials. Railway exposes them as BUCKET,
        ACCESS_KEY_ID, SECRET_ACCESS_KEY, ENDPOINT and REGION on the bucket;
        the worker receives them as S3_* through variable references so the
        names cannot collide with anything else in its environment."""
        values = {
            "S3_ENDPOINT": os.getenv("S3_ENDPOINT", "").strip(),
            "S3_BUCKET": os.getenv("S3_BUCKET", "").strip(),
            "S3_ACCESS_KEY_ID": os.getenv("S3_ACCESS_KEY_ID", "").strip(),
            "S3_SECRET_ACCESS_KEY": os.getenv("S3_SECRET_ACCESS_KEY", "").strip(),
        }
        missing = sorted(k for k, v in values.items() if not v)
        if missing:
            raise StorageUnavailable("missing required environment variables: " + ", ".join(missing))
        style = (os.getenv("S3_URL_STYLE") or "virtual").strip().lower()
        if style not in ("virtual", "path"):
            raise StorageUnavailable("S3_URL_STYLE must be 'virtual' or 'path'")
        return cls(
            endpoint=values["S3_ENDPOINT"].rstrip("/"),
            bucket=values["S3_BUCKET"],
            access_key_id=values["S3_ACCESS_KEY_ID"],
            secret_access_key=values["S3_SECRET_ACCESS_KEY"],
            region=(os.getenv("S3_REGION") or "auto").strip(),
            url_style=style,
        )

    def host_and_path(self, key: str) -> tuple[str, str, str]:
        """(scheme, host, canonical path) for an object key."""
        parts = urlsplit(self.endpoint)
        scheme = parts.scheme or "https"
        host = parts.netloc
        path_key = "/" + _uri_encode(key.lstrip("/"), keep_slash=True)
        if self.url_style == "virtual":
            return scheme, f"{self.bucket}.{host}", path_key
        return scheme, host, "/" + _uri_encode(self.bucket, keep_slash=False) + path_key


def _canonical_query(params: Mapping[str, str]) -> str:
    return "&".join(
        f"{_uri_encode(k, keep_slash=False)}={_uri_encode(v, keep_slash=False)}"
        for k, v in sorted(params.items())
    )


def sign_headers(
    cfg: S3Config,
    method: str,
    key: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload_sha256: str = EMPTY_SHA256,
    now: datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """Header-signed request. Returns (url, headers to send)."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    scheme, host, path = cfg.host_and_path(key)

    hdrs = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
    hdrs["host"] = host
    hdrs["x-amz-date"] = amz_date
    hdrs["x-amz-content-sha256"] = payload_sha256
    signed = ";".join(sorted(hdrs))
    canonical_headers = "".join(f"{k}:{hdrs[k]}\n" for k in sorted(hdrs))

    canonical_request = "\n".join(
        [method, path, "", canonical_headers, signed, payload_sha256])
    scope = f"{date}/{cfg.region}/s3/aws4_request"
    to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(signing_key(cfg.secret_access_key, date, cfg.region),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    hdrs["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={cfg.access_key_id}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}")
    hdrs.pop("host")  # httpx sets it from the URL
    return f"{scheme}://{host}{path}", hdrs


def presign_get(
    cfg: S3Config,
    key: str,
    *,
    expires: int = PRESIGN_SECONDS,
    now: datetime | None = None,
    response_params: Mapping[str, str] | None = None,
) -> str:
    """A GET URL that works for `expires` seconds with no credentials.

    `response_params` (e.g. response-content-type) are signed into the URL so
    a browser is told what the bytes are even when the object was stored
    under a generic type.
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    scheme, host, path = cfg.host_and_path(key)
    scope = f"{date}/{cfg.region}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{cfg.access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
        **dict(response_params or {}),
    }
    query = _canonical_query(params)
    canonical_request = "\n".join(
        ["GET", path, query, f"host:{host}\n", "host", UNSIGNED])
    to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(signing_key(cfg.secret_access_key, date, cfg.region),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{path}?{query}&X-Amz-Signature={signature}"


def _slices(data: bytes | bytearray, size: int = 1 << 20):
    view = memoryview(data)
    for start in range(0, len(view), size):
        yield view[start:start + size]


class S3Client:
    """The four calls the archive needs. Every failure raises StorageUnavailable
    with the HTTP status in the message, because the caller records it."""

    def __init__(self, cfg: S3Config, http: httpx.Client | None = None, timeout: float = 60.0):
        self.cfg = cfg
        self._owns_http = http is None
        self.http = http or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "S3Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def exists(self, key: str) -> bool:
        url, hdrs = sign_headers(self.cfg, "HEAD", key)
        r = self.http.head(url, headers=hdrs)
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        raise StorageUnavailable(f"HEAD {key}: HTTP {r.status_code}")

    def put(self, key: str, data: bytes | bytearray, *, sha256_hex: str, content_type: str) -> None:
        url, hdrs = sign_headers(
            self.cfg, "PUT", key,
            headers={"content-type": content_type, "content-length": str(len(data))},
            payload_sha256=sha256_hex)
        # httpx reads a bytearray as an iterable of INTS, and bytes(data) would
        # copy a 100 MiB file. Zero-copy memoryview slices with the explicit
        # Content-Length above: httpx then sends a plain body, not chunked
        # encoding, which S3 PUT does not accept.
        r = self.http.put(url, headers=hdrs, content=_slices(data))
        if r.status_code not in (200, 201):
            raise StorageUnavailable(f"PUT {key}: HTTP {r.status_code} {r.text[:200]}")

    def get_json(self, key: str) -> dict | None:
        """A small JSON object, or None when it does not exist."""
        url, hdrs = sign_headers(self.cfg, "GET", key)
        r = self.http.get(url, headers=hdrs)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise StorageUnavailable(f"GET {key}: HTTP {r.status_code}")
        try:
            value = r.json()
        except ValueError as exc:
            raise StorageUnavailable(f"GET {key}: not JSON") from exc
        return value if isinstance(value, dict) else None

    def put_json(self, key: str, value: dict) -> None:
        data = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.put(key, data, sha256_hex=hashlib.sha256(data).hexdigest(),
                 content_type="application/json")

    def delete(self, key: str) -> None:
        url, hdrs = sign_headers(self.cfg, "DELETE", key)
        r = self.http.delete(url, headers=hdrs)
        if r.status_code not in (200, 204, 404):
            raise StorageUnavailable(f"DELETE {key}: HTTP {r.status_code}")

    def presign(self, key: str, *, content_type: str | None = None,
                filename: str | None = None, expires: int = PRESIGN_SECONDS) -> str:
        extra: dict[str, str] = {}
        if content_type:
            extra["response-content-type"] = content_type
        if filename:
            # inline, so images and PDFs open in the browser instead of downloading
            extra["response-content-disposition"] = (
                "inline; filename*=UTF-8''" + quote(filename, safe=""))
        return presign_get(self.cfg, key, expires=expires, response_params=extra)
