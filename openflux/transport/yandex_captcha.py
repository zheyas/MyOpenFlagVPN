"""Yandex proof-of-work captcha solver — a port of transport/yandex/captcha.go.

Handles the first-tier ``showcaptchafast`` PoW challenge (SmartCaptcha, the
second tier, cannot be solved here and is surfaced as ErrCaptchaRequired by the
callers). Uses an aiohttp session so cookies accumulate in its shared jar.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import random
import re
import time
from typing import Optional, Tuple

import aiohttp

from .. import logging_util as log

_re_ssr = re.compile(r'window\.__SSR_DATA__\s*=\s*JSON\.parse\(atob\("([^"]+)"\)\)')
_re_form = re.compile(r'<form[^>]*id="tmgrdfrend-form"[^>]*action="([^"]+)"')

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) Gecko/20100101 Firefox/153.0"


def _browser_headers(ua: str) -> dict:
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-GPC": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    }


def _check_complexity(h: bytes, complexity: int) -> bool:
    if complexity < 0 or complexity > 8 * len(h):
        return False
    e = 0
    o = 0
    while e <= complexity - 8:
        if h[o] != 0:
            return False
        e += 8
        o += 1
    mask = (255 << (8 + e - complexity)) & 0xFF
    return (h[o] & mask) == 0


def _solve_pow(prefix_hex: str, complexity: int) -> Tuple[str, int]:
    try:
        prefix = bytes.fromhex(prefix_hex)
        if not prefix:
            prefix = prefix_hex.encode()
    except ValueError:
        prefix = prefix_hex.encode()
    for attempts in range(1, 10_000_000):
        ts = int(time.time() * 1000)
        nonce = ts.to_bytes(8, "little") + random.getrandbits(63).to_bytes(8, "little")
        h = hashlib.sha256(nonce + prefix).digest()
        if _check_complexity(h, complexity):
            return nonce.hex(), attempts
    return "", 0


def _build_fingerprint(nonce_hex: str, ua: str) -> dict:
    return {
        "b6": 8, "b7": 8, "b9": ["en-US", "en"],
        "c2": "", "c4": "MacIntel", "c5": [], "c9": ua,
        "f4": 1080, "f5": 1920, "f6": 24, "f7": 1080, "f8": True,
        "f9": [1920, 1080], "g1": 1920,
        "g2": "Europe/Moscow", "g3": -180,
        "j5": True,
        "m2": {"mTP": 0, "tE": False, "tS": False},
        "n6": False,
        "o2": 0, "o3": "srgb", "o4": 0, "o5": "en-US",
        "o8": None, "o9": None,
        "p1": None, "p2": 0, "p3": None, "p4": None,
        "p5": None, "p6": None, "p8": [], "p9": "111111111",
        "j6": 48000,
        "a1": "",
        "a2": {"w": False, "d": ""},
        "a3": {
            "acos": 1.4444399284962483, "asin": 0.12349655394506357,
            "atan": 0.4636476090008061, "cos": -0.8390715290095377,
            "exp": 2.718281828459045, "log1p": 2.3978952727983707,
            "sin": -0.9917788534431158, "tan": -0.23206847684369653,
        },
        "a4": {"minDelta": 0.1, "maxDelta": 1.2},
        "a5": None,
        "k4": [],
        "j1": {"vn": "WebKit", "vr": "WebKit WebGL", "vU": "",
               "r": "Mozilla", "rU": "", "sLV": "WebGL GLSL ES 1.0 (1.0)"},
        "j2": {"cA": [], "p": [], "sP": [], "e": [], "eP": []},
        "m10": nonce_hex,
        "version": "1.5.0",
    }


def _encode_fingerprint(fp: dict) -> str:
    raw = json.dumps(fp, separators=(",", ":")).encode()
    return "~" + base64.b64encode(gzip.compress(raw)).decode() + "~"


async def solve_captcha(session: aiohttp.ClientSession, doc_url: str,
                        user_agent: str = DEFAULT_UA) -> Optional[str]:
    """Solves the PoW captcha for doc_url. Returns retpath, or None when no
    captcha was required. Cookies update in the session's jar in place."""
    log.debugf("[CAPTCHA] solve start: url=%s", doc_url)
    captcha_url = ""
    current = doc_url
    for _ in range(10):
        async with session.get(current, headers=_browser_headers(user_agent),
                                allow_redirects=False) as resp:
            await resp.read()
            status = resp.status
            loc = resp.headers.get("Location", "")
        if status == 200:
            return None
        if status < 300 or status >= 400:
            raise RuntimeError(f"captcha unexpected status {status}")
        if not loc:
            raise RuntimeError("captcha: redirect without Location")
        if "showcaptchafast" in loc:
            captcha_url = loc
            break
        current = loc
    if not captcha_url:
        raise RuntimeError("captcha: showcaptchafast not found in redirect chain")

    async with session.get(captcha_url, headers=_browser_headers(user_agent),
                           allow_redirects=False) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"captcha showcaptcha status {resp.status}")

    m = _re_ssr.search(body)
    if not m:
        raise RuntimeError("captcha: __SSR_DATA__ not found")
    ssr = json.loads(base64.b64decode(m.group(1)))
    m2 = _re_form.search(body)
    if not m2:
        raise RuntimeError("captcha: form action not found")
    form_action = m2.group(1).replace("&amp;", "&")
    if form_action.startswith("/"):
        form_action = "https://docs.yandex.ru" + form_action

    pow_cfg = ssr.get("pow", {})
    nonce_hex, attempts = _solve_pow(pow_cfg.get("prefix", ""), pow_cfg.get("complexity", 0))
    log.debugf("[CAPTCHA] PoW solved: attempts=%d", attempts)

    fp = _encode_fingerprint(_build_fingerprint(nonce_hex, user_agent))
    form = {
        "version": "1.5.0",
        "uniquekey": ssr.get("uniqueKey", ""),
        "chstate": "ok",
        "fingerprint": fp,
    }
    headers = _browser_headers(user_agent)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Origin"] = "https://docs.yandex.ru"
    headers["Referer"] = captcha_url
    async with session.post(form_action, data=form, headers=headers,
                            allow_redirects=False) as resp:
        await resp.read()
        if resp.status < 300 or resp.status >= 400:
            raise RuntimeError(f"captcha POST unexpected status {resp.status}")
        retpath = resp.headers.get("Location", "") or doc_url
    log.debugf("[CAPTCHA] solve OK")
    return retpath
