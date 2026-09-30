"""MAX (VK / oneme) transport — a port of transport/oneme/*.go.

The Go code ships with ``useICEInjection = true``: it does NOT move data over a
real WebRTC data channel. It places (or answers) a MAX call, and then smuggles
each tunnel packet as a base64 string inside a fake ICE ``candidate`` field of a
``transmit-data`` signaling message, which MAX relays to the other call
participant. ``sendSDP`` is a no-op in the Go source and the PeerConnection is
vestigial — so this port needs neither aiortc nor a TURN path, just the MAX
signaling WebSockets.

Client mode places a call to the exit's uid; exit mode answers incoming calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import lzma  # noqa: F401  (kept for parity; vcp uses lz4, decoded below)
import struct
import uuid
from typing import Optional

import aiohttp

from .base import BaseTransport, TransportConfig
from .. import logging_util as log

WS_HOST = "wss://ws-api.oneme.ru/websocket"
RPC_VERSION = 11
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")


def _gen_uuid() -> str:
    return str(uuid.uuid4())


def _decode_call_details(vcp: str) -> str:
    """Decode the lz4-block-compressed call config (vcp) — see max_call_helper.go.
    Format: 3-digit decimal uncompressed size, then ':', then base64 lz4 block."""
    if len(vcp) < 4:
        raise ValueError("vcp too short")
    size = int(vcp[:3])
    import lz4.block  # optional dep; only oneme needs it
    decoded = base64.b64decode(vcp[4:])
    return lz4.block.decompress(decoded, uncompressed_size=size).decode()


def _craft_endpoint(conv_id: str, json_config: str) -> str:
    cfg = json.loads(json_config)
    base_url = cfg.get("wse", "")
    if len(base_url) > 4:
        base_url = base_url[:-4]
    turn_user = cfg.get("trnu", "")
    user_id = turn_user
    if ":" in turn_user:
        user_id = turn_user.rsplit(":", 1)[1]
    token = cfg.get("tkn", "")
    return (f"{base_url}/ws2?userId={user_id}&entityType=USER&deviceIdx=0"
            f"&conversationId={conv_id}&token={token}&platform=WEB&appVersion=1.1"
            f"&version=5&device=browser&capabilities=2A03F&clientType=ONE_ME&tgt=accept")


class MaxClient:
    def __init__(self) -> None:
        self.device_id = _gen_uuid()
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._seq = 0
        self._pending: dict = {}
        self._on_event = None
        self._logged_in = False
        self._tasks: list = []

    def set_event_callback(self, cb) -> None:
        self._on_event = cb

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            WS_HOST, headers={"Origin": "https://web.max.ru", "User-Agent": UA})
        self._tasks.append(asyncio.ensure_future(self._read_loop()))
        log.infof("[MAX] Connected")

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    packet = json.loads(msg.data)
                except Exception:  # noqa: BLE001
                    continue
                seq = packet.get("seq")
                fut = self._pending.pop(seq, None)
                if fut is not None and not fut.done():
                    fut.set_result(packet)
                elif self._on_event:
                    self._on_event(packet)
        except Exception as e:  # noqa: BLE001
            log.debugf("[MAX] read loop ended: %s", e)

    async def invoke(self, opcode: int, payload: dict, timeout: float = 30.0) -> dict:
        self._seq += 1
        seq = self._seq
        req = {"ver": RPC_VERSION, "cmd": 0, "seq": seq, "opcode": opcode, "payload": payload}
        fut = asyncio.get_event_loop().create_future()
        self._pending[seq] = fut
        await self._ws.send_str(json.dumps(req))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(seq, None)

    async def login_by_token(self, token: str) -> None:
        await self.invoke(6, {
            "userAgent": {"deviceType": "WEB", "locale": "ru_RU", "osVersion": "macOS",
                          "deviceName": "openflux-py", "appVersion": "25.9.15",
                          "screen": "956x1470 2.0x", "timezone": "Asia/Vladivostok"},
            "deviceId": self.device_id,
        })
        resp = await self.invoke(19, {"interactive": True, "token": token, "chatsSync": 0,
                                      "contactsSync": 0, "presenceSync": 0, "draftsSync": 0,
                                      "chatsCount": 40})
        payload = resp.get("payload", {})
        if "error" in payload:
            raise RuntimeError(f"login failed: {payload['error']}")
        self._logged_in = True
        self._tasks.append(asyncio.ensure_future(self._keepalive()))

    async def _keepalive(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                if self._logged_in:
                    await self.invoke(1, {"interactive": False})
        except (asyncio.CancelledError, Exception):
            return

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()


class CallHandler:
    def __init__(self, tag: str, role: str) -> None:
        self.tag = tag
        self.role = role
        self.seq = 1
        self.local_id = 0
        self.remote_id = 0
        self.accept_sent = False
        self.dc_inbound = None
        self._conn: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._reconnect = asyncio.Event()

    async def _read_loop(self) -> None:
        assert self._conn is not None
        log.infof("[%s] Signaling connected", self.tag)
        try:
            async for msg in self._conn:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                text = msg.data
                if "accepted-call" in text:
                    log.debugf("[%s] call accepted", self.tag)
                    continue
                if text == "ping":
                    await self._conn.send_str("pong")
                    continue
                if len(text) < 10:
                    continue
                try:
                    data = json.loads(text)
                except Exception:  # noqa: BLE001
                    continue
                if data.get("type") in ("response", "error"):
                    continue
                if isinstance(data.get("participantId"), (int, float)):
                    self.remote_id = int(data["participantId"])
                self._msg_handler(data)
        except Exception as e:  # noqa: BLE001
            log.debugf("[%s] signaling disconnected: %s", self.tag, e)
            self._reconnect.set()

    def _extract_local_id(self, data: dict, want_creator: bool) -> None:
        if self.local_id != 0:
            return
        conv = data.get("conversation")
        if not isinstance(conv, dict):
            return
        for p in conv.get("participants", []):
            roles = p.get("roles", [])
            is_creator = "CREATOR" in roles
            if is_creator == want_creator:
                self.local_id = int(p["id"])
                log.debugf("[%s] Local ID: %d", self.tag, self.local_id)

    def _handle_candidate(self, d: dict) -> None:
        c = d.get("candidate")
        if isinstance(c, dict):
            cand = c.get("candidate", "")
            try:
                decoded = base64.b64decode(cand)
            except Exception:  # noqa: BLE001
                return
            if self.dc_inbound:
                self.dc_inbound(decoded)

    async def _inject(self, payload: bytes) -> None:
        if self._conn is None:
            return
        b64 = base64.b64encode(payload).decode()
        msg = {"command": "transmit-data", "sequence": self.seq, "participantId": self.local_id,
               "data": {"candidate": {"candidate": b64}}, "participantType": "USER"}
        self.seq += 1
        await self._conn.send_str(json.dumps(msg))

    async def send(self, data: bytes) -> None:
        await self._inject(data)

    async def _send_accept(self) -> None:
        if self.accept_sent or self._conn is None:
            return
        self.accept_sent = True
        msg = ('{"command":"accept-call","sequence":%d,"mediaSettings":'
               '{"isAudioEnabled":true,"isVideoEnabled":false,"isScreenSharingEnabled":false,'
               '"isFastScreenSharingEnabled":false,"isAudioSharingEnabled":false,'
               '"isAnimojiEnabled":false}}' % self.seq)
        self.seq += 1
        await self._conn.send_str(msg)
        log.debugf("[%s] Accept-call sent", self.tag)

    # role-specific handlers assigned in start_* below
    def _msg_handler(self, data: dict) -> None:  # overridden
        pass


def start_outgoing_call(client: MaxClient, callee_id: int, loop) -> CallHandler:
    h = CallHandler("CALLER", "caller")

    def handler(data: dict) -> None:
        h._extract_local_id(data, want_creator=False)
        if "conversationParams" in data:
            # ICE-injection mode: nothing to negotiate; data flows via candidates.
            log.debugf("[CALLER] conversationParams received; ready")
            return
        d = data.get("data")
        if not isinstance(d, dict):
            return
        if "sdp" in d:
            return  # SDP ignored in ICE-injection mode
        if "candidate" in d:
            h._handle_candidate(d)

    h._msg_handler = handler
    asyncio.ensure_future(_caller_loop(client, callee_id, h))
    return h


async def _caller_loop(client: MaxClient, callee_id: int, h: CallHandler) -> None:
    while True:
        h.accept_sent = False
        h.seq = 1
        resp = await client.invoke(78, {
            "conversationId": _gen_uuid(),
            "calleeIds": [callee_id],
            "internalParams": ('{"deviceId":"%s","sdkVersion":"2.8.9",'
                               '"clientAppKey":"CNHIJPLGDIHBABABA","platform":"WEB",'
                               '"protocolVersion":5,"domainId":"","capabilities":"2A03F"}'
                               % client.device_id),
            "isVideo": False,
        })
        payload = resp.get("payload", {})
        params_str = payload.get("internalCallerParams", "{}")
        params = json.loads(params_str)
        endpoint = params.get("endpoint", "") + \
            "&platform=WEB&appVersion=1.1&version=5&device=browser&capabilities=2A03F&clientType=ONE_ME&tgt=start"
        try:
            h._session = aiohttp.ClientSession()
            h._conn = await h._session.ws_connect(endpoint)
        except Exception as e:  # noqa: BLE001
            log.errorf("[CALLER] Dial error: %s, retrying...", e)
            await asyncio.sleep(1)
            continue
        h._reconnect.clear()
        asyncio.ensure_future(h._read_loop())
        await h._reconnect.wait()
        log.infof("[CALLER] Reconnecting in 1s...")
        try:
            await h._conn.close()
            await h._session.close()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1)


def start_incoming_listener(client: MaxClient) -> CallHandler:
    h = CallHandler("RECEIVER", "receiver")

    def handler(data: dict) -> None:
        h._extract_local_id(data, want_creator=True)
        if "conversationParams" in data:
            return
        d = data.get("data")
        if not isinstance(d, dict):
            return
        if "sdp" in d:
            return
        if "candidate" in d:
            h._handle_candidate(d)

    h._msg_handler = handler

    def on_event(packet: dict) -> None:
        if packet.get("opcode") == 137:
            asyncio.ensure_future(_answer_call(packet, h))

    client.set_event_callback(on_event)
    log.infof("[RECEIVER] Waiting for calls...")
    return h


async def _answer_call(packet: dict, h: CallHandler) -> None:
    payload = packet.get("payload", {})
    conv_id = payload.get("conversationId", "")
    vcp = payload.get("vcp", "")
    try:
        call_details = _decode_call_details(vcp)
    except Exception as e:  # noqa: BLE001
        log.errorf("[RECEIVER] Decode error: %s", e)
        return
    endpoint = _craft_endpoint(conv_id, call_details)
    try:
        h._session = aiohttp.ClientSession()
        h._conn = await h._session.ws_connect(endpoint)
    except Exception as e:  # noqa: BLE001
        log.errorf("[RECEIVER] Connect error: %s", e)
        return
    h._reconnect.clear()
    asyncio.ensure_future(h._read_loop())
    await asyncio.sleep(1)
    if not h.accept_sent:
        await h._send_accept()


class OneMeTransport(BaseTransport):
    def __init__(self, is_exit: bool, token: str, uid: int, config: TransportConfig) -> None:
        super().__init__(config)
        self._exit = is_exit
        self._token = token
        self._uid = uid
        self._client: Optional[MaxClient] = None
        self._ch: Optional[CallHandler] = None

    async def start(self) -> None:
        await super().start()
        self._client = MaxClient()
        await self._client.connect()
        await self._client.login_by_token(self._token)
        loop = asyncio.get_event_loop()
        if self._exit:
            self._ch = start_incoming_listener(self._client)
        else:
            self._ch = start_outgoing_call(self._client, self._uid, loop)
        self._ch.dc_inbound = self.call_receive
        self.set_connected(True)

    async def stop(self) -> None:
        if self._client:
            await self._client.close()
        self.set_connected(False)
        await super().stop()

    async def send(self, data: bytes) -> None:
        if self._ch:
            await self._ch.send(data)

    def is_connected(self) -> bool:
        return self._connected
