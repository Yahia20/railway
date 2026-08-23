"""The follow-up-history bullet has one format, and three places render it.

Module 4 is 20% of the agent's grade and cannot be observed inside a single
phone call, so it is scored from a FOLLOW-UP HISTORY block describing the
customer's later timeline. On day 13 every call that HAD such a timeline still
scored Module 4 = null, because the block that reached the prompt said only
"phone_call by unknown": no direction, no distinction between a queue recording
and a genuine unknown, and no message text for the criterion that grades message
quality.

The block was rebuilt. The renderer that matters lives in SQL — the
`Build follow-up history` node of n8n/workflows/02-calls-ingest-evaluate.json,
dumped to scripts/sql/02_build_follow_up_history.sql — because that is what
production sends. Two Python copies exist for reasons that are not going away:

- `metrics.later_contact_line`, for callers inside the worker;
- `compare_day.render_current_history`, so `--history-format current` and the
  D6 fixtures test the prompt against the block production sends TODAY rather
  than a hand-typed lookalike.

Three renderers is two too many, and the failure mode is silent: a copy drifts,
the fixtures keep passing against the drifted copy, and the suite reports on
input production never sends. That is the exact mistake `--history-format
current` was introduced to prevent, so these tests pin all three together and
fail if any one of them moves.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from app.evaluate import metrics

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "scripts" / "sql" / "02_build_follow_up_history.sql"


def _load_compare_day():
    spec = importlib.util.spec_from_file_location(
        "compare_day_followup", ROOT / "scripts" / "compare_day.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cd = _load_compare_day()


# Every shape the SQL's two CASE expressions can produce, so a drifted copy has
# nowhere to hide: an outbound agent message, a queue callback, a bot-handled
# contact, and a contact we simply know nothing about.
ENTRIES = [
    {"started_at": "2026-08-14 11:20", "channel": "whatsapp",
     "direction": "outbound", "hours_after": 19.5,
     "agent_name": "تركي العتيبي", "first_message": "قررتم شي؟"},
    {"started_at": "2026-08-15 09:05", "channel": "phone_call", "kind": "q",
     "direction": None, "hours_after": 41.0,
     "agent_name": None, "first_message": None},
    {"started_at": "2026-08-15 10:00", "channel": "whatsapp",
     "direction": "inbound", "hours_after": 42.0,
     "agent_name": None, "is_bot_handled": True, "first_message": None},
    {"started_at": "2026-08-16 08:00", "channel": "phone_call",
     "direction": None, "hours_after": 60.0,
     "agent_name": None, "first_message": None},
]


# ── the two Python renderers agree ──────────────────────────────────────────

@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: str(e["started_at"]))
def test_the_worker_and_the_comparison_runner_render_the_same_bullet(entry):
    """`metrics` is what the worker would send; `compare_day` is what the D6
    fixtures and every `--history-format current` re-run send. If they disagree,
    the audit measured the prompt against a block production does not produce."""
    from_compare = cd.render_current_history([entry]).split("\n")[1]
    assert metrics.later_contact_line(entry) == from_compare


def test_both_renderers_agree_on_a_whole_block():
    """Ordering included: the SQL sorts by `started_at`, and a block whose lines
    are in a different order is a different block to a model reading a
    timeline."""
    theirs = cd.render_current_history(ENTRIES)
    mine = metrics.followup_history_block([], ENTRIES)
    assert theirs.split("\n")[1:] == mine.split("\n")[2:]


def test_the_lines_are_sorted_by_time_not_by_argument_order():
    shuffled = list(reversed(ENTRIES))
    assert (metrics.followup_history_block([], shuffled)
            == metrics.followup_history_block([], ENTRIES))


# ── ...and both agree with the SQL, which is the one that ships ─────────────

def test_the_python_format_string_is_the_sql_format_string():
    """Pinned to the literal in the dumped SQL rather than to a copy of it.

    `scripts/sql/` is generated from the workflow JSON, so this test follows the
    node that actually runs in production: change the format there without
    changing it here and this fails.
    """
    sql = SQL.read_text(encoding="utf-8")
    assert ("format('  - [%s] %s, %s, %sh after this conversation, "
            "handled by %s%s'" in sql)

    rendered = metrics.later_contact_line(ENTRIES[0])
    assert re.fullmatch(
        r"  - \[.+?\] .+?, .+?, [\d.]+h after this conversation, "
        r"handled by .+", rendered)


@pytest.mark.parametrize("literal", [
    "INBOUND: the customer called in, this is not an agent follow-up",
    "no individual agent recorded (queue recording)",
    "the qualification bot, not a human agent",
    "direction not recorded",
    "Subsequent contact with this customer:",
])
def test_every_label_is_the_sql_label_verbatim(literal):
    """The judge is told to read these strings. A paraphrase in one renderer is
    a different instruction to the model, not a cosmetic difference."""
    assert literal in SQL.read_text(encoding="utf-8")

    produced = "\n".join(
        [metrics.followup_history_block([], ENTRIES)]
        + [metrics.later_contact_line(e) for e in ENTRIES])
    assert literal in produced


# ── the three defects the rebuilt block exists to fix ───────────────────────

def test_an_inbound_queue_callback_is_not_credited_to_the_agent():
    """The half that matters. Nearly every recording in this corpus is a queue
    recording, so a renderer that let one read as agent follow-up would hand out
    20% of the grade for the customer's own effort as the NORMAL case."""
    line = metrics.later_contact_line(ENTRIES[1])
    assert "INBOUND: the customer called in, this is not an agent follow-up" in line
    assert "direction " not in line.replace(
        "INBOUND: the customer called in, this is not an agent follow-up", "")


