"""Tests for alert/n8n_hook.py (N8nHook class)."""

from unittest.mock import MagicMock, Mock, patch
from datetime import datetime

import pytest

from alert.n8n_hook import N8nHook
from alert.models import AnalyzedAnnouncement


@pytest.fixture
def mock_n8n_config():
    """Mock N8nConfig object."""
    config = MagicMock()
    config.webhook_url = "https://n8n.example.com/webhook/test"
    config.notify_on = ["new_relevant", "status_change"]
    return config


@pytest.fixture
def mock_n8n_config_disabled():
    """Mock N8nConfig with empty webhook URL (disabled)."""
    config = MagicMock()
    config.webhook_url = ""
    config.notify_on = ["new_relevant"]
    return config


@pytest.fixture
def sample_announcement_data():
    """Sample announcement data for webhook payload."""
    return AnalyzedAnnouncement(
        id=123,
        source="bizinfo",
        source_id="test-001",
        title="스마트팜 혁신 지원사업",
        url="https://example.com/test-001",
        summary="스마트팜 지원",
        author="농림축산식품부",
        category="농업",
        target="농업인",
        period_end="2026-04-30",
        relevance_score=0.85,
        relevance_reason="키워드 매칭",
        matched_keywords=["스마트팜"],
    )


class TestN8nHook:
    """Test N8nHook class."""

    def test_initialization_with_config(self, mock_n8n_config):
        """N8nHook should initialize with config values."""
        hook = N8nHook(config=mock_n8n_config)

        assert hook._webhook_url == mock_n8n_config.webhook_url
        assert hook._notify_on == mock_n8n_config.notify_on
        assert hook._client is None  # Lazy initialization

    def test_initialization_without_config(self):
        """N8nHook should handle None config gracefully."""
        hook = N8nHook(config=None)

        assert hook._webhook_url == ""
        assert hook._notify_on == ["new_relevant", "status_change"]
        assert hook._client is None

    def test_disabled_when_webhook_url_empty(self, mock_n8n_config_disabled):
        """send_event() should return False when webhook_url is empty."""
        hook = N8nHook(config=mock_n8n_config_disabled)

        result = hook.send_event("new_relevant_announcement", data=None)

        assert result is False

    def test_send_event_returns_false_when_disabled(self):
        """send_event() should return False immediately when disabled."""
        hook = N8nHook(config=None)

        with patch.object(hook, "_get_client") as mock_get_client:
            result = hook.send_event("test_event", data=None)

            assert result is False
            # Should not even try to get client
            mock_get_client.assert_not_called()

    def test_get_client_lazy_initialization(self, mock_n8n_config):
        """_get_client() should lazy-initialize httpx.Client."""
        hook = N8nHook(config=mock_n8n_config)

        mock_httpx_module = MagicMock()
        mock_httpx_client = MagicMock()
        mock_httpx_module.Client.return_value = mock_httpx_client

        # Mock the import inside _get_client
        with patch.dict("sys.modules", {"httpx": mock_httpx_module}):
            client = hook._get_client()

            assert client is mock_httpx_client
            mock_httpx_module.Client.assert_called_once_with(timeout=5.0)

            # Second call should return cached client
            client2 = hook._get_client()
            assert client2 is mock_httpx_client
            assert mock_httpx_module.Client.call_count == 1  # Not called again

    def test_get_client_handles_missing_httpx(self, mock_n8n_config):
        """_get_client() should return None when httpx is not installed."""
        hook = N8nHook(config=mock_n8n_config)

        # Simulate ImportError when trying to import httpx
        def import_side_effect(name, *args, **kwargs):
            if name == "httpx":
                raise ImportError("No module named 'httpx'")
            return __import__(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_side_effect):
            client = hook._get_client()

            assert client is None

    def test_send_event_respects_notify_on_filter(self, mock_n8n_config):
        """send_event() should skip events not in notify_on list."""
        hook = N8nHook(config=mock_n8n_config)

        # "research_generated" is not in notify_on
        result = hook.send_event("research_report_generated", data=None)

        assert result is False

    def test_send_event_allows_filtered_events(self, mock_n8n_config, sample_announcement_data):
        """send_event() should allow events in notify_on list."""
        hook = N8nHook(config=mock_n8n_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response

        with patch.object(hook, "_get_client", return_value=mock_client):
            result = hook.send_event("new_relevant_announcement", data=sample_announcement_data)

            assert result is True
            mock_client.post.assert_called_once()

    def test_send_event_sends_correct_payload_structure(self, mock_n8n_config, sample_announcement_data):
        """send_event() should send payload with correct structure."""
        hook = N8nHook(config=mock_n8n_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response

        with patch.object(hook, "_get_client", return_value=mock_client):
            result = hook.send_event(
                "new_relevant_announcement",
                data=sample_announcement_data,
                obsidian_path="/path/to/note.md"
            )

            assert result is True

            # Check call arguments
            call_args = mock_client.post.call_args
            assert call_args[0][0] == mock_n8n_config.webhook_url

            payload = call_args[1]["json"]
            assert payload["event"] == "new_relevant_announcement"
            assert "timestamp" in payload
            assert "announcement" in payload
            assert payload["announcement"]["id"] == 123
            assert payload["announcement"]["title"] == "스마트팜 혁신 지원사업"
            assert payload["announcement"]["source"] == "bizinfo"
            assert payload["announcement"]["relevance_score"] == 0.85
            assert payload["obsidian_path"] == "/path/to/note.md"

    def test_send_event_handles_missing_announcement_fields(self, mock_n8n_config):
        """send_event() should handle announcements with missing optional fields."""
        hook = N8nHook(config=mock_n8n_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response

        # Minimal announcement with only title, other attributes don't exist
        minimal_data = Mock(spec=["title"])
        minimal_data.title = "Test Title"

        with patch.object(hook, "_get_client", return_value=mock_client):
            result = hook.send_event("new_relevant_announcement", data=minimal_data)

            assert result is True

            payload = mock_client.post.call_args[1]["json"]
            assert payload["announcement"]["title"] == "Test Title"
            assert payload["announcement"]["id"] is None
            assert payload["announcement"]["source"] == ""
            assert payload["announcement"]["relevance_score"] == 0.0

    def test_send_event_without_announcement_data(self, mock_n8n_config):
        """send_event() should work without announcement data."""
        hook = N8nHook(config=mock_n8n_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response

        with patch.object(hook, "_get_client", return_value=mock_client):
            result = hook.send_event("application_status_changed", data=None)

            assert result is True

            payload = mock_client.post.call_args[1]["json"]
            assert payload["event"] == "application_status_changed"
            assert "announcement" not in payload

    def test_send_event_http_success_codes(self, mock_n8n_config):
        """send_event() should return True for HTTP codes < 400."""
        hook = N8nHook(config=mock_n8n_config)

        for status_code in [200, 201, 204, 301, 302]:
            mock_response = MagicMock()
            mock_response.status_code = status_code
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response

            with patch.object(hook, "_get_client", return_value=mock_client):
                result = hook.send_event("new_relevant_announcement", data=None)

                assert result is True, f"Expected True for status code {status_code}"

    def test_send_event_http_error_codes_retry(self, mock_n8n_config):
        """send_event() should retry once on HTTP error codes."""
        hook = N8nHook(config=mock_n8n_config)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response

        with patch.object(hook, "_get_client", return_value=mock_client):
            result = hook.send_event("new_relevant_announcement", data=None)

            assert result is False
            # Should retry (2 total attempts)
            assert mock_client.post.call_count == 2

    def test_send_event_http_exception_retry(self, mock_n8n_config):
        """send_event() should retry once on HTTP exceptions."""
        hook = N8nHook(config=mock_n8n_config)

        mock_client = MagicMock()
        mock_client.post.side_effect = Exception("Network error")

        with patch.object(hook, "_get_client", return_value=mock_client):
            result = hook.send_event("new_relevant_announcement", data=None)

            assert result is False
            # Should retry (2 total attempts)
            assert mock_client.post.call_count == 2

    def test_send_event_success_on_second_attempt(self, mock_n8n_config):
        """send_event() should return True if second attempt succeeds."""
        hook = N8nHook(config=mock_n8n_config)

        mock_client = MagicMock()
        # First call fails, second succeeds
        mock_client.post.side_effect = [
            Exception("Temporary error"),
            MagicMock(status_code=200)
        ]

        with patch.object(hook, "_get_client", return_value=mock_client):
            result = hook.send_event("new_relevant_announcement", data=None)

            assert result is True
            assert mock_client.post.call_count == 2

    def test_send_event_includes_content_type_header(self, mock_n8n_config):
        """send_event() should include Content-Type header."""
        hook = N8nHook(config=mock_n8n_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response

        with patch.object(hook, "_get_client", return_value=mock_client):
            hook.send_event("new_relevant_announcement", data=None)

            call_args = mock_client.post.call_args
            headers = call_args[1]["headers"]
            assert headers["Content-Type"] == "application/json"

    def test_event_to_category_mapping(self):
        """_event_to_category() should map event types correctly."""
        assert N8nHook._event_to_category("new_relevant_announcement") == "new_relevant"
        assert N8nHook._event_to_category("application_status_changed") == "status_change"
        assert N8nHook._event_to_category("research_report_generated") == "research_generated"
        assert N8nHook._event_to_category("unknown_event") == "unknown_event"

    def test_close_closes_client(self, mock_n8n_config):
        """close() should close httpx client if it exists."""
        hook = N8nHook(config=mock_n8n_config)

        mock_client = MagicMock()
        hook._client = mock_client

        hook.close()

        mock_client.close.assert_called_once()
        assert hook._client is None

    def test_close_safe_when_no_client(self, mock_n8n_config):
        """close() should be safe to call when client is None."""
        hook = N8nHook(config=mock_n8n_config)

        # Should not raise exception
        hook.close()
        assert hook._client is None

    def test_close_multiple_times(self, mock_n8n_config):
        """close() should be safe to call multiple times."""
        hook = N8nHook(config=mock_n8n_config)

        mock_client = MagicMock()
        hook._client = mock_client

        hook.close()
        hook.close()  # Second call should not raise

        # Should only call close once
        mock_client.close.assert_called_once()
        assert hook._client is None

    def test_send_event_timestamp_format(self, mock_n8n_config):
        """send_event() should include ISO format timestamp."""
        hook = N8nHook(config=mock_n8n_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response

        with patch.object(hook, "_get_client", return_value=mock_client):
            with patch("alert.n8n_hook.datetime") as mock_datetime:
                mock_now = MagicMock()
                mock_now.isoformat.return_value = "2026-03-25T12:00:00"
                mock_datetime.now.return_value = mock_now

                hook.send_event("new_relevant_announcement", data=None)

                payload = mock_client.post.call_args[1]["json"]
                assert payload["timestamp"] == "2026-03-25T12:00:00"
