"""Logging helpers, mirroring Go's utils/logging.go.

Debugf is a no-op unless debug logging is enabled (``--debug``); Infof always
prints. The format matches the Go project loosely: a timestamp, a level and the
message. Everything goes to stderr so stdout stays clean for machine-readable
output (e.g. the cupsonline room list the exit prints).
"""

from __future__ import annotations

import sys
import threading
import time

_verbose = False
_lock = threading.Lock()
_sinks: list = []


def add_sink(fn) -> None:
    """Register ``fn(level, message)`` to also receive every emitted line
    (used by the desktop GUI to show a live log)."""
    _sinks.append(fn)


def remove_sink(fn) -> None:
    if fn in _sinks:
        _sinks.remove(fn)


def _emit(level: str, msg: str) -> None:
    for fn in list(_sinks):
        try:
            fn(level, msg)
        except Exception:
            pass


def enable_debug() -> None:
    global _verbose
    _verbose = True


def set_debug(on: bool) -> None:
    global _verbose
    _verbose = on


def is_verbose() -> bool:
    return _verbose


def _stamp() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


def debugf(fmt: str, *args: object) -> None:
    if not _verbose:
        return
    msg = fmt % args if args else fmt
    with _lock:
        print(f"{_stamp()} [DBG] {msg}", file=sys.stderr, flush=True)
    _emit("DBG", msg)


def infof(fmt: str, *args: object) -> None:
    msg = fmt % args if args else fmt
    with _lock:
        print(f"{_stamp()} [INF] {msg}", file=sys.stderr, flush=True)
    _emit("INF", msg)


def errorf(fmt: str, *args: object) -> None:
    msg = fmt % args if args else fmt
    with _lock:
        print(f"{_stamp()} [ERR] {msg}", file=sys.stderr, flush=True)
    _emit("ERR", msg)
