"""Tests for the forest-policy crawlers (kofpi, forest_press, lawmaking, coop).

All parsing is exercised against saved HTML fixtures in tests/fixtures/,
so these tests never touch the network.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from alert.classifier import DOMAIN_KEYWORDS, DOMAIN_KOREAN_MAP, DomainClassifier
from alert.crawlers.coop import CoopCrawler
from alert.crawlers.forest_press import ForestPressCrawler
from alert.crawlers.kofpi import KofpiCrawler
from alert.crawlers.lawmaking import LawmakingCrawler

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> BeautifulSoup:
    """Load a saved HTML fixture as BeautifulSoup."""
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


def make_config(source_name: str, base_url: str) -> MagicMock:
    """Build a mock AppConfig exposing a single enabled crawler source."""
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "test-agent"

    source = MagicMock()
    source.enabled = True
    source.base_url = base_url
    config.crawler.sources = {source_name: source}
    return config


class TestKofpiCrawler:
    """한국임업진흥원 공지/입찰공모 게시판 파서."""

    @pytest.fixture
    def crawler(self):
        config = make_config("kofpi", "https://www.kofpi.or.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield KofpiCrawler()

    def test_parse_notice_board(self, crawler):
        """공지사항 목록에서 제목/seq/게시일을 추출한다."""
        items = crawler.parse_list(
            load_fixture("kofpi_notice.html"), "/notice/notice_01view.do", "공지"
        )

        assert len(items) >= 5
        first = items[0]
        assert first["seq"] == "12658"
        assert first["title"] == "[모집] 2026 산림분야 오픈이노베이션 참여기업 모집(~9.30)"
        assert first["date"] == "2026-09-07"
        assert first["link"] == "/notice/notice_01view.do?bb_seq=12658"
        # 배지(긴급/공지)는 제목이 아니라 분류로 분리된다
        assert "긴급" not in first["title"]
        assert "긴급" in first["category"]

    def test_parse_bid_board_has_gubun_column(self, crawler):
        """입찰/공모 게시판의 '구분' 컬럼이 분류에 포함된다."""
        items = crawler.parse_list(
            load_fixture("kofpi_bid.html"), "/notice/notice_03view.do", "입찰/공모"
        )

        assert len(items) >= 5
        first = items[0]
        assert first["seq"] == "12427"
        assert first["title"].startswith("[공모] 2027년 산림소득 공모사업")
        assert "입찰/공모" in first["category"]
        assert "공모" in first["category"]

    def test_to_announcement_builds_detail_url_without_a_deadline(self, crawler):
        """상세 URL은 만들고, 제목 속 ``(~9.30)`` 은 마감으로 쓰지 않는다."""
        items = crawler.parse_list(
            load_fixture("kofpi_notice.html"), "/notice/notice_01view.do", "공지"
        )
        ann = crawler._to_announcement(items[0], "https://www.kofpi.or.kr")

        assert ann is not None
        assert ann.source == "kofpi"
        assert ann.source_id == "12658"
        assert ann.url == "https://www.kofpi.or.kr/notice/notice_01view.do?bb_seq=12658"
        assert ann.author == "한국임업진흥원"
        # 13차: 제목 괄호 추출기를 폐기했다. 게시일도, 제목 표기도 기간이
        # 아니다 - kofpi 는 기간을 만들 수 없다 (9차 게이트 HIGH ③).
        assert (ann.period_start, ann.period_end) == (None, None)
        assert json.loads(ann.raw_data)["posted"] == "2026-09-07"

    def test_the_title_deadline_extractor_is_retired(self, crawler):
        """13차: 제목 마감 추출기 자체가 사라졌다."""
        assert not hasattr(crawler, "_extract_deadline")
        assert not hasattr(crawler, "_period_from_title")

    @pytest.mark.parametrize("title", [
        "공모(~9.30)",                      # 12차까지 마감 2026-09-30
        "2025년 사업 결과 안내(~9.30)",      # 과거 결과 공고
        "공모 안내(~2025.9.30)",             # 명시 과거 연도
        "공모 안내(~2027.2.30)",             # 달력에 없는 날짜
        "공모(~9.30 접수 후 발표)",
        "마감 표기 없는 공고",
    ])
    def test_no_title_pattern_becomes_a_deadline(self, crawler, title):
        """어떤 제목 표기도 마감이 되지 않는다 (9차 게이트 HIGH ③)."""
        ann = crawler._to_announcement(
            {"title": title, "link": "/notice/notice_01view.do?bb_seq=1",
             "seq": "1", "date": "2026-09-07"},
            "https://www.kofpi.or.kr",
        )
        assert (ann.period_start, ann.period_end) == (None, None)

    def test_extract_seq_patterns(self, crawler):
        """fnGoView / bb_seq 양쪽 형태에서 seq를 추출한다."""
        assert crawler._extract_seq("fnGoView('12658'); return false;") == "12658"
        assert crawler._extract_seq("/notice/notice_01view.do?bb_seq=999") == "999"
        assert crawler._extract_seq("") == ""

    def test_normalize_date_formats(self, crawler):
        assert crawler._normalize_date("2026-09-07") == "2026-09-07"
        assert crawler._normalize_date("2026.9.7") == "2026-09-07"
        assert crawler._normalize_date("20260907") == "2026-09-07"
        assert crawler._normalize_date("") is None

    def test_fetch_returns_empty_on_http_error(self, crawler):
        """HTTP 실패 시 빈 리스트."""
        with patch.object(crawler, "get", return_value=None):
            assert crawler.fetch() == []


class TestForestPressCrawler:
    """산림청 보도자료 게시판 파서."""

    @pytest.fixture
    def crawler(self):
        config = make_config("forest_press", "https://www.forest.go.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield ForestPressCrawler()

    def test_source_name_is_distinct_from_forest_service(self, crawler):
        """같은 게시판 엔진을 재사용하지만 소스 이름은 분리된다."""
        assert crawler.source_name == "forest_press"

    def test_parse_press_list(self, crawler):
        items = crawler._parse_press_list(load_fixture("forest_press_list.html"))

        assert len(items) >= 5
        first = items[0]
        assert first["title"] == "청양산림항공관리소, 논산시 꿈빛나래 페스티벌 진로체험 부스 운영"
        assert first["date"] == "2026-09-12"
        assert "nttId=3224171" in first["link"]
        assert first["summary"]

    def test_session_id_is_stripped_from_link(self, crawler):
        """URL이 수집마다 달라지지 않도록 jsessionid를 제거한다."""
        items = crawler._parse_press_list(load_fixture("forest_press_list.html"))

        assert all("jsessionid" not in item["link"] for item in items)
        assert crawler._strip_session_id(
            "/kfsweb/cop/bbs/selectBoardArticle.do;jsessionid=ABC123?nttId=1"
        ) == "/kfsweb/cop/bbs/selectBoardArticle.do?nttId=1"

    def test_to_announcement_marks_policy_category(self, crawler):
        """보도자료는 category '정책/보도'로 구분된다."""
        items = crawler._parse_press_list(load_fixture("forest_press_list.html"))
        ann = crawler._to_announcement(items[0], "https://www.forest.go.kr")

        assert ann is not None
        assert ann.source == "forest_press"
        assert ann.source_id == "3224171"
        assert ann.category == "정책/보도"
        assert ann.url.startswith("https://www.forest.go.kr/kfsweb/cop/bbs/selectBoardArticle.do")
        # 보도자료의 날짜는 **게시일**이다 - 접수 시작일이 아니다
        # (6차 게이트 #1). forest_press 는 forest_service 를 상속하므로
        # 같은 수리가 함께 적용된다.
        assert ann.period_start is None
        assert ann.period_end is None
        assert json.loads(ann.raw_data)["posted"] == "2026-09-12"
        # 보도자료에는 마감일이 없다 - 게시일을 마감일로 채우지 않는다
        assert ann.period_end is None
        assert ann.summary

    def test_inherited_helpers_still_work(self, crawler):
        """ForestServiceCrawler의 URL/ID 헬퍼를 그대로 재사용한다."""
        assert crawler._extract_post_id("/x.do?nttId=555") == "555"
        assert crawler._normalize_url("/a", "https://www.forest.go.kr") == \
            "https://www.forest.go.kr/a"

    def test_fetch_returns_empty_on_http_error(self, crawler):
        with patch.object(crawler, "get", return_value=None):
            assert crawler.fetch() == []


class TestLawmakingCrawler:
    """국민참여입법센터 산림청 입법예고 파서."""

    @pytest.fixture
    def crawler(self):
        config = make_config("lawmaking", "https://opinion.lawmaking.go.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield LawmakingCrawler()

    def test_parse_list(self, crawler):
        items = crawler.parse_list(load_fixture("lawmaking_list.html"))

        assert len(items) >= 4
        first = items[0]
        assert first["title"] == "산림재난방지법 시행령 일부개정령안 입법예고"
        assert first["author"] == "산림청"
        assert first["law_type"] == "대통령령"
        assert "88388" in first["link"]
        assert "2026. 9. 7." in first["period"]

    def test_to_announcement_leaves_the_period_to_the_gate(self, crawler):
        """'2026. 9. 7. ~2026. 10. 19.' 셀은 관문이 마감으로 읽는다 (13차)."""
        from alert.main import _finalize_periods

        items = crawler.parse_list(load_fixture("lawmaking_list.html"))
        ann = crawler._to_announcement(items[0], "https://opinion.lawmaking.go.kr")

        assert ann is not None
        assert ann.source == "lawmaking"
        assert ann.source_id == "88388"
        assert ann.url == (
            "https://opinion.lawmaking.go.kr/gcom/ogLmPp/88388"
            "?isOgYn=Y&cptOfiOrgCd=1400000&opYn=Y"
        )
        assert (ann.period_start, ann.period_end) == (None, None)
        gated = _finalize_periods(ann.source, ann)
        assert (gated.period_start, gated.period_end) == (
            "2026-09-07", "2026-10-19"
        )
        assert ann.category.startswith("입법예고")

    def test_normalize_date_accepts_spaced_dots(self, crawler):
        assert crawler._normalize_date("2026. 9. 7.") == "2026-09-07"
        assert crawler._normalize_date("2026-10-19") == "2026-10-19"
        assert crawler._normalize_date("") is None

    def test_parse_period_single_date(self, crawler):
        start, end = crawler._parse_period("2026. 9. 7.")
        assert start == "2026-09-07"
        assert end == "2026-09-07"

        assert crawler._parse_period("") == (None, None)

    def test_extract_law_id(self, crawler):
        assert crawler._extract_law_id("/gcom/ogLmPp/88388?isOgYn=Y") == "88388"
        assert crawler._extract_law_id("") == ""

    def test_fetch_returns_empty_on_http_error(self, crawler):
        with patch.object(crawler, "get", return_value=None):
            assert crawler.fetch() == []


class TestCoopCrawler:
    """협동조합 포털 공지사항 파서."""

    @pytest.fixture
    def crawler(self):
        config = make_config("coop", "https://www.coop.go.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield CoopCrawler()

    def test_parse_list(self, crawler):
        items = crawler.parse_list(load_fixture("coop_notice.html"))

        assert len(items) >= 5
        first = items[0]
        assert first["brd_no"] == "14893"
        assert first["date"] == "2026.09.10"
        assert "협동조합연합회" in first["title"]

    def test_to_announcement_builds_detail_url(self, crawler):
        items = crawler.parse_list(load_fixture("coop_notice.html"))
        ann = crawler._to_announcement(items[0], "https://www.coop.go.kr")

        assert ann is not None
        assert ann.source == "coop"
        assert ann.source_id == "14893"
        assert ann.url == (
            "https://www.coop.go.kr/home/boardView.do"
            "?brd_mgrno=2&menu_no=2038&brd_no=14893"
        )
        # 게시일은 기간 필드가 아니라 raw_data.posted 로 간다 (계약 v2.1 판정 4)
        assert ann.period_start is None
        assert ann.period_end is None
        assert json.loads(ann.raw_data)["posted"] == "2026-09-10"

    def test_posting_date_never_becomes_a_period(self, crawler):
        """게시일만 있는 공지는 접수기간·마감을 만들지 않는다.

        회귀: 게시일을 period_start 로 쓰면 존재하지 않는 접수기간이
        브리핑에 표시되고, period_end 로 쓰면 판정 4의 마감 경과 제외에
        걸려 살아있는 공고가 사라진다.
        """
        items = crawler.parse_list(load_fixture("coop_notice.html"))
        announcements = [
            crawler._to_announcement(item, "https://www.coop.go.kr")
            for item in items
        ]
        assert announcements
        assert all(a.period_start is None for a in announcements)
        assert all(a.period_end is None for a in announcements)
        # 게시일 자체는 보존된다 - "새 소식" 판정에 쓸 수 있어야 한다
        posted = [json.loads(a.raw_data).get("posted") for a in announcements]
        assert any(p for p in posted)

    def test_extract_brd_no(self, crawler):
        assert crawler._extract_brd_no("javascript:fView('14893')") == "14893"
        assert crawler._extract_brd_no("/home/boardView.do?brd_no=1") == "1"
        assert crawler._extract_brd_no("") == ""

    def test_duplicate_rows_are_deduplicated(self, crawler):
        """같은 글이 공지 고정+일반 목록에 중복 노출돼도 한 번만 수집한다."""
        items = crawler.parse_list(load_fixture("coop_notice.html"))
        ids = [item["brd_no"] for item in items if item["brd_no"]]
        assert len(ids) == len(set(ids))

    def test_fetch_returns_empty_on_http_error(self, crawler):
        with patch.object(crawler, "get", return_value=None):
            assert crawler.fetch() == []


class TestForestSocialEconomyDomain:
    """신설 키워드 도메인 '산림형 사회적경제'."""

    def test_domain_registered(self):
        assert "forest_social_economy" in DOMAIN_KEYWORDS
        assert DOMAIN_KOREAN_MAP["forest_social_economy"] == "산림형 사회적경제"

    def test_classifies_forest_social_economy_text(self):
        dc = DomainClassifier()
        domain, confidence = dc.classify_text(
            "산림형 사회적경제 기업 육성 - 산림사업법인 및 산촌 마을기업 지원"
        )
        assert domain == "forest_social_economy"
        assert confidence > 0.0

    def test_existing_domains_unchanged(self):
        """기존 6개 도메인의 대표 문구 분류는 그대로여야 한다."""
        dc = DomainClassifier()
        cases = [
            ("이끼 스마트팜 시설원예 지원사업", "moss_agriculture"),
            ("조경공사 도시녹화 사업 공고", "landscape"),
            ("치유농업 산림치유 프로그램", "healing"),
            ("소공인 제조혁신 지원", "manufacturing"),
            ("나라장터 사회적기업 우선구매", "public_procurement"),
            ("AI융합 디지털전환 사업", "ai_digital"),
        ]
        for text, expected in cases:
            assert dc.classify_text(text)[0] == expected


class TestCycle11PolicyWhitelist:
    """11차 허용목록 (b) KOFPI 제목 마감 · (c) 입법예고 의견제출 기간."""

    def test_kofpi_makes_no_period_at_all(self):
        """(b 폐기) 제목 괄호 표기는 더 이상 마감이 아니다."""
        config = make_config("kofpi", "https://www.kofpi.or.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            crawler = KofpiCrawler()

        items = crawler.parse_list(
            load_fixture("kofpi_notice.html"), "/notice/notice_01view.do", "공지"
        )
        built = [
            crawler._to_announcement(i, "https://www.kofpi.or.kr") for i in items
        ]
        assert built and all(a is not None for a in built)
        assert not any(a.period_start or a.period_end for a in built)
        assert json.loads(built[0].raw_data)["posted"] == "2026-09-07"

    def test_lawmaking_period_comes_from_the_gate(self):
        """(c) 의견제출 기간 셀은 **관문에서** 시작·종료가 된다."""
        config = make_config("lawmaking", "https://opinion.lawmaking.go.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            crawler = LawmakingCrawler()

        from alert.main import _finalize_periods

        items = crawler.parse_list(load_fixture("lawmaking_list.html"))
        ann = crawler._to_announcement(items[0], "https://opinion.lawmaking.go.kr")
        # 크롤러 단계에서는 비어 있다
        assert (ann.period_start, ann.period_end) == (None, None)

        gated = _finalize_periods(ann.source, ann)
        assert (gated.period_start, gated.period_end) == (
            "2026-09-07", "2026-10-19"
        )
