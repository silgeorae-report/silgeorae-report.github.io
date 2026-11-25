"""Claude로 글 초안을 쓰고 2차 검수한다."""
import json
import os
import shutil
import subprocess
import tempfile
import time

ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "body_md": {"type": "string"},
        "faq": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                "required": ["question", "answer"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "description", "tags", "body_md", "faq"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "issues"],
    "additionalProperties": False,
}

SYSTEM = """당신은 한국 부동산 실거래 데이터를 해설하는 데이터 저널리스트입니다.
독자는 집을 사고팔거나 시세를 확인하려는 일반인입니다. 정확하고, 읽기 쉽고, 실제로 도움이 되는 글을 씁니다.

## 숫자 규칙 (가장 중요)
- 글에 들어가는 모든 수치·금액·비율·건수·날짜는 반드시 제공된 facts의 플레이스홀더 {{키}} 로만 씁니다.
  예: "이번 달 거래는 {{trade_count}}로 전월({{prev_count}}) 대비 {{count_change}} 변했습니다."
- facts에 없는 숫자를 직접 쓰거나 계산하거나 추정하지 마세요. 아라비아 숫자를 직접 타이핑하지 마세요.
- facts 값이 "비교 불가"나 "자료 없음"이면 해당 비교는 언급하지 않거나 "비교할 자료가 아직 없다"고만 씁니다.
- 제목(title)과 설명(description)에도 같은 규칙이 적용됩니다.
- 플레이스홀더 바로 뒤 조사(이/가, 은/는, 을/를, 와/과, 으로/로, 이었/였)는 시스템이 값에 맞게 자동 교정하므로 아무 쪽으로 써도 됩니다.

## 구성 규칙
- body_md는 마크다운입니다. ## 소제목을 3~5개 사용하고, 첫 문단은 핵심 요약입니다.
- 지정된 표 마커(예: [[TABLE:top10]])와 [[CHART]] 마커를 본문의 적절한 위치에 각각 정확히 한 번, 독립된 줄로 넣습니다.
- 표와 차트를 그대로 반복하지 말고, 그 안에서 읽어낼 수 있는 의미를 해설합니다.
- 본문 분량은 표를 제외하고 1,200~2,000자 정도.
- 출처·면책 문구는 시스템이 자동으로 붙이므로 쓰지 않습니다.

## 금지
- 매수·매도 권유, 가격 전망 단정("오를 것", "지금이 기회"), 과장·낚시성 표현.
- 데이터로 확인할 수 없는 원인 단정(정책·금리 등 외부 요인은 "~일 수 있다" 수준으로만, 또는 언급하지 않음).
- 특정 단지 비방.
- 중간값(중위가)을 '가장 많이 거래된 가격대'로 설명하는 것 (중간값은 가격을 순서대로 늘어놓았을 때 가운데 값).

## 기타
- title: 검색 의도를 반영한 자연스러운 제목, 60자 이내.
- description: 검색 결과에 보일 요약, 80~150자.
- tags: 3~6개.
- faq: 독자가 궁금해할 질문 3개와 답. 답에도 숫자 규칙 적용.
"""

SHORTS_SCENES = ["hook", "volume", "price", "top_deal", "busy", "outro"]

_scene = {
    "type": "object",
    "properties": {"headline": {"type": "string"}, "narration": {"type": "string"}},
    "required": ["headline", "narration"],
    "additionalProperties": False,
}
SHORTS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "scenes": {
            "type": "object",
            "properties": {k: _scene for k in SHORTS_SCENES},
            "required": SHORTS_SCENES,
            "additionalProperties": False,
        },
    },
    "required": ["title", "description", "hashtags", "scenes"],
    "additionalProperties": False,
}

