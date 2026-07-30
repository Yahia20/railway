"""Minimal Railway GraphQL client, for when the CLI is not installed.

    export RAILWAY_TOKEN=<project token>
    python scripts/railway_api.py info
    python scripts/railway_api.py vars <service-name>
    python scripts/railway_api.py domains

A PROJECT token is scoped to one project and environment, which is exactly the
blast radius we want: it cannot touch anything else in the account.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

API = "https://backboard.railway.com/graphql/v2"


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
        raise SystemExit("railway API: " + json.dumps(payload["errors"], indent=2))
    return payload["data"]


def scope() -> tuple[str, str]:
    d = gql("query { projectToken { projectId environmentId } }")
    t = d["projectToken"]
    return t["projectId"], t["environmentId"]


def services(project_id: str) -> dict[str, str]:
    d = gql(
        "query($id: String!) { project(id: $id) { name services { edges { node { id name } } } } }",
        id=project_id,
    )
    return {e["node"]["name"]: e["node"]["id"] for e in d["project"]["services"]["edges"]}


def variables(project_id: str, environment_id: str, service_id: str) -> dict:
    d = gql(
        """query($p: String!, $e: String!, $s: String!) {
             variables(projectId: $p, environmentId: $e, serviceId: $s)
           }""",
        p=project_id, e=environment_id, s=service_id,
    )
    return d["variables"]


def domains(project_id: str, environment_id: str, service_id: str) -> dict:
    d = gql(
        """query($p: String!, $e: String!, $s: String!) {
             domains(projectId: $p, environmentId: $e, serviceId: $s) {
               serviceDomains { domain }
               customDomains { domain }
             }
           }""",
        p=project_id, e=environment_id, s=service_id,
    )
    return d["domains"]


SECRET_HINT = ("PASSWORD", "SECRET", "KEY", "TOKEN", "AUTH")


def redact(name: str, value: str) -> str:
    """Show connection strings, mask credentials. A DATABASE_URL contains a
    password, so it is masked in the middle rather than printed whole."""
    if any(h in name.upper() for h in SECRET_HINT):
        return f"[hidden, {len(value)} chars]"
    if "://" in value and "@" in value:
        head, _, tail = value.partition("://")
        creds, _, host = tail.partition("@")
        user = creds.split(":")[0]
        return f"{head}://{user}:***@{host}"
    return value


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    project_id, environment_id = scope()
    svcs = services(project_id)

    if cmd == "info":
        print(f"project     : {project_id}")
        print(f"environment : {environment_id}")
        print("services    :")
        for name, sid in svcs.items():
            print(f"  {name:12s} {sid}")
            for dom in domains(project_id, environment_id, sid).get("serviceDomains", []):
                print(f"               https://{dom['domain']}")
        return 0

    if cmd == "vars":
        target = sys.argv[2]
        if target not in svcs:
            raise SystemExit(f"no service {target!r}; have {list(svcs)}")
        for name, value in sorted(variables(project_id, environment_id, svcs[target]).items()):
            print(f"{name} = {redact(name, str(value))}")
        return 0

    if cmd == "raw":
        target, key = sys.argv[2], sys.argv[3]
        print(variables(project_id, environment_id, svcs[target]).get(key, ""))
        return 0

    raise SystemExit("commands: info | vars <service> | raw <service> <VAR>")


if __name__ == "__main__":
    sys.exit(main())
