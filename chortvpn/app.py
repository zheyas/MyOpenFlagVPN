"""ChortVPN — desktop client (Kivy) for the OpenFlux tunnel."""

from __future__ import annotations

import os
import time
from collections import deque

os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_LOG_MODE", "PYTHON")

from kivy.config import Config  # noqa: E402

Config.set("graphics", "width", "1120")
Config.set("graphics", "height", "720")
Config.set("graphics", "minimum_width", "880")
Config.set("graphics", "minimum_height", "640")
Config.set("input", "mouse", "mouse,disable_multitouch")
Config.set("kivy", "exit_on_escape", "0")

from kivy.animation import Animation  # noqa: E402
from kivy.app import App  # noqa: E402
from kivy.clock import Clock, mainthread  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.lang import Builder  # noqa: E402
from kivy.properties import (BooleanProperty, ColorProperty, NumericProperty,  # noqa: E402
                             ObjectProperty, StringProperty)
from kivy.uix.behaviors import ButtonBehavior  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.label import Label  # noqa: E402
from kivy.uix.widget import Widget  # noqa: E402

from openflux import logging_util as log  # noqa: E402
from openflux.cli import VALID_TRANSPORTS  # noqa: E402

from . import engine as eng  # noqa: E402
from .settings import SettingsStore, registration_visible  # noqa: E402
from .theme import C, hexa, register_fonts  # noqa: E402
from .widgets import HoverMixin  # noqa: E402

VERSION = "0.1"


# ------------------------------------------------------------------ widgets
class NavButton(HoverMixin, ButtonBehavior, BoxLayout):
    text = StringProperty("")
    icon = StringProperty("home")
    active = BooleanProperty(False)
    badge = StringProperty("")
    glow = NumericProperty(0.0)

    def on_active(self, *_):
        self._animate()

    def on_hovered(self, *_):
        self._animate()

    def _animate(self):
        Animation.cancel_all(self, "glow")
        Animation(glow=1.0 if (self.active or self.hovered) else 0.0, d=0.2).start(self)


class PrimaryButton(HoverMixin, ButtonBehavior, Label):
    lift = NumericProperty(0.0)

    def on_hovered(self, *_):
        Animation.cancel_all(self, "lift")
        Animation(lift=1.0 if self.hovered and not self.disabled else 0.0, d=0.2).start(self)


class Field(BoxLayout):
    title = StringProperty("")
    text = StringProperty("")
    hint = StringProperty("")
    password = BooleanProperty(False)
    input_filter = ObjectProperty(None, allownone=True)


class StatusDot(Widget):
    color = ColorProperty(hexa("#a18d84"))
    pulse = NumericProperty(0.0)

    def __init__(self, **kw):
        super().__init__(**kw)
        a = Animation(pulse=1, d=1.4, t="out_quad") + Animation(pulse=0, d=0.01)
        a.repeat = True
        a.start(self)


class Txt(Label):
    pass


