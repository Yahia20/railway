"""The chat as the model reads it, and as every quote is checked against.

One structure, three readers: the prompt (`Chat.render`), quote verification
(`Chat.line`), and the dashboard's evidence bubbles. Building the text once is
what makes "the quote is inside message n" mean the same thing everywhere.

Format (handoff section 3):

    [1] 09-14 21:41 العميل: ...
    [3] 09-14 21:43 الموظف (أ): ...

Names, phone numbers, links and attachments are masked BEFORE the model sees
the chat — `[اسم العميل]`, `[رقم]`, `[لينك]`, `[مرفق]` — so the masked text is
also what the quotes are verified against and what the dashboard shows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..common import RIYADH
from ..score_v02 import hints

HUMAN_ROLES = frozenset({"customer", "agent"})
SPEAKER = {"customer": "العميل", "agent": "الموظف", "bot": "البوت", "system": "نظام"}
AGENT_LETTERS = "أبجدهوزحطي"

# Every non-text message is an attachment to the model: the prompt's offer and
# attachment_unclear rules are written against "[مرفق]" alone.
ATTACHMENT_TYPES = frozenset({"image", "document", "video", "audio", "file"})
SYSTEM_TEXT = "[رسالة آلية - المحتوى محجوب]"

_TOKEN_URL = re.compile(r"https?://\S*token=\S+")
_URL = re.compile(r"https?://\S+|\bwww\.\S+")
_EMAIL = re.compile(r"\S+@\S+\.\S+")
# Nine or more digits, optionally spaced or dashed: a phone number. A date
# ("2026-10-11", 8 digits) or a price ("12 500 000") is left alone on purpose —
# both are things the scorecard reads.
_PHONE = re.compile(r"(?<!\d)(?:\+|00)?\d(?:[\s-]?\d){8,14}(?!\d)")
# Name tokens that are also everyday words. Masking "الله" as a name is the
# handoff's known masker bug ("بإذن الله" -> "بإذن [اسم العميل]").
_NOT_A_NAME = frozenset({"الله", "عبد", "ابو", "أبو", "ام", "أم", "بن", "بنت", "آل", "ال"})


@dataclass(frozen=True)
class Line:
    """One numbered message, exactly as rendered to the model."""

    n: int
    role: str          # customer | agent | bot | system
    speaker: str       # Arabic label, with (أ)/(ب) when several agents wrote
    at: datetime
    text: str          # masked

    @property
    def is_human(self) -> bool:
        return self.role in HUMAN_ROLES

    def rendered(self) -> str:
        return f"[{self.n}] {self.at.astimezone(RIYADH):%m-%d %H:%M} {self.speaker}: {self.text}"


@dataclass(frozen=True)
class Chat:
    lines: tuple[Line, ...]
    question_lines: tuple[int, ...] = ()
    objection_lines: tuple[int, ...] = ()

    def line(self, n: Any) -> Line | None:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(self.lines):
            return None
        return self.lines[n - 1]

    def render(self) -> str:
        """The user turn for every model call that reads the whole chat."""
        hint = lambda ns: ", ".join(f"[{n}]" for n in ns) or "(none)"
        return ("المحادثة:\n\n" + "\n".join(x.rendered() for x in self.lines)
                + f"\n\nCUSTOMER QUESTIONS: {hint(self.question_lines)}"
                + f"\nOBJECTION LINES: {hint(self.objection_lines)}")


def name_words(customer_name: dict | None) -> list[str]:
    """The tokens of the customer's recorded name worth masking."""
    name = (customer_name or {}).get("name") or ""
    return [w for w in name.split() if len(w) >= 3 and w not in _NOT_A_NAME]


def mask(text: str, names: list[str]) -> str:
    text = _TOKEN_URL.sub("[مرفق]", text)       # Bitrix file links carry a token
    text = _URL.sub("[لينك]", text)
    text = _EMAIL.sub("[إيميل]", text)
    text = _PHONE.sub("[رقم]", text)
    for w in names:
        text = re.sub(rf"(?<!\w){re.escape(w)}(?!\w)", "[اسم العميل]", text)
    return text


def _body(m: dict[str, Any], names: list[str]) -> str:
    if m["role"] == "system":
        return SYSTEM_TEXT
    if m.get("type") in ATTACHMENT_TYPES:
        return "[مرفق]"
    body = (m.get("body") or "").strip()
    return mask(body, names) or "[مرفق]"     # a body that is only a file link masks to "[مرفق]"


def build(thread: dict[str, Any]) -> Chat:
    """The chat from `qa.engine.load_thread()`'s shape (role/type/body/at/agent_ext)."""
    names = name_words(thread.get("customer_name"))
    order: list[str | None] = []
    for m in thread["messages"]:
        if m["role"] == "agent" and m.get("agent_ext") not in order:
            order.append(m.get("agent_ext"))
    letter = {ext: AGENT_LETTERS[i % len(AGENT_LETTERS)] for i, ext in enumerate(order)}

    lines = []
    for i, m in enumerate(thread["messages"], 1):
        speaker = SPEAKER[m["role"]]
        if m["role"] == "agent" and len(order) > 1:
            speaker += f" ({letter[m.get('agent_ext')]})"
        lines.append(Line(i, m["role"], speaker, datetime.fromisoformat(m["at"]), _body(m, names)))

    # The hint lists are chosen by the same code the approved v0.5 run used;
    # the "unavailable" list is not sent any more (prompt note: drop it).
    h = hints(thread)
    return Chat(tuple(lines), tuple(h["questions"]), tuple(h["objection"]))
