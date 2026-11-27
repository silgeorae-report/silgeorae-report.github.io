import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from pipeline import collect, db, generate, qa, site
from pipeline.config import load_config
from pipeline.demo import make_trades
from pipeline.write import FakeWriter

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><aptNm>테스트아파트</aptNm><dealAmount>  82,500</dealAmount><excluUseAr>84.97</excluUseAr>
<floor>12</floor><buildYear>2008</buildYear><dealYear>2026</dealYear><dealMonth>10</dealMonth>
<dealDay>3</dealDay><umdNm>역삼동</umdNm><jibun>123</jibun><aptSeq>11680-111</aptSeq>
<cdealType> </cdealType><dealingGbn>중개거래</dealingGbn></item>
<item><aptNm>취소아파트</aptNm><dealAmount>50,000</dealAmount><excluUseAr>59.9</excluUseAr>
<floor>3</floor><buildYear>1999</buildYear><dealYear>2026</dealYear><dealMonth>10</dealMonth>
<dealDay>5</dealDay><umdNm>역삼동</umdNm><jibun>9</jibun><cdealType>O</cdealType></item>
</items><numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body></response>"""


class CollectTest(unittest.TestCase):
    def test_parse(self):
        rows, total = collect.parse_items(SAMPLE_XML, "11680")
        self.assertEqual(total, 2)
        self.assertEqual(rows[0]["price"], 82500)
        self.assertEqual(rows[0]["deal_date"], "2026-10-03")
        self.assertEqual(rows[0]["canceled"], 0)
        self.assertEqual(rows[1]["canceled"], 1)

    def test_auth_error(self):
        xml = ("<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
               "</returnAuthMsg></cmmMsgHeader></OpenAPI_ServiceResponse>")
        with self.assertRaises(collect.CollectError):
            collect.parse_items(xml, "11680")

    def test_recent_months(self):
        self.assertEqual(collect.recent_months(3, date(2026, 2, 10)), ["202602", "202601", "202512"])


class QATest(unittest.TestCase):
    cfg = load_config()
    facts = {"region": "강남구", "trade_count": "120건", "median_price": "25억 3,000만원", "as_of": "2026-11-14"}

    def draft(self, body, title="강남구 아파트 거래 {{trade_count}} 정리 리포트"):
        filler = "\n\n## 소제목 하나\n\n## 소제목 둘\n\n## 소제목 셋\n\n" + "충분한 분량의 해설 문장입니다. " * 80
        return {"title": title, "description": "강남구 아파트 실거래 데이터를 정리했습니다. 거래 건수와 중위 가격을 확인하세요.",
                "tags": ["강남구"], "body_md": body + filler + "\n\n[[CHART]]\n\n[[TABLE:top10]]\n",
                "faq": [{"question": "출처?", "answer": "국토교통부 {{as_of}}"}] * 3}

    def test_clean_passes(self):
        self.assertEqual(qa.check("gu_monthly", self.draft("거래는 {{trade_count}}입니다."), self.facts, self.cfg), [])

    def test_area_mislabel_and_korean_number_rejected(self):
        p = qa.check("gu_monthly", self.draft("소형 비중이 낮아 중대형 거래가 더 많았다는 뜻입니다."), self.facts, self.cfg)
        self.assertTrue(any("면적 해석" in x for x in p))
        p = qa.check("gu_monthly", self.draft("전용 육십 제곱미터 이하 거래입니다."), self.facts, self.cfg)
        self.assertTrue(any("숫자 표기" in x for x in p))
        ok = qa.check("gu_monthly", self.draft("전용 60㎡를 넘는 거래가 더 많았습니다. 중대형 거래가 많은 달에는 중위가가 오릅니다."),
                      self.facts, self.cfg)
        self.assertEqual(ok, [])

    def test_price_shrink_and_all_time_rejected(self):
        p = qa.check("gu_monthly", self.draft("거래량과 중위가가 모두 줄었습니다."), self.facts, self.cfg)
        self.assertTrue(any("표현 오류" in x for x in p))
        p = qa.check("gu_monthly", self.draft("역대 최고가 거래가 나왔습니다."), self.facts, self.cfg)
        self.assertTrue(any("근거 없는 표현" in x for x in p))
        ok = qa.check("gu_monthly", self.draft("거래량은 줄고 중위가는 내렸습니다."), self.facts, self.cfg)
        self.assertEqual(ok, [])

    def test_median_as_mode_rejected(self):
        p = qa.check("gu_monthly", self.draft("중간값은 이 범위의 가운데쯤에 있어 실제로 많이 거래된 가격대를 보여줍니다."), self.facts, self.cfg)
        self.assertTrue(any("해석 오류" in x for x in p))
        ok = qa.check("gu_monthly", self.draft("중간값은 가격을 순서대로 늘어놓았을 때 가운데 값입니다."), self.facts, self.cfg)
        self.assertEqual(ok, [])

    def test_report_lag_sentence_rejected(self):
        for s in ["실거래 신고는 계약 후 일정 기간 안에 하면 되므로 최근 계약분이 늘 수 있습니다.",
                  "실거래가는 계약 후 신고까지 시차가 있습니다.",
                  "최근 거래는 아직 다 반영되지 않았을 수 있습니다.",
                  "최근 몇 주의 거래는 이후 추가 신고로 늘어날 수 있습니다."]:
            p = qa.check("gu_monthly", self.draft(s), self.facts, self.cfg)
            self.assertTrue(any("중복 안내" in x for x in p), s)
        ok = qa.check("gu_monthly", self.draft("신고가 거래는 기존 최고 거래가를 넘어선 거래입니다. 기간 중 거래를 정리했습니다."),
                      self.facts, self.cfg)
        self.assertEqual(ok, [])

    def test_raw_number_rejected(self):
        p = qa.check("gu_monthly", self.draft("거래는 135건으로 늘었습니다."), self.facts, self.cfg)
        self.assertTrue(any("135" in x for x in p))

    def test_number_from_facts_allowed(self):
        self.assertEqual(qa.check("gu_monthly", self.draft("거래 120건."), self.facts, self.cfg), [])

    def test_list_numbers_and_rank_allowed(self):
        self.assertEqual(qa.check("gu_monthly", self.draft("1. 거래 {{trade_count}}\n2. 표의 1위 단지"), self.facts,
                                  self.cfg), [])
        p = qa.check("gu_monthly", self.draft("단지 2곳에서 거래"), self.facts, self.cfg)
        self.assertTrue(any("'2'" in x for x in p))

    def test_unknown_placeholder(self):
        p = qa.check("gu_monthly", self.draft("{{made_up}}"), self.facts, self.cfg)
        self.assertTrue(any("made_up" in x for x in p))

    def test_banned_phrase(self):
        p = qa.check("gu_monthly", self.draft("지금 사야 합니다."), self.facts, self.cfg)
        self.assertTrue(any("금지" in x for x in p))

    def test_josa(self):
        f = {"a": "404건", "b": "6억 7,000만원", "c": "상계동", "d": "6층", "e": "15건", "f": "노원구", "g": "3.2%",
             "h": "서울"}
        self.assertEqual(qa.fill("{{a}}였습니다 {{a}}와 {{b}}로 {{c}}와 {{d}}로 {{e}}가", f),
                         "404건이었습니다 404건과 6억 7,000만원으로 상계동과 6층으로 15건이")
        self.assertEqual(qa.fill("{{f}}이 {{f}}은 {{g}}로 {{h}}으로 {{f}}이번", f),
                         "노원구가 노원구는 3.2%로 서울로 노원구이번")
        self.assertEqual(qa.fill("{{c}}와는 {{f}}과도", f), "상계동과는 노원구와도")
        # 끝의 괄호 설명은 건너뛰고 그 앞 글자의 받침으로 고른다
        self.assertEqual(qa.fill("{{x}}가 {{y}}이", {"x": "극동(105)", "y": "현대(관악)"}),
                         "극동(105)이 현대(관악)가")
        # 같은 값을 두 번 나열하지 않는다
        self.assertEqual(qa.fill("각각 {{p}}, {{q}}으로 {{p}}, 7건", {"p": "6건", "q": "6건"}),
                         "각각 6건으로 6건, 7건")

    def test_render_fills(self):
        out = qa.render(self.draft("거래 {{trade_count}}"), self.facts, {"top10": "|a|"}, "/c.png", "alt")
        self.assertIn("거래 120건", out["body_md"])
        self.assertNotIn("{{", out["body_md"])
        self.assertIn("![alt](/c.png)", out["body_md"])


class EndToEndTest(unittest.TestCase):
    def test_demo_pipeline(self):
        cfg = load_config()
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            today = date(2026, 11, 14)
            db.upsert_trades(conn, make_trades(["11680", "11650"], today=today))
            jobs = generate.plan(conn, cfg, today)
            self.assertTrue(jobs)
            for job in jobs:
                ok, problems = generate.produce(conn, cfg, FakeWriter(), job, "2026-11-14")
                self.assertTrue(ok, problems)
            # 같은 데이터로 다시 계획하면 할 일이 줄어야 함 (중복 생성 방지)
            again = {j["slug"] for j in generate.plan(conn, cfg, today)}
            self.assertFalse(again & {j["slug"] for j in jobs})
            out = Path(tmp) / "public"
            n = site.build(conn, cfg, out)
            self.assertEqual(n, len(jobs))
            self.assertTrue((out / "sitemap.xml").exists())
            # 서치 콘솔 소유권 확인 파일이 매 빌드마다 배포돼야 함
            vf = out / "google0ca3dda0f1e228c8.html"
            self.assertEqual(vf.read_text(encoding="utf-8").strip(),
                             "google-site-verification: google0ca3dda0f1e228c8.html")
            self.assertEqual((out / "naverbfd29d919134007c40e4b91c4b161d16.html").read_text(encoding="utf-8").strip(),
                             "naver-site-verification: naverbfd29d919134007c40e4b91c4b161d16.html")
            html = (out / "posts" / jobs[0]["slug"] / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("{{", html)
            conn.close()


class RedactTest(unittest.TestCase):
    def test_secrets_removed(self):
        import os
        from unittest import mock
        from pipeline.run import redact
        s = {"errors": ["403 for url: https://x/?serviceKey=abc123SECRETxyz&LAWD_CD=1", "tok sk-ant-oat01-ZZZZZZZZ"]}
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-ZZZZZZZZ"}):
            out = json.dumps(redact(s))
        self.assertNotIn("abc123SECRETxyz", out)
        self.assertNotIn("ZZZZZZZZ", out)
        self.assertIn("LAWD_CD=1", out)

    def test_403_message_has_no_url(self):
        from unittest import mock
        resp = mock.Mock(status_code=403, text="Forbidden")
        sess = mock.Mock(get=mock.Mock(return_value=resp))
        with self.assertRaises(collect.CollectError) as cm:
            collect.fetch_month("https://x", "11110", "202610", api_key="SECRETKEY123", session=sess)
        self.assertNotIn("SECRETKEY123", str(cm.exception))
        self.assertIn("인증", str(cm.exception))


class InstagramTest(unittest.TestCase):
    def test_token_state_and_publish(self):
        import os
        from unittest import mock
        from cryptography.fernet import Fernet
        from pipeline import instagram

        def resp(code, data):
            return mock.Mock(status_code=code, json=mock.Mock(return_value=data))

        gets = {"refresh_access_token": resp(200, {"access_token": "NEWTOKEN"}),
                "me": resp(200, {"user_id": "123", "username": "x"}),
                "C1": resp(200, {"status_code": "FINISHED"})}
        posts = {"media": resp(200, {"id": "C1"}), "media_publish": resp(200, {"id": "M9"})}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(instagram, "STATE", Path(tmp) / "s.enc"), \
                mock.patch.dict(os.environ, {"IG_STATE_KEY": Fernet.generate_key().decode(), "IG_ACCESS_TOKEN": "OLD"}), \
                mock.patch.object(instagram.requests, "get", lambda url, **k: gets[url.rsplit("/", 1)[1]]), \
                mock.patch.object(instagram.requests, "post", lambda url, **k: posts[url.rsplit("/", 1)[1]]):
            self.assertTrue(instagram.configured())
            self.assertEqual(instagram.publish_reel("https://x/v.mp4", "cap"), "M9")
            raw = instagram.STATE.read_bytes()
            self.assertNotIn(b"NEWTOKEN", raw)  # 암호화 저장
            self.assertEqual(instagram._load()["token"], "NEWTOKEN")


class InstagramBusinessTest(unittest.TestCase):
    def test_system_user_token_uses_facebook_graph(self):
        import os
        from unittest import mock
        from pipeline import instagram
        calls = []

        def resp(data):
            return mock.Mock(status_code=200, json=mock.Mock(return_value=data))

        def post(url, **k):
            calls.append(url)
            return resp({"id": "C1"} if url.endswith("/media") else {"id": "M9"})

        def get(url, **k):
            calls.append(url)
            return resp({"status_code": "FINISHED"})

        with mock.patch.dict(os.environ, {"IG_ACCESS_TOKEN": "EAAxxxx", "IG_USER_ID": "1784"}, clear=False), \
                mock.patch.dict(os.environ, {"IG_STATE_KEY": ""}), \
                mock.patch.object(instagram.requests, "get", get), \
                mock.patch.object(instagram.requests, "post", post):
            self.assertTrue(instagram.configured())
            self.assertEqual(instagram.publish_reel("https://x/v.mp4", "cap"), "M9")
        self.assertTrue(all(u.startswith("https://graph.facebook.com/") for u in calls))
        self.assertIn("https://graph.facebook.com/1784/media", calls)

    def test_token_bom_and_newline_are_removed(self):
        from pipeline import instagram
        self.assertEqual(instagram._clean("﻿EAAxx\r\n"), "EAAxx")
        self.assertEqual(instagram._clean(' "EAAxx" '), "EAAxx")
        self.assertNotIn("EAAxx", instagram._shape("EAAxx"))


class WriterTest(unittest.TestCase):
    def test_parse_json_fallback(self):
        from pipeline.write import _parse_json
        self.assertEqual(_parse_json('```json\n{"approved": true, "issues": []}\n```')["approved"], True)
        self.assertEqual(_parse_json('검수 결과: {"approved": false, "issues": ["x"]}')["issues"], ["x"])

    def test_api_key_not_passed(self):
        import os
        from unittest import mock
        from pipeline.write import ClaudeWriter
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x", "CLAUDE_BIN": "claude"}):
            self.assertNotIn("ANTHROPIC_API_KEY", ClaudeWriter("m").env)


class ShortsTest(unittest.TestCase):
    def test_script_check_and_render(self):
        from pipeline import shorts
        cfg = load_config()
        cfg["shorts"]["tts_provider"] = "silent"
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            today = date(2026, 11, 14)
            db.upsert_trades(conn, make_trades(["11680"], today=today))
            for job in generate.plan(conn, cfg, today):
                if job["kind"] == "gu_monthly":
                    generate.produce(conn, cfg, FakeWriter(), job, "2026-11-14")
            posts = shorts.plan(conn, cfg)
            self.assertTrue(posts)
            facts = __import__("json").loads(posts[0]["facts_json"])

            bad = FakeWriter().write_shorts(facts)
            bad["scenes"]["volume"]["narration"] = "거래가 777건 늘었습니다."
            self.assertTrue(any("777" in p for p in shorts.check_script(bad, facts, cfg)))
            self.assertEqual(shorts.check_script(FakeWriter().write_shorts(facts), facts, cfg), [])

            ok, problems = shorts.produce(conn, cfg, FakeWriter(), posts[0], Path(tmp) / "v")
            self.assertTrue(ok, problems)
            self.assertTrue((Path(tmp) / "v" / f"{posts[0]['slug']}.mp4").stat().st_size > 10000)
            v = conn.execute("SELECT * FROM videos").fetchone()
            self.assertTrue(v["title"].endswith("#Shorts"))
            self.assertIn("/posts/", v["description"])
            self.assertLessEqual(v["seconds"], cfg["shorts"]["max_seconds"])
            # 이미 영상이 있는 글은 다시 계획되지 않음
            self.assertNotIn(posts[0]["slug"], [p["slug"] for p in shorts.plan(conn, cfg)])
            conn.close()

    def test_too_long_audio_retries_shorter_script(self):
        from unittest import mock
        from pipeline import shorts
        cfg = load_config()
        cfg["shorts"]["tts_provider"] = "silent"
        seen = []

        class Recorder(FakeWriter):
            def write_shorts(self, facts, feedback=None, guidance=""):
                seen.append(feedback)
                return super().write_shorts(facts, feedback, guidance)

        real_speak = shorts.tts.speak
        calls = {"n": 0}

        def speak(text, out, provider, voice):
            real_speak(text, out, provider, voice)
            calls["n"] += 1
            return 20.0 if calls["n"] <= len(shorts.SHORTS_SCENES) else 3.0   # 첫 대본만 한도 초과

        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            db.upsert_trades(conn, make_trades(["11680"], today=date(2026, 11, 14)))
            for job in generate.plan(conn, cfg, date(2026, 11, 14)):
                if job["kind"] == "gu_monthly":
                    generate.produce(conn, cfg, FakeWriter(), job, "2026-11-14")
            post = shorts.plan(conn, cfg)[0]
            with mock.patch.object(shorts.tts, "speak", speak):
                ok, problems = shorts.produce(conn, cfg, Recorder(), post, Path(tmp) / "v")
            self.assertTrue(ok, problems)
            self.assertEqual(len(seen), 2)
            self.assertTrue(any("짧게" in f for f in seen[1]))
            conn.close()




class Timelapse3DTest(unittest.TestCase):
    def test_facts_and_frame(self):
        import statistics
        from pipeline import timelapse3d as tl
        from pipeline.analyze import PYEONG, won
        from pipeline.regions import SEOUL
        shapes = tl.shapes()
        self.assertEqual(set(shapes), {gu for gu, _ in SEOUL.values()})            # 25개 구 실제 경계 모두 있음
        for gu, ring in shapes.items():
            self.assertGreater(len(ring), 10, gu)
            self.assertGreater(tl._area(ring), 0, gu)                               # 꼭짓점 순서 통일
            parts = tl.tiles(ring)                                                  # 격자 조각 넓이 합 = 구 넓이
            self.assertAlmostEqual(sum(abs(tl._area(q)) for _, _, q, _ in parts if q), tl._area(ring), places=6)
        east = tl._centroid(shapes["강동구"])[0]
        west = tl._centroid(shapes["강서구"])[0]
        north = tl._centroid(shapes["도봉구"])[1]
        south = tl._centroid(shapes["서초구"])[1]
        self.assertGreater(east, west)                                              # 동쪽이 x 큼
        self.assertLess(north, south)                                               # 북쪽이 y 작음
        self.assertEqual(tl.default_end(date(2026, 11, 15)), "202609")              # 신고 기한 안 끝난 달 제외
        today = date(2026, 11, 14)
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            db.upsert_trades(conn, make_trades(["11680", "11650"], today=today))
            data = tl.seoul_facts(conn, "202610", 6)
            self.assertEqual(len(data["months"]), 6)
            rows = conn.execute("SELECT price, area FROM trades WHERE canceled = 0 AND sgg_cd = '11680' "
                                "AND deal_ym = '202610'").fetchall()
            expect = statistics.median(r["price"] / (r["area"] / PYEONG) for r in rows)
            self.assertAlmostEqual(data["values"]["202610"]["강남구"], expect)
            self.assertIn(data["facts"]["top1_ppp"], {won(v) for v in data["values"]["202610"].values()})
            frame = tl.Timelapse(data).frame(3.0)
            self.assertEqual(frame.size, (1080, 1920))
            conn.close()

    def test_geometry(self):
        from pipeline import timelapse3d as tl
        hull = tl._hull([(0, 0), (2, 0), (2, 2), (0, 2), (1, 1)])
        self.assertEqual(len(hull), 4)
        self.assertTrue(tl._inside((1, 1), hull))
        self.assertFalse(tl._inside((3, 1), hull))
        near = tl.project(0, 1, 0, 0.0, 1, 0, 0)[2]
        far = tl.project(0, -1, 0, 0.0, 1, 0, 0)[2]
        self.assertGreater(near, far)                                               # 남쪽(아래)이 앞

    def test_monthly_script_and_skip(self):
        from pipeline import timelapse3d as tl
        from pipeline.config import load_config
        facts = {"start_month": "2025년 10월", "end_month": "2026년 9월", "top1_gu": "강남구",
                 "top1_ppp": "1억 1,730만원", "low1_gu": "도봉구", "low1_ppp": "2,528만원", "trade_total": "66,603건"}
        sc = tl.script(facts, "https://example.com/")
        self.assertEqual(tl.check(sc, facts, ["투자 추천"]), [])
        self.assertIn("1억 1,730만원", sc["narration"])
        bad = dict(sc, narration=sc["narration"] + " 내년엔 15% 오릅니다")
        self.assertTrue(any("15" in p for p in tl.check(bad, facts)))              # 근거 없는 숫자 차단
        self.assertEqual(tl.slug_for("202609"), "seoul-3d-202609")
        with tempfile.TemporaryDirectory() as tmp:                                  # 이미 만든 달은 다시 만들지 않음
            conn = db.connect(Path(tmp) / "t.db")
            conn.execute("INSERT INTO videos (slug, title, description, tags, seconds, status, created_at, updated_at)"
                         " VALUES ('seoul-3d-202609', 't', 'd', '[]', 1, 'uploaded', 'x', 'x')")
            self.assertIsNone(tl.produce_monthly(conn, load_config(), Path(tmp), date(2026, 11, 16)))
            conn.close()


class InstagramErrorTest(unittest.TestCase):
    def test_error_hides_ids(self):
        from pipeline import instagram

        class R:
            status_code = 400
            def json(self):
                return {"error": {"type": "GraphMethodException", "code": 100,
                                  "message": "Object with ID '17841400000000000' does not exist"}}
        msg = instagram._err(R())
        self.assertNotIn("17841400000000000", msg)
        self.assertIn("<ID>", msg)


class DoubleNegativeTest(unittest.TestCase):
    def test_minus_with_decrease_word(self):
        out = qa.fill("전월보다 {{chg}}% 낮아졌고 거래는 {{t}}% 늘었습니다", {"chg": -13.2, "t": 5.0})
        self.assertIn("13.2% 낮아졌고", out)
        self.assertNotIn("-13.2", out)


class AlignTrendTest(unittest.TestCase):
    def test_last_bar_matches_post_count(self):
        from pipeline import shorts
        trend = [("202609", 84, 6.0), ("202610", 101, 6.05)]
        self.assertEqual(shorts.align_trend(trend, {"trade_count": "98건"})[-1], ("202610", 98, 6.05))
        self.assertEqual(shorts.align_trend(trend, {})[-1], ("202610", 101, 6.05))
        self.assertEqual(shorts.align_trend([], {"trade_count": "98건"}), [])

    def test_last_point_matches_post_median(self):
        from pipeline import shorts
        trend = [("202608", 203, 71000), ("202609", 205, 73700)]
        out = shorts.align_trend(trend, {"trade_count": "202건", "median_price": "7억 3,900만원"})
        self.assertEqual(out[-1], ("202609", 202, 73900))
        self.assertEqual(shorts.align_trend(trend, {"median_price": "9,800만원"})[-1][2], 9800)
        self.assertEqual(shorts.align_trend(trend, {"median_price": "12억원"})[-1][2], 120000)
        self.assertEqual(shorts.align_trend(trend, {"median_price": "자료 없음"})[-1][2], 73700)


class ClipThemeTest(unittest.TestCase):
    def test_light_theme_on_dark_clip_uses_light_text(self):
        from pipeline import motion
        clip = motion.ClipBackground(Path("none.mp4"))     # 열지 않음: 종류만 판별
        paper = motion.THEMES["paper"]
        self.assertEqual(motion.readable_theme(paper, clip)["text"], "#ffffff")
        self.assertEqual(motion.readable_theme(paper, None)["text"], paper["text"])   # 그라데이션
        self.assertIs(motion.readable_theme(motion.THEMES["midnight"], clip), motion.THEMES["midnight"])
        for name, th in motion.THEMES.items():
            self.assertEqual(motion.is_light(th), name == "paper")


class MediaFallbackTest(unittest.TestCase):
    def test_no_off_variant(self):
        from pipeline import learn
        pb = learn.load()
        for i in range(50):
            self.assertIn(learn.choose(pb, f"seoul-x-{i}")["photo"], ("on", "clip"))

    def test_people_tags_filtered(self):
        from pipeline import assets
        self.assertTrue(assets.PEOPLE_TAGS.search("seoul, woman, street"))
        self.assertFalse(assets.PEOPLE_TAGS.search("seoul, skyline, apartment, manhattan"))

    def test_rank_scores_robust_to_zero_median(self):
        from pipeline import learn
        r = learn._rank_scores({"a": 0, "b": 0, "c": 0, "d": 139})
        self.assertEqual(r["d"], 1.5)
        self.assertLess(r["a"], 1.0)
        self.assertEqual(r["a"], r["b"])
        self.assertEqual(learn._rank_scores({"a": 0, "b": 0}), {})

    def test_instagram_insights_views(self):
        from unittest import mock
        from pipeline import metrics
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"data": [{"name": "views", "total_value": {"value": 120}},
                                         {"name": "shares", "values": [{"value": 3}]}]}
        with mock.patch.object(metrics.requests, "get", return_value=ok):
            self.assertEqual(metrics._ig_insights("1", "t"), (120, 3))
        with mock.patch.object(metrics.requests, "get", return_value=mock.Mock(status_code=400)):
            self.assertEqual(metrics._ig_insights("1", "t"), (None, None))

    def test_pixabay_clip_requires_korea_tags(self):
        import os
        from unittest import mock
        from pipeline import assets
        hits = {"hits": [
            {"id": 1, "duration": 20, "tags": "temple, palm trees, india", "videos": {"medium": {"url": "u1"}}},
            {"id": 2, "duration": 20, "tags": "seoul, skyline, night", "videos": {"medium": {"url": "u2"}}},
            {"id": 3, "duration": 20, "tags": "korea, people, street", "videos": {"medium": {"url": "u3"}}},
        ]}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = hits
        with mock.patch.dict(os.environ, {"PIXABAY_API_KEY": "x"}),                 mock.patch.object(assets.requests, "get", return_value=resp):
            out = assets._pixabay("seoul skyline")
        self.assertEqual([c["id"] for c in out], ["2"])

    def test_seoul_photo_needs_writer(self):
        import sqlite3, tempfile
        from pathlib import Path
        from pipeline import assets
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.assertEqual(assets.seoul_photo(conn, object(), "s", Path(tempfile.gettempdir())), (None, None, None))


class KtoPhotoTest(unittest.TestCase):
    def test_only_type1_and_district_code(self):
        import os
        from unittest import mock
        from pipeline import assets
        body = {"response": {"body": {"items": {"item": [
            {"title": "A공원", "firstimage": "http://tong.visitkorea.or.kr/a.jpg", "cpyrhtDivCd": "Type1",
             "contentid": "1", "addr1": "서울"},
            {"title": "B궁", "firstimage": "http://tong.visitkorea.or.kr/b.jpg", "cpyrhtDivCd": "Type3",
             "contentid": "2"},
            {"title": "C", "firstimage": "", "cpyrhtDivCd": "Type1", "contentid": "3"}]}}}}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = body
        with mock.patch.dict(os.environ, {"DATA_GO_KR_KEY": "k"}), \
                mock.patch.object(assets.requests, "get", return_value=resp) as g:
            out = assets.kto_search("강남구")
        self.assertEqual([c["title"] for c in out], ["A공원"])
        self.assertTrue(out[0]["file_url"].startswith("https://"))
        self.assertTrue(assets.license_ok(out[0]["license"]))
        p = g.call_args.kwargs["params"]
        self.assertEqual((p["lDongRegnCd"], p["lDongSignguCd"]), ("11", "680"))

    def test_no_key_no_call(self):
        import os
        from unittest import mock
        from pipeline import assets
        with mock.patch.dict(os.environ, {"DATA_GO_KR_KEY": ""}):
            self.assertEqual(assets.kto_search("강남구"), [])


class DeadlineNoteTest(unittest.TestCase):
    def test_closed_month_drops_deadline_wording(self):
        from pipeline import qa
        self.assertNotIn("신고 기한", qa._deadline_note({"month": "2026년 9월", "as_of": "2026-11-19"}))
        self.assertIn("신고 기한", qa._deadline_note({"month": "2026년 10월", "as_of": "2026-11-19"}))
        self.assertIn("신고 기한", qa._deadline_note({"as_of": "2026-11-19"}))

    def test_pyeong_note_is_not_data_number(self):
        from pipeline import qa
        self.assertNotIn("1", qa.NON_DATA.sub("", "전용면적 3.3㎡(1평)당"))


if __name__ == "__main__":
    unittest.main()


class CarryMediaTest(unittest.TestCase):
    def test_recent_video_kept_on_rebuild(self):
        from datetime import datetime, timezone
        conn = db.connect(":memory:")
        now = datetime.now(timezone.utc).isoformat()
        for slug, created in (("seoul-a-2026-09", now), ("seoul-old-2026-01", "2026-01-01T00:00:00+00:00"),
                              ("seoul-here-2026-09", now)):
            conn.execute("INSERT INTO videos (slug,title,description,tags,seconds,status,created_at,updated_at)"
                         " VALUES (?,?,?,?,?,?,?,?)", (slug, "t", "d", "[]", 30, "uploaded", created, created))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "media").mkdir()
            (out / "media" / "seoul-here-2026-09.mp4").write_bytes(b"x")
            asked = []
            carried = site._carry_media(conn, {"base_url": "https://ex.github.io"}, out,
                                        fetch=lambda u: asked.append(u) or b"mp4")
            self.assertEqual(carried, ["seoul-a-2026-09"])
            self.assertEqual(asked, ["https://ex.github.io/media/seoul-a-2026-09.mp4"])
            self.assertTrue((out / "media" / "seoul-a-2026-09.mp4").exists())


class DataAsOfTest(unittest.TestCase):
    def test_collect_failure_uses_last_collected_date(self):
        from datetime import datetime, timezone
        from pipeline import run
        conn = db.connect(":memory:")
        conn.execute("INSERT INTO trades (id, sgg_cd, apt_name, area, deal_date, deal_ym, price, collected_at) "
                     "VALUES ('t1', '11350', 'a', 59.9, '2026-10-01', '202610', 1, '2026-11-26T16:10:00+00:00')")
        now = datetime(2026, 11, 28, 1, 30, tzinfo=run.KST)
        self.assertEqual(run.data_as_of(conn, {"errors": []}, now), "2026-11-28")
        failed = {"errors": ["collect: CollectError: API 인증 오류: HTTP 에러"]}
        self.assertEqual(run.data_as_of(conn, failed, now), "2026-11-27")  # UTC 16:10 = KST 다음 날 01:10
