"""Tests for alert/crawlers/ (BaseCrawler and specific crawlers)."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from alert.crawlers.base import BaseCrawler
from alert.crawlers.bizinfo import BizinfoCrawler
from alert.crawlers.g2b import G2bCrawler
from alert.crawlers.kstartup import KStartupCrawler
from alert.crawlers.smes import SmesCrawler
from alert.models import RawAnnouncement


@pytest.fixture
def mock_crawler_config():
    """Mock crawler config."""
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 3
    config.crawler.retry_delay = 1.0
    config.crawler.user_agent = "test-user-agent"
    config.crawler.sources = {
        "test_source": MagicMock(enabled=True, base_url="https://example.com"),
        "disabled_source": MagicMock(enabled=False, base_url="https://example.com"),
    }
    return config


class ConcreteCrawler(BaseCrawler):
    """Concrete implementation of BaseCrawler for testing."""

    def fetch(self):
        """Dummy implementation."""
        return [
            RawAnnouncement(
                source=self.source_name,
                source_id="test-001",
                title="Test Announcement",
                url="https://example.com/test-001",
                summary="Test summary",
                author="Test Author",
                category="Test Category",
                target="Test Target",
            )
        ]


class TestBaseCrawler:
    """Test BaseCrawler abstract base class."""

    def test_initialization(self, mock_crawler_config):
        """BaseCrawler should initialize with config values."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            with patch("alert.crawlers.base.setup_logger") as mock_logger:
                crawler = ConcreteCrawler(source_name="test_source")

                assert crawler.source_name == "test_source"
                assert crawler.timeout == 10
                assert crawler.retry_count == 3
                assert crawler.retry_delay == 1.0
                assert crawler.user_agent == "test-user-agent"
                assert isinstance(crawler.session, requests.Session)
                mock_logger.assert_called_once_with("crawler.test_source")

    def test_session_headers(self, mock_crawler_config):
        """BaseCrawler should set User-Agent header on session."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            assert "User-Agent" in crawler.session.headers
            assert crawler.session.headers["User-Agent"] == "test-user-agent"
            assert "Accept" in crawler.session.headers

    def test_fetch_is_abstract(self, mock_crawler_config):
        """fetch() must be implemented by subclass."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            # Cannot instantiate BaseCrawler directly (abstract)
            with pytest.raises(TypeError):
                BaseCrawler(source_name="test")

    def test_get_with_retry_success(self, mock_crawler_config):
        """get() should succeed on first attempt."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            mock_response = MagicMock()
            mock_response.status_code = 200

            with patch.object(crawler.session, "request", return_value=mock_response) as mock_request:
                response = crawler.get("https://example.com/test")

                assert response == mock_response
                mock_request.assert_called_once_with(
                    "GET", "https://example.com/test", timeout=10
                )

    def test_get_with_retry_http_error_retries(self, mock_crawler_config):
        """get() should retry on HTTP errors."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            with patch.object(crawler.session, "request") as mock_request:
                mock_request.side_effect = requests.RequestException("Network error")

                with patch("alert.crawlers.base.time.sleep"):  # Skip actual delay
                    response = crawler.get("https://example.com/test")

                assert response is None
                # Should retry 3 times
                assert mock_request.call_count == 3

    def test_get_with_retry_success_on_second_attempt(self, mock_crawler_config):
        """get() should succeed on retry."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            mock_response = MagicMock()
            mock_response.status_code = 200

            with patch.object(crawler.session, "request") as mock_request:
                # Fail first, succeed second
                mock_request.side_effect = [
                    requests.RequestException("Temporary error"),
                    mock_response
                ]

                with patch("alert.crawlers.base.time.sleep"):
                    response = crawler.get("https://example.com/test")

                assert response == mock_response
                assert mock_request.call_count == 2

    def test_post_with_retry(self, mock_crawler_config):
        """post() should work with retry logic."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            mock_response = MagicMock()
            mock_response.status_code = 200

            with patch.object(crawler.session, "request", return_value=mock_response) as mock_request:
                response = crawler.post("https://example.com/test", json={"key": "value"})

                assert response == mock_response
                mock_request.assert_called_once_with(
                    "POST", "https://example.com/test", json={"key": "value"}, timeout=10
                )

    def test_is_enabled_returns_true_for_enabled_source(self, mock_crawler_config):
        """is_enabled() should return True for enabled source."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            assert crawler.is_enabled() is True

    def test_is_enabled_returns_false_for_disabled_source(self, mock_crawler_config):
        """is_enabled() should return False for disabled source."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="disabled_source")

            assert crawler.is_enabled() is False

    def test_is_enabled_defaults_to_true_for_unknown_source(self, mock_crawler_config):
        """is_enabled() should default to True if source not in config."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="unknown_source")

            assert crawler.is_enabled() is True

    def test_get_base_url(self, mock_crawler_config):
        """get_base_url() should return base_url from config."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            assert crawler.get_base_url() == "https://example.com"

    def test_get_base_url_returns_empty_for_unknown_source(self, mock_crawler_config):
        """get_base_url() should return empty string if source not found."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="unknown_source")

            assert crawler.get_base_url() == ""

    def test_safe_fetch_success(self, mock_crawler_config):
        """safe_fetch() should return results on success."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            results = crawler.safe_fetch()

            assert len(results) == 1
            assert results[0].source_id == "test-001"

    def test_safe_fetch_skips_disabled_crawler(self, mock_crawler_config):
        """safe_fetch() should return empty list for disabled crawler."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="disabled_source")

            results = crawler.safe_fetch()

            assert results == []

    def test_safe_fetch_handles_exceptions(self, mock_crawler_config):
        """safe_fetch() should catch and log exceptions, return empty list."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            with patch.object(crawler, "fetch", side_effect=Exception("Fetch failed")):
                results = crawler.safe_fetch()

                assert results == []

    def test_request_with_retry_custom_timeout(self, mock_crawler_config):
        """_request_with_retry() should allow custom timeout."""
        with patch("alert.crawlers.base.get_config", return_value=mock_crawler_config):
            crawler = ConcreteCrawler(source_name="test_source")

            mock_response = MagicMock()
            mock_response.status_code = 200

            with patch.object(crawler.session, "request", return_value=mock_response) as mock_request:
                crawler.get("https://example.com/test", timeout=5)

                # Should use custom timeout
                assert mock_request.call_args[1]["timeout"] == 5


