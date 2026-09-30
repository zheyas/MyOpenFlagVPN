"""Mail.ru Docs transport — a port of transport/mailru/mailru.go.

Same coauthoring-editor family as Yandex.Docs: two peers open the same public
Mail.ru cloud document and smuggle packets through the editor's cursor field
over socket.io/engine.io. Accepts a bare weblink or a full public URL.
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

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")

_re_cursor = re.compile(r'"cursor":"[^;]+;([^"]+)"')


def _normalize_weblink(weblink: str) -> str:
    weblink = weblink.strip()
    for prefix in ("https://cloud.mail.ru/public/", "http://cloud.mail.ru/public/",
                   "https://cloud.mail.ru/", "http://cloud.mail.ru/"):
        if weblink.startswith(prefix):
            return weblink[len(prefix):].strip("/")
    return weblink


class _DocInfo:
    token = ""
    doc_key = ""
    ws_url = ""
    file_type = ""
    doc_url = ""
    doc_title = ""
    permissions: dict = {}
    callback_url = ""
    editor_user_id = ""


class MailruDocsTransport(BaseTransport):
    def __init__(self, weblink: str, config: TransportConfig) -> None:
        super().__init__(config)
        self._weblink = _normalize_weblink(weblink)
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._base_user = f"{random.randint(0, 999999999):010d}"
        self._counter = 0
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
            except Exception as e:  # noqa: BLE001
                log.debugf("[M-DOCS] connect failed: %s", e)
            self.set_connected(False)
            attempt += 1
            await asyncio.sleep(min(0.5 * (2 ** min(attempt, 5)), 15))

    async def _connect_once(self) -> None:
        self._counter += 1
        user_id = self._base_user + f"{self._counter % 1000:03d}"
        info = await self._fetch_doc_info()

        headers = {"User-Agent": UA, "Origin": "https://docs.datacloudmail.ru"}
        assert self._session is not None
        async with self._session.ws_connect(info.ws_url, headers=headers,
                                             max_msg_size=16 << 20) as ws:
            self._ws = ws
            self.set_connected(True)
            log.debugf("[M-DOCS] WebSocket connected")

            await ws.send_str(f'40{{"token":"{info.token}"}}')
            auth_msg = {
                "type": "auth", "docid": info.doc_key, "documentCallbackUrl": info.callback_url,
                "token": "fghhfgsjdgfjs",
                "user": {"id": info.editor_user_id, "username": user_id, "indexUser": -1},
                "editorType": 0, "lastOtherSaveTime": -1, "block": [],
                "documentFormatSave": 65, "view": False, "isCloseCoAuthoring": False,
                "openCmd": {"c": "open", "id": info.doc_key, "userid": info.editor_user_id,
                            "format": info.file_type, "url": info.doc_url, "title": info.doc_title,
                            "lcid": 25, "nobase64": True, "convertToOrigin": ".pdf.xps.oxps.djvu"},
                "lang": "ru", "mode": "edit", "permissions": info.permissions,
                "IsAnonymousUser": False, "timezoneOffset": -180, "coEditingMode": "fast",
                "jwtOpen": info.token, "time": 1000, "supportAuthChangesAck": True,
            }
            await ws.send_str("42" + json.dumps(["message", auth_msg]))

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
        if '"type":"auth"' in text and '"result":1' in text:
            return
        if "cursor" in text:
            m = _re_cursor.search(text)
            if not m:
                return
            try:
                decoded = base64.b64decode(m.group(1))
            except Exception:  # noqa: BLE001
                return
            self.record_receive(len(decoded))
            self.call_receive(decoded)

    async def _fetch_doc_info(self) -> _DocInfo:
        assert self._session is not None
        body = {"x-email": "anonym", "public": "/" + self._weblink, "platform": "desktop_web"}
        headers = {
            "Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
            "User-Agent": UA, "X-Api-Version": "4",
            "Referer": f"https://cloud.mail.ru/public/{self._weblink}?weblink={self._weblink}",
        }
        async with self._session.post("https://cloud.mail.ru/api/v4/r7/edit",
                                       json=body, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"API returned status {resp.status}")
            res = await resp.json()

        api_base = res.get("api", "")
        document = res.get("document") or {}
        if not document:
            raise RuntimeError("document object missing")
        editor_config = res.get("editorConfig") or {}
        if not editor_config:
            raise RuntimeError("editorConfig object missing")

        info = _DocInfo()
        info.token = res.get("token", "")
        info.doc_key = document.get("key", "")
        info.file_type = document.get("fileType", "")
        info.doc_url = document.get("url", "")
        info.doc_title = document.get("title", "")
        info.permissions = document.get("permissions") or {}
        info.callback_url = editor_config.get("callbackUrl", "")
        user = editor_config.get("user") or {}
        info.editor_user_id = user.get("id", "")
        ws_base = api_base.replace("https://", "wss://")
        info.ws_url = f"{ws_base}/doc/{info.doc_key}/c/?EIO=4&transport=websocket"
        return info

    # ---- cookie exchange ----

    def fetch_cookies(self) -> dict:
        if not self._session:
            return {}
        out = {}
        for name, morsel in self._session.cookie_jar.filter_cookies("https://cloud.mail.ru/").items():
            out[name] = morsel.value
        return out

    def apply_cookies(self, values: dict) -> None:
        if not values or not self._session:
            return
        self._session.cookie_jar.update_cookies(values)
        if self._ws:
            asyncio.ensure_future(self._ws.close())
