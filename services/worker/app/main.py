"""HTTP surface for the worker. n8n orchestrates; this service does the work.

The split is deliberate. n8n is good at scheduling, retries, branching and
showing a business user what ran. It is bad at rubric
arithmetic and evidence validation, which belong in tested Python. So every n8n node here is a
single HTTP call to one of these endpoints.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import budget, db, report
from . import status as pipeline_status  # `status` is FastAPI's, used below
from .config import settings
from .evaluate import judge, metrics, scoring
from .media import links as media_links
from .normalize.phone import try_normalize
from .sources.base import Conversation, Message
from .sources.bitrix_chats import BitrixWebhookSource

log = logging.getLogger("worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

@asynccontextmanager
async def _lifespan(_: FastAPI):
    """Nothing to open — the database pool is lazy and the judge is stateless.
    The shutdown half matters: Railway replaces containers on every deploy, and
    a pool that is not closed leaves its server-side connections to time out."""
    yield
    db.close()


app = FastAPI(title="Customer 360 worker", version="1.0.0", lifespan=_lifespan)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Shared-secret auth. The worker is reachable on Railway's private network,
    but n8n workflows get exported, shared and pasted into chats — so the
    endpoint is never left open on the assumption the network protects it."""
    if not settings.worker_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WORKER_API_KEY not configured")
    if x_api_key != settings.worker_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing X-API-Key")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ParseChatRequest(BaseModel):
    payload: dict[str, Any]


class StoredMessage(BaseModel):
    """One `chat_messages` row, as the database holds it."""

    seq: int
    sender: str
    body: str
    sent_at: str = Field(description="ISO-8601 with offset, straight from timestamptz")


class PrepareChatRequest(BaseModel):
    """A thread already stored by workflow 01c, on its way to the judge.

    Workflow 01c stores and stops, so the conversation the judge must read is
    spread over `chat_messages` rows rather than sitting in a webhook payload.
    This is the same job `/chats/parse` does for a live Bitrix webhook, from
    the other direction.
    """

    external_id: str
    channel: str = "other"
    messages: list[StoredMessage]
    # Every later contact with the same customer, oldest first, straight out of
    # `interactions`. Module 4 is 20% of the grade and asks a question this
    # conversation cannot answer — did the agent come back? — so the judge is
    # given the customer's timeline alongside the thread.
    #
    # Sent as ROWS, rendered here. The bullet format is a rule and rules live in
    # `evaluate/metrics.py`; rendering it in the workflow's SQL instead would be
    # a second copy of the format to keep in step with the first, which is
    # exactly how Module 4 came to be unanswerable on day 13.
    # `None` means the caller did not look; `[]` means it looked and found
    # nothing. Module 4 scores null for the first and zero for the second, and
    # a default of [] here would turn every caller that forgot the field into a
    # silent claim that the agent never followed up.
    later_interactions: list[dict[str, Any]] | None = None


class EvaluateRequest(BaseModel):
    conversation: str
    input_type: Literal["chat"] = "chat"
    metadata: dict[str, Any] = Field(default_factory=dict)
    followup_history: str | None = None
    run_pass1: bool = True
    run_pass2: bool = True


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    """Liveness only — no dependency checks, so Railway does not restart the
    container because DeepSeek had a slow minute."""
    return {"status": "ok", "version": app.version}


