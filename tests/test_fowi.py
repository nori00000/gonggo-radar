"""Tests for FowiCrawler."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from alert.crawlers.fowi import FowiCrawler
from alert.models import RawAnnouncement

FIXTURES = Path(__file__).parent / "fixtures"


class TestFowiCrawler:
    """Test FowiCrawler implementation."""

    @pytest.fixture
    def mock_fowi_config(self):
        """Mock config for FowiCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        fowi_source = MagicMock()
        fowi_source.enabled = True
        fowi_source.base_url = "https://fowi.or.kr"

        config.crawler.sources = {"fowi": fowi_source}
        return config

    def test_initialization(self, mock_fowi_config):
        """FowiCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            crawler = FowiCrawler()
            assert crawler.source_name == "fowi"

    def test_extract_post_id(self, mock_fowi_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            crawler = FowiCrawler()

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?nttId=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?seq=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?idx=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "123456"

            # Empty link
            assert crawler._extract_post_id("") == ""

            # Hash fallback for unrecognized pattern
            result = crawler._extract_post_id("https://example.com/some-page")
            assert len(result) == 16  # MD5 hash truncated to 16 chars

    def test_normalize_url(self, mock_fowi_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            crawler = FowiCrawler()
            base_url = "https://fowi.or.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/test/path", base_url) == "https://fowi.or.kr/test/path"

            # Relative path
            assert crawler._normalize_url("test/path", base_url) == "https://fowi.or.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_fowi_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            crawler = FowiCrawler()

            # YYYY-MM-DD format
            assert crawler._normalize_date("2026-04-01") == "2026-04-01"

            # YYYY.MM.DD format
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"

            # YYYY/MM/DD format
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"

            # YYYYMMDD format
            assert crawler._normalize_date("20260401") == "2026-04-01"

            # With single-digit month/day
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"

            # Empty string
            assert crawler._normalize_date("") is None

            # Invalid format
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_fowi_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            crawler = FowiCrawler()

            item = {
                "title": "한국산림복지진흥원 지원사업 공고",
                "link": "/user/board/boardView.do?nttId=12345",
                "author": "한국산림복지진흥원",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-05-01",
            }

            base_url = "https://fowi.or.kr"
            announcement = crawler._to_announcement(item, base_url)

            assert announcement is not None
            assert isinstance(announcement, RawAnnouncement)
            assert announcement.source == "fowi"
            assert announcement.source_id == "12345"
            assert announcement.title == "한국산림복지진흥원 지원사업 공고"
            assert announcement.url == "https://fowi.or.kr/user/board/boardView.do?nttId=12345"
            assert announcement.author == "한국산림복지진흥원"
            assert announcement.category == "지원사업"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-05-01"

    def test_to_announcement_missing_title(self, mock_fowi_config):
        """_to_announcement() should return None for item without title."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            crawler = FowiCrawler()

            item = {
                "title": "",
                "link": "/user/board/boardView.do?nttId=12345",
                "author": "한국산림복지진흥원",
                "category": "지원사업",
                "date": "2026-04-01",
            }

            base_url = "https://fowi.or.kr"
            announcement = crawler._to_announcement(item, base_url)

            assert announcement is None

    def test_fetch_without_beautifulsoup(self, mock_fowi_config):
        """fetch() should return empty list if BeautifulSoup is not available."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            with patch("alert.crawlers.fowi.BeautifulSoup", None):
                crawler = FowiCrawler()
                result = crawler.fetch()
                assert result == []

    def test_fetch_board_listing_table_strategy(self, mock_fowi_config):
        """_fetch_board_listing() should use table strategy when table structure is detected."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            crawler = FowiCrawler()

            html = """
            <html>
            <body>
                <table class="board_list">
                    <tbody>
                        <tr>
                            <td class="title">
                                <a href="/user/board/boardView.do?nttId=12345">공고 제목</a>
                            </td>
                            <td class="author">한국산림복지진흥원</td>
                            <td class="date">2026-04-01</td>
                        </tr>
                    </tbody>
                </table>
            </body>
            </html>
            """

            mock_response = MagicMock()
            mock_response.text = html
            mock_response.apparent_encoding = "utf-8"
            mock_response.encoding = "utf-8"

            with patch.object(crawler, "get", return_value=mock_response):
                items = crawler._fetch_board_listing("https://fowi.or.kr/user/board/boardList.do")

                assert len(items) > 0
                assert items[0]["title"] == "공고 제목"
                assert items[0]["link"] == "/user/board/boardView.do?nttId=12345"
                assert items[0]["author"] == "한국산림복지진흥원"
                assert items[0]["date"] == "2026-04-01"

    def test_parse_period(self, mock_fowi_config):
        """_parse_period() should parse period strings correctly."""
        with patch("alert.crawlers.base.get_config", return_value=mock_fowi_config):
            crawler = FowiCrawler()

            # Period with tilde
            start, end = crawler._parse_period("2026-04-01 ~ 2026-05-01")
            assert start == "2026-04-01"
            assert end == "2026-05-01"

            # Period with unicode tilde
            start, end = crawler._parse_period("2026-04-01 \u223c 2026-05-01")
            assert start == "2026-04-01"
            assert end == "2026-05-01"

            # Single date
            start, end = crawler._parse_period("2026-04-01")
            assert start == "2026-04-01"
            assert end == "2026-04-01"

            # Empty string
            start, end = crawler._parse_period("")
            assert start is None
            assert end is None


