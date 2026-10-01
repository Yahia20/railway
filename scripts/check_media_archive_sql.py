#!/usr/bin/env python3
"""Run workflow 09's real SQL against a throwaway database, end to end.

The workflow is SQL that n8n executes; nothing in the unit suite can execute
it. This applies every migration to an EMPTY database and then drives the
exact queries in n8n/workflows/09-chat-media-archive.json (and the 026
reader queries) through a full lifecycle with synthetic rows:

    lease -> discover -> parse (the workflow's own JS, via node) -> record
    -> claim -> outcomes (stored / expired / failed / stale claim) -> reclaim
    -> retention -> delete stamping -> lease release -> purge_raw_content (025)

    python scripts/check_media_archive_sql.py --dsn postgresql://postgres@127.0.0.1:5433/c360_scratch

REFUSES any database whose name does not end in `_scratch`. It creates and
drops tables; it must never see production.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

REPO = Path(__file__).resolve().parent.parent
WF = json.loads((REPO / "n8n" / "workflows" / "09-chat-media-archive.json").read_text(encoding="utf-8"))
SQL = {n["name"]: n["parameters"]["query"] for n in WF["nodes"] if "query" in n["parameters"]}
JS = next(n for n in WF["nodes"] if n["name"] == "Parse links")["parameters"]["jsCode"]

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def q(cur, name_or_sql: str, *params):
    """Run a workflow query with n8n's $1..$n placeholders."""
    sql = SQL.get(name_or_sql, name_or_sql)
    sql = re.sub(r"\$(\d+)", lambda m: f"%(p{m.group(1)})s", sql)
    cur.execute(sql, {f"p{i + 1}": v for i, v in enumerate(params)})
    return cur.fetchall() if cur.description else []


def parse_with_workflow_js(rows: list[dict]) -> dict:
    harness = (
        "var ROWS = " + json.dumps(rows, ensure_ascii=False, default=str) + ";\n"
        "var $input = { all: function () { return ROWS.map(function (r) { return { json: r }; }); } };\n"
        "var __r = (function () {\n" + JS + "\n})();\n"
        "process.stdout.write(JSON.stringify(__r[0].json));"
    )
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True,
                         encoding="utf-8", timeout=30, check=True)
    return json.loads(out.stdout)


def _reader_sql() -> dict[str, str]:
    """The reader's query strings, read from source without importing the web
    stack (this check runs with nothing but psycopg installed)."""
    import ast
    tree = ast.parse((REPO / "services" / "worker" / "app" / "media" / "reader.py").read_text(encoding="utf-8"))
    return {t.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name) and t.id.startswith("SQL_")
            and isinstance(node.value, ast.Constant)}


def apply_migrations(conn) -> None:
    for path in sorted((REPO / "db" / "migrations").glob("*.sql")):
        try:
            conn.execute(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"migration {path.name} failed on an empty database: {exc}") from exc
    print(f"  ok    {len(list((REPO / 'db' / 'migrations').glob('*.sql')))} migrations applied to an empty database")


