"""Tests for GgeeaCrawler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.ggeea import GgeeaCrawler
from alert.models import RawAnnouncement


class TestGgeeaCrawler:
    """Test GgeeaCrawler implementation."""

    @pytest.fixture
    def mock_ggeea_config(self):
        """Mock config for GgeeaCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        ggeea_source = MagicMock()
        ggeea_source.enabled = True
        ggeea_source.base_url = "https://www.ggeea.or.kr"

        config.crawler.sources = {"ggeea": ggeea_source}
        return config

    def test_initialization(self, mock_ggeea_config):
        """GgeeaCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            crawler = GgeeaCrawler()
            assert crawler.source_name == "ggeea"

    def test_extract_post_id(self, mock_ggeea_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            crawler = GgeeaCrawler()

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

    def test_normalize_url(self, mock_ggeea_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            crawler = GgeeaCrawler()
            base_url = "https://www.ggeea.or.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/test/path", base_url) == "https://www.ggeea.or.kr/test/path"

            # Relative path
            assert crawler._normalize_url("test/path", base_url) == "https://www.ggeea.or.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_ggeea_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            crawler = GgeeaCrawler()

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

    def test_to_announcement_valid(self, mock_ggeea_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            crawler = GgeeaCrawler()

            item = {
                "title": "경기환경에너지진흥원 지원사업 공고",
                "link": "/front/board/boardView.do?nttId=12345",
                "author": "경기환경에너지진흥원",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            result = crawler._to_announcement(item, "https://www.ggeea.or.kr")

            assert result is not None
            assert isinstance(result, RawAnnouncement)
            assert result.source == "ggeea"
            assert result.source_id == "12345"
            assert result.title == "경기환경에너지진흥원 지원사업 공고"
            assert result.url == "https://www.ggeea.or.kr/front/board/boardView.do?nttId=12345"
            assert result.author == "경기환경에너지진흥원"
            assert result.category == "지원사업"
            assert result.period_start == "2026-04-01"
            assert result.period_end == "2026-04-30"

    def test_to_announcement_missing_title(self, mock_ggeea_config):
        """_to_announcement() should return None if title is missing."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            crawler = GgeeaCrawler()

            item = {
                "title": "",
                "link": "/front/board/boardView.do?nttId=12345",
                "author": "경기환경에너지진흥원",
                "category": "지원사업",
                "date": "2026-04-01",
            }

            result = crawler._to_announcement(item, "https://www.ggeea.or.kr")
            assert result is None

    def test_fetch_without_beautifulsoup(self, mock_ggeea_config):
        """fetch() should return empty list if BeautifulSoup is not available."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            with patch("alert.crawlers.ggeea.BeautifulSoup", None):
                crawler = GgeeaCrawler()
                result = crawler.fetch()
                assert result == []

    def test_fetch_board_listing_table_strategy(self, mock_ggeea_config):
        """_fetch_board_listing() should use table strategy when applicable."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            crawler = GgeeaCrawler()

            mock_response = MagicMock()
            mock_response.text = """
                <html>
                    <table class="board_list">
                        <tbody>
                            <tr>
                                <td class="title"><a href="/view?nttId=123">Test Title 1</a></td>
                                <td class="author">경기환경에너지진흥원</td>
                                <td class="date">2026-04-01</td>
                            </tr>
                            <tr>
                                <td class="title"><a href="/view?nttId=124">Test Title 2</a></td>
                                <td class="author">경기환경에너지진흥원</td>
                                <td class="date">2026-04-02</td>
                            </tr>
                        </tbody>
                    </table>
                </html>
            """
            mock_response.apparent_encoding = "utf-8"

            with patch.object(crawler, "get", return_value=mock_response):
                items = crawler._fetch_board_listing("https://www.ggeea.or.kr/front/board/boardList.do")

                assert len(items) == 2
                assert items[0]["title"] == "Test Title 1"
                assert items[0]["link"] == "/view?nttId=123"
                assert items[0]["author"] == "경기환경에너지진흥원"
                assert items[0]["date"] == "2026-04-01"

                assert items[1]["title"] == "Test Title 2"
                assert items[1]["link"] == "/view?nttId=124"
                assert items[1]["author"] == "경기환경에너지진흥원"
                assert items[1]["date"] == "2026-04-02"

    def test_parse_period(self, mock_ggeea_config):
        """_parse_period() should parse period strings into start and end dates."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ggeea_config):
            crawler = GgeeaCrawler()

            # Period with tilde
            start, end = crawler._parse_period("2026-04-01 ~ 2026-04-30")
            assert start == "2026-04-01"
            assert end == "2026-04-30"

            # Period with unicode wave dash
            start, end = crawler._parse_period("2026-04-01 \u223c 2026-04-30")
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
