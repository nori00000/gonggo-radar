"""옵시디언 볼트 마크다운 생성 및 동기화 -- CMDS 규격 준수."""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

__all__ = ["ObsidianSync"]

from .classifier import DOMAIN_KOREAN_MAP

logger = logging.getLogger(__name__)


class ObsidianSync:
    """옵시디언 볼트 CMDS 규격 마크다운 동기화."""

    def __init__(self, config=None):
        """Initialize with ObsidianConfig.

        Args:
            config: ObsidianConfig instance
        """
        self._vault_path = Path(config.vault_path) if config and config.vault_path else None
        self._announcement_folder = config.announcement_folder if config else "11.(주)어반정글/영업/공고"
        self._research_folder = config.research_folder if config else "Research/공고분석"
        self._min_relevance = config.min_relevance_for_sync if config else 0.5
        self._author = config.author if config else "이상민"

    @property
    def is_available(self) -> bool:
        """Check if vault path is configured and accessible."""
        return self._vault_path is not None and self._vault_path.exists()

    def generate_note(self, ann) -> Optional[str]:
        """Generate a CMDS-compliant Markdown note for an announcement.

        Args:
            ann: ClassifiedAnnouncement (or AnalyzedAnnouncement with business_domain)

        Returns:
            Relative path to the created file within vault, or None if failed
        """
        if not self.is_available:
            logger.warning("Obsidian vault not available, skipping note generation")
            return None

        # Build filename: YYYY-MM-DD-{source}-{sanitized_title}.md
        today = datetime.now().strftime("%Y-%m-%d")
        safe_title = self._sanitize_filename(ann.title)[:50]
        filename = f"{today}-{ann.source}-{safe_title}.md"

        # Determine target folder
        folder_path = self._vault_path / self._announcement_folder
        folder_path.mkdir(parents=True, exist_ok=True)

        file_path = folder_path / filename

        # Skip if already exists (idempotent)
        if file_path.exists():
            rel_path = str(file_path.relative_to(self._vault_path))
            logger.debug(f"Obsidian note already exists: {rel_path}")
            return rel_path

        # Render content
        content = self._render_announcement_note(ann)

        # Write atomically (temp file + rename)
        tmp_path = file_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.rename(file_path)
            rel_path = str(file_path.relative_to(self._vault_path))
            logger.info(f"Created Obsidian note: {rel_path}")
            return rel_path
        except Exception as e:
            logger.error(f"Failed to write Obsidian note: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            return None

    def generate_research_note(self, title: str, content: str, period: str, version: int = 1) -> Optional[str]:
        """Generate a CMDS-compliant research document in the vault.

        Returns:
            Relative path to the created file, or None if failed
        """
        if not self.is_available:
            return None

        today = datetime.now().strftime("%Y-%m-%d")
        safe_title = self._sanitize_filename(title)[:60]
        filename = f"{today}-{safe_title}.md"

        folder_path = self._vault_path / self._research_folder
        folder_path.mkdir(parents=True, exist_ok=True)

        file_path = folder_path / filename

        # Idempotency check
        if file_path.exists():
            rel_path = str(file_path.relative_to(self._vault_path))
            logger.debug(f"Research note already exists: {rel_path}")
            return rel_path

        note_content = self._render_research_note(title, content, period, version)

        tmp_path = file_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(note_content, encoding="utf-8")
            tmp_path.rename(file_path)
            rel_path = str(file_path.relative_to(self._vault_path))
            logger.info(f"Created research note: {rel_path}")
            return rel_path
        except Exception as e:
            logger.error(f"Failed to write research note: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            return None

    def _render_announcement_note(self, ann) -> str:
        """Render CMDS-compliant Markdown note for an announcement."""
        today = datetime.now().strftime("%Y-%m-%d")
        domain = getattr(ann, "business_domain", "") or ""
        domain_korean = DOMAIN_KOREAN_MAP.get(domain, domain)
        confidence = getattr(ann, "domain_confidence", 0.0)
        similar = getattr(ann, "similar_announcements", [])

        # YAML frontmatter (2-space indent, 6 required props first)
        lines = [
            "---",
            "type: note",
            "aliases: []",
            "author:",
            f'  - "[[{self._author}]]"',
            f"date created: {today}",
            f"date modified: {today}",
            "tags:",
            "  - 공고",
        ]
        if domain_korean:
            lines.append(f"  - {domain_korean}")
        lines.append(f"  - {ann.source}")
        # Non-standard properties AFTER the 6 required
        lines.extend([
            "status: unread",
            f"relevance: {ann.relevance_score}",
            f"domain: {domain}",
            f'source_url: "{ann.url}"',
            f'period: "{ann.period_start or "미정"} ~ {ann.period_end or "미정"}"',
            "---",
        ])

        # Markdown body
        lines.extend([
            "",
            f"# {ann.title}",
            "",
            "## 기본 정보",
            "",
            "| 항목 | 내용 |",
            "|------|------|",
            f"| **출처** | {ann.source} |",
            f"| **주관기관** | {ann.author or '미상'} |",
            f"| **지원대상** | {ann.target or '미상'} |",
            f"| **접수기간** | {ann.period_start or '미정'} ~ {ann.period_end or '미정'} |",
            f"| **관련성 점수** | {ann.relevance_score:.0%} |",
            f"| **사업 도메인** | {domain_korean or '미분류'} (신뢰도: {confidence:.0%}) |",
            "",
            "## 요약",
            "",
            ann.summary or "(요약 없음)",
            "",
            "## AI 분석",
            "",
            ann.relevance_reason or "(분석 없음)",
            "",
            "## 유사 과거 공고",
            "",
        ])

        if similar:
            for ann_id, distance in similar:
                lines.append(f"- 공고 #{ann_id} (유사도: {1.0 - distance:.2f})")
        else:
            lines.append("- (유사 공고 데이터 없음)")

        lines.extend([
            "",
            "## 대응 메모",
            "",
            "- [ ] 신청 검토",
            "- [ ] 필요 서류 확인",
            "- [ ] 신청서 작성",
            "",
            "---",
            "> Auto-generated by gonggo-radar AI Knowledge Layer",
            f"> Source: {ann.url}",
        ])

        return "\n".join(lines) + "\n"

    def _render_research_note(self, title: str, content: str, period: str, version: int) -> str:
        """Render CMDS-compliant research document."""
        today = datetime.now().strftime("%Y-%m-%d")

        lines = [
            "---",
            "type: note",
            "aliases: []",
            "author:",
            f'  - "[[{self._author}]]"',
            f"date created: {today}",
            f"date modified: {today}",
            "tags:",
            "  - 리서치",
            "  - 공고분석",
            f"  - {period}",
            "status: inProgress",
            "---",
            "",
            f"# {title}",
            "",
            content,
            "",
            "---",
            f"> Auto-generated by gonggo-radar Research Generator v{version}",
        ]

        return "\n".join(lines) + "\n"

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Remove characters invalid for filenames."""
        # Replace common problematic chars
        name = re.sub(r'[<>:"/\\|?*]', '', name)
        name = re.sub(r'\s+', '-', name.strip())
        return name or "untitled"
