"""Tests for GoyangStartupCrawler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.goyang_startup import GoyangStartupCrawler
from alert.models import RawAnnouncement


class TestGoyangStartupCrawler:
    """Test GoyangStartupCrawler implementation."""

    @pytest.fixture
    def mock_goyang_config(self):
        """Mock config for GoyangStartupCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        goyang_source = MagicMock()
        goyang_source.enabled = True
        goyang_source.base_url = "https://www.goyangstartup.kr"

        config.crawler.sources = {"goyang_startup": goyang_source}
        return config

    def test_initialization(self, mock_goyang_config):
        """GoyangStartupCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            crawler = GoyangStartupCrawler()
            assert crawler.source_name == "goyang_startup"

    def test_extract_post_id(self, mock_goyang_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            crawler = GoyangStartupCrawler()

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

    def test_normalize_url(self, mock_goyang_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            crawler = GoyangStartupCrawler()
            base_url = "https://www.goyangstartup.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/test/path", base_url) == "https://www.goyangstartup.kr/test/path"

            # Relative path
            assert crawler._normalize_url("test/path", base_url) == "https://www.goyangstartup.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_goyang_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            crawler = GoyangStartupCrawler()

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

    def test_to_announcement_valid(self, mock_goyang_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            crawler = GoyangStartupCrawler()

            item = {
                "title": "고양시 스타트업 지원사업 공고",
                "link": "/front/board/boardView.do?nttId=12345",
                "author": "고양스타트업",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            base_url = "https://www.goyangstartup.kr"
            announcement = crawler._to_announcement(item, base_url)

            assert announcement is not None
            assert announcement.source == "goyang_startup"
            assert announcement.source_id == "12345"
            assert announcement.title == "고양시 스타트업 지원사업 공고"
            assert announcement.url == "https://www.goyangstartup.kr/front/board/boardView.do?nttId=12345"
            assert announcement.author == "고양스타트업"
            assert announcement.category == "지원사업"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"

    def test_to_announcement_missing_title(self, mock_goyang_config):
        """_to_announcement() should return None if title is missing."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            crawler = GoyangStartupCrawler()

            item = {
                "title": "",
                "link": "/front/board/boardView.do?nttId=12345",
                "author": "고양스타트업",
                "category": "지원사업",
                "date": "2026-04-01",
            }

            base_url = "https://www.goyangstartup.kr"
            announcement = crawler._to_announcement(item, base_url)

            assert announcement is None

    def test_fetch_without_beautifulsoup(self, mock_goyang_config):
        """fetch() should return empty list if BeautifulSoup is not available."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            with patch("alert.crawlers.goyang_startup.BeautifulSoup", None):
                crawler = GoyangStartupCrawler()
                announcements = crawler.fetch()
                assert announcements == []

    def test_fetch_board_listing_table_strategy(self, mock_goyang_config):
        """_fetch_board_listing() should parse table-based board."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            crawler = GoyangStartupCrawler()

            html = """
            <html>
            <body>
                <table class="board_list">
                    <tbody>
                        <tr>
                            <td class="title"><a href="/view/123">고양시 스타트업 지원</a></td>
                            <td class="author">고양스타트업</td>
                            <td class="date">2026-04-01</td>
                        </tr>
                        <tr>
                            <td class="title"><a href="/view/124">창업 지원 프로그램</a></td>
                            <td class="author">고양스타트업</td>
                            <td class="date">2026-04-05</td>
                        </tr>
                    </tbody>
                </table>
            </body>
            </html>
            """

            mock_response = MagicMock()
            mock_response.text = html
            mock_response.apparent_encoding = "utf-8"

            with patch.object(crawler, "get", return_value=mock_response):
                items = crawler._fetch_board_listing("https://www.goyangstartup.kr/board")

                assert len(items) == 2
                assert items[0]["title"] == "고양시 스타트업 지원"
                assert items[0]["link"] == "/view/123"
                assert items[0]["author"] == "고양스타트업"
                assert items[1]["title"] == "창업 지원 프로그램"

    def test_parse_period(self, mock_goyang_config):
        """_parse_period() should parse period strings correctly."""
        with patch("alert.crawlers.base.get_config", return_value=mock_goyang_config):
            crawler = GoyangStartupCrawler()

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

            # Period with unicode tilde
            start, end = crawler._parse_period("2026-04-01 \u223c 2026-04-30")
            assert start == "2026-04-01"
            assert end == "2026-04-30"