class TestBizinfoCrawler:
    """Test BizinfoCrawler implementation."""

    @pytest.fixture
    def mock_bizinfo_config(self):
        """Mock config for BizinfoCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        bizinfo_source = MagicMock()
        bizinfo_source.enabled = True
        bizinfo_source.base_url = "https://www.bizinfo.go.kr"
        bizinfo_source.api_key = "test-api-key"
        bizinfo_source.search_cnt = 50

        config.crawler.sources = {"bizinfo": bizinfo_source}
        return config

    def test_initialization_with_api_key(self, mock_bizinfo_config):
        """BizinfoCrawler should get API key from environment or config."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "env-api-key"}):
                crawler = BizinfoCrawler()

                assert crawler.source_name == "bizinfo"
                assert crawler.api_key == "env-api-key"
                assert crawler.search_cnt == 50

    def test_initialization_without_api_key_graceful(self, mock_bizinfo_config):
        """BizinfoCrawler should gracefully handle missing API key."""
        # Remove api_key from config
        mock_bizinfo_config.crawler.sources["bizinfo"].api_key = ""

        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {}, clear=True):
                crawler = BizinfoCrawler()
                assert crawler.api_key == ""
                assert crawler.fetch() == []

    def test_fetch_success(self, mock_bizinfo_config):
        """fetch() should parse API response and return announcements."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                # Mock API response
                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "response": {
                        "header": {"resultCode": "00"},
                        "body": {
                            "items": {
                                "item": [
                                    {
                                        "pblancId": "001",
                                        "pblancNm": "스마트팜 지원사업",
                                        "detailUrl": "https://example.com/001",
                                        "sbjctCn": "스마트팜 지원",
                                        "jrsdInsttNm": "농림축산식품부",
                                        "pldirSportRealmLclasCodeNm": "농업",
                                        "trgetNm": "농업인",
                                        "reqstBeginEndDe": "20260401~20260430",
                                    }
                                ]
                            }
                        }
                    }
                }

                with patch.object(crawler, "get", return_value=mock_response):
                    results = crawler.fetch()

                    assert len(results) == 1
                    assert results[0].source == "bizinfo"
                    assert results[0].source_id == "001"
                    assert results[0].title == "스마트팜 지원사업"
                    assert results[0].url == "https://example.com/001"
                    assert results[0].period_start == "2026-04-01"
                    assert results[0].period_end == "2026-04-30"

    def test_fetch_handles_http_error(self, mock_bizinfo_config):
        """fetch() should return empty list on HTTP error."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                with patch.object(crawler, "get", return_value=None):
                    results = crawler.fetch()

                    assert results == []

    def test_fetch_handles_json_decode_error(self, mock_bizinfo_config):
        """fetch() should handle JSON decode errors gracefully."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                mock_response = MagicMock()
                mock_response.json.side_effect = json.JSONDecodeError("Invalid JSON", "", 0)
                mock_response.text = "invalid json"

                with patch.object(crawler, "get", return_value=mock_response):
                    results = crawler.fetch()

                    assert results == []

    def test_fetch_handles_timeout(self, mock_bizinfo_config):
        """fetch() should handle timeout gracefully."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                with patch.object(crawler, "get", side_effect=requests.Timeout("Timeout")):
                    with pytest.raises(requests.Timeout):
                        crawler.fetch()

    def test_fetch_handles_500_error(self, mock_bizinfo_config):
        """fetch() should handle 500 error gracefully."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                # get() returns None on error (handled by BaseCrawler)
                with patch.object(crawler, "get", return_value=None):
                    results = crawler.fetch()

                    assert results == []

    def test_extract_items_various_formats(self, mock_bizinfo_config):
        """_extract_items() should handle various response formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                # Format 1: items.item as list
                data1 = {
                    "response": {
                        "body": {
                            "items": {"item": [{"id": "1"}, {"id": "2"}]}
                        }
                    }
                }
                items1 = crawler._extract_items(data1)
                assert len(items1) == 2

                # Format 2: items.item as single dict
                data2 = {
                    "response": {
                        "body": {
                            "items": {"item": {"id": "1"}}
                        }
                    }
                }
                items2 = crawler._extract_items(data2)
                assert len(items2) == 1

                # Format 3: items as list directly
                data3 = {
                    "response": {
                        "body": {
                            "items": [{"id": "1"}, {"id": "2"}]
                        }
                    }
                }
                items3 = crawler._extract_items(data3)
                assert len(items3) == 2

    def test_parse_item_missing_required_fields(self, mock_bizinfo_config):
        """_parse_item() should return None if required fields missing."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                # Missing title
                item = {
                    "pblancId": "001",
                    "detailUrl": "https://example.com/001",
                }

                result = crawler._parse_item(item)
                assert result is None

    def test_parse_period_formats(self, mock_bizinfo_config):
        """_parse_period() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                # Format: YYYYMMDD~YYYYMMDD
                start, end = crawler._parse_period("20260401~20260430")
                assert start == "2026-04-01"
                assert end == "2026-04-30"

                # Format: Single date
                start, end = crawler._parse_period("20260401")
                assert start == "2026-04-01"
                assert end == "2026-04-01"

                # Format: With hyphens
                start, end = crawler._parse_period("2026-04-01~2026-04-30")
                assert start == "2026-04-01"
                assert end == "2026-04-30"

                # Empty string
                start, end = crawler._parse_period("")
                assert start is None
                assert end is None

    def test_normalize_date(self, mock_bizinfo_config):
        """_normalize_date() should normalize date to ISO format."""
        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {"BIZINFO_API_KEY": "test-key"}):
                crawler = BizinfoCrawler()

                # YYYYMMDD format
                assert crawler._normalize_date("20260401") == "2026-04-01"

                # Already ISO format
                assert crawler._normalize_date("2026-04-01") == "2026-04-01"

                # Empty string
                assert crawler._normalize_date("") is None

                # Invalid format
                assert crawler._normalize_date("invalid") is None


