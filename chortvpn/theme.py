"""Palette and fonts (mirrors the HTML concept's CSS variables)."""

from __future__ import annotations

import os
from pathlib import Path

from kivy.core.text import LabelBase
from kivy.resources import resource_find


class C:
    coal = "#16110f"
    coal_content = "#17110f"
    side = "#211513"
    surface = "#2b1b17"
    ember = "#ff713f"
    gold = "#ffd18a"
    cream = "#f9eddf"
    muted = "#c3aa9d"
    border = "#754334"


def hexa(h: str, a: float = 1.0):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    if len(h) == 8:
        a = int(h[6:8], 16) / 255.0
    return [r, g, b, a]


ASSETS = Path(__file__).resolve().parent / "assets" / "fonts"


def _first(*cands):
    for c in cands:
        if c and os.path.exists(c):
            return str(c)
    return None


def register_fonts() -> dict:
    """Use Eczar / DM Sans if dropped into chortvpn/assets/fonts, else good
    system fallbacks (all with Cyrillic). Returns which brand glyph font to use."""
    roboto = resource_find("data/fonts/Roboto-Regular.ttf")
    roboto_b = resource_find("data/fonts/Roboto-Bold.ttf")
    sup = "/System/Library/Fonts/Supplemental"
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

    display = _first(ASSETS / "Eczar-Bold.ttf", ASSETS / "Eczar-SemiBold.ttf",
                     f"{sup}/Georgia Bold.ttf", os.path.join(win, "georgiab.ttf"),
                     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", roboto_b)
    LabelBase.register("Display", display)

    body = _first(ASSETS / "DMSans-Regular.ttf", roboto)
    body_b = _first(ASSETS / "DMSans-Bold.ttf", ASSETS / "DMSans-SemiBold.ttf", roboto_b)
    LabelBase.register("Body", body, fn_bold=body_b)
    LabelBase.register("BodyBold", body_b)

    symbols = _first(f"{sup}/Apple Symbols.ttf", "/System/Library/Fonts/Apple Symbols.ttf",
                     os.path.join(win, "seguisym.ttf"),
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if symbols:
        LabelBase.register("Symbols", symbols)
    return {"brand_glyph": "\u265c" if symbols else "Ч",
            "brand_font": "Symbols" if symbols else "Display"}
