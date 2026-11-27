"""파이프라인 진입점.

    python -m pipeline.run all            # 수집 → 생성 → 사이트 빌드 (매일 자동 실행되는 것)
    python -m pipeline.run collect
    python -m pipeline.run generate
    python -m pipeline.run build
    python -m pipeline.run all --demo     # API 키 없이 가상 데이터 + 가짜 작성기로 전체 시험
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import collect, db, generate, site
from .config import DATA_DIR, DB_PATH, load_config
from .regions import get_group

KST = timezone(timedelta(hours=9))


def log(msg: str):
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


SECRET_ENVS = ("DATA_GO_KR_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "IG_ACCESS_TOKEN", "IG_STATE_KEY", "IG_USER_ID",
               "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN", "YT_CLIENT_ID", "PEXELS_API_KEY", "PIXABAY_API_KEY")


def redact(summary: dict) -> dict:
    """공개 저장소에 커밋되는 요약에서 비밀값·키가 든 URL 파라미터를 지운다."""
    text = json.dumps(summary, ensure_ascii=False)
    for name in SECRET_ENVS:
        val = os.environ.get(name)
        if val and len(val) >= 8:
            text = text.replace(val, "***")
            text = text.replace(urllib.parse.quote(val, safe=""), "***")
    text = re.sub(r"(?i)(serviceKey|key|token|secret)=[^&\s\"']+", r"\1=***", text)
    return json.loads(text)


def do_collect(conn, cfg, summary, demo=False):
    _, _, regions = get_group(cfg["collect"]["regions"])
    if demo:
        from .demo import make_trades
        new = db.upsert_trades(conn, make_trades(list(regions)[:3]))
        summary["collected_new"] = new
        log(f"[demo] 가상 거래 {new}건 적재")
        return
    c = cfg["collect"]
    today = datetime.now(KST).date()
    recent = collect.recent_months(c["months_back"], today)
    window = collect.recent_months(c["backfill_months"], today)
    have = {(r[0], r[1]) for r in conn.execute("SELECT DISTINCT sgg_cd, deal_ym FROM trades")}
    new, errors, calls = 0, [], 0
    for sgg, (name, _) in regions.items():
        # 최근 N개월은 매일 다시, 그보다 오래된 달은 DB에 없을 때만 (자동 보충)
        months = recent + [ym for ym in window if ym not in recent and (sgg, ym) not in have]
        for ym in months:
            calls += 1
            try:
                new += db.upsert_trades(conn, collect.fetch_month(c["endpoint"], sgg, ym))
            except Exception as e:  # 한 지역 실패가 전체를 멈추지 않게
                errors.append(f"{name} {ym}: {e}")
                if isinstance(e, collect.CollectError) and "인증" in str(e):
                    raise
    summary["collected_new"] = new
    summary["collect_errors"] = errors
    log(f"수집 완료: 신규 {new}건, 오류 {len(errors)}건")
    if errors and len(errors) == calls:
        raise RuntimeError("모든 수집 요청이 실패했습니다: " + errors[0])


def data_as_of(conn, summary, now) -> str:
    """글 하단 '수집 기준' 날짜. 이번 수집이 실패했으면 오늘이 아니라 마지막으로 거래가 들어온 날."""
    today = now.strftime("%Y-%m-%d")
    if not any(str(e).startswith("collect:") for e in summary.get("errors", [])):
        return today
    last = conn.execute("SELECT MAX(collected_at) FROM trades").fetchone()[0]
    if not last:
        return today
    try:
        return datetime.fromisoformat(last).astimezone(KST).strftime("%Y-%m-%d")
    except ValueError:
        return today


def do_generate(conn, cfg, summary, fake=False):
    if not cfg["safety"]["enabled"]:
        log("safety.enabled=false → 생성 건너뜀 (킬 스위치)")
        summary["generate"] = "disabled"
        return
    from .write import ClaudeWriter, FakeWriter
    writer = FakeWriter() if fake else ClaudeWriter(cfg["generate"]["model"], cfg["generate"]["effort"])
    now = datetime.now(KST)
    jobs = generate.plan(conn, cfg, now.date())
    as_of = data_as_of(conn, summary, now)
    log(f"작업 {len(jobs)}건 계획됨 (자료 기준일 {as_of})")
    ok, failed, streak = [], [], 0
    for job in jobs:
        try:
            success, problems = generate.produce(conn, cfg, writer, job, as_of)
        except Exception as e:
            success, problems = False, [f"{type(e).__name__}: {e}"]
        if success:
            ok.append(job["slug"])
            streak = 0
            log(f"  발행 {job['slug']}")
        else:
            failed.append({"slug": job["slug"], "problems": problems[:5]})
            streak += 1
            log(f"  반려 {job['slug']}: {problems[:3]}")
            if streak >= cfg["safety"]["max_consecutive_failures"]:
                log("연속 실패 한도 도달 → 이번 실행 생성 중단")
                break
    summary.update(published=ok, rejected=failed, usage=writer.usage)
    if jobs and not ok:
        raise RuntimeError(f"계획된 {len(jobs)}건 중 발행 0건 — 확인 필요")


def do_shorts(conn, cfg, summary, fake=False, tts_override=None):
    from . import shorts, youtube
    from .write import ClaudeWriter, FakeWriter
    if not (cfg["safety"]["enabled"] and cfg["shorts"]["enabled"]):
        log("숏폼 비활성화 → 건너뜀")
        return
    if tts_override:
        cfg["shorts"]["tts_provider"] = tts_override
    shorts.VIDEO_DIR.mkdir(exist_ok=True)
    can_upload = cfg["youtube"]["upload"] and youtube.configured()

    # 업로드 실패 등으로 파일이 사라진 영상은 다시 만들도록 기록 삭제 (Actions는 매번 새 환경)
    if can_upload:
        for v in conn.execute("SELECT slug FROM videos WHERE status != 'uploaded'").fetchall():
            if not (shorts.VIDEO_DIR / f"{v['slug']}.mp4").exists():
                conn.execute("DELETE FROM videos WHERE slug = ?", (v["slug"],))
        conn.commit()

    posts = shorts.plan(conn, cfg)
    log(f"숏폼 {len(posts)}건 계획됨 (TTS: {cfg['shorts']['tts_provider']})")
    writer = None
    made, failed = [], []
    for p in posts:
        writer = writer or (FakeWriter() if fake else ClaudeWriter(cfg["generate"]["model"], cfg["generate"]["effort"]))
        try:
            ok, problems = shorts.produce(conn, cfg, writer, p)
        except Exception as e:
            ok, problems = False, [f"{type(e).__name__}: {e}"]
        (made if ok else failed).append(p["slug"] if ok else {"slug": p["slug"], "problems": problems[:3]})
        log(f"  {'영상 완성' if ok else '영상 실패'} {p['slug']} {'' if ok else problems[:2]}")

    # 월 1회: 서울 25개 구 3D 타임랩스 (끝 달이 바뀌는 달에 한 편, 실패해도 일반 숏폼은 계속)
    if cfg["shorts"].get("timelapse_monthly", False) and not fake:
        from . import timelapse3d
        try:
            slug = timelapse3d.produce_monthly(conn, cfg, shorts.VIDEO_DIR)
            if slug:
                made.append(slug)
                log(f"  3D 타임랩스 완성 {slug}")
        except Exception as e:
            failed.append({"slug": "seoul-3d", "problems": [f"{type(e).__name__}: {e}"[:300]]})
            log(f"  3D 타임랩스 실패: {e}")

    uploaded = []
    if can_upload:
        for v in conn.execute("SELECT * FROM videos WHERE status IN ('rendered','upload_failed')").fetchall():
            path = shorts.VIDEO_DIR / f"{v['slug']}.mp4"
            if not path.exists():
                continue
            try:
                vid = youtube.upload(path, v["title"], v["description"], json.loads(v["tags"]),
                                     cfg["youtube"]["privacy"], cfg["youtube"]["category_id"])
                conn.execute("UPDATE videos SET status='uploaded', youtube_id=?, error=NULL, updated_at=? WHERE slug=?",
                             (vid, db.now_iso(), v["slug"]))
                uploaded.append(vid)
                log(f"  YouTube 업로드 {v['slug']} → {vid}")
            except Exception as e:
                conn.execute("UPDATE videos SET status='upload_failed', error=?, updated_at=? WHERE slug=?",
                             (str(e)[:500], db.now_iso(), v["slug"]))
                log(f"  YouTube 업로드 실패 {v['slug']}: {e}")
            conn.commit()
    elif made:
        log("  YouTube 자격 증명이 없어 업로드는 건너뜀 (영상은 videos/ 및 Actions 아티팩트로 보관)")
    summary["shorts"] = {"made": made, "failed": failed, "uploaded": uploaded}
    if writer:
        summary["usage"] = writer.usage
    if posts and not made:
        raise RuntimeError(f"숏폼 {len(posts)}건 모두 실패")


def do_instagram(conn, cfg, summary):
    """Pages 배포가 끝난 뒤 실행: 최근 영상을 릴스로 게시."""
    import requests

    from . import instagram
    ic = cfg.get("instagram", {})
    if not (cfg["safety"]["enabled"] and ic.get("enabled")):
        log("Instagram 비활성화 → 건너뜀")
        return
    if not instagram.configured():
        log("Instagram 자격 증명이 없어 건너뜀")
        return
    base = cfg["site"]["base_url"].rstrip("/")
    rows = conn.execute(
        """SELECT v.* FROM videos v LEFT JOIN social_posts s ON s.slug = v.slug AND s.platform = 'instagram'
           WHERE (s.status IS NULL OR (s.status = 'failed' AND s.attempts < 3))
             AND v.created_at >= datetime('now', ?)
           ORDER BY v.created_at DESC""", (f"-{int(ic.get('days_back', 3))} days",)).fetchall()
    posted, failed = [], []
    limit = int(ic.get("max_per_run", 2))
    for v in rows:
        if len(posted) + len(failed) >= limit:
            break
        url = f"{base}/media/{v['slug']}.mp4"
        # 영상 파일은 만든 날의 배포에만 들어가므로, 파일이 없는 예전 영상은 건너뛰고 다음 영상을 시도
        if requests.head(url, timeout=30, allow_redirects=True).status_code != 200:
            log(f"  Instagram 건너뜀 {v['slug']}: 영상 파일이 배포본에 없음")
            continue
        try:
            has_post = conn.execute("SELECT 1 FROM posts WHERE slug = ?", (v["slug"],)).fetchone()
            post_url = f"{base}/posts/{v['slug']}/" if has_post else f"{base}/"   # 3D 타임랩스처럼 글이 없는 영상
            mid = instagram.publish_reel(url, instagram.caption_for(dict(v), post_url))
            status, err = "posted", None
            posted.append(mid)
            log(f"  Instagram 릴스 게시 {v['slug']} → {mid}")
        except Exception as e:
            mid, status, err = None, "failed", str(e)[:500]
            failed.append({"slug": v["slug"], "error": err})
            log(f"  Instagram 게시 실패 {v['slug']}: {e}")
        conn.execute(
            """INSERT INTO social_posts (slug, platform, status, remote_id, error, attempts, updated_at)
               VALUES (?, 'instagram', ?, ?, ?, 1, ?)
               ON CONFLICT(slug, platform) DO UPDATE SET status=excluded.status, remote_id=excluded.remote_id,
                   error=excluded.error, attempts=attempts+1, updated_at=excluded.updated_at""",
            (v["slug"], status, mid, err, db.now_iso()))
        conn.commit()
    summary["instagram"] = {"posted": posted, "failed": failed}
    try:
        from . import metrics
        metrics.collect_instagram(conn)
    except Exception as e:
        log(f"Instagram 성과 수집 건너뜀: {e}")
    if failed and not posted:
        raise RuntimeError(f"Instagram 게시 {len(failed)}건 모두 실패")


def do_learn(conn, cfg, summary):
    """성과 수집 → 가중치·교훈 갱신. 실패해도 발행은 계속한다 (오류로 세지 않음)."""
    from . import learn, metrics
    from .write import ClaudeWriter
    try:
        got = {"youtube": metrics.collect_youtube(conn), "tiktok": metrics.collect_tiktok(conn)}
        res = learn.update(conn, lambda: ClaudeWriter(cfg["generate"]["model"], cfg["generate"]["effort"]))
        summary["learn"] = {"collected": got, **res}
        log(f"성과 학습: 수집 {got}, 평가 대상 {res['scored']}편")
    except Exception as e:
        summary["learn"] = {"error": f"{type(e).__name__}: {e}"[:300]}
        log(f"성과 학습 건너뜀: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["collect", "generate", "build", "shorts", "all", "instagram", "learn"])
    ap.add_argument("--demo", action="store_true", help="가상 데이터 + 가짜 작성기 (별도 DB 사용)")
    ap.add_argument("--fake-writer", action="store_true", help="Claude 대신 가짜 작성기")
    ap.add_argument("--tts", choices=["auto", "edge", "gtts", "silent"], help="TTS 공급자 덮어쓰기")
    args = ap.parse_args(argv)

    cfg = load_config()
    db_path = DATA_DIR / "demo.db" if args.demo else DB_PATH
    conn = db.connect(db_path)
    run_id = db.start_run(conn)
    summary = {"command": args.command, "demo": args.demo, "errors": []}

    def stage(name, fn):
        # 한 단계가 실패해도 다음 단계(특히 사이트 빌드)는 계속 진행
        try:
            fn()
        except Exception as e:
            summary["errors"].append(f"{name}: {type(e).__name__}: {e}")
            log(f"오류[{name}]: {e}")

    def build():
        summary["site_posts"] = site.build(conn, cfg)
        log(f"사이트 빌드 완료: 글 {summary['site_posts']}편 → public/")

    if args.command in ("collect", "all"):
        stage("collect", lambda: do_collect(conn, cfg, summary, demo=args.demo))
    if args.command in ("generate", "all"):
        stage("generate", lambda: do_generate(conn, cfg, summary, fake=args.demo or args.fake_writer))
    if args.command in ("learn", "all") and not args.demo:
        do_learn(conn, cfg, summary)   # 새 영상을 만들기 전에 지난 성과를 반영
    if args.command in ("shorts", "all"):
        stage("shorts", lambda: do_shorts(conn, cfg, summary, fake=args.demo or args.fake_writer,
                                          tts_override=args.tts))
    if args.command in ("build", "all"):
        stage("build", build)  # 업로드된 영상 ID를 글에 넣기 위해 숏폼 다음에 빌드
    if args.command == "instagram":  # 'all'에 포함하지 않음: Pages 배포 이후 별도 작업으로 실행
        stage("instagram", lambda: do_instagram(conn, cfg, summary))

    status = "error" if summary["errors"] else "ok"
    summary = redact(summary)
    db.finish_run(conn, run_id, status, summary)
    if not args.demo and args.command != "instagram":
        (DATA_DIR / "last_run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
