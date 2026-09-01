#!/usr/bin/env python3
"""Point the judge at a different endpoint and model.

WHY A SCRIPT. `scripts/railway_configure.py` writes the whole configuration set
at once, including a `DEEPSEEK_MODEL` value that is now a retired alias, so
running it to change one setting would quietly undo two others. This changes
only the judge's endpoint, model and key, and prints the before and after.

CHANGING THE MODEL IS A NEW SCORING BASELINE, NOT A TUNING KNOB. Two scores are
only comparable when the prompt version, the rubric version AND the model all
match — `v_agent_scorecard` groups on exactly those three for that reason. Every
score already in the database came from a different model, and after this they
are a separate population that must never be averaged with what comes next.

Occasioned by 2026-09-01: `stealth/ox-alpha` returned 404 from OpenRouter — a
preview model withdrawn without notice. Nothing alerted, because the calls
queue had been empty since 19 August, so the judge had not been called in seven
days and every scheduled run "succeeded" doing nothing.

    export RAILWAY_TOKEN=...
    python scripts/set_judge_model.py --model deepseek-v4-pro \
        --base-url https://api.deepseek.com --key sk-... --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from railway_api import gql, scope, services, variables  # noqa: E402

WORKER_SERVICE = "railway"
KEYS = ("DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY",
        "DEEPSEEK_REASONING_EFFORT", "DEEPSEEK_THINKING")


def show(key: str, value):
    if value is None:
        return "(unset)"
    return f"[hidden, {len(str(value))} chars]" if "KEY" in key else value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--key", help="new DEEPSEEK_API_KEY; omit to keep the current one")
    ap.add_argument("--drop-reasoning-effort", action="store_true",
                    help="DEEPSEEK_REASONING_EFFORT sends OpenRouter's unified "
                         "`reasoning.effort` field, which a native endpoint does "
                         "not define. Drop it when leaving OpenRouter.")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    pid, eid = scope()
    svcs = services(pid)
    sid = svcs[WORKER_SERVICE]
    current = variables(pid, eid, sid)

    print("current:")
    for k in KEYS:
        print(f"  {k:28} = {show(k, current.get(k))}")

    new = dict(current)
    new["DEEPSEEK_BASE_URL"] = args.base_url
    new["DEEPSEEK_MODEL"] = args.model
    if args.key:
        new["DEEPSEEK_API_KEY"] = args.key
    if args.drop_reasoning_effort:
        # Railway has no "delete one variable" in this mutation, so it is set
        # empty; config.py reads `_env(...) or None`, and an empty string is
        # falsy, so the header is never sent.
        new["DEEPSEEK_REASONING_EFFORT"] = ""

    print("\nnew:")
    for k in KEYS:
        changed = "  <-- changed" if new.get(k) != current.get(k) else ""
        print(f"  {k:28} = {show(k, new.get(k))}{changed}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    gql("""
    mutation($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }""", input={"projectId": pid, "environmentId": eid,
                 "serviceId": sid, "variables": new})
    print(f"\nAPPLIED to service {WORKER_SERVICE} ({sid}).")
    print("Railway redeploys on a variable change; /ready will show the new "
          "judge_model once it is up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
