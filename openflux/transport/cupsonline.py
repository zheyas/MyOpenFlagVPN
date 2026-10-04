"""Cups.online transport — a port of transport/cupsonline/cupsonline.go.

Tunnels packets through cups.online interview "live-coding" rooms: it joins a
room's Centrifugo channel and smuggles bytes inside the integer cursor
positions of the shared editor (the server relays a cursor array of integers
verbatim and in order). The exit creates rooms and prints a base64 room list;
the client joins those rooms. Several rooms run at once and outbound frames are
spread across them round-robin.

Faithful to the Go logic: room create/join, connect+subscribe, keepalive,
send batching with per-channel pacing and message chunking, 6-byte-per-integer
cursor packing, and length-prefixed receive reassembly across messages.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

import aiohttp

from .base import BaseTransport, TransportConfig
from .. import logging_util as log

BASE_ROOM_URL = "https://interview.cups.online/live-coding/"
CUPS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")

BYTES_PER_NUMBER = 6

_re_conn_token = re.compile(r'<meta[^>]+name="centrifuge-connection-token"[^>]+content="([^"]+)"')
_re_conn_url = re.compile(r'<meta[^>]+name="centrifuge-connection-url"[^>]+content="([^"]+)"')
_re_sub_url = re.compile(r'<meta[^>]+name="centrifuge-subscription-token-url"[^>]+content="([^"]+)"')
_re_room_uuid = re.compile(r'data-room="\{&quot;uuid&quot;:\s*&quot;([0-9a-f-]{36})&quot;')
_re_user_uuid = re.compile(r'data-user="\{&quot;uuid&quot;:\s*&quot;([0-9a-f-]{36})&quot;')


@dataclass
class CupsConfig:
    ws_handshake_timeout: float = 15.0
    ws_read_timeout: float = 90.0
    keepalive_interval: float = 20.0
    reconnect_min_delay: float = 0.1
    reconnect_max_delay: float = 10.0
    reconnect_multiplier: float = 1.3
    batch_max_packets: int = 256
    batch_max_bytes: int = 11000
    # Bytes of codec data per cups message. The server relays a cursor array of
    # ~1000 integers verbatim (6 bytes each), and probing showed ~21 KB passes;
    # 11000 holds a full ~9 KB batched+encrypted frame in ONE paced message
    # instead of splitting it across two (which halved throughput at 4096).
    max_message_data: int = 11000
    batch_timeout: float = 0.002
    send_interval: float = 0.018
    send_queue_size: int = 1024
    max_message_bytes: int = 8 << 20
    max_payload_bytes: int = 65535
    # More rooms = more parallel channels = higher throughput, at the cost of a
    # slower start (each room is a couple of round trips) and more load on the
    # account/IP. Tunable via OPENFLUX_CUPS_ROOMS (applied in the transport).
    num_rooms: int = 8
    room_create_pause: float = 0.5


def _first(rx: re.Pattern, s: str) -> str:
    m = rx.search(s)
    return m.group(1) if m else ""


def _origin_of(raw: str) -> str:
    u = urlparse(raw)
    return f"{u.scheme}://{u.netloc}"


def pack_rooms(ids: List[str]) -> str:
    raw = json.dumps(ids, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unpack_rooms(s: str) -> List[str]:
    pad = "=" * (-len(s) % 4)
    raw = base64.urlsafe_b64decode(s + pad)
    return json.loads(raw)


def parse_room_list(raw_url: str) -> List[str]:
    raw_url = raw_url.strip()
    if not raw_url:
        return []
    try:
        u = urlparse(raw_url)
        q = parse_qs(u.query)
        if "rooms" in q:
            return unpack_rooms(q["rooms"][0])
        if "room" in q:
            return [q["room"][0]]
    except Exception:  # noqa: BLE001
        pass
    try:
        return unpack_rooms(raw_url)
    except Exception:  # noqa: BLE001
        return []


def _pack_numbers(blob: bytes) -> List[int]:
    n = (len(blob) + BYTES_PER_NUMBER - 1) // BYTES_PER_NUMBER
    out = []
    for i in range(n):
        v = 0
        for j in range(BYTES_PER_NUMBER):
            v <<= 8
            idx = i * BYTES_PER_NUMBER + j
            if idx < len(blob):
                v |= blob[idx]
        out.append(v)
    return out


def _bytes_from_numbers(nums: List[int]) -> bytes:
    out = bytearray()
    for v in nums:
        b = bytearray(BYTES_PER_NUMBER)
        for j in range(BYTES_PER_NUMBER - 1, -1, -1):
            b[j] = v & 0xFF
            v >>= 8
        out += b
    return bytes(out)


def _cursors_from_bytes(blob: bytes) -> list:
    nums = _pack_numbers(blob)
    cursors = []
    for i in range(0, len(nums), 2):
        c = {"row": nums[i], "column": 0}
        if i + 1 < len(nums):
            c["column"] = nums[i + 1]
        cursors.append(c)
    return cursors


class _CupsAuth:
    def __init__(self) -> None:
        self.room_uuid = ""
        self.user_uuid = ""
        self.conn_token = ""
        self.conn_url = ""
        self.sub_url = ""
        self.sub_token = ""
        self.channel = ""
        self.csrf = ""
        self.session: Optional[aiohttp.ClientSession] = None


async def _authorize(room_url: str, session: Optional[aiohttp.ClientSession]) -> _CupsAuth:
    close_after = False
    if session is None:
        session = aiohttp.ClientSession()
        close_after = False  # kept for reuse by caller
    headers = {
        "User-Agent": CUPS_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    async with session.get(room_url, headers=headers) as resp:
        body = await resp.text()
        if resp.status in (404, 410):
            raise RoomGone(f"GET room status {resp.status}")
        if resp.status != 200:
            raise RuntimeError(f"GET room status {resp.status}")

    a = _CupsAuth()
    a.session = session
    a.room_uuid = _first(_re_room_uuid, body)
    a.user_uuid = _first(_re_user_uuid, body)
    a.conn_token = _first(_re_conn_token, body)
    a.conn_url = _first(_re_conn_url, body)
    a.sub_url = _first(_re_sub_url, body)

    filt = session.cookie_jar.filter_cookies(room_url)
    if "csrftoken" in filt:
        a.csrf = filt["csrftoken"].value

    if not a.room_uuid or not a.user_uuid:
        raise RoomGone("room/user uuid missing")
    if not a.conn_token or not a.conn_url or not a.sub_url:
        raise RuntimeError("centrifuge meta missing")
    if not a.csrf:
        raise RuntimeError("csrftoken missing")
    a.channel = f"$shared_editor:room-{a.room_uuid}"

    async with session.post(a.sub_url, json={"channel": a.channel}, headers={
        "User-Agent": CUPS_UA, "Content-Type": "application/json",
        "X-CSRFToken": a.csrf, "Origin": _origin_of(room_url), "Referer": room_url,
    }) as resp:
        if resp.status != 200:
            raise RuntimeError(f"sub token status {resp.status}")
        j = await resp.json()
    a.sub_token = j.get("token", "")
    if not a.sub_token:
        raise RuntimeError("empty sub token")
    log.debugf("[CUPS] auth OK: room=%s user=%s", a.room_uuid, a.user_uuid)
    return a


class RoomGone(Exception):
    pass


def _join_url(room_uuid: str) -> str:
    return BASE_ROOM_URL + "?room=" + room_uuid


async def _join_room(room_uuid: str, session: Optional[aiohttp.ClientSession]) -> _CupsAuth:
    a = await _authorize(_join_url(room_uuid), session)
    if a.room_uuid != room_uuid:
        raise RoomGone(f"got new room {a.room_uuid[:8]} instead")
    return a


async def _create_rooms(n: int, pause: float) -> List[_CupsAuth]:
    out: List[_CupsAuth] = []
    for i in range(n):
        a = None
        for attempt in range(6):
            try:
                sess = aiohttp.ClientSession()
                a = await _authorize(BASE_ROOM_URL, sess)
                break
            except Exception as e:  # noqa: BLE001
                await sess.close()
                wait = pause * (attempt + 1)
                log.debugf("[CUPS] room %d attempt %d failed: %s", i + 1, attempt + 1, e)
                await asyncio.sleep(wait)
        if a is None:
            continue
        out.append(a)
        log.debugf("[CUPS] created room %d/%d: %s", i + 1, n, a.room_uuid)
        if i < n - 1:
            await asyncio.sleep(pause)
    if not out:
        raise RuntimeError("could not create any room")
    return out


def _is_ping(line: str) -> bool:
    return line == "{}"


def _split_frame(raw: str) -> List[str]:
    return [ln.strip() for ln in raw.split("\n") if ln.strip()]


class _CupsWS:
    def __init__(self, idx: int, room_uuid: str, cfg: CupsConfig,
                 auth: Optional[_CupsAuth], on_data) -> None:
        self.idx = idx
        self.room_uuid = room_uuid
        self.cfg = cfg
        self.auth = auth
        self.session: Optional[aiohttp.ClientSession] = auth.session if auth else None
        self.on_data = on_data
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.connected = False
        self.dead = False
        self._rpc_id = 2
        self._recv_buf = bytearray()
        self._send_q: asyncio.Queue = asyncio.Queue(maxsize=cfg.send_queue_size)
        self._last_send = 0.0
        self._stop = False
        self.packets_sent = 0
        self.packets_recv = 0

    def next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def run(self) -> None:
        delay = self.cfg.reconnect_min_delay
        need_join = self.auth is None
        while not self._stop:
            if need_join or self.dead:
                try:
                    await self._join()
                    need_join = False
                except Exception as e:  # noqa: BLE001
                    if not isinstance(e, RoomGone):
                        log.debugf("[CUPS] join %s failed: %s", self.room_uuid, e)
                    else:
                        self.dead = True
                    await asyncio.sleep(delay)
                    delay = min(delay * self.cfg.reconnect_multiplier, self.cfg.reconnect_max_delay)
                    continue
            try:
                await self._connect_and_serve()
            except Exception as e:  # noqa: BLE001
                log.debugf("[CUPS] ws error (%s): %s", self.room_uuid, e)
            self.connected = False
            if self._stop:
                return
            await asyncio.sleep(delay)
            delay = min(delay * self.cfg.reconnect_multiplier, self.cfg.reconnect_max_delay)

    async def _join(self) -> None:
        a = await _join_room(self.room_uuid, self.session)
        self.session = a.session
        self.auth = a
        self.dead = False

    async def _connect_and_serve(self) -> None:
        a = self.auth
        ws_url = a.conn_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = ws_url.rstrip("/") + "/websocket"
        headers = {"Origin": _origin_of(a.conn_url), "User-Agent": CUPS_UA}

        assert self.session is not None
        async with self.session.ws_connect(
                ws_url, headers=headers, max_msg_size=self.cfg.max_message_bytes,
                heartbeat=None, timeout=self.cfg.ws_handshake_timeout) as ws:
            self.ws = ws
            self._recv_buf.clear()
            await ws.send_str(json.dumps({"id": 1, "connect": {"token": a.conn_token, "name": "js"}}))
            if not await self._read_reply(ws, 1):
                raise RuntimeError("connect refused")
            await ws.send_str(json.dumps({"id": 2, "subscribe": {"channel": a.channel, "token": a.sub_token}}))
            if not await self._read_reply(ws, 2):
                raise RuntimeError("subscribe refused")
            self.connected = True
            log.debugf("[CUPS] WS ready: %s", a.room_uuid)

            ka = asyncio.ensure_future(self._keepalive(ws))
            sender = asyncio.ensure_future(self._send_loop(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_message(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                ka.cancel()
                sender.cancel()
                self.ws = None

    async def _read_reply(self, ws, cmd_id: int) -> bool:
        deadline = time.monotonic() + self.cfg.ws_handshake_timeout
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=self.cfg.ws_handshake_timeout)
            except asyncio.TimeoutError:
                return False
            if msg.type != aiohttp.WSMsgType.TEXT:
                return False
            for line in _split_frame(msg.data):
                if _is_ping(line):
                    await ws.send_str("{}")
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if obj.get("id") == cmd_id:
                    if obj.get("error"):
                        return False
                    return True
                self._handle_reply(line)
        return False

    async def _keepalive(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.cfg.keepalive_interval)
                await ws.send_str(json.dumps({
                    "rpc": {"method": "shared_editor_ping",
                            "data": {"room": self.auth.room_uuid, "user": self.auth.user_uuid}},
                    "id": self.next_id(),
                }))
        except (asyncio.CancelledError, Exception):
            return

    async def enqueue(self, data: bytes) -> None:
        await self._send_q.put(data)

    async def _send_loop(self, ws) -> None:
        batch: List[bytes] = []
        total = 0
        try:
            while True:
                if not batch:
                    pkt = await self._send_q.get()
                    batch.append(pkt)
                    total = 2 + len(pkt)
                # drain more, up to limits, with a small linger
                try:
                    while len(batch) < self.cfg.batch_max_packets and total < self.cfg.batch_max_bytes:
                        pkt = await asyncio.wait_for(self._send_q.get(), timeout=self.cfg.batch_timeout)
                        if total + 2 + len(pkt) > self.cfg.batch_max_bytes and batch:
                            await self._send_batch(ws, batch)
                            batch, total = [], 0
                        batch.append(pkt)
                        total += 2 + len(pkt)
                except asyncio.TimeoutError:
                    pass
                if batch:
                    await self._send_batch(ws, batch)
                    batch, total = [], 0
        except (asyncio.CancelledError, Exception):
            return

    async def _send_batch(self, ws, batch: List[bytes]) -> None:
        blob = bytearray()
        for p in batch:
            blob += struct.pack(">H", len(p))
            blob += p
        blob = bytes(blob)
        for off in range(0, len(blob), self.cfg.max_message_data):
            end = min(off + self.cfg.max_message_data, len(blob))
            await self._pace()
            await self._send_chunk(ws, blob[off:end])
        self.packets_sent += len(batch)

    async def _pace(self) -> None:
        if self.cfg.send_interval <= 0:
            return
        d = self.cfg.send_interval - (time.monotonic() - self._last_send)
        if d > 0:
            await asyncio.sleep(d)
        self._last_send = time.monotonic()

    async def _send_chunk(self, ws, chunk: bytes) -> None:
        payload = struct.pack(">H", len(chunk)) + chunk
        msg = {
            "rpc": {"method": "shared_editor_change_cursors",
                    "data": {"cursors": _cursors_from_bytes(payload), "ranges": [],
                             "room": self.auth.room_uuid, "user": self.auth.user_uuid}},
            "id": self.next_id(),
        }
        await ws.send_str(json.dumps(msg))

    def _handle_message(self, raw: str) -> None:
        for line in _split_frame(raw):
            self._handle_reply(line)

    def _handle_reply(self, raw: str) -> None:
        if _is_ping(raw):
            if self.ws:
                asyncio.ensure_future(self.ws.send_str("{}"))
            return
        try:
            obj = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        push = obj.get("push")
        if not isinstance(push, dict):
            return
        pub = push.get("pub", {})
        data = pub.get("data", {})
        if not isinstance(data, dict) or data.get("type") != "cursors_update":
            return
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            return
        if payload.get("user_uuid") == self.auth.user_uuid:
            return
        cursors = payload.get("cursors", [])
        if not cursors:
            return
        nums = []
        for c in cursors:
            if not isinstance(c, dict):
                return
            row = c.get("row")
            col = c.get("column", 0)
            if not isinstance(row, (int, float)) or row < 0:
                return
            nums.append(int(row))
            nums.append(int(col) if isinstance(col, (int, float)) and col >= 0 else 0)
        decoded = _bytes_from_numbers(nums)
        if len(decoded) < 2:
            return
        data_len = struct.unpack_from(">H", decoded, 0)[0]
        if 2 + data_len > len(decoded):
            return
        chunk = decoded[2:2 + data_len]

        self._recv_buf += chunk
        buf = self._recv_buf
        adv = 0
        while len(buf) - adv >= 2:
            ln = struct.unpack_from(">H", buf, adv)[0]
            if ln == 0:
                adv = len(buf)
                break
            if len(buf) - adv < 2 + ln:
                break
            pkt = bytes(buf[adv + 2:adv + 2 + ln])
            if self.on_data:
                self.on_data(pkt)
            adv += 2 + ln
            self.packets_recv += 1
        self._recv_buf = bytearray(buf[adv:])
        if len(self._recv_buf) > self.cfg.max_payload_bytes + self.cfg.max_message_data:
            log.debugf("[CUPS] recv stream out of sync (%s), resetting", self.room_uuid)
            self._recv_buf.clear()

    async def stop(self) -> None:
        self._stop = True
        if self.ws:
            await self.ws.close()
        if self.session:
            try:
                await self.session.close()
            except Exception:  # noqa: BLE001
                pass


class CupsonlineTransport(BaseTransport):
    def __init__(self, raw_url: str, config: TransportConfig, is_client: bool) -> None:
        super().__init__(config)
        self._cfg = CupsConfig()
        import os
        try:
            n = int(os.getenv("OPENFLUX_CUPS_ROOMS", str(self._cfg.num_rooms)))
            if n > 0:
                self._cfg.num_rooms = n
        except ValueError:
            pass
        self._is_client = is_client
        self._room_ids = parse_room_list(raw_url)
        self._wss: List[_CupsWS] = []
        self._rr = 0
        self._client_err: Optional[str] = None
        self._tasks: List[asyncio.Task] = []
        if is_client and not self._room_ids:
            self._client_err = "client mode: --url must contain base64 room list"

    async def start(self) -> None:
        if self._client_err:
            raise RuntimeError(self._client_err)
        await super().start()
        slots = await self._enter_rooms()
        for i, (room_id, auth) in enumerate(slots):
            ws = _CupsWS(i, room_id, self._cfg, auth, self.call_receive)
            if auth is None:
                ws.dead = True
            self._wss.append(ws)
        for ws in self._wss:
            self._tasks.append(asyncio.ensure_future(ws.run()))
        self.set_connected(True)
        log.debugf("[CUPS] transport started: %d channels", len(self._wss))

    async def _enter_rooms(self):
        if self._room_ids:
            slots = []
            results = await asyncio.gather(
                *[self._try_join(rid) for rid in self._room_ids], return_exceptions=True)
            joined = 0
            for rid, res in zip(self._room_ids, results):
                if isinstance(res, _CupsAuth):
                    slots.append((rid, res))
                    joined += 1
                else:
                    slots.append((rid, None))
            log.infof("[CUPS] joined %d of %d rooms", joined, len(slots))
            if joined > 0:
                return slots
            if self._is_client:
                raise RuntimeError("no rooms joined")
            log.infof("[CUPS] saved rooms unreachable; creating new ones")

        auths = await _create_rooms(self._cfg.num_rooms, self._cfg.room_create_pause)
        ids = [a.room_uuid for a in auths]
        print("\n=== COPY THIS TO CLIENT ===")
        print(pack_rooms(ids))
        print("===========================")
        print("Save it and pass it back as --url to reuse these rooms after a restart.\n", flush=True)
        return [(a.room_uuid, a) for a in auths]

    async def _try_join(self, room_id: str) -> _CupsAuth:
        return await _join_room(room_id, None)

    async def stop(self) -> None:
        for ws in self._wss:
            await ws.stop()
        for t in self._tasks:
            t.cancel()
        self.set_connected(False)
        await super().stop()

    async def send(self, data: bytes) -> None:
        if len(data) > self._cfg.max_payload_bytes:
            raise ValueError(f"cups: payload {len(data)} over limit {self._cfg.max_payload_bytes}")
        ws = self._pick_room()
        if ws is None:
            raise RuntimeError("cups: no connected room")
        await ws.enqueue(data)

    def _pick_room(self) -> Optional[_CupsWS]:
        n = len(self._wss)
        if n == 0:
            return None
        self._rr += 1
        for i in range(n):
            ws = self._wss[(self._rr + i) % n]
            if ws.connected:
                return ws
        return None

    def is_connected(self) -> bool:
        return any(ws.connected for ws in self._wss)

    def room_list(self) -> str:
        ids = [ws.room_uuid for ws in self._wss if ws.room_uuid]
        if ids:
            return pack_rooms(ids)
        if self._room_ids:
            return pack_rooms(self._room_ids)
        return ""
