# -*- coding: utf-8 -*-
"""Release-1 ASR text cleaning. Every case here is a failure mode a real
transcript actually produced (2026-08-09 batch, 10-call validation trial);
the text is synthetic but the shapes are not."""
import pytest

from app.asr.text_quality import (
    GAP,
    HARMLESS_CONTROL_TOKENS,
    LOST_AUDIO_CONTROL_TOKENS,
    ChunkQuality,
    assess_call,
    clean_chunk,
    reconstruct,
)


def _roundtrip(raw: str) -> ChunkQuality:
    cq = clean_chunk(raw)
    assert reconstruct(cq.clean_text, cq.ops) == raw
    return cq


# --- Tier-1 removal --------------------------------------------------------

def test_qusay_watermark_removed():
    raw = "تمام يا فندم التفريغ والتدقيق قصي البياتي طيب نكمل الحجز"
    cq = _roundtrip(raw)
    assert "قصي البياتي" not in cq.clean_text
    assert "نكمل الحجز" in cq.clean_text
    assert GAP in cq.clean_text
    assert len(cq.ops) == 1


def test_qusay_with_attached_fake_names_removed_as_one():
    raw = ("خذ راحتك التفريغ والتدقيق قصي البياتي ..عبد الناصر عاشور "
           "..جواد الخفاجي ممكن تقول لي تاريخ السفر؟")
    cq = _roundtrip(raw)
    assert "عاشور" not in cq.clean_text
    assert "تاريخ السفر" in cq.clean_text


def test_eddirasa_with_label_removed():
    raw = "شكرا لك مع السلامة موقع الدراسة الجزائري : www.eddirasa.com"
    cq = _roundtrip(raw)
    assert "eddirasa" not in cq.clean_text
    assert "الدراسة الجزائري" not in cq.clean_text
    assert "مع السلامة" in cq.clean_text


def test_atsign_blank_removed_but_bare_word_kept():
    raw = "لحظة واحدة @@@فراغ في فراغ في الجدول يوم الخميس"
    cq = _roundtrip(raw)
    assert "@@@" not in cq.clean_text
    # the genuine word فراغ (a real gap in a schedule) must survive
    assert "في فراغ في الجدول" in cq.clean_text


# --- Model control tokens ("<hesitation>") ---------------------------------

def test_control_token_removed_without_leaving_a_gap():
    raw = "ايه الصوت <hesitation> واضح يا فندم"
    cq = _roundtrip(raw)
    assert "<hesitation>" not in cq.clean_text
    # No marker and no doubled space: the utterance is continuous speech, and
    # a GAP here would stop the judge quoting across it.
    assert cq.clean_text == "ايه الصوت واضح يا فندم"
    assert GAP not in cq.clean_text
    assert cq.control_tokens == 1


def test_harmless_token_ledger_reconstructs_and_leaves_no_marker():
    raw = "طيب <hesitation> نكمل <hesitation> الحجز"
    cq = _roundtrip(raw)          # _roundtrip asserts the char-for-char rebuild
    assert cq.clean_text == "طيب نكمل الحجز"
    assert cq.control_tokens == 2
    assert [op["pattern_id"] for op in cq.ops] == ["control_token_v1"] * 2
    assert all(op["replacement_text"] == "" for op in cq.ops)


# --- F2: lost-audio markers are NOT harmless punctuation -------------------
# Sol's finding: q1 deleted <inaudible>/<noise>/<silence> with no marker, so a
# judge could quote straight across audio nobody heard. They are missing audio
# and get the same GAP as any other Tier-1 removal.

@pytest.mark.parametrize("tok", sorted(LOST_AUDIO_CONTROL_TOKENS))
def test_every_lost_audio_token_becomes_a_gap(tok):
    raw = f"العميل قال السعر <{tok}> والموظف رد عليه"
    cq = _roundtrip(raw)
    assert f"<{tok}>" not in cq.clean_text
    assert cq.clean_text == f"العميل قال السعر {GAP} والموظف رد عليه"
    assert [op["pattern_id"] for op in cq.ops] == ["control_token_gap_v1"]
    assert cq.control_gaps == 1
    assert cq.control_tokens == 0      # not the harmless class


