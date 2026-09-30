"""Yandex Boards transport — a port of transport/yandex/boards.go.

Tunnels packets through a Yandex whiteboard (boards.yandex.ru) over
socket.io/engine.io: packets are base64'd into the ``notify-position`` cursor
event and read back from peers' notify-position / modify-objects events. The
URL carries the board hash (``...?hash=<hash>``). Best-effort carrier.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs

import aiohttp

from .base import BaseTransport, TransportConfig
from .. import logging_util as log

BASE = "boards.yandex.ru"
UA = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Mobile Safari/537.36")
DEFAULT_WS_HOST = "socket33.boards.yandex.ru"
PING_INTERVAL = 20.0


def _extract_hash(raw_url: str) -> str:
    try:
        return parse_qs(urlparse(raw_url).query).get("hash", [""])[0]
    except Exception:  # noqa: BLE001
        return ""


def _random_guest_name() -> str:
    return "guest_" + os.urandom(3).hex()


def _jwt_payload(jwt: str) -> dict:
    parts = jwt.split(".")
    if len(parts) < 2:
        return {}
    b = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(b))
    except Exception:  # noqa: BLE001
        return {}


class _Info:
    hash = ""
    name = ""
    user_hash = ""
    jwt = ""
    cookies: dict = {}
    ws_host = DEFAULT_WS_HOST
    session = ""
    dashboard = ""
    current_slide = ""


class BoardsTransport(BaseTransport):
    def __init__(self, raw_url: str, config: TransportConfig) -> None:
        super().__init__(config)
        self._url = raw_url
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._on_data = None
        self._info: Optional[_Info] = None
        self._ack = 0
        self._write_q: asyncio.Queue = asyncio.Queue(maxsize=config.max_queue_size)
        self._tasks: list = []
        self._stop = False
        self._participant = ""

    async def start(self) -> None:
        await super().start()
        self._stop = False
        h = _extract_hash(self._url)
        if not h:
            raise ValueError(f"boards: no hash in URL {self._url!r}")
        self._session = aiohttp.ClientSession()
        self._info = await self._authorize(h, _random_guest_name())
        self._participant = self._info.user_hash
        self._tasks.append(asyncio.ensure_future(self._connect_loop()))

    async def stop(self) -> None:
        self._stop = True
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        for t in self._tasks:
            t.cancel()
        self.set_connected(False)
        await super().stop()

    def set_receive(self, cb) -> None:
        self._on_data = cb

    async def send(self, data: bytes) -> None:
        if not data:
            return
        try:
            self._write_q.put_nowait(bytes(data))
        except asyncio.QueueFull:
            raise RuntimeError("boards: queue full")

    # ---- authorize ----

    async def _authorize(self, h: str, name: str) -> _Info:
        session = self._session
        assert session is not None
        doc_url = f"https://{BASE}/whiteboard/?hash={h}"
        async with session.get(doc_url, headers={"User-Agent": UA}, allow_redirects=False) as resp:
            await resp.read()  # captcha path omitted (rare); PoW handled elsewhere

        await self._post_api(h, "request-guest-token", {"name": name, "hash": h})

        jwt = ""
        for name_, morsel in session.cookie_jar.filter_cookies(f"https://{BASE}").items():
            if name_ == "token_" + h:
                jwt = morsel.value
        if not jwt:
            raise RuntimeError(f"token_{h} not found")
        payload = _jwt_payload(jwt)
        user_hash = payload.get("u", "")

        state = await self._get_whiteboard_info(h)

        info = _Info()
        info.hash = h
        info.name = name
        info.user_hash = user_hash
        info.jwt = jwt
        info.cookies = {k: m.value for k, m in session.cookie_jar.filter_cookies(f"https://{BASE}").items()}
        info.ws_host = state.get("ws_host", DEFAULT_WS_HOST)
        info.dashboard = state.get("dashboard", "")
        info.current_slide = state.get("current_slide", "")
        log.debugf("[BOARDS] auth OK hash=%s user=%s ws=%s", h, user_hash, info.ws_host)
        return info

    async def _post_api(self, h: str, action: str, content: dict) -> None:
        content_b64 = base64.b64encode(json.dumps(content).encode()).decode()
        async with self._session.post(f"https://{BASE}/api",
                                       data={"action": action, "content": content_b64}, headers={
                "User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": f"https://{BASE}/guest/?hash={h}", "Origin": f"https://{BASE}",
        }) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"{action} status {resp.status}: {body[:200]}")

    async def _get_whiteboard_info(self, h: str) -> dict:
        content_b64 = base64.b64encode(json.dumps({"hash": h}).encode()).decode()
        try:
            async with self._session.post(f"https://{BASE}/api",
                                           data={"action": "get-whiteboard-info", "content": content_b64},
                                           headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                                                    "Referer": f"https://{BASE}/guest/?hash={h}",
                                                    "Origin": f"https://{BASE}"}) as resp:
                if resp.status != 200:
                    return {}
                info = await resp.json()
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        pres = info.get("presentation") or {}
        props = pres.get("properties") or {}
        cs = props.get("current_slide")
        if cs:
            out["current_slide"] = cs
            out["dashboard"] = cs
        socket_servers = info.get("socket_servers") or []
        if socket_servers:
            out["ws_host"] = socket_servers[0].get("ip", DEFAULT_WS_HOST)
        return out

    # ---- WS ----

    async def _connect_loop(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                await self._connect_and_serve()
            except Exception as e:  # noqa: BLE001
                log.debugf("[BOARDS] ws error: %s", e)
            self.set_connected(False)
            if self._stop:
                return
            attempt = min(attempt + 1, 10)
            await asyncio.sleep(min(0.5 * (2 ** min(attempt, 4)), 15))

    async def _connect_and_serve(self) -> None:
        info = self._info
        assert info is not None and self._session is not None
        ws_url = f"wss://{info.ws_host}/socket.io/?EIO=4&transport=websocket"
        cookies = dict(info.cookies)
        cookies.setdefault("token_" + info.hash, info.jwt)
        cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers = {"User-Agent": UA, "Origin": f"https://{BASE}", "Cookie": cookie_hdr}

        async with self._session.ws_connect(ws_url, headers=headers, max_msg_size=16 << 20) as ws:
            self._ws = ws
            await self._handshake(ws)
            self.set_connected(True)
            writer = asyncio.ensure_future(self._writer_loop(ws))
            ka = asyncio.ensure_future(self._keepalive(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_message(ws, msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                writer.cancel()
                ka.cancel()
                self._ws = None

    async def _write_event(self, ws, ns: str, obj) -> None:
        ack = self._ack
        self._ack += 1
        await ws.send_str(f"42{ack}" + json.dumps([ns, obj]))

    async def _handshake(self, ws) -> None:
        await ws.receive()          # engine.io hello (0{...})
        await ws.send_str("40")
        await ws.receive()          # socket.io connect ack (40...)
        await self._write_event(ws, "im", {"operation": "subscribe", "user": None})
        # wait for "subscribed"
        for _ in range(10):
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            if msg.type == aiohttp.WSMsgType.TEXT and '"subscribed"' in msg.data:
                break
        await self._send_subscribe(ws, self._participant)

    async def _send_subscribe(self, ws, participant: str) -> None:
        info = self._info
        data = {
            "session": info.session, "dashboard": info.dashboard, "presentation": info.hash,
            "properties": {"guest_mode": True, "guest_role": 1, "guest_password": None,
                           "guest_password_expiration_date": None, "current_slide": info.current_slide},
            "participant_team_role": -1, "participant": participant,
            "options": {"type": "landing",
                        "participant": {"hash": participant, "partner": "yandex",
                                        "userHash": info.user_hash, "name": info.name,
                                        "additional": {"guest": True}, "module": "yandex",
                                        "presentation": info.hash,
                                        "identityCandidates": {"uidHash": None, "legacyHash": participant,
                                                               "uid": None, "partner": "yandex"},
                                        "module_type": "yandex", "participantCaptionName": info.name},
                        "intermediate": "",
                        "device": {"screen": "674 x 619", "browser": "Chrome", "mobile": True,
                                   "os": "Android", "userAgent": UA, "platform": "MacIntel"}},
        }
        await self._write_event(ws, "dashboard",
                                {"action": "subscribe-slide-dashboard", "data": data,
                                 "participant": participant})

    async def _writer_loop(self, ws) -> None:
        try:
            while True:
                pkt = await self._write_q.get()
                b64 = base64.b64encode(pkt).decode()
                obj = {"action": "notify-position",
                       "data": {"position": {"x": b64, "y": 123.0},
                                "vpt": {"translate": {"x": 0, "y": 0}, "scale": 1, "whyrugay": 1}},
                       "participant": self._participant}
                await self._write_event(ws, "dashboard", obj)
        except (asyncio.CancelledError, Exception):
            return

    async def _keepalive(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await ws.send_str("2")  # engine.io ping
                await self._write_event(ws, "dashboard",
                                        {"action": "heartbeat", "data": {},
                                         "participant": self._participant})
        except (asyncio.CancelledError, Exception):
            return

    def _handle_message(self, ws, raw: str) -> None:
        if raw == "2":
            asyncio.ensure_future(ws.send_str("3"))
            return
        if raw == "3":
            return
        if not raw.startswith("42["):
            return
        idx = raw.find("[")
        try:
            arr = json.loads(raw[idx:])
        except Exception:  # noqa: BLE001
            return
        if len(arr) < 2 or not isinstance(arr[1], dict):
            return
        env = arr[1]
        action = env.get("action")
        if action == "notify-position":
            self._handle_notify(env.get("data"), env.get("participant", ""))
        elif action in ("server-modify-objects", "modify-objects"):
            self._handle_modify(env.get("data"))

    def _handle_notify(self, data, envelope_part: str) -> None:
        if isinstance(data, dict):
            pos = data.get("position") or {}
            x = pos.get("x")
            if isinstance(x, str) and x:
                if envelope_part and envelope_part == self._participant:
                    return
                self._emit(x)
                return
        if isinstance(data, list) and len(data) >= 5:
            sender = data[2] if isinstance(data[2], str) else ""
            if sender == self._info.name:
                return
            if isinstance(data[4], str):
                self._emit(data[4])

    def _handle_modify(self, data) -> None:
        if not isinstance(data, dict):
            return
        if data.get("name") and data.get("name") == self._info.name:
            return
        for o in data.get("objects", []):
            attrs = o.get("_attributes_") or {}
            val = attrs.get("value", "")
            if not val:
                continue
            creator = attrs.get("creatorHash", "")
            if creator and creator in (self._participant, self._info.user_hash):
                continue
            self._emit(val)

    def _emit(self, b64: str) -> None:
        try:
            decoded = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return
        if not decoded:
            return
        self.record_receive(len(decoded))
        if self._on_data:
            self._on_data(decoded)
