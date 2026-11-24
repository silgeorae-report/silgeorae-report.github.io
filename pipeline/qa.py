"""자동 품질 게이트: 사람 검수 대신 규칙 검사 + (선택) Claude 2차 검수."""
import re

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
MARKER = re.compile(r"\[\[(CHART|TABLE:[a-z0-9_]+)\]\]")
DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
# 설명에 자연스럽게 쓰일 수 있는 고정 상수 (평 환산, 소형 기준, 신고기한)
ALWAYS_OK = {"3.3", "60", "30"}
# 데이터 값이 아닌 숫자: 마크다운 번호 목록("1. "), 표 순위("1위"~"10위"), 평 환산 설명 "(1평)"
NON_DATA = re.compile(r"(?m)^\s*\d{1,2}[.)]\s|(?<!\d)(?:10|[1-9])(?=위)|\(1평\)")
# 소형(60㎡ 이하)의 나머지 전체를 '중대형'으로 부르는 해석 오류 (60~85㎡는 중소형)
AREA_MISLABEL = re.compile(r"나머지[^.\n]{0,15}(?:중대형|중형 이상)"
                           r"|(?:중대형|중형 이상)[^.\n]{0,15}(?:더 많았|절반(?:이|을) 넘|많았다는 뜻)")
# 숫자를 한글로 풀어 쓴 면적 표기 (예: '육십 제곱미터')
KOREAN_NUMBER = re.compile(r"(?:육십|삼십|팔십오?)\s?(?:제곱미터|㎡)")
# 가격에 '줄었다'를 쓰는 표현 (예: '거래량과 중위가가 모두 줄었다') → 가격은 '내렸다/낮아졌다'
PRICE_SHRINK = re.compile(r"(?:중위\s?가(?:격)?|가격)[^.\n]{0,30}(?:모두|함께|둘 다)\s?(?:전월보다\s?)?줄")
# 수집 기간(high_since) 안의 비교일 뿐이라 '역대·사상 최고'는 근거 없음
ALL_TIME = re.compile(r"역대\s?최고|사상\s?최고")
# 중간값(중위값)을 최빈값처럼 '가장 많이 거래된 가격대'로 설명하는 해석 오류
MEDIAN_AS_MODE = re.compile(r"(?:중간값|중간 가격|중위\s?(?:가격|거래가|가|값))[^.\n]{0,60}"
                            r"(?:많이 거래된|거래가 (?:가장 )?많이 몰린|가장 많이 몰린) 가격대")
# 본문에서 신고 기한·시차를 다시 설명하는 문장 (하단 주석에 자동으로 붙으므로 중복. 2차 검수 반려의 최다 사유)
REPORT_LAG = re.compile(r"신고(?!가)[^.\n]{0,40}(?:시차|일정 기간|기한)|(?:시차|일정 기간)[^.\n]{0,30}신고"
                        r"|아직 (?:다 )?반영되지 않|추가 신고|추가로 (?:반영|신고)")

REQUIRED_MARKERS = {
    "gu_monthly": {"CHART", "TABLE:top10"},
    "complex": {"CHART", "TABLE:areas", "TABLE:recent"},
}


def _allowed_numbers(facts: dict) -> set[str]:
    ok = set(ALWAYS_OK)
    for v in facts.values():
        for d in DIGITS.findall(str(v)):
            ok.add(d)
            ok.add(d.replace(",", ""))
            ok.update(re.split(r"[.,-]", d))
    return ok


