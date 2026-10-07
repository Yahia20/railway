"""Workflow 09 (chat media archive) against the code it must agree with.

Three things live in three places and must not drift:

  * the link rules — Python (`app/media/links.py`, used by the worker and the
    reader) and JavaScript (workflow 09's "Parse links" node, used for
    discovery). A URL one accepts and the other refuses is either a file never
    archived or a download the worker then refuses. The JS is EXECUTED here.
  * the outcome words — the worker's FetchResult and 09's "Record outcome".
  * the status words — migration 026's CHECK and every status 09 writes.

Plus the n8n traps this repo has already paid for (gotchas 4, 5, 15 and the
`}}` truncation): no credentials needed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.media import fetch, links

ROOT = Path(__file__).resolve().parents[3]
WF_PATH = ROOT / "n8n" / "workflows" / "09-chat-media-archive.json"
MIGRATION = ROOT / "db" / "migrations" / "026_chat_media.sql"
BUILDER = ROOT / "scripts" / "build_wf09_media_archive.py"


@pytest.fixture(scope="module")
def wf() -> dict:
    return json.loads(WF_PATH.read_text(encoding="utf-8"))


def node(wf, name):
    return next(n for n in wf["nodes"] if n["name"] == name)


# -- the committed JSON is the builder's output ----------------------------------

def test_committed_json_matches_the_builder():
    sys.path.insert(0, str(BUILDER.parent))
    try:
        import importlib
        builder = importlib.import_module("build_wf09_media_archive")
        importlib.reload(builder)
    finally:
        sys.path.pop(0)
    assert json.loads(WF_PATH.read_text(encoding="utf-8")) == builder.workflow, \
        "run: python scripts/build_wf09_media_archive.py"


# -- link rules: JavaScript == Python ------------------------------------------------

BODIES = [
    "Voice message\nhttps://travelgate.bitrix24.ae/rest/1/tok123/download/?token=disk|a|b",
    "[Attachment: عرض باريس.pdf]\nhttps://travelgate.bitrix24.ae/~Ab12Cd",
    "صورة\nhttps://gupconnector.cultivbureau.com/connector/gupshup-media/eyJ.ZyX.sig",
    "https://filemanager.gupshup.io/wa/app/wa/media/9?download=false",
    "two https://travelgate.bitrix24.ae/~Aa1 and https://travelgate.bitrix24.ae/~Bb2.",
    "dup https://travelgate.bitrix24.ae/~Aa1 https://travelgate.bitrix24.ae/~Aa1",
    "trail https://travelgate.bitrix24.ae/~Ab12Cd، تمام",
    "port443 https://travelgate.bitrix24.ae:443/~Ab12Cd",
    "port8443 https://travelgate.bitrix24.ae:8443/~Ab12Cd",
    "user https://u@travelgate.bitrix24.ae/~Ab12Cd",
    "badport https://travelgate.bitrix24.ae:bad/~Ab12Cd",
    "http http://travelgate.bitrix24.ae/~Ab12Cd",
    "suffix https://travelgate.bitrix24.ae.evil.example/~Ab12Cd",
    "upper https://TravelGate.Bitrix24.AE/~Ab12Cd",
    "dots https://travelgate.bitrix24.ae/~Ab12Cd/../../rest",
    "deal page https://travelgate.bitrix24.ae/crm/deal/details/1/",
    "site https://travelgateksa.com/assets/offer.jpg",
    "meta https://169.254.169.254/latest/meta-data/",
    "",
    "نص عادي من غير روابط",
]


def _run_js(rows: list[dict]) -> dict:
    code = node(json.loads(WF_PATH.read_text(encoding="utf-8")), "Parse links")["parameters"]["jsCode"]
    harness = (
        "var ROWS = " + json.dumps(rows, ensure_ascii=False) + ";\n"
        "var $input = { all: function () { return ROWS.map(function (r) { return { json: r }; }); } };\n"
        "var __r = (function () {\n" + code + "\n})();\n"
    )
    try:
        import quickjs  # the engine the rest of the suite uses
        return json.loads(quickjs.Context().eval(harness + "JSON.stringify(__r[0].json);"))
    except ImportError:
        pass
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("needs `pip install quickjs` or node on PATH")
    out = subprocess.run([node_bin, "-e", harness + "process.stdout.write(JSON.stringify(__r[0].json));"],
                         capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_discovery_parser_agrees_with_python_on_every_case():
    rows = [{"message_id": str(i), "body": b, "content_type": "text", "seen_at": "2026-10-01T10:00:00+00:00"}
            for i, b in enumerate(BODIES)]
    js = _run_js(rows)
    assert js["scanned"] == len(BODIES)
    js_by_msg: dict[str, list] = {}
    for r in js["refs"]:
        js_by_msg.setdefault(r["message_id"], []).append((r["ordinal"], r["url"], r["family"]))
    for i, body in enumerate(BODIES):
        py = [(k, l.url, l.family) for k, l in enumerate(links.find_links(body))]
        assert js_by_msg.get(str(i), []) == py, body
    scans = {s["message_id"]: s["links_found"] for s in js["scans"]}
    assert all(scans[str(i)] == len(links.find_links(b)) for i, b in enumerate(BODIES))


def test_the_parser_reads_the_attachment_name_like_python():
    body = BODIES[1]
    js = _run_js([{"message_id": "7", "body": body, "content_type": "document", "seen_at": "x"}])
    assert js["refs"][0]["file_name"] == links.attachment_name(body)
    assert js["refs"][0]["declared_type"] == "document"


def test_the_empty_item_from_always_output_data_is_skipped():
    assert _run_js([{}]) == {"scans": [], "refs": [], "scanned": 0, "links": 0}


# -- outcome and status words ----------------------------------------------------------

def test_record_outcome_knows_every_worker_outcome():
    sql = node(json.loads(WF_PATH.read_text(encoding="utf-8")), "Record outcome")["parameters"]["query"]
    fetch_src = (ROOT / "services" / "worker" / "app" / "media" / "fetch.py").read_text(encoding="utf-8")
    worker_outcomes = set(re.findall(r'FetchResult\("([a-z_]+)"', fetch_src))
    assert worker_outcomes == {"stored", "expired", "too_large", "not_a_file", "not_allowed", "failed"}
    listed = re.search(r"IN \('stored','expired','too_large','not_a_file',\s*'not_allowed','failed'\)", sql)
    assert listed, "Record outcome must accept exactly the worker's outcome words"


def test_every_status_the_workflow_writes_is_allowed_by_026(wf):
    allowed = set(re.findall(r"'([a-z_]+)',?\s+--", MIGRATION.read_text(encoding="utf-8")))
    allowed &= {"pending", "fetching", "retry_wait", "stored", "recovery_pending", "rejected", "purged"}
    assert len(allowed) == 7
    written = set()
    for n in wf["nodes"]:
        q = n["parameters"].get("query", "")
        written |= set(re.findall(r"status\s*=\s*'([a-z_]+)'", q))
        written |= set(re.findall(r"THEN '([a-z_]+)'", q)) & {
            "pending", "fetching", "retry_wait", "stored", "recovery_pending", "rejected", "purged"}
    written -= {"present", "deleting", "deleted", "on"}   # media_objects.state / config values
    assert written <= allowed, written - allowed


# -- n8n traps ---------------------------------------------------------------------

def _expressions(wf):
    for n in wf["nodes"]:
        def walk(v, path):
            if isinstance(v, str) and v.startswith("="):
                yield n["name"], path, v
            elif isinstance(v, dict):
                for k, x in v.items():
                    yield from walk(x, path + "/" + k)
            elif isinstance(v, list):
                for i, x in enumerate(v):
                    yield from walk(x, f"{path}/{i}")
        yield from walk(n["parameters"], "")


def test_no_expression_is_truncated_by_a_literal_double_brace(wf):
    for name, path, expr in _expressions(wf):
        m = re.match(r"^=\s*\{\{(.*)\}\}\s*$", expr, re.S)
        if m:
            assert "}}" not in m.group(1), f"{name}{path}: '}}}}' ends the expression early"


def test_query_replacements_use_the_array_form(wf):
    for n in wf["nodes"]:
        rep = n["parameters"].get("options", {}).get("queryReplacement")
        if rep:
            assert rep.startswith("={{ ["), f"{n['name']}: gotcha 4, commas split the parameters"


def test_every_node_reference_exists(wf):
    names = {n["name"] for n in wf["nodes"]}
    for name, path, expr in _expressions(wf):
        for ref in re.findall(r"\$\('([^']+)'\)", expr):
            assert ref in names, f"{name}{path} refers to missing node {ref!r}"


def test_execution_data_is_never_saved(wf):
    s = wf["settings"]
    assert s["saveDataSuccessExecution"] == "none" and s["saveDataErrorExecution"] == "none", \
        "claimed rows carry source URLs; bitrix_rest URLs carry a live REST token"
    assert s["timezone"] == "Asia/Riyadh"


def test_downloads_run_one_at_a_time_through_a_loop(wf):
    """The HTTP node's own batching only spaces out request STARTS and awaits
    them together (round-3 review), so it must not be relied on: the fetch is
    fed one item per iteration by a splitInBatches loop that waits for the
    record before handing out the next."""
    n = node(wf, "Download through worker")
    assert n["onError"] == "continueRegularOutput"
    assert "batching" not in n["parameters"]["options"]
    assert "X-API-Key" in json.dumps(n["parameters"]["headerParameters"])
    loop = node(wf, "Each download")
    assert loop["type"] == "n8n-nodes-base.splitInBatches" and loop["parameters"]["batchSize"] == 1
    conns = wf["connections"]
    assert conns["Each download"]["main"][1][0]["node"] == "Download through worker"   # loop output
    assert conns["Record outcome"]["main"][0][0]["node"] == "Each download"            # back round
    assert "$('Each download').item.json" in node(wf, "Record outcome")["parameters"]["options"]["queryReplacement"]


def test_the_serial_batch_fits_the_claim_and_the_execution(wf):
    timeout_s = node(wf, "Download through worker")["parameters"]["options"]["timeout"] / 1000
    batch = int(re.search(r"\('fetch_batch', '(\d+)'\)", MIGRATION.read_text(encoding="utf-8")).group(1))
    claim = node(wf, "Claim downloads")["parameters"]["query"]
    assert "interval '5 minutes'" in claim
    assert batch * timeout_s + 30 < wf["settings"]["executionTimeout"] <= 5 * 60
    assert "interval '6 minutes'" in node(wf, "Take run lease")["parameters"]["query"]


def test_a_delete_that_did_not_answer_stops_this_runs_fetches(wf):
    """It may still be running in the worker; a fetch now could race it."""
    conns = wf["connections"]
    assert conns["Delete from bucket"]["main"][0][0]["node"] == "Delete answered?"
    assert conns["Delete answered?"]["main"][1][0]["node"] == "Release run lease"


def test_an_elapsed_claim_cannot_finish(wf):
    assert "j.claim_until > now()" in node(wf, "Record outcome")["parameters"]["query"]


def test_no_answer_pauses_without_spending_attempts(wf):
    q = node(wf, "Record outcome")["parameters"]["query"]
    assert "WHEN d.outcome = 'no_answer' THEN greatest(j.attempts - 1, 0)" in q


def test_a_claim_is_gated_on_its_type_not_truthiness(wf):
    """Gotcha 15: an error item must never look like a claimed job."""
    cond = node(wf, "Claimed one?")["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert "typeof $json.claim_token === 'string'" in cond


def test_the_run_lease_is_always_released(wf):
    conns = wf["connections"]
    rel = {"node": "Release run lease", "type": "main", "index": 0}
    assert rel in conns["Claimed one?"]["main"][1]          # nothing claimed
    assert rel in conns["Each download"]["main"][0]         # loop done
    assert rel in conns["Delete answered?"]["main"][1]      # delete unconfirmed


def test_retention_runs_before_any_fetch(wf):
    order = ["Take run lease", "Retention", "Find unexamined messages", "Claim downloads",
             "Download through worker"]
    xs = [node(wf, n)["position"][0] for n in order]
    assert xs == sorted(xs)
    assert wf["connections"]["Lease taken?"]["main"][0][0]["node"] == "Retention"
