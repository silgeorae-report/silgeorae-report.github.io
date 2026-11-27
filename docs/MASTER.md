# 실거래가 리포트 — 마스터 문서

> 이 문서는 프로젝트의 **변하지 않는 기준**(목표·원칙·구조·계정·운영 방법)을 담는다.
> 진행 상황과 할 일은 [STATUS.md](STATUS.md)에 적는다. 구조나 계정·원칙이 바뀌면 이 문서를 같은 커밋에서 고친다.

## 1. 목표
- 국토교통부 아파트 매매 실거래가 공개 데이터로 서울 25개 구의 콘텐츠를 **사람 개입 없이** 매일 만들고, 올리고, 관리하고, 개선한다.
- 수익: 블로그 광고(애드센스, 글이 쌓인 뒤 신청), YouTube·Instagram·TikTok 채널 성장.

## 2. 운영 원칙 (사용자 지시)
| 원칙 | 내용 |
|---|---|
| 무료 | 유료 결제가 필요한 수단은 쓰지 않는다 (API 종량 과금, 유료 TTS, 도메인 구입 없음). Claude는 구독 토큰으로 호출 |
| 모델 | 기본 모델은 Opus (`claude-opus-5-5`) |
| 개인정보 | 개인 연락처·본명·구글 이메일이 들어간 아이디는 공개되면 안 된다. 계정 아이디는 그대로 두되, URL·설명 등 공개되는 곳에는 넣지 않는다 |
| 품질 | 완전 자동화이므로 품질은 Claude가 직접 보고 검토한다 (자동 검사 + Claude 2차 검수 + 정기 직접 검토) |
| 언어 | 사용자에게 하는 설명·보고는 한국어 |
| 문서 | 변경이 있을 때마다 MASTER.md / STATUS.md / 현황 아티팩트를 갱신한다 |
| 브라우저 | 웹 설정 작업은 필요하면 크롬(Claude in Chrome)을 직접 조작한다. 비밀번호·인증 코드·토큰 값은 Claude가 입력하거나 보지 않는다 |

## 3. 전체 구조
```
매일 01:00 KST  GitHub Actions (daily-pipeline)
  run       : 테스트 → 수집(data.go.kr) → 글 생성(Claude CLI) → 규칙 검사 → Claude 2차 검수 → 사이트 빌드
              → 숏츠 생성(Pillow+ffmpeg+TTS) → YouTube 업로드 → DB 커밋
  deploy    : GitHub Pages 배포 (public/, media/*.mp4 포함)
  instagram : 방금 배포된 mp4 URL로 릴스 게시 (Graph API, 시스템 사용자 토큰)
  report    : 오류가 있으면 실패 표시 → GitHub 이메일 알림
매일 08:10 KST  이 PC의 Claude 예약 작업 "틱톡 숏츠 자동 업로드"
  scripts/tiktok_prepare.py 로 대기열 준비 → 크롬(Claude in Chrome)으로 TikTok Studio 업로드 (하루 최대 2편)
  → TikTok 조회수를 읽어 data/tiktok_metrics.json 으로 저장소에 기록
매일 02:00 KST  이 PC의 Claude 예약 작업 "매일 운영 점검·품질 검토"
  실행 결과 점검 → 새 글·영상 직접 검토·수정 → 재발 방지 규칙 보강 → STATUS.md·현황 아티팩트 갱신
매주 월 02:30   이 PC의 Claude 예약 작업 "콘텐츠 고도화 탐색 (주간)"
  인기 Claude 스킬·모션 기법·Claude 활용법·성장 방법 탐색 → docs/RESEARCH.md 기록 → 대기열에서 1개 구현
```
- **PC 예약 작업 순서 잠금:** PC가 꺼져 있다 켜지면 밀린 예약 작업이 한꺼번에 시작된다. 세 작업은 같은 폴더를 고치므로
  `private\claude_ara.lock`(작업 폴더 안, git 제외. Claude 설정 폴더에 두면 승인 창이 떠 무인 실행이 멈춤) 파일로 한 번에 하나만 돈다 (최대 60분 대기, 90분 지난 잠금은 무시).
- **YouTube 하루 업로드 한도:** 채널의 하루 업로드 수를 넘으면 `uploadLimitExceeded`로 거부된다. 코드 문제가 아니며 다음 실행에서 다시 만들어 올린다.

