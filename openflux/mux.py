"""Reliable stream multiplexer (RMUX) — the L4 core of the Python port.

The Go project tunnels *raw IPv4 packets* and relies on gVisor / the OS to
provide reliable TCP end to end. The Python port takes the L4 route the user
chose: instead of IP packets, the client and exit exchange multiplexed logical
streams. Because the document/room transports can drop, reorder and duplicate
messages, this layer adds its own reliability (a cumulative-ACK, windowed,
retransmitting protocol — TCP-lite) so each logical stream is an ordered,
lossless byte stream.

The carriers (cups.online rooms, docs, MAX) are slow and high-latency, so the
reliability is tuned for that: adaptive RTO (never a low fixed timeout),
delayed/coalesced ACKs (one ACK per batch of packets, not per packet, to spare
the equally-slow reverse channel), fast retransmit on duplicate ACKs, and a
single ordered sender queue (no per-packet task spray).

Wire (one mux packet == one transport message, before batched+encrypted):

    type:u8 | stream_id:u32 | seq:u32 | ack:u32 | data...      (13-byte header)

types:
    SYN(1)      client->exit  open a TCP stream; data = encoded target addr
    SYNACK(2)   exit->client  data[0]=status (0 ok, else failure)
    DATA(3)     both           seq = 0-based segment index; data = segment
    ACK(4)      both           ack = next expected seq (cumulative)
    FIN(5)      both           seq = total number of DATA segments sent
    RST(6)      both           abort the stream
    UDP(7)      both           unreliable datagram; data = encoded addr+payload
    KEEPALIVE(8) both          empty; keeps the session warm

Only the client opens streams (driven by SOCKS5), so stream ids never collide.
"""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Callable, Dict, Optional, Tuple

from . import logging_util as log
from .transport.base import Transport

# packet types
SYN = 1
SYNACK = 2
DATA = 3
ACK = 4
FIN = 5
RST = 6
UDP = 7
KEEPALIVE = 8

_HDR = struct.Struct(">BIII")  # type, stream_id, seq, ack
HEADER_LEN = _HDR.size

MAX_SEGMENT = 4096          # bytes of app data per DATA packet
SEND_WINDOW = 256           # unacked DATA packets in flight (x MAX_SEGMENT = 1 MiB)

# Retransmit timing. The carriers have RTTs of hundreds of ms to several
# seconds under load, so the timeout is adaptive (RFC6298-style) with a high
# floor — a low fixed RTO caused a retransmission storm that collapsed goodput.
RTO_INIT = 2.0
RTO_MIN = 1.0
RTO_MAX = 30.0
MAX_RETRIES = 12            # give up (RST) after this many retransmits of one packet

# ACK pacing. One cumulative ACK per batch of packets / per tick instead of one
# ACK per DATA packet, so the (equally slow) reverse channel isn't flooded.
ACK_FLUSH_INTERVAL = 0.03   # s; timer that flushes pending ACKs
ACK_MAX_PENDING = 16        # force an ACK once this many packets are unacked
TICK_INTERVAL = 0.03        # s; retransmit + ACK-flush timer granularity
DUPACK_FAST_RETRANSMIT = 3  # duplicate ACKs that trigger a fast retransmit

EOF = b""                   # sentinel pushed to recv queue on clean close

OUT_QUEUE_DEPTH = 8192      # mux packets queued toward the transport


# ---- address encoding (SOCKS5-style atyp) ----

ATYP_IPV4 = 1
ATYP_DOMAIN = 3
ATYP_IPV6 = 4


def encode_addr(host: str, port: int) -> bytes:
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 4:
            return bytes([ATYP_IPV4]) + ip.packed + struct.pack(">H", port)
        return bytes([ATYP_IPV6]) + ip.packed + struct.pack(">H", port)
    except ValueError:
        h = host.encode()
        return bytes([ATYP_DOMAIN, len(h)]) + h + struct.pack(">H", port)


