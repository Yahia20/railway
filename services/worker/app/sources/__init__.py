"""Source registry.

`get_chat_source()` / `get_call_source()` are the only places the rest of the
application learns which implementation is live. Swapping the mock for the real
Bitrix or Drive client is an env var, not a code change — which is what lets the
pipeline be built, tested and demoed before either API exists.
"""
from __future__ import annotations

import os

from .base import CallRecording, CallSource, ChatSource, Conversation, Message

__all__ = [
    "CallRecording", "CallSource", "ChatSource", "Conversation", "Message",
    "get_chat_source", "get_call_source",
]


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


def get_call_source(kind: str | None = None) -> CallSource:
    kind = (kind or os.getenv("CALL_SOURCE", "mock")).lower()
    if kind == "mock":
        from .mock import MockCallSource
        return MockCallSource()
    if kind == "drive":
        from .drive_calls import DriveCallSource
        return DriveCallSource(
            os.environ["DRIVE_CALLS_FOLDER_ID"],
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"],
            int(os.getenv("PBX_TZ_OFFSET_HOURS", "3")),
        )
    raise ValueError(f"unknown CALL_SOURCE {kind!r}; expected mock|drive")