class TestG2bCrawler:
    """Test G2bCrawler implementation."""

    @pytest.fixture
    def mock_g2b_config(self):
        """Mock config for G2bCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        g2b_source = MagicMock()
        g2b_source.enabled = True
        g2b_source.base_url = "http://www.g2b.go.kr"
        g2b_source.api_key = "test-api-key"
        g2b_source.lookback_days = 7

        config.crawler.sources = {"g2b": g2b_source}
        return config

    def test_initialization_without_api_key_graceful(self, mock_g2b_config):
        """G2bCrawler should gracefully handle missing API key."""
        mock_g2b_config.crawler.sources["g2b"].api_key = ""

        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {}, clear=True):
                crawler = G2bCrawler()
                assert crawler.api_key == ""
                assert crawler.fetch() == []

    def test_initialization_with_api_key_from_env(self, mock_g2b_config):
        """G2bCrawler should get API key from environment."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "env-api-key"}):
                crawler = G2bCrawler()

                assert crawler.source_name == "g2b"
                assert crawler.api_key == "env-api-key"
                assert crawler.lookback_days == 7

    def test_fetch_queries_all_operations(self, mock_g2b_config):
        """fetch() should query all 3 operation types (용역/물품/공사)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                # Mock API responses for all operations
                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "response": {
                        "header": {"resultCode": "00"},
                        "body": {"items": []},
                    }
                }

                with patch.object(crawler, "get", return_value=mock_response) as mock_get:
                    crawler.fetch()

                    # Should call get 3 times (once for each operation)
                    assert mock_get.call_count == 3
                    # Verify all operation paths are called
                    called_urls = [call.args[0] for call in mock_get.call_args_list]
                    assert all("BidPublicInfoService" in url for url in called_urls)

    def test_extract_items_with_valid_response(self, mock_g2b_config):
        """_extract_items() should extract items from valid API response."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                data = {
                    "response": {
                        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                        "body": {
                            "items": [
                                {"bidNtceNo": "001", "bidNtceNm": "Test Bid 1"},
                                {"bidNtceNo": "002", "bidNtceNm": "Test Bid 2"},
                            ],
                            "totalCount": 2,
                        }
                    }
                }

                items = crawler._extract_items(data)
                assert len(items) == 2
                assert items[0]["bidNtceNo"] == "001"
                assert items[1]["bidNtceNo"] == "002"

    def test_extract_items_with_error_response(self, mock_g2b_config):
        """_extract_items() should return empty list for non-00 resultCode."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                data = {
                    "response": {
                        "header": {"resultCode": "99", "resultMsg": "Service Error"},
                        "body": {"items": []},
                    }
                }

                items = crawler._extract_items(data)
                assert items == []

    def test_parse_item_with_valid_data(self, mock_g2b_config):
        """_parse_item() should parse valid item data."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                item = {
                    "bidNtceNo": "20260101-001",
                    "bidNtceNm": "스마트팜 설치 입찰",
                    "bidNtceUrl": "http://www.g2b.go.kr/test/001",
                    "ntceKindNm": "일반경쟁입찰",
                    "presmptPrce": "100,000,000",
                    "ntceInsttNm": "농림축산식품부",
                    "dminsttNm": "농업기술센터",
                    "bidBeginDt": "202604010900",
                    "bidClseDt": "202604301800",
                }

                announcement = crawler._parse_item(item, "용역")

                assert announcement is not None
                assert announcement.source == "g2b"
                assert announcement.source_id == "20260101-001"
                assert announcement.title == "스마트팜 설치 입찰"
                assert announcement.url == "http://www.g2b.go.kr/test/001"
                assert announcement.category == "용역"
                assert announcement.author == "농림축산식품부"
                assert announcement.target == "농업기술센터"
                assert announcement.period_start == "2026-04-01"
                assert announcement.period_end == "2026-04-30"

    def test_parse_item_missing_required_fields(self, mock_g2b_config):
        """_parse_item() should return None if required fields are missing."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                # Missing title (bidNtceNm)
                item = {
                    "bidNtceNo": "001",
                    "bidNtceUrl": "http://www.g2b.go.kr/test/001",
                }

                result = crawler._parse_item(item, "용역")
                assert result is None

    def test_parse_datetime_with_various_formats(self, mock_g2b_config):
        """_parse_datetime() should handle various datetime formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                # YYYYMMDDHHmm format
                assert crawler._parse_datetime("202604010900") == "2026-04-01"

                # YYYYMMDD format (no time)
                assert crawler._parse_datetime("20260401") == "2026-04-01"

                # With non-digit characters
                assert crawler._parse_datetime("2026-04-01 09:00") == "2026-04-01"

                # Empty string
                assert crawler._parse_datetime("") is None

                # Invalid format (too short)
                assert crawler._parse_datetime("202604") is None

    def test_get_filter_keywords_uses_defaults(self, mock_g2b_config):
        """_get_filter_keywords() should return default keywords if config not set."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                keywords = crawler._get_filter_keywords()

                # Should return default keywords
                assert len(keywords) > 0
                assert "조경" in keywords
                assert "녹화" in keywords
                assert "스마트팜" in keywords

    def test_get_filter_keywords_uses_config(self, mock_g2b_config):
        """_get_filter_keywords() should use config keywords if set."""
        # Set custom keywords in config
        mock_g2b_config.crawler.sources["g2b"].filter_keywords = ["테스트", "키워드"]

        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                keywords = crawler._get_filter_keywords()

                assert keywords == ["테스트", "키워드"]

    def test_get_filter_keywords_returns_empty_when_config_empty(self, mock_g2b_config):
        """_get_filter_keywords() should return empty list when config explicitly sets empty."""
        # Set empty keywords in config (disable filtering)
        mock_g2b_config.crawler.sources["g2b"].filter_keywords = []

        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                keywords = crawler._get_filter_keywords()

                assert keywords == []

    def test_filter_by_keywords_keeps_matching_items(self, mock_g2b_config):
        """_filter_by_keywords() should keep items with matching keywords."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                # Create test announcements
                announcements = [
                    RawAnnouncement(
                        source="g2b",
                        source_id="001",
                        title="조경 공사 입찰",
                        url="http://example.com/001",
                        summary="공원 조경 설계",
                        author="Test",
                        category="공사",
                        target="Test",
                    ),
                    RawAnnouncement(
                        source="g2b",
                        source_id="002",
                        title="건물 신축 공사",
                        url="http://example.com/002",
                        summary="일반 건축물",
                        author="Test",
                        category="공사",
                        target="Test",
                    ),
                    RawAnnouncement(
                        source="g2b",
                        source_id="003",
                        title="스마트팜 시설 설치",
                        url="http://example.com/003",
                        summary="농업 시설 구축",
                        author="Test",
                        category="용역",
                        target="Test",
                    ),
                ]

                filtered = crawler._filter_by_keywords(announcements)

                # Should keep items with matching keywords
                assert len(filtered) == 2
                assert filtered[0].source_id == "001"  # Has "조경"
                assert filtered[1].source_id == "003"  # Has "스마트팜"

    def test_filter_by_keywords_removes_non_matching_items(self, mock_g2b_config):
        """_filter_by_keywords() should remove items without matching keywords."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                announcements = [
                    RawAnnouncement(
                        source="g2b",
                        source_id="001",
                        title="일반 건축 공사",
                        url="http://example.com/001",
                        summary="건물 신축",
                        author="Test",
                        category="공사",
                        target="Test",
                    ),
                    RawAnnouncement(
                        source="g2b",
                        source_id="002",
                        title="도로 보수 공사",
                        url="http://example.com/002",
                        summary="도로 포장",
                        author="Test",
                        category="공사",
                        target="Test",
                    ),
                ]

                filtered = crawler._filter_by_keywords(announcements)

                # No items should match
                assert len(filtered) == 0

    def test_filter_by_keywords_no_filtering_when_empty_keywords(self, mock_g2b_config):
        """_filter_by_keywords() should return all items when no keywords configured."""
        # Set empty keywords in config (disable filtering)
        mock_g2b_config.crawler.sources["g2b"].filter_keywords = []

        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                announcements = [
                    RawAnnouncement(
                        source="g2b",
                        source_id="001",
                        title="일반 건축 공사",
                        url="http://example.com/001",
                        summary="건물 신축",
                        author="Test",
                        category="공사",
                        target="Test",
                    ),
                    RawAnnouncement(
                        source="g2b",
                        source_id="002",
                        title="도로 보수 공사",
                        url="http://example.com/002",
                        summary="도로 포장",
                        author="Test",
                        category="공사",
                        target="Test",
                    ),
                ]

                filtered = crawler._filter_by_keywords(announcements)

                # All items should be returned (no filtering)
                assert len(filtered) == 2

    def test_filter_by_keywords_checks_title_and_summary(self, mock_g2b_config):
        """_filter_by_keywords() should check both title and summary."""
        with patch("alert.crawlers.base.get_config", return_value=mock_g2b_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = G2bCrawler()

                announcements = [
                    RawAnnouncement(
                        source="g2b",
                        source_id="001",
                        title="일반 공사",  # No keyword
                        url="http://example.com/001",
                        summary="공고종류: 녹화 공사",  # Has keyword "녹화"
                        author="Test",
                        category="공사",
                        target="Test",
                    ),
                    RawAnnouncement(
                        source="g2b",
                        source_id="002",
                        title="정원 조성 공사",  # Has keyword "정원"
                        url="http://example.com/002",
                        summary="일반 공사",  # No keyword
                        author="Test",
                        category="공사",
                        target="Test",
                    ),
                ]

                filtered = crawler._filter_by_keywords(announcements)

                # Both should match (one in summary, one in title)
                assert len(filtered) == 2


class TestKStartupCrawler:
    """Test KStartupCrawler implementation."""

    @pytest.fixture
    def mock_kstartup_config(self):
        """Mock config for KStartupCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        kstartup_source = MagicMock()
        kstartup_source.enabled = True
        kstartup_source.base_url = "https://www.k-startup.go.kr"
        kstartup_source.api_key = "test-api-key"
        kstartup_source.per_page = 100

        config.crawler.sources = {"kstartup": kstartup_source}
        return config

    def test_initialization_without_api_key_graceful(self, mock_kstartup_config):
        """KStartupCrawler should gracefully handle missing API key."""
        mock_kstartup_config.crawler.sources["kstartup"].api_key = ""

        with patch("alert.crawlers.base.get_config", return_value=mock_kstartup_config):
            with patch.dict("os.environ", {}, clear=True):
                crawler = KStartupCrawler()
                assert crawler.api_key == ""
                assert crawler.fetch() == []

    def test_initialization_with_api_key_from_env(self, mock_kstartup_config):
        """KStartupCrawler should get API key from environment."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kstartup_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "env-api-key"}):
                crawler = KStartupCrawler()

                assert crawler.source_name == "kstartup"
                assert crawler.api_key == "env-api-key"
                assert crawler.per_page == 100

    def test_fetch_with_mocked_api_response(self, mock_kstartup_config):
        """fetch() should parse API response and return announcements."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kstartup_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = KStartupCrawler()

                # Mock API response
                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "currentCount": 2,
                    "totalCount": 2,
                    "data": [
                        {
                            "pbanc_sn": "12345",
                            "biz_pbanc_nm": "창업지원사업 공고",
                            "detl_pg_url": "https://www.k-startup.go.kr/detail/12345",
                            "aply_trgt_ctnt": "예비창업자, 초기창업자",
                            "sprv_inst": "중소벤처기업부",
                            "supt_biz_clsfc": "창업지원",
                            "aply_trgt": "예비창업자",
                            "pbanc_rcpt_bgng_dt": "20260401",
                            "pbanc_rcpt_end_dt": "20260430",
                        }
                    ]
                }

                with patch.object(crawler, "get", return_value=mock_response):
                    results = crawler.fetch()

                    assert len(results) == 1
                    assert results[0].source == "kstartup"
                    assert results[0].source_id == "12345"
                    assert results[0].title == "창업지원사업 공고"
                    assert results[0].period_start == "2026-04-01"
                    assert results[0].period_end == "2026-04-30"

    def test_extract_items_from_data_key(self, mock_kstartup_config):
        """_extract_items() should extract items from 'data' key."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kstartup_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = KStartupCrawler()

                data = {
                    "currentCount": 2,
                    "totalCount": 2,
                    "data": [
                        {"pbanc_sn": "001", "biz_pbanc_nm": "Test 1"},
                        {"pbanc_sn": "002", "biz_pbanc_nm": "Test 2"},
                    ]
                }

                items = crawler._extract_items(data)
                assert len(items) == 2
                assert items[0]["pbanc_sn"] == "001"
                assert items[1]["pbanc_sn"] == "002"

    def test_parse_item_with_valid_data(self, mock_kstartup_config):
        """_parse_item() should parse valid item data."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kstartup_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = KStartupCrawler()

                item = {
                    "pbanc_sn": "12345",
                    "biz_pbanc_nm": "창업지원사업 공고",
                    "detl_pg_url": "https://www.k-startup.go.kr/detail/12345",
                    "aply_trgt_ctnt": "예비창업자, 초기창업자",
                    "sprv_inst": "중소벤처기업부",
                    "supt_biz_clsfc": "창업지원",
                    "aply_trgt": "예비창업자",
                    "pbanc_rcpt_bgng_dt": "20260401",
                    "pbanc_rcpt_end_dt": "20260430",
                }

                announcement = crawler._parse_item(item)

                assert announcement is not None
                assert announcement.source == "kstartup"
                assert announcement.source_id == "12345"
                assert announcement.title == "창업지원사업 공고"
                assert announcement.url == "https://www.k-startup.go.kr/detail/12345"
                assert announcement.summary == "예비창업자, 초기창업자"
                assert announcement.author == "중소벤처기업부"
                assert announcement.category == "창업지원"
                assert announcement.target == "예비창업자"

    def test_parse_item_generates_hash_source_id_when_empty(self, mock_kstartup_config):
        """_parse_item() should generate hash source_id when pbanc_sn is empty."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kstartup_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = KStartupCrawler()

                item = {
                    "pbanc_sn": "",  # Empty source ID
                    "biz_pbanc_nm": "창업지원사업 공고",
                    "detl_pg_url": "https://www.k-startup.go.kr/detail/12345",
                    "pbanc_rcpt_bgng_dt": "20260401",
                    "pbanc_rcpt_end_dt": "20260430",
                }

                announcement = crawler._parse_item(item)

                assert announcement is not None
                assert announcement.source_id != ""
                assert len(announcement.source_id) == 16  # MD5 hash truncated to 16 chars

    def test_normalize_date_with_yyyymmdd_format(self, mock_kstartup_config):
        """_normalize_date() should handle YYYYMMDD format."""
        with patch("alert.crawlers.base.get_config", return_value=mock_kstartup_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = KStartupCrawler()

                # YYYYMMDD format
                assert crawler._normalize_date("20260401") == "2026-04-01"

                # Already ISO format
                assert crawler._normalize_date("2026-04-01") == "2026-04-01"

                # Empty string
                assert crawler._normalize_date("") is None

                # Invalid format
                assert crawler._normalize_date("invalid") is None


class TestSmesCrawler:
    """Test SmesCrawler implementation."""

    @pytest.fixture
    def mock_smes_config(self):
        """Mock config for SmesCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        smes_source = MagicMock()
        smes_source.enabled = True
        smes_source.base_url = "https://www.smes.go.kr"

        config.crawler.sources = {"smes": smes_source}
        return config

    def test_initialization_without_api_key(self, mock_smes_config):
        """SmesCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_smes_config):
            # Should not raise error even without API key
            crawler = SmesCrawler()

            assert crawler.source_name == "smes"

    def test_extract_post_id_with_various_patterns(self, mock_smes_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_smes_config):
            crawler = SmesCrawler()

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?announcementId=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?notifyId=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?seq=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "123456"

            # Empty link should return empty string
            assert crawler._extract_post_id("") == ""

    def test_normalize_url_with_relative_and_absolute_urls(self, mock_smes_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_smes_config):
            crawler = SmesCrawler()

            base_url = "https://www.smes.go.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/test/path", base_url) == "https://www.smes.go.kr/test/path"

            # Relative path
            assert crawler._normalize_url("test/path", base_url) == "https://www.smes.go.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_parse_period_with_separator(self, mock_smes_config):
        """_parse_period() should parse period with ~ separator."""
        with patch("alert.crawlers.base.get_config", return_value=mock_smes_config):
            crawler = SmesCrawler()

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

    def test_parse_period_with_single_date(self, mock_smes_config):
        """_parse_period() should handle single date."""
        with patch("alert.crawlers.base.get_config", return_value=mock_smes_config):
            crawler = SmesCrawler()

            # Single date
            start, end = crawler._parse_period("2026-04-01")
            assert start == "2026-04-01"
            assert end == "2026-04-01"

            # Empty string
            start, end = crawler._parse_period("")
            assert start is None
            assert end is None

    def test_normalize_date_with_various_formats(self, mock_smes_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_smes_config):
            crawler = SmesCrawler()

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

    def test_to_announcement_with_valid_item(self, mock_smes_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_smes_config):
            crawler = SmesCrawler()

            item = {
                "title": "중소기업 지원사업 공고",
                "link": "/main/front/notify/view.do?announcementId=12345",
                "author": "중소벤처기업부",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.smes.go.kr")

            assert announcement is not None
            assert announcement.source == "smes"
            assert announcement.title == "중소기업 지원사업 공고"
            assert announcement.url == "https://www.smes.go.kr/main/front/notify/view.do?announcementId=12345"
            assert announcement.author == "중소벤처기업부"
            assert announcement.category == "지원사업"
            assert announcement.period_start == "2026-04-01"
            assert announcement.period_end == "2026-04-30"
            assert announcement.source_id == "12345"

    def test_to_announcement_with_empty_title_returns_none(self, mock_smes_config):
        """_to_announcement() should return None if title is empty."""
        with patch("alert.crawlers.base.get_config", return_value=mock_smes_config):
            crawler = SmesCrawler()

            item = {
                "title": "",
                "link": "/main/front/notify/view.do?announcementId=12345",
                "author": "중소벤처기업부",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            announcement = crawler._to_announcement(item, "https://www.smes.go.kr")

            assert announcement is None


# ---------------------------------------------------------------------------
# P2-X H1. 크롤러 전면 실패가 run_history 에 error 로 남는다
# ---------------------------------------------------------------------------

class _SilentFailureCrawler(BaseCrawler):
    """semas 를 본뜬 크롤러 — 목록 fetch 실패를 삼키고 ``[]`` 를 돌려준다."""

    def fetch(self):
        self.get("https://example.com/board")     # None 을 받고 조용히 끝낸다
        return []


class _BoomCrawler(BaseCrawler):
    def fetch(self):
        raise RuntimeError("파서가 터졌다")


class _PartialCrawler(BaseCrawler):
    """한 페이지는 실패하고 한 페이지는 성공 — 부분 수집은 정상이다."""

    def fetch(self):
        self.get("https://example.com/dead")
        return [RawAnnouncement(source=self.source_name, source_id="a",
                                title="살아있는 공고",
                                url="https://example.com/a")]


def _crawler(cls, config, name="test_source"):
    with patch("alert.crawlers.base.get_config", return_value=config):
        return cls(source_name=name)


class TestFetchErrorRecording:
    """실패 사유가 크롤러에 남는다 — 로그만 남기면 파이프라인이 못 본다."""

    def test_all_retries_failed_records_a_reason(self, mock_crawler_config):
        crawler = _crawler(_SilentFailureCrawler, mock_crawler_config)
        with patch.object(crawler.session, "request",
                          side_effect=requests.ConnectionError("SSL handshake")):
            with patch("alert.crawlers.base.time.sleep"):
                assert crawler.safe_fetch() == []
        assert crawler.fetch_errors
        assert "SSL handshake" in crawler.fetch_error_summary()
        assert "https://example.com/board" in crawler.fetch_error_summary()

    def test_exception_in_fetch_records_a_reason(self, mock_crawler_config):
        crawler = _crawler(_BoomCrawler, mock_crawler_config)
        assert crawler.safe_fetch() == []
        assert "RuntimeError: 파서가 터졌다" in crawler.fetch_error_summary()

    def test_successful_run_records_nothing(self, mock_crawler_config):
        crawler = _crawler(ConcreteCrawler, mock_crawler_config)
        assert len(crawler.safe_fetch()) == 1
        assert crawler.fetch_errors == []
        assert crawler.fetch_error_summary() == ""

    def test_reasons_reset_between_runs(self, mock_crawler_config):
        """같은 객체를 다시 돌리면 지난 실행의 사유가 남지 않는다."""
        crawler = _crawler(_BoomCrawler, mock_crawler_config)
        crawler.safe_fetch()
        crawler.fetch = lambda: []
        assert crawler.safe_fetch() == []
        assert crawler.fetch_errors == []

    def test_summary_caps_the_number_of_reasons(self, mock_crawler_config):
        crawler = _crawler(ConcreteCrawler, mock_crawler_config)
        for i in range(5):
            crawler.record_fetch_error(f"오류{i}")
        summary = crawler.fetch_error_summary(limit=3)
        assert "오류0" in summary and "오류4" not in summary
        assert "외 2건" in summary


class TestCrawlSingleStatus:
    """``alert.main._crawl_single`` — 0건 + 사유 = error (감사 V H-1)."""

    def _run(self, cls, config, name="test_source"):
        import logging

        from alert.main import _crawl_single

        with patch("alert.crawlers.base.get_config", return_value=config):
            with patch("alert.crawlers.base.time.sleep"):
                return _crawl_single(
                    name, lambda: cls(source_name=name),
                    logging.getLogger("test"))

    def test_total_failure_is_recorded_as_error(self, mock_crawler_config):
        """semas 형 침묵 실패: 예외가 올라오지 않아도 status='error'."""
        with patch.object(requests.Session, "request",
                          side_effect=requests.ConnectionError("SSLV3_ALERT")):
            name, raw, status, error = self._run(
                _SilentFailureCrawler, mock_crawler_config)
        assert (name, raw, status) == ("test_source", [], "error")
        assert "SSLV3_ALERT" in error

    def test_error_message_is_per_source_not_a_global_copy(
        self, mock_crawler_config
    ):
        """소스마다 **자기 사유**를 갖는다 — 전역 문장 복사 회귀.

        감사 V M-2 가 잡은 것과 같은 결함이다(notified_count 가 전역값을 모든
        소스 행에 복사했다). 사유가 한 문장으로 뭉개지면 SSL 거부·인증서 오류·
        타임아웃을 표에서 구별할 수 없다.
        """
        with patch.object(requests.Session, "request",
                          side_effect=requests.ConnectionError("SSLV3_ALERT")):
            _n1, _r1, _s1, first = self._run(
                _SilentFailureCrawler, mock_crawler_config, "test_source")
        _n2, _r2, _s2, second = self._run(_BoomCrawler, mock_crawler_config)
        assert first != second
        assert "SSLV3_ALERT" in first
        assert "RuntimeError" in second
        assert "crawl failed (see log above)" not in (first + second)

    def test_partial_failure_is_still_success(self, mock_crawler_config):
        """한 건이라도 가져왔으면 성공이다 — 부분 수집은 정상 운영이다."""
        real_request = requests.Session.request

        def selective(self, method, url, **kwargs):
            if "dead" in url:
                raise requests.ConnectionError("timeout")
            return real_request(self, method, url, **kwargs)

        with patch.object(requests.Session, "request", selective):
            name, raw, status, error = self._run(
                _PartialCrawler, mock_crawler_config)
        assert status == "success" and error == "" and len(raw) == 1

    def test_empty_but_healthy_source_stays_success(self, mock_crawler_config):
        """진짜 무공고는 오류가 아니다 — 사유가 없으면 success."""

        class _Empty(BaseCrawler):
            def fetch(self):
                return []

        name, raw, status, error = self._run(_Empty, mock_crawler_config)
        assert (status, error) == ("success", "")

    def test_disabled_source_is_not_an_error(self, mock_crawler_config):
        name, raw, status, error = self._run(
            ConcreteCrawler, mock_crawler_config, "disabled_source")
        assert (status, error) == ("disabled", "")


def test_run_pipeline_wires_the_per_source_error_message_into_run_history():
    """배선 회귀 (P2-X H1) — 소스별 사유가 run_history 까지 간다.

    ``run_pipeline`` 은 네트워크·DB·텔레그램을 한 번에 쓰므로 단위 테스트로
    돌릴 수 없다. 대신 **구조**를 고정한다: ①크롤 결과를 4-튜플로 받고
    ②error 분기의 ``error_message`` 가 그 변수에서 오며(문자열 리터럴이 아니라)
    ③``insert_run`` 이 그 값을 ``error_msg`` 로 넘긴다.
    이 셋 중 하나만 끊겨도 실패가 다시 조용해진다.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent
              / "alert" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    pipeline = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_pipeline")

    # ① 4-튜플 언팩 (crawler_name, raw, status, error_message)
    unpacks = [
        node for node in ast.walk(pipeline)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Tuple)
        and len(node.targets[0].elts) == 4
        and {getattr(el, "id", None) for el in node.targets[0].elts}
        == {"crawler_name", "raw_announcements", "status", "error_message"}
    ]
    assert unpacks, "크롤 결과를 4-튜플로 받지 않는다"

    # ② error 분기의 error_message 가 변수에서 온다
    error_dicts = [
        node for node in ast.walk(pipeline)
        if isinstance(node, ast.Dict)
        and any(isinstance(key, ast.Constant) and key.value == "error_message"
                for key in node.keys)
    ]
    assert error_dicts, "error_message 를 담는 run_stats 항목이 없다"
    from_variable = []
    for node in error_dicts:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "error_message":
                from_variable.append(
                    any(isinstance(inner, ast.Name)
                        for inner in ast.walk(value)))
    assert any(from_variable), "error_message 가 전부 리터럴이다(전역 문장 복사)"

    # ③ insert_run 이 그 값을 넘긴다
    inserts = [
        node for node in ast.walk(pipeline)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "insert_run"
    ]
    assert inserts, "insert_run 호출이 없다"
    assert any(
        keyword.arg == "error_msg"
        and "error_message" in ast.unparse(keyword.value)
        for call in inserts for keyword in call.keywords
    ), "insert_run 이 error_message 를 넘기지 않는다"


