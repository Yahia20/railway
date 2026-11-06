"""The v6 chat scorecard (app/qa/v6/scorecard.py), approved for production 2026-11-05.

Every test is a mistake the 2026-11-01 review of the v5.2 trial found, or an owner decision of 2026-11-03/04,
reproduced with a small written chat through the real engine. These are the trial's 45 tests, moved unchanged;
the ones at the end cover what changed on the way into the worker. No model call, no database, no credentials.
"""
import datetime as dt

from app.qa.v6 import scorecard as qs

OBS = dt.datetime(2026, 11, 3, 12, 0)
D = dt.datetime


def msg(role, day, clock, text, sender=None):
    return {"role": role, "date": f"10-{day:02d}", "time": clock, "text": text, "reply_to": None, "sender": sender or ("a1" if role == "agent" else None)}


def chat(*messages, **extra):
    return {"id": "T", "employee": "Agent", "date": "2026-10-20", "messages": list(messages), "attribution": "single", **extra}


def run(c, out=None, alerts=None, votes=3):
    base = {"chat_type": {"type": "sales"}, "scope": {"scope": "in"}, "booking": {}, "probes": [], "offer": {}, "followup": [],
            "q18": {}, "promises": [], "requests": [], "objections": [], "closure": {}}
    base.update(out or {})
    return qs.score_chat(c, [base] * votes, alerts or {"loss_reason": {"reason": "No Response"}, "alerts": []}, OBS, 2026)


def offer_chat(after=()):
    return chat(msg("customer", 20, "11:00", "ابي رحلة تركيا"),
                msg("agent", 20, "11:05", "السعر 3000 ريال"),
                *after)


OFFER = {"offer": {"offer_quote": "السعر 3000 ريال", "offer_line": 2}}


# ---- working minutes
def test_the_hour_after_midnight_counts_when_the_wait_starts_in_it():
    assert qs.work_minutes(D(2026, 10, 21, 0, 48), D(2026, 10, 21, 10, 26)) == 38
    assert qs.work_minutes(D(2026, 10, 21, 0, 10), D(2026, 10, 21, 0, 50)) == 40


def test_a_wait_across_midnight_is_not_counted_twice():
    assert qs.work_minutes(D(2026, 10, 20, 23, 0), D(2026, 10, 21, 0, 30)) == 90
    assert qs.work_minutes(D(2026, 10, 20, 23, 0), D(2026, 10, 21, 10, 30)) == 150


def test_friday_is_a_working_day():
    friday = D(2026, 10, 23, 11, 0)
    assert friday.weekday() == 4
    assert qs.work_minutes(friday, friday + dt.timedelta(minutes=30)) == 30


# ---- follow-up: the owner's decisions
def test_three_follow_ups_earn_full_points_and_fewer_earn_their_share():
    after = [msg("agent", 21, "11:00", "هل اطلعت؟"), msg("agent", 21, "16:00", "موجود؟"), msg("agent", 22, "11:00", "مهتم؟")]
    fu = [{"line": 3 + i, "quote": t} for i, t in enumerate(["هل اطلعت؟", "موجود؟", "مهتم؟"])]
    assert run(offer_chat(after), {**OFFER, "followup": fu})["checks"]["followup"]["status"] == "yes"
    one = run(offer_chat(after[:1]), {**OFFER, "followup": fu[:1]})["checks"]["followup"]
    assert one["status"] == "partial" and abs(one["v"] - 1 / 3) < 0.01


def test_no_follow_up_after_24_hours_scores_zero():
    c = chat(msg("customer", 20, "11:00", "ابي رحلة تركيا"), msg("agent", 20, "11:05", "السعر 3000 ريال"))
    assert run(c, OFFER)["checks"]["followup"]["status"] == "no"


def test_under_24_hours_is_pending():
    c = chat(msg("customer", 20, "11:00", "ابي رحلة تركيا"), msg("agent", 20, "11:05", "السعر 3000 ريال"))
    r = qs.score_chat(c, [{"chat_type": {"type": "sales"}, "offer": OFFER["offer"]}] * 3, {"loss_reason": {"reason": "No Response"}, "alerts": []}, D(2026, 10, 21, 0, 0), 2026)
    assert r["checks"]["followup"]["status"] == "pending"