KV = r"""
#:import hexa chortvpn.theme.hexa
#:import C chortvpn.theme.C
#:import dp kivy.metrics.dp

<Txt>:
    font_name: 'Body'
    color: hexa(C.cream)
    size_hint_y: None
    height: self.texture_size[1]
    text_size: self.width, None
    halign: 'center'
    markup: True

<StatusDot>:
    size_hint: None, None
    size: dp(8), dp(8)
    canvas:
        Color:
            rgba: self.color[0], self.color[1], self.color[2], 0.35 * (1 - self.pulse)
        Ellipse:
            pos: self.center_x - dp(4) - dp(6) * self.pulse, self.center_y - dp(4) - dp(6) * self.pulse
            size: dp(8) + dp(12) * self.pulse, dp(8) + dp(12) * self.pulse
        Color:
            rgba: self.color
        Ellipse:
            pos: self.pos
            size: self.size

<NavButton>:
    size_hint_y: None
    height: dp(46)
    padding: dp(13), 0
    spacing: dp(10)
    canvas.before:
        Color:
            rgba: 1, .443, .247, 0.094 * self.glow
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]
        Color:
            rgba: 1, .443, .247, 0.24 * self.glow
        Line:
            rounded_rectangle: self.x, self.y, self.width, self.height, dp(12)
            width: 1
    NavIcon:
        kind: root.icon
        size_hint: None, None
        size: dp(19), dp(19)
        pos_hint: {'center_y': .5}
        color: hexa(C.cream) if root.glow > .5 else hexa(C.muted)
    Label:
        text: root.text
        font_name: 'Body'
        font_size: '15sp'
        color: hexa(C.cream) if root.glow > .5 else hexa(C.muted)
        text_size: self.size
        shorten: True
        halign: 'left'
        valign: 'middle'
    Label:
        text: root.badge
        opacity: 1 if root.badge else 0
        size_hint_x: None
        width: self.texture_size[0] + dp(12) if root.badge else 0
        font_name: 'BodyBold'
        font_size: '9sp'
        color: hexa(C.gold)
        canvas.before:
            Color:
                rgba: hexa('#ff713f', .18)
            RoundedRectangle:
                pos: self.x, self.center_y - dp(10)
                size: self.width, dp(20)
                radius: [dp(10)]

<PrimaryButton>:
    font_name: 'BodyBold'
    font_size: '15sp'
    color: hexa('#31140d')
    size_hint: None, None
    size: self.texture_size[0] + dp(40), dp(46)
    opacity: .45 if self.disabled else 1
    disabled_color: hexa('#31140d')
    canvas.before:
        PushMatrix
        Translate:
            y: dp(2) * self.lift
        Color:
            rgba: hexa(C.ember, .35 * self.lift)
        RoundedRectangle:
            pos: self.x - dp(4), self.y - dp(6)
            size: self.width + dp(8), self.height + dp(8)
            radius: [dp(14)]
        Color:
            rgba: (1, .48 + .08 * self.lift, .30 + .06 * self.lift, 1) if self.state == 'normal' else hexa('#e8602f')
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(11)]
    canvas.after:
        PopMatrix

<Field>:
    orientation: 'vertical'
    size_hint_y: None
    height: dp(76)
    spacing: dp(7)
    Label:
        text: root.title
        font_name: 'BodyBold'
        font_size: '12sp'
        color: hexa(C.muted)
        size_hint_y: None
        height: dp(16)
        text_size: self.size
        halign: 'left'
        valign: 'middle'
    TextInput:
        text: root.text
        on_text: root.text = self.text
        hint_text: root.hint
        password: root.password
        input_filter: root.input_filter
        disabled: root.disabled
        multiline: False
        write_tab: False
        font_name: 'Body'
        font_size: '15sp'
        background_normal: ''
        background_active: ''
        background_disabled_normal: ''
        background_color: 0, 0, 0, 0
        foreground_color: hexa(C.cream)
        disabled_foreground_color: hexa(C.muted, .45)
        hint_text_color: hexa(C.muted, .45)
        cursor_color: hexa(C.ember)
        selection_color: hexa(C.ember, .35)
        padding: dp(14), (self.height - self.line_height) / 2
        canvas.before:
            Color:
                rgba: hexa('#1d1210') if not self.disabled else hexa('#1d1210', .6)
            RoundedRectangle:
                pos: self.pos
                size: self.size
                radius: [dp(11)]
            Color:
                rgba: hexa(C.ember, .75) if self.focus else hexa('#ac6246', .35)
            Line:
                rounded_rectangle: self.x, self.y, self.width, self.height, dp(11)
                width: 1.1 if self.focus else 1
            # TextInput's own style sets the text colour in canvas.before; our
            # drawing above replaced it, so restore it for the glyphs.
            Color:
                rgba: self.disabled_foreground_color if self.disabled else (self.hint_text_color if not self.text else self.foreground_color)

<Card@BoxLayout>:
    orientation: 'vertical'
    size_hint: None, None
    width: min(dp(520), self.parent.width if self.parent else dp(520))
    height: self.minimum_height
    padding: dp(38)
    spacing: dp(14)
    canvas.before:
        Color:
            rgba: hexa('#090504', .45)
        RoundedRectangle:
            pos: self.x - dp(6), self.y - dp(18)
            size: self.width + dp(12), self.height + dp(12)
            radius: [dp(26)]
        Color:
            rgba: hexa(C.surface)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(22)]
        Color:
            rgba: hexa('#ac6246', .31)
        Line:
            rounded_rectangle: self.x, self.y, self.width, self.height, dp(22)
            width: 1

<Eyebrow@Txt>:
    font_name: 'BodyBold'
    font_size: '12sp'
    color: hexa(C.gold, .85)

<Rule@Widget>:
    size_hint_y: None
    height: dp(1)
    canvas:
        Color:
            rgba: hexa('#a86647', .30)
        Rectangle:
            pos: self.pos
            size: self.size

<SwitchRow@BoxLayout>:
    title: ''
    note: ''
    active: False
    size_hint_y: None
    height: dp(48)
    spacing: dp(12)
    BoxLayout:
        orientation: 'vertical'
        Label:
            text: root.title
            font_name: 'BodyBold'
            font_size: '14sp'
            color: hexa(C.cream)
            text_size: self.size
            halign: 'left'
            valign: 'bottom'
        Label:
            text: root.note
            font_name: 'Body'
            font_size: '12sp'
            color: hexa(C.muted)
            text_size: self.size
            halign: 'left'
            valign: 'top'
    ToggleSwitch:
        size_hint_x: None
        width: dp(46)
        active: root.active
        on_active: root.active = self.active

<RootUI>:
    BoxLayout:
        id: sidebar
        orientation: 'vertical'
        size_hint_x: None
        width: dp(248)
        padding: dp(18), dp(32), dp(18), dp(26)
        spacing: dp(7)
        canvas.before:
            Color:
                rgba: hexa(C.side)
            Rectangle:
                pos: self.pos
                size: self.size
            Color:
                rgba: hexa('#734130', .4)
            Rectangle:
                pos: self.right - 1, self.y
                size: 1, self.height
        BoxLayout:
            size_hint_y: None
            height: dp(36)
            spacing: dp(12)
            Label:
                text: app.brand_glyph
                font_name: app.brand_font
                font_size: '22sp'
                color: hexa('#2c110c')
                size_hint: None, None
                size: dp(34), dp(34)
                pos_hint: {'center_y': .5}
                canvas.before:
                    Color:
                        rgba: hexa(C.ember, .12 + .25 * app.heat)
                    RoundedRectangle:
                        pos: self.x - dp(6), self.y - dp(6)
                        size: self.width + dp(12), self.height + dp(12)
                        radius: [dp(15)]
                    Color:
                        rgba: hexa(C.ember)
                    RoundedRectangle:
                        pos: self.pos
                        size: self.size
                        radius: [dp(10)]
            Label:
                text: 'ChortVPN'
                font_name: 'Display'
                font_size: '22sp'
                color: hexa(C.cream)
                text_size: self.size
                halign: 'left'
                valign: 'middle'
        Widget:
            size_hint_y: None
            height: dp(30)
        NavButton:
            id: nav_home
            text: 'Главная'
            icon: 'home'
            active: sm.current == 'home'
            on_release: app.go('home')
        NavButton:
            id: nav_legend
            text: 'Легенда'
            icon: 'flame'
            active: sm.current == 'legend'
            on_release: app.go('legend')
        NavButton:
            id: nav_settings
            text: 'Настройки'
            icon: 'gear'
            active: sm.current == 'settings'
            on_release: app.go('settings')
        NavButton:
            id: nav_register
            text: 'Регистрация'
            icon: 'user'
            badge: 'СКОРО'
            active: sm.current == 'register'
            on_release: app.go('register')
            opacity: 1 if app.show_registration else 0
            disabled: not app.show_registration
            height: dp(46) if app.show_registration else 0
        Widget:
        Rule:
        BoxLayout:
            size_hint_y: None
            height: dp(42)
            spacing: dp(10)
            padding: 0, dp(14), 0, 0
            StatusDot:
                color: hexa(C.ember) if app.status == 'connected' else (hexa('#d0503a') if app.status == 'error' else hexa('#a18d84'))
                pos_hint: {'center_y': .5}
            Label:
                text: app.note_text
                font_name: 'Body'
                font_size: '13sp'
                color: hexa(C.muted)
                text_size: self.size
                halign: 'left'
                valign: 'middle'
                shorten: True

    FloatLayout:
        ContentBackdrop:
            id: backdrop
            pos: self.parent.pos
            size: self.parent.size
            intensity: app.heat
        BoxLayout:
            orientation: 'vertical'
            pos: self.parent.pos
            size: self.parent.size
            padding: dp(56), dp(34), dp(56), dp(24)
            BoxLayout:
                size_hint_y: None
                height: dp(30)
                Label:
                    text: 'ЖАР В ДЕТАЛЯХ. ПОКОЙ ВНУТРИ.'
                    font_name: 'BodyBold'
                    font_size: '12sp'
                    color: hexa(C.cream, .85)
                    text_size: self.size
                    halign: 'left'
                    valign: 'middle'
                Label:
                    text: app.tag_text
                    font_name: 'BodyBold'
                    font_size: '11sp'
                    color: hexa(C.cream)
                    size_hint_x: None
                    width: self.texture_size[0] + dp(26)
                    canvas.before:
                        Color:
                            rgba: hexa('#9f6047')
                        Line:
                            rounded_rectangle: self.x, self.center_y - dp(13), self.width, dp(26), dp(13)
                            width: 1
            ScreenManager:
                id: sm
                Screen:
                    name: 'home'
                    BoxLayout:
                        id: home_box
                        orientation: 'vertical'
                        padding: 0, dp(10)
                        Widget:
                        Eyebrow:
                            text: 'ПО ТУ СТОРОНУ ШУМА'
                        Widget:
                            size_hint_y: None
                            height: dp(10)
                        Txt:
                            id: title
                            text: app.title_text
                            font_name: 'Display'
                            font_size: '44sp'
                        Txt:
                            text: app.subtitle_text
                            font_size: '16sp'
                            color: hexa(C.cream, .82)
                        FloatLayout:
                            size_hint_y: None
                            height: max(dp(150), min(dp(330), home_box.height - dp(250)))
                            EmberField:
                                heat: app.heat
                                size_hint: None, None
                                size: self.parent.height * 1.7, self.parent.height * 1.35
                                center: self.parent.center
                            PowerButton:
                                id: power
                                status: app.status
                                size_hint: None, None
                                size: self.parent.height, self.parent.height
                                center: self.parent.center
                                on_release: app.toggle()
                        BoxLayout:
                            size_hint: None, None
                            height: dp(24)
                            width: self.minimum_width
                            pos_hint: {'center_x': .5}
                            spacing: dp(10)
                            StatusDot:
                                color: hexa('#ff985c') if app.status == 'connected' else (hexa('#d0503a') if app.status == 'error' else (hexa(C.gold) if app.status in ('connecting', 'stopping') else hexa('#a18d84')))
                                pos_hint: {'center_y': .5}
                            Label:
                                text: app.status_text
                                font_name: 'BodyBold'
                                font_size: '15sp'
                                color: hexa(C.cream)
                                size_hint_x: None
                                width: self.texture_size[0]
                        Widget:
                            size_hint_y: None
                            height: dp(8)
                        Txt:
                            text: app.detail_text
                            font_size: '13sp'
                            color: hexa('#ffb4a0') if app.status == 'error' else hexa(C.muted)
                        Widget:

                Screen:
                    name: 'legend'
                    AnchorLayout:
                        Card:
                            AnchorLayout:
                                anchor_x: 'left'
                                size_hint_y: None
                                height: dp(54)
                                Widget:
                                    size_hint: None, None
                                    size: dp(54), dp(54)
                                    canvas:
                                        Color:
                                            rgba: hexa(C.ember, .15)
                                        RoundedRectangle:
                                            pos: self.pos
                                            size: self.size
                                            radius: [dp(16)]
                                    NavIcon:
                                        kind: 'flame'
                                        color: hexa('#ffae79')
                                        size: dp(28), dp(28)
                                        center: self.parent.center
                            Widget:
                                size_hint_y: None
                                height: dp(8)
                            Eyebrow:
                                text: 'ИСТОРИЯ ОБРАЗА'
                                halign: 'left'
                            Txt:
                                text: 'Там, где тлеют искры.'
                                font_name: 'Display'
                                font_size: '32sp'
                                halign: 'left'
                            Txt:
                                text: 'Угольные оттенки, огненные кольца и свет пламени — наша игривая отсылка к адским мотивам. Никакой магии: только надёжный туннель и один понятный переключатель.'
                                font_size: '15sp'
                                color: hexa(C.cream, .85)
                                halign: 'left'
                            Rule:
                            Txt:
                                text: 'Трафик шифруется AES-256-GCM и уходит через выходной узел. Приложение поднимает локальный SOCKS5 и, если разрешено, само направляет на него системный прокси.'
                                font_size: '13sp'
                                color: hexa(C.muted)
                                halign: 'left'
                            Widget:
                                size_hint_y: None
                                height: dp(6)
                            PrimaryButton:
                                text: 'Вернуться к вратам'
                                on_release: app.go('home')

                Screen:
                    name: 'settings'
                    ScrollView:
                        do_scroll_x: False
                        bar_color: hexa(C.ember, .6)
                        bar_inactive_color: hexa(C.ember, .2)
                        bar_width: dp(4)
                        AnchorLayout:
                            size_hint_y: None
                            height: max(self.parent.height, settings_card.height + dp(30))
                            Card:
                                id: settings_card
                                width: min(dp(560), self.parent.width)
                                Eyebrow:
                                    text: 'НАСТРОЙКИ ПОДКЛЮЧЕНИЯ'
                                    halign: 'left'
                                Txt:
                                    text: 'Ключи от врат'
                                    font_name: 'Display'
                                    font_size: '28sp'
                                    halign: 'left'
                                Txt:
                                    text: app.settings_msg
                                    font_size: '13sp'
                                    color: hexa(C.gold) if app.settings_msg else hexa(C.muted)
                                    halign: 'left'
                                    opacity: 1 if app.settings_msg else 0
                                Field:
                                    id: f_rooms
                                    title: 'Адрес выхода (ROOMS_URL)'
                                    hint: 'https://<app>.onrender.com/rooms'
                                Field:
                                    id: f_key
                                    title: 'Ключ шифрования'
                                    hint: 'выдаётся вместе с доступом'
                                    password: True
                                Field:
                                    id: f_doc
                                    title: 'Список комнат вручную (необязательно)'
                                    hint: 'используется, если адрес выхода недоступен'
                                BoxLayout:
                                    size_hint_y: None
                                    height: dp(76)
                                    spacing: dp(14)
                                    Field:
                                        id: f_port
                                        title: 'Порт SOCKS5'
                                        input_filter: 'int'
                                    BoxLayout:
                                        orientation: 'vertical'
                                        spacing: dp(7)
                                        Label:
                                            text: 'Транспорт'
                                            font_name: 'BodyBold'
                                            font_size: '12sp'
                                            color: hexa(C.muted)
                                            size_hint_y: None
                                            height: dp(16)
                                            text_size: self.size
                                            halign: 'left'
                                        Spinner:
                                            id: f_transport
                                            values: app.transports
                                            font_name: 'Body'
                                            background_normal: ''
                                            background_down: ''
                                            background_color: 0, 0, 0, 0
                                            color: hexa(C.cream)
                                            canvas.before:
                                                Color:
                                                    rgba: hexa('#1d1210')
                                                RoundedRectangle:
                                                    pos: self.pos
                                                    size: self.size
                                                    radius: [dp(11)]
                                                Color:
                                                    rgba: hexa('#ac6246', .35)
                                                Line:
                                                    rounded_rectangle: self.x, self.y, self.width, self.height, dp(11)
                                                    width: 1
                                Rule:
                                SwitchRow:
                                    id: f_proxy
                                    title: 'Системный прокси'
                                    note: 'Направлять трафик системы в туннель при подключении'
                                SwitchRow:
                                    id: f_debug
                                    title: 'Подробный журнал'
                                    note: 'Для диагностики проблем с подключением'
                                Widget:
                                    size_hint_y: None
                                    height: dp(4)
                                BoxLayout:
                                    size_hint_y: None
                                    height: dp(46)
                                    spacing: dp(12)
                                    PrimaryButton:
                                        text: 'Сохранить'
                                        on_release: app.save_settings()
                                    Label:
                                        text: app.saved_note
                                        font_name: 'Body'
                                        font_size: '13sp'
                                        color: hexa(C.gold)
                                        text_size: self.size
                                        halign: 'left'
                                        valign: 'middle'
                                Rule:
                                Eyebrow:
                                    text: 'ЖУРНАЛ'
                                    halign: 'left'
                                Label:
                                    text: app.log_text
                                    font_name: 'RobotoMono-Regular'
                                    font_size: '11sp'
                                    color: hexa(C.muted)
                                    size_hint_y: None
                                    height: max(dp(80), self.texture_size[1] + dp(20))
                                    text_size: self.width - dp(24), None
                                    halign: 'left'
                                    valign: 'top'
                                    padding: dp(12), dp(10)
                                    canvas.before:
                                        Color:
                                            rgba: hexa('#120c0a')
                                        RoundedRectangle:
                                            pos: self.pos
                                            size: self.size
                                            radius: [dp(10)]

                Screen:
                    name: 'register'
                    AnchorLayout:
                        Card:
                            Label:
                                text: 'Регистрация откроется после запуска'
                                font_name: 'BodyBold'
                                font_size: '12sp'
                                color: hexa(C.gold)
                                size_hint: None, None
                                size: self.texture_size[0] + dp(28), dp(28)
                                canvas.before:
                                    Color:
                                        rgba: hexa(C.ember, .16)
                                    RoundedRectangle:
                                        pos: self.pos
                                        size: self.size
                                        radius: [dp(14)]
                            Txt:
                                text: 'Создайте аккаунт'
                                font_name: 'Display'
                                font_size: '30sp'
                                halign: 'left'
                            Txt:
                                text: 'Пока доступ выдаётся вручную — ключ и адрес выхода вводятся в настройках.'
                                font_size: '13sp'
                                color: hexa(C.muted)
                                halign: 'left'
                            Field:
                                title: 'Электронная почта'
                                hint: 'you@example.com'
                                disabled: not app.registration_enabled
                            Field:
                                title: 'Пароль'
                                password: True
                                disabled: not app.registration_enabled
                            Field:
                                title: 'Повторите пароль'
                                password: True
                                disabled: not app.registration_enabled
                            Widget:
                                size_hint_y: None
                                height: dp(4)
                            PrimaryButton:
                                text: 'Создать аккаунт'
                                disabled: not app.registration_enabled

            BoxLayout:
                size_hint_y: None
                height: dp(46)
                padding: 0, dp(18), 0, 0
                canvas.before:
                    Color:
                        rgba: hexa('#b16a45', .2)
                    Rectangle:
                        pos: self.x, self.top - 1
                        size: self.width, 1
                Label:
                    text: 'ChortVPN © 2026'
                    font_name: 'Body'
                    font_size: '13sp'
                    color: hexa(C.muted)
                    text_size: self.size
                    halign: 'left'
                    valign: 'middle'
                Label:
                    text: 'Пламя — в образе. Управление — у вас.'
                    font_name: 'Body'
                    font_size: '13sp'
                    color: hexa(C.muted)
                    text_size: self.size
                    halign: 'right'
                    valign: 'middle'
"""


