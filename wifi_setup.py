#!/usr/bin/env python3
"""Wi-Fi kurulum servisi (root olarak calisir).

Kayitli hicbir aga baglanilamazsa "Pi-Kurulum" erisim noktasini acar ve 10.42.0.1 uzerinde
telefon icin bir ayar sayfasi (captive portal) sunar. Girilen ag NetworkManager'a kalici
olarak kaydedilir. AP acikken belirli araliklarla kayitli aglar tekrar denenir; boylece
modem gec acilsa bile Pi kendiliginden geri baglanir.

Panel (dashboard.py) durumu /run/wifi-setup/state.json dosyasindan okuyup ekranda gosterir;
dokunmatik Wi-Fi menusu icin 127.0.0.1:8099 uzerindeki yerel API'yi kullanir:
  GET  /api/networks[?rescan=1]   GET /api/status
  POST /api/connect {ssid, password, hidden}   POST /api/forget {ssid}
  GET  /api/bt   POST /api/bt/power {on}   POST /api/bt/scan   POST /api/bt/<pair|connect|disconnect|remove> {mac}
"""
import html
import json
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bt_backend

AP_SSID = "Pi-Kurulum"
AP_PASS = "pikurulum"
AP_CON = "kurulum-ap"
IFACE = "wlan0"
PORTAL_IP = "10.42.0.1"
STATE = Path("/run/wifi-setup/state.json")
DNS_HIJACK = Path("/etc/NetworkManager/dnsmasq-shared.d/captive-portal.conf")

WAIT_ONLINE = 90        # acilista / baglanti kopunca AP acmadan once bekleme (sn)
AP_RETRY = 3 * 60       # AP acikken bu araliklarla kayitli aglari tekrar dene
PORTAL_IDLE = 2 * 60    # biri ayar sayfasini kullaniyorsa tekrar denemeyi ertele
RETRY_WINDOW = 45       # tekrar denemede baglanti icin bekleme
API_ADDR = ("127.0.0.1", 8099)

AP_ACTIVE = threading.Event()   # kurulum AP'si acik mi
CONNECT_LOCK = threading.Lock()
CONNECT = {"state": "idle"}     # ekrandan baslatilan son baglanma denemesi: connecting / ok / error


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def nmcli(*args, timeout=60, check=False):
    r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r


