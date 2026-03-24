"""Two-stage announcement analysis: keyword matching + Claude API."""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

from .config import get_config
from .db import Database
from .models import AnalyzedAnnouncement, Keyword, RawAnnouncement

logger = logging.getLogger(__name__)

# Try to import anthropic; if not available, Claude analysis will be skipped
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    logger.warning(
        "anthropic SDK not installed. Claude analysis will be disabled. "
        "Install with: pip install anthropic"
    )


# ---------------------------------------------------------------------------
# User Context for Claude
# ---------------------------------------------------------------------------

USER_CONTEXT = """
경기도 고양시에서 이끼 재배(스마트팜/시설원예)를 운영 중인 사회적기업입니다.

회사명: (주)어반정글
- 사회적기업(사회적협동조합)
- 주요 사업:
  1. 조경 설계·시공·관리 (도시녹화, 학교숲, 탄소숲, 도시재생)
  2. 치유산업 (산림치유, 원예치료, 노인복지)
  3. 제조업 (반려동물 굿즈, 이끼 기반 사인물/조형물)
  4. 스마트팜 (이끼 재배, ICT 융합 시설원예)

공공조달 참여: 나라장터, 벤처나라, 학교장터

관심 분야:
- 이끼 재배 및 스마트팜 기술
- 도시녹화, 조경, 학교숲, 탄소숲
- 도시재생, 인공지반녹화, 옥상녹화, 벽면녹화
- 치유농업, 산림치유
- 사회적경제, 사회적기업 지원
- AI/ICT 융합 농업
- 공공조달 우선구매
""".strip()


# ---------------------------------------------------------------------------
# Stage 1: Keyword Matching (Local, Free)
# ---------------------------------------------------------------------------

class KeywordAnalyzer:
    """Stage 1: Local keyword-based relevance scoring.

    Matches announcements against must_match, boost, and exclude keywords
    from config and database.
    """

    def __init__(self, db: Optional[Database] = None):
        """Initialize with keywords from config and database.

        Args:
            db: Optional Database instance. If None, creates a new connection.
        """
        self.config = get_config()
        self.db = db or Database()
        self._own_db = db is None  # Track if we created the DB connection

        # Load keywords from database
        self.keywords_by_category = self._load_keywords()

    def _load_keywords(self) -> dict[str, List[Keyword]]:
        """Load keywords from database, grouped by category."""
        all_keywords = self.db.get_keywords()

        # Initialize DB keywords if empty
        if not all_keywords:
            logger.info("Initializing keywords from config...")
            self.db.init_default_keywords()
            all_keywords = self.db.get_keywords()

        # Group by category
        by_category: dict[str, List[Keyword]] = {
            "must_match": [],
            "boost": [],
            "exclude": [],
        }
        for kw in all_keywords:
            if kw.is_active and kw.category in by_category:
                by_category[kw.category].append(kw)

        logger.info(
            f"Loaded keywords: must_match={len(by_category['must_match'])}, "
            f"boost={len(by_category['boost'])}, exclude={len(by_category['exclude'])}"
        )
        return by_category

    def analyze(self, announcement: RawAnnouncement) -> AnalyzedAnnouncement:
        """Analyze a single announcement using keyword matching.

        Scoring logic:
        1. Check exclude keywords first -> score = 0.0 if any match
        2. must_match: if any keyword found, base score = 0.5
        3. boost: each keyword adds weight (0.05 per keyword, capped at 0.5)
        4. Total score = must_match_score + boost_score, capped at 1.0

        Args:
            announcement: Raw announcement to analyze.

        Returns:
            AnalyzedAnnouncement with relevance_score, relevance_reason, and matched_keywords.
        """
        # Combine text fields for searching
        search_text = " ".join([
            announcement.title,
            announcement.summary,
            announcement.target,
            announcement.category,
        ]).lower()

        matched_keywords: List[str] = []
        must_match_score = 0.0
        boost_score = 0.0

        # Step 1: Check exclude keywords
        for kw in self.keywords_by_category["exclude"]:
            if kw.keyword.lower() in search_text:
                return AnalyzedAnnouncement(
                    **announcement.__dict__,
                    relevance_score=0.0,
                    relevance_reason="제외 키워드 발견",
                    matched_keywords=[kw.keyword],
                )

        # Step 2: must_match keywords
        for kw in self.keywords_by_category["must_match"]:
            if kw.keyword.lower() in search_text:
                must_match_score = 0.5
                matched_keywords.append(kw.keyword)

        # Step 3: boost keywords
        boost_keywords = []
        for kw in self.keywords_by_category["boost"]:
            if kw.keyword.lower() in search_text:
                boost_keywords.append(kw.keyword)
                matched_keywords.append(kw.keyword)

        # Calculate boost score: 0.05 per keyword, max 0.5
        if boost_keywords:
            boost_score = min(0.05 * len(boost_keywords), 0.5)

        # Total score
        total_score = min(must_match_score + boost_score, 1.0)

        # Build reason
        if total_score == 0.0:
            reason = "관련 키워드 없음"
        else:
            reason_parts = []
            if must_match_score > 0:
                reason_parts.append("필수 키워드 매칭")
            if boost_keywords:
                reason_parts.append(f"추가 키워드 {len(boost_keywords)}개")
            reason = " + ".join(reason_parts)

        return AnalyzedAnnouncement(
            **announcement.__dict__,
            relevance_score=total_score,
            relevance_reason=reason,
            matched_keywords=matched_keywords,
        )

    def analyze_batch(self, announcements: List[RawAnnouncement]) -> List[AnalyzedAnnouncement]:
        """Analyze multiple announcements and filter by threshold.

        Args:
            announcements: List of raw announcements.

        Returns:
            List of analyzed announcements with score >= keyword_threshold,
            sorted by score descending.
        """
        threshold = self.config.analyzer.keyword_threshold

        results = []
        for ann in announcements:
            analyzed = self.analyze(ann)
            if analyzed.relevance_score >= threshold:
                results.append(analyzed)

        # Sort by score descending
        results.sort(key=lambda x: x.relevance_score, reverse=True)

        logger.info(
            f"Keyword analysis: {len(announcements)} total, "
            f"{len(results)} above threshold ({threshold})"
        )
        return results

    def close(self) -> None:
        """Close database connection if we own it."""
        if self._own_db and self.db:
            self.db.close()


