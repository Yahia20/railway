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


OWNER = "00000000-0000-0000-0000-00000000000a"


def test_a_different_name_said_by_the_agent_is_a_mismatch():
    from app.calls_audio.service import name_check
    agent = "السلام عليكم معاك محمد من ترافل جيت"
    assert name_check({"said_name": "محمد", "same_person": "no"}, "Sara", OWNER, agent)["verdict"] == "no"
    assert name_check({"said_name": "محمد", "same_person": "yes"}, "Mohamed", OWNER, agent)["verdict"] == "yes"
    assert name_check({"said_name": "محمد", "same_person": "no"}, "Sara", OWNER, agent)["owner_id"] == OWNER


def test_a_name_the_customer_said_is_not_the_agents_introduction():
    # Codex review 2026-11-18: the customer saying "منى" must not clear Sara's call.
    from app.calls_audio.service import name_check
    agent_lines = "السلام عليكم ترافل جيت تفضل"           # the customer's "انا منى" is in the other voice
    assert name_check({"said_name": "منى", "same_person": "no"}, "Sara", OWNER, agent_lines)["verdict"] == "unknown"


def test_a_name_never_said_or_no_owner_never_takes_a_call_away():
    from app.calls_audio.service import name_check
    agent = "السلام عليكم ترافل جيت"
    assert name_check({"said_name": "خالد", "same_person": "no"}, "Sara", OWNER, agent)["verdict"] == "unknown"   # not in the call
    assert name_check({"said_name": "", "same_person": "no"}, "Sara", OWNER, agent)["verdict"] == "unknown"
    assert name_check({"said_name": "ترافل", "same_person": "no"}, None, None, agent)["verdict"] == "unknown"   # owner unknown
    assert name_check({}, "Sara", OWNER, agent) == {"said": None, "owner": "Sara", "owner_id": OWNER, "verdict": "unknown"}


def test_stereo_lands_next_to_the_recording_never_over_it():
    assert stereo_key("calls/2026/11/14/1794664989.324053.wav") == "stereo/2026/11/14/1794664989.324053.wav"
