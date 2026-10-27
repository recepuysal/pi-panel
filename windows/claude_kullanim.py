"""Claude abonelik kullanimini (5 saatlik oturum + haftalik) okuyup Pi Panel'e yollar.

Claude Code'un oturum anahtari (~/.claude/.credentials.json) bu bilgisayarda kalir; Pi'ye yalniz yuzdeler gider.
Gorev Zamanlayicisi 5 dk'da bir calistirir (windows/claude-kullanim-gorevi.ps1).
  pythonw windows\\claude_kullanim.py
"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path

CRED = Path.home() / ".claude" / ".credentials.json"
URL = "https://api.anthropic.com/api/oauth/usage"
TARGET = "~/dashboard/cache/claude-usage.json"


def usage():
    token = json.loads(CRED.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request(URL, headers={"Authorization": "Bearer " + token,
                                               "anthropic-beta": "oauth-2025-04-20",
                                               "User-Agent": "pi-panel/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        u = json.loads(r.read())
    pick = lambda k: {"percent": (u.get(k) or {}).get("utilization"), "resets_at": (u.get(k) or {}).get("resets_at")}
    return {"time": time.time(), "session": pick("five_hour"), "week": pick("seven_day")}


def main():
    data = json.dumps(usage())
    subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "pi", f"cat > {TARGET}"],
                   input=data.encode(), timeout=30, check=True, creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    main()
