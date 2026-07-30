"""Entrypoint that reads its own configuration instead of relying on a shell.

    python -m app.serve

Railway runs a service's start command **tokenized, without a shell**, so
`--port $PORT` and `--port ${PORT:-8000}` are passed to uvicorn as literal
strings and it exits with:

    Error: Invalid value for '--port': '${PORT:-8000}' is not a valid integer.

Doing the environment lookup in Python removes the dependency on who invokes us
and how. It behaves identically under `docker run`, `python -m app.serve`, a
Railway start command, and a Kubernetes exec-form entrypoint.

Host defaults to `::` because Railway's private network is IPv6-only — a service
bound to 0.0.0.0 is unreachable at <service>.railway.internal. `::` is dual-stack
on Linux, so IPv4 callers still work.
"""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.getenv("PORT") or 8000)
    host = os.getenv("HOST") or "::"
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        # One worker: the container is I/O-bound waiting on ASR and model calls,
        # and Railway scales by replica rather than by process.
        workers=1,
        log_level=(os.getenv("LOG_LEVEL") or "info").lower(),
        access_log=True,
        proxy_headers=True,          # behind Railway's edge proxy
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
