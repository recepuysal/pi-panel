#!/usr/bin/env python3
"""Surekli acik yt-dlp: stdin'den {"id": n, "v": "<video id>"} satirlari okur,
stdout'a ses adresi + sarki bilgisini JSON satiri olarak yazar.

yt-dlp her calistirmada yeniden yuklenirse Zero 2'de ~25 sn suruyor; acik kalinca birkac saniye.
YouTube sifre cozmesi icin Node 22 (node22/) kullanilir; yoksa QuickJS (cok yavas).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "yt-dlp"))  # tek dosyalik yt-dlp bir zip arsivi: dogrudan ice aktarilir
import yt_dlp  # noqa: E402

node22 = os.path.join(HERE, "node22", "bin", "node")
runtimes = {"node": {"path": node22}} if os.path.exists(node22) else {"quickjs": {}}
ydl = yt_dlp.YoutubeDL({
    "format": "bestaudio[ext=m4a]/bestaudio",
    "quiet": True, "no_warnings": True, "noplaylist": True,
    "js_runtimes": runtimes,
    "cachedir": os.path.join(HERE, ".cache", "yt-dlp"),
})
# YouTube (video) yayini: panelin icindeki 604x340 pencere icin 480p H.264 yeterli (1080p 3 cekirdegi doldurur)
ydlv = yt_dlp.YoutubeDL({
    "format": "bv*[vcodec^=avc1][height<=480][fps<=30]+ba[ext=m4a]/bv*[vcodec^=avc1][height<=480]+ba[ext=m4a]"
              "/b[vcodec^=avc1][height<=480]/b",
    "quiet": True, "no_warnings": True, "noplaylist": True,
    "js_runtimes": runtimes,
    "cachedir": os.path.join(HERE, ".cache", "yt-dlp"),
})


def best_thumb(info):
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url") and ".webp" not in t["url"]]
    thumbs.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    return thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{info.get('id')}/hqdefault.jpg"


print(json.dumps({"ready": True, "runtime": next(iter(runtimes))}), flush=True)
for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    try:
        if req.get("video"):
            info = ydlv.extract_info("https://www.youtube.com/watch?v=" + req["v"], download=False)
        else:
            info = ydl.extract_info("https://music.youtube.com/watch?v=" + req["v"], download=False)
        artists = info.get("artists") or ([info["artist"]] if info.get("artist") else
                                          [(info.get("uploader") or "").removesuffix(" - Topic")])
        fmts = info.get("requested_formats") or [info]
        a = fmts[-1]  # ses (tek parca bicimde video+ses ayni dosya)
        out = {"id": req.get("id"), "url": a["url"], "headers": a.get("http_headers") or {},
               "size": a.get("filesize") or a.get("filesize_approx") or 0, "ext": a.get("ext"),
               "name": info.get("track") or info.get("title") or "", "artists": [a for a in artists if a],
               "album": info.get("album") or "", "duration": info.get("duration") or 0, "cover": best_thumb(info)}
        if req.get("video") and len(fmts) > 1:
            v = fmts[0]
            out.update({"vurl": v["url"], "vheaders": v.get("http_headers") or {},
                        "vsize": v.get("filesize") or v.get("filesize_approx") or 0, "vext": v.get("ext")})
    except Exception as e:  # noqa: BLE001 - her hata istemciye donsun
        out = {"id": req.get("id"), "error": str(e)[:300]}
    print(json.dumps(out, ensure_ascii=False), flush=True)
