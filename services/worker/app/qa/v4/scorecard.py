"""The owner's chat scorecard (2026-10-11): nine weighted items -> a 0-100 chat score.

The model never scores. Each of the three runs of call 1 is turned into item
values HERE, by the rules of handoff section 5, from quotes that passed
verification (`evidence.py`); then the runs are combined by a fixed rule.

    one run      `assess_run()`   -> RunAssessment (items, follow-up, red flag ...)
    three runs   `combine()`      -> Grade (median per item, majority per label)

What a re-grade can and cannot change. Everything below is a pure function of
(the chat, the model's answers): the same answers always give the same grade,
which is why the answers are stored beside the grade. The only source of
run-to-run movement is the model's answers themselves, and the combination
rule — an item applies only if it applies in 2 of 3 runs, its value is the
median — is what keeps one odd run from moving the score.

Nothing sets a chat to zero (the owner removed q35/q37/q38 on 2026-10-11).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from ..common import norm
from .evidence import AGENT, CUSTOMER, EITHER, check, verified_line
from .transcript import Chat

# Owner-approved weights (2026-10-11). An item that does not apply to a chat
# is left out of both the numerator and the denominator.
WEIGHTS: dict[str, int] = {
    "probe": 10, "offer": 15, "match": 5, "followup": 15, "promise": 10,
    "objection": 5, "answered": 15, "correct": 10, "professional": 15,
}

# Dashboard colour bands, owner's rule: >= 80 green, 60-79 yellow, < 60 red.
BANDS = {"good_min": 80, "mid_min": 60}

FOLLOWUP_GAP_HOURS = 4          # a new attempt needs a customer line between, or this gap
FOLLOWUP_PASS = 2               # attempts needed to pass
PROMISE_CLOSED_HOURS = 2        # 5.4b rule 2

OBJECTION_ELEMENTS = ("acknowledge", "reason", "alternative", "question", "next_step", "discount")

# Per objection type, what makes the answer GOOD (5.4). A discount counts as an
# element — the objection is then not IGNORED — but never makes it good alone.
_GOOD = {
    "price": lambda e: bool(e["reason"] or e["alternative"]) and sum(map(bool, e.values())) >= 2,
    "competitor": lambda e: bool(e["question"] or e["reason"]),
    "delay": lambda e: bool(e["next_step"]),
    "product": lambda e: bool(e["alternative"]),
    "trust": lambda e: bool(e["reason"] or e["next_step"]),
}

# Bitrix UF_CRM_CANCEL_REASON values (5.5), plus the two code-made splits.
LOSS_REASONS = ("Expensive Price", "Prefers a Competitor", "Booked online", "Product not Available",
                "visa issue", "Supplier", "Customer changed mind", "Other", "No Response", "Booked")
NO_RESPONSE, NO_FOLLOWUP, NO_RESPONSE_AGENT = "No Response", "No follow-up", "No Response (agent)"
_SILENT = frozenset({NO_RESPONSE, NO_FOLLOWUP})
# An objection type whose definition IS a loss reason's definition (prompt q32 vs q41):
# "too expensive / over budget" = "too expensive for the customer"; "found it
# cheaper or better elsewhere" = "found it cheaper or better at another agency".
_OBJECTION_LOSS = {"price": "Expensive Price", "competitor": "Prefers a Competitor"}
# Who may show each stated reason (prompt: the customer, or the agent for these three).
_AGENT_MAY_SHOW = frozenset({"Product not Available", "visa issue", "Supplier"})

CHAT_TYPES = ("sales", "visa", "after_sales", "other")

# A customer line that only ANNOUNCES a question ("ابغى استفسر") lists no
# request (prompt q25, qa-chat-v4.1). Filler words around it are ignored.
_OPENERS = frozenset(norm(x) for x in (
    "ابغى استفسر", "ابي استفسر", "ابغا استفسر", "بغيت استفسر", "حابب استفسر", "حابه استفسر",
    "عايز استفسر", "عاوز استفسر", "بدي استفسر", "ممكن استفسر", "عندي استفسار", "استفسار",
    "عندي سؤال", "سؤال", "ممكن سؤال", "ممكن اسأل", "ابغى اسأل", "ابي اسأل", "عايز اسأل",
))
_FILLER = frozenset(norm(x) for x in (
    "لو", "سمحت", "سمحتي", "سمحتو", "من", "فضلك", "فضلكم", "بس", "يا", "اخي", "اختي", "اخوي",
    "السلام", "عليكم", "مرحبا", "هلا", "اهلا", "الو", "شكرا", "طيب",
))


def _is_opener(quote: str) -> bool:
    words = [w for w in norm(quote).split() if w not in _FILLER]
    return bool(words) and " ".join(words) in _OPENERS


@dataclass(frozen=True)
class Item:
    value: float                      # 0..1
    lines: tuple[int, ...] = ()       # the evidence, as message numbers


@dataclass
class RunAssessment:
    """What ONE model run says about the chat, after verification."""

    chat_type: str | None
    booking: dict[str, int | None]            # destination/date/travellers -> verified line
    qualified: bool
    offer_line: int | None
    red_flag: str | None                       # 'no_offer' | 'review_attachment' | None
    items: dict[str, Item]                     # every item except 'followup'
    followup_lines: list[int]                  # first line of each grouped attempt
    loss_label: str | None                     # this run's q41 (DeepSeek), validated
    loss_line: int | None
    objections: tuple[tuple[int, str], ...] = ()   # verified (customer line, objection_type)


@dataclass
class Grade:
    score: int | None
    items: dict[str, Item]
    chat_type: str | None
    qualified: bool
    red_flag: str | None
    booking: dict[str, int | None]
    offer_line: int | None
    followup_lines: list[int]
    loss_reason: str | None
    loss_line: int | None
    runs: int = field(default=3)


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

def _block(out: dict, key: str) -> dict:
    v = out.get(key)
    return v if isinstance(v, dict) else {}


def _list(out: dict, key: str) -> list[dict]:
    v = out.get(key)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _hours(chat: Chat, a: int, b: int) -> float:
    return (chat.lines[b - 1].at - chat.lines[a - 1].at).total_seconds() / 3600


def followup_attempts(chat: Chat, out: dict) -> list[int]:
    """5.3: verified follow-up lines grouped into attempts. A new attempt needs
    a customer message in between, or a gap of FOLLOWUP_GAP_HOURS or more."""
    lines = sorted({ln for x in _list(out, "followup")
                    if (ln := verified_line(chat, x.get("quote"), x.get("line"), AGENT))})
    attempts: list[int] = []
    for ln in lines:
        if attempts:
            prev = attempts[-1]
            customer_between = any(chat.lines[i - 1].role == CUSTOMER for i in range(prev + 1, ln))
            if not customer_between and _hours(chat, prev, ln) < FOLLOWUP_GAP_HOURS:
                continue
        attempts.append(ln)
    return attempts


def _promise(chat: Chat, q21: dict) -> Item | None:
    """5.4b: N/A (None) when the agent had no real chance to deliver."""
    promise = check(chat, q21, "promise", AGENT)
    if not promise:
        return None
    delivered = check(chat, q21, "delivery", AGENT)
    if delivered and delivered < promise:
        delivered = None                    # a "delivery" before the promise delivers nothing
    if not delivered:
        condition = q21.get("promise_condition") in ("customer_details", "external")
        if condition and not check(chat, q21, "condition_met", EITHER):
            return None
        closed = check(chat, q21, "closed", CUSTOMER)
        if closed and closed > promise and _hours(chat, promise, closed) <= PROMISE_CLOSED_HOURS:
            return None
    return Item(1.0 if delivered else 0.0, tuple(x for x in (promise, delivered) if x))


def _objection(chat: Chat, objections: list[dict]) -> Item | None:
    """5.4: GOOD 1, WEAK 0.5, IGNORED 0 per verified objection; the item is their mean."""
    values, lines = [], set()
    for it in objections:
        src = verified_line(chat, it.get("source_quote"), it.get("line"), CUSTOMER)
        if not src:
            continue
        el = {k: check(chat, it, k, AGENT) for k in OBJECTION_ELEMENTS}
        good = _GOOD.get(it.get("objection_type"))
        values.append(0.0 if not any(el.values()) else 1.0 if good and good(el) else 0.5)
        lines.update([src, *filter(None, el.values())])
    return Item(sum(values) / len(values), tuple(sorted(lines))) if values else None


def _answered(chat: Chat, requests: list[dict]) -> Item | None:
    """Every verified customer request got a verified answer AFTER it."""
    unanswered, seen = [], False
    for it in requests:
        src = verified_line(chat, it.get("source_quote"), it.get("line"), CUSTOMER)
        if not src or _is_opener(it.get("source_quote") or ""):
            continue
        seen = True
        ans = verified_line(chat, it.get("answer_quote"), it.get("answer_line"), AGENT)
        if not ans or ans < src:
            unanswered.append(src)
    if not seen:
        return None
    return Item(0.0 if unanswered else 1.0, tuple(sorted(set(unanswered))))


def _correct(chat: Chat, out: dict) -> Item:
    """No q26 contradiction (two agent lines) and no q36 money/booking error."""
    q26, q36 = _block(out, "q26"), _block(out, "q36")
    wrong: list[int] = []
    a = verified_line(chat, q26.get("quote_a"), q26.get("line_a"), AGENT)
    if a:   # one line alone = the agent admitting a mistake
        wrong += [a, *filter(None, [verified_line(chat, q26.get("quote_b"), q26.get("line_b"), AGENT)])]
    a = verified_line(chat, q36.get("quote_a"), q36.get("line_a"), EITHER)
    b = verified_line(chat, q36.get("quote_b"), q36.get("line_b"), AGENT)
    if a and b:
        wrong += [a, b]
    return Item(0.0 if wrong else 1.0, tuple(sorted(set(wrong))))


def _professional(chat: Chat, out: dict) -> Item:
    bad = [ln for k in ("q13", "q15", "q16", "q34") if (ln := check(chat, _block(out, k), "violation", AGENT))]
    return Item(0.0 if bad else 1.0, tuple(sorted(set(bad))))


def loss_label(chat: Chat, q41: dict) -> tuple[str | None, int | None]:
    """The run's q41 label, if it is one of the list; its evidence line if verified."""
    label = q41.get("reason")
    if label not in LOSS_REASONS:
        return None, None
    role = EITHER if label in _AGENT_MAY_SHOW else CUSTOMER
    return label, check(chat, q41, "reason", role)


