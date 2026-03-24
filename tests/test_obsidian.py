"""Tests for alert.obsidian -- filename sanitization and CMDS compliance."""

import pytest
from alert.obsidian import ObsidianSync


class TestSanitizeFilename:
    """Tests for _sanitize_filename edge cases."""

    def test_empty_string(self):
        """Empty string → 'untitled'."""
        assert ObsidianSync._sanitize_filename("") == "untitled"

    def test_whitespace_only(self):
        """Whitespace only → 'untitled'."""
        assert ObsidianSync._sanitize_filename("   ") == "untitled"

    def test_special_chars_removed(self):
        """Special chars <>:"/\\|?* should be removed."""
        result = ObsidianSync._sanitize_filename('test<>:"/\\|?*file')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert '"' not in result
        assert "\\" not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result

    def test_spaces_to_hyphens(self):
        """Spaces should become hyphens."""
        result = ObsidianSync._sanitize_filename("hello world test")
        assert result == "hello-world-test"

    def test_normal_korean_title(self):
        """Korean title should pass through cleanly."""
        result = ObsidianSync._sanitize_filename("2026년 스마트팜 지원사업")
        assert "2026년" in result
        assert "스마트팜" in result


class TestConfigurableAuthor:
    """Tests for ObsidianConfig.author field."""

    def test_default_author(self):
        """Default (no config) → '이상민'."""
        sync = ObsidianSync()
        assert sync._author == "이상민"

    def test_custom_author(self):
        """Custom config.author → reflected in sync._author."""
        class FakeConfig:
            vault_path = "/tmp/test"
            announcement_folder = "test"
            research_folder = "test"
            min_relevance_for_sync = 0.5
            author = "테스트유저"

        sync = ObsidianSync(FakeConfig())
        assert sync._author == "테스트유저"


class TestCMDSFrontmatter:
    """Tests for CMDS-compliant Obsidian note rendering."""

    def _make_sync_with_vault(self, tmp_path):
        """Create ObsidianSync with a temporary vault path."""
        class FakeConfig:
            vault_path = str(tmp_path)
            announcement_folder = "공고"
            research_folder = "리서치"
            min_relevance_for_sync = 0.5
            author = "이상민"
        return ObsidianSync(FakeConfig())

    def test_announcement_note_has_required_props(self, tmp_path):
        """Announcement note should have all 6 CMDS required properties."""
        sync = self._make_sync_with_vault(tmp_path)

        # Create a mock classified announcement
        ann = type("Ann", (), {
            "title": "테스트 공고",
            "source": "test",
            "source_id": "t-001",
            "url": "https://example.com",
            "summary": "요약 텍스트",
            "author": "농림부",
            "category": "지원",
            "target": "농업인",
            "period_start": "2026-01-01",
            "period_end": "2026-12-31",
            "relevance_score": 0.8,
            "relevance_reason": "관련성 높음",
            "matched_keywords": ["스마트팜"],
            "business_domain": "moss_agriculture",
            "domain_confidence": 0.75,
            "similar_announcements": [],
        })()

        content = sync._render_announcement_note(ann)

        # Check 6 CMDS required properties exist in frontmatter
        assert "type: note" in content
        assert "aliases: []" in content
        assert "author:" in content
        assert "[[이상민]]" in content
        assert "date created:" in content
        assert "date modified:" in content
        assert "tags:" in content

    def test_research_note_has_required_props(self, tmp_path):
        """Research note should have all 6 CMDS required properties."""
        sync = self._make_sync_with_vault(tmp_path)

        content = sync._render_research_note(
            title="테스트 리서치",
            content="본문 내용",
            period="2026년 Q1",
            version=1,
        )

        assert "type: note" in content
        assert "aliases: []" in content
        assert "[[이상민]]" in content
        assert "date created:" in content
        assert "date modified:" in content
        assert "tags:" in content
