"""calls-audio pure parts: no models loaded.  cd services/audio && python -m pytest -q"""
import pipeline as p


def test_pieces_for_the_transcriber_never_overlap():
    # s1 says "اه" (3-4 s) inside s0's long turn; fill_two's spans overlap there,
    # which wrote the same seconds into both voices' lines (2026-11-18).
    raw = [[0, 10, "s0"], [3, 4, "s1"], [11, 20, "s1"], [19.5, 25, "s0"]]
    parts = p.partition(raw, 30)
    assert all(a[1] <= b[0] for a, b in zip(parts, parts[1:]))
    assert parts[0] == [0.0, 10.5, "A"]            # the backchannel stays in the talker's piece
    assert parts[-1][1] == 30.0 and {v for *_, v in parts} == {"A", "B"}


def test_a_turns_words_share_its_time_and_keep_their_order():
    w = p.spread(1.0, 3.0, "B", "اهلا عايز احجز")
    assert [x[3] for x in w] == [" اهلا", " عايز", " احجز"]
    assert w[0][0] == 1.0 and abs(w[-1][1] - 3.0) < 0.02 and all(x[2] == "B" for x in w)
    assert p.spread(0, 1, "A", "  ") == []


def test_no_speech_no_pieces():
    assert p.partition([], 5) == []
