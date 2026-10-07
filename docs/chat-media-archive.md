# Chat media archive

Keeps the files a chat refers to (customer photos and PDFs, agent quotes,
voice notes, stickers, video) in a private bucket, and puts them back in place
when a conversation is pulled.

Design reviewed in two adversarial rounds with Codex (gpt-6-astra, read-only,
2026-10-01). Verdict: GO for phase 1 as amended; not a deployment sign-off.

## Why

The chat API sends a file as text: a caption or `[Attachment: name.ext]`, a
newline, a URL. We stored the URL, never the file.

| link family | example host | who sends it | lifetime |
|---|---|---|---|
| `bitrix_short` | `travelgate.bitrix24.ae/~XXXX` | agents | as long as Bitrix keeps it |
| `bitrix_rest` | `travelgate.bitrix24.ae/rest/<u>/<TOKEN>/download/…` | voice notes, both sides | carries a **live REST token** |
| `gupconnector` | `gupconnector.cultivbureau.com/connector/gupshup-media/…` | customers | **HTTP 410 after ~20 minutes** |
| `gupshup` | `filemanager.gupshup.io/wa/…` | stickers | long |

So every customer passport, transfer receipt and photo was a dead link in our
store.

## What runs

```
chat_messages ──► 09 · Chat media archive (n8n, every minute, one run at a time)
                    1 run lease            media_archive_config
                    2 retention            refs past 90 days → jobs purged → objects deleted
                    3 discover             unexamined messages → chat_media_scan,
                                           media_fetch_jobs (one per URL), chat_media
                    4 claim                up to 3 jobs: token + 5-min deadline, young customer links first
                    5 fetch                worker POST /media/fetch, ONE AT A TIME (a loop, 75 s cap each)
                    6 record               fenced on the claim token AND its deadline (row locked)
                  worker ──► Railway Bucket (private): sha256/<2>/<sha256>
                                                       receipts/<2>/<url_hash>.json
reader:  GET /conversations                    page (paste WORKER_API_KEY, type a deal number)
         GET /conversations/by-deal/{deal_id}  JSON, every stored thread of the deal
         GET /conversations/{interaction_id}   JSON, one stored thread
         GET /conversations/media/{id}/link    a fresh 5-minute link for one file (the page
                                               calls it when a link it holds has expired)
```

### Job states (`media_fetch_jobs.status`)

| status | meaning |
|---|---|
| `pending` | waiting its turn |
| `fetching` | claimed; a download is in flight |
| `retry_wait` | timeout, 429, 5xx or bucket error: backoff 30 s doubling to 30 min, rejected after 8. **No answer at all** (worker down, 503, no bucket) is a pause, not a verdict: the attempt is given back and the job waits 5 minutes, so an outage can never reject good files |
| `stored` | bytes in the bucket |
| `recovery_pending` | the source answered 404/410. Bitrix still has the file; automatic recovery needs a Bitrix credential that does not exist yet, so these are counted, not lost |
| `rejected` | too large (> 100 MiB), not a file (HTML/JSON page), a redirect off the allow-list, or 8 failed attempts |
| `purged` | every reference expired |

Nothing expires on a clock. ~20 minutes only sets priority; a 404/410 from the
source is the only "gone" signal.

### Guarantees and their limits

* **Lost answer after upload.** The worker writes a receipt keyed by the URL
  BEFORE the bytes. A retry with a receipt whose object exists answers
  `stored` without touching the (possibly dead) source.
* **Deletes never race fetches.** One bucket operation at a time in the
  worker (one replica — more replicas would need a bucket-level fence), and a
  delete that did not answer stops that run's fetches. A purged job's receipt
  is read before it is deleted; bytes only the receipt knew about are
  registered and deleted next run. A receipt delete that fails is retried
  (`receipt_deleted_at`).
* **Same file many times.** One object per sha256; one download per URL. Forty
  customers sent the same quote → forty references, one object.
* **Access.** A file is reached only through its message. Links are presigned
  bucket URLs, at most 5 minutes and never past the attachment's retention
  date. They are transferable until they expire (accepted for an owner-only
  reader).