def test_silence_starts_after_the_conversation_stopped_not_at_the_offer():
    c = offer_chat([msg("customer", 22, "11:00", "طيب كم الطيران"), msg("agent", 22, "11:10", "الطيران 900")])
    assert run(c, OFFER)["checks"]["followup"]["d"]["start"] == 4


def test_customer_who_closed_owes_no_follow_up():
    c = offer_chat([msg("customer", 20, "12:00", "شكرا لك خلاص")])
    r = run(c, {**OFFER, "closure": {"closed_quote": "شكرا لك خلاص", "closed_line": 3}})
    assert r["checks"]["followup"]["status"] == "na" and r["checks"]["followup"]["d"]["why"] == "customer_closed"


def test_thanks_alone_does_not_close():
    c = offer_chat([msg("customer", 20, "12:00", "شكرا لك")])
    r = run(c, {**OFFER, "closure": {"closed_quote": "شكرا لك", "closed_line": 3}})
    assert r["checks"]["followup"]["d"].get("why") != "customer_closed"


def test_a_customer_who_went_on_asking_did_not_close():
    c = offer_chat([msg("customer", 20, "12:00", "خلاص"), msg("customer", 20, "12:05", "كم سعر الفندق")])
    r = run(c, {**OFFER, "closure": {"closed_quote": "خلاص", "closed_line": 3}})
    assert r["checks"]["followup"]["d"].get("why") != "customer_closed"


def test_coming_tomorrow_waits_for_tomorrow_then_the_agent_owes_a_chase():
    c = offer_chat([msg("customer", 20, "12:00", "بكره اجيكم باذن الله")])
    out = {"chat_type": {"type": "sales"}, "offer": OFFER["offer"], "closure": {"next_step_quote": "بكره اجيكم باذن الله", "next_step_line": 3}}
    early = qs.score_chat(c, [out] * 3, {"loss_reason": {"reason": "No Response"}, "alerts": []}, D(2026, 10, 21, 12, 0), 2026)
    assert early["checks"]["followup"]["status"] == "pending"
    late = qs.score_chat(c, [out] * 3, {"loss_reason": {"reason": "No Response"}, "alerts": []}, OBS, 2026)
    assert late["checks"]["followup"]["status"] == "no"


def test_transfer_nobody_picked_up_goes_to_review():
    c = chat(msg("customer", 20, "11:00", "ابي رحلة"), msg("agent", 20, "11:05", "سوف يتم التواصل معك من القسم المختص"))
    r = run(c, {"q18": {"answer_quote": "سوف يتم التواصل معك من القسم المختص", "answer_line": 2}})
    assert r["checks"]["followup"]["status"] == "review" and r["loss_reason"] != "No follow-up"


def test_transfer_the_other_department_answered_is_accepted():
    c = chat(msg("customer", 20, "11:00", "ابي تأشيرة"), msg("agent", 20, "11:05", "سوف يتم التواصل معك من القسم المختص"),
             msg("agent", 20, "11:30", "معك قسم التأشيرات", sender="a2"))
    r = run(c, {"q18": {"answer_quote": "سوف يتم التواصل معك من القسم المختص", "answer_line": 2}})
    assert r["checks"]["followup"]["d"]["why"] == "transfer_accepted"


def test_transfer_is_not_an_answer_and_not_a_promise():
    c = chat(msg("customer", 20, "11:00", "كم سعر التأشيرة"), msg("agent", 20, "11:05", "تم ارسال رقم حضرتك للمسئول"))
    r = run(c, {"q18": {"answer_quote": "تم ارسال رقم حضرتك للمسئول", "answer_line": 2},
                "requests": [{"line": 1, "source_quote": "كم سعر التأشيرة"}],
                "promises": [{"line": 2, "promise_quote": "تم ارسال رقم حضرتك للمسئول"}]})
    assert r["checks"]["answered"]["status"] == "review"
    assert "promise" not in r["checks"]


