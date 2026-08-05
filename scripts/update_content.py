#!/usr/bin/env python3
"""新着コンテンツ(data/*.json)を更新するスクリプト。

- イベント: connpassグループページをスクレイプ(APIキー不要)
- Podcast: 配信元のRSSフィードから取得(認証不要)。環境変数
  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET が設定されている場合は
  Spotify公式APIを優先し、失敗時はRSSにフォールバックする。

GitHub Actions (.github/workflows/update-content.yml) から日次実行される。
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONNPASS_GROUP_URL = "https://ai-robot-japan.connpass.com/"
SPOTIFY_SHOW_ID = "3eTibJbIqve5Rne4MkS1Ao"
# 配信元(Spotify for Podcasters)のRSS。認証不要で常に最新のエピソードが取れる。
PODCAST_RSS_URL = "https://anchor.fm/s/10fc47344/podcast/rss"
SPOTIFY_SHOW_URL = f"https://open.spotify.com/show/{SPOTIFY_SHOW_ID}"
ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
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


def parse_member_count(html):
    """connpassグループページのサイドバーからメンバー数を取り出す。"""
    m = re.search(
        r'participation/"[^>]*>\D{0,3}<span[^>]*>([0-9,]+)</span>', html)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def fetch_events():
    html = http_get(CONNPASS_GROUP_URL)
    members = parse_member_count(html)
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
    return {
        "generated_at": now_iso(),
        "members": members,
        "total": len(events),
        "events": events,
    }


def fetch_podcast_from_api():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
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

    auth_headers = {"Authorization": f"Bearer {token}"}
    show = json.loads(http_get(
        f"https://api.spotify.com/v1/shows/{SPOTIFY_SHOW_ID}?market=JP",
        headers=auth_headers,
    ))
    episodes_page = json.loads(http_get(
        f"https://api.spotify.com/v1/shows/{SPOTIFY_SHOW_ID}/episodes?market=JP&limit=10",
        headers=auth_headers,
    ))
    episodes = [
        {
            "title": ep.get("name"),
            "url": (ep.get("external_urls") or {}).get("spotify"),
            "image": (ep.get("images") or [{}])[0].get("url"),
            "release_date": ep.get("release_date"),
        }
        for ep in episodes_page.get("items", [])
        if ep
    ]
    if not episodes:
        # 空リストで既存JSONを上書きしない（トップページの表示が消えるため）
        raise RuntimeError("Spotify APIからエピソードを取得できませんでした")
    return {
        "generated_at": now_iso(),
        "source": "spotify-api",
        "total": show.get("total_episodes") or episodes_page.get("total"),
        "episodes": episodes,
    }


def fetch_show_thumbnail():
    """Spotify oEmbed から番組アートワークの軽量サムネイルURLを取得する（認証不要）。

    RSSのアートワークは原寸(数百KB)で一覧表示には重いため、取得できた場合はこちらを使う。
    """
    try:
        data = json.loads(http_get(
            "https://open.spotify.com/oembed?url=" + urllib.parse.quote(SPOTIFY_SHOW_URL, safe="")
        ))
        return data.get("thumbnail_url") or None
    except Exception as exc:  # noqa: BLE001
        print(f"podcast: oEmbedサムネイル取得に失敗しました（RSSの画像を使用）: {exc}",
              file=sys.stderr)
        return None


def fetch_podcast_from_rss():
    """配信元のRSSから最新エピソードを取得する（認証不要）。"""
    channel = ET.fromstring(http_get(PODCAST_RSS_URL)).find("channel")
    if channel is None:
        raise RuntimeError("RSSにchannelがありません")

    channel_image = channel.find(f"{ITUNES_NS}image")
    fallback_image = channel_image.get("href") if channel_image is not None else None
    thumbnail = fetch_show_thumbnail()

    episodes = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        item_image = item.find(f"{ITUNES_NS}image")
        release_date = None
        pub_date = item.findtext("pubDate")
        if pub_date:
            try:
                release_date = parsedate_to_datetime(pub_date).date().isoformat()
            except (TypeError, ValueError):
                release_date = None
        episodes.append({
            "title": title,
            "url": (item.findtext("link") or "").strip() or SPOTIFY_SHOW_URL,
            "image": thumbnail
                     or (item_image.get("href") if item_image is not None else None)
                     or fallback_image,
            "release_date": release_date,
        })

    if not episodes:
        raise RuntimeError("RSSからエピソードを取得できませんでした")

    # 新しい順に並べ替え（RSSは通常新しい順だが順序を保証する）
    episodes.sort(key=lambda e: e["release_date"] or "", reverse=True)
    return {
        "generated_at": now_iso(),
        "source": "rss",
        "total": len(episodes),
        "episodes": episodes[:10],
    }


def fetch_podcast():
    """Spotify公式API（認証情報がある場合）を優先し、なければ/失敗時はRSSを使う。"""
    try:
        payload = fetch_podcast_from_api()
        if payload:
            print("podcast: Spotify公式APIから取得しました")
            return payload
        print("podcast: SPOTIFY_CLIENT_ID/SECRET 未設定のためRSSから取得します")
    except Exception as exc:  # noqa: BLE001
        print(f"podcast: Spotify API取得に失敗したためRSSにフォールバックします: {exc}",
              file=sys.stderr)

    payload = fetch_podcast_from_rss()
    print("podcast: RSSから取得しました")
    return payload


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