class RootUI(BoxLayout):
    pass


def _fmt_bytes(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024.0
    return str(n)


TEXTS = {
    eng.OFF: ("Разбудите огонь.", "Одно нажатие — и врата открыты.", "Врата закрыты", "Защита выключена"),
    eng.CONNECTING: ("Огонь разгорается.", "Собираем каналы и проверяем выход.", "Открываем врата…", "Подключение…"),
    eng.CONNECTED: ("Огонь горит.", "Трафик идёт через пламя. Нажмите, чтобы погасить.", "Врата открыты", "Защита активна"),
    eng.STOPPING: ("Пламя стихает.", "Закрываем каналы и возвращаем сеть.", "Закрываем врата…", "Отключение…"),
    eng.ERROR: ("Пламя угасло.", "Нажмите, чтобы попробовать снова.", "Не удалось открыть врата", "Ошибка подключения"),
}


class ChortApp(App):
    status = StringProperty(eng.OFF)
    heat = NumericProperty(0.2)
    title_text = StringProperty(TEXTS[eng.OFF][0])
    subtitle_text = StringProperty(TEXTS[eng.OFF][1])
    status_text = StringProperty(TEXTS[eng.OFF][2])
    note_text = StringProperty(TEXTS[eng.OFF][3])
    detail_text = StringProperty("")
    tag_text = StringProperty(f"v{VERSION}")
    settings_msg = StringProperty("")
    saved_note = StringProperty("")
    log_text = StringProperty("Журнал пуст")
    brand_glyph = StringProperty("Ч")
    brand_font = StringProperty("Display")
    show_registration = BooleanProperty(False)
    registration_enabled = BooleanProperty(False)   # form stays inactive until prod
    transports = list(VALID_TRANSPORTS)

    def build(self):
        self.title = "ChortVPN"
        self.icon = ""
        glyph = register_fonts()
        self.brand_glyph, self.brand_font = glyph["brand_glyph"], glyph["brand_font"]
        Window.clearcolor = hexa(C.coal)
        Builder.load_string(KV)
        self.show_registration = registration_visible()
        self.store = SettingsStore(self.user_data_dir)
        self.settings = self.store.load()
        self.engine = eng.TunnelEngine(self._on_state_bg, self._on_stats_bg)
        os.makedirs(self.user_data_dir, exist_ok=True)
        self.engine.proxy_marker = os.path.join(self.user_data_dir, "proxy.on")
        self.engine.restore_proxy_if_stale()
        self._log = deque(maxlen=60)
        self._log_trigger = Clock.create_trigger(self._flush_log, 0.25)
        log.add_sink(self._on_log_bg)
        self.root_ui = RootUI()
        self.ids = self.root_ui.ids
        self._fill_settings_form()
        self._apply_state(eng.OFF, "")
        return self.root_ui

    # ---------------------------------------------------------------- nav
    def go(self, name: str):
        sm = self.ids.sm
        if sm.current == name:
            return
        from kivy.uix.screenmanager import FadeTransition
        sm.transition = FadeTransition(duration=0.22)
        if name == "settings":
            self.saved_note = ""
        sm.current = name

    # ---------------------------------------------------------------- power
    def toggle(self):
        st = self.engine.state
        if st in (eng.CONNECTING, eng.CONNECTED):
            self.engine.stop()
            return
        if st == eng.STOPPING or self.engine.busy:
            return
        problem = self.settings.problems()
        if problem:
            self.settings_msg = problem
            self._apply_state(eng.ERROR, problem)
            Clock.schedule_once(lambda *_: self.go("settings"), 0.6)
            return
        self.settings_msg = ""
        self.engine.start(self.settings)

    def _on_state_bg(self, state, detail):
        self._dispatch_state(state, detail)

    @mainthread
    def _dispatch_state(self, state, detail):
        self._apply_state(state, detail)

    def _apply_state(self, state, detail):
        self.status = state
        title, sub, status, note = TEXTS[state]
        self.status_text, self.note_text = status, note
        if state == eng.CONNECTED:
            detail = f"{detail} · 00:00:00"
        self.detail_text = detail
        if state in (eng.CONNECTED, eng.OFF, eng.ERROR):
            self.tag_text = (self.settings.transport.upper() if state == eng.CONNECTED else f"v{VERSION}")
        heat = {eng.OFF: 0.2, eng.CONNECTING: 0.7, eng.CONNECTED: 1.0, eng.STOPPING: 0.4, eng.ERROR: 0.05}[state]
        Animation.cancel_all(self, "heat")
        Animation(heat=heat, d=1.2, t="out_cubic").start(self)
        if title != self.title_text:
            self._swap_title(title, sub)

    def _swap_title(self, title, sub):
        w = self.ids.title
        Animation.cancel_all(w, "opacity")
        out = Animation(opacity=0, d=0.18)

        def _set(*_):
            self.title_text, self.subtitle_text = title, sub
            Animation(opacity=1, d=0.35, t="out_quad").start(w)
        out.bind(on_complete=_set)
        out.start(w)

    def _on_stats_bg(self, stats):
        self._show_stats(stats.since, stats.bytes_received, stats.bytes_sent)

    @mainthread
    def _show_stats(self, since, rx, tx):
        if self.status != eng.CONNECTED:
            return
        el = int(time.time() - since)
        hh, mm, ss = el // 3600, el // 60 % 60, el % 60
        self.detail_text = (f"SOCKS5 {self.settings.socks_addr} · {hh:02d}:{mm:02d}:{ss:02d}"
                            f"  ·  получено {_fmt_bytes(rx)}  ·  отправлено {_fmt_bytes(tx)}")

    # ---------------------------------------------------------------- settings
    def _fill_settings_form(self):
        s, ids = self.settings, self.ids
        ids.f_rooms.text = s.rooms_url
        ids.f_key.text = s.encryption_key
        ids.f_doc.text = s.doc_url
        ids.f_port.text = str(s.socks_port)
        ids.f_transport.text = s.transport
        ids.f_proxy.active = s.system_proxy
        ids.f_debug.active = s.debug

    def save_settings(self):
        ids, s = self.ids, self.settings
        s.rooms_url = ids.f_rooms.text.strip()
        s.encryption_key = ids.f_key.text.strip()
        s.doc_url = ids.f_doc.text.strip()
        try:
            s.socks_port = int(ids.f_port.text or 1080)
        except ValueError:
            s.socks_port = 1080
        s.transport = ids.f_transport.text or "cupsonline"
        s.system_proxy = bool(ids.f_proxy.active)
        s.debug = bool(ids.f_debug.active)
        self.store.save(s)
        self.settings_msg = s.problems() or ""
        self.saved_note = ("Сохранено. Изменения вступят в силу при следующем подключении."
                           if self.engine.busy else "Сохранено")

    # ---------------------------------------------------------------- log
    def _on_log_bg(self, level, msg):
        self._log.append(f"{time.strftime('%H:%M:%S')} {level} {msg}")
        self._log_trigger()

    def _flush_log(self, *_):
        self.log_text = "\n".join(list(self._log)[-14:]) or "Журнал пуст"

    def on_stop(self):
        log.remove_sink(self._on_log_bg)
        self.engine.stop(wait=6.0)


def main():
    ChortApp().run()


if __name__ == "__main__":
    main()
