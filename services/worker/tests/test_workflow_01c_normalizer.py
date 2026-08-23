"""Run workflow 01c's Code node for real, against the production payload shape.

The normaliser is JavaScript living inside a JSON file, which is exactly the
kind of code that rots unwatched. These tests execute the committed `jsCode`
itself — not a Python re-implementation of it — with n8n's `$input` stubbed out.

Install the engine to run them:  pip install quickjs
Without it every test here skips and the rest of the suite is unaffected.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WORKFLOW = (Path(__file__).resolve().parents[3]
            / "n8n" / "workflows" / "01c-chats-store-only.json")

RIYADH = timezone(timedelta(hours=3))
START = datetime(2026, 8, 20, 19, 28, 55, tzinfo=RIYADH)
CONV = "54008fdc-d08f-42e1-b257-f183e988c6c5"
DEAL = "37800"
CONTACT = "49498"


def _js_code() -> str:
    wf = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    node = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.code")
    return node["parameters"]["jsCode"]


def normalize(body) -> dict:
    """Execute the committed Code node against `body` and return its one item."""
    quickjs = pytest.importorskip("quickjs")
    harness = (
        "var BODY = " + json.dumps(body, ensure_ascii=False) + ";\n"
        "var $input = { first: function () { return { json: { body: BODY } }; } };\n"
        "var __result = (function () {\n" + _js_code() + "\n})();\n"
        "JSON.stringify(__result[0].json);\n"
    )
    return json.loads(quickjs.Context().eval(harness))


def row(text: str, role: str, minutes: int, **overrides) -> dict:
    """One message row in the exact shape the production API sends."""
    base = {
        "message": text,
        "dealid": DEAL,
        "crm_entity_id": DEAL,
        "contact_id": CONTACT,
        "created_at": START.isoformat(),
        "updated_at": (START + timedelta(minutes=45)).isoformat(),
        "conversation_id": CONV,
        "sender_id": "86" if role == "Agent" else CONTACT,
        "timestamp": (START + timedelta(minutes=minutes)).isoformat(),
        "sender_role": role,
        "content_type": "text",
    }
    base.update(overrides)
    return base


THREAD = [
    row("السلام عليكم، ممكن تفاصيل برنامج شرم الشيخ؟", "Customer", 0),
    row("وعليكم السلام، حاضر أبعتلك البرنامج.", "Agent", 4),
    row("تمام، في انتظارك", "Customer", 6),
    row("اتفضل استاذي الكريم البرنامج وفي انتظار رد حضرتك الكريم ان شاء الله", "Agent", 41),
]


# --------------------------------------------------------------------------
# The shape the API actually sends
# --------------------------------------------------------------------------

def test_the_documented_single_row_payload_parses():
    """The literal example from the API team, verbatim."""
    out = normalize([{
        "message": "اتفضل استاذي الكريم البرنامج وفي انتظار رد حضرتك الكريم ان شاء الله",
        "dealid": "37800",
        "crm_entity_id": "37800",
        "contact_id": "49498",
        "created_at": "2026-08-20T19:28:55+03:00",
        "updated_at": "2026-08-23T12:15:10+03:00",
        "conversation_id": CONV,
        "sender_id": "86",
        "timestamp": "2026-08-23T12:31:09+03:00",
        "sender_role": "Agent",
        "content_type": "text",
    }])
    assert out["stats"]["conversations"] == 1
    conv = out["conversations"][0]
    assert conv["external_id"] == CONV
    assert conv["external_deal_id"] == "37800"
    assert conv["external_contact_id"] == "49498"
    assert conv["messages"][0]["sender"] == "agent"
    assert conv["messages"][0]["sender_external_id"] == "86"
    assert conv["messages"][0]["content_type"] == "text"
    assert out["reject_reason"] == ""


def test_repeated_metadata_collapses_to_one_conversation():
    """Four rows carrying four copies of the deal id become one record."""
    out = normalize(THREAD)
    assert out["stats"]["conversations"] == 1
    assert out["stats"]["rows_stored"] == 4
    conv = out["conversations"][0]
    assert conv["external_deal_id"] == DEAL
    assert conv["external_contact_id"] == CONTACT
    assert conv["started_at"] == THREAD[0]["timestamp"]
    assert conv["ended_at"] == THREAD[3]["timestamp"]
    # The deal id appears once on the conversation, not once per message.
    assert "external_deal_id" not in conv["messages"][0]


def test_messages_come_out_in_time_order():
    out = normalize(list(reversed(THREAD)))
    sent = [m["sent_at"] for m in out["conversations"][0]["messages"]]
    assert sent == sorted(sent)


def test_the_timezone_offset_survives():
    """+03:00 must reach Postgres intact; re-rendering in UTC loses what was said."""
    out = normalize(THREAD)
    assert out["conversations"][0]["messages"][0]["sent_at"].endswith("+03:00")


# --------------------------------------------------------------------------
# Not storing the same thing twice
# --------------------------------------------------------------------------

def test_an_exact_duplicate_row_is_collapsed():
    out = normalize(THREAD + [THREAD[1]])
    assert out["stats"]["duplicates_in_batch"] == 1
    assert out["stats"]["rows_stored"] == 4


def test_the_same_instant_in_a_different_offset_is_the_same_message():
    """+03:00 and Z can spell one instant; a string compare would store both."""
    same = dict(THREAD[1])
    same["timestamp"] = (START + timedelta(minutes=4)).astimezone(timezone.utc).isoformat()
    out = normalize(THREAD + [same])
    assert out["stats"]["duplicates_in_batch"] == 1
    assert out["stats"]["rows_stored"] == 4


def test_the_same_words_at_a_different_time_are_two_messages():
    """Dedup must not swallow a customer who really did say نعم twice."""
    out = normalize([row("نعم", "Customer", 1), row("نعم", "Customer", 9)])
    assert out["stats"]["duplicates_in_batch"] == 0
    assert out["stats"]["rows_stored"] == 2


def test_metadata_conflicts_resolve_to_the_newest_row():
    older = row("قديم", "Agent", 1, contact_id="11111",
                updated_at=(START + timedelta(minutes=1)).isoformat())
    newer = row("جديد", "Agent", 2, contact_id="22222",
                updated_at=(START + timedelta(minutes=2)).isoformat())
    assert normalize([newer, older])["conversations"][0]["external_contact_id"] == "22222"
    assert normalize([older, newer])["conversations"][0]["external_contact_id"] == "22222"


# --------------------------------------------------------------------------
# Batches, and rows that cannot be stored
# --------------------------------------------------------------------------

def test_two_conversations_in_one_array_stay_separate():
    other = row("محادثة تانية", "Customer", 3, conversation_id="other-conv", dealid="41000")
    out = normalize(THREAD + [other])
    assert out["stats"]["conversations"] == 2
    assert sorted(c["external_id"] for c in out["conversations"]) == \
        sorted([CONV, "other-conv"])
    # external_ref is only meaningful for a single-deal batch.
    assert out["external_ref"] is None


def test_a_single_deal_batch_names_it_as_the_external_ref():
    assert normalize(THREAD)["external_ref"] == DEAL


def test_a_row_without_a_timestamp_is_dropped_not_guessed():
    broken = row("بدون وقت", "Agent", 5)
    broken["timestamp"] = ""
    out = normalize(THREAD + [broken])
    assert out["stats"]["rejected_no_timestamp"] == 1
    assert out["stats"]["rows_stored"] == 4
    assert "partial" in out["reject_reason"]


def test_a_row_with_no_conversation_key_is_dropped():
    orphan = row("بدون مفتاح", "Agent", 5)
    for key in ("conversation_id", "dealid", "crm_entity_id"):
        orphan.pop(key)
    out = normalize([orphan])
    assert out["stats"]["rejected_no_key"] == 1
    assert out["conversations"] == []
    assert "nothing storable" in out["reject_reason"]


def test_the_deal_id_is_the_fallback_thread_key():
    no_conv = row("بدون conversation_id", "Agent", 5)
    no_conv.pop("conversation_id")
    assert normalize([no_conv])["conversations"][0]["external_id"] == f"deal-{DEAL}"


def test_a_bare_object_and_the_wrapped_forms_are_all_accepted():
    for body in (THREAD[0], {"messages": THREAD}, {"data": THREAD}):
        assert normalize(body)["stats"]["conversations"] == 1


def test_junk_is_reported_rather_than_thrown():
    """The webhook already answered 200; a throw here loses the delivery."""
    out = normalize(["not an object", 42, None])
    assert out["conversations"] == []
    assert out["stats"]["rejected_not_an_object"] == 3
    assert "nothing storable" in out["reject_reason"]


def test_an_empty_array_is_survivable():
    out = normalize([])
    assert out["conversations"] == []
    assert out["external_ids"] == []
    assert "nothing storable" in out["reject_reason"]


# --------------------------------------------------------------------------
# Role mapping
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sent,stored", [
    ("Customer", "customer"), ("customer", "customer"), ("Client", "customer"),
    ("Agent", "agent"), ("agent", "agent"), ("Operator", "agent"),
    ("Bot", "bot"), ("System", "system"), ("Supervisor", "unknown"), ("", "unknown"),
])
def test_sender_role_maps_onto_the_speaker_role_enum(sent, stored):
    """An unmapped role must land as 'unknown', not as an invalid enum value."""
    assert normalize([row("x", sent, 0)])["conversations"][0]["messages"][0]["sender"] == stored
