"""Yandex Volga transport — a port of transport/yandex/vyandex.go.

Uses the Yandex "Volga" document backend: outbound packets are POSTed to an
HTTP relay endpoint (batched, base64 inside a fake edit bundle), and inbound
packets arrive over a push.yandex.ru WebSocket. The document URL is shared out
of band. PoW captcha in the redirect chain is solved; SmartCaptcha / login
raise. Best-effort carrier; the mux layer adds reliability.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

import aiohttp

from .base import BaseTransport, TransportConfig
from .. import logging_util as log
from .yandex_captcha import solve_captcha
from .yandex_docs import CaptchaRequired, LoginRequired

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) Gecko/20100101 Firefox/153.0"
_re_client_config = re.compile(r'<script[^>]*id="client-config"[^>]*>(.*?)</script>', re.S)


def _decode_batch(decoded: bytes) -> List[bytes]:
    out = []
    off = 0
    while len(decoded) - off >= 2:
        ln = struct.unpack_from(">H", decoded, off)[0]
        off += 2
        if ln == 0 or len(decoded) - off < ln:
            break
        out.append(decoded[off:off + ln])
        off += ln
    if not out and decoded:
        out.append(decoded)
    return out


class _Auth:
    session: Optional[aiohttp.ClientSession] = None
    token = ""
    request_path = ""
    user_id = 0
    user_id_str = ""
    sign = ""
    ts = ""
    session_id = ""


class YandexVolgaTransport(BaseTransport):
    def __init__(self, doc_url: str, config: TransportConfig) -> None:
        super().__init__(config)
        self._doc_url = doc_url
        self._session: Optional[aiohttp.ClientSession] = None
        self._auth: Optional[_Auth] = None
        self._on_data = None
        self._send_q: asyncio.Queue = asyncio.Queue(maxsize=100000)
        self._tasks: list = []
        self._stop = False
        self._bundle_id = 0
        self._seq = 0
        self._local_id = 0
        self._frontier = ""

    async def start(self) -> None:
        await super().start()
        self._stop = False
        self._session = aiohttp.ClientSession()
        self._auth = await self._authorize()
        self.set_connected(True)
        self._tasks.append(asyncio.ensure_future(self._relay_loop()))
        self._tasks.append(asyncio.ensure_future(self._ws_loop()))
        self._tasks.append(asyncio.ensure_future(self._keepalive()))

    async def stop(self) -> None:
        self._stop = True
        for t in self._tasks:
            t.cancel()
        if self._session:
            await self._session.close()
        self.set_connected(False)
        await super().stop()

    def set_receive(self, cb) -> None:
        self._on_data = cb

    async def send(self, data: bytes) -> None:
        if not data:
            return
        try:
            self._send_q.put_nowait(bytes(data))
        except asyncio.QueueFull:
            raise RuntimeError("queue full")

    # ---- authorize ----

    async def _authorize(self) -> _Auth:
        session = self._session
        assert session is not None
        current = self._doc_url
        final_body = ""
        final_url = self._doc_url
        for _ in range(15):
            async with session.get(current, headers={
                    "User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }, allow_redirects=False) as resp:
                if resp.status == 200:
                    final_body = await resp.text()
                    final_url = str(resp.url)
                    break
                if 300 <= resp.status < 400:
                    loc = resp.headers.get("Location", "")
                    await resp.read()
                    if not loc:
                        raise RuntimeError("redirect without Location")
                    if "showcaptcha" in loc and "showcaptchafast" not in loc:
                        raise CaptchaRequired("smartcaptcha")
                    if "passport.yandex" in loc:
                        raise LoginRequired("login")
                    if "showcaptchafast" in loc:
                        await solve_captcha(session, self._doc_url, UA)
                        current = self._doc_url
                        continue
                    if loc.startswith("/"):
                        u = urlparse(current)
                        loc = f"{u.scheme}://{u.netloc}{loc}"
                    current = loc
                    continue
                await resp.read()
                raise RuntimeError(f"unexpected status {resp.status}")
        if not final_body:
            raise RuntimeError("too many redirects")

        m = _re_client_config.search(final_body)
        if not m:
            raise RuntimeError("client-config not found")
        cfg = json.loads(m.group(1))
        office = cfg.get("officeActionData") or {}
        editor = cfg.get("editorParams") or {}
        action_url = office.get("action_url", "")
        access_token = office.get("access_token", "")
        ttl = office.get("access_token_ttl", 0)
        if not action_url or not access_token:
            raise RuntimeError("action_url/access_token missing")

        form = {"access_token": access_token, "access_token_ttl": str(int(ttl) if ttl else 0)}
        async with session.post(action_url, data=form, headers={
                "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://disk.yandex.ru", "Referer": final_url,
        }, allow_redirects=False) as resp:
            await resp.read()
            if resp.status != 302:
                raise RuntimeError(f"auth/initial status {resp.status}")
            location = resp.headers.get("Location", "")
        if not location or "/document/error/" in location:
            raise RuntimeError("auth/initial bad Location")

        qs = parse_qs(urlparse(location).query)
        a = _Auth()
        a.session = session
        a.token = qs.get("token", [""])[0]
        a.request_path = qs.get("request-path", [""])[0]
        json_str = qs.get("json", [""])[0]
        if not json_str:
            raise RuntimeError("no json in Location")
        jd = json.loads(json_str)
        a.session_id = jd.get("sessionId", "")
        a.user_id = int(jd.get("userId", 0))
        xiva = jd.get("xiva") or {}
        a.sign = xiva.get("sign", "")
        a.ts = xiva.get("ts", "")
        a.user_id_str = xiva.get("user", "")

        async with session.get(location, headers={"User-Agent": UA, "Referer": action_url}) as resp:
            await resp.read()

        if not (a.token and a.request_path and a.user_id_str and a.sign):
            raise RuntimeError("incomplete auth")
        log.debugf("[VOLGA] auth OK user=%d rp=%s", a.user_id, a.request_path)
        return a

    # ---- relay (send) ----

    async def _relay_loop(self) -> None:
        try:
            while not self._stop:
                first = await self._send_q.get()
                batch = [first]
                total = len(first)
                try:
                    while len(batch) < 20 and total < 4 * 1024 * 1024:
                        pkt = await asyncio.wait_for(self._send_q.get(), timeout=0.002)
                        batch.append(pkt)
                        total += len(pkt)
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._send_batch(batch)
                except Exception as e:  # noqa: BLE001
                    log.debugf("[VOLGA] batch send failed: %s", e)
        except (asyncio.CancelledError, Exception):
            return

    async def _send_batch(self, batch: List[bytes]) -> None:
        a = self._auth
        assert a is not None and a.session is not None
        blob = bytearray()
        for p in batch:
            blob += struct.pack(">H", len(p))
            blob += p
        encoded = base64.b64encode(bytes(blob)).decode()

        self._seq += 1
        op_id = f"1-{a.user_id}.{self._seq}"
        self._seq += 1
        relay_op_id = f"1-{a.user_id}.{self._seq}"
        self._local_id += 1
        lid1 = self._local_id
        self._local_id += 1
        lid2 = self._local_id
        self._bundle_id += 1

        frontier = [self._frontier] if self._frontier else []
        bundle = [
            {"id": op_id, "frontier": frontier, "undoable": True, "actionName": "textInsert",
             "ops": [["it", "vyd:t/00000000000008", 0, "A"]], "sideEffect": False, "localId": lid1},
            {"id": relay_op_id, "frontier": [op_id], "undoable": False, "actionName": "setCaret",
             "ops": [["us", a.user_id, [[["vyd:t/00000000000008", 0, -1],
                                          ["vyd:t/00000000000008", 0, -1]]]]],
             "sideEffect": True, "localId": lid2},
            encoded,
        ]
        payload = {"message": {"bundleId": self._bundle_id, "bundle": bundle}, "targetUserId": None}
        url = f"https://volga.yandex.ru/session/main/{a.request_path}/relay"
        headers = {
            "User-Agent": UA, "Authorization": "Bearer " + a.token,
            "Content-Type": "application/json", "Origin": "https://volga.yandex.ru",
            "Referer": f"https://volga.yandex.ru/document/?request-path={a.request_path}",
            "Accept": "*/*",
        }
        async with a.session.post(url, data=json.dumps(payload), headers=headers) as resp:
            await resp.read()
            if resp.status not in (200, 204):
                raise RuntimeError(f"relay status {resp.status}")

    # ---- WS (receive) ----

    async def _ws_loop(self) -> None:
        while not self._stop:
            try:
                await self._ws_connect()
            except Exception as e:  # noqa: BLE001
                log.debugf("[VOLGA] WS error: %s", e)
            if self._stop:
                return
            await asyncio.sleep(0.5)

    async def _ws_connect(self) -> None:
        a = self._auth
        assert a is not None and a.session is not None
        from urllib.parse import quote
        ws_url = ("wss://push.yandex.ru/v2/subscribe/websocket?service=volga"
                  f"&user={quote(a.user_id_str)}&sign={a.sign}&ts={a.ts}&client=web"
                  f"&session={a.session_id}"
                  f"&fetch_history={quote(a.user_id_str + ':volga:0:1')}&x_request_attempt=0")
        headers = {"User-Agent": UA, "Origin": "https://volga.yandex.ru"}
        async with a.session.ws_connect(ws_url, headers=headers, max_msg_size=16 << 20) as ws:
            log.debugf("[VOLGA] WS connected user=%s", a.user_id_str)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_ws(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return

    def _handle_ws(self, raw: str) -> None:
        try:
            envelope = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        op = envelope.get("operation")
        if op == "ping" or op not in ("SESSION", "WORKER"):
            return
        message = envelope.get("message")
        if not message:
            return
        try:
            inner = json.loads(message)
        except Exception:  # noqa: BLE001
            return
        if inner.get("userId") == self._auth.user_id:
            return
        t = inner.get("t")
        if t == "relay":
            rel = inner.get("message") or {}
            for item in rel.get("bundle", []):
                self._handle_bundle_item(item)
        elif t == "exchange":
            self._handle_bundle(inner.get("bundle"))

    def _handle_bundle(self, raw) -> None:
        if isinstance(raw, list):
            for item in raw:
                self._handle_bundle_item(item)
        elif isinstance(raw, dict):
            for item in raw.get("value", []):
                self._handle_bundle_item(item)

    def _handle_bundle_item(self, item) -> None:
        if isinstance(item, dict):
            if item.get("actionName") and item.get("id"):
                self._frontier = item["id"]
            return
        if isinstance(item, str) and item:
            try:
                decoded = base64.b64decode(item)
            except Exception:  # noqa: BLE001
                return
            for pkt in _decode_batch(decoded):
                self.record_receive(len(pkt))
                if self._on_data:
                    self._on_data(pkt)

    async def _keepalive(self) -> None:
        try:
            while not self._stop:
                await asyncio.sleep(self.get_config().keepalive_interval)
                try:
                    self._send_q.put_nowait(b"\x00")
                except asyncio.QueueFull:
                    pass
        except (asyncio.CancelledError, Exception):
            return
