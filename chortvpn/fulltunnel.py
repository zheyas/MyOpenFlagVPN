"""Full-system tunnel on macOS: utun + tun2socks into the local SOCKS5.

The system-proxy mode only covers apps that honour the proxy setting; this
mode routes ALL IPv4 traffic of the Mac into a utun device, where tun2socks
turns it into connections to our SOCKS5 (and so into the tunnel). The
carrier's own hosts (cups.online ...) are routed around the utun, otherwise
the tunnel would carry itself.

Creating the utun and changing routes needs root, so ``up`` asks for the
administrator password once (the standard macOS prompt) and starts
``tunhelper.sh`` as root. That helper also watches the app and undoes
everything when the app asks it to (a stop file), exits or crashes.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional

HERE = Path(__file__).resolve().parent
HELPER = HERE / "tunhelper.sh"
TUN2SOCKS = HERE / "bin" / "tun2socks"


def unavailable_reason() -> Optional[str]:
    if sys.platform != "darwin":
        return "полный туннель пока есть только для macOS"
    if not TUN2SOCKS.exists():
        return f"нет {TUN2SOCKS} (скачайте tun2socks)"
    if not HELPER.exists():
        return f"нет {HELPER}"
    return None


def _free_utun(start: int = 233) -> str:
    out = subprocess.run(["ifconfig", "-l"], capture_output=True, text=True).stdout.split()
    n = start
    while f"utun{n}" in out:
        n += 1
    return f"utun{n}"


def resolve_ipv4(hosts: Iterable[str]) -> List[str]:
    ips = set()
    for h in hosts:
        if not h:
            continue
        try:
            socket.inet_aton(h)
            ips.add(h)
            continue
        except OSError:
            pass
        try:
            for info in socket.getaddrinfo(h, 443, socket.AF_INET, socket.SOCK_STREAM):
                ips.add(info[4][0])
        except OSError:
            pass
    return sorted(ips)


def _applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class FullTunnel:
    def __init__(self, state_dir: str) -> None:
        self.state = Path(state_dir)

    @property
    def is_up(self) -> bool:
        return (self.state / "up").exists()

    async def up(self, socks_port: int, bypass_hosts: Iterable[str], timeout: float = 25.0) -> List[str]:
        reason = unavailable_reason()
        if reason:
            raise RuntimeError(reason)
        self.state.mkdir(parents=True, exist_ok=True)
        ips = await asyncio.to_thread(resolve_ipv4, list(bypass_hosts))
        if not ips:
            raise RuntimeError("не удалось определить адреса транспорта для обхода туннеля")
        dev = await asyncio.to_thread(_free_utun)
        args = [str(HELPER), str(TUN2SOCKS), str(int(socks_port)), str(os.getpid()),
                str(self.state), dev, *ips]
        shell = "nohup /bin/sh " + " ".join(shlex.quote(a) for a in args) + " >/dev/null 2>&1 &"
        script = (f"do shell script {_applescript_quote(shell)} with administrator privileges "
                  f"with prompt {_applescript_quote('ChortVPN включает полный туннель: весь трафик Mac пойдёт через защищённый канал.')}")
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip()
            if "-128" in msg:
                raise RuntimeError("полный туннель отменён: пароль администратора не введён")
            raise RuntimeError(f"не удалось запустить полный туннель: {msg or proc.returncode}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if (self.state / "up").exists():
                return ips
            failed = self.state / "failed"
            if failed.exists():
                raise RuntimeError(f"полный туннель: {failed.read_text().strip()}")
            await asyncio.sleep(0.2)
        raise RuntimeError("полный туннель не поднялся (см. helper.log)")

    async def down(self, timeout: float = 10.0) -> None:
        if not self.is_up:
            return
        try:
            (self.state / "stop").touch()
        except OSError:
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.is_up and loop.time() < deadline:
            await asyncio.sleep(0.2)
