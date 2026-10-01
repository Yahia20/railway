"""Link discovery and the fetch-and-store step, with no network and no bucket.

Bodies below are the shapes production delivered (2026-09-21 probe), with the
tokens replaced. The cases are the ways an archive like this goes wrong:
fetching a URL nobody vetted, storing an HTML error page as a passport,
following a redirect into the private network, and leaking the Bitrix REST
token back out through the text it returns.
"""
from __future__ import annotations

import hashlib
import json
import logging

import httpx
import pytest

from app.media import fetch, links
from app.media.s3 import StorageUnavailable

VOICE = ("Voice message\nhttps://travelgate.bitrix24.ae/rest/1/abcSECRETtoken123/download/"
         "?token=disk|aWQ9MTIzNDU2|ImRvd25sb2FkIg==|abc")
AGENT_PDF = "[Attachment: عرض باريس.pdf]\nhttps://travelgate.bitrix24.ae/~Ab12Cd"
CUSTOMER_IMG = ("صورة الجواز\nhttps://gupconnector.cultivbureau.com/connector/gupshup-media/"
                "eyJ1cmwiOiJ4In0.ZyXw.sig")
STICKER = "https://filemanager.gupshup.io/wa/app-1/wa/media/998877?download=false"


# -- links ------------------------------------------------------------------

@pytest.mark.parametrize("body,family", [
    (VOICE, "bitrix_rest"), (AGENT_PDF, "bitrix_short"),
    (CUSTOMER_IMG, "gupconnector"), (STICKER, "gupshup"),
])
def test_every_production_family_is_recognised(body, family):
    found = links.find_links(body)
    assert [l.family for l in found] == [family]
    assert found[0].url_hash == hashlib.sha256(found[0].url.encode()).hexdigest()


@pytest.mark.parametrize("url", [
    "https://travelgate.bitrix24.ae.evil.example/~Ab12Cd",      # suffix trick
    "http://travelgate.bitrix24.ae/~Ab12Cd",                    # not https
    "https://user@travelgate.bitrix24.ae/~Ab12Cd",              # credentials in URL
    "https://travelgate.bitrix24.ae:8443/~Ab12Cd",              # odd port
    "https://travelgate.bitrix24.ae/crm/deal/details/1/",       # right host, not a file path
    "https://travelgate.bitrix24.ae/~Ab12Cd/../../rest",        # short-link shape broken
    "https://169.254.169.254/latest/meta-data/",                # cloud metadata
    "https://travelgateksa.com/assets/offer.jpg",               # our site, not a chat file
])
def test_anything_else_is_not_fetched(url):
    assert links.classify(url) is None
    assert links.find_links(f"look {url}") == []


def test_trailing_punctuation_is_not_part_of_the_link():
    found = links.find_links("الملف هنا https://travelgate.bitrix24.ae/~Ab12Cd، شكرا")
    assert found[0].url == "https://travelgate.bitrix24.ae/~Ab12Cd"


def test_the_same_link_twice_in_one_message_is_one_file():
    assert len(links.find_links(AGENT_PDF + "\n" + AGENT_PDF)) == 1


def test_caption_drops_the_link_and_the_api_placeholders():
    assert links.caption(VOICE) == ""
    assert links.caption(AGENT_PDF) == ""
    assert links.caption(CUSTOMER_IMG) == "صورة الجواز"
    assert links.caption("شوف https://example.com/x") == "شوف https://example.com/x"


def test_attachment_name_is_read_from_the_placeholder():
    assert links.attachment_name(AGENT_PDF) == "عرض باريس.pdf"
    assert links.attachment_name(CUSTOMER_IMG) is None


def test_redact_removes_the_rest_token_and_nothing_else():
    out = links.redact(VOICE)
    assert "abcSECRETtoken123" not in out
    assert "/rest/1/[redacted]/download/" in out
    assert links.redact(AGENT_PDF) == AGENT_PDF


def test_caption_never_contains_the_rest_token():
    assert "SECRET" not in links.caption(VOICE)


# -- sniffing ---------------------------------------------------------------

