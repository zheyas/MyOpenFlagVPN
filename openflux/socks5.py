"""SOCKS5 server — the client's inbound, a Python-native replacement for the
Go client's SOCKS5+gVisor path. Each CONNECT becomes a reliable mux stream to
the exit; UDP ASSOCIATE is relayed as best-effort mux datagrams.
"""

from __future__ import annotations

import asyncio
import ipaddress
import struct
from typing import Optional

from . import logging_util as log
from .mux import EOF, Mux, Stream

SOCKS_VERSION = 0x05
CMD_CONNECT = 0x01
CMD_UDP_ASSOCIATE = 0x03

REP_SUCCESS = 0x00
REP_GENERAL_FAILURE = 0x01
REP_HOST_UNREACHABLE = 0x04
REP_CMD_NOT_SUPPORTED = 0x07


async def _read_addr(reader: asyncio.StreamReader):
    atyp = (await reader.readexactly(1))[0]
    if atyp == 0x01:
        host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
    elif atyp == 0x04:
        host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
    elif atyp == 0x03:
        ln = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(ln)).decode()
    else:
        raise ValueError(f"bad atyp {atyp}")
    port = struct.unpack(">H", await reader.readexactly(2))[0]
    return host, port


def _reply(rep: int, bind_host: str = "0.0.0.0", bind_port: int = 0) -> bytes:
    return (bytes([SOCKS_VERSION, rep, 0x00, 0x01])
            + ipaddress.IPv4Address(bind_host).packed
            + struct.pack(">H", bind_port))


class SOCKS5Server:
    def __init__(self, listen_addr: str, mux: Mux) -> None:
        self._addr = listen_addr
        self._mux = mux
        self._server: Optional[asyncio.AbstractServer] = None
        self._udp_assoc_next = 1 << 31   # separate id space from TCP streams

    async def start(self) -> None:
        host, _, port = self._addr.rpartition(":")
        if host == "":
            host = "127.0.0.1"
        self._server = await asyncio.start_server(self._handle, host, int(port))
        log.infof("[SOCKS5] listening on %s", self._addr)

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            ver = (await reader.readexactly(1))[0]
            if ver != SOCKS_VERSION:
                writer.close()
                return
            nmethods = (await reader.readexactly(1))[0]
            await reader.readexactly(nmethods)
            writer.write(bytes([SOCKS_VERSION, 0x00]))   # no auth
            await writer.drain()

            ver = (await reader.readexactly(1))[0]
            cmd = (await reader.readexactly(1))[0]
            await reader.readexactly(1)  # RSV
            host, port = await _read_addr(reader)

            if cmd == CMD_CONNECT:
                await self._do_connect(reader, writer, host, port)
            elif cmd == CMD_UDP_ASSOCIATE:
                await self._do_udp_associate(reader, writer)
            else:
                writer.write(_reply(REP_CMD_NOT_SUPPORTED))
                await writer.drain()
                writer.close()
        except (asyncio.IncompleteReadError, ConnectionError):
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            log.debugf("[SOCKS5] handler error: %s", e)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _do_connect(self, reader, writer, host, port) -> None:
        try:
            stream = await self._mux.open_tcp(host, port)
        except Exception as e:  # noqa: BLE001
            log.debugf("[SOCKS5] open %s:%d failed: %s", host, port, e)
            writer.write(_reply(REP_HOST_UNREACHABLE))
            await writer.drain()
            writer.close()
            return
        writer.write(_reply(REP_SUCCESS))
        await writer.drain()
        log.debugf("[SOCKS5] CONNECT %s:%d -> stream %d", host, port, stream.id)
        await self._pipe(reader, writer, stream)

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    stream: Stream) -> None:
        async def local_to_stream() -> None:
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await stream.write(data)
                await stream.close_send()
            except Exception:  # noqa: BLE001
                stream.reset()

        async def stream_to_local() -> None:
            try:
                while True:
                    chunk = await stream.read()
                    if chunk is EOF or chunk == EOF:
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

        await asyncio.gather(local_to_stream(), stream_to_local())

    async def _do_udp_associate(self, reader, writer) -> None:
        """Minimal UDP ASSOCIATE: bind a local UDP relay socket, forward each
        datagram to the exit as a mux UDP packet, and return replies."""
        loop = asyncio.get_event_loop()
        assoc_id = self._udp_assoc_next
        self._udp_assoc_next += 1

        transport_holder = {}

        class _RelayProto(asyncio.DatagramProtocol):
            def __init__(self, srv: "SOCKS5Server"):
                self.srv = srv
                self.client_addr = None

            def connection_made(self, transport):
                transport_holder["t"] = transport

            def datagram_received(self, data: bytes, addr):
                # SOCKS5 UDP request header: RSV(2) FRAG(1) ATYP ADDR PORT DATA
                self.client_addr = addr
                try:
                    if data[2] != 0:
                        return  # fragmentation unsupported
                    off = 3
                    atyp = data[off]; off += 1
                    if atyp == 0x01:
                        host = str(ipaddress.IPv4Address(data[off:off + 4])); off += 4
                    elif atyp == 0x04:
                        host = str(ipaddress.IPv6Address(data[off:off + 16])); off += 16
                    elif atyp == 0x03:
                        ln = data[off]; off += 1
                        host = data[off:off + ln].decode(); off += ln
                    else:
                        return
                    port = struct.unpack_from(">H", data, off)[0]; off += 2
                    payload = data[off:]
                except Exception:  # noqa: BLE001
                    return
                self.srv._udp_targets[assoc_id] = (transport_holder.get("t"), addr, host, port)
                self.srv._mux.send_udp(assoc_id, host, port, payload)

        sock_transport, _ = await loop.create_datagram_endpoint(
            lambda: _RelayProto(self), local_addr=("127.0.0.1", 0))
        relay_host, relay_port = sock_transport.get_extra_info("sockname")[:2]
        self._udp_targets = getattr(self, "_udp_targets", {})

        writer.write(_reply(REP_SUCCESS, relay_host, relay_port))
        await writer.drain()

        # Hold the TCP control connection open; when it closes, tear down UDP.
        try:
            while True:
                data = await reader.read(1)
                if not data:
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            sock_transport.close()
            self._udp_targets.pop(assoc_id, None)
            writer.close()

    def deliver_udp(self, assoc_id: int, host: str, port: int, payload: bytes) -> None:
        """Called by the client when the exit returns a UDP reply."""
        targets = getattr(self, "_udp_targets", {})
        entry = targets.get(assoc_id)
        if not entry:
            return
        udp_transport, client_addr, _, _ = entry
        if udp_transport is None:
            return
        # Rebuild SOCKS5 UDP header for the reply.
        hdr = bytes([0, 0, 0, 0x01]) + ipaddress.IPv4Address(host if _is_ipv4(host) else "0.0.0.0").packed
        hdr += struct.pack(">H", port)
        udp_transport.sendto(hdr + payload, client_addr)


def _is_ipv4(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 4
    except ValueError:
        return False
