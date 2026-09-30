"""L4 exit node — terminates each mux stream and re-dials the real server.

This is the Python port's only exit backend (the Go l3 raw-SNAT backend needs
root + Linux kernel sockets and has no place in a pure-Python L4 port). It
accepts SYN from the client mux, opens a real TCP connection to the requested
target, and pipes bytes both ways. UDP datagrams are forwarded per association.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional, Tuple

from . import logging_util as log
from .mux import EOF, Mux, Stream
from .transport.base import Transport


class ExitNode:
    def __init__(self, transport: Transport) -> None:
        self._t = transport
        self._mux = Mux(transport, is_exit=True,
                        on_stream=self._on_stream, on_udp=self._on_udp)
        # assoc_id -> (DatagramTransport, {(host,port) already bound})
        self._udp_socks: Dict[int, asyncio.DatagramTransport] = {}

    async def start(self) -> None:
        await self._mux.start()
        log.infof("[EXIT] L4 exit node ready")

    def _on_stream(self, stream: Stream, host: str, port: int) -> None:
        asyncio.ensure_future(self._serve_stream(stream, host, port))

    async def _serve_stream(self, stream: Stream, host: str, port: int) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=20.0)
        except Exception as e:  # noqa: BLE001
            log.debugf("[EXIT] dial %s:%d failed: %s", host, port, e)
            self._mux.accept_fail(stream)
            return
        log.debugf("[EXIT] stream %d -> %s:%d connected", stream.id, host, port)
        self._mux.accept_ok(stream)
        await self._pipe(stream, reader, writer)

    async def _pipe(self, stream: Stream, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter) -> None:
        async def remote_to_stream() -> None:
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await stream.write(data)
                await stream.close_send()
            except Exception:  # noqa: BLE001
                stream.reset()

        async def stream_to_remote() -> None:
            try:
                while True:
                    chunk = await stream.read()
                    if chunk == EOF:
                        break
                    writer.write(chunk)
                    await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(remote_to_stream(), stream_to_remote())

    # ---- UDP ----

    def _on_udp(self, assoc_id: int, host: str, port: int, payload: bytes) -> None:
        loop = asyncio.get_event_loop()
        transport = self._udp_socks.get(assoc_id)
        if transport is None:
            asyncio.ensure_future(self._open_udp(assoc_id, host, port, payload))
            return
        try:
            transport.sendto(payload, (host, port))
        except Exception as e:  # noqa: BLE001
            log.debugf("[EXIT] udp send: %s", e)

    async def _open_udp(self, assoc_id: int, host: str, port: int, payload: bytes) -> None:
        loop = asyncio.get_event_loop()
        mux = self._mux

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr) -> None:
                mux.send_udp(assoc_id, addr[0], addr[1], data)

        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Proto, remote_addr=None, family=0)
        except Exception as e:  # noqa: BLE001
            log.debugf("[EXIT] udp assoc %d: %s", assoc_id, e)
            return
        self._udp_socks[assoc_id] = transport
        try:
            transport.sendto(payload, (host, port))
        except Exception as e:  # noqa: BLE001
            log.debugf("[EXIT] udp first send: %s", e)
