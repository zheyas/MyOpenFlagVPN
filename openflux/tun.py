"""TUN devices for L3 mode: Linux /dev/net/tun and macOS utun, plus the
interface / route setup around them. Everything here needs root.

A device yields and accepts bare IPv4 packets; the macOS 4-byte protocol
header is added and stripped here.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import socket
import struct
import subprocess
import sys
from typing import Callable, List, Optional

from . import logging_util as log

MTU = 1400


def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        if check:
            raise RuntimeError(f"{cmd[0]} not found (install iproute2 / iptables)")
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
    return r


class TunDevice:
    def __init__(self, fd: int, name: str, af_header: bool) -> None:
        self.fd = fd
        self.name = name
        self._af = af_header           # macOS utun: 4-byte address family prefix
        os.set_blocking(fd, False)

    @classmethod
    def open(cls, name: str = "") -> "TunDevice":
        if sys.platform.startswith("linux"):
            return cls._open_linux(name or "ofx0")
        if sys.platform == "darwin":
            return cls._open_utun()
        raise RuntimeError(f"L3 mode: no TUN support on {sys.platform}")

    @classmethod
    def _open_linux(cls, name: str) -> "TunDevice":
        TUNSETIFF, IFF_TUN, IFF_NO_PI = 0x400454CA, 0x0001, 0x1000
        fd = os.open("/dev/net/tun", os.O_RDWR)
        ifr = struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI)
        res = fcntl.ioctl(fd, TUNSETIFF, ifr)
        real = res[:16].rstrip(b"\0").decode()
        return cls(fd, real, af_header=False)

    @classmethod
    def _open_utun(cls) -> "TunDevice":
        PF_SYSTEM, SYSPROTO_CONTROL, UTUN_OPT_IFNAME = 32, 2, 2
        CTLIOCGINFO = 0xC0644E03
        s = socket.socket(PF_SYSTEM, socket.SOCK_DGRAM, SYSPROTO_CONTROL)
        info = struct.pack("I96s", 0, b"com.apple.net.utun_control")
        ctl_id = struct.unpack("I96s", fcntl.ioctl(s.fileno(), CTLIOCGINFO, info))[0]
        s.connect((ctl_id, 0))          # unit 0: the kernel picks a free utunN
        name = s.getsockopt(SYSPROTO_CONTROL, UTUN_OPT_IFNAME, 16).rstrip(b"\0").decode()
        fd = s.detach()
        return cls(fd, name, af_header=True)

    def read_loop(self, on_packet: Callable[[bytes], None]) -> None:
        loop = asyncio.get_running_loop()

        def readable() -> None:
            for _ in range(64):
                try:
                    data = os.read(self.fd, 65536)
                except BlockingIOError:
                    return
                except OSError as e:
                    log.errorf("[TUN] read: %s", e)
                    loop.remove_reader(self.fd)
                    return
                if self._af:
                    data = data[4:]
                if data and data[0] >> 4 == 4:
                    on_packet(data)

        loop.add_reader(self.fd, readable)

    def write(self, pkt: bytes) -> None:
        try:
            os.write(self.fd, (struct.pack(">I", socket.AF_INET) + pkt) if self._af else pkt)
        except (BlockingIOError, OSError) as e:
            log.debugf("[TUN] write dropped: %s", e)

    def close(self) -> None:
        try:
            asyncio.get_running_loop().remove_reader(self.fd)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---- addressing / routes -------------------------------------------------

EXIT_ADDR, CLIENT_ADDR, NET = "10.66.0.1", "10.66.0.2", "10.66.0.0/24"


def _enable_forwarding() -> None:
    path = "/proc/sys/net/ipv4/ip_forward"
    try:
        with open(path) as f:
            if f.read().strip() == "1":
                return
        with open(path, "w") as f:
            f.write("1")
    except OSError as e:
        # In a container /proc/sys is read-only: start it with
        # --sysctl net.ipv4.ip_forward=1 instead.
        log.errorf("[L3] cannot enable IPv4 forwarding (%s); in Docker run with "
                   "--sysctl net.ipv4.ip_forward=1", e)


def setup_exit(dev: TunDevice) -> List[List[str]]:
    """Linux exit: address, forwarding, NAT. Returns undo commands."""
    _run("ip", "addr", "add", f"{EXIT_ADDR}/24", "dev", dev.name)
    _run("ip", "link", "set", "dev", dev.name, "mtu", str(MTU), "up")
    _enable_forwarding()
    nat = ["-t", "nat", "POSTROUTING", "-s", NET, "!", "-o", dev.name, "-j", "MASQUERADE"]
    _run("iptables", "-t", "nat", "-A", *nat[2:])
    # Clamp TCP MSS so full-size segments fit the tunnel MTU.
    mss = ["FORWARD", "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--clamp-mss-to-pmtu"]
    _run("iptables", "-t", "mangle", "-A", *mss, check=False)
    return [["iptables", "-t", "nat", "-D", *nat[2:]], ["iptables", "-t", "mangle", "-D", *mss]]


def _default_route() -> tuple:
    if sys.platform == "darwin":
        out = _run("route", "-n", "get", "default").stdout
        gw = next((l.split()[-1] for l in out.splitlines() if "gateway:" in l), "")
        dev = next((l.split()[-1] for l in out.splitlines() if "interface:" in l), "")
        return gw, dev
    out = _run("ip", "route", "show", "default").stdout.split()
    gw = out[out.index("via") + 1] if "via" in out else ""
    dev = out[out.index("dev") + 1] if "dev" in out else ""
    return gw, dev


def setup_client(dev: TunDevice, bypass_ips: List[str]) -> List[List[str]]:
    """Client: address the device, route the carrier's own hosts around it
    and everything else (0/1 + 128/1) into it. Returns undo commands."""
    gw, gw_dev = _default_route()
    undo: List[List[str]] = []
    if sys.platform == "darwin":
        _run("ifconfig", dev.name, CLIENT_ADDR, EXIT_ADDR, "mtu", str(MTU), "up")
        via = [gw] if gw else ["-interface", gw_dev]
        for ip in bypass_ips:
            _run("route", "-q", "-n", "add", "-host", ip, *via, check=False)
            undo.append(["route", "-q", "-n", "delete", "-host", ip])
        for net in ("0.0.0.0/1", "128.0.0.0/1"):
            _run("route", "-q", "-n", "add", "-net", net, EXIT_ADDR)
            undo.append(["route", "-q", "-n", "delete", "-net", net, EXIT_ADDR])
    else:
        _run("ip", "addr", "add", f"{CLIENT_ADDR}/24", "dev", dev.name)
        _run("ip", "link", "set", "dev", dev.name, "mtu", str(MTU), "up")
        via = ["via", gw] if gw else []
        for ip in bypass_ips:
            _run("ip", "route", "add", f"{ip}/32", *via, "dev", gw_dev, check=False)
            undo.append(["ip", "route", "del", f"{ip}/32"])
        for net in ("0.0.0.0/1", "128.0.0.0/1"):
            _run("ip", "route", "add", net, "dev", dev.name)
            undo.append(["ip", "route", "del", net, "dev", dev.name])
    return undo


def undo_all(cmds: List[List[str]]) -> None:
    for c in reversed(cmds):
        _run(*c, check=False)
