"""Structural invariants of the 017 workflow changes.

n8n has no compile step. Every assertion here is something that imports
cleanly, activates cleanly, and then costs money or loses data quietly:

  * a schedule that drifts back into DeepSeek's peak window, doubling the bill
  * a claim that goes back to one job per tick, capping the pipeline at 48/day
  * the reopen ceiling removed, letting one thread be judged without limit
  * both n8n and Modal claiming the same call, so it is transcribed twice
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

WF = Path(__file__).resolve().parents[3] / "n8n" / "workflows"


def load(name: str) -> dict:
    return json.loads((WF / name).read_text(encoding="utf-8"))


def nodes(wf: dict) -> dict:
    return {n["name"]: n for n in wf["nodes"]}


def query(wf: dict, node: str) -> str:
    return nodes(wf)[node]["parameters"]["query"]


def crons(wf: dict) -> list[str]:
    return [i["expression"]
            for n in wf["nodes"] if n["type"].endswith("scheduleTrigger")
            for i in n["parameters"]["rule"]["interval"]
            if i.get("field") == "cronExpression"]


CHATS = "01d-chats-evaluate.json"
CALLS = "02-calls-v2-state-machine.json"
HOUSE = "04-nightly-housekeeping.json"


# ---------------------------------------------------------------------------
# the schedule is the discount
# ---------------------------------------------------------------------------

# DeepSeek peak is 01:00-04:00 and 06:00-10:00 UTC Mon-Fri, where every rate
# doubles. n8n runs on Asia/Riyadh (UTC+3), so peak is 04:00-07:00 and
# 09:00-13:00 LOCAL. Anything scheduled inside those hours costs twice as much
# for identical work.
PEAK_LOCAL_HOURS = set(range(4, 7)) | set(range(9, 13))


def local_hours(expr: str) -> set[int]:
    field = expr.split()[1]
    if field == "*":
        return set(range(24))
    out: set[int] = set()
    for part in field.split(","):
        if "-" in part:
            a, b = part.split("-")
            out |= set(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


@pytest.mark.parametrize("wf_name", [CHATS, CALLS, HOUSE])
def test_nothing_is_scheduled_inside_deepseek_peak(wf_name):
    for expr in crons(load(wf_name)):
        assert not (local_hours(expr) & PEAK_LOCAL_HOURS), (
            f"{wf_name}: cron {expr!r} runs inside DeepSeek's peak window "
            f"(04:00-07:00 / 09:00-13:00 Riyadh) and pays double")


def test_judging_finishes_before_peak_starts():
    """The window must END before 04:00 local, not merely start outside it.

    It wraps midnight, so "the last hour" is not max() — the invariant is that
    04:00 itself is never in the set, because that is the instant DeepSeek's
    peak rate begins.
    """
    hours = local_hours(crons(load(CHATS))[0])
    assert hours == {23, 0, 1, 2, 3}, hours


def test_calls_workflow_lists_drive_once_a_day():
    """Every */15 tick listed 611 recordings and stored 530 KB of execution
    data — 51 MB a day to discover nothing new."""
    discovery = [e for e in crons(load(CALLS)) if e.startswith("0 ")]
    assert discovery == ["0 23 * * *"]


# ---------------------------------------------------------------------------
# throughput and the re-judge ceiling
# ---------------------------------------------------------------------------

def test_the_judge_claims_a_batch_not_one_job():
    """LIMIT 1 on a 30-minute tick is a hard ceiling of 48 evaluations a day."""
    q = query(load(CHATS), "Claim work")
    assert "LIMIT 10" in q and "LIMIT 1\n" not in q


def test_reopen_has_both_ceilings():
    """A thread that keeps waking up must not be judged without limit: at most
    one re-judge, and only if it actually grew by half."""
    q = query(load(CHATS), "Register due threads")
    assert "judge_runs < 2" in q
    assert "judged_message_count, 0) * 1.5" in q


def test_judge_runs_is_counted_where_a_judge_is_known_to_have_finished():
    """Counting it at claim time would count an attempt that died mid-call."""
    q = query(load(CHATS), "Mark evaluated")
    assert "judge_runs  = judge_runs + 1" in q
    assert "judged_message_count = coalesce(observed_message_count" in q


def test_judge_calls_are_paced():
    """Ten simultaneous evaluations turn a claimed batch into ten 429s and ten
    spent attempts."""
    for wf_name in (CHATS, CALLS):
        opts = nodes(load(wf_name))["Two AI passes"]["parameters"]["options"]
        assert opts["batching"]["batch"]["batchSize"] <= 3


# ---------------------------------------------------------------------------
# the Modal boundary
# ---------------------------------------------------------------------------

def test_n8n_no_longer_claims_transcription():
    """Two systems that both claim a job both transcribe it and both pay."""
    q = query(load(CALLS), "Claim work")
    assert "'transcribed', 'judge_failed'" in q
    assert "'discovered', 'asr_failed'" not in q.split("WHERE")[1].split("LIMIT")[0]


def test_the_asr_node_is_disabled_not_deleted():
    """Left in place as the rollback: re-enable it and add the 'discovered'
    clause back, and the old path works again."""
    node = nodes(load(CALLS))["Cohere Arabic ASR"]
    assert node.get("disabled") is True


def test_modal_writes_the_same_namespace_as_n8n():
    """A different external_source turns one call into two half-filled rows —
    the split-namespace bug the chat side already had to migrate out of."""
    job = (Path(__file__).resolve().parents[3] / "modal" / "transcribe_job.py"
           ).read_text(encoding="utf-8")
    assert "'asterisk_drive'" in job
    assert "'pbx_drive'" not in job


def test_modal_releases_the_lease_on_handoff():
    """n8n renews the lease because its next node is the judge; Modal is
    finished, so it must set 'transcribed' AND drop the token, or n8n's claim
    will never see the row."""
    job = (Path(__file__).resolve().parents[3] / "modal" / "transcribe_job.py"
           ).read_text(encoding="utf-8")
    tail = job[job.index("UPDATE call_ingest_jobs j\nSET interaction_id"):]
    assert "status         = 'transcribed'" in tail
    assert "claim_token    = NULL" in tail


# ---------------------------------------------------------------------------
# cost telemetry and the budget guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wf_name", [CHATS, CALLS])
def test_every_judge_call_is_recorded_with_its_idempotency_key(wf_name):
    q = query(load(wf_name), "Record model cost")
    assert "INSERT INTO model_calls" in q
    assert "ON CONFLICT (purpose, input_hash, prompt_version) DO NOTHING" in q


@pytest.mark.parametrize("wf_name", [CHATS, CALLS])
def test_requests_are_written_under_a_live_lease(wf_name):
    """An upsert keyed on (interaction_id, seq): a stale execution holding an
    older reading of the thread would overwrite a newer one."""
    q = " ".join(query(load(wf_name), "Store requests").split())
    assert "WITH lease AS MATERIALIZED" in q and "FOR UPDATE" in q
    # The lease must be LIVE, not merely present: an expired one is a row the
    # recovery sweep is entitled to reclaim.
    assert "claim_until > now()" in q
    assert "FROM lease, jsonb_array_elements" in q


def test_the_budget_guard_writes_a_row_rather_than_throwing():
    """A workflow that throws stops, and a nightly job that stops is one more
    thing that fails quietly."""
    q = query(load(HOUSE), "Nightly health check")
    assert "INSERT INTO job_runs" in q
    assert "'over_budget'" in q and "spend_month > 40" in q
    assert "'alert'" in q


def test_retention_reads_its_window_from_the_function_not_the_node():
    """One definition of 'old', so a backfill and this node cannot disagree."""
    q = query(load(HOUSE), "Purge raw content")
    assert "purge_raw_content(90, 365, false)" in q


def test_bitrix_pull_asks_only_for_allowlisted_fields():
    """UF_CRM_1781281581 contains prose addressed to a bot, and a dozen fields
    hold another system's AI verdicts on the same questions we answer."""
    body = nodes(load(HOUSE))["Fetch Bitrix deals"]["parameters"]["jsonBody"]
    assert "UF_CRM" not in body
    for field in ("ID", "STAGE_ID", "OPPORTUNITY", "CONTACT_ID", "DATE_MODIFY"):
        assert f"'{field}'" in body


