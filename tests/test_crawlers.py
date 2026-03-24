"""Tests for alert/crawlers/ (BaseCrawler and specific crawlers)."""

import json
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import pytest
import requests

from alert.crawlers.base import BaseCrawler
from alert.crawlers.bizinfo import BizinfoCrawler
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

    def test_initialization_without_api_key_raises_error(self, mock_bizinfo_config):
        """BizinfoCrawler should raise ValueError if API key not found."""
        # Remove api_key from config
        mock_bizinfo_config.crawler.sources["bizinfo"].api_key = ""

        with patch("alert.crawlers.base.get_config", return_value=mock_bizinfo_config):
            with patch.dict("os.environ", {}, clear=True):
                with pytest.raises(ValueError, match="BIZINFO_API_KEY not found"):
                    BizinfoCrawler()

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
