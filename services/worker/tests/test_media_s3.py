"""SigV4 signing, checked against the worked examples AWS publishes for S3.

The values below are AWS's own (examplebucket, AKIAIOSFODNN7EXAMPLE,
2013-05-24, us-east-1), from "Signature Calculations for the Authorization
Header: Transferring Payload in a Single Chunk" and "Authenticating Requests:
Using Query Parameters". If a signature here stops matching, every request to
the bucket would be answered 403 — the tests are the only place that shows
which part of the canonical request drifted.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import httpx
import pytest

from app.media import s3

AWS = s3.S3Config(
    endpoint="https://s3.amazonaws.com",
    bucket="examplebucket",
    access_key_id="AKIAIOSFODNN7EXAMPLE",
    secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    region="us-east-1",
)
WHEN = datetime(2013, 5, 24, tzinfo=timezone.utc)


def _signature(headers: dict) -> str:
    return headers["authorization"].rsplit("Signature=", 1)[1]


def _signed_headers(headers: dict) -> str:
    return headers["authorization"].split("SignedHeaders=", 1)[1].split(",", 1)[0]


def test_get_object_with_range_matches_aws_example():
    url, hdrs = s3.sign_headers(AWS, "GET", "test.txt",
                                headers={"Range": "bytes=0-9"}, now=WHEN)
    assert url == "https://examplebucket.s3.amazonaws.com/test.txt"
    assert _signed_headers(hdrs) == "host;range;x-amz-content-sha256;x-amz-date"
    assert _signature(hdrs) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"


def test_put_object_matches_aws_example():
    # A key with '$' in it: S3 percent-encodes it in the canonical path.
    url, hdrs = s3.sign_headers(
        AWS, "PUT", "test$file.text",
        headers={"Date": "Fri, 24 May 2013 00:00:00 GMT",
                 "x-amz-storage-class": "REDUCED_REDUNDANCY"},
        payload_sha256=hashlib.sha256(b"Welcome to Amazon S3.").hexdigest(),
        now=WHEN)
    assert url == "https://examplebucket.s3.amazonaws.com/test%24file.text"
    assert _signature(hdrs) == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"


def test_presigned_get_matches_aws_example():
    url = s3.presign_get(AWS, "test.txt", expires=86400, now=WHEN)
    assert url.startswith("https://examplebucket.s3.amazonaws.com/test.txt?")
    assert url.endswith(
        "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    assert "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" in url


def test_path_style_puts_the_bucket_in_the_path():
    cfg = s3.S3Config(endpoint="https://t3.storageapi.dev", bucket="media-x1",
                      access_key_id="k", secret_access_key="s", url_style="path")
    url, _ = s3.sign_headers(cfg, "HEAD", "sha256/ab/abcd")
    assert url == "https://t3.storageapi.dev/media-x1/sha256/ab/abcd"


def test_virtual_style_is_the_default_railway_addressing():
    cfg = s3.S3Config(endpoint="https://t3.storageapi.dev/", bucket="media-x1",
                      access_key_id="k", secret_access_key="s")
    _, host, path = cfg.host_and_path("sha256/ab/abcd")
    assert (host, path) == ("media-x1.t3.storageapi.dev", "/sha256/ab/abcd")


def test_presign_signs_the_response_overrides_into_the_url():
    cfg = s3.S3Config(endpoint="https://t3.storageapi.dev", bucket="b",
                      access_key_id="k", secret_access_key="s")
    url = s3.S3Client(cfg).presign("sha256/ab/abcd", content_type="audio/ogg",
                                   filename="رسالة صوتية.ogg")
    assert "response-content-type=audio%2Fogg" in url
    # Arabic file names survive, percent-encoded twice (RFC 5987 inside a query)
    assert "response-content-disposition=inline%3B%20filename%2A%3DUTF-8%27%27" in url
    assert "X-Amz-Expires=300" in url


def test_from_env_names_every_missing_variable(monkeypatch):
    for k in ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(s3.StorageUnavailable) as exc:
        s3.S3Config.from_env()
    assert "S3_ACCESS_KEY_ID" in str(exc.value) and "S3_ENDPOINT" in str(exc.value)


def test_from_env_rejects_an_unknown_url_style(monkeypatch):
    for k in ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("S3_URL_STYLE", "dns")
    with pytest.raises(s3.StorageUnavailable):
        s3.S3Config.from_env()


def test_put_sends_a_bytearray_whole_with_content_length_not_chunked():
    """httpx reads a bytearray as an iterable of ints; the client must send
    the real bytes, once, with the Content-Length S3 requires."""
    seen = {}

    def handler(req):
        seen["headers"] = dict(req.headers)
        seen["body"] = req.read()
        return httpx.Response(200)

    cfg = s3.S3Config(endpoint="https://t3.storageapi.dev", bucket="b",
                      access_key_id="k", secret_access_key="s")
    data = bytearray(b"\xff\xd8\xff" + b"z" * (3 << 20))
    client = s3.S3Client(cfg, http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.put("sha256/ab/x", data, sha256_hex=hashlib.sha256(data).hexdigest(),
               content_type="image/jpeg")
    assert seen["body"] == bytes(data)
    assert seen["headers"]["content-length"] == str(len(data))
    assert "transfer-encoding" not in seen["headers"]
    assert seen["headers"]["x-amz-content-sha256"] == hashlib.sha256(data).hexdigest()


def test_get_json_treats_404_as_absent():
    cfg = s3.S3Config(endpoint="https://t3.storageapi.dev", bucket="b",
                      access_key_id="k", secret_access_key="s")
    client = s3.S3Client(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(404))))
    assert client.get_json("receipts/ab/x.json") is None
