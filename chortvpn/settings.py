"""Client settings: a small JSON file in the per-user data dir.

On first launch defaults are seeded from a ``.env`` next to the app (the same
file ``run-client.sh`` reads), so a developer checkout works out of the box.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

# Registration is built but switched off until the production launch.
# Set CHORTVPN_REGISTRATION=1 to preview the (still inactive) form.
REGISTRATION_ENABLED = False


def registration_visible() -> bool:
    return REGISTRATION_ENABLED or os.getenv("CHORTVPN_REGISTRATION") == "1"


@dataclass
class Settings:
    transport: str = "cupsonline"
    rooms_url: str = ""          # exit's /rooms endpoint (preferred)
    doc_url: str = ""            # pinned room list / document URL (fallback)
    encryption_key: str = ""
    socks_port: int = 1080
    system_proxy: bool = True    # point the OS SOCKS proxy at us while connected
    debug: bool = False

    @property
    def socks_addr(self) -> str:
        return f"127.0.0.1:{self.socks_port}"

    def problems(self) -> Optional[str]:
        if not self.encryption_key:
            return "Укажите ключ шифрования в настройках"
        if not (self.rooms_url or self.doc_url) and self.transport != "direct":
            return "Укажите адрес выхода (ROOMS_URL) в настройках"
        if not (1 <= int(self.socks_port) <= 65535):
            return "Некорректный порт SOCKS5"
        return None


def _read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _from_env(env: dict) -> Settings:
    s = Settings()
    s.transport = env.get("TRANSPORT") or s.transport
    s.rooms_url = env.get("ROOMS_URL", "")
    s.doc_url = env.get("DOC_URL") or env.get("URL", "")
    s.encryption_key = env.get("ENCRYPTION_KEY", "")
    listen = env.get("SOCKS5_LISTEN", "")
    if listen.rpartition(":")[2].isdigit():
        s.socks_port = int(listen.rpartition(":")[2])
    s.debug = env.get("DEBUG") == "1"
    return s


class SettingsStore:
    def __init__(self, data_dir: str) -> None:
        self.path = Path(data_dir) / "settings.json"

    def load(self) -> Settings:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                known = {f.name for f in fields(Settings)}
                return Settings(**{k: v for k, v in raw.items() if k in known})
            except (OSError, ValueError, TypeError):
                pass
        for cand in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
            if cand.exists():
                return _from_env(_read_env(cand))
        return Settings()

    def save(self, s: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)   # holds the shared secret
        except OSError:
            pass
        tmp.replace(self.path)
