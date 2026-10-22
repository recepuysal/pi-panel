#!/usr/bin/env python3
"""Dokunmatik testi: dokunulan yerlere renkli iz cizer, koordinatlari yazdirir. 60 sn surer."""
import select
import time

import evdev
import pygame

import dashboard as d

DURATION = 60
COLORS = [(79, 160, 255), (255, 120, 120), (110, 230, 140), (255, 210, 80), (200, 130, 255),
          (80, 220, 220), (255, 160, 60), (255, 255, 255), (160, 160, 255), (255, 120, 220)]

pygame.font.init()
dev = next(evdev.InputDevice(p) for p in evdev.list_devices() if "Waveshare" in evdev.InputDevice(p).name)
fb = d.Framebuffer()
W, H = fb.w, fb.h
canvas = pygame.Surface((W, H))
canvas.fill((14, 18, 28))
d.text(canvas, "Ekrana dokunun", d.font("Inter-SemiBold", 56), (255, 255, 255), (W // 2, 60), "midtop", shadow=False)
d.text(canvas, "Birden fazla parmakla da deneyebilirsiniz", d.font("Inter-Regular", 26), (160, 170, 190),
       (W // 2, 135), "midtop", shadow=False)

slots, cur = {}, 0  # slot -> {"x", "y", "id", "last"}
touches, start, dirty, shown = 0, time.time(), True, None
while time.time() - start < DURATION:
    if select.select([dev.fd], [], [], 0.05)[0]:
        for ev in dev.read():
            if ev.type != evdev.ecodes.EV_ABS:
                continue
            if ev.code == evdev.ecodes.ABS_MT_SLOT:
                cur = ev.value
            elif ev.code == evdev.ecodes.ABS_MT_TRACKING_ID:
                if ev.value == -1:
                    slots.pop(cur, None)
                else:
                    slots[cur] = {"x": None, "y": None, "last": None}
                    touches += 1
            elif ev.code in (evdev.ecodes.ABS_MT_POSITION_X, evdev.ecodes.ABS_MT_POSITION_Y) and cur in slots:
                slots[cur]["x" if ev.code == evdev.ecodes.ABS_MT_POSITION_X else "y"] = ev.value
        for slot, t in slots.items():
            if t["x"] is None or t["y"] is None:
                continue
            col = COLORS[slot % len(COLORS)]
            if t["last"]:
                pygame.draw.line(canvas, col, t["last"], (t["x"], t["y"]), 10)
            pygame.draw.circle(canvas, col, (t["x"], t["y"]), 14)
            if t["last"] is None:
                print(f"dokunus parmak={slot} x={t['x']} y={t['y']}", flush=True)
            t["last"] = (t["x"], t["y"])
        dirty = True
    left = DURATION - int(time.time() - start)
    if dirty or left != shown:
        shown = left
        frame = canvas.copy()
        d.text(frame, f"{left} sn  ·  {touches} dokunuş  ·  aynı anda {len(slots)} parmak",
               d.font("Inter-Medium", 24), (160, 170, 190), (W // 2, H - 30), "midbottom", shadow=False)
        fb.show(frame)
        dirty = False
print(f"toplam dokunus: {touches}")
