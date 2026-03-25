"""Tests for HTML crawlers Batch A: GyeonggiCrawler, GafiCrawler, GbsaCrawler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.gyeonggi import GyeonggiCrawler
from alert.crawlers.gafi import GafiCrawler
from alert.crawlers.gbsa import GbsaCrawler
from alert.models import RawAnnouncement


# ---------------------------------------------------------------------------
# GyeonggiCrawler Tests
# ---------------------------------------------------------------------------

class TestGyeonggiCrawler:
    """Test GyeonggiCrawler implementation."""

    @pytest.fixture
    def mock_gyeonggi_config(self):
        """Mock config for GyeonggiCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        gyeonggi_source = MagicMock()
        gyeonggi_source.enabled = True
        gyeonggi_source.base_url = "https://www.gg.go.kr"

        config.crawler.sources = {"gyeonggi": gyeonggi_source}
        return config

    def test_initialization(self, mock_gyeonggi_config):
        """GyeonggiCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gyeonggi_config):
            crawler = GyeonggiCrawler()
            assert crawler.source_name == "gyeonggi"

    def test_extract_post_id(self, mock_gyeonggi_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gyeonggi_config):
            crawler = GyeonggiCrawler()

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

    def test_normalize_url(self, mock_gyeonggi_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gyeonggi_config):
            crawler = GyeonggiCrawler()
            base_url = "https://www.gg.go.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/test/path", base_url) == "https://www.gg.go.kr/test/path"

            # Relative path
            assert crawler._normalize_url("test/path", base_url) == "https://www.gg.go.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_gyeonggi_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gyeonggi_config):
            crawler = GyeonggiCrawler()

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

    def test_to_announcement_valid(self, mock_gyeonggi_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gyeonggi_config):
            crawler = GyeonggiCrawler()

            item = {
                "title": "경기도 지원사업 공고",
                "link": "/bbs/board.do?boardView&nttId=12345",
                "author": "경기도청",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.gg.go.kr")

            assert announcement is not None
            assert announcement.source == "gyeonggi"
            assert announcement.title == "경기도 지원사업 공고"
            assert announcement.url == "https://www.gg.go.kr/bbs/board.do?boardView&nttId=12345"
            assert announcement.author == "경기도청"
            assert announcement.category == "지원사업"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "12345"


# ---------------------------------------------------------------------------
# GafiCrawler Tests
# ---------------------------------------------------------------------------

class TestGafiCrawler:
    """Test GafiCrawler implementation."""

    @pytest.fixture
    def mock_gafi_config(self):
        """Mock config for GafiCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        gafi_source = MagicMock()
        gafi_source.enabled = True
        gafi_source.base_url = "https://www.gafi.or.kr"

        config.crawler.sources = {"gafi": gafi_source}
        return config

    def test_initialization(self, mock_gafi_config):
        """GafiCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gafi_config):
            crawler = GafiCrawler()
            assert crawler.source_name == "gafi"

    def test_extract_post_id(self, mock_gafi_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gafi_config):
            crawler = GafiCrawler()

            # contents_id parameter
            assert crawler._extract_post_id("https://example.com?contents_id=abc123") == "abc123"

            # Standard parameters
            assert crawler._extract_post_id("https://example.com?seq=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?nttId=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "123456"

            # Empty link
            assert crawler._extract_post_id("") == ""

            # Hash fallback
            result = crawler._extract_post_id("https://example.com/some-page")
            assert len(result) == 16

    def test_normalize_url(self, mock_gafi_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gafi_config):
            crawler = GafiCrawler()
            base_url = "https://www.gafi.or.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/test/path", base_url) == "https://www.gafi.or.kr/test/path"
            assert crawler._normalize_url("test/path", base_url) == "https://www.gafi.or.kr/test/path"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_gafi_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gafi_config):
            crawler = GafiCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_gafi_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gafi_config):
            crawler = GafiCrawler()

            item = {
                "title": "농수산 지원사업 공고",
                "link": "/web/board/boardContentsView.do?contents_id=abc123",
                "author": "경기도농수산진흥원",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.gafi.or.kr")

            assert announcement is not None
            assert announcement.source == "gafi"
            assert announcement.title == "농수산 지원사업 공고"
            assert announcement.url == "https://www.gafi.or.kr/web/board/boardContentsView.do?contents_id=abc123"
            assert announcement.author == "경기도농수산진흥원"
            assert announcement.category == "지원사업"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "abc123"


# ---------------------------------------------------------------------------
# GbsaCrawler Tests
# ---------------------------------------------------------------------------

class TestGbsaCrawler:
    """Test GbsaCrawler implementation."""

    @pytest.fixture
    def mock_gbsa_config(self):
        """Mock config for GbsaCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        gbsa_source = MagicMock()
        gbsa_source.enabled = True
        gbsa_source.base_url = "https://www.gbsa.or.kr"

        config.crawler.sources = {"gbsa": gbsa_source}
        return config

    def test_initialization(self, mock_gbsa_config):
        """GbsaCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gbsa_config):
            crawler = GbsaCrawler()
            assert crawler.source_name == "gbsa"

    def test_extract_post_id(self, mock_gbsa_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gbsa_config):
            crawler = GbsaCrawler()

            # seq parameter (first priority for GBSA)
            assert crawler._extract_post_id("https://example.com?seq=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?nttId=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?idx=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "123456"

            # Empty link
            assert crawler._extract_post_id("") == ""

            # Hash fallback
            result = crawler._extract_post_id("https://example.com/some-page")
            assert len(result) == 16

    def test_normalize_url(self, mock_gbsa_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gbsa_config):
            crawler = GbsaCrawler()
            base_url = "https://www.gbsa.or.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/test/path", base_url) == "https://www.gbsa.or.kr/test/path"
            assert crawler._normalize_url("test/path", base_url) == "https://www.gbsa.or.kr/test/path"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_gbsa_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gbsa_config):
            crawler = GbsaCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_gbsa_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_gbsa_config):
            crawler = GbsaCrawler()

            item = {
                "title": "경기도 경제과학 지원사업 공고",
                "link": "/board/noticeView.do?seq=12345",
                "author": "경기도경제과학진흥원",
                "category": "공지사항",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.gbsa.or.kr")

            assert announcement is not None
            assert announcement.source == "gbsa"
            assert announcement.title == "경기도 경제과학 지원사업 공고"
            assert announcement.url == "https://www.gbsa.or.kr/board/noticeView.do?seq=12345"
            assert announcement.author == "경기도경제과학진흥원"
            assert announcement.category == "공지사항"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "12345"