def test_phone_mention_sends_a_missing_follow_up_to_review():
    c = offer_chat([msg("agent", 20, "11:06", "اتصلت بيك وما رديت")])
    assert run(c, OFFER)["checks"]["followup"]["status"] == "review"


def test_bitrix_call_after_the_silence_sends_it_to_review():
    c = offer_chat()
    c["calls"] = ["2026-10-21T12:00"]
    assert run(c, OFFER)["checks"]["followup"]["status"] == "review"


def test_a_won_deal_still_owes_follow_up():
    # owner decision 2026-11-04: follow-up is owed on won and lost deals alike
    c = offer_chat()
    c["outcome"] = "won"
    assert run(c, OFFER)["checks"]["followup"]["status"] == "no"


# ---- not a real ask
def test_job_seeker_is_not_counted_and_gets_a_title():
    c = chat(msg("customer", 20, "11:00", "رسلت لكم cv"), msg("agent", 20, "11:05", "تمام"))
    r = run(c, {"chat_type": {"type": "other", "other_kind": "job_seeker"}})
    assert r["score"] is None and r["total"] == 0 and r["checks"] == {} and r["title"] == "بيدور على شغل"


def test_supplier_is_not_counted():
    c = chat(msg("customer", 20, "11:00", "نقدم خدمات فنادق"), msg("agent", 20, "11:05", "شكرا"))
    r = run(c, {"chat_type": {"type": "other", "other_kind": "supplier"}})
    assert r["score"] is None and r["title"] == "مورّد بيعرض خدمة"


# ---- probe
BOOKING = {"destination_quote": "موريشيوس", "destination_line": 3, "date_quote": "18 نوفمبر", "date_line": 3, "travellers_quote": "2 عرسان", "travellers_line": 3}


def test_details_volunteered_after_a_two_line_greeting_are_not_a_miss():
    c = chat(msg("agent", 20, "11:00", "السلام عليكم"), msg("agent", 20, "11:00", "معك فلان"),
             msg("customer", 20, "11:01", "موريشيوس 18 نوفمبر 2 عرسان"), msg("agent", 20, "11:05", "تمام"))
    assert run(c, {"booking": BOOKING})["checks"]["probe"]["status"] == "yes"


def test_a_question_after_the_answer_earns_nothing_and_counts_as_a_re_ask():
    c = chat(msg("customer", 20, "11:00", "السلام"), msg("agent", 20, "11:01", "هلا"), msg("agent", 20, "11:02", "اهلا"),
             msg("customer", 20, "11:02", "تمام"), msg("agent", 20, "11:03", "كيف"),
             msg("customer", 20, "11:04", "4 بالغين"), msg("agent", 20, "11:05", "4 بالغين ؟"))
    r = run(c, {"booking": {"travellers_quote": "4 بالغين", "travellers_line": 6},
                "probes": [{"line": 7, "quote": "4 بالغين ؟", "asks_for": "travellers", "refines": False}]})
    assert r["checks"]["probe"]["d"]["satisfied"] == [] and r["checks"]["noask"]["status"] == "no"


def test_a_changed_quote_is_review_not_never_given():
    c = chat(msg("customer", 20, "11:00", "تفاصيل عن باقة: malysia"), msg("agent", 20, "11:05", "تمام"))
    r = run(c, {"booking": {"destination_quote": "ماليزيا", "destination_line": 1}})
    assert r["checks"]["probe"]["status"] == "review"


# ---- offer
FULL = {"destination_quote": "سويسرا", "destination_line": 1, "date_quote": "٢٧ اكتوبر", "date_line": 1, "travellers_quote": "٢ بالغين", "travellers_line": 1}


def test_question_without_a_question_mark_suspends_the_offer():
    c = chat(msg("customer", 20, "11:00", "سويسرا ٢٧ اكتوبر ٢ بالغين"), msg("agent", 20, "11:05", "كم عمر الاطفال"))
    assert run(c, {"booking": FULL})["checks"]["offer"]["d"]["why"] == "waiting"


