"""Source registry.

`get_chat_source()` is the only place the rest of the
application learns which implementation is live. Swapping the mock for the real
Bitrix client is an env var, not a code change — which is what lets the
pipeline be built, tested and demoed before either API exists.
"""
from __future__ import annotations

import os

from .base import ChatSource, Conversation, Message

__all__ = ["ChatSource", "Conversation", "Message", "get_chat_source"]


def get_chat_source(kind: str | None = None) -> ChatSource:
    kind = (kind or os.getenv("CHAT_SOURCE", "mock")).lower()
    if kind == "mock":
        from .mock import MockChatSource
        return MockChatSource()
    if kind == "bitrix":
        from .bitrix_chats import BitrixRestSource
        return BitrixRestSource(
            os.environ["BITRIX_PORTAL_DOMAIN"], os.environ["BITRIX_WEBHOOK_TOKEN"]
        )
    raise ValueError(f"unknown CHAT_SOURCE {kind!r}; expected mock|bitrix")