def seed(cur) -> dict:
    iid = str(uuid.uuid4())
    cur.execute("""
      INSERT INTO interactions (interaction_id, channel, external_id, external_source, started_at,
                                ended_at, external_deal_id)
      VALUES (%s, 'other', %s, 'bitrix_chat_api', now() - interval '1 day', now(), '90001')""",
                (iid, str(uuid.uuid4())))
    bodies = [
        ("customer", "image", "صورة التحويل\nhttps://gupconnector.cultivbureau.com/connector/gupshup-media/AAA.sig"),
        ("agent", "document", "[Attachment: عرض.pdf]\nhttps://travelgate.bitrix24.ae/~Pdf111"),
        ("customer", "audio", "Voice message\nhttps://travelgate.bitrix24.ae/rest/1/TOKENX/download/?token=disk|a"),
        ("agent", "text", "تمام، وصلني"),
        ("agent", "document", "[Attachment: عرض.pdf]\nhttps://travelgate.bitrix24.ae/~Pdf111"),  # same URL again
    ]
    ids = []
    for i, (sender, ctype, body) in enumerate(bodies):
        cur.execute("""
          INSERT INTO chat_messages (interaction_id, seq, sender, body, sent_at, content_type)
          VALUES (%s, %s, %s::speaker_role, %s, now() - interval '1 day' + make_interval(mins => %s), %s)
          RETURNING message_id""", (iid, i + 1, sender, body, i, ctype))
        ids.append(cur.fetchone()["message_id"])
    return {"iid": iid, "msg_ids": ids}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    args = ap.parse_args()
    dbname = psycopg.conninfo.conninfo_to_dict(args.dsn).get("dbname", "")
    if not dbname.endswith("_scratch"):
        raise SystemExit(f"refusing to run against {dbname!r}: the database name must end in _scratch")

    with psycopg.connect(args.dsn, autocommit=True, row_factory=dict_row) as conn:
        print("migrations")
        apply_migrations(conn)
        cur = conn.cursor()

        print("\nlease")
        r = q(cur, "Take run lease")[0]
        check(r["acquired"] is False and r["mode"] == "off", "mode 'off' (as deployed) takes no lease")
        cur.execute("UPDATE media_archive_config SET value='on' WHERE key='mode'")
        lease = q(cur, "Take run lease")[0]
        check(lease["acquired"] is True and len(lease["run_token"]) == 36, "mode 'on' takes the lease")
        check(q(cur, "Take run lease")[0]["acquired"] is False, "a second run cannot take a held lease")

        s = seed(cur)

        print("\nretention on a young thread")
        ret = q(cur, "Retention", 90)[0]
        check(ret["refs_expired"] == 0 and ret["objects"] == [] and ret["receipts"] == [], "nothing to delete")

        print("\ndiscovery")
        rows = q(cur, "Find unexamined messages", 300, 90)
        check(len(rows) == 5, "every unexamined message comes back, link or not")
        parsed = parse_with_workflow_js(rows)
        rec = q(cur, "Record discoveries", json.dumps(parsed), "links-v1")[0]
        check(rec["scanned"] == 5, "five messages recorded as examined")
        check(rec["references_added"] == 4, "four attachment references (text message has none)")
        cur.execute("SELECT count(*) AS n FROM media_fetch_jobs")
        check(cur.fetchone()["n"] == 3, "three jobs: the PDF sent twice is ONE download")
        check(q(cur, "Find unexamined messages", 300, 90) == [], "examined messages are not returned again")
        rec2 = q(cur, "Record discoveries", json.dumps(parsed), "links-v1")[0]
        check(rec2["references_added"] == 0 and rec2["scanned"] == 0, "replaying a discovery adds nothing")

        print("\nclaim")
        claimed = q(cur, "Claim downloads", 4)
        check(len(claimed) == 3, "all three jobs claimed")
        check(claimed and claimed[0]["family"] == "gupconnector", "the young customer link is claimed first")
        check(all(c["claim_token"] for c in claimed), "every claim carries a token")
        check(q(cur, "Claim downloads", 4) == [], "a claimed job is not claimed twice")
        by_family = {c["family"]: c for c in claimed}

        print("\noutcomes")
        sha = "ab" * 32
        stored = {"outcome": "stored", "sha256": sha, "bytes": 1234, "mime": "application/pdf", "http_status": 200}
        out = q(cur, "Record outcome", json.dumps({"job": by_family["bitrix_short"], "res": stored}), 8)[0]
        check(out["fenced_write_applied"] and out["status"] == "stored", "stored -> stored")
        stale = dict(by_family["bitrix_short"], claim_token=str(uuid.uuid4()))
        out = q(cur, "Record outcome", json.dumps({"job": stale, "res": stored}), 8)[0]
        check(out["fenced_write_applied"] is False, "a stale claim token writes nothing")
        out = q(cur, "Record outcome", json.dumps({"job": by_family["gupconnector"],
                                                   "res": {"outcome": "expired", "http_status": 410}}), 8)[0]
        check(out["status"] == "recovery_pending", "410 -> recovery_pending, not a dead end")
        cur.execute("SELECT attempts FROM media_fetch_jobs WHERE family='bitrix_rest'")
        attempts_before = cur.fetchone()["attempts"]
        out = q(cur, "Record outcome", json.dumps({"job": by_family["bitrix_rest"],
                                                   "res": {"error": "timeout"}}), 8)[0]
        check(out["status"] == "retry_wait" and out["outcome"] == "no_answer", "an error item -> retry_wait")
        cur.execute("SELECT attempts, next_attempt_at > now() + interval '4 minutes' AS paused "
                    "FROM media_fetch_jobs WHERE family='bitrix_rest'")
        j = cur.fetchone()
        check(j["attempts"] == attempts_before - 1 and j["paused"],
              "no_answer gives the attempt back and pauses 5 minutes")
        cur.execute("SELECT family, status, source_url IS NULL AS url_gone FROM media_fetch_jobs")
        jobs = {r["family"]: r for r in cur.fetchall()}
        check(jobs["bitrix_short"]["url_gone"] and jobs["gupconnector"]["url_gone"],
              "the URL is dropped on stored and on recovery_pending")
        check(not jobs["bitrix_rest"]["url_gone"], "retry_wait keeps the URL it needs")
        bad_sha = {"outcome": "stored", "sha256": "not-a-hash", "bytes": 1}
        cur.execute("UPDATE media_fetch_jobs SET next_attempt_at = now() WHERE family='bitrix_rest'")
        again = q(cur, "Claim downloads", 4)
        out = q(cur, "Record outcome", json.dumps({"job": again[0], "res": bad_sha}), 8)[0]
        cur.execute("SELECT status, source_url IS NOT NULL AS has_url FROM media_fetch_jobs WHERE family='bitrix_rest'")
        j = cur.fetchone()
        check(j["status"] == "retry_wait" and j["has_url"], "a malformed 'stored' answer keeps the job retryable")

        print("\ndeadline fence")
        cur.execute("UPDATE media_fetch_jobs SET next_attempt_at = now() WHERE family='bitrix_rest'")
        late = q(cur, "Claim downloads", 4)
        cur.execute("UPDATE media_fetch_jobs SET claim_until = now() - interval '1 second' WHERE status='fetching'")
        out = q(cur, "Record outcome", json.dumps({"job": late[0], "res": stored}), 8)[0]
        check(out["fenced_write_applied"] is False,
              "an elapsed claim cannot finish even before anything reclaims it")
        cur.execute("UPDATE media_fetch_jobs SET status='retry_wait', claim_token=NULL, claim_until=NULL, "
                    "claimed_at=NULL WHERE status='fetching'")

        print("\nreclaim")
        cur.execute("UPDATE media_fetch_jobs SET next_attempt_at = now() WHERE family='bitrix_rest'")
        held = q(cur, "Claim downloads", 4)
        cur.execute("UPDATE media_fetch_jobs SET claim_until = now() - interval '1 second' WHERE status='fetching'")
        cur.execute("UPDATE media_fetch_jobs SET next_attempt_at = now() WHERE family='bitrix_rest'")
        q(cur, "Claim downloads", 4)
        cur.execute("SELECT last_outcome FROM media_fetch_jobs WHERE family='bitrix_rest'")
        out = q(cur, "Record outcome", json.dumps({"job": held[0], "res": stored}), 8)[0]
        check(out["fenced_write_applied"] is False, "the expired claim's late answer is fenced out")

        print("\nreader queries")
        reader_sql = _reader_sql()
        cur.execute(reader_sql["SQL_MEDIA"], {"iid": s["iid"]})
        media = cur.fetchall()
        check(len(media) == 4, "reader sees all four attachment references")
        check(sum(1 for m in media if m["status"] == "stored" and m["object_state"] == "present") == 2,
              "both references to the stored PDF resolve to one present object")
        cur.execute(reader_sql["SQL_MESSAGES"], {"iid": s["iid"], "limit": 100})
        check(len(cur.fetchall()) == 5, "reader message query returns the thread")
        cur.execute(reader_sql["SQL_DEAL_INTERACTIONS"], {"deal": "90001", "limit": 10})
        check(len(cur.fetchall()) == 1, "reader finds the thread by deal number")
        cur.execute(reader_sql["SQL_RETENTION"])
        check(cur.fetchone()["value"] == "90", "reader reads the retention window")

        print("\nretention past the window")
        cur.execute("UPDATE interactions SET started_at = now() - interval '91 days' WHERE interaction_id = %s",
                    (s["iid"],))
        ret = q(cur, "Retention", 90)[0]
        check(ret["refs_expired"] == 4, "all four references expire with their conversation")
        check(len(ret["receipts"]) == 3, "all three jobs purged in the SAME run, with their receipts")
        check(ret["objects"] == [sha], "the now-unreferenced object is marked for deletion")
        cur.execute("SELECT count(*) AS n FROM media_fetch_jobs WHERE status='purged' AND source_url IS NULL")
        check(cur.fetchone()["n"] == 3, "purged jobs keep no URL")
        orphan_sha = "ee" * 32
        receipts = ret["receipts"]
        answer = {"objects": [{"sha256": sha, "ok": True}],
                  # one receipt delete fails: it must be asked for again next run
                  "receipts": [{"url_hash": u, "ok": i != 0} for i, u in enumerate(receipts)],
                  # bytes only a receipt knew: an upload whose answer never landed
                  "receipt_objects": [{"sha256": orphan_sha, "bytes": 77, "mime": "image/jpeg"}]}
        stamped = q(cur, "Stamp deleted", json.dumps(answer))[0]
        check(stamped["objects_deleted"] == 1, "a confirmed object delete is stamped")
        check(stamped["receipts_deleted"] == len(receipts) - 1, "only confirmed receipts are stamped")
        check(stamped["orphans_registered"] == 1, "receipt-only bytes are registered for deletion")
        cur.execute("SELECT state, deleted_at IS NOT NULL AS d FROM media_objects WHERE sha256=%s", (sha,))
        o = cur.fetchone()
        check(o["state"] == "deleted" and o["d"], "object is deleted")
        nxt = q(cur, "Retention", 90)[0]
        check(nxt["receipts"] == [receipts[0]], "the unconfirmed receipt is asked for again next run")
        check(orphan_sha in nxt["objects"], "the orphaned upload is deleted next run")
        check(q(cur, "Find unexamined messages", 300, 90) == [], "expired threads are not rediscovered")

        print("\na purged URL sent again")
        q(cur, "Stamp deleted", json.dumps({"objects": [], "receipts": [{"url_hash": u, "ok": True}
                                                                        for u in receipts],
                                            "receipt_objects": []}))
        fresh = seed(cur)          # a new, young conversation re-sending the same links
        rows = q(cur, "Find unexamined messages", 300, 90)
        q(cur, "Record discoveries", json.dumps(parse_with_workflow_js(rows)), "links-v1")
        cur.execute("""SELECT status, source_url IS NOT NULL AS has_url, receipt_deleted_at IS NULL AS rec
                       FROM media_fetch_jobs WHERE family = 'bitrix_short'""")
        j = cur.fetchone()
        check(j["status"] == "pending" and j["has_url"], "a purged job is revived with its URL")
        check(j["rec"], "a revived job's old receipt stamp is cleared")
        check(len(q(cur, "Claim downloads", 4)) >= 1, "the revived job is claimable")
        cur.execute("UPDATE media_fetch_jobs SET status='retry_wait', claim_token=NULL, claim_until=NULL, "
                    "claimed_at=NULL WHERE status='fetching'")
        _ = fresh

        print("\nlease release")
        released = q(cur, "Release run lease", str(uuid.uuid4()))
        check(released == [], "a wrong run token releases nothing")
        released = q(cur, "Release run lease", lease["run_token"])
        check(len(released) == 1, "the owner releases its lease")
        check(q(cur, "Take run lease")[0]["acquired"] is True, "the next run can take it")

        print("\n025 retention function")
        res = {r["what"]: r["rows_affected"] for r in q(cur, "SELECT * FROM purge_raw_content(90, 365, false)")}
        check(res.get("chat_messages deleted") == 5, "purge_raw_content runs and deletes the old thread")

        print("\nhealth view")
        cur.execute("SELECT * FROM v_media_health")
        check(cur.fetchone()["mode"] == "on", "v_media_health answers")

    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
