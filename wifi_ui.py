"""Dokunmatik menuler: Wi-Fi (ag listesi, Turkce ekran klavyesi), Bluetooth (eslestirme, baglanma), radyo.

Ag islemleri wifi_setup.py'nin yerel API'si (127.0.0.1:8099) uzerinden yapilir; boylece
kurulum modu (AP) acikken de ayni menu calisir. Cizim yardimcilari dashboard modulunden gelir.
"""
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pygame

API = "http://127.0.0.1:8099"

LETTERS = [
    list("qwertyuıopğü"),
    list("asdfghjklşi"),
    ["⇧"] + list("zxcvbnmöç") + ["⌫"],
    ["123", " ", ".", "OK"],
]
SYMBOLS = [
    list("1234567890-_"),
    list("@#$%&*()+=/"),
    ["abc"] + list("!?,;:'\"<>") + ["⌫"],
    ["abc", " ", ".", "OK"],
]
# Satirdaki tus genislikleri (birim); 1. 3. ve 4. satir 12 birim, 2. satir 11 birim ortali
WIDE = {"⇧": 1.5, "⌫": 1.5, "123": 1.5, "abc": 1.5, " ": 7.5, "OK": 2}
TR_UPPER = {"i": "İ", "ı": "I"}

KEY_BG = (52, 62, 86)
KEY_SPECIAL = (34, 42, 60)
PRIMARY = (79, 140, 255)
DANGER = (220, 80, 90)
MUTED = (160, 170, 190)
WHITE = (255, 255, 255)
SOFT = (220, 226, 236)


def upper(ch):
    return TR_UPPER.get(ch, ch.upper())


# ---------------------------------------------------------------- dokunmatik okuyucu

