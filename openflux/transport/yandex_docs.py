"""Yandex.Docs transport — a port of transport/yandex/yandex.go.

Two peers open the same public Yandex document and smuggle packets through the
"cursor" field of the collaborative editor (socket.io/engine.io over WS). The
document URL is shared out of band. Handles the PoW captcha in the redirect
chain; SmartCaptcha / login walls raise so the caller can fetch cookies out of
band. This is a best-effort message carrier; the mux layer adds reliability.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
from typing import Optional

import aiohttp

from .base import BaseTransport, TransportConfig
from .. import logging_util as log
from .yandex_captcha import solve_captcha

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) Gecko/20100101 Firefox/153.0"

_re_cursor = re.compile(r'"cursor":"[^;]+;([^"]+)"')
_re_client_config = re.compile(r'<script[^>]*id="client-config"[^>]*>(.*?)</script>', re.S)


class CaptchaRequired(Exception):
    pass


class LoginRequired(Exception):
    pass


class _DocInfo:
    def __init__(self) -> None:
        self.cookie_str = ""
        self.token = ""
        self.doc_id = ""
        self.origin = ""
        self.host = ""
        self.ws_url = ""
        self.permissions: dict = {}
        self.open_cmd: dict = {}


def _rand_user_id() -> str:
    return f"{random.randint(0, 999999999):010d}"


class YandexDocsTransport(BaseTransport):
    def __init__(self, url: str, config: TransportConfig) -> None:
        super().__init__(config)
        self._url = url
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._base_user = _rand_user_id()
        self._user_counter = 0
        self._write_q: asyncio.Queue = asyncio.Queue(maxsize=config.max_queue_size)
        self._tasks: list = []
        self._stop = False

    async def start(self) -> None:
        await super().start()
        self._stop = False
        self._session = aiohttp.ClientSession()
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

    async def send(self, data: bytes) -> None:
        if not self.is_connected():
            raise RuntimeError("transport not connected")
        try:
            self._write_q.put_nowait(bytes(data))
            self.record_send(len(data))
        except asyncio.QueueFull:
            raise RuntimeError("write queue full")

    async def _connect_loop(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                await self._connect_once()
            except (CaptchaRequired, LoginRequired) as e:
                log.debugf("[YDOCS] external solver needed: %s", e)
                await asyncio.sleep(30)
            except Exception as e:  # noqa: BLE001
                log.debugf("[YDOCS] connect failed: %s", e)
            self.set_connected(False)
            attempt += 1
            await asyncio.sleep(min(1.5 * (2 ** min(attempt, 4)), 30))

    async def _connect_once(self) -> None:
        self._user_counter += 1
        user_id = self._base_user + f"{self._user_counter % 1000:03d}"
        info = await self._fetch_doc_info(self._url, user_id)

        headers = {"User-Agent": "Mozilla/5.0", "Origin": info.origin, "Cookie": info.cookie_str}
        assert self._session is not None
        async with self._session.ws_connect(info.ws_url, headers=headers,
                                             max_msg_size=16 << 20) as ws:
            self._ws = ws
            self.set_connected(True)
            log.debugf("[YDOCS] WebSocket connected to %s", info.host)

            await ws.send_str(f'40{{"token":"{info.token}"}}')
            auth_data = {
                "type": "auth", "docid": info.doc_id, "token": "fghhfgsjdgfjs",
                "user": {"id": user_id}, "editorType": 0, "lastOtherSaveTime": -1,
                "permissions": info.permissions, "openCmd": info.open_cmd,
                "coEditingMode": "fast", "jwtOpen": info.token,
            }
            await ws.send_str("42" + json.dumps(["message", auth_data]))

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
                self.set_connected(False)

    async def _writer_loop(self, ws) -> None:
        try:
            while True:
                pkt = await self._write_q.get()
                payload = base64.b64encode(pkt).decode()
                await ws.send_str(f'42["message",{{"type":"cursor","cursor":"18;{payload}"}}]')
        except (asyncio.CancelledError, Exception):
            return

    async def _keepalive(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.get_config().keepalive_interval)
                await ws.send_str('42["message",{"type":"cursor","cursor":"18;---KA---"}]')
        except (asyncio.CancelledError, Exception):
            return

    def _handle_message(self, ws, text: str) -> None:
        if "---KA---" in text:
            return
        if text == "2":
            asyncio.ensure_future(ws.send_str("3"))
            return
        if text == "3":
            return
        if "saveChanges" in text or "cursor" in text:
            b64 = self._extract_b64(text)
            if not b64:
                return
            try:
                decoded = base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                return
            self.record_receive(len(decoded))
            self.call_receive(decoded)

    @staticmethod
    def _extract_b64(response: str) -> str:
        if "saveChanges" in response:
            marker = '"excelAdditionalInfo":"'
            left = response.find(marker)
            if left < 0:
                return ""
            left += len(marker)
            right = response.find('"', left)
            if right == -1:
                return ""
            return response[left:right]
        m = _re_cursor.search(response)
        return m.group(1) if m else ""

    async def _fetch_doc_info(self, url: str, user_id: str) -> _DocInfo:
        assert self._session is not None
        session = self._session
        current = url
        html = ""
        final_url = url
        for _ in range(10):
            async with session.get(current, headers={"User-Agent": UA},
                                    allow_redirects=False) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    final_url = str(resp.url)
                    break
                if 300 <= resp.status < 400:
                    loc = resp.headers.get("Location", "")
                    await resp.read()
                    if not loc:
                        raise RuntimeError("redirect without Location")
                    if "showcaptcha" in loc and "showcaptchafast" not in loc:
                        raise CaptchaRequired("smartcaptcha")
                    if "showcaptchafast" in loc:
                        await solve_captcha(session, current, UA)
                        current = url
                        continue
                    if "passport.yandex" in loc:
                        raise LoginRequired("login")
                    current = loc
                    continue
                await resp.read()
                raise RuntimeError(f"unexpected status {resp.status}")
        if not html:
            raise RuntimeError("no document body after redirects")

        m = _re_client_config.search(html)
        if not m:
            raise RuntimeError("client-config not found (doc not public?)")
        config = json.loads(m.group(1))
        office = config.get("officeActionData") or {}
        editor = office.get("editor_config") or {}
        balancer = office.get("balancer_url") or ""
        if not balancer:
            raise RuntimeError("balancer_url missing")
        document = editor.get("document") or {}
        token = editor.get("token") or ""
        doc_key = document.get("key") or ""
        if not token or not doc_key:
            raise RuntimeError("editor token/key missing")
        perms = document.get("permissions") or {}
        host = balancer.replace("https://", "")

        cookie_parts = []
        for name, morsel in session.cookie_jar.filter_cookies(final_url).items():
            cookie_parts.append(f"{name}={morsel.value}")

        info = _DocInfo()
        info.cookie_str = "; ".join(cookie_parts)
        info.token = token
        info.doc_id = doc_key
        info.origin = balancer
        info.host = host
        info.ws_url = f"wss://{host}/2024.1.1-375/doc/{doc_key}/c/?EIO=4&transport=websocket"
        info.permissions = perms
        info.open_cmd = {
            "c": "open", "id": doc_key, "userid": user_id,
            "format": document.get("fileType"), "url": document.get("url"),
            "title": document.get("title"), "lcid": 25,
        }
        return info

    # ---- cookie exchange ----

    def fetch_cookies(self) -> dict:
        if not self._session:
            return {}
        out = {}
        for name, morsel in self._session.cookie_jar.filter_cookies(self._url).items():
            out[name] = morsel.value
        return out

    def apply_cookies(self, values: dict) -> None:
        if not values or not self._session:
            return
        self._session.cookie_jar.update_cookies(values)
        if self._ws:
            asyncio.ensure_future(self._ws.close())
