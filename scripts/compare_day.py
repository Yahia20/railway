#!/usr/bin/env python
"""Re-score a day of stored evaluations with the current judge and diff the two.

    python scripts/compare_day.py --input day13/compare_input.json --out day13/run1
    python scripts/compare_day.py --input ... --out ... --dry-run   # no network
    python scripts/compare_day.py --input ... --out ... --limit 5 --workers 2

    # the A/A baseline: OLD prompts through the NEW code
    python scripts/compare_day.py --input ... --out day13/runAA \
        --pass1-prompt pass1_customer_v4.md --pass2-prompt pass2_agent_quality_v3.md

    # what the prompt change is worth once the model's own spread is removed
    python scripts/compare_day.py --out day13/run2 --aa-compare day13/runAA

    # the M3 exclusion regression suite, against the live judge
    python scripts/compare_day.py --out day13/run2 --m3-fixtures

The point is not "did the score move" but "did it move for a reason we can
read". Prompt and scoring changes are judged on real conversations that already
carry an old verdict, so every delta has a before and an after side by side —
and every discarded finding, rejected quote and contract failure is listed with
the quote that caused it, because a score change nobody can explain is
indistinguishable from a regression.

Input is a JSON array. Each item carries the conversation exactly as production
sent it, plus the `old` block read back out of `agent_evaluations`. The item is
re-run through the SAME functions `/evaluate` calls — `run_pass1`, `run_pass2`
and the `MIN_SCOREABLE_CHARS` gate — never a re-implementation of them, so a
difference here is a real difference in production behaviour and not a
divergence between this script and the service.

Outputs, all under `--out`:
    new_results.jsonl   full new pass1/pass2 payloads, one JSON object per line
    comparison.csv      one row per item, old and new side by side
    report.md           the summary a human actually reads

Results are cached per item under `--out/cache/`, keyed by a canonical hash of
every input that can change the answer — conversation, metadata block,
follow-up history, input type, `--only-pass2`, rubric, model, the speech gate,
and the CONTENTS of every prompt file — so a re-run after an interrupted pass
costs nothing and any edit that matters invalidates every entry it should.

Reads DEEPSEEK_API_KEY from the environment (not needed for --dry-run).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))

from app.evaluate import judge, scoring          # noqa: E402
from app.main import MIN_SCOREABLE_CHARS, spoken_content   # noqa: E402

MODULES = list(scoring.WEIGHTS)

# Published deepseek-v4-flash rates, USD per 1M tokens, cache-miss input, PEAK
# (api-docs.deepseek.com/quick_start/pricing, read 2026-08-22: 0.22–0.44 in,
# 0.66–1.32 out, off-peak to peak). Peak is the default because a cost estimate
# that can only be wrong in one direction should be wrong high.
#
# They were 0.27 / 1.10, labelled "deepseek-chat rates" — a model name that no
# longer appears in the pricing table at all. They have moved more than once, so
# they are flags rather than constants and every total built from them is
# labelled an estimate. Token counts in the report are measured; money is not.
DEFAULT_PRICE_IN = 0.44
DEFAULT_PRICE_OUT = 1.32


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ("interaction_id", "conversation")


def load_items(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"{path}: expected a JSON array, got {type(raw).__name__}")

    items, problems = [], []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"item {i}: not an object")
            continue
        missing = [f for f in REQUIRED_FIELDS if item.get(f) in (None, "")]
        if missing:
            problems.append(f"item {i}: missing {', '.join(missing)}")
            continue
        iid = str(item["interaction_id"])
        if iid in seen:
            problems.append(f"item {i}: duplicate interaction_id {iid}")
            continue
        seen.add(iid)
        items.append(item)

    if problems:
        print(f"input problems ({len(problems)}):", file=sys.stderr)
        for p in problems[:20]:
            print(f"  ! {p}", file=sys.stderr)
    if not items:
        raise SystemExit("no usable items in input")
    return items


def input_type_of(item: dict) -> str:
    """chat vs call_transcript, from whatever the row actually carries.

    `kind` is the PBX/Bitrix code and is not a channel name — the day-13 export
    is all `'q'` (queue call). A recording id, a duration or an ASR confidence
    means audio; nothing else does. Guessing wrong swaps the channel rules
    block, which changes what the judge is allowed to deduct for.
    """
    kind = str(item.get("kind") or "").strip().lower()
    if kind in ("chat", "bitrix_chat", "im", "message"):
        return "chat"
    if kind in ("call", "call_transcript", "phone", "phone_call", "q"):
        return "call_transcript"
    audio = (item.get("uniqueid") or item.get("asr_confidence") is not None
             or item.get("asr_quality_status") or item.get("duration_seconds"))
    return "call_transcript" if audio else "chat"


def followup_for(item: dict, history_format: str = "stored") -> str:
    """`None` becomes the literal 'unavailable', exactly as production sends it.

    Not cosmetic: the prompt branches on this string, and a Python `None`
    formatted into the template would read as the word "None" — a follow-up
    history the model would then try to grade.
    """
    return followup_source_for(item, history_format)[0]


# The current production renderer, `scripts/sql/02_build_follow_up_history.sql`
# (generated from the 'Build follow-up history' node). Module 4 is 20% of the
# grade and cannot be observed inside one phone call, so it is scored across the
# customer's timeline — and on day 13 the five calls that HAD a timeline still
# scored Module 4 null, because the block that reached the prompt said only
# "phone_call by unknown". The current block names the direction, distinguishes
# a queue recording from a genuinely unknown agent, and carries the first agent
# message so criterion 3 (message quality, 30 of the module's 100 points) is
# answerable at all.
#
# Reproduced here field for field. A comparison run that sends the OLD string
# while production sends the new one is measuring a prompt against input
# production stopped using.
def render_current_history(later: list[dict]) -> str:
    lines = ["Subsequent contact with this customer:"]
    for entry in sorted(later, key=lambda e: str(e.get("started_at") or "")):
        channel = str(entry.get("channel") or "phone_call")
        if channel == "phone_call" and str(entry.get("kind") or "") == "q":
            direction = ("INBOUND: the customer called in, this is not an "
                         "agent follow-up")
        elif entry.get("direction"):
            direction = f"direction {entry['direction']}"
        else:
            direction = "direction not recorded"

        if entry.get("agent_name"):
            handler = str(entry["agent_name"])
        elif entry.get("is_bot_handled"):
            handler = "the qualification bot, not a human agent"
        elif str(entry.get("kind") or "") == "q":
            handler = "no individual agent recorded (queue recording)"
        else:
            handler = "not recorded"

        body = entry.get("first_message")
        message = f': "{str(body)[:300]}"' if body else ""
        hours = entry.get("hours_after")
        hours_text = f"{float(hours):.1f}" if isinstance(hours, (int, float)) else "?"
        lines.append(
            f"  - [{entry.get('started_at', '?')}] {channel}, {direction}, "
            f"{hours_text}h after this conversation, handled by {handler}{message}"
        )
    return "\n".join(lines)


def followup_source_for(item: dict, history_format: str = "stored") -> tuple[str, str]:
    """(the block to send, where it came from).

    `stored` sends the string production sent at the time, which is what makes a
    re-run of a stored day comparable to its stored scores.

    `current` re-renders the block the way production renders it TODAY, from
    `later_interactions` on the item. When the export does not carry that
    structure there is nothing to re-render, so it falls back to the stored
    string and SAYS SO — silently sending the old format under a flag that
    promises the new one would make Module 4 look tested when it was not. The
    day-13 export is exactly that case: it carries `followup_history` and a
    `followup_history_now` copy identical to it, and no per-interaction rows.
    """
    stored = item.get("followup_history")
    stored = stored if isinstance(stored, str) and stored.strip() else None

    if history_format == "current":
        later = item.get("later_interactions")
        if isinstance(later, list) and later:
            return render_current_history(later), "current"
        now = item.get("followup_history_now")
        if isinstance(now, str) and now.strip() and now != stored:
            return now, "current-precomputed"
        if stored:
            return stored, "fallback-stored"
        return "unavailable", "unavailable"

    return (stored, "stored") if stored else ("unavailable", "unavailable")


# The metadata block the n8n evaluate path builds, field for field
# (02-calls-ingest-evaluate.json, "Build evaluate request"). The prompt calls it
# "computed, authoritative — do not recalculate", and several criteria are
# scored as unmeasurable when a field it needs is absent. Sending `{}` therefore
# does not make the comparison neutral; it makes the new run answer a different
# question from the old one, on a difference nothing in the output records.
PRODUCTION_METADATA_FIELDS = ("asr_confidence", "diarization", "duration_seconds", "channels")

# Top-level export columns that can stand in for a production metadata field.
_METADATA_FALLBACKS = {
    "asr_confidence": ("asr_confidence",),
    "duration_seconds": ("duration_seconds",),
    "diarization": ("diarization",),
    "channels": ("channels",),
}


def metadata_for(item: dict) -> tuple[dict[str, Any], str, list[str]]:
    """(metadata block, where it came from, fields that could not be filled).

    Three sources, in order of trustworthiness:

    - `nested`   — the item carries the exact block production sent. Used as-is.
    - `rebuilt`  — reconstructed from top-level export columns, with every field
                   that could not be filled named.
    - `empty`    — nothing usable. Never sent without an explicit opt-in.
    """
    nested = item.get("metadata")
    if isinstance(nested, dict) and nested:
        missing = [f for f in PRODUCTION_METADATA_FIELDS if nested.get(f) is None]
        return dict(nested), "nested", missing

    rebuilt: dict[str, Any] = {}
    for field, columns in _METADATA_FALLBACKS.items():
        for column in columns:
            value = item.get(column)
            if value is not None:
                rebuilt[field] = value
                break
    missing = [f for f in PRODUCTION_METADATA_FIELDS if rebuilt.get(f) is None]
    return rebuilt, ("rebuilt" if rebuilt else "empty"), missing


# ---------------------------------------------------------------------------
# running the new judge
# ---------------------------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_print_lock = threading.Lock()


def _say(*args: Any) -> None:
    with _print_lock:
        print(*args, flush=True)


def _prompt_fingerprint() -> str:
    """sha256 over the CONTENTS of every prompt file a request is built from.

    Version labels are a promise, not a measurement: editing
    `pass2_agent_quality_v4.md` without bumping `PASS2_VERSION` is exactly the
    workflow an iteration cycle uses, and the old cache key could not see it.
    """
    h = hashlib.sha256()
    for name in sorted(p.name for p in judge.PROMPT_DIR.glob("*.md")):
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update((judge.PROMPT_DIR / name).read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


_PROMPT_FINGERPRINT = _prompt_fingerprint()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_label_for(filename: str) -> str:
    """`pass2_agent_quality_v3.md` → `pass2-agent-quality-v3`.

    The same mapping `judge.PASS1_VERSION` / `PASS2_VERSION` hard-code for the
    current files. Derived rather than passed in, because a label typed by hand
    is a label that will eventually name the wrong file — and that label is
    stamped on every row of the output.
    """
    return Path(filename).stem.replace("_", "-")


def select_prompts(pass1: Path | None, pass2: Path | None) -> dict[str, str]:
    """Point the judge at specific prompt files. Returns what each pass will use.

    This is what makes an A/A run possible: the OLD prompts through the NEW
    code, so the variance the model contributes on its own can be measured and
    subtracted from the variance attributed to a prompt edit. Without it, a
    prompt change and DeepSeek's own run-to-run spread arrive as one number —
    which was the whole finding of the day-13 review: mean +0.09, MAE 10.0.

    The file must live in the prompts directory. `build_pass2_prompt` loads the
    channel-rules block by name from there, so a prompt elsewhere would compose
    against files it cannot see.
    """
    chosen: dict[str, str] = {}
    for which, given, file_attr, version_attr in (
        ("pass1", pass1, "PASS1_PROMPT_FILE", "PASS1_VERSION"),
        ("pass2", pass2, "PASS2_PROMPT_FILE", "PASS2_VERSION"),
    ):
        if given is None:
            chosen[which] = getattr(judge, file_attr)
            continue
        path = Path(given)
        if not path.exists():
            path = judge.PROMPT_DIR / Path(given).name
        if not path.exists():
            raise SystemExit(f"--{which}-prompt: no such file: {given}")
        if path.resolve().parent != judge.PROMPT_DIR.resolve():
            raise SystemExit(
                f"--{which}-prompt must name a file inside {judge.PROMPT_DIR} — "
                f"the prompt composes against channel_rules_*.md loaded from there"
            )
        setattr(judge, file_attr, path.name)
        setattr(judge, version_attr, version_label_for(path.name))
        chosen[which] = path.name
    return chosen


def cache_key(item: dict, only_pass2: bool, history_format: str = "stored") -> str:
    """A canonical hash of everything that can change the answer.

    The old key was the interaction id plus two version labels. It ignored the
    conversation, the metadata, the follow-up history, the input type, the
    rubric, the model, `--only-pass2` and the prompt text itself — so a
    `--only-pass2` smoke run left cache entries with no pass 1 in them, and a
    later full run over the same ids read them back and reported pass-1
    validation as absent for the whole day. A cache that can answer a question
    it was never asked is worse than no cache.
    """
    metadata, source, _ = metadata_for(item)
    history, history_source = followup_source_for(item, history_format)
    canonical = json.dumps({
        "interaction_id": str(item.get("interaction_id")),
        "conversation": item.get("conversation") or "",
        "input_type": input_type_of(item),
        "metadata": metadata,
        "metadata_source": source,
        "followup_history": history,
        "followup_history_source": history_source,
        "only_pass2": bool(only_pass2),
        "pass1_version": judge.PASS1_VERSION,
        "pass2_version": judge.PASS2_VERSION,
        # The all-prompts fingerprint cannot see WHICH file was chosen — every
        # version sits in the same directory, so an A/A run and a B run hash
        # identically under it. The two chosen files are therefore hashed by
        # name AND by content, which is what keeps `--pass2-prompt v3` from
        # reading back a cached v4 answer.
        "prompt_fingerprint": _PROMPT_FINGERPRINT,
        "pass1_prompt_file": judge.PASS1_PROMPT_FILE,
        "pass2_prompt_file": judge.PASS2_PROMPT_FILE,
        "pass1_prompt_sha": _file_sha(judge.PROMPT_DIR / judge.PASS1_PROMPT_FILE),
        "pass2_prompt_sha": _file_sha(judge.PROMPT_DIR / judge.PASS2_PROMPT_FILE),
        "rubric_version": scoring.RUBRIC_VERSION,
        "validator_version": scoring.VALIDATOR_VERSION,
        # What will actually be asked for, not the source default: DEEPSEEK_MODEL
        # overrides it, and a cache keyed on the default would serve a v4-flash
        # answer to a v4-pro run.
        "model": os.getenv("DEEPSEEK_MODEL") or judge.DEFAULT_MODEL,
        "thinking": os.getenv("DEEPSEEK_THINKING") or judge.DEFAULT_THINKING,
        "min_scoreable_chars": MIN_SCOREABLE_CHARS,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_path(out: Path, item: dict, only_pass2: bool,
               history_format: str = "stored") -> Path:
    iid = _SAFE.sub("_", str(item.get("interaction_id")))[:40]
    key = cache_key(item, only_pass2, history_format)[:16]
    return out / "cache" / f"{iid}__{key}.json"


def _with_backoff(fn, attempts: int = 4):
    """Retry the whole pass on 429 and 5xx.

    `DeepSeekClient` already retries inside a single call; this is the outer
    loop that survives a sustained rate limit across a batch, with jitter so
    two workers backing off together do not resynchronise on the retry.
    """
    delay = 4.0
    for attempt in range(attempts):
        try:
            return fn()
        except (judge.JudgeError, httpx.HTTPError) as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            transient = status_code in (429, 500, 502, 503, 504) or status_code is None
            if attempt == attempts - 1 or not transient:
                raise
            time.sleep(delay + random.uniform(0, delay / 2))
            delay *= 2
    raise AssertionError("unreachable")


def evaluate_item(item: dict, client: judge.DeepSeekClient | None,
                  only_pass2: bool, history_format: str = "stored") -> dict[str, Any]:
    """Run the current judge over one item. Never raises; errors are recorded."""
    conversation = item["conversation"] or ""
    metadata, metadata_source, metadata_missing = metadata_for(item)
    history, history_source = followup_source_for(item, history_format)
    out: dict[str, Any] = {
        "interaction_id": str(item["interaction_id"]),
        "prompt_version_pass1": judge.PASS1_VERSION,
        "prompt_version_pass2": judge.PASS2_VERSION,
        "prompt_file_pass1": judge.PASS1_PROMPT_FILE,
        "prompt_file_pass2": judge.PASS2_PROMPT_FILE,
        "rubric_version": scoring.RUBRIC_VERSION,
        "validator_version": scoring.VALIDATOR_VERSION,
        "prompt_fingerprint": _PROMPT_FINGERPRINT[:16],
        "metadata_source": metadata_source,
        "metadata_missing_fields": metadata_missing,
        "followup_history_source": history_source,
        "spoken_chars": len(spoken_content(conversation)),
        "min_scoreable_chars": MIN_SCOREABLE_CHARS,
        "unscoreable": False,
        "error": None,
        "pass1": None,
        "pass2": None,
    }

    # The same gate /evaluate applies, for the same reason: an empty or
    # near-empty transcript is a missing conversation, not a bad one, and the
    # judge cannot tell the difference. Scoring it produces a confident zero.
    body = spoken_content(conversation)
    if len(body) < MIN_SCOREABLE_CHARS:
        out["unscoreable"] = True
        out["unscoreable_reason"] = (
            f"{len(body)} normalised characters of speech (timestamps, speaker "
            f"labels and whitespace runs removed), below the "
            f"{MIN_SCOREABLE_CHARS} needed to score"
        )
        return out

    try:
        if not only_pass2:
            p1 = _with_backoff(lambda: judge.run_pass1(conversation, client=client))
            out["pass1"] = {
                "payload": p1.payload,
                "pass1_validation": p1.validation,
                "usage": p1.usage,
                "prompt_version": p1.prompt_version,
            }

        p2 = _with_backoff(lambda: judge.run_pass2(
            conversation, input_type_of(item),
            metadata=metadata,
            followup_history=history,
            client=client,
        ))
        out["pass2"] = {
            "payload": p2.payload,
            "final_score": p2.score.final_score,
            # The score of the breakdown the model returned, before evidence
            # enforcement touched it. Splitting the delta on this is the only
            # way to tell a prompt effect from a code effect.
            "pre_enforcement_score": p2.pre_enforcement_score,
            "performance_level": p2.score.performance_level,
            "weight_applied": p2.score.weight_applied,
            "gradeable": p2.score.gradeable,
            "modules": p2.score.modules,
            "warnings": p2.warnings,
            "contract_status": p2.contract_status,
            "contract_violations": p2.contract_violations,
            "evidence_rejected": p2.evidence_rejected,
            # Modules struck out because every deduction in them was discarded.
            # Without this the row shows a null module score and nothing says
            # whether the situation never arose or the judge could ground
            # nothing — opposite meanings, identical column.
            "ungradeable_modules": p2.ungradeable_modules,
            "usage": p2.usage,
            "prompt_version": p2.prompt_version,
        }
    except Exception as exc:                       # noqa: BLE001 - recorded, not raised
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def run_all(items: list[dict], out_dir: Path, workers: int,
            only_pass2: bool, refresh: bool,
            history_format: str = "stored") -> list[dict]:
    (out_dir / "cache").mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    todo: list[dict] = []

    for item in items:
        path = cache_path(out_dir, item, only_pass2, history_format)
        if path.exists() and not refresh:
            try:
                results[str(item["interaction_id"])] = json.loads(
                    path.read_text(encoding="utf-8"))
                continue
            except json.JSONDecodeError:
                pass          # a half-written cache file is not a result
        todo.append(item)

    _say(f"{len(results)} cached, {len(todo)} to evaluate, {workers} workers")

    client = judge.DeepSeekClient() if todo else None
    done = 0
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(evaluate_item, item, client, only_pass2,
                            history_format): item
                for item in todo
            }
            for future in as_completed(futures):
                item = futures[future]
                iid = str(item["interaction_id"])
                result = future.result()
                results[iid] = result
                cache_path(out_dir, item, only_pass2, history_format).write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8")
                done += 1
                flag = ("ERROR" if result["error"]
                        else "unscoreable" if result["unscoreable"]
                        else (result["pass2"] or {}).get("contract_status", "?"))
                score = (result["pass2"] or {}).get("final_score")
                _say(f"  [{done}/{len(todo)}] {iid[:8]} {flag} score={score}")

    return [results[str(i["interaction_id"])] for i in items
            if str(i["interaction_id"]) in results]


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _module3(payload: dict) -> tuple[Any, Any]:
    """(refusal flag, unavailable_service_objection score) out of a pass-2 payload."""
    block = ((payload or {}).get("modules") or {}).get("module3_objections") or {}
    check = block.get("refusal_check")
    flag = check.get("agent_refused_or_declared_unavailable") if isinstance(check, dict) else None
    scored = (block.get("breakdown") or {}).get("unavailable_service_objection")
    return flag, scored


def _refusal_consistent(payload: dict) -> bool | None:
    """Does the response agree with itself about whether a refusal happened?

    The same rule `scoring.validate_refusal_link` enforces, reported rather than
    enforced — the old rows were produced before the check existed, and the
    whole question here is how many of them would have failed it.
    """
    flag, scored = _module3(payload)
    if not isinstance(flag, bool):
        return None
    return not (flag and scored is None) and not (not flag and scored is not None)


def _objections_scored(payload: dict) -> int | None:
    """How many Module 3 criteria carry a number — i.e. how many objections the
    model says it saw. The v4 prompt exists to raise this on polite refusals."""
    block = ((payload or {}).get("modules") or {}).get("module3_objections") or {}
    breakdown = block.get("breakdown")
    if not isinstance(breakdown, dict):
        return None
    return sum(1 for k in scoring.CRITERION_MAX["module3_objections"]
               if _num(breakdown.get(k)) is not None)


OBJECTION_CRITERIA = list(scoring.CRITERION_MAX["module3_objections"])


def _objection_values(payload: dict) -> dict[str, float | None]:
    """Each Module 3 criterion's score, or None when the model said `null`.

    The mean number of scored objections is a coarse warning: it moves the same
    amount whether the prompt found one real objection it used to miss or
    invented one that was never raised. What matters is WHICH criterion flipped
    and on how many conversations — `thinking_time_objection` above all, because
    "terminal courtesy = Need Time to Think" is the rule most likely to fire on
    a customer who simply said thank you and hung up.
    """
    block = ((payload or {}).get("modules") or {}).get("module3_objections") or {}
    breakdown = block.get("breakdown")
    if not isinstance(breakdown, dict):
        return {k: None for k in OBJECTION_CRITERIA}
    return {k: _num(breakdown.get(k)) for k in OBJECTION_CRITERIA}


def _evidence_quotes_for(payload: dict, criterion: str, limit: int = 2) -> list[str]:
    """The quotes the response offered for one Module 3 criterion.

    A null→numeric flip without the quote behind it is an unexplained number.
    The whole question about the polite-objection rule is what text the judge
    read as an objection, so the report must print it.
    """
    quotes = []
    for item in (payload or {}).get("evidence") or []:
        if not isinstance(item, dict):
            continue
        key = scoring.evidence_criterion_key(item.get("module"), item.get("criterion"))
        if key and key[1] == criterion:
            quote = item.get("quote")
            if isinstance(quote, str) and quote.strip():
                quotes.append(quote.strip())
    return quotes[:limit]


def _real_ask(payload: dict) -> Any:
    ask = (payload or {}).get("real_ask")
    if isinstance(ask, dict):
        return ask.get("is_real_inquiry")
    return ask if isinstance(ask, bool) else None


def _nulls(modules: dict | None) -> int | None:
    if not isinstance(modules, dict):
        return None
    return sum(1 for k in MODULES if modules.get(k) is None)


def build_row(item: dict, result: dict) -> dict[str, Any]:
    old = item.get("old") or {}
    old_modules = old.get("modules") if isinstance(old.get("modules"), dict) else {}
    p2 = result.get("pass2") or {}
    p1 = result.get("pass1") or {}
    new_modules = p2.get("modules") or {}

    old_final, new_final = _num(old.get("final_score")), _num(p2.get("final_score"))
    pre_score = _num(p2.get("pre_enforcement_score"))
    old_p2_payload = old.get("pass2_payload") or {}
    new_p2_payload = p2.get("payload") or {}
    validation = p1.get("pass1_validation") or {}
    promises = validation.get("promises") or []
    rejected = p2.get("evidence_rejected") or []

    row: dict[str, Any] = {
        "interaction_id": result["interaction_id"],
        "uniqueid": item.get("uniqueid"),
        "started_at": item.get("started_at"),
        "agent_id": item.get("agent_id"),
        "agent_name": item.get("agent_name"),
        "kind": item.get("kind"),
        "input_type": input_type_of(item),
        "duration_seconds": item.get("duration_seconds"),
        "asr_confidence": item.get("asr_confidence"),
        "asr_quality_status": item.get("asr_quality_status"),
        "followup_history_supplied": followup_for(item) != "unavailable",

        "unscoreable": result.get("unscoreable"),
        "spoken_chars": result.get("spoken_chars"),
        "followup_history_source": result.get("followup_history_source"),
        "metadata_source": result.get("metadata_source"),
        "metadata_missing_fields": " | ".join(
            result.get("metadata_missing_fields") or []) or None,
        "error": result.get("error"),

        "old_final_score": old_final,
        "new_final_score": new_final,
        "score_delta": (round(new_final - old_final, 2)
                        if old_final is not None and new_final is not None else None),

        # The delta, split at the only seam that separates cause from cause.
        # `prompt_delta` is what the new prompt judged differently;
        # `enforcement_delta` is what this PR's code handed back on findings the
        # judge would not anchor. Reported as one number they are unreadable:
        # a +15 mean could be a prompt that got kinder or a validator that got
        # stricter, and the two call for opposite responses.
        "pre_enforcement_score": pre_score,
        "prompt_delta": (round(pre_score - old_final, 2)
                         if old_final is not None and pre_score is not None else None),
        "enforcement_delta": (round(new_final - pre_score, 2)
                              if new_final is not None and pre_score is not None else None),
        "old_performance_level": old.get("performance_level"),
        "new_performance_level": p2.get("performance_level"),
        "performance_level_changed": (
            None if not old.get("performance_level") or not p2.get("performance_level")
            else old.get("performance_level") != p2.get("performance_level")),
        "old_weight_applied": _num(old.get("weight_applied")),
        "new_weight_applied": _num(p2.get("weight_applied")),
        "old_nulls": _nulls(old_modules),
        "new_nulls": _nulls(new_modules),
        "new_gradeable": p2.get("gradeable"),
        "new_contract_status": p2.get("contract_status"),
        "new_contract_violations": " | ".join(p2.get("contract_violations") or []) or None,

        "evidence_rejected_count": len(rejected),
        "evidence_restored_points": sum(
            (_num(r.get("restored_to")) or 0) - (_num(r.get("model_score")) or 0)
            for r in rejected),
        "evidence_rejected_criteria": " | ".join(
            f"{r.get('module')}.{r.get('criterion')}" for r in rejected) or None,

        # A module whose every deduction was discarded scores null instead of
        # being restored to 100. Reported separately from `new_nulls`, which
        # counts nulls of every cause including "the situation never arose".
        "ungradeable_modules_count": len(p2.get("ungradeable_modules") or []),
        "ungradeable_modules": " | ".join(
            str(e.get("module")) for e in (p2.get("ungradeable_modules") or [])) or None,

        # The pre-enforcement performance band, for the A/A comparison. The
        # observed "15 of 74 changed band" was measured before enforcement, and
        # comparing it against a post-enforcement band would mix the two causes
        # the whole split exists to keep apart.
        "pre_enforcement_performance_level": scoring.performance_level(pre_score),

        "old_stage_reached": old.get("stage_reached") or old_p2_payload.get("stage_reached"),
        "new_stage_reached": new_p2_payload.get("stage_reached"),

        "old_refusal_flag": _module3(old_p2_payload)[0],
        "new_refusal_flag": _module3(new_p2_payload)[0],
        "old_refusal_consistent": _refusal_consistent(old_p2_payload),
        "new_refusal_consistent": _refusal_consistent(new_p2_payload),
        "old_objections_scored": _objections_scored(old_p2_payload),
        "new_objections_scored": _objections_scored(new_p2_payload),

        "old_real_ask": _real_ask(old.get("pass1_payload") or {}),
        "new_real_ask": _real_ask(p1.get("payload") or {}),

        "pass1_real_ask_quote_valid": validation.get("real_ask_quote_valid"),
        "pass1_promises_total": len(promises) if p1 else None,
        "pass1_promises_invalid": (sum(1 for p in promises if not p.get("quote_valid"))
                                   if p1 else None),
        "pass1_intent_evidence_valid": validation.get("intent_evidence_valid"),
        "pass1_validator_version": validation.get("validator_version"),
    }

    for key in MODULES:
        old_m, new_m = _num(old_modules.get(key)), _num(new_modules.get(key))
        row[f"old_{key}"] = old_m
        row[f"new_{key}"] = new_m
        row[f"delta_{key}"] = (round(new_m - old_m, 2)
                               if old_m is not None and new_m is not None else None)

    # Per-criterion objection flips. `null → numeric` is the over-trigger
    # direction: the model now claims an objection it previously said never
    # arose. `numeric → null` is the opposite. Both are per-criterion because
    # the aggregate hides which rule fired.
    old_obj, new_obj = _objection_values(old_p2_payload), _objection_values(new_p2_payload)
    for criterion in OBJECTION_CRITERIA:
        row[f"old_{criterion}"] = old_obj[criterion]
        row[f"new_{criterion}"] = new_obj[criterion]
        row[f"flip_{criterion}"] = (
            "null_to_numeric" if old_obj[criterion] is None and new_obj[criterion] is not None
            else "numeric_to_null" if old_obj[criterion] is not None and new_obj[criterion] is None
            else None)
        row[f"quotes_{criterion}"] = " ⏐ ".join(
            _evidence_quotes_for(new_p2_payload, criterion)) or None
    return row


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------

def write_jsonl(path: Path, results: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[dict]) -> None:
    import csv
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # utf-8-sig: Excel opens a plain utf-8 CSV as cp1252 and turns every Arabic
    # agent name into mojibake. The BOM is the only thing that stops it.
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})


def _stats(values: list[float]) -> str:
    if not values:
        return "n/a"
    return (f"mean {statistics.fmean(values):+.2f}, "
            f"median {statistics.median(values):+.2f}, "
            f"min {min(values):+.2f}, max {max(values):+.2f}")


def _reason_class(reason: Any) -> str:
    """The rejection reasons, bucketed — the bucket names the place to fix it."""
    text = str(reason or "")
    if "no evidence cited" in text:
        return "nothing quoted"
    if "ASR gap" in text:
        return "quoted the gap marker"
    if "not found in conversation" in text:
        return "quote not in transcript"
    if "empty quote" in text:
        return "empty quote"
    return "other"


def _usage_totals(results: list[dict]) -> dict[str, int]:
    """Token and call totals. `api_calls` counts correction re-asks.

    `run_pass2` now aggregates the usage of both calls into one dict and reports
    `api_calls`, so a conversation that needed a correction contributes both
    calls here instead of only the second.
    """
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "prompt_cache_hit_tokens": 0, "api_calls": 0}
    for result in results:
        for key in ("pass1", "pass2"):
            usage = (result.get(key) or {}).get("usage") or {}
            if not usage:
                continue
            for field in total:
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    total[field] += value
            # pass1 makes exactly one call and reports no counter of its own.
            if "api_calls" not in usage:
                total["api_calls"] += 1
    return total


# Spoken-character thresholds for the gate sensitivity table. 20 is today's
# `MIN_SCOREABLE_CHARS`; the rest are the candidates.
GATE_THRESHOLDS = (20, 50, 100, 200)


def _gate_sensitivity(rows: list[dict]) -> str:
    """How many calls each candidate speech gate would refuse, and what they scored.

    The open question, in one table. A five-second call whose entire transcript
    is "مساء الخير معكرونة من شركة" — 34 characters — clears today's 20-char gate.
    The old judge scored it 0 everywhere with no evidence; under evidence
    enforcement every one of those unsupported zeros is restored and it becomes
    100. Neither number describes anything that happened on that call.

    Raising the gate is therefore tempting and is deliberately NOT done here: it
    would change what every stored score is comparable to, for a reason invisible
    in the data. `MIN_SCOREABLE_CHARS` is an env var so the choice can be made
    from this table and recorded, rather than shipped as a changed constant.
    """
    lines = ["## Speech-gate sensitivity", ""]
    usable = [r for r in rows if r.get("spoken_chars") is not None]
    if not usable:
        return "\n".join(lines + ["No spoken-character counts recorded.", ""])

    lines.append(f"Today's gate is **{MIN_SCOREABLE_CHARS}** characters of speech "
                 "(timestamps and speaker labels stripped). What each candidate would "
                 "refuse, over the rows in this run:")
    lines += ["", "| gate | refused | share | mean old score of the refused | "
                  "mean new score of the refused |", "|---|---|---|---|---|"]
    def _mean(values: list[float]) -> str:
        return f"{statistics.fmean(values):.1f} (n={len(values)})" if values else "n/a"

    for threshold in GATE_THRESHOLDS:
        under = [r for r in usable if r["spoken_chars"] < threshold]
        old = [r["old_final_score"] for r in under if r["old_final_score"] is not None]
        new = [r["new_final_score"] for r in under if r["new_final_score"] is not None]
        label = f"{threshold} (current)" if threshold == MIN_SCOREABLE_CHARS else str(threshold)
        lines.append(f"| {label} | {len(under)} | {len(under) / len(usable):.0%} | "
                     f"{_mean(old)} | {_mean(new)} |")

    shortest = sorted(usable, key=lambda r: r["spoken_chars"])[:10]
    lines += ["", "The ten shortest conversations in the input, with what they scored "
                  "before and after — these are the calls the gate decision is about:", ""]
    lines += ["| id | spoken chars | duration | old | new | status |", "|---|---|---|---|---|---|"]
    for row in shortest:
        status = ("unscoreable" if row["unscoreable"] else row["error"] and "ERROR"
                  or row["new_contract_status"] or "?")
        lines.append(
            f"| `{row['interaction_id'][:8]}` | {row['spoken_chars']} | "
            f"{row['duration_seconds']} | {row['old_final_score']} | "
            f"{row['new_final_score']} | {status} |")
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path, items: list[dict], results: list[dict], rows: list[dict],
                 price_in: float, price_out: float, elapsed: float) -> None:
    by_id = {str(i["interaction_id"]): i for i in items}
    lines: list[str] = []
    add = lines.append

    scored = [r for r in rows if not r["unscoreable"] and not r["error"]]
    deltas = [r["score_delta"] for r in scored if r["score_delta"] is not None]

    add(f"# compare_day — {judge.PASS1_VERSION} / {judge.PASS2_VERSION}")
    add("")
    add(f"Prompts: `{judge.PASS1_PROMPT_FILE}` / `{judge.PASS2_PROMPT_FILE}` · "
        f"rubric {scoring.RUBRIC_VERSION} · validator {scoring.VALIDATOR_VERSION} · "
        f"gate {MIN_SCOREABLE_CHARS} · {elapsed:.0f}s wall clock")
    history_sources: dict[str, int] = {}
    for row in rows:
        key = str(row.get("followup_history_source"))
        history_sources[key] = history_sources.get(key, 0) + 1
    add("")
    add(f"Follow-up history sent: {history_sources}")
    if history_sources.get("fallback-stored"):
        add("")
        add(f"⚠️ **{history_sources['fallback-stored']} item(s) fell back to the "
            "STORED follow-up block** — the input carries no `later_interactions` "
            "rows to re-render in the current production format, so Module 4 was "
            "shown the old string. Nothing in this run tests the current block.")
    add("")
    add("## Counts")
    add("")
    add(f"- items in input: **{len(rows)}**")
    add(f"- evaluated: **{len(scored)}**")
    add(f"- unscoreable (below the {MIN_SCOREABLE_CHARS}-char speech gate): "
        f"**{sum(1 for r in rows if r['unscoreable'])}**")
    add(f"- errored: **{sum(1 for r in rows if r['error'])}**")
    add(f"- contract_failed: **{sum(1 for r in scored if r['new_contract_status'] == 'contract_failed')}**")
    ungradeable = [r for r in scored if r["new_contract_status"] == "ungradeable"]
    add(f"- ungradeable (too little rubric weight survived): **{len(ungradeable)}**")
    for row in ungradeable:
        add(f"  - `{row['interaction_id'][:8]}` — {row['spoken_chars']} spoken chars, "
            f"weight {row['new_weight_applied']}, modules struck out: "
            f"{row['ungradeable_modules'] or 'none (model nulls only)'} "
            f"(old score {row['old_final_score']})")
    struck = [r for r in scored if r["ungradeable_modules_count"]]
    add(f"- calls with at least one `evidence_ungroundable` module: **{len(struck)}**")
    for row in struck:
        add(f"  - `{row['interaction_id'][:8]}` — {row['ungradeable_modules']} "
            f"(final {row['old_final_score']} → {row['new_final_score']})")
    add(f"- comparable to an old score: **{len(deltas)}** "
        f"(old score missing for {sum(1 for r in scored if r['old_final_score'] is None)})")
    add("")

    add("## Score delta (new − old)")
    add("")
    add(f"- {_stats(deltas)}")

    # The same movement, split at the seam that separates cause from cause.
    prompt_deltas = [r["prompt_delta"] for r in scored if r["prompt_delta"] is not None]
    enforcement_deltas = [r["enforcement_delta"] for r in scored
                          if r["enforcement_delta"] is not None]
    add("")
    add("**Split by cause.** A single delta cannot be acted on: the same +15 mean is "
        "either a prompt that judges more kindly or a validator that hands points back, "
        "and those call for opposite responses.")
    add("")
    add("| component | definition | n | stats |")
    add("|---|---|---|---|")
    add(f"| `prompt_delta` | pre-enforcement − old — what the NEW PROMPT judged "
        f"differently | {len(prompt_deltas)} | {_stats(prompt_deltas)} |")
    add(f"| `enforcement_delta` | final − pre-enforcement — what THIS PR's evidence rule "
        f"handed back | {len(enforcement_deltas)} | {_stats(enforcement_deltas)} |")
    add(f"| `score_delta` | final − old — the two together | {len(deltas)} | "
        f"{_stats(deltas)} |")
    add("")
    if enforcement_deltas:
        touched = sum(1 for d in enforcement_deltas if d != 0)
        up = sum(1 for d in enforcement_deltas if d > 0)
        down = sum(1 for d in enforcement_deltas if d < 0)
        add(f"- enforcement moved **{touched}** of {len(enforcement_deltas)} scores "
            f"— **{up}** up, **{down}** down.")
        add("")
        # Before iteration 2 this could only ever move a score up, and the report
        # said so. It is no longer true and the sign is the interesting part: a
        # restored deduction raises the score, but a module struck out as
        # `evidence_ungroundable` leaves the weighted average, which can lower it.
        add("  Restoring an unsupported deduction raises a score. Striking a module "
            "out as `evidence_ungroundable` removes it from the weighted average "
            "instead, which can move the score either way — so unlike before "
            "iteration 2, `enforcement_delta` is no longer one-signed.")
    add("")
    if deltas:
        add(f"- moved up: **{sum(1 for d in deltas if d > 0)}** · "
            f"down: **{sum(1 for d in deltas if d < 0)}** · "
            f"unchanged: **{sum(1 for d in deltas if d == 0)}**")
        # Every one of them, not the first fifteen. The day-13 report announced
        # 17 band changes and printed 15, so the two omitted ids — `5e2a7743`
        # and `bb68337f` — were invisible to the review that had to explain
        # them. A count that does not match its own list is worse than no list.
        changed = [r for r in scored if r["performance_level_changed"]]
        add(f"- performance level changed: **{len(changed)}** "
            f"(all {len(changed)} listed below)")
        for row in changed:
            add(f"  - `{row['interaction_id'][:8]}` "
                f"{row['old_performance_level']} → {row['new_performance_level']} "
                f"({row['old_final_score']} → {row['new_final_score']})")

        # The same question asked before enforcement, which is the number the
        # A/A run compares against.
        pre_changed = [
            r for r in scored
            if r["old_performance_level"] and r["pre_enforcement_performance_level"]
            and r["old_performance_level"] != r["pre_enforcement_performance_level"]]
        add(f"- performance level changed BEFORE enforcement (prompt alone): "
            f"**{len(pre_changed)}**")
    add("")

    add("## Per-module delta")
    add("")
    add("| module | n | mean | median | new nulls | old nulls |")
    add("|---|---|---|---|---|---|")
    for key in MODULES:
        values = [r[f"delta_{key}"] for r in scored if r[f"delta_{key}"] is not None]
        new_null = sum(1 for r in scored if r[f"new_{key}"] is None)
        old_null = sum(1 for r in scored if r[f"old_{key}"] is None)
        if values:
            cell = f"| {statistics.fmean(values):+.2f} | {statistics.median(values):+.2f} "
        else:
            cell = "| n/a | n/a "
        add(f"| `{key}` | {len(values)} {cell}| {new_null} | {old_null} |")
    add("")

    add("## Evidence enforcement")
    add("")
    total_rejected = sum(r["evidence_rejected_count"] for r in scored)
    restored = sum(r["evidence_restored_points"] for r in scored)
    add(f"- findings discarded for missing or unverifiable evidence: **{total_rejected}** "
        f"across **{sum(1 for r in scored if r['evidence_rejected_count'])}** conversations")
    add(f"- criterion points restored to agents: **{restored:.0f}** "
        "(raw criterion points, before module rescaling and weighting)")
    if total_rejected:
        add("")
        add("Every one of these survived the correction re-ask: the judge was shown "
            "the criterion by name and told to anchor it or withdraw it, and did "
            "neither. A finding restored here is a deduction nobody could support.")
        # Counts alone cannot be prioritised. 30 discarded findings worth 5
        # points each and 6 worth 25 read identically in a count column and mean
        # entirely different things for an agent's score — and the REASON says
        # whether the fix belongs in the prompt (nothing quoted) or in the
        # transcript pipeline (quotes that no longer match the cleaned text).
        by_criterion: dict[str, dict[str, Any]] = {}
        for result in results:
            for rej in (result.get("pass2") or {}).get("evidence_rejected") or []:
                key = f"{rej.get('module')}.{rej.get('criterion')}"
                entry = by_criterion.setdefault(
                    key, {"n": 0, "points": 0.0, "reasons": {}, "examples": []})
                entry["n"] += 1
                entry["points"] += ((_num(rej.get("restored_to")) or 0)
                                    - (_num(rej.get("model_score")) or 0))
                reason = _reason_class(rej.get("reason"))
                entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
                if rej.get("quote") and len(entry["examples"]) < 3:
                    entry["examples"].append(
                        (result["interaction_id"][:8], str(rej["quote"])[:120]))
        add("")
        add("| criterion | discarded | points restored | mean per finding | reasons |")
        add("|---|---|---|---|---|")
        for key, entry in sorted(by_criterion.items(), key=lambda kv: -kv[1]["points"]):
            reasons = ", ".join(f"{r} ×{c}" for r, c in
                                sorted(entry["reasons"].items(), key=lambda kv: -kv[1]))
            add(f"| `{key}` | {entry['n']} | **{entry['points']:.0f}** | "
                f"{entry['points'] / entry['n']:.1f} | {reasons} |")

        add("")
        add("Rejected quotes, by criterion — the ones the judge offered and the "
            "validator could not find:")
        for key, entry in sorted(by_criterion.items(), key=lambda kv: -kv[1]["points"]):
            if not entry["examples"]:
                continue
            add("")
            add(f"- `{key}`")
            for iid, quote in entry["examples"]:
                add(f"  - `{iid}` offered: `{quote}`")
    add("")

    add("## Contract failures")
    add("")
    failed = [r for r in scored if r["new_contract_status"] == "contract_failed"]
    if not failed:
        add("None — every response agreed with itself after at most one correction.")
    for row in failed:
        add(f"- `{row['interaction_id'][:8]}` — {row['new_contract_violations']}")
    add("")

    add("## Refusal / objection consistency")
    add("")
    for label, field in (("old", "old_refusal_consistent"), ("new", "new_refusal_consistent")):
        checked = [r for r in scored if r[field] is not None]
        bad = [r for r in checked if r[field] is False]
        add(f"- {label}: **{len(bad)}** inconsistent of {len(checked)} carrying a refusal flag")
    old_obj = [r["old_objections_scored"] for r in scored if r["old_objections_scored"] is not None]
    new_obj = [r["new_objections_scored"] for r in scored if r["new_objections_scored"] is not None]
    if old_obj and new_obj:
        add(f"- Module 3 criteria scored (objections seen): old mean "
            f"{statistics.fmean(old_obj):.2f}, new mean {statistics.fmean(new_obj):.2f} "
            "— a coarse warning only; the per-criterion table below is the measurement")
    add("")

    # The over-trigger metric. A mean moves the same amount whether the prompt
    # found a real objection it used to miss or invented one that was never
    # raised; only the per-criterion flip rate, with the quote that caused it,
    # can tell those apart. `thinking_time_objection` is the one to watch:
    # "terminal courtesy = Need Time to Think" is the rule most likely to fire
    # on a customer who said thank you and hung up.
    add("### Objection flips, per criterion")
    add("")
    comparable = [r for r in scored if r["old_objections_scored"] is not None]
    add(f"Over the **{len(comparable)}** conversations carrying an old Module 3 "
        f"breakdown. `null → numeric` means the new run claims an objection the old "
        f"run said never arose.")
    add("")
    add("| criterion | null → numeric | share | numeric → null | old scored | new scored |")
    add("|---|---|---|---|---|---|")
    for criterion in OBJECTION_CRITERIA:
        up = [r for r in comparable if r[f"flip_{criterion}"] == "null_to_numeric"]
        down = [r for r in comparable if r[f"flip_{criterion}"] == "numeric_to_null"]
        old_n = sum(1 for r in comparable if r[f"old_{criterion}"] is not None)
        new_n = sum(1 for r in comparable if r[f"new_{criterion}"] is not None)
        share = f"{len(up) / len(comparable):.0%}" if comparable else "n/a"
        mark = " ⚠️" if criterion == "thinking_time_objection" else ""
        add(f"| `{criterion}`{mark} | **{len(up)}** | {share} | {len(down)} | "
            f"{old_n} | {new_n} |")
    add("")

    for criterion in OBJECTION_CRITERIA:
        up = [r for r in comparable if r[f"flip_{criterion}"] == "null_to_numeric"]
        if not up:
            continue
        add(f"**`{criterion}` — {len(up)} newly claimed.** The text the judge read as "
            f"an objection:")
        add("")
        for row in up[:10]:
            quotes = row[f"quotes_{criterion}"] or "— no evidence quoted for it —"
            add(f"- `{row['interaction_id'][:8]}` scored {row[f'new_{criterion}']}: "
                f"`{str(quotes)[:220]}`")
        add("")

    add("## Pass-1 quote validation")
    add("")
    with_p1 = [r for r in scored if r["pass1_validator_version"]]
    if not with_p1:
        add("Pass 1 was not run (`--only-pass2`).")
    else:
        bad_ask = [r for r in with_p1 if r["pass1_real_ask_quote_valid"] is False]
        checked_ask = [r for r in with_p1 if r["pass1_real_ask_quote_valid"] is not None]
        bad_promise = [r for r in with_p1 if (r["pass1_promises_invalid"] or 0) > 0]
        total_promises = sum(r["pass1_promises_total"] or 0 for r in with_p1)
        invalid_promises = sum(r["pass1_promises_invalid"] or 0 for r in with_p1)
        add(f"- `real_ask` quotes: **{len(bad_ask)}** failed of {len(checked_ask)} checked "
            f"({len(with_p1) - len(checked_ask)} carried no quote)")
        add(f"- `promises_made_by_agent` quotes: **{invalid_promises}** failed of "
            f"{total_promises}, across {len(bad_promise)} conversations")
        add(f"- real_ask flipped vs old: **"
            f"{sum(1 for r in with_p1 if r['old_real_ask'] is not None and r['new_real_ask'] is not None and r['old_real_ask'] != r['new_real_ask'])}**")
        # One entry per conversation, not one per failing check: an item whose
        # real_ask AND promises both failed is one problem to investigate, and
        # listing it twice under a label that only mentions real_ask is how a
        # promise failure gets read as a quote failure and fixed in the wrong
        # prompt.
        by_result = {x["interaction_id"]: x for x in results}
        flagged = {r["interaction_id"]: r for r in bad_ask + bad_promise}
        for iid, row in list(flagged.items())[:10]:
            item = by_id.get(iid, {})
            payload = ((by_result.get(iid, {}).get("pass1") or {}).get("payload") or {})
            failing = []
            if row["pass1_real_ask_quote_valid"] is False:
                failing.append("real_ask")
            if (row["pass1_promises_invalid"] or 0) > 0:
                failing.append(f"{row['pass1_promises_invalid']} promise(s)")
            add("")
            add(f"- `{iid[:8]}` ({item.get('agent_name') or 'unknown agent'}) — "
                f"failed: {', '.join(failing)}")

            if row["pass1_real_ask_quote_valid"] is False:
                ask = payload.get("real_ask")
                quotes = ([e.get("quote") if isinstance(e, dict) else e
                           for e in (ask.get("evidence") or [])]
                          if isinstance(ask, dict) else [])
                for quote in quotes[:3]:
                    add(f"  - real_ask quote: `{str(quote)[:160]}`")

            # Only the promises that actually failed, by the index the
            # validator recorded, so the quote shown is the quote rejected.
            bad_indexes = {p["index"] for p in
                           ((by_result.get(iid, {}).get("pass1") or {})
                            .get("pass1_validation") or {}).get("promises") or []
                           if not p.get("quote_valid")}
            raw = payload.get("promises_made_by_agent")
            if isinstance(raw, list):
                for i in sorted(bad_indexes)[:3]:
                    if i < len(raw):
                        entry = raw[i]
                        # Same key rule as the validator (judge.py): a
                        # present-but-empty `promise` is the rejected quote,
                        # not a cue to fall through to `quote`.
                        quote = ((entry["promise"] if "promise" in entry
                                  else entry.get("quote"))
                                 if isinstance(entry, dict) else entry)
                        add(f"  - promise[{i}] quote: `{str(quote)[:160]}`")
    add("")

    add("## Top 10 largest score deltas")
    add("")
    ranked = sorted((r for r in scored if r["score_delta"] is not None),
                    key=lambda r: -abs(r["score_delta"]))[:10]
    if not ranked:
        add("No item had both an old and a new score.")
    for row in ranked:
        result = next((x for x in results if x["interaction_id"] == row["interaction_id"]), {})
        add("")
        add(f"### `{row['interaction_id'][:8]}` {row['score_delta']:+.1f} "
            f"({row['old_final_score']} → {row['new_final_score']})")
        add("")
        add(f"- agent: {row['agent_name'] or 'unknown'} · {row['started_at']} · "
            f"ASR {row['asr_quality_status']} ({row['asr_confidence']})")
        add(f"- weight applied: {row['old_weight_applied']} → {row['new_weight_applied']} · "
            f"nulls: {row['old_nulls']} → {row['new_nulls']}")
        add(f"- stage: {row['old_stage_reached']} → {row['new_stage_reached']} · "
            f"objections scored: {row['old_objections_scored']} → {row['new_objections_scored']}")
        moved = [f"`{k}` {row[f'old_{k}']}→{row[f'new_{k}']}" for k in MODULES
                 if row[f"delta_{k}"] not in (None, 0)]
        if moved:
            add(f"- modules moved: {', '.join(moved)}")
        for rej in (result.get("pass2") or {}).get("evidence_rejected") or []:
            add(f"- discarded `{rej.get('module')}.{rej.get('criterion')}` "
                f"{rej.get('model_score')}→{rej.get('restored_to')}: {rej.get('reason')}")
            if rej.get("quote"):
                add(f"  - offered quote: `{str(rej['quote'])[:160]}`")
        for warning in (result.get("pass2") or {}).get("warnings") or []:
            add(f"- warning: {warning[:200]}")
    add("")

    add(_gate_sensitivity(rows))

    usage = _usage_totals(results)
    cost = (usage["prompt_tokens"] / 1e6 * price_in
            + usage["completion_tokens"] / 1e6 * price_out)
    corrected = sum(1 for r in results
                    if any("re-asked once" in w
                           for w in ((r.get("pass2") or {}).get("warnings") or [])))
    add("## Usage")
    add("")
    add(f"- prompt tokens: **{usage['prompt_tokens']:,}** "
        f"(cache hits {usage['prompt_cache_hit_tokens']:,})")
    add(f"- completion tokens: **{usage['completion_tokens']:,}**")
    add(f"- total tokens: **{usage['total_tokens']:,}**")
    # The correction re-ask used to overwrite the first attempt's usage, so the
    # totals undercounted exactly the conversations that cost most.
    add(f"- model calls: **{usage['api_calls']:,}** across "
        f"{sum(1 for r in results if r.get('pass2') or r.get('pass1'))} evaluations "
        f"— **{corrected}** needed a correction re-ask; both calls of each are "
        f"counted here")
    add(f"- estimated cost: **${cost:.4f}** at ${price_in}/${price_out} per 1M in/out "
        "— an estimate at the rates passed in, not a billed figure; cache hits are "
        "charged less and are not discounted here.")
    add("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# A/A comparison — separating DeepSeek's own variance from the prompt's effect
# ---------------------------------------------------------------------------
#
# The day-13 run reported a prompt effect with mean +0.09 and MAE 10.0. Those
# two numbers together say nothing about the prompt: pairing controls the call
# mix, not the model's run-to-run spread, and a judge that answers differently
# on the same input twice produces exactly that shape. Re-running the OLD
# prompts through the NEW code gives a floor for that spread, and the prompt is
# credited only with what exceeds it.
#
#   A_i = A-run pre-enforcement − stored old score   (old prompts, new code)
#   B_i = B-run pre-enforcement − stored old score   (new prompts, new code)
#
# Pre-enforcement on both sides on purpose: enforcement is a code effect and
# belongs to neither prompt.

def read_comparison_csv(path: Path) -> dict[str, dict[str, Any]]:
    import csv
    if not path.exists():
        raise SystemExit(f"no comparison.csv in {path.parent} — run that directory first")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {row["interaction_id"]: row for row in csv.DictReader(fh)}


def _f(row: dict, key: str) -> float | None:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def _variance(values: list[float]) -> float:
    """Population variance. Sample variance would divide two paired runs of the
    same size by different denominators only if their n differed, which it does
    not here — but the ratio is the point, so the estimator is stated."""
    return statistics.pvariance(values) if len(values) > 1 else 0.0


def _rms(values: list[float]) -> float:
    return (sum(v * v for v in values) / len(values)) ** 0.5 if values else 0.0


def aa_compare(a_dir: Path, b_dir: Path, out: Path,
               a_label: str = "A", b_label: str = "B") -> dict[str, Any]:
    """Sol's variance metric over two run directories. No model calls."""
    a_rows = read_comparison_csv(a_dir / "comparison.csv")
    b_rows = read_comparison_csv(b_dir / "comparison.csv")

    paired: list[tuple[str, float, float]] = []
    for iid, a in a_rows.items():
        b = b_rows.get(iid)
        if b is None:
            continue
        old_a, old_b = _f(a, "old_final_score"), _f(b, "old_final_score")
        pre_a, pre_b = _f(a, "pre_enforcement_score"), _f(b, "pre_enforcement_score")
        if None in (old_a, old_b, pre_a, pre_b):
            continue
        if abs(old_a - old_b) > 1e-6:
            continue          # different stored score: not the same comparison
        paired.append((iid, pre_a - old_a, pre_b - old_b))

    A = [x[1] for x in paired]
    B = [x[2] for x in paired]
    var_a, var_b = _variance(A), _variance(B)

    def band_flips(rows: dict[str, dict], ids: list[str], field: str) -> int:
        n = 0
        for iid in ids:
            row = rows[iid]
            old, new = row.get("old_performance_level"), row.get(field)
            if old and new and old != new:
                n += 1
        return n

    ids = [x[0] for x in paired]
    metrics = {
        "n_paired": len(paired),
        "a_dir": str(a_dir), "b_dir": str(b_dir),
        "mean_a": statistics.fmean(A) if A else None,
        "mean_b": statistics.fmean(B) if B else None,
        "var_a": var_a, "var_b": var_b,
        "sd_a": var_a ** 0.5, "sd_b": var_b ** 0.5,
        "mae_a": statistics.fmean([abs(v) for v in A]) if A else None,
        "mae_b": statistics.fmean([abs(v) for v in B]) if B else None,
        "rmse_a": _rms(A), "rmse_b": _rms(B),
        "noise_share": min(1.0, var_a / var_b) if var_b > 0 else None,
        "prompt_attributable_rms": max(0.0, var_b - var_a) ** 0.5,
        "prompt_bias": (statistics.fmean(B) - statistics.fmean(A)) if A and B else None,
        "band_flips_a_pre": band_flips(a_rows, ids, "pre_enforcement_performance_level"),
        "band_flips_b_pre": band_flips(b_rows, ids, "pre_enforcement_performance_level"),
        "band_flips_a_final": band_flips(a_rows, ids, "new_performance_level"),
        "band_flips_b_final": band_flips(b_rows, ids, "new_performance_level"),
    }

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.1%}"

    def num(x: float | None, places: int = 2) -> str:
        return "n/a" if x is None else f"{x:+.{places}f}"

    lines = [
        f"# A/A comparison — {a_label} vs {b_label}",
        "",
        f"`{a_dir}` (A) vs `{b_dir}` (B) · **{len(paired)}** conversations paired "
        "on the same stored old score.",
        "",
        "`A_i` = A-run pre-enforcement − stored old. `B_i` = B-run pre-enforcement "
        "− stored old. Pre-enforcement on both sides: evidence enforcement is a "
        "code effect and belongs to neither prompt.",
        "",
        "| metric | A | B |",
        "|---|---|---|",
        f"| mean | {num(metrics['mean_a'])} | {num(metrics['mean_b'])} |",
        f"| variance | {metrics['var_a']:.2f} | {metrics['var_b']:.2f} |",
        f"| sd | {metrics['sd_a']:.2f} | {metrics['sd_b']:.2f} |",
        f"| MAE | {metrics['mae_a']:.2f} | {metrics['mae_b']:.2f} |",
        f"| RMSE | {metrics['rmse_a']:.2f} | {metrics['rmse_b']:.2f} |",
        f"| band flips (pre-enforcement) | {metrics['band_flips_a_pre']}/{len(paired)} "
        f"| {metrics['band_flips_b_pre']}/{len(paired)} |",
        f"| band flips (final) | {metrics['band_flips_a_final']}/{len(paired)} "
        f"| {metrics['band_flips_b_final']}/{len(paired)} |",
        "",
        "## The verdict metrics",
        "",
        "| | value | reading |",
        "|---|---|---|",
        f"| noise share `min(1, Var(A)/Var(B))` | **{pct(metrics['noise_share'])}** | "
        "the share of B's spread that the model reproduces on its own, with no "
        "prompt change at all. At 100% the prompt explains nothing. |",
        f"| prompt-attributable RMS `sqrt(max(0, Var(B)−Var(A)))` | "
        f"**{metrics['prompt_attributable_rms']:.2f}** | points of movement left "
        "over once A's spread is removed. |",
        f"| prompt bias `mean(B)−mean(A)` | **{num(metrics['prompt_bias'])}** | "
        "whether the new prompt grades higher or lower on average. |",
        "",
    ]

    biggest = sorted(paired, key=lambda x: -abs(x[2] - x[1]))[:15]
    if biggest:
        lines += ["## Where A and B disagree most", "",
                  "| id | A_i | B_i | B−A |", "|---|---|---|---|"]
        lines += [f"| `{iid[:8]}` | {a:+.1f} | {b:+.1f} | {b - a:+.1f} |"
                  for iid, a, b in biggest]
        lines.append("")

    unpaired = sorted(set(a_rows) ^ set(b_rows))
    if unpaired:
        lines += [f"{len(unpaired)} id(s) appear in only one run and are excluded: "
                  + ", ".join(x[:8] for x in unpaired[:20]) + "", ""]

    out.write_text("\n".join(lines), encoding="utf-8")
    (out.parent / "aa_metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


# ---------------------------------------------------------------------------
# M3 fixtures — the prompt-behaviour regression suite
# ---------------------------------------------------------------------------

M3_FIXTURES = (ROOT / "services" / "worker" / "tests" / "fixtures"
               / "m3_unavailable_service_cases.json")

# What the n8n evaluate path sends for a clean single-channel call. Fixtures are
# synthetic, so nothing real can be read off them; sending `{}` instead would
# make several criteria unmeasurable and change what is being tested.
FIXTURE_METADATA = {"asr_confidence": 1, "diarization": "none",
                    "channels": 1, "duration_seconds": 60}


def _m3_outcome(payload: dict) -> tuple[str, Any, Any]:
    """(did the objection fire, its score, the refusal flag)."""
    flag, scored = _module3(payload)
    return ("scored" if scored is not None else "null"), scored, flag


def _m4_outcome(payload: dict) -> tuple[str, Any, Any]:
    """(was Module 4 graded, its score, None).

    `null` is the answer the rubric requires when no agent follow-up can be
    seen, so "scored" and "null" are both correct answers depending on the
    history block — which is exactly what the D6 fixture pair tests.
    """
    block = ((payload or {}).get("modules") or {}).get("module4_followup") or {}
    score = _num(block.get("score"))
    return ("scored" if score is not None else "null"), score, None


CRITERIA = {"m3": _m3_outcome, "m4": _m4_outcome}


def judge_case_once(case: dict, client: judge.DeepSeekClient,
                    criterion: str) -> dict[str, Any]:
    """One pass-2 run over one case. Never raises; the error is the result."""
    try:
        result = _with_backoff(lambda: judge.run_pass2(
            case["conversation"], case.get("input_type", "call_transcript"),
            metadata=case.get("metadata") or FIXTURE_METADATA,
            followup_history=case.get("followup_history") or "unavailable",
            client=client))
    except Exception as exc:                        # noqa: BLE001 - recorded
        return {"outcome": "ERROR", "value": None, "flag": None, "stage": None,
                "final_score": None, "contract_status": None, "quotes": [],
                "error": f"{type(exc).__name__}: {exc}"}

    payload = result.payload
    outcome, value, flag = CRITERIA[criterion](payload)
    quote_field = ("unavailable_service_objection" if criterion == "m3"
                   else "timing")
    return {
        "outcome": outcome,
        "value": value,
        "flag": flag,
        # The stage the run chose. Round 3 found `174898da` nulled on STAGE
        # grounds — "the conversation never reached a price offer, so Modules 3,
        # 4 and 5 were dropped" — and nothing in the report showed it, because a
        # dropped module and a correctly-absent one look identical from the
        # outside. Printing the stage next to the outcome is what makes the
        # difference visible without a diagnostic run.
        "stage": payload.get("stage_reached"),
        "final_score": result.score.final_score,
        "contract_status": result.contract_status,
        "quotes": _evidence_quotes_for(payload, quote_field),
        "usage": result.usage,
        "error": None,
    }


def _majority(outcomes: list[str]) -> str:
    """The outcome a majority of runs agreed on, or 'tie'/'ERROR'.

    Errors are not votes — a rate limit is not the judge's opinion — but a case
    where every run errored has no majority to report, and saying so beats
    reporting the error as a verdict.
    """
    votes = [o for o in outcomes if o != "ERROR"]
    if not votes:
        return "ERROR"
    counts: dict[str, int] = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    best = max(counts.values())
    winners = sorted(k for k, n in counts.items() if n == best)
    return winners[0] if len(winners) == 1 else "tie"


def run_repeated(cases: list[dict], out: Path, name: str, criterion: str,
                 repeat: int = 1, workers: int = 2,
                 preamble: str = "") -> dict[str, Any]:
    """Run every case `repeat` times and report the per-case majority.

    One run of a prompt fixture answers "what did the model say", which is not
    the question. The day-13 review turned on a pair of cases that passed on
    clean synthetic text and failed on real transcripts, and on an A/A run that
    moved 11 of 68 performance bands with no prompt change at all: a single
    green run is inside that noise. Three runs and a majority are not proof
    either, but they distinguish a stable verdict from a coin flip, and both
    numbers are printed so a 2-1 is never read as a 3-0.
    """
    client = judge.DeepSeekClient()
    jobs = [(case, replicate) for case in cases for replicate in range(repeat)]
    runs: dict[str, list[dict[str, Any]]] = {c["id"]: [None] * repeat for c in cases}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(judge_case_once, case, client, criterion): (case, rep)
                   for case, rep in jobs}
        for future in as_completed(futures):
            case, rep = futures[future]
            runs[case["id"]][rep] = future.result()
            done += 1
            _say(f"  [{done}/{len(jobs)}] {case['id']}#{rep + 1} "
                 f"{runs[case['id']][rep]['outcome']}")

    outcomes = []
    for case in cases:
        these = runs[case["id"]]
        majority = _majority([r["outcome"] for r in these])
        expected = case.get("expect_outcome")
        outcomes.append({
            "id": case["id"],
            "stands_for": case.get("stands_for", ""),
            "pattern": case.get("pattern", ""),
            "expected": expected,
            "expected_stage": case.get("expected_stage"),
            "noisy": case.get("noisy", False),
            "runs": these,
            "run_outcomes": [r["outcome"] for r in these],
            "majority": majority,
            "unanimous": len({r["outcome"] for r in these}) == 1,
            "majority_correct": None if expected is None else majority == expected,
            "all_correct": None if expected is None
                           else all(r["outcome"] == expected for r in these),
        })

    judged = [o for o in outcomes if o["majority_correct"] is not None]
    passed = sum(1 for o in judged if o["majority_correct"])

    header = [
        f"# {name} — {repeat}× repeat",
        "",
        f"Prompt `{judge.PASS2_PROMPT_FILE}` (`{judge.PASS2_VERSION}`) · "
        f"model `{os.getenv('DEEPSEEK_MODEL') or judge.DEFAULT_MODEL}` "
        f"thinking=`{os.getenv('DEEPSEEK_THINKING') or judge.DEFAULT_THINKING}` · "
        f"{len(cases)} cases × {repeat} runs.",
        "",
    ]
    if judged:
        header += [f"**{passed}/{len(judged)} correct by majority.** "
                   f"{sum(1 for o in judged if o['all_correct'])} of those were "
                   f"{repeat}/{repeat}.", ""]
    if preamble:
        header += [preamble, ""]

    lines = header + [
        "| case | expected | " + " | ".join(f"run {i + 1}" for i in range(repeat))
        + " | majority | verdict |",
        "|---|---|" + "---|" * repeat + "---|---|",
    ]
    for o in outcomes:
        if o["majority_correct"] is None:
            verdict = "—"
        elif o["majority_correct"] and o["all_correct"]:
            verdict = f"pass {repeat}/{repeat}"
        elif o["majority_correct"]:
            verdict = "pass (majority only)"
        else:
            verdict = "**FAIL**"
        cells = " | ".join(
            f"{r['outcome']}" + (f" ({r['value']:g})" if isinstance(r["value"], (int, float))
                                 and not isinstance(r["value"], bool) else "")
            for r in o["runs"])
        lines.append(f"| `{o['id']}` | {o['expected'] or '—'} | {cells} | "
                     f"{o['majority']} | {verdict} |")

    lines += ["", "## Every run, in full", ""]
    for o in outcomes:
        lines += [f"### `{o['id']}` — {o['pattern']}", ""]
        if o["stands_for"] and o["stands_for"] != "n/a":
            lines.append(f"stands for: `{o['stands_for']}`")
            lines.append("")
        if o.get("expected_stage"):
            lines.append(f"expected stage: `{o['expected_stage']}`"
                         + ("  ·  noisy fixture (truncation, misrecognition, "
                            "`[[ASR_GAP]]`)" if o.get("noisy") else ""))
            lines.append("")
        for i, r in enumerate(o["runs"], 1):
            if r["error"]:
                lines.append(f"- run {i}: ERROR {r['error']}")
                continue
            lines.append(
                f"- run {i}: **{r['outcome']}** value=`{r['value']}` "
                f"flag=`{r['flag']}` stage=`{r.get('stage')}` "
                f"final_score=`{r['final_score']}` "
                f"status=`{r['contract_status']}`")
            if r["quotes"]:
                lines.append(f"  - quoted: {r['quotes']}")
        lines.append("")

    (out / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    (out / f"{name}.json").write_text(
        json.dumps(outcomes, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"passed": passed, "judged": len(judged), "total": len(cases),
            "outcomes": outcomes}


# ---------------------------------------------------------------------------
# M3 — the unavailable-service exclusion / counterweight suite
# ---------------------------------------------------------------------------

def m3_fixture_cases() -> list[dict]:
    spec = json.loads(M3_FIXTURES.read_text(encoding="utf-8"))
    return [{
        "id": c["id"],
        "stands_for": c.get("stands_for", ""),
        "pattern": c.get("pattern", ""),
        "conversation": c["conversation"],
        "input_type": c.get("input_type", "call_transcript"),
        "expect_outcome": c["expect"]["unavailable_service_objection"],
        # Declared by the fixture, reported beside every run, and asserted about
        # the fixture's own SHAPE offline in tests/test_m3_fixtures.py. It is not
        # a pass/fail gate on the model: the question this suite answers is
        # whether the objection fires, and a stage disagreement is a diagnosis
        # of a wrong answer rather than a wrong answer of its own.
        "expected_stage": c.get("expected_stage"),
        "noisy": c.get("noisy", False),
    } for c in spec["cases"]]


def run_m3_fixtures(out: Path, workers: int = 2, repeat: int = 1) -> dict[str, Any]:
    """Run the M3 exclusion/counterweight cases through the live judge.

    These cannot be unit tests: whether `unavailable_service_objection` fires is
    decided by the prompt, and nothing in this repository can be handed a
    transcript and asked. So the fixtures are data, and this is the runner.
    """
    return run_repeated(
        m3_fixture_cases(), out, "m3_fixtures", "m3", repeat, workers,
        preamble=(
            "Seven cases stand for the seven day-13 flips the review named — four "
            "wrong additions, three correct drops — plus two positive controls, "
            "because a prompt that had stopped firing the criterion altogether "
            "would otherwise score 7/7. Five more were added for the v5 "
            "counterweight: the two real v4.1 false negatives (`e779317b`, "
            "`174898da`), the two products the counterweight names (airport/ground "
            "transfer, travel insurance), and the call-transfer boundary control "
            "that must still stay `null`. Every snippet is synthetic: no real "
            "customer text is in this repository.\n\n"
            "Round 4 rebuilt them to the shape of the transcripts rather than the "
            "shape of the rule. Five of the six cases that must fire now stop at "
            "`reception` with nothing priced, packaged or offered — the shape "
            "`174898da` has and every clean v5 fixture lacked — and five cases "
            "carry production ASR damage: a mid-turn truncation, a misrecognised "
            "token, and an `[[ASR_GAP]]` between the request and the refusal, so "
            "no single quote can span both. The stage each run chose is printed "
            "next to its outcome."
        ))


# ---------------------------------------------------------------------------
# real named cases from a compare input, repeated
# ---------------------------------------------------------------------------

def real_cases_from_input(items: list[dict], ids: list[str],
                          history_format: str,
                          expectations: dict[str, str] | None = None) -> list[dict]:
    """The named items, sent exactly as a normal run would send them.

    Prefix matching, because the review and the report name calls by the first
    eight characters of the uuid and typing the whole thing is how a case gets
    silently dropped from a suite that then reports a clean pass.
    """
    by_id = {str(i["interaction_id"]): i for i in items}
    cases, missing = [], []
    for wanted in ids:
        match = by_id.get(wanted) or next(
            (v for k, v in by_id.items() if k.startswith(wanted)), None)
        if match is None:
            missing.append(wanted)
            continue
        iid = str(match["interaction_id"])
        metadata, _, _ = metadata_for(match)
        history, _ = followup_source_for(match, history_format)
        cases.append({
            "id": wanted,
            "stands_for": iid,
            "pattern": "real day-13 call",
            "conversation": match.get("conversation") or "",
            "input_type": input_type_of(match),
            "metadata": metadata,
            "followup_history": history,
            "expect_outcome": (expectations or {}).get(wanted),
        })
    if missing:
        raise SystemExit(f"--repeat-ids: not in the input: {', '.join(missing)}")
    return cases


# ---------------------------------------------------------------------------
# D6 — the Module-4 follow-up fixture pair (the PR1A rollout gate)
# ---------------------------------------------------------------------------

# Two synthetic calls and two follow-up-history blocks, built through
# `render_current_history` — the same function that reproduces
# scripts/sql/02_build_follow_up_history.sql field for field — so the block the
# judge sees here is the block production renders today, not a hand-typed
# lookalike that would test nothing.
#
# The pair exists because of a specific day-13 failure: five calls HAD later
# interactions in the database and all five scored Module 4 = null, because the
# block said only "phone_call by unknown". The current block names the
# direction. That fix is only worth anything if the judge reads it, and reading
# it means BOTH halves:
#
#   (a) an outbound agent follow-up after the call  → Module 4 must be SCORED;
#   (b) a later INBOUND queue call from the same customer, labelled
#       "INBOUND: the customer called in, this is not an agent follow-up"
#       → Module 4 must be NULL.
#
# (b) is the half that matters. A judge that credits an inbound callback as
# agent follow-up hands out 20% of the grade for the customer's own effort, and
# on this corpus nearly every recording is a queue recording — so the error
# would be the normal case, not the edge one.
M4_CALL = (
    "[00:00] AGENT: ألو ترافل جيت، مساء الخير، معك تركي، تفضل\n"
    "[00:05] CUSTOMER: أبغى باكج لجورجيا لأربعة أشخاص في شهر عشرة، أسبوع تقريبا\n"
    "[00:16] AGENT: أبشر، عندنا باكج تبليسي وباتومي ثمان ليالي شامل الفندق والتنقلات\n"
    "[00:27] CUSTOMER: طيب كم السعر؟\n"
    "[00:30] AGENT: أربعة عشر ألف ريال للأربعة، شامل التذاكر\n"
    "[00:37] CUSTOMER: طيب خليني أشاور أهلي وأرد عليك\n"
    "[00:42] AGENT: تمام، أنا برسل لك العرض على الواتساب وأتواصل معك بكرة إن شاء الله\n"
    "[00:52] CUSTOMER: الله يعطيك العافية، مع السلامة\n"
)


def m4_fixture_cases() -> list[dict]:
    outbound = render_current_history([{
        "started_at": "2026-08-14 11:20",
        "channel": "whatsapp",
        "direction": "outbound",
        "hours_after": 19.5,
        "agent_name": "تركي العتيبي",
        "first_message": ("أهلا أبو محمد، عرض جورجيا اللي كلمتك عنه أمس لا زال "
                          "متاح بنفس السعر والأماكن محدودة، شامل الفندق "
                          "والتنقلات — قررتم شي؟"),
    }])
    inbound = render_current_history([{
        "started_at": "2026-08-15 09:05",
        "channel": "phone_call",
        "kind": "q",
        "direction": None,
        "hours_after": 41.0,
        "agent_name": None,
        "first_message": None,
    }])
    return [
        {
            "id": "m4_outbound_agent_followup",
            "stands_for": "D6(a)",
            "pattern": ("an outbound agent WhatsApp follow-up 19.5h after the "
                        "call, with real message content"),
            "conversation": M4_CALL,
            "input_type": "call_transcript",
            "followup_history": outbound,
            "expect_outcome": "scored",
            # Same conversation on both halves, so the same stage: the agent
            # quoted 14,000 and the customer went away to think about it.
            "expected_stage": "negotiation",
            "noisy": False,
        },
        {
            "id": "m4_inbound_customer_callback",
            "stands_for": "D6(b)",
            "pattern": ("a later INBOUND queue call from the same customer — "
                        "labelled as not an agent follow-up"),
            "conversation": M4_CALL,
            "input_type": "call_transcript",
            "followup_history": inbound,
            "expect_outcome": "null",
            "expected_stage": "negotiation",
            "noisy": False,
        },
    ]


def run_m4_fixtures(out: Path, workers: int = 2, repeat: int = 1) -> dict[str, Any]:
    return run_repeated(
        m4_fixture_cases(), out, "m4_fixtures", "m4", repeat, workers,
        preamble=(
            "Both cases send the SAME conversation and differ only in the "
            "FOLLOW-UP HISTORY block, so any difference in Module 4 is "
            "attributable to the block and to nothing else. The blocks are "
            "rendered by `render_current_history`, which reproduces "
            "`scripts/sql/02_build_follow_up_history.sql` field for field."
        ))


# ---------------------------------------------------------------------------

def run_suites(args) -> int:
    """Run whichever prompt-behaviour suites were asked for. 0 if all passed.

    Shared by the two entry points — suites on their own, and suites appended
    to a full day comparison — so the two cannot drift into running the fixture
    set differently.
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY is not set", file=sys.stderr)
        return 2

    suites: list[tuple[str, dict[str, Any]]] = []
    if args.m3_fixtures:
        suites.append(("m3_fixtures",
                       run_m3_fixtures(args.out, args.workers, args.repeat)))
    if args.m4_fixtures:
        suites.append(("m4_fixtures",
                       run_m4_fixtures(args.out, args.workers, args.repeat)))
    if args.repeat_ids:
        if args.input is None:
            print("--repeat-ids needs --input to read the conversations from",
                  file=sys.stderr)
            return 2
        expectations = (json.loads(args.expect.read_text(encoding="utf-8"))
                        if args.expect else None)
        cases = real_cases_from_input(
            load_items(args.input),
            [i.strip() for i in args.repeat_ids.split(",") if i.strip()],
            args.history_format, expectations)
        suites.append(("m3_real_cases", run_repeated(
            cases, args.out, "m3_real_cases", "m3", args.repeat, args.workers,
            preamble=("Real day-13 calls, sent exactly as a normal run sends them "
                      "— production metadata block and follow-up history included. "
                      "No transcript text is written to this file; only the "
                      "verdicts and the judge's own quotes are."))))

    failed = 0
    for name, outcome in suites:
        print(f"written: {args.out / (name + '.md')}")
        if outcome["judged"]:
            print(f"{name}: {outcome['passed']}/{outcome['judged']} correct by "
                  f"majority over {args.repeat} run(s)")
            failed += outcome["judged"] - outcome["passed"]
        else:
            print(f"{name}: {outcome['total']} cases run, no expectations given")
    return 0 if failed == 0 else 1


def main() -> int:
    # Before parse_args, not after: `--help` is printed by argparse from inside
    # parse_args, and the help text has arrows and em-dashes in it. On a cp1252
    # console `--help` therefore died with a UnicodeEncodeError and no help.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Re-score stored evaluations with the current judge and diff them.")
    ap.add_argument("--input", type=Path, default=None, help="JSON array of items")
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument("--pass1-prompt", type=Path, default=None,
                    help="pass-1 prompt file inside app/prompts/ to run instead "
                         "of the current one; the version label is derived from "
                         "the filename (pass1_customer_v4.md → pass1-customer-v4)")
    ap.add_argument("--pass2-prompt", type=Path, default=None,
                    help="pass-2 prompt file inside app/prompts/ to run instead "
                         "of the current one. Together with --pass1-prompt this "
                         "is the A/A baseline: OLD prompts through NEW code")
    ap.add_argument("--history-format", choices=("stored", "current"),
                    default="stored",
                    help="'stored' sends the follow-up block production sent at "
                         "the time; 'current' re-renders it the way production "
                         "renders it today, from `later_interactions` on the "
                         "item, and falls back to the stored string — named in "
                         "the output — when the input carries no such rows")
    ap.add_argument("--aa-compare", type=Path, default=None, metavar="A_DIR",
                    help="compare a previous run directory (the A run) against "
                         "--out (the B run) and write aa_report.md into --out. "
                         "Reads comparison.csv from both; no model calls, no "
                         "--input needed")
    ap.add_argument("--m3-fixtures", action="store_true",
                    help="run the M3 unavailable-service regression fixtures "
                         "through the live judge and write m3_fixtures.md")
    ap.add_argument("--m4-fixtures", action="store_true",
                    help="run the D6 Module-4 follow-up fixture pair through the "
                         "live judge and write m4_fixtures.md. Two synthetic "
                         "calls that differ only in their FOLLOW-UP HISTORY "
                         "block, rendered in the CURRENT production format: an "
                         "outbound agent follow-up (Module 4 must be scored) and "
                         "a later inbound queue call from the same customer "
                         "(Module 4 must be null)")
    ap.add_argument("--repeat-ids", default=None, metavar="ID[,ID...]",
                    help="re-run these interaction ids from --input through the "
                         "live judge and report the per-case majority. Ids may "
                         "be given as the 8-character prefix the reports use. "
                         "Needs --input; use --repeat to say how many times")
    ap.add_argument("--expect", type=Path, default=None,
                    help="JSON {id: \"scored\"|\"null\"} of what the M3 objection "
                         "should do on each --repeat-ids case, so the run reports "
                         "a verdict rather than only an outcome")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="run every fixture / named case N times and report the "
                         "per-case majority alongside all N outputs (default 1). "
                         "One run of a prompt fixture is inside the model's own "
                         "spread — the day-13 A/A run moved 11 of 68 bands with "
                         "no prompt change at all")
    ap.add_argument("--limit", type=int, default=None, help="only the first N items")
    ap.add_argument("--workers", type=int, default=2, help="parallel evaluations (default 2)")
    ap.add_argument("--only-pass2", action="store_true", help="skip pass 1")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the input and print counts; no network, no key needed")
    ap.add_argument("--refresh", action="store_true", help="ignore the on-disk cache")
    ap.add_argument("--allow-incomplete-metadata", action="store_true",
                    help="proceed even though some items carry no nested production "
                         "`metadata` block; the block is reconstructed from top-level "
                         "columns and the missing fields are named in the report")
    ap.add_argument("--price-in", type=float, default=DEFAULT_PRICE_IN,
                    help=f"USD per 1M prompt tokens (default {DEFAULT_PRICE_IN})")
    ap.add_argument("--price-out", type=float, default=DEFAULT_PRICE_OUT,
                    help=f"USD per 1M completion tokens (default {DEFAULT_PRICE_OUT})")
    args = ap.parse_args()

    chosen = select_prompts(args.pass1_prompt, args.pass2_prompt)

    # Two side modes that need no input file and make no comparison of their own.
    if args.aa_compare is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        metrics = aa_compare(args.aa_compare, args.out, args.out / "aa_report.md",
                             a_label=args.aa_compare.name, b_label=args.out.name)
        print(f"written: {args.out / 'aa_report.md'}")
        print(f"paired {metrics['n_paired']} · noise share "
              f"{metrics['noise_share']} · prompt-attributable RMS "
              f"{metrics['prompt_attributable_rms']:.2f} · bias "
              f"{metrics['prompt_bias']}")
        return 0

    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2

    # The prompt-behaviour suites, when nothing else was asked for. They need
    # the live judge, make no comparison against stored scores, and only
    # --repeat-ids needs an input file. Requested ALONGSIDE a day comparison
    # they run after it instead, at the bottom of this function.
    if (args.m3_fixtures or args.m4_fixtures or args.repeat_ids) and (
            args.input is None or args.repeat_ids):
        args.out.mkdir(parents=True, exist_ok=True)
        return run_suites(args)

    if args.input is None:
        print("--input is required unless --aa-compare or a fixture suite is used",
              file=sys.stderr)
        return 2

    items = load_items(args.input)
    if args.limit:
        items = items[: args.limit]

    print(f"input   : {args.input} — {len(items)} items")
    print(f"pass1   : {judge.PASS1_VERSION} ({chosen['pass1']})")
    print(f"pass2   : {judge.PASS2_VERSION} ({chosen['pass2']})")
    print(f"rubric  : {scoring.RUBRIC_VERSION}   validator: {scoring.VALIDATOR_VERSION}")

    unscoreable = [i for i in items
                   if len(spoken_content(i.get("conversation") or "")) < MIN_SCOREABLE_CHARS]
    with_old = [i for i in items if (i.get("old") or {}).get("final_score") is not None]
    with_followup = [i for i in items
                     if followup_for(i, args.history_format) != "unavailable"]

    # Where each item's follow-up block comes from. `fallback-stored` is the one
    # that matters: --history-format current was asked for and could not be
    # honoured, so Module 4 is being shown the format production has stopped
    # using — and nothing in the run would say so otherwise.
    history_sources: dict[str, int] = {}
    for item in items:
        source = followup_source_for(item, args.history_format)[1]
        history_sources[source] = history_sources.get(source, 0) + 1
    kinds: dict[str, int] = {}
    for item in items:
        key = input_type_of(item)
        kinds[key] = kinds.get(key, 0) + 1

    # The metadata block is not decoration: the prompt calls it "computed,
    # authoritative", and criteria whose numbers are absent from it are scored
    # as unmeasurable. Sending `{}` does not make the comparison neutral — it
    # makes the new run answer a different question from the old one, on a
    # difference nothing in the output would record. So it is named and gated,
    # never silent.
    sources: dict[str, int] = {}
    missing_fields: dict[str, int] = {}
    without_nested: list[str] = []
    for item in items:
        _, source, missing = metadata_for(item)
        sources[source] = sources.get(source, 0) + 1
        if source != "nested":
            without_nested.append(str(item["interaction_id"]))
        for field in missing:
            missing_fields[field] = missing_fields.get(field, 0) + 1

    print(f"unscoreable (gate {MIN_SCOREABLE_CHARS} chars): {len(unscoreable)}")
    print(f"carrying an old final_score        : {len(with_old)}")
    print(f"carrying a follow-up history       : {len(with_followup)} "
          f"({len(items) - len(with_followup)} will send 'unavailable')")
    print(f"input types                        : {kinds}")
    print(f"metadata blocks                    : {sources}")
    print(f"follow-up history ({args.history_format:<8})       : {history_sources}")
    if history_sources.get("fallback-stored"):
        print(f"\n--history-format current: {history_sources['fallback-stored']} "
              f"item(s) carry no `later_interactions` rows, so the CURRENT "
              f"production block cannot be rendered for them and the stored "
              f"string is sent instead. Module 4 is not tested by this run.",
              file=sys.stderr)
    if missing_fields:
        print(f"metadata fields missing            : {missing_fields}")

    # Spoken-character distribution, for the gate decision. Printed on every
    # run, dry or live, because it needs no model call.
    buckets = {t: sum(1 for i in items
                      if len(spoken_content(i.get("conversation") or "")) < t)
               for t in GATE_THRESHOLDS}
    print(f"spoken chars below {GATE_THRESHOLDS}   : "
          f"{[buckets[t] for t in GATE_THRESHOLDS]}")

    metadata_ok = not without_nested or args.allow_incomplete_metadata
    if without_nested:
        print(f"\n{len(without_nested)} item(s) carry no nested production `metadata` "
              f"block: {', '.join(x[:8] for x in without_nested[:10])}"
              f"{' …' if len(without_nested) > 10 else ''}", file=sys.stderr)
        print("The judge would be sent a reconstructed or empty block, which is not "
              "what production sent. Fix the export, or pass "
              "--allow-incomplete-metadata to accept it knowingly.", file=sys.stderr)

    if args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "dry_run.json").write_text(json.dumps({
            "input": str(args.input), "items": len(items),
            "unscoreable": len(unscoreable), "with_old_score": len(with_old),
            "with_followup_history": len(with_followup), "input_types": kinds,
            "pass1_version": judge.PASS1_VERSION, "pass2_version": judge.PASS2_VERSION,
            "pass1_prompt_file": judge.PASS1_PROMPT_FILE,
            "pass2_prompt_file": judge.PASS2_PROMPT_FILE,
            "prompt_fingerprint": _PROMPT_FINGERPRINT,
            "rubric_version": scoring.RUBRIC_VERSION,
            "validator_version": scoring.VALIDATOR_VERSION,
            "min_scoreable_chars": MIN_SCOREABLE_CHARS,
            "history_format": args.history_format,
            "followup_history_sources": history_sources,
            "history_format_honoured": not history_sources.get("fallback-stored"),
            "metadata_sources": sources,
            "metadata_missing_fields": missing_fields,
            "items_without_nested_metadata": without_nested,
            "spoken_char_gate_sensitivity": buckets,
            "metadata_ok": metadata_ok,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwritten: {args.out / 'dry_run.json'}")
        if not metadata_ok:
            print("--dry-run: input is NOT usable as-is — see the metadata warning "
                  "above.", file=sys.stderr)
            return 3
        print("--dry-run: input is usable; no model was called.")
        return 0

    if not metadata_ok:
        return 3

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY is not set", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    results = run_all(items, args.out, max(1, args.workers), args.only_pass2,
                      args.refresh, args.history_format)
    elapsed = time.time() - started

    by_id = {str(i["interaction_id"]): i for i in items}
    rows = [build_row(by_id[r["interaction_id"]], r) for r in results]

    write_jsonl(args.out / "new_results.jsonl", results)
    write_csv(args.out / "comparison.csv", rows)
    write_report(args.out / "report.md", items, results, rows,
                 args.price_in, args.price_out, elapsed)

    if args.m3_fixtures or args.m4_fixtures:
        run_suites(args)

    errors = sum(1 for r in rows if r["error"])
    print(f"\nwritten: {args.out / 'new_results.jsonl'}")
    print(f"written: {args.out / 'comparison.csv'}")
    print(f"written: {args.out / 'report.md'}")
    if errors:
        print(f"{errors} item(s) errored — see the `error` column", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
