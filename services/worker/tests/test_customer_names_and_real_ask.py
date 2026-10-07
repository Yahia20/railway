"""027 (a customer has a name) and the real-ask panel.

Every assertion here is a bug that already happened, or the exact shape of one
that did. The two families share a root cause worth naming once:

    A column exists, nothing writes it, and no query fails.

`customers.display_name` sat empty from 002 to 027. `interaction_analysis
.customer_name` was in no INSERT while pass 1 filled `raw_response` with the
value it was meant to hold. `interaction_destinations` is empty for the same
reason and is the trap the real-ask panel had to walk around. None of the three
produced an error, a warning, or a failing query — they produced a dashboard
that identified human beings by phone number and a funnel that could not be
built.

Nothing in here needs a database.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / "n8n" / "workflows"
MIGRATION = REPO / "db" / "migrations" / "027_customer_names.sql"
REPORT_PY = Path(__file__).resolve().parents[1] / "app" / "report.py"
REPORT_HTML = Path(__file__).resolve().parents[1] / "app" / "static" / "report.html"


def _wf(name: str) -> dict:
    return json.loads((WORKFLOWS / name).read_text(encoding="utf-8"))


def _node_sql(wf: dict, node_name: str) -> str:
    for n in wf["nodes"]:
        if n["name"] == node_name:
            return n.get("parameters", {}).get("query", "")
    raise AssertionError(f"node {node_name!r} not found in workflow")


# ---------------------------------------------------------------------------
# The field nobody asked for
# ---------------------------------------------------------------------------

def test_contact_select_covers_every_field_the_sql_reads():
    """The bug, generalised: `c->>'NAME'` on a field absent from the select list
    is NULL, not an error.

    This is the contact-side twin of
    `test_deal_select_covers_every_field_the_sql_reads`. The deal version exists
    because CLOSED was missing and every row of every batch failed loudly. The
    contact version exists because NAME was missing and nothing failed at all —
    which cost far more, for far longer.
    """
    from app.main import CONTACT_SELECT

    sql = _node_sql(_wf("04-nightly-housekeeping.json"), "Store contact names")
    read = set(re.findall(r"c->>'([A-Z_0-9]+)'", sql))
    missing = sorted(read - set(CONTACT_SELECT))
    assert not missing, (
        f"'Store contact names' reads {missing} from the contact object but "
        f"CONTACT_SELECT does not request them, so they arrive NULL")


def test_contact_select_still_asks_for_the_phone():
    """The name is an addition, not a replacement. `Pair contact to phone` and
    the whole identity chain behind it read PHONE off the same response."""
    from app.main import CONTACT_SELECT

    assert "PHONE" in CONTACT_SELECT
    assert "ID" in CONTACT_SELECT


def test_a_refetch_cannot_erase_a_stored_name():
    """If the select list is ever narrowed again, the fields stop arriving and
    arrive as NULL. The failure mode of that must be "we learn nothing new",
    never "we forget what we knew"."""
    sql = _node_sql(_wf("04-nightly-housekeeping.json"), "Store contact names")
    for col in ("name", "second_name", "last_name", "phone_raw"):
        assert f"{col}" in sql and f"coalesce(EXCLUDED.{col}" in sql, (
            f"{col} must coalesce onto the stored value on conflict, so an "
            f"absent field cannot blank a name we already have")


# ---------------------------------------------------------------------------
# Who may overwrite whom
# ---------------------------------------------------------------------------

def test_migration_declares_all_four_name_sources():
    """002's CHECK named four sources before anything could fill them. All four
    have to exist in the rank table or the CHECK rejects the write."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for source in ("manual", "crm_contact", "deal_title", "ai_extracted"):
        assert f"'{source}'" in sql, f"{source} missing from 027"