SHORTS_SYSTEM = """당신은 부동산 데이터 숏폼(세로 영상, 약 40초) 대본 작가입니다.
화면에는 시스템이 숫자 카드와 차트를 그리고, 당신은 장면별 짧은 제목(headline)과 내레이션(narration)을 씁니다.

## 숫자 규칙 (가장 중요)
- 모든 수치·금액·비율·건수·날짜는 facts 플레이스홀더 {{키}} 로만 씁니다. 아라비아 숫자를 직접 쓰지 마세요.
- facts 값이 "비교 불가"/"자료 없음"이면 그 비교는 말하지 않습니다.

## 장면 (순서 고정)
- hook: 첫 2초에 시선을 끄는 질문이나 핵심 한 줄 (지역·월 언급)
- volume: 거래 건수와 전월 대비 변화
- price: 중위 거래가와 변화 (화면에 월별 추이 차트가 나옴)
- top_deal: 이번 달 최고가 거래 단지·면적·금액
- busy: 거래가 많았던 단지들 또는 동
- outro: "단지별 표는 블로그에서 확인하세요" 취지의 마무리 (구독 권유는 한 마디 이하)

## 형식
- narration: 장면당 한두 문장, 숫자를 채웠을 때 60자 안팎. 구어체, 또박또박 읽히는 문장.
- headline: 화면 상단 큰 글씨, 15자 이내.
- title: 유튜브 제목 60자 이내, 끝에 #Shorts 를 붙이지 마세요(시스템이 붙임).
- description: 2~3문장 요약. hashtags: 3~5개, # 없이. facts에 나오지 않는 단지명·동 이름은 해시태그에 넣지 마세요.
- 매수·매도 권유, 가격 전망 단정, 과장·낚시 표현 금지.
- 실거래는 이미 계약된 거래입니다. '매물'(팔려고 내놓은 집)·'호가'라고 부르지 말고 '거래'·'계약'이라고 쓰세요.
"""

