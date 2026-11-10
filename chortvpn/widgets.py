"""Custom animated widgets: ember background, the power button, nav icons,
toggle switch and text field."""

from __future__ import annotations

import math
import random

from kivy.animation import Animation
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import (Color, Ellipse, Line, PopMatrix, PushMatrix, Rectangle,
                           Rotate, RoundedRectangle, Scale)
from kivy.graphics.texture import Texture
from kivy.metrics import dp
from kivy.properties import (BooleanProperty, ColorProperty, ListProperty,
                             NumericProperty, OptionProperty, StringProperty)
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.widget import Widget

from .theme import C, hexa

_glow_cache: dict = {}


def glow_texture(size: int = 128, falloff: float = 2.2) -> Texture:
    """White radial gradient with alpha falloff; tint it with a Color."""
    key = (size, falloff)
    if key in _glow_cache:
        return _glow_cache[key]
    buf = bytearray(size * size * 4)
    c = (size - 1) / 2.0
    i = 0
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c) / c
            a = max(0.0, 1.0 - d) ** falloff
            buf[i:i + 4] = b"\xff\xff\xff" + bytes([int(a * 255)])
            i += 4
    tex = Texture.create(size=(size, size), colorfmt="rgba")
    tex.blit_buffer(bytes(buf), colorfmt="rgba", bufferfmt="ubyte")
    _glow_cache[key] = tex
    return tex


class HoverMixin:
    hovered = BooleanProperty(False)

    def __init__(self, **kw):
        super().__init__(**kw)
        Window.bind(mouse_pos=self._on_mouse)

    def _on_mouse(self, _w, pos):
        if not self.get_root_window():
            return
        inside = self.collide_point(*self.to_widget(*pos))
        if inside != self.hovered:
            self.hovered = inside


# --------------------------------------------------------------- backgrounds
class ContentBackdrop(Widget):
    """Warm radial glow + a faint dot grid, slowly breathing."""
    breath = NumericProperty(0.0)
    intensity = NumericProperty(0.0)   # 0 off .. 1 connected

    def __init__(self, **kw):
        super().__init__(**kw)
        tex = glow_texture(128, 1.6)
        with self.canvas:
            Color(*hexa(C.coal_content))
            self._bg = Rectangle()
            self._c1 = Color(*hexa("#57271c", 0.9))
            self._g1 = Rectangle(texture=tex)
            self._c2 = Color(*hexa("#763322", 0.55))
            self._g2 = Rectangle(texture=tex)

        self.bind(pos=self._layout, size=self._layout, breath=self._layout, intensity=self._layout)
        anim = Animation(breath=1, d=4.5, t="in_out_sine") + Animation(breath=0, d=4.5, t="in_out_sine")
        anim.repeat = True
        anim.start(self)

    def _layout(self, *_):
        x, y, w, h = *self.pos, *self.size
        self._bg.pos, self._bg.size = self.pos, self.size
        r = max(w, h) * (0.62 + 0.04 * self.breath + 0.08 * self.intensity)
        cx, cy = x + w * 0.55, y + h * 0.48
        self._g1.pos, self._g1.size = (cx - r, cy - r), (2 * r, 2 * r)
        self._c1.a = 0.75 + 0.25 * self.intensity
        r2 = max(w, h) * 0.45
        self._g2.pos, self._g2.size = (x + w * 0.85 - r2, y + h * 0.92 - r2), (2 * r2, 2 * r2)
        # Dot grid fades in towards the bottom-right, like the CSS mask.
        key = (x, y, w, h)
        if key == getattr(self, "_grid_key", None):
            return
        self._grid_key = key
        self.canvas.after.clear()
        with self.canvas.after:
            sx, sy = dp(37), dp(43)
            gx = 0.0
            while gx < w:
                gy = 0.0
                while gy < h:
                    t = (gx / max(w, 1)) * 0.6 + (1 - gy / max(h, 1)) * 0.4
                    if t > 0.35:
                        Color(*hexa("#ffb477", 0.22 * min(1, (t - 0.35) / 0.5)))
                        Ellipse(pos=(x + gx, y + gy), size=(dp(1.6), dp(1.6)))
                    gy += sy
                gx += sx


