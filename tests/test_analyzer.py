"""Tests for alert/analyzer.py (KeywordAnalyzer and ClaudeAnalyzer)."""

import json
from unittest.mock import MagicMock, Mock, patch

import pytest

from alert.analyzer import KeywordAnalyzer, ClaudeAnalyzer
from alert.models import RawAnnouncement, AnalyzedAnnouncement, Keyword


@pytest.fixture
def mock_db():
    """Mock Database instance."""
    db = MagicMock()
    db.get_keywords.return_value = [
        Keyword(keyword="스마트팜", category="must_match", weight=1.0, is_active=True),
        Keyword(keyword="시설원예", category="boost", weight=1.0, is_active=True),
        Keyword(keyword="조경", category="boost", weight=1.0, is_active=True),
        Keyword(keyword="부동산", category="exclude", weight=1.0, is_active=True),
    ]
    db.init_default_keywords.return_value = None
    return db


@pytest.fixture
def mock_config():
    """Mock config object."""
    config = MagicMock()
    config.analyzer.keyword_threshold = 0.3
    config.analyzer.claude_threshold = 0.5
    config.analyzer.max_claude_calls_per_run = 5
    config.analyzer.api_key = ""
    config.analyzer.claude_model = "claude-3-5-sonnet-20241022"
    return config


@pytest.fixture
def sample_raw_announcement():
    """Sample RawAnnouncement for testing."""
    return RawAnnouncement(
        source="test",
        source_id="test-001",
        title="스마트팜 혁신 지원사업",
        url="https://example.com/test-001",
        summary="시설원예 농가를 위한 지원",
        author="농림축산식품부",
        category="농업",
        target="농업인",
    )


class TestKeywordAnalyzer:
    """Test KeywordAnalyzer class."""

    def test_initialization_with_config(self, mock_db, mock_config):
        """KeywordAnalyzer should load keywords from database on init."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            # Should call get_keywords
            mock_db.get_keywords.assert_called_once()

            # Should load keywords by category
            assert "must_match" in analyzer.keywords_by_category
            assert "boost" in analyzer.keywords_by_category
            assert "exclude" in analyzer.keywords_by_category
            assert len(analyzer.keywords_by_category["must_match"]) == 1
            assert len(analyzer.keywords_by_category["boost"]) == 2
            assert len(analyzer.keywords_by_category["exclude"]) == 1

    def test_initialization_empty_keywords(self, mock_config):
        """KeywordAnalyzer should initialize default keywords if DB is empty."""
        mock_db = MagicMock()
        mock_db.get_keywords.side_effect = [[], []]  # Empty first, then empty again
        mock_db.init_default_keywords.return_value = None

        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            # Should call init_default_keywords when empty
            mock_db.init_default_keywords.assert_called_once()

    def test_analyze_exclude_keyword_match(self, mock_db, mock_config):
        """analyze() should return score 0.0 when exclude keyword matches."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            announcement = RawAnnouncement(
                source="test",
                source_id="test-001",
                title="부동산 투자 사업",
                url="https://example.com",
                summary="부동산 개발",
                author="test",
                category="test",
                target="test",
            )

            result = analyzer.analyze(announcement)

            assert result.relevance_score == 0.0
            assert result.relevance_reason == "제외 키워드 발견"
            assert "부동산" in result.matched_keywords

    def test_analyze_must_match_keyword(self, mock_db, mock_config, sample_raw_announcement):
        """analyze() should set base score 0.5 when must_match keyword found."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            result = analyzer.analyze(sample_raw_announcement)

            # Must match keyword "스마트팜" is in title
            assert result.relevance_score >= 0.5
            assert "스마트팜" in result.matched_keywords
            assert "필수 키워드 매칭" in result.relevance_reason

    def test_analyze_boost_keywords(self, mock_db, mock_config):
        """analyze() should add 0.05 per boost keyword, max 0.5."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            announcement = RawAnnouncement(
                source="test",
                source_id="test-001",
                title="스마트팜 시설원예 조경 사업",
                url="https://example.com",
                summary="스마트팜 기반 시설원예",
                author="test",
                category="test",
                target="test",
            )

            result = analyzer.analyze(announcement)

            # must_match (스마트팜) = 0.5 + boost (시설원예, 조경) = 0.1
            assert result.relevance_score == 0.6
            assert "스마트팜" in result.matched_keywords
            assert "시설원예" in result.matched_keywords
            assert "조경" in result.matched_keywords

    def test_analyze_no_keywords_match(self, mock_db, mock_config):
        """analyze() should return 0.0 score when no keywords match."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            announcement = RawAnnouncement(
                source="test",
                source_id="test-001",
                title="일반 사무직 채용 공고",
                url="https://example.com",
                summary="사무직 채용",
                author="test",
                category="test",
                target="test",
            )

            result = analyzer.analyze(announcement)

            assert result.relevance_score == 0.0
            assert result.relevance_reason == "관련 키워드 없음"
            assert len(result.matched_keywords) == 0

    def test_analyze_returns_analyzed_announcement(self, mock_db, mock_config, sample_raw_announcement):
        """analyze() should return AnalyzedAnnouncement with correct attributes."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            result = analyzer.analyze(sample_raw_announcement)

            assert isinstance(result, AnalyzedAnnouncement)
            assert result.title == sample_raw_announcement.title
            assert result.source == sample_raw_announcement.source
            assert hasattr(result, "relevance_score")
            assert hasattr(result, "relevance_reason")
            assert hasattr(result, "matched_keywords")

    def test_analyze_batch_filters_by_threshold(self, mock_db, mock_config):
        """analyze_batch() should filter announcements by keyword_threshold."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            announcements = [
                RawAnnouncement(
                    source="test", source_id="1", title="스마트팜 사업",
                    url="https://example.com/1", summary="", author="", category="", target=""
                ),
                RawAnnouncement(
                    source="test", source_id="2", title="일반 사업",
                    url="https://example.com/2", summary="", author="", category="", target=""
                ),
            ]

            results = analyzer.analyze_batch(announcements)

            # Only first announcement should pass threshold (0.3)
            assert len(results) == 1
            assert results[0].source_id == "1"
            assert results[0].relevance_score >= mock_config.analyzer.keyword_threshold

    def test_analyze_batch_sorts_by_score_descending(self, mock_db, mock_config):
        """analyze_batch() should sort results by relevance_score descending."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)

            announcements = [
                RawAnnouncement(
                    source="test", source_id="1", title="스마트팜",
                    url="https://example.com/1", summary="", author="", category="", target=""
                ),
                RawAnnouncement(
                    source="test", source_id="2", title="스마트팜 시설원예 조경",
                    url="https://example.com/2", summary="", author="", category="", target=""
                ),
            ]

            results = analyzer.analyze_batch(announcements)

            # Second announcement should have higher score
            assert len(results) == 2
            assert results[0].source_id == "2"  # Higher score first
            assert results[0].relevance_score > results[1].relevance_score

    def test_close_closes_owned_db(self, mock_config):
        """close() should close DB connection if analyzer owns it."""
        mock_db = MagicMock()
        mock_db.get_keywords.return_value = []
        mock_db.init_default_keywords.return_value = None

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.Database", return_value=mock_db):
                analyzer = KeywordAnalyzer()  # No db passed, creates own
                analyzer.close()

                # Should close the DB
                mock_db.close.assert_called_once()

    def test_close_does_not_close_external_db(self, mock_db, mock_config):
        """close() should NOT close DB connection if provided externally."""
        with patch("alert.analyzer.get_config", return_value=mock_config):
            analyzer = KeywordAnalyzer(db=mock_db)
            analyzer.close()

            # Should NOT close external DB
            mock_db.close.assert_not_called()


