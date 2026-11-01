"""Call recordings archive: keep the PBX's call recordings in a private bucket
of their own, ready for transcription and scoring later.

WHO DOES WHAT, AND WHY IT IS SPLIT THIS WAY. The PBX (Teleswift, Asterisk at
tgate.teleswift.net:8443) admits only whitelisted IPs, and only the n8n
service has static egress IPs (enabled 2026-11-01). So n8n talks to the PBX:
workflow 11 indexes the call log, workflow 12 downloads one recording at a
time and POSTs the bytes here. The worker's only job is to check that the
bytes are audio and put them in the `call-recordings` bucket under a key
derived from the call, then answer what happened.

The worker never writes to Postgres (CLAUDE.md rule 11); n8n records every
answer. See db/migrations/032_call_recordings.sql.
"""
