"""P2-S 계약: 소스 정비 — 신규 HTML 소스 2개 + epis 경로 교체 + 비활성화 근거.

네트워크를 타지 않는다: 모든 파싱은 ``tests/fixtures`` 의 저장 픽스처를 읽고,
설정은 운영 ``alert/config.yaml`` 을 그대로 읽는다.
"""

import json
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
