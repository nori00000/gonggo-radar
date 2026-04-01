"""Tests for alert/crawlers/forest_service.py (ForestServiceCrawler)."""

from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.forest_service import ForestServiceCrawler


class TestForestServiceCrawler:
    """Test ForestServiceCrawler implementation."""

    @pytest.fixture
    def mock_forest_config(self):
        """Mock config for ForestServiceCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        forest_source = MagicMock()
        forest_source.enabled = True
        forest_source.base_url = "https://www.forest.go.kr"

        config.crawler.sources = {"forest_service": forest_source}
        return config

    def test_initialization(self, mock_forest_config):
        """ForestServiceCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            assert crawler.source_name == "forest_service"

    def test_extract_post_id_patterns(self, mock_forest_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            # Parameter-based ID - nttId
            assert crawler._extract_post_id(
                "https://www.forest.go.kr/kfsweb/cop/bbs/selectBoardArticle.do?nttId=12345"
            ) == "12345"

            # Parameter-based ID - seq
            assert crawler._extract_post_id(
                "https://www.forest.go.kr/view?seq=67890"
            ) == "67890"

            # Parameter-based ID - idx
            assert crawler._extract_post_id(
                "https://www.forest.go.kr/view?idx=54321"
            ) == "54321"

            # Path-based ID
            assert crawler._extract_post_id(
                "https://www.forest.go.kr/view/123456"
            ) == "123456"

            # Empty link
            assert crawler._extract_post_id("") == ""

            # Fallback to hash for links without IDs
            result = crawler._extract_post_id("https://www.forest.go.kr/some/page")
            assert len(result) == 16  # MD5 hash truncated

    def test_normalize_url(self, mock_forest_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            base_url = "https://www.forest.go.kr"

            # Absolute URL (https)
            assert crawler._normalize_url(
                "https://example.com/test", base_url
            ) == "https://example.com/test"

            # Absolute URL (http)
            assert crawler._normalize_url(
                "http://example.com/test", base_url
            ) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url(
                "//example.com/test", base_url
            ) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url(
                "/kfsweb/test", base_url
            ) == "https://www.forest.go.kr/kfsweb/test"

            # Relative path
            assert crawler._normalize_url(
                "test/path", base_url
            ) == "https://www.forest.go.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date_formats(self, mock_forest_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

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

    def test_parse_period(self, mock_forest_config):
        """_parse_period() should parse period strings."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            # Standard ~ separator
            start, end = crawler._parse_period("2026-04-01 ~ 2026-04-30")
            assert start == "2026-04-01"
            assert end == "2026-04-30"

            # Dot separator
            start, end = crawler._parse_period("2026.04.01~2026.04.30")
            assert start == "2026-04-01"
            assert end == "2026-04-30"

            # YYYYMMDD format
            start, end = crawler._parse_period("20260401~20260430")
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

    def test_to_announcement_valid(self, mock_forest_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            item = {
                "title": "산림사업 공고",
                "link": "/kfsweb/cop/bbs/selectBoardArticle.do?nttId=12345",
                "author": "산림청",
                "category": "공지사항",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.forest.go.kr")

            assert announcement is not None
            assert announcement.source == "forest_service"
            assert announcement.title == "산림사업 공고"
            assert announcement.url == "https://www.forest.go.kr/kfsweb/cop/bbs/selectBoardArticle.do?nttId=12345"
            assert announcement.author == "산림청"
            assert announcement.category == "공지사항"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "12345"

    def test_to_announcement_empty_title_returns_none(self, mock_forest_config):
        """_to_announcement() should return None if title is empty."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            item = {
                "title": "",
                "link": "/kfsweb/cop/bbs/selectBoardArticle.do?nttId=12345",
                "author": "산림청",
                "category": "공지사항",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.forest.go.kr")

            assert announcement is None

    def test_fetch_returns_empty_when_beautifulsoup_not_available(self, mock_forest_config):
        """fetch() should return empty list when BeautifulSoup is not installed."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            with patch("alert.crawlers.forest_service.BeautifulSoup", None):
                results = crawler.fetch()

                assert results == []

    def test_to_announcement_uses_default_author(self, mock_forest_config):
        """_to_announcement() should use default author when not provided."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            item = {
                "title": "산림사업 공고",
                "link": "/kfsweb/cop/bbs/selectBoardArticle.do?nttId=99999",
                "author": "",
                "category": "",
                "date": "",
            }

            announcement = crawler._to_announcement(item, "https://www.forest.go.kr")

            assert announcement is not None
            assert announcement.author == "산림청"

    def test_fetch_handles_http_error(self, mock_forest_config):
        """fetch() should handle HTTP errors gracefully."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            with patch.object(crawler, "get", return_value=None):
                results = crawler.fetch()

                assert results == []

    def test_to_announcement_generates_hash_when_no_id(self, mock_forest_config):
        """_to_announcement() should generate hash source_id when link has no extractable ID."""
        with patch("alert.crawlers.base.get_config", return_value=mock_forest_config):
            crawler = ForestServiceCrawler()

            item = {
                "title": "산림 지원사업",
                "link": "",
                "author": "산림청",
                "category": "",
                "date": "",
            }

            announcement = crawler._to_announcement(item, "https://www.forest.go.kr")

            assert announcement is not None
            assert len(announcement.source_id) == 16  # MD5 hash truncated
