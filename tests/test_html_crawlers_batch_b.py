"""Tests for HTML crawlers Batch B: SocialenterpriseCrawler, EkrCrawler, EpisCrawler."""

from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.socialenterprise import SocialenterpriseCrawler
from alert.crawlers.ekr import EkrCrawler
from alert.crawlers.epis import EpisCrawler


# ---------------------------------------------------------------------------
# SocialenterpriseCrawler Tests
# ---------------------------------------------------------------------------

class TestSocialenterpriseCrawler:
    """Test SocialenterpriseCrawler implementation."""

    @pytest.fixture
    def mock_socialenterprise_config(self):
        """Mock config for SocialenterpriseCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        se_source = MagicMock()
        se_source.enabled = True
        se_source.base_url = "https://www.socialenterprise.or.kr"

        config.crawler.sources = {"socialenterprise": se_source}
        return config

    def test_initialization(self, mock_socialenterprise_config):
        """SocialenterpriseCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_socialenterprise_config):
            crawler = SocialenterpriseCrawler()
            assert crawler.source_name == "socialenterprise"

    def test_extract_post_id(self, mock_socialenterprise_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_socialenterprise_config):
            crawler = SocialenterpriseCrawler()

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?nttId=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?seq=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?idx=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "123456"

            # Empty link
            assert crawler._extract_post_id("") == ""

            # Hash fallback
            result = crawler._extract_post_id("https://example.com/some-page")
            assert len(result) == 16

    def test_normalize_url(self, mock_socialenterprise_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_socialenterprise_config):
            crawler = SocialenterpriseCrawler()
            base_url = "https://www.socialenterprise.or.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/test/path", base_url) == "https://www.socialenterprise.or.kr/test/path"
            assert crawler._normalize_url("test/path", base_url) == "https://www.socialenterprise.or.kr/test/path"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_socialenterprise_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_socialenterprise_config):
            crawler = SocialenterpriseCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_socialenterprise_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_socialenterprise_config):
            crawler = SocialenterpriseCrawler()

            item = {
                "title": "사회적기업 지원사업 공고",
                "link": "/board/view.do?nttId=12345",
                "author": "",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.socialenterprise.or.kr")

            assert announcement is not None
            assert announcement.source == "socialenterprise"
            assert announcement.title == "사회적기업 지원사업 공고"
            assert announcement.url == "https://www.socialenterprise.or.kr/board/view.do?nttId=12345"
            assert announcement.author == "한국사회적기업진흥원"  # default author
            assert announcement.category == "지원사업"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "12345"


# ---------------------------------------------------------------------------
# EkrCrawler Tests
# ---------------------------------------------------------------------------

class TestEkrCrawler:
    """Test EkrCrawler implementation."""

    @pytest.fixture
    def mock_ekr_config(self):
        """Mock config for EkrCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        ekr_source = MagicMock()
        ekr_source.enabled = True
        ekr_source.base_url = "https://www.ekr.or.kr"

        config.crawler.sources = {"ekr": ekr_source}
        return config

    def test_initialization(self, mock_ekr_config):
        """EkrCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ekr_config):
            crawler = EkrCrawler()
            assert crawler.source_name == "ekr"

    def test_extract_post_id(self, mock_ekr_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ekr_config):
            crawler = EkrCrawler()

            # contentUid parameter (EKR-specific)
            assert crawler._extract_post_id("https://example.com?contentUid=abc123def") == "abc123def"

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

    def test_normalize_url(self, mock_ekr_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ekr_config):
            crawler = EkrCrawler()
            base_url = "https://www.ekr.or.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/test/path", base_url) == "https://www.ekr.or.kr/test/path"
            assert crawler._normalize_url("test/path", base_url) == "https://www.ekr.or.kr/test/path"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_ekr_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ekr_config):
            crawler = EkrCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_ekr_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ekr_config):
            crawler = EkrCrawler()

            item = {
                "title": "한국농어촌공사 입찰공고",
                "link": "/index.krc?contentUid=abc123def",
                "author": "",
                "category": "입찰",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.ekr.or.kr")

            assert announcement is not None
            assert announcement.source == "ekr"
            assert announcement.title == "한국농어촌공사 입찰공고"
            assert announcement.url == "https://www.ekr.or.kr/index.krc?contentUid=abc123def"
            assert announcement.author == "한국농어촌공사"  # default author
            assert announcement.category == "입찰"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "abc123def"


# ---------------------------------------------------------------------------
# EpisCrawler Tests
# ---------------------------------------------------------------------------

class TestEpisCrawler:
    """Test EpisCrawler implementation."""

    @pytest.fixture
    def mock_epis_config(self):
        """Mock config for EpisCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        epis_source = MagicMock()
        epis_source.enabled = True
        epis_source.base_url = "https://www.epis.or.kr"

        config.crawler.sources = {"epis": epis_source}
        return config

    def test_initialization(self, mock_epis_config):
        """EpisCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_epis_config):
            crawler = EpisCrawler()
            assert crawler.source_name == "epis"

    def test_extract_post_id(self, mock_epis_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_epis_config):
            crawler = EpisCrawler()

            # boardNo parameter (EPIS-specific)
            assert crawler._extract_post_id("https://example.com/board/read?boardNo=12345") == "12345"

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

    def test_normalize_url(self, mock_epis_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_epis_config):
            crawler = EpisCrawler()
            base_url = "https://www.epis.or.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/test/path", base_url) == "https://www.epis.or.kr/test/path"
            assert crawler._normalize_url("test/path", base_url) == "https://www.epis.or.kr/test/path"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_epis_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_epis_config):
            crawler = EpisCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_epis_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_epis_config):
            crawler = EpisCrawler()

            item = {
                "title": "농림수산식품 교육 공고",
                "link": "/board/read?boardNo=12345",
                "author": "",
                "category": "공고",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.epis.or.kr")

            assert announcement is not None
            assert announcement.source == "epis"
            assert announcement.title == "농림수산식품 교육 공고"
            assert announcement.url == "https://www.epis.or.kr/board/read?boardNo=12345"
            assert announcement.author == "농림수산식품교육문화정보원"  # default author
            assert announcement.category == "공고"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "12345"