def _probe(chat: Chat, ctype: str | None, booking: dict[str, int | None], probe: int | None) -> Item | None:
    """Applies to a sales chat whose three trip details were not all given
    before the agent's first message."""
    first_agent = next((x.n for x in chat.lines if x.role == AGENT), None)
    if ctype != "sales" or first_agent is None:
        return None
    if all(booking.values()) and max(booking.values()) < first_agent:
        return None
    return Item(1.0 if probe else 0.0, (probe,) if probe else ())


def _waiting_on_customer(chat: Chat, offer: int | None, probe: int | None) -> bool:
    """No offer, the agent's last line asks the customer something, and the
    customer never wrote after it (decided 2026-10-10: not a red flag)."""
    last_agent = next((x.n for x in reversed(chat.lines) if x.role == AGENT), None)
    if offer or last_agent is None:
        return False
    asked = bool(re.search("[؟?]", chat.lines[last_agent - 1].text)) or probe == last_agent
    return asked and not any(x.role == CUSTOMER for x in chat.lines[last_agent:])


def _offer_items(chat: Chat, out: dict, qualified: bool,
                 probe: int | None) -> tuple[int | None, str | None, dict[str, Item]]:
    """5.2: (offer line, red flag, the `offer` and `match` items that apply)."""
    o = _block(out, "offer")
    offer = check(chat, o, "offer", AGENT)
    transferred = check(chat, _block(out, "q18"), "answer", AGENT)
    excused = bool(transferred) or _waiting_on_customer(chat, offer, probe)
    unclear = o.get("attachment_unclear") is True
    items: dict[str, Item] = {}
    red_flag = None
    if qualified and not offer and not excused:
        red_flag = "review_attachment" if unclear else "no_offer"
    if qualified and not excused and not unclear:
        items["offer"] = Item(1.0 if offer else 0.0, (offer,) if offer else ())
    if offer:   # the offer must not conflict with what the customer asked
        mc = check(chat, o, "mismatch_customer", CUSTOMER)
        ma = check(chat, o, "mismatch_agent", AGENT)
        items["match"] = (Item(0.0, tuple(x for x in (mc, ma) if x)) if ma and ma >= offer
                          else Item(1.0, (offer,)))
    return offer, red_flag, items