KIND_GUIDE = {
    "gu_monthly": (
        "구 단위 월간 아파트 매매 실거래 리포트입니다. 사용할 마커: [[TABLE:top10]], [[CHART]] (월별 거래량·중위가 추이).\n"
        "facts 의미 (해석 주의):\n"
        "- new_high_count: 구 전체에서, 각 단지·전용면적의 기존 최고 거래가를 넘어선 거래 건수 (특정 단지의 건수가 아님)\n"
        "- high_since: 신고가 비교의 기준이 되는 수집 시작 월. 그 이전 거래는 비교에 들어가지 않으니 '역대'·'사상'이라는 말을 쓰지 말고('역대 최고' 한 번만 써도 반려), "
        "신고가 건수가 많다고 '과열'·'상승세'로 해석하지 마세요. 신고가를 처음 말할 때 '{{high_since}} 이후 기록 기준'임을 밝히세요.\n"
        "- 본문·FAQ에서 신고 기한(계약 후 30일)·신고 시차('계약 후 일정 기간 안에 신고', '아직 반영되지 않은 거래', '추가 신고')를 언급하지 마세요. "
        "표현을 바꿔도 같은 내용이면 반려됩니다. 수치 변동 가능성은 글 하단 주석에 자동으로 붙습니다.\n"
        "- 가격은 '올랐다/내렸다/낮아졌다'로 쓰고 '늘었다/줄었다'는 거래량에만 쓰세요.\n"
        "- small_share: 구 전체 거래 중 전용 60㎡ 이하 비중. 나머지는 '전용 60㎡ 초과'일 뿐이니 '중대형'·'중형 이상'이라 부르지 마세요 (60~85㎡는 중소형)\n"
        "- 본문 숫자는 아라비아 숫자로 쓰세요 ('육십 제곱미터'가 아니라 60㎡).\n"
        "- top_apt, top_area, top_floor, top_price, top_date, top_apt_umd: 이번 달 구 전체 최고가 거래 1건과 그 단지가 있는 동\n"
        "- top_umd, top_umd_count: 거래 건수가 가장 많은 동과 그 건수 (최고가 단지의 소재지가 아님! 최고가 단지 소재지는 top_apt_umd)\n"
        "- yoy_count: 전년 같은 달의 거래 건수 (이번 달 건수가 아님)\n"
        "- median_ppp: 전용면적 3.3㎡(1평)당 중위 가격 ('㎡당'이 아님)\n"
        "- 재건축 추진 여부, 브랜드, 학군 등 facts에 없는 단지 특성은 쓰지 마세요.\n"
        "- busy*_apt, busy*_count: 거래 건수 상위 단지와 그 단지의 건수\n"
        "- 단지명(apt)은 원자료 표기 그대로이며 쉼표가 들어 있어도 하나의 단지입니다. 쉼표로 나눠 두 단지처럼 쓰지 마세요.\n"
        "- facts에 없는 전월 단지별 수치·비중·원인은 추정하지 마세요. 신고가는 '기존 최고가를 넘어선 거래'입니다."
    ),
    "complex": (
        "개별 아파트 단지의 실거래가 정리 페이지입니다. 사용할 마커: [[TABLE:areas]], [[TABLE:recent]], [[CHART]] (거래 시점별 가격 분포).\n"
        "facts 의미 (해석 주의):\n"
        "- main_*: 거래가 가장 많은 면적(주력 면적) 하나의 값입니다. 다른 면적의 거래 건수는 facts에 없고 표에만 나옵니다. "
        "그러니 '주력 면적에 거래가 몰렸다', '나머지 면적은 몇 건 안 된다', '다른 면적은 거래가 적다'처럼 다른 면적의 건수를 짐작해 쓰지 마세요 "
        "(실제로는 둘째 면적도 비슷하게 많은 경우가 흔합니다). '면적별 건수는 표에서 확인하세요' 정도로만 쓰세요.\n"
        "- latest_date, main_latest_date, high_date 등 날짜는 모두 계약일입니다. '신고된 날'로 쓰지 마세요.\n"
        "- main_first, main_latest는 개별 거래 두 건이고, main_change는 그 둘의 차이일 뿐 단지 상승률이 아닙니다.\n"
        "- 개별 거래의 층·사정(저층, 가족 간 거래, 급매 등)을 짐작해 설명하지 마세요.\n"
        "- 실거래 자료에는 층 정보가 있습니다(최근 거래 표의 층 열). '층을 알 수 없다'고 쓰지 마세요. "
        "자료로 알 수 없는 것은 향·내부 상태·수리 여부 같은 조건입니다.\n"
        "- 계약된 거래를 '매물'이라 부르지 마세요 ('34㎡ 매물로 팔렸다' ✗ → '34㎡ 거래로, 금액은 ~' ○). 매물은 아직 팔리지 않은 집입니다.\n"
        "- 본문·FAQ에서 신고 기한, 신고 시차, '계약 후 일정 기간 안에 신고', '아직 반영되지 않은 거래', '추가 신고' 이야기를 하지 마세요. "
        "표현을 바꿔도 같은 내용이면 반려됩니다. 이 안내는 글 하단 주석에 자동으로 붙습니다."
    ),
}


class WriterError(RuntimeError):
    pass


def _user_prompt(kind: str, facts: dict) -> str:
    lines = [f"- {{{{{k}}}}} = {v}" for k, v in facts.items()]
    return (f"{KIND_GUIDE[kind]}\n\n사용 가능한 facts (플레이스홀더 = 현재 값):\n"
            + "\n".join(lines)
            + "\n\n위 규칙에 따라 글을 작성하세요.")


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise WriterError("응답에서 JSON을 찾지 못했습니다.")
    return json.loads(text[start:end + 1])


