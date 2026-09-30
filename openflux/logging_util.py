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


def infof(fmt: str, *args: object) -> None:
    msg = fmt % args if args else fmt
    with _lock:
        print(f"{_stamp()} [INF] {msg}", file=sys.stderr, flush=True)


def errorf(fmt: str, *args: object) -> None:
    msg = fmt % args if args else fmt
    with _lock:
        print(f"{_stamp()} [ERR] {msg}", file=sys.stderr, flush=True)
