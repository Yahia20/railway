#!/usr/bin/env python3
"""Does the data mean what the reports say it means?

    railway connect postgres --tunnel-only --port 55432
    export PGPASSWORD=...
    python scripts/audit_data_integrity.py --port 55432

WHY THIS EXISTS. Every number on `/report` and every row of
`v_agent_scorecard` is read by a person who will act on it. A constraint stops
a row being *impossible*; nothing stops a row being *misleading* — an
evaluation attached to no agent, a request counted twice, a rate computed over
the wrong denominator. Those are the failures that survive a green test suite
and a clean schema, and they are the ones that produce a confident wrong
decision.

Each check below is one assertion about meaning, with the reason it matters.
FAIL means a report is currently lying. WARN means it will lie under a
condition that has not happened yet.
"""
from __future__ import annotations

import argparse
import os
import sys

# (id, severity, question, sql, predicate on the single returned value)
CHECKS: list[tuple[str, str, str, str]] = [

    # ---------------------------------------------------------------- keys
    ("dup-interactions", "FAIL",
     "Two rows for one source conversation would double every count that "
     "groups by interaction.",
     """SELECT count(*) FROM (
          SELECT external_source, external_id FROM interactions
          GROUP BY 1,2 HAVING count(*) > 1) d"""),

    ("dup-analysis", "FAIL",
     "interaction_analysis is UNIQUE per interaction; more than one would mean "
     "the funnel counts a conversation twice.",
     """SELECT count(*) FROM (
          SELECT interaction_id FROM interaction_analysis
          GROUP BY 1 HAVING count(*) > 1) d"""),

    ("dup-evaluations", "FAIL",
     "Two evaluations for one interaction would average an agent against "
     "themselves.",
     """SELECT count(*) FROM (
          SELECT interaction_id FROM agent_evaluations
          GROUP BY 1 HAVING count(*) > 1) d"""),

    ("dup-deals", "FAIL",
     "A Bitrix deal appearing twice would double revenue in every funnel view.",
     """SELECT count(*) FROM (
          SELECT bitrix_deal_id FROM deals
          GROUP BY 1 HAVING count(*) > 1) d"""),

    # ------------------------------------------------------------ orphans
    ("eval-without-analysis", "WARN",
     "An agent was scored on a conversation pass 1 never read. The two passes "
     "are independent by design, but a systematic gap means one of them is "
     "failing silently.",
     """SELECT count(*) FROM agent_evaluations e
         WHERE NOT EXISTS (SELECT 1 FROM interaction_analysis a
                            WHERE a.interaction_id = e.interaction_id)"""),

    ("analysis-without-interaction", "FAIL",
     "Analysis pointing at no conversation cannot be attributed to anyone.",
     """SELECT count(*) FROM interaction_analysis a
         WHERE NOT EXISTS (SELECT 1 FROM interactions i
                            WHERE i.interaction_id = a.interaction_id)"""),

    ("request-without-analysis", "WARN",
     "interaction_requests is meant to expand what interaction_analysis "
     "summarises; a request with no parent analysis has no primary to compare "
     "against.",
     """SELECT count(*) FROM interaction_requests r
         WHERE NOT EXISTS (SELECT 1 FROM interaction_analysis a
                            WHERE a.interaction_id = r.interaction_id)"""),

    # ------------------------------------------------- scorecard integrity
    ("scored-bot", "FAIL",
     "A bot in the agent scorecard is a number about a cron job filed under a "
     "person's name.",
     """SELECT count(*) FROM v_agent_scorecard s
          JOIN agents a ON a.agent_id = s.agent_id WHERE a.is_bot"""),

    ("eval-agent-mismatch", "FAIL",
     "agent_evaluations.agent_id disagreeing with interactions.agent_id means "
     "the scorecard and the conversation list credit different people.",
     """SELECT count(*) FROM agent_evaluations e
          JOIN interactions i ON i.interaction_id = e.interaction_id
         WHERE e.agent_id IS DISTINCT FROM i.agent_id"""),

    ("score-out-of-range", "FAIL",
     "A final_score outside 0-100 breaks every band and average built on it.",
     """SELECT count(*) FROM agent_evaluations
         WHERE final_score IS NOT NULL
           AND (final_score < 0 OR final_score > 100)"""),

    ("module-zero-vs-null", "WARN",
     "Rule 2: a module that did not apply must be NULL, never 0. A row scoring "
     "0 on every module at once is the old give-away-the-weight bug returning.",
     """SELECT count(*) FROM agent_evaluations
         WHERE coalesce(m1_reception,-1) = 0 AND coalesce(m2_offer,-1) = 0
           AND coalesce(m3_objections,-1) = 0 AND coalesce(m4_followup,-1) = 0
           AND coalesce(m5_closing,-1) = 0"""),

    ("weight-applied-missing", "WARN",
     "final_score is computed over weight_applied. Without it the number "
     "cannot be reproduced or defended.",
     """SELECT count(*) FROM agent_evaluations
         WHERE final_score IS NOT NULL AND weight_applied IS NULL"""),

    # ------------------------------------------------------ identity truth
    ("phone-not-e164", "FAIL",
     "A phone that is not E.164 will never match a customer, so the "
     "conversation silently belongs to nobody.",
     r"""SELECT count(*) FROM interactions
          WHERE customer_phone_e164 IS NOT NULL
            AND customer_phone_e164 !~ '^\+[1-9][0-9]{6,14}$'"""),

    ("customer-phone-collision", "FAIL",
     "Two customers sharing a phone means identity resolution has merged or "
     "split someone wrongly.",
     """SELECT count(*) FROM (
          SELECT primary_phone_e164 FROM customers
           WHERE primary_phone_e164 IS NOT NULL
           GROUP BY 1 HAVING count(*) > 1) d"""),

    ("identity-without-customer", "FAIL",
     "customer_identities IS the merge audit log; a dangling row makes a bad "
     "merge undiscoverable.",
     """SELECT count(*) FROM customer_identities ci
         WHERE NOT EXISTS (SELECT 1 FROM customers c
                            WHERE c.customer_id = ci.customer_id)"""),

    # ------------------------------------------------------- job integrity
    ("job-without-interaction", "FAIL",
     "A queue row pointing at a deleted conversation can never terminate.",
     """SELECT count(*) FROM chat_eval_jobs j
         WHERE NOT EXISTS (SELECT 1 FROM interactions i
                            WHERE i.interaction_id = j.interaction_id)"""),

    ("lease-half-set", "FAIL",
     "A claim token without a deadline is a lease nothing can reclaim; a "
     "deadline without a token is a lease nothing can fence a write against.",
     """SELECT count(*) FROM chat_eval_jobs
         WHERE (claim_token IS NULL) <> (claim_until IS NULL)"""),

    ("evaluated-without-evaluation", "FAIL",
     "A job marked evaluated with no evaluation row is work reported as done "
     "that produced nothing.",
     """SELECT count(*) FROM chat_eval_jobs j
         WHERE j.status = 'evaluated'
           AND NOT EXISTS (SELECT 1 FROM agent_evaluations e
                            WHERE e.interaction_id = j.interaction_id)"""),

    ("stale-lease", "WARN",
     "A lease past its deadline that the recovery sweep has not reclaimed "
     "means the sweep is not running.",
     """SELECT count(*) FROM chat_eval_jobs
         WHERE status = 'evaluating' AND claim_until < now() - interval '1 hour'"""),

    # ---------------------------------------------------------- money truth
    ("judged-without-cost", "WARN",
     "Rule 12: every judge call must land in model_calls, or the bill has a "
     "hole in it. Counts only evaluations made since cost recording existed.",
     """SELECT count(*) FROM agent_evaluations e
         WHERE e.created_at > timestamptz '2026-09-07'
           AND NOT EXISTS (SELECT 1 FROM model_calls m
                            WHERE m.interaction_id = e.interaction_id)"""),

    ("negative-cost", "FAIL",
     "A negative cost silently reduces the reported spend.",
     "SELECT count(*) FROM model_calls WHERE cost_usd < 0"),

    # -------------------------------------------------------- report truth
    ("alerts-unevaluated", "WARN",
     "A judged thread whose alert rules never ran is a follow-up nobody will "
     "see. Should be zero.",
     "SELECT count(*) FROM v_chat_alerts_pending"),

    ("metrics-missing-for-evaluated", "WARN",
     "v_agent_scorecard LEFT JOINs interaction_metrics for response time; "
     "without it every agent shows a blank instead of a number.",
     """SELECT count(*) FROM agent_evaluations e
          JOIN interactions i ON i.interaction_id = e.interaction_id
         WHERE i.channel <> 'phone_call'
           AND NOT EXISTS (SELECT 1 FROM interaction_metrics m
                            WHERE m.interaction_id = e.interaction_id)"""),

    ("deal-without-owner", "WARN",
     "A deal with no agent cannot appear in any per-agent revenue view.",
     "SELECT count(*) FROM deals WHERE agent_id IS NULL AND origin = 'bitrix'"),

    # ------------------------------------------------- a score is never naked
    #
    # The owner's rule: a score is ALWAYS shown, and always with the number of
    # evaluations it rests on plus the fact that the method is interim. These
    # assert the rule holds in the DATA, not just in the renderer — a template
    # can be edited, a view cannot be edited by accident.

    ("mean-without-sample-size", "FAIL",
     "A mean with no n_usable beside it is the number somebody acts on at n=1. "
     "Every display row exposing a value must expose its denominator.",
     """SELECT count(*) FROM v_agent_scorecard_display
         WHERE score_display->>'value' IS NOT NULL
           AND score_display->>'n_usable' IS NULL"""),

    ("mean-without-label", "FAIL",
     "The confidence label is what makes a provisional number honest rather "
     "than misleading. It must never be absent where a value is present.",
     """SELECT count(*) FROM v_agent_scorecard_display
         WHERE score_display->>'value' IS NOT NULL
           AND coalesce(score_display->>'label', '') = ''"""),

    ("quality-mean-without-label", "FAIL",
     "Same rule for the input-quality panel, which is also read by a person.",
     """SELECT count(*) FROM v_quality_by_input_display
         WHERE score_display->>'value' IS NOT NULL
           AND (coalesce(score_display->>'label', '') = ''
                OR score_display->>'n_usable' IS NULL)"""),

    ("mean-suppressed", "FAIL",
     "The owner decided a score is ALWAYS visible. A row with usable "
     "evaluations and no value means something started hiding it again.",
     """SELECT count(*) FROM v_agent_scorecard_display
         WHERE n_usable > 0 AND score_display->>'value' IS NULL"""),

    ("label-disagrees-with-provisional", "FAIL",
     "is_provisional is what an automated consumer gates on; the label is what "
     "a person reads. If they can disagree, one of them is lying.",
     """SELECT count(*) FROM v_agent_scorecard_display
         WHERE is_provisional <> (confidence_label <> 'published')"""),

    ("version-coordinate-collapsed", "FAIL",
     "The scorecard is one row per agent PER VERSION CO-ORDINATE. Two agents "
     "genuinely have both a v4-flash and a v4-pro row. If a co-ordinate column "
     "were ever dropped from the grouping, those rows would silently average a "
     "v1 score together with a v6 one.",
     """SELECT count(*) FROM (
          SELECT agent_id, prompt_version, rubric_version, model,
                 model_fingerprint
            FROM v_agent_scorecard_display
           GROUP BY 1,2,3,4,5 HAVING count(*) > 1) d"""),

    ("method-label-missing", "WARN",
     "Without it, two rows for one agent read as duplicates and get compared.",
     """SELECT count(*) FROM v_agent_scorecard_display
         WHERE coalesce(method_label, '') = ''"""),

    # ---------------------------------------- the drill-down shows both halves
    #
    # The per-conversation panel prints the five module scores and
    # `weight_applied` side by side, which makes this checkable by eye for the
    # first time. `final_score` is a weighted mean over the modules that were
    # not null, so if the stored denominator disagrees with which modules are
    # actually null, the score on screen cannot be reproduced from the numbers
    # printed beside it — and a reader recomputing it by hand gets a different
    # answer with no way to tell which one is wrong.
    ("weight-does-not-match-modules", "FAIL",
     "weight_applied must be the sum of the weights of the non-null modules "
     "(0.15 / 0.25 / 0.25 / 0.20 / 0.15). A mismatch makes final_score "
     "unreproducible from the modules displayed next to it.",
     """SELECT count(*) FROM agent_evaluations
         WHERE final_score IS NOT NULL
           AND round(weight_applied, 3) IS DISTINCT FROM round((
                 (CASE WHEN m1_reception  IS NOT NULL THEN 0.15 ELSE 0 END)
               + (CASE WHEN m2_offer      IS NOT NULL THEN 0.25 ELSE 0 END)
               + (CASE WHEN m3_objections IS NOT NULL THEN 0.25 ELSE 0 END)
               + (CASE WHEN m4_followup   IS NOT NULL THEN 0.20 ELSE 0 END)
               + (CASE WHEN m5_closing    IS NOT NULL THEN 0.15 ELSE 0 END)
             )::numeric, 3)"""),

    ("scored-below-minimum-weight", "FAIL",
     "Below MIN_WEIGHT_APPLIED (0.40) too little of the rubric survived to "
     "average into anything meaningful; scoring.py reports ungradeable rather "
     "than a confident number built from one module. A scored row under it "
     "means that gate was bypassed.",
     """SELECT count(*) FROM agent_evaluations
         WHERE final_score IS NOT NULL AND weight_applied < 0.40"""),

    ("published-without-measured-noise", "FAIL",
     "Nothing may be labelled 'published' while its band rests on no measured "
     "judge noise. This is the assertion that catches somebody 'fixing' the "
     "blank scorecard by inserting a fabricated eval_noise_params row.",
     """SELECT count(*) FROM v_agent_scorecard_display
         WHERE confidence_label = 'published' AND noise_variance IS NULL"""),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.getenv("PGPORT", "55432"))
    ap.add_argument("--database", default="customer360")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on WARN as well as FAIL")
    args = ap.parse_args()

    password = os.getenv("PGPASSWORD")
    if not password:
        raise SystemExit("PGPASSWORD is not set")

    import psycopg

    dsn = f"postgresql://postgres:{password}@127.0.0.1:{args.port}/{args.database}"
    fails = warns = 0
    with psycopg.connect(dsn, connect_timeout=20, autocommit=True) as conn:
        for check_id, severity, why, sql in CHECKS:
            try:
                n = conn.execute(sql).fetchone()[0]
            except Exception as exc:                       # noqa: BLE001
                print(f"  ERROR  {check_id}: query failed -- {exc}")
                fails += 1
                continue
            if n == 0:
                print(f"  ok     {check_id}")
                continue
            if severity == "FAIL":
                fails += 1
            else:
                warns += 1
            print(f"  {severity}   {check_id}: {n} row(s)")
            print(f"         {why}")

    print(f"\n{fails} failure(s), {warns} warning(s), "
          f"{len(CHECKS) - fails - warns} clean")
    if fails:
        return 1
    return 1 if (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
