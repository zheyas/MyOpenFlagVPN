"""Tunnel engine: runs the OpenFlux client stack on a background asyncio loop.

One press of the power button walks through the same steps as
``run-client.sh`` + ``python -m openflux --role client``:

  1. fetch the room list from the exit's /rooms endpoint (with retries, the
     free PaaS exit may be asleep),
  2. build transport -> Batched -> Encrypted, join the rooms,
  3. start the mux and the local SOCKS5 server,
  4. probe the exit end to end,
  5. point the system SOCKS proxy at us.

Stopping unwinds it in reverse. Callbacks fire on the engine thread; the GUI
marshals them onto the Kivy main thread.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from openflux import logging_util as log
from openflux.mux import Mux
from openflux.socks5 import SOCKS5Server
from openflux.transport.base import Transport, default_config
from openflux.transport.batched import BatchedTransport
from openflux.transport.encrypted import EncryptedTransport
from openflux.transport.factory import make_transport

from . import sysproxy
from .settings import Settings

OFF, CONNECTING, CONNECTED, STOPPING, ERROR = "off", "connecting", "connected", "stopping", "error"

PROBE_HOST, PROBE_PORT = "1.1.1.1", 443


@dataclass
class Stats:
    since: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0
    reconnects: int = 0


class _Counting(Transport):
    """Counts payload bytes for the GUI: not every transport (cupsonline)
    fills in its own stats."""

    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self.sent = 0
        self.received = 0

    async def start(self) -> None:
        await self._inner.start()

    async def stop(self) -> None:
        await self._inner.stop()

    async def send(self, data: bytes) -> None:
        self.sent += len(data)
        await self._inner.send(data)

    def set_receive(self, cb) -> None:
        def counted(data: bytes) -> None:
            self.received += len(data)
            cb(data)
        self._inner.set_receive(counted)

    def is_connected(self) -> bool:
        return self._inner.is_connected()

    def stats(self):
        return self._inner.stats()


class TunnelEngine:
    def __init__(self,
                 on_state: Callable[[str, str], None],
                 on_stats: Callable[[Stats], None]) -> None:
        self._on_state = on_state
        self._on_stats = on_stats
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt: Optional[asyncio.Event] = None
        self._main: Optional[asyncio.Task] = None
        self.state = OFF
        self._used_saved = False
        # File that exists while we own the system proxy, so a crashed run can
        # be cleaned up on the next launch (see restore_proxy_if_stale).
        self.proxy_marker: Optional[str] = None
        # Called (engine thread) with a freshly fetched room list so the GUI
        # can remember it: the next connect then works even when the exit's
        # HTTP endpoint (Render) is unreachable from this network.
        self.on_rooms: Optional[Callable[[str], None]] = None

    # ---- public API (GUI thread) ----
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, settings: Settings) -> None:
        if self.busy:
            return
        self._thread = threading.Thread(target=self._thread_main, args=(settings,),
                                        name="openflux-engine", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 0.0) -> None:
        loop, main = self._loop, self._main
        if loop and main and not loop.is_closed():
            def _req():
                if self._stop_evt and not self._stop_evt.is_set() and self.state == CONNECTED:
                    self._stop_evt.set()
                else:
                    main.cancel()   # still connecting: abort right away
            try:
                loop.call_soon_threadsafe(_req)
            except RuntimeError:
                pass
        if wait and self._thread:
            self._thread.join(wait)

    # ---- engine thread ----
    def _set(self, state: str, detail: str = "") -> None:
        self.state = state
        self._on_state(state, detail)

    def _thread_main(self, settings: Settings) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            self._main = loop.create_task(self._run(settings))
            loop.run_until_complete(self._main)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._loop = None
            self._main = None

    async def _run(self, s: Settings) -> None:
        log.set_debug(bool(s.debug))
        self._stop_evt = asyncio.Event()
        raw = transport = mux = server = None
        proxy_on = False
        failed = None
        try:
            self._set(CONNECTING, "Ищем врата…")
            url = await self._resolve_rooms(s)

            self._set(CONNECTING, "Разжигаем каналы…")
            context = s.transport if (s.transport in ("cupsonline", "direct") or not url) else url
            params = {"dial": url} if s.transport == "direct" else {}
            raw = make_transport(s.transport, url, default_config(), True, params)
            transport = _Counting(EncryptedTransport(BatchedTransport(raw), s.encryption_key,
                                                     context, exit_node=False))
            mux = Mux(transport, is_exit=False, on_udp=lambda *a: server and server.deliver_udp(*a))
            await transport.start()
            await mux.start()

            self._set(CONNECTING, "Открываем SOCKS5…")
            server = SOCKS5Server(s.socks_addr, mux)
            await server.start()

            self._set(CONNECTING, "Проверяем выход…")
            try:
                await self._probe(mux)
            except RuntimeError as e:
                if self._used_saved:
                    raise RuntimeError(f"{e}. Выход мог уснуть или пересоздать комнаты — "
                                       "подключитесь один раз с доступом к адресу выхода") from None
                raise

            if s.system_proxy:
                self._mark(True)
                err = await asyncio.to_thread(sysproxy.enable, "127.0.0.1", s.socks_port)
                proxy_on = True
                if err:
                    log.errorf("[GUI] системный прокси: %s", err)

            stats = Stats(since=time.time())
            self._set(CONNECTED, f"SOCKS5 {s.socks_addr}")
            while not self._stop_evt.is_set():
                stats.bytes_sent, stats.bytes_received = transport.sent, transport.received
                stats.reconnects = raw.stats().reconnects
                self._on_stats(stats)
                try:
                    await asyncio.wait_for(self._stop_evt.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        except OSError as e:
            failed = f"порт {s.socks_port} занят" if e.errno in (48, 98, 10048) else str(e)
        except Exception as e:  # noqa: BLE001
            failed = str(e) or e.__class__.__name__
        finally:
            self._set(STOPPING, "Гасим пламя…")
            if proxy_on:
                await asyncio.to_thread(sysproxy.disable)
                self._mark(False)
            for closer in (server and server.close(), mux and mux.stop(), transport and transport.stop()):
                if closer is None:
                    continue
                try:
                    await asyncio.wait_for(closer, 3)
                except BaseException:  # noqa: BLE001 - never block teardown
                    pass
            if failed:
                log.errorf("[GUI] %s", failed)
                self._set(ERROR, failed)
            else:
                self._set(OFF, "")

    def _mark(self, on: bool) -> None:
        if not self.proxy_marker:
            return
        try:
            if on:
                open(self.proxy_marker, "w").close()
            elif os.path.exists(self.proxy_marker):
                os.remove(self.proxy_marker)
        except OSError:
            pass

    def restore_proxy_if_stale(self) -> None:
        if self.proxy_marker and os.path.exists(self.proxy_marker):
            log.infof("[GUI] previous session left the system proxy on; turning it off")
            sysproxy.disable()
            self._mark(False)

    async def _resolve_rooms(self, s: Settings) -> str:
        """Room list to join. A fresh copy from the exit's /rooms wins; when
        that endpoint is unreachable (e.g. *.onrender.com is blocked) fall back
        quickly to the pinned or last-known list instead of waiting minutes."""
        self._used_saved = False
        known = s.doc_url or s.cached_rooms
        if s.transport == "direct":
            return s.doc_url
        fetched = ""
        if s.rooms_url:
            if known:
                fetched = await self._fetch_rooms(s.rooms_url, retries=1, timeout=7.0)
            else:
                fetched = await self._fetch_rooms(s.rooms_url)
        if fetched:
            if fetched != s.cached_rooms and self.on_rooms:
                self.on_rooms(fetched)
            return fetched
        if known:
            self._used_saved = bool(s.rooms_url)
            if s.rooms_url:
                log.infof("[GUI] exit endpoint unreachable; using the saved room list")
            return known
        raise RuntimeError("адрес выхода недоступен, а сохранённого списка комнат нет. "
                           "Подключитесь один раз с доступом к нему или вставьте список в настройках")

    async def _fetch_rooms(self, rooms_url: str, retries: int = 30, delay: float = 3.0,
                           timeout: float = 20.0) -> str:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for i in range(1, retries + 1):
                try:
                    async with sess.get(rooms_url) as r:
                        body = "".join((await r.text()).split())
                        if r.status == 200 and body:
                            log.infof("[GUI] room list received from exit")
                            return body
                        log.infof("[GUI] exit not ready (HTTP %d), retry %d/%d", r.status, i, retries)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.infof("[GUI] exit unreachable (%s), retry %d/%d", e.__class__.__name__, i, retries)
                if i < retries:
                    self._set(CONNECTING, f"Будим выход… {i}/{retries}")
                    await asyncio.sleep(delay)
        return ""

    async def _probe(self, mux: Mux, attempts: int = 2) -> None:
        last = None
        for _ in range(attempts):
            try:
                st = await mux.open_tcp(PROBE_HOST, PROBE_PORT, timeout=25.0)
                st.reset()
                return
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"выход не отвечает ({last or 'timeout'})")