def assess_run(chat: Chat, out: dict) -> RunAssessment:
    """Item -> value for the items that apply to this chat, in one model run."""
    ctype = _block(out, "chat_type").get("type")
    ctype = ctype if ctype in CHAT_TYPES else None
    b = _block(out, "booking")
    booking = {k: check(chat, b, k, CUSTOMER) for k in ("destination", "date", "travellers")}
    qualified = ctype == "sales" and all(booking.values())
    probe = check(chat, _block(out, "q3"), "answer", AGENT)
    offer, red_flag, items = _offer_items(chat, out, qualified, probe)

    candidates = {"probe": _probe(chat, ctype, booking, probe),
                  "promise": _promise(chat, _block(out, "q21")),
                  "objection": _objection(chat, _list(out, "q32")),
                  "answered": _answered(chat, _list(out, "q25")),
                  "correct": _correct(chat, out),
                  "professional": _professional(chat, out)}
    items.update({k: v for k, v in candidates.items() if v is not None})

    label, label_line = loss_label(chat, _block(out, "q41"))
    objections = tuple((ln, it.get("objection_type")) for it in _list(out, "q32")
                       if (ln := verified_line(chat, it.get("source_quote"), it.get("line"), CUSTOMER)))
    return RunAssessment(ctype, booking, qualified, offer, red_flag, items,
                         followup_attempts(chat, out), label, label_line, objections)


