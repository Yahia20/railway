"""The validator must judge CONTENT, not rendering.

PR2 iteration 2, validator `span-v2`. A transcript is rendered with `[04:00]`
segment markers and `AGENT:` / `CUSTOMER:` labels inserted between words that
were spoken contiguously. A verbatim quote of that speech contains no marker, so
it did not match a haystack that still did — and the finding it supported was
discarded as fabricated.

Day 13, `36c6d304`: 190 characters of genuine, contiguous agent speech about
arranging a hotel, tours, transfers and the visa, rejected because a `[04:00]`
had been inserted into the middle of it. That restored 20 points to an agent on
a finding that was real.

Normalising the markers out of BOTH sides fixes it, and is strictly a rendering
concession: a quote of words nobody said still fails, and `[[ASR_GAP]]` — which
marks removed machine output, so the text either side may be unrelated moments
of the call — is still a hard boundary a quote may not cross.
"""
import pytest

from app.evaluate import scoring


def valid(quote, conversation):
    return scoring.quote_problem(quote, *scoring.conversation_spans(conversation)) is None


def problem(quote, conversation):
    return scoring.quote_problem(quote, *scoring.conversation_spans(conversation))


# ── the 36c6d304 case ───────────────────────────────────────────────────────

SEGMENTED = (
    "[00:00] AGENT: خليني ارتب لك كده الفندق مع الجولات السياحيه والتنقلات\n"
    "[04:00] والطيران الدولي واشوف لك برضه امر التاشيره اذا كانت تحتاج تاشيره ام لا\n"
)


def test_a_quote_spanning_a_segment_boundary_matches():
    """The exact text, as the agent said it, across an inserted timestamp."""
    quote = ("خليني ارتب لك كده الفندق مع الجولات السياحيه والتنقلات "
             "والطيران الدولي واشوف لك برضه امر التاشيره اذا كانت تحتاج تاشيره ام لا")
    assert valid(quote, SEGMENTED)


def test_a_quote_containing_the_timestamp_itself_matches():
    """The judge may quote the transcript as rendered. Same speech either way."""
    quote = ("والتنقلات\n[04:00] والطيران الدولي")
    assert valid(quote, SEGMENTED)


def test_a_quote_carrying_a_speaker_label_matches():
    assert valid("AGENT: خليني ارتب لك كده الفندق", SEGMENTED)
    assert valid("CUSTOMER: نعم تفضل", "[00:05] CUSTOMER: نعم تفضل")


@pytest.mark.parametrize("label", ["AGENT:", "CUSTOMER:", "BOT:", "SYSTEM:",
                                   "SPEAKER_1:", "agent:"])
def test_every_label_shape_the_renderer_emits_is_normalised(label):
    assert scoring.strip_transcript_furniture(f"[00:07] {label} نعم تفضل") == "نعم تفضل"


@pytest.mark.parametrize("stamp", ["[0:00]", "[04:00]", "[123:45]", "[01:23:45]"])
def test_every_timestamp_shape_the_renderer_emits_is_normalised(stamp):
    assert scoring.strip_transcript_furniture(f"{stamp} نعم تفضل") == "نعم تفضل"


# ── what normalisation must NOT relax ───────────────────────────────────────

def test_a_fabricated_quote_still_fails():
    assert problem("السعر ألفين دولار شامل كل شيء", SEGMENTED)


def test_a_translated_word_inside_a_real_quote_still_fails():
    """The e5ab9937 case. Not a rendering difference — different words."""
    call = "English please. أظن. Can you speak English? One minute."
    assert problem("English please. I think. Can you speak English?", call)


def test_the_asr_gap_is_still_a_hard_boundary():
    """Stripping timestamps must not dissolve the seam it protects.

    The split happens before normalisation, so text either side of a removed
    ASR passage stays in separate spans however the two halves are rendered.
    """
    gapped = ("[00:10] AGENT: السعر ألفين ريال\n"
              "[[ASR_GAP]]\n"
              "[06:40] CUSTOMER: خلاص ما عليه")
    assert valid("السعر ألفين ريال", gapped)
    assert valid("خلاص ما عليه", gapped)
    assert problem("السعر ألفين ريال خلاص ما عليه", gapped)


def test_the_gap_marker_is_still_unquotable():
    gapped = "AGENT: السعر ألفين ريال [[ASR_GAP]] CUSTOMER: خلاص ما عليه"
    assert "ASR gap marker" in problem("السعر ألفين ريال [[ASR_GAP]]", gapped)


def test_a_quote_that_is_only_furniture_is_refused():
    """`[04:00]` is not evidence of anything, and it appears in every call."""
    assert "only transcript furniture" in problem("[04:00]", SEGMENTED)
    assert "only transcript furniture" in problem("[00:00] AGENT:", SEGMENTED)


def test_arabic_folding_still_applies_after_normalisation():
    """One glyph of ASR spelling drift must still not sink a real quote."""
    assert valid("خليني ارتب لك كده الفندق", SEGMENTED.replace("ا", "أ", 1))


def test_the_speech_gate_and_the_validator_share_one_definition():
    """Two definitions would mean text the gate calls empty and the validator
    calls quotable — or the reverse."""
    from app.main import spoken_content

    rendered = "[00:00] AGENT: هلا صباح الخير\n[00:04] CUSTOMER: هلا صباح الخير\n"
    assert spoken_content(rendered) == scoring.strip_transcript_furniture(rendered)
    assert spoken_content(rendered) == "هلا صباح الخير هلا صباح الخير"


def test_the_validator_version_moved_with_the_behaviour():
    """A changed matcher under an unchanged version label makes every stored
    `pass1_validation` row uninterpretable."""
    assert scoring.VALIDATOR_VERSION == "span-v2"