def decode_addr(data: bytes) -> Tuple[str, int, int]:
    """Returns (host, port, bytes_consumed)."""
    import ipaddress
    atyp = data[0]
    if atyp == ATYP_IPV4:
        host = str(ipaddress.IPv4Address(data[1:5]))
        port = struct.unpack_from(">H", data, 5)[0]
        return host, port, 7
    if atyp == ATYP_IPV6:
        host = str(ipaddress.IPv6Address(data[1:17]))
        port = struct.unpack_from(">H", data, 17)[0]
        return host, port, 19
    if atyp == ATYP_DOMAIN:
        ln = data[1]
        host = data[2:2 + ln].decode()
        port = struct.unpack_from(">H", data, 2 + ln)[0]
        return host, port, 2 + ln + 2
    raise ValueError(f"bad atyp {atyp}")


class Stream:
    """One reliable, ordered byte stream over the mux."""

    def __init__(self, mux: "Mux", stream_id: int) -> None:
        self._mux = mux
        self.id = stream_id
        # send side
        self._send_buf = bytearray()
        self._next_seq = 0
        self._send_base = 0
        self._unacked: Dict[int, Tuple[bytes, float, int]] = {}   # seq -> (data, sent, retries)
        self._fin_sent = False
        self._fin_seq = -1
        self._write_wakeup = asyncio.Event()
        self._write_wakeup.set()
        # adaptive RTO (RFC6298-ish)
        self._srtt = 0.0
        self._rttvar = 0.0
        self._rto = RTO_INIT
        # fast retransmit
        self._last_ack_seen = 0
        self._dupacks = 0
        # receive side
        self._rcv_next = 0
        self._ooo: Dict[int, bytes] = {}
        self._recv_q: asyncio.Queue = asyncio.Queue()
        self._peer_fin_seq = -1
        self._got_eof = False
        # delayed ACK
        self._ack_pending = False
        self._last_acked = 0
        # lifecycle
        self.closed = False
        self._syn_ok = asyncio.Future()  # resolved on SYNACK / RST for the opener
        self._syn_ok.add_done_callback(lambda f: f.cancelled() or f.exception())

    # ---- application-facing API ----

    async def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("stream closed")
        self._send_buf += data
        await self._pump_send()

    async def read(self) -> bytes:
        """Returns a chunk of received bytes, or b'' at EOF."""
        return await self._recv_q.get()

    async def close_send(self) -> None:
        if self._fin_sent:
            return
        await self._pump_send()
        self._fin_sent = True
        self._fin_seq = self._next_seq
        self._send_packet(FIN, self.id, seq=self._next_seq)

    def reset(self) -> None:
        if self.closed:
            return
        self._send_packet(RST, self.id)
        self._teardown()

    # ---- send machinery ----

    async def _pump_send(self) -> None:
        while self._send_buf:
            while (self._next_seq - self._send_base) >= SEND_WINDOW:
                self._write_wakeup.clear()
                await self._write_wakeup.wait()
                if self.closed:
                    return
            take = min(MAX_SEGMENT, len(self._send_buf))
            seg = bytes(self._send_buf[:take])
            del self._send_buf[:take]
            seq = self._next_seq
            self._next_seq += 1
            self._unacked[seq] = (seg, time.monotonic(), 0)
            self._send_packet(DATA, self.id, seq=seq, data=seg)

    def _send_packet(self, ptype: int, sid: int, seq: int = 0, ack: int = 0, data: bytes = b"") -> None:
        self._mux._send_raw(ptype, sid, seq, ack, data)

    def _sample_rtt(self, r: float) -> None:
        if r <= 0:
            return
        if self._srtt == 0.0:
            self._srtt = r
            self._rttvar = r / 2
        else:
            self._rttvar = 0.75 * self._rttvar + 0.25 * abs(self._srtt - r)
            self._srtt = 0.875 * self._srtt + 0.125 * r
        self._rto = min(max(self._srtt + 4 * self._rttvar, RTO_MIN), RTO_MAX)

    def _on_ack(self, ack: int) -> None:
        now = time.monotonic()
        if ack > self._send_base:
            # New data acknowledged: advance, sampling RTT from non-retransmitted
            # packets only (Karn's algorithm).
            while self._send_base < ack:
                entry = self._unacked.pop(self._send_base, None)
                if entry is not None and entry[2] == 0:
                    self._sample_rtt(now - entry[1])
                self._send_base += 1
            self._dupacks = 0
            self._last_ack_seen = ack
            if (self._next_seq - self._send_base) < SEND_WINDOW:
                self._write_wakeup.set()
            self._maybe_done()
        elif ack == self._send_base and self._send_base < self._next_seq and self._unacked:
            # Duplicate ACK: the peer is missing send_base. Fast-retransmit it
            # after a few dups instead of waiting for the RTO.
            self._dupacks += 1
            if self._dupacks == DUPACK_FAST_RETRANSMIT:
                self._retransmit(self._send_base, now)

    def _retransmit(self, seq: int, now: float) -> None:
        entry = self._unacked.get(seq)
        if entry is None:
            return
        data, _, retries = entry
        if retries >= MAX_RETRIES:
            log.debugf("[MUX] stream %d seq %d exceeded retries; reset", self.id, seq)
            self.reset()
            return
        self._unacked[seq] = (data, now, retries + 1)
        # Back off the RTO on a timeout-driven retransmit.
        self._rto = min(self._rto * 2, RTO_MAX)
        self._send_packet(DATA, self.id, seq=seq, data=data)

    def _on_data(self, seq: int, data: bytes) -> None:
        if seq < self._rcv_next:
            self._send_packet(ACK, self.id, ack=self._rcv_next)   # dup; help peer advance
            return
        if seq == self._rcv_next:
            self._deliver(data)
            self._rcv_next += 1
            while self._rcv_next in self._ooo:
                self._deliver(self._ooo.pop(self._rcv_next))
                self._rcv_next += 1
            # In-order: delay the ACK (coalesce), but don't let too many build up.
            self._ack_pending = True
            if self._rcv_next - self._last_acked >= ACK_MAX_PENDING:
                self._flush_ack()
        else:
            # Gap: buffer and send an immediate (duplicate) ACK so the sender's
            # fast-retransmit kicks in without waiting for its RTO.
            self._ooo[seq] = data
            self._send_packet(ACK, self.id, ack=self._rcv_next)
            self._last_acked = self._rcv_next
            self._ack_pending = False
        self._check_peer_fin()

    def _flush_ack(self) -> None:
        if self._ack_pending and not self.closed:
            self._send_packet(ACK, self.id, ack=self._rcv_next)
            self._last_acked = self._rcv_next
            self._ack_pending = False

    def _deliver(self, data: bytes) -> None:
        self._recv_q.put_nowait(data)

    def _on_fin(self, seq: int) -> None:
        self._peer_fin_seq = seq
        self._flush_ack()
        self._send_packet(ACK, self.id, ack=self._rcv_next)
        self._check_peer_fin()

    def _check_peer_fin(self) -> None:
        if self._peer_fin_seq >= 0 and self._rcv_next >= self._peer_fin_seq and not self._got_eof:
            self._got_eof = True
            self._recv_q.put_nowait(EOF)
            self._maybe_done()

    def _maybe_done(self) -> None:
        send_done = self._fin_sent and self._send_base >= self._fin_seq and not self._unacked
        if send_done and self._got_eof:
            self._teardown()

    def _teardown(self) -> None:
        if self.closed:
            return
        self.closed = True
        if not self._got_eof:
            self._recv_q.put_nowait(EOF)
        self._write_wakeup.set()
        if not self._syn_ok.done():
            self._syn_ok.set_exception(ConnectionError("stream reset"))
        self._mux._remove_stream(self.id)

    def _on_tick(self, now: float) -> None:
        """Called every tick: retransmit timed-out packets, flush pending ACK."""
        for seq, (data, sent, retries) in list(self._unacked.items()):
            if now - sent >= self._rto:
                self._retransmit(seq, now)
                if self.closed:
                    return
        self._flush_ack()
        if self._fin_sent and self._send_base >= self._fin_seq and not self._unacked:
            self._maybe_done()


