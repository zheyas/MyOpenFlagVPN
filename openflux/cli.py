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
from typing import List, Optional

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
    p.add_argument("--mode", "-m", default="l4", choices=["l4", "l3"],
                   help="Exit mode: l4 (streams, any OS) or l3 (raw IPv4 + NAT; Linux, root, "
                        "/dev/net/tun, iptables). An l3 exit still serves l4 clients.")
    p.add_argument("--inbound", default="socks5", choices=["socks5", "tun"],
                   help="Client inbound: socks5, or tun (L3: route all IPv4 into a TUN; "
                        "needs root and an exit started with --mode l3)")
    p.add_argument("--url", "-u", default="", help="Document / room URL")
    p.add_argument("--socks5", "-s", default=":1080", help="SOCKS5 listen address (client)")
    p.add_argument("--encryption-key-file", default="",
                   help="Path to a file with the shared AES-256-GCM secret")
    p.add_argument("--direct-dial", default="", help="direct: exit host:port (client)")
    p.add_argument("--direct-listen", default="", help="direct: listen addr (exit)")
    p.add_argument("--oneme-token", default="", help="oneme: MAX token")
    p.add_argument("--oneme-uid", default="", help="oneme: MAX uid")
    p.add_argument("--transports", default="",
                   help="Several carriers with failover, e.g. cupsonline:100,direct:50")
    for t in ("cupsonline", "yandex", "vyandex", "boards", "mailru"):
        p.add_argument(f"--{t}-url", default="", help=f"{t}: document / room URL (sessions)")
    p.add_argument("--config", default="", help="Path to an OpenFlux .conf file")
    p.add_argument("--debug", "-d", action="store_true", help="Verbose logging")
    return p


def _spec_params(ttype: str, values: dict, is_exit: bool, args: argparse.Namespace) -> dict:
    if ttype == "direct":
        if is_exit:
            return {"listen": values.get("Listen") or args.direct_listen}
        return {"dial": values.get("Dial") or args.direct_dial}
    if ttype == "oneme":
        return {"token": values.get("Token") or args.oneme_token,
                "uid": values.get("UID") or args.oneme_uid}
    return {}


def _apply_conf(args: argparse.Namespace, conf: ConfFile) -> List[dict]:
    """Overlay .conf [Interface] onto args (CLI wins) and return one transport
    spec per [Transport] section (empty list: use the flags)."""
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

    specs = []
    for t in conf.transports:
        ttype = t.values.get("Type") or t.name
        try:
            prio = int(t.values.get("Priority", "50"))
        except ValueError:
            prio = 50
        specs.append({"type": ttype, "name": t.name or ttype, "priority": prio,
                      "url": t.values.get("URL") or args.url,
                      "params": _spec_params(ttype, t.values, args.role == "exit", args)})
    return specs


def _specs_from_flags(args: argparse.Namespace) -> List[dict]:
    """--transports=cupsonline:100,direct:50 (per-type URLs from
    --<type>-url, falling back to --url), else the single --transport."""
    per_url = {"cupsonline": args.cupsonline_url, "yandex": args.yandex_url,
               "vyandex": args.vyandex_url, "boards": args.boards_url, "mailru": args.mailru_url}
    items = [x.strip() for x in (args.transports or "").split(",") if x.strip()] or [args.transport]
    specs = []
    for item in items:
        name, _, prio = item.partition(":")
        try:
            priority = int(prio) if prio else 50
        except ValueError:
            priority = 50
        specs.append({"type": name, "priority": priority,
                      "url": per_url.get(name) or args.url,
                      "params": _spec_params(name, {}, args.role == "exit", args)})
    return specs


def carrier_hosts(raws: dict, specs: List[dict]) -> List[str]:
    """Hosts the carriers reach directly (a full tunnel routes them around
    itself): what each transport reports, plus spec URL / dial hosts."""
    from urllib.parse import urlparse
    hosts = set()
    for raw in raws.values():
        fn = getattr(raw, "carrier_hosts", None)
        if fn:
            hosts.update(fn())
    for sp in specs:
        u = sp.get("url") or ""
        params = sp.get("params") or {}
        if sp["type"] == "direct":
            d = params.get("dial") or u
            hosts.add(d.rpartition(":")[0] or d)
        elif u.startswith("http"):
            hosts.add(urlparse(u).hostname or "")
    return sorted(h for h in hosts if h)


