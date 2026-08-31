"""`/chats/prepare` — rebuilding a stored thread for the judge.

Every test here is a way the endpoint could hand the judge something wrong
without failing: a thread rendered in the wrong order, a timestamp read in the
wrong timezone, a bot log scored as an agent's work. None of them raises.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("WORKER_API_KEY", "test-key")

from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


def prepare(messages, external_id="conv-1", channel="whatsapp"):
    return client.post(
        "/chats/prepare",
        json={"external_id": external_id, "channel": channel, "messages": messages},
        headers=AUTH,
    )


def msg(seq, sender, body, sent_at):
    return {"seq": seq, "sender": sender, "body": body, "sent_at": sent_at}


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_renders_in_time_order_not_input_order():
    """01c renumbers seq after every batch. A thread read mid-renumber must
    still come out in the order the messages were sent."""
    r = prepare([
        msg(2, "agent", "second", "2026-08-27T10:05:00+03:00"),
        msg(1, "customer", "first", "2026-08-27T10:00:00+03:00"),
        msg(3, "customer", "third", "2026-08-27T10:09:00+03:00"),
    ])
    assert r.status_code == 200
    lines = r.json()["transcript_text"].splitlines()
    assert [l.split(": ", 1)[1] for l in lines] == ["first", "second", "third"]


def test_seq_breaks_ties_at_the_same_instant():
    """Two messages in the same second collided under the old handler's
    count(*)+1 numbering; whichever order they land in, the render must be
    stable rather than dependent on dict ordering."""
    r = prepare([
        msg(7, "agent", "later", "2026-08-27T10:00:00+03:00"),
        msg(6, "customer", "earlier", "2026-08-27T10:00:00+03:00"),
    ])
    bodies = [l.split(": ", 1)[1] for l in r.json()["transcript_text"].splitlines()]
    assert bodies == ["earlier", "later"]


# ---------------------------------------------------------------------------
# Metrics — the numbers no model may be asked for
# ---------------------------------------------------------------------------

def test_first_response_is_the_agents_reply_gap():
    r = prepare([
        msg(1, "customer", "السلام عليكم", "2026-08-27T10:00:00+03:00"),
        msg(2, "agent", "أهلاً بك", "2026-08-27T10:01:30+03:00"),
    ])
    assert r.json()["metrics"]["first_response_seconds"] == 90


def test_first_response_is_never_negative_when_the_agent_opens():
    """The replaced handler computed min(agent) - min(customer) in SQL, which
    is negative on every thread the agent starts. Outbound campaigns are all of
    them."""
    r = prepare([
        msg(1, "agent", "عرض خاص لك", "2026-08-27T10:00:00+03:00"),
        msg(2, "customer", "كم السعر؟", "2026-08-27T11:00:00+03:00"),
        msg(3, "agent", "٢٥٠٠ ريال", "2026-08-27T11:02:00+03:00"),
    ])
    frs = r.json()["metrics"]["first_response_seconds"]
    assert frs is None or frs >= 0


def test_offsets_are_honoured_not_dropped():
    """Gotcha 11. The same wall clock in two zones is two different instants,
    and after_hours reads the portal's local time, not the string's."""
    riyadh = prepare([
        msg(1, "customer", "مساء الخير", "2026-08-27T19:24:00+03:00"),
        msg(2, "agent", "أهلاً", "2026-08-27T19:30:00+03:00"),
    ]).json()["metrics"]
    utc = prepare([
        msg(1, "customer", "مساء الخير", "2026-08-27T19:24:00+00:00"),
        msg(2, "agent", "أهلاً", "2026-08-27T19:30:00+00:00"),
    ]).json()["metrics"]
    # 19:24+00 is 22:24 in Riyadh — plainly after hours — while 19:24+03 is
    # not. A reader that dropped the offset would call both the same.
    assert riyadh["after_hours"] != utc["after_hours"]


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------

def test_bot_only_thread_is_not_scoreable():
    """No human agent ever joined, so there is no agent to grade. Scoring it
    files a number about a bot under a person's name."""
    body = prepare([
        msg(1, "customer", "مرحبا", "2026-08-27T10:00:00+03:00"),
        msg(2, "bot", "أهلاً! لحظات لتحويلك", "2026-08-27T10:00:05+03:00"),
    ]).json()
    assert body["is_bot_only"] is True
    assert body["should_evaluate"] is False


def test_thread_with_no_customer_turn_is_not_scoreable():
    """Observed on 63% of a 60-conversation sample: every inbound turn arrives
    labelled Agent. Pass 2 would grade the agent on the customer's sentences."""
    body = prepare([
        msg(1, "agent", "واحد 25/7", "2026-08-27T10:00:00+03:00"),
        msg(2, "agent", "تمام", "2026-08-27T10:01:00+03:00"),
    ]).json()
    assert body["has_no_customer_turn"] is True
    assert body["should_evaluate"] is False


def test_a_normal_thread_is_scoreable():
    body = prepare([
        msg(1, "customer", "عايز رحلة دبي", "2026-08-27T10:00:00+03:00"),
        msg(2, "bot", "لحظات", "2026-08-27T10:00:03+03:00"),
        msg(3, "agent", "أهلاً، معك أحمد", "2026-08-27T10:01:00+03:00"),
    ]).json()
    assert body["should_evaluate"] is True
    assert body["message_count"] == 3


# ---------------------------------------------------------------------------
# Input the database can actually contain
# ---------------------------------------------------------------------------

def test_empty_thread_is_rejected_not_scored_as_silence():
    assert prepare([]).status_code == 422


def test_unparseable_timestamp_names_the_message():
    r = prepare([msg(1, "customer", "hi", "not-a-date")])
    assert r.status_code == 422
    assert "seq 1" in r.json()["detail"]


def test_unknown_sender_is_neither_agent_nor_customer():
    """01c writes 'unknown' when the API sends a role we do not map. It must
    not be silently promoted into either side of the conversation."""
    body = prepare([
        msg(1, "customer", "hi", "2026-08-27T10:00:00+03:00"),
        msg(2, "sales_manager", "hello", "2026-08-27T10:01:00+03:00"),
    ]).json()
    assert "UNKNOWN: hello" in body["transcript_text"]
    assert body["is_bot_only"] is True  # no mapped agent turn


def test_empty_body_still_occupies_its_turn():
    """An attachment arrives with no text. Dropping the turn shortens every
    response gap around it; the replaced handler dropped them."""
    body = prepare([
        msg(1, "customer", "", "2026-08-27T10:00:00+03:00"),
        msg(2, "agent", "وصلتني الصورة", "2026-08-27T10:02:00+03:00"),
    ]).json()
    assert body["message_count"] == 2
    assert body["metrics"]["first_response_seconds"] == 120


def test_auth_is_required():
    r = client.post("/chats/prepare", json={"external_id": "x", "messages": []})
    assert r.status_code == 401
