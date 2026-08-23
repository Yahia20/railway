#!/usr/bin/env python3
"""Static checks for an exported n8n workflow.

Every rule in here corresponds to a failure this project actually hit. n8n
gives you no compile step: a workflow with a typo'd node reference, a Postgres
mutation that silently collapses 25 items into one `{success:true}`, or an
expression reaching into a node that did not run on this branch, imports
cleanly, activates cleanly, and then produces wrong data quietly. This script
is the compile step.

    python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json
    python scripts/check_workflow_json.py n8n/workflows/*.json --dump-sql scripts/sql

Exit status is 1 if any ERROR was reported; warnings alone exit 0.

SQL parsing is optional. `pip install sqlglot` and every embedded query is
parsed with the Postgres dialect as well; without it that check is skipped and
says so, rather than pretending the SQL was verified.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# The rules, in one place, so a reviewer can read them without reading the code.
# ---------------------------------------------------------------------------
POSTGRES_TYPE = "n8n-nodes-base.postgres"
MUTATION_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE | re.MULTILINE)
RETURNING_RE = re.compile(r"\bRETURNING\b", re.IGNORECASE)
NODE_REF_RE = re.compile(r"\$\(\s*(['\"])(?P<name>.*?)\1\s*\)")
TEMPLATE_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)

# A node that branches. Only these count as an explicit success gate.
GATE_TYPES = ("n8n-nodes-base.if", "n8n-nodes-base.switch")
# `$json` on a node fed by a conditional write is the dangerous read: when the
# write matched nothing, n8n substitutes a `{success:true}` placeholder and the
# field the expression wanted is simply absent.
JSON_REF_RE = re.compile(r"\$json\b")
# A statement that can match nothing. `ON CONFLICT ... DO UPDATE` with no WHERE
# always returns its row, so it is NOT conditional -- calling it one would
# demand a gate on every plain upsert in the repo and teach people to ignore
# this check.
WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
ON_CONFLICT_DO_NOTHING_RE = re.compile(r"\bON\s+CONFLICT\b.*?\bDO\s+NOTHING\b",
                                       re.IGNORECASE | re.DOTALL)
SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# Functions that write even though the statement calling them reads as a SELECT.
MUTATING_FUNCTION_RE = re.compile(
    r"\b(evaluate_alert_rules|reconcile_alert_evaluations)\s*\(", re.IGNORECASE)
# An opt-out that has to be written next to the SQL it excuses, with a reason.
LEASE_EXEMPT_RE = re.compile(r"--\s*lease-exempt:", re.IGNORECASE)
LEASE_TOKEN = "claim_token"
# A fence is only atomic if it LOCKS the job row. `FOR UPDATE` is how.
FOR_UPDATE_RE = re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE)
# The opt-out for a fenced write that needs no explicit lock, written next to
# the SQL it excuses, with a reason -- same discipline as lease-exempt.
FENCE_EXEMPT_RE = re.compile(r"--\s*fence-exempt:", re.IGNORECASE)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def walk_strings(value, path="") -> list[tuple[str, str]]:
    """Every string in a nested structure, with a dotted path to it."""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        out.append((path, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(walk_strings(v, "%s.%s" % (path, k) if path else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(walk_strings(v, "%s[%d]" % (path, i)))
    return out


def expression_strings(node: dict) -> list:
    """Every string in a node's parameters that n8n could evaluate.

    Comments inside a Code node's jsCode are stripped first: naming a node in a
    comment is documentation, and flagging it would make the checks below
    punish the explanation of the very bug they exist to prevent.
    """
    params = dict(node.get("parameters") or {})
    if node.get("type") == "n8n-nodes-base.code" and isinstance(params.get("jsCode"), str):
        params["jsCode"] = strip_js_comments(params["jsCode"])
    return walk_strings(params, node.get("name") or "?")


def outgoing(connections: dict, name: str) -> list[tuple[int, str]]:
    """[(output_index, target_name)] for one node."""
    out = []
    for idx, group in enumerate(connections.get(name, {}).get("main", []) or []):
        for target in group or []:
            out.append((idx, target.get("node")))
    return out


def reachable(connections: dict, starts: list[str]) -> set:
    seen, stack = set(), list(starts)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        for _, t in outgoing(connections, n):
            if t not in seen:
                stack.append(t)
    return seen


def sql_of(node: dict) -> str | None:
    if node.get("type") != POSTGRES_TYPE:
        return None
    return (node.get("parameters") or {}).get("query")


def strip_sql_comments(sql: str) -> str:
    """Drop `--` comments. Every query in this repo carries a long explanatory
    header, and matching keywords or column names inside prose is how a static
    check earns its reputation for crying wolf."""
    return SQL_LINE_COMMENT_RE.sub("", sql)


def is_conditional(sql: str) -> bool:
    """Can this statement match nothing, and so return nothing?"""
    body = strip_sql_comments(sql)
    return bool(WHERE_RE.search(body)) or bool(ON_CONFLICT_DO_NOTHING_RE.search(body))


def strip_js_comments(code: str) -> str:
    """Drop // and /* */ comments. A node named in a comment is documentation,
    not a reference, and must not trip the expression checks."""
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", code)


def count_array_elements(expression: str) -> int | None:
    """How many top-level values an `={{ [ a, b, c ] }}` expression supplies.

    Commas inside nested calls, brackets or string literals do not count --
    `JSON.stringify({a: 1, b: 2})` is ONE parameter, and miscounting it is how
    you conclude a correct node is broken.
    """
    m = re.match(r"^\s*=\s*\{\{(?P<body>.*)\}\}\s*$", expression, re.DOTALL)
    if not m:
        return None
    body = m.group("body").strip()
    if not (body.startswith("[") and body.endswith("]")):
        return None
    inner = body[1:-1]
    if not inner.strip():
        return 0
    depth, elements = 0, 1
    in_str, quote, escaped = False, "", False
    for ch in inner:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
        elif ch in "'\"`":
            in_str, quote = True, ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            elements += 1
    return elements


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_names(nodes: list, rep: Report) -> None:
    """Node names must be unique: n8n addresses nodes by NAME in $('...')."""
    seen = defaultdict(int)
    for n in nodes:
        seen[n.get("name")] += 1
    for name, count in seen.items():
        if count > 1:
            rep.error("duplicate node name %r (%d nodes) -- $('%s') is ambiguous"
                      % (name, count, name))
    ids = defaultdict(int)
    for n in nodes:
        ids[n.get("id")] += 1
    for node_id, count in ids.items():
        if count > 1:
            rep.error("duplicate node id %r (%d nodes)" % (node_id, count))


def check_connections(nodes: list, connections: dict, rep: Report) -> None:
    """Every endpoint of every connection must be a node that exists."""
    names = {n.get("name") for n in nodes}
    for source, spec in connections.items():
        if source not in names:
            rep.error("connections reference unknown source node %r" % source)
        for idx, group in enumerate(spec.get("main", []) or []):
            for target in group or []:
                if target.get("node") not in names:
                    rep.error("connection %s[output %d] -> unknown node %r"
                              % (source, idx, target.get("node")))

    # Orphans: everything except the trigger should be reachable from a trigger.
    triggers = [n["name"] for n in nodes
                if "trigger" in (n.get("type") or "").lower()
                or (n.get("type") or "").endswith("webhook")]
    if triggers:
        live = reachable(connections, triggers)
        for n in nodes:
            if n["name"] not in live:
                rep.error("node %r is not reachable from any trigger -- it will "
                          "never run" % n["name"])
    else:
        rep.warn("no trigger node found; skipped the reachability check")


def check_node_references(nodes: list, rep: Report) -> None:
    """$('Some node') must name a node that exists."""
    names = {n.get("name") for n in nodes}
    for n in nodes:
        for path, text in expression_strings(n):
            for m in NODE_REF_RE.finditer(text):
                if m.group("name") not in names:
                    rep.error("%s references $('%s'), which is not a node in this "
                              "workflow" % (path, m.group("name")))


def check_returning(nodes: list, connections: dict, rep: Report) -> None:
    """Per-item Postgres mutations must RETURN correlation fields.

    A Postgres node whose statement returns no rows emits a single
    `{success:true}` no matter how many items went in. That collapses N items
    to 1, so every downstream `$('Node').item` loses its pairing and reads the
    wrong row -- or nothing. The fix is always the same: RETURNING.
    """
    for n in nodes:
        sql = sql_of(n)
        if not sql or not MUTATION_RE.search(sql):
            continue
        if RETURNING_RE.search(sql):
            continue
        downstream = [t for _, t in outgoing(connections, n["name"])]
        if downstream:
            rep.error("%s is a Postgres mutation with no RETURNING and feeds %s -- "
                      "N items collapse to one {success:true} and item pairing "
                      "breaks" % (n["name"], ", ".join(repr(d) for d in downstream)))
        else:
            rep.warn("%s is a Postgres mutation with no RETURNING. It is a leaf so "
                     "nothing depends on its output, but the run log will show "
                     "{success:true} instead of what changed" % n["name"])


def check_returning_gates(nodes: list, connections: dict, rep: Report) -> None:
    """A conditional RETURNING needs an explicit success gate.

    `RETURNING` fixes item pairing only while the statement actually matches a
    row. Every fenced write in workflow 02 is conditional by design -- it
    carries `AND claim_token = $token` precisely so that it can match nothing --
    and n8n answers "nothing" with a `{success:true}` placeholder that looks
    like success to every downstream node.

    So: if a conditional mutation with RETURNING feeds a node that reads
    `$json`, the next node must be an IF/Switch that can send the zero-row case
    somewhere harmless. Feeding a node that does NOT read `$json` is fine and is
    reported as a note, because that node cannot be misled by the placeholder --
    it is reading `$('Some node')` or constants instead.
    """
    by_name = {n["name"]: n for n in nodes}
    gated = ungated = 0
    for n in nodes:
        sql = sql_of(n)
        if not sql or not MUTATION_RE.search(sql) or not RETURNING_RE.search(sql):
            continue
        if not is_conditional(sql):
            continue          # INSERT ... VALUES / DO UPDATE always returns a row
        for _, target in outgoing(connections, n["name"]):
            downstream = by_name.get(target)
            if downstream is None:
                continue
            if downstream.get("type") in GATE_TYPES:
                gated += 1
                continue
            reads_json = any(JSON_REF_RE.search(text)
                             for _, text in expression_strings(downstream))
            if reads_json:
                ungated += 1
                rep.error("%s: conditional RETURNING feeds %r, which reads $json "
                          "but is not an IF/Switch. When the statement matches no "
                          "row n8n substitutes {success:true} and that node reads "
                          "an absent field as if it were data. Add an explicit "
                          "success gate." % (n["name"], target))
            else:
                rep.note("%s: conditional RETURNING feeds %r, which does not read "
                         "$json -- no gate required" % (n["name"], target))
    rep.note("returning gates: %d gated, %d ungated" % (gated, ungated))


def check_lease_fencing(nodes: list, rep: Report) -> None:
    """A durable write must carry the lease token, or say why it does not.

    The lease is worthless if only the job-status updates carry it: a worker
    whose lease expired, whose row was recovered and re-claimed by somebody
    else, could still overwrite the new owner's transcript, pass-1 payload or
    evaluation, and would learn about it only from an `UPDATE 0` that nothing
    reads. Every statement that writes durable data must therefore select
    through a matching `uniqueid + claim_token + in-flight status` row.

    Three statements legitimately have no lease to carry -- the discovery
    INSERT, the recovery sweep, and the claim itself. They opt out with a
    `-- lease-exempt: <reason>` comment written next to the SQL, so the reason
    is reviewable in the same place as the exception.

    CARRYING THE TOKEN IS NOT ENOUGH: THE FENCE MUST BE ATOMIC. A fence that
    reads `SELECT 1 FROM call_ingest_jobs WHERE ... claim_token = $t` in a CTE
    and then upserts is a read-then-write with a window in it -- the recovery
    sweep can reclaim the row and a new worker re-claim it between the read and
    the write, and the stale write still commits. That is a blocking review
    finding this project actually shipped once. A multi-statement fence must
    therefore take a real row lock with `FOR UPDATE`.

    A SINGLE-statement fence does not need one: `UPDATE ... WHERE claim_token =
    $t` locks the row and re-evaluates its own predicate against the latest
    committed version by itself. Those declare `-- fence-exempt: <reason>`, in
    the same place and with the same discipline as lease-exempt, so that the
    difference between "atomic by construction" and "nobody thought about it"
    is written down rather than inferred.

    The check is SELF-SCOPING: it only runs on a workflow that takes a lease
    somewhere. Workflows 01, 01b and 03 have no job queue and no lease to
    carry, and demanding one of them would be noise that teaches people to skip
    the validator's output.
    """
    sqls = [sql_of(n) for n in nodes]
    if not any(s and LEASE_TOKEN in strip_sql_comments(s) for s in sqls):
        rep.note("lease fencing: this workflow takes no lease -- check skipped")
        return

    fenced = exempt = locked = fence_exempt = 0
    for n in nodes:
        sql = sql_of(n)
        if not sql:
            continue
        writes = bool(MUTATION_RE.search(sql)) or bool(MUTATING_FUNCTION_RE.search(sql))
        if not writes:
            continue
        if LEASE_EXEMPT_RE.search(sql):
            exempt += 1
            reason = LEASE_EXEMPT_RE.split(sql, 1)[1].strip().splitlines()[0]
            rep.note("%s: lease-exempt -- %s" % (n["name"], reason.strip()))
            continue
        body = strip_sql_comments(sql)
        if LEASE_TOKEN not in body:
            rep.error("%s writes durable data but never mentions %s. A stale "
                      "worker can overwrite the current owner's data here. Fence "
                      "it through a matching lease row, or declare "
                      "'-- lease-exempt: <reason>' in the query."
                      % (n["name"], LEASE_TOKEN))
            continue
        fenced += 1
        if FOR_UPDATE_RE.search(body):
            locked += 1
        elif FENCE_EXEMPT_RE.search(sql):
            fence_exempt += 1
            reason = FENCE_EXEMPT_RE.split(sql, 1)[1].strip().splitlines()[0]
            rep.note("%s: fence-exempt -- %s" % (n["name"], reason.strip()))
        else:
            rep.error("%s carries %s but never locks the job row. Without "
                      "FOR UPDATE the fence is a read-then-write: the recovery "
                      "sweep can reclaim the row and a new worker re-claim it "
                      "between the check and the write, and this stale write "
                      "still commits. Acquire the row with SELECT ... FOR "
                      "UPDATE in the same statement, or declare "
                      "'-- fence-exempt: <reason>' in the query."
                      % (n["name"], LEASE_TOKEN))
    rep.note("lease fencing: %d fenced write(s) (%d locked with FOR UPDATE, "
             "%d fence-exempt), %d declared lease-exempt"
             % (fenced, locked, fence_exempt, exempt))


def check_query_replacement(nodes: list, rep: Report) -> None:
    """queryReplacement must use the ARRAY form.

    n8n splits a comma-separated queryReplacement string on commas, which
    shreds JSON.stringify(...) and Arabic text into dozens of bogus parameters.
    """
    for n in nodes:
        if n.get("type") != POSTGRES_TYPE:
            continue
        params = n.get("parameters") or {}
        qr = (params.get("options") or {}).get("queryReplacement")
        if qr is None:
            continue
        if not isinstance(qr, str) or not re.match(r"^=\{\{\s*\[", qr.strip()):
            rep.error("%s: queryReplacement is not the array form "
                      "(={{ [ a, b ] }}); n8n will split it on commas" % n["name"])
            continue
        sql = params.get("query") or ""
        # $n inside a SQL string literal or a dollar-quoted body is not a
        # parameter. Strip single-quoted literals before counting.
        used = {int(x) for x in re.findall(r"\$(\d+)", re.sub(r"'(?:[^']|'')*'", "''", sql))}
        supplied = count_array_elements(qr)
        if used and supplied is not None and max(used) > supplied:
            rep.error("%s: SQL uses $%d but queryReplacement supplies %d value(s)"
                      % (n["name"], max(used), supplied))


def check_branch_isolation(nodes: list, connections: dict, rep: Report) -> None:
    """A branch may not read from a node on a branch that did not run.

    Workflow 02 claims work in two stages. A job re-claimed for evaluation never
    runs the ASR node, so any expression on that path reaching into
    $('Cohere Arabic ASR') resolves against a node with no run data. The general
    rule: for every Switch/IF output, nodes reachable only from OTHER outputs
    are off-limits to expressions on this one.
    """
    branchers = [n for n in nodes
                 if n.get("type") in ("n8n-nodes-base.switch", "n8n-nodes-base.if")]
    by_name = {n["name"]: n for n in nodes}
    checked = 0
    for b in branchers:
        groups = connections.get(b["name"], {}).get("main", []) or []
        if len(groups) < 2:
            continue
        per_output = []
        for group in groups:
            starts = [t.get("node") for t in (group or [])]
            per_output.append(reachable(connections, starts) if starts else set())
        for i, mine in enumerate(per_output):
            others = set().union(*[s for j, s in enumerate(per_output) if j != i]) \
                if len(per_output) > 1 else set()
            forbidden = others - mine
            if not forbidden:
                continue
            checked += 1
            for node_name in sorted(mine):
                node = by_name.get(node_name)
                if node is None:
                    continue
                for path, text in expression_strings(node):
                    for m in NODE_REF_RE.finditer(text):
                        target = m.group("name")
                        if target in forbidden:
                            rep.error(
                                "%s: reads $('%s'), but %r only runs on another output "
                                "of %r. On this branch that node has no run data."
                                % (path, target, target, b["name"]))
    rep.note("branch isolation: checked %d branch/output pair(s)" % checked)


def check_always_output_data(nodes: list, connections: dict, rep: Report) -> None:
    """A node that can legitimately produce zero rows must not stall its chain."""
    by_name = {n["name"]: n for n in nodes}
    for n in nodes:
        sql = sql_of(n)
        if not sql:
            continue
        downstream = [t for _, t in outgoing(connections, n["name"])]
        if not downstream:
            continue
        # A statement that can match nothing and has no aggregate fallback.
        conditional = bool(MUTATION_RE.search(sql)) or "WHERE" in sql.upper()
        aggregated = re.search(r"\b(coalesce|string_agg|count)\s*\(", sql, re.I)
        if conditional and not aggregated and not n.get("alwaysOutputData"):
            rep.note("%s can return zero rows and feeds %s; it relies on n8n's "
                     "{success:true} placeholder rather than Always Output Data"
                     % (n["name"], ", ".join(repr(d) for d in downstream)))
        _ = by_name


def check_sql_parses(nodes: list, rep: Report) -> None:
    try:
        import sqlglot                      # optional
    except ImportError:
        rep.note("sqlglot not installed (pip install sqlglot) -- SQL was NOT parsed")
        return
    parsed = 0
    for n in nodes:
        sql = sql_of(n)
        if not sql:
            continue
        # n8n expressions are not SQL. Replace them with a literal so the
        # surrounding statement can still be parsed.
        cleaned = TEMPLATE_RE.sub("'__n8n_expr__'", sql)
        try:
            statements = sqlglot.parse(cleaned, read="postgres")
        except Exception as exc:                        # noqa: BLE001
            rep.error("%s: sqlglot could not parse the query: %s" % (n["name"], exc))
            continue
        if not statements:
            rep.error("%s: query parsed to nothing" % n["name"])
            continue
        parsed += 1
    rep.note("sqlglot: parsed %d query/queries with the postgres dialect" % parsed)


# ---------------------------------------------------------------------------
# summary + sql dump
# ---------------------------------------------------------------------------

def summarise(wf: dict) -> str:
    nodes = wf.get("nodes", [])
    connections = wf.get("connections", {})
    kinds = defaultdict(int)
    for n in nodes:
        kinds[(n.get("type") or "?").replace("n8n-nodes-base.", "")] += 1
    edges = sum(len(g or []) for spec in connections.values()
                for g in (spec.get("main") or []))

    lines = ["  nodes: %d   edges: %d" % (len(nodes), edges),
             "  types: " + ", ".join("%s x%d" % (k, v) for k, v in sorted(kinds.items())),
             "  graph:"]
    for n in nodes:
        outs = outgoing(connections, n["name"])
        if not outs:
            lines.append("    %-26s ->" % n["name"])
            continue
        for idx, target in outs:
            lines.append("    %-26s -[%d]-> %s" % (n["name"], idx, target))
    return "\n".join(lines)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def dump_sql(wf: dict, path: str, out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.basename(path).split("-")[0]
    written = []
    for n in wf.get("nodes", []):
        sql = sql_of(n)
        if not sql:
            continue
        target = os.path.join(out_dir, "%s_%s.sql" % (stem, slugify(n["name"])))
        qr = ((n.get("parameters") or {}).get("options") or {}).get("queryReplacement")
        header = [
            "-- GENERATED from %s, node %r." % (path.replace(os.sep, "/"), n["name"]),
            "-- Do not edit here: edit the workflow JSON and re-run",
            "--   python scripts/check_workflow_json.py %s --dump-sql %s"
            % (path.replace(os.sep, "/"), out_dir.replace(os.sep, "/")),
        ]
        if qr:
            header.append("-- $n parameters: %s" % qr)
        header.append("")
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(header) + sql.rstrip() + "\n")
        written.append(target)
    return written


# ---------------------------------------------------------------------------

def check_file(path: str, args) -> bool:
    rep = Report()
    try:
        with open(path, encoding="utf-8") as fh:
            wf = json.load(fh)
    except (OSError, ValueError) as exc:
        print("ERROR  %s: %s" % (path, exc))
        return False

    nodes = wf.get("nodes") or []
    connections = wf.get("connections") or {}
    if not nodes:
        rep.error("no nodes in this workflow")

    check_names(nodes, rep)
    check_connections(nodes, connections, rep)
    check_node_references(nodes, rep)
    check_returning(nodes, connections, rep)
    check_returning_gates(nodes, connections, rep)
    check_lease_fencing(nodes, rep)
    check_query_replacement(nodes, rep)
    check_branch_isolation(nodes, connections, rep)
    check_always_output_data(nodes, connections, rep)
    check_sql_parses(nodes, rep)

    print("=" * 78)
    print("%s   (%s)" % (path.replace(os.sep, "/"), wf.get("name", "unnamed")))
    print("=" * 78)
    print(summarise(wf))
    print()
    for msg in rep.errors:
        print("  ERROR    " + msg)
    for msg in rep.warnings:
        print("  WARNING  " + msg)
    for msg in rep.notes:
        print("  note     " + msg)
    print("\n  %d error(s), %d warning(s)\n" % (len(rep.errors), len(rep.warnings)))

    if args.dump_sql:
        written = dump_sql(wf, path, args.dump_sql)
        print("  wrote %d SQL file(s) to %s" % (len(written), args.dump_sql))
        for w in written:
            print("    " + w.replace(os.sep, "/"))
        print()
    return not rep.errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workflows", nargs="+", help="exported n8n workflow JSON files")
    ap.add_argument("--dump-sql", metavar="DIR",
                    help="also write every embedded Postgres query to DIR, one file "
                         "per node, so the SQL can be reviewed without reading JSON")
    args = ap.parse_args()
    ok = True
    for path in args.workflows:
        ok = check_file(path, args) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