def test_lost_audio_token_is_a_hard_quote_boundary_for_the_judge():
    from app.evaluate.scoring import validate_evidence

    cq = clean_chunk("العميل سأل عن السعر <inaudible> الموظف قال مع السلامة")
    crossing = {"evidence": [{"quote": "عن السعر الموظف قال"}]}
    assert validate_evidence(crossing, cq.clean_text)
    assert validate_evidence({"evidence": [{"quote": "سأل عن السعر"}]},
                             cq.clean_text) == []


def test_lost_audio_chars_are_tier1_but_never_invented_seconds():
    """They ARE missing audio, so they bill characters. They do NOT bill
    seconds: no token timestamps exist and a marker's length says nothing
    about the audio behind it."""
    raw = "تمام <inaudible_speech> يا فندم نكمل الحجز بكرة ان شاء الله"
    cq = _roundtrip(raw)
    q = assess_call([cq], [100.0], set(), 100.0,
                    clean_chars=800, raw_chars=len(raw), chunks_empty=0)
    assert q["tier1_chars_removed"] == len("<inaudible_speech>")
    assert q["control_token_gaps"] == 1
    assert q["invalid_seconds"] == 0.0        # no fabricated audio accounting
    assert q["status"] == "amber"             # normal any-removal trigger


def test_unknown_control_token_is_left_alone_and_flagged():
    """Deleting a marker we cannot classify is the failure this rule exists to
    prevent, so the text is untouched and a flag carries it to a human."""
    raw = "تمام <foo_bar> يا فندم <foo_bar> نكمل"
    cq = _roundtrip(raw)
    assert cq.clean_text == raw               # verbatim
    assert not cq.ops
    assert cq.unknown_control_tokens == 2
    assert cq.flags == [{"flag": "unknown_control_token",
                         "token": "<foo_bar>", "count": 2}]
    q = assess_call([cq], [100.0], set(), 100.0,
                    clean_chars=800, raw_chars=len(raw), chunks_empty=0)
    assert q["unknown_control_tokens"] == 2
    assert q["tier1_chars_removed"] == 0
    assert q["status"] == "amber"             # existing any-flag path, no new one


def test_allowlist_is_only_what_was_observed():
    """The model card documents no control-token vocabulary, so nothing joins
    the allowlist on the strength of looking like filler."""
    assert HARMLESS_CONTROL_TOKENS == frozenset({"hesitation"})
    assert not (HARMLESS_CONTROL_TOKENS & LOST_AUDIO_CONTROL_TOKENS)


# --- F3: harmless control chars stay out of every status calculation -------

def test_merged_with_contamination_bills_only_the_contamination():
    raw = "تمام <hesitation> التفريغ والتدقيق قصي البياتي طيب نكمل الحجز"
    cq = _roundtrip(raw)
    assert len(cq.ops) == 1                   # one merged span, one GAP
    op = cq.ops[0]
    assert "+" in op["pattern_id"]
    assert op["control_chars"] == len("<hesitation> ")
    q = assess_call([cq], [90.0], set(), 90.0,
                    clean_chars=800, raw_chars=len(raw), chunks_empty=0)
    # exactly the watermark's characters, not the token's
    assert q["tier1_chars_removed"] == (
        len(op["removed_text"]) - len("<hesitation> "))
    assert q["control_tokens_removed"] == 1


def test_harmless_chars_excluded_from_density():
    """20 <hesitation>s inflate raw length by 260 chars. On a short call that
    is enough to cross the >22 chars/sec density trigger; it must not."""
    before, after = "ايوه يا فندم تمام", "نكمل الحجز بكرة"
    speech = f"{before} {after}"
    raw = before + " " + " ".join(["<hesitation>"] * 20) + " " + after
    cq = _roundtrip(raw)
    assert cq.clean_text == speech
    assert len(raw) / 12.0 > 22               # raw length WOULD trip it
    q = assess_call([cq], [12.0], set(), 12.0,
                    clean_chars=len(speech), raw_chars=len(raw), chunks_empty=0)
    assert q["speech_chars"] == len(speech)
    assert q["speech_chars"] / 12.0 <= 22
    assert q["status"] == "green"


