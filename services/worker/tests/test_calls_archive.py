"""The call recordings store endpoint over real HTTP, with the bucket mocked.

Workflow 12 downloads a recording from the PBX (only n8n is whitelisted
there) and POSTs the raw bytes here. What must hold:

  * audio is stored under calls/<yyyy>/<mm>/<dd>/<uniqueid>.wav, once
  * anything that is not audio (an HTML error page, an empty body) is refused
    and never written
  * a bad id or day is a 422 before the bucket is touched
  * the call-recordings bucket is configured from CALLS_S3_*, never from the
    chat-media S3_* variables
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.calls_archive import api, store as calls_store
from app.config import settings
from app.main import app
from app.media import s3

KEY = "test-key-calls-archive"
H = {"X-API-Key": KEY, "Content-Type": "audio/x-wav"}
WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 200
UID = "1793546440.46642"
DAY = "2026-11-01"
URL = f"/calls/recording/store?uniqueid={UID}&day={DAY}"


class Store:
    def __init__(self):
        self.objects, self.puts = {}, []

    def exists(self, key):
        return key in self.objects

    def put(self, key, data, *, sha256_hex, content_type):
        self.puts.append((key, content_type))
        self.objects[key] = bytes(data)


@pytest.fixture()
def bucket(monkeypatch):
    b = Store()
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    monkeypatch.setattr(api, "storage", lambda: b)
    return b


@pytest.fixture()
def client():
    return TestClient(app)


def test_audio_is_stored_under_its_day_and_id(client, bucket):
    r = client.post(URL, content=WAV, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    key = f"calls/2026/11/01/{UID}.wav"
    assert body["outcome"] == "stored" and body["storage_key"] == key
    assert body["sha256"] == hashlib.sha256(WAV).hexdigest() and body["bytes"] == len(WAV)
    assert body["mime"] == "audio/wav" and body["already_stored"] is False
    assert bucket.objects[key] == WAV and bucket.puts == [(key, "audio/wav")]


def test_storing_the_same_call_twice_writes_once(client, bucket):
    client.post(URL, content=WAV, headers=H)
    r = client.post(URL, content=WAV, headers=H)
    assert r.json()["outcome"] == "stored" and r.json()["already_stored"] is True
    assert len(bucket.puts) == 1


def test_an_html_error_page_is_not_stored(client, bucket):
    r = client.post(URL, content=b"<html><body>502 Bad Gateway</body></html>", headers=H)
    assert r.status_code == 200 and r.json()["outcome"] == "not_audio"
    assert bucket.objects == {}


def test_an_empty_body_is_not_stored(client, bucket):
    r = client.post(URL, content=b"", headers=H)
    assert r.json()["outcome"] == "not_audio" and bucket.objects == {}


def test_too_large_is_refused_before_reading(client, bucket, monkeypatch):
    monkeypatch.setattr(calls_store, "MAX_BYTES", 10)
    r = client.post(URL, content=WAV, headers=H)
    assert r.json()["outcome"] == "too_large" and bucket.objects == {}


@pytest.mark.parametrize("qs", [
    "uniqueid=../../etc&day=2026-11-01",
    "uniqueid=1793546440.46642&day=2026-11-1",
    "uniqueid=1793546440.46642&day=2026-11-01T18:20:40",
    "uniqueid=abc&day=2026-11-01",
])
def test_bad_ids_and_days_are_422(client, bucket, qs):
    r = client.post("/calls/recording/store?" + qs, content=WAV, headers=H)
    assert r.status_code == 422 and bucket.objects == {}


def test_no_key_no_access(client, bucket):
    assert client.post(URL, content=WAV).status_code == 401


def test_calls_bucket_reads_its_own_prefix(monkeypatch):
    for k in ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "REGION", "URL_STYLE"):
        monkeypatch.delenv("S3_" + k, raising=False)
        monkeypatch.delenv("CALLS_S3_" + k, raising=False)
    monkeypatch.setenv("S3_BUCKET", "chat-media-x")
    monkeypatch.setenv("CALLS_S3_ENDPOINT", "https://t3.storageapi.dev")
    monkeypatch.setenv("CALLS_S3_BUCKET", "call-recordings-y")
    monkeypatch.setenv("CALLS_S3_ACCESS_KEY_ID", "tid")
    monkeypatch.setenv("CALLS_S3_SECRET_ACCESS_KEY", "tsec")
    cfg = s3.S3Config.from_env(prefix=api.S3_PREFIX)
    assert cfg.bucket == "call-recordings-y" and cfg.region == "auto"


def test_calls_bucket_missing_names_its_own_variables(monkeypatch):
    for k in ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY"):
        monkeypatch.setenv("S3_" + k, "set")
        monkeypatch.delenv("CALLS_S3_" + k, raising=False)
    with pytest.raises(s3.StorageUnavailable) as exc:
        s3.S3Config.from_env(prefix=api.S3_PREFIX)
    named = str(exc.value).split(": ", 1)[1].split(", ")
    assert "CALLS_S3_BUCKET" in named and all(n.startswith("CALLS_S3_") for n in named)


def test_without_a_calls_bucket_the_endpoint_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    monkeypatch.setattr(api, "_client", None)
    for k in ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY"):
        monkeypatch.delenv("CALLS_S3_" + k, raising=False)
    r = client.post(URL, content=WAV, headers=H)
    assert r.status_code == 503 and "CALLS_S3_BUCKET" in r.text
