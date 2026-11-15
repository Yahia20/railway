"""Who answered an inbound call: the journey reduction. Synthetic journeys only."""
import pytest

from app.calls_archive.attribution import reduce_journey


def journey(*middle):
    """A complete inbound journey with the ring-group fan-out real calls carry."""
    fanout = []
    for ext in ("201", "202", "203"):
        fanout += [{"event": "RING", "agent": ext}, {"event": "TRANSFER", "agent": ext},
                   {"event": "UNANSWERED", "agent": ext}]
    return {"journey": [{"event": "INBOUND", "time": "2026-11-03T14:46:49", "from_number": "0500000000"},
                        {"event": "QUEUE_ENTER", "queue": "3000"}, *fanout, *middle,
                        {"event": "HANGUP", "reason": "BUSY", "ended_by": "customer"}]}


def test_the_answering_extension_wins_not_the_first_ring():
    r = reduce_journey(journey({"event": "ANSWER", "agent": "202"}, {"event": "ANSWER", "agent": "202"}))
    assert r["verdict"] == "answered"
    assert r["answer_extension"] == "202"


def test_ring_group_fanout_is_dropped_and_the_caller_number_never_kept():
    r = reduce_journey(journey({"event": "ANSWER", "agent": "202"}))
    kinds = [e["event"] for e in r["events"]]
    assert "RING" not in kinds and "TRANSFER" not in kinds and "UNANSWERED" not in kinds
    assert all("from_number" not in e for e in r["events"])


@pytest.mark.parametrize("middle,verdict", [
    ((), "no_answer_evidence"),
    (({"event": "ANSWER"},), "answer_identity_missing"),
    (({"event": "ANSWER", "agent": " "},), "answer_identity_missing"),
    # an outbound-style dial string in the agent field is not a staff extension
    (({"event": "ANSWER", "agent": "00966500000000"},), "answer_identity_missing"),
    (({"event": "ANSWER", "agent": "201"}, {"event": "ANSWER", "agent": "202"}), "multiple_answering_extensions"),
    (({"event": "ANSWER", "agent": "201"}, {"event": "WHISPER", "agent": "202"}), "supervision_requires_review"),
])
def test_anything_but_one_answer_has_no_extension(middle, verdict):
    r = reduce_journey(journey(*middle))
    assert r["verdict"] == verdict
    assert r["answer_extension"] is None


def test_two_answerers_are_reported_for_the_team_view():
    r = reduce_journey(journey({"event": "ANSWER", "agent": "201"}, {"event": "ANSWER", "agent": "202"}))
    assert r["answer_extensions"] == ["201", "202"]


@pytest.mark.parametrize("body", [None, {}, {"journey": None}, {"journey": ["x"]}, "text"])
def test_malformed_bodies_fail_closed(body):
    assert reduce_journey(body) == {"verdict": "malformed", "answer_extension": None,
                                    "answer_extensions": [], "events": []}


def test_incomplete_journey_fails_closed():
    body = {"journey": [{"event": "INBOUND"}, {"event": "ANSWER", "agent": "201"}]}  # no HANGUP
    assert reduce_journey(body)["verdict"] == "journey_incomplete"


def test_outbound_journey_is_not_an_inbound_answer():
    body = {"journey": [{"event": "OUTBOUND", "to_number": "0500000000"},
                        {"event": "ANSWER", "agent": "201"}, {"event": "HANGUP"}]}
    assert reduce_journey(body)["verdict"] == "not_inbound"