def test_asked_back_line_suspends_the_offer_even_with_later_greetings():
    c = chat(msg("customer", 20, "11:00", "سويسرا ٢٧ اكتوبر ٢ بالغين"), msg("agent", 20, "11:05", "في مدن محدده تريديها"), msg("agent", 21, "11:05", "السلام عليكم"))
    r = run(c, {"booking": FULL, "requests": [{"line": 1, "source_quote": "سويسرا ٢٧ اكتوبر ٢ بالغين", "asked_back_quote": "في مدن محدده تريديها", "asked_back_line": 2}]})
    assert r["checks"]["offer"]["d"]["why"] == "waiting"


def test_a_changed_offer_quote_is_review_not_a_fail():
    c = chat(msg("customer", 20, "11:00", "سويسرا ٢٧ اكتوبر ٢ بالغين"), msg("agent", 20, "11:05", "اتفض البرنامج"))
    r = run(c, {"booking": FULL, "offer": {"offer_quote": "اتفضل البرنامج", "offer_line": 2}})
    assert r["checks"]["offer"]["status"] == "review"


def test_file_after_the_request_is_review_not_a_fail():
    c = chat(msg("customer", 20, "11:00", "سويسرا ٢٧ اكتوبر ٢ بالغين"), msg("agent", 20, "11:05", "[مرفق]"))
    r = run(c, {"booking": FULL})
    assert r["checks"]["offer"]["status"] == "review" and r["checks"]["offer"]["d"]["why"] == "attachment"


def test_not_available_goes_to_review():
    # owner decision 2026-11-04: the agent's "not available" neither exempts nor deducts
    c = chat(msg("customer", 20, "11:00", "سويسرا ٢٧ اكتوبر ٢ بالغين"), msg("agent", 20, "11:05", "غير متاح"))
    r = run(c, {"booking": FULL}, {"loss_reason": {"reason": "Product not Available", "reason_quote": "غير متاح", "reason_line": 2}, "alerts": []})
    assert r["checks"]["offer"]["status"] == "review" and r["checks"]["offer"]["d"]["why"] == "exemption_unverified"


def test_out_of_scope_goes_to_review():
    c = chat(msg("customer", 20, "11:00", "فيزا هندي سويسرا ٢٧ اكتوبر ٢ بالغين"), msg("agent", 20, "11:05", "تمام نقدم"))
    b = {k: "فيزا هندي" if k.endswith("quote") else 1 for k in FULL}
    r = run(c, {"booking": b, "scope": {"scope": "out", "scope_quote": "فيزا هندي", "scope_line": 1}})
    assert r["checks"]["followup"]["status"] == "review" and r["checks"]["offer"]["status"] == "review"


# ---- violations never deduct automatically
def test_all_votes_on_a_violation_go_to_review_not_zero():
    c = chat(msg("customer", 20, "11:00", "كم السعر"), msg("agent", 20, "11:05", "شوف بنفسك"))
    r = run(c, {"q13": {"violation_quote": "شوف بنفسك", "violation_line": 2}})
    assert r["checks"]["polite"]["status"] == "review" and r["checks"]["polite"]["v"] is None


def test_wrong_destination_goes_to_review_and_stops_helping_the_score():
    c = chat(msg("customer", 20, "11:00", "Madina to Sialkot"), msg("agent", 20, "11:05", "[مرفق]"), msg("customer", 21, "11:00", "you send me Madrid"))
    out = {"offer": {"offer_quote": "[مرفق]", "offer_line": 2}}
    r = run(c, out, {"loss_reason": {"reason": "Booked online", "reason_quote": "you send me Madrid", "reason_line": 3},
                     "alerts": [{"kind": "wrong_customer", "quote_a": "[مرفق]", "line_a": 2}]})
    assert r["checks"]["match"]["status"] == "review"