@pytest.mark.parametrize("head,name,mime", [
    (b"\xff\xd8\xff\xe0\x00\x10JFIF", None, "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n\x00", None, "image/png"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", None, "image/webp"),
    (b"%PDF-1.7\n", "x.pdf", "application/pdf"),
    (b"OggS\x00\x02", None, "audio/ogg"),
    (b"\x00\x00\x00\x20ftypisom", None, "video/mp4"),
    (b"\x00\x00\x00\x20ftypM4A ", None, "audio/mp4"),
    (b"PK\x03\x04\x14\x00", "contract.docx",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (b"PK\x03\x04\x14\x00", None, "application/zip"),
    (b"<!DOCTYPE html><html>", None, None),
    (b'{"error":"expired"}', None, None),
    (b"   \n<html>", None, None),
])
def test_sniff_trusts_bytes_not_headers(head, name, mime):
    assert fetch.sniff(head, name) == mime


# -- fetch_and_store ----------------------------------------------------------

class FakeStore:
    def __init__(self, existing: set[str] | None = None, fail: bool = False):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.receipts: dict[str, dict] = {}
        self.existing = existing or set()
        self.fail = fail

    def exists(self, key):
        if self.fail:
            raise StorageUnavailable("HEAD: HTTP 503")
        return key in self.existing or key in self.objects

    def put(self, key, data, *, sha256_hex, content_type):
        assert hashlib.sha256(data).hexdigest() == sha256_hex
        self.objects[key] = (data, content_type)

    def get_json(self, key):
        return self.receipts.get(key)

    def put_json(self, key, value):
        # the receipt must be written BEFORE the bytes (see fetch_and_store)
        assert fetch.object_key(value["sha256"]) not in self.objects
        self.receipts[key] = value


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


JPEG = b"\xff\xd8\xff\xe0" + b"x" * 5000
URL = "https://gupconnector.cultivbureau.com/connector/gupshup-media/tok.sig"


def test_a_file_is_stored_under_its_sha256():
    store = FakeStore()
    res = fetch.fetch_and_store(URL, store=store, http=_client(
        lambda req: httpx.Response(200, content=JPEG, headers={"content-type": "text/plain"})))
    sha = hashlib.sha256(JPEG).hexdigest()
    assert res.outcome == "stored" and res.sha256 == sha and res.mime == "image/jpeg"
    assert res.already_stored is False
    assert store.objects[fetch.object_key(sha)][1] == "image/jpeg"


def test_the_same_bytes_are_not_uploaded_twice():
    sha = hashlib.sha256(JPEG).hexdigest()
    store = FakeStore(existing={fetch.object_key(sha)})
    res = fetch.fetch_and_store(URL, store=store, http=_client(
        lambda req: httpx.Response(200, content=JPEG)))
    assert res.outcome == "stored" and res.already_stored is True
    assert store.objects == {}


@pytest.mark.parametrize("code", [404, 410])
def test_a_gone_link_is_expired_not_failed(code):
    res = fetch.fetch_and_store(URL, store=FakeStore(), http=_client(
        lambda req: httpx.Response(code, json={"detail": "expired"})))
    assert res.outcome == "expired" and res.http_status == code


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_a_transient_answer_is_retryable(code):
    res = fetch.fetch_and_store(URL, store=FakeStore(), http=_client(
        lambda req: httpx.Response(code)))
    assert res.outcome == "failed" and res.http_status == code


def test_an_html_page_with_200_is_not_stored():
    store = FakeStore()
    res = fetch.fetch_and_store(URL, store=store, http=_client(
        lambda req: httpx.Response(200, content=b"<html>Link expired</html>",
                                   headers={"content-type": "image/jpeg"})))
    assert res.outcome == "not_a_file" and store.objects == {}


def test_size_cap_from_the_header_and_while_streaming():
    big = b"\xff\xd8\xff" + b"0" * 2000
    res = fetch.fetch_and_store(URL, store=FakeStore(), max_bytes=1000, http=_client(
        lambda req: httpx.Response(200, content=big)))
    assert res.outcome == "too_large"


def test_a_redirect_into_the_private_network_is_refused():
    def handler(req):
        return httpx.Response(302, headers={"location": "https://postgres.railway.internal/x"})
    res = fetch.fetch_and_store(URL, store=FakeStore(), http=_client(handler))
    assert res.outcome == "not_allowed"


def test_a_redirect_to_an_allowed_cdn_is_followed():
    def handler(req):
        if req.url.host == "gupconnector.cultivbureau.com":
            return httpx.Response(302, headers={"location": "https://mmg.whatsapp.net/v/f.jpg"})
        return httpx.Response(200, content=JPEG)
    res = fetch.fetch_and_store(URL, store=FakeStore(), http=_client(handler))
    assert res.outcome == "stored" and res.redirected_to_host == "mmg.whatsapp.net"


def test_a_url_outside_the_families_is_never_requested():
    calls = []
    res = fetch.fetch_and_store("https://example.com/a.jpg", store=FakeStore(),
                                http=_client(lambda req: calls.append(req) or httpx.Response(200)))
    assert res.outcome == "not_allowed" and calls == []


def test_a_bucket_outage_is_retryable_and_says_so():
    res = fetch.fetch_and_store(URL, store=FakeStore(fail=True), http=_client(
        lambda req: httpx.Response(200, content=JPEG)))
    assert res.outcome == "failed" and res.error.startswith("bucket:")


def test_a_lost_response_is_recovered_from_the_receipt_not_the_dead_link():
    """The 2026-09-30 shape: n8n never saw the answer, retries after the
    customer link has died. The retry must say `stored`, not `expired`."""
    store = FakeStore()
    first = fetch.fetch_and_store(URL, store=store, http=_client(
        lambda req: httpx.Response(200, content=JPEG)))
    calls = []
    retry = fetch.fetch_and_store(URL, store=store, http=_client(
        lambda req: calls.append(req) or httpx.Response(410)))
    assert retry.outcome == "stored" and retry.sha256 == first.sha256
    assert calls == [], "the retry must not touch the source at all"


def test_a_receipt_whose_bytes_never_landed_does_not_count():
    """Crash after the receipt, before the object: the retry must fetch again,
    not report a file that is not there."""
    store = FakeStore()
    store.receipts[fetch.receipt_key(links.url_hash(URL))] = {"sha256": "ab" * 32}
    res = fetch.fetch_and_store(URL, store=store, http=_client(
        lambda req: httpx.Response(200, content=JPEG)))
    assert res.outcome == "stored" and res.sha256 == hashlib.sha256(JPEG).hexdigest()
    assert fetch.object_key(res.sha256) in store.objects


def test_a_dead_link_with_a_dangling_receipt_is_expired_not_stored():
    store = FakeStore()
    store.receipts[fetch.receipt_key(links.url_hash(URL))] = {"sha256": "ab" * 32}
    res = fetch.fetch_and_store(URL, store=store, http=_client(
        lambda req: httpx.Response(410)))
    assert res.outcome == "expired"


def test_a_network_error_is_retryable():
    def handler(req):
        raise httpx.ConnectTimeout("slow", request=req)
    res = fetch.fetch_and_store(URL, store=FakeStore(), http=_client(handler))
    assert res.outcome == "failed" and "ConnectTimeout" in res.error


# -- round-2 review findings (astra, 2026-10-01) ------------------------------

@pytest.mark.parametrize("url", [
    "https://travelgate.bitrix24.ae:bad/~Ab12Cd",
    "https://[::1/~Ab12Cd",
])
def test_a_malformed_url_is_unfetchable_not_an_exception(url):
    assert links.classify(url) is None
    assert links.find_links("x " + url) == []


@pytest.mark.parametrize("brand,mime", [
    (b"heic", "image/heic"), (b"mif1", "image/heic"), (b"avif", "image/avif"),
    (b"3gp5", "video/3gpp"), (b"isom", "video/mp4"), (b"zzzz", "application/octet-stream"),
])
def test_iso_bmff_is_not_assumed_to_be_video(brand, mime):
    assert fetch.sniff(b"\x00\x00\x00\x18ftyp" + brand) == mime


def test_fetch_closes_the_http_client_it_created(monkeypatch):
    closed = []

    class Tracked(httpx.Client):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(fetch.httpx, "Client", lambda **kw: Tracked(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=JPEG))))
    fetch.fetch_and_store(URL, store=FakeStore())
    assert closed == [True]