# ---------------------------------------------------------------------------
# Three runs -> one grade
# ---------------------------------------------------------------------------

def _majority(values: list[Any]) -> Any:
    """The value more than half the votes agree on (2 of 3, 2 of 2), else None."""
    if not values:
        return None
    value, n = Counter(values).most_common(1)[0]
    return value if n * 2 > len(values) else None


def split_silence(chat: Chat, label: str | None, attempts: int) -> str | None:
    """5.5: "No Response" is split by code. The customer wrote last and nobody
    answered -> the AGENT did not respond; otherwise fewer than two follow-up
    attempts -> "No follow-up", two or more -> a genuine "No Response"."""
    if label not in _SILENT:
        return label
    humans = [x for x in chat.lines if x.is_human]
    if humans and humans[-1].role == CUSTOMER:
        return NO_RESPONSE_AGENT
    return NO_RESPONSE if attempts >= FOLLOWUP_PASS else NO_FOLLOWUP


def pick_loss_label(votes: list[tuple[str | None, int | None]]) -> tuple[str | None, int | None]:
    """The chat's loss reason from the votes, in this order:

    1. A STATED reason that some vote proved with a verified customer (or, for
       three labels, agent) line outranks "No Response": the prompt defines No
       Response as "nothing in the chat says why", and a verified line saying
       why contradicts it. Measured 2026-10-11: on a chat ending "معليش بغلط
       اسف", runs split No Response / Other, and re-grading moved the score
       38 points through the follow-up item. Among stated reasons, the majority,
       else the first.
    2. Otherwise the majority label.
    3. With no majority, the first vote whose evidence verified (the owner's rule).

    The evidence line comes from a vote for the chosen label that verified."""
    proven = [v for v, ln in votes if v and ln and v not in _SILENT]
    labels = proven or [v for v, _ in votes if v]
    label = _majority(labels) or (proven[0] if proven else None)
    if label is None:
        label = next((v for v, ln in votes if v and ln), None)
    if label is None:
        return None, None
    return label, next((ln for v, ln in votes if v == label and ln), None)