class EmberField(Widget):
    """Rising sparks. ``heat`` scales their count, speed and brightness."""
    heat = NumericProperty(0.25)

    def __init__(self, count: int = 46, **kw):
        super().__init__(**kw)
        self._tex = glow_texture(64, 2.4)
        self._parts = []
        with self.canvas:
            for _ in range(count):
                col = Color(1, 0.6, 0.3, 0)
                rect = Rectangle(texture=self._tex, size=(0, 0))
                self._parts.append({"c": col, "r": rect, "x": 0.0, "y": 0.0,
                                    "life": 0.0, "max": 1.0, "vx": 0.0, "vy": 0.0,
                                    "s": 0.0, "ph": 0.0, "gold": False})
        for p in self._parts:
            self._respawn(p, initial=True)
        Clock.schedule_interval(self._tick, 1 / 60)

    def _respawn(self, p, initial=False):
        w, h = self.width or 400, self.height or 400
        ang = random.uniform(0, 2 * math.pi)
        rad = random.uniform(0.15, 0.48) * min(w, h)
        p["x"] = w / 2 + math.cos(ang) * rad
        p["y"] = h / 2 + math.sin(ang) * rad * 0.8 - h * 0.08
        p["max"] = random.uniform(2.5, 6.0)
        p["life"] = random.uniform(0, p["max"]) if initial else 0.0
        p["vx"] = random.uniform(-6, 6)
        p["vy"] = random.uniform(14, 40)
        p["s"] = random.uniform(dp(5), dp(13))
        p["ph"] = random.uniform(0, 6.28)
        p["gold"] = random.random() < 0.35

    def _tick(self, dt):
        heat = self.heat
        speed = 0.5 + heat * 1.6
        active = int(len(self._parts) * (0.35 + 0.65 * heat))
        for i, p in enumerate(self._parts):
            p["life"] += dt * speed
            if p["life"] >= p["max"]:
                self._respawn(p)
            t = p["life"] / p["max"]
            p["x"] += (p["vx"] + math.sin(p["life"] * 2 + p["ph"]) * 8) * dt * speed
            p["y"] += p["vy"] * dt * speed
            fade = math.sin(math.pi * t)
            flick = 0.75 + 0.25 * math.sin(p["life"] * 13 + p["ph"])
            a = fade * flick * (0.35 + 0.65 * heat) * (1 if i < active else 0)
            c = p["c"]
            if p["gold"]:
                c.rgba = (1.0, 0.82, 0.54, a)
            else:
                c.rgba = (1.0, 0.44, 0.25, a)
            s = p["s"] * (1 - 0.4 * t)
            p["r"].pos = (self.x + p["x"] - s / 2, self.y + p["y"] - s / 2)
            p["r"].size = (s, s)