@app.get("/ready", dependencies=[Depends(require_api_key)])
def ready() -> dict:
    """What is actually configured. The go-live checklist reads this."""
    def state(*caps: str) -> str:
        try:
            settings.validate_for(*caps)
            return "ready"
        except RuntimeError as exc:
            return str(exc)

    return {
        "database": state("db"),
        "judge": state("judge"),
        "chats_source": state("chats"),
        "rubric_version": scoring.RUBRIC_VERSION,
        "prompt_versions": {"pass1": judge.PASS1_VERSION, "pass2": judge.PASS2_VERSION},
        "prompt_files": {"pass1": judge.PASS1_PROMPT_FILE, "pass2": judge.PASS2_PROMPT_FILE},
        # What this worker will actually ask for. The env var wins over the
        # default, so a stale DEEPSEEK_MODEL on the platform is invisible in the
        # source and visible only here — and after 2026-08-22 the value that
        # matters is whether it still says `deepseek-chat`, an alias the vendor
        # scheduled for removal on 2026-07-24.
        "judge_model": settings.deepseek_model or judge.DEFAULT_MODEL,
        "judge_thinking": settings.deepseek_thinking or judge.DEFAULT_THINKING,
        # The rest of the judge's effective backend configuration. On an
        # OpenRouter-routed reasoning model a missing DEEPSEEK_REASONING_EFFORT
        # is the difference between 14-second answers and content=null on every
        # real prompt — a worker in that state must not look ready-and-normal
        # here (Sol review, 2026-08-24).
        "judge_base_url": os.getenv("DEEPSEEK_BASE_URL") or judge.DEEPSEEK_BASE_URL,
        "judge_reasoning_effort": os.getenv("DEEPSEEK_REASONING_EFFORT"),
        "judge_m4_quarantine": bool(os.getenv("JUDGE_M4_QUARANTINE")),
        "default_phone_region": settings.default_phone_region,
    }


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

@app.post("/chats/parse", dependencies=[Depends(require_api_key)])
def parse_chat(req: ParseChatRequest) -> dict:
    """Normalise a Bitrix webhook payload into our conversation shape.

    Returns the computed metrics alongside, because those must be calculated
    from timestamps here and handed to the judge — never inferred by the model.
    """
    try:
        conv = BitrixWebhookSource.parse(req.payload)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    phone, phone_error = try_normalize(conv.customer_phone_raw, settings.default_phone_region)
    computed = metrics.compute_chat_metrics(conv)

    # The conversation API exposes no deal_id and no field that joins to /deals,
    # so nothing real can be filled in here yet. With SYNTHETIC_DEAL_IDS on we
    # mint a stand-in instead, deterministic per conversation so re-ingesting the
    # same thread lands on the same row.
    #
    # It is prefixed rather than numeric on purpose. bitrix_deal_id is text, so
    # 'synthetic:<id>' stores cleanly and can never be mistaken for, or collide
    # with, a Bitrix deal id — every synthetic row is one LIKE away from being
    # excluded from a revenue figure. The flag also carries into the response so
    # a caller never has to infer it from the prefix.
    deal_id, deal_id_is_synthetic = conv.bitrix_deal_id, False
    if not deal_id and settings.synthetic_deal_ids:
        deal_id, deal_id_is_synthetic = f"synthetic:{conv.external_id}", True

    return {
        "external_id": conv.external_id,
        "external_source": conv.external_source,
        "channel": conv.channel,
        "started_at": conv.started_at.isoformat(),
        "ended_at": conv.ended_at.isoformat() if conv.ended_at else None,
        "customer_phone_e164": phone,
        "phone_error": phone_error,
        "bitrix_deal_id": deal_id,
        "bitrix_deal_id_is_synthetic": deal_id_is_synthetic,
        "bitrix_contact_id": conv.bitrix_contact_id,
        "agent_external_id": conv.agent_external_id,
        "is_bot_only": conv.is_bot_only,
        "has_no_customer_turn": conv.has_no_customer_turn,
        # Two ways a thread is unscoreable, both refusals rather than low scores.
        #
        # Bot-only: the bot qualifies the customer before a human joins, and
        # grading humans on bot messages makes every QA number wrong. Overridable
        # by SCORE_BOT_ONLY_CONVERSATIONS because it is a policy choice.
        #
        # No customer turn: the source labelled every message as the agent, so
        # the transcript is not a conversation. Not overridable — there is no
        # setting under which grading an agent on the customer's own sentences
        # produces a meaningful number.
        "should_evaluate": (
            (not conv.is_bot_only or settings.score_bot_only_conversations)
            and not conv.has_no_customer_turn
        ),
        "messages": [
            {"seq": m.seq, "sender": m.sender, "body": m.body, "sent_at": m.sent_at.isoformat()}
            for m in conv.messages
        ],
        "transcript_text": conv.transcript_text(),
        "metrics": computed.as_dict(),
    }


