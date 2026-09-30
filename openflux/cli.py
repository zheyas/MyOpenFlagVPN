"""Command-line entry — a port of the runtime wiring in main.go.

Supported roles: ``client`` (SOCKS5 inbound) and ``exit`` (L4 stream proxy).
Single-transport mode (``--transport``) is the primary path. A ``.conf`` with a
single ``[Transport ...]`` section is also accepted; multi-transport failover
(the Go ``--transports`` session) is not ported — if several transports are
configured the highest-priority one is used and a warning is logged.

The l3 raw-SNAT exit backend and the utun/iOS clients from the Go project have
no pure-Python equivalent and are intentionally omitted; the exit is always L4.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from typing import Optional

from . import logging_util as log
from .client import run_client
from .conf import ConfFile, conf_bool, parse_conf
from .exit_node import ExitNode
from .transport.base import default_config
from .transport.batched import BatchedTransport
from .transport.encrypted import EncryptedTransport
from .transport.factory import make_transport, transport_has_cookies

VALID_TRANSPORTS = ("yandex", "vyandex", "oneme", "cupsonline", "mailru", "boards", "direct")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openflux", description="OpenFlux (Python port) — L4 tunnel with pluggable transports")
    p.add_argument("--role", "-r", default="client", choices=["client", "exit"],
                   help="client | exit")
    p.add_argument("--transport", "-t", default="cupsonline",
                   help="Transport type: " + ", ".join(VALID_TRANSPORTS))
    p.add_argument("--mode", "-m", default="l4", choices=["l4"],
                   help="Exit mode (only l4 is available in the Python port)")
    p.add_argument("--url", "-u", default="", help="Document / room URL")
    p.add_argument("--socks5", "-s", default=":1080", help="SOCKS5 listen address (client)")
    p.add_argument("--encryption-key-file", default="",
                   help="Path to a file with the shared AES-256-GCM secret")
    p.add_argument("--direct-dial", default="", help="direct: exit host:port (client)")
    p.add_argument("--direct-listen", default="", help="direct: listen addr (exit)")
    p.add_argument("--oneme-token", default="", help="oneme: MAX token")
    p.add_argument("--oneme-uid", default="", help="oneme: MAX uid")
    p.add_argument("--config", default="", help="Path to an OpenFlux .conf file")
    p.add_argument("--debug", "-d", action="store_true", help="Verbose logging")
    return p


def _apply_conf(args: argparse.Namespace, conf: ConfFile) -> Optional[dict]:
    """Overlay .conf [Interface] onto args (CLI wins) and return the chosen
    single [Transport] section's params, if any."""
    iface = conf.interface
    explicit = set(a[2:].split("=")[0].replace("-", "_") for a in sys.argv if a.startswith("--"))

    def maybe(attr: str, key: str) -> None:
        if attr not in explicit and key in iface:
            setattr(args, attr, iface[key])

    maybe("role", "Role")
    maybe("transport", "Transport")
    maybe("mode", "Mode")
    maybe("socks5", "Socks5")
    maybe("encryption_key_file", "EncryptionKeyFile")
    maybe("url", "URL")
    if "Debug" in iface and "debug" not in explicit:
        args.debug = conf_bool(iface["Debug"], args.debug)

    if not conf.transports:
        return None
    # Pick highest-priority transport section.
    def prio(t):
        try:
            return int(t.values.get("Priority", "50"))
        except ValueError:
            return 50
    chosen = max(conf.transports, key=prio)
    if len(conf.transports) > 1:
        log.infof("[CONF] %d transports configured; multi-transport failover is not "
                  "ported, using %r", len(conf.transports), chosen.name)
    ttype = chosen.values.get("Type") or chosen.name
    args.transport = ttype
    if chosen.values.get("URL"):
        args.url = chosen.values["URL"]
    params = {}
    if ttype == "direct":
        if args.role == "exit":
            params["listen"] = chosen.values.get("Listen", "")
        else:
            params["dial"] = chosen.values.get("Dial", "")
    if ttype == "oneme":
        params["token"] = chosen.values.get("Token", "")
        params["uid"] = chosen.values.get("UID", "")
    return params


async def _amain(args: argparse.Namespace) -> None:
    if args.debug:
        log.enable_debug()

    params: dict = {}
    if args.config:
        conf = parse_conf(args.config)
        params = _apply_conf(args, conf) or {}
    # Role may come from the .conf, so resolve is_client only now.
    is_client = args.role == "client"

    if args.transport not in VALID_TRANSPORTS:
        log.errorf("unknown --transport %r", args.transport)
        sys.exit(2)

    # Transport-specific params from flags (when not from conf).
    if args.transport == "direct" and not params:
        params = {"dial": args.direct_dial, "listen": args.direct_listen}
    if args.transport == "oneme" and not params:
        params = {"token": args.oneme_token, "uid": args.oneme_uid}

    # Shared secret.
    secret = ""
    if args.encryption_key_file:
        with open(args.encryption_key_file, "r", encoding="utf-8") as f:
            secret = f.read().strip()
    if args.transport == "direct" and not secret:
        log.errorf("--transport=direct requires --encryption-key-file")
        sys.exit(2)

    # Encryption context: transport type, or the URL for non-cupsonline.
    context = args.transport
    if args.url and args.transport != "cupsonline":
        context = args.url

    base_cfg = default_config()
    raw = make_transport(args.transport, args.url, base_cfg, is_client, params)

    log.infof("=== OpenFlux (Python) ===")
    log.infof("Role: %s | Transport: %s | Mode: l4", args.role, args.transport)

    inner = BatchedTransport(raw)
    if secret:
        transport = EncryptedTransport(inner, secret, context, exit_node=not is_client)
        log.infof("Transport encryption: AES-256-GCM enabled")
    else:
        transport = inner

    # Exit HTTP endpoint (PaaS health + cupsonline /rooms).
    if args.role == "exit":
        addr = _http_listen_addr()
        if addr:
            await _start_exit_http(addr, raw if args.transport == "cupsonline" else None)

    if args.role == "exit":
        # Create the exit (installs the mux receive handler) before starting
        # the transport, so no early peer message is dropped.
        exit_node = ExitNode(transport)
        await transport.start()
        await exit_node.start()
        await asyncio.Event().wait()  # run forever
    else:
        await run_client(transport, args.socks5)


def _http_listen_addr() -> str:
    a = os.getenv("OPENFLUX_HTTP_ADDR")
    if a:
        return a
    p = os.getenv("PORT")
    if p:
        return ":" + p
    return ""


async def _start_exit_http(addr: str, cups) -> None:
    from aiohttp import web

    async def rooms(request):
        if cups is None:
            return web.Response(status=404, text="no cupsonline transport")
        lst = cups.room_list()
        if not lst:
            return web.Response(status=503, text="rooms not ready")
        return web.Response(text=lst + "\n")

    async def health(request):
        return web.Response(text="ok\n")

    app = web.Application()
    app.router.add_get("/rooms", rooms)
    app.router.add_get("/{tail:.*}", health)
    host, _, port = addr.rpartition(":")
    if host == "":
        host = "0.0.0.0"
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, int(port))
    await site.start()
    log.infof("HTTP endpoint on %s (GET /rooms for the room list)", addr)


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
