"""Tests for MoisSseCrawler (행정안전부 사회연대경제)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.mois_sse import MoisSseCrawler
from alert.models import RawAnnouncement


class TestMoisSseCrawler:
    """Test MoisSseCrawler implementation."""

    @pytest.fixture
    def mock_mois_sse_config(self):
        """Mock config for MoisSseCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        mois_sse_source = MagicMock()
        mois_sse_source.enabled = True
        mois_sse_source.base_url = "https://www.mois.go.kr"

        config.crawler.sources = {"mois_sse": mois_sse_source}
        return config

    def test_initialization(self, mock_mois_sse_config):
        """MoisSseCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            crawler = MoisSseCrawler()
            assert crawler.source_name == "mois_sse"

    def test_extract_post_id(self, mock_mois_sse_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            crawler = MoisSseCrawler()

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

    def test_normalize_url(self, mock_mois_sse_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            crawler = MoisSseCrawler()
            base_url = "https://www.mois.go.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/test/path", base_url) == "https://www.mois.go.kr/test/path"

            # Relative path
            assert crawler._normalize_url("test/path", base_url) == "https://www.mois.go.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_mois_sse_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            crawler = MoisSseCrawler()

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

    def test_to_announcement_valid(self, mock_mois_sse_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            crawler = MoisSseCrawler()

            item = {
                "title": "사회연대경제 지원사업 공고",
                "link": "/frt/bbs/type001/commonSelectBoardArticle.do?nttId=12345",
                "author": "행정안전부",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            result = crawler._to_announcement(item, "https://www.mois.go.kr")

            assert result is not None
            assert isinstance(result, RawAnnouncement)
            assert result.source == "mois_sse"
            assert result.title == "사회연대경제 지원사업 공고"
            assert result.url == "https://www.mois.go.kr/frt/bbs/type001/commonSelectBoardArticle.do?nttId=12345"
            assert result.author == "행정안전부"
            assert result.category == "지원사업"
            assert result.period_start == "2026-04-01"
            assert result.period_end == "2026-04-30"

    def test_to_announcement_missing_title(self, mock_mois_sse_config):
        """_to_announcement() should return None if title is missing."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            crawler = MoisSseCrawler()

            item = {
                "title": "",
                "link": "/frt/bbs/type001/commonSelectBoardArticle.do?nttId=12345",
                "author": "행정안전부",
                "category": "",
                "date": "",
            }

            result = crawler._to_announcement(item, "https://www.mois.go.kr")
            assert result is None

    def test_fetch_without_beautifulsoup(self, mock_mois_sse_config):
        """fetch() should return empty list if BeautifulSoup is not available."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            with patch("alert.crawlers.mois_sse.BeautifulSoup", None):
                crawler = MoisSseCrawler()
                result = crawler.fetch()
                assert result == []

    def test_fetch_board_listing_table_strategy(self, mock_mois_sse_config):
        """_fetch_board_listing() should parse table-based board."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            crawler = MoisSseCrawler()

            mock_html = """
            <html>
                <body>
                    <table class="board_list">
                        <tbody>
                            <tr>
                                <td class="title">
                                    <a href="/frt/bbs/type001/commonSelectBoardArticle.do?nttId=12345">
                                        사회연대경제 지원사업
                                    </a>
                                </td>
                                <td class="author">행정안전부</td>
                                <td class="date">2026-04-01</td>
                            </tr>
                            <tr>
                                <td class="title">
                                    <a href="/frt/bbs/type001/commonSelectBoardArticle.do?nttId=12346">
                                        협동조합 지원사업
                                    </a>
                                </td>
                                <td class="author">행정안전부</td>
                                <td class="date">2026-04-02</td>
                            </tr>
                        </tbody>
                    </table>
                </body>
            </html>
            """

            mock_response = MagicMock()
            mock_response.text = mock_html
            mock_response.apparent_encoding = "utf-8"

            with patch.object(crawler, "get", return_value=mock_response):
                items = crawler._fetch_board_listing("https://www.mois.go.kr/board")

            assert len(items) == 2
            assert items[0]["title"] == "사회연대경제 지원사업"
            assert items[0]["author"] == "행정안전부"
            assert items[1]["title"] == "협동조합 지원사업"

    def test_parse_period(self, mock_mois_sse_config):
        """_parse_period() should parse period strings correctly."""
        with patch("alert.crawlers.base.get_config", return_value=mock_mois_sse_config):
            crawler = MoisSseCrawler()

            # Period with tilde
            start, end = crawler._parse_period("2026-04-01 ~ 2026-04-30")
            assert start == "2026-04-01"
            assert end == "2026-04-30"

            # Single date
            start, end = crawler._parse_period("2026-04-15")
            assert start == "2026-04-15"
            assert end == "2026-04-15"

            # Empty string
            start, end = crawler._parse_period("")
            assert start is None
            assert end is None

            # Unicode tilde
            start, end = crawler._parse_period("2026.04.01 \u223c 2026.04.30")
            assert start == "2026-04-01"
            assert end == "2026-04-30"