class TestClaudeAnalyzer:
    """Test ClaudeAnalyzer class."""

    def test_initialization_without_api_key(self, mock_config):
        """ClaudeAnalyzer should skip initialization when API key is missing."""
        mock_config.analyzer.api_key = ""

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch.dict("os.environ", {}, clear=True):
                with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                    analyzer = ClaudeAnalyzer()

                    assert analyzer.client is None

    def test_initialization_without_anthropic_sdk(self, mock_config):
        """ClaudeAnalyzer should skip initialization when anthropic SDK unavailable."""
        mock_config.analyzer.api_key = "test-key"

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", False):
                analyzer = ClaudeAnalyzer()

                assert analyzer.client is None

    def test_initialization_with_api_key(self, mock_config):
        """ClaudeAnalyzer should initialize client when API key is available."""
        mock_config.analyzer.api_key = "test-key"

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                with patch("alert.analyzer.anthropic.Anthropic") as mock_anthropic:
                    analyzer = ClaudeAnalyzer()

                    mock_anthropic.assert_called_once_with(api_key="test-key")
                    assert analyzer.model == mock_config.analyzer.claude_model

    def test_analyze_skips_when_client_unavailable(self, mock_config, sample_announcement):
        """analyze() should return unchanged announcement when client is None."""
        mock_config.analyzer.api_key = ""

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                analyzer = ClaudeAnalyzer()

                result = analyzer.analyze(sample_announcement)

                # Should return the same announcement unchanged
                assert result == sample_announcement

    def test_analyze_calls_claude_api(self, mock_config, sample_announcement):
        """analyze() should call Claude API with correct parameters."""
        mock_config.analyzer.api_key = "test-key"

        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [
            MagicMock(text='{"score": 0.9, "reason": "매우 관련성 높음", "matched_aspects": ["스마트팜", "시설원예"]}')
        ]
        mock_client.messages.create.return_value = mock_message

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                with patch("alert.analyzer.anthropic.Anthropic", return_value=mock_client):
                    analyzer = ClaudeAnalyzer()
                    result = analyzer.analyze(sample_announcement)

                    # Should call API
                    mock_client.messages.create.assert_called_once()
                    call_args = mock_client.messages.create.call_args

                    assert call_args[1]["model"] == mock_config.analyzer.claude_model
                    assert call_args[1]["max_tokens"] == 1024
                    assert len(call_args[1]["messages"]) == 1

                    # Should update score and reason
                    assert result.relevance_score == 0.9
                    assert "매우 관련성 높음" in result.relevance_reason

    def test_analyze_handles_json_decode_error(self, mock_config, sample_announcement):
        """analyze() should handle JSON decode errors gracefully."""
        mock_config.analyzer.api_key = "test-key"

        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text='invalid json')]
        mock_client.messages.create.return_value = mock_message

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                with patch("alert.analyzer.anthropic.Anthropic", return_value=mock_client):
                    analyzer = ClaudeAnalyzer()
                    original_score = sample_announcement.relevance_score

                    result = analyzer.analyze(sample_announcement)

                    # Should return original announcement unchanged
                    assert result.relevance_score == original_score

    def test_analyze_handles_api_exception(self, mock_config, sample_announcement):
        """analyze() should handle API exceptions gracefully."""
        mock_config.analyzer.api_key = "test-key"

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API Error")

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                with patch("alert.analyzer.anthropic.Anthropic", return_value=mock_client):
                    analyzer = ClaudeAnalyzer()
                    original_score = sample_announcement.relevance_score

                    result = analyzer.analyze(sample_announcement)

                    # Should return original announcement unchanged
                    assert result.relevance_score == original_score

    def test_analyze_batch_filters_by_threshold(self, mock_config):
        """analyze_batch() should only analyze announcements >= claude_threshold."""
        mock_config.analyzer.api_key = "test-key"
        mock_config.analyzer.claude_threshold = 0.5

        announcements = [
            AnalyzedAnnouncement(
                source="test", source_id="1", title="High score",
                url="https://example.com/1", summary="", author="", category="", target="",
                relevance_score=0.8, relevance_reason="", matched_keywords=[]
            ),
            AnalyzedAnnouncement(
                source="test", source_id="2", title="Low score",
                url="https://example.com/2", summary="", author="", category="", target="",
                relevance_score=0.3, relevance_reason="", matched_keywords=[]
            ),
        ]

        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text='{"score": 0.9, "reason": "test"}')]
        mock_client.messages.create.return_value = mock_message

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                with patch("alert.analyzer.anthropic.Anthropic", return_value=mock_client):
                    analyzer = ClaudeAnalyzer()
                    results = analyzer.analyze_batch(announcements)

                    # Should only analyze first announcement (score >= 0.5)
                    assert mock_client.messages.create.call_count == 1

    def test_analyze_batch_respects_max_calls_limit(self, mock_config):
        """analyze_batch() should respect max_claude_calls_per_run limit."""
        mock_config.analyzer.api_key = "test-key"
        mock_config.analyzer.claude_threshold = 0.5
        mock_config.analyzer.max_claude_calls_per_run = 2

        announcements = [
            AnalyzedAnnouncement(
                source="test", source_id=str(i), title=f"Ann {i}",
                url=f"https://example.com/{i}", summary="", author="", category="", target="",
                relevance_score=0.8, relevance_reason="", matched_keywords=[]
            )
            for i in range(5)
        ]

        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text='{"score": 0.9, "reason": "test"}')]
        mock_client.messages.create.return_value = mock_message

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                with patch("alert.analyzer.anthropic.Anthropic", return_value=mock_client):
                    analyzer = ClaudeAnalyzer()
                    results = analyzer.analyze_batch(announcements)

                    # Should only analyze 2 announcements
                    assert mock_client.messages.create.call_count == 2
                    assert len(results) == 5  # All announcements returned

    def test_analyze_batch_skips_when_client_unavailable(self, mock_config):
        """analyze_batch() should return unchanged announcements when client is None."""
        mock_config.analyzer.api_key = ""

        announcements = [
            AnalyzedAnnouncement(
                source="test", source_id="1", title="Test",
                url="https://example.com/1", summary="", author="", category="", target="",
                relevance_score=0.8, relevance_reason="", matched_keywords=[]
            ),
        ]

        with patch("alert.analyzer.get_config", return_value=mock_config):
            with patch("alert.analyzer.ANTHROPIC_AVAILABLE", True):
                analyzer = ClaudeAnalyzer()

                results = analyzer.analyze_batch(announcements)

                # Should return the same announcements unchanged
                assert results == announcements
