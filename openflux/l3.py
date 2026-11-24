"""L3 mode: raw IPv4 packets over the carrier, NAT on the exit.

The client reads packets from a TUN device and sends them as transport
messages; the exit writes them into its own TUN, where the Linux kernel
forwards and masquerades them, and sends the replies back. One end-to-end
TCP connection per flow (no proxy termination), so it is faster than L4,
but the exit needs root, /dev/net/tun and iptables (a VPS; not Render).

Framing: an IPv4 packet always starts with 0x4_ while mux packets start with
their type (1-8) and multi-transport control frames with 0x00, so an exit
can serve L3 and L4 clients over the same transports, telling them apart by
the first byte. Lost packets are not retransmitted here: TCP inside does it.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Callable, List

from . import logging_util as log
from . import tun as tunmod
from .transport.base import Transport


def is_ipv4(msg: bytes) -> bool:
    return bool(msg) and msg[0] >> 4 == 4


def _csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def fix_l4_checksum(pkt: bytes) -> bytes:
    """Recompute the TCP/UDP checksum of an IPv4 packet.

    A Linux exit forwarding into its TUN can hand us packets whose checksum
    was left for NIC offload (veth in Docker, many VPS NICs): the client's
    kernel then drops every reply (Tcp InCsumErrors) and nothing connects.
    Fixing it here makes the exit independent of the host's offload setup."""
    if len(pkt) < 20:
        return pkt
    ihl = (pkt[0] & 0x0F) * 4
    proto = pkt[9]
    frag = struct.unpack_from("!H", pkt, 6)[0]
    if proto not in (6, 17) or frag & 0x1FFF or len(pkt) < ihl + 8:
        return pkt                     # non-first fragments carry no L4 header
    total = struct.unpack_from("!H", pkt, 2)[0]
    seg = bytearray(pkt[ihl:total])
    off = 16 if proto == 6 else 6
    if len(seg) < off + 2:
        return pkt
    seg[off:off + 2] = b"\0\0"
    pseudo = pkt[12:20] + struct.pack("!BBH", 0, proto, len(seg))
    c = _csum(pseudo + bytes(seg))
    if proto == 17 and c == 0:
        c = 0xFFFF
    struct.pack_into("!H", seg, off, c)
    return pkt[:ihl] + bytes(seg)


class _Pump:
    def __init__(self, transport: Transport, dev: tunmod.TunDevice, fix_csum: bool = False) -> None:
        self.t = transport
        self.dev = dev
        self.fix_csum = fix_csum
        self._q: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self.up = self.down = 0

    def from_tun(self, pkt: bytes) -> None:
        if self.fix_csum:
            pkt = fix_l4_checksum(pkt)
        try:
            self._q.put_nowait(pkt)
        except asyncio.QueueFull:
            pass                      # drop: TCP will slow down

    def to_tun(self, pkt: bytes) -> None:
        self.down += 1
        self.dev.write(pkt)

    async def run(self) -> None:
        self.dev.read_loop(self.from_tun)
        while True:
            pkt = await self._q.get()
            try:
                await self.t.send(pkt)
                self.up += 1
            except Exception as e:  # noqa: BLE001
                log.debugf("[L3] send dropped: %s", e)


def _resolve(hosts: List[str]) -> List[str]:
    import socket
    ips = set()
    for h in hosts:
        try:
            socket.inet_aton(h)
            ips.add(h)
            continue
        except OSError:
            pass
        try:
            for info in socket.getaddrinfo(h, 443, socket.AF_INET, socket.SOCK_STREAM):
                ips.add(info[4][0])
        except OSError:
            log.errorf("[L3] cannot resolve carrier host %s", h)
    return sorted(ips)


async def run_l3_client(transport: Transport, bypass_hosts_fn: Callable[[], List[str]]) -> None:
    """Client side (root): TUN in, carrier out. Runs until cancelled."""
    await transport.start()
    dev = tunmod.TunDevice.open()
    pump = _Pump(transport, dev)
    transport.set_receive(lambda m: pump.to_tun(m) if is_ipv4(m) else None)
    bypass = _resolve(bypass_hosts_fn())
    undo: List[List[str]] = []
    try:
        undo = tunmod.setup_client(dev, bypass)
        log.infof("[L3] client up on %s (%s); carrier bypass: %s", dev.name, tunmod.CLIENT_ADDR,
                  " ".join(bypass) or "none")
        await pump.run()
    finally:
        tunmod.undo_all(undo)
        dev.close()
        log.infof("[L3] client down")


class L3Exit:
    """Exit side: wraps the transport's receive callback so IPv4 packets go to
    the TUN and everything else to the L4 mux as before."""

    def __init__(self, transport: Transport, mux_on_message: Callable[[bytes], None]) -> None:
        self.dev = tunmod.TunDevice.open()
        self.undo = tunmod.setup_exit(self.dev)
        self.pump = _Pump(transport, self.dev, fix_csum=True)
        transport.set_receive(lambda m: self.pump.to_tun(m) if is_ipv4(m) else mux_on_message(m))
        log.infof("[L3] exit NAT on %s (%s, %s)", self.dev.name, tunmod.EXIT_ADDR, tunmod.NET)

    async def run(self) -> None:
        try:
            await self.pump.run()
        finally:
            tunmod.undo_all(self.undo)
            self.dev.close()
