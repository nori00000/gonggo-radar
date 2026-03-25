"""Tests for FowiCrawler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.fowi import FowiCrawler
from alert.models import RawAnnouncement


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