def split_terse(line):
    """nmcli -t ciktisini boler; degerlerdeki '\\:' kacisini korur."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def write_state(**kw):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    kw["time"] = time.time()
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def wifi_connected():
    """wlan0 normal bir aga bagliysa baglanti adini, degilse None dondurur."""
    for line in nmcli("-t", "-f", "DEVICE,STATE,CONNECTION", "dev").stdout.splitlines():
        dev, state, con = (split_terse(line) + ["", ""])[:3]
        if dev == IFACE and state == "connected" and con != AP_CON:
            return con
    return None


def wait_online(seconds):
    end = time.time() + seconds
    while time.time() < end:
        con = wifi_connected()
        if con:
            return con
        time.sleep(3)
    return None


def scan(rescan=True):
    if rescan:
        nmcli("dev", "wifi", "rescan", timeout=20)
        time.sleep(5)
    nets = {}
    out = nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "no").stdout
    for line in out.splitlines():
        in_use, ssid, signal_, sec = (split_terse(line) + ["", "", "", ""])[:4]
        if not ssid or ssid == AP_SSID:
            continue
        sig = int(signal_ or 0)
        if ssid not in nets or nets[ssid]["signal"] < sig or in_use == "*":
            sec = "" if sec in ("", "--") else sec
            nets[ssid] = {"ssid": ssid, "signal": sig, "security": sec, "in_use": in_use == "*",
                          "wpa3_only": "WPA3" in sec and "WPA2" not in sec and "WPA1" not in sec}
    return sorted(nets.values(), key=lambda n: (not n["in_use"], -n["signal"]))


def saved_profiles():
    """{ssid: [profil adlari]} - kayitli Wi-Fi aglari (kurulum AP'si haric)."""
    result = {}
    for line in nmcli("-t", "-f", "NAME,TYPE", "con", "show").stdout.splitlines():
        name, ctype = (split_terse(line) + [""])[:2]
        if ctype != "802-11-wireless" or name == AP_CON:
            continue
        ssid = nmcli("-g", "802-11-wireless.ssid", "con", "show", name).stdout.strip()
        if ssid:
            result.setdefault(ssid, []).append(name)
    return result


def set_connect(state, ssid, message=""):
    CONNECT.clear()
    CONNECT.update(state=state, ssid=ssid, message=message, time=time.time())


def start_ap():
    DNS_HIJACK.parent.mkdir(parents=True, exist_ok=True)
    DNS_HIJACK.write_text(f"address=/#/{PORTAL_IP}\n")  # her alan adi -> ayar sayfasi
    nmcli("con", "delete", AP_CON)
    nmcli("con", "add", "type", "wifi", "ifname", IFACE, "con-name", AP_CON, "autoconnect", "no",
          "ssid", AP_SSID, "mode", "ap", "802-11-wireless.band", "bg", "802-11-wireless.channel", "6",
          "ipv4.method", "shared", "ipv4.addresses", f"{PORTAL_IP}/24", "ipv6.method", "disabled",
          "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", AP_PASS,
          "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", check=True)
    nmcli("con", "up", AP_CON, check=True)
    log("AP acildi:", AP_SSID)


def stop_ap():
    nmcli("con", "down", AP_CON)
    nmcli("con", "delete", AP_CON)
    DNS_HIJACK.unlink(missing_ok=True)


def save_and_connect(ssid, password, hidden):
    """Agi kalici profil olarak kaydeder ve baglanir. Basarisizsa profili siler."""
    name = f"wifi-{ssid}"
    nmcli("con", "delete", name)
    args = ["con", "add", "type", "wifi", "ifname", IFACE, "con-name", name, "ssid", ssid,
            "connection.autoconnect", "yes", "connection.autoconnect-priority", "20",
            "connection.autoconnect-retries", "0"]
    if hidden:
        args += ["802-11-wireless.hidden", "yes"]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    nmcli(*args, check=True)
    r = nmcli("--wait", "45", "con", "up", name, timeout=70)
    if r.returncode == 0 and wifi_connected():
        log("baglandi ve kaydedildi:", ssid)
        for old in saved_profiles().get(ssid, []):  # ayni agin eski kayitlarini temizle (sifre degismis olabilir)
            if old != name:
                nmcli("con", "delete", old)
        return None
    nmcli("con", "delete", name)
    err = (r.stderr or r.stdout).strip().splitlines()
    log("baglanilamadi:", ssid, err[-1] if err else "")
    return "Bağlanılamadı. Şifreyi ve ağ adını kontrol edin."


# ---------------------------------------------------------------- ayar sayfasi

PAGE = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pi Wi-Fi Kurulumu</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1320;color:#e8edf6}}
main{{max-width:460px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:22px;margin:8px 0 4px}} p{{color:#9aa6bb;font-size:14px;line-height:1.45}}
.err{{background:#4a1d24;color:#ffd0d6;padding:12px 14px;border-radius:12px;font-size:14px}}
.net{{display:flex;align-items:center;gap:12px;background:#18213a;border-radius:14px;padding:14px;margin:8px 0;cursor:pointer}}
.net input{{width:20px;height:20px;accent-color:#4f8cff}} .net span{{flex:1;font-size:16px;word-break:break-all}}
.net small{{color:#9aa6bb;font-size:12px}} .warn{{color:#ffb4a0}}
label.f{{display:block;font-size:14px;color:#9aa6bb;margin:18px 0 6px}}
input[type=text],input[type=password]{{width:100%;box-sizing:border-box;font-size:17px;padding:13px;border-radius:12px;border:1px solid #2c3858;background:#111a2e;color:#fff}}
.show{{font-size:14px;color:#9aa6bb;margin-top:8px;display:flex;gap:8px;align-items:center}}
button{{width:100%;margin-top:22px;padding:15px;font-size:17px;font-weight:600;border:0;border-radius:14px;background:#4f8cff;color:#fff}}
</style></head><body><main>
<h1>Wi-Fi ayarı</h1>
<p>Pi'nin bağlanacağı ağı seçin ve şifresini girin. Pi sadece 2.4 GHz ağları görebilir.</p>
{error}
<form method="post" action="/save">
{networks}
<label class="net"><input type="radio" name="ssid" value="__other__" {other_checked}><span>Başka bir ağ (gizli ağ)</span></label>
<input type="text" name="other" placeholder="Ağ adı (SSID)" autocapitalize="off" autocorrect="off">
<label class="f" for="pw">Şifre</label>
<input type="password" id="pw" name="password" autocomplete="off" placeholder="Şifresiz ağ ise boş bırakın">
<div class="show"><input type="checkbox" id="sh" onclick="pw.type=this.checked?'text':'password'"><label for="sh">Şifreyi göster</label></div>
<button type="submit">Kaydet ve bağlan</button>
</form></main></body></html>"""

DONE = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bağlanılıyor</title>
<style>body{{margin:0;font-family:system-ui,sans-serif;background:#0d1320;color:#e8edf6}}
main{{max-width:460px;margin:0 auto;padding:40px 20px}} p{{color:#9aa6bb;line-height:1.5}}</style></head>
<body><main><h1>Kaydedildi ✓</h1><p><b>{ssid}</b> ağına bağlanılıyor.</p>
<p>Pi bu kurulum ağını kapatacağı için telefonunuzun bağlantısı kopacak. Sonucu Pi ekranında görebilirsiniz.
Bağlanamazsa kurulum ağı birkaç saniye içinde tekrar açılır.</p></main></body></html>"""


class Portal:
    def __init__(self):
        self.networks = []
        self.error = None
        self.requests = queue.Queue()
        self.last_hit = 0
        self.server = None

    def page(self):
        rows = []
        for i, n in enumerate(self.networks):
            bars = "▂▄▆█"[: max(1, min(4, n["signal"] // 25 + 1))]
            note = '<small class="warn">WPA3 – desteklenmiyor olabilir</small>' if n["wpa3_only"] else \
                f'<small>{bars} {"🔒" if n["security"] else "açık"}</small>'
            rows.append(f'<label class="net"><input type="radio" name="ssid" value="{html.escape(n["ssid"])}"'
                        f'{" checked" if i == 0 else ""}><span>{html.escape(n["ssid"])}</span>{note}</label>')
        if not rows:
            rows.append("<p>Yakında ağ bulunamadı. Ağ adını aşağıya elle yazabilirsiniz.</p>")
        err = f'<div class="err">{html.escape(self.error)}</div>' if self.error else ""
        return PAGE.format(networks="\n".join(rows), error=err,
                           other_checked="" if self.networks else "checked")

    def start(self):
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body="", headers=None):
                data = body.encode("utf-8")
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if data:
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                portal.last_hit = time.time()
                host = self.headers.get("Host", "")
                if self.path != "/" or host not in (PORTAL_IP, ""):
                    # Telefonlarin internet kontrolu (generate_204, hotspot-detect...) -> ayar sayfasina yonlendir
                    return self.send(302, headers={"Location": f"http://{PORTAL_IP}/"})
                self.send(200, portal.page())

            def do_POST(self):
                portal.last_hit = time.time()
                if self.path != "/save":
                    return self.send(404)
                length = int(self.headers.get("Content-Length", 0))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                ssid = form.get("ssid", [""])[0]
                hidden = ssid == "__other__"
                if hidden:
                    ssid = form.get("other", [""])[0].strip()
                password = form.get("password", [""])[0]
                if not ssid:
                    portal.error = "Lütfen bir ağ seçin veya ağ adını yazın."
                    return self.send(303, headers={"Location": "/"})
                if password and not 8 <= len(password) <= 63:
                    portal.error = "Wi-Fi şifresi 8 ile 63 karakter arasında olmalı."
                    return self.send(303, headers={"Location": "/"})
                portal.error = None
                self.send(200, DONE.format(ssid=html.escape(ssid)))
                portal.requests.put((ssid, password, hidden))

        self.server = ThreadingHTTPServer((PORTAL_IP, 80), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# ---------------------------------------------------------------- ana dongu

def wait_for_portal(portal):
    """'saved' (baglandi), 'error' (sifre yanlis vb.) ya da 'retry' (kimse kullanmadi) dondurur."""
    opened = time.time()
    while True:
        try:
            ssid, password, hidden = portal.requests.get(timeout=3)
        except queue.Empty:
            if time.time() - opened > AP_RETRY and time.time() - portal.last_hit > PORTAL_IDLE:
                return "retry"
            continue
        time.sleep(2)  # telefona "kaydedildi" sayfasi ulassin
        portal.stop()
        stop_ap()
        write_state(mode="connecting", ssid=ssid)
        set_connect("connecting", ssid)
        portal.error = save_and_connect(ssid, password, hidden)
        if portal.error is None:
            set_connect("ok", ssid)
            return "saved"
        set_connect("error", ssid, portal.error)
        write_state(mode="error", ssid=ssid, error=portal.error)
        time.sleep(4)
        return "error"


def run_ap_mode(portal):
    """AP'yi acar; biri ag kaydedip baglanirsa ya da kayitli bir ag geri gelirse doner."""
    while True:
        portal.networks = scan()
        start_ap()
        AP_ACTIVE.set()
        portal.start()
        write_state(mode="ap", ssid=AP_SSID, password=AP_PASS, url=f"http://{PORTAL_IP}", error=portal.error)
        result = wait_for_portal(portal)
        portal.stop()
        stop_ap()
        AP_ACTIVE.clear()
        if result == "saved":
            return
        if result == "error":
            continue  # kurulum agini hemen tekrar ac
        # kimse kullanmiyor: AP kapali, NetworkManager kayitli aglari denesin
        portal.error = None
        write_state(mode="checking")
        nmcli("dev", "wifi", "rescan", timeout=20)
        nmcli("dev", "connect", IFACE, timeout=60)
        if wait_online(RETRY_WINDOW):
            return


# ---------------------------------------------------------------- yerel API (dokunmatik menu)

def connect_from_screen(portal, ssid, password, hidden):
    set_connect("connecting", ssid)
    if AP_ACTIVE.is_set():
        portal.requests.put((ssid, password, hidden))  # kurulum modundaki akis halleder
        return
    with CONNECT_LOCK:
        saved = saved_profiles().get(ssid)
        if saved and not password and not hidden:
            r = nmcli("--wait", "45", "con", "up", saved[0], timeout=70)
            err = None if r.returncode == 0 and wifi_connected() else "Bağlanılamadı."
        else:
            err = save_and_connect(ssid, password, hidden)
        set_connect("ok" if err is None else "error", ssid, err or "")


def start_api(portal):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/api/networks":
                if AP_ACTIVE.is_set():
                    nets = portal.networks  # AP acikken tarama yapilamaz; son liste
                else:
                    nets = scan(rescan="rescan=1" in url.query)
                saved = saved_profiles()
                for n in nets:
                    n["saved"] = n["ssid"] in saved
                return self.reply({"networks": nets, "current": wifi_connected(), "ap": AP_ACTIVE.is_set()})
            if url.path == "/api/bt":
                return self.reply(bt_backend.status())
            if url.path == "/api/status":
                return self.reply({"connect": dict(CONNECT), "current": wifi_connected(), "ap": AP_ACTIVE.is_set()})
            self.reply({"error": "yok"}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path.startswith("/api/bt/"):
                action = self.path.rsplit("/", 1)[1]
                if action == "power":
                    bt_backend.set_power(bool(body.get("on")))
                    if body.get("on"):
                        bt_backend.scan()
                elif action == "scan":
                    bt_backend.scan()
                elif action in ("pair", "connect", "disconnect", "remove") and body.get("mac"):
                    bt_backend.run_op(action, body["mac"])
                else:
                    return self.reply({"error": "gecersiz istek"}, 400)
                return self.reply({"ok": True})
            ssid = (body.get("ssid") or "").strip()
            if not ssid:
                return self.reply({"error": "ssid gerekli"}, 400)
            if self.path == "/api/connect":
                password = body.get("password") or ""
                if password and not 8 <= len(password) <= 63:
                    return self.reply({"error": "Şifre 8-63 karakter olmalı"}, 400)
                threading.Thread(target=connect_from_screen,
                                 args=(portal, ssid, password, bool(body.get("hidden"))), daemon=True).start()
                return self.reply({"ok": True})
            if self.path == "/api/forget":
                for name in saved_profiles().get(ssid, []):
                    nmcli("con", "delete", name)
                log("unutuldu:", ssid)
                return self.reply({"ok": True})
            self.reply({"error": "yok"}, 404)

    server = ThreadingHTTPServer(API_ADDR, Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    portal = Portal()
    start_api(portal)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        stop_ap()  # onceki calismadan kalmis olabilir
        write_state(mode="checking")
        while True:
            con = wait_online(WAIT_ONLINE)
            if not con:
                log("kayitli aglarin hicbirine baglanilamadi, kurulum modu")
                run_ap_mode(portal)
                continue
            write_state(mode="online", ssid=con)
            log("bagli:", con)
            while wifi_connected():
                time.sleep(10)
            log("baglanti koptu")
            write_state(mode="checking")
    finally:
        portal.stop()
        stop_ap()
        write_state(mode="stopped")


if __name__ == "__main__":
    main()