UDPHandler = Callable[[int, str, int, bytes], None]


class Mux:
    def __init__(self, transport: Transport, is_exit: bool,
                 on_stream: Optional[Callable[[Stream, str, int], None]] = None,
                 on_udp: Optional[UDPHandler] = None) -> None:
        self._t = transport
        self._is_exit = is_exit
        self._on_stream = on_stream
        self._on_udp = on_udp
        self._streams: Dict[int, Stream] = {}
        self._next_id = 1
        self._timer_task: Optional[asyncio.Task] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._out_q: asyncio.Queue = asyncio.Queue(maxsize=OUT_QUEUE_DEPTH)
        transport.set_receive(self._on_message)

    async def start(self) -> None:
        self._timer_task = asyncio.ensure_future(self._tick_loop())
        self._writer_task = asyncio.ensure_future(self._writer_loop())

    async def stop(self) -> None:
        for t in (self._timer_task, self._writer_task):
            if t:
                t.cancel()

    # ---- client side: open a stream ----

    async def open_tcp(self, host: str, port: int, timeout: float = 30.0) -> Stream:
        sid = self._next_id
        self._next_id += 1
        st = Stream(self, sid)
        self._streams[sid] = st
        self._send_raw(SYN, sid, 0, 0, encode_addr(host, port))
        try:
            await asyncio.wait_for(asyncio.shield(st._syn_ok), timeout=timeout)
        except Exception:
            st._teardown()
            raise
        return st

    def send_udp(self, assoc_id: int, host: str, port: int, payload: bytes) -> None:
        self._send_raw(UDP, assoc_id, 0, 0, encode_addr(host, port) + payload)

    # ---- transport plumbing ----

    def _send_raw(self, ptype: int, sid: int, seq: int, ack: int, data: bytes) -> None:
        pkt = _HDR.pack(ptype, sid, seq, ack) + data
        try:
            self._out_q.put_nowait(pkt)
        except asyncio.QueueFull:
            # DATA loss is recovered by retransmit; a dropped ACK by the next one.
            log.debugf("[MUX] out queue full, dropping %d-byte packet", len(pkt))

    async def _writer_loop(self) -> None:
        try:
            while True:
                pkt = await self._out_q.get()
                try:
                    await self._t.send(pkt)
                except Exception as e:  # noqa: BLE001
                    log.debugf("[MUX] transport send: %s", e)
        except asyncio.CancelledError:
            return

    def _on_message(self, msg: bytes) -> None:
        if len(msg) < HEADER_LEN:
            return
        ptype, sid, seq, ack = _HDR.unpack_from(msg)
        data = msg[HEADER_LEN:]

        if ptype == SYN:
            self._handle_syn(sid, data)
            return
        if ptype == UDP:
            self._handle_udp(sid, data)
            return
        if ptype == KEEPALIVE:
            return

        st = self._streams.get(sid)
        if st is None:
            if ptype != RST:
                self._send_raw(RST, sid, 0, 0, b"")
            return

        if ptype == SYNACK:
            status = data[0] if data else 0
            if status == 0:
                if not st._syn_ok.done():
                    st._syn_ok.set_result(True)
            else:
                if not st._syn_ok.done():
                    st._syn_ok.set_exception(ConnectionError(f"connect failed (status {status})"))
                st._teardown()
        elif ptype == DATA:
            st._on_data(seq, data)
        elif ptype == ACK:
            st._on_ack(ack)
        elif ptype == FIN:
            st._on_fin(seq)
        elif ptype == RST:
            st._teardown()

    def _handle_syn(self, sid: int, data: bytes) -> None:
        if not self._is_exit or self._on_stream is None:
            self._send_raw(RST, sid, 0, 0, b"")
            return
        if sid in self._streams:
            return  # duplicate SYN retransmit
        try:
            host, port, _ = decode_addr(data)
        except Exception:  # noqa: BLE001
            self._send_raw(RST, sid, 0, 0, b"")
            return
        st = Stream(self, sid)
        self._streams[sid] = st
        self._on_stream(st, host, port)

    def accept_ok(self, st: Stream) -> None:
        self._send_raw(SYNACK, st.id, 0, 0, bytes([0]))

    def accept_fail(self, st: Stream, status: int = 1) -> None:
        self._send_raw(SYNACK, st.id, 0, 0, bytes([status & 0xFF]))
        st._teardown()

    def _handle_udp(self, assoc_id: int, data: bytes) -> None:
        if self._on_udp is None:
            return
        try:
            host, port, consumed = decode_addr(data)
        except Exception:  # noqa: BLE001
            return
        self._on_udp(assoc_id, host, port, data[consumed:])

    def _remove_stream(self, sid: int) -> None:
        self._streams.pop(sid, None)

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(TICK_INTERVAL)
                now = time.monotonic()
                for st in list(self._streams.values()):
                    st._on_tick(now)
        except asyncio.CancelledError:
            return