def test_manual_outranks_every_automated_source():
    """A human who corrected a name looked at something no automated source can
    see. A nightly job must never undo that."""
    sql = MIGRATION.read_text(encoding="utf-8")
    ranks = dict(re.findall(r"\('(\w+)',\s+(\d+),", sql))
    assert ranks, "name_source_rank seed not found"
    assert int(ranks["manual"]) == 0
    assert int(ranks["manual"]) < int(ranks["crm_contact"]) < \
           int(ranks["deal_title"]) < int(ranks["ai_extracted"]), (
        "the precedence must be manual > crm_contact > deal_title > ai_extracted")


def test_every_name_write_is_guarded_against_demotion():
    """Three UPDATEs, three guards. A missing one lets tonight's run replace a
    CRM name with a guess, and it would look like the name simply changed."""
    sql = MIGRATION.read_text(encoding="utf-8")
    fn = sql.split("CREATE OR REPLACE FUNCTION resolve_customer_names", 1)[1]
    fn = fn.split("$fn$;", 1)[0]
    updates = fn.count("UPDATE customers c")
    guards = fn.count("FROM name_source_rank r")
    assert updates == 3, f"expected three name sources, found {updates} UPDATEs"
    assert guards == updates, (
        f"{updates} UPDATEs but {guards} rank guards — an unguarded write can "
        f"demote a name to a worse source")


def test_deal_title_is_off_by_default():
    """84% of titles yield a usable name, which is a good number and not a
    guarantee: "Dunes Exhibition" passes every test the extractor applies.
    Turning it on is an argument, not a deploy."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "p_use_deal_title boolean DEFAULT false" in sql


def test_deal_title_splits_on_the_first_separator():
    """THE BUG THIS TEST EXISTS FOR, measured on 17,709 real titles.

    A Bitrix deal title is three fields, not one:

        Abu Hassoun  -  WhatsApp API Bureau Gupshup  -  Travel Gate

    Split on the LAST separator — the obvious reading — and you keep
    "Abu Hassoun - WhatsApp API Bureau Gupshup", seven words, which any
    word-count filter then throws away as a sentence. Meanwhile ". - Travel
    Gate" is four words and sails through. The first version of this migration
    did exactly that: it accepted 6% of titles and they were mostly the
    placeholders. Splitting on the FIRST separator inverts it to 84% usable.
    """
    code = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))
    assert "split_part(" in code, "the extractor must split, not word-count"
    assert "' - ', 1)" in code, (
        "split_part(..., ' - ', 1) takes the FIRST field. Anything that takes "
        "the last one keeps the source channel and loses the name")
    assert not re.search(r"BETWEEN 2 AND 4", code), (
        "the word-count filter was the bug; it must not come back")


def test_deal_title_extraction_is_defined_once():
    """`resolve_customer_names` writes the name and `v_customer_name_candidates`
    previews it. Two copies of the parsing rule is how a preview stops
    predicting what the write will do."""
    code = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))
    assert "CREATE OR REPLACE FUNCTION deal_title_name" in code
    assert code.count("deal_title_name(") >= 4, (
        "both the resolver and the candidates view must call the function")


def test_deal_title_extractor_is_immutable():
    """It is called inside a WHERE on every deal and belongs in an index later.
    A STABLE function there is a sequential scan nobody asked for."""
    code = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))
    fn = code.split("CREATE OR REPLACE FUNCTION deal_title_name", 1)[1]
    assert "IMMUTABLE" in fn.split("$fn$", 1)[0]


def test_the_generated_name_column_uses_only_immutable_functions():
    """`concat_ws` is STABLE, not IMMUTABLE — Postgres refuses it in a generated
    column with "generation expression is not immutable", and the migration
    fails at CREATE TABLE. This caught it before it ever ran."""
    code = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))
    assert "concat_ws" not in code
    gen = code.split("GENERATED ALWAYS AS", 1)[1].split("STORED", 1)[0]
    called = set(re.findall(r"([a-z_]+)\s*\(", gen))
    assert called <= {"nullif", "btrim", "regexp_replace", "coalesce"}, sorted(called)


def test_an_uncertain_name_is_never_promoted():
    """Pass 1 puts "customer.name" in uncertain_fields when it could not tell
    the two speakers apart, and the prompt is explicit that a wrong name is far
    worse than a missing one: it creates a person who does not exist and
    identity resolution then merges real people onto them."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert 'uncertain_fields @> \'["customer.name"]\'::jsonb' in sql


