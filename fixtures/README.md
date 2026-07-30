# Fixtures — local only, never committed

The tests in `services/worker/tests/test_sources_smoke.py` run against **real**
production data rather than invented samples, because the whole point is to catch
a broken adapter the day the live APIs are swapped in. Invented fixtures only
prove the adapter parses invented fixtures.

That data cannot live in this repository. It contains a live Bitrix REST token
and personal data belonging to real customers — names, phone numbers, and a
recording of someone's voice. This repo is public.

So the fixture files are gitignored, and the tests that need them **skip** when
they are absent. `pytest` passing on a clean clone means the logic is sound; to
exercise the adapters you populate these directories yourself.

## What to put here

```
fixtures/
  chats/
    bitrix-chat15556.json      a captured Bitrix webhook payload
  calls/
    q-<ext>-<number>-<YYYYMMDD>-<HHMMSS>-<uniqueid>.wav
```

### chats/

Any raw webhook body, saved as JSON. Either a bare object or a single-element
array — the parser accepts both, because that is what the capture tools produce.

### calls/

The filename **is** the metadata, so it must keep the PBX convention exactly:

```
q-3009-0500000000-20260701-170522-1782914722.226.wav
│ │    │          │        │      └── Asterisk uniqueid (epoch.sequence)
│ │    │          │        └───────── HHMMSS, PBX local time
│ │    │          └────────────────── YYYYMMDD
│ │    └───────────────────────────── caller number, national format
│ └────────────────────────────────── queue / agent extension
└──────────────────────────────────── 'q' = queue recording
```

`parse_recording_name()` rejects anything else rather than guessing, because a
misparsed extension attributes the call to the wrong agent's scorecard.

Audio must be **16-bit PCM WAV**. 8 kHz mono is what Asterisk produces and what
the pipeline expects; the ASR resamples internally. There is deliberately no
ffmpeg in the image, so compressed formats will not load.

## Handling this data

Treat anything you put here as production personal data:

- Do not paste transcripts or recordings into chat tools, issues or tickets.
- Keep it off shared drives that are not already approved for customer data.
- Delete it when you are finished debugging.
- Under both Egyptian and Saudi PDPL, a call recording needs a lawful basis and
  consent — which is what the `consents` table with `kind = 'call_recording'`
  exists to record.
