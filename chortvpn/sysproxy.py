"""Point the operating system's SOCKS proxy at the local tunnel and back.

macOS: ``networksetup`` on every enabled network service.
Windows: WinINet registry (``socks=host:port``) + settings-changed broadcast.
Linux (GNOME): ``gsettings``.
All functions are best-effort and return a human-readable error or None.
"""

from __future__ import annotations

import subprocess
import sys
from typing import List, Optional


def _run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def _mac_services() -> List[str]:
    out = _run("networksetup", "-listallnetworkservices").stdout.splitlines()
    # First line is an explanatory note; '*' marks disabled services.
    return [s for s in out[1:] if s.strip() and not s.startswith("*")]


def enable(host: str, port: int) -> Optional[str]:
    try:
        if sys.platform == "darwin":
            errs = []
            for svc in _mac_services():
                r1 = _run("networksetup", "-setsocksfirewallproxy", svc, host, str(port))
                r2 = _run("networksetup", "-setsocksfirewallproxystate", svc, "on")
                if r1.returncode or r2.returncode:
                    errs.append(f"{svc}: {(r1.stderr or r2.stderr or r1.stdout).strip()}")
            return "; ".join(errs) or None
        if sys.platform.startswith("win"):
            return _win_set(f"socks={host}:{port}", True)
        if sys.platform.startswith("linux"):
            _run("gsettings", "set", "org.gnome.system.proxy.socks", "host", host)
            _run("gsettings", "set", "org.gnome.system.proxy.socks", "port", str(port))
            r = _run("gsettings", "set", "org.gnome.system.proxy", "mode", "manual")
            return r.stderr.strip() or None
        return f"системный прокси не поддерживается на {sys.platform}"
    except Exception as e:  # noqa: BLE001 - surface anything to the UI
        return str(e)


def disable() -> Optional[str]:
    try:
        if sys.platform == "darwin":
            for svc in _mac_services():
                _run("networksetup", "-setsocksfirewallproxystate", svc, "off")
            return None
        if sys.platform.startswith("win"):
            return _win_set("", False)
        if sys.platform.startswith("linux"):
            r = _run("gsettings", "set", "org.gnome.system.proxy", "mode", "none")
            return r.stderr.strip() or None
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


def _win_set(server: str, on: bool) -> Optional[str]:
    import ctypes
    import winreg  # type: ignore[import-not-found]

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                         0, winreg.KEY_SET_VALUE)
    with key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if on else 0)
        if on:
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
    inet = ctypes.windll.wininet  # type: ignore[attr-defined]
    inet.InternetSetOptionW(0, 39, 0, 0)   # INTERNET_OPTION_SETTINGS_CHANGED
    inet.InternetSetOptionW(0, 37, 0, 0)   # INTERNET_OPTION_REFRESH
    return None