def test_calc_error_alert_sends_correct_to_review():
    c = chat(msg("customer", 20, "11:00", "كم يوم"), msg("agent", 20, "11:05", "من 10 إلى 21 (11 يوم)"))
    r = run(c, {}, {"loss_reason": {"reason": "No Response"}, "alerts": [{"kind": "calc_error", "quote_a": "من 10 إلى 21 (11 يوم)", "line_a": 2, "quote_b": "من 10 إلى 21 (11 يوم)", "line_b": 2}]})
    assert r["checks"]["correct"]["status"] == "review"


def test_votes_that_split_go_to_review():
    c = chat(msg("customer", 20, "11:00", "سويسرا ٢٧ اكتوبر ٢ بالغين"), msg("agent", 20, "11:05", "السعر 3000"), msg("agent", 20, "11:06", "تمام"))
    yes = {"chat_type": {"type": "sales"}, "booking": FULL, "offer": {"offer_quote": "السعر 3000", "offer_line": 2}}
    no = {"chat_type": {"type": "sales"}, "booking": FULL, "offer": {}}
    r = qs.score_chat(c, [yes, no, no], {"loss_reason": {"reason": "Other"}, "alerts": []}, OBS, 2026)
    assert r["checks"]["offer"]["status"] == "review" and r["checks"]["offer"]["d"]["why"] == "votes_disagree"


# ---- requests and objections
def test_impatience_is_not_a_request():
    c = chat(msg("customer", 20, "11:00", "متى راح تتواصلون معي"), msg("agent", 20, "11:05", "هلا"))
    r = run(c, {"requests": [{"line": 1, "source_quote": "متى راح تتواصلون معي"}]},
            {"loss_reason": {"reason": "No Response"}, "alerts": [{"kind": "impatience", "quote_a": "متى راح تتواصلون معي", "line_a": 1}]})
    assert "answered" not in r["checks"]


def test_a_file_after_a_question_is_review_not_unanswered():
    c = chat(msg("customer", 20, "11:00", "ابي البرنامج"), msg("agent", 20, "11:05", "[مرفق]"))
    r = run(c, {"requests": [{"line": 1, "source_quote": "ابي البرنامج"}]})
    assert r["checks"]["answered"]["status"] == "review"


def test_a_question_the_bot_answered_is_not_charged_to_the_agent():
    c = chat(msg("customer", 20, "11:00", "من وين الاقلاع"), msg("bot", 20, "11:00", "من الرياض"), msg("agent", 20, "11:05", "هلا"))
    r = run(c, {"requests": [{"line": 1, "source_quote": "من وين الاقلاع", "bot_answer_quote": "من الرياض", "bot_answer_line": 2}]})
    assert "answered" not in r["checks"]


def test_price_objection_before_any_offer_is_not_an_objection():
    c = chat(msg("customer", 20, "11:00", "ابي ارخص رحله"), msg("agent", 20, "11:05", "هلا"))
    r = run(c, {"objections": [{"line": 1, "source_quote": "ابي ارخص رحله", "objection_type": "price"}]})
    assert "objection" not in r["checks"]


# ---- speed
def test_a_reply_to_the_agents_question_is_timed():
    c = chat(msg("customer", 20, "11:00", "ابي رحله"), msg("agent", 20, "11:01", "من اي مطار ؟"), msg("customer", 20, "15:15", "البحرين"), msg("agent", 20, "17:33", "تمام"))
    assert run(c)["checks"]["speed_next"]["d"]["minutes"] == 138


def test_a_bare_greeting_does_not_end_the_wait():
    c = chat(msg("customer", 20, "11:00", "ابي رحله"), msg("agent", 20, "11:01", "تبي زيادة حقيبة ؟"), msg("customer", 20, "12:00", "نعم"),
             msg("agent", 20, "12:05", "السلام عليكم"), msg("agent", 20, "15:00", "السعر 900"))
    assert run(c)["checks"]["speed_next"]["d"]["minutes"] == 180


def test_queue_time_is_not_charged_to_the_agent():
    c = chat(msg("customer", 20, "11:00", "ابي رحله"), msg("agent", 20, "12:10", "هلا"))
    c["assigned_at"] = "2026-10-20T12:00"
    d = run(c)["checks"]["speed_first"]["d"]
    assert d["minutes"] == 10 and d["queue_minutes"] == 60