def check(kind: str, draft: dict, facts: dict, cfg: dict) -> list[str]:
    problems = []
    texts = {"title": draft["title"], "description": draft["description"], "body": draft["body_md"]}
    for i, qa in enumerate(draft["faq"]):
        texts[f"faq{i}"] = qa["question"] + " " + qa["answer"]

    allowed = _allowed_numbers(facts)
    for name, text in texts.items():
        for key in PLACEHOLDER.findall(text):
            if key not in facts:
                problems.append(f"{name}: 존재하지 않는 플레이스홀더 {{{{{key}}}}}")
        stripped = NON_DATA.sub("", MARKER.sub("", PLACEHOLDER.sub("", text)))
        for mt in DIGITS.finditer(stripped):
            num = mt.group()
            if num not in allowed and num.replace(",", "") not in allowed:
                ctx = stripped[max(0, mt.start() - 25): mt.end() + 15].replace("\n", " ").strip()
                problems.append(f"{name}: 근거 없는 숫자 '{num}' 직접 기재 (문맥: …{ctx}…) "
                                f"→ facts 플레이스홀더로 바꾸거나 숫자 없이 표현")

    body = draft["body_md"]
    markers = MARKER.findall(body)
    for m in REQUIRED_MARKERS[kind]:
        n = markers.count(m)
        if n != 1:
            problems.append(f"body: 마커 [[{m}]] {n}회 (정확히 1회 필요)")

    prose = MARKER.sub("", PLACEHOLDER.sub("값", body))
    if len(prose) < cfg["quality"]["min_chars"]:
        problems.append(f"body: 분량 부족 ({len(prose)}자 < {cfg['quality']['min_chars']}자)")
    if len(re.findall(r"^##\s", body, flags=re.M)) < 3:
        problems.append("body: 소제목(##) 3개 미만")

    all_text = " ".join(texts.values())
    for mt in AREA_MISLABEL.finditer(all_text):
        problems.append(f"면적 해석 오류: '{mt.group()}' → 소형의 나머지는 '전용 60㎡ 초과'로 쓰세요 (중대형 아님)")
    for mt in KOREAN_NUMBER.finditer(all_text):
        problems.append(f"숫자 표기: '{mt.group()}' → 아라비아 숫자로 쓰세요 (예: 60㎡)")
    for mt in PRICE_SHRINK.finditer(all_text):
        problems.append(f"표현 오류: '{mt.group()}' → 가격은 '내렸다/낮아졌다', '줄었다'는 거래량에만")
    for mt in ALL_TIME.finditer(all_text):
        problems.append(f"근거 없는 표현: '{mt.group()}' → 신고가는 {{{{high_since}}}} 이후 기록 기준일 뿐입니다")
    for mt in MEDIAN_AS_MODE.finditer(all_text):
        problems.append(f"해석 오류: '{mt.group()}' → 중간값은 가격을 순서대로 늘어놓았을 때 가운데 값입니다 (가장 많이 거래된 가격대가 아님)")
    for mt in REPORT_LAG.finditer(all_text):
        problems.append(f"중복 안내: '{mt.group()}' → 신고 기한·시차·추가 신고 이야기는 하단 주석에 자동으로 붙으니 본문·FAQ에서 빼세요")
    for phrase in cfg["quality"]["banned_phrases"]:
        if phrase in all_text:
            problems.append(f"금지 표현 포함: '{phrase}'")

    if not (10 <= len(draft["title"]) <= 80):
        problems.append(f"title 길이 이상 ({len(draft['title'])}자)")
    if not (40 <= len(draft["description"]) <= 200):
        problems.append(f"description 길이 이상 ({len(draft['description'])}자)")
    if len(draft["faq"]) < 2:
        problems.append("faq 2개 미만")
    return problems


# 플레이스홀더 바로 뒤의 조사를 값의 받침에 맞게 고른다 (예: "404건였습니다" → "404건이었습니다")
# (받침 있을 때, 받침 없을 때). 긴 것부터 검사.
JOSA = [("이었", "였"), ("이라", "라"), ("이며", "며"), ("이나", "나"), ("으로", "로"),
        ("이", "가"), ("은", "는"), ("을", "를"), ("과", "와")]
_JOSA_RE = re.compile(
    r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}(" + "|".join(sorted({j for p in JOSA for j in p}, key=len, reverse=True))
    + r")(?:(?=는|도)|(?![가-힣]))|\{\{\s*([a-zA-Z0-9_]+)\s*\}\}(이었|였|으로|로)")
_DIGIT_JONG = {"0": 1, "1": 8, "2": 0, "3": 1, "4": 0, "5": 0, "6": 1, "7": 8, "8": 8, "9": 0}


