# Pi Panel

Raspberry Pi için ev ekranı: saat, hava durumu, radyo, Spotify / YouTube yayını ve Bluetooth ses.
Masaüstü ortamı olmadan çalışır; Python + pygame ile doğrudan HDMI ekrana (`/dev/fb0`) çizer,
dokunmatik ekranla kullanılır. Raspberry Pi 4 Model B (4 GB) ve 1280×800 Waveshare USB dokunmatik ekranla denendi.

## Özellikler

- **Ana ekran:** saniyeli saat, tarih, günün resmi / dini günü, hava tahmininin üstünde sakin bir haber şeridi (TRT Haber manşetleri, 30 sn’de bir değişir; ayarlardan kapatılır), İstanbul hava durumu (Open-Meteo) ve 6 günlük tahmin,
  Bing günün duvar kâğıdı, dolar ve gram altın, IP adresi ve CPU sıcaklığı.
- **Akşam modu:** 17:00'den gün doğumuna kadar koyu tema ve ay evresi kartı.
- **Gece modu:** belirlenen saatlerde (varsayılan 00:00–09:00) ya da bilgisayar kilitliyken siyah ekranda soluk saat.
- **Radyo:** 10 Türk radyo istasyonu, çalan şarkı adı, Bluetooth kulaklığa çalar.
- **YouTube / YouTube Music yayını:** telefonda “Pi Panel” olarak görünür. YouTube Music'ten ses, YouTube'dan video
  (YouTube sayfasında, sesle senkron). Yerel ağ cihaz bulmayı engelliyorsa “TV kodu ile bağla” desteklenir.
- **Spotify Connect:** raspotify ile; çalan şarkı ana ekranda kart olarak görünür.
- **Wi-Fi kurulumu:** bilinen ağ yoksa `Pi-Kurulum` erişim noktası ve QR kodlu ayar sayfası; ekrandan ağ seçme ve
  Türkçe klavye.
- **Bluetooth menüsü:** kulaklık / hoparlör eşleştirme ve bağlama (bluez-alsa).

## Dosyalar

| Dosya | Görev |
|---|---|
| `dashboard.py` | Ana program: ekran, veri güncelleme, dokunma, akşam / gece modu |
| `wifi_ui.py` | Sayfalar ve menüler: Wi-Fi, Bluetooth, radyo, YouTube, ayarlar |
| `wifi_setup.py`, `bt_backend.py` | Root servisi: Wi-Fi erişim noktası / bağlantı ve Bluetooth (yerel API 127.0.0.1:8099) |
| `ytcast/` | YouTube yayın alıcısı (Node.js, yt-cast-receiver + yt-dlp + mpv) |
| `spotify_event.py` | raspotify olaylarını ekrana aktarır |
| `assets/` | Phosphor Icons SVG'leri (kurulumda beyaz PNG'ye çevrilir) |
| `setup/` | Kurulum betiği ve systemd servisleri |
| `windows/` | Bilgisayar kilitlenince Pi'yi gece ekranına alan Görev Zamanlayıcısı görevleri |

## Kurulum

1. Raspberry Pi Imager ile **Raspberry Pi OS Lite (64-bit)** yazın; kullanıcı adı `pi`, Wi-Fi ve SSH'yi açın.
2. Pi'de:
   ```bash
   git clone https://github.com/recepuysal/pi-panel.git
   cd pi-panel
   sudo bash setup/install.sh
   ```
   Betik paketleri kurar, dosyaları `~/dashboard` ve `~/ytcast`'e kopyalar, servisleri açar ve Pi'yi yeniden başlatır.
3. İsteğe bağlı Spotify: `bash setup/raspotify-kur.sh`
4. İsteğe bağlı, Windows bilgisayar kilitlenince gece ekranı:
   `powershell -ExecutionPolicy Bypass -File windows\pc-kilit-gorevleri.ps1`
   (ssh'ta `pi` adlı, anahtarla girilen bir Host tanımlı olmalı).

Şehir ve modlar `config.json`'dan ya da ekranın üst kenarından aşağı kaydırılan ayarlar panelinden değiştirilir.

## Önizleme

Pi olmadan da tek kare üretilebilir:

```bash
SDL_VIDEODRIVER=dummy python3 dashboard.py --shot ekran.png            # ana ekran
SDL_VIDEODRIVER=dummy python3 dashboard.py --shot ekran.png --evening  # akşam modu
SDL_VIDEODRIVER=dummy python3 dashboard.py --shot ekran.png --ui radio # radyo sayfası (yt, settings, bt ...)
```

## Kaynaklar

- Hava durumu: [Open-Meteo](https://open-meteo.com) · Dini günler: [Aladhan](https://aladhan.com) (Diyanet takvimi)
- Simgeler: [Phosphor Icons](https://phosphoricons.com) (MIT)
- YouTube yayını: [yt-cast-receiver](https://github.com/patrickkfkan/yt-cast-receiver), [yt-dlp](https://github.com/yt-dlp/yt-dlp), [mpv](https://mpv.io)
- Spotify: [raspotify](https://github.com/dtcooper/raspotify)