def build_transport(specs: List[dict], secret: str, is_client: bool):
    """Build the carrier stack: per spec raw -> Batched -> Encrypted (if a
    secret is set); several specs become one MultiTransport. Returns
    (transport, raw transports by type)."""
    from .transport.multi import Child, MultiTransport

    built = []
    raws = {}
    for sp in specs:
        ttype, url = sp["type"], sp.get("url") or ""
        if ttype not in VALID_TRANSPORTS:
            raise ValueError(f"unknown transport {ttype!r}")
        if ttype == "direct" and not secret:
            raise ValueError("transport direct requires an encryption key")
        # Encryption context: the transport type, or the URL for non-cupsonline.
        context = url if (url and ttype not in ("cupsonline", "direct")) else ttype
        raw = make_transport(ttype, url, default_config(), is_client, sp.get("params") or {})
        raws.setdefault(ttype, raw)
        t = BatchedTransport(raw)
        if secret:
            t = EncryptedTransport(t, secret, context, exit_node=not is_client)
        built.append((sp.get("name") or ttype, int(sp.get("priority", 50)), t))
    if len(built) == 1:
        return built[0][2], raws
    return MultiTransport([Child(name=n, priority=p, transport=t) for n, p, t in built]), raws


async def _amain(args: argparse.Namespace) -> None:
    if args.debug:
        log.enable_debug()

    specs: List[dict] = []
    if args.config:
        conf = parse_conf(args.config)
        specs = _apply_conf(args, conf)
    if args.debug:
        log.enable_debug()
    is_client = args.role == "client"
    if not specs:
        specs = _specs_from_flags(args)
    for sp in specs:
        if sp["type"] not in VALID_TRANSPORTS:
            log.errorf("unknown transport %r", sp["type"])
            sys.exit(2)

    secret = ""
    if args.encryption_key_file:
        with open(args.encryption_key_file, "r", encoding="utf-8") as f:
            secret = f.read().strip()

    try:
        transport, raws = build_transport(specs, secret, is_client)
    except ValueError as e:
        log.errorf("%s", e)
        sys.exit(2)

    log.infof("=== OpenFlux (Python) ===")
    log.infof("Role: %s | Transport: %s | Mode: %s", args.role,
              ", ".join(f"{sp['type']}:{sp['priority']}" for sp in specs),
              args.mode if args.role == "exit" else args.inbound)
    if secret:
        log.infof("Transport encryption: AES-256-GCM enabled")

    if args.role == "exit":
        addr = _http_listen_addr()
        if addr:
            await _start_exit_http(addr, raws.get("cupsonline"))
        # Create the exit (installs the mux receive handler) before starting
        # the transport, so no early peer message is dropped.
        exit_node = ExitNode(transport)
        l3 = None
        if args.mode == "l3":
            from .l3 import L3Exit
            l3 = L3Exit(transport, exit_node._mux._on_message)
        await transport.start()
        await exit_node.start()
        if l3 is not None:
            await l3.run()
        await asyncio.Event().wait()  # run forever
    elif args.inbound == "tun":
        from .l3 import run_l3_client
        await run_l3_client(transport, lambda: carrier_hosts(raws, specs))
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
    _start_keepalive()


_bg_tasks: set = set()


def _start_keepalive() -> None:
    """Keep a free Render instance awake. It sleeps after ~15 min without
    inbound HTTP, and clients on networks that block *.onrender.com can never
    wake it (they only reach cups.online). Pinging our own public URL goes
    through Render's edge and counts as inbound traffic.

    URL: OPENFLUX_KEEPALIVE_URL, else RENDER_EXTERNAL_URL (set by Render).
    Interval: OPENFLUX_KEEPALIVE_SECONDS (default 600). Set the URL to "off"
    to disable."""
    url = os.getenv("OPENFLUX_KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    if not url or url == "off":
        return
    try:
        every = max(60.0, float(os.getenv("OPENFLUX_KEEPALIVE_SECONDS", "600")))
    except ValueError:
        every = 600.0

    async def loop() -> None:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=30)
        while True:
            await asyncio.sleep(every)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(url.rstrip("/") + "/healthz") as r:
                        log.debugf("[KEEPALIVE] %s -> %d", url, r.status)
            except Exception as e:  # noqa: BLE001 - never let this kill the exit
                log.infof("[KEEPALIVE] ping failed: %s", e)

    _bg_tasks.add(asyncio.ensure_future(loop()))   # keep a strong ref
    log.infof("Keep-alive: pinging %s every %.0fs", url, every)


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