# ---------------------------------------------------------------------------
# The column pass 1 was filling into a black hole
# ---------------------------------------------------------------------------

def test_01d_writes_the_extracted_customer_name():
    """`interaction_analysis.customer_name` has existed since 004 and was in no
    INSERT in any workflow."""
    sql = _node_sql(_wf("01d-chats-evaluate.json"), "Store pass1")
    assert "customer_name" in sql
    assert "p.payload->'customer'->>'name'" in sql, (
        "the value lives under the `customer` object in pass 1's response")
    assert "customer_name        = EXCLUDED.customer_name" in sql, (
        "a re-judged thread must refresh the name too, or the row keeps a value "
        "from a response that no longer exists")


def test_the_name_resolver_runs_in_03_not_04():
    """Both sources are keyed on customer_id, and 03 at 03:40 is what writes it.
    In 04 at 03:20 this would work off last night's resolution and be
    permanently one night behind — the bug 03's own resolver shipped with, and
    the reason link_deal_customers() sits in 03."""
    wf03 = _wf("03-nightly-resolve-and-aggregate.json")
    names = [n["name"] for n in wf03["nodes"]]
    assert "Resolve customer names" in names

    # A CALL, not a mention. 04's `Store contact names` explains in a comment
    # why the resolving happens elsewhere, and a test that cannot tell prose
    # from code would punish the comment for being accurate.
    wf04 = _wf("04-nightly-housekeeping.json")
    for node in wf04["nodes"]:
        sql = node.get("parameters", {}).get("query", "")
        code = re.sub(r"--[^\n]*", "", sql)
        assert "resolve_customer_names(" not in code, (
            f"{node['name']} calls the resolver: naming must not run in 04, "
            f"because the contact->customer mapping it reads is written by 03 "
            f"twenty minutes later")


def test_the_name_resolver_runs_after_identity_and_deal_linking():
    """Order, not just presence. It reads customer_identities rows that
    `Link deal customers` writes and interactions.customer_id that
    `Resolve identity (phone)` writes."""
    wf = _wf("03-nightly-resolve-and-aggregate.json")
    conns = wf["connections"]

    def reaches(src: str, dst: str, seen=None) -> bool:
        seen = seen or set()
        if src in seen:
            return False
        seen.add(src)
        for out in conns.get(src, {}).get("main", []):
            for c in out:
                if c["node"] == dst or reaches(c["node"], dst, seen):
                    return True
        return False

    assert reaches("Resolve identity (phone)", "Resolve customer names")
    assert reaches("Link deal customers", "Resolve customer names")


def test_the_name_resolver_runs_once_per_tick():
    """A function call that scans whole tables, downstream of a node that
    returns many rows. `job_runs` once held 2,622 copies of one row for exactly
    this reason."""
    wf = _wf("03-nightly-resolve-and-aggregate.json")
    node = next(n for n in wf["nodes"] if n["name"] == "Resolve customer names")
    assert node.get("executeOnce") is True


def test_storing_contact_names_runs_once_per_tick():
    """Same rule, and check_workflow_json cannot see this one: the SQL carries a
    `$1`, so the checker treats it as parameterised per item. The parameter is
    the same whole array for every input item, which makes it whole-table work
    wearing a placeholder."""
    wf = _wf("04-nightly-housekeeping.json")
    node = next(n for n in wf["nodes"] if n["name"] == "Store contact names")
    assert node.get("executeOnce") is True