### 스스로 나아지는 고리 (성과 → 개선 → 적용)
1. **실험**: 영상마다 실험 변수(테마 4종, 도입부 유형 3종, 배경 종류 사진·스톡 영상 — 실패 시 서울 공통 사진으로 대체, 모두 실패할 때만 배경 없음)를 가중치에 따라 고른다 (`learn.choose`, videos.variant에 기록)
2. **평가**: 매일 조회수·반응을 모으고(`metrics.py`), 게시 2일 뒤 채널별 중앙값 대비 상대 점수를 낸다 (`learn.score`)
3. **개선**: 변수 값별 평균 점수로 가중치를 고친다. 표본 3개 미만은 계속 시험. 주 1회 Claude가 상·하위 영상을 비교해 제목·도입부 교훈을 뽑는다 (`learn.update` → `data/playbook.json`)
4. **적용**: 다음 영상의 렌더 설정과 대본 지침에 가중치와 교훈이 들어간다 (`learn.guidance`)
5. **확장**: 주간 탐색 작업이 새 표현·형식을 찾아 구현하면 새 실험 변수나 새 콘텐츠 형식으로 고리에 들어온다

### 공개 자료 사용 기준
- 지도 경계: OpenStreetMap(ODbL 1.0) 서울 구 경계를 한 번 받아 `data/seoul_gu_osm.json`에 저장. 화면과 설명란에 "© OpenStreetMap contributors (ODbL)" 표기
- 출처: 위키미디어 공용, 한국관광공사 TourAPI 관광지 사진(공공누리 1유형만, 기존 DATA_GO_KR_KEY), Pixabay 영상·사진 (Pexels는 키 발급 재개 시). Pixabay 사진은 주소를 저장하지 않고 매번 검색·내려받기. 허용 라이선스: CC0, 퍼블릭 도메인, CC BY, 공공누리 1유형만. SA·NC·ND 제외
- 지도·로고·문서·인물 사진 제외, 후보 설명을 Claude가 읽고 선택. 쓴 자료는 `assets` 테이블에 기록하고 영상 화면·설명란에 출처 표기
- 외부 코드·스킬은 그대로 설치하지 않고 읽고 이해한 뒤 직접 구현
- DB: `data/pipeline.db` (SQLite, 저장소에 커밋). 테이블: trades, posts, videos, social_posts, runs, rejections 등
- 실행 요약: `data/last_run.json` (비밀값 자동 제거)

## 4. 코드 지도
| 파일 | 역할 |
|---|---|
| `config.toml` | 모든 설정 (사이트 주소, 수집 기간, 모델, 숏츠·YouTube·Instagram 옵션, 킬 스위치) |
| `pipeline/collect.py` | 실거래 API 수집. 오류 메시지에 URL·키를 남기지 않음 |
| `pipeline/analyze.py` | 구·월별 facts 계산 (median_ppp=3.3㎡당, top_apt_umd=최고가 단지 동, top_umd=거래 최다 동) |
| `pipeline/write.py` | Claude CLI 호출, 글쓰기 지침(facts 정의), 2차 검수 프롬프트 |
| `pipeline/qa.py` | 규칙 검사(근거 없는 숫자·금지어·마커), 플레이스홀더 채우기와 조사 자동 교정 |
| `pipeline/generate.py` | 글 계획·생성. 지적을 피드백으로 최대 3회 작성 |
| `pipeline/shorts.py`, `tts.py` | 숏츠 대본 검사·제작 흐름·음성(edge → gTTS) |
| `pipeline/motion.py` (+`render.py` 공용 글꼴·합치기) | 모션 인포그래픽 렌더러: 카운트업, 막대·선·순위 애니메이션, 구절 자막, 진행 막대, 테마 |
| `pipeline/timelapse3d.py` | 서울 25개 구 3D 타임랩스(구별 평당 중위가 12개월). 실제 구 경계(`data/seoul_gu_osm.json`, OpenStreetMap ODbL)를 높이만큼 세우고, 격자 칸 단위로 앞뒤를 정해 직접 등각 투영. `python -m pipeline.timelapse3d --out out/seoul_3d.mp4`. 매달 한 번 `produce_monthly`가 숏츠로 만들어 게시 흐름에 넣음 (`[shorts] timelapse_monthly`) |
| `pipeline/assets.py` | 공개 사진 수급과 라이선스·출처 기록 |
| `pipeline/metrics.py`, `learn.py` | 성과 수집, 실험 가중치·교훈 갱신 (`data/playbook.json`) |
| `docs/RESEARCH.md` | 주간 탐색 기록과 구현 대기열 |
| `pipeline/youtube.py` | YouTube Data API 업로드 (공개) |
| `pipeline/instagram.py` | 릴스 게시 (시스템 사용자 토큰=graph.facebook.com / IG 토큰=graph.instagram.com) |
| `pipeline/site.py` | 정적 사이트 빌드, sitemap, media 복사 (최근 `carry_media_days`일 영상은 배포된 사이트에서 받아 재배포 때도 유지) |
| `pipeline/run.py` | 명령 진입점 (`all`, `instagram` 등), 비밀값 마스킹 |
| `scripts/tiktok_prepare.py` | TikTok 업로드 대기열/기록 (`--done <slug>`) |
| `scripts/set_data_key.ps1`, `setup_instagram.ps1` | 클립보드의 비밀값을 화면에 표시하지 않고 GitHub Secrets에 등록 |
| `tests/test_pipeline.py` | 단위·통합 테스트 (실패하면 발행 안 함) |

