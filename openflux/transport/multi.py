"""Multi-transport session: several carriers at once, with failover.

A port of the idea behind the Go build's ``--transports`` sessions. Every
child transport (already wrapped in Batched + Encrypted, each with its own
encryption context) runs side by side. Traffic goes over the highest-priority
child on which the peer has been heard recently; equal priorities share the
load round-robin. A child that goes quiet is skipped until it is heard again,
so when cupsonline dies the tunnel moves to the backup on its own, and moves
back when the primary returns. Lost packets around a switch are retransmitted
by the mux, which runs above this layer.

Liveness: each side pings every child every ``PING_INTERVAL`` seconds and
answers pings with pongs. These control frames start with ``CTRL_MAGIC`` and
never reach the mux (which also ignores unknown packet types, for peers that
run a single transport).

A child that fails to start is retried in the background with backoff.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .. import logging_util as log
from .base import Transport, TransportStats

CTRL_MAGIC = b"\x00OFMT"        # mux packet type 0 is never used
CTRL_PING = CTRL_MAGIC + b"I"
CTRL_PONG = CTRL_MAGIC + b"O"
PING_INTERVAL = 10.0             # s
HEARD_WINDOW = 35.0              # s: a child counts as live if heard within this
SILENCE_LIMIT = 8.0              # s of sending with no answer at all -> dead
RETRY_MIN, RETRY_MAX = 2.0, 60.0


@dataclass
class Child:
    name: str
    priority: int
    transport: Transport
    started: bool = False
    last_heard: float = 0.0
    sent: int = 0
    received: int = 0
    errors: int = 0
    unanswered_since: float = 0.0    # first send after we last heard the peer
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def live(self, now: float) -> bool:
        if not (self.started and self.transport.is_connected()):
            return False
        if now - self.last_heard > HEARD_WINDOW:
            return False
        # A carrier that swallows everything still says "connected": treat
        # SILENCE_LIMIT seconds of sending without a single reply as dead.
        return not (self.unanswered_since and now - self.unanswered_since > SILENCE_LIMIT)

    def note_sent(self, now: float) -> None:
        if not self.unanswered_since:
            self.unanswered_since = now


class MultiTransport(Transport):
    def __init__(self, children: List[Child]) -> None:
        if not children:
            raise ValueError("multi: no transports")
        self.children = sorted(children, key=lambda c: -c.priority)
        self._cb: Optional[Callable[[bytes], None]] = None
        self._rr = 0
        self._tasks: List[asyncio.Task] = []
        self._stopping = False
        self._active: Optional[str] = None
        self._switch_listeners: List[Callable[[str, str], None]] = []
        for c in self.children:
            c.transport.set_receive(lambda data, c=c: self._on_child(c, data))

    # ---- Transport API ----
    async def start(self) -> None:
        """Start every child; return once at least one is up (or all failed
        their first attempt, in which case the first error is raised)."""
        first_up = asyncio.Event()
        errors: List[BaseException] = []

        async def run_child(c: Child) -> None:
            delay = RETRY_MIN
            while not self._stopping:
                try:
                    await c.transport.start()
                    c.started = True
                    log.infof("[MULTI] %s up (priority %d)", c.name, c.priority)
                    first_up.set()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    log.infof("[MULTI] %s failed to start: %s; retry in %.0fs", c.name, e, delay)
                    if len(errors) >= len(self.children) and not first_up.is_set():
                        first_up.set()      # let start() report; retries go on
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RETRY_MAX)

        for c in self.children:
            c.task = asyncio.ensure_future(run_child(c))
            self._tasks.append(c.task)
        await first_up.wait()
        if not any(c.started for c in self.children):
            raise RuntimeError(f"multi: no transport could start ({errors[0] if errors else 'unknown'})")
        self._tasks.append(asyncio.ensure_future(self._ping_loop()))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for c in self.children:
            if c.started:
                try:
                    await asyncio.wait_for(c.transport.stop(), 5)
                except BaseException:  # noqa: BLE001
                    pass

    def set_receive(self, cb: Callable[[bytes], None]) -> None:
        self._cb = cb

    def is_connected(self) -> bool:
        return any(c.started and c.transport.is_connected() for c in self.children)

    def stats(self) -> TransportStats:
        out = TransportStats()
        for c in self.children:
            if not c.started:
                continue
            s = c.transport.stats()
            out.bytes_sent += s.bytes_sent
            out.bytes_received += s.bytes_received
            out.packets_sent += s.packets_sent
            out.packets_recv += s.packets_recv
            out.reconnects += s.reconnects
        out.connected = self.is_connected()
        return out

    async def send(self, data: bytes) -> None:
        last: Optional[BaseException] = None
        for c in self._candidates():
            try:
                await c.transport.send(data)
                c.sent += 1
                c.note_sent(time.monotonic())
                if self._active != c.name:
                    prev, self._active = self._active, c.name
                    log.infof("[MULTI] sending via %s", c.name)
                    if prev is not None:
                        for fn in list(self._switch_listeners):
                            try:
                                fn(prev, c.name)
                            except Exception:  # noqa: BLE001
                                pass
                return
            except Exception as e:  # noqa: BLE001
                c.errors += 1
                last = e
        raise RuntimeError(f"multi: no transport could send ({last})")

    def add_switch_listener(self, fn: Callable[[str, str], None]) -> None:
        """fn(old, new) runs when traffic moves to another carrier, so the mux
        can resend what was lost on the dead one at once."""
        self._switch_listeners.append(fn)

    # ---- internals ----
    def _candidates(self) -> List[Child]:
        """Live children by priority (equal priorities rotated), then the
        merely connected ones as a last resort (e.g. before the first pong)."""
        now = time.monotonic()
        live = [c for c in self.children if c.live(now)]
        rest = [c for c in self.children
                if c not in live and c.started and c.transport.is_connected()]
        out: List[Child] = []
        for group in (live, rest):
            i = 0
            while i < len(group):
                j = i
                while j < len(group) and group[j].priority == group[i].priority:
                    j += 1
                tier = group[i:j]
                if len(tier) > 1:
                    self._rr += 1
                    k = self._rr % len(tier)
                    tier = tier[k:] + tier[:k]
                out.extend(tier)
                i = j
        return out

    def _on_child(self, c: Child, data: bytes) -> None:
        c.last_heard = time.monotonic()
        c.unanswered_since = 0.0
        c.received += 1
        if data[:len(CTRL_MAGIC)] == CTRL_MAGIC:
            if data == CTRL_PING:
                asyncio.ensure_future(self._send_ctrl(c, CTRL_PONG))
            return
        if self._cb:
            self._cb(data)

    async def _send_ctrl(self, c: Child, frame: bytes) -> None:
        try:
            await c.transport.send(frame)
            if frame == CTRL_PING:
                c.note_sent(time.monotonic())
        except Exception:  # noqa: BLE001
            c.errors += 1

    async def _ping_loop(self) -> None:
        try:
            while True:
                for c in self.children:
                    if c.started and c.transport.is_connected():
                        await self._send_ctrl(c, CTRL_PING)
                await asyncio.sleep(PING_INTERVAL)
        except asyncio.CancelledError:
            return

    def status(self) -> List[dict]:
        now = time.monotonic()
        return [{"name": c.name, "priority": c.priority, "started": c.started,
                 "live": c.live(now), "sent": c.sent, "received": c.received}
                for c in self.children]
