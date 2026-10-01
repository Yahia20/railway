"""Chat media archive: fetch the files a conversation refers to, keep them in a
private bucket, and hand them back when a conversation is pulled.

The worker never writes to Postgres (CLAUDE.md rule 11). It talks to the
bucket and to the source URLs; n8n records what happened.
"""