# ---------------------------------------------------------------------------
# P2-X 오류 4종 — 검증을 끄지 않고 고친다 (감사 V §4)
# ---------------------------------------------------------------------------

class TestSiteTlsPolicy:
    """`alert/crawlers/tls.py` — 사이트별 TLS 정책."""

    def test_context_keeps_verification_on(self):
        import ssl

        from alert.crawlers.tls import build_context

        ctx = build_context(legacy_security_level=True)
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_extra_ca_adds_to_the_default_store(self):
        """번들은 기본 저장소를 **대체하지 않고 더한다**."""
        from alert.crawlers.tls import CERTS_DIR, build_context

        base = len(build_context().get_ca_certs())
        widened = len(build_context(
            extra_ca_file=CERTS_DIR / "ggeea-intermediate.pem").get_ca_certs())
        assert widened == base + 1

    def test_missing_bundle_is_fail_closed(self):
        """선언한 번들이 없으면 예외다 — 조용히 검증을 낮추지 않는다."""
        from alert.crawlers.tls import CERTS_DIR, CertsNotFound, build_context

        with pytest.raises(CertsNotFound):
            build_context(extra_ca_file=CERTS_DIR / "없는번들.pem")

    def test_module_never_disables_verification(self):
        """코드에 CERT_NONE·verify=False·check_hostname=False 가 없다.

        문자열·주석이 아니라 **실행되는 코드**를 본다(AST) — 이 모듈의 존재
        이유가 "검증을 끄지 않고 고친다" 이므로 그 성질을 여기서 고정한다.
        """
        import ast
        from pathlib import Path

        tree = ast.parse((Path(__file__).resolve().parent.parent
                          / "alert" / "crawlers" / "tls.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "CERT_NONE", "검증을 끄는 상수가 있다"
            if isinstance(node, ast.keyword) and node.arg in (
                    "verify", "check_hostname"):
                assert not (isinstance(node.value, ast.Constant)
                            and node.value.value is False)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute)
                            and target.attr == "check_hostname"):
                        assert not (isinstance(node.value, ast.Constant)
                                    and node.value.value is False)


