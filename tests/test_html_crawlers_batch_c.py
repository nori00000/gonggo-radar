"""Tests for HTML crawlers Batch C: NongupGgCrawler, RdaCrawler, SemasCrawler."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.nongup_gg import NongupGgCrawler
from alert.crawlers.rda import RdaCrawler
from alert.crawlers.semas import SemasCrawler


# ============================================================
# NongupGgCrawler Tests
# ============================================================

class TestNongupGgCrawler:
    """Test NongupGgCrawler implementation."""

    @pytest.fixture
    def mock_nongup_gg_config(self):
        """Mock config for NongupGgCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        nongup_gg_source = MagicMock()
        nongup_gg_source.enabled = True
        nongup_gg_source.base_url = "https://nongup.gg.go.kr"

        config.crawler.sources = {"nongup_gg": nongup_gg_source}
        return config

    def test_initialization(self, mock_nongup_gg_config):
        """NongupGgCrawler should initialize with correct source_name."""
        with patch("alert.crawlers.base.get_config", return_value=mock_nongup_gg_config):
            crawler = NongupGgCrawler()
            assert crawler.source_name == "nongup_gg"

    def test_extract_post_id(self, mock_nongup_gg_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_nongup_gg_config):
            crawler = NongupGgCrawler()

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

    def test_normalize_url(self, mock_nongup_gg_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_nongup_gg_config):
            crawler = NongupGgCrawler()
            base_url = "https://nongup.gg.go.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/noti/35", base_url) == "https://nongup.gg.go.kr/noti/35"

            # Relative path
            assert crawler._normalize_url("noti/35", base_url) == "https://nongup.gg.go.kr/noti/35"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_nongup_gg_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_nongup_gg_config):
            crawler = NongupGgCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_nongup_gg_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_nongup_gg_config):
            crawler = NongupGgCrawler()

            item = {
                "title": "경기도 농업기술 교육 공고",
                "link": "/noti/35/view?seq=12345",
                "author": "경기도농업기술원",
                "category": "교육",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://nongup.gg.go.kr")

            assert announcement is not None
            assert announcement.source == "nongup_gg"
            assert announcement.title == "경기도 농업기술 교육 공고"
            assert announcement.url == "https://nongup.gg.go.kr/noti/35/view?seq=12345"
            assert announcement.author == "경기도농업기술원"
            assert announcement.category == "교육"
            # 11차(허용목록): 목록의 무라벨 범위는 기간이 아니다 - 게시일로만 남는다
            assert announcement.period_start is None
            assert announcement.period_end is None
            assert json.loads(announcement.raw_data)["posted"] == "2026-04-01"
            assert announcement.source_id == "12345"


# ============================================================
# RdaCrawler Tests
# ============================================================

class TestRdaCrawler:
    """Test RdaCrawler implementation."""

    @pytest.fixture
    def mock_rda_config(self):
        """Mock config for RdaCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        rda_source = MagicMock()
        rda_source.enabled = True
        rda_source.base_url = "https://www.rda.go.kr"

        config.crawler.sources = {"rda": rda_source}
        return config

    def test_initialization(self, mock_rda_config):
        """RdaCrawler should initialize with correct source_name."""
        with patch("alert.crawlers.base.get_config", return_value=mock_rda_config):
            crawler = RdaCrawler()
            assert crawler.source_name == "rda"

    def test_extract_post_id(self, mock_rda_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_rda_config):
            crawler = RdaCrawler()

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?dataNo=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?nttId=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?seq=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/board/123456") == "123456"

            # Hash fallback
            link = "https://example.com/some-page"
            expected_hash = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
            assert crawler._extract_post_id(link) == expected_hash

            # Empty link
            assert crawler._extract_post_id("") == ""

    def test_normalize_url(self, mock_rda_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_rda_config):
            crawler = RdaCrawler()
            base_url = "https://www.rda.go.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/board/view.do", base_url) == "https://www.rda.go.kr/board/view.do"
            assert crawler._normalize_url("board/view.do", base_url) == "https://www.rda.go.kr/board/view.do"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_rda_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_rda_config):
            crawler = RdaCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_rda_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_rda_config):
            crawler = RdaCrawler()

            item = {
                "title": "농촌진흥청 연구과제 공모",
                "link": "/board/board.do?mode=view&dataNo=67890",
                "author": "농촌진흥청",
                "category": "연구과제",
                "date": "2026-05-01 ~ 2026-05-31",
            }

            announcement = crawler._to_announcement(item, "https://www.rda.go.kr")

            assert announcement is not None
            assert announcement.source == "rda"
            assert announcement.title == "농촌진흥청 연구과제 공모"
            assert announcement.url == "https://www.rda.go.kr/board/board.do?mode=view&dataNo=67890"
            assert announcement.author == "농촌진흥청"
            assert announcement.category == "연구과제"
            assert announcement.period_start == "2026-05-01"
            assert announcement.period_end == "2026-05-31"
            assert announcement.source_id == "67890"


# ============================================================
# SemasCrawler Tests
# ============================================================

class TestSemasCrawler:
    """Test SemasCrawler implementation."""

    @pytest.fixture
    def mock_semas_config(self):
        """Mock config for SemasCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        semas_source = MagicMock()
        semas_source.enabled = True
        semas_source.base_url = "https://www.semas.or.kr"

        config.crawler.sources = {"semas": semas_source}
        return config

    def test_initialization(self, mock_semas_config):
        """SemasCrawler should initialize with correct source_name."""
        with patch("alert.crawlers.base.get_config", return_value=mock_semas_config):
            crawler = SemasCrawler()
            assert crawler.source_name == "semas"

    def test_extract_post_id(self, mock_semas_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_semas_config):
            crawler = SemasCrawler()

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?seq=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?nttId=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?articleId=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "123456"

            # Hash fallback
            link = "https://example.com/some-page"
            expected_hash = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
            assert crawler._extract_post_id(link) == expected_hash

            # Empty link
            assert crawler._extract_post_id("") == ""

    def test_normalize_url(self, mock_semas_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_semas_config):
            crawler = SemasCrawler()
            base_url = "https://www.semas.or.kr"

            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("/board/view.do", base_url) == "https://www.semas.or.kr/board/view.do"
            assert crawler._normalize_url("board/view.do", base_url) == "https://www.semas.or.kr/board/view.do"
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_semas_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_semas_config):
            crawler = SemasCrawler()

            assert crawler._normalize_date("2026-04-01") == "2026-04-01"
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"
            assert crawler._normalize_date("20260401") == "2026-04-01"
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"
            assert crawler._normalize_date("") is None
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_semas_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_semas_config):
            crawler = SemasCrawler()

            item = {
                "title": "소상공인 지원사업 공고",
                "link": "/board/boardView.do?seq=11111",
                "author": "",
                "category": "지원사업",
                "date": "2026-06-01 ~ 2026-06-30",
            }

            announcement = crawler._to_announcement(item, "https://www.semas.or.kr")

            assert announcement is not None
            assert announcement.source == "semas"
            assert announcement.title == "소상공인 지원사업 공고"
            assert announcement.url == "https://www.semas.or.kr/board/boardView.do?seq=11111"
            assert announcement.author == "소상공인시장진흥공단"  # default author
            assert announcement.category == "지원사업"
            assert announcement.period_start == "2026-06-01"
            assert announcement.period_end == "2026-06-30"
            assert announcement.source_id == "11111"
