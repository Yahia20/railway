"""Who answered an inbound call, from the PBX journey. Pure: no I/O, no writes.

call_log.extension is the FIRST leg that rang for an inbound call, not the one
that answered (one queue extension holds most of them). The journey endpoint
(`/api/call-log/journey?linkedid=`) records every leg; this module reduces it
to one answer, or to the reason there is none.

It decides the EXTENSION only. Which person owns an extension is resolved live
in SQL (v_call_owner, migration 036) from the owner-approved mapping, so a
corrected mapping re-attributes every past call without re-reading anything.

What a real journey looks like (inbound, ring-group queue):

    INBOUND, QUEUE_ENTER, then ~90 RING / TRANSFER / UNANSWERED lines (the
    group fan-out: one per extension rung, NOT real transfers), then
    ANSWER agent=125 (often twice: the bridge and the talk leg), HANGUP.

Fails closed: anything that is not exactly one answering extension in a
complete journey is a verdict with no extension, and the call is shown as
unattributed — never guessed onto a person.
"""
from __future__ import annotations

import re
from typing import Any

# Events worth keeping. RING/TRANSFER/UNANSWERED are the ring-group fan-out.
KEEP = {"INBOUND", "OUTBOUND", "QUEUE_ENTER", "ANSWER", "HANGUP",
        "LISTEN", "WHISPER", "BARGE", "BLIND_TRANSFER", "ATTENDED_TRANSFER"}
SUPERVISION = {"LISTEN", "WHISPER", "BARGE"}
# A staff extension is a short number. An outbound ANSWER carries the dialled
# external number in the same field, which must never pass for a person.
EXTENSION = re.compile(r"^[0-9]{2,6}$")
PRIVATE_FIELDS = {"from_number", "to_number"}


def reduce_journey(body: Any) -> dict[str, Any]:
    """Return {verdict, answer_extension, answer_extensions, events}.

    verdict is 'answered' only when the journey is complete (an INBOUND and a
    HANGUP) and every ANSWER names the same staff extension.
    """
    journey = body.get("journey") if isinstance(body, dict) else None
    if not isinstance(journey, list) or not all(isinstance(e, dict) for e in journey):
        return _out("malformed", [], [])

    events = [
        {k: v for k, v in e.items() if k not in PRIVATE_FIELDS}
        for e in journey if e.get("event") in KEEP
    ]
    kinds = {e.get("event") for e in journey}
    if "OUTBOUND" in kinds and "INBOUND" not in kinds:
        return _out("not_inbound", [], events)
    if "INBOUND" not in kinds or "HANGUP" not in kinds:
        return _out("journey_incomplete", [], events)
    if kinds & SUPERVISION:
        return _out("supervision_requires_review", [], events)

    answers = [e for e in journey if e.get("event") == "ANSWER"]
    if not answers:
        return _out("no_answer_evidence", [], events)
    agents = [e.get("agent") for e in answers]
    if any(not isinstance(a, str) or not EXTENSION.match(a.strip()) for a in agents):
        return _out("answer_identity_missing", [], events)
    extensions = sorted({a.strip() for a in agents})
    if len(extensions) != 1:
        # Two people answered (a real transfer, or a pickup): the call has no
        # single owner, so it counts for the team and for nobody's average.
        return _out("multiple_answering_extensions", extensions, events)
    return {"verdict": "answered", "answer_extension": extensions[0],
            "answer_extensions": extensions, "events": events}


def _out(verdict: str, extensions: list[str], events: list[dict]) -> dict[str, Any]:
    return {"verdict": verdict, "answer_extension": None,
            "answer_extensions": extensions, "events": events}
