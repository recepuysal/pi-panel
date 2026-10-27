#!/usr/bin/env python3
"""Pi Zero 2 W HDMI paneli: saat, hava durumu, gunluk duvar kagidi, IP.

Masaustu gerektirmez: pygame ile cizer, sonucu dogrudan /dev/fb0'a yazar.
  python3 dashboard.py                 # ekranda calistir
  python3 dashboard.py --shot out.png  # tek kare uretip PNG kaydet (test icin)

Ayarlar (opsiyonel) config.json: {"city": "Istanbul", "lat": 41.01, "lon": 28.95, "bing_market": "tr-TR"}
"""
import datetime as dt
import email.utils
import functools
import html
import json
import math
import mmap
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
warnings.filterwarnings("ignore")
import pygame  # noqa: E402

import wifi_ui  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
FONT_DIR = "/usr/share/fonts/opentype/inter/"
ASSETS = HERE / "assets"
WIFI_STATE = Path("/run/wifi-setup/state.json")  # wifi_setup.py yazar
# Telefondan gonderilen muzik: kaynak -> (durum dosyasi, gorunen ad, renk)
MEDIA_SOURCES = {
    "spotify": (Path("/run/raspotify/now.json"), "Spotify", (30, 215, 96)),   # spotify_event.py yazar
    "youtube": (Path("/run/ytcast/now.json"), "YouTube", (255, 78, 69)),      # ytcast/receiver.mjs yazar
}
PC_LOCKED = Path("/tmp/pc-locked")  # bilgisayar Win+L ile kilitlenince (Gorev Zamanlayicisi, ssh) olusur
MPV_SOCK = "/run/ytcast/mpv.sock"  # ytcast alicisinin mpv'si: YouTube videosu ekrana dogrudan (DRM) oynar

DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
DAYS_SHORT = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
          "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]

# WMO hava kodu -> (Turkce aciklama, ikon turu)
WMO = {
    0: ("Açık", "clear"), 1: ("Çoğunlukla açık", "clear"), 2: ("Parçalı bulutlu", "partly"),
    3: ("Kapalı", "cloudy"), 45: ("Sisli", "fog"), 48: ("Kırağılı sis", "fog"),
    51: ("Hafif çisenti", "rain"), 53: ("Çisenti", "rain"), 55: ("Yoğun çisenti", "rain"),
    56: ("Dondurucu çisenti", "rain"), 57: ("Dondurucu çisenti", "rain"),
    61: ("Hafif yağmur", "rain"), 63: ("Yağmurlu", "rain"), 65: ("Şiddetli yağmur", "rain"),
    66: ("Dondurucu yağmur", "rain"), 67: ("Dondurucu yağmur", "rain"),
    71: ("Hafif kar", "snow"), 73: ("Karlı", "snow"), 75: ("Yoğun kar", "snow"), 77: ("Kar taneleri", "snow"),
    80: ("Hafif sağanak", "rain"), 81: ("Sağanak", "rain"), 82: ("Şiddetli sağanak", "rain"),
    85: ("Kar sağanağı", "snow"), 86: ("Yoğun kar sağanağı", "snow"),
    95: ("Gök gürültülü", "storm"), 96: ("Dolulu fırtına", "storm"), 99: ("Dolulu fırtına", "storm"),
}

WEATHER_EVERY = 15 * 60
RATE_EVERY = 60 * 60
NEWS_URL = "https://www.trthaber.com/manset_articles.rss"  # TRT Haber mansetleri, editor sirasiyla
NEWS_EVERY = 15 * 60
NEWS_SHOW = 30  # her baslik kac saniye ekranda kalir
OUNCE_G = 31.1035  # 1 troy ons = 31,1035 gram
WHITE = (255, 255, 255)
SOFT = (215, 222, 235)
MUTED = (160, 170, 190)
RAIN_BLUE = (120, 190, 255)
UP_GREEN = (110, 230, 140)
ICON_OFF_ALPHA = 95  # kapali/bos simge: yari saydam beyaz = gri
DOWN_RED = (255, 120, 120)

SYNODIC = 29.530588853  # ay evresi suresi (gun)
MOON_REF = dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)  # bilinen bir yeni ay
MOON_PHASES = ["Yeni Ay", "Büyüyen Hilal", "İlk Dördün", "Büyüyen Şişkin Ay",
               "Dolunay", "Küçülen Şişkin Ay", "Son Dördün", "Küçülen Hilal"]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "pi-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_config():
    cfg = {"city": None, "lat": None, "lon": None, "bing_market": "tr-TR", **wifi_ui.SETTINGS_DEFAULTS}
    path = HERE / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


# ---------------------------------------------------------------- veri

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.weather = None
        self.rates = None
        self.wall_path = None
        self.wall_meta = {}
        self.special = []  # [(tarih, ad)] resmi ve dini gunler
        self.news = []  # [(saat, baslik)] gunun mansetleri

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.version += 1


def locate(cfg):
    if cfg["lat"] is not None:
        return cfg
    cached = CACHE / "location.json"
    if cached.exists():
        cfg.update(json.loads(cached.read_text(encoding="utf-8")))
        return cfg
    info = json.loads(http_get("http://ip-api.com/json/?fields=status,city,lat,lon"))
    if info.get("status") == "success":
        loc = {"city": info["city"], "lat": info["lat"], "lon": info["lon"]}
        cached.write_text(json.dumps(loc, ensure_ascii=False), encoding="utf-8")
        cfg.update(loc)
    return cfg


def fetch_weather(cfg):
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={cfg['lat']}&longitude={cfg['lon']}"
           "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max"
           "&timezone=auto&forecast_days=6")
    return json.loads(http_get(url))


def fetch_rates():
    """Dolar ve gram altin (TL). Truncgil: Turkiye piyasasi + gunluk degisim; olmazsa yedekten hesaplar."""
    try:
        d = json.loads(http_get("https://finans.truncgil.com/v4/today.json"))
        return {"usd": d["USD"]["Selling"], "usd_chg": d["USD"]["Change"],
                "gold": d["GRA"]["Selling"], "gold_chg": d["GRA"]["Change"]}
    except Exception as e:
        log("truncgil olmadi, Yahoo deneniyor:", e)
    try:
        usd, usd_prev = yahoo_price("TRY=X")
        oz, oz_prev = yahoo_price("GC=F")  # altin, dolar/ons
        gold, gold_prev = oz * usd / OUNCE_G, oz_prev * usd_prev / OUNCE_G
        return {"usd": usd, "usd_chg": (usd / usd_prev - 1) * 100,
                "gold": gold, "gold_chg": (gold / gold_prev - 1) * 100}
    except Exception as e:
        log("Yahoo olmadi, son yedek:", e)
    usd = float(json.loads(http_get("https://open.er-api.com/v6/latest/USD"))["rates"]["TRY"])
    xau = float(json.loads(http_get("https://api.gold-api.com/price/XAU"))["price"])
    return {"usd": usd, "usd_chg": None, "gold": xau * usd / OUNCE_G, "gold_chg": None}


