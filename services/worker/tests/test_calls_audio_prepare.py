"""The call transcript of record: masking, lines, timings, stereo. Synthetic data only."""
import io
import wave

import numpy as np

from app.calls_audio import prepare
from app.calls_audio.service import stereo_key


def test_numbers_emails_and_the_callers_name_never_reach_a_model():
    text = "رقمي 0501234567 والحجز ٤٥٦٧٨٩ ايميلي a.b@x.com وأنا Ahmed Saleh"
    out = prepare.mask(text, prepare.name_parts("Ahmed Saleh"))
    assert "0501234567" not in out and "٤٥٦٧٨٩" not in out and "@" not in out
    assert "Ahmed" not in out and "Saleh" not in out
    assert "[رقم]" in out and "[بريد]" in out and "[اسم]" in out


def test_four_digit_runs_are_masked_but_day_numbers_survive():
    assert "1550" not in prepare.mask("السعر 1550", [])   # 4 digits could be a PIN or a card's last four
    assert "12" in prepare.mask("يوم 12 ديسمبر", [])


def test_words_become_lines_by_voice_and_long_pauses():
    words = [[0.0, 0.4, "A", " السلام"], [0.4, 0.8, "A", " عليكم"], [1.0, 1.5, "B", " أهلا"],
             [5.0, 5.5, "B", " عندي"], [5.6, 6.0, "A", " تفضل"]]
    lines = prepare.turns(words)
    assert [(t["voice"], t["text"]) for t in lines] == [("A", "السلام عليكم"), ("B", "أهلا"), ("B", "عندي"), ("A", "تفضل")]


def test_segments_label_the_agent_voice_and_number_from_one():
    lines = [{"voice": "B", "start": 0, "end": 1, "text": "x"}, {"voice": "A", "start": 1, "end": 2, "text": "y"}]
    segs = prepare.segments(lines, agent_voice="B")
    assert [(s["id"], s["speaker"]) for s in segs] == [("1", "agent"), ("2", "customer")]


def test_timings_come_from_the_diariser_not_a_model():
    out = {"audio_seconds": 20.0, "voices": 2, "speech_seconds": {"A": 8.0, "B": 6.0},
           "raw_segments": [[1.0, 9.0, "s0"], [8.0, 14.0, "s1"]]}
    segs = [{"speaker": "customer", "start": 1.0}, {"speaker": "agent", "start": 8.0}]
    m = prepare.metrics(out, "B", segs)
    assert m["overlap_seconds"] == 1.0                  # 8-9 s both voices
    assert m["silence_seconds"] == 7.0                  # 0-1 and 14-20
    assert m["longest_silence_seconds"] == 1.0          # only gaps BETWEEN speech count
    assert m["agent_speech_seconds"] == 6.0 and m["customer_speech_seconds"] == 8.0
    assert m["agent_first_words_at"] == 8.0


def _wav(samples, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buf.getvalue()


def test_stereo_puts_the_agent_left_and_the_customer_right():
    rate = 8000
    pcm = _wav(np.full(rate * 2, 1000), rate)
    st = prepare.stereo_wav(pcm, [[0.0, 1.0, "A"], [1.0, 2.0, "B"]], agent_voice="B")
    with wave.open(io.BytesIO(st)) as w:
        assert w.getnchannels() == 2 and w.getframerate() == rate and w.getnframes() == rate * 2
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
    left, right = x[:, 0], x[:, 1]
    assert abs(int(left[rate // 2])) < 50 and abs(int(right[rate // 2]) - 1000) < 50       # first second: customer (A) on the right
    assert abs(int(left[rate + rate // 2]) - 1000) < 50 and abs(int(right[rate + rate // 2])) < 50


AHMED = "00000000-0000-0000-0000-00000000000a"
SARA = "00000000-0000-0000-0000-00000000000b"
AHMED2 = "00000000-0000-0000-0000-00000000000c"
ROSTER = [{"agent_id": AHMED, "first": "Ahmed"}, {"agent_id": SARA, "first": "Sara"},
          {"agent_id": AHMED2, "first": "AHMED"}]


def test_the_name_said_in_the_call_wins_over_the_phone():
    # The owner's rule, 2026-11-20: Sara answers Ahmed's phone -> Sara's call.
    from app.calls_audio.service import name_check
    agent = "السلام عليكم معاك سارة من ترافل جيت"
    r = name_check({"said_name": "سارة", "employee_name": "Sara"}, ROSTER, AHMED, agent)
    assert (r["status"], r["agent_id"], r["method"]) == ("matched", SARA, "unique")
    assert name_check({"said_name": "سارة", "employee_name": "Sara"}, ROSTER, None, agent)["agent_id"] == SARA


def test_no_name_said_is_its_own_answer_not_the_lines_owner():
    from app.calls_audio.service import name_check
    agent = "السلام عليكم ترافل جيت"
    for answer in ({}, {"said_name": "", "employee_name": ""}):
        r = name_check(answer, ROSTER, AHMED, agent)
        assert (r["status"], r["agent_id"]) == ("not_said", None)


def test_a_name_the_customer_said_is_not_the_agents_introduction():
    # Codex review 2026-11-18: the customer saying "منى" is not the agent introducing themselves.
    from app.calls_audio.service import name_check
    agent_lines = "السلام عليكم ترافل جيت تفضل"           # the customer's "انا سارة" is in the other voice
    r = name_check({"said_name": "سارة", "employee_name": "Sara"}, ROSTER, AHMED, agent_lines)
    assert (r["status"], r["agent_id"], r["said"]) == ("not_said", None, None)


def test_a_name_that_is_no_employee_gives_the_call_to_nobody():
    from app.calls_audio.service import name_check
    agent = "معاك خالد من ترافل جيت"
    for picked in ("", "Khaled"):                         # the model found no one, or invented a name off the list
        r = name_check({"said_name": "خالد", "employee_name": picked}, ROSTER, AHMED, agent)
        assert (r["status"], r["agent_id"]) == ("unknown_name", None)


def test_a_shared_name_is_settled_by_the_line_or_left_to_the_team():
    from app.calls_audio.service import name_check
    agent = "معاك احمد من ترافل جيت"
    r = name_check({"said_name": "احمد", "employee_name": "Ahmed"}, ROSTER, AHMED2, agent)   # case differs, same name
    assert (r["status"], r["agent_id"], r["method"], r["candidates"]) == ("matched", AHMED2, "by_line", 2)
    r = name_check({"said_name": "احمد", "employee_name": "Ahmed"}, ROSTER, SARA, agent)
    assert (r["status"], r["agent_id"]) == ("ambiguous", None)
    assert name_check({"said_name": "احمد", "employee_name": "Ahmed"}, ROSTER, None, agent)["status"] == "ambiguous"


def test_the_model_chooses_from_one_spelling_per_name():
    from app.calls_audio.service import first_names
    assert first_names(ROSTER + [{"agent_id": "x", "first": " "}]) == ["Ahmed", "Sara"]


def test_stereo_lands_next_to_the_recording_never_over_it():
    assert stereo_key("calls/2026/11/14/1794664989.324053.wav") == "stereo/2026/11/14/1794664989.324053.wav"