@app.post("/chats/prepare", dependencies=[Depends(require_api_key)])
def prepare_chat(req: PrepareChatRequest) -> dict:
    """Turn stored `chat_messages` rows into what `/evaluate` needs.

    Exists because storing and scoring are different pipelines. Workflow 01c
    lands the production chat API's messages and stops; the thread the judge
    reads therefore has to be rebuilt from rows, and the rebuild has to produce
    EXACTLY what `/chats/parse` produces from a live webhook — same transcript
    rendering, same metrics, same refusal rules — or a chat scored through one
    path is not comparable with the same chat scored through the other.

    So it builds the same `Conversation` and calls the same
    `metrics.compute_chat_metrics`. Nothing here re-derives a response gap or
    an after-hours rule; both live in `evaluate/metrics.py` and a second copy
    in this function would be a second copy to keep in step.

    `should_evaluate` is the caller's gate, and it is false for two different
    kinds of nothing:

    - `is_bot_only` — no human agent ever joined, so there is no agent to
      grade. Scoring it produces a number about a bot and files it under a
      person.
    - `has_no_customer_turn` — every turn is labelled as the agent, which is a
      labelling fault at the source rather than a conversation. Pass 1 would
      read the customer's words as the agent's and pass 2 would grade the agent
      on sentences the customer wrote, both confidently.
    """
    if not req.messages:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no messages")

    parsed: list[Message] = []
    for m in req.messages:
        try:
            sent_at = datetime.fromisoformat(m.sent_at)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"message seq {m.seq} has an unparseable sent_at {m.sent_at!r}",
            ) from exc
        # chat_messages.sent_at is timestamptz, so an offset is always present
        # in practice. A naive value would silently be read as portal-local by
        # is_after_hours, which is the bug gotcha 11 exists to prevent.
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        sender = m.sender if m.sender in ("customer", "agent", "bot", "system") else "unknown"
        # Voice-note links embed a live Bitrix REST token. It must never reach
        # a model provider, so it is cut here — the one door every stored body
        # passes through on its way to the judge. Nothing else in the text
        # changes: any wider edit to the judge input is a new scoring baseline.
        parsed.append(Message(seq=m.seq, sender=sender, body=media_links.redact(m.body),
                              sent_at=sent_at))

    # Ordered by time, not by the caller's ordering or by seq: seq is renumbered
    # by 01c after every batch, and a thread mid-renumber must still render in
    # the order the messages were actually sent.
    parsed.sort(key=lambda m: (m.sent_at, m.seq))

    conv = Conversation(
        external_id=req.external_id,
        external_source="bitrix_chat_api",
        channel=req.channel,
        started_at=parsed[0].sent_at,
        ended_at=parsed[-1].sent_at,
        messages=parsed,
    )
    computed = metrics.compute_chat_metrics(conv)

    return {
        "external_id": conv.external_id,
        "message_count": len(parsed),
        "is_bot_only": conv.is_bot_only,
        "has_no_customer_turn": conv.has_no_customer_turn,
        "should_evaluate": (
            (not conv.is_bot_only or settings.score_bot_only_conversations)
            and not conv.has_no_customer_turn
        ),
        "transcript_text": conv.transcript_text(),
        "metrics": computed.as_dict(),
        # `None`, not "", when there is nothing to send. `build_pass2_prompt`
        # turns None into the literal word "unavailable", which is what the
        # rubric branches on — an empty string would render as a header with no
        # bullets under it, and the model would have to guess whether that means
        # "we looked and there was nothing" or "we did not look".
        "followup_history": metrics.followup_history_block(
            later_contacts=_redact_later(req.later_interactions)),
    }


