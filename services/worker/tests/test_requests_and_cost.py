"""Multi-request extraction and per-call cost accounting (017).

Every test here is a way the two features can produce a number that looks fine
and is wrong:

  * a request the model invented, counted as revenue nobody logged
  * an enum label the model made up, aborting the whole write
  * a prompt priced as if none of it was cached, overstating the bill ~5x
  * a night run priced at the peak rate, overstating it another 2x
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.evaluate import judge

CONVERSATION = """
[00:01] العميل: السلام عليكم، عايز باكدج لتركيا لأربع أفراد
[00:02] الموظف: وعليكم السلام، أبشر
[00:03] العميل: وكمان محتاج فيزا شنغن لأخويا
[00:04] الموظف: تمام نجهزلك الاتنين
"""


def _payload(requests, is_real=True):
    return {"real_ask": {"is_real_inquiry": is_real}, "requests": requests}


# ---------------------------------------------------------------------------
# requests[] → interaction_requests rows
# ---------------------------------------------------------------------------

def test_two_requests_both_survive():
    """The whole point of 017: the second request used to be dropped."""
    rows = judge.normalize_requests(_payload([
        {"service": "package", "destination": "تركيا", "travelers_total": 4,
         "buying_stage": "consideration",
         "evidence": ["عايز باكدج لتركيا لأربع أفراد"]},
        {"service": "visa", "destination": "شنغن", "buying_stage": "awareness",
         "evidence": ["وكمان محتاج فيزا شنغن لأخويا"]},
    ]), CONVERSATION)

    assert [r["seq"] for r in rows] == [1, 2]
    assert [r["service"] for r in rows] == ["package", "visa"]
    assert all(r["evidence_valid"] is True for r in rows)


def test_invented_quote_is_flagged_not_dropped():
    """A fabricated request must not be silently deleted OR silently counted.

    `v_request_reconciliation` reads an unmatched request as 'the agent never
    opened a deal for this', which sends a human after a customer. So the row
    is kept for audit and marked False, and the view excludes it from the count.
    """
    rows = judge.normalize_requests(_payload([
        {"service": "hotel", "evidence": ["عايز حجز فندق في دبي لخمس ليالي"]},
    ]), CONVERSATION)

    assert len(rows) == 1, "the row is kept as evidence of what the model did"
    assert rows[0]["evidence_valid"] is False


def test_missing_quote_is_none_not_false():
    """Absent is not the same as fabricated, and the view treats them apart."""
    rows = judge.normalize_requests(_payload([{"service": "flight"}]), CONVERSATION)
    assert rows[0]["evidence_valid"] is None


def test_unknown_enum_label_becomes_null_and_keeps_the_word():
    """An enum column refuses an unknown label with a 22P02 that aborts the
    whole transaction — including the pass-1 row that was perfectly fine."""
    rows = judge.normalize_requests(_payload([
        {"service": "umrah_package", "buying_stage": "very_hot",
         "evidence": ["عايز باكدج لتركيا لأربع أفراد"]},
    ]), CONVERSATION)

    assert rows[0]["service"] is None
    assert rows[0]["service_raw"] == "umrah_package"
    assert rows[0]["buying_stage"] is None


def test_outcome_follows_the_fixed_rule_not_the_model():
    """Same mapping as deal_outcome_from_analysis (015), so one request and one
    conversation can never disagree about the same two observations."""
    rows = judge.normalize_requests(_payload([
        {"service": "package", "buying_stage": "purchased"},
        {"service": "visa", "buying_stage": "consideration"},
        {"service": "hotel", "buying_stage": "lost"},
        {"service": "flight"},
    ]), CONVERSATION)
    assert [r["outcome"] for r in rows] == ["won", "in_progress", "lost", "unknown"]


def test_not_a_real_inquiry_outranks_every_stage():
    """A supplier pitching us is not a lost sale, whatever stage it claims —
    counting it as one is how a funnel acquires a fake denominator."""
    rows = judge.normalize_requests(
        _payload([{"service": "package", "buying_stage": "purchased"}], is_real=False),
        CONVERSATION)
    assert rows[0]["outcome"] == "no_opportunity"


def test_old_prompt_without_requests_returns_empty():
    """Empty means 'the prompt is too old to know', never 'they wanted nothing'
    — the caller falls back to the single-request columns."""
    assert judge.normalize_requests({"intent": "price_inquiry"}, CONVERSATION) == []
    assert judge.normalize_requests({"requests": "two"}, CONVERSATION) == []


def test_non_dict_entries_are_skipped_without_renumbering_gaps():
    rows = judge.normalize_requests(_payload([
        {"service": "package"}, "nonsense", {"service": "visa"},
    ]), CONVERSATION)
    assert [r["service"] for r in rows] == ["package", "visa"]


# ---------------------------------------------------------------------------
# cost accounting
# ---------------------------------------------------------------------------

PEAK = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)        # Wednesday 07:00
OFFPEAK = datetime(2026, 9, 2, 22, 0, tzinfo=timezone.utc)    # Wednesday 22:00
WEEKEND = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)     # Saturday 07:00


def test_peak_window_matches_the_published_hours():
    assert judge.is_peak(PEAK) is True
    assert judge.is_peak(OFFPEAK) is False
    assert judge.is_peak(datetime(2026, 9, 2, 2, 0, tzinfo=timezone.utc)) is True
    assert judge.is_peak(datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)) is False


def test_weekend_is_never_peak():
    """Peak is Monday-Friday. Saturday at 07:00 UTC is half price."""
    assert judge.is_peak(WEEKEND) is False


def test_cached_prompt_is_not_priced_as_fresh():
    """The static prompt is ~15k tokens and a cache hit costs 1/31 of a miss.
    Pricing the whole prompt at the miss rate overstates a chat by about 5x."""
    usage = {"prompt_tokens": 15_400, "prompt_cache_hit_tokens": 14_979,
             "prompt_cache_miss_tokens": 421, "completion_tokens": 1_849}
    cached = judge.estimate_cost_usd(usage, "deepseek-v4-flash", PEAK)
    naive = judge.estimate_cost_usd(
        {"prompt_tokens": 15_400, "prompt_cache_miss_tokens": 15_400,
         "completion_tokens": 1_849}, "deepseek-v4-flash", PEAK)
    assert cached < naive / 2
    assert cached == pytest.approx(0.00285, abs=5e-5)


def test_off_peak_is_exactly_half():
    usage = {"prompt_tokens": 15_400, "prompt_cache_hit_tokens": 14_979,
             "prompt_cache_miss_tokens": 421, "completion_tokens": 1_849}
    assert (judge.estimate_cost_usd(usage, "deepseek-v4-flash", OFFPEAK)
            == pytest.approx(judge.estimate_cost_usd(usage, "deepseek-v4-flash", PEAK) / 2,
                             rel=1e-6))


def test_missing_split_infers_the_miss_side_and_never_goes_negative():
    """Only one side reported: the other is whatever is left of the total."""
    assert judge.estimate_cost_usd(
        {"prompt_tokens": 1_000, "prompt_cache_hit_tokens": 900,
         "completion_tokens": 100}, "deepseek-v4-flash", PEAK) > 0
    # A hit count larger than the total is nonsense the API should never send;
    # it must not produce a negative charge.
    assert judge.estimate_cost_usd(
        {"prompt_tokens": 100, "prompt_cache_hit_tokens": 900,
         "completion_tokens": 0}, "deepseek-v4-flash", PEAK) >= 0


def test_unknown_model_returns_none_rather_than_a_guess():
    """A wrong cost_usd is worse than a null: a null shows up in a sum as a
    missing row, a wrong number does not show up at all."""
    assert judge.estimate_cost_usd({"prompt_tokens": 100}, "stealth/ox-alpha") is None


def test_cost_row_carries_the_idempotency_key():
    row = judge.cost_row(
        "pass1_customer", model="deepseek-v4-flash", prompt_version="pass1-customer-v6",
        input_hash="abc123", usage={"prompt_tokens": 10, "completion_tokens": 5})
    assert row["purpose"] == "pass1_customer"
    assert row["input_hash"] == "abc123"
    assert row["prompt_version"] == "pass1-customer-v6"
    assert row["provider"] == "deepseek"


# ---------------------------------------------------------------------------
# coercion — every one of these aborts an INSERT if it reaches Postgres raw
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["next August", "١٥/١١/٢٠٢٦", "", "2026-13-45", None, 7])
def test_unparseable_dates_become_null_not_a_transaction_abort(bad):
    """A date column handed 'next August' raises 22007 and Postgres aborts the
    WHOLE statement — losing every other request in the same conversation."""
    rows = judge.normalize_requests(_payload([{"service": "package", "date_start": bad}]),
                                    CONVERSATION)
    assert rows[0]["date_start"] is None


def test_real_dates_survive():
    rows = judge.normalize_requests(
        _payload([{"service": "package", "date_start": "2026-11-15",
                   "date_end": "2026-11-22T00:00:00"}]), CONVERSATION)
    assert rows[0]["date_start"] == "2026-11-15"
    assert rows[0]["date_end"] == "2026-11-22"


def test_budget_written_the_way_a_human_writes_it_is_kept():
    """'17,200 ريال' is the field the commercial half of this project exists to
    collect. Dropping it to NULL because of a comma loses the number."""
    rows = judge.normalize_requests(
        _payload([{"service": "package", "budget_amount": "17,200 ريال",
                   "budget_currency": "sar"}]), CONVERSATION)
    assert rows[0]["budget_amount"] == 17200.0
    assert rows[0]["budget_currency"] == "SAR"


@pytest.mark.parametrize("bad", ["كتير", "", None, "غير محدد"])
def test_unparseable_budget_becomes_null(bad):
    rows = judge.normalize_requests(
        _payload([{"service": "package", "budget_amount": bad}]), CONVERSATION)
    assert rows[0]["budget_amount"] is None


def test_travelers_and_nights_are_ints_or_null():
    rows = judge.normalize_requests(
        _payload([{"service": "package", "travelers_total": "4", "nights": "سبعة"}]),
        CONVERSATION)
    assert rows[0]["travelers_total"] == 4
    assert rows[0]["nights"] is None
