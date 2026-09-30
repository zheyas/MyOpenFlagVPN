"""Client side: SOCKS5 inbound -> reliable mux -> transport.

Replaces the Go client's SOCKS5+gVisor / utun paths with a pure-Python SOCKS5
server whose connections become reliable mux streams to the exit node.
"""

from __future__ import annotations

import asyncio

from . import logging_util as log
from .mux import Mux
from .socks5 import SOCKS5Server
from .transport.base import Transport


async def run_client(transport: Transport, socks_addr: str) -> None:
    udp_router = {}

    def on_udp(assoc_id: int, host: str, port: int, payload: bytes) -> None:
        srv = udp_router.get("srv")
        if srv is not None:
            srv.deliver_udp(assoc_id, host, port, payload)

    mux = Mux(transport, is_exit=False, on_udp=on_udp)
    await transport.start()
    await mux.start()

    server = SOCKS5Server(socks_addr, mux)
    udp_router["srv"] = server
    log.infof("[CLIENT] SOCKS5 client ready on %s", socks_addr)
    await server.serve_forever()