# ---------------------------------------------------------------------------
# ...and the discount only exists if n8n resolves the cron in Riyadh
#
# Every test above reads a cron expression and calls its hours "local", which
# silently assumes local IS Asia/Riyadh. In production it was not. n8n resolves
# an unqualified cron in GENERIC_TIMEZONE, that variable is not set on the n8n
# service, and only workflow 04 declared a timezone of its own — so the judging
# window ran on n8n's own default instead, landing 23:00-03:59 somewhere else
# entirely and putting most of the night inside DeepSeek's peak.
#
# The bill doubled and every assertion above still passed, because the cron
# text never changed. The fix is to stop depending on a platform default: each
# scheduled workflow carries `settings.timezone`, which the export keeps and
# which no environment variable can override.
# ---------------------------------------------------------------------------

PORTAL_TZ = "Asia/Riyadh"


def scheduled_workflows() -> list[str]:
    out = []
    for path in sorted(WF.glob("*.json")):
        wf = json.loads(path.read_text(encoding="utf-8"))
        if any(n["type"].endswith("scheduleTrigger") for n in wf["nodes"]):
            out.append(path.name)
    return out


def test_there_are_scheduled_workflows_to_check():
    """Keeps the parametrised test below from passing on an empty list."""
    assert len(scheduled_workflows()) >= 4


@pytest.mark.parametrize("wf_name", scheduled_workflows())
def test_every_scheduled_workflow_pins_its_own_timezone(wf_name):
    """A cron with no timezone is not a time. It is a time in whatever zone the
    n8n instance happens to default to, which is not this project's zone and is
    not set anywhere in this repo."""
    tz = json.loads((WF / wf_name).read_text(encoding="utf-8")).get("settings", {}).get("timezone")
    assert tz == PORTAL_TZ, (
        f"{wf_name} has settings.timezone={tz!r}. Every cron in this repo is "
        f"written in {PORTAL_TZ} — the peak-window tests above assume it — so "
        f"the workflow must say so rather than inherit GENERIC_TIMEZONE.")


def test_housekeeping_still_runs_before_identity_resolution():
    """04 fetches the phones that 03 matches on, so 04 must run first. Both now
    declare the same timezone, which is what makes comparing their hours mean
    anything at all."""
    house = load(HOUSE)
    ident = load("03-nightly-resolve-and-aggregate.json")
    assert house["settings"]["timezone"] == ident["settings"]["timezone"]

    def minutes(wf):
        expr = crons(wf)[0].split()
        return int(expr[1]) * 60 + int(expr[0])

    assert minutes(house) < minutes(ident), (
        "04 must run before 03: 03 resolves customers on the E.164 phone and "
        "04 is what fetches it")
