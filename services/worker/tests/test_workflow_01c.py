"""Static checks on n8n workflow 01c — the store-only chat ingest.

Every assertion here is a mistake that has already cost a broken deploy or a
wrong number on this project, and none of them is visible in the n8n editor
until a real payload arrives:

  * a comma-separated queryReplacement, which n8n splits on every comma in the
    JSON and in the Arabic text (gotcha 4);
  * an expression referring to a node by a name that no longer exists, which
    evaluates to nothing rather than raising (gotcha 5);
  * a respondToWebhook node on a fire-and-forget ingest, which executionOrder
    v1 is free to run after a long branch, leaving the sender with no reply
    (gotcha 12).

The workflow JSON is source code. These run with no credentials.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

WORKFLOW = (Path(__file__).resolve().parents[3]
            / "n8n" / "workflows" / "01c-chats-store-only.json")

WEBHOOK_TYPE = "n8n-nodes-base.webhook"
POSTGRES_TYPE = "n8n-nodes-base.postgres"


@pytest.fixture(scope="module")
def wf() -> dict:
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nodes(wf) -> list[dict]:
    return wf["nodes"]


def _params_text(node: dict) -> str:
    """Only the executable parameters. `notes` is prose and may mention anything."""
    return json.dumps(node.get("parameters", {}), ensure_ascii=False)


def test_node_names_are_unique(nodes):
    names = [n["name"] for n in nodes]
    assert len(names) == len(set(names)), "n8n addresses nodes by name in expressions"


def test_connections_only_reference_real_nodes(wf, nodes):
    names = {n["name"] for n in nodes}
    for source, conn in wf["connections"].items():
        assert source in names, source
        for group in conn["main"]:
            for link in group:
                assert link["node"] in names, link["node"]


def test_expressions_only_reference_real_nodes(nodes):
    """$('Renamed node') silently reads nothing instead of raising."""
    names = {n["name"] for n in nodes}
    for node in nodes:
        for referenced in re.findall(r"\$\('([^']+)'\)", _params_text(node)):
            assert referenced in names, f"{node['name']} references missing {referenced!r}"


def test_query_parameters_use_the_array_form(nodes):
    """A comma-separated queryReplacement is shredded into bogus parameters."""
    for node in nodes:
        if node["type"] != POSTGRES_TYPE:
            continue
        replacement = node["parameters"]["options"]["queryReplacement"]
        assert replacement.startswith("={{ ["), (
            f"{node['name']}: queryReplacement must be an array expression, "
            f"got {replacement[:60]!r}"
        )


def test_every_query_parameter_is_bound(nodes):
    """$3 in the SQL with two values in the array fails only at runtime."""
    for node in nodes:
        if node["type"] != POSTGRES_TYPE:
            continue
        query = node["parameters"]["query"]
        replacement = node["parameters"]["options"]["queryReplacement"]
        highest = max((int(m) for m in re.findall(r"\$(\d+)", query)), default=0)
        supplied = replacement.count(",") + 1 if replacement.strip("={} ") != "[]" else 0
        assert highest <= supplied, (
            f"{node['name']}: SQL uses ${highest} but the array looks shorter"
        )


def test_every_postgres_node_carries_a_credential(nodes):
    for node in nodes:
        if node["type"] == POSTGRES_TYPE:
            assert "postgres" in (node.get("credentials") or {}), node["name"]


def test_the_webhook_acknowledges_on_receipt(nodes):
    """Not a respondToWebhook node: executionOrder v1 can run it last."""
    hooks = [n for n in nodes if n["type"] == WEBHOOK_TYPE]
    assert len(hooks) == 1
    assert hooks[0]["parameters"]["responseMode"] == "onReceived"
    assert hooks[0]["parameters"]["path"] == "travelgate/chat-message"


def test_no_responder_node(nodes):
    types = {n["type"] for n in nodes}
    assert "n8n-nodes-base.respondToWebhook" not in types


def test_it_stores_and_does_not_score(nodes):
    """01c is the store-only path. An HTTP node here means it grew a second job."""
    types = {n["type"] for n in nodes}
    assert "n8n-nodes-base.httpRequest" not in types
    assert "n8n-nodes-base.wait" not in types


def test_counters_come_from_the_stored_rows_not_the_payload(nodes):
    """Accumulating from the payload doubles every counter on a redelivery."""
    refresh = next(n for n in nodes if n["name"] == "Renumber and refresh counters")
    query = refresh["parameters"]["query"]
    assert "FROM chat_messages m" in query
    assert "message_count          = agg.n_total" in query


def test_writes_are_idempotent(nodes):
    """Every insert has to survive the same batch arriving twice."""
    by_name = {n["name"]: n for n in nodes}
    assert "ON CONFLICT (source, payload_hash) DO NOTHING" in \
        by_name["Land raw request"]["parameters"]["query"]
    assert "ON CONFLICT (external_source, external_id) DO UPDATE" in \
        by_name["Upsert conversations"]["parameters"]["query"]
    assert "ON CONFLICT (interaction_id, sender, sent_at, body_hash) DO NOTHING" in \
        by_name["Insert messages (dedup)"]["parameters"]["query"]


def test_it_writes_only_tables_this_project_owns(nodes):
    """n8n owns railway/public and has an `agents` table of its own."""
    allowed = {"raw_events", "interactions", "chat_messages"}
    written = set()
    for node in nodes:
        if node["type"] != POSTGRES_TYPE:
            continue
        query = node["parameters"]["query"]
        written |= set(re.findall(r"(?:INSERT INTO|UPDATE)\s+([a-z_]+)", query))
    assert written <= allowed, f"unexpected write target: {written - allowed}"


def test_the_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    wf = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    for node in wf["nodes"]:
        query = node.get("parameters", {}).get("query")
        if query:
            sqlglot.parse(query, dialect="postgres")


def test_the_normalizer_never_invents_a_timestamp(nodes):
    """A guessed sent_at becomes a response-time metric with nothing marking it."""
    code = next(n for n in nodes if n["type"] == "n8n-nodes-base.code")
    js = code["parameters"]["jsCode"]
    assert "Date.now()" not in js
    assert "new Date()" not in js
    assert "rejected_no_timestamp" in js


def test_the_normalizer_cannot_take_the_delivery_down_with_it(nodes):
    """The webhook already answered 200; a throw here loses the payload."""
    code = next(n for n in nodes if n["type"] == "n8n-nodes-base.code")
    assert "} catch (e) {" in code["parameters"]["jsCode"]
