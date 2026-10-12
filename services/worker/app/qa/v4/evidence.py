"""Quote verification (handoff 5.1): a quote counts only if it is inside the
message it names, and that message was written by the right side.

"Right text, wrong message" is the failure this catches — a phrase that does
appear in the chat, attributed to a line it is not in. A quote that fails is
treated as null, exactly as if the model had not answered.

Bot and system lines never count, for any field: they are not the agent's
words and not the customer's.
"""
from __future__ import annotations

import re
from typing import Any

from ..common import norm
from .transcript import Chat

CUSTOMER, AGENT, EITHER = "customer", "agent", None


def _contains(quote: str, text: str) -> bool:
    """`quote` is a continuous piece of `text`, ignoring the differences a copy
    can introduce without changing a word: whitespace, diacritics, hamza and
    alef forms, ta marbuta, Arabic-Indic vs Western digits, punctuation.

    A quote that is ONLY punctuation ("؟؟") normalises to nothing, so it is
    compared with whitespace collapsed instead of being allowed to match
    everything.
    """
    q = norm(quote)
    if q:
        return q in norm(text)
    squash = lambda s: " ".join(s.split())
    return bool(squash(quote)) and squash(quote) in squash(text)


def verified_line(chat: Chat, quote: Any, line: Any, role: str | None) -> int | None:
    """The line number when the quote is genuinely there, else None."""
    if not isinstance(quote, str) or not quote.strip():
        return None
    msg = chat.line(_as_int(line))
    if msg is None or not msg.is_human:
        return None
    if role is not None and msg.role != role:
        return None
    return msg.n if _contains(quote, msg.text) else None


def _as_int(v: Any) -> int | None:
    """Line numbers come back as 7, "7" or "[7]"; anything else is no line."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        digits = re.sub(r"\D", "", v)
        return int(digits) if digits and len(digits) <= 4 else None
    return None


def check(chat: Chat, block: Any, key: str, role: str | None) -> int | None:
    """Verify `block[key + '_quote']` against `block[key + '_line']`."""
    if not isinstance(block, dict):
        return None
    return verified_line(chat, block.get(f"{key}_quote"), block.get(f"{key}_line"), role)


__all__ = ["AGENT", "CUSTOMER", "EITHER", "check", "verified_line"]