def test_storing_contact_names_does_not_break_the_phone_chain():
    """`Normalise phones` reads `$json.values` straight off `Pair contact to
    phone`. A Postgres node inserted between them replaces $json with
    {success:true} and the phone backfill silently stops — gotcha 5, which has
    already caused three separate confusing failures in this repo."""
    wf = _wf("04-nightly-housekeeping.json")
    conns = wf["connections"]

    def targets(src: str) -> list[str]:
        return [c["node"] for out in conns.get(src, {}).get("main", []) for c in out]

    assert targets("Pair contact to phone") == ["Normalise phones"], (
        "nothing may come between the pairing node and the normaliser")
    assert "Store contact names" in targets("Fetch Bitrix contacts"), (
        "the name branch hangs off the HTTP node, beside the phone chain")


# ---------------------------------------------------------------------------
# What makes an ask real
# ---------------------------------------------------------------------------

def _report_src() -> str:
    return REPORT_PY.read_text(encoding="utf-8")


def test_real_ask_is_defined_exactly_once():
    """Three panels count real asks. A second copy of the predicate is how the
    agent table and the headline start disagreeing by one."""
    src = _report_src()
    assert src.count("_REAL_ASK = ") == 1
    assert src.count(".format(real_ask=_REAL_ASK)") == 3, (
        "every query that counts a real ask must bind the shared fragment")


def test_real_ask_requires_all_three_fields():
    """The client's definition: destination AND travellers AND date. Dropping
    any one of them turns "serious enquiry" into "said something"."""
    from app import report

    frag = report._REAL_ASK
    assert "rq.destination" in frag
    assert "rq.travelers_total IS NOT NULL" in frag
    assert "rq.date_start      IS NOT NULL" in frag or "rq.date_start IS NOT NULL" in frag


def test_real_ask_reads_both_the_requests_and_the_primary_row():
    """BOTH, and the second one is what actually fires.

    Rule 9 says a conversation can hold more than one request and
    `interaction_requests` keeps every one, so that table is checked first: when
    it fires it is the more precise answer. But it holds 2 rows for 46 analyses
    — pass 1 emits `requests[]` only when it sees more than one distinct ask,
    and on this corpus it almost never does. Reading it alone returned ZERO real
    asks out of 46, which is not a finding, it is an empty table. The real
    number, 14, was in the primary-request columns.

    Dropping either branch loses conversations silently.
    """
    from app import report

    assert "interaction_requests" in report._REAL_ASK
    assert "interaction_analysis" in report._REAL_ASK
    assert report._REAL_ASK.count("EXISTS") >= 3, (
        "two independent branches, one of which nests a destination check")


def test_real_ask_excludes_invented_requests():
    """Rule 9: a request whose quote was not found verbatim is kept for audit
    and excluded from every count, because an invented request sends a
    salesperson after a customer who never asked."""
    from app import report

    assert "evidence_valid IS NOT false" in report._REAL_ASK


def test_every_table_the_real_ask_reads_has_a_writer():
    """THE GENERAL FORM OF THE BUG THIS WHOLE FILE IS ABOUT.

    `customers.display_name`, `interaction_analysis.customer_name`, the nine
    trip columns and `interaction_destinations` were all defined, all correct,
    and all written by nothing. None of them produced an error. A metric built
    on a table nobody writes reports zero forever and reads as a finding — which
    is exactly what "0 real asks out of 46" looked like until somebody checked.

    So: every table the predicate reads must be written by some workflow. This
    is the assertion that would have caught it on day one.
    """
    from app import report

    tables = set(re.findall(r"FROM\s+(\w+)", report._REAL_ASK))
    assert tables, "the predicate reads no table at all?"
    workflows = "\n".join(p.read_text(encoding="utf-8")
                          for p in sorted(WORKFLOWS.glob("*.json")))
    for table in tables:
        assert f"INSERT INTO {table}" in workflows, (
            f"the real-ask predicate reads {table} and no workflow writes it. "
            f"That metric will report zero forever and look like a finding")


