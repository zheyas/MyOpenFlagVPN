"""Transport factory — a port of transport_factory.go.

Builds a raw transport from a type name and params. The CLI then wraps it with
Batched + Encrypted before handing it to the mux.
"""

from __future__ import annotations

from typing import Optional

from .base import Transport, TransportConfig
from .cupsonline import CupsonlineTransport
from .direct import DirectConfig, DirectTransport
from .mailru import MailruDocsTransport
from .oneme import OneMeTransport
from .yandex_boards import BoardsTransport
from .yandex_docs import YandexDocsTransport
from .yandex_volga import YandexVolgaTransport


def make_transport(ttype: str, url: str, base_cfg: TransportConfig,
                   is_client: bool, params: Optional[dict] = None) -> Transport:
    params = params or {}
    if ttype == "yandex":
        return YandexDocsTransport(url, base_cfg)
    if ttype == "vyandex":
        return YandexVolgaTransport(url, base_cfg)
    if ttype == "boards":
        return BoardsTransport(url, base_cfg)
    if ttype == "mailru":
        return MailruDocsTransport(url, base_cfg)
    if ttype == "cupsonline":
        return CupsonlineTransport(url, base_cfg, is_client)
    if ttype == "oneme":
        token = params.get("token", "")
        uid = int(params.get("uid") or 0)
        return OneMeTransport(not is_client, token, uid, base_cfg)
    if ttype == "direct":
        cfg = DirectConfig()
        cfg.listen_addr = params.get("listen", "") or ""
        cfg.dial_addr = params.get("dial", "") or ""
        cfg.is_exit = not is_client
        return DirectTransport(base_cfg, cfg)
    raise ValueError(f"factory: unknown transport type {ttype!r}")


def transport_has_cookies(ttype: str) -> bool:
    return ttype in ("yandex", "vyandex", "boards", "mailru", "cupsonline")
