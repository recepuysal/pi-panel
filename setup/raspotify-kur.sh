#!/bin/bash
# Spotify Connect (raspotify) - resmi kurulum betigi + Pi Panel ayarlari.
# Kurulum bittikten sonra Pi'de calistir:  bash setup/raspotify-kur.sh
set -e
curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
sudo mkdir -p /etc/systemd/system/raspotify.service.d
sudo cp /home/pi/dashboard/raspotify-override.conf /etc/systemd/system/raspotify.service.d/override.conf
sudo sed -i 's/^#\?LIBRESPOT_DISABLE_CREDENTIAL_CACHE=.*/#LIBRESPOT_DISABLE_CREDENTIAL_CACHE=/' /etc/raspotify/conf
sudo systemctl daemon-reload
sudo systemctl restart raspotify
echo "Tamam: telefonda Spotify > cihazlar > 'Pi Panel'"
