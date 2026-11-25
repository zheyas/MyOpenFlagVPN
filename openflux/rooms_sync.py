"""Keeping clients' cupsonline room lists current without reaching the exit.

cups.online deletes rooms after a while, so the exit's room list changes,
and a client behind a whitelist cannot ask the exit (Render is blocked) for
the new one. Three layers:

1. In-band: the exit announces its current list over the tunnel every
   ANNOUNCE_INTERVAL (frame ``ROOMS_MAGIC + packed list``); the client saves
   it. It also rotates to fresh rooms before cups.online deletes the old
   ones, keeping both for an overlap, so a client that connects every few
   days always holds a working list.
2. Mailbox: the exit stores the list, encrypted with the shared secret, in a
   public file on Yandex Disk (whitelisted). A client whose saved rooms are
   all gone downloads it by its public link. The link alone reveals nothing.
3. Manual: the room list pasted in the app / ``--url``.

The mailbox needs a Yandex OAuth token on the exit (``YADISK_TOKEN``, scopes
cloud_api:disk.read + cloud_api:disk.write); clients only need the public URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import logging_util as log

ROOMS_MAGIC = b"\x00OFRL"          # never a mux type (0) nor IPv4 (0x4_)
ANNOUNCE_INTERVAL = 30.0
MAILBOX_PATH = "disk:/chortvpn-rooms.bin"
MAILBOX_MAGIC = b"OFRM1"
API = "https://cloud-api.yandex.net/v1/disk"


# ---- in-band announcement -------------------------------------------------

def announce_frame(packed_rooms: str) -> bytes:
    return ROOMS_MAGIC + packed_rooms.encode()


def parse_announce(msg: bytes) -> Optional[str]:
    if msg[:len(ROOMS_MAGIC)] != ROOMS_MAGIC:
        return None
    try:
        return msg[len(ROOMS_MAGIC):].decode("ascii").strip() or None
    except UnicodeDecodeError:
        return None


# ---- mailbox crypto -------------------------------------------------------

def _mailbox_key(secret: str) -> bytes:
    salt = hashlib.sha256(b"OpenFlux rooms mailbox v1").digest()
    return hashlib.scrypt(secret.encode(), salt=salt, n=16384, r=8, p=1, dklen=32,
                          maxmem=64 * 1024 * 1024)


def seal(secret: str, packed_rooms: str) -> bytes:
    nonce = os.urandom(12)
    body = json.dumps({"rooms": packed_rooms, "ts": int(time.time())}).encode()
    return MAILBOX_MAGIC + nonce + AESGCM(_mailbox_key(secret)).encrypt(nonce, body, MAILBOX_MAGIC)


def unseal(secret: str, blob: bytes) -> dict:
    if blob[:len(MAILBOX_MAGIC)] != MAILBOX_MAGIC:
        raise ValueError("not a ChortVPN rooms mailbox")
    nonce = blob[len(MAILBOX_MAGIC):len(MAILBOX_MAGIC) + 12]
    ct = blob[len(MAILBOX_MAGIC) + 12:]
    body = AESGCM(_mailbox_key(secret)).decrypt(nonce, ct, MAILBOX_MAGIC)
    return json.loads(body)


# ---- Yandex Disk ----------------------------------------------------------

async def _json(sess, method: str, url: str, **kw) -> dict:
    async with sess.request(method, url, **kw) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"Yandex Disk {method} {url.split('?')[0]}: HTTP {r.status} {text[:200]}")
        return json.loads(text) if text else {}


async def publish(token: str, blob: bytes, path: str = MAILBOX_PATH) -> str:
    """Upload ``blob`` to the owner's disk, publish it, return its public URL."""
    import aiohttp
    hdr = {"Authorization": f"OAuth {token}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        up = await _json(s, "GET", f"{API}/resources/upload", params={"path": path, "overwrite": "true"}, headers=hdr)
        async with s.put(up["href"], data=blob) as r:
            if r.status >= 400:
                raise RuntimeError(f"Yandex Disk upload: HTTP {r.status}")
        await _json(s, "PUT", f"{API}/resources/publish", params={"path": path}, headers=hdr)
        meta = await _json(s, "GET", f"{API}/resources", params={"path": path, "fields": "public_url"}, headers=hdr)
        return meta.get("public_url", "")


async def read_own(token: str, path: str = MAILBOX_PATH) -> Optional[bytes]:
    """The exit reads back its own mailbox (e.g. after a restart)."""
    import aiohttp
    hdr = {"Authorization": f"OAuth {token}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        try:
            d = await _json(s, "GET", f"{API}/resources/download", params={"path": path}, headers=hdr)
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise
        async with s.get(d["href"]) as r:
            return await r.read() if r.status < 400 else None


async def fetch_public(public_url: str) -> bytes:
    """A client downloads the mailbox by its public link (no account)."""
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
        d = await _json(s, "GET", f"{API}/public/resources/download", params={"public_key": public_url})
        async with s.get(d["href"]) as r:
            if r.status >= 400:
                raise RuntimeError(f"mailbox download: HTTP {r.status}")
            return await r.read()


async def fetch_rooms(public_url: str, secret: str) -> str:
    data = unseal(secret, await fetch_public(public_url))
    log.infof("[ROOMS] mailbox list from %s", time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("ts", 0))))
    return data["rooms"]
