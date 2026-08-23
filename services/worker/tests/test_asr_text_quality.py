# -*- coding: utf-8 -*-
"""Release-1 ASR text cleaning. Every case here is a failure mode a real
transcript actually produced (2026-08-09 batch, 10-call validation trial);
the text is synthetic but the shapes are not."""
from app.asr.text_quality import (
    GAP,
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
