"""Bluetooth islemleri (bluetoothctl uzerinden; wifi_setup.py icinde root olarak calisir).

Eslesmemis cihazlari BlueZ ~30 sn sonra unuttugu icin bulunan cihazlar burada tutulur.
Eslestirme, PIN sormayan "NoInputNoOutput" ajaniyla yapilir (kulaklik, hoparlor, fare vb.).
"""
import re
import select
import subprocess
import threading
import time
from pathlib import Path

LOCK = threading.Lock()  # ayni anda tek bluetoothctl islemi (tarama / eslestirme)
SEEN = {}                # mac -> {"name", "seen"}: taramada gorulen isimli cihazlar
ICONS = {}               # mac -> BlueZ simge turu (audio-headset, input-keyboard ...)
SCAN = {"active": False}
OP = {"state": "idle"}   # son islem: pair/connect/disconnect/remove -> busy / ok / error
MAC_RE = re.compile(r"Device ([0-9A-F:]{17}) ?(.*)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02")


def log(*a):
    print(time.strftime("%H:%M:%S"), "[bt]", *a, flush=True)


def btctl(*args, timeout=20):
    return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout).stdout


def listed(*filters):
    """{mac: ad} - 'bluetoothctl devices [Paired|Connected]'"""
    return {m.group(1): m.group(2).strip() for m in map(MAC_RE.search, btctl("devices", *filters).splitlines()) if m}


def has_name(mac, name):
    return bool(name) and name.replace("-", ":") != mac


class Session:
    """Etkilesimli bluetoothctl oturumu (ajan gerektiren eslestirme icin)."""

    def __init__(self):
        self.p = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1)

    def send(self, cmd):
        self.p.stdin.write(cmd + "\n")
        self.p.stdin.flush()
        time.sleep(0.3)

    def wait(self, done, timeout):
        """Ciktida done(satir) True/False dondurene kadar bekler; zaman asiminda None."""
        end = time.time() + timeout
        while time.time() < end:
            if not select.select([self.p.stdout], [], [], 0.5)[0]:
                continue
            line = self.p.stdout.readline()
            if not line:
                return None
            result = done(ANSI_RE.sub("", line).strip())
            if result is not None:
                return result
        return None

    def close(self):
        try:
            self.send("quit")
            self.p.wait(3)
        except (OSError, subprocess.TimeoutExpired):
            self.p.kill()


def rfkill_blocked():
    for rf in Path("/sys/class/rfkill").iterdir():
        if (rf / "type").read_text().strip() == "bluetooth":
            return (rf / "soft").read_text().strip() != "0" or (rf / "hard").read_text().strip() != "0"
    return True


def powered():
    return not rfkill_blocked() and "Powered: yes" in btctl("show")


def set_power(on):
    with LOCK:
        if on:
            subprocess.run(["rfkill", "unblock", "bluetooth"])
            time.sleep(1.5)
            btctl("power", "on")
        else:
            btctl("power", "off")
    log("guc:", "acik" if on else "kapali")


def icon_of(mac):
    if mac not in ICONS:
        m = re.search(r"^\s*Icon: (.*)$", btctl("info", mac, timeout=5), re.M)
        ICONS[mac] = m.group(1).strip() if m else ""
    return ICONS[mac]


def device_list():
    """Eslesmis cihazlar + son 2 dakikada gorulen isimli cihazlar."""
    names, paired, connected = listed(), listed("Paired"), listed("Connected")
    for mac, d in SEEN.items():
        if time.time() - d["seen"] < 120:
            names.setdefault(mac, d["name"])
    result = []
    for mac, name in names.items():
        if mac not in paired and not has_name(mac, name):
            continue  # isimsiz BLE yayinlari listeyi kalabaliklastirmasin
        result.append({"mac": mac, "name": name if has_name(mac, name) else mac,
                       "icon": icon_of(mac) if has_name(mac, name) or mac in paired else "",
                       "paired": mac in paired, "connected": mac in connected})
    return sorted(result, key=lambda x: (not x["connected"], not x["paired"], x["name"].lower()))


def scan(seconds=12):
    if SCAN["active"] or not powered():
        return
    SCAN["active"] = True

    def run():
        try:
            with LOCK:
                btctl("--timeout", str(seconds), "scan", "on", timeout=seconds + 10)
                for mac, name in listed().items():
                    if has_name(mac, name):
                        SEEN[mac] = {"name": name, "seen": time.time()}
        finally:
            SCAN["active"] = False
    threading.Thread(target=run, daemon=True).start()


def set_op(state, mac, action, message=""):
    OP.clear()
    OP.update(state=state, mac=mac, action=action, message=message, time=time.time())


def run_op(action, mac):
    """pair / connect / disconnect / remove - arka planda; sonucu OP'ye yazar."""
    set_op("busy", mac, action)

    def run():
        with LOCK:
            try:
                ok, msg = do_op(action, mac)
            except Exception as e:
                ok, msg = False, str(e)
        set_op("ok" if ok else "error", mac, action, msg)
        log(action, mac, "tamam" if ok else f"hata: {msg}")
    threading.Thread(target=run, daemon=True).start()


def pair(mac):
    s = Session()
    try:
        s.send("agent NoInputNoOutput")
        s.send("default-agent")
        if mac not in listed():  # BlueZ unutmus: ayni oturumda kisa tarama ile tekrar bul
            s.send("scan on")
            s.wait(lambda line: True if mac in line else None, 12)
            s.send("scan off")
        s.send(f"pair {mac}")
        ok = s.wait(lambda line: True if ("Pairing successful" in line or "AlreadyExists" in line)
                    else False if ("Failed to pair" in line or "not available" in line) else None, 40)
    finally:
        s.close()
    if not ok:
        return False, "Eşleştirilemedi. Cihaz eşleştirme modunda mı?"
    btctl("trust", mac)
    btctl("connect", mac, timeout=25)
    ICONS.pop(mac, None)
    return True, ""


def do_op(action, mac):
    if action == "pair":
        return pair(mac)
    if action == "connect":
        btctl("connect", mac, timeout=25)
        return mac in listed("Connected"), "Bağlanılamadı. Cihaz açık ve yakında mı?"
    if action == "disconnect":
        btctl("disconnect", mac, timeout=15)
        return mac not in listed("Connected"), ""
    if action == "remove":
        btctl("remove", mac, timeout=15)
        SEEN.pop(mac, None)
        ICONS.pop(mac, None)
        return True, ""
    return False, "bilinmeyen işlem"


def status():
    on = powered()
    name = re.search(r"Name: (.*)", btctl("show"))
    return {"powered": on, "scanning": SCAN["active"], "devices": device_list() if on else [],
            "op": dict(OP), "name": name.group(1).strip() if name else "pi"}