# --------------------------------------------------------------- power button
class PowerButton(HoverMixin, ButtonBehavior, Widget):
    status = OptionProperty("off", options=["off", "connecting", "connected", "stopping", "error"])
    fill = ColorProperty(hexa(C.ember))
    fill_dark = ColorProperty(hexa("#9d301f"))
    icon_color = ColorProperty(hexa("#31140d"))
    ring_color = ColorProperty(hexa("#bd6548", 0.32))
    halo = NumericProperty(0.0)
    halo_alpha = NumericProperty(0.35)
    press_scale = NumericProperty(1.0)
    arc_alpha = NumericProperty(0.0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self._spin = 0.0
        self._arc = 0.0
        self._ripples = []      # list of [t 0..1]
        self._ripple_timer = 0.0
        self._geom = None
        tex = glow_texture(128, 2.0)
        with self.canvas:
            self._halo_c = Color(*hexa(C.ember, 0.35))
            self._halo = Rectangle(texture=tex)
            self._ripple_instr = []
            for _ in range(3):
                c = Color(*hexa(C.ember, 0))
                ln = Line(circle=(0, 0, 1), width=dp(1.4))
                self._ripple_instr.append((c, ln))
            self._ring_c = Color(*self.ring_color)
            self._ring = Line(circle=(0, 0, 1), width=dp(1))
            PushMatrix()
            self._rot = Rotate(angle=0, origin=(0, 0))
            self._dash_c = Color(*hexa("#bd6548", 0.45))
            self._dash = Line(circle=(0, 0, 1), width=1, dash_length=dp(5), dash_offset=dp(7))
            PopMatrix()
            self._arc_c = Color(*hexa(C.gold, 0))
            self._arc_line = Line(circle=(0, 0, 1, 0, 90), width=dp(2.6), cap="round")
            PushMatrix()
            self._scale = Scale(1, 1, 1, origin=(0, 0))
            self._shadow_c = Color(*hexa("#ff572d", 0.30))
            self._shadow = Rectangle(texture=tex)
            self._dark_c = Color(*self.fill_dark)
            self._dark = Ellipse()
            self._fill_c = Color(*self.fill)
            self._fill = Ellipse()
            self._border_c = Color(*hexa("#ffb47a"))
            self._border = Line(circle=(0, 0, 1), width=dp(1.1))
            self._icon_c = Color(*self.icon_color)
            self._icon_bar = Line(points=[], width=dp(3), cap="round")
            self._icon_arc = Line(circle=(0, 0, 1, 33, 327), width=dp(3), cap="round")
            PopMatrix()
        self.bind(pos=self._layout, size=self._layout, press_scale=self._layout,
                  hovered=self._on_hover, status=self._on_status,
                  fill=self._colors, fill_dark=self._colors, icon_color=self._colors,
                  ring_color=self._colors, arc_alpha=self._colors, halo_alpha=self._colors)
        breath = Animation(halo=1, d=2.2, t="in_out_sine") + Animation(halo=0, d=2.2, t="in_out_sine")
        breath.repeat = True
        breath.start(self)
        Clock.schedule_interval(self._tick, 1 / 60)

    # geometry
    @property
    def _R(self):
        return min(self.width, self.height) / 2.0

    def _layout(self, *_):
        cx, cy = self.center
        R = self._R
        self._ring.circle = (cx, cy, R - dp(1))
        self._rot.origin = (cx, cy)
        self._dash.circle = (cx, cy, R - dp(22))
        r = R * 0.69
        s = self.press_scale
        self._scale.origin = (cx, cy)
        self._scale.x = self._scale.y = s
        sh = r * 1.65
        self._shadow.pos, self._shadow.size = (cx - sh, cy - sh - dp(16)), (2 * sh, 2 * sh)
        self._dark.pos, self._dark.size = (cx - r, cy - r), (2 * r, 2 * r)
        inner = r - dp(2)
        self._fill.pos, self._fill.size = (cx - inner, cy - inner + dp(3)), (2 * inner, 2 * inner - dp(2))
        self._border.circle = (cx, cy, r)
        u = r * 0.59 / 40.0     # icon: the 80-unit SVG power glyph
        self._icon_bar.points = [cx, cy + 27 * u, cx, cy - 4 * u]
        self._icon_arc.circle = (cx, cy - 6.2 * u, 29 * u, 33.5, 326.5)
        w = max(dp(2.4), r * 0.035)
        self._icon_bar.width = self._icon_arc.width = w
        self._update_dynamic()

    def _colors(self, *_):
        self._fill_c.rgba = self.fill
        self._dark_c.rgba = self.fill_dark
        self._icon_c.rgba = self.icon_color
        self._ring_c.rgba = self.ring_color
        a = self.arc_alpha
        self._arc_c.rgba = (*hexa(C.gold)[:3], a)

    def _update_dynamic(self):
        cx, cy = self.center
        R = self._R
        hs = R * (1.05 + 0.08 * self.halo)
        self._halo.pos, self._halo.size = (cx - hs, cy - hs), (2 * hs, 2 * hs)
        self._halo_c.a = self.halo_alpha * (0.75 + 0.25 * self.halo)
        self._rot.angle = self._spin
        self._arc_line.circle = (cx, cy, R - dp(11), self._arc, self._arc + 70 + 40 * math.sin(self._arc / 40))
        r0 = R * 0.69
        for i, (c, ln) in enumerate(self._ripple_instr):
            if i < len(self._ripples):
                t = self._ripples[i]
                ln.circle = (cx, cy, r0 + (R * 1.04 - r0) * t)
                c.rgba = (*hexa(C.ember)[:3], 0.55 * (1 - t) ** 1.5)
            else:
                c.a = 0

    def _tick(self, dt):
        # A FloatLayout parent binds pos -> its trigger, which returns True and
        # stops the pos event before our own binding sees it, so re-check the
        # geometry every frame instead of trusting the pos callback.
        key = (self.x, self.y, self.width, self.height, self.press_scale)
        if key != self._geom:
            self._geom = key
            self._layout()
        speed = {"off": 8, "connecting": 140, "connected": 18, "stopping": -90, "error": 4}[self.status]
        self._spin = (self._spin - speed * dt) % 360
        if self.status in ("connecting", "stopping"):
            self._arc = (self._arc + 260 * dt) % 360
        if self.status == "connected":
            self._ripple_timer += dt
            if self._ripple_timer > 1.25 and len(self._ripples) < 3:
                self._ripple_timer = 0
                self._ripples.append(0.0)
        self._ripples = [t + dt / 3.4 for t in self._ripples if t + dt / 3.4 < 1]
        self._update_dynamic()

    # interaction / state
    def _on_hover(self, *_):
        if self.state == "normal":
            Animation.cancel_all(self, "press_scale")
            Animation(press_scale=1.04 if self.hovered else 1.0, d=0.25, t="out_quad").start(self)

    def on_state(self, _w, value):
        Animation.cancel_all(self, "press_scale")
        if value == "down":
            Animation(press_scale=0.95, d=0.08, t="out_quad").start(self)
        else:
            Animation(press_scale=1.04 if self.hovered else 1.0, d=0.45, t="out_elastic").start(self)

    def _on_status(self, *_):
        st = self.status
        Animation.cancel_all(self, "fill", "fill_dark", "icon_color", "ring_color", "arc_alpha", "halo_alpha")
        if st == "connected":
            target = dict(fill=hexa("#2a1712"), fill_dark=hexa("#1a0d09"), icon_color=hexa(C.gold),
                          ring_color=hexa("#ffae6c", 0.9), arc_alpha=0, halo_alpha=0.65)
        elif st in ("connecting", "stopping"):
            target = dict(fill=hexa("#e8602f"), fill_dark=hexa("#7d2616"), icon_color=hexa("#3b170d"),
                          ring_color=hexa("#ffae6c", 0.55), arc_alpha=1, halo_alpha=0.45)
        elif st == "error":
            target = dict(fill=hexa("#7a2a20"), fill_dark=hexa("#3d1410"), icon_color=hexa("#ffb4a0"),
                          ring_color=hexa("#d0503a", 0.6), arc_alpha=0, halo_alpha=0.2)
        else:
            target = dict(fill=hexa(C.ember), fill_dark=hexa("#9d301f"), icon_color=hexa("#31140d"),
                          ring_color=hexa("#bd6548", 0.32), arc_alpha=0, halo_alpha=0.35)
        Animation(d=0.6, t="out_cubic", **target).start(self)
        if st != "connected":
            self._ripple_timer = 0


# --------------------------------------------------------------- small pieces
class NavIcon(Widget):
    """Line icons for the sidebar (home / flame / gear / user)."""
    kind = StringProperty("home")
    color = ColorProperty(hexa(C.muted))

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self._draw, size=self._draw, kind=self._draw, color=self._draw)

    def _draw(self, *_):
        self.canvas.clear()
        x, y = self.pos
        s = min(self.size) / 24.0
        w = dp(1.5)

        def P(*pts):   # 24-unit SVG coords (y down) -> widget coords
            out = []
            for i in range(0, len(pts), 2):
                out += [x + pts[i] * s, y + (24 - pts[i + 1]) * s]
            return out

        with self.canvas:
            Color(*self.color)
            k = self.kind
            if k == "home":
                Line(points=P(4, 11.5, 12, 5, 20, 11.5, 20, 20, 4, 20, 4, 11.5), width=w, joint="round")
                Line(points=P(9, 20, 9, 14, 15, 14, 15, 20), width=w, joint="round")
            elif k == "flame":
                Line(bezier=P(12, 22, 4, 21, 3, 15, 4, 13), width=w)
                Line(bezier=P(4, 13, 6, 8, 11, 6, 10, 2), width=w)
                Line(bezier=P(10, 2, 14, 5, 15, 8, 14, 10.5), width=w)
                Line(bezier=P(14, 10.5, 16, 9, 16, 7, 16, 7), width=w)
                Line(bezier=P(16, 7, 22, 11, 21, 21, 12, 22), width=w)
                Line(bezier=P(12, 22, 8, 21.5, 8, 17, 10, 15), width=w)
                Line(bezier=P(10, 15, 13, 17, 14, 15, 13.7, 14.9), width=w)
                Line(bezier=P(13.7, 14.9, 17, 17, 16, 22, 12, 22), width=w)
            elif k == "gear":
                Line(circle=(x + 12 * s, y + 12 * s, 3.2 * s), width=w)
                Line(circle=(x + 12 * s, y + 12 * s, 7.4 * s), width=w)
                for a in range(0, 360, 45):
                    ra = math.radians(a)
                    Line(points=[x + 12 * s + math.cos(ra) * 7.4 * s, y + 12 * s + math.sin(ra) * 7.4 * s,
                                 x + 12 * s + math.cos(ra) * 10 * s, y + 12 * s + math.sin(ra) * 10 * s],
                         width=w, cap="round")
            elif k == "user":
                Line(circle=(x + 12 * s, y + 15.5 * s, 4 * s), width=w)
                Line(circle=(x + 12 * s, y + 1.5 * s, 8.5 * s, -62, 62), width=w)
            elif k == "lock":
                Line(rounded_rectangle=(x + 5 * s, y + 3 * s, 14 * s, 10 * s, 2 * s), width=w)
                Line(circle=(x + 12 * s, y + 13 * s, 4 * s, -90, 90), width=w)


class ToggleSwitch(ButtonBehavior, Widget):
    active = BooleanProperty(False)
    knob = NumericProperty(0.0)

    def __init__(self, **kw):
        super().__init__(**kw)
        with self.canvas:
            self._track_c = Color()
            self._track = RoundedRectangle()
            self._knob_c = Color(*hexa(C.cream))
            self._knob = Ellipse()
        self.knob = 1.0 if self.active else 0.0
        self.bind(pos=self._draw, size=self._draw, knob=self._draw)
        self._draw()

    def on_press(self):
        self.active = not self.active

    def on_active(self, *_):
        Animation(knob=1.0 if self.active else 0.0, d=0.22, t="out_quad").start(self)

    def _draw(self, *_):
        w, h = dp(46), dp(26)
        x, y = self.x, self.center_y - h / 2
        t = self.knob
        off, on = hexa("#4a2f27"), hexa(C.ember)
        self._track_c.rgba = [off[i] + (on[i] - off[i]) * t for i in range(4)]
        self._track.pos, self._track.size, self._track.radius = (x, y), (w, h), [h / 2]
        k = h - dp(6)
        self._knob.pos = (x + dp(3) + (w - k - dp(6)) * t, y + dp(3))
        self._knob.size = (k, k)
