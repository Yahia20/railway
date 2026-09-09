"""Can we afford to start work, and where has the money gone?

WHY A PIPELINE NEEDS THIS AT ALL. On 2026-09-09 DeepSeek's balance was
-0.10 USD with `is_available: false`. Nothing in the system knew. The next
judging window would have claimed 599 threads ten at a time, failed every one
on a payment error, incremented `judge_attempts`, and dead-lettered the whole
queue inside three nights — destroying work for a reason that has nothing to
do with the conversations in it.

So the rule is: **stop before claiming, not after failing.** A job that is
never claimed keeps its status, its attempt count and its place in the queue,
so an outage of any length costs nothing and loses nothing, and work resumes on
its own when the money returns.

ADDING A PROVIDER IS ONE FUNCTION AND ONE ROW. Write a `_probe_<name>()` that
returns a `ProviderProbe`, register it in `PROBES`, and insert a row in
`provider_budgets`. Nothing else in the system needs to change: the gate view,
the workflow gates and the spend report all read the same two tables.

THIS MODULE ONLY READS. Rule 11 — the worker reads, n8n writes. The probe
returns a verdict and n8n persists it into `provider_status`, so there is no
second write path to reason about.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Callable

import httpx

from . import db

log = logging.getLogger("worker.budget")

# A probe must not be able to hold up the whole nightly window. Two seconds is
# already generous for a balance endpoint; the fallback on timeout is
# "unknown", which the gate treats as unavailable for anything we can probe.
PROBE_TIMEOUT_SECONDS = 8.0


@dataclass
class ProviderProbe:
    """One provider's answer to 'may we spend money with you right now?'"""

    provider: str
    available: bool
    reason: str | None = None
    balance_usd: float | None = None
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _probe_deepseek() -> ProviderProbe:
    """DeepSeek publishes a balance endpoint, so ask it rather than guess.

    `is_available` is the provider's own verdict and is what we honour — it is
    false at exactly zero, where arithmetic on the balance alone would still
    say "0.00 is not negative, carry on" and then fail every call.
    """
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return ProviderProbe("deepseek", False, "DEEPSEEK_API_KEY is not set")

    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    # Only DeepSeek's own API serves this; an OpenRouter-style base URL has no
    # equivalent, and guessing one would report a confident wrong answer.
    if "deepseek.com" not in base:
        return ProviderProbe("deepseek", True,
                             f"balance not checkable for base_url {base}")
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_SECONDS) as client:
            r = client.get(f"{base}/user/balance",
                           headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            data = r.json()
    except Exception as exc:                       # noqa: BLE001 - any failure is "unknown"
        log.warning("deepseek balance probe failed: %s", exc)
        return ProviderProbe("deepseek", False, f"balance probe failed: {exc}")

    infos = data.get("balance_infos") or []
    usd = next((b for b in infos if b.get("currency") == "USD"), None) or (
        infos[0] if infos else {})
    try:
        balance = float(usd.get("total_balance"))
    except (TypeError, ValueError):
        balance = None

    available = bool(data.get("is_available"))
    reason = None if available else (
        f"DeepSeek reports no available balance (total_balance "
        f"{usd.get('total_balance', '?')} {usd.get('currency', '')})".strip())
    return ProviderProbe("deepseek", available, reason, balance, data)


def _probe_modal() -> ProviderProbe:
    """Modal exposes no balance API, so our own meter is the only signal.

    That is why `provider_budgets.monthly_cap_usd` is the whole control for
    Modal and `require_positive_balance` is false: the 30 USD free credit is
    enforced by `v_pipeline_gate` reading `v_spend_mtd`, not by asking Modal.
    A probe that returned "available" from no evidence would be a lie, so this
    one says exactly what it knows.
    """
    return ProviderProbe("modal", True,
                         "no balance API; the monthly cap is the only control")


def _probe_cohere() -> ProviderProbe:
    return ProviderProbe("cohere", True,
                         "no balance API; the monthly cap is the only control")


PROBES: dict[str, Callable[[], ProviderProbe]] = {
    "deepseek": _probe_deepseek,
    "modal": _probe_modal,
    "cohere": _probe_cohere,
}


def preflight(providers: list[str] | None = None) -> dict:
    """Probe each provider and pair it with what it has already spent.

    Returns the probes AND the gate's own verdict, so a caller that only wants
    a yes/no does not have to re-implement the policy. The authoritative gate
    is still `v_pipeline_gate` in SQL — this is the same answer, computed from
    the same rows, for the convenience of a human reading the response.
    """
    wanted = providers or list(PROBES)
    probes = {}
    for name in wanted:
        fn = PROBES.get(name)
        if fn is None:
            probes[name] = ProviderProbe(name, False, "no probe registered").as_dict()
            continue
        try:
            probes[name] = fn().as_dict()
        except Exception as exc:                   # noqa: BLE001
            log.exception("probe %s blew up", name)
            probes[name] = ProviderProbe(name, False, f"probe error: {exc}").as_dict()

    spend = {r["provider"]: r for r in db.rows("SELECT * FROM v_spend_mtd")}
    for name, p in probes.items():
        s = spend.get(name) or {}
        p["spend_mtd_usd"] = float(s.get("spend_mtd_usd") or 0)
        p["monthly_cap_usd"] = (
            float(s["monthly_cap_usd"]) if s.get("monthly_cap_usd") is not None else None)
        p["over_cap"] = bool(s.get("over_cap"))
        p["enabled"] = bool(s.get("enabled", True))
        # The same policy v_pipeline_gate applies, so the HTTP answer and the
        # SQL answer cannot drift apart in a way nobody notices.
        blocked = None
        if not p["enabled"]:
            blocked = "disabled in provider_budgets"
        elif p["over_cap"]:
            blocked = (f"monthly cap reached: {p['spend_mtd_usd']:.2f} of "
                       f"{p['monthly_cap_usd']:.2f} USD spent")
        elif not p["available"]:
            blocked = p.get("reason") or "provider reports no available balance"
        p["may_run"] = blocked is None
        p["blocked_reason"] = blocked

    return {"providers": probes,
            "checked_at": db.one("SELECT now() AS now")["now"].isoformat()}


# A batch that has never run leaves nothing to measure, so the first one needs
# a number. 0.9 GPU-seconds of A10G per recording is the conservative end of
# what the cost model assumed (RTFx 120 over a ~2 minute average call); once
# `asr_runs` holds a real batch this constant stops being used at all.
DEFAULT_GPU_SECONDS_PER_RECORDING = 0.9
A10G_HOURLY_USD = 1.10


def asr_claim_allowance(requested: int) -> tuple[int, str | None]:
    """How many recordings Modal may claim right now, and why not more.

    ENFORCED HERE, NOT IN MODAL. Modal reaches the database only through this
    worker, so the worker is the one chokepoint every batch must pass. A cap
    living in `modal/transcribe_job.py` would be advisory — a redeploy, a
    `--limit` override or a hand-run `modal run` would step straight past it.
    Here it holds whatever calls in.

    TWO GATES, NOT ONE:
      * over the cap, claim nothing at all;
      * under it, claim only as many recordings as the REMAINING budget can
        pay for, so a single large batch cannot vault over the ceiling in one
        go. Without the second gate a 500-recording batch could spend far past
        30 USD before anything got the chance to say stop.
    """
    gate = db.rows("SELECT * FROM v_pipeline_gate WHERE provider = 'modal'")
    if not gate:
        return 0, "no budget row for provider 'modal'"
    g = gate[0]
    if not g["may_run"]:
        return 0, g["reason"] or "modal is blocked by its budget"

    remaining = g.get("remaining_usd")
    if remaining is None:
        return requested, None                      # no cap configured

    remaining = float(remaining)
    if remaining <= 0:
        return 0, (f"monthly cap reached: {float(g['spend_mtd_usd']):.2f} of "
                   f"{float(g['monthly_cap_usd']):.2f} USD spent")

    # What one recording has actually cost us, measured where possible.
    seen = db.one("""
        SELECT coalesce(sum(gpu_seconds), 0) AS gpu, coalesce(sum(processed), 0) AS n
          FROM asr_runs WHERE processed > 0
    """)
    per_recording_gpu = (float(seen["gpu"]) / float(seen["n"])
                         if seen and float(seen["n"]) > 0
                         else DEFAULT_GPU_SECONDS_PER_RECORDING)
    per_recording_usd = per_recording_gpu / 3600.0 * A10G_HOURLY_USD
    if per_recording_usd <= 0:
        return requested, None

    affordable = int(remaining / per_recording_usd)
    if affordable <= 0:
        return 0, (f"only {remaining:.4f} USD left of the "
                   f"{float(g['monthly_cap_usd']):.2f} USD cap, which does not "
                   f"cover one recording")
    if affordable < requested:
        return affordable, (
            f"trimmed to {affordable} recordings: {remaining:.4f} USD left of "
            f"the {float(g['monthly_cap_usd']):.2f} USD cap at "
            f"~{per_recording_usd:.5f} USD each")
    return requested, None


def spend_report() -> dict:
    """Where the money went this month, and how much room is left."""
    return {
        "by_provider": db.rows("SELECT * FROM v_spend_mtd ORDER BY provider"),
        "by_component": db.rows(
            "SELECT * FROM v_spend_by_component ORDER BY provider, component"),
        "gate": db.rows("SELECT * FROM v_pipeline_gate ORDER BY provider"),
    }