def _redact_later(later: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """The follow-up block quotes each later contact's first message, cut at
    300 characters. Redact BEFORE that cut, or a token split across the
    boundary survives as a prefix. `None` stays `None` (rule 2)."""
    if later is None:
        return None
    return [
        {**row, "first_message": media_links.redact(row["first_message"])}
        if isinstance(row.get("first_message"), str) else row
        for row in later
    ]


class NormalizePhonesRequest(BaseModel):
    values: list[str | None] = Field(
        description="Raw phone strings, in any shape the CRM stores them")
    default_region: str | None = Field(
        default=None,
        description="Overrides DEFAULT_PHONE_REGION. Almost never set it — the "
                    "region is a decision, not a per-call preference.")


@app.post("/phones/normalize", dependencies=[Depends(require_api_key)])
def normalize_phones(req: NormalizePhonesRequest) -> dict:
    """Raw CRM phone strings to E.164, in bulk.

    WHY THIS IS AN ENDPOINT AND NOT SQL. The nightly Bitrix pull needs to turn
    contact phones into the E.164 key that identity resolution matches on, and
    n8n can only do that in SQL. A second copy of the rule in SQL is a second
    copy to keep in step — and this particular rule is not a formatting
    convention, it is a decision with a failure mode:

    `0500000000` is a valid Saudi mobile and means nothing in Egypt, so
    DEFAULT_PHONE_REGION=SA is applied to bare national numbers. A bare
    Egyptian number FAILS TO NORMALISE rather than being assigned +966 — a null
    phone is recoverable, a wrong-country match merges two real people.

    Errors are returned per value, never raised: one unparseable number in a
    batch of five hundred must not lose the other four hundred and ninety-nine.
    """
    region = req.default_region or settings.default_phone_region
    out = []
    for raw in req.values:
        e164, error = try_normalize(raw, region)
        out.append({"raw": raw, "e164": e164, "error": error})
    return {
        "region": region,
        "results": out,
        "normalised": sum(1 for r in out if r["e164"]),
        "failed": sum(1 for r in out if r["e164"] is None and r["raw"]),
    }


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

# Below this many NORMALISED characters of speech there is no conversation to
# grade — timestamps, speaker labels and whitespace runs removed first, so the
# count is of what was said and not of how it was rendered.
#
# 100, raised from 20 in PR2 iteration 2 and still env-overridable. The
# sensitivity table on the day-13 run (81 calls: 6 / 9 / 11 / 24 below
# 20 / 50 / 100 / 200) is the evidence: the five calls between 50 and 100
# characters are greeting and dead-air fragments, two of which carried a stored
# score of 36.9 that described nothing. 20 let a whole call of
# "هلا صباح الخير هلا صباح الخير" (29 characters) be graded 33.1 in one run and
# 0.0 in another — the same call, twice, with no agent behaviour in between.
#
# A duration floor was considered and rejected: duration counts silence, hold
# music and IVR routing, none of which is a conversation.
#
# Moving the gate does make new scores incomparable with old ones below 100
# characters. That is the point — those old scores were not measurements — but
# it is a deployment decision, so it stays an env var and is recorded in
# docs/PR2-judge-integrity.md rather than living only in this constant.
MIN_SCOREABLE_CHARS = int(os.getenv("MIN_SCOREABLE_CHARS", "100"))


def spoken_content(transcript: str) -> str:
    """The transcript with timestamps and speaker labels removed.

    Delegates to the validator's normaliser so the gate and quote matching
    share one definition of "what was actually said". Two definitions would
    mean a call the gate calls empty and the validator calls quotable.
    """
    return scoring.strip_transcript_furniture(transcript)


def _unscoreable(reason: str) -> dict:
    """A refusal shaped like a result, so callers store it like one.

    Every key a successful `pass2` carries is present here with its
    not-applicable value. A consumer that has to test for a missing key is a
    consumer that will one day forget to, and the failure mode of forgetting is
    a `None` treated as a score of zero.
    """
    return {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "rubric_version": scoring.RUBRIC_VERSION,
        "pass2": {
            "payload": {}, "final_score": None, "performance_level": None,
            "weight_applied": 0.0, "gradeable": False, "modules": {},
            "warnings": [reason], "prompt_version": judge.PASS2_VERSION,
            # agent_evaluations.model is NOT NULL, and a refusal still gets a
            # row — the fact that a call was looked at and found unscoreable is
            # worth recording. Naming the non-event beats writing the model
            # that was never asked, which would read as a real evaluation.
            "model": "none (refused before any model call)",
            "usage": None, "input_hash": None,
            # Same keys as a real result, so a consumer never has to branch on
            # their absence. Third status value: neither a good evaluation nor
            # a self-contradicting one — there was nothing to evaluate.
            "contract_status": "unscoreable",
            "contract_violations": [], "evidence_rejected": [],
            "ungradeable_modules": [], "pre_enforcement_score": None,
        },
    }


@app.post("/evaluate", dependencies=[Depends(require_api_key)])
def evaluate(req: EvaluateRequest) -> dict:
    """Run the two passes and return both, scored locally.

    **The usable-score rule.** `pass2` always carries `contract_status`,
    `gradeable` and `final_score`, on every path including the pre-model
    refusal. A score may be stored, averaged, reported or shown to an agent
    ONLY when all three agree:

        contract_status == "ok"  AND  gradeable  AND  final_score is not None

    Any other combination is a row that records why there is no number, and it
    must be stored as such — with a null score — never retried as a judge
    fault and never counted in a denominator. The four statuses:

    - `ok` — scored. `gradeable` true and `final_score` a number.
    - `contract_failed` — the response still contradicted itself after one
      correction. No score; `contract_violations` says what broke.
    - `ungradeable` — too little of the rubric survived evidence enforcement to
      average. No score; `ungradeable_modules` says which modules were struck.
    - `unscoreable` — the transcript held too little speech to grade and no
      model was called at all.

    The three fields are redundant on purpose. `final_score` alone cannot
    distinguish "no number because the call was empty" from "no number because
    the judge broke its own contract", and every reporting bug this pipeline
    has had came from a consumer inferring one from the other.
    """
    settings.validate_for("judge")

    # An empty transcript is not a bad conversation, it is a missing one, and
    # the judge cannot tell the difference: asked to grade nothing it returns
    # zeros with full confidence. Seen live on 2026-08-11 — 17 of 20 calls came
    # back from the ASR Space with empty text and confidence 0 under burst load,
    # and every one was stored as final_score 0, "Below Average", gradeable.
    # That is an agent's scorecard destroyed by someone else's rate limit.
    body = spoken_content(req.conversation)
    if len(body) < MIN_SCOREABLE_CHARS:
        return _unscoreable(
            f"transcript holds {len(body)} normalised characters of speech "
            f"(timestamps, speaker labels and whitespace runs removed), below "
            f"the {MIN_SCOREABLE_CHARS} needed to score: treated as a failed or "
            f"abandoned call, not a badly handled one"
        )

    client = judge.DeepSeekClient(settings.deepseek_api_key, settings.deepseek_model,
                                  thinking=settings.deepseek_thinking)

    out: dict[str, Any] = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "rubric_version": scoring.RUBRIC_VERSION,
    }

    if req.run_pass1:
        # Same contract as pass 2 below: a judge that could not be reached or
        # answered unusably is a 422, never a 500. Measured 2026-08-24 — an
        # OpenRouter rate limit during pass 1 escaped as an unhandled
        # JudgeError, and n8n recorded `500 - "Internal Server Error"`, which
        # names no cause and reads like a worker fault rather than a throttled
        # upstream.
        try:
            p1 = judge.run_pass1(req.conversation, client=client)
        except judge.JudgeError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"pass 1 could not be produced: {exc}",
            ) from exc
        out["pass1"] = {
            "payload": p1.payload, "prompt_version": p1.prompt_version,
            "model": p1.model, "usage": p1.usage, "input_hash": p1.input_hash,
            # Also inside payload; lifted out because the alert rules read it
            # and should not have to reach through a jsonb blob to find out
            # whether the quote behind a follow-up task was ever real.
            "pass1_validation": p1.validation,
            # One row per distinct thing the customer asked for, evidence
            # already checked (017). Lifted out for the same reason: n8n writes
            # these straight into interaction_requests and must not have to
            # parse the payload to do it. Empty on a pre-v6 prompt.
            "requests": p1.requests,
        }
        out.setdefault("model_calls", []).append(judge.cost_row(
            "pass1_customer", model=p1.model, prompt_version=p1.prompt_version,
            input_hash=p1.input_hash, usage=p1.usage,
        ))

    if req.run_pass2:
        # A response that still breaks the rubric after the re-ask is a bad
        # response, not a server fault. Surface it as such: a 500 with a stack
        # trace tells the caller nothing about which criterion the model broke.
        try:
            p2 = judge.run_pass2(
                req.conversation, req.input_type,
                metadata=req.metadata, followup_history=req.followup_history, client=client,
            )
        except (scoring.RubricError, judge.JudgeError) as exc:
            # 422 now means only one thing: the model's output was not usable
            # JSON at all. A response that parses but contradicts itself comes
            # back 200 with contract_status="contract_failed" and no score —
            # the caller stores the row and can see why it has no number.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"judge response was structurally unusable: {exc}",
            ) from exc
        out["pass2"] = {
            "payload": p2.payload,
            "final_score": p2.score.final_score,
            "performance_level": p2.score.performance_level,
            "weight_applied": p2.score.weight_applied,
            "gradeable": p2.score.gradeable,
            "modules": p2.score.modules,
            "warnings": p2.warnings,
            "prompt_version": p2.prompt_version,
            "model": p2.model,
            "usage": p2.usage,
            "input_hash": p2.input_hash,
            "contract_status": p2.contract_status,
            "contract_violations": p2.contract_violations,
            "evidence_rejected": p2.evidence_rejected,
            # Additive. Names the modules struck out as `evidence_ungroundable`
            # — every deduction in them discarded — which is why the score can
            # be null on a response with no contract violation at all.
            "ungradeable_modules": p2.ungradeable_modules,
            # The weighted score of the breakdown the model returned, taken
            # before evidence enforcement touched it. Diagnostic only — it is
            # NOT a usable score and must never be stored as one.
            "pre_enforcement_score": p2.pre_enforcement_score,
        }
        # Recorded whatever the contract status: a response that broke the
        # rubric still burned tokens, and a cost table that only counts the
        # successes understates the bill by exactly the retries.
        out.setdefault("model_calls", []).append(judge.cost_row(
            "pass2_agent", model=p2.model, prompt_version=p2.prompt_version,
            input_hash=p2.input_hash, usage=p2.usage,
            succeeded=p2.contract_status == "ok",
            error=None if p2.contract_status == "ok" else p2.contract_status,
        ))

    return out