class ClaudeWriter:
    """Claude Code CLI(`claude -p`)를 호출한다.

    API 종량 과금 대신 Claude 구독(Pro/Max) 한도 안에서 동작한다.
    인증: 로컬은 로그인된 Claude Code, GitHub Actions는 `claude setup-token`으로 만든
    CLAUDE_CODE_OAUTH_TOKEN. 종량 과금을 막기 위해 ANTHROPIC_API_KEY는 자식 프로세스에서 제거한다.
    """

    def __init__(self, model: str, effort: str = "medium", timeout: int = 900):
        self.bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
        if not self.bin:
            raise WriterError("claude CLI를 찾을 수 없습니다 (npm i -g @anthropic-ai/claude-code 또는 CLAUDE_BIN).")
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "calls": 0}
        self.env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        self.workdir = tempfile.mkdtemp(prefix="writer-")  # 프로젝트 설정·CLAUDE.md를 읽지 않도록 빈 폴더에서 실행

    def _call(self, system: str, user: str, schema: dict, effort: str, max_tokens: int) -> dict:
        cmd = [self.bin, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--system-prompt", system, "--tools", "", "--strict-mcp-config",
               "--model", self.model, "--effort", effort, "--no-session-persistence"]
        last = ""
        for attempt in range(3):
            try:
                proc = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                                      timeout=self.timeout, env=self.env, cwd=self.workdir)
            except subprocess.TimeoutExpired:
                last = "시간 초과"
                continue
            try:
                out = json.loads(proc.stdout)
            except json.JSONDecodeError:
                last = (proc.stderr or proc.stdout)[-500:]
                time.sleep(20 * (attempt + 1))
                continue
            self.usage["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                self.usage[k] += (out.get("usage") or {}).get(k, 0) or 0
            if out.get("is_error"):
                last = str(out.get("result") or out.get("subtype"))[:500]
                low = last.lower()
                if "login" in low or "auth" in low or "token" in low:
                    raise WriterError(f"Claude 인증 실패(CLAUDE_CODE_OAUTH_TOKEN 확인): {last}")
                if "limit" in low:  # 구독 사용 한도 도달 → 재시도해도 소용없음, 다음 날 이어서 진행
                    raise WriterError(f"Claude 구독 사용 한도: {last}")
                time.sleep(20 * (attempt + 1))
                continue
            data = out.get("structured_output")
            if isinstance(data, dict):
                return data
            return _parse_json(out.get("result") or "")
        raise WriterError(f"claude CLI 호출 실패: {last}")

    def write(self, kind: str, facts: dict, feedback: list[str] | None = None) -> dict:
        user = _user_prompt(kind, facts)
        if feedback:
            user += ("\n\n이전 초안이 자동 검수에서 아래 이유로 반려되었습니다. 모두 고쳐서 다시 쓰세요:\n- "
                     + "\n- ".join(feedback))
        return self._call(SYSTEM, user, ARTICLE_SCHEMA, self.effort, 16000)

    def write_shorts(self, facts: dict, feedback: list[str] | None = None, guidance: str = "") -> dict:
        user = ("사용 가능한 facts (플레이스홀더 = 현재 값):\n"
                + "\n".join(f"- {{{{{k}}}}} = {v}" for k, v in facts.items())
                + "\n\n규칙에 따라 숏폼 대본을 작성하세요.")
        if guidance:
            user += "\n\n이번 영상의 추가 지침:\n" + guidance
        if feedback:
            user += "\n\n이전 대본이 아래 이유로 반려되었습니다. 모두 고치세요:\n- " + "\n- ".join(feedback)
        return self._call(SHORTS_SYSTEM, user, SHORTS_SCHEMA, self.effort, 8000)

    def review(self, rendered_md: str, facts: dict) -> dict:
        system = (
            "당신은 부동산 데이터 기사 편집장입니다. 발행 전 최종 검수를 합니다. "
            "다음 중 하나라도 있으면 approved=false 로 하고 issues에 구체적으로 적으세요: "
            "(1) facts와 모순되는 서술이나 수치 해석 오류(예: 감소를 증가로 표현), "
            "(2) 매수·매도 권유나 가격 전망 단정, (3) 근거 없는 원인 단정, "
            "(4) 문장이 깨지거나 같은 내용이 반복되어 읽기 어려움, (5) 사실과 다른 일반 상식, "
            "(6) 단위 오류(median_ppp는 3.3㎡=1평당 값이므로 '㎡당'으로 쓰면 오류), "
            "(7) 표·facts에 없는 단지 특성(재건축 추진 여부, 브랜드, 학군 등)을 근거로 한 단정, "
            "(8) 동·단지 소재지가 표와 다름, (9) 조사 오류. "
            "사소한 문체 취향은 문제로 삼지 마세요.\n\n[구 월간 리포트]\n" + KIND_GUIDE["gu_monthly"]
            + "\n\n[단지 정리 페이지]\n" + KIND_GUIDE["complex"]
        )
        user = ("facts:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
                + "\n\n검수할 글:\n" + rendered_md)
        return self._call(system, user, REVIEW_SCHEMA, "medium", 4000)


class FakeWriter:
    """API 키 없이 파이프라인 전체를 시험하기 위한 가짜 작성기."""

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

    def write(self, kind: str, facts: dict, feedback: list[str] | None = None) -> dict:
        if kind == "gu_monthly":
            body = (
                "{{month}} {{region}} 아파트 매매는 {{trade_count}}가 신고되었습니다. "
                "중위 거래가는 {{median_price}}이며 전월 대비 {{median_change}} 변동했습니다.\n\n"
                "## 거래량과 가격 흐름\n\n"
                + "이 구간은 시험용 문장입니다. 거래량과 중위 가격의 움직임을 함께 살펴봅니다. " * 12
                + "\n\n[[CHART]]\n\n## 고가 거래 상위 단지\n\n"
                "가장 비싸게 거래된 곳은 {{top_apt}} {{top_area}}로 {{top_price}}였습니다.\n\n[[TABLE:top10]]\n\n"
                "## 지역별 특징\n\n"
                + "거래가 가장 많았던 동은 {{top_umd}}({{top_umd_count}})입니다. 시험용 문장을 이어 씁니다. " * 10
            )
            title = "{{month}} {{region}} 아파트 실거래 리포트: 거래 {{trade_count}}"
        else:
            body = (
                "{{apt}}({{region}} {{umd}})의 실거래는 {{trade_count}}가 확인됩니다.\n\n"
                "## 주력 평형 시세\n\n"
                + "가장 많이 거래된 {{main_area}}의 중위 거래가는 {{main_median}}입니다. 시험용 문장입니다. " * 10
                + "\n\n[[TABLE:areas]]\n\n## 가격 흐름\n\n[[CHART]]\n\n"
                + "최근 거래는 {{latest_date}} {{latest_price}}입니다. 시험용 문장을 조금 더 길게 이어 씁니다. " * 14
                + "\n\n## 최근 거래 내역\n\n[[TABLE:recent]]\n"
            )
            title = "{{apt}} 실거래가 정리 ({{region}} {{umd}})"
        return {
            "title": title,
            "description": "{{region}} 아파트 실거래 데이터를 국토교통부 공개 자료로 정리했습니다. 거래 건수, 중위 가격, 최고가 거래를 한눈에 확인하세요.",
            "tags": ["실거래가", facts.get("region", "서울")],
            "body_md": body,
            "faq": [{"question": "자료 출처는 어디인가요?",
                     "answer": "국토교통부 실거래가 공개 자료이며 {{as_of}} 기준입니다."}] * 3,
        }

    def write_shorts(self, facts: dict, feedback: list[str] | None = None, guidance: str = "") -> dict:
        s = lambda h, n: {"headline": h, "narration": n}  # noqa: E731
        return {
            "title": "{{month}} {{region}} 아파트 거래 {{trade_count}}, 최고가는?",
            "description": "{{region}} 아파트 실거래를 정리했습니다. 단지별 표는 블로그에서 확인하세요.",
            "hashtags": ["실거래가", "부동산", "{{region}}"],
            "scenes": {
                "hook": s("{{region}} 실거래", "{{month}} {{region}} 아파트, 얼마에 거래됐을까요?"),
                "volume": s("거래량", "거래는 {{trade_count}}로 전월보다 {{count_change}} 변했습니다."),
                "price": s("중위 거래가", "중위 거래가는 {{median_price}}, 전월 대비 {{median_change}}입니다."),
                "top_deal": s("최고가 거래", "최고가는 {{top_apt}} {{top_area}}, {{top_price}}였습니다."),
                "busy": s("거래 많은 단지", "거래가 가장 많은 단지는 {{busy1_apt}}였습니다."),
                "outro": s("자세한 표는 블로그", "단지별 상세 표는 블로그에서 확인하세요."),
            },
        }

    def review(self, rendered_md: str, facts: dict) -> dict:
        return {"approved": True, "issues": []}