def objection_reason(chat: Chat, runs: list[RunAssessment]) -> tuple[str | None, int | None]:
    """The loss reason the customer's LAST message states as an objection, when a
    majority of runs verified it as a price or competitor objection.

    The prompt's q41 rule is that a reason in the customer's last messages explains
    the ending; measured 2026-10-11, DeepSeek still answered "No Response" in 4 of 6
    runs on a chat ending "الوديعه مرتفعه جدا" while all 6 listed that line as a
    price objection, and the follow-up item moved the score 19 points.
    """
    last = next((x.n for x in reversed(chat.lines) if x.role == CUSTOMER), None)
    kinds = [next((t for ln, t in r.objections if ln == last), None) for r in runs]
    label = _OBJECTION_LOSS.get(_majority(kinds))
    return (label, last) if label else (None, None)


def combine(chat: Chat, runs: list[RunAssessment],
            loss_votes: list[tuple[str | None, int | None]] | None = None) -> Grade:
    """5.8. An item applies if it applies in a majority of runs; its value is
    the median of the runs where it applies; its evidence comes from the first
    run that has that value. `loss_votes` are the loss-reason model's answers
    (call 4); without them the call-1 runs vote."""
    n = len(runs)
    items: dict[str, Item] = {}
    for key in WEIGHTS:
        applied = [r.items[key] for r in runs if key in r.items]
        if key == "followup" or len(applied) * 2 <= n:
            continue
        med = median(sorted(x.value for x in applied))
        # An even count can give a mid-point; snap to an observed value, lower first.
        value = min((x.value for x in applied), key=lambda v: (abs(v - med), v))
        items[key] = Item(value, next(x.lines for x in applied if x.value == value))

    counts = sorted(len(r.followup_lines) for r in runs)
    attempts = counts[n // 2]
    followup_lines = next(r.followup_lines for r in runs if len(r.followup_lines) == attempts)

    label, loss_line = pick_loss_label(loss_votes if loss_votes
                                       else [(r.loss_label, r.loss_line) for r in runs])
    if label is None or label in _SILENT:
        stated, stated_line = objection_reason(chat, runs)
        if stated:
            label, loss_line = stated, stated_line
    loss = split_silence(chat, label, attempts)
    if loss in _SILENT:
        items["followup"] = Item(1.0 if attempts >= FOLLOWUP_PASS else 0.0, tuple(followup_lines))

    total = sum(WEIGHTS[k] for k in items)
    score = round(100 * sum(WEIGHTS[k] * x.value for k, x in items.items()) / total) if total else None

    booking = {k: _majority([r.booking[k] for r in runs]) for k in ("destination", "date", "travellers")}
    return Grade(
        score=score,
        items={k: items[k] for k in WEIGHTS if k in items},
        chat_type=_majority([r.chat_type for r in runs]),
        qualified=_majority([r.qualified for r in runs]) is True,
        red_flag=_majority([r.red_flag for r in runs]),
        booking=booking,
        offer_line=_majority([r.offer_line for r in runs]),
        followup_lines=list(followup_lines),
        loss_reason=loss,
        loss_line=loss_line,
        runs=n,
    )
