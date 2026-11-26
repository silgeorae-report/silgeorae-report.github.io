"""DB의 글로 정적 사이트(public/)를 만든다. GitHub Pages 등 어떤 정적 호스팅에도 올릴 수 있다."""
import json
import shutil
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import analyze, charts
from .config import PUBLIC_DIR, ROOT
from .regions import get_group


def _env():
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html"]))
    env.filters["date"] = lambda iso: iso[:10]
    return env


def _draw_chart(conn, spec: dict, path, as_of: str) -> bool:
    if spec["type"] == "gu_trend":
        m = analyze.gu_monthly(conn, spec["sgg"], spec["gu"], spec["ym"], as_of)
        if not m or len(m["trend"]) < 1:
            return False
        charts.gu_trend(m["trend"], f"{spec['gu']} 월별 아파트 매매 거래량·중위 거래가", path)
    else:
        m = analyze.complex_page(conn, spec["sgg"], spec["gu"], spec["key"], as_of)
        if not m:
            return False
        charts.complex_scatter(m["points"], f"{m['facts']['apt']} 실거래가 분포", path)
    return True


def build(conn, cfg, out=PUBLIC_DIR) -> int:
    site = dict(cfg["site"])
    site["base_path"] = urlparse(site["base_url"]).path.rstrip("/")
    gname, gslug, regions = get_group(cfg["collect"]["regions"])
    gu_by_code = {code: {"name": n, "slug": s} for code, (n, s) in regions.items()}
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if out.exists():
        shutil.rmtree(out)
    (out / "charts").mkdir(parents=True)
    shutil.copytree(ROOT / "static", out / "static")

    env = _env()
    md = markdown.Markdown(extensions=["tables"])
    rows = conn.execute(
        """SELECT p.*, v.youtube_id FROM posts p LEFT JOIN videos v
           ON v.slug = p.slug AND v.status = 'uploaded' ORDER BY p.updated_at DESC""").fetchall()
    posts = []
    for r in rows:
        p = dict(r)
        p["tags"] = json.loads(p["tags"])
        p["url"] = f"{site['base_path']}/posts/{p['slug']}/"
        p["abs_url"] = f"{site['base_url']}/posts/{p['slug']}/"
        p["gu"] = gu_by_code.get(p["region_code"])
        p["kind_label"] = "월간 리포트" if p["kind"] == "gu_monthly" else "단지 실거래가"
        body = p["body_md"].replace("](/", f"]({site['base_path']}/")
        p["html"] = md.reset().convert(body)
        for spec in json.loads(p["charts"]):
            _draw_chart(conn, spec, out / "charts" / f"{p['slug']}.png", as_of)
        p["image"] = f"{site['base_url']}/charts/{p['slug']}.png"
        posts.append(p)

    def write(rel: str, html: str):
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    ctx = {"site": site, "regions": gu_by_code, "group_name": gname, "build_date": as_of}
    for p in posts:
        related = [q for q in posts if q["region_code"] == p["region_code"] and q["slug"] != p["slug"]][:6]
        write(f"posts/{p['slug']}/index.html",
              env.get_template("post.html").render(**ctx, post=p, related=related,
                                                   jsonld=json.dumps(_jsonld(p, site), ensure_ascii=False)))

    reports = [p for p in posts if p["kind"] == "gu_monthly"]
    complexes = [p for p in posts if p["kind"] == "complex"]
    write("index.html", env.get_template("index.html").render(
        **ctx, reports=reports[:12], complexes=complexes[:24], total=len(posts)))
    for code, gu in gu_by_code.items():
        items = [p for p in posts if p["region_code"] == code]
        if items:
            write(f"region/{gu['slug']}/index.html", env.get_template("region.html").render(
                **ctx, gu=gu, reports=[p for p in items if p["kind"] == "gu_monthly"],
                complexes=[p for p in items if p["kind"] == "complex"]))
    for page in ("about", "privacy"):
        write(f"{page}/index.html", env.get_template(f"{page}.html").render(**ctx))
    write("404.html", env.get_template("404.html").render(**ctx))

    _sitemap(out, site, posts, gu_by_code)
    _feed(out, site, posts[:30])
    write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {site['base_url']}/sitemap.xml\n")
    if site.get("adsense_client"):
        pub = site["adsense_client"].replace("ca-", "")
        write("ads.txt", f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n")
    host = urlparse(site["base_url"]).hostname or ""
    if host and not host.endswith("github.io") and host != "example.com":
        write("CNAME", host + "\n")
    (out / ".nojekyll").write_text("")
    # 검색엔진 소유권 확인 파일 (google…html / naver…html) — 지우면 인증이 풀림
    for name in site.get("site_verification_files", []):
        engine = "naver" if name.startswith("naver") else "google"
        write(name, f"{engine}-site-verification: {name}" + ("\n" if engine == "google" else ""))
    # 이번 실행에서 만든 영상을 공개 URL로 배포 (Instagram이 이 URL에서 영상을 가져감)
    video_dir = ROOT / "videos"
    if video_dir.exists():
        for mp4 in video_dir.glob("*.mp4"):
            (out / "media").mkdir(exist_ok=True)
            shutil.copy2(mp4, out / "media" / mp4.name)
    _carry_media(conn, site, out)
    return len(posts)


def _carry_media(conn, site: dict, out, fetch=None) -> list[str]:
    """최근 며칠 영상 중 이번 빌드에 없는 것을 지금 배포된 사이트에서 받아 다시 넣는다.

    배포는 public/ 전체를 바꾸므로, 그냥 두면 build 모드 재배포나 다음 날 실행이 전날 영상을 지워
    TikTok 예약 작업(08:10)이 받을 파일이 사라진다 (11-26 송파 9월분). CI에서만 동작(fetch 주입 시 예외).
    """
    import os
    days = int(site.get("carry_media_days", 3))
    if days <= 0 or (fetch is None and not os.environ.get("GITHUB_ACTIONS")):
        return []
    if fetch is None:
        import requests

        def fetch(url):
            r = requests.get(url, timeout=60)
            ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("video")
            return r.content if ok else None
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        slugs = [r[0] for r in conn.execute(
            "SELECT slug FROM videos WHERE created_at >= ?", (since,)).fetchall()]
    except Exception:
        return []
    carried = []
    for slug in slugs:
        dest = out / "media" / f"{slug}.mp4"
        if dest.exists():
            continue
        try:
            data = fetch(f"{site['base_url'].rstrip('/')}/media/{slug}.mp4")
        except Exception:
            data = None
        if data:
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(data)
            carried.append(slug)
    return carried


def _jsonld(p: dict, site: dict) -> dict:
    return {
        "@context": "https://schema.org", "@type": "Article",
        "headline": p["title"], "description": p["description"],
        "datePublished": p["created_at"], "dateModified": p["updated_at"],
        "image": [p["image"]], "mainEntityOfPage": p["abs_url"], "inLanguage": "ko",
        "author": {"@type": "Organization", "name": site["author"]},
        "publisher": {"@type": "Organization", "name": site["title"]},
        "isBasedOn": "https://rt.molit.go.kr/",
    }


def _sitemap(out, site, posts, gu_by_code):
    urls = [(f"{site['base_url']}/", posts[0]["updated_at"] if posts else None)]
    urls += [(p["abs_url"], p["updated_at"]) for p in posts]
    used = {p["region_code"] for p in posts}
    urls += [(f"{site['base_url']}/region/{g['slug']}/", None) for c, g in gu_by_code.items() if c in used]
    body = "\n".join(
        f"  <url><loc>{escape(u)}</loc>" + (f"<lastmod>{m[:10]}</lastmod>" if m else "") + "</url>"
        for u, m in urls)
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "\n</urlset>\n",
        encoding="utf-8")


def _feed(out, site, posts):
    def rfc822(iso):
        return format_datetime(datetime.fromisoformat(iso))
    items = "\n".join(
        f"<item><title>{escape(p['title'])}</title><link>{escape(p['abs_url'])}</link>"
        f"<guid>{escape(p['abs_url'])}</guid><pubDate>{rfc822(p['updated_at'])}</pubDate>"
        f"<description>{escape(p['description'])}</description></item>" for p in posts)
    (out / "feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        f"<title>{escape(site['title'])}</title><link>{escape(site['base_url'])}/</link>"
        f"<description>{escape(site['tagline'])}</description><language>ko</language>\n"
        + items + "\n</channel></rss>\n", encoding="utf-8")