def test_a_queue_recording_is_distinguishable_from_an_unknown_handler():
    """Day-13 defect 1: `coalesce(full_name, 'unknown')` rendered "unknown" for
    essentially the whole corpus, because 'q' recordings carry agent_id = NULL
    by design — the extension in a queue filename is the QUEUE, not a person."""
    queue = metrics.later_contact_line(ENTRIES[1])
    unknown = metrics.later_contact_line(ENTRIES[3])
    assert "no individual agent recorded (queue recording)" in queue
    assert "not recorded" in unknown
    assert queue != unknown
    assert "unknown" not in queue and "unknown" not in unknown


def test_the_message_text_is_carried_and_quoted():
    """Day-13 defect 3: criterion 3 is follow-up MESSAGE QUALITY, 30 of the
    module's 100 points, and is unanswerable from a bullet without the text."""
    assert 'قررتم شي؟"' in metrics.later_contact_line(ENTRIES[0])


def test_a_long_message_is_truncated_the_way_the_sql_truncates_it():
    """`left(msg.body, 300)`. A renderer that sent the whole thread would change
    the token cost and the evidence the judge quotes."""
    entry = dict(ENTRIES[0], first_message="ب" * 500)
    line = metrics.later_contact_line(entry)
    assert "ب" * 300 in line
    assert "ب" * 301 not in line
    assert "left(msg.body, 300)" in SQL.read_text(encoding="utf-8")


def test_the_old_by_unknown_shape_cannot_come_back():
    """The literal string the day-13 block produced. It is asserted against
    rather than described so that a revert is a test failure and not a review
    comment nobody makes."""
    for entry in ENTRIES:
        line = metrics.later_contact_line(entry)
        # The old bullet put the handler directly after the channel —
        # "[ts] phone_call by unknown, 41.0h after..." — with nothing between
        # them. The current one has the direction there and says "handled by".
        assert not re.search(r"\]\s+\S+\s+by\s", line), line
        assert "unknown" not in line, line
        # ...and the hours are followed by the handler, not by the message.
        assert "after this conversation, handled by " in line, line


# ── the 'we cannot see' sentinel, which the prompt reads as null ────────────

def test_nothing_known_is_the_word_the_prompt_looks_for():
    """`unavailable` is not a formatting choice: the pass-2 prompt matches it to
    decide Module 4 = null. An empty string or "none" would be scored."""
    assert metrics.followup_history_block([], []) == "unavailable"


def test_a_promise_with_no_later_contact_says_so_explicitly():
    """Distinct from `unavailable`: here we CAN see the timeline and it is
    empty, which is a missed follow-up rather than an unobservable one."""
    block = metrics.followup_history_block(
        [{"timestamp": "00:42", "promise": "أتواصل معك بكرة"}], [])
    assert "Subsequent contact with this customer: NONE recorded." in block
    assert "أتواصل معك بكرة" in block


def test_the_promises_section_has_no_sql_counterpart():
    """It is derived from the conversation, not from the customer's timeline, so
    it is the one part of this block the SQL does not and should not render."""
    assert "Promises made by the agent" not in SQL.read_text(encoding="utf-8")
    block = metrics.followup_history_block(
        [{"timestamp": "00:42", "promise": "أتواصل معك بكرة", "due_hint": "بكرة"}],
        ENTRIES)
    assert block.index("Promises made by the agent") < block.index(
        "Subsequent contact with this customer:")
