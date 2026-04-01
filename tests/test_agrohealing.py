"""Tests for AgrohealingCrawler."""

from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.agrohealing import AgrohealingCrawler


class TestAgrohealingCrawler:
    """Test AgrohealingCrawler implementation."""

    @pytest.fixture
    def mock_config(self):
        """Mock config for AgrohealingCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        agrohealing_source = MagicMock()
        agrohealing_source.enabled = True
        agrohealing_source.base_url = "https://agrohealing.go.kr"

        config.crawler.sources = {"agrohealing": agrohealing_source}
        return config

    def test_initialization(self, mock_config):
        """AgrohealingCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            crawler = AgrohealingCrawler()
            assert crawler.source_name == "agrohealing"

    def test_extract_post_id(self, mock_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            crawler = AgrohealingCrawler()

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

    def test_normalize_url(self, mock_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            crawler = AgrohealingCrawler()
            base_url = "https://agrohealing.go.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/test/path", base_url) == "https://agrohealing.go.kr/test/path"

            # Relative path
            assert crawler._normalize_url("test/path", base_url) == "https://agrohealing.go.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            crawler = AgrohealingCrawler()

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

    def test_to_announcement_valid(self, mock_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            crawler = AgrohealingCrawler()

            item = {
                "title": "치유농업 지원사업 공고",
                "link": "/front/board/boardView.do?nttId=12345",
                "author": "치유농업ON",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://agrohealing.go.kr")

            assert announcement is not None
            assert announcement.source == "agrohealing"
            assert announcement.source_id == "12345"
            assert announcement.title == "치유농업 지원사업 공고"
            assert announcement.url == "https://agrohealing.go.kr/front/board/boardView.do?nttId=12345"
            assert announcement.author == "치유농업ON"
            assert announcement.category == "지원사업"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"

    def test_to_announcement_missing_title(self, mock_config):
        """_to_announcement() should return None if title is missing."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            crawler = AgrohealingCrawler()

            item = {
                "title": "",
                "link": "/board/view/12345",
                "author": "치유농업ON",
                "category": "공고",
                "date": "2026-04-01",
            }

            announcement = crawler._to_announcement(item, "https://agrohealing.go.kr")
            assert announcement is None

    def test_fetch_without_beautifulsoup(self, mock_config):
        """fetch() should return empty list if BeautifulSoup is not available."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            with patch("alert.crawlers.agrohealing.BeautifulSoup", None):
                crawler = AgrohealingCrawler()
                announcements = crawler.fetch()
                assert announcements == []

    def test_fetch_board_listing_table_strategy(self, mock_config):
        """_fetch_board_listing() should parse table-based board."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            crawler = AgrohealingCrawler()

            html = """
            <table class="board_list">
                <tbody>
                    <tr>
                        <td class="title"><a href="/view/123">테스트 공고 1</a></td>
                        <td class="author">치유농업ON</td>
                        <td class="date">2026-04-01</td>
                    </tr>
                    <tr>
                        <td class="title"><a href="/view/456">테스트 공고 2</a></td>
                        <td class="author">관리자</td>
                        <td class="date">2026-04-02</td>
                    </tr>
                </tbody>
            </table>
            """

            mock_response = MagicMock()
            mock_response.text = html
            mock_response.apparent_encoding = "utf-8"

            with patch.object(crawler, "get", return_value=mock_response):
                items = crawler._fetch_board_listing("https://agrohealing.go.kr/board")

                assert len(items) == 2
                assert items[0]["title"] == "테스트 공고 1"
                assert items[0]["link"] == "/view/123"
                assert items[0]["author"] == "치유농업ON"
                assert items[0]["date"] == "2026-04-01"

                assert items[1]["title"] == "테스트 공고 2"
                assert items[1]["link"] == "/view/456"
                assert items[1]["author"] == "관리자"
                assert items[1]["date"] == "2026-04-02"

    def test_parse_period(self, mock_config):
        """_parse_period() should parse period strings correctly."""
        with patch("alert.crawlers.base.get_config", return_value=mock_config):
            crawler = AgrohealingCrawler()

            # Period with tilde
            start, end = crawler._parse_period("2026-04-01 ~ 2026-04-30")
            assert start == "2026-04-01"
            assert end == "2026-04-30"

            # Single date
            start, end = crawler._parse_period("2026-04-01")
            assert start == "2026-04-01"
            assert end == "2026-04-01"

            # Empty string
            start, end = crawler._parse_period("")
            assert start is None
            assert end is None

            # Invalid format
            start, end = crawler._parse_period("invalid date")
            assert start is None
            assert end is None
