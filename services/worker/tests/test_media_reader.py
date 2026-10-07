"""The conversation reader: what a pulled chat may and may not contain.

No database and no bucket: `db` is stubbed per query, the bucket by a fake
signer. The cases are the review conditions (Codex/astra, 2026-10-01) plus the
ways a reader like this leaks:

  * a link that outlives the attachment's retention date (C4);
  * a REST token surfacing through text or a fallback link;
  * duplicate deliveries silently merged, or a feed guess presented as fact (C7);
  * a read query that could write.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.media import reader

KEY = "test-key-reader"
NOW = datetime.now(timezone.utc)
TOKEN = "zzRESTtoken77"
VOICE = f"Voice message\nhttps://travelgate.bitrix24.ae/rest/1/{TOKEN}/download/?token=disk|a|b"
AGENT_PDF = "[Attachment: عرض.pdf]\nhttps://travelgate.bitrix24.ae/~Ab12Cd"
CUST_IMG = "صورة التحويل\nhttps://gupconnector.cultivbureau.com/connector/gupshup-media/x.y.z"

V4 = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"
V5 = "74738ff5-5367-5958-9aee-98fffdcd1876"


def _interaction(iid, conv, started):
    return {"interaction_id": iid, "external_id": conv, "external_deal_id": "43742",
            "external_contact_id": "52596", "started_at": started,
            "ended_at": started + timedelta(hours=1), "content_purged_at": None,
            "message_count": 3}


def _msg(mid, sender, body, minutes, ctype="text"):
    t = NOW - timedelta(days=1) + timedelta(minutes=minutes)
    return {"message_id": str(mid), "seq": mid, "sender": sender, "sender_external_id": "86",
            "content_type": ctype, "body": body, "sent_at": t, "received_at": t}


class FakeStore:
    def __init__(self):
        self.calls = []

    def presign(self, key, *, content_type=None, filename=None, expires=300):
        self.calls.append(expires)
        return f"https://b.t3.storageapi.dev/{key}?X-Amz-Expires={expires}&X-Amz-Signature=s"


@pytest.fixture()
def world(monkeypatch):
    state = {
        "interactions": [_interaction("11111111-1111-1111-1111-111111111111", V4, NOW - timedelta(days=1)),
                         _interaction("22222222-2222-2222-2222-222222222222", V5, NOW - timedelta(days=1))],
        "messages": [_msg(1, "customer", CUST_IMG, 0, "image"),
                     _msg(2, "agent", AGENT_PDF, 2, "document"),
                     _msg(3, "customer", VOICE, 3, "audio")],
        "media": [],
        "retention": "90",
        "media_missing": False,
        "store": FakeStore(),
    }
    sql_seen = []

    def rows(sql, params=None):
        sql_seen.append(sql)
        if sql is reader.SQL_DEAL_INTERACTIONS:
            return [i for i in state["interactions"] if i["external_deal_id"] == params["deal"]]
        if sql is reader.SQL_MESSAGES:
            return list(state["messages"])
        if sql is reader.SQL_MEDIA:
            if state["media_missing"]:
                raise RuntimeError('relation "chat_media" does not exist')
            return list(state["media"])
        raise AssertionError("unexpected query")

    def one(sql, params=None):
        sql_seen.append(sql)
        if sql is reader.SQL_RETENTION:
            return {"value": state["retention"]}
        if sql is reader.SQL_INTERACTION:
            return next((i for i in state["interactions"] if i["interaction_id"] == params["iid"]), {})
        raise AssertionError("unexpected query")

    monkeypatch.setattr(reader.db, "rows", rows)
    monkeypatch.setattr(reader.db, "one", one)
    monkeypatch.setattr(reader, "_store_or_none", lambda: state["store"])
    monkeypatch.setattr(settings, "worker_api_key", KEY, raising=False)
    state["sql_seen"] = sql_seen
    return state


@pytest.fixture()
def client():
    return TestClient(app)


def get(client, path):
    return client.get(path, headers={"X-API-Key": KEY})


def _media(mid, status, sha="ab" * 32, name="x.jpg", mime="image/jpeg", ctype="image", obj="present"):
    return {"media_id": f"m-{mid}", "message_id": str(mid), "ordinal": 0, "declared_type": ctype,
            "file_name": name, "status": status, "family": "gupconnector", "last_outcome": status,
            "sha256": sha if status == "stored" else None, "bytes": 1234 if status == "stored" else None,
            "mime": mime if status == "stored" else None,
            "storage_key": f"sha256/ab/{sha}" if status == "stored" else None,
            "object_state": obj if status == "stored" else None}


# -- access ----------------------------------------------------------------------

def test_no_key_no_conversation(client, world):
    assert client.get("/conversations/by-deal/43742").status_code == 401


def test_the_page_itself_carries_no_data(client):
    r = client.get("/conversations")
    assert r.status_code == 200 and "43742" not in r.text
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("bad", ["43742;drop", "abc", "1" * 13])
def test_deal_id_must_be_digits(client, world, bad):
    assert get(client, f"/conversations/by-deal/{bad}").status_code in (404, 422)


# -- C7: threads separate, guesses labelled ----------------------------------------

def test_a_deals_threads_are_returned_separately_and_never_merged(client, world):
    data = get(client, "/conversations/by-deal/43742").json()
    assert data["merged"] is False and len(data["threads"]) == 2
    for t in data["threads"]:
        assert t["order"] == "stored_thread_order"
        assert t["time_basis"] == "source_processing_time"
        assert t["feed_hint"]["verified"] is False
        assert t["feed_hint"]["basis"] == "conversation_id_pattern"
    assert {t["feed_hint"]["value"] for t in data["threads"]} == {"uuid_v4", "uuid_v5"}


# -- C4: links never outlive the reference ------------------------------------------

def test_a_stored_file_gets_a_link_capped_at_five_minutes(client, world):
    world["media"] = [_media(1, "stored")]
    data = get(client, "/conversations/by-deal/43742").json()
    img = data["threads"][0]["messages"][0]["media"][0]
    assert img["status"] == "stored" and img["kind"] == "image"
    assert img["access_url"].startswith("https://")
    assert max(world["store"].calls) <= 300


def test_a_link_never_outlives_the_retention_date(client, world):
    # Thread started 90 days minus 2 minutes ago: 120 seconds of life left.
    for i in world["interactions"]:
        i["started_at"] = NOW - timedelta(days=90) + timedelta(minutes=2)
    world["media"] = [_media(1, "stored")]
    get(client, "/conversations/by-deal/43742")
    assert world["store"].calls and max(world["store"].calls) <= 120


def test_past_retention_there_is_no_text_and_no_link(client, world):
    for i in world["interactions"]:
        i["started_at"] = NOW - timedelta(days=91)
    world["media"] = [_media(1, "stored")]
    t = get(client, "/conversations/by-deal/43742").json()["threads"][0]
    assert t["content_status"] == "purged" and t["messages"] == []
    assert world["store"].calls == []


def test_a_deleted_object_is_shown_as_purged_without_a_link(client, world):
    world["media"] = [_media(1, "stored", obj="deleted")]
    img = get(client, "/conversations/by-deal/43742").json()["threads"][0]["messages"][0]["media"][0]
    assert img["access_url"] is None and img["status"] == "purged"


def test_a_file_whose_source_died_says_so(client, world):
    world["media"] = [_media(1, "recovery_pending")]
    img = get(client, "/conversations/by-deal/43742").json()["threads"][0]["messages"][0]["media"][0]
    assert img["access_url"] is None and "dead" in img["reason"]


# -- text, and the REST token --------------------------------------------------------

def test_captions_stay_links_go(client, world):
    msgs = get(client, "/conversations/by-deal/43742").json()["threads"][0]["messages"]
    assert msgs[0]["text"] == "صورة التحويل"
    assert msgs[1]["text"] == "" and msgs[2]["text"] == ""


def test_the_rest_token_appears_nowhere_in_the_response(client, world):
    for media in ([], [_media(3, "pending", ctype="audio")]):
        world["media"] = media
        raw = get(client, "/conversations/by-deal/43742").text
        assert TOKEN not in raw


def test_before_the_archive_exists_public_links_show_and_token_links_do_not(client, world):
    world["media_missing"] = True
    t = get(client, "/conversations/by-deal/43742").json()["threads"][0]
    assert t["archive"] == "not_installed"
    pdf = t["messages"][1]["media"][0]
    voice = t["messages"][2]["media"][0]
    img = t["messages"][0]["media"][0]
    assert pdf["access_url"] == "https://travelgate.bitrix24.ae/~Ab12Cd"
    assert voice["access_url"] is None and "credential" in voice["reason"]
    assert img["access_url"] is None and "short-lived" in img["reason"]


def test_ordinary_links_people_typed_are_kept(client, world):
    world["messages"] = [_msg(1, "agent", "الحجز هنا https://example.com/booking?id=5", 0)]
    m = get(client, "/conversations/by-deal/43742").json()["threads"][0]["messages"][0]
    assert m["text"].endswith("https://example.com/booking?id=5") and m["media"] == []


# -- read-only ------------------------------------------------------------------------

def test_every_reader_query_is_a_select():
    for name in ("SQL_INTERACTION", "SQL_DEAL_INTERACTIONS", "SQL_MESSAGES", "SQL_MEDIA", "SQL_RETENTION"):
        sql = getattr(reader, name).strip().upper()
        assert sql.startswith("SELECT"), name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b", sql), name


def test_messages_come_back_in_stored_order():
    assert "ORDER BY m.sent_at, m.message_id" in reader.SQL_MESSAGES


# -- round-3 review findings -----------------------------------------------------

def test_link_lifetime_is_measured_at_signing_not_at_request_start(client, world, monkeypatch):
    """Ten seconds left when the request starts, fifteen seconds of queries:
    no link may be signed, because by signing time the reference is gone."""
    for i in world["interactions"]:
        i["started_at"] = NOW - timedelta(days=90) + timedelta(seconds=10)
    world["media"] = [_media(1, "stored")]
    clock = {"t": NOW}

    def ticking():
        clock["t"] += timedelta(seconds=15)    # every look at the clock costs 15 s
        return clock["t"]
    monkeypatch.setattr(reader, "_now", ticking)
    get(client, "/conversations/by-deal/43742")
    assert world["store"].calls == [], "no link may be signed once the reference has expired"


def test_a_thread_with_no_start_date_fails_closed(client, world):
    for i in world["interactions"]:
        i["started_at"] = None
    world["media"] = [_media(1, "stored")]
    t = get(client, "/conversations/by-deal/43742").json()["threads"][0]
    assert t["content_status"] == "purged" and world["store"].calls == []


def test_a_fresh_link_can_be_asked_for_one_attachment(client, world, monkeypatch):
    row = {"media_id": "11111111-2222-3333-4444-555555555555", "file_name": "x.jpg",
           "declared_type": "image", "status": "stored", "bytes": 10, "mime": "image/jpeg",
           "storage_key": "sha256/ab/" + "ab" * 32, "object_state": "present",
           "started_at": NOW - timedelta(days=1), "content_purged_at": None}
    orig_one = reader.db.one
    monkeypatch.setattr(reader.db, "one", lambda sql, p=None: row if sql is reader.SQL_MEDIA_ONE else orig_one(sql, p))
    r = get(client, f"/conversations/media/{row['media_id']}/link")
    assert r.status_code == 200 and r.json()["access_url"].startswith("https://")
    row["started_at"] = NOW - timedelta(days=91)
    assert get(client, f"/conversations/media/{row['media_id']}/link").status_code == 410
    row["started_at"], row["status"] = NOW - timedelta(days=1), "recovery_pending"
    assert get(client, f"/conversations/media/{row['media_id']}/link").status_code == 404


def test_the_page_renews_an_expired_link_instead_of_showing_a_broken_file(client):
    html = client.get("/conversations").text
    assert "conversations/media/" in html and 'addEventListener("error"' in html
