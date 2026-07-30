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

from .base import CallRecording, Conversation, Message

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


class MockCallSource:
    """Serves local .wav files named with the PBX convention."""

    name = "asterisk_drive"

    def __init__(self, fixtures_dir: Path | None = None, tz_offset_hours: int = 3):
        self.dir = fixtures_dir or FIXTURES / "calls"
        self.tz_offset_hours = tz_offset_hours

    def list_since(self, since: datetime, limit: int = 500) -> Iterator[CallRecording]:
        from .drive_calls import RecordingNameError, parse_recording_name

        if not self.dir.exists():
            return
        for path in sorted(self.dir.glob("*.wav"))[:limit]:
            try:
                meta = parse_recording_name(path.name, self.tz_offset_hours)
            except RecordingNameError:
                continue
            if meta["started_at"] < since:
                continue
            yield CallRecording(
                external_id=meta["uniqueid"],
                external_source=self.name,
                audio_uri=f"file://{path}",
                started_at=meta["started_at"],
                customer_phone_raw=meta["customer_phone_raw"],
                agent_extension=meta["agent_extension"],
                size_bytes=path.stat().st_size,
                raw={"parsed_name": meta, "local_path": str(path)},
            )

    def download(self, rec: CallRecording, dest_dir: str) -> str:
        src = rec.raw["local_path"]
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{rec.external_id}.wav")
        if os.path.abspath(src) != os.path.abspath(dest):
            shutil.copyfile(src, dest)
        rec.raw["local_path"] = dest
        return dest


def synthetic_conversation(messages: list[tuple[str, str, int]],
                           start: datetime | None = None) -> Conversation:
    """Build a conversation from (sender, body, minutes_offset) tuples.

    For testing scoring behaviour against hand-built cases — an agent who never
    greets, one who ignores an objection — without needing a real payload.
    """
    start = start or datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    return Conversation(
        external_id="synthetic",
        external_source="mock",
        channel="whatsapp",
        started_at=start,
        messages=[
            Message(seq=i, sender=sender, body=body,
                    sent_at=start + timedelta(minutes=offset))
            for i, (sender, body, offset) in enumerate(messages, start=1)
        ],
    )