class TestSemasSecurityLevel:
    """semas — 클라이언트 SECLEVEL 이 원인, 검증 유지한 채 고친다."""

    def test_crawler_declares_legacy_security_level(self):
        from alert.crawlers.semas import SemasCrawler

        assert SemasCrawler.TLS_LEGACY_SECURITY_LEVEL is True
        assert SemasCrawler.TLS_EXTRA_CA_FILE is None

    def test_session_context_lowers_ciphers_but_not_trust(self):
        import ssl

        from alert.crawlers.semas import SemasCrawler
        from alert.crawlers.tls import LEGACY_CIPHERS, _ContextAdapter

        adapter = SemasCrawler().session.get_adapter("https://www.semas.or.kr")
        assert isinstance(adapter, _ContextAdapter)
        ctx = adapter._ssl_context
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
        plain = ssl.create_default_context()
        assert len(ctx.get_ciphers()) > len(plain.get_ciphers())
        assert LEGACY_CIPHERS == "DEFAULT@SECLEVEL=1"


class TestGgeeaCertBundle:
    """ggeea — 서버가 보내지 않는 중간 인증서를 번들로 채운다."""

    def test_crawler_declares_the_bundle(self):
        from alert.crawlers.ggeea import GgeeaCrawler
        from alert.crawlers.tls import CERTS_DIR

        assert GgeeaCrawler.TLS_EXTRA_CA_FILE == "ggeea-intermediate.pem"
        assert GgeeaCrawler.TLS_LEGACY_SECURITY_LEVEL is False
        assert (CERTS_DIR / GgeeaCrawler.TLS_EXTRA_CA_FILE).exists()

    def test_bundle_is_the_expected_intermediate(self):
        """번들이 조용히 바뀌면 red — 무엇을 신뢰에 더했는지가 계약이다."""
        import ssl

        from alert.crawlers.ggeea import GgeeaCrawler
        from alert.crawlers.tls import CERTS_DIR

        pem = (CERTS_DIR / GgeeaCrawler.TLS_EXTRA_CA_FILE).read_text(
            encoding="utf-8")
        assert pem.count("BEGIN CERTIFICATE") == 1, "번들은 중간 인증서 1장이다"
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(
            cafile=str(CERTS_DIR / GgeeaCrawler.TLS_EXTRA_CA_FILE))
        added = [cert for cert in ctx.get_ca_certs()
                 if any("Sectigo Public Server Authentication CA DV R36" in str(v)
                        for rdn in cert["subject"] for v in rdn)]
        assert added, "기대한 중간 인증서가 아니다"

    def test_bundle_is_not_a_root_replacement(self):
        """기본 루트를 덮어쓰지 않는다 — 이 소스만, 더하기만."""
        import ssl

        from alert.crawlers.ggeea import GgeeaCrawler
        from alert.crawlers.tls import _ContextAdapter

        adapter = GgeeaCrawler().session.get_adapter("https://www.ggeea.or.kr")
        assert isinstance(adapter, _ContextAdapter)
        ctx = adapter._ssl_context
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
        assert len(ctx.get_ca_certs()) == len(
            ssl.create_default_context().get_ca_certs()) + 1