# ---- attribution
def test_shared_chat_does_not_count_for_an_agent():
    c = chat(msg("customer", 20, "11:00", "ابي رحله"), msg("agent", 20, "11:05", "هلا"), attribution="shared")
    assert run(c)["attribution"] == "shared"


# ---- found by reading the first real run
def test_customer_who_sent_only_emojis_and_files_is_not_a_sales_request():
    c = chat(msg("customer", 20, "11:00", "👍🙏"), msg("customer", 20, "11:01", "[مرفق]"), msg("agent", 20, "11:05", "How can i help you ?"))
    assert run(c)["score"] is None


def test_asking_for_a_city_inside_the_country_given_is_not_a_re_ask():
    c = chat(msg("customer", 20, "11:00", "سويسرا"), msg("agent", 20, "11:05", "في مدن محدده تريديها"))
    r = run(c, {"booking": {"destination_quote": "سويسرا", "destination_line": 1},
                "probes": [{"line": 2, "quote": "في مدن محدده تريديها", "asks_for": "destination", "refines": False}]})
    assert r["checks"]["noask"]["status"] == "yes"


def test_requests_wait_while_the_customer_has_not_answered_the_agents_question():
    c = chat(msg("customer", 20, "11:00", "please share the ticket price"), msg("agent", 20, "11:05", "do need visa for Bahrein ?"))
    r = run(c, {"requests": [{"line": 1, "source_quote": "please share the ticket price"}]})
    assert r["checks"]["answered"]["status"] == "na" and r["checks"]["followup"]["status"] == "no"


def test_a_detail_is_not_a_request():
    c = chat(msg("customer", 20, "11:00", "موريشيوس"), msg("customer", 20, "11:00", "من جدة"), msg("agent", 20, "11:05", "تمام"))
    r = run(c, {"booking": {"destination_quote": "موريشيوس", "destination_line": 1},
                "requests": [{"line": 1, "source_quote": "موريشيوس"}, {"line": 2, "source_quote": "من جدة"}]})
    assert "answered" not in r["checks"]


def test_thanks_or_a_sticker_does_not_erase_earlier_follow_ups():
    fu = [("agent", 21, "11:00", "هل اطلعت؟"), ("agent", 22, "11:00", "موجود؟"), ("agent", 23, "11:00", "مهتم؟")]
    after = [msg(*x) for x in fu] + [msg("customer", 23, "11:05", "شكرا لك"), msg("agent", 23, "11:10", "العفو"), msg("customer", 23, "11:12", "[ملصق]")]
    out = {**OFFER, "followup": [{"line": 3 + i, "quote": x[3]} for i, x in enumerate(fu)]}
    assert run(offer_chat(after), out)["checks"]["followup"]["status"] == "yes"


# ---- moved into the worker
def test_no_staff_names_in_the_public_engine():
    # rule 7: who is left out of review lives in agents.qa_excluded, set from the git-ignored roster
    assert qs.EXCLUDED_AGENTS == () and not qs.excluded("anyone")


def test_full_timestamps_keep_a_chat_in_order_across_new_year():
    c = chat({"role": "customer", "date": "12-31", "time": "23:50", "at": "2026-12-31T23:50", "text": "ابي رحلة", "reply_to": None, "sender": None},
             {"role": "agent", "date": "01-01", "time": "00:05", "at": "2027-01-01T00:05", "text": "هلا", "reply_to": None, "sender": "a1"})
    st = qs.stamps(c, 2026)
    assert st[1] - st[0] == dt.timedelta(minutes=15)


def test_old_chats_without_full_timestamps_still_read_the_short_date():
    c = chat(msg("customer", 20, "11:00", "ابي رحلة"), msg("agent", 20, "11:05", "هلا"))
    assert qs.stamps(c, 2026) == [D(2026, 10, 20, 11, 0), D(2026, 10, 20, 11, 5)]
