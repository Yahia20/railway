"""The seam between us and the two APIs we do not have yet.

Everything downstream — normalisation, ASR, both AI passes, scoring — consumes
`Conversation` and `CallRecording` only. Nothing downstream imports Bitrix or
Google Drive. The day the real APIs arrive, one file in `sources/` changes and
the pipeline is untouched.

To add a source: implement `ChatSource` or `CallSource`, register it in
`sources/__init__.py`, and set the matching env var. That is the whole contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Literal, Protocol, runtime_checkable

SpeakerRole = Literal["customer", "agent", "bot", "system", "unknown"]


@dataclass(frozen=True)
class Message:
    """One message in a chat thread."""

    seq: int
    sender: SpeakerRole
    body: str
    sent_at: datetime


@dataclass
class Conversation:
    """A chat thread, normalised. The unit both AI passes operate on."""

    external_id: str                  # 'chat15556'
    external_source: str              # 'bitrix'
    channel: str                      # 'whatsapp' | 'facebook' | ...
    started_at: datetime
    messages: list[Message] = field(default_factory=list)

    customer_phone_raw: str | None = None
    agent_external_id: str | None = None
    bitrix_deal_id: str | None = None
    bitrix_contact_id: str | None = None
    ended_at: datetime | None = None

    # The untouched source payload. Landed in raw_events so a bad parse can be
    # replayed without re-reading the source system.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_bot_only(self) -> bool:
        """No human agent ever joined. These must never reach agent scoring."""
        return not any(m.sender == "agent" for m in self.messages)

    @property
    def has_no_customer_turn(self) -> bool:
        """Nobody is labelled as the customer, on a thread that has messages.

        Observed on 63% of a 60-conversation sample from the chat API: every
        inbound turn arrives labelled `Agent`, including plain customer replies
        like "واحد 25/7" answering the bot's "how many travellers and when?".

        A conversation cannot be a conversation with one side missing, so this
        is a labelling fault at the source, not a real transcript. Scoring it
        anyway is the worst option available: pass 1 would read the customer's
        own words as the agent's, pass 2 would grade the agent on sentences the
        customer wrote, and both would come back confident. A missing score is
        recoverable once the source is fixed; a plausible wrong one is not.
        """
        return bool(self.messages) and not any(
            m.sender == "customer" for m in self.messages
        )

    def transcript_text(self) -> str:
        """Render for the model. Speaker + timestamp on every line, because
        every timing and attribution rule in the rubric depends on both."""
        return "\n".join(
            f"[{m.sent_at:%Y-%m-%d %H:%M:%S}] {m.sender.upper()}: {m.body}"
            for m in self.messages
        )


@dataclass
class CallRecording:
    """A call recording, before transcription."""

    external_id: str                  # asterisk uniqueid, or the Drive file id
    external_source: str              # 'asterisk_drive'
    audio_uri: str                    # 'drive://<fileId>'
    started_at: datetime

    customer_phone_raw: str | None = None
    agent_extension: str | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def local_path(self) -> str:
        """Path to the downloaded audio. Set by the fetcher, not the lister."""
        return self.raw["local_path"]


@runtime_checkable
class ChatSource(Protocol):
    """Where chat conversations come from."""

    name: str

    def fetch_since(self, since: datetime, limit: int = 500) -> Iterator[Conversation]:
        """Conversations created or updated since `since`, oldest first.

        Must be safe to call with an overlapping window: the ingest layer
        deduplicates on (external_source, external_id) and on message hash, so
        re-delivering a conversation is a no-op rather than a duplicate.
        """
        ...

    def fetch_one(self, external_id: str) -> Conversation | None:
        ...


@runtime_checkable
class CallSource(Protocol):
    """Where call recordings come from."""

    name: str

    def list_since(self, since: datetime, limit: int = 500) -> Iterator[CallRecording]:
        ...

    def download(self, rec: CallRecording, dest_dir: str) -> str:
        """Fetch the audio to local disk. Returns the path, and sets it on
        `rec.raw['local_path']`."""
        ...