@app.post("/score/recompute", dependencies=[Depends(require_api_key)])
def recompute(modules: dict[str, Any]) -> dict:
    """Re-run the weighting on stored module breakdowns, no model call.

    Use this after a rubric revision to rescore history without paying for
    re-evaluation, and to prove a score is reproducible from its breakdown.
    """
    result = scoring.compute(modules)
    return {
        "final_score": result.final_score,
        "performance_level": result.performance_level,
        "weight_applied": result.weight_applied,
        "modules": result.modules,
        "gradeable": result.gradeable,
        "warnings": result.warnings,
    }


# ---------------------------------------------------------------------------
# Report
#
# The page is served WITHOUT the API key and contains no data; the data
# endpoint behind it requires the key like every other endpoint here. That
# split exists because a browser cannot set an X-API-Key header on a normal
# navigation, and the alternative — putting the key in the query string —
# writes a shared secret into browser history, proxy logs and every Referer
# header the page emits.
# ---------------------------------------------------------------------------

REPORT_PAGE = Path(__file__).resolve().parent / "static" / "report.html"
SPEND_PAGE = Path(__file__).resolve().parent / "static" / "spend.html"


@app.get("/report", response_class=HTMLResponse, include_in_schema=False)
def report_page() -> HTMLResponse:
    """The operational report, as a page. Holds no customer data: it asks for
    WORKER_API_KEY and fetches /report/data itself."""
    try:
        html = REPORT_PAGE.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"report page missing: {exc}") from exc
    return HTMLResponse(
        html,
        headers={
            # Never let a shared cache hold the shell, and never let this page
            # be framed by anything.
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.get("/report/data", dependencies=[Depends(require_api_key)])
def report_data(days: int = 30, limit: int = report.SAMPLE_LIMIT) -> dict:
    """Every number the report shows, in one response.

    `days` bounds the cost panels only; counts and the reconciliation verdict
    are over all of history, because "a request nobody logged" does not stop
    being one after thirty days.
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 500))
    try:
        return report.build(days=days, limit=limit)
    except db.DatabaseUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


# What is broken right now, and why. /health stays what Railway's healthcheck
# hits (process up); this is whether the PIPELINE is up, which a process that
# answers 200 says nothing about.
STATUS_PAGE = Path(__file__).resolve().parent / "static" / "status.html"


@app.get("/status", response_class=HTMLResponse, include_in_schema=False)
def status_page() -> HTMLResponse:
    """Holds no data and no key, like /report: it fetches /status/data."""
    try:
        html = STATUS_PAGE.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"status page missing: {exc}") from exc
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    })


@app.get("/status/data", dependencies=[Depends(require_api_key)])
def status_data() -> dict:
    try:
        return pipeline_status.build()
    except db.DatabaseUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


# ---------------------------------------------------------------------------
# Bitrix CRM pulls
#
# WHY THESE ARE HERE AND NOT IN n8n. Workflow 04 called crm.deal.list directly
# with `start: 0` and never followed `next`. Bitrix pages every list method at
# 50 rows, so 04 imported 50 of the 676 deals modified in the last week, every
# night, and reported success — the missing 626 were invisible because a short
# page and a small result set look identical.
#
# Paging is a loop with a stop condition, which is the kind of thing this split
# puts in Python: n8n schedules and branches, the worker does the work.
# ---------------------------------------------------------------------------

# Every field the Upsert deals SQL reads must appear here, or `d->>'FIELD'`
# silently yields NULL. CLOSED was missing and `deals.is_closed` is NOT NULL, so
# the insert violated the constraint on the very first row and workflow 04 had
# never stored a single Bitrix deal. `test_deal_select_covers_every_field_the_sql_reads`
# now fails if the two drift apart again.
DEAL_SELECT = [
    "ID", "TITLE", "STAGE_ID", "STAGE_SEMANTIC_ID", "CATEGORY_ID", "CLOSED",
    "OPPORTUNITY", "CURRENCY_ID", "CONTACT_ID", "ASSIGNED_BY_ID",
    "SOURCE_ID", "DATE_CREATE", "DATE_MODIFY", "BEGINDATE", "CLOSEDATE",
]

# A CONSTANT FOR THE SAME REASON DEAL_SELECT IS ONE: so a test can read it.
#
# A field absent from this list arrives NULL rather than as an error (gotcha
# 16), so the omission is invisible at the call site and only shows up wherever
# the value was supposed to land — which for NAME was `customers.display_name`,
# empty since 002. `test_contact_select_covers_every_field_the_sql_reads` now
# parses workflow 04's SQL and fails if the two ever drift apart again.
CONTACT_SELECT = ["ID", "PHONE", "NAME", "SECOND_NAME", "LAST_NAME"]

# NEWEST FIRST, because `max_rows` cuts the END of the list. Bitrix's default
# order is ID ascending, so a truncated pull kept the oldest deals and dropped
# the new ones. On 2026-10-04/05 something touched 19,955 deals at once; every
# one of them matched `>DATE_MODIFY`, the 5,000 cap filled with deals from
# 2025, and for two nights no deal opened that week reached the database —
# 3 of 149 conversations on 10-06 found their deal. With ID descending a cut
# drops the oldest, which the previous nights already stored.
DEAL_ORDER = {"ID": "DESC"}


class BitrixDealsRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=365,
                      description="Look back this many days on DATE_MODIFY.")
    max_rows: int = Field(default=5000, ge=1, le=20000)


class BitrixContactsRequest(BaseModel):
    contact_ids: list[str] = Field(default_factory=list)
    max_rows: int = Field(default=5000, ge=1, le=20000)


def _bitrix_rest() -> "BitrixRestSource":
    # validate_for raises a bare RuntimeError, which becomes a 500 and an ASGI
    # traceback — "Internal Server Error" for what is really "nobody set
    # BITRIX_WEBHOOK_TOKEN". Workflow 04 runs unattended at 03:20, so the
    # difference between those two is whether tomorrow starts with a diagnosis
    # or with a log dig. 503 with the variable named, like /report/data does.
    try:
        settings.validate_for("chats")
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    from .sources.bitrix_chats import BitrixRestSource
    return BitrixRestSource(
        portal_domain=settings.bitrix_portal_domain,
        webhook_token=settings.bitrix_webhook_token,
        user_id=settings.bitrix_webhook_user_id,
    )


@app.post("/bitrix/deals", dependencies=[Depends(require_api_key)])
def bitrix_deals(req: BitrixDealsRequest) -> dict:
    """Every deal modified in the last `days`, all pages.

    The shape is Bitrix's own — `{"result": [...]}` — so the SQL in workflow 04
    that reads `$json.result` did not have to change when paging moved here.

    ONLY THE ALLOWLISTED FIELDS (rule 8). The raw deal carries
    UF_CRM_1781281581, which holds prose addressed to a bot, and a dozen fields
    holding another system's AI verdicts on the very things this project derives
    from the conversation with evidence. `select` is the allowlist.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=req.days)).date().isoformat()
    src = _bitrix_rest()
    try:
        rows, total, requests = src.list_all(
            "crm.deal.list", select=DEAL_SELECT,
            filter={">DATE_MODIFY": since}, order=DEAL_ORDER,
            max_rows=req.max_rows)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"bitrix: {exc}") from exc

    return {
        "result": rows,
        # `total` is what Bitrix says matched. When it exceeds len(result) the
        # pull was truncated by max_rows, and the caller can see that rather
        # than inferring completeness from a successful response.
        "total": total,
        "fetched": len(rows),
        "truncated": len(rows) < total,
        "requests": requests,
        "since": since,
    }