class TestFowiBoardParsing:
    """실제 fowi 게시판(fowi_board_list.html) 파싱.

    픽스처는 2026-09-12 `https://fowi.or.kr/user/bbs/bbsList.do?bbsManageId=12`
    실응답(200, 79,965 B)이다.

    W7/W8 실측 결함: fowi 행 10건이 전부 `/user/contents/contentsView.do?cntntsId=...`
    (산림치유 소개·지도사 양성기관 등 정적 안내 페이지)였다. 실제 게시글 링크는
    href가 아니라 `onclick="javascript:goView('<bbsId>')"`이고, 목록 표
    `table.BasicTable_02`에 tbody가 없어 기존 table/list 전략이 전부 실패한 뒤
    범용 링크 전략의 `view.do` 패턴이 `contentsView.do`를 집어삼켰다.
    """

    @pytest.fixture
    def crawler(self):
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "test-agent"
        source = MagicMock()
        source.enabled = True
        source.base_url = "https://fowi.or.kr"
        config.crawler.sources = {"fowi": source}
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield FowiCrawler()

    @staticmethod
    def _soup():
        html = (FIXTURES / "fowi_board_list.html").read_text(encoding="utf-8")
        return BeautifulSoup(html, "html.parser")

    def test_is_navigation_link(self, crawler):
        """contentsView.do / cntntsId= 는 안내 페이지."""
        assert crawler._is_navigation_link(
            "/user/contents/contentsView.do?cntntsId=380"
        ) is True
        assert crawler._is_navigation_link(
            "https://fowi.or.kr/user/contents/contentsView.do?cntntsId=349"
        ) is True
        assert crawler._is_navigation_link(
            "/user/bbs/bbsView.do?bbsManageId=12&bbsId=9191"
        ) is False
        assert crawler._is_navigation_link("") is False

    def test_extract_bbs_manage_id(self, crawler):
        """목록 URL에서 게시판 번호를 뽑는다."""
        assert crawler._extract_bbs_manage_id(
            "https://fowi.or.kr/user/bbs/bbsList.do?bbsManageId=25"
        ) == "25"
        # 없으면 기본 게시판
        assert crawler._extract_bbs_manage_id("https://fowi.or.kr/") == "12"

    def test_goview_strategy_parses_real_posts(self, crawler):
        """onclick goView에서 게시글 10건을 뽑고 본문 URL을 조립한다."""
        items = crawler._parse_goview_board(self._soup(), "12")

        assert len(items) == 10
        first = items[0]
        assert first["title"] == "2026년 카이스트 멘토링 캠퍼스 투어 모집 안내"
        assert first["link"] == "/user/bbs/bbsView.do?bbsManageId=12&bbsId=9191"
        assert first["author"] == "AX정보화팀"
        assert first["date"] == "2026-09-11"
        # 안내 페이지는 한 건도 없다
        assert all("contentsView" not in item["link"] for item in items)
        assert all("cntntsId" not in item["link"] for item in items)

    def test_href_strategies_find_no_posts_on_real_page(self, crawler):
        """href 기반 전략만으로는 게시글을 못 찾는다(=goView 전략이 필요한 이유)."""
        soup = self._soup()

        # table.BasicTable_02에 tbody가 없어 표 전략이 실패한다
        assert crawler._parse_table_board(soup) == []

        # 범용 링크 전략은 안내 페이지를 더 이상 게시글로 반환하지 않는다
        generic = crawler._parse_generic_links(soup)
        assert all("contentsView" not in item["link"] for item in generic)
        assert all("cntntsId" not in item["link"] for item in generic)

    def test_fetch_board_listing_returns_posts_only(self, crawler):
        """목록 파싱 결과는 게시글 10건이고 안내 페이지가 0건이다."""
        html = (FIXTURES / "fowi_board_list.html").read_text(encoding="utf-8")
        response = MagicMock()
        response.text = html
        response.apparent_encoding = "utf-8"

        with patch.object(crawler, "get", return_value=response):
            items = crawler._fetch_board_listing(
                "https://fowi.or.kr/user/bbs/bbsList.do?bbsManageId=12"
            )

        assert len(items) == 10
        assert all("bbsView.do" in item["link"] for item in items)
        assert all("contentsView" not in item["link"] for item in items)

    def test_to_announcement_rejects_navigation_link(self, crawler):
        """어떤 전략을 타든 안내 페이지는 RawAnnouncement가 되지 않는다."""
        nav_item = {
            "title": "산림치유효과",
            "link": "/user/contents/contentsView.do?cntntsId=349",
            "author": "",
            "category": "",
            "date": "",
        }
        assert crawler._to_announcement(nav_item, "https://fowi.or.kr") is None

    def test_to_announcement_uses_bbs_id(self, crawler):
        """게시글은 bbsId를 source_id로 쓴다 (md5 폴백이 아니라)."""
        post_item = {
            "title": "2026년 카이스트 멘토링 캠퍼스 투어 모집 안내",
            "link": "/user/bbs/bbsView.do?bbsManageId=12&bbsId=9191",
            "author": "AX정보화팀",
            "category": "",
            "date": "2026-09-11",
        }
        announcement = crawler._to_announcement(post_item, "https://fowi.or.kr")

        assert isinstance(announcement, RawAnnouncement)
        assert announcement.source_id == "9191"
        assert announcement.url == (
            "https://fowi.or.kr/user/bbs/bbsView.do?bbsManageId=12&bbsId=9191"
        )
        assert announcement.period_end == "2026-09-11"

    def test_fetch_end_to_end_yields_real_announcements(self, crawler):
        """fetch()가 게시글 10건을 RawAnnouncement로 돌려준다."""
        html = (FIXTURES / "fowi_board_list.html").read_text(encoding="utf-8")
        response = MagicMock()
        response.text = html
        response.apparent_encoding = "utf-8"

        with patch.object(crawler, "get", return_value=response):
            announcements = crawler.fetch()

        assert len(announcements) == 10
        assert all(a.source == "fowi" for a in announcements)
        assert all("bbsView.do" in a.url for a in announcements)
        assert all("contentsView" not in a.url for a in announcements)