def test_fetch_leaves_a_caller_supplied_client_open():
    client = _client(lambda req: httpx.Response(200, content=JPEG))
    fetch.fetch_and_store(URL, store=FakeStore(), http=client)
    assert not client.is_closed


# -- round-3 review findings (astra, 2026-10-01) ------------------------------

@pytest.mark.parametrize("body", [
    "Voice message\nhttps://travelgate.bitrix24.ae/rest/1/SECRETTOKEN99/do...",   # cut copy
    "Voice message\nhttps://travelgate.bitrix24.ae/rest/1/SECRETTOK...",          # cut inside the token
    "ابعتلك https://travelgate.bitrix24.ae/rest/12/SECRETTOKEN99/download/?token=x شوف",
    "http://travelgate.bitrix24.ae/rest/1/SECRETTOKEN99/download/",                # not even https
])
def test_the_rest_token_is_redacted_even_when_the_link_is_not_fetchable(body):
    assert "SECRETTOK" not in links.redact(body)


def test_caption_redacts_a_cut_rest_link_next_to_a_real_attachment():
    body = ("[Attachment: a.pdf]\nhttps://travelgate.bitrix24.ae/~Ab12Cd "
            "https://travelgate.bitrix24.ae/rest/1/SECRETTOKEN99/do...")
    assert "SECRETTOKEN99" not in links.caption(body)


def test_an_error_text_never_carries_the_rest_token():
    r = fetch.FetchResult("failed", error="ConnectError: https://travelgate.bitrix24.ae/rest/1/SECRETTOKEN99/x")
    assert "SECRETTOKEN99" not in r.error and "SECRETTOKEN99" not in json.dumps(r.as_dict())


def test_httpx_does_not_log_the_source_url(caplog):
    import app.media.api  # noqa: F401  (sets the httpx logger level)
    caplog.set_level(logging.INFO)
    url = "https://travelgate.bitrix24.ae/rest/1/SECRETTOKEN99/download/?token=a"
    fetch.fetch_and_store(url, store=FakeStore(), http=_client(lambda req: httpx.Response(200, content=JPEG)))
    assert "SECRETTOKEN99" not in caplog.text