@app.post("/bitrix/contacts", dependencies=[Depends(require_api_key)])
def bitrix_contacts(req: BitrixContactsRequest) -> dict:
    """Phones for a specific list of contact ids, all pages.

    Bitrix caps a `filter: {ID: [...]}` result at 50 like everything else, so
    asking for 500 ids and reading one page returned the first 50 and silently
    dropped the rest — the same bug as the deals pull, one node later.
    """
    ids = [str(i).strip() for i in req.contact_ids if str(i).strip()]
    if not ids:
        return {"result": [], "total": 0, "fetched": 0,
                "truncated": False, "requests": 0}

    src = _bitrix_rest()
    try:
        rows, total, requests = src.list_all(
            # NAME / SECOND_NAME / LAST_NAME are in CONTACT_SELECT because a
            # customer with no name is the most visible hole in every report
            # this system produces, and the CRM has been able to answer it all
            # along. The request succeeded without them, the objects came back
            # without them, and nothing anywhere said so.
            "crm.contact.list", select=CONTACT_SELECT,
            filter={"ID": ids}, max_rows=req.max_rows)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"bitrix: {exc}") from exc

    return {"result": rows, "total": total, "fetched": len(rows),
            "truncated": len(rows) < total, "requests": requests,
            "requested_ids": len(ids)}


