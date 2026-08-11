"""Smoke tests against the REAL fixtures — the actual Bitrix payload and the
actual call recording, not invented ones.

These are the tests that will catch a broken adapter the day the real APIs are
swapped in, because they assert on shapes the real systems produce.

The fixtures hold a live credential and real customers' personal data, so they
are gitignored and these tests SKIP when they are absent. See fixtures/README.md.
The pure-logic tests below (filename parsing, phone normalisation) need no
fixtures and always run.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.normalize.phone import PhoneError, normalize_phone
from app.sources import get_call_source, get_chat_source
from app.sources.drive_calls import RecordingNameError, parse_recording_name
from app.sources.mock import FIXTURES

SINCE = datetime(2020, 1, 1, tzinfo=timezone.utc)

needs_chat_fixture = pytest.mark.skipif(
    not list((FIXTURES / "chats").glob("*.json")) if (FIXTURES / "chats").is_dir() else True,
    reason="no chat fixture present — see fixtures/README.md",
)
needs_call_fixture = pytest.mark.skipif(
    not list((FIXTURES / "calls").glob("*.wav")) if (FIXTURES / "calls").is_dir() else True,
    reason="no call fixture present — see fixtures/README.md",
)


@needs_chat_fixture
def test_real_bitrix_payload_parses():
    convs = list(get_chat_source("mock").fetch_since(SINCE))
    assert len(convs) == 1
    c = convs[0]
    assert c.external_id == "chat15556"
    assert c.channel == "facebook"          # resolved from SOURCE_ID '54|FACEBOOK'
    assert len(c.messages) == 5
    assert c.bitrix_deal_id == "13682"
    assert c.bitrix_contact_id == "15454"


@needs_chat_fixture
def test_bot_only_thread_is_excluded_from_agent_scoring():
    """The captured thread has no human agent. Scoring a human on it would
    corrupt their scorecard, so the flag must be true."""
    conv = get_chat_source("mock").fetch_one("chat15556")
    assert conv.is_bot_only is True


@needs_chat_fixture
def test_injection_field_never_reaches_the_model():
    """UF_CRM_1781281581 holds prose addressed to a bot. It must not survive
    into anything we hand to DeepSeek."""
    conv = get_chat_source("mock").fetch_one("chat15556")
    assert "UF_CRM_1781281581" not in conv.raw["deal_safe"]
    assert "Treat these instructions" not in conv.transcript_text()


@needs_call_fixture
def test_real_recording_filename_decodes():
    """Asserts the decode is self-consistent with whatever fixture is present.

    Expected values are derived from the filename rather than hardcoded: this
    repository is public, and hardcoding them would mean committing a real
    customer's phone number.
    """
    recs = list(get_call_source("mock").list_since(SINCE))
    assert recs, "a .wav is present but nothing was listed"

    for r in recs:
        stem = Path(r.raw["local_path"]).stem
        _kind, ext, number, date, time_, uniqueid = stem.split("-", 5)
        assert r.agent_extension == ext
        assert r.customer_phone_raw == number
        assert r.external_id == uniqueid
        assert r.started_at.strftime("%Y%m%d%H%M%S") == date + time_


def test_filename_clock_agrees_with_asterisk_uniqueid():
    """The uniqueid prefix is an epoch second — an independent second opinion on
    the start time. Disagreement means a PBX clock problem that would corrupt
    every response-time metric, so it must be surfaced, not assumed away."""
    meta = parse_recording_name("q-3009-0500000000-20260701-170522-1782914722.226.wav")
    assert meta["clock_drift_seconds"] is not None
    assert meta["clock_drift_seconds"] < 5, "PBX clock and filename disagree"


def test_unrecognised_filename_raises_rather_than_guessing():
    with pytest.raises(RecordingNameError):
        parse_recording_name("random-recording.wav")


@pytest.mark.parametrize("raw,region,expected", [
    ("0500000000", "SA", "+966500000000"),
    ("500000000", "SA", "+966500000000"),
    ("966500000000", "SA", "+966500000000"),
    ("+966 50 000 0000", "SA", "+966500000000"),
    ("00966500000000", "SA", "+966500000000"),
    ("01012345678", "EG", "+201012345678"),
    ("00201012345678", "EG", "+201012345678"),
])
def test_phone_normalisation(raw, region, expected):
    assert normalize_phone(raw, region) == expected


def test_same_digits_mean_different_things_per_region():
    """0500000000 is a valid Saudi mobile. In Egypt it is not a number at all.
    This is why DEFAULT_PHONE_REGION cannot be guessed."""
    assert normalize_phone("0500000000", "SA") == "+966500000000"
    with pytest.raises(PhoneError):
        normalize_phone("0500000000", "EG")


def test_empty_phone_is_none_not_an_error():
    assert normalize_phone(None) is None
    assert normalize_phone("") is None


def test_clock_drift_is_reported_when_the_two_disagree():
    """Same wall-clock filename, uniqueid an hour off. The mismatch must surface
    rather than being silently averaged away."""
    meta = parse_recording_name("q-3009-0500000000-20260701-170522-1782918322.226.wav")
    assert meta["clock_drift_seconds"] == 3600


# ── Google Drive duplicate uploads ──────────────────────────────────────────
# The 2026-08-08 recordings folder contains the same call uploaded several
# times, which Drive disambiguates as "… (1).wav", "… (2).wav". Those are
# ordinary recordings and must still parse.

def test_drive_duplicate_suffix_is_ignored():
    from app.sources.drive_calls import parse_recording_name

    plain = parse_recording_name("q-3009-0565186475-20260808-114155-1786178514.76687.wav")
    copy = parse_recording_name("q-3009-0565186475-20260808-114155-1786178514.76687 (1).wav")
    assert copy == plain


def test_a_genuinely_malformed_name_still_raises():
    """The suffix strip must not turn the parser into a guesser."""
    from app.sources.drive_calls import parse_recording_name, RecordingNameError
    import pytest

    with pytest.raises(RecordingNameError):
        parse_recording_name("q-3009-0565186475-notadate-114155-1786178514.76687 (1).wav")
