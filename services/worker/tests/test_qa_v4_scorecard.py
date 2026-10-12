"""The v4 chat scorecard (app/qa/v4): the owner's handoff of 2026-10-11, section 5.

Each test is a rule from the handoff or a fixture from qa_prompt_v4_fixtures.md
(A-F), with the model's answer written by hand — so what is tested is the
code's reading of an answer, which is the only part that must never move.
No credentials, no database, no model call.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.qa.v4 import claude, engine, render, scorecard, texts, transcript
from app.qa.v4.evidence import verified_line

AGENT_A, AGENT_B = "501", "502"


def thread(*rows: tuple[str, str, str] | tuple[str, str, str, str], name: str | None = None) -> dict:
    """rows: (role, "MM-DD HH:MM", text[, agent_ext]) in order."""
    msgs = []
    for row in rows:
        role, when, text = row[:3]
        ext = row[3] if len(row) > 3 else (AGENT_A if role == "agent" else None)
        day, hm = when.split()
        msgs.append({"role": role, "type": "text", "body": text,
                     "at": f"2026-{day}T{hm}:00+03:00", "agent_ext": ext})
    return {"id": "00000000-0000-0000-0000-000000000001", "agent_id": "00000000-0000-0000-0000-0000000000aa",
            "customer_name": {"name": name, "source": "crm_contact"} if name else None, "messages": msgs}


def answer(**blocks: Any) -> dict:
    """A call-1 answer with every key present and empty, then the given blocks."""
    out: dict[str, Any] = {
        "chat_type": {"type": "sales"}, "booking": {}, "offer": {"attachment_unclear": False},
        "followup": [], "q3": {}, "q13": {}, "q15": {}, "q16": {}, "q18": {}, "q21": {},
        "q25": [], "q26": {}, "q30": {"option_quotes": [], "option_lines": []}, "q32": [],
        "q34": {}, "q36": {}, "q41": {"reason": "No Response"},
    }
    out.update(blocks)
    return out


def grade(t: dict, out: dict, runs: int = 3, loss_votes=None) -> scorecard.Grade:
    chat = transcript.build(t)
    return scorecard.combine(chat, [scorecard.assess_run(chat, out) for _ in range(runs)], loss_votes)


BOOKING_1 = {"destination_quote": "جورجيا", "destination_line": 1, "date_quote": "٨ نوفمبر", "date_line": 1,
             "travellers_quote": "شخصين", "travellers_line": 1}


# ---------------------------------------------------------------------------
# Fixtures A-F (qa_prompt_v4_fixtures.md)
# ---------------------------------------------------------------------------

def test_fixture_a_qualified_promised_never_delivered_is_a_red_flag():
    t = thread(("customer", "10-01 09:00", "ابغى جورجيا ٢٢-٢٦ شوال انا وزوجتي"),
               ("agent", "10-01 09:01", "بكرا ارسل البرنامج"),
               ("agent", "10-02 10:00", "هل ما زلت مهتم"))
    g = grade(t, answer(
        booking={"destination_quote": "جورجيا", "destination_line": 1, "date_quote": "٢٢-٢٦ شوال",
                 "date_line": 1, "travellers_quote": "انا وزوجتي", "travellers_line": 1},
        q21={"promise_quote": "بكرا ارسل البرنامج", "promise_line": 2}))
    assert g.qualified and g.red_flag == "no_offer"
    assert g.items["offer"].value == 0.0
    assert g.items["promise"] == scorecard.Item(0.0, (2,))


def test_fixture_b_price_is_delivery_and_two_employees_follow_up_together():
    t = thread(("customer", "10-01 09:00", "جورجيا ٨ نوفمبر شخصين بكم"),
               ("agent", "10-01 09:01", "اشيك وارجع لك بالسعر", AGENT_A),
               ("agent", "10-01 09:05", "السعر ٦٠٠٠ ريال لشخصين", AGENT_A),
               ("agent", "10-01 09:05", "شامل السكن والتنقلات", AGENT_A),
               ("agent", "10-02 10:00", "هل اطلعت على العرض", AGENT_A),
               ("agent", "10-03 10:00", "هل عندك استفسار عن العرض", AGENT_B))
    g = grade(t, answer(
        booking=BOOKING_1,
        offer={"offer_quote": "السعر ٦٠٠٠ ريال لشخصين", "offer_line": 3, "attachment_unclear": False},
        followup=[{"line": 5, "quote": "هل اطلعت على العرض"}, {"line": 6, "quote": "هل عندك استفسار عن العرض"}],
        q21={"promise_quote": "اشيك وارجع لك بالسعر", "promise_line": 2,
             "delivery_quote": "السعر ٦٠٠٠ ريال لشخصين", "delivery_line": 3},
        q25=[{"line": 1, "source_quote": "بكم", "answer_quote": "السعر ٦٠٠٠ ريال لشخصين", "answer_line": 3}]))
    assert g.qualified and g.red_flag is None
    assert g.followup_lines == [5, 6] and g.items["followup"].value == 1.0
    assert g.loss_reason == "No Response"
    assert g.items["promise"].value == 1.0 and g.items["answered"].value == 1.0
    assert "(أ)" in transcript.build(t).lines[1].speaker and "(ب)" in transcript.build(t).lines[5].speaker


def test_fixture_c_caption_plus_attachment_is_an_offer_and_a_bare_one_goes_to_review():
    t = thread(("customer", "10-01 09:00", "ارسل برنامج جورجيا لشخصين في نوفمبر"),
               ("agent", "10-01 09:01", "هذا البرنامج"), ("agent", "10-01 09:01", "[مرفق]"))
    booking = {"destination_quote": "جورجيا", "destination_line": 1, "date_quote": "في نوفمبر",
               "date_line": 1, "travellers_quote": "لشخصين", "travellers_line": 1}
    g = grade(t, answer(booking=booking, offer={"offer_quote": "[مرفق]", "offer_line": 3,
                                                "attachment_unclear": False}))
    assert g.red_flag is None and g.items["offer"].value == 1.0

    bare = thread(("customer", "10-01 09:00", "ارسل برنامج جورجيا لشخصين في نوفمبر"),
                  ("agent", "10-01 09:01", "[مرفق]"))
    g = grade(bare, answer(booking=booking, offer={"offer_quote": None, "attachment_unclear": True}))
    assert g.red_flag == "review_attachment"
    assert "offer" not in g.items           # review, not a definitive failure


def test_fixture_d_a_customer_reply_splits_attempts_and_earlier_ones_still_count():
    t = thread(("customer", "10-01 09:00", "جورجيا نوفمبر شخصين بكم"),
               ("agent", "10-01 09:01", "السعر ٦٠٠٠ ريال"),
               ("agent", "10-02 10:00", "هل اطلعت على العرض"),
               ("customer", "10-02 10:10", "هل يشمل الطيران"),
               ("agent", "10-02 10:11", "لا يشمل الطيران"),
               ("agent", "10-02 11:00", "هل يناسبك العرض"))
    out = answer(offer={"offer_quote": "السعر ٦٠٠٠ ريال", "offer_line": 2},
                 followup=[{"line": 3, "quote": "هل اطلعت على العرض"}, {"line": 6, "quote": "هل يناسبك العرض"}])
    # 49 minutes apart, but the customer wrote in between: two attempts.
    assert scorecard.followup_attempts(transcript.build(t), out) == [3, 6]


def test_two_follow_ups_close_together_with_no_reply_are_one_attempt():
    t = thread(("customer", "10-01 09:00", "جورجيا نوفمبر شخصين بكم"),
               ("agent", "10-01 09:01", "السعر ٦٠٠٠ ريال"),
               ("agent", "10-02 10:00", "هل اطلعت على العرض"),
               ("agent", "10-02 12:00", "في اي استفسار انا موجود"))
    out = answer(followup=[{"line": 3, "quote": "هل اطلعت على العرض"},
                           {"line": 4, "quote": "في اي استفسار انا موجود"}])
    assert scorecard.followup_attempts(transcript.build(t), out) == [3]


def _objection_thread(objection: str, *agent_lines: str) -> dict:
    rows = [("customer", "10-01 09:00", "جورجيا ٨ نوفمبر شخصين بكم"),
            ("agent", "10-01 09:05", "السعر ٦٠٠٠ ريال لشخصين"),
            ("customer", "10-01 09:10", objection)]
    rows += [("agent", f"10-01 09:{11 + i:02d}", x) for i, x in enumerate(agent_lines)]
    return thread(*rows)


def test_fixture_e_price_objection_with_reason_and_alternative_is_good():
    t = _objection_thread("غالي مرة", "فاهم حضرتك، السعر شامل الطيران والفندق 5 نجوم والانتقالات",
                          "ولو تبي أقل عندنا فندق 4 نجوم بـ ٤٨٠٠ ريال")
    g = grade(t, answer(booking=BOOKING_1, offer={"offer_quote": "السعر ٦٠٠٠ ريال لشخصين", "offer_line": 2},
                        q32=[{"line": 3, "source_quote": "غالي مرة", "objection_type": "price",
                              "acknowledge_quote": "فاهم حضرتك", "acknowledge_line": 4,
                              "reason_quote": "السعر شامل الطيران والفندق 5 نجوم والانتقالات", "reason_line": 4,
                              "alternative_quote": "عندنا فندق 4 نجوم بـ ٤٨٠٠ ريال", "alternative_line": 5}]))
    assert g.items["objection"].value == 1.0 and g.red_flag is None


def test_fixture_f_delay_objection_answered_only_by_a_discount_is_weak_not_ignored():
    t = _objection_thread("خلني اشاور وارجع لك", "تمام على راحتك", "لو حجزت اليوم أنزّل لك ٣٠٠ ريال")
    g = grade(t, answer(booking=BOOKING_1, offer={"offer_quote": "السعر ٦٠٠٠ ريال لشخصين", "offer_line": 2},
                        q32=[{"line": 3, "source_quote": "خلني اشاور وارجع لك", "objection_type": "delay",
                              "discount_quote": "لو حجزت اليوم أنزّل لك ٣٠٠ ريال", "discount_line": 5}]))
    assert g.items["objection"].value == 0.5
    assert g.red_flag is None                      # agents may give discounts (2026-10-11)


def test_an_objection_with_no_element_at_all_is_ignored():
    t = _objection_thread("غالي", "تمام")
    g = grade(t, answer(q32=[{"line": 3, "source_quote": "غالي", "objection_type": "price"}]))
    assert g.items["objection"].value == 0.0


# ---------------------------------------------------------------------------
# 5.4b promise, 5.5 loss reason, 5.2 waiting / transferred
# ---------------------------------------------------------------------------

PROMISE_ROWS = (("customer", "10-01 09:00", "ابغى عرض لجورجيا"),
                ("agent", "10-01 09:05", "خليني اشيك وارسلك"))


@pytest.mark.parametrize("later_rows, q21, expected", [
    # 5.4b rule 1: the promise waited on details the customer never sent
    ((), {"promise_condition": "customer_details"}, None),
    # 5.4b rule 2: the customer closed the request 20 minutes later
    ((("customer", "10-01 09:25", "تم الحجز"),), {"closed_quote": "تم الحجز", "closed_line": 3}, None),
    # closed after more than two hours: the agent had the chance, and missed it
    ((("customer", "10-01 12:30", "تم الحجز"),), {"closed_quote": "تم الحجز", "closed_line": 3}, 0.0),
])
def test_promise_is_not_applicable_only_when_the_agent_had_no_real_chance(later_rows, q21, expected):
    t = thread(*PROMISE_ROWS, *later_rows)
    g = grade(t, answer(q21={"promise_quote": "خليني اشيك وارسلك", "promise_line": 2, **q21}))
    assert (g.items["promise"].value if "promise" in g.items else None) == expected


def test_a_delivery_quoted_from_before_the_promise_delivers_nothing():
    t = thread(("agent", "10-01 08:59", "السعر ٥٠٠٠"), *PROMISE_ROWS)
    g = grade(t, answer(q21={"promise_quote": "خليني اشيك وارسلك", "promise_line": 3,
                             "delivery_quote": "السعر ٥٠٠٠", "delivery_line": 1}))
    assert g.items["promise"].value == 0.0


def test_no_response_is_split_by_who_wrote_last_and_how_often_the_agent_followed_up():
    customer_last = transcript.build(thread(("agent", "10-01 09:00", "السعر ٥٠٠٠"),
                                            ("customer", "10-01 09:05", "طيب")))
    agent_last = transcript.build(thread(("customer", "10-01 09:00", "كم"),
                                         ("agent", "10-01 09:05", "السعر ٥٠٠٠")))
    assert scorecard.split_silence(customer_last, "No Response", 0) == "No Response (agent)"
    assert scorecard.split_silence(agent_last, "No Response", 1) == "No follow-up"
    assert scorecard.split_silence(agent_last, "No Response", 2) == "No Response"
    assert scorecard.split_silence(agent_last, "Expensive Price", 0) == "Expensive Price"


def test_followup_item_applies_only_when_the_customer_went_silent():
    t = thread(("customer", "10-01 09:00", "كم السعر"), ("agent", "10-01 09:05", "السعر ٥٠٠٠"),
               ("customer", "10-01 09:10", "والله الصراحه عاليه"))
    g = grade(t, answer(q41={"reason": "Expensive Price", "reason_quote": "والله الصراحه عاليه",
                             "reason_line": 3}))
    assert g.loss_reason == "Expensive Price" and "followup" not in g.items
    assert g.loss_line == 3


def test_a_qualified_customer_who_was_asked_a_question_is_waiting_not_a_red_flag():
    t = thread(("customer", "10-01 09:00", "جورجيا ٨ نوفمبر شخصين"),
               ("agent", "10-01 09:05", "حضرتك تفضل فندق ٤ ولا ٥ نجوم؟"))
    g = grade(t, answer(booking=BOOKING_1))
    assert g.qualified and g.red_flag is None and "offer" not in g.items


def test_a_transferred_chat_is_not_a_red_flag():
    t = thread(("customer", "10-01 09:00", "جورجيا ٨ نوفمبر شخصين"),
               ("agent", "10-01 09:05", "زميلي هيتواصل مع حضرتك"))
    g = grade(t, answer(booking=BOOKING_1, q18={"answer_quote": "زميلي هيتواصل مع حضرتك", "answer_line": 2}))
    assert g.red_flag is None and "offer" not in g.items


# ---------------------------------------------------------------------------
# 5.1 verification, determinism of the combination, the score
# ---------------------------------------------------------------------------

def test_a_quote_counts_only_inside_the_message_and_side_it_names():
    chat = transcript.build(thread(("customer", "10-01 09:00", "كم السعر"),
                                   ("agent", "10-01 09:05", "السعر ٥٠٠٠ ريال"),
                                   ("bot", "10-01 09:06", "السعر ٥٠٠٠ ريال")))
    assert verified_line(chat, "السعر ٥٠٠٠", 2, "agent") == 2
    assert verified_line(chat, "السعر ٥٠٠٠", 1, "agent") is None       # right text, wrong message
    assert verified_line(chat, "السعر ٥٠٠٠", 2, "customer") is None    # wrong side
    assert verified_line(chat, "السعر ٥٠٠٠", 3, None) is None          # a bot line never counts
    assert verified_line(chat, "السعر 5000 ريال", "[2]", "agent") == 2  # digits and "[n]" normalise


def test_an_item_counts_only_if_it_applies_in_two_runs_and_takes_the_median():
    t = thread(("customer", "10-01 09:00", "كم السعر"), ("agent", "10-01 09:05", "السعر ٥٠٠٠"),
               ("customer", "10-01 09:10", "يشمل الفطور"))
    chat = transcript.build(t)
    asked = [{"line": 1, "source_quote": "كم السعر", "answer_quote": "السعر ٥٠٠٠", "answer_line": 2}]
    unanswered = asked + [{"line": 3, "source_quote": "يشمل الفطور"}]
    runs = [scorecard.assess_run(chat, answer(q25=q)) for q in (asked, unanswered, asked)]
    assert scorecard.combine(chat, runs).items["answered"].value == 1.0
    runs = [scorecard.assess_run(chat, answer(q25=q)) for q in (unanswered, [], [])]
    assert "answered" not in scorecard.combine(chat, runs).items


def test_an_opener_that_only_announces_a_question_is_not_a_request():
    t = thread(("customer", "10-01 09:00", "السلام عليكم ابغى استفسر"), ("agent", "10-01 09:05", "تفضل"))
    g = grade(t, answer(q25=[{"line": 1, "source_quote": "ابغى استفسر"}]))
    assert "answered" not in g.items


def test_score_is_weighted_over_the_items_that_applied_and_nothing_zeroes_a_chat():
    t = thread(("customer", "10-01 09:00", "كم السعر"), ("agent", "10-01 09:05", "اقرا فوق"))
    g = grade(t, answer(q13={"violation_quote": "اقرا فوق", "violation_line": 2},
                        q25=[{"line": 1, "source_quote": "كم السعر"}]))
    # probe 0 (10), answered 0 (15), correct 1 (10), professional 0 (15), followup 0 (15)
    assert g.items["professional"].value == 0.0
    assert g.score == round(100 * 10 / (10 + 15 + 10 + 15 + 15))
    assert sum(scorecard.WEIGHTS.values()) == 100


@pytest.mark.parametrize("votes, expected", [
    ([("Other", None), ("Other", 4), ("Booked", None)], ("Other", 4)),                # majority
    ([("Other", None), ("Booked", 2), ("Supplier", None)], ("Booked", 2)),           # first verified
    ([("Other", None), ("Booked", None)], (None, None)),                             # nothing to stand on
    ([("No Response", None), ("No Response", None), ("Other", 3)], ("Other", 3)),    # 2026-10-11 regression:
    ([("No Response", None), ("No Response", None)], ("No Response", None)),         #   a proven reason beats
    ([("Expensive Price", 5), ("Other", 3), ("Expensive Price", 5)], ("Expensive Price", 5)),  # silence
])
def test_loss_label_prefers_a_proven_reason_then_the_majority(votes, expected):
    assert scorecard.pick_loss_label(votes) == expected


# ---------------------------------------------------------------------------
# What the model is sent
# ---------------------------------------------------------------------------

def test_the_scoring_note_is_stripped_and_removed_questions_are_gone():
    prompt = engine.SYSTEM_PROMPT
    assert "NOTE FOR THE SCORING CODE" not in prompt and "# " not in prompt.splitlines()[-1]
    for gone in ('"q35"', '"q37"', '"q38"'):
        assert gone not in prompt
    for field in ("promise_condition", "condition_met_quote", "closed_quote"):
        assert field in prompt


def test_the_chat_is_numbered_masked_and_keeps_dates_and_prices():
    t = thread(("customer", "10-01 09:00", "انا عبدالله رقمي 0501234567 بإذن الله ٢٠٢٦-١٠-١١ بـ 12500"),
               ("agent", "10-01 09:05", "شوف https://example.com/x"),
               name="عبدالله الحربي")
    text = transcript.build(t).render()
    assert text.startswith("المحادثة:\n\n[1] 10-01 09:00 العميل: ")
    assert "[اسم العميل]" in text and "بإذن الله" in text
    assert "[رقم]" in text and "0501234567" not in text
    assert "٢٠٢٦-١٠-١١" in text and "12500" in text
    assert "[لينك]" in text
    assert text.endswith("CUSTOMER QUESTIONS: (none)\nOBJECTION LINES: (none)")


# ---------------------------------------------------------------------------
# The engine: calls, costs, and where the loss reason comes from
# ---------------------------------------------------------------------------

class FakeDeepSeek:
    model = "deepseek-v4-flash"

    def __init__(self, out: dict):
        self.out = out

    def complete_json(self, text, **kw):
        return self.out, {"model": "deepseek-flash", "prompt_tokens": 1000, "completion_tokens": 100}


class FakeClaude:
    model = claude.MODEL

    def __init__(self, label="Expensive Price", fail=False):
        self.label, self.fail = label, fail

    def complete_json(self, system, user):
        usage = {"model": claude.MODEL, "prompt_tokens": 9000, "completion_tokens": 500, "cost_usd": 0.01}
        if self.fail:
            raise claude.ClaudeStopped("Claude stopped with 'refusal'", usage)
        return {"q41": {"reason": self.label, "reason_quote": "غالي", "reason_line": 2}}, usage


ENGINE_THREAD = thread(("customer", "10-01 09:00", "كم السعر"), ("customer", "10-01 09:01", "غالي"),
                       ("agent", "10-01 09:05", "السعر ٥٠٠٠"))
SONNET_ON = engine.Settings(loss_model="sonnet", sonnet_votes=1, sonnet_gate_open=True)


def test_engine_three_deepseek_runs_each_its_own_model_calls_row():
    r = engine.evaluate(ENGINE_THREAD, engine.Settings(), deepseek=FakeDeepSeek(answer()))
    assert len(r["calls"]) == 3
    assert len({c["input_hash"] for c in r["calls"]}) == 3
    assert {c["provider"] for c in r["calls"]} == {"deepseek"} and r["loss_reason_source"] == "deepseek"
    assert r["prompt_version"] == "qa-chat-v4.1" and r["runs"] == 3


def test_engine_takes_the_loss_reason_from_sonnet_and_books_it_under_anthropic():
    r = engine.evaluate(ENGINE_THREAD, SONNET_ON, deepseek=FakeDeepSeek(answer()), claude_factory=FakeClaude)
    assert r["loss_reason"] == "Expensive Price" and r["loss_reason_source"] == "sonnet"
    sonnet = [c for c in r["calls"] if c["provider"] == "anthropic"]
    assert len(sonnet) == 1 and sonnet[0]["cost_usd"] == 0.01 and sonnet[0]["priced_at_peak"] is False
    assert r["detail"]["loss_evidence"]["line"] == 2


def test_engine_does_not_ask_sonnet_about_a_booked_chat():
    r = engine.evaluate(ENGINE_THREAD, SONNET_ON, deepseek=FakeDeepSeek(answer(q41={"reason": "Booked"})),
                        claude_factory=FakeClaude)
    assert r["loss_reason"] == "Booked"
    assert not [c for c in r["calls"] if c["provider"] == "anthropic"], "a booked chat costs no Sonnet call"


def test_a_refused_sonnet_vote_is_paid_for_and_the_deepseek_runs_vote_instead():
    r = engine.evaluate(ENGINE_THREAD, SONNET_ON, deepseek=FakeDeepSeek(answer(q41={"reason": "Other"})),
                        claude_factory=lambda: FakeClaude(fail=True))
    failed = [c for c in r["calls"] if c["provider"] == "anthropic"]
    assert len(failed) == 1 and failed[0]["succeeded"] is False and failed[0]["cost_usd"] == 0.01
    assert r["loss_reason"] == "Other" and r["loss_reason_source"] == "deepseek"
    assert "DeepSeek" in r["detail"]["loss_note"]


def test_sonnet_settings_need_the_switch_the_gate_and_a_vote():
    assert not engine.Settings().use_sonnet
    assert not engine.Settings("sonnet", 1, False).use_sonnet
    assert not engine.Settings("sonnet", 0, True).use_sonnet
    assert SONNET_ON.use_sonnet


def test_claude_answer_parsing_and_pricing():
    assert claude.first_json_object('note {"a": 1} {"b": 2}') == {"a": 1}
    with pytest.raises(claude.JudgeError):
        claude.first_json_object("no json here")
    usage = {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000, "output_tokens": 100_000}
    assert claude.cost_usd(usage, claude.MODEL) == 2.0 + 0.2 + 1.0


# ---------------------------------------------------------------------------
# What the dashboard shows
# ---------------------------------------------------------------------------

def test_meta_names_every_item_and_every_reason_the_code_can_produce():
    m = texts.meta()
    assert [i["key"] for i in m["items"]] == list(scorecard.WEIGHTS)
    produced = set(scorecard.LOSS_REASONS) | {scorecard.NO_FOLLOWUP, scorecard.NO_RESPONSE_AGENT}
    assert produced == set(m["loss_reasons"])


def test_the_chat_fragment_escapes_quotes_and_shows_the_red_flag():
    row = {"score": 40, "chat_type": "sales", "qualified": True, "red_flag": "no_offer",
           "loss_reason": "No follow-up", "loss_reason_source": "deepseek", "followup_attempts": 0,
           "items": {"followup": {"value": 0.0, "lines": []}, "answered": {"value": 0.0, "lines": [1]}},
           "detail": {"evidence": {"1": {"line": 1, "by": "customer", "text": "<script>x</script>"}},
                      "booking": {}}}
    html = render.chat_html(row)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert texts.RED_FLAGS["no_offer"] in html and "40%" in html


def test_needs_action_lists_only_broken_promises(monkeypatch):
    """The to-do list is the promises the agent did NOT keep; kept ones stay off it."""
    from app import dashboard, db
    promises = [{"interaction_id": "a", "kept": True}, {"interaction_id": "b", "kept": False}]
    monkeypatch.setattr(db, "rows", lambda sql, p=None: promises if sql == dashboard.SQL_PROMISES else [])
    monkeypatch.setattr(db, "one", lambda sql, p=None: {})
    ov = dashboard.overview("2026-10-01", "2026-10-12")
    assert [x["interaction_id"] for x in ov["broken_promises"]] == ["b"]
    assert dashboard.promises("2026-10-01", "2026-10-12")["totals"] == {"promises": 2, "kept": 1, "broken": 1}
