"""Tests for HTML crawlers Batch D: KosmesCrawler, IpetCrawler, ApfsCrawler."""

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.kosmes import KosmesCrawler
from alert.crawlers.ipet import IpetCrawler
from alert.crawlers.apfs import ApfsCrawler
from alert.models import RawAnnouncement


# ============================================================
# KosmesCrawler Tests
# ============================================================

class TestKosmesCrawler:
    """Test KosmesCrawler implementation."""

    @pytest.fixture
    def mock_kosmes_config(self):
        """Mock config for KosmesCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        kosmes_source = MagicMock()
        kosmes_source.enabled = True
        kosmes_source.base_url = "https://www.kosmes.or.kr"

        config.crawler.sources = {"kosmes": kosmes_source}
        return config

    def test_initialization(self, mock_kosmes_config):
        """KosmesCrawler should initialize with correct source_name."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kosmes_config):
            crawler = KosmesCrawler()
            assert crawler.source_name == "kosmes"

    def test_extract_post_id(self, mock_kosmes_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kosmes_config):
            crawler = KosmesCrawler()

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?seq=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?nttId=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?idx=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "123456"

            # Hash fallback
            link = "https://example.com/some-page"
            expected_hash = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
            assert crawler._extract_post_id(link) == expected_hash

            # Empty link
            assert crawler._extract_post_id("") == ""

    def test_normalize_url(self, mock_kosmes_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kosmes_config):
            crawler = KosmesCrawler()
            base_url = "https://www.kosmes.or.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/board/view.do", base_url) == "https://www.kosmes.or.kr/board/view.do"
            assert crawler._normalize_url("board/view.do", base_url) == "https://www.kosmes.or.kr/board/view.do"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_kosmes_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kosmes_config):
            crawler = KosmesCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_kosmes_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kosmes_config):
            crawler = KosmesCrawler()

            item = {
                "title": "중소기업 수출지원 사업 공고",
                "link": "/nsh/SH/view.do?seq=12345",
                "author": "",
                "category": "수출지원",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.kosmes.or.kr")

            assert announcement is not None
            assert announcement.source == "kosmes"
            assert announcement.title == "중소기업 수출지원 사업 공고"
            assert announcement.url == "https://www.kosmes.or.kr/nsh/SH/view.do?seq=12345"
            assert announcement.author == "중소벤처기업진흥공단"  # default author
            assert announcement.category == "수출지원"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "12345"


# ============================================================
# IpetCrawler Tests
# ============================================================

class TestIpetCrawler:
    """Test IpetCrawler implementation."""

    @pytest.fixture
    def mock_ipet_config(self):
        """Mock config for IpetCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        ipet_source = MagicMock()
        ipet_source.enabled = True
        ipet_source.base_url = "https://www.ipet.re.kr"

        config.crawler.sources = {"ipet": ipet_source}
        return config

    def test_initialization(self, mock_ipet_config):
        """IpetCrawler should initialize with correct source_name."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ipet_config):
            crawler = IpetCrawler()
            assert crawler.source_name == "ipet"

    def test_extract_post_id(self, mock_ipet_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ipet_config):
            crawler = IpetCrawler()

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?nttId=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?seq=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?articleId=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/notice/123456") == "123456"

            # Hash fallback
            link = "https://example.com/some-page"
            expected_hash = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
            assert crawler._extract_post_id(link) == expected_hash

            # Empty link
            assert crawler._extract_post_id("") == ""

    def test_normalize_url(self, mock_ipet_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ipet_config):
            crawler = IpetCrawler()
            base_url = "https://www.ipet.re.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/index.do?boardId=notice", base_url) == "https://www.ipet.re.kr/index.do?boardId=notice"
            assert crawler._normalize_url("index.do", base_url) == "https://www.ipet.re.kr/index.do"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_ipet_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ipet_config):
            crawler = IpetCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_ipet_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_ipet_config):
            crawler = IpetCrawler()

            item = {
                "title": "농림식품 R&D 사업 공모",
                "link": "/boardView.do?nttId=99999",
                "author": "",
                "category": "R&D",
                "date": "2026-07-01 ~ 2026-07-31",
            }

            announcement = crawler._to_announcement(item, "https://www.ipet.re.kr")

            assert announcement is not None
            assert announcement.source == "ipet"
            assert announcement.title == "농림식품 R&D 사업 공모"
            assert announcement.url == "https://www.ipet.re.kr/boardView.do?nttId=99999"
            assert announcement.author == "농림식품기술기획평가원"  # default author
            assert announcement.category == "R&D"
            assert announcement.period_start == "2026-07-01"
            assert announcement.period_end == "2026-07-31"
            assert announcement.source_id == "99999"


# ============================================================
# ApfsCrawler Tests
# ============================================================

class TestApfsCrawler:
    """Test ApfsCrawler implementation."""

    @pytest.fixture
    def mock_apfs_config(self):
        """Mock config for ApfsCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        apfs_source = MagicMock()
        apfs_source.enabled = True
        apfs_source.base_url = "https://www.apfs.kr"

        config.crawler.sources = {"apfs": apfs_source}
        return config

    def test_initialization(self, mock_apfs_config):
        """ApfsCrawler should initialize with correct source_name."""
        with patch("alert.crawlers.base.get_config", return_value=mock_apfs_config):
            crawler = ApfsCrawler()
            assert crawler.source_name == "apfs"

    def test_extract_post_id(self, mock_apfs_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_apfs_config):
            crawler = ApfsCrawler()

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?nttId=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?seq=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?bbsId=BBSMSTR_001") == "BBSMSTR_001"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/bbs/123456") == "123456"

            # Hash fallback
            link = "https://example.com/some-page"
            expected_hash = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
            assert crawler._extract_post_id(link) == expected_hash

            # Empty link
            assert crawler._extract_post_id("") == ""

    def test_normalize_url(self, mock_apfs_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_apfs_config):
            crawler = ApfsCrawler()
            base_url = "https://www.apfs.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/usr/inform/bbs/view.do", base_url) == "https://www.apfs.kr/usr/inform/bbs/view.do"
            assert crawler._normalize_url("usr/inform/bbs/view.do", base_url) == "https://www.apfs.kr/usr/inform/bbs/view.do"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_apfs_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_apfs_config):
            crawler = ApfsCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_apfs_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_apfs_config):
            crawler = ApfsCrawler()

            item = {
                "title": "농업정책보험 안내 공고",
                "link": "/usr/inform/bbs/view.do?nttId=88888",
                "author": "",
                "category": "보험",
                "date": "2026-08-01 ~ 2026-08-31",
            }

            announcement = crawler._to_announcement(item, "https://www.apfs.kr")

            assert announcement is not None
            assert announcement.source == "apfs"
            assert announcement.title == "농업정책보험 안내 공고"
            assert announcement.url == "https://www.apfs.kr/usr/inform/bbs/view.do?nttId=88888"
            assert announcement.author == "농업정책보험금융원"  # default author
            assert announcement.category == "보험"
            assert announcement.period_start == "2026-08-01"
            assert announcement.period_end == "2026-08-31"
            assert announcement.source_id == "88888"
