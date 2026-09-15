"""The follow-up-history bullet, and the fact that nothing currently sends one.

Module 4 is 20% of the agent's grade and cannot be observed inside a single
conversation, so it is scored from a FOLLOW-UP HISTORY block describing the
customer's later timeline. On day 13 every conversation that HAD such a timeline
still scored Module 4 = null, because the block that reached the prompt said
only "<channel> by unknown": no direction, no distinction between a bot and a
genuine unknown, and no message text for the criterion that grades message
quality. The block was rebuilt, and `metrics.later_contact_line` is that shape.

WHAT 2026-09-14 FOUND, AND FIXED.

Removing the calls workflow took the SQL renderer with it, and looking for the
chat equivalent turned up the real defect: **01d had never had one.** It built
the prompt without the block, so the judge answered `null` for Module 4 on every
chat the pipeline had ever scored — `m4_followup` was not-applicable on 822 of
834 evaluations. 20% of the rubric, never exercised, and invisible: because
`weight_applied` renormalises over the modules that DID apply, the score looked
like a conversation that needed no follow-up.

The chain now exists end to end and these tests pin every link of it:

    01d "Load thread"        selects `later_interactions` from the customer's
                             own timeline — ROWS, not a rendered string
    /chats/prepare           renders them with `metrics.later_contact_line`
    01d "Two AI passes"      forwards `followup_history` to /evaluate
    build_pass2_prompt       substitutes it into {{FOLLOWUP_HISTORY}}

RULE 2 REACHES THE BLOCK ITSELF. `[]` means we searched the whole timeline and
found nothing — an agent who never came back, which SCORES. `None` means nobody
searched, which NULLS. Collapsing the two is how the module went missing in the
first place, so `followup_history_block` distinguishes them and a test below
holds it to that.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.evaluate import metrics

ROOT = Path(__file__).resolve().parents[3]
CHAT_JUDGE = ROOT / "n8n" / "workflows" / "01d-chats-evaluate.json"


def _load_compare_day():
    spec = importlib.util.spec_from_file_location(
        "compare_day_followup", ROOT / "scripts" / "compare_day.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ENTRIES = [
    {"started_at": "2026-08-15 09:05", "channel": "chat", "direction": "outbound",
     "hours_after": 3.2, "agent_name": "Sara", "first_message": "أهلاً، العرض جاهز"},
    {"started_at": "2026-08-16 11:00", "channel": "chat", "direction": "inbound",
     "hours_after": 29.1, "is_bot_handled": True, "first_message": "؟"},
    {"started_at": "2026-08-17 08:00", "channel": "chat",
     "hours_after": 50.0},
]


# ── the renderer's shape ────────────────────────────────────────────────────

@pytest.mark.parametrize("entry", ENTRIES)
def test_the_worker_and_the_comparison_runner_render_the_same_bullet(entry):
    """Two Python copies remain and they must not drift.

    `compare_day --history-format current` exists so the fixtures test the
    prompt against the block production would send, not a hand-typed lookalike.
    A drifted copy passes its own fixtures forever.
    """
    compare_day = _load_compare_day()
    assert (metrics.later_contact_line(entry)
            in compare_day.render_current_history([entry]))


def test_every_field_the_prompt_reads_survives_into_the_bullet():
    line = metrics.later_contact_line(ENTRIES[0])
    assert "2026-08-15 09:05" in line
    assert "chat" in line
    assert "direction outbound" in line
    assert "3.2h after this conversation" in line
    assert "handled by Sara" in line
    assert "أهلاً، العرض جاهز" in line


def test_a_bot_message_is_never_credited_to_a_human():
    """The whole point of `agents.is_bot`, reaching Module 4. An automation's
    follow-up must not earn a salesperson the timing points."""
    line = metrics.later_contact_line(ENTRIES[1])
    assert "the qualification bot, not a human agent" in line


def test_an_unknown_handler_says_so_rather_than_guessing():
    line = metrics.later_contact_line(ENTRIES[2])
    assert "handled by not recorded" in line
    assert "direction not recorded" in line


def test_the_old_by_unknown_shape_cannot_come_back():
    """The exact string that made Module 4 unanswerable on day 13."""
    for entry in ENTRIES:
        assert " by unknown" not in metrics.later_contact_line(entry)


def test_a_long_message_is_truncated_not_dropped():
    entry = dict(ENTRIES[0], first_message="ب" * 900)
    line = metrics.later_contact_line(entry)
    assert "ب" * 300 in line
    assert "ب" * 301 not in line


def test_the_queue_recording_label_went_with_the_calls_lane():
    """A label for a thing that can no longer occur is a label that will be
    misapplied. There are no queue recordings any more."""
    src = (ROOT / "services" / "worker" / "app" / "evaluate" / "metrics.py").read_text(encoding="utf-8")
    assert "queue recording" not in src


# ── the gap this file now exists to keep visible ────────────────────────────

def _workflow():
    return json.loads(CHAT_JUDGE.read_text(encoding="utf-8"))


def _node(name):
    return [n for n in _workflow()["nodes"] if n["name"] == name][0]


def test_01d_reads_the_customers_later_timeline():
    """The link that did not exist. Without it Module 4 is null forever."""
    query = _node("Load thread")["parameters"]["query"]
    assert "later_interactions" in query
    assert "nx.started_at > i.ended_at" in query, "must look AFTER this conversation"


def test_the_timeline_is_matched_on_the_customer_not_the_thread():
    """A follow-up is a later contact with the SAME PERSON. Matching on
    anything else — the deal, the agent, the phone alone — either misses the
    contact or credits the agent with somebody else's."""
    query = _node("Load thread")["parameters"]["query"]
    assert "nx.customer_id = i.customer_id" in query
    # customer_id is resolved nightly by 03, so a thread ingested today does not
    # have one yet and the phone has to carry it.
    assert "nx.customer_phone_e164 = i.customer_phone_e164" in query


