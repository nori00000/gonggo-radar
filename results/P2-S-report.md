# P2-S 소스 정비 보고 (2026-09-15)

워크트리 `m1-pro:~/gonggo-w-p2s`, 브랜치 `feat/source-repair`, base `8ffbae6`.
파이프라인 실행·DB 쓰기·발송 없음. 라이브 GET 은 소스/게시판당 1회(목록만).

---

## §1 mois_sse — 비활성화 (마을기업 공고 게시판 부재)

**판정: 행정안전부에 마을기업 공고 게시판이 없다.** 추측 경로를 넣지 않고 껐다.

WebFetch 로 **열어서 확인한** 게시판 4개 (2026-09-15):

| bbsId | 실제 게시판 이름 | 목록 내용 | 마을기업 공고? |
|---|---|---|---|
| `BBSMSTR_000000000062` (기존 크롤러 경로) | 지방공기업제도·운영 | 2027년도 지방출자·출연기관 예산편성지침(2026.08.18) 외 | 없음 |
| `BBSMSTR_000000000058` (사회연대경제 > 민간협력) | 비영리민간단체지원 | 비영리민간단체 등록현황('26.3.31.기준)(2026.04.28) 외 | 없음 |
| `BBSMSTR_000000000006` (새소식 > 알립니다) | 부처 일반 공고 | 2026 건전노사관계 구축 유공 정부포상 추천 후보자 공개검증(2026.09.15) 외 | 없음 |
| `BBSMSTR_000000000600` (마을기업 페이지의 유일한 게시판 링크) | 월간 마을기업 웹진 | 9월호 수원 화성 특집(2026.08.31) 등 **총 6건** | 웹진이지 공고 아님 |

`https://www.mois.go.kr/frt/sub/a06/b06/village/screen.do`(사회연대경제 > 마을기업)
에 걸린 게시판 링크는 위 웹진 하나뿐이다. 마을기업 모집·지정 공고는 시·도가
내고 행안부는 지침만 낸다(예: 충남 공고는 bizinfo 에 뜬다).

- 조치: `alert/config.yaml` `crawler.sources.mois_sse.enabled: false` + 위 근거 주석.
- **`council_profile.sources` 에서는 빼지 않았다** — 프로파일 목록은 계약이 고정한다
  (`tests/test_council_profile.py:199` 가 `mois_sse` 포함을 단언). 비활성화는 크롤러만 끈다.
- 크롤러 코드(`alert/crawlers/mois_sse.py`)·`alert/digest/composer.py` 라벨은 그대로.
- 고정 테스트: `TestSourceRegistryContract::test_mois_sse_is_disabled`,
  `::test_mois_sse_stays_in_council_profile`

## §2-a smes — 비활성화 (SPA 이전, HTML 파싱 불가)

`https://www.smes.go.kr/main/sportsBsnsPolicy` → **302** →
`https://portal.smes.go.kr/home/sportsBsnsPolicy`.
이전지 응답 본문 실측 **555 바이트**, 내용은 `<div id="root"></div>` + Vite 번들
(`/home/assets/index-CylEB-wG.js`) 뿐이다. 목록이 서버 HTML 에 없다.
`portal.smes.go.kr/home/pblancCalendar` 도 동일하게 빈 셸.

- 조치: `crawler.sources.smes.enabled: false` + 사유 주석. 크롤러 코드는 보존.
- 공개 목록 API 경로를 **확인한 뒤에만** 재개한다(추측 경로 금지).
- 고정 테스트: `TestSourceRegistryContract::test_smes_is_disabled`

## §2-b epis — 경로 교체 (신규 사이트)

옛 경로 `/home/kor/M373320876/board.do` 는 사이트 개편으로 사라졌다. 새 경로를
열어 목록 내용을 확인하고 교체했다.

| 새 경로 | 게시판 | 확인한 첫 행 |
|---|---|---|
| `https://www.epis.or.kr/bbs/list.do?key=2604210075` | 공지사항 (482건) | `[2026년 단 한 번!] 고소득 귀농의 정석!…` 2026-09-08 |
| `https://www.epis.or.kr/bbs/list.do?key=2604210073` | 입찰/공모 | `2026년 하반기 발주사업 안내` |

신규 게시판은 `table.table_basics_area` 정적 HTML 이지만 제목 링크의 href 가
`javascript:void(0);` 이고 이동은 `onclick="goView('<pstSn>')"` → 폼이
`/bbs/view.do?key=…&pstSn=…` 로 GET 전송한다. 기존 3개 전략(table/list/generic
href)은 **전부 0건**이 되므로 전용 전략을 앞에 넣었다.

- `alert/crawlers/epis.py:EpisCrawler.BOARD_PATHS` — 새 두 경로로 교체
- `alert/crawlers/epis.py:EpisCrawler._parse_goview_board` — 신규(전략 0)
- `alert/crawlers/epis.py:EpisCrawler._extract_board_key` — 목록 URL → `key`
- `alert/crawlers/epis.py:EpisCrawler._row_period_text` — 기간 칸을 **근거로만** 읽음
- `alert/crawlers/epis.py:EpisCrawler._fetch_board_listing` — 전략 0 삽입
- `alert/crawlers/epis.py:EpisCrawler._extract_post_id` — `pstSn=` 패턴 추가
- 기존 `fetch()` 의 "첫 성공 게시판에서 break" 동작은 그대로 뒀다(기존 동작 보존).
  운영에서 실제로 도는 건 공지사항이고, 입찰/공모는 폴백이다.

**기간을 만들지 않게 한 지점**: epis 는 `PERIOD_EXTRACTORS` 에 없다. 전략 0은
등록일을 `item["date"]` 로 올리지 않고 `item["posted"]`(ISO)로만 싣는다 —
`_to_announcement` 가 `date` 로 `_parse_period` 를 돌리기 때문이다. 입찰/공모
게시판에는 등록일 칸 자체가 없고 기간 칸(`2026-01-01 ~ 2026-06-30`)만 있는데,
`td.date` 클래스로만 게시일을 인정해 그 기간이 게시일로 새지 않게 했다.

## §3 신규 HTML 소스 2개

| 소스 | 목록 URL (열어서 확인) | 파일 | 줄수 |
|---|---|---|---|
| `moel` 고용노동부 공지사항 | `https://www.moel.go.kr/news/notice/noticeList.do` (전체 7,893건) | `alert/crawlers/moel.py` | 199 |
| `mss` 중소벤처기업부 사업공고 | `https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=310` | `alert/crawlers/mss.py` | 200 |

둘 다 **기간 추출기 없음**(`period_start`/`period_end` 항상 `None`),
**`bypass_threshold` 없음**, `council_profile.sources` 등록, `fetch_detail: false`.

파싱 함정과 대응:

- **moel**: 같은 행의 첨부 전체받기 링크(`/common/downloadAllZip.do?bbs_seq=…`)도
  `bbs_seq` 를 쓴다 → 제목 링크를 `strong.b_tit a` 로 한정. 링크 텍스트에는
  `[공고]` 머리표가 붙고 `title` 속성에 머리표 없는 제목이 있다 → 제목은 속성,
  분류는 머리표. 칸은 `aria-label` 로 고른다(순서 무의존).
- **mss**: 행이 모바일용으로 한 번 더 그려진다(`td.mobile`) → `tr[onclick^=doBbsFView]`
  + `bc_idx` 중복 제거. 제목 칸에 **신청기간이 노출된다**
  (`2026-09-14 ~ 2026-10-13`) → 등록일은 `fullmatch` 하는 단일 날짜 칸만 인정하고
  신청기간은 `raw_data.apply_period_text` 근거로만 저장, 기간 필드는 만들지 않는다.

배선:
- `alert/crawlers/__init__.py` — `MoelCrawler`·`MssCrawler` import/`__all__`
- `alert/main.py:_import_crawlers` — 레지스트리 2행 추가 (36 → 38종)
- `alert/config.yaml` — `crawler.sources.moel`/`.mss`, `council_profile.sources` 에 `"moel"`·`"mss"`
- `resolve_source_kind` 결과: 둘 다 `gonggo` (실행 확인)

---

## 테스트

신규 파일 `tests/test_source_repair.py` (25개). 픽스처는 2026-09-15 라이브 목록에서
게시판 표만 잘라 저장했다 — 테스트는 네트워크를 타지 않는다.

- `tests/fixtures/moel_notice.html` (9,396 B)
- `tests/fixtures/mss_bsns_notice.html` (32,799 B — 숨김 `span.single-file` 제거)
- `tests/fixtures/epis_notice.html` (4,362 B)
- `tests/fixtures/epis_bid.html` (7,616 B)

| 클래스 | 테스트 |
|---|---|
| `TestMoelCrawler` | `test_parse_list`, `test_title_excludes_category_prefix`, `test_attachment_link_is_not_mistaken_for_a_post`, `test_to_announcement_builds_detail_url`, `test_posting_date_never_becomes_a_period`, `test_fetch_returns_empty_on_http_error` |
| `TestMssCrawler` | `test_parse_list`, `test_mobile_duplicate_markup_does_not_double_rows`, `test_to_announcement_builds_detail_url`, `test_apply_period_never_becomes_a_period`, `test_fetch_returns_empty_on_http_error` |
| `TestEpisBoardRepair` | `test_board_paths_point_at_the_new_site`, `test_parse_notice_board`, `test_parse_bid_board`, `test_bid_board_period_column_is_not_a_posting_date`, `test_goview_rows_never_produce_periods`, `test_source_id_is_the_pstSn`, `test_href_strategies_alone_find_nothing` |
| `TestSourceRegistryContract` | `test_mois_sse_is_disabled`, `test_mois_sse_stays_in_council_profile`, `test_smes_is_disabled`, `test_new_sources_registered`, `test_new_sources_have_no_bypass_threshold`, `test_new_sources_have_no_period_extractor`, `test_new_sources_are_importable_by_the_pipeline` |

수정한 기존 테스트 1건: `tests/test_source_health.py::TestProbeUrl::test_real_crawlers_resolve_to_a_url`
— 하드코딩 크롤러 수 `36` → `38` (moel·mss 추가분).

### pytest 요약

```
tests/test_source_repair.py:  25 passed in 0.72s
전체:                         1 failed, 2168 passed, 24 skipped, 1 warning in 20.82s
```

**유일한 실패는 base 에서 이미 깨져 있던 것이다** —
`tests/test_monthly_digest.py::test_monthly_plist_is_a_draft_and_not_loaded`:
base 커밋 `8ffbae6` 자체가 `launchd/com.gonggo-radar.monthly.plist` 를
`Minute: 0` → `30` 으로 바꾸면서 그 값을 단언하는 테스트를 안 고쳤다
(`git log -1 8ffbae6 -- launchd/com.gonggo-radar.monthly.plist` 확인).
P2-S 가 건드린 파일이 아니라 손대지 않았다 — **§V/§D 레인에 넘긴다.**

---

## §4 라이브 1회 파싱 (저장 없음)

각 소스 `fetch()` 1회. DB·notify·파이프라인 미접촉.

| 소스 | 건수 | 최신 게시일 | 게시일 확보 | period 생성 | 첫 항목 URL |
|---|---|---|---|---|---|
| moel | 10 | 2026-09-14 | 10/10 | **0** | `https://www.moel.go.kr/news/notice/noticeView.do?bbs_seq=20260900480` |
| mss | 10 | 2026-09-15 | 10/10 | **0** | `https://www.mss.go.kr/site/smba/ex/bbs/View.do?cbIdx=310&bcIdx=1071194&parentSeq=1071194` |
| epis | 10 | 2026-09-08 | 10/10 | **0** | `https://www.epis.or.kr/bbs/view.do?key=2604210075&pstSn=2609080002` |

(건수 10은 각 게시판의 페이지당 기본 노출 건수다 — moel 은 전체 7,893건 중 1페이지.)

비활성화 2종은 네트워크를 전혀 타지 않는다(실행 확인):

```
mois_sse enabled= False safe_fetch= [] requests= 0
smes     enabled= False safe_fetch= [] requests= 0
```

---

## 커밋

`4274e04f27a43a6f444d22ee29e683fa48185c05`
`feat(sources): P2-S 소스 정비 - mois_sse·smes 비활성화, epis 경로 교체, moel·mss 신규`
(브랜치 `feat/source-repair`, base `8ffbae6`)

위 커밋이 코드·테스트·픽스처 전부다. 이 보고서에 해시를 적는 커밋은 그 뒤에
따로 온다 - 같은 커밋 안에 자기 해시를 적을 수 없다.

## 남는 것 (이 레인 밖)

- `test_monthly_plist_is_a_draft_and_not_loaded` — base 유래 실패, 미수정.
- smes 재개는 portal.smes.go.kr 의 공개 목록 API 를 **확인한 뒤**에만.
- mois_sse 는 행안부가 마을기업 공고 게시판을 만들기 전에는 켤 근거가 없다.
  마을기업 공고 자체는 bizinfo(시·도 공고 집계)로 들어올 수 있다 — 별도 판단 필요.

---

# 라운드 2 — Codex 게이트 대응 (2026-09-15)

게이트 대상 `d852f27` (HIGH 1 + MEDIUM 2). 먼저 `git merge main --no-edit`
(main = `7484a0c`) — 충돌 없음(`alert/config.yaml` auto-merge), 병합 커밋
`149c31f`. main 이 plist-minute 테스트를 고쳐 라운드 1에서 남아 있던 실패
1건이 사라졌다(병합 직후 2199 passed).

## HIGH — epis 폴백 경로가 기간과 javascript URL 을 만들던 문제

**증상**: `_parse_goview_board` 의 정규식이 실패하면 `_fetch_board_listing`
이 옛 `_parse_table_board` 로 내려간다. 그 파서는 (a) 기간 칸
(`2026-09-07 ~ 2026-09-18`)을 `date` 로 올리고 `_to_announcement` 가
`_parse_period` 로 `period_start`/`period_end` 를 채웠으며, (b) href
`javascript:void(0);` 를 그대로 URL 로 써서 모든 행이
`https://www.epis.or.kr/javascript:void(0);` 가 되고 `_extract_post_id` 가
같은 해시를 내 **source_id 가 통째로 충돌**했다.

**수정** — `alert/crawlers/epis.py:EpisCrawler._to_announcement`:

1. 기간 두 필드를 **무조건 `None`** 으로 둔다. epis 는 `PERIOD_EXTRACTORS`
   에 없으므로 어떤 파싱 전략을 타든 기간을 만들 자격이 없다. 날짜 문자열은
   `raw_data` 근거로만 남는다(항목 딕셔너리가 그대로 직렬화된다).
   `_parse_period` 호출을 제거했다.
2. 원본 href 가 `javascript:` 로 시작하면 **goView id 를 먼저 건져 본다**.
   건지면 `/bbs/view.do?key=…&pstSn=…` 로 복구하고(전략 0이 실어 준
   `board_key` 사용), 못 건지면 `logger.warning` 한 줄 남기고 항목을 버린다.
   javascript URL 이 DB 에 닿는 경로가 없다.

전략 0(`_parse_goview_board`)은 항목에 `board_key` 를 함께 싣도록 했다.

**고정 테스트** (`tests/test_source_repair.py::TestEpisFallbackCannotFabricate`,
픽스처 `CODEX_FALLBACK_ROW` = goView onclick + 기간 칸 + `javascript:void(0);`
href, `GOVIEW_PATTERN` 을 절대 매치하지 않는 정규식으로 교체해 전략 0을 죽인다):

- `test_broken_goview_regex_falls_back_to_the_table_parser` — 전제 확인:
  전략 0이 0건이 되고 폴백 파서가 기간 칸·javascript href 를 올린다는 것
  (막는 자리가 변환 단계임을 드러낸다)
- `test_fallback_rows_never_produce_a_period`
- `test_fallback_rows_never_produce_a_javascript_url`
- `test_recoverable_javascript_href_becomes_a_goview_url`
- `test_fallback_ids_do_not_collide`

## MEDIUM 1 — epis `fetch` 의 조기 break

**증상**: 첫 게시판에 항목이 있으면 `break` 해서 입찰/공모 게시판이 한 번도
수집되지 않았다(라운드 1 보고서에서 "기존 동작 보존"이라 적었던 바로 그 지점).

**수정** — `alert/crawlers/epis.py:EpisCrawler.fetch`: `break` 를 없애고 두
게시판을 각각 수집한다. 같은 글이 겹쳐 뜨면 `source_id`(= pstSn)로 거른다.

**고정 테스트** (`TestEpisCollectsBothBoards`):
`test_both_boards_are_collected`(두 게시판 스텁 → 양쪽 항목 모두),
`test_same_post_on_both_boards_is_deduped`,
`test_empty_first_board_does_not_stop_the_second`.

## MEDIUM 2 — moel `_cell_text` 의 날짜 되짚기

**증상**: `aria-label` 이 없으면 행의 모든 `td` 를 훑어 "날짜처럼 보이는 첫
칸" 을 등록일로 썼다. 제목이 날짜 하나뿐인 공고(`"2026.01.02"`)가 있으면
**제목 칸이 게시일**이 된다.

**수정** — `alert/crawlers/moel.py`:

- `parse_list` 가 `header_labels(table)`(기존 `date_labels` 헬퍼)로
  `등록일`·`담당부서` **칸 번호**를 한 번 센다.
- `_cell_text(row, aria_label, columns, title_link)` 는 근거를 **둘로 한정**
  한다: `aria-label` 속성, 아니면 헤더에서 센 칸 번호. 어느 쪽도 없으면 빈
  문자열이다 — 날짜를 지어내지 않는다.
- 제목 링크가 든 칸은 두 경로 모두에서 **제외**한다.
- 폴백 스캔이 사라져 쓰이지 않게 된 `FULL_DATE_PATTERN` 상수를 지웠다.

**고정 테스트** (`TestMoelPostedComesFromThePostedColumn`, 픽스처
`CODEX_MOEL_DATE_TITLE_ROW` = 제목이 `2026.01.02`, 등록일 칸은 `2026.09.14`,
`aria-label` 없음):
`test_date_only_title_does_not_become_posted`,
`test_posted_column_is_found_by_header_index_without_aria`,
`test_without_a_posted_column_no_date_is_invented`.

## 기존 테스트 1건 수정

`tests/test_html_crawlers_batch_b.py::TestEpisCrawler::test_to_announcement_valid`
이 `period_start == "2026-04-01"`·`period_end == "2026-04-30"` 을 단언하고
있었다 — 게이트가 HIGH 로 잡은 바로 그 동작을 고정하던 테스트다. 두 필드가
`None` 이고 날짜 문자열이 `raw_data["date"]` 근거로 남는 것을 단언하도록 바꿨다.

## 회귀성 확인 (수정 전 실패)

수정한 두 파일만 `HEAD`(라운드 1) 판으로 되돌리고 새 테스트를 돌렸다:

```
8 failed, 28 passed
FAILED ...TestEpisFallbackCannotFabricate::test_fallback_rows_never_produce_a_period
FAILED ...TestEpisFallbackCannotFabricate::test_fallback_rows_never_produce_a_javascript_url
FAILED ...TestEpisFallbackCannotFabricate::test_recoverable_javascript_href_becomes_a_goview_url
FAILED ...TestEpisFallbackCannotFabricate::test_fallback_ids_do_not_collide
FAILED ...TestEpisCollectsBothBoards::test_both_boards_are_collected
FAILED ...TestMoelPostedComesFromThePostedColumn::test_date_only_title_does_not_become_posted
FAILED ...TestMoelPostedComesFromThePostedColumn::test_posted_column_is_found_by_header_index_without_aria
FAILED ...TestMoelPostedComesFromThePostedColumn::test_without_a_posted_column_no_date_is_invented
```

나머지 3건(전제 확인 1 + dedupe/빈-게시판 2)은 옛 코드에서도 통과한다 — 새
동작을 가르는 테스트가 아니라 경계를 고정하는 테스트라서다.

## pytest 요약 (라운드 2)

```
tests/test_source_repair.py:  36 passed in 0.88s   (25 -> 36, 신규 11)
전체:                         2210 passed, 24 skipped, 1 warning in 20.02s
```

라운드 1에서 남겼던 `test_monthly_plist_is_a_draft_and_not_loaded` 실패는
main 병합으로 해소됐다. **실패 0건.**

## 라이브 1회 파싱 (라운드 2, 저장 없음)

| 소스 | 건수 | 최신 게시일 | 게시일 확보 | period 생성 | javascript URL | id 중복 |
|---|---|---|---|---|---|---|
| moel | 10 | 2026-09-14 | 10 | **0** | **0** | **0** |
| mss | 10 | 2026-09-15 | 10 | **0** | **0** | **0** |
| epis | **24** | 2026-09-08 | 10 | **0** | **0** | **0** |

epis 가 10 → **24** 로 는 것이 MEDIUM 1 수정의 실표면 증거다(공지사항 10 +
입찰/공모 14). 게시일 확보가 10건뿐인 것은 정상이다 — 입찰/공모 게시판에는
등록일 칸 자체가 없고, 있는 것은 기간 칸뿐이라 게시일을 만들지 않는다.

줄수 상한(계약 §3 "각 ≤200줄") 유지: `moel.py` 200, `mss.py` 200.

## 라운드 2 커밋

`ROUND2_HASH`
