"""P2-S 계약: 소스 정비 — 신규 HTML 소스 2개 + epis 경로 교체 + 비활성화 근거.

네트워크를 타지 않는다: 모든 파싱은 ``tests/fixtures`` 의 저장 픽스처를 읽고,
설정은 운영 ``alert/config.yaml`` 을 그대로 읽는다.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from alert.config import get_config
from alert.crawlers.epis import EpisCrawler
from alert.crawlers.moel import MoelCrawler
from alert.crawlers.mss import MssCrawler
from alert.crawlers.period_extractors import PERIOD_EXTRACTORS
from alert.main import _import_crawlers

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - 운영 의존성
    BeautifulSoup = None

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> "BeautifulSoup":
    return BeautifulSoup((FIXTURES / name).read_text(encoding="utf-8"), "html.parser")


def make_config(source_name: str, base_url: str):
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 3
    config.crawler.retry_delay = 1.0
    config.crawler.user_agent = "test-agent"

    source = MagicMock()
    source.enabled = True
    source.base_url = base_url
    source.fetch_detail = False
    config.crawler.sources = {source_name: source}
    return config


# ---------------------------------------------------------------------------
# §3 고용노동부 공지사항 (moel)
# ---------------------------------------------------------------------------

class TestMoelCrawler:
    """고용노동부 공지사항 목록 파서 (픽스처: moel_notice.html)."""

    @pytest.fixture
    def crawler(self):
        config = make_config("moel", "https://www.moel.go.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield MoelCrawler()

    def test_parse_list(self, crawler):
        items = crawler.parse_list(load_fixture("moel_notice.html"))

        assert len(items) >= 10
        first = items[0]
        assert first["bbs_seq"] == "20260900480"
        assert first["title"] == (
            "2026년도 하반기 퇴직공무원(일반) 정부포상 추천 후보자 사전공개"
        )
        assert first["category"] == "포상대상자공개"
        assert first["author"] == "인사계"
        assert first["date"] == "2026.09.14"

    def test_title_excludes_category_prefix(self, crawler):
        """``[공고] 제목`` 의 머리표는 제목에 들어가지 않는다."""
        items = crawler.parse_list(load_fixture("moel_notice.html"))
        categorized = [i for i in items if i["category"]]
        assert categorized
        for item in categorized:
            assert not item["title"].startswith("[")
            assert f"[{item['category']}]" not in item["title"]

    def test_attachment_link_is_not_mistaken_for_a_post(self, crawler):
        """첨부 전체받기 링크도 ``bbs_seq`` 를 쓰지만 공고가 되면 안 된다."""
        items = crawler.parse_list(load_fixture("moel_notice.html"))
        ids = [i["bbs_seq"] for i in items]
        assert len(ids) == len(set(ids))
        assert all(i["title"] for i in items)

    def test_to_announcement_builds_detail_url(self, crawler):
        items = crawler.parse_list(load_fixture("moel_notice.html"))
        ann = crawler._to_announcement(items[0], "https://www.moel.go.kr")

        assert ann is not None
        assert ann.source == "moel"
        assert ann.source_id == "20260900480"
        assert ann.url == (
            "https://www.moel.go.kr/news/notice/noticeView.do?bbs_seq=20260900480"
        )
        assert ann.author == "인사계"

    def test_posting_date_never_becomes_a_period(self, crawler):
        """등록일은 raw_data.posted 로만 남고 기간 필드를 만들지 않는다."""
        items = crawler.parse_list(load_fixture("moel_notice.html"))
        announcements = [
            crawler._to_announcement(item, "https://www.moel.go.kr")
            for item in items
        ]
        assert announcements
        assert all(a.period_start is None for a in announcements)
        assert all(a.period_end is None for a in announcements)
        posted = [json.loads(a.raw_data).get("posted") for a in announcements]
        assert "2026-09-14" in posted

    def test_fetch_returns_empty_on_http_error(self, crawler):
        with patch.object(crawler, "get", return_value=None):
            assert crawler.fetch() == []


# ---------------------------------------------------------------------------
# §3 중소벤처기업부 사업공고 (mss)
# ---------------------------------------------------------------------------

class TestMssCrawler:
    """중소벤처기업부 사업공고 목록 파서 (픽스처: mss_bsns_notice.html)."""

    @pytest.fixture
    def crawler(self):
        config = make_config("mss", "https://www.mss.go.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield MssCrawler()

    def test_parse_list(self, crawler):
        items = crawler.parse_list(load_fixture("mss_bsns_notice.html"))

        assert len(items) == 10
        second = items[1]
        assert second["bc_idx"] == "1071161"
        assert second["parent_seq"] == "1071161"
        assert second["title"] == "「2026년 상생결제 확산 유공」 포상 후보자 모집 공고"
        assert second["author"] == "상생협력지원과"
        assert second["notice_no"] == "제2026-544호"
        assert second["date"] == "2026.09.14"

    def test_mobile_duplicate_markup_does_not_double_rows(self, crawler):
        """행이 모바일용으로 한 번 더 그려져도 공고는 한 번만 잡힌다."""
        items = crawler.parse_list(load_fixture("mss_bsns_notice.html"))
        ids = [i["bc_idx"] for i in items]
        assert len(ids) == len(set(ids))

    def test_to_announcement_builds_detail_url(self, crawler):
        items = crawler.parse_list(load_fixture("mss_bsns_notice.html"))
        ann = crawler._to_announcement(items[1], "https://www.mss.go.kr")

        assert ann is not None
        assert ann.source == "mss"
        assert ann.source_id == "1071161"
        assert ann.url == (
            "https://www.mss.go.kr/site/smba/ex/bbs/View.do"
            "?cbIdx=310&bcIdx=1071161&parentSeq=1071161"
        )

    def test_apply_period_never_becomes_a_period(self, crawler):
        """목록에 신청기간이 보여도 기간 필드는 만들지 않는다 (근거만 저장).

        회귀: mss 목록은 ``신청기간 2026-09-14 ~ 2026-10-13`` 을 그대로 보여
        준다. 전용 추출기가 없는 소스가 이 값을 period_end 로 쓰면 초크포인트가
        지우기 전까지 근거 없는 마감이 생긴다.
        """
        items = crawler.parse_list(load_fixture("mss_bsns_notice.html"))
        announcements = [
            crawler._to_announcement(item, "https://www.mss.go.kr")
            for item in items
        ]
        assert announcements
        assert all(a.period_start is None for a in announcements)
        assert all(a.period_end is None for a in announcements)

        payloads = [json.loads(a.raw_data) for a in announcements]
        with_period = [p for p in payloads if p.get("apply_period_text")]
        assert with_period, "신청기간 근거가 최소 한 건은 보존돼야 한다"
        assert "2026-09-14 ~ 2026-10-13" in [
            p["apply_period_text"] for p in with_period
        ]
        # 신청기간이 등록일(posted)로 새지 않았다
        assert all("~" not in (p.get("posted") or "") for p in payloads)
        assert "2026-09-14" in [p.get("posted") for p in payloads]

    def test_fetch_returns_empty_on_http_error(self, crawler):
        with patch.object(crawler, "get", return_value=None):
            assert crawler.fetch() == []


# ---------------------------------------------------------------------------
# §2 epis 경로 교체
# ---------------------------------------------------------------------------

class TestEpisBoardRepair:
    """개편된 epis 게시판(goView) 파서."""

    @pytest.fixture
    def crawler(self):
        config = make_config("epis", "https://www.epis.or.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield EpisCrawler()

    def test_board_paths_point_at_the_new_site(self, crawler):
        assert crawler.BOARD_PATHS == [
            "/bbs/list.do?key=2604210075",
            "/bbs/list.do?key=2604210073",
        ]
        assert not any("M373320876" in p for p in crawler.BOARD_PATHS)

    def test_parse_notice_board(self, crawler):
        items = crawler._parse_goview_board(
            load_fixture("epis_notice.html"), "2604210075"
        )

        assert len(items) >= 10
        first = items[0]
        assert first["link"] == "/bbs/view.do?key=2604210075&pstSn=2609080002"
        assert "딸기 스마트팜" in first["title"]
        assert first["posted"] == "2026-09-08"

    def test_parse_bid_board(self, crawler):
        items = crawler._parse_goview_board(
            load_fixture("epis_bid.html"), "2604210073"
        )

        assert len(items) >= 10
        titles = [i["title"] for i in items]
        assert "2026년 하반기 발주사업 안내" in titles
        assert any(i["category"] in {"입찰", "공모"} for i in items)

    def test_bid_board_period_column_is_not_a_posting_date(self, crawler):
        """입찰/공모 게시판에는 등록일 칸이 없다 - 기간 칸을 게시일로 쓰지 않는다.

        회귀: 칸 순서나 "날짜처럼 보이는 첫 칸" 으로 읽으면
        ``2026-01-01 ~ 2026-06-30`` 의 시작일이 게시일이 된다.
        """
        items = crawler._parse_goview_board(
            load_fixture("epis_bid.html"), "2604210073"
        )
        assert items
        assert all("posted" not in i for i in items)
        period_texts = [i.get("period_text", "") for i in items]
        assert "2026-01-01 ~ 2026-06-30" in period_texts

    def test_goview_rows_never_produce_periods(self, crawler):
        """epis 는 전용 추출기가 없다 - 두 게시판 모두 기간 필드가 None 이다."""
        assert "epis" not in PERIOD_EXTRACTORS
        for fixture, key in (
            ("epis_notice.html", "2604210075"),
            ("epis_bid.html", "2604210073"),
        ):
            items = crawler._parse_goview_board(load_fixture(fixture), key)
            announcements = [
                crawler._to_announcement(i, "https://www.epis.or.kr") for i in items
            ]
            assert announcements
            assert all(a.period_start is None for a in announcements)
            assert all(a.period_end is None for a in announcements)

    def test_source_id_is_the_pstSn(self, crawler):
        items = crawler._parse_goview_board(
            load_fixture("epis_notice.html"), "2604210075"
        )
        ann = crawler._to_announcement(items[0], "https://www.epis.or.kr")
        assert ann is not None
        assert ann.source_id == "2609080002"
        assert ann.url == (
            "https://www.epis.or.kr/bbs/view.do?key=2604210075&pstSn=2609080002"
        )

    def test_href_strategies_alone_find_nothing(self, crawler):
        """href 가 javascript:void(0) 라 옛 전략들은 게시글을 못 찾는다.

        전략 0(goView)이 사라지면 조용히 0건이 되는 것을 고정한다.
        """
        soup = load_fixture("epis_notice.html")
        assert crawler._parse_generic_links(soup) == []


# ---------------------------------------------------------------------------
# §1·§2 비활성화 + §3 등록 (운영 설정 정본)
# ---------------------------------------------------------------------------

class TestSourceRegistryContract:
    """운영 config.yaml 이 계약대로 정비됐는지."""

    @pytest.fixture
    def config(self):
        return get_config(reload=True)

    def test_mois_sse_is_disabled(self, config):
        """행안부 마을기업 공고 게시판이 없어 비활성화 (P2-S §1)."""
        assert config.crawler.sources["mois_sse"].enabled is False

    def test_mois_sse_stays_in_council_profile(self, config):
        """비활성화는 크롤러만 끈다 - 프로파일 목록은 계약이 고정한다."""
        assert "mois_sse" in config.council_profile.sources

    def test_smes_is_disabled(self, config):
        """portal.smes.go.kr 이 SPA 라 HTML 파싱 불가 (P2-S §2)."""
        assert config.crawler.sources["smes"].enabled is False

    def test_new_sources_registered(self, config):
        for name in ("moel", "mss"):
            source = config.crawler.sources[name]
            assert source.enabled is True
            assert name in config.council_profile.sources

    def test_new_sources_have_no_bypass_threshold(self, config):
        """계약 §3: bypass 없음 - 임계값을 정상으로 통과해야 적재된다."""
        for name in ("moel", "mss"):
            assert config.crawler.sources[name].bypass_threshold is False

    def test_new_sources_have_no_period_extractor(self, config):
        """계약 §3: 기간 추출 없음 - 초크포인트가 period 를 항상 None 으로 둔다."""
        assert "moel" not in PERIOD_EXTRACTORS
        assert "mss" not in PERIOD_EXTRACTORS

    def test_new_sources_are_importable_by_the_pipeline(self):
        crawlers = _import_crawlers()
        assert crawlers["moel"] is MoelCrawler
        assert crawlers["mss"] is MssCrawler


# ---------------------------------------------------------------------------
# 라운드 2 — Codex 게이트 회귀 (HIGH 1 + MEDIUM 2)
# ---------------------------------------------------------------------------

# goView 정규식이 실패했을 때 폴백 표 파서가 보는 행. 제목 href 는
# javascript:void(0); 이고 기간 칸(td.date 가 아닌 무클래스 칸)이 있다.
CODEX_FALLBACK_ROW = """
<html><body>
<table class="board_list">
  <thead>
    <tr><th>No</th><th>구분</th><th>제목</th><th>파일</th><th>기간</th><th>상태</th></tr>
  </thead>
  <tbody>
    <tr>
      <td class="num">3</td>
      <td class="sort"><a href="javascript:void(0);"
          onclick="goView('2609080002');">입찰</a></td>
      <td class="tit"><a href="javascript:void(0);"
          onclick="goView('2609080002');">계절근로 제도 운영실태 조사분석</a></td>
      <td class="file">-</td>
      <td>2026-09-07 ~ 2026-09-18</td>
      <td class="state">진행중</td>
    </tr>
    <tr>
      <td class="num">2</td>
      <td class="sort"><a href="javascript:void(0);"
          onclick="goView('2609080001');">공모</a></td>
      <td class="tit"><a href="javascript:void(0);"
          onclick="goView('2609080001');">청년농 교육과정 개선 연구용역</a></td>
      <td class="file">-</td>
      <td>2026-09-01 ~ 2026-09-30</td>
      <td class="state">진행중</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

