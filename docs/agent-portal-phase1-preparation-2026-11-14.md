# Call ownership preparation — no new paid services

The owner asked to continue without spending. No model requests, compute
provisioning, deployment, DB updates, account creation or workflow changes were
performed. This is local preparation for phase 1, not a completed rollout.

Read-only staff/call-log inspection found 47 distinct origin strings:

| Category | Count |
|---|---:|
| Named staff extensions | 38 |
| Exact staff-name proposals | 7 |
| Same name after normalizing hyphens | 2 |
| Spelling/short-name candidates requiring explicit review | 9 |
| Matched QA-excluded accounts | 2 |
| Names needing identification | 18 |

The last five categories partition the 38 named extensions. All proposals remain
unapproved. Similar spelling does not establish staff identity. Nine remaining
origin strings are unnamed/nonstaff values; they include 18 outbound calls.
Therefore the prompt's blanket statement that outbound extensions are reliable
requires a qualification: an outbound origin must match an approved staff
extension before it can appear on a personal page.

Private review artifacts, deliberately excluded from Git:

- `local-reports/phase1-attribution/mapping-review.html`
- `local-reports/phase1-attribution/mapping-review.csv`
- `local-reports/phase1-attribution/mapping-proposal-private.json`

The HTML presents the PBX name, proposed employee, extension, uncertainty and
outbound count. The owner must confirm/correct the mapping before it is written
or used. This is the explicit mapping-approval requirement in the supplied task,
not an additional deployment approval process.

## Local ownership resolver

Added `services/worker/app/calls_archive/attribution.py`, a pure function with
no database/network access. It is not connected to live routes or workflows.

- Inbound: requires a verified complete journey. Repeated ANSWER events for
  one extension do not duplicate ownership. First-ring and fan-out events do
  not establish ownership.
- Multiple answering extensions, missing identities, supervision or unverified
  evidence return an unattributed result with an explicit reason.
- Outbound: uses only the approved origin mapping; journey ANSWER may be the
  customer's external number and is not used for staff identity.
- Unapproved, disabled or excluded accounts never produce a personal owner.

`journey_verified` is a future trusted-importer assertion, default false. The
importer still needs implementation and validation of linkedid association,
complete HTTP response, event chronology and transfer/supervision consistency.
Do not set it true just because a response contains an ANSWER event.

Validation: **17 focused tests passed**. Pytest could not write its local cache;
this did not affect test execution. No live evaluation was triggered.

The focused synthetic tests exercise these ownership boundaries without APIs,
credentials, real employee names or paid models. Paid audio benchmarks and
production integration remain pending. The $35.90–38.90 monthly estimate is
still provisional and no monthly plan was activated.