def yahoo_price(symbol):
    """(son fiyat, onceki kapanis)"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1h"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        meta = json.loads(r.read())["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"]), float(meta["chartPreviousClose"])


def tr_number(v, decimals):
    """49.5821 -> '49,58', 6564.96 -> '6.565' (Turkce bicim)."""
    return f"{v:,.{decimals}f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def fetch_wallpaper(market):
    """Bing gunun fotografini indirir; son 7 gunu saklar. (yol, meta) dondurur."""
    today = dt.date.today().isoformat()
    path = CACHE / f"wall-{today}.jpg"
    meta_path = CACHE / f"wall-{today}.json"
    if not path.exists():
        data = json.loads(http_get(f"https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt={market}"))
        info = data["images"][0]
        img = http_get("https://www.bing.com" + info["urlbase"] + "_1920x1080.jpg", timeout=60)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(img)
        tmp.replace(path)
        meta_path.write_text(json.dumps({"title": info.get("title", ""), "copyright": info.get("copyright", "")},
                                        ensure_ascii=False), encoding="utf-8")
        for old in sorted(CACHE.glob("wall-*.jpg"))[:-7]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return path, meta


def latest_cached_wallpaper():
    walls = sorted(CACHE.glob("wall-*.jpg"))
    if not walls:
        return None, {}
    meta_path = walls[-1].with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return walls[-1], meta


# ---------------------------------------------------------------- resmi ve dini gunler

OFFICIAL_DAYS = {  # (ay, gun) -> ad; resmi tatiller + anma gunleri
    (1, 1): "Yılbaşı",
    (3, 18): "Çanakkale Zaferi ve Şehitleri Anma Günü",
    (4, 23): "Ulusal Egemenlik ve Çocuk Bayramı",
    (5, 1): "Emek ve Dayanışma Günü",
    (5, 19): "Atatürk'ü Anma, Gençlik ve Spor Bayramı",
    (7, 15): "Demokrasi ve Milli Birlik Günü",
    (8, 30): "Zafer Bayramı",
    (10, 28): "Cumhuriyet Bayramı Arifesi",
    (10, 29): "Cumhuriyet Bayramı",
    (11, 10): "Atatürk'ü Anma Günü",
}
SPECIAL_COLOR = (255, 196, 92)  # bugun ozel bir gunse
HIJRI_URL = "https://api.aladhan.com/v1/gToHCalendar/{m}/{y}?calendarMethod=DIYANET"


def hijri_month(y, m):
    """Miladi ayin her gunu icin (tarih, hicri gun, hicri ay); Diyanet takvimi (aladhan.com), ay ay onbellekte."""
    path = CACHE / f"hijri-{y}-{m:02d}.json"
    if not path.exists():
        data = json.loads(http_get(HIJRI_URL.format(y=y, m=m), timeout=60))["data"]  # servis yavas (~15 sn)
        days = [[x["gregorian"]["date"], int(x["hijri"]["day"]), x["hijri"]["month"]["number"]] for x in data]
        path.write_text(json.dumps(days), encoding="utf-8")
        time.sleep(1)  # API'yi yormayalim
    return [(dt.datetime.strptime(d, "%d-%m-%Y").date(), hd, hm)
            for d, hd, hm in json.loads(path.read_text(encoding="utf-8"))]


def religious_days(days):
    """Hicri tarihlerden dini gunler. Kandiller gecedir: Diyanet bir onceki gunun aksamini yazar."""
    out = []
    idx = {(hm, hd): i for i, (_, hd, hm) in enumerate(days)}
    on_day = {(1, 1): "Hicri Yılbaşı", (1, 10): "Aşure Günü", (7, 1): "Üç Ayların Başlangıcı",
              (9, 1): "Ramazan'ın İlk Günü", (12, 9): "Kurban Bayramı Arifesi"}
    night_before = {(3, 12): "Mevlid Kandili", (7, 27): "Miraç Kandili", (8, 15): "Berat Kandili",
                    (9, 27): "Kadir Gecesi"}
    for i, (day, hd, hm) in enumerate(days):
        if (hm, hd) in on_day:
            out.append((day, on_day[hm, hd]))
        if (hm, hd) in night_before and i > 0:
            out.append((days[i - 1][0], night_before[hm, hd]))
        if hm == 10 and hd <= 3:
            out.append((day, f"Ramazan Bayramı {hd}. Gün"))
            if hd == 1 and i > 0:
                out.append((days[i - 1][0], "Ramazan Bayramı Arifesi"))
        if hm == 12 and 10 <= hd <= 13:
            out.append((day, f"Kurban Bayramı {hd - 9}. Gün"))
        # Regaip: Recep'in ilk cumasindan onceki persembe gecesi
        if hm == 7 and day.weekday() == 4 and hd <= 7 and i > 0:
            out.append((days[i - 1][0], "Regaip Kandili"))
    return out


def special_days(months=13):
    """Bugunden itibaren ~1 yilin resmi ve dini gunleri: [(tarih, ad)], tarihe gore."""
    today = dt.date.today()
    days, y, m = [], today.year, today.month
    for _ in range(months):
        days += hijri_month(y, m)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    out = religious_days(days)
    for day, _, _ in days:
        if (day.month, day.day) in OFFICIAL_DAYS:
            out.append((day, OFFICIAL_DAYS[day.month, day.day]))
    return sorted((d, n) for d, n in out if d >= today)


def fetch_news(url, limit=10):
    """Son 24 saatin ilk `limit` manseti: [("08:18", "Baslik"), ...]."""
    root = ET.fromstring(http_get(url))
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for item in root.iter("item"):
        title = " ".join(html.unescape(item.findtext("title") or "").split())
        try:
            when = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            when = None
        if title and (when is None or now - when < dt.timedelta(hours=24)):
            out.append((when.astimezone().strftime("%H:%M") if when else "", title))
    return out[:limit]


def special_line(special, today):
    """Ana ekran satiri: yalniz bugun resmi / dini bir gunse (yoksa bos, satir gorunmez)."""
    names = [n for d, n in special if d == today]
    return ("Bugün " + " · ".join(names), SPECIAL_COLOR) if names else ("", SOFT)


def worker(state, cfg):
    """Arka planda konum, hava durumu ve duvar kagidini gunceller."""
    last_weather = last_rate = last_news = 0
    wall_day = special_day = None
    path, meta = latest_cached_wallpaper()
    if path:
        state.update(wall_path=path, wall_meta=meta)
    while True:
        try:
            if special_day != dt.date.today():
                state.special = special_days()  # saat katmaninda cizilir; aylar onbellekte, aninda
                special_day = dt.date.today()
                log("ozel gunler:", ", ".join(f"{d:%d.%m} {n}" for d, n in state.special[:3]))
            locate(cfg)
            if time.time() - last_weather > WEATHER_EVERY and cfg["lat"] is not None:
                state.update(weather=fetch_weather(cfg))
                last_weather = time.time()
                log("hava durumu guncellendi")
            if time.time() - last_rate > RATE_EVERY:
                state.rates = fetch_rates()  # her saniye cizilen katmanda; arka plani yeniden cizmeye gerek yok
                last_rate = time.time()
                log("kurlar guncellendi")
            if wall_day != dt.date.today():
                path, meta = fetch_wallpaper(cfg["bing_market"])
                if path != state.wall_path:
                    state.update(wall_path=path, wall_meta=meta)
                    log("duvar kagidi:", meta.get("title", path.name))
                wall_day = dt.date.today()
            if cfg["news"] and time.time() - last_news > NEWS_EVERY:
                last_news = time.time()  # kaynak cevap vermezse diger veriler beklemesin: 15 dk sonra tekrar
                state.news = fetch_news(cfg.get("news_url") or NEWS_URL)  # saat katmaninda cizilir
                log("haberler guncellendi:", len(state.news))
            time.sleep(30)
        except Exception as e:  # ag yoksa sessizce tekrar dene
            log("guncelleme hatasi:", e)
            time.sleep(60)


def ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "bağlantı yok"


def wifi_name():
    try:
        out = subprocess.run(["nmcli", "-t", "-g", "GENERAL.CONNECTION", "dev", "show", "wlan0"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out.removeprefix("netplan-wlan0-") or "-"  # Imager/cloud-init profilleri "netplan-wlan0-<ssid>"
    except Exception:
        return "-"


def wifi_signal():
    """Wi-Fi sinyali 1-3 parca (Apple tarzi); okunamazsa None."""
    try:
        for line in Path("/proc/net/wireless").read_text().splitlines()[2:]:
            parts = line.split()
            if parts and parts[0] == "wlan0:":
                quality = float(parts[2].rstrip("."))  # 0-70
                return max(1, min(3, round(quality / 70 * 3)))
    except (OSError, ValueError, IndexError):
        pass
    return None


def bluetooth_on():
    """Bluetooth acik mi: rfkill engeli yoksa ve denetleyici 'Powered: yes' ise."""
    try:
        blocked = True
        for rf in Path("/sys/class/rfkill").iterdir():
            if (rf / "type").read_text().strip() == "bluetooth":
                blocked = (rf / "soft").read_text().strip() != "0" or (rf / "hard").read_text().strip() != "0"
        if blocked:
            return False
        out = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=4).stdout
        return "Powered: yes" in out
    except Exception:
        return False


def cpu_temp():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except OSError:
        return None


# ---------------------------------------------------------------- cizim yardimcilari

_fonts = {}


def font(name, size):
    key = (name, size)
    if key not in _fonts:
        try:
            _fonts[key] = pygame.font.Font(FONT_DIR + name + ".otf", size)
        except (OSError, FileNotFoundError):
            _fonts[key] = pygame.font.Font(None, size)
    return _fonts[key]


def text(surf, s, f, color, anchor_pos, anchor="topleft", shadow=True, alpha=255):
    img = f.render(s, True, color)
    if alpha < 255:
        img.set_alpha(alpha)
    rect = img.get_rect(**{anchor: anchor_pos})
    if shadow:
        sh = f.render(s, True, (0, 0, 0))
        sh.set_alpha(110 * alpha // 255)
        surf.blit(sh, rect.move(2, 3))
    surf.blit(img, rect)
    return rect


def fit_text(s, f, max_w):
    if f.size(s)[0] <= max_w:
        return s
    while s and f.size(s + "…")[0] > max_w:
        s = s[:-1]
    return s + "…"


def wrap_lines(s, f, max_w, max_lines):
    """Kelime kelime satirlara boler; sigmayan son satir '…' ile biter."""
    lines, cur, words = [], "", s.split()
    for i, word in enumerate(words):
        if not cur or f.size(cur + " " + word)[0] <= max_w:
            cur = (cur + " " + word).strip()
            continue
        lines.append(cur)
        cur = word
        if len(lines) == max_lines:  # yer kalmadi: kalan kelimeler kesilir
            last = lines[-1] + " " + " ".join(words[i:])
            lines[-1] = fit_text(last, f, max_w)
            return [fit_text(line, f, max_w) for line in lines]
    if cur:
        lines.append(cur)
    return [fit_text(line, f, max_w) for line in lines]


def panel(surf, rect, alpha=105, radius=26):
    p = pygame.Surface(rect.size, pygame.SRCALPHA)
    pygame.draw.rect(p, (8, 12, 22, alpha), p.get_rect(), border_radius=radius)
    pygame.draw.rect(p, (255, 255, 255, 28), p.get_rect(), width=1, border_radius=radius)
    surf.blit(p, rect.topleft)


def cover(img, w, h):
    iw, ih = img.get_size()
    k = max(w / iw, h / ih)
    img = pygame.transform.smoothscale(img, (math.ceil(iw * k), math.ceil(ih * k)))
    x, y = (img.get_width() - w) // 2, (img.get_height() - h) // 2
    return img.subsurface((x, y, w, h)).copy()


def fallback_background(w, h):
    col = pygame.Surface((1, 2))
    col.set_at((0, 0), (25, 40, 75))
    col.set_at((0, 1), (8, 10, 20))
    return pygame.transform.smoothscale(col, (w, h))


def shade_overlay(w, h):
    """Ust ve altta yaziyi okunur kilan koyu gecis."""
    col = pygame.Surface((1, 100), pygame.SRCALPHA)
    for y in range(100):
        top = max(0.0, 1 - y / 45) * 150
        bottom = max(0.0, (y - 50) / 50) * 190
        col.set_at((0, y), (0, 0, 0, int(max(top, bottom))))
    return pygame.transform.smoothscale(col, (w, h))


def weather_icon(kind, size, day=True):
    S = size * 3  # buyuk cizip kucult: kenarlar yumusak olsun
    s = pygame.Surface((S, S), pygame.SRCALPHA)
    sun_c, cloud_c, dark_c = (255, 196, 60), (242, 245, 250), (150, 158, 172)

    def sun(cx, cy, r):
        for i in range(8):
            a = i * math.pi / 4
            p1 = (cx + math.cos(a) * r * 1.4, cy + math.sin(a) * r * 1.4)
            p2 = (cx + math.cos(a) * r * 1.8, cy + math.sin(a) * r * 1.8)
            pygame.draw.line(s, sun_c, p1, p2, max(3, int(r * 0.2)))
        pygame.draw.circle(s, sun_c, (cx, cy), r)

    def moon(cx, cy, r):
        pygame.draw.circle(s, (238, 236, 215), (cx, cy), r)
        pygame.draw.circle(s, (0, 0, 0, 0), (cx + r * 0.5, cy - r * 0.35), r * 0.85)

    def cloud(cx, cy, w, col):
        r = w * 0.2
        yb = cy + r
        pygame.draw.circle(s, col, (cx - w * 0.25, yb - r), r)
        pygame.draw.circle(s, col, (cx, yb - r * 1.3), r * 1.3)
        pygame.draw.circle(s, col, (cx + w * 0.25, yb - r * 0.9), r * 0.9)
        pygame.draw.rect(s, col, (cx - w * 0.25, yb - r, w * 0.5, r))

    sky = sun if day else moon
    if kind == "clear":
        sky(S * 0.5, S * 0.5, S * 0.22)
    elif kind == "partly":
        sky(S * 0.36, S * 0.36, S * 0.16)
        cloud(S * 0.56, S * 0.56, S * 0.72, cloud_c)
    elif kind == "cloudy":
        cloud(S * 0.62, S * 0.4, S * 0.6, dark_c)
        cloud(S * 0.46, S * 0.56, S * 0.78, cloud_c)
    else:
        cloud(S * 0.5, S * 0.42, S * 0.82, dark_c if kind == "storm" else cloud_c)
        if kind == "rain":
            for i in range(3):
                x = S * (0.32 + i * 0.18)
                pygame.draw.line(s, RAIN_BLUE, (x, S * 0.68), (x - S * 0.05, S * 0.84), int(S * 0.04))
        elif kind == "snow":
            for i in range(3):
                pygame.draw.circle(s, WHITE, (S * (0.32 + i * 0.18), S * (0.74 + (i % 2) * 0.08)), S * 0.04)
        elif kind == "storm":
            bolt = [(0.54, 0.58), (0.42, 0.78), (0.51, 0.78), (0.45, 0.96), (0.63, 0.72), (0.54, 0.72), (0.61, 0.58)]
            pygame.draw.polygon(s, sun_c, [(x * S, y * S) for x, y in bolt])
        elif kind == "fog":
            for i in range(3):
                y = S * (0.7 + i * 0.09)
                pygame.draw.line(s, SOFT, (S * (0.22 + i * 0.04), y), (S * (0.78 - i * 0.04), y), int(S * 0.035))
    return pygame.transform.smoothscale(s, (size, size))


_qr_cache = {}


def qr_surface(data, size):
    if (data, size) not in _qr_cache:
        import qrcode
        q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(data)
        q.make(fit=True)
        m = q.get_matrix()
        cell = max(1, size // len(m))
        s = pygame.Surface((cell * len(m), cell * len(m)))
        s.fill(WHITE)
        for y, row in enumerate(m):
            for x, v in enumerate(row):
                if v:
                    s.fill((0, 0, 0), (x * cell, y * cell, cell, cell))
        _qr_cache[(data, size)] = s
    return _qr_cache[(data, size)]


def wifi_qr_data(ssid, password):
    # QR Wi-Fi bicimi: \ ; , : " karakterleri ters bolu ile kacirilir
    esc = lambda t: re.sub(r'[\\;,:"]', lambda m: "\\" + m.group(0), t)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


@functools.lru_cache(maxsize=32)
def asset_icon(name, size, alpha=255):
    """assets/<name>.png (beyaz, saydam zemin; Phosphor Icons, MIT) -> size x size, istege bagli soluk."""
    img = pygame.transform.smoothscale(pygame.image.load(str(ASSETS / f"{name}.png")), (size, size))
    if alpha < 255:
        img.fill((255, 255, 255, alpha), special_flags=pygame.BLEND_RGBA_MULT)
    return img


@functools.lru_cache(maxsize=16)
def wifi_icon(size, level):
    """Gri tam simge ustunde sinyal seviyesi kadar beyaz kisim. level 1-3, None = bagli degil (tamamen gri)."""
    icon = asset_icon("wifi-high", size, ICON_OFF_ALPHA).copy()
    if level:
        icon.blit(asset_icon(("wifi-low", "wifi-medium", "wifi-high")[level - 1], size), (0, 0))
    return icon


def bluetooth_icon(size, on):
    return asset_icon("bluetooth", size, 255 if on else ICON_OFF_ALPHA)


def moon_info():
    """(yas_gun, aydinlik 0-1, evre adi, (siradaki evre, kac gun sonra))"""
    age = (dt.datetime.now(dt.timezone.utc) - MOON_REF).total_seconds() / 86400 % SYNODIC
    illum = (1 - math.cos(2 * math.pi * age / SYNODIC)) / 2
    name = MOON_PHASES[int(age / SYNODIC * 8 + 0.5) % 8]
    nxt = ("Dolunay", SYNODIC / 2 - age) if age < SYNODIC / 2 else ("Yeni Ay", SYNODIC - age)
    return age, illum, name, nxt


@functools.lru_cache(maxsize=4)
def moon_icon(size, age_q):
    """Evresine gore aydinlanmis ay (age_q = gun * 4)."""
    age = age_q / 4
    S = size * 3
    r = S // 2 - 3
    c = S // 2
    s = pygame.Surface((S, S), pygame.SRCALPHA)
    pygame.draw.circle(s, (58, 62, 76), (c, c), r)
    k = math.cos(2 * math.pi * age / SYNODIC)
    for yy in range(-r, r + 1):
        w = math.sqrt(max(0, r * r - yy * yy))
        x1, x2 = (k * w, w) if age < SYNODIC / 2 else (-w, -k * w)  # buyurken sag, kuculurken sol taraf aydinlik
        if x2 > x1:
            pygame.draw.line(s, (240, 236, 220), (c + x1, c + yy), (c + x2, c + yy))
    return pygame.transform.smoothscale(s, (size, size))


def to_minutes(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


# ---------------------------------------------------------------- sahne

class Dashboard:
    def __init__(self, w, h, state, cfg):
        self.w, self.h = w, h
        self.u = h / 800  # olcek: 800 piksel yukseklik referans
        self.state, self.cfg = state, cfg
        self.static = None
        self.static_version = -1
        self.overlay = shade_overlay(w, h)
        self.wall_cache = (None, None)
        self.status = ("", "", None, None, False)
        self.status_time = 0
        self.wifi = {}
        self.wifi_mtime = None
        self.wifi_hit = None   # sag ustteki Wi-Fi alanina dokunma bolgesi
        self.bt_hit = None     # sag ustteki Bluetooth simgesi
        self.ap_button = None  # kurulum ekranindaki "Bu ekrandan ayarla"
        self.static_theme, self.static_day = None, None
        self.force_evening = False  # onizleme icin
        self.spot, self.spot_cover = {}, (None, None)
        self.media = {k: ({}, None) for k in MEDIA_SOURCES}  # kaynak -> (durum, mtime)

    def U(self, v):
        return int(v * self.u)

    def wifi_state(self):
        try:
            mtime = WIFI_STATE.stat().st_mtime
            if mtime != self.wifi_mtime:
                self.wifi = json.loads(WIFI_STATE.read_text(encoding="utf-8"))
                self.wifi_mtime = mtime
        except (OSError, ValueError):
            self.wifi = {}
        return self.wifi

    def pill(self, surf, msg, color=WHITE):
        f = font("Inter-Medium", self.U(24))
        w, h = f.size(msg)
        r = pygame.Rect(0, 0, w + self.U(48), h + self.U(22))
        r.midtop = (self.w // 2, self.U(18))
        panel(surf, r, alpha=190, radius=r.h // 2)
        text(surf, msg, f, color, r.center, "center", shadow=False)

    def draw_wifi_setup(self, surf, ws):
        U, W, H = self.U, self.w, self.h
        dim = pygame.Surface((W, H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 175))
        surf.blit(dim, (0, 0))
        box = pygame.Rect(0, 0, U(1060), U(470))
        box.center = (W // 2, H // 2)
        panel(surf, box, alpha=215)
        qr = qr_surface(wifi_qr_data(ws["ssid"], ws["password"]), U(330))
        qr_rect = qr.get_rect(midleft=(box.x + U(40), box.centery))
        pygame.draw.rect(surf, WHITE, qr_rect.inflate(U(16), U(16)), border_radius=U(12))
        surf.blit(qr, qr_rect)
        x = qr_rect.right + U(48)
        y = box.y + U(46)
        text(surf, "Wi-Fi bağlantısı yok", font("Inter-SemiBold", U(40)), WHITE, (x, y), shadow=False)
        y += U(72)
        big, small = font("Inter-Medium", U(25)), font("Inter-Regular", U(21))
        steps = [
            ("1", "Telefon kamerasıyla QR kodu okutun", f"ya da “{ws['ssid']}” ağına bağlanın · şifre: {ws['password']}"),
            ("2", "Açılan sayfadan ağınızı seçin", "şifresini girip “Kaydet ve bağlan”a dokunun"),
        ]
        for num, line1, line2 in steps:
            pygame.draw.circle(surf, (79, 140, 255), (x + U(18), y + U(16)), U(18))
            text(surf, num, font("Inter-SemiBold", U(22)), WHITE, (x + U(18), y + U(16)), "center", shadow=False)
            text(surf, line1, big, WHITE, (x + U(50), y), shadow=False)
            text(surf, line2, small, MUTED, (x + U(50), y + U(36)), shadow=False)
            y += U(100)
        text(surf, f"Sayfa açılmazsa tarayıcıya yazın:  {ws['url'].replace('http://', '')}", small, SOFT,
             (x, y + U(6)), shadow=False)
        if ws.get("error"):
            text(surf, ws["error"], font("Inter-Medium", U(21)), DOWN_RED, (x, y + U(44)), shadow=False)
            y += U(40)
        self.ap_button = pygame.Rect(x, y + U(44), U(330), U(58))
        pygame.draw.rect(surf, (79, 140, 255), self.ap_button, border_radius=U(14))
        text(surf, "Bu ekrandan ayarla", font("Inter-SemiBold", U(24)), WHITE, self.ap_button.center, "center",
             shadow=False)

    def background(self, path):
        if path is None:
            return fallback_background(self.w, self.h)
        if self.wall_cache[0] != path:
            try:
                self.wall_cache = (path, cover(pygame.image.load(str(path)), self.w, self.h))
            except pygame.error as e:
                log("duvar kagidi yuklenemedi:", e)
                return fallback_background(self.w, self.h)
        return self.wall_cache[1]

    def evening_active(self):
        """Aksam modu: evening_start'tan (varsayilan 17:00) gun dogumuna kadar koyu tema."""
        if self.force_evening:
            return True
        if not self.cfg["evening_mode"]:
            return False
        now = dt.datetime.now()
        w = self.state.weather
        try:
            sunrise = dt.datetime.fromisoformat(w["daily"]["sunrise"][0])
        except (TypeError, KeyError, ValueError, IndexError):
            sunrise = now.replace(hour=6, minute=0)
        minutes = now.hour * 60 + now.minute
        return minutes >= to_minutes(self.cfg["evening_start"]) or now < sunrise

    def night_active(self):
        """Gece modu: ayarlardaki saat araliginda (gece yarisini asabilir)."""
        if not self.cfg["night_mode"]:
            return False
        now = dt.datetime.now()
        t = now.hour * 60 + now.minute
        a, b = to_minutes(self.cfg["night_start"]), to_minutes(self.cfg["night_end"])
        return a <= t < b if a < b else (t >= a or t < b)

    def wallpaper(self, path, dark):
        surf = self.background(path).copy()
        if dark:
            surf.fill((105, 108, 128), special_flags=pygame.BLEND_MULT)  # koyu, hafif mavi gece tonu
        surf.blit(self.overlay, (0, 0))
        return surf

    def backdrop(self):
        """Duvar kagidi + okunurluk gecisi (radyo sayfasi icin, saat ve kartlar olmadan)."""
        key = (self.state.wall_path, self.evening_active())
        if getattr(self, "_backdrop", (None,))[0] != key:
            self._backdrop = (key, self.wallpaper(*key))
        return self._backdrop[1]

    def night_frame(self):
        """Gece: siyah ekranda soluk buyuk saat; iz kalmasin diye konum dakikada bir hafif kayar."""
        U, W, H = self.U, self.w, self.h
        surf = pygame.Surface((W, H))
        now = dt.datetime.now()
        dx, dy = (now.minute % 7 - 3) * U(10), (now.minute % 5 - 2) * U(8)
        r = text(surf, now.strftime("%H:%M"), font("InterDisplay-Light", U(240)), (105, 108, 120),
                 (W // 2 + dx, H // 2 - U(30) + dy), "center", shadow=False)
        date = f"{now.day} {MONTHS[now.month - 1]}, {DAYS[now.weekday()]}"
        text(surf, date, font("Inter-Regular", U(30)), (70, 72, 82), (W // 2 + dx, r.bottom + U(4)), "midtop",
             shadow=False)
        return surf

    def spotify_state(self):
        """Telefondan calinan sarki (Spotify / YouTube): calani, yoksa en son degiseni secer.
        Kapak resmi arka planda indirilir."""
        for src, (path, _, _) in MEDIA_SOURCES.items():
            try:
                mtime = path.stat().st_mtime
                if mtime != self.media[src][1]:
                    st = json.loads(path.read_text(encoding="utf-8"))
                    st["source"] = src
                    self.media[src] = (st, mtime)
            except (OSError, ValueError):
                self.media[src] = ({}, None)
        states = [st for st, _ in self.media.values() if st.get("name")]
        self.spot = max(states, key=lambda st: (st.get("state") == "playing", st.get("time", 0)), default={})
        url = self.spot.get("cover")
        if url and self.spot_cover[0] != url:
            self.spot_cover = (url, None)

            def fetch():
                try:
                    path = CACHE / "media_cover.jpg"
                    path.write_bytes(http_get(url))
                    size = self.U(118)
                    img = cover(pygame.image.load(str(path)), size, size)
                    rounded = pygame.Surface((size, size), pygame.SRCALPHA)
                    rounded.blit(img, (0, 0))
                    mask = pygame.Surface((size, size), pygame.SRCALPHA)
                    pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=self.U(12))
                    rounded.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
                    img = rounded
                    if self.spot_cover[0] == url:
                        self.spot_cover = (url, img)
                except Exception as e:
                    log("spotify kapak:", e)
            threading.Thread(target=fetch, daemon=True).start()
        return self.spot

    def spotify_active(self):
        st = self.spotify_state()
        return bool(st.get("name")) and (st.get("state") == "playing" or
                                          (st.get("state") == "paused" and time.time() - st.get("time", 0) < 600))

    def draw_spotify(self, surf):
        U, st = self.U, self.spot
        sc = pygame.Rect(U(56), U(372), U(560), U(150))
        panel(surf, sc, alpha=150 if self.static_theme else 105)
        cov = pygame.Rect(sc.x + U(16), sc.y + U(16), U(118), U(118))
        img = self.spot_cover[1]
        if img:
            surf.blit(img, cov)
        else:
            pygame.draw.rect(surf, (40, 46, 60), cov, border_radius=U(10))
            ico = asset_icon("play", U(44), 120)
            surf.blit(ico, ico.get_rect(center=cov.center))
        tx, tw = cov.right + U(20), sc.right - cov.right - U(44)
        playing = st.get("state") == "playing"
        _, label, color = MEDIA_SOURCES[st.get("source", "spotify")]
        text(surf, label + " · " + ("çalıyor" if playing else "duraklatıldı"), font("Inter-SemiBold", U(18)),
             color if playing else MUTED, (tx, sc.y + U(16)), shadow=False)
        f = font("Inter-SemiBold", U(28))
        text(surf, fit_text(st.get("name", ""), f, tw), f, WHITE, (tx, sc.y + U(42)), shadow=False)
        f = font("Inter-Regular", U(21))
        text(surf, fit_text(", ".join(st.get("artists", [])), f, tw), f, SOFT, (tx, sc.y + U(80)), shadow=False)
        # ilerleme cubugu
        dur = st.get("duration") or 0
        if dur:
            pos = st.get("position", 0) + ((time.time() - st.get("time", 0)) * 1000 if playing else 0)
            bar = pygame.Rect(tx, sc.bottom - U(26), tw, U(4))
            pygame.draw.rect(surf, (80, 86, 100), bar, border_radius=U(2))
            done = bar.copy()
            done.w = int(bar.w * max(0, min(1, pos / dur)))
            if done.w:
                pygame.draw.rect(surf, WHITE, done, border_radius=U(2))

    def draw_moon(self, surf):
        U = self.U
        mc = pygame.Rect(self.w - U(56) - U(470), U(372), U(470), U(150))
        panel(surf, mc, alpha=150)
        age, illum, name, (nname, ndays) = moon_info()
        ico = moon_icon(U(104), round(age * 4))
        surf.blit(ico, ico.get_rect(midleft=(mc.x + U(24), mc.centery)))
        tx = mc.x + U(152)
        text(surf, "Bu gece ay", font("Inter-Medium", U(19)), MUTED, (tx, mc.y + U(18)), shadow=False)
        text(surf, name, font("Inter-SemiBold", U(28)), WHITE, (tx, mc.y + U(44)), shadow=False)
        text(surf, f"Aydınlık %{round(illum * 100)}", font("Inter-Regular", U(20)), SOFT, (tx, mc.y + U(84)),
             shadow=False)
        when = dt.date.today() + dt.timedelta(days=round(ndays))
        left = "bu gece" if ndays < 1 else f"{round(ndays)} gün sonra"
        text(surf, f"{nname}: {when.day} {MONTHS[when.month - 1]} · {left}", font("Inter-Regular", U(20)), MUTED,
             (tx, mc.y + U(112)), shadow=False)

    def draw_news(self, surf, y):
        """Tarihin altinda sakin bir manset: NEWS_SHOW saniyede bir degisir, 2 sn'de yavasca belirir."""
        news = self.state.news if self.cfg["news"] else []
        if not news:
            return
        U = self.U
        slot, sec = divmod(int(time.time()), NEWS_SHOW)
        when, title = news[slot % len(news)]
        alpha = (90, 170)[sec] if sec < 2 else 230
        f, lh = font("Inter-Regular", U(23)), U(30)
        x = U(60)
        if when:
            x = text(surf, when, font("Inter-Medium", U(19)), MUTED, (x, y + U(3)), alpha=alpha).right + U(14)
        lines = max(1, (U(368) - y) // lh)  # medya karti y=372'de baslar
        for i, s in enumerate(wrap_lines(title, f, U(694) - x, lines)):
            text(surf, s, f, SOFT, (x, y + i * lh), alpha=alpha)

    def build_static(self):
        """Saat disindaki her sey: veri degisince bir kez cizilir."""
        with self.state.lock:
            weather, path, meta = self.state.weather, self.state.wall_path, dict(self.state.wall_meta)
            self.static_version = self.state.version
        dark = self.static_theme = self.evening_active()
        self.static_day = dt.date.today()
        surf = self.wallpaper(path, dark)
        U, W, H = self.U, self.w, self.h
        alpha = 150 if dark else 105

        # Hava durumu karti (sag ust)
        card = pygame.Rect(W - U(56) - U(470), U(52), U(470), U(300))
        panel(surf, card, alpha=alpha)
        city = self.cfg.get("city") or "Konum aranıyor"
        text(surf, city, font("Inter-SemiBold", U(28)), WHITE, (card.x + U(28), card.y + U(22)), shadow=False)
        if weather:
            cur = weather["current"]
            desc, kind = WMO.get(cur["weather_code"], ("-", "cloudy"))
            surf.blit(weather_icon(kind, U(130), bool(cur.get("is_day", 1))), (card.x + U(20), card.y + U(62)))
            text(surf, f"{round(cur['temperature_2m'])}°", font("InterDisplay-Light", U(112)), WHITE,
                 (card.x + U(170), card.y + U(52)), shadow=False)
            text(surf, desc, font("Inter-Medium", U(28)), SOFT, (card.x + U(28), card.y + U(198)), shadow=False)
            small = font("Inter-Regular", U(20))
            text(surf, f"Hissedilen {round(cur['apparent_temperature'])}°   Nem %{cur['relative_humidity_2m']}   "
                       f"Rüzgar {round(cur['wind_speed_10m'])} km/s",
                 small, MUTED, (card.x + U(28), card.y + U(238)), shadow=False)
            d = weather["daily"]
            text(surf, f"Gün doğumu {d['sunrise'][0][-5:]}   Gün batımı {d['sunset'][0][-5:]}",
                 small, MUTED, (card.x + U(28), card.y + U(266)), shadow=False)

            # 6 gunluk tahmin (alt)
            fc = pygame.Rect(U(56), H - U(56) - U(196), W - U(112), U(196))
            panel(surf, fc, alpha=alpha)
            n = len(d["time"])
            colw = fc.w / n
            for i in range(n):
                cx = int(fc.x + colw * (i + 0.5))
                day = dt.date.fromisoformat(d["time"][i])
                label = "Bugün" if i == 0 else DAYS_SHORT[day.weekday()]
                text(surf, label, font("Inter-SemiBold", U(24)), WHITE if i == 0 else SOFT,
                     (cx, fc.y + U(18)), "midtop", shadow=False)
                _, k = WMO.get(d["weather_code"][i], ("-", "cloudy"))
                ico = weather_icon(k, U(72))
                surf.blit(ico, ico.get_rect(midtop=(cx, fc.y + U(52))))
                hi = font("Inter-SemiBold", U(26)).render(f"{round(d['temperature_2m_max'][i])}°", True, WHITE)
                lo = font("Inter-Regular", U(26)).render(f"{round(d['temperature_2m_min'][i])}°", True, MUTED)
                gap = U(12)
                x0 = cx - (hi.get_width() + gap + lo.get_width()) // 2
                surf.blit(hi, (x0, fc.y + U(130)))
                surf.blit(lo, (x0 + hi.get_width() + gap, fc.y + U(130)))
                rain = d.get("precipitation_probability_max", [None] * n)[i]
                if rain:
                    text(surf, f"%{rain} yağış", font("Inter-Medium", U(17)), RAIN_BLUE,
                         (cx, fc.y + U(164)), "midtop", shadow=False)
        else:
            text(surf, "Hava durumu yükleniyor…", font("Inter-Regular", U(24)), MUTED,
                 (card.x + U(28), card.y + U(130)), shadow=False)

        if dark:
            self.draw_moon(surf)
        self.static = surf

    def frame(self, overlays=True):
        if (self.static is None or self.static_version != self.state.version
                or self.static_theme != self.evening_active() or self.static_day != dt.date.today()):
            self.build_static()
        surf = self.static.copy()
        U, W, H = self.U, self.w, self.h
        now = dt.datetime.now()

        # Saat + tarih (sol ust)
        clock = text(surf, now.strftime("%H:%M"), font("InterDisplay-Light", U(200)), WHITE, (U(48), U(10)))
        text(surf, now.strftime(":%S"), font("InterDisplay-Light", U(72)), SOFT,
             (clock.right + U(6), clock.bottom - U(38)), "bottomleft")
        date = f"{now.day} {MONTHS[now.month - 1]} {now.year}, {DAYS[now.weekday()]}"
        drect = text(surf, date, font("Inter-Medium", U(40)), WHITE, (U(58), clock.bottom - U(16)))
        # Selamlama yerine: bugun resmi / dini bir gunse
        line, color = special_line(self.state.special, now.date())
        if line:
            f = font("Inter-Regular", U(26))
            drect = text(surf, fit_text(line, f, U(680)), f, color, (U(60), drect.bottom + U(10)))  # hava karti x=754
        self.draw_news(surf, drect.bottom + U(16))

        if self.spotify_active():
            self.draw_spotify(surf)

        # Durum satiri (sol alt kenar): IP her zaman gorunur
        if time.time() - self.status_time > 15:
            ssid = wifi_name()
            online = ssid not in ("-", "", "kurulum-ap")
            self.status = (ip_address(), ssid if online else "", cpu_temp(),
                           wifi_signal() if online else None, bluetooth_on())
            self.status_time = time.time()
        ip, ssid, temp, level, bt = self.status
        line = f"IP  {ip}"
        if temp is not None:
            line += f"     CPU  {temp:.0f}°C"
        if getattr(self, "now_playing", None):
            line += f"     ♪  {self.now_playing}"
        text(surf, line, font("Inter-Medium", U(20)), WHITE, (U(58), H - U(18)), "bottomleft")

        # Piyasa satiri (sag alt kenar)
        r = self.state.rates
        if r:
            segs = []
            for label, val, dec, chg in (("USD", r["usd"], 2, r["usd_chg"]), ("Gram altın", r["gold"], 0, r["gold_chg"])):
                if segs:
                    segs.append(("      ", WHITE))
                segs.append((f"{label}  {tr_number(val, dec)} ₺", WHITE))
                if chg is not None:
                    up = chg >= 0
                    segs.append((f"  {'▲' if up else '▼'} %{tr_number(abs(chg), 2)}", UP_GREEN if up else DOWN_RED))
            f = font("Inter-Medium", U(20))
            x = W - U(56) - sum(f.size(t)[0] for t, _ in segs)
            for t, c in segs:
                x = text(surf, t, f, c, (x, H - U(18)), "bottomleft").right

        # Telefon tarzi durum simgeleri (sag ust kose): ag adi, Wi-Fi sinyali, Bluetooth
        size = U(30)
        top = U(12)
        x = W - U(56) - size
        surf.blit(bluetooth_icon(size, bt), (x, top))
        self.bt_hit = pygame.Rect(x - U(8), 0, W - x + U(8), U(80))
        wifi = wifi_icon(size, level)
        x -= wifi.get_width() + U(16)
        surf.blit(wifi, (x, top))
        left = x
        if ssid:
            left = text(surf, ssid, font("Inter-Medium", U(20)), WHITE, (x - U(12), top + size // 2), "midright",
                        shadow=False).x
        self.wifi_hit = pygame.Rect(left - U(30), 0, self.bt_hit.x - left + U(30), U(80))  # parmak icin genis alan

        # Wi-Fi kurulum durumu (wifi_setup.py)
        ws = self.wifi_state()
        mode = ws.get("mode")
        self.ap_button = None
        if not overlays:
            return surf
        if mode == "ap":
            self.draw_wifi_setup(surf, ws)
        elif mode == "connecting":
            self.pill(surf, f"“{ws['ssid']}” ağına bağlanılıyor…")
        elif mode == "error":
            self.pill(surf, f"“{ws['ssid']}” ağına bağlanılamadı", DOWN_RED)
        elif mode == "checking" and time.time() - ws.get("time", 0) > 10:
            self.pill(surf, "Wi-Fi aranıyor…")
        return surf


# ---------------------------------------------------------------- ekran

class Framebuffer:
    def __init__(self, dev="/dev/fb0"):
        sysfs = "/sys/class/graphics/" + Path(dev).name + "/"
        self.w, self.h = (int(v) for v in Path(sysfs + "virtual_size").read_text().split(","))
        self.bpp = int(Path(sysfs + "bits_per_pixel").read_text())
        self.stride = int(Path(sysfs + "stride").read_text())
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, self.stride * self.h)
        self.surf = pygame.Surface((self.w, self.h), 0, self.bpp)

    def show(self, frame):
        self.surf.blit(frame, (0, 0))
        raw = self.surf.get_buffer().raw
        pitch = self.surf.get_pitch()
        if pitch == self.stride:
            self.mm[:len(raw)] = raw
        else:
            row = self.w * self.bpp // 8
            for y in range(self.h):
                self.mm[y * self.stride:y * self.stride + row] = raw[y * pitch:y * pitch + row]

    def clear(self):
        self.mm[:] = bytes(len(self.mm))


class MpvVideo:
    """YouTube videosu mpv'de (DRM, sesle senkron) oynar; panel ona yer ve ust katman verir.

    Video acikken ekrani mpv tutar: /dev/fb0'a yazilanlar gorunmez. Sayfa, video yeri saydam
    (yuvarlak koseli) BGRA katman olarak mpv'ye verilir (overlay-add)."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.on, self.key, self.flip = None, None, 0

    def cmd(self, *command):
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(2)
                s.connect(MPV_SOCK)
                s.sendall((json.dumps({"command": list(command)}) + "\n").encode())
        except OSError as e:
            log("mpv:", e)

    def set_video(self, on, rect, key):
        """Video goster/gizle; degisiklik olduysa True. Yeni videoda alici vid=no ile baslatir: yeniden ayarla."""
        if key != self.key:
            self.key, self.on = key, None
        if on == self.on:
            return False
        self.on = on
        if on:
            for side, v in (("left", rect.x / self.w), ("right", 1 - rect.right / self.w),
                            ("top", rect.y / self.h), ("bottom", 1 - rect.bottom / self.h)):
                self.cmd("set_property", f"video-margin-ratio-{side}", round(v, 5))
            self.cmd("set_property", "vid", "auto")
        else:
            self.cmd("set_property", "vid", "no")
            self.cmd("overlay-remove", 1)
        return True

    def overlay(self, frame, hole, radius):
        ov = pygame.Surface((self.w, self.h), pygame.SRCALPHA, 32)
        ov.blit(frame, (0, 0))
        pygame.draw.rect(ov, (0, 0, 0, 0), hole, border_radius=radius)  # video yeri saydam
        self.flip ^= 1  # mpv onceki dosyayi okurken uzerine yazilmasin
        path = f"/dev/shm/pi-overlay{self.flip}.bgra"
        with open(path, "wb") as f:
            f.write(pygame.image.tobytes(ov, "BGRA"))
        self.cmd("overlay-add", 1, 0, 0, path, 0, "bgra", self.w, self.h, self.w * 4)


def main():
    pygame.font.init()
    CACHE.mkdir(exist_ok=True)
    cfg = load_config()
    state = State()
    threading.Thread(target=worker, args=(state, cfg), daemon=True).start()

    if "--shot" in sys.argv:
        out = sys.argv[sys.argv.index("--shot") + 1]
        deadline = time.time() + 60  # verilerin gelmesini bekle
        while time.time() < deadline and (state.weather is None or state.wall_path is None or state.rates is None
                                         or (cfg["news"] and not state.news)):
            time.sleep(0.5)
        dash = Dashboard(1280, 800, state, cfg)
        if "--ap-demo" in sys.argv:  # kurulum ekranini Wi-Fi'yi kesmeden onizle
            dash.wifi_state = lambda: {"mode": "ap", "ssid": "Pi-Kurulum", "password": "pikurulum",
                                       "url": "http://10.42.0.1", "error": None}
        dash.force_evening = "--evening" in sys.argv
        if "--spotify-demo" in sys.argv:
            dash.spotify_state = lambda: dash.spot
            dash.spot = {"state": "playing", "name": "Gel Gör Beni Aşk Neyledi", "artists": ["Kubat"],
                         "source": "youtube",
                         "duration": 240000, "position": 90000, "time": time.time()}
        frame = dash.night_frame() if "--night" in sys.argv else dash.frame()
        if "--ui" in sys.argv and sys.argv[sys.argv.index("--ui") + 1] == "settings":  # ayarlar paneli
            ui = wifi_ui.SettingsUI(sys.modules[__name__], 1280, 800, cfg, HERE / "config.json")
            ui.open()
            frame = ui.render(dash.frame(overlays=False))
        elif "--ui" in sys.argv and sys.argv[sys.argv.index("--ui") + 1] == "yt":  # YouTube Music sayfasi
            ui = wifi_ui.YtUI(sys.modules[__name__], 1280, 800)
            ui.backdrop = dash.backdrop
            ui.open()
            frame = ui.render(dash.frame(overlays=False))
        elif "--ui" in sys.argv and sys.argv[sys.argv.index("--ui") + 1] == "radio":  # radyo sayfasi onizleme
            ui = wifi_ui.RadioUI(sys.modules[__name__], 1280, 800)
            ui.backdrop = dash.backdrop
            ui.mode = "page"
            ui.refresh_sink()
            ui.playing, ui.status, ui.track = 1, "Çalıyor", ("Seni Düşündüm", "Çağla")
            frame = ui.render(dash.frame(overlays=False))
        elif "--ui" in sys.argv and sys.argv[sys.argv.index("--ui") + 1] == "bt":  # Bluetooth menusu onizleme
            ui = wifi_ui.BtUI(sys.modules[__name__], 1280, 800)
            ui.data, ui.loaded, ui.mode = ui.api("/api/bt", timeout=20), True, "list"
            frame = ui.render(dash.frame(overlays=False))
        elif "--ui" in sys.argv:  # dokunmatik menuyu onizle (gercek ag listesiyle)
            ui = wifi_ui.WifiUI(sys.modules[__name__], 1280, 800)
            d = ui.api("/api/networks", timeout=40)
            ui.networks, ui.current = d.get("networks", []), d.get("current")
            ui.mode = sys.argv[sys.argv.index("--ui") + 1]
            ui.target = ui.networks[1] if len(ui.networks) > 1 else {"ssid": "Ornek", "security": "WPA2"}
            ui.text, ui.conn_ssid, ui.result_msg = "sifre123", ui.target["ssid"], ""
            ui.forget_ssid = ui.target["ssid"]
            frame = ui.render(dash.frame(overlays=False))
        pygame.image.save(frame, out)
        log("kaydedildi:", out)
        return

    fb = Framebuffer()
    dash = Dashboard(fb.w, fb.h, state, cfg)
    dash.force_evening = "--evening" in sys.argv  # denemek icin: aksam modu gunduz de
    wifi_menu = wifi_ui.WifiUI(sys.modules[__name__], fb.w, fb.h)
    bt_menu = wifi_ui.BtUI(sys.modules[__name__], fb.w, fb.h)
    radio = wifi_ui.RadioUI(sys.modules[__name__], fb.w, fb.h)
    radio.backdrop = dash.backdrop
    yt = wifi_ui.YtUI(sys.modules[__name__], fb.w, fb.h)
    yt.backdrop = dash.backdrop
    settings = wifi_ui.SettingsUI(sys.modules[__name__], fb.w, fb.h, cfg, HERE / "config.json")
    touch = wifi_ui.Touch(fb.w, fb.h)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    base, base_key, shown = None, None, None
    yt_video_seen = None
    mpv = MpvVideo(fb.w, fb.h)
    wake_until, force_night, spotify_seen, radio_was = 0, False, 0, None  # gece ekranina dokununca 1 dk uyanir; ayarlardan "goster"
    try:
        while True:
            ui = next((mn for mn in (radio, yt, settings, bt_menu, wifi_menu) if mn.active), wifi_menu)  # acik menu
            idle = time.time() - max(ui.last_touch, wake_until - 60)
            # gece: saat araliginda ya da bilgisayar kilitliyken; dokununca 1 dk uyanir
            dark = dash.night_active() or PC_LOCKED.exists()
            night = force_night or (dark and time.time() > wake_until and (not ui.active or idle > 60))
            if night and ui.active:
                ui.close()  # gece: acik sayfa kapanir (radyo calmaya devam eder)
            # Dokunma bekle: menu animasyonluysa kisa, degilse bir sonraki saniyeye kadar
            if ui.animating() and not night:
                wait = getattr(ui, "frame_interval", 0.08)
            else:
                wait = 1.02 - time.time() % 1
            try:
                events = [touch.events.get(timeout=max(0.01, wait))]
                while not touch.events.empty():
                    events.append(touch.events.get_nowait())
            except queue.Empty:
                events = []
            was_active = ui.active
            if night:
                if any(k in ("tap", "swipe", "vswipe") for k, _, _ in events):
                    force_night, wake_until = False, time.time() + 60
                events = []  # gece ekranina ilk dokunus sadece uyandirir
            elif events and dark:
                wake_until = time.time() + 60  # kullanildikca uyanik kalsin
            for kind, x, y in events:
                if kind == "swipe":  # yatay kaydirma (x = dx): sola -> radyo, saga -> YouTube Music
                    if ui in (radio, yt):
                        back = x > 0 if ui is radio else x < 0  # geldigi yonun tersi: ana ekran
                        if back and ui.swipe_allowed():
                            ui.close()
                    elif not ui.active:
                        ui = radio if x < 0 else yt
                        ui.open()
                elif kind == "vswipe":  # x = baslangic y'si, y = dikey mesafe
                    if ui is settings and y < 0:
                        settings.close()
                    elif not ui.active and x < dash.U(200) and y > 0:  # ust kenardan asagi: ayarlar
                        ui = settings
                        settings.open()
                elif ui.active:
                    ui.handle(kind, x, y)
                elif kind == "tap" and dash.bt_hit and dash.bt_hit.collidepoint(x, y):
                    ui = bt_menu
                    ui.open()
                elif kind == "tap" and any(r and r.collidepoint(x, y) for r in (dash.wifi_hit, dash.ap_button)):
                    ui = wifi_menu
                    ui.open()
            if ui.active and ui.auto_close and ui.mode != "connecting" and time.time() - ui.last_touch > 90:
                ui.close()  # unutulan menu ekranda kalmasin
            if radio.playing is not None and radio.playing != radio_was and dash.media["youtube"][0].get("state") == "playing":
                yt.cmd("/pause")
            radio_was = radio.playing
            ytst = dash.media["youtube"][0]  # telefondan yeni video yayinlaninca YouTube sayfasi acilir
            if ytst.get("video") and ytst.get("state") == "playing" and ytst.get("name") != yt_video_seen:
                yt_video_seen = ytst.get("name")
                if not ui.active and not night:
                    ui = yt
                    yt.open()
            sp = dash.spotify_state()
            if sp.get("state") == "playing" and sp.get("time", 0) > spotify_seen:
                spotify_seen = sp["time"]
                if radio.playing is not None:
                    radio.stop()
            if settings.request:  # ayarlardaki kisayollar
                req, settings.request = settings.request, None
                if req == "night":
                    force_night = True
                else:
                    ui = {"wifi": wifi_menu, "bt": bt_menu, "radio": radio, "yt": yt}[req]
                    ui.open()
            if was_active and not ui.active:
                dash.status_time = 0  # Wi-Fi / Bluetooth simgeleri hemen guncellensin

            # Menu acikken arka plan (saat) donar: her saniye 100+ ms'lik yeniden cizim yazmayi yavaslatmasin
            if night:
                key = ("night", dt.datetime.now().strftime("%H:%M"))
            else:
                key = ("menu" if ui.active else dt.datetime.now().strftime("%H:%M:%S"), state.version, ui.active)
            dash.now_playing = wifi_ui.STATIONS[radio.playing][0] if radio.playing is not None else None
            if key != base_key:
                t0 = time.time()
                base = dash.night_frame() if night else dash.frame(overlays=not ui.active)
                if base_key is None:
                    log(f"ilk kare {time.time() - t0:.2f} sn")
                base_key = key
            show_ui = ui.active and not night
            # YouTube sayfasinda video: mpv ekrani alir, sayfa onun ustune saydam katman olur
            video = show_ui and ui is yt and yt.video_mode()
            if mpv.set_video(video, yt.video_rect(), yt.st.get("name")) and not video:
                shown = None  # mpv ekrani birakti: panel yeniden yazilsin
            view = (base_key, id(ui), ui.version, ui.mode, show_ui and ui.animating() and time.time())
            if view != shown:
                frame = ui.render(base) if show_ui else base
                if video:
                    mpv.overlay(frame, yt.video_rect(), yt.video_radius())
                else:
                    fb.show(frame)
                shown = view
    finally:
        fb.clear()


if __name__ == "__main__":
    main()
