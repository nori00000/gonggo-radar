"""분기별 리서치 문서 자동 생성 -- 트렌드 분석 및 전략 제안."""

import json
import logging

__all__ = ["ResearchGenerator"]
from datetime import datetime
from typing import Optional

from .classifier import DOMAIN_KOREAN_MAP
from .models import ResearchDocument

logger = logging.getLogger(__name__)


class ResearchGenerator:
    """분기별 리서치 문서 자동 생성기."""

    def __init__(self, db, config=None, obsidian=None):
        """Initialize.

        Args:
            db: Database instance
            config: KnowledgeConfig instance (optional)
            obsidian: ObsidianSync instance (optional)
        """
        self.db = db
        self._config = config
        self._obsidian = obsidian
        self._model = config.research.claude_model if config and hasattr(config, 'research') else "claude-sonnet-4-5-20250929"
        self._max_tokens = config.research.max_tokens if config and hasattr(config, 'research') else 4096

    def generate(self, period_start: str, period_end: str) -> Optional[int]:
        """Generate a research document for the given period.

        Args:
            period_start: Start date (YYYY-MM-DD)
            period_end: End date (YYYY-MM-DD)

        Returns:
            Research document row ID, or None if generation failed
        """
        logger.info(f"Generating research document for {period_start} ~ {period_end}")

        # 1. Gather data
        announcements = self.db.get_announcements_by_period(period_start, period_end)
        domain_stats = self.db.get_domain_stats(period_start, period_end)

        if not announcements:
            logger.info("No announcements found for the period, skipping research generation")
            return None

        # 2. Build statistics
        stats = self._build_statistics(announcements, domain_stats)

        # 3. Get top relevant announcements
        top_announcements = sorted(announcements, key=lambda a: a.relevance_score, reverse=True)[:10]

        # 4. Generate report content
        content = self._generate_content(stats, top_announcements, period_start, period_end)

        # 5. Determine title and period label
        period_label = self._period_label(period_start, period_end)
        title = f"{period_label} 공고 분석 리서치"

        # 6. Save to database
        doc = ResearchDocument(
            title=title,
            doc_type="quarterly",
            period_start=period_start,
            period_end=period_end,
            content=content,
            metadata=stats,
            version=1,
        )
        doc_id = self.db.insert_research_document(doc)
        logger.info(f"Research document saved to DB (id={doc_id})")

        # 7. Write to Obsidian vault
        if self._obsidian and self._obsidian.is_available:
            obsidian_path = self._obsidian.generate_research_note(
                title=title,
                content=content,
                period=period_label,
                version=1,
            )
            if obsidian_path:
                self.db.update_research_document(doc_id, content, 1, obsidian_path=obsidian_path)
                logger.info(f"Research note written to Obsidian: {obsidian_path}")

        return doc_id

    def _build_statistics(self, announcements, domain_stats) -> dict:
        """Build aggregate statistics from announcements."""
        total = len(announcements)

        # Source distribution
        source_counts = {}
        for ann in announcements:
            source_counts[ann.source] = source_counts.get(ann.source, 0) + 1

        # Score distribution
        high_relevance = sum(1 for a in announcements if a.relevance_score >= 0.7)
        mid_relevance = sum(1 for a in announcements if 0.3 <= a.relevance_score < 0.7)
        low_relevance = sum(1 for a in announcements if a.relevance_score < 0.3)

        # Average score
        avg_score = sum(a.relevance_score for a in announcements) / total if total > 0 else 0.0

        return {
            "total_announcements": total,
            "domain_distribution": domain_stats,
            "source_distribution": source_counts,
            "relevance_distribution": {
                "high": high_relevance,
                "medium": mid_relevance,
                "low": low_relevance,
            },
            "average_relevance": round(avg_score, 3),
        }

    def _generate_content(self, stats: dict, top_announcements: list,
                          period_start: str, period_end: str) -> str:
        """Generate research report content.

        Tries Claude API first; falls back to template-based generation.
        """
        # Try Claude-powered generation
        try:
            return self._generate_with_claude(stats, top_announcements, period_start, period_end)
        except Exception as e:
            logger.warning(f"Claude generation failed, using template: {e}")
            return self._generate_template(stats, top_announcements, period_start, period_end)

    def _generate_with_claude(self, stats: dict, top_announcements: list,
                               period_start: str, period_end: str) -> str:
        """Generate content using Claude API."""
        import anthropic

        client = anthropic.Anthropic()

        # Prepare announcement summaries for prompt
        ann_summaries = []
        for i, ann in enumerate(top_announcements, 1):
            ann_summaries.append(
                f"{i}. [{ann.source}] {ann.title} (관련성: {ann.relevance_score:.0%})\n"
                f"   요약: {ann.summary[:200] if ann.summary else '없음'}"
            )

        prompt = f"""다음은 {period_start} ~ {period_end} 기간 동안 수집된 농업/사업 공고 데이터 분석입니다.

## 통계
- 총 수집 공고: {stats['total_announcements']}건
- 도메인 분포: {json.dumps(stats.get('domain_distribution', {}), ensure_ascii=False)}
- 출처 분포: {json.dumps(stats.get('source_distribution', {}), ensure_ascii=False)}
- 관련성 분포: 높음 {stats['relevance_distribution']['high']}건, 중간 {stats['relevance_distribution']['medium']}건, 낮음 {stats['relevance_distribution']['low']}건
- 평균 관련성: {stats['average_relevance']:.1%}

## 주요 공고 (관련성 상위 10건)
{chr(10).join(ann_summaries)}

## 사용자 컨텍스트
- 이상민, (주)어반정글 대표 (조경/치유산업/제조/공공조달)
- 네이처커뮤니티 (이끼 재배, 스마트팜)
- AI 1인 크리에이터 사업

위 데이터를 분석하여 다음 섹션으로 구성된 한국어 리서치 보고서를 작성하세요:

1. **분기 개요** - 전체 트렌드 요약
2. **도메인별 분석** - 각 사업 도메인 동향
3. **트렌드 발견** - 새로운 패턴이나 키워드
4. **전략 제안** - 다음 분기 대응 전략
5. **다음 분기 예측** - 예상되는 공고 트렌드

마크다운 형식으로 작성하세요."""

        response = client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text

    def _generate_template(self, stats: dict, top_announcements: list,
                            period_start: str, period_end: str) -> str:
        """Fallback template-based report generation (no API needed)."""
        period_label = self._period_label(period_start, period_end)

        lines = [
            "## 1. 분기 개요",
            "",
            f"**{period_label}** 기간 동안 총 **{stats['total_announcements']}건**의 공고가 수집되었습니다.",
            f"평균 관련성 점수: **{stats['average_relevance']:.1%}**",
            "",
            "## 2. 도메인별 분석",
            "",
        ]

        domain_dist = stats.get("domain_distribution", {})
        if domain_dist:
            lines.append("| 도메인 | 건수 |")
            lines.append("|--------|------|")
            for domain, count in sorted(domain_dist.items(), key=lambda x: x[1], reverse=True):
                korean = DOMAIN_KOREAN_MAP.get(domain, domain)
                lines.append(f"| {korean} | {count} |")
        else:
            lines.append("(도메인 분류 데이터 없음)")

        lines.extend([
            "",
            "## 3. 출처별 분포",
            "",
        ])

        source_dist = stats.get("source_distribution", {})
        if source_dist:
            lines.append("| 출처 | 건수 |")
            lines.append("|------|------|")
            for source, count in sorted(source_dist.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"| {source} | {count} |")

        lines.extend([
            "",
            "## 4. 관련성 분포",
            "",
            f"- 높음 (≥70%): {stats['relevance_distribution']['high']}건",
            f"- 중간 (30~70%): {stats['relevance_distribution']['medium']}건",
            f"- 낮음 (<30%): {stats['relevance_distribution']['low']}건",
            "",
            "## 5. 주요 공고 (상위 10건)",
            "",
        ])

        for i, ann in enumerate(top_announcements, 1):
            lines.append(f"{i}. **{ann.title}** [{ann.source}] (관련성: {ann.relevance_score:.0%})")
            if ann.summary:
                lines.append(f"\t{ann.summary[:150]}")
            lines.append("")

        lines.extend([
            "## 6. 전략 제안",
            "",
            "(Claude API 분석 미사용 -- 향후 자동 생성 예정)",
            "",
            "---",
            f"*데이터 기준: {period_start} ~ {period_end}*",
        ])

        return "\n".join(lines)

    @staticmethod
    def _period_label(start: str, end: str) -> str:
        """Generate a human-readable period label."""
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            quarter = (start_dt.month - 1) // 3 + 1
            return f"{start_dt.year}년 Q{quarter}"
        except ValueError:
            return f"{start} ~ {end}"
