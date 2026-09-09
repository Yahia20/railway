"""Structural invariants of the 018/019 production-hardening changes.

Every assertion here is a bug that was live, imported cleanly, activated
cleanly, and then produced wrong numbers or wasted money in silence:

  * a whole-table statement running once per upstream row — `job_runs` reached
    2,622 identical rows in one night, and `Claim work` claimed ten jobs per
    upstream row instead of ten per tick
  * an automation's own LLM prompt reaching the judge as agent speech, and
    being graded as salesmanship
  * alerts evaluated for calls only, leaving the follow-up queue dark the
    moment calls paused
  * the agent roster — 48 real names — landing in a public repository
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WF = REPO / "n8n" / "workflows"

CHATS = "01d-chats-evaluate.json"
NIGHTLY = "03-nightly-resolve-and-aggregate.json"
HOUSEKEEPING = "04-nightly-housekeeping.json"

POSTGRES = "n8n-nodes-base.postgres"


def load(name: str) -> dict:
    return json.loads((WF / name).read_text(encoding="utf-8"))


def nodes(wf: dict) -> dict:
    return {n["name"]: n for n in wf["nodes"]}


def query(wf: dict, node: str) -> str:
    return nodes(wf)[node]["parameters"].get("query", "")


def strip_comments(sql: str) -> str:
    return "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))


def trigger_fed(wf: dict) -> set[str]:
    """Nodes wired straight to a trigger. A trigger emits exactly one item."""
    out: set[str] = set()
    for name, conn in wf["connections"].items():
        src = nodes(wf).get(name)
        if src and "trigger" in src["type"].lower():
            for branch in conn.get("main", []):
                out.update(t["node"] for t in branch)
    return out


# ---------------------------------------------------------------------------
# 1 · executeOnce
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wf_name", [CHATS, NIGHTLY, HOUSEKEEPING])
def test_whole_table_statements_run_once_per_execution(wf_name):
    """A Postgres node runs once per INPUT ITEM, not once per execution.

    A statement with no `$N` placeholder describes work over the whole table,
    so running it once per upstream row does the same work N times. This is
    what wrote one `nightly_health` row 2,622 times in a single night: the
    upstream `Purge raw content` returned 2,622 rows.
    """
    wf = load(wf_name)
    fed_by_trigger = trigger_fed(wf)
    offenders = []
    for node in wf["nodes"]:
        if node["type"] != POSTGRES:
            continue
        body = strip_comments(node["parameters"].get("query", ""))
        if not body.strip() or re.search(r"\$\d", body):
            continue                                  # parameterised: fine
        if node["name"] in fed_by_trigger:
            continue                                  # one item in, by definition
        if not node.get("executeOnce"):
            offenders.append(node["name"])
    assert offenders == [], (
        f"{wf_name}: whole-table nodes without executeOnce: {offenders}")


def test_claim_work_claims_once_per_tick_not_once_per_upstream_row():
    """`Claim work` has a LIMIT, so running it per item multiplies the claim.

    01d's chain is Register -> Recover -> Claim. Register returns one row per
    thread it registered, so without executeOnce the claim ran once per
    registered thread and took `LIMIT` jobs EACH time — hundreds of concurrent
    judge calls against a 0.5 GB worker, which is what produced the
    `socket hang up` and `timeout of 300000ms` dead letters. Workflow 02 has
    always had this right; 01d lost it.
    """
    wf = load(CHATS)
    n = nodes(wf)
    assert "LIMIT" in strip_comments(query(wf, "Claim work")).upper(), \
        "Claim work no longer has a LIMIT — this test is guarding the wrong thing"
    for name in ("Claim work", "Recover expired leases"):
        assert n[name].get("executeOnce") is True, f"{name} must set executeOnce"


# ---------------------------------------------------------------------------
# 2 · an automation's turns must never reach the judge as agent speech
# ---------------------------------------------------------------------------

def test_load_thread_relabels_bot_agent_turns():
    """Bitrix user 1 stores the PROMPT it sends its own model as an agent turn.

    Left labelled 'agent' that text became instructions inside our judge's
    input — rule 8's prompt-injection failure arriving through a door nobody
    was watching — and was graded as salesmanship.

    The relabel must be driven off `agents.is_bot` and not a hardcoded id, so
    that silencing the next automation is one UPDATE and no deploy.
    """
    sql = strip_comments(query(load(CHATS), "Load thread"))
    assert "agents" in sql and "is_bot" in sql, \
        "Load thread must join agents and read is_bot"
    assert "'bot'" in sql, "bot turns must be relabelled to sender='bot'"
    assert not re.search(r"sender_external_id\s*=\s*'1'", sql), \
        "the exclusion must not hardcode a user id — use agents.is_bot"


def test_bot_turns_are_relabelled_not_deleted():
    """Deleting the turn would close a response gap that really was that long.

    An empty or automated turn still sits between a customer question and the
    agent's answer. 011 made the same point about attachments. Keep the row,
    change its label.
    """
    sql = strip_comments(query(load(CHATS), "Load thread"))
    assert "jsonb_agg" in sql
    assert "CASE WHEN" in sql.upper(), "expected a CASE relabel, not a WHERE filter"
    # the only FILTER present is the pre-existing NULL guard from the LEFT JOIN
    assert "a.is_bot" not in sql.split("FILTER", 1)[-1].split(")", 1)[0], \
        "is_bot must not appear in the aggregate FILTER — that would drop turns"


# ---------------------------------------------------------------------------
# 3 · alerts must cover both channels
# ---------------------------------------------------------------------------

def test_chats_evaluate_alert_rules():
    """`evaluate_alert_rules(uuid)` was always channel-agnostic; only 02 called it.

    Every alert occurrence in the database is a `phone_call`. With calls paused
    that leaves the follow-up queue — the thing the alert table exists to
    produce — completely empty.
    """
    wf = load(CHATS)
    n = nodes(wf)
    assert "Evaluate alert rules" in n, "01d must evaluate alert rules"
    sql = strip_comments(query(wf, "Evaluate alert rules"))
    assert "evaluate_alert_rules" in sql
    assert "alerts_evaluated_at" in sql, \
        "the evaluation must stamp in the same statement, or a failure is lost"
    assert "FOR UPDATE" in sql.upper(), \
        "read-and-stamp must be atomic, like 02's"


def test_alert_evaluation_runs_after_the_terminal_transition():
    """It must not run before the job is marked evaluated, or the fence is wrong."""
    wf = load(CHATS)
    targets = [t["node"]
               for branch in wf["connections"].get("Mark evaluated", {}).get("main", [])
               for t in branch]
    assert "Evaluate alert rules" in targets, \
        "alert evaluation must be chained after Mark evaluated"


# ---------------------------------------------------------------------------
# 4 · the roster must not be in the public repository
# ---------------------------------------------------------------------------

def test_migrations_carry_no_staff_names():
    """Rule 7: this repo is public. 48 real people's names belong in a gitignored
    file that `scripts/seed_agents.py` reads, not inlined in a migration."""
    for path in (REPO / "db" / "migrations").glob("*.sql"):
        text = path.read_text(encoding="utf-8")
        assert "INSERT INTO agents (bitrix_user_id, full_name" not in text, (
            f"{path.name} inlines the agent roster. Move it to "
            f"local-reports/agent_roster.json and seed with scripts/seed_agents.py")


def test_seed_script_exists_and_reads_a_roster_file():
    seed = REPO / "scripts" / "seed_agents.py"
    assert seed.exists(), "scripts/seed_agents.py is the roster's home"
    text = seed.read_text(encoding="utf-8")
    assert "--roster" in text and "is_bot" in text


# ---------------------------------------------------------------------------
# 5 · attribution must not charge a human for an automation's thread
# ---------------------------------------------------------------------------

def test_attribution_function_excludes_bots_and_clears_stale_links():
    sql = (REPO / "db" / "migrations" / "019_bot_turns_and_chat_alerts.sql").read_text(
        encoding="utf-8")
    assert "a.is_bot = false" in sql, "the human pick must exclude bot accounts"
    assert "SET agent_id = NULL" in sql, (
        "flagging an automation must take its threads back off whichever human "
        "they were previously charged to")
    # the deal fallback must not fire when a bot did speak
    fallback = sql.split("-- c) the deal's owner", 1)[1].split("GET DIAGNOSTICS", 1)[0]
    assert "NOT EXISTS" in fallback, (
        "the deal-owner fallback must apply only where nobody identifiable typed")
