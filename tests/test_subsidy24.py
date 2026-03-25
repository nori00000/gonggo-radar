"""Tests for alert/crawlers/subsidy24.py (Subsidy24Crawler)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.subsidy24 import Subsidy24Crawler
from alert.models import RawAnnouncement


class TestSubsidy24Crawler:
    """Test Subsidy24Crawler implementation."""

    EXPECTED_BASE_URL = (
        "https://apis.data.go.kr/B554287/"
        "SubsidyBusinessInfoService/getSubsidyBusinessInfo"
    )

    @pytest.fixture
    def mock_subsidy24_config(self):
        """Mock config for Subsidy24Crawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        subsidy24_source = MagicMock()
        subsidy24_source.enabled = True
        subsidy24_source.base_url = self.EXPECTED_BASE_URL
        subsidy24_source.api_key = ""
        subsidy24_source.per_page = 100

        config.crawler.sources = {"subsidy24": subsidy24_source}
        return config

    def test_base_url_is_configured(self):
        """BASE_URL should point to the data.go.kr subsidy24 API endpoint."""
        assert Subsidy24Crawler.BASE_URL == self.EXPECTED_BASE_URL

    def test_initialization_without_api_key(self, mock_subsidy24_config):
        """Subsidy24Crawler should NOT raise if API key not found."""
        mock_subsidy24_config.crawler.sources["subsidy24"].api_key = ""

        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {}, clear=True):
                # Should NOT raise ValueError - just warn
                crawler = Subsidy24Crawler()

                assert crawler.source_name == "subsidy24"
                assert crawler.api_key == ""

    def test_initialization_with_api_key_from_env(self, mock_subsidy24_config):
        """Subsidy24Crawler should get API key from environment."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "env-api-key"}):
                crawler = Subsidy24Crawler()

                assert crawler.source_name == "subsidy24"
                assert crawler.api_key == "env-api-key"
                assert crawler.per_page == 100

    def test_fetch_returns_empty_when_endpoint_not_configured(self, mock_subsidy24_config):
        """fetch() should return empty list when BASE_URL is not set."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                # Override BASE_URL to empty to test the guard clause
                with patch.object(Subsidy24Crawler, "BASE_URL", ""):
                    results = crawler.fetch()
                    assert results == []

    def test_fetch_returns_empty_when_no_api_key(self, mock_subsidy24_config):
        """fetch() should return empty list when API key is missing."""
        mock_subsidy24_config.crawler.sources["subsidy24"].api_key = ""

        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {}, clear=True):
                crawler = Subsidy24Crawler()

                # BASE_URL is set but no API key
                results = crawler.fetch()
                assert results == []

    def test_fetch_with_mocked_api_response(self, mock_subsidy24_config):
        """fetch() should parse API response when endpoint IS configured."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "response": {
                        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                        "body": {
                            "items": [
                                {
                                    "biz_nm": "농업인 보조금 지원사업",
                                    "biz_no": "SUB-001",
                                    "dtl_page_url": "https://www.subsidy24.go.kr/detail/001",
                                    "biz_summary": "농업인 대상 보조금 지원",
                                    "oper_inst_nm": "농림축산식품부",
                                    "biz_clsfc_nm": "농업지원",
                                    "apply_trgt": "농업인",
                                    "rcpt_bgn_dt": "20260401",
                                    "rcpt_end_dt": "20260430",
                                }
                            ],
                            "totalCount": 1,
                        }
                    }
                }

                with patch.object(crawler, "get", return_value=mock_response):
                    results = crawler.fetch()

                    assert len(results) == 1
                    assert results[0].source == "subsidy24"
                    assert results[0].source_id == "SUB-001"
                    assert results[0].title == "농업인 보조금 지원사업"
                    assert results[0].url == "https://www.subsidy24.go.kr/detail/001"
                    assert results[0].summary == "농업인 대상 보조금 지원"
                    assert results[0].author == "농림축산식품부"
                    assert results[0].category == "농업지원"
                    assert results[0].target == "농업인"
                    assert results[0].period_start == "2026-04-01"
                    assert results[0].period_end == "2026-04-30"

    def test_extract_items_from_standard_response(self, mock_subsidy24_config):
        """_extract_items() should extract items from standard data.go.kr response."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                # Standard data.go.kr format
                data = {
                    "response": {
                        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                        "body": {
                            "items": [
                                {"biz_nm": "사업1", "biz_no": "001"},
                                {"biz_nm": "사업2", "biz_no": "002"},
                            ],
                            "totalCount": 2,
                        }
                    }
                }

                items = crawler._extract_items(data)
                assert len(items) == 2
                assert items[0]["biz_no"] == "001"
                assert items[1]["biz_no"] == "002"

    def test_extract_items_error_response(self, mock_subsidy24_config):
        """_extract_items() should return empty list on API error."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                data_error = {
                    "response": {
                        "header": {"resultCode": "99", "resultMsg": "Service Error"},
                        "body": {"items": []},
                    }
                }
                items_error = crawler._extract_items(data_error)
                assert items_error == []

    def test_extract_items_single_item_not_list(self, mock_subsidy24_config):
        """_extract_items() should handle single item (dict instead of list)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                data_single = {
                    "response": {
                        "header": {"resultCode": "00"},
                        "body": {
                            "items": {"biz_nm": "단일사업", "biz_no": "003"},
                        }
                    }
                }
                items_single = crawler._extract_items(data_single)
                assert len(items_single) == 1
                assert items_single[0]["biz_no"] == "003"

    def test_extract_items_nested_item_key(self, mock_subsidy24_config):
        """_extract_items() should handle response > body > items > item nesting."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                # Some data.go.kr APIs nest items inside items > item
                data_nested = {
                    "response": {
                        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                        "body": {
                            "items": {
                                "item": [
                                    {"biz_nm": "사업A", "biz_no": "A01"},
                                    {"biz_nm": "사업B", "biz_no": "A02"},
                                ]
                            },
                            "totalCount": 2,
                        }
                    }
                }

                items = crawler._extract_items(data_nested)
                assert len(items) == 2
                assert items[0]["biz_no"] == "A01"
                assert items[1]["biz_no"] == "A02"

    def test_extract_items_v2_flat_format(self, mock_subsidy24_config):
        """_extract_items() should handle 공공데이터포털 2.0 flat format."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                # 공공데이터포털 2.0 format
                data_v2 = {
                    "currentCount": 2,
                    "data": [
                        {"biz_nm": "V2 사업1", "biz_no": "V2-001"},
                        {"biz_nm": "V2 사업2", "biz_no": "V2-002"},
                    ],
                    "matchCount": 100,
                    "page": 1,
                    "perPage": 10,
                    "totalCount": 100,
                }

                items = crawler._extract_items(data_v2)
                assert len(items) == 2
                assert items[0]["biz_no"] == "V2-001"
                assert items[1]["biz_no"] == "V2-002"

    def test_extract_items_unrecognized_format(self, mock_subsidy24_config):
        """_extract_items() should return empty list for unrecognized format."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                data_unknown = {"status": "ok", "result": [{"foo": "bar"}]}
                items = crawler._extract_items(data_unknown)
                assert items == []

    def test_parse_item_with_valid_data(self, mock_subsidy24_config):
        """_parse_item() should parse valid item data."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                item = {
                    "biz_nm": "스마트팜 보조금 지원",
                    "biz_no": "BIZ-12345",
                    "dtl_page_url": "https://www.subsidy24.go.kr/detail/12345",
                    "biz_summary": "스마트팜 도입 보조금",
                    "oper_inst_nm": "농림축산식품부",
                    "biz_clsfc_nm": "농업지원",
                    "apply_trgt": "농업인",
                    "rcpt_bgn_dt": "20260401",
                    "rcpt_end_dt": "20260430",
                }

                announcement = crawler._parse_item(item)

                assert announcement is not None
                assert announcement.source == "subsidy24"
                assert announcement.source_id == "BIZ-12345"
                assert announcement.title == "스마트팜 보조금 지원"
                assert announcement.url == "https://www.subsidy24.go.kr/detail/12345"
                assert announcement.summary == "스마트팜 도입 보조금"
                assert announcement.author == "농림축산식품부"
                assert announcement.category == "농업지원"
                assert announcement.target == "농업인"
                assert announcement.period_start == "2026-04-01"
                assert announcement.period_end == "2026-04-30"

    def test_parse_item_missing_title_returns_none(self, mock_subsidy24_config):
        """_parse_item() should return None if title is missing."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                # Missing title
                item = {
                    "biz_nm": "",
                    "biz_no": "BIZ-001",
                    "dtl_page_url": "https://example.com",
                }

                result = crawler._parse_item(item)
                assert result is None

                # No biz_nm key at all
                item_no_key = {
                    "biz_no": "BIZ-002",
                }

                result_no_key = crawler._parse_item(item_no_key)
                assert result_no_key is None

    def test_parse_item_generates_hash_when_no_biz_no(self, mock_subsidy24_config):
        """_parse_item() should generate hash source_id when biz_no is empty."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                item = {
                    "biz_nm": "보조금 지원사업",
                    "biz_no": "",
                    "rcpt_bgn_dt": "20260401",
                    "rcpt_end_dt": "20260430",
                }

                announcement = crawler._parse_item(item)

                assert announcement is not None
                assert announcement.source_id != ""
                assert len(announcement.source_id) == 16  # MD5 hash truncated

    def test_normalize_date_formats(self, mock_subsidy24_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                # YYYYMMDD format
                assert crawler._normalize_date("20260401") == "2026-04-01"

                # Already ISO format
                assert crawler._normalize_date("2026-04-01") == "2026-04-01"

                # Dot separator
                assert crawler._normalize_date("2026.04.01") == "2026-04-01"

                # Slash separator
                assert crawler._normalize_date("2026/04/01") == "2026-04-01"

                # Empty string
                assert crawler._normalize_date("") is None

                # Invalid format
                assert crawler._normalize_date("invalid") is None

    def test_validate_endpoint_returns_false_when_empty(self, mock_subsidy24_config):
        """_validate_endpoint() should return False when BASE_URL is empty."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                with patch.object(Subsidy24Crawler, "BASE_URL", ""):
                    assert crawler._validate_endpoint() is False

    def test_validate_endpoint_returns_true_when_set(self, mock_subsidy24_config):
        """_validate_endpoint() should return True when BASE_URL is set."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                # BASE_URL is now set by default
                assert crawler._validate_endpoint() is True

    def test_fetch_handles_json_decode_error(self, mock_subsidy24_config):
        """fetch() should handle JSON decode errors gracefully."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                mock_response = MagicMock()
                mock_response.json.side_effect = json.JSONDecodeError("Invalid JSON", "", 0)
                mock_response.text = "invalid json"

                with patch.object(crawler, "get", return_value=mock_response):
                    results = crawler.fetch()
                    assert results == []

    def test_fetch_handles_none_response(self, mock_subsidy24_config):
        """fetch() should handle None response (network error) gracefully."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                with patch.object(crawler, "get", return_value=None):
                    results = crawler.fetch()
                    assert results == []

    def test_fetch_with_v2_format_response(self, mock_subsidy24_config):
        """fetch() should correctly parse 공공데이터포털 2.0 format end-to-end."""
        with patch("alert.crawlers.base.get_config", return_value=mock_subsidy24_config):
            with patch.dict("os.environ", {"DATA_GO_KR_API_KEY": "test-key"}):
                crawler = Subsidy24Crawler()

                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "currentCount": 1,
                    "data": [
                        {
                            "biz_nm": "청년 농업인 육성 지원",
                            "biz_no": "V2-BIZ-001",
                            "dtl_page_url": "https://www.subsidy24.go.kr/detail/v2/001",
                            "biz_summary": "청년 농업인 대상 창업 보조금",
                            "oper_inst_nm": "농림축산식품부",
                            "biz_clsfc_nm": "농업지원",
                            "apply_trgt": "만 18~39세 청년",
                            "rcpt_bgn_dt": "2026-05-01",
                            "rcpt_end_dt": "2026-06-30",
                        }
                    ],
                    "matchCount": 1,
                    "page": 1,
                    "perPage": 10,
                    "totalCount": 1,
                }

                with patch.object(crawler, "get", return_value=mock_response):
                    results = crawler.fetch()

                    assert len(results) == 1
                    assert results[0].source == "subsidy24"
                    assert results[0].source_id == "V2-BIZ-001"
                    assert results[0].title == "청년 농업인 육성 지원"
                    assert results[0].period_start == "2026-05-01"
                    assert results[0].period_end == "2026-06-30"
                    assert results[0].target == "만 18~39세 청년"