def _jong(value: str) -> int:
    """마지막 글자의 받침: 0=없음, 8=ㄹ, 그 밖=있음."""
    # 끝의 괄호 설명은 읽지 않으므로 그 앞 글자로 판단한다 ("극동(105)" → "극동이")
    s = re.sub(r"\s*\([^()]*\)\s*$", "", value) or value
    s = re.sub(r"[\s)\]}'\".,·]+$", "", s)
    if not s:
        return 0
    c = s[-1]
    if "가" <= c <= "힣":
        return (ord(c) - 0xAC00) % 28
    if c.isdigit():
        return _DIGIT_JONG[c]
    if c.lower() == "l":
        return 8
    if c.lower() in "mn":
        return 1
    return 0


def _pick(josa: str, jong: int) -> str:
    for with_b, without_b in JOSA:
        if josa in (with_b, without_b):
            if with_b == "으로":  # ㄹ 받침은 "로"
                return "로" if jong in (0, 8) else "으로"
            return with_b if jong else without_b
    return josa


def fill(text: str, facts: dict) -> str:
    def josa(m):
        key = m.group(1) or m.group(3)
        val = str(facts[key])
        return val + _pick(m.group(2) or m.group(4), _jong(val))
    text = _JOSA_RE.sub(josa, text)
    text = PLACEHOLDER.sub(lambda m: str(facts[m.group(1)]), text)
    # 음수 변화율 뒤에 감소 표현이 오면 부호를 뺀다 ("-13.2% 낮아졌다" → "13.2% 낮아졌다")
    text = _DOUBLE_NEG.sub(r"\1 \2", text)
    # 같은 값을 두 번 나열한 표현을 줄인다 ("각각 6건, 6건으로" → "각각 6건으로")
    return _SAME_TWICE.sub(r"각각 \1", text)


_SAME_TWICE = re.compile(r"각각 (\d[\d,.]*[가-힣%]+), \1(?!\d)")
_DOUBLE_NEG = re.compile(r"-(\d+(?:\.\d+)?%) ?(낮|줄|떨어|내|하락|감소)")


def _deadline_note(facts: dict) -> str:
    """대상 월의 신고 기한(계약 후 30일)이 수집일 전에 끝났으면 '추가 신고' 대신 해제·정정만 언급한다."""
    import calendar
    from datetime import date, timedelta
    m = re.fullmatch(r"(\d{4})년 (\d{1,2})월", str(facts.get("month", "")))
    try:
        as_of = date.fromisoformat(str(facts.get("as_of", ""))[:10])
    except ValueError:
        as_of = None
    if m and as_of:
        y, mo = int(m.group(1)), int(m.group(2))
        if date(y, mo, calendar.monthrange(y, mo)[1]) + timedelta(days=30) < as_of:
            return "이후 거래 해제나 정정 신고로 수치가 일부 바뀔 수 있습니다."
    return "신고 기한(계약 후 30일) 내 추가 신고로 수치가 바뀔 수 있습니다."


def render(draft: dict, facts: dict, tables: dict, chart_url: str, chart_alt: str) -> dict:
    """플레이스홀더 치환 + 표/차트 삽입 + FAQ/출처 부착. 최종 마크다운을 반환."""
    body = fill(draft["body_md"], facts)

    def marker(m):
        tag = m.group(1)
        if tag == "CHART":
            return f"![{chart_alt}]({chart_url})"
        return "\n" + tables[tag.split(":", 1)[1]] + "\n"

    body = MARKER.sub(marker, body)
    faq = [{"question": fill(q["question"], facts), "answer": fill(q["answer"], facts)}
           for q in draft["faq"]]
    body += "\n\n## 자주 묻는 질문\n\n" + "\n\n".join(
        f"**Q. {q['question']}**\n\nA. {q['answer']}" for q in faq)
    body += (
        "\n\n---\n\n"
        f"*자료: 국토교통부 실거래가 공개시스템(공공데이터포털 API), {facts['as_of']} 수집 기준. "
        f"해제(취소)된 거래는 제외했으며, {_deadline_note(facts)} "
        + (f"신고가(기존 최고가를 넘어선 거래)는 {facts['high_since']} 이후 수집된 거래와 비교한 값입니다. "
           if facts.get("high_since") else "")
        + "이 글은 공개 데이터를 정리한 정보이며 투자 권유가 아닙니다.*"
    )
    return {
        "title": fill(draft["title"], facts),
        "description": fill(draft["description"], facts),
        "tags": [fill(t, facts) for t in draft["tags"]][:6],
        "body_md": body,
        "faq": faq,
    }
