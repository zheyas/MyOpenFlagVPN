"""OpenFlux .conf parser — a port of conf.go (INI-like, wg-quick style).

    [Interface]
    Role = client
    Transport = cupsonline
    EncryptionKeyFile = secret.txt
    URL = ...

    [Transport "direct"]
    Priority = 100
    Dial = 127.0.0.1:8445
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ConfTransport:
    name: str
    values: Dict[str, str] = field(default_factory=dict)


@dataclass
class ConfFile:
    interface: Dict[str, str] = field(default_factory=dict)
    transports: List[ConfTransport] = field(default_factory=list)


def parse_conf(path: str) -> ConfFile:
    out = ConfFile()
    current: Optional[ConfTransport] = None
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                body = line[1:-1].strip()
                if body.lower() == "interface":
                    current = None
                    continue
                if body.lower().startswith("transport"):
                    name = body[len("transport"):].strip().strip('"')
                    current = ConfTransport(name=name)
                    out.transports.append(current)
                    continue
                raise ValueError(f"{path}:{lineno}: unknown section [{body}]")
            if "=" not in line:
                raise ValueError(f"{path}:{lineno}: expected key = value")
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            for ch in ("#", ";"):
                i = v.find(ch)
                if i >= 0:
                    v = v[:i].strip()
            if current is None:
                out.interface[k] = v
            else:
                current.values[k] = v
    return out


def conf_bool(v: str, default: bool) -> bool:
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def conf_int(v: str, default: int) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return default
