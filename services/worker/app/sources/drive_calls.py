"""Call recordings stored in Google Drive.

Filename convention, confirmed against the sample recording:

    q-3009-0500000000-20260701-170522-1782914722.226.wav
    │ │    │          │        │      └── Asterisk uniqueid (epoch.seq)
    │ │    │          │        └───────── HHMMSS, portal local time
    │ │    │          └────────────────── YYYYMMDD
    │ │    └───────────────────────────── caller number, national format
    │ └────────────────────────────────── queue / agent extension
    └──────────────────────────────────── 'q' = queue recording

The Asterisk uniqueid prefix is an epoch second, so it is a second, independent
source for the call start time. `parse_recording_name` compares the two and
reports disagreement rather than silently trusting either — a mismatch means the
PBX clock and the filename convention have drifted, which would corrupt every
timing metric downstream.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .base import CallRecording

# q-<ext>-<number>-<YYYYMMDD>-<HHMMSS>-<uniqueid>.wav
_NAME_RE = re.compile(
    r"^(?P<kind>[a-z]+)-(?P<ext>\d+)-(?P<number>\d+)-"
    r"(?P<date>\d{8})-(?P<time>\d{6})-(?P<uniqueid>[\d.]+)\.wav$",
    re.IGNORECASE,
)


class RecordingNameError(ValueError):
    """The filename did not match the PBX convention. Never guess — a wrong
    agent extension attributes a call to the wrong person's scorecard."""


def parse_recording_name(filename: str, tz_offset_hours: int = 3) -> dict:
    """Pull the call's metadata out of its filename.

    `tz_offset_hours` is the PBX's local offset (+3 for both Riyadh and Cairo
    in summer). The filename carries no zone, so this must be configured, not
    assumed — see PBX_TZ_OFFSET_HOURS.
    """
    m = _NAME_RE.match(os.path.basename(filename))
    if not m:
        raise RecordingNameError(f"unrecognised recording filename: {filename!r}")

    tz = timezone(timedelta(hours=tz_offset_hours))
    started_at = datetime.strptime(
        f"{m.group('date')}{m.group('time')}", "%Y%m%d%H%M%S"
    ).replace(tzinfo=tz)

    # Cross-check against the epoch embedded in the Asterisk uniqueid.
    drift_seconds = None
    try:
        epoch = float(m.group("uniqueid").split(".")[0])
        drift_seconds = abs((datetime.fromtimestamp(epoch, tz) - started_at).total_seconds())
    except (ValueError, OSError, OverflowError):
        pass

    return {
        "kind": m.group("kind"),
        "agent_extension": m.group("ext"),
        "customer_phone_raw": m.group("number"),
        "started_at": started_at,
        "uniqueid": m.group("uniqueid"),
        # Non-zero drift means the filename clock and the PBX clock disagree.
        # Surfaced, not swallowed: every response-time metric depends on this.
        "clock_drift_seconds": drift_seconds,
    }


class DriveCallSource:
    """Lists and downloads call recordings from a Google Drive folder.

    Requires a service account with read access to the folder. Uses the Drive
    v3 API directly so there is no dependency on a user OAuth session — a
    scheduled job cannot complete an interactive consent flow.
    """

    name = "asterisk_drive"

    def __init__(self, folder_id: str, credentials_json: str, tz_offset_hours: int = 3):
        self.folder_id = folder_id
        self.tz_offset_hours = tz_offset_hours
        self._credentials_json = credentials_json
        self._service = None

    def _client(self):
        if self._service is None:
            import json

            from google.oauth2 import service_account          # google-auth
            from googleapiclient.discovery import build        # google-api-python-client

            creds = service_account.Credentials.from_service_account_info(
                json.loads(self._credentials_json),
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def list_since(self, since: datetime, limit: int = 500) -> Iterator[CallRecording]:
        files = self._client().files()
        query = (
            f"'{self.folder_id}' in parents and trashed = false "
            f"and mimeType contains 'audio/' "
            f"and modifiedTime > '{since.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}'"
        )
        page_token, seen = None, 0
        while seen < limit:
            resp = files.list(
                q=query,
                orderBy="modifiedTime",
                pageSize=min(100, limit - seen),
                pageToken=page_token,
                fields="nextPageToken, files(id, name, size, modifiedTime, mimeType)",
            ).execute()

            for f in resp.get("files", []):
                seen += 1
                try:
                    meta = parse_recording_name(f["name"], self.tz_offset_hours)
                except RecordingNameError:
                    # Skipped, not crashed: one oddly named file must not stop
                    # the batch. The count surfaces in job_runs.items_failed.
                    yield from ()
                    continue
                yield CallRecording(
                    external_id=meta["uniqueid"],
                    external_source=self.name,
                    audio_uri=f"drive://{f['id']}",
                    started_at=meta["started_at"],
                    customer_phone_raw=meta["customer_phone_raw"],
                    agent_extension=meta["agent_extension"],
                    size_bytes=int(f.get("size", 0)) or None,
                    raw={"drive_file": f, "parsed_name": meta},
                )

            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def download(self, rec: CallRecording, dest_dir: str) -> str:
        import io

        from googleapiclient.http import MediaIoBaseDownload

        file_id = rec.audio_uri.removeprefix("drive://")
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, f"{rec.external_id}.wav")

        request = self._client().files().get_media(fileId=file_id)
        with io.FileIO(path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        rec.raw["local_path"] = path
        return path