def test_harmless_chars_excluded_from_ngram_corroboration():
    """A 6-15 run is only invalid when corroborated. A wall of control tokens
    must not do the corroborating: that would charge the chunk's whole
    duration to invalid_seconds."""
    run = " ".join(["لا"] * 8)
    raw = ("بص يا باشا " + run + " " + " ".join(["<hesitation>"] * 30)
           + " طيب نشوف حل تاني")
    cq = _roundtrip(raw)
    assert cq.warn_run == 8
    assert cq.ngram_fraction < 0.45
    q = assess_call([cq], [120.0], set(), 120.0,
                    clean_chars=800, raw_chars=len(raw), chunks_empty=0)
    assert q["invalid_seconds"] == 0.0
    assert q["status"] == "amber"             # warn_only, not invalid


# --- F3: boundary regressions ----------------------------------------------

def test_boundary_tokens_at_chunk_start_and_end():
    assert _roundtrip("<inaudible> تمام يا فندم").clean_text == \
        f"{GAP} تمام يا فندم"
    assert _roundtrip("تمام يا فندم <inaudible>").clean_text == \
        f"تمام يا فندم {GAP}"
    assert _roundtrip("<hesitation> تمام").clean_text == "تمام"
    assert _roundtrip("تمام <hesitation>").clean_text == "تمام"
    assert _roundtrip("<inaudible>").clean_text == GAP


def test_boundary_token_adjacent_to_an_existing_gap_marker():
    for raw in (f"العميل {GAP} <hesitation> الموظف",
                f"العميل <hesitation> {GAP} الموظف"):
        cq = _roundtrip(raw)
        # the pre-existing marker is neither doubled nor eaten
        assert cq.clean_text.count(GAP) == 1
        assert "<hesitation>" not in cq.clean_text
    cq = _roundtrip(f"العميل {GAP} <inaudible> الموظف")
    assert cq.clean_text.count(GAP) == 2
    assert cq.control_gaps == 1


def test_boundary_twenty_lost_audio_tokens_in_a_row_collapse_to_one_gap():
    raw = "تقدر تساعدني " + " ".join(["<inaudible>"] * 20) + " طيب نشوف حل"
    cq = _roundtrip(raw)
    assert not cq.hard_loop                   # a marker stream, not a loop
    assert cq.control_gaps == 20
    assert cq.clean_text.count(GAP) == 1      # adjacent spans merge
    assert cq.clean_text == f"تقدر تساعدني {GAP} طيب نشوف حل"
    q = assess_call([cq], [120.0], set(), 120.0,
                    clean_chars=400, raw_chars=len(raw), chunks_empty=0)
    assert q["invalid_seconds"] == 0.0
    assert q["tier1_chars_removed"] > 200
    assert q["status"] == "red"               # >= 40 chars and >= 25% of speech


def test_boundary_mixed_harmless_and_lost_audio_tokens():
    raw = "الصوت <hesitation> واضح <inaudible> بس مش سامعك <hesitation> كويس"
    cq = _roundtrip(raw)
    assert cq.control_tokens == 2
    assert cq.control_gaps == 1
    assert cq.clean_text == f"الصوت واضح {GAP} بس مش سامعك كويس"
    q = assess_call([cq], [60.0], set(), 60.0,
                    clean_chars=len(cq.clean_text), raw_chars=len(raw),
                    chunks_empty=0)
    assert q["tier1_chars_removed"] == len("<inaudible>")
    assert q["speech_chars"] == len(raw) - 2 * len("<hesitation> ")


def test_boundary_back_to_back_mixed_tokens_do_not_glue_words():
    cq = _roundtrip("الصوت <hesitation><inaudible> واضح")
    assert cq.clean_text == f"الصوت {GAP} واضح"
    cq = _roundtrip("الصوت <inaudible><hesitation> واضح")
    assert cq.clean_text == f"الصوت {GAP} واضح"


def test_control_token_at_edges_collapses_whitespace():
    assert _roundtrip("<hesitation> تمام").clean_text == "تمام"
    assert _roundtrip("تمام <hesitation>").clean_text == "تمام"


def test_back_to_back_control_tokens_do_not_glue_words():
    raw = "الصوت <hesitation><hesitation> واضح"
    cq = _roundtrip(raw)
    assert cq.clean_text == "الصوت واضح"
    assert cq.control_tokens == 2


