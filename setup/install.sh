#!/bin/bash
# Pi Panel kurulumu: Raspberry Pi OS Lite (64-bit) uzerinde root olarak calisir.
#   sudo bash setup/install.sh            (depo kokunden; kullanici adi "pi" varsayilir)
# SD karttan ilk acilista da calisabilir: depoyu bootfs'e kopyalayip user-data'ya runcmd ekleyin.
# Ilerleme: setup/install.log  - bitti isareti: /var/lib/pi-panel.done (tekrar icin silin)
set -u
[ -e /var/lib/pi-panel.done ] && exit 0
SRC=$(cd "$(dirname "$0")/.." && pwd)
LOG=$SRC/setup/install.log
H=/home/pi
exec >>"$LOG" 2>&1
echo "==== $(date) kurulum basladi"
step() { echo; echo "---- $(date +%T) $*"; }

step "ag bekleniyor"
for i in $(seq 1 60); do
  getent hosts deb.debian.org >/dev/null && curl -sfI -m 10 https://archive.raspberrypi.com >/dev/null && break
  sleep 5
done

step "paketler"
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=900 update
apt-get -o DPkg::Lock::Timeout=900 install -y --no-install-recommends \
  python3-pygame python3-evdev python3-qrcode fonts-inter librsvg2-bin \
  network-manager bluez bluez-alsa-utils rfkill mpg123 mpv \
  nodejs npm curl ca-certificates xz-utils iw htop || echo "!! apt hatasi"

step "dosyalar"
mkdir -p $H/dashboard/cache $H/ytcast
cp -r $SRC/*.py $SRC/config.json $SRC/raspotify-override.conf $SRC/assets $H/dashboard/
cp -r $SRC/ytcast/. $H/ytcast/

step "ikonlar (SVG -> beyaz 128px PNG)"
for s in $H/dashboard/assets/*.svg; do
  sed 's/currentColor/#ffffff/g' "$s" | rsvg-convert -w 128 -h 128 -o "${s%.svg}.png" || echo "!! $s"
done

step "gruplar"
usermod -aG video,input,audio,bluetooth,netdev,gpio pi 2>/dev/null || usermod -aG video,input,audio pi

step "YouTube alicisi (npm, yt-dlp, Node 22)"
cd $H/ytcast
chown -R pi:pi $H
sudo -u pi npm install --omit=dev --no-audit --no-fund || echo "!! npm hatasi"
curl -fL -o yt-dlp https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp && chmod +x yt-dlp || echo "!! yt-dlp indirilemedi"
if [ "$(uname -m)" = aarch64 ]; then NA=arm64; else NA=armv7l; fi
NV=v22.12.0
if curl -fL -o /tmp/node22.tar.xz https://nodejs.org/dist/$NV/node-$NV-linux-$NA.tar.xz; then
  rm -rf node22 && mkdir node22 && tar xJf /tmp/node22.tar.xz -C node22 --strip-components=1 && rm /tmp/node22.tar.xz
else
  echo "!! node22 indirilemedi (QuickJS ile yavas calisir)"
fi

step "servisler"
cp $SRC/setup/systemd/*.service /etc/systemd/system/
install -m 755 $H/dashboard/spotify_event.py /usr/local/bin/spotify-event
mkdir -p /etc/systemd/system/raspotify.service.d
cp $H/dashboard/raspotify-override.conf /etc/systemd/system/raspotify.service.d/override.conf
systemctl daemon-reload
systemctl disable getty@tty1.service
systemctl enable NetworkManager bluetooth bluealsa bluealsa-aplay dashboard wifi-setup ytcast
rfkill unblock bluetooth 2>/dev/null

step "ekran: sessiz acilis"
CL=/boot/firmware/cmdline.txt
grep -q consoleblank $CL || { cp $CL $CL.bak-dashboard; sed -i '1 s/$/ consoleblank=0 vt.global_cursor_default=0 logo.nologo quiet loglevel=3/' $CL; }
grep -q '^disable_splash' /boot/firmware/config.txt || echo "disable_splash=1" >> /boot/firmware/config.txt

chown -R pi:pi $H
touch /var/lib/pi-panel.done
echo "==== $(date) kurulum bitti, yeniden baslatiliyor (Spotify icin: bash setup/raspotify-kur.sh)"
sync
systemctl reboot
