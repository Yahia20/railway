"""Smoke tests against the REAL fixtures — the actual Bitrix payload and the
actual Bitrix payload, not invented ones.

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
from app.sources import get_chat_source
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
