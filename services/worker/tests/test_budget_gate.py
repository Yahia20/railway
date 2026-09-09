"""The money gate: stop before claiming, never after failing.

The bug these guard against is not hypothetical. On 2026-09-09 DeepSeek's
balance was -0.10 USD with `is_available: false`, and 599 threads sat pending.
Without a gate the next window would have claimed them ten at a time, failed
every call on a payment error, incremented `judge_attempts`, and dead-lettered
the entire queue within three nights — destroying work for a reason that has
nothing to do with the conversations in it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WF = REPO / "n8n" / "workflows"
POSTGRES = "n8n-nodes-base.postgres"

SPENDERS = ["01d-chats-evaluate.json", "02-calls-v2-state-machine.json"]


def load(name: str) -> dict:
    return json.loads((WF / name).read_text(encoding="utf-8"))


def nodes(wf: dict) -> dict:
    return {n["name"]: n for n in wf["nodes"]}


def sql_body(sql: str) -> str:
    """The statement without its commentary.

    Every assertion here is about what the database will RUN. These files
    document their own reasoning at length, and a comment that quotes the old
    broken code — which several deliberately do — must not read as the broken
    code still being there.
    """
    return "\n".join(l for l in sql.splitlines()
                     if not l.strip().startswith("--"))


def py_body(src: str) -> str:
    """Same idea for Python: drop whole-line `#` comments and docstrings."""
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and node.body and isinstance(node.body[0], ast.Expr):
                node.body.pop(0)
                if not node.body:
                    node.body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


def edges(wf: dict) -> list[tuple[str, str]]:
    return [(src, t["node"])
            for src, conn in wf["connections"].items()
            for branch in conn.get("main", [])
            for t in branch]


def reachable_from_triggers(wf: dict, cut: str | None = None) -> set[str]:
    """Every node a trigger can reach, optionally with one node removed.

    Cutting a node and re-running reachability is how you ask "is this node on
    EVERY path?" rather than the much weaker "is it on SOME path" — which is
    the difference between a gate and a suggestion.
    """
    out: dict[str, list[str]] = {}
    for src, dst in edges(wf):
        if src == cut:
            continue
        out.setdefault(src, []).append(dst)
    stack = [n["name"] for n in wf["nodes"] if "trigger" in n["type"].lower()]
    seen: set[str] = set()
    while stack:
        n = stack.pop()
        if n in seen or n == cut:
            continue
        seen.add(n)
        stack.extend(out.get(n, []))
    return seen


# ---------------------------------------------------------------------------
# The gate exists, and it is genuinely upstream of the money
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wf_name", SPENDERS)
def test_workflow_has_a_budget_gate(wf_name):
    n = nodes(load(wf_name))
    for required in ("Check budget", "Record provider status",
                     "Read budget gate", "May we spend?", "Log blocked run"):
        assert required in n, f"{wf_name} is missing {required!r}"


@pytest.mark.parametrize("wf_name", SPENDERS)
def test_nothing_is_claimed_before_the_gate(wf_name):
    """`Claim work` is the moment money starts being spent.

    Everything that runs before it must be free. If the claim can be reached
    without passing `May we spend?`, the gate is decoration: the jobs are
    already claimed and their attempts already spent by the time anyone asks
    whether we can afford it.
    """
    wf = load(wf_name)
    assert "Claim work" in reachable_from_triggers(wf), (
        f"{wf_name}: Claim work is unreachable — this test is guarding nothing")
    # Remove the gate. If the claim is STILL reachable, some path goes round it.
    without_gate = reachable_from_triggers(wf, cut="May we spend?")
    assert "Claim work" not in without_gate, (
        f"{wf_name}: Claim work is reachable without passing 'May we spend?'. "
        f"The gate is bypassable, so jobs get claimed and their attempts spent "
        f"before anything asks whether we can afford it.")


@pytest.mark.parametrize("wf_name", SPENDERS)
def test_blocked_branch_writes_a_run_log_and_claims_nothing(wf_name):
    """A pipeline that stops silently is indistinguishable from a broken one."""
    wf = load(wf_name)
    n = nodes(wf)
    false_branch = [t["node"]
                    for t in wf["connections"]["May we spend?"]["main"][1]]
    assert false_branch == ["Log blocked run"], (
        f"{wf_name}: the blocked branch must lead to Log blocked run only, "
        f"got {false_branch}")

    sql = sql_body(n["Log blocked run"]["parameters"]["query"])
    assert "job_runs" in sql, "the blocked run must be recorded"
    assert "'skipped'" in sql, (
        "status must be 'skipped', not 'failed': nothing was attempted")
    for forbidden in ("UPDATE chat_eval_jobs", "UPDATE call_ingest_jobs",
                      "judge_attempts", "claim_token"):
        assert forbidden not in sql, (
            f"the blocked path must not touch the queue, found {forbidden!r}")


@pytest.mark.parametrize("wf_name", SPENDERS)
def test_gate_reads_the_view_and_does_not_reimplement_the_policy(wf_name):
    """Cap reached? balance gone? disabled? All of that lives in one view.

    A workflow that re-derives the rule is a workflow that acquires a fourth
    condition nobody adds to the other one.
    """
    sql = sql_body(nodes(load(wf_name))["Read budget gate"]["parameters"]["query"])
    assert "v_pipeline_gate" in sql
    for reimplemented in ("monthly_cap_usd >", "spend_mtd_usd >", "is_available"):
        assert reimplemented not in sql, (
            f"{wf_name} re-derives budget policy ({reimplemented!r}); read the view")


@pytest.mark.parametrize("wf_name", SPENDERS)
def test_status_write_and_gate_read_are_separate_statements(wf_name):
    """A CTE beside the INSERT would read the pre-update snapshot.

    That is the exact bug workflow 03 shipped: a data-modifying CTE and a main
    query share one snapshot, so the read never sees the write. It also cannot
    be one node for a duller reason — a parameterised query may carry only one
    command.
    """
    n = nodes(load(wf_name))
    record = sql_body(n["Record provider status"]["parameters"]["query"])
    read = sql_body(n["Read budget gate"]["parameters"]["query"])
    assert "v_pipeline_gate" not in record, (
        "the verdict must be read in a separate statement, or it reads a "
        "snapshot taken before the probe was stored")
    assert "INSERT" not in read.upper()
    # one command each
    assert record.rstrip().rstrip(";").count(";") == 0
    assert read.rstrip().rstrip(";").count(";") == 0


# ---------------------------------------------------------------------------
# The Modal cap is enforced where it cannot be bypassed
# ---------------------------------------------------------------------------

def test_asr_claim_is_gated_in_the_worker_not_in_modal():
    """Modal reaches the database only through the worker, so the worker is the
    one chokepoint. A cap in `modal/transcribe_job.py` would be advisory: a
    redeploy or a hand-run `modal run --limit 500` steps straight past it."""
    main = py_body((REPO / "services" / "worker" / "app" / "main.py")
                   .read_text(encoding="utf-8"))
    claim = main.split("def asr_claim(", 1)[1].split("\ndef ", 1)[0]
    assert "budget.asr_claim_allowance" in claim, (
        "/asr/claim must ask the budget before handing out work")
    assert "allowed" in claim and "req.limit" in claim, (
        "the claim must be trimmed to what the remaining budget can pay for")


def test_modal_job_does_not_enforce_its_own_cap():
    """Belt and braces is fine; a cap that exists ONLY in Modal is not.

    This asserts the enforcement is not silently moved back into the batch,
    where a `--limit` override would defeat it.
    """
    job = py_body((REPO / "modal" / "transcribe_job.py").read_text(encoding="utf-8"))
    assert "provider_budgets" not in job, (
        "the cap belongs in the worker; Modal must not be the only enforcer")


def test_audio_fetch_is_scheme_dispatched():
    """The call source is going to change. `audio_uri` has always been
    scheme-prefixed, but the batch used to strip 'drive://' and call Drive
    unconditionally — which silently made Drive the only possible source."""
    raw = (REPO / "modal" / "transcribe_job.py").read_text(encoding="utf-8")
    assert "FETCHERS" in raw and "def fetch_audio(" in raw
    code = py_body(raw)
    assert "replace('drive://'" not in code, "stripping the scheme hardcodes Drive"
    assert 'replace("drive://"' not in code, "stripping the scheme hardcodes Drive"
    for scheme in ("drive", "https", "s3"):
        assert f"'{scheme}'" in code or f'"{scheme}"' in code, (
            f"no fetcher registered for {scheme}")


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------

def test_migration_declares_the_modal_hard_cap():
    sql = (REPO / "db" / "migrations" / "020_spend_governance.sql").read_text(
        encoding="utf-8")
    assert "'modal'" in sql and "30.00" in sql, "the 30 USD Modal cap must be seeded"
    assert "hard_stop" in sql


def test_gate_fails_closed_for_a_provider_we_can_probe():
    """An unchecked DeepSeek is exactly the state that would empty the queue.

    For a provider with a balance API, 'we have not looked' must mean stop —
    not carry on and find out by failing.
    """
    sql = (REPO / "db" / "migrations" / "020_spend_governance.sql").read_text(
        encoding="utf-8")
    gate = sql.split("CREATE OR REPLACE VIEW v_pipeline_gate", 1)[1]
    assert "st.provider IS NULL AND b.require_positive_balance" in gate, (
        "a provider that has never been probed must not be allowed to run")