def test_the_denominator_travels_with_the_count():
    """Rule 2, moved out of the rubric and into a sales report.

    "2 real asks" beside 35 customers reports 33 time-wasters where the truth is
    that 31 were never looked at. `analysed` must be in the same row so the page
    can render the pair, and it must be the number the page divides by.
    """
    from app import report

    assert "AS analysed" in report.SQL_AGENT_COMMERCIAL
    assert "AS real_asks" in report.SQL_AGENT_COMMERCIAL

    html = REPORT_HTML.read_text(encoding="utf-8")
    assert "askShare" in html
    share = html.split("function askShare", 1)[1].split("\n}", 1)[0]
    assert "analysed" in share, "the renderer must divide by what was analysed"
    assert "مقروء" in share, "the denominator has to be visible, not implied"


def test_an_agent_with_nothing_analysed_shows_no_number():
    """Not a zero. A zero says "we read their conversations and none were
    serious"; a dash says "nobody has read them". Collapsing the two is the
    whole failure."""
    html = REPORT_HTML.read_text(encoding="utf-8")
    share = html.split("function askShare", 1)[1].split("\n}", 1)[0]
    assert "Number(analysed) === 0" in share
    assert '"—"' in share or "—" in share


def test_the_bot_is_not_in_the_sales_ranking():
    """Bitrix user 1 sat top of v_agent_scorecard for a month on 1,157 turns it
    never sent to a customer. `is_bot` is the flag that ended that."""
    from app import report

    assert "is_bot IS NOT TRUE" in report.SQL_AGENT_COMMERCIAL


def test_deals_are_not_clipped_to_the_report_window():
    """A deal opened in June and won in September belongs to the agent who won
    it. Windowing it would empty the column that exists to show sales."""
    from app import report

    deals_cte = report.SQL_AGENT_COMMERCIAL.split("per_agent_deals AS (", 1)[1]
    deals_cte = deals_cte.split(")", 1)[0]
    assert "%(days)s" not in deals_cte


# ---------------------------------------------------------------------------
# The follow-up queue
# ---------------------------------------------------------------------------

def test_the_followup_queue_puts_the_worst_first():
    """A missed promise is a customer already let down; an open one can still be
    reached in time. Sorting by date buries the first under the second."""
    from app import report

    order = report.SQL_FOLLOWUPS.split("ORDER BY", 1)[1]
    assert order.index("'missed'") < order.index("'open'")


def test_the_followup_queue_carries_the_customer_name():
    """A follow-up queue that says "+9665…" is a queue nobody works from. This
    is most of why 027 exists."""
    from app import report

    assert "c.display_name" in report.SQL_FOLLOWUPS


def test_an_empty_followup_queue_explains_itself():
    """Empty here is almost never "agents keep their promises" — it is "pass 1
    has read 42 conversations". A reader cannot tell those apart from a blank
    table."""
    html = REPORT_HTML.read_text(encoding="utf-8")
    assert "fuHint" in html
    assert "مش لأن مفيش وعود" in html


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("panel", [
    "real_ask_funnel", "agent_commercial", "service_mix",
    "customers", "name_coverage", "followup_totals", "followups",
])
def test_every_new_panel_is_registered(panel):
    """A panel that exists as SQL and is never called is a panel that silently
    does not appear."""
    assert f'_panel("{panel}"' in _report_src()


@pytest.mark.parametrize("element", [
    "askBig", "askNote", "serviceMix", "commercial",
    "nameCoverage", "customers", "fuTotals", "followups",
])
def test_every_new_panel_has_somewhere_to_render(element):
    """The other half: data that arrives and lands nowhere."""
    html = REPORT_HTML.read_text(encoding="utf-8")
    assert f'id="{element}"' in html
