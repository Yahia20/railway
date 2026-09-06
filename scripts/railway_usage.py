"""What Railway is actually charging, per service. No dashboard, no guessing.

    python scripts/railway_usage.py                 # this project, last 7 days
    python scripts/railway_usage.py --days 30
    python scripts/railway_usage.py --workspace     # every project in the account

The token comes from `~/.railway/config.json` (the CLI's own login) unless
RAILWAY_TOKEN is set. The CLI refreshes that token on any command, so if a call
comes back "Not Authorized", run `railway whoami` once and try again.

TWO THINGS THAT MAKE THIS HARDER THAN IT LOOKS, both cost an hour to find:

1. **The API's time-based measurements are in unit-MINUTES, not unit-hours.**
   MEMORY_USAGE_GB comes back as GB-minutes. Divide by 43,200 (minutes in a
   30-day month) to get the average GB, and multiply by the monthly rate. The
   numbers here were calibrated against the dashboard's own Estimated Usage
   panel and reproduce it to the cent.

2. **`usage(projectId:)` is refused for an account token; `projectServiceUsage`
   is not.** The latter is workspace-scoped, so it returns every project in the
   account and the rows must be filtered by the projectId tag.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

API = "https://backboard.railway.com/graphql/v2"
MINUTES_PER_MONTH = 43_200

# docs.railway.com/reference/pricing/plans, read 2026-09-03. Standard (non-VM)
# workloads. The first four are per unit-MINUTE; egress is per GB outright.
RATES = {
    "MEMORY_USAGE_GB":  10.00 / MINUTES_PER_MONTH,
    "CPU_USAGE":        20.00 / MINUTES_PER_MONTH,
    "DISK_USAGE_GB":     0.15 / MINUTES_PER_MONTH,
    "BACKUP_USAGE_GB":   0.15 / MINUTES_PER_MONTH,
    "NETWORK_TX_GB":     0.05,
}


def token() -> str:
    if os.environ.get("RAILWAY_TOKEN"):
        return os.environ["RAILWAY_TOKEN"]
    cfg = Path.home() / ".railway" / "config.json"
    if cfg.exists():
        user = json.loads(cfg.read_text(encoding="utf-8")).get("user") or {}
        if user.get("accessToken"):
            return user["accessToken"]
    raise SystemExit("no token: set RAILWAY_TOKEN, or run `railway login`")


def gql(query: str, **variables):
    r = httpx.post(API, headers={"Authorization": f"Bearer {token()}",
                                 "Content-Type": "application/json"},
                   json={"query": query, "variables": variables}, timeout=120.0)
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise SystemExit("railway API: " + json.dumps(payload["errors"], indent=2)[:600]
                         + "\n(try `railway whoami` to refresh the CLI token)")
    return payload["data"]


def linked_project() -> str:
    """The project this working directory is linked to, per the CLI's config."""
    if os.environ.get("RAILWAY_PROJECT_ID"):
        return os.environ["RAILWAY_PROJECT_ID"]
    cfg = json.loads((Path.home() / ".railway" / "config.json").read_text(encoding="utf-8"))
    here = str(Path.cwd())
    for path, entry in (cfg.get("projects") or {}).items():
        if here.lower().startswith(path.lower()):
            return entry["project"]
    raise SystemExit("no linked project here; set RAILWAY_PROJECT_ID or run `railway link`")


def cost(measures: dict) -> float:
    return sum(measures.get(name, 0.0) * rate for name, rate in RATES.items())


def main() -> int:
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
    by_project = "--workspace" in sys.argv

    workspace = gql("query { me { workspaces { id name plan members { id } } } }")["me"]["workspaces"][0]
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    if by_project:
        rows = gql("""query($w:String!,$m:[MetricMeasurement!]!,$s:DateTime!,$e:DateTime!){
              usage(workspaceId:$w,measurements:$m,startDate:$s,endDate:$e,groupBy:PROJECT_ID){
                measurement value tags{ projectId } } }""",
            w=workspace["id"], m=list(RATES), s=start.isoformat(), e=now.isoformat())["usage"]
        names = {e["node"]["id"]: e["node"]["name"] for e in gql(
            "query { me { workspaces { projects { edges { node { id name } } } } } }"
        )["me"]["workspaces"][0]["projects"]["edges"]}
        key, label = "projectId", "project"
    else:
        project_id = linked_project()
        rows = [r for r in gql("""query($w:String!,$m:[MetricMeasurement!]!,$s:DateTime!,$e:DateTime!){
              projectServiceUsage(workspaceId:$w,measurements:$m,startDate:$s,endDate:$e,first:200){
                usage{ measurement value tags{ serviceId projectId } } } }""",
            w=workspace["id"], m=list(RATES), s=start.isoformat(), e=now.isoformat()
            )["projectServiceUsage"]["usage"] if (r["tags"] or {}).get("projectId") == project_id]
        names = {e["node"]["id"]: e["node"]["name"] for e in gql(
            "query($id:String!){ project(id:$id){ services{ edges{ node{ id name } } } } }",
            id=project_id)["project"]["services"]["edges"]}
        key, label = "serviceId", "service"

    grouped: dict[str, dict[str, float]] = {}
    for row in rows:
        ident = (row["tags"] or {}).get(key) or "(none)"
        grouped.setdefault(names.get(ident, ident[:8]), {})[row["measurement"]] = row["value"]

    minutes = days * 24 * 60
    print(f"{workspace['name']}  ({workspace['plan']}, {len(workspace['members'])} members)"
          f"   window: {days} days\n")
    print(f"{label:22s}{'RAM avg GB':>12s}{'vCPU avg':>10s}{'disk GB':>9s}"
          f"{'egress GB':>11s}{'$ / month':>11s}")
    total = 0.0
    for name, measures in sorted(grouped.items(), key=lambda kv: -cost(kv[1])):
        monthly = cost(measures) / days * 30
        total += monthly
        print(f"{name:22s}{measures.get('MEMORY_USAGE_GB', 0)/minutes:12.2f}"
              f"{measures.get('CPU_USAGE', 0)/minutes:10.3f}"
              f"{measures.get('DISK_USAGE_GB', 0)/minutes:9.2f}"
              f"{measures.get('NETWORK_TX_GB', 0)/days*30:11.2f}{monthly:11.2f}")
    print(f"\n{'USAGE TOTAL':22s}{'':42s}{total:11.2f}")
    print("plus the plan subscription; its included credit is applied against this.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
