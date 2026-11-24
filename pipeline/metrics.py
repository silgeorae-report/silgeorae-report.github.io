"""성과 수집: 채널별 조회수·반응을 날짜별로 쌓는다 (learn.py가 이 표로 학습).

  YouTube   : Data API(권한이 되면) → 안 되면 공개 시청 페이지에서 조회수만 읽음
  Instagram : Graph API media 목록의 좋아요·댓글 (instagram 작업에서 호출)
  TikTok    : 이 PC의 예약 작업이 data/tiktok_metrics.json 에 적어 올린 값
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from .config import DATA_DIR

TIKTOK_FILE = DATA_DIR / "tiktok_metrics.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    slug TEXT NOT NULL,
    platform TEXT NOT NULL,          -- youtube | instagram | tiktok
    day TEXT NOT NULL,               -- YYYY-MM-DD (UTC)
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    PRIMARY KEY (slug, platform, day)
);
"""


def _ensure(conn):
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
    if "retention" not in cols:   # YouTube 평균 시청 비율(%) — 분석 권한이 있을 때만
        conn.execute("ALTER TABLE metrics ADD COLUMN retention REAL")
    if "shares" not in cols:
        conn.execute("ALTER TABLE metrics ADD COLUMN shares INTEGER")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _save(conn, slug, platform, views=None, likes=None, comments=None, day=None, retention=None, shares=None):
    _ensure(conn)
    conn.execute("""INSERT OR REPLACE INTO metrics (slug, platform, day, views, likes, comments, retention, shares)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (slug, platform, day or _today(), views, likes, comments, retention, shares))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def collect_youtube(conn) -> int:
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT slug, youtube_id FROM videos WHERE youtube_id IS NOT NULL").fetchall()
    if not rows:
        return 0
    by_id = {r["youtube_id"]: r["slug"] for r in rows}
    got = {}
    try:  # 0) 분석 API (시청 지속률) — 재동의한 토큰일 때
        from . import youtube
        if youtube.configured():
            end = datetime.now(timezone.utc).date()
            res = youtube.analytics(list(by_id), (end - timedelta(days=90)).isoformat(), end.isoformat())
            for vid, m in res.items():
                _save(conn, by_id[vid], "youtube", m["views"], m["likes"], None,
                      retention=m["avg_view_pct"], shares=m["shares"])
                got[vid] = None
            conn.commit()
            if got:
                return len(got)
    except Exception:
        got = {}
    try:  # 1) 공식 API (토큰 권한이 통계 조회를 허용할 때)
        from . import youtube
        if youtube.configured():
            tok = youtube.access_token()
            ids = list(by_id)
            for i in range(0, len(ids), 50):
                r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=30,
                                 params={"part": "statistics", "id": ",".join(ids[i:i + 50])},
                                 headers={"Authorization": f"Bearer {tok}"})
                if r.status_code != 200:
                    break
                for it in r.json().get("items", []):
                    s = it.get("statistics", {})
                    got[it["id"]] = (_int(s.get("viewCount")), _int(s.get("likeCount")), _int(s.get("commentCount")))
    except Exception:
        pass
    for vid in by_id:  # 2) 공개 페이지 (조회수만)
        if vid in got:
            continue
        try:
            r = requests.get(f"https://www.youtube.com/watch?v={vid}", timeout=30,
                             headers={"Accept-Language": "en-US,en;q=0.8", "User-Agent": "Mozilla/5.0"})
            m = re.search(r'"viewCount":"(\d+)"', r.text)
            if m:
                got[vid] = (int(m.group(1)), None, None)
        except Exception:
            continue
    for vid, (v, l, c) in got.items():
        _save(conn, by_id[vid], "youtube", v, l, c)
    conn.commit()
    return len(got)


def collect_instagram(conn) -> int:
    conn.executescript(SCHEMA)
    tok, ig = os.environ.get("IG_ACCESS_TOKEN", "").strip(), os.environ.get("IG_USER_ID", "").strip()
    rows = conn.execute("SELECT slug, remote_id FROM social_posts WHERE platform='instagram' AND status='posted'").fetchall()
    if not (tok and ig and rows) or tok.startswith("IG"):
        return 0
    by_id = {r["remote_id"]: r["slug"] for r in rows}
    r = requests.get(f"https://graph.facebook.com/{ig}/media", timeout=30,
                     params={"fields": "id,like_count,comments_count", "limit": 100, "access_token": tok})
    if r.status_code != 200:
        return 0
    n = 0
    for it in r.json().get("data", []):
        if it["id"] in by_id:
            views, shares = _ig_insights(it["id"], tok)
            _save(conn, by_id[it["id"]], "instagram", views, _int(it.get("like_count")), _int(it.get("comments_count")),
                  shares=shares)
            n += 1
    conn.commit()
    return n


_IG_ERR_LOGGED = False


def _ig_insights(media_id: str, tok: str) -> tuple[int | None, int | None]:
    """릴스 조회 수·공유 수 (인사이트 권한 필요). 실패하면 (None, None)."""
    try:
        r = requests.get(f"https://graph.facebook.com/{media_id}/insights", timeout=30,
                         params={"metric": "views,shares", "access_token": tok})
        if r.status_code != 200:
            global _IG_ERR_LOGGED
            if not _IG_ERR_LOGGED:  # 공개 로그이므로 메시지(ID 포함 가능) 대신 코드만
                _IG_ERR_LOGGED = True
                e = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error", {})
                print(f"[metrics] instagram insights 실패: HTTP {r.status_code}, type={e.get('type')}, "
                      f"code={e.get('code')}, subcode={e.get('error_subcode')}")
            return None, None
        got = {}
        for d in r.json().get("data", []):
            v = (d.get("total_value") or {}).get("value")
            if v is None and d.get("values"):
                v = d["values"][0].get("value")
            got[d.get("name")] = _int(v)
        return got.get("views"), got.get("shares")
    except Exception:
        return None, None


def collect_tiktok(conn) -> int:
    conn.executescript(SCHEMA)
    if not TIKTOK_FILE.exists():
        return 0
    data = json.loads(TIKTOK_FILE.read_text(encoding="utf-8"))
    n = 0
    for slug, days in data.items():
        for day, m in days.items():
            _save(conn, slug, "tiktok", _int(m.get("views")), _int(m.get("likes")), _int(m.get("comments")), day)
            n += 1
    conn.commit()
    return n