def test_control_token_counted_in_metrics_without_moving_status():
    raw = "ايه الصوت <hesitation> واضح يا فندم وشكرا لحضرتك على الاتصال"
    cq = clean_chunk(raw)
    q = assess_call([cq], [100.0], set(), 100.0,
                    clean_chars=800, raw_chars=800, chunks_empty=0)
    assert q["control_tokens_removed"] == 1
    assert q["tier1_chars_removed"] == 0   # not contamination
    assert q["invalid_seconds"] == 0.0
    assert q["status"] == "green"           # status-neutral by design


def test_control_token_run_is_not_a_decoder_loop():
    raw = "تقدر تساعدني " + " ".join(["<hesitation>"] * 20) + " طيب نشوف حل"
    cq = _roundtrip(raw)
    assert not cq.hard_loop        # machine punctuation, not lost audio
    assert cq.control_tokens == 20
    assert cq.clean_text == "تقدر تساعدني طيب نشوف حل"
    q = assess_call([cq], [120.0], set(), 120.0,
                    clean_chars=800, raw_chars=900, chunks_empty=0)
    assert q["invalid_seconds"] == 0.0
    assert q["status"] == "green"


def test_arabic_speech_untouched_by_control_token_rule():
    raw = "ايه يا فندم الحجز اتعمل امبارح والتذكرة وصلت على الايميل"
    cq = _roundtrip(raw)
    assert cq.clean_text == raw
    assert not cq.ops and cq.control_tokens == 0


def test_bare_angle_brackets_untouched():
    # a price range read aloud / stray punctuation: no closing tag, no removal
    raw = "السعر < من 3000 و > من 5000 يا فندم <hesitation2 غير مكتمل"
    cq = _roundtrip(raw)
    assert cq.clean_text == raw
    assert not cq.ops and cq.control_tokens == 0


def test_asr_gap_marker_untouched_by_control_token_rule():
    raw = f"العميل سأل عن السعر {GAP} الموظف قال مع السلامة"
    cq = _roundtrip(raw)
    assert cq.clean_text == raw
    assert not cq.ops and cq.control_tokens == 0


def test_control_token_adjacent_to_contamination_still_gets_a_gap():
    raw = "تمام <hesitation> التفريغ والتدقيق قصي البياتي طيب نكمل"
    cq = _roundtrip(raw)
    assert "قصي البياتي" not in cq.clean_text
    assert "<hesitation>" not in cq.clean_text
    assert GAP in cq.clean_text     # real contamination was removed here
    assert cq.control_tokens == 1


# --- URL-garbage chains (trial discovery) ----------------------------------

def test_url_chain_with_contamination_label_removed():
    raw = ("العودة يوم اثنين وعشرين fontsalon.com.au.eu.fontsalon.com.au.eu."
           "fontsalon.com والله قدمت يومين")
    cq = _roundtrip(raw)
    assert "fontsalon" not in cq.clean_text
    assert "قدمت يومين" in cq.clean_text


def test_url_repeated_label_chain_removed():
    raw = "ثواني بس www.site.uk.uk.uk.uk.uk.uk.uk.uk.uk.uk تمام يا فندم"
    cq = _roundtrip(raw)
    assert ".uk" not in cq.clean_text
    assert "تمام يا فندم" in cq.clean_text


def test_genuine_short_url_survives():
    raw = "ابعت لنا على www.travelgate.com والايميل الرسمي"
    cq = _roundtrip(raw)
    assert "www.travelgate.com" in cq.clean_text
    assert not cq.ops


# --- Precision controls ----------------------------------------------------

def test_real_repetition_of_five_untouched():
    raw = "مطار الرياض الرياض الرياض الرياض الرياض أيوا لحظة تمام"
    cq = _roundtrip(raw)
    assert cq.clean_text == raw
    assert not cq.ops and not cq.flags


def test_real_translation_talk_untouched():
    raw = "طلعت تأشيرة وكان عندي فك حضانة وطلبته مني الترجمة ودفعت زيادة"
    cq = _roundtrip(raw)
    assert cq.clean_text == raw
    assert not cq.ops


def test_spoken_phone_digits_exempt_from_warning():
    raw = "الرقم تسعه تسعه تسعه تسعه تسعه تسعه تسعه صفر خمسة"
    cq = _roundtrip(raw)
    assert cq.clean_text == raw
    assert not cq.flags  # 7-run of a digit-word: numeric exemption


