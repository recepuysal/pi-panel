#!/usr/bin/env python3
"""librespot --onevent betigi: calan sarkiyi /run/raspotify/now.json'a yazar (dashboard okur).

raspotify servisi pi kullanicisiyla calisir (override.conf), /run/raspotify ona aittir.
"""
import json
import os
import time

PATH = "/run/raspotify/now.json"
e = os.environ
event = e.get("PLAYER_EVENT", "")

try:
    with open(PATH, encoding="utf-8") as f:
        st = json.load(f)
except (OSError, ValueError):
    st = {}

if event == "track_changed":
    st.update(track=e.get("TRACK_ID", ""), name=e.get("NAME", ""), album=e.get("ALBUM", ""),
              artists=[a for a in e.get("ARTISTS", "").split("\n") if a],
              cover=(e.get("COVERS", "").split("\n") or [""])[0],  # ilki en buyuk kapak
              duration=int(e.get("DURATION_MS") or 0))
elif event in ("playing", "paused", "stopped", "session_disconnected"):
    st.update(state=event, position=int(e.get("POSITION_MS") or 0))
elif event == "session_connected":
    st.update(user=e.get("USER_NAME", ""), state="connected")
else:
    raise SystemExit(0)

st["time"] = time.time()
tmp = PATH + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(st, f, ensure_ascii=False)
os.chmod(tmp, 0o644)
os.replace(tmp, PATH)
