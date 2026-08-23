"""Set the worker + n8n service variables on Railway, and clear the start command.

    export RAILWAY_TOKEN=<project token>
    export DEEPSEEK_API_KEY=...
    export PGPASSWORD=...
    python scripts/railway_configure.py [--apply]

Without --apply it prints the plan and changes nothing.

Why this exists: Railway injects only RAILWAY_* variables. It does NOT set PORT.
A start command of `--port $PORT` therefore expands to `--port` with no value,
uvicorn exits immediately, and the deploy fails at the healthcheck with no clue
as to why. Setting PORT explicitly is the fix, and doing it from here means the
whole configuration is reproducible instead of clicked in by hand.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

import httpx

API = "https://backboard.railway.com/graphql/v2"
WORKER_SERVICE = "railway"          # the GitHub-deployed service
N8N_SERVICE = "n8n"

SECRET_HINT = ("PASSWORD", "SECRET", "KEY", "TOKEN", "DATABASE_URL")


def gql(query: str, **variables):
    token = os.environ.get("RAILWAY_TOKEN")
    if not token:
        raise SystemExit("RAILWAY_TOKEN is not set")
    r = httpx.post(
        API,
        headers={"Project-Access-Token": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=60.0,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise SystemExit("railway API error:\n" + str(payload["errors"]))
    return payload["data"]


def show(name: str, value: str) -> str:
    return f"[{len(value)} chars hidden]" if any(h in name.upper() for h in SECRET_HINT) else value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write the variables")
    args = ap.parse_args()

    scope = gql("query { projectToken { projectId environmentId } }")["projectToken"]
    pid, eid = scope["projectId"], scope["environmentId"]

    svc = gql("query($id:String!){ project(id:$id){ services{ edges{ node{ id name } } } } }", id=pid)
    ids = {e["node"]["name"]: e["node"]["id"] for e in svc["project"]["services"]["edges"]}
    for needed in (WORKER_SERVICE, N8N_SERVICE):
        if needed not in ids:
            raise SystemExit(f"no service named {needed!r}; found {list(ids)}")

    worker_id, n8n_id = ids[WORKER_SERVICE], ids[N8N_SERVICE]

    # Reuse an existing key if one is already set, so re-running does not
    # invalidate the secret n8n is already using.
    existing = gql(
        "query($p:String!,$e:String!,$s:String!){ variables(projectId:$p,environmentId:$e,serviceId:$s) }",
        p=pid, e=eid, s=worker_id,
    )["variables"]
    worker_api_key = existing.get("WORKER_API_KEY") or secrets.token_hex(32)
    bitrix_secret = existing.get("BITRIX_WEBHOOK_SECRET") or secrets.token_hex(24)

    pg_password = os.environ.get("PGPASSWORD", "")
    if not pg_password:
        raise SystemExit("PGPASSWORD is not set (the Railway Postgres password)")
    deepseek = os.environ.get("DEEPSEEK_API_KEY", "")
    if not deepseek:
        raise SystemExit("DEEPSEEK_API_KEY is not set")

    # Private host of the worker. Named after the service, which is 'railway'
    # here — not 'worker' — so it must be read, not assumed.
    worker_host = existing.get("RAILWAY_PRIVATE_DOMAIN")
    if not worker_host:
        raise SystemExit("worker has no RAILWAY_PRIVATE_DOMAIN yet; deploy it once first")

    worker_vars = {
        # The fix. Without this the start command has no port to bind.
        "PORT": "8000",
        "WORKER_API_KEY": worker_api_key,
        "DEEPSEEK_API_KEY": deepseek,
        # The explicit model id, not the `deepseek-chat` alias: that alias was
        # scheduled for removal on 2026-07-24 and still answers, so nothing
        # fails loudly when it stops. It resolved to V4 Flash NON-thinking,
        # while an explicit v4 request defaults to thinking ENABLED — so the
        # rename is only behaviour-preserving with DEEPSEEK_THINKING set too.
        # An environment variable beats every default in the source, so setting
        # these here is what actually decides what production asks for; verify
        # on /ready, which reports judge_model and judge_thinking.
        "DEEPSEEK_MODEL": "deepseek-v4-flash",
        "DEEPSEEK_THINKING": "disabled",
        # customer360, NOT railway — n8n owns railway/public and already has a
        # table called `agents` that would collide with ours.
        "DATABASE_URL": f"postgresql://postgres:{pg_password}@postgres.railway.internal:5432/customer360",
        "ASR_BACKEND": "space",
        "ASR_CHUNK_SECONDS": "40",
        "DEFAULT_PHONE_REGION": "SA",
        "PBX_TZ_OFFSET_HOURS": "3",
        "REANALYSIS_IDLE_MINUTES": "30",
        "SCORE_BOT_ONLY_CONVERSATIONS": "false",
        "BITRIX_PORTAL_DOMAIN": "cultiv.bitrix24.com",
        "BITRIX_WEBHOOK_SECRET": bitrix_secret,
        "WORK_DIR": "/tmp/customer360",
        "LOG_LEVEL": "INFO",
    }

    n8n_vars = {
        "WORKER_URL": f"http://{worker_host}:8000",
        "WORKER_API_KEY": worker_api_key,
    }

    print(f"project     {pid}")
    print(f"environment {eid}")
    print(f"worker      {WORKER_SERVICE} ({worker_id})")
    print(f"            private host: {worker_host}\n")

    for label, values in ((WORKER_SERVICE, worker_vars), (N8N_SERVICE, n8n_vars)):
        print(f"--- {label} ---")
        for k, v in values.items():
            print(f"  {k:30s} = {show(k, v)}")
        print()

    if not args.apply:
        print("dry run — re-run with --apply to write these")
        return 0

    mutation = """
    mutation($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }"""
    for sid, values in ((worker_id, worker_vars), (n8n_id, n8n_vars)):
        gql(mutation, input={
            "projectId": pid, "environmentId": eid, "serviceId": sid, "variables": values,
        })
        print(f"upserted {len(values)} variables on {sid}")

    # Clear the saved start command so the Dockerfile CMD wins. The CMD binds
    # `::` because Railway's private network is IPv6-only — a service on
    # 0.0.0.0 is unreachable at *.railway.internal regardless of healthchecks.
    try:
        gql("""
        mutation($s:String!,$e:String!,$in:ServiceInstanceUpdateInput!){
          serviceInstanceUpdate(serviceId:$s, environmentId:$e, input:$in)
        }""", s=worker_id, e=eid, **{"in": {"startCommand": None}})
        print("cleared startCommand (Dockerfile CMD now applies)")
    except SystemExit as exc:
        print(f"NOTE: could not clear startCommand automatically: {exc}")
        print("      Clear it by hand: Railway > railway service > Settings > Deploy >")
        print("      Start Command -> empty. The Dockerfile CMD binds :: with a PORT default.")

    print("\nDone. Redeploy the worker service to pick these up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