NEVER_MATCHES = re.compile(r"(?!x)x__goview_disabled__")


class TestEpisFallbackCannotFabricate:
    """라운드 2 HIGH: goView 전략이 실패해도 기간·javascript URL 이 새지 않는다."""

    @pytest.fixture
    def crawler(self):
        config = make_config("epis", "https://www.epis.or.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield EpisCrawler()

    def test_broken_goview_regex_falls_back_to_the_table_parser(self, crawler):
        """전제 확인: 정규식이 죽으면 전략 0은 0건, 폴백 표 파서가 행을 준다."""
        soup = BeautifulSoup(CODEX_FALLBACK_ROW, "html.parser")
        with patch.object(EpisCrawler, "GOVIEW_PATTERN", NEVER_MATCHES):
            assert crawler._parse_goview_board(soup, "2604210073") == []
            fallback = crawler._parse_table_board(soup)
        assert len(fallback) == 2
        # 폴백 파서 자체는 여전히 기간 칸과 javascript href 를 올린다 -
        # 막는 자리는 변환 단계다 (아래 두 테스트).
        assert fallback[0]["link"].startswith("javascript:")
        assert "~" in fallback[0]["date"]

    def test_fallback_rows_never_produce_a_period(self, crawler):
        """기간 칸이 있어도 period_start/period_end 는 무조건 None 이다."""
        soup = BeautifulSoup(CODEX_FALLBACK_ROW, "html.parser")
        with patch.object(EpisCrawler, "GOVIEW_PATTERN", NEVER_MATCHES):
            items = crawler._parse_table_board(soup)
            announcements = [
                crawler._to_announcement(i, "https://www.epis.or.kr") for i in items
            ]

        kept = [a for a in announcements if a is not None]
        assert all(a.period_start is None for a in kept)
        assert all(a.period_end is None for a in kept)

    def test_fallback_rows_never_produce_a_javascript_url(self, crawler):
        """goView id 를 못 건진 javascript: 행은 버린다 - URL 이 되지 않는다.

        회귀: ``javascript:void(0);`` 를 URL 로 쓰면 모든 행이
        ``…/javascript:void(0);`` 가 되고 source_id 가 통째로 충돌한다.
        """
        soup = BeautifulSoup(CODEX_FALLBACK_ROW, "html.parser")
        with patch.object(EpisCrawler, "GOVIEW_PATTERN", NEVER_MATCHES):
            items = crawler._parse_table_board(soup)
            announcements = [
                crawler._to_announcement(i, "https://www.epis.or.kr") for i in items
            ]

        # id 를 못 건졌으므로 전부 버려진다
        assert announcements == [None, None]
        kept = [a for a in announcements if a is not None]
        assert all("javascript:" not in a.url for a in kept)

    def test_recoverable_javascript_href_becomes_a_goview_url(self, crawler):
        """href 자체에 goView id 가 있으면 버리지 않고 본문 URL 로 복구한다."""
        item = {
            "title": "계절근로 제도 운영실태 조사분석",
            "link": "javascript:goView('2609080002');",
            "board_key": "2604210073",
            "date": "2026-09-07 ~ 2026-09-18",
        }
        ann = crawler._to_announcement(item, "https://www.epis.or.kr")

        assert ann is not None
        assert "javascript:" not in ann.url
        assert ann.url == (
            "https://www.epis.or.kr/bbs/view.do?key=2604210073&pstSn=2609080002"
        )
        assert ann.source_id == "2609080002"
        assert ann.period_start is None and ann.period_end is None
        # 기간 문자열은 근거로만 남는다
        assert json.loads(ann.raw_data)["date"] == "2026-09-07 ~ 2026-09-18"

    def test_fallback_ids_do_not_collide(self, crawler):
        """버리지 않고 복구되는 경우에도 행마다 다른 source_id 가 나온다."""
        items = [
            {"title": "가", "link": "javascript:goView('111');"},
            {"title": "나", "link": "javascript:goView('222');"},
        ]
        ids = [
            crawler._to_announcement(i, "https://www.epis.or.kr").source_id
            for i in items
        ]
        assert ids == ["111", "222"]


class TestEpisCollectsBothBoards:
    """라운드 2 MEDIUM: 첫 게시판에서 break 하지 않는다."""

    @pytest.fixture
    def crawler(self):
        config = make_config("epis", "https://www.epis.or.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield EpisCrawler()

    def test_both_boards_are_collected(self, crawler):
        """공지사항·입찰공모 두 게시판의 항목이 모두 들어온다."""
        boards = {
            "/bbs/list.do?key=2604210075": [
                {"title": "공지 1", "link": "/bbs/view.do?key=2604210075&pstSn=1001"},
                {"title": "공지 2", "link": "/bbs/view.do?key=2604210075&pstSn=1002"},
            ],
            "/bbs/list.do?key=2604210073": [
                {"title": "입찰 1", "link": "/bbs/view.do?key=2604210073&pstSn=2001"},
            ],
        }

        def fake_listing(url):
            for path, items in boards.items():
                if url.endswith(path):
                    return list(items)
            return []

        with patch.object(crawler, "_fetch_board_listing", side_effect=fake_listing):
            announcements = crawler.fetch()

        assert [a.title for a in announcements] == ["공지 1", "공지 2", "입찰 1"]
        assert [a.source_id for a in announcements] == ["1001", "1002", "2001"]

    def test_same_post_on_both_boards_is_deduped(self, crawler):
        """같은 pstSn 이 두 게시판에 겹쳐 뜨면 한 번만 싣는다."""
        def fake_listing(url):
            return [{"title": "겹친 글", "link": f"/bbs/view.do?pstSn=9001"}]

        with patch.object(crawler, "_fetch_board_listing", side_effect=fake_listing):
            announcements = crawler.fetch()

        assert len(announcements) == 1
        assert announcements[0].source_id == "9001"

    def test_empty_first_board_does_not_stop_the_second(self, crawler):
        """첫 게시판이 0건이어도 두 번째 게시판을 계속 본다."""
        def fake_listing(url):
            if url.endswith("key=2604210075"):
                return []
            return [{"title": "입찰 1", "link": "/bbs/view.do?pstSn=2001"}]

        with patch.object(crawler, "_fetch_board_listing", side_effect=fake_listing):
            announcements = crawler.fetch()

        assert [a.title for a in announcements] == ["입찰 1"]


# 제목이 날짜 하나뿐인 공고. "날짜처럼 보이는 첫 칸" 규칙이면 제목이 게시일이 된다.
CODEX_MOEL_DATE_TITLE_ROW = """
<html><body>
<table class="tstyle_list">
  <thead>
    <tr><th scope="col">번호</th><th scope="col">제목</th>
        <th scope="col">담당부서</th><th scope="col">첨부</th>
        <th scope="col">등록일</th><th scope="col">조회</th></tr>
  </thead>
  <tbody>
    <tr>
      <td class="m_hidden">7894</td>
      <td class="txt_left">
        <strong class="b_tit">
          <a href="/news/notice/noticeView.do?bbs_seq=20260900999"
             title="2026.01.02">2026.01.02</a>
        </strong>
      </td>
      <td><span class="ellipsis">기획재정담당관</span></td>
      <td>-</td>
      <td>2026.09.14</td>
      <td class="txt_right">1</td>
    </tr>
  </tbody>
</table>
</body></html>
"""


class TestMoelPostedComesFromThePostedColumn:
    """라운드 2 MEDIUM: 제목 칸은 게시일 후보가 아니다."""

    @pytest.fixture
    def crawler(self):
        config = make_config("moel", "https://www.moel.go.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield MoelCrawler()

    def test_date_only_title_does_not_become_posted(self, crawler):
        """제목이 ``2026.01.02`` 뿐이어도 게시일은 등록일 칸(2026.09.14)이다."""
        soup = BeautifulSoup(CODEX_MOEL_DATE_TITLE_ROW, "html.parser")
        items = crawler.parse_list(soup)

        assert len(items) == 1
        assert items[0]["title"] == "2026.01.02"
        assert items[0]["date"] == "2026.09.14"

        ann = crawler._to_announcement(items[0], "https://www.moel.go.kr")
        assert json.loads(ann.raw_data)["posted"] == "2026-09-14"

    def test_posted_column_is_found_by_header_index_without_aria(self, crawler):
        """``aria-label`` 이 없어도 표 헤더에서 센 칸 번호로 등록일을 찾는다."""
        soup = BeautifulSoup(CODEX_MOEL_DATE_TITLE_ROW, "html.parser")
        assert not soup.find("td", attrs={"aria-label": "등록일"})
        items = crawler.parse_list(soup)
        assert items[0]["date"] == "2026.09.14"
        assert items[0]["author"] == "기획재정담당관"

    def test_without_a_posted_column_no_date_is_invented(self, crawler):
        """등록일 칸도 라벨도 없으면 게시일을 만들지 않는다 (빈 값)."""
        html = CODEX_MOEL_DATE_TITLE_ROW.replace(
            '<th scope="col">등록일</th>', '<th scope="col">기간</th>'
        )
        items = crawler.parse_list(BeautifulSoup(html, "html.parser"))

        assert len(items) == 1
        assert items[0]["date"] == ""
        ann = crawler._to_announcement(items[0], "https://www.moel.go.kr")
        assert "posted" not in json.loads(ann.raw_data)
        assert ann.period_start is None and ann.period_end is None