# --- Loop truncation -------------------------------------------------------

def test_hard_loop_keeps_first_three_and_following_text():
    loop = " ".join(["لا"] * 30)
    raw = f"تقدر تساعدني فيها؟ {loop} طيب نشوف حل تاني"
    cq = _roundtrip(raw)
    assert cq.hard_loop
    # the genuine refusal survives as exactly three occurrences
    assert "لا لا لا " + GAP in cq.clean_text
    assert "لا لا لا لا" not in cq.clean_text
    # text AFTER the loop is retained (round-4 amendment)
    assert "نشوف حل تاني" in cq.clean_text


def test_punctuation_variants_count_as_one_run():
    # "نعم،" vs "نعم" must normalize to the same token for run counting
    loop = " ".join(("نعم،" if i % 2 else "نعم") for i in range(20))
    raw = f"زين {loop} طيب"
    cq = _roundtrip(raw)
    assert cq.hard_loop


def test_gray_zone_run_flagged_not_removed():
    raw = "بص انت شايفها " + " ".join(["لا"] * 12) + " انا عشان يا استاذ"
    cq = _roundtrip(raw)
    assert not cq.ops
    assert any(f["flag"] == "token_run_6_15" for f in cq.flags)
    assert cq.clean_text == raw


# --- Call gate -------------------------------------------------------------

def _cq(**kw) -> ChunkQuality:
    base = dict(clean_text="نص سليم", ops=[], flags=[], hard_loop=False,
                warn_run=0, ngram_fraction=0.0)
    base.update(kw)
    return ChunkQuality(**base)


def test_gate_green_on_clean_call():
    q = assess_call([_cq()], [100.0], set(), 100.0,
                    clean_chars=800, raw_chars=800, chunks_empty=0)
    assert q["status"] == "green"


def test_gate_red_on_hard_loop_long_chunk():
    q = assess_call([_cq(hard_loop=True)], [120.0], set(), 120.0,
                    clean_chars=500, raw_chars=900, chunks_empty=0)
    assert q["status"] == "red"  # whole 120s chunk counts invalid => >=60s span


def test_gate_amber_on_small_failed_fraction():
    # 1 failed chunk of 30s in a 200s call: 15% invalid, span < 60s => amber
    q = assess_call([_cq(), _cq()], [170.0, 30.0], {1}, 200.0,
                    clean_chars=1500, raw_chars=1500, chunks_empty=0)
    assert q["status"] == "amber"


def test_gate_red_on_contamination_dominated():
    ops = [{"pattern_id": "eddirasa_domain_v1", "raw_start": 0, "raw_end": 300,
            "removed_text": "x" * 300, "replacement_text": GAP}]
    q = assess_call([_cq(ops=ops)], [60.0], set(), 60.0,
                    clean_chars=400, raw_chars=800, chunks_empty=0)
    assert q["status"] == "red"  # 300 >= 40 chars and >= 25% of raw


def test_gate_amber_on_single_small_removal():
    ops = [{"pattern_id": "qusay_credit_v1", "raw_start": 0, "raw_end": 28,
            "removed_text": "x" * 28, "replacement_text": GAP}]
    q = assess_call([_cq(ops=ops)], [90.0], set(), 90.0,
                    clean_chars=800, raw_chars=828, chunks_empty=0)
    assert q["status"] == "amber"


def test_gate_red_below_20_clean_chars():
    q = assess_call([_cq(clean_text="الو")], [40.0], set(), 40.0,
                    clean_chars=3, raw_chars=3, chunks_empty=0)
    assert q["status"] == "red"


# --- Evidence validation with gaps -----------------------------------------

def test_evidence_must_not_cross_gap():
    from app.evaluate.scoring import validate_evidence

    conv = f"العميل سأل عن السعر {GAP} الموظف قال مع السلامة"
    ok = {"evidence": [{"quote": "سأل عن السعر"}]}
    crossing = {"evidence": [{"quote": "عن السعر الموظف قال"}]}
    marker = {"evidence": [{"quote": f"السعر {GAP} الموظف"}]}
    assert validate_evidence(ok, conv) == []
    assert validate_evidence(crossing, conv)
    assert validate_evidence(marker, conv)
