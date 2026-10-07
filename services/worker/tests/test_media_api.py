"""The two machine endpoints over real HTTP, with the bucket mocked.

Round-3 review found both answering 422 to every valid body: the request
models were local classes, and with postponed annotations FastAPI read `req`
as a missing query parameter. Testing fetch_and_store directly could never
have seen that, so these go through the app.
"""
from __future__ import annotations

import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.media import api, fetch

KEY = "test-key-media-api"
H = {"X-API-Key": KEY}
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 500


class Store:
    def __init__(self):
        self.objects, self.receipts, self.deleted = {}, {}, []

    def get_json(self, key):
        return self.receipts.get(key)

    def put_json(self, key, value):
        self.receipts[key] = value

    def exists(self, key):
        return key in self.objects

    def put(self, key, data, *, sha256_hex, content_type):
        self.objects[key] = bytes(data)

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)
        self.receipts.pop(key, None)


@pytest.fixture()
def store(monkeypatch):
    s = Store()
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    monkeypatch.setattr(api, "storage", lambda: s)
    return s


@pytest.fixture()
def client():
    return TestClient(app)


def test_fetch_accepts_a_json_body_and_never_echoes_the_url(client, store, monkeypatch):
    orig = httpx.Client
    monkeypatch.setattr(fetch.httpx, "Client", lambda **kw: orig(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=JPEG))))
    url = "https://travelgate.bitrix24.ae/rest/1/SECRETTOKEN99/download/?token=a"
    r = client.post("/media/fetch", json={"url": url}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "stored" and body["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert "SECRETTOKEN99" not in r.text and body["url_hash"] == hashlib.sha256(url.encode()).hexdigest()


def test_fetch_refuses_a_url_outside_the_families_without_requesting_it(client, store):
    r = client.post("/media/fetch", json={"url": "https://example.com/a.jpg"}, headers=H)
    assert r.status_code == 200 and r.json()["outcome"] == "not_allowed"


def test_fetch_without_a_bucket_is_a_503(client, monkeypatch):
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    monkeypatch.setattr(api, "_client", None)
    for k in ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = client.post("/media/fetch", json={"url": "https://travelgate.bitrix24.ae/~Ab12Cd"}, headers=H)
    assert r.status_code == 503 and "S3_BUCKET" in r.text


def test_delete_answers_per_key_and_returns_receipted_bytes(client, store):
    sha = "ab" * 32
    uh = "cd" * 32
    store.objects[fetch.object_key(sha)] = b"x"
    store.receipts[fetch.receipt_key(uh)] = {"sha256": "ef" * 32, "bytes": 9, "mime": "image/jpeg"}
    r = client.post("/media/delete", json={"sha256": [sha, "nope"], "url_hashes": [uh]}, headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["objects"] == [{"sha256": sha, "ok": True},
                            {"sha256": "nope", "ok": False, "error": "not a sha256"}]
    assert d["receipts"] == [{"url_hash": uh, "ok": True}]
    assert d["receipt_objects"] == [{"sha256": "ef" * 32, "bytes": 9, "mime": "image/jpeg"}]


def test_delete_batches_are_bounded(client, store):
    r = client.post("/media/delete", json={"sha256": ["ab" * 32] * (api.MAX_DELETE_OBJECTS + 1)}, headers=H)
    assert r.status_code == 422


def test_no_key_no_access(client, store):
    assert client.post("/media/fetch", json={"url": "x"}).status_code == 401
