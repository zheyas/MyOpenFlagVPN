"""Transport interface and shared base, mirroring Go's transport/transport.go.

The port is asyncio-based. A Transport carries opaque byte *messages* (not a
byte stream): every ``send`` delivers one message, and every message handed to
the receive callback is exactly one message the peer sent. Delivery may be
lossy, reordered or duplicated for the document/room transports — the reliable
mux layer (``openflux.mux``) is what turns that into ordered TCP-like streams,
the same job gVisor did in the Go client.

A concrete transport implements:
    async def start(self) -> None
    async def stop(self) -> None
    async def send(self, data: bytes) -> None
    def set_receive(self, cb: Callable[[bytes], None]) -> None
    def is_connected(self) -> bool
    def stats(self) -> TransportStats
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class TransportConfig:
    max_reconnect_attempts: int = 999999
    reconnect_delay: float = 0.0          # seconds
    reconnect_multiplier: float = 1.1
    max_queue_size: int = 1024
    keepalive_interval: float = 10.0      # seconds


def default_config() -> TransportConfig:
    return TransportConfig()


@dataclass
class TransportStats:
    bytes_sent: int = 0
    bytes_received: int = 0
    packets_sent: int = 0
    packets_recv: int = 0
    reconnects: int = 0
    connected: bool = False
    uptime: float = 0.0


ReceiveCallback = Callable[[bytes], None]


class Transport(ABC):
    """Abstract transport. All methods that touch the network are async."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, data: bytes) -> None: ...

    @abstractmethod
    def set_receive(self, cb: ReceiveCallback) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    def stats(self) -> TransportStats:
        return TransportStats()


class BaseTransport(Transport):
    """Common bookkeeping: running/connected flags, stats, receive callback.

    Concrete transports subclass this and call :meth:`call_receive`,
    :meth:`record_send`, :meth:`record_receive` and :meth:`set_connected`.
    """

    def __init__(self, config: Optional[TransportConfig] = None) -> None:
        self._config = config or default_config()
        self._running = False
        self._connected = False
        self._start_time = time.monotonic()
        self._receive_cb: Optional[ReceiveCallback] = None
        self._stats = TransportStats()
        self._reconnects = 0

    # ---- lifecycle ----

    async def start(self) -> None:
        self._running = True
        self._start_time = time.monotonic()

    async def stop(self) -> None:
        self._running = False
        self._connected = False

    def is_running(self) -> bool:
        return self._running

    def is_connected(self) -> bool:
        return self._connected

    def set_connected(self, connected: bool) -> None:
        self._connected = connected

    # ---- receive ----

    def set_receive(self, cb: ReceiveCallback) -> None:
        self._receive_cb = cb

    def call_receive(self, data: bytes) -> None:
        cb = self._receive_cb
        if cb is not None:
            cb(data)

    # ---- stats ----

    def record_send(self, n: int) -> None:
        self._stats.bytes_sent += n
        self._stats.packets_sent += 1

    def record_receive(self, n: int) -> None:
        self._stats.bytes_received += n
        self._stats.packets_recv += 1

    def record_reconnect(self) -> None:
        self._reconnects += 1

    def get_config(self) -> TransportConfig:
        return self._config

    def stats(self) -> TransportStats:
        return TransportStats(
            bytes_sent=self._stats.bytes_sent,
            bytes_received=self._stats.bytes_received,
            packets_sent=self._stats.packets_sent,
            packets_recv=self._stats.packets_recv,
            reconnects=self._reconnects,
            connected=self._connected,
            uptime=time.monotonic() - self._start_time,
        )
