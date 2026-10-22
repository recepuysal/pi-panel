#!/bin/bash
# Kullanim: ./push.sh dosya [dosya...]  -> Pi'de ~/dashboard/ altina gonderir (zaman asimi + sha256 kontrolu)
set -u
for f in "$@"; do
  name=$(basename "$f")
  for try in 1 2 3; do
    timeout 30 ssh pi "cat > ~/dashboard/$name.new" < "$f" &&
    [ "$(sha256sum "$f" | cut -c1-64)" = "$(timeout 15 ssh pi "sha256sum ~/dashboard/$name.new" | cut -c1-64)" ] &&
    timeout 15 ssh pi "mv ~/dashboard/$name.new ~/dashboard/$name" && { echo "gonderildi: $name"; break; }
    echo "tekrar deneniyor ($try): $name"
  done
done