def test_the_lookahead_is_bounded():
    """"Ever after" is not follow-up. A contact three months later is a new
    enquiry, and counting it awards the timing points to an unrelated sale."""
    assert "interval '14 days'" in _node("Load thread")["parameters"]["query"]


def test_the_block_is_not_rendered_in_sql():
    """Rule 3, and the specific mistake that cost Module 4 a whole corpus: the
    bullet format written out twice drifts in one of the copies. The SQL returns
    rows; `metrics.later_contact_line` is the only renderer."""
    query = _node("Load thread")["parameters"]["query"]
    assert "Subsequent contact" not in query
    assert "h after this conversation" not in query


def test_the_rows_reach_the_worker_and_the_block_reaches_the_judge():
    """Four links, and the chain is worth nothing if any one of them is
    missing — which is exactly the state it was in until today."""
    assert "later_interactions" in _node("Prepare chat input")["parameters"]["jsonBody"]
    assert "followup_history" in _node("Two AI passes")["parameters"]["jsonBody"]


def test_the_block_says_which_channels_it_searched():
    """The correction that matters most in this feature.

    `[]` does NOT mean the agent never came back. It means no CHAT follow-up
    was recorded — and chat is the only channel this system has, since the
    telephone lane was removed from it. An agent who called the customer looks
    identical to one who did nothing.

    The block has to say that itself, in the text the model reads. Both the
    rubric and the renderer carry the caveat, because a caveat that lives only
    in the rubric is one a future renderer can quietly contradict.
    """
    assert metrics.followup_history_block(later_contacts=None) == "unavailable"
    searched = metrics.followup_history_block(later_contacts=[])
    assert searched != "unavailable"
    assert "NO CHAT FOLLOW-UP RECORDED" in searched
    assert "phone call" in searched
    assert "may well have happened" in searched


def test_the_prompt_separates_all_three_states_of_the_block():
    """`unavailable` nulls. `NO CHAT FOLLOW-UP RECORDED` also nulls, for a
    different and more interesting reason. A list of contacts scores.

    The middle state is the one the pipeline will be in most often, and the
    reason it nulls is the point: **only chat is searched.** An agent who phoned
    the customer produces the same empty block as one who forgot them, so the
    evidence cannot separate a missed follow-up from an unrecorded one.
    """
    prompt = (ROOT / "services" / "worker" / "app" / "prompts"
              / "pass2_agent_quality_v7.md").read_text(encoding="utf-8")
    assert "is the literal word `unavailable`" in prompt
    assert "Module 4 = `null`" in prompt
    assert "NO CHAT FOLLOW-UP RECORDED" in prompt
    assert "invisible to this system" in prompt