class Touch:
    """Tek parmak olaylari: ('down', x, y), ('move', x, y), ('up', x, y), ('tap', x, y), ('swipe', dx, dy),
    ('vswipe', baslangic_y, dy)."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.events = queue.Queue()
        threading.Thread(target=self.run, daemon=True).start()

    def find(self):
        import evdev
        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            abs_codes = [c for c, _ in dev.capabilities(absinfo=True).get(evdev.ecodes.EV_ABS, [])]
            if evdev.ecodes.ABS_MT_POSITION_X in abs_codes:
                return dev
        return None

    def run(self):
        import evdev
        e = evdev.ecodes
        while True:
            dev = None
            try:
                dev = self.find()
            except Exception:
                pass
            if dev is None:
                time.sleep(5)  # dokunmatik takili degil; sonra tekrar bak
                continue
            mx = dev.absinfo(e.ABS_MT_POSITION_X).max or self.w
            my = dev.absinfo(e.ABS_MT_POSITION_Y).max or self.h
            slot, x, y, down, pending, last_move = 0, None, None, None, False, (0, 0, 0)
            try:
                for ev in dev.read_loop():
                    if ev.type == e.EV_ABS:
                        if ev.code == e.ABS_MT_SLOT:
                            slot = ev.value
                        elif slot != 0:
                            continue  # sadece ilk parmak
                        elif ev.code == e.ABS_MT_TRACKING_ID:
                            if ev.value == -1:
                                if down and x is not None:
                                    self.events.put(("up", x, y))
                                    dx, dy = x - down[0], y - down[1]
                                    if abs(dx) < 45 and abs(dy) < 45:
                                        self.events.put(("tap", *down))
                                    elif abs(dx) > 150 and abs(dx) > 2 * abs(dy):
                                        self.events.put(("swipe", dx, dy))
                                    elif abs(dy) > 150 and abs(dy) > 2 * abs(dx):
                                        self.events.put(("vswipe", down[1], dy))
                                down, pending = None, False
                            else:
                                pending = True
                        elif ev.code == e.ABS_MT_POSITION_X:
                            x = ev.value * self.w // mx
                        elif ev.code == e.ABS_MT_POSITION_Y:
                            y = ev.value * self.h // my
                    elif ev.type == e.EV_SYN and pending and x is not None and y is not None:
                        down, pending = (x, y), False
                        last_move = (x, y, time.time())
                        self.events.put(("down", x, y))
                    elif ev.type == e.EV_SYN and down and x is not None:
                        # surukleme: 6 px'ten fazla ve 30 ms'de bir (kuyruk tasmasin)
                        if abs(x - last_move[0]) + abs(y - last_move[1]) > 6 and time.time() - last_move[2] > 0.03:
                            last_move = (x, y, time.time())
                            self.events.put(("move", x, y))
            except OSError:
                time.sleep(2)  # kablo cikarildi


# ---------------------------------------------------------------- menu

class Menu:
    """Dokunmatik menulerin ortak parcasi: dugmeler, katmanli cizim, dokunma isleme."""

    auto_close = True  # uzun sure dokunulmazsa kapansin

    def __init__(self, m, width, height):
        self.m = m  # dashboard modulu: font, text, asset_icon, wifi_icon, fit_text
        self.w, self.h = width, height
        self.u = height / 800
        self.mode = None
        self.version = 0  # veri degisince artar -> yeniden ciz
        self.buttons = []
        self.pressed = None
        self.last_touch = 0
        self.list_box = pygame.Rect(0, 0, 0, 0)  # "list" modunda disina dokununca kapanir

    def U(self, v):
        return int(v * self.u)

    @property
    def active(self):
        return self.mode is not None

    def touch(self):
        self.last_touch = time.time()
        self.version += 1

    def api(self, path, data=None, timeout=15):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(API + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as err:
            return json.loads(err.read() or b"{}") | {"http_error": err.code}

    def animating(self):
        return self.pressed is not None and time.time() - self.pressed[1] < 0.3

    def on_error(self, err):
        pass

    def background(self, fn):
        def run():
            try:
                fn()
            except Exception as err:  # servis yanit vermedi vb.
                self.on_error(err)
            self.version += 1
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def darken(surf, alpha):
        """Arka plani karart: yari saydam katman yerine renk carpimi (Zero 2'de ~10 kat hizli)."""
        f = 255 - alpha
        surf.fill((f, f, f), special_flags=pygame.BLEND_MULT)

    def card(self, surf, rect, radius=None):
        r = radius if radius is not None else self.U(28)
        pygame.draw.rect(surf, (17, 22, 34), rect, border_radius=r)
        pygame.draw.rect(surf, (44, 52, 72), rect, width=1, border_radius=r)

    def button(self, surf, rect, label=None, icon=None, cb=None, style="normal", enabled=True, font_size=24):
        m = self.m
        col = {"normal": KEY_BG, "special": KEY_SPECIAL, "primary": PRIMARY, "danger": DANGER,
               "ghost": None}[style]
        if col:
            if not enabled:
                col = tuple(c // 2 for c in col)
            pygame.draw.rect(surf, col, rect, border_radius=self.U(14))
        fg = WHITE if enabled else MUTED
        icon_img = m.asset_icon(icon, self.U(30), 255 if enabled else 120) if icon else None
        label_img = m.font("Inter-Medium", self.U(font_size)).render(label, True, fg) if label else None
        gap = self.U(10) if icon_img and label_img else 0
        total = (icon_img.get_width() if icon_img else 0) + gap + (label_img.get_width() if label_img else 0)
        x = rect.centerx - total // 2
        if icon_img:
            surf.blit(icon_img, icon_img.get_rect(midleft=(x, rect.centery)))
            x += icon_img.get_width() + gap
        if label_img:
            surf.blit(label_img, label_img.get_rect(midleft=(x, rect.centery)))
        if cb and enabled:
            self.buttons.append((rect, cb))

    def draw_pressed(self, surf):
        """Basili dugmenin uzerine acik renkli yari saydam vurgu (sadece o dugme kadar alan)."""
        if self.pressed is None or time.time() - self.pressed[1] >= 0.3:
            return
        rect = self.pressed[0]
        hl = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(hl, (255, 255, 255, 55), hl.get_rect(), border_radius=self.U(14))
        surf.blit(hl, rect.topleft)

    def handle(self, kind, x, y):
        self.touch()
        hit = next((b for b in reversed(self.buttons) if b[0].collidepoint(x, y)), None)
        if kind == "down":
            self.pressed = (hit[0], time.time()) if hit else None
            return
        if kind != "tap":
            return
        if hit:
            hit[1]()
        elif self.mode == "list" and not self.list_box.collidepoint(x, y):
            self.close()  # pencere disina dokununca kapat

    def close(self):
        self.mode = None
        self.version += 1

    # ------------------------------------------------ cizim
    def layer_key(self):
        return (self.mode,)

    def before_render(self):
        """True donerse menu bu karede kapanir."""
        return False

    def draw_dynamic(self, surf):
        """Her karede cizilen, katmana alinmayan kisimlar."""

    def render(self, base):
        if self.before_render() or self.mode is None:
            return base
        key = self.layer_key()
        if key != getattr(self, "_layer_key", None):
            layer = base.copy()
            self.buttons = []
            getattr(self, "draw_" + self.mode)(layer)
            self._layer, self._layer_key, self._layer_buttons = layer, key, self.buttons
        surf = self._layer.copy()
        self.buttons = self._layer_buttons
        self.draw_dynamic(surf)
        self.draw_pressed(surf)
        return surf

    def modal(self, surf, w, h, alpha=175):
        self.darken(surf, alpha)
        box = pygame.Rect(0, 0, self.U(w), self.U(h))
        box.center = (self.w // 2, self.h // 2)
        self.card(surf, box)
        return box

    def spinner(self, surf, center, r):
        t = time.time() * 5
        for i in range(10):
            a = t + i * 0.55
            alpha = int(255 * (i + 1) / 10)
            p = (center[0] + r * math.cos(a), center[1] + r * math.sin(a))
            dot = pygame.Surface((self.U(14), self.U(14)), pygame.SRCALPHA)
            pygame.draw.circle(dot, (*PRIMARY, alpha), (self.U(7), self.U(7)), self.U(6))
            surf.blit(dot, dot.get_rect(center=p))


class WifiUI(Menu):
    def __init__(self, m, width, height):
        super().__init__(m, width, height)  # mode: list | keyboard | connecting | result | forget

        self.networks, self.current, self.ap = [], None, False
        self.loading, self.list_error = False, ""
        self.target, self.hidden, self.entry = None, False, "password"
        self.text, self.shift, self.symbols, self.show_pw, self.hint = "", False, False, False, ""
        self.conn_ssid, self.conn_pw, self.conn_start = None, "", 0
        self.result_ok, self.result_msg, self.result_time = False, "", 0
        self.forget_ssid = None
        self.field_rect = pygame.Rect(0, 0, 0, 0)
        self.spinner_pos = (0, 0)

    def animating(self):
        return (super().animating() or self.mode == "connecting" or self.loading
                or (self.mode == "result" and self.result_ok))

    def on_error(self, err):
        self.list_error = f"Wi-Fi servisine ulaşılamadı ({err.__class__.__name__})"
        self.loading = False

    def open(self):
        self.mode = "list"
        self.touch()
        self.refresh(rescan=False)
        self.refresh(rescan=True)

    def refresh(self, rescan):
        self.loading = True
        self.list_error = ""

        def load():
            d = self.api("/api/networks" + ("?rescan=1" if rescan else ""), timeout=40)
            self.networks, self.current, self.ap = d.get("networks", []), d.get("current"), d.get("ap", False)
            self.loading = False
        self.background(load)

    def select(self, net):
        if net.get("in_use"):
            return
        self.target, self.hidden = net, False
        if net.get("saved") or not net.get("security"):
            self.connect(net["ssid"], "")
        else:
            self.open_keyboard("password")

    def open_keyboard(self, entry, keep_text=False):
        self.mode, self.entry = "keyboard", entry
        if not keep_text:
            self.text = ""
        self.shift, self.symbols, self.show_pw, self.hint = False, False, False, ""

    def add_hidden(self):
        self.target, self.hidden = None, True
        self.open_keyboard("ssid")

    def key(self, k):
        if k == "⇧":
            self.shift = not self.shift
        elif k == "⌫":
            self.text = self.text[:-1]
        elif k in ("123", "abc"):
            self.symbols = not self.symbols
        elif k == "OK":
            self.submit()
        elif len(self.text) < 63:
            self.text += upper(k) if self.shift else k
            self.shift = False
        self.hint = ""

    def submit(self):
        if self.entry == "ssid":
            if not self.text.strip():
                self.hint = "Ağ adını yazın"
                return
            self.target = {"ssid": self.text.strip(), "security": "WPA2"}
            self.open_keyboard("password")
            self.hint = "Şifresiz ağ ise boş bırakıp Bağlan'a dokunun"
            return
        secure = bool(self.target.get("security")) and not self.hidden
        if (secure or self.text) and not 8 <= len(self.text) <= 63:
            self.hint = "Wi-Fi şifresi en az 8 karakter olmalı"
            return
        self.connect(self.target["ssid"], self.text)

    def connect(self, ssid, password):
        self.mode, self.conn_ssid, self.conn_pw, self.conn_start = "connecting", ssid, password, time.time()

        def run():
            try:
                poll()
            except Exception:
                self.finish(False, "Wi-Fi servisine ulaşılamadı")

        def poll():
            r = self.api("/api/connect", {"ssid": ssid, "password": password, "hidden": self.hidden})
            if r.get("http_error"):
                return self.finish(False, r.get("error", "Hata"))
            while time.time() - self.conn_start < 100:
                time.sleep(1)
                try:
                    c = self.api("/api/status", timeout=5).get("connect", {})
                except Exception:
                    continue  # ag degisirken servis kisa sure yanit vermeyebilir
                if c.get("ssid") == ssid and c.get("time", 0) >= self.conn_start - 1 and c.get("state") in ("ok", "error"):
                    return self.finish(c["state"] == "ok", c.get("message") or "")
            self.finish(False, "Zaman aşımı")
        self.background(run)

    def finish(self, ok, msg):
        self.mode, self.result_ok, self.result_msg, self.result_time = "result", ok, msg, time.time()
        self.version += 1

    def ask_forget(self, ssid):
        self.mode, self.forget_ssid = "forget", ssid

    def forget(self):
        ssid = self.forget_ssid
        self.mode = "list"
        self.loading = True

        def run():
            self.api("/api/forget", {"ssid": ssid})
            d = self.api("/api/networks", timeout=40)
            self.networks, self.current = d.get("networks", []), d.get("current")
            self.loading = False
        self.background(run)

    # ------------------------------------------------ cizim
    def layer_key(self):
        """Statik katmani etkileyen her sey; degismedikce katman yeniden cizilmez."""
        nets = tuple((n["ssid"], n.get("in_use"), n.get("saved"), n["signal"] // 34) for n in self.networks)
        return (self.mode, self.shift, self.symbols, self.entry, self.show_pw,
                self.target and self.target.get("ssid"), nets, self.loading, self.list_error, self.current,
                self.ap, self.result_ok, self.result_msg, self.conn_ssid, self.forget_ssid)

    def draw_list(self, surf):
        m, U = self.m, self.U
        box = self.list_box = self.modal(surf, 820, 680)
        m.text(surf, "Wi-Fi", m.font("Inter-SemiBold", U(36)), WHITE, (box.x + U(32), box.y + U(26)), shadow=False)
        self.button(surf, pygame.Rect(box.right - U(84), box.y + U(20), U(60), U(56)), icon="x",
                    cb=self.close, style="special")
        self.button(surf, pygame.Rect(box.right - U(156), box.y + U(20), U(60), U(56)), icon="arrow-clockwise",
                    cb=lambda: self.refresh(rescan=True), style="special", enabled=not self.loading and not self.ap)
        if self.loading:
            sub = "Ağlar aranıyor…"
        elif self.list_error:
            sub = self.list_error
        elif self.ap:
            sub = "Kurulum modu açık · son taranan ağlar"
        else:
            sub = f"Bağlı: {self.current}" if self.current else "Hiçbir ağa bağlı değil"
        m.text(surf, sub, m.font("Inter-Regular", U(21)), MUTED, (box.x + U(34), box.y + U(78)), shadow=False)

        row_h, y = U(66), box.y + U(122)
        rows = max(0, (box.bottom - U(100) - y) // row_h)
        for net in self.networks[:rows]:
            r = pygame.Rect(box.x + U(20), y, box.w - U(40), row_h - U(8))
            self.button(surf, r, cb=lambda n=net: self.select(n), style="ghost")
            level = 3 if net["signal"] > 66 else 2 if net["signal"] > 33 else 1
            surf.blit(m.wifi_icon(U(30), level), (r.x + U(14), r.centery - U(15)))
            right = r.right - U(14)
            if net.get("in_use"):
                chk = m.asset_icon("check", U(26))
                surf.blit(chk, chk.get_rect(midright=(right, r.centery)))
                right -= U(36)
            elif net.get("saved"):
                fb = pygame.Rect(0, 0, U(84), U(42))
                fb.midright = (right, r.centery)
                self.button(surf, fb, "Unut", cb=lambda s=net["ssid"]: self.ask_forget(s), style="special",
                            font_size=19)
                right = fb.x - U(12)
            if net.get("security"):
                lock = m.asset_icon("lock-simple", U(22), 150)
                surf.blit(lock, lock.get_rect(midright=(right, r.centery)))
                right -= U(32)
            name_f = m.font("Inter-Medium", U(25))
            name = m.fit_text(net["ssid"], name_f, right - (r.x + U(64)) - U(10))
            if net.get("wpa3_only"):
                m.text(surf, name, name_f, WHITE, (r.x + U(64), r.y + U(4)), shadow=False)
                m.text(surf, "WPA3 · desteklenmiyor olabilir", m.font("Inter-Regular", U(16)), (255, 170, 150),
                       (r.x + U(64), r.y + U(33)), shadow=False)
            else:
                m.text(surf, name, name_f, PRIMARY if net.get("in_use") else WHITE,
                       (r.x + U(64), r.centery), "midleft", shadow=False)
            pygame.draw.line(surf, (48, 56, 76), (r.x + U(64), r.bottom + U(3)), (r.right, r.bottom + U(3)))
            y += row_h
        if not self.networks and not self.loading:
            m.text(surf, "Ağ bulunamadı", m.font("Inter-Regular", U(24)), MUTED, (box.centerx, box.centery),
                   "center", shadow=False)
        self.button(surf, pygame.Rect(box.x + U(24), box.bottom - U(84), U(260), U(60)), "Gizli ağ ekle",
                    icon="plus", cb=self.add_hidden, style="special", font_size=21)

    def draw_keyboard(self, surf):
        m, U, W, H = self.m, self.U, self.w, self.h
        self.darken(surf, 215)
        top = pygame.Rect(U(32), U(24), W - U(64), U(236))
        self.card(surf, top, U(24))
        self.button(surf, pygame.Rect(top.x + U(18), top.y + U(18), U(60), U(54)), icon="caret-left",
                    cb=lambda: setattr(self, "mode", "list"), style="special")
        title = "Gizli ağın adı" if self.entry == "ssid" else f"“{self.target['ssid']}” şifresi"
        m.text(surf, m.fit_text(title, m.font("Inter-SemiBold", U(30)), top.w - U(140)),
               m.font("Inter-SemiBold", U(30)), WHITE, (top.x + U(94), top.y + U(45)), "midleft", shadow=False)

        ok_label = "İleri" if self.entry == "ssid" else "Bağlan"
        ok_rect = pygame.Rect(top.right - U(214), top.y + U(96), U(190), U(76))
        self.button(surf, ok_rect, ok_label, cb=self.submit, style="primary", font_size=27)
        field = self.field_rect = pygame.Rect(top.x + U(24), top.y + U(96), ok_rect.x - top.x - U(44), U(76))
        pygame.draw.rect(surf, (12, 16, 26), field, border_radius=U(14))
        pygame.draw.rect(surf, PRIMARY, field, width=2, border_radius=U(14))
        if self.entry == "password":
            self.button(surf, pygame.Rect(field.right - U(76), field.y + U(8), U(64), field.h - U(16)),
                        icon="eye-slash" if self.show_pw else "eye",
                        cb=lambda: setattr(self, "show_pw", not self.show_pw), style="ghost")
        self.draw_keys(surf, top)

    def draw_field(self, surf):
        """Her tusta yeniden cizilen tek kisim: yazilan metin, imlec ve ipucu."""
        m, U, field = self.m, self.U, self.field_rect
        masked = self.entry == "password" and not self.show_pw
        shown = "•" * len(self.text) if masked else self.text
        f = m.font("Inter-Medium", U(32))
        max_w = field.w - U(110)
        while shown and f.size(shown)[0] > max_w:
            shown = shown[1:]  # uzun metinde sonu gorunsun
        tx = m.text(surf, shown, f, WHITE, (field.x + U(20), field.centery), "midleft", shadow=False) \
            if shown else pygame.Rect(field.x + U(20), field.centery, 0, 0)
        pygame.draw.line(surf, PRIMARY, (tx.right + U(3), field.centery - U(20)),
                         (tx.right + U(3), field.centery + U(20)), max(2, U(3)))
        hint = self.hint or (f"{len(self.text)} karakter" if self.text else "")
        if hint:
            warn = self.hint and ("olmalı" in self.hint or "yazın" in self.hint)
            col = (255, 170, 150) if warn else MUTED
            m.text(surf, hint, m.font("Inter-Regular", U(20)), col, (field.x + U(6), field.bottom + U(14)),
                   shadow=False)

    def draw_keys(self, surf, top):
        U, W, H = self.U, self.w, self.h
        kb = pygame.Rect(U(32), top.bottom + U(16), W - U(64), H - top.bottom - U(40))
        self.card(surf, kb, U(24))
        gap = U(9)
        inner = kb.inflate(-U(24), -U(24))
        rows = SYMBOLS if self.symbols else LETTERS
        key_h = (inner.h - gap * 3) // 4
        unit = (inner.w - gap * 11) / 12
        for ri, row in enumerate(rows):
            units = sum(WIDE.get(k, 1) for k in row)
            x = inner.x + (12 - units) * (unit + gap) / 2
            y = inner.y + ri * (key_h + gap)
            for k in row:
                kw = WIDE.get(k, 1)
                rect = pygame.Rect(round(x), y, round(kw * unit + (kw - 1) * gap), key_h)
                special = k in ("⇧", "⌫", "123", "abc")
                if k == "OK":
                    self.button(surf, rect, "İleri" if self.entry == "ssid" else "Bağlan", cb=self.submit,
                                style="primary", font_size=26)
                elif k == "⇧":
                    self.button(surf, rect, icon="arrow-fat-up", cb=lambda: self.key("⇧"),
                                style="primary" if self.shift else "special")
                elif k == "⌫":
                    self.button(surf, rect, icon="backspace", cb=lambda: self.key("⌫"), style="special")
                else:
                    label = "boşluk" if k == " " else (upper(k) if self.shift else k)
                    self.button(surf, rect, label, cb=lambda c=k: self.key(c),
                                style="special" if special else "normal",
                                font_size=24 if special or k == " " else 34)
                x += kw * unit + kw * gap

    def draw_connecting(self, surf):
        m, U = self.m, self.U
        box = self.modal(surf, 620, 300, alpha=200)
        self.spinner_pos = (box.centerx, box.y + U(100))
        m.text(surf, m.fit_text(f"“{self.conn_ssid}” ağına bağlanılıyor…", m.font("Inter-Medium", U(27)), box.w - U(60)),
               m.font("Inter-Medium", U(27)), WHITE, (box.centerx, box.y + U(190)), "center", shadow=False)
        m.text(surf, "Bu işlem 30 saniye kadar sürebilir", m.font("Inter-Regular", U(19)), MUTED,
               (box.centerx, box.y + U(234)), "center", shadow=False)

    def draw_result(self, surf):
        m, U = self.m, self.U
        box = self.modal(surf, 660, 340, alpha=200)
        if self.result_ok:
            pygame.draw.circle(surf, (60, 180, 110), (box.centerx, box.y + U(96)), U(46))
            chk = m.asset_icon("check", U(56))
            surf.blit(chk, chk.get_rect(center=(box.centerx, box.y + U(96))))
            m.text(surf, "Bağlandı", m.font("Inter-SemiBold", U(34)), WHITE, (box.centerx, box.y + U(190)),
                   "center", shadow=False)
            m.text(surf, f"“{self.conn_ssid}” kaydedildi", m.font("Inter-Regular", U(21)), MUTED,
                   (box.centerx, box.y + U(236)), "center", shadow=False)
            return
        pygame.draw.circle(surf, DANGER, (box.centerx, box.y + U(80)), U(40))
        x = m.asset_icon("x", U(46))
        surf.blit(x, x.get_rect(center=(box.centerx, box.y + U(80))))
        m.text(surf, "Bağlanılamadı", m.font("Inter-SemiBold", U(32)), WHITE, (box.centerx, box.y + U(158)),
               "center", shadow=False)
        msg = self.result_msg or "Şifreyi kontrol edip tekrar deneyin"
        m.text(surf, m.fit_text(msg, m.font("Inter-Regular", U(20)), box.w - U(60)), m.font("Inter-Regular", U(20)),
               MUTED, (box.centerx, box.y + U(200)), "center", shadow=False)
        bw = (box.w - U(72)) // 2
        self.button(surf, pygame.Rect(box.x + U(24), box.bottom - U(92), bw, U(66)), "Kapat",
                    cb=lambda: setattr(self, "mode", "list"), style="special")
        can_retry = self.target is not None and (self.conn_pw or self.target.get("security"))
        self.button(surf, pygame.Rect(box.x + U(48) + bw, box.bottom - U(92), bw, U(66)), "Tekrar dene",
                    cb=lambda: self.open_keyboard("password", keep_text=True), style="primary", enabled=bool(can_retry))

    def draw_forget(self, surf):
        m, U = self.m, self.U
        box = self.modal(surf, 640, 320)
        m.text(surf, m.fit_text(f"“{self.forget_ssid}” unutulsun mu?", m.font("Inter-SemiBold", U(30)), box.w - U(60)),
               m.font("Inter-SemiBold", U(30)), WHITE, (box.centerx, box.y + U(70)), "center", shadow=False)
        note = ("Şu an bu ağa bağlısınız; bağlantı kesilir." if self.forget_ssid == self.current
                else "Pi bu ağa bir daha kendiliğinden bağlanmaz.")
        m.text(surf, note, m.font("Inter-Regular", U(21)), (255, 170, 150) if self.forget_ssid == self.current else MUTED,
               (box.centerx, box.y + U(130)), "center", shadow=False)
        bw = (box.w - U(72)) // 2
        self.button(surf, pygame.Rect(box.x + U(24), box.bottom - U(96), bw, U(68)), "Vazgeç",
                    cb=lambda: setattr(self, "mode", "list"), style="special")
        self.button(surf, pygame.Rect(box.x + U(48) + bw, box.bottom - U(96), bw, U(68)), "Unut",
                    cb=self.forget, style="danger")

    def before_render(self):
        if self.mode == "result" and self.result_ok and time.time() - self.result_time > 2.5:
            self.close()
            return True
        return False

    def draw_dynamic(self, surf):
        if self.mode == "keyboard":
            self.draw_field(surf)
        elif self.mode == "connecting":
            self.spinner(surf, self.spinner_pos, self.U(38))


# ---------------------------------------------------------------- Bluetooth menusu

# BlueZ simge turu -> assets/ simgesi
BT_ICONS = {"audio-headset": "headphones", "audio-headphones": "headphones", "audio-card": "speaker-high",
            "input-keyboard": "keyboard", "input-mouse": "mouse", "input-tablet": "mouse", "input-gaming": "mouse",
            "phone": "device-mobile"}
BT_BUSY = {"pair": "Eşleştiriliyor…", "connect": "Bağlanıyor…", "disconnect": "Ayrılıyor…", "remove": "Siliniyor…"}
BT_DONE = {"pair": "Eşleştirildi ve bağlandı", "connect": "Bağlandı", "disconnect": "Bağlantı kesildi",
           "remove": "Cihaz unutuldu"}
GREEN = (60, 180, 110)


class BtUI(Menu):
    """Bluetooth ac/kapat, cihaz arama, eslestirme, baglanma, unutma."""

    def __init__(self, m, width, height):
        super().__init__(m, width, height)  # mode: list | forget
        self.data = {"powered": False, "devices": [], "scanning": False, "op": {}, "name": "pi"}
        self.loaded, self.busy_power, self.opened_at = False, False, 0
        self.toast, self.toast_time, self.toast_ok = "", 0, True
        self.remove_dev = None
        self.seen_op = None
        self._poll_id = 0

    # ------------------------------------------------ veri
    def open(self):
        self.mode, self.loaded, self.opened_at = "list", False, time.time()
        self.touch()
        self.post("/api/bt/scan")
        self._poll_id += 1
        pid = self._poll_id

        def poll():  # menu acik oldugu surece 2 sn'de bir durum
            while self.mode is not None and pid == self._poll_id:
                try:
                    d = self.api("/api/bt", timeout=20)
                except Exception:
                    d = None
                if d:
                    op = d.get("op") or {}
                    if op.get("state") in ("ok", "error") and op.get("time", 0) > self.opened_at \
                            and op.get("time") != self.seen_op:
                        self.seen_op = op["time"]
                        self.show_toast(op)
                    self.data, self.loaded = d, True
                    self.version += 1
                time.sleep(2)
        threading.Thread(target=poll, daemon=True).start()

    def post(self, path, body=None):
        self.background(lambda: self.api(path, body or {}))

    def show_toast(self, op):
        ok = op["state"] == "ok"
        self.toast = BT_DONE.get(op.get("action"), "Tamam") if ok else (op.get("message") or "İşlem başarısız")
        self.toast_time, self.toast_ok = time.time(), ok

    def toast_visible(self):
        return bool(self.toast) and time.time() - self.toast_time < 4

    def busy(self):
        op = self.data.get("op") or {}
        return op.get("mac") if op.get("state") == "busy" else None

    # ------------------------------------------------ eylemler
    def toggle_power(self):
        on = not self.data["powered"]
        self.busy_power = True

        def run():
            try:
                self.api("/api/bt/power", {"on": on}, timeout=30)
                self.data = self.api("/api/bt", timeout=20)
            finally:
                self.busy_power = False
        self.background(run)

    def tap_device(self, dev):
        if self.busy():
            return
        action = "disconnect" if dev["connected"] else "connect" if dev["paired"] else "pair"
        self.data["op"] = {"state": "busy", "mac": dev["mac"], "action": action}
        self.post(f"/api/bt/{action}", {"mac": dev["mac"]})

    def ask_remove(self, dev):
        self.mode, self.remove_dev = "forget", dev

    def remove(self):
        self.data["op"] = {"state": "busy", "mac": self.remove_dev["mac"], "action": "remove"}
        self.post("/api/bt/remove", {"mac": self.remove_dev["mac"]})
        self.mode = "list"

    # ------------------------------------------------ cizim
    def animating(self):
        return super().animating() or self.busy_power or self.toast_visible()

    def layer_key(self):
        devs = tuple((d["mac"], d["name"], d["paired"], d["connected"], d["icon"]) for d in self.data["devices"])
        op = self.data.get("op") or {}
        return (self.mode, self.loaded, self.busy_power, self.data["powered"], self.data["scanning"], devs,
                op.get("state"), op.get("mac"), self.toast if self.toast_visible() else "",
                self.remove_dev and self.remove_dev["mac"])

    def switch(self, surf, rect, on):
        pygame.draw.rect(surf, PRIMARY if on else (70, 78, 98), rect, border_radius=rect.h // 2)
        r = rect.h // 2 - self.U(5)
        cx = rect.right - rect.h // 2 if on else rect.x + rect.h // 2
        pygame.draw.circle(surf, WHITE, (cx, rect.centery), r)
        self.buttons.append((rect, self.toggle_power))

    def device_icon(self, dev):
        if "watch" in dev["name"].lower():
            return "watch"
        return BT_ICONS.get(dev["icon"], "bluetooth")

    def draw_list(self, surf):
        m, U = self.m, self.U
        box = self.list_box = self.modal(surf, 820, 680)
        m.text(surf, "Bluetooth", m.font("Inter-SemiBold", U(36)), WHITE, (box.x + U(32), box.y + U(26)),
               shadow=False)
        self.button(surf, pygame.Rect(box.right - U(84), box.y + U(20), U(60), U(56)), icon="x",
                    cb=self.close, style="special")
        powered = self.data["powered"]
        if not self.busy_power:
            self.switch(surf, pygame.Rect(box.right - U(196), box.y + U(26), U(92), U(46)), powered)
        if not self.loaded:
            sub = "Yükleniyor…"
        elif self.busy_power:
            sub = "Kapatılıyor…" if powered else "Açılıyor…"
        elif not powered:
            sub = "Kapalı"
        elif self.data["scanning"]:
            sub = "Açık · cihazlar aranıyor…"
        else:
            sub = f"Açık · Pi'nin görünen adı: {self.data.get('name', 'pi')}"
        m.text(surf, sub, m.font("Inter-Regular", U(21)), MUTED, (box.x + U(34), box.y + U(78)), shadow=False)

        if self.loaded and not powered and not self.busy_power:
            m.text(surf, "Bluetooth kapalı", m.font("Inter-Medium", U(28)), MUTED, (box.centerx, box.centery - U(40)),
                   "center", shadow=False)
            self.button(surf, pygame.Rect(box.centerx - U(110), box.centery + U(10), U(220), U(66)), "Aç",
                        icon="bluetooth", cb=self.toggle_power, style="primary", font_size=26)
        elif powered:
            self.draw_devices(surf, box)
            scanning = self.data["scanning"]
            self.button(surf, pygame.Rect(box.x + U(24), box.bottom - U(84), U(220), U(60)),
                        "Aranıyor…" if scanning else "Ara", icon="magnifying-glass",
                        cb=lambda: self.post("/api/bt/scan"), style="special", enabled=not scanning, font_size=21)
        if self.toast_visible():
            f = m.font("Inter-Medium", U(21))
            w = f.size(self.toast)[0] + U(40)
            r = pygame.Rect(box.right - U(24) - w, box.bottom - U(80), w, U(52))
            pygame.draw.rect(surf, GREEN if self.toast_ok else DANGER, r, border_radius=r.h // 2)
            m.text(surf, self.toast, f, WHITE, r.center, "center", shadow=False)

    def draw_devices(self, surf, box):
        m, U = self.m, self.U
        devices = self.data["devices"]
        busy = self.busy()
        op = self.data.get("op") or {}
        y, bottom = box.y + U(118), box.bottom - U(100)
        sections = [("Cihazlarım", [d for d in devices if d["paired"]]),
                    ("Yakındaki cihazlar", [d for d in devices if not d["paired"]])]
        row_h = U(62)
        for title, devs in sections:
            if not devs or y + U(34) + row_h > bottom:
                continue
            m.text(surf, title.upper(), m.font("Inter-SemiBold", U(16)), MUTED, (box.x + U(34), y + U(6)),
                   shadow=False)
            y += U(34)
            for dev in devs:
                if y + row_h > bottom:
                    break
                r = pygame.Rect(box.x + U(20), y, box.w - U(40), row_h - U(6))
                self.button(surf, r, cb=lambda d=dev: self.tap_device(d), style="ghost")
                ico = m.asset_icon(self.device_icon(dev), U(28), 255 if dev["connected"] else 150)
                surf.blit(ico, ico.get_rect(midleft=(r.x + U(14), r.centery)))
                right = r.right - U(14)
                if dev["paired"]:
                    fb = pygame.Rect(0, 0, U(84), U(42))
                    fb.midright = (right, r.centery)
                    self.button(surf, fb, "Unut", cb=lambda d=dev: self.ask_remove(d), style="special", font_size=19)
                    right = fb.x - U(14)
                if busy == dev["mac"]:
                    status, col = BT_BUSY.get(op.get("action"), "…"), PRIMARY
                elif dev["connected"]:
                    status, col = "Bağlı", PRIMARY
                elif dev["paired"]:
                    status, col = "Bağlı değil", MUTED
                else:
                    status, col = "Eşleştir", MUTED
                sf = m.font("Inter-Regular", U(19))
                srect = m.text(surf, status, sf, col, (right, r.centery), "midright", shadow=False)
                nf = m.font("Inter-Medium", U(24))
                name = m.fit_text(dev["name"], nf, srect.x - (r.x + U(58)) - U(16))
                m.text(surf, name, nf, WHITE, (r.x + U(58), r.centery), "midleft", shadow=False)
                y += row_h
            y += U(8)
        if not devices:
            m.text(surf, "Yakında cihaz bulunamadı", m.font("Inter-Medium", U(24)), MUTED,
                   (box.centerx, box.centery - U(20)), "center", shadow=False)
            m.text(surf, "Cihazınızı eşleştirme moduna alıp Ara'ya dokunun", m.font("Inter-Regular", U(19)), MUTED,
                   (box.centerx, box.centery + U(16)), "center", shadow=False)

    def draw_forget(self, surf):
        m, U = self.m, self.U
        box = self.modal(surf, 640, 320)
        dev = self.remove_dev
        m.text(surf, m.fit_text(f"“{dev['name']}” unutulsun mu?", m.font("Inter-SemiBold", U(30)), box.w - U(60)),
               m.font("Inter-SemiBold", U(30)), WHITE, (box.centerx, box.y + U(70)), "center", shadow=False)
        m.text(surf, "Tekrar kullanmak için yeniden eşleştirmeniz gerekir.", m.font("Inter-Regular", U(21)), MUTED,
               (box.centerx, box.y + U(130)), "center", shadow=False)
        bw = (box.w - U(72)) // 2
        self.button(surf, pygame.Rect(box.x + U(24), box.bottom - U(96), bw, U(68)), "Vazgeç",
                    cb=lambda: setattr(self, "mode", "list"), style="special")
        self.button(surf, pygame.Rect(box.x + U(48) + bw, box.bottom - U(96), bw, U(68)), "Unut",
                    cb=self.remove, style="danger")


# ---------------------------------------------------------------- internet radyosu sayfasi

# radio-browser.info'dan secilmis MP3 yayinlari: (ad, adres, kapak renk gecisi)
STATIONS = [  # (ad, yayin adresi, rozet etiketi)
    ("Kral Türk FM", "https://live.radyositesihazir.com/8032/stream", "KT"),
    ("Super FM", "https://playerservices.streamtheworld.com/api/livestream-redirect/SUPER_FM_SC", "S"),
    ("Virgin Radio", "http://29073.live.streamtheworld.com/VIRGIN_RADIO_SC", "V"),
    ("Joy Türk", "https://playerservices.streamtheworld.com/api/livestream-redirect/JOY_TURK_SC", "JT"),
    ("Radyo 7", "http://46.20.3.250/;stream", "R7"),
    ("Damar Türk FM", "https://live.radyositesihazir.com:10997/", "DT"),
    ("Türkülerle Türkiye", "http://37.247.98.8/stream/22/;", "TT"),
    ("90'lar Türkiye", "http://37.247.98.8/stream/166/", "90"),
    ("Ankara Havaları", "http://37.247.98.8/stream/30/;", "AH"),
    ("Radyo Şiran", "https://live.radyositesihazir.com/8078/stream", "RŞ"),
]
VOLUME_STEP = 13  # bluealsa 0-127 -> ~%10
ICY_RE = re.compile(rb"StreamTitle='(.*?)';")


def bt_sink():
    """Bagli Bluetooth ses cihazi: (mac, bluealsa PCM yolu) ya da (None, None)."""
    try:
        out = subprocess.run(["bluealsa-cli", "list-pcms"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None, None
    for line in out.splitlines():
        m = re.search(r"dev_([0-9A-F_]{17})/a2dpsrc/sink", line)
        if m:
            return m.group(1).replace("_", ":"), line.strip()
    return None, None


def parse_track(raw):
    """"Sanatci - Sarki" -> (sarki, sanatci). Bazi yayinlar ayni metni iki kez yollar; tekrari ayikla."""
    text = raw.decode("utf-8", "replace").strip(" -")
    if not text:
        return None
    half = text.split(" - ")
    if len(half) == 2 and half[0].strip() == half[1].strip():
        return half[0].strip(), ""
    if len(half) >= 2:
        return " - ".join(half[1:]).strip(), half[0].strip()
    return text, ""


_badge_cache = {}


def station_badge(i, size, m, active):
    """Istasyon rozeti: tek renk. Normalde saydam cam daire + beyaz harf; calarken beyaz daire + koyu harf."""
    key = (i, size, active)
    if key not in _badge_cache:
        big = size * 3  # buyuk cizip kucult: kenar yumusak olsun
        surf = pygame.Surface((big, big), pygame.SRCALPHA)
        c = (big // 2, big // 2)
        if active:
            pygame.draw.circle(surf, (255, 255, 255, 255), c, big // 2)
        else:
            pygame.draw.circle(surf, (255, 255, 255, 26), c, big // 2)
            pygame.draw.circle(surf, (255, 255, 255, 60), c, big // 2, width=3)
        label = STATIONS[i][2]
        f = m.font("Inter-SemiBold", int(big * (0.36 if len(label) > 1 else 0.44)))
        t = f.render(label, True, (18, 18, 22) if active else (255, 255, 255))
        surf.blit(t, t.get_rect(center=(c[0], c[1] + big // 60)))
        _badge_cache[key] = pygame.transform.smoothscale(surf, (size, size))
    return _badge_cache[key]


class RadioUI(Menu):
    """Ana ekranla ayni duzende radyo sayfasi: solda calan, sagda ses karti, altta istasyonlar."""

    auto_close = False  # sayfa; kendiliginden kapanmasin

    def __init__(self, m, width, height):
        super().__init__(m, width, height)  # mode: page
        self.proc, self.playing, self.status, self.last = None, None, "", 0
        self.track = None  # (sarki, sanatci) - yayin gonderiyorsa
        self.volume, self.muted = 127, False
        self.sink_mac, self.sink_pcm, self.sink_name = None, None, ""
        self.dragging, self.drag_end = False, 0
        self.bar_rect = pygame.Rect(0, 0, 0, 0)
        self.card_rect = pygame.Rect(0, 0, 0, 0)
        self.eq_spots = []
        self._vol_pending, self._vol_worker = None, False
        self._dark_icons = {}
        self.backdrop = None  # dashboard verir: duvar kagidi + gecis (saat/hava durumu olmadan)

    def open(self):
        self.mode = "page"
        self.touch()
        self.background(self.refresh_sink)

    def refresh_sink(self):
        self.sink_mac, self.sink_pcm = bt_sink()
        self.sink_name = ""
        if self.sink_mac:
            out = subprocess.run(["bluetoothctl", "info", self.sink_mac], capture_output=True, text=True,
                                 timeout=5).stdout
            name = re.search(r"Name: (.*)", out)
            self.sink_name = name.group(1).strip() if name else self.sink_mac
            vol = subprocess.run(["bluealsa-cli", "volume", self.sink_pcm], capture_output=True, text=True,
                                 timeout=5).stdout
            m = re.search(r"L: (\d+)", vol)
            if m:
                self.volume = int(m.group(1))

    # ------------------------------------------------ calma
    def play(self, i):
        self.stop()
        self.playing, self.last, self.status, self.track = i, i, "Bağlanıyor…", None

        def run():
            self.refresh_sink()
            if not self.sink_mac:
                self.playing, self.status = None, "Bluetooth kulaklık / hoparlör bağlı değil"
                return
            # -v: yayindaki sarki adlarini (ICY-META) stderr'e yazar
            proc = subprocess.Popen(["mpg123", "-v", "-o", "alsa", "-a",
                                     f"bluealsa:DEV={self.sink_mac},PROFILE=a2dp", STATIONS[i][1]],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.proc = proc
            threading.Thread(target=self.read_meta, args=(proc,), daemon=True).start()
            time.sleep(4)
            if proc is not self.proc:
                return  # bu arada baska istasyon secildi
            self.status = "Çalıyor" if proc.poll() is None else "Yayın açılamadı"
            if proc.poll() is not None:
                self.playing = None
            self.version += 1
            proc.wait()
            if proc is self.proc:  # kendiliginden bitti (yayin koptu, kulaklik ayrildi)
                self.proc, self.playing, self.status = None, None, "Yayın kesildi"
                self.version += 1
        self.background(run)

    def read_meta(self, proc):
        buf = b""
        while True:
            chunk = os.read(proc.stderr.fileno(), 4096)
            if not chunk:
                return
            buf = (buf + chunk)[-8192:]
            found = ICY_RE.findall(buf)
            if found and proc is self.proc:
                track = parse_track(found[-1])
                if track != self.track:
                    self.track = track
                    self.version += 1
                buf = buf[buf.rfind(b"StreamTitle"):][20:]

    def stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
        self.playing, self.status, self.track = None, "", None
        self.version += 1

    def toggle_play(self):
        self.stop() if self.playing is not None else self.play(self.last)

    def step_station(self, d):
        """Onceki / sonraki istasyon (calmiyorsa yalniz secim degisir)."""
        i = ((self.playing if self.playing is not None else self.last) + d) % len(STATIONS)
        if self.playing is not None:
            self.play(i)
        else:
            self.last, self.status = i, ""
            self.version += 1

    def set_volume(self, v):
        """Ekranda hemen gunceller; bluealsa'ya en son degeri en fazla 10 kez/sn gonderir."""
        self.volume, self.muted = max(0, min(127, v)), False
        self.version += 1
        if not self.sink_pcm:
            return
        self._vol_pending = self.volume
        if self._vol_worker:
            return
        self._vol_worker = True

        def run():
            try:
                subprocess.run(["bluealsa-cli", "mute", self.sink_pcm, "off"], timeout=5)
                while self._vol_pending is not None:
                    vol, self._vol_pending = str(self._vol_pending), None
                    subprocess.run(["bluealsa-cli", "volume", self.sink_pcm, vol, vol], timeout=5)
                    time.sleep(0.1)
            finally:
                self._vol_worker = False
        threading.Thread(target=run, daemon=True).start()

    def volume_at(self, x):
        bar = self.bar_rect
        return round(max(0, min(1, (x - bar.x) / max(1, bar.w))) * 127)

    def toggle_mute(self):
        self.muted = not self.muted
        if self.sink_pcm:
            pcm, state = self.sink_pcm, "on" if self.muted else "off"
            self.background(lambda: subprocess.run(["bluealsa-cli", "mute", pcm, state], timeout=5))

    # ------------------------------------------------ cizim
    frame_interval = 0.2  # calarken ses cubuklari animasyonu ~5 kare/sn (islemciyi yormasin)

    def animating(self):
        return super().animating() or self.status == "Bağlanıyor…" or self.playing is not None or self.dragging

    def layer_key(self):
        # ses duzeyi ve animasyon burada yok: her karede ayrica cizilir
        return (self.mode, self.playing, self.status, self.muted, self.sink_mac, self.sink_name, self.track)

    def dark_icon(self, name, size):
        key = (name, size)
        if key not in self._dark_icons:
            ico = self.m.asset_icon(name, size).copy()
            ico.fill((18, 18, 22, 255), special_flags=pygame.BLEND_RGBA_MULT)
            self._dark_icons[key] = ico
        return self._dark_icons[key]

    def draw_page(self, surf):
        m, U, W, H = self.m, self.U, self.w, self.h
        if self.backdrop:
            surf.blit(self.backdrop(), (0, 0))
        self.eq_spots = []
        playing = self.playing is not None

        # ---- sol ust (saatin yeri): istasyon, sarki, sanatci
        if playing:
            name = STATIONS[self.playing][0]
        else:
            name = STATIONS[self.last][0] if self.status else "Radyo"
        big = m.font("InterDisplay-Light", U(112))
        nrect = m.text(surf, m.fit_text(name, big, U(660)), big, WHITE, (U(48), U(24)))
        if playing and self.status == "Çalıyor":
            self.eq_spots.append((nrect.right + U(22), nrect.bottom - U(34), U(42), 4, U(7)))
        y = nrect.bottom + U(4)
        if self.track and playing:
            song, artist = self.track
            f = m.font("Inter-Medium", U(40))
            r = m.text(surf, m.fit_text(song, f, U(660)), f, WHITE, (U(58), y))
            if artist:
                m.text(surf, m.fit_text(artist, m.font("Inter-Regular", U(28)), U(660)),
                       m.font("Inter-Regular", U(28)), SOFT, (U(60), r.bottom + U(10)))
        else:
            sub = self.status or "Aşağıdan bir istasyon seçin"
            warn = sub in ("Yayın açılamadı", "Yayın kesildi") or sub.startswith("Bluetooth")
            m.text(surf, sub, m.font("Inter-Medium", U(34)), (255, 170, 150) if warn else WHITE, (U(58), y))

        # ---- sag ust (hava durumu kartinin yeri): cikis cihazi, onceki / oynat-durdur / sonraki, ses
        card = pygame.Rect(W - U(56) - U(470), U(52), U(470), U(300))
        self.card_rect = card
        m.panel(surf, card)
        f = m.font("Inter-Medium", U(22))
        ico = m.asset_icon("headphones" if self.sink_mac else "speaker-x", U(24), 255 if self.sink_mac else 150)
        surf.blit(ico, (card.x + U(28), card.y + U(26)))
        label = m.fit_text(self.sink_name, f, card.w - U(100)) if self.sink_mac else "Ses cihazı bağlı değil"
        m.text(surf, label, f, WHITE if self.sink_mac else SOFT, (card.x + U(64), card.y + U(25)), shadow=False)
        cy = card.y + U(132)
        btn = pygame.Rect(0, 0, U(104), U(104))
        btn.center = (card.centerx, cy)
        pygame.draw.circle(surf, WHITE, btn.center, btn.w // 2)
        ico = self.dark_icon("stop" if playing else "play", U(42))
        surf.blit(ico, ico.get_rect(center=(btn.centerx + (0 if playing else U(4)), btn.centery)))
        self.buttons.append((btn, self.toggle_play))
        for dx, name, d in ((-U(140), "skip-back", -1), (U(140), "skip-forward", 1)):
            r = pygame.Rect(0, 0, U(80), U(80))
            r.center = (card.centerx + dx, cy)
            ico = m.asset_icon(name, U(34), 235)
            surf.blit(ico, ico.get_rect(center=r.center))
            self.buttons.append((r, lambda d=d: self.step_station(d)))
        vy = card.y + U(250)
        lo = pygame.Rect(card.x + U(16), vy - U(24), U(48), U(48))
        self.buttons.append((lo, self.toggle_mute))
        ico = m.asset_icon("speaker-x" if self.muted else "speaker-low", U(26), 220)
        surf.blit(ico, ico.get_rect(center=lo.center))
        hi = m.asset_icon("speaker-high", U(26), 220)
        hi_rect = hi.get_rect(midright=(card.right - U(28), vy))
        surf.blit(hi, hi_rect)
        self.bar_rect = pygame.Rect(lo.right + U(12), vy - U(3), hi_rect.x - lo.right - U(30), U(6))

        # ---- alt (6 gunluk tahminin yeri): istasyonlar, 2 x 5
        fc = pygame.Rect(U(56), H - U(56) - U(196), W - U(112), U(196))
        m.panel(surf, fc)
        cols, rows = 5, 2
        cw, ch = (fc.w - U(16)) // cols, (fc.h - U(16)) // rows
        nf = m.font("Inter-SemiBold", U(22))
        for i, (sname, _, _) in enumerate(STATIONS):
            r = pygame.Rect(fc.x + U(8) + (i % cols) * cw, fc.y + U(8) + (i // cols) * ch, cw, ch)
            active = i == self.playing
            if active:
                hl = pygame.Surface(r.size, pygame.SRCALPHA)
                pygame.draw.rect(hl, (255, 255, 255, 26), hl.get_rect(), border_radius=U(18))
                surf.blit(hl, r.topleft)
            badge = station_badge(i, U(52), m, active)
            surf.blit(badge, badge.get_rect(midleft=(r.x + U(16), r.centery)))
            tx = r.x + U(80)
            avail = r.right - tx - (U(34) if active else U(8))
            lines = [sname]
            if nf.size(sname)[0] > avail and " " in sname:  # uzun ad: iki satir
                words = sname.split()
                k = max(1, len(words) // 2)
                lines = [" ".join(words[:k]), " ".join(words[k:])]
            lh = nf.get_linesize()
            ty = r.centery - lh * len(lines) // 2
            for line in lines:
                m.text(surf, m.fit_text(line, nf, avail), nf, WHITE if active else SOFT, (tx, ty), shadow=False)
                ty += lh
            if active and self.status == "Çalıyor":
                self.eq_spots.append((r.right - U(30), r.centery + U(9), U(18), 3, U(6)))
            self.buttons.append((r, lambda i=i: self.stop() if i == self.playing else self.play(i)))

        # ---- alt kenar: ana ekrana donus
        f = m.font("Inter-Medium", U(20))
        back = m.text(surf, "‹  Ana ekran", f, WHITE, (U(58), H - U(18)), "bottomleft")
        self.buttons.append((back.inflate(U(40), U(36)), self.close))
        m.text(surf, "Ana ekrana dönmek için sağa kaydırın", f, SOFT, (W - U(56), H - U(18)), "bottomright")

    def draw_dynamic(self, surf):
        """Her karede: ses yuzdesi, ses cubugu ve calarken hareketli ses cubuklari."""
        m, U, bar = self.m, self.U, self.bar_rect
        pct = "Sessiz" if self.muted else f"%{round(self.volume / 127 * 100)}"
        m.text(surf, pct, m.font("Inter-Medium", U(20)), SOFT, (bar.right, bar.y - U(16)), "bottomright",
               shadow=False)
        dim = pygame.Surface(bar.size, pygame.SRCALPHA)
        pygame.draw.rect(dim, (255, 255, 255, 70), dim.get_rect(), border_radius=bar.h // 2)
        surf.blit(dim, bar.topleft)
        fill = bar.copy()
        fill.w = int(bar.w * self.volume / 127)
        if fill.w and not self.muted:
            pygame.draw.rect(surf, WHITE, fill, border_radius=bar.h // 2)
        knob_r = U(16 if self.dragging else 13)
        pygame.draw.circle(surf, (0, 0, 0), (bar.x + fill.w, bar.centery + U(2)), knob_r + U(1))  # golge
        pygame.draw.circle(surf, WHITE, (bar.x + fill.w, bar.centery), knob_r)
        if self.playing is not None and self.status == "Çalıyor":
            t = time.time()
            for x, y, h, n, step in self.eq_spots:
                bw = max(U(3), step * 2 // 3)
                for k in range(n):
                    bh = max(U(3), int(h * (0.35 + 0.65 * abs(math.sin(t * (2.6 + k * 0.9) + k * 1.3)))))
                    pygame.draw.rect(surf, WHITE, (x + k * step, y - bh, bw, bh), border_radius=bw // 2)

    def handle(self, kind, x, y):
        on_bar = self.bar_rect.inflate(self.U(30), self.U(60)).collidepoint(x, y)
        if kind == "down" and on_bar:
            self.touch()
            self.dragging = True
            self.set_volume(self.volume_at(x))
            return
        if self.dragging:
            self.touch()
            if kind == "move":
                self.set_volume(self.volume_at(x))
            elif kind == "up":
                self.set_volume(self.volume_at(x))
                self.dragging, self.drag_end = False, time.time()
            return
        super().handle(kind, x, y)

    def swipe_allowed(self):
        """Ses cubugunu surukleyip birakmak sayfa degistirme sayilmasin."""
        return not self.dragging and time.time() - self.drag_end > 0.4


# ---------------------------------------------------------------- ayarlar (ustten asagi cekilen panel)

SETTINGS_DEFAULTS = {"evening_mode": True, "evening_start": "17:00", "night_mode": True, "night_start": "00:00",
                     "night_end": "09:00"}


class SettingsUI(Menu):
    """Ust kenardan asagi kaydirinca acilan panel: kisayollar, aksam modu, gece modu ve saatleri."""

    def __init__(self, m, width, height, cfg, path):
        super().__init__(m, width, height)  # mode: list
        self.cfg, self.path, self.request = cfg, path, None

    def open(self):
        self.mode, self.request = "list", None
        self.touch()

    def save(self, **kw):
        self.cfg.update(kw)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            data.update(kw)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass
        self.version += 1

    def shift(self, key, minutes):
        h, mi = map(int, self.cfg[key].split(":"))
        t = (h * 60 + mi + minutes) % 1440
        self.save(**{key: f"{t // 60:02d}:{t % 60:02d}"})

    def go(self, what):
        self.request = what
        self.close()

    def layer_key(self):
        return (self.mode,) + tuple(self.cfg[k] for k in SETTINGS_DEFAULTS)

    def switch(self, surf, rect, on):
        pygame.draw.rect(surf, PRIMARY if on else (70, 78, 98), rect, border_radius=rect.h // 2)
        cx = rect.right - rect.h // 2 if on else rect.x + rect.h // 2
        pygame.draw.circle(surf, WHITE, (cx, rect.centery), rect.h // 2 - self.U(5))

    def draw_list(self, surf):
        m, U, W = self.m, self.U, self.w
        self.darken(surf, 120)
        box = self.list_box = pygame.Rect(0, 0, U(780), U(610))
        box.midtop = (W // 2, -U(32))  # ust koseler ekran disinda: yukaridan inen panel
        self.card(surf, box, U(32))
        pygame.draw.rect(surf, (90, 98, 120), (box.centerx - U(32), box.bottom - U(18), U(64), U(6)),
                         border_radius=U(3))
        x0, x1, y = box.x + U(36), box.right - U(36), U(28)
        m.text(surf, "Ayarlar", m.font("Inter-SemiBold", U(36)), WHITE, (x0, y), shadow=False)
        self.button(surf, pygame.Rect(x1 - U(60), y - U(4), U(60), U(56)), icon="x", cb=self.close, style="special")
        y += U(76)

        # kisayollar
        gap = U(16)
        bw = (x1 - x0 - 2 * gap) // 3
        for k, (label, icon, req) in enumerate((("Wi-Fi", "wifi-high", "wifi"), ("Bluetooth", "bluetooth", "bt"),
                                                ("Radyo", "radio", "radio"))):
            self.button(surf, pygame.Rect(x0 + k * (bw + gap), y, bw, U(64)), label, icon=icon,
                        cb=lambda r=req: self.go(r), style="special", font_size=22)
        y += U(88)

        def row(title, sub, key):
            nonlocal y
            pygame.draw.line(surf, (44, 52, 72), (x0, y), (x1, y))
            r = pygame.Rect(x0, y, x1 - x0, U(84))
            m.text(surf, title, m.font("Inter-Medium", U(26)), WHITE, (x0, r.y + U(14)), shadow=False)
            m.text(surf, sub, m.font("Inter-Regular", U(19)), MUTED, (x0, r.y + U(50)), shadow=False)
            self.switch(surf, pygame.Rect(x1 - U(92), r.centery - U(23), U(92), U(46)), self.cfg[key])
            self.buttons.append((r, lambda: self.save(**{key: not self.cfg[key]})))
            y += U(84)

        row("Akşam modu", f"Saat {self.cfg['evening_start']} – sabah: koyu tema ve ay bilgisi", "evening_mode")
        row("Gece modu", "Ekranı karartır, yalnızca saat görünür", "night_mode")

        # gece saatleri: [-] 00:00 [+]   [-] 09:00 [+]
        pygame.draw.line(surf, (44, 52, 72), (x0, y), (x1, y))
        on = self.cfg["night_mode"]
        half = (x1 - x0) // 2
        for k, (label, key) in enumerate((("Başlangıç", "night_start"), ("Bitiş", "night_end"))):
            gx = x0 + k * (half + U(10))
            m.text(surf, label, m.font("Inter-Regular", U(19)), MUTED, (gx, y + U(14)), shadow=False)
            cy = y + U(66)
            self.button(surf, pygame.Rect(gx, cy - U(26), U(60), U(52)), icon="minus",
                        cb=lambda key=key: self.shift(key, -30), style="special", enabled=on)
            m.text(surf, self.cfg[key], m.font("Inter-SemiBold", U(32)), WHITE if on else MUTED,
                   (gx + U(140), cy), "center", shadow=False)
            self.button(surf, pygame.Rect(gx + U(220), cy - U(26), U(60), U(52)), icon="plus",
                        cb=lambda key=key: self.shift(key, 30), style="special", enabled=on)
        y += U(112)
        self.button(surf, pygame.Rect(x0, y, U(330), U(56)), "Gece ekranını göster", cb=lambda: self.go("night"),
                    style="special", font_size=21)
        m.text(surf, "Dokununca 1 dk normal ekran açılır", m.font("Inter-Regular", U(17)), MUTED,
               (x1, y + U(28)), "midright", shadow=False)


# ---------------------------------------------------------------- YouTube Music sayfasi

YT_NOW = "/run/ytcast/now.json"        # ytcast/receiver.mjs yazar
YT_API = "http://127.0.0.1:8100"       # ayni alicinin kontrol adresi
YT_RED = (255, 78, 69)


class YtUI(Menu):
    """Telefondan YouTube Music'le 'Pi Panel'e yayinlanan sarki: kapak, ilerleme, onceki/oynat/sonraki, ses.
    YouTube uygulamasindan video yayinlaninca kapak yerine video oynar (sayfa kendiliginden acilir).
    Ana ekranda saga kaydirinca acilir; duzen ana ekranla ayni."""

    auto_close = False
    frame_interval = 0.5  # ilerleme cubugu ve yeni sarki icin

    def __init__(self, m, width, height):
        super().__init__(m, width, height)  # mode: page
        self.backdrop = None
        self.st, self.st_mtime = {}, None
        self.art = (None, None)  # (url, yuzey)
        self.dragging, self.drag_end, self.drag_vol = False, 0, None
        self.seeking, self.seek_pos = False, None  # ilerleme cubugunda surukleme (ms)
        self.bar_rect = self.prog_rect = pygame.Rect(0, 0, 0, 0)
        self.sink, self.sink_checked, self.sink_busy, self.sink_names = None, 0, False, {}  # ses cikisi (alt kenar)

    def open(self):
        self.mode = "page"
        self.touch()

    def animating(self):
        return self.active

    # ------------------------------------------------ veri
    def refresh(self):
        try:
            mtime = os.stat(YT_NOW).st_mtime
            if mtime != self.st_mtime:
                with open(YT_NOW, encoding="utf-8") as f:
                    self.st, self.st_mtime = json.load(f), mtime
        except (OSError, ValueError):
            self.st = {}
        url = self.st.get("cover")
        if url and self.art[0] != url:
            self.art = (url, None)
            size = self.U(300)

            def fetch():
                req = urllib.request.Request(url, headers={"User-Agent": "pi-dashboard/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                path = "/tmp/yt_cover.jpg"
                with open(path, "wb") as f:
                    f.write(data)
                img = self.m.cover(pygame.image.load(path), size, size)
                rounded = pygame.Surface((size, size), pygame.SRCALPHA)
                rounded.blit(img, (0, 0))
                mask = pygame.Surface((size, size), pygame.SRCALPHA)
                pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=self.U(22))
                rounded.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
                if self.art[0] == url:
                    self.art = (url, rounded)
            self.background(fetch)

    def playing(self):
        return self.st.get("state") == "playing"

    def check_sink(self):
        """Bagli Bluetooth kulaklik adi ("" = bagli degil); 5 sn'de bir arka planda."""
        if self.sink_busy or time.time() - self.sink_checked < 5:
            return
        self.sink_checked, self.sink_busy = time.time(), True

        def run():
            try:
                mac, _ = bt_sink()
                if mac and mac not in self.sink_names:
                    out = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True,
                                         timeout=5).stdout
                    found = re.search(r"^\s*Name: (.+)$", out, re.M)
                    self.sink_names[mac] = found.group(1).strip() if found else mac
                self.sink = self.sink_names.get(mac, "") if mac else ""
            finally:
                self.sink_busy = False
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------ video
    def video_mode(self):
        return self.active and bool(self.st.get("video")) and self.has_track()

    def video_rect(self):
        U = self.U
        return pygame.Rect(U(56), U(100), U(768), U(432))

    def video_radius(self):
        return self.U(22)

    def has_track(self):
        return bool(self.st.get("name")) and self.st.get("state") in ("playing", "paused", "loading")

    def position(self):
        if self.seek_pos is not None:
            return self.seek_pos
        pos = self.st.get("position", 0)
        if self.playing():
            pos += (time.time() - self.st.get("time", time.time())) * 1000
        return max(0, min(pos, self.st.get("duration") or pos))

    def cmd(self, path):
        def run():
            urllib.request.urlopen(urllib.request.Request(YT_API + path, data=b""), timeout=10).read()
        self.background(run)

    def volume(self):
        return self.drag_vol if self.drag_vol is not None else self.st.get("volume", 50)

    # ------------------------------------------------ cizim
    def layer_key(self):
        self.refresh()
        self.check_sink()
        st = self.st
        return (self.mode, st.get("state"), st.get("name"), tuple(st.get("artists", [])), st.get("album"),
                self.art[0], self.art[1] is not None, st.get("muted"), st.get("video"), self.sink)

    def draw_page(self, surf):
        m, U, W, H = self.m, self.U, self.w, self.h
        if self.backdrop:
            surf.blit(self.backdrop(), (0, 0))
        st, track = self.st, self.has_track()
        if track and st.get("video"):
            self.draw_video_page(surf)
        else:
            self.draw_music_page(surf)
        self.draw_controls(surf)

    def draw_video_page(self, surf):
        """Solda video, sagda baslik; kontroller altta (muzik sayfasiyla ayni)."""
        m, U, W = self.m, self.U, self.w
        st = self.st
        logo = m.asset_icon("youtube-logo", U(34), 255).copy()
        logo.fill((*YT_RED, 255), special_flags=pygame.BLEND_RGBA_MULT)
        surf.blit(logo, (U(56), U(40)))
        state = {"playing": "oynatılıyor", "paused": "duraklatıldı", "loading": "yükleniyor…"}.get(st.get("state"), "")
        m.text(surf, "YouTube" + (f" · {state}" if state else ""), m.font("Inter-Medium", U(26)), WHITE,
               (U(100), U(57)), "midleft")
        vr = self.video_rect()
        sh = pygame.Surface((32, 32), pygame.SRCALPHA)  # yumusak golge
        pygame.draw.rect(sh, (0, 0, 0, 110), (6, 7, 20, 20), border_radius=5)
        sh = pygame.transform.smoothscale(sh, (vr.w + U(110), vr.h + U(110)))
        surf.blit(sh, (vr.x - U(55), vr.y - U(48)))
        tx, tw = vr.right + U(36), W - U(56) - vr.right - U(36)
        f = m.font("Inter-SemiBold", U(36))
        y = vr.y + U(4)
        for line in m.wrap_lines(st.get("name", ""), f, tw, 4):
            m.text(surf, line, f, WHITE, (tx, y))
            y += f.get_linesize()
        f = m.font("Inter-Regular", U(26))
        m.text(surf, m.fit_text(", ".join(st.get("artists", [])), f, tw), f, SOFT, (tx, y + U(12)))

    def draw_music_page(self, surf):
        m, U, W = self.m, self.U, self.w
        st, track = self.st, self.has_track()

        # ---- sol ust (saatin yeri): sarki, sanatci, kaynak
        logo = m.asset_icon("youtube-logo", U(34), 255)
        tint = logo.copy()
        tint.fill((*YT_RED, 255), special_flags=pygame.BLEND_RGBA_MULT)
        surf.blit(tint, (U(56), U(40)))
        state = {"playing": "çalıyor", "paused": "duraklatıldı", "loading": "yükleniyor…"}.get(st.get("state"), "")
        m.text(surf, "YouTube Music" + (f" · {state}" if track and state else ""), m.font("Inter-Medium", U(26)),
               WHITE, (U(100), U(57)), "midleft")
        if track:
            big = m.font("InterDisplay-Light", U(88))
            r = m.text(surf, m.fit_text(st["name"], big, U(700)), big, WHITE, (U(48), U(84)))
            f = m.font("Inter-Medium", U(40))
            r = m.text(surf, m.fit_text(", ".join(st.get("artists", [])), f, U(700)), f, WHITE,
                       (U(58), r.bottom + U(2)))
            if st.get("album"):
                m.text(surf, m.fit_text(st["album"], m.font("Inter-Regular", U(28)), U(700)),
                       m.font("Inter-Regular", U(28)), SOFT, (U(60), r.bottom + U(10)))
        else:
            m.text(surf, "Bağlı değil", m.font("InterDisplay-Light", U(88)), WHITE, (U(48), U(84)))
            f = m.font("Inter-Regular", U(28))
            y = U(210)
            for line in ("Telefonda YouTube Music'i (şarkı) ya da YouTube'u (video)",
                         "açın, yayınla (Cast) simgesine dokunun",
                         "ve “Pi Panel”i seçin."):
                m.text(surf, line, f, SOFT, (U(60), y))
                y += U(40)

        # ---- sag ust (hava durumu kartinin yeri): kapak
        art = pygame.Rect(W - U(56) - U(300), U(52), U(300), U(300))
        if self.art[1] is not None and track:
            sh = pygame.Surface((32, 32), pygame.SRCALPHA)  # yumusak golge
            pygame.draw.rect(sh, (0, 0, 0, 110), (6, 7, 20, 20), border_radius=5)
            sh = pygame.transform.smoothscale(sh, (art.w + U(110), art.h + U(110)))
            surf.blit(sh, (art.x - U(55), art.y - U(48)))
            surf.blit(self.art[1], art)
        else:
            m.panel(surf, art, radius=U(22))
            ico = m.asset_icon("youtube-logo", U(110), 110)
            surf.blit(ico, ico.get_rect(center=art.center))

    def draw_controls(self, surf):
        m, U, W, H = self.m, self.U, self.w, self.h
        st, track = self.st, self.has_track()

        # ---- alt panel (tahminin yeri): ilerleme, kontroller, ses
        fc = pygame.Rect(U(56), H - U(56) - U(196), W - U(112), U(196))
        m.panel(surf, fc)
        self.prog_rect = pygame.Rect(fc.x + U(40), fc.y + U(40), fc.w - U(80), U(6))
        cy = fc.y + U(124)
        btn = pygame.Rect(0, 0, U(88), U(88))
        btn.center = (fc.x + U(240), cy)
        pygame.draw.circle(surf, WHITE if track else (120, 126, 140), btn.center, btn.w // 2)
        ico = m.asset_icon("pause" if self.playing() else "play-fill", U(38))
        ico = ico.copy()
        ico.fill((18, 18, 22, 255), special_flags=pygame.BLEND_RGBA_MULT)
        surf.blit(ico, ico.get_rect(center=(btn.centerx + (0 if self.playing() else U(3)), btn.centery)))
        if track:
            self.buttons.append((btn, lambda: self.cmd("/pause" if self.playing() else "/play")))
        for dx, name, path in ((-U(130), "skip-back", "/previous"), (U(130), "skip-forward", "/next")):
            r = pygame.Rect(0, 0, U(76), U(76))
            r.center = (btn.centerx + dx, cy)
            ico = m.asset_icon(name, U(38), 255 if track else 110)
            surf.blit(ico, ico.get_rect(center=r.center))
            if track:
                self.buttons.append((r, lambda path=path: self.cmd(path)))
        # ses: [kisik] ----o---- [yuksek]
        lo = pygame.Rect(fc.x + U(560), cy - U(24), U(48), U(48))
        self.buttons.append((lo, lambda: self.cmd("/mute")))
        ico = m.asset_icon("speaker-x" if st.get("muted") else "speaker-low", U(28), 220)
        surf.blit(ico, ico.get_rect(center=lo.center))
        hi = m.asset_icon("speaker-high", U(28), 220)
        hi_rect = hi.get_rect(midright=(fc.right - U(40), cy))
        surf.blit(hi, hi_rect)
        self.bar_rect = pygame.Rect(lo.right + U(16), cy - U(3), hi_rect.x - lo.right - U(36), U(6))

        # ---- alt kenar
        f = m.font("Inter-Medium", U(20))
        back = m.text(surf, "Ana ekran  ›", f, WHITE, (W - U(56), H - U(18)), "bottomright")
        self.buttons.append((back.inflate(U(40), U(36)), self.close))
        m.text(surf, "Ana ekrana dönmek için sola kaydırın", f, SOFT, (U(58), H - U(18)), "bottomleft")
        if self.sink is not None:  # ses nereden cikiyor
            label = m.fit_text(self.sink, f, U(380)) if self.sink else "Kulaklık bağlı değil · ses ekrandan"
            ico = m.asset_icon("headphones" if self.sink else "speaker-high", U(24), 255 if self.sink else 160)
            x = W // 2 - (ico.get_width() + U(10) + f.size(label)[0]) // 2
            r = m.text(surf, label, f, WHITE if self.sink else SOFT, (x + ico.get_width() + U(10), H - U(18)),
                       "bottomleft")
            surf.blit(ico, ico.get_rect(midleft=(x, r.centery)))

    def draw_dynamic(self, surf):
        m, U = self.m, self.U
        if self.video_mode():  # ekranda burasi delik olur, alttan mpv'nin videosu gorunur (dashboard MpvVideo)
            pygame.draw.rect(surf, (0, 0, 0), self.video_rect(), border_radius=self.video_radius())
        # ilerleme + sureler
        bar, dur = self.prog_rect, self.st.get("duration") or 0
        dim = pygame.Surface(bar.size, pygame.SRCALPHA)
        pygame.draw.rect(dim, (255, 255, 255, 70), dim.get_rect(), border_radius=bar.h // 2)
        surf.blit(dim, bar.topleft)
        if dur and self.has_track():
            pos = self.position()
            done = bar.copy()
            done.w = int(bar.w * pos / dur)
            if done.w:
                pygame.draw.rect(surf, WHITE, done, border_radius=bar.h // 2)
            pygame.draw.circle(surf, WHITE, (bar.x + done.w, bar.centery), U(13 if self.seeking else 8))
            f = m.font("Inter-Medium", U(19))
            mmss = lambda ms: f"{int(ms // 60000)}:{int(ms // 1000 % 60):02d}"
            m.text(surf, mmss(pos), f, SOFT, (bar.x, bar.bottom + U(12)), shadow=False)
            m.text(surf, mmss(dur), f, SOFT, (bar.right, bar.bottom + U(12)), "topright", shadow=False)
        # ses cubugu
        bar = self.bar_rect
        surf.blit(pygame.transform.scale(dim, bar.size), bar.topleft)
        fill = bar.copy()
        fill.w = int(bar.w * self.volume() / 100)
        if fill.w and not self.st.get("muted"):
            pygame.draw.rect(surf, WHITE, fill, border_radius=bar.h // 2)
        knob = U(16 if self.dragging else 13)
        pygame.draw.circle(surf, (0, 0, 0), (bar.x + fill.w, bar.centery + U(2)), knob + U(1))
        pygame.draw.circle(surf, WHITE, (bar.x + fill.w, bar.centery), knob)

    def handle(self, kind, x, y):
        on_bar = self.bar_rect.inflate(self.U(30), self.U(60)).collidepoint(x, y)
        on_prog = self.prog_rect.inflate(self.U(30), self.U(60)).collidepoint(x, y)
        dur = self.st.get("duration") or 0
        if kind == "down" and on_prog and dur and self.has_track():
            self.seeking = True
        if self.seeking:  # ileri/geri sarma: parmak neredeyse oraya
            self.touch()
            bar = self.prog_rect
            self.seek_pos = max(0, min(1, (x - bar.x) / max(1, bar.w))) * dur
            if kind == "up":
                self.seeking, self.drag_end = False, time.time()
                self.cmd(f"/seek?pos={self.seek_pos / 1000:.1f}")
                self.st.update(position=self.seek_pos, time=time.time())  # alici dosyayi guncelleyene kadar
                self.seek_pos = None
            return
        if kind == "down" and on_bar:
            self.dragging = True
        if self.dragging:
            self.touch()
            bar = self.bar_rect
            self.drag_vol = round(max(0, min(1, (x - bar.x) / max(1, bar.w))) * 100)
            if kind == "up":
                self.dragging, self.drag_end = False, time.time()
                vol = self.drag_vol
                self.cmd(f"/volume?level={vol}")
                self.st["volume"] = vol  # alici dosyayi guncelleyene kadar
                self.drag_vol = None
            return
        super().handle(kind, x, y)

    def swipe_allowed(self):
        return not self.dragging and not self.seeking and time.time() - self.drag_end > 0.4