## 5. 계정·서비스
> 이 저장소는 공개다. 계정 아이디·내부 ID는 로컬 전용 `private/ACCOUNTS.md`(git 제외)에만 적고, 비밀값은 어디에도 적지 않는다.

| 서비스 | 식별 정보 | 비고 |
|---|---|---|
| GitHub | 조직 `silgeorae-report`, 저장소 `silgeorae-report/silgeorae-report.github.io` (공개) | 개인 계정은 조직 비공개 멤버 |
| 블로그 | https://silgeorae-report.github.io/ | GitHub Pages (Actions 배포) |
| 공공데이터포털 | 아파트 매매 실거래가 상세 (Dev 엔드포인트) | Secret `DATA_GO_KR_KEY` (재발급본) |
| Claude | 구독 토큰 | Secret `CLAUDE_CODE_OAUTH_TOKEN` |
| YouTube | 채널 "실거래가 리포트" (Google Cloud OAuth 앱, 프로덕션 게시) | Secrets `YT_CLIENT_ID/SECRET/REFRESH_TOKEN`. 채널 휴대폰 인증 완료 |
| Instagram | 크리에이터 계정 | Meta 비즈니스 포트폴리오 "실거래가 리포트", 앱(개발 모드), 시스템 사용자, 만료 없는 토큰. Secrets `IG_ACCESS_TOKEN`, `IG_USER_ID` |
| TikTok | 표시 이름 "실거래가 리포트" | API 미사용 (미심사 앱은 비공개만 가능) → 크롬 예약 작업으로 업로드 |
| 저장소 변수 | `SITE_BASE_URL=https://silgeorae-report.github.io` | `PIPELINE_ENABLED=false` 로 두면 전체 중단 |

## 6. 운영 방법
- 수동 실행: GitHub → Actions → daily-pipeline → Run workflow (또는 `gh workflow run daily-pipeline --repo silgeorae-report/silgeorae-report.github.io`). `-f mode=build` 를 주면 사이트만 다시 빌드·배포(콘텐츠 생성·업로드·릴스 없음)
- 검색엔진 인증: Google 서치 콘솔(URL 접두어 속성, HTML 파일 인증), 네이버 서치어드바이저(HTML 파일 인증 — 크롬 확장으로는 접근 불가, 사용자가 직접 조작). 인증 파일명은 `config.toml` 의 `site_verification_files` — 빌드마다 배포되므로 지우지 말 것. 사이트맵 `sitemap.xml` 제출됨
- 긴급 중단: 저장소 변수 `PIPELINE_ENABLED=false` 또는 `config.toml`의 `[safety] enabled=false`
- 로컬 테스트: `.venv\Scripts\python.exe -m unittest discover -s tests`
- 비밀값 교체: 값을 Ctrl+C → `scripts\set_data_key.ps1` 또는 `scripts\setup_instagram.ps1 -IgUserId <ID>`
- 품질 검토(Claude): 실행 후 `data/last_run.json`의 published/rejected 확인, 새 글 본문 직접 읽기, 알려진 오류 패턴(㎡당·재건축·학군·소재지) 검색
- 커밋 규칙: `-c user.name=pipeline-setup -c user.email=pipeline-setup@users.noreply.github.com`, 데이터 커밋은 봇이 함

## 7. 하지 않기로 한 것
- TikTok Content Posting API: 미심사 앱은 비공개 게시만 가능하고 "자기 계정 업로드 도구"는 심사에서 거절되는 유형
- 유료 도메인·유료 TTS·Claude API 종량 과금