# ---------------------------------------------------------------------------
# Stage 2: Claude API Analysis
# ---------------------------------------------------------------------------

class ClaudeAnalyzer:
    """Stage 2: AI-powered relevance analysis using Claude API.

    Refines keyword-matched announcements with contextual understanding.
    """

    def __init__(self):
        """Initialize Claude analyzer with API key from config."""
        self.config = get_config()
        self.api_key = self.config.analyzer.api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model = self.config.analyzer.claude_model

        if not ANTHROPIC_AVAILABLE:
            logger.warning("Claude analysis unavailable: anthropic SDK not installed")
            self.client = None
        elif not self.api_key:
            logger.warning("Claude analysis unavailable: ANTHROPIC_API_KEY not set")
            self.client = None
        else:
            self.client = anthropic.Anthropic(api_key=self.api_key)
            logger.info(f"Claude analyzer initialized with model: {self.model}")

    def analyze(
        self,
        announcement: AnalyzedAnnouncement,
        user_context: str = USER_CONTEXT
    ) -> AnalyzedAnnouncement:
        """Analyze a single announcement using Claude API.

        Args:
            announcement: Announcement with keyword score.
            user_context: Description of user's situation and interests.

        Returns:
            Updated announcement with Claude's score and reason.
            Falls back to keyword score if API fails.
        """
        if not self.client:
            logger.debug(f"Skipping Claude analysis for: {announcement.title}")
            return announcement

        try:
            # Build prompt
            system_prompt = f"""당신은 정부 및 지자체 공고의 관련성을 평가하는 전문가입니다.

다음은 사용자의 상황입니다:
{user_context}

공고의 관련성을 0.0~1.0 점수로 평가하고, JSON 형식으로 응답하세요.

응답 형식:
{{
  "score": 0.0~1.0,
  "reason": "평가 이유 (한국어, 2-3문장)",
  "matched_aspects": ["매칭된 관심사1", "매칭된 관심사2"]
}}"""

            user_prompt = f"""다음 공고를 평가해주세요:

제목: {announcement.title}
요약: {announcement.summary}
대상: {announcement.target}
분류: {announcement.category}
기간: {announcement.period_start or ''} ~ {announcement.period_end or ''}

키워드 매칭 점수: {announcement.relevance_score:.2f}
매칭된 키워드: {', '.join(announcement.matched_keywords)}"""

            # Call Claude API
            message = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}]
            )

            # Parse response
            response_text = message.content[0].text
            result = json.loads(response_text)

            # Update announcement
            announcement.relevance_score = float(result.get("score", announcement.relevance_score))
            announcement.relevance_reason = result.get("reason", announcement.relevance_reason)

            # Add matched aspects to reason if provided
            matched_aspects = result.get("matched_aspects", [])
            if matched_aspects:
                announcement.relevance_reason += f" (매칭: {', '.join(matched_aspects)})"

            logger.debug(
                f"Claude analysis complete: {announcement.title} -> "
                f"score={announcement.relevance_score:.2f}"
            )

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response: {e}")
        except Exception as e:
            logger.error(f"Claude API error for '{announcement.title}': {e}")

        return announcement

    def analyze_batch(
        self,
        announcements: List[AnalyzedAnnouncement],
        user_context: str = USER_CONTEXT
    ) -> List[AnalyzedAnnouncement]:
        """Analyze multiple announcements using Claude API.

        Only processes announcements with keyword score >= claude_threshold.
        Respects max_claude_calls_per_run limit.

        Args:
            announcements: List of keyword-analyzed announcements.
            user_context: Description of user's situation.

        Returns:
            Updated announcements sorted by score descending.
        """
        if not self.client:
            logger.info("Claude analysis skipped (client not available)")
            return announcements

        threshold = self.config.analyzer.claude_threshold
        max_calls = self.config.analyzer.max_claude_calls_per_run

        # Filter candidates above threshold
        candidates = [ann for ann in announcements if ann.relevance_score >= threshold]

        # Respect max calls limit
        to_analyze = candidates[:max_calls]

        logger.info(
            f"Claude analysis: {len(to_analyze)} candidates "
            f"(threshold={threshold}, max_calls={max_calls})"
        )

        # Analyze each candidate
        results = []
        for i, ann in enumerate(to_analyze, 1):
            logger.debug(f"Claude analyzing {i}/{len(to_analyze)}: {ann.title}")
            analyzed = self.analyze(ann, user_context)
            results.append(analyzed)

        # Add remaining announcements that weren't analyzed
        not_analyzed = candidates[max_calls:]
        results.extend(not_analyzed)

        # Add announcements below threshold (unmodified)
        below_threshold = [ann for ann in announcements if ann.relevance_score < threshold]
        results.extend(below_threshold)

        # Sort by final score
        results.sort(key=lambda x: x.relevance_score, reverse=True)

        logger.info(f"Claude analysis complete: {len(to_analyze)} analyzed")
        return results