class TestSlowSourceRetryPolicy:
    """ipet·fowi — 타임아웃·재시도 정책만 조정한다 (경로·파서 무변경)."""

    def test_ipet_prefers_short_reads_and_more_tries(self):
        from alert.crawlers.ipet import IpetCrawler

        crawler = IpetCrawler()
        assert crawler.request_timeout() == (5.0, 8.0)
        assert crawler.retry_count == 4 and crawler.retry_delay == 2.0
        # 소스당 최악 대기: 4×8 + 3×2 = 38초 (옛 정책 3×30 + 2×5 = 100초)
        worst = crawler.retry_count * crawler.request_timeout()[1] + (
            crawler.retry_count - 1) * crawler.retry_delay
        assert worst < 40

    def test_fowi_uses_the_same_shape(self):
        from alert.crawlers.fowi import FowiCrawler

        crawler = FowiCrawler()
        assert crawler.request_timeout() == (5.0, 12.0)
        assert crawler.retry_count == 4 and crawler.retry_delay == 2.0

    def test_other_sources_keep_the_global_policy(self, mock_crawler_config):
        """전역값을 흔들지 않았다 — 손잡이는 선언한 크롤러에만 붙는다."""
        crawler = _crawler(ConcreteCrawler, mock_crawler_config)
        assert crawler.request_timeout() == 10          # 픽스처의 전역 timeout
        assert crawler.retry_count == 3 and crawler.retry_delay == 1.0
        assert crawler.session.get_adapter("https://x").__class__.__name__ \
            == "HTTPAdapter"

    def test_timeout_tuple_reaches_the_request(self, mock_crawler_config):
        """선언한 튜플이 실제 요청 인자로 간다 (배선 회귀)."""
        from alert.crawlers.ipet import IpetCrawler

        crawler = IpetCrawler()
        seen = {}

        def capture(method, url, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            raise requests.ConnectionError("stop")

        with patch.object(crawler.session, "request", capture):
            with patch("alert.crawlers.base.time.sleep"):
                assert crawler.get("https://www.ipet.re.kr/x") is None
        assert seen["timeout"] == (5.0, 8.0)
