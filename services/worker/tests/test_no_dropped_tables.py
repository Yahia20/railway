"""Nothing that runs may still name a table 023 dropped.

023 removed `transcripts`, `call_ingest_jobs` and `asr_runs`. Two references
survived it, both invisible until they executed: the nightly health check in
workflow 04 and purge_raw_content() from 017. PL/pgSQL resolves a table when a
statement first runs, not when the function is created, so the migration
applied cleanly and retention then failed every night from 2026-09-16 with
`relation "transcripts" does not exist` — rolling back the chat deletes that
ran before it. 025 redefined the function; these tests keep it that way.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DROPPED = ("transcripts", "call_ingest_jobs", "asr_runs")
WORD = re.compile(r"\b(" + "|".join(DROPPED) + r")\b")


def _sql_without_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


@pytest.mark.parametrize("path", sorted((ROOT / "n8n" / "workflows").glob("*.json")),
                         ids=lambda p: p.name)
def test_no_workflow_queries_a_dropped_table(path):
    wf = json.loads(path.read_text(encoding="utf-8"))
    for node in wf["nodes"]:
        query = node.get("parameters", {}).get("query")
        if isinstance(query, str):
            hits = WORD.findall(_sql_without_comments(query))
            assert not hits, f"{path.name} / {node['name']} still reads {sorted(set(hits))}"


def _latest_definitions() -> dict[str, tuple[str, str]]:
    """function name -> (migration file, body) for the LAST CREATE OR REPLACE."""
    found: dict[str, tuple[str, str]] = {}
    pattern = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+([a-z_][a-z0-9_]*)\s*\(.*?\$(\w*)\$(.*?)\$\2\$",
        re.S | re.I)
    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        for m in pattern.finditer(path.read_text(encoding="utf-8")):
            found[m.group(1).lower()] = (path.name, m.group(3))
    return found


# Two functions still name a dropped table in their literal source, and each
# has a reason that is checked below rather than taken on trust.
#   evaluate_alert_rules        023 rewrites it in place from pg_get_functiondef
#                               and RAISEs if `transcripts` survives the patch.
#   reconcile_alert_evaluations its only caller was workflow 02, which 023
#                               removed. Dead, so it can never execute.
PATCHED_IN_PLACE = {"evaluate_alert_rules": "023_remove_calls.sql"}
DEAD = {"reconcile_alert_evaluations"}


def test_no_live_function_body_reads_a_dropped_table():
    offenders = {
        name: (file, sorted(set(WORD.findall(_sql_without_comments(body)))))
        for name, (file, body) in _latest_definitions().items()
        if WORD.search(_sql_without_comments(body))
        and name not in PATCHED_IN_PLACE and name not in DEAD
    }
    assert not offenders, offenders


def test_the_in_place_patch_still_refuses_to_leave_transcripts_behind():
    for name, file in PATCHED_IN_PLACE.items():
        sql = (ROOT / "db" / "migrations" / file).read_text(encoding="utf-8")
        assert f"p.proname = '{name}'" in sql
        assert "still references transcripts after patching" in sql


@pytest.mark.parametrize("name", sorted(DEAD))
def test_dead_functions_have_no_caller(name):
    callers = [p.name for p in (ROOT / "n8n" / "workflows").glob("*.json")
               if f"{name}(" in p.read_text(encoding="utf-8")]
    callers += [p.name for p in (ROOT / "services" / "worker" / "app").rglob("*.py")
                if f"{name}(" in p.read_text(encoding="utf-8")]
    assert not callers, f"{name} reads a dropped table and is called from {callers}"


def test_purge_keeps_the_signature_workflow_04_calls():
    file, body = _latest_definitions()["purge_raw_content"]
    assert file >= "025", file
    wf = json.loads((ROOT / "n8n" / "workflows" / "04-nightly-housekeeping.json")
                    .read_text(encoding="utf-8"))
    calls = [n["parameters"]["query"] for n in wf["nodes"]
             if "purge_raw_content(" in n.get("parameters", {}).get("query", "")]
    assert calls and "purge_raw_content(90, 365, false)" in calls[0]
