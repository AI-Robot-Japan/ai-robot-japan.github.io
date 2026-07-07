#!/usr/bin/env python3
"""新着コンテンツ(data/*.json)を更新するスクリプト。

- イベント: connpassグループページをスクレイプ(APIキー不要)
- Podcast: 環境変数 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET があれば
  Spotify公式APIで更新。なければ既存の data/podcast.json を維持する。

GitHub Actions (.github/workflows/update-content.yml) から日次実行される。
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONNPASS_GROUP_URL = "https://ai-robot-japan.connpass.com/"
SPOTIFY_SHOW_ID = "3eTibJbIqve5Rne4MkS1Ao"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
JST = timezone(timedelta(hours=9))


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="replace")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_events():
    html = http_get(CONNPASS_GROUP_URL)
    event_urls = sorted(set(re.findall(
        r"https://ai-robot-japan\.connpass\.com/event/\d+/", html)))

    events = []
    for url in event_urls:
        page = http_get(url)
        og_title = re.search(r'property="og:title" content="([^"]+)"', page)
        og_image = re.search(r'property="og:image" content="([^"]+)"', page)
        if not og_title:
            continue
        title = og_title.group(1)
        # og:title は「イベント名 (2026/06/29 14:00〜)」形式
        m = re.search(r"^(.*)\s\((\d{4})/(\d{2})/(\d{2})(?:\s(\d{2}):(\d{2}))?[^)]*\)$", title)
        if m:
            name = m.group(1)
            y, mo, d = int(m.group(2)), int(m.group(3)), int(m.group(4))
            h = int(m.group(5) or 0)
            mi = int(m.group(6) or 0)
            started_at = datetime(y, mo, d, h, mi, tzinfo=JST).isoformat()
        else:
            name = title
            started_at = None
        events.append({
            "title": name,
            "url": url,
            "image": og_image.group(1) if og_image else None,
            "started_at": started_at,
        })

    events.sort(key=lambda e: e["started_at"] or "", reverse=True)
    return {"generated_at": now_iso(), "total": len(events), "events": events}


def fetch_podcast():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("SPOTIFY_CLIENT_ID/SECRET 未設定のため podcast.json は既存のまま維持します")
        return None

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={"Authorization": f"Basic {auth}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        token = json.load(res)["access_token"]

    show = json.loads(http_get(
        f"https://api.spotify.com/v1/shows/{SPOTIFY_SHOW_ID}?market=JP",
        headers={"Authorization": f"Bearer {token}"},
    ))
    episodes = [
        {
            "title": ep.get("name"),
            "url": (ep.get("external_urls") or {}).get("spotify"),
            "image": (ep.get("images") or [{}])[0].get("url"),
            "release_date": ep.get("release_date"),
        }
        for ep in (show.get("episodes") or {}).get("items", [])
    ]
    return {
        "generated_at": now_iso(),
        "total": show.get("total_episodes"),
        "episodes": episodes,
    }


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def main():
    ok = True
    try:
        write_json(DATA_DIR / "events.json", fetch_events())
    except Exception as exc:  # noqa: BLE001
        print(f"events 更新失敗: {exc}", file=sys.stderr)
        ok = False
    try:
        podcast = fetch_podcast()
        if podcast is not None:
            write_json(DATA_DIR / "podcast.json", podcast)
    except Exception as exc:  # noqa: BLE001
        print(f"podcast 更新失敗: {exc}", file=sys.stderr)
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
