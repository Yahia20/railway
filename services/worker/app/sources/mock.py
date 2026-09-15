"""Mock sources, so the whole pipeline is runnable before either API exists.

These are not toys: the chat fixture is the real Bitrix payload from
`api_response.txt`, and the call fixture is the real recording filename from
Drive. Anything that passes here is exercising the same shapes production will.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .base import Conversation, Message

def _find_fixtures() -> Path:
    """Locate the fixtures directory by walking up from this file.

    An explicit env var wins. Otherwise search upward, because the useful root
    differs by context: the repo root when running tests, and a mounted path
    inside the container — the Docker image only copies `app/`.
    """
    override = os.getenv("FIXTURES_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "fixtures"
        if candidate.is_dir():
            return candidate
    return here.parents[3] / "fixtures"      # sensible default when absent


FIXTURES = _find_fixtures()


class MockChatSource:
    name = "bitrix"

    def __init__(self, fixtures_dir: Path | None = None):
        self.dir = fixtures_dir or FIXTURES / "chats"

    def _load(self) -> list[Conversation]:
        from .bitrix_chats import BitrixWebhookSource

        out = []
        if not self.dir.exists():
            return out
        for path in sorted(self.dir.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            payload = raw[0] if isinstance(raw, list) else raw
            out.append(BitrixWebhookSource.parse(payload))
        return out

    def fetch_since(self, since: datetime, limit: int = 500) -> Iterator[Conversation]:
        for conv in self._load():
            if conv.started_at >= since:
                yield conv

    def fetch_one(self, external_id: str) -> Conversation | None:
        return next((c for c in self._load() if c.external_id == external_id), None)