* **Retention** follows the conversation: 90 days from
  `interactions.started_at`, the same rule as `purge_raw_content`. 09 enforces
  it itself, so it does not depend on the chat purge working.
* **The REST token** is never returned by the reader, never sent to the judge
  (`/chats/prepare` redacts it, including the follow-up history), and dropped
  from the job row the moment the job cannot use it again. It is still in
  `chat_messages.body` and `raw_events` — separate cleanup, not done here.
* **Execution data is not saved** for 09 (success or error): claimed rows hold
  source URLs. `v_media_health` is the record.

**Not covered — do not claim otherwise:**

* Messages the webhook acknowledged but never stored. 01c answers 200 before
  its first write; that boundary is unchanged here (separate decision: ask
  Cultiv whether their senders retry on a non-2xx).
* Customer files sent before go-live: their links are already dead
  (`recovery_pending`). Agent files and voice notes back to the 90-day window
  are recoverable and will be fetched.
* Duplicate deliveries (every message twice since 2026-09-23) are NOT merged.
  `by-deal` returns every thread separately; `feed_hint` is a labelled guess.
* Voice → text, PDF → text, image classification, MRZ from the archive: later.

## Go live

Owner decisions first: (1) create the bucket, (2) who may open passports and
contracts, (3) whether passport images stay for the 90 days or go after MRZ.

1. **Bucket.** Railway → project → New → Bucket (private). In the `railway`
   (worker) service add variable references:
   `S3_ENDPOINT=${{<bucket>.ENDPOINT}}`, `S3_BUCKET=${{<bucket>.BUCKET}}`,
   `S3_ACCESS_KEY_ID=${{<bucket>.ACCESS_KEY_ID}}`,
   `S3_SECRET_ACCESS_KEY=${{<bucket>.SECRET_ACCESS_KEY}}`,
   `S3_REGION=${{<bucket>.REGION}}`. If the bucket's credentials tab says
   path-style, also `S3_URL_STYLE=path`.
2. **Migrations**, over the tunnel, with `scripts/apply_migration.py`
   (CLAUDE.md "Commands"): `025_purge_without_transcripts.sql` then
   `026_chat_media.sql`. 025 is needed regardless — retention has been failing
   since 2026-09-16 (reproduced on an empty PG16 built to 024: `relation
   "transcripts" does not exist`; runs clean after 025) and the first thread
   reaches 90 days on 2026-11-21.
   **Not `psql -f` from a Windows checkout:** git there converts the files to
   CRLF, the `\r` ends up inside function bodies, and 023's text patch of
   `evaluate_alert_rules()` then refuses to apply. `apply_migration.py` reads
   in text mode and sends LF.
3. **Worker deploy** (push to main → Railway). Then a bucket smoke test, which
   nothing local can prove: `POST /media/fetch` with one agent `~XXXX` link,
   expect `stored`; repeat, expect `already_stored: true`.
4. **Workflow 09**: `python scripts/n8n_deploy.py 09` (created OFF; write the id
   into TARGETS). Activate it in n8n — it still does nothing while
   `media_archive_config.mode = 'off'`.
5. **Switch on**: `UPDATE media_archive_config SET value = 'on' WHERE key = 'mode';`
   Watch `SELECT * FROM v_media_health;` — `customer_links_at_risk` should stay
   near 0 and `stored` should grow. Off again = set mode back to `off`.
6. **Workflow 04** (`python scripts/n8n_deploy.py 04`) to pick up the health
   check without `transcripts`.

## Checks

```bash
cd services/worker && py -3.13 -m pytest tests -q           # unit, no credentials
python scripts/check_workflow_json.py n8n/workflows/09-chat-media-archive.json
python scripts/build_wf09_media_archive.py                   # regenerate 09 after editing SQL
# workflow 09's real SQL, end to end, against an EMPTY throwaway database:
python scripts/check_media_archive_sql.py --dsn postgresql://postgres@127.0.0.1:5433/c360_scratch
```
