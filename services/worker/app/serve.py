"""Entrypoint that reads its own configuration and binds a genuine dual-stack socket.

    python -m app.serve

Two Railway behaviours forced this file to exist, and both cost a failed deploy
to find:

1. **No shell.** Railway tokenizes a service's start command and executes it
   without a shell, so `--port $PORT` and `--port ${PORT:-8000}` reach uvicorn as
   literal text:

       Error: Invalid value for '--port': '${PORT:-8000}' is not a valid integer.

   Doing the lookup in Python removes any dependence on who invokes us.

2. **Two networks, two address families.** Railway's private network
   (`<service>.railway.internal`) is IPv6-only, while its edge proxy and
   healthcheck reach the container over IPv4. `--host ::` alone is not reliably
   enough: whether an AF_INET6 socket also accepts IPv4 depends on the kernel's
   `net.ipv6.bindv6only`, and a v6-only socket refuses the healthcheck while the
   log still cheerfully reads "Uvicorn running on http://[::]:8000".

   So the socket is created here with `IPV6_V6ONLY = 0` explicitly, which serves
   both families no matter what the host default is.
"""
from __future__ import annotations

import logging
import os
import socket

import uvicorn

log = logging.getLogger("serve")

BACKLOG = 2048


def dual_stack_socket(port: int) -> socket.socket:
    """An AF_INET6 listener that also accepts IPv4, or an IPv4 one if the host
    has no IPv6 at all. Never silently v6-only."""
    if socket.has_dualstack_ipv6():
        sock = socket.create_server(
            ("::", port), family=socket.AF_INET6, dualstack_ipv6=True, backlog=BACKLOG,
            reuse_port=False,
        )
        log.info("listening on [::]:%d (dual-stack: IPv4 and IPv6)", port)
        return sock

    if socket.has_ipv6:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            log.warning("could not clear IPV6_V6ONLY; IPv4 callers may be refused")
        sock.bind(("::", port))
        sock.listen(BACKLOG)
        log.info("listening on [::]:%d (IPv6, v6only cleared)", port)
        return sock

    sock = socket.create_server(("0.0.0.0", port), backlog=BACKLOG)
    log.warning("no IPv6 on this host; listening on 0.0.0.0:%d only — "
                "private networking will not work", port)
    return sock


def main() -> None:
    port = int(os.getenv("PORT") or 8000)
    level = (os.getenv("LOG_LEVEL") or "info").lower()
    logging.basicConfig(level=level.upper())

    # HOST is an escape hatch for environments that need a single family.
    # Unset (the normal case) means dual-stack.
    forced_host = os.getenv("HOST")

    config = uvicorn.Config(
        "app.main:app",
        # One worker: the container is I/O-bound waiting on ASR and model calls,
        # and Railway scales by replica rather than by process.
        workers=1,
        log_level=level,
        access_log=True,
        proxy_headers=True,              # behind Railway's edge proxy
        forwarded_allow_ips="*",
        timeout_keep_alive=65,           # above the edge's idle timeout
        **({"host": forced_host, "port": port} if forced_host else {}),
    )
    server = uvicorn.Server(config)

    if forced_host:
        log.info("HOST is set to %s; binding that address only", forced_host)
        server.run()
    else:
        server.run(sockets=[dual_stack_socket(port)])


if __name__ == "__main__":
    main()