# ---------------------------------------------------------------------------
# Money. See app/budget.py for why a pipeline needs a gate before it claims.
# ---------------------------------------------------------------------------

class PreflightRequest(BaseModel):
    providers: list[str] | None = Field(
        default=None,
        description="Which providers to probe. Default: all registered ones.")


@app.post("/budget/preflight", dependencies=[Depends(require_api_key)])
def budget_preflight(req: PreflightRequest) -> dict:
    """May we start spending, and with whom?

    Called FIRST by every workflow that costs money, before it claims any
    work. A job that is never claimed keeps its status and its attempt count,
    so an outage costs nothing and loses nothing however long it lasts.

    This endpoint only READS (rule 11): it returns the verdict and n8n writes
    it into `provider_status`, so there is exactly one write path.
    """
    return budget.preflight(req.providers)


@app.get("/budget/spend", dependencies=[Depends(require_api_key)])
def budget_spend() -> dict:
    """Where the money went this month, per provider and per component."""
    return budget.spend_report()


@app.get("/spend", response_class=HTMLResponse, include_in_schema=False)
def spend_page() -> HTMLResponse:
    """The spend report as a page. Holds no data and no key: it fetches
    /budget/spend itself, for the same reason /report does — a browser cannot
    set a header on a navigation, and a key in the URL lands in history and in
    every proxy log along the way."""
    try:
        html = SPEND_PAGE.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"spend page missing: {exc}") from exc
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )


# ---------------------------------------------------------------------------
# Chat media archive — workflow 09 downloads through these; see app/media/.
# Mounted last so its routes can never shadow an existing one.
# ---------------------------------------------------------------------------

from .media import api as media_api  # noqa: E402  (needs require_api_key above)
from .media import reader as media_reader  # noqa: E402

CONVERSATION_PAGE = Path(__file__).resolve().parent / "static" / "conversation.html"


@app.get("/conversations", response_class=HTMLResponse, include_in_schema=False)
def conversation_page() -> HTMLResponse:
    """Read one deal's chat with its files in place. Holds no data and no key:
    like /report it asks for WORKER_API_KEY and fetches the JSON itself."""
    try:
        html = CONVERSATION_PAGE.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"conversation page missing: {exc}") from exc
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


app.include_router(media_api.build_router(require_api_key))
app.include_router(media_reader.build_router(require_api_key))
