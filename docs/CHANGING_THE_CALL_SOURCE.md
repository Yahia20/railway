# Changing where call recordings come from

The PBX is going to be replaced, and the new recorder will hand us a different
shape of data. This is the list of everything that has to change, and — more
usefully — everything that does not.

The design intent is that **only `sources/` and one fetcher know what a Drive
file is.** Everything downstream consumes `CallRecording` and a transcript.

---

## The three seams

| seam | what it does | file |
|---|---|---|
| **listing** | "what recordings exist since X" | `services/worker/app/sources/<yours>.py` |
| **fetching** | "get the audio bytes for this row" | `modal/transcribe_job.py` → `FETCHERS` |
| **identity** | "which call is this, and whose" | the `uniqueid` and `agent_extension` you set when listing |

They are deliberately separate. The worker lists (cheap, needs CRM/API
credentials); Modal fetches (needs bandwidth and the storage credential, and
must never route audio through Railway — that is what made the worker
expensive).

---

## 1 · Write the source

Implement the `CallSource` protocol in `services/worker/app/sources/base.py`:

```python
class CallSource(Protocol):
    name: str
    def list_since(self, since: datetime, limit: int = 500) -> Iterator[CallRecording]: ...
    def download(self, rec: CallRecording, dest_dir: str) -> str: ...
```

Fill in `CallRecording`:

| field | rule |
|---|---|
| `external_id` | **The call's identity, not its storage id.** Today this is the Asterisk `uniqueid`, because the same call arrives on Drive as `name (1).wav` copies and the file id would make each copy a separate job. Use whatever your recorder calls one call. |
| `external_source` | A new namespace, e.g. `'threecx'`. **Do not reuse `asterisk_drive`** unless the ids mean the same thing — one call under two namespaces becomes two half-filled rows. |
| `audio_uri` | `<scheme>://<ref>`. The scheme is what picks the fetcher. |
| `agent_extension` | The extension that **answered**, if the recorder gives it. See the warning below. |
| `started_at` | Timezone-aware. `PBX_TZ_OFFSET_HOURS` exists because the current PBX writes local time with no offset. |

Register it:

```python
# services/worker/app/sources/__init__.py
if kind == "threecx":
    from .threecx import ThreeCXSource
    return ThreeCXSource(...)
```

Then set `CALL_SOURCE=threecx`. That is the whole switch — nothing else reads
the env var.

## 2 · Write the fetcher

Only if the audio lives somewhere new. `audio_uri` is scheme-dispatched:

```python
# modal/transcribe_job.py
FETCHERS = {"drive": _fetch_drive, "https": ..., "http": ..., "s3": _fetch_s3}
```

`https://`, `http://` and `s3://` already work. A recorder that can produce a
signed URL needs **no new code at all** — write `https://...` into `audio_uri`
and the batch fetches it. Use a signed URL: an unsigned one that works from
Modal works from anywhere, and these are recordings of real customers.

An unknown scheme fails loudly and terminally. That is deliberate — it is a
configuration mistake, not a transient one, and retrying it three times helps
nobody.

## 3 · What you do NOT have to touch

- **The database.** `call_ingest_jobs`, `transcripts`, `interactions` are
  source-agnostic. `audio_uri` was always documented as
  `drive://<fileId> or s3://...`.
- **ASR.** `asr/cohere_arabic.py` reads a local WAV. It does not know or care
  where the file came from.
- **Both AI passes, scoring, the rubric, the prompts.** They consume a
  transcript.
- **The state machine.** Workflow 02 claims `transcribed`/`judge_failed`; Modal
  owns `discovered`/`asr_failed`. That boundary (gotcha 13) is about job state,
  not about who recorded the call.
- **The budget gate.** Enforced in `/asr/claim`, which every batch passes
  through whatever the source is.

## 4 · Two things to insist on with the new recorder

Both are free at install time and expensive to retrofit.

**Record the answering extension, per call.** Every one of the current 1,119
recordings decodes to extension `3009` — a queue, not a person. The consequence
is not cosmetic: **calls cannot be scored per agent at all**, and 616 of the
623 promises pass 1 has extracted are locked in call transcripts that can never
become `follow_ups` rows. `agents.phone_extension` exists and is empty because
there is no honest value to put in it.

**Record two channels.** Mono means nothing separates agent from customer, so
speaker attribution is inferred from content and the prompt has to suppress its
absolute rules when `diarization = 'none'`. Dual-channel fixes diarization for
free (gotcha 10).

## 5 · Migrating without losing history

Old rows keep their namespace; new rows get the new one. Nothing merges them,
and nothing should:

```sql
SELECT external_source, count(*), max(started_at)
  FROM interactions GROUP BY 1;
```

If the same physical call could arrive under both sources during a cutover,
give the new source a deterministic `external_id` derived from the same thing
the old one used, and the `UNIQUE (external_source, external_id)` constraint
will keep them apart while `customer_phone_e164` still joins them to one
customer.

## 6 · Prove it before switching

```bash
CALL_SOURCE=threecx python -c "
from app.sources import get_call_source
from datetime import datetime, timezone, timedelta
src = get_call_source()
for r in list(src.list_since(datetime.now(timezone.utc) - timedelta(days=1), 5)):
    print(r.external_id, r.audio_uri, r.agent_extension, r.started_at)
"

modal run modal/transcribe_job.py::main --limit 5 --dry-run   # claims + releases
python scripts/audit_data_integrity.py --port 55432
```

The dry run claims and releases without loading the model, which proves the
lease and the fetcher without spending GPU time.
