"""Two-stage announcement analysis: keyword matching + Claude API."""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

import requests

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
(주)어반정글은 고양시 소재 농업회사법인으로, 6개 핵심 사업 도메인을 운영합니다:

1. 이끼농업/스마트팜: 이끼(moss) 재배 전문기업. 모스월, 이끼테라리움, 이끼조경 등 이끼 관련 제품 생산.
   스마트팜 기술(IoT 센서, 환경제어, 정밀농업)을 이끼 재배에 적용.

2. 조경/도시녹화: 조경공사, 옥상녹화, 벽면녹화, 인공지반녹화 사업.
   학교숲, 탄소중립숲, 도시숲 조성. 정원박람회 참여 및 국가정원 조성 사업.

3. 치유농업: 농림축산식품부 치유농업사 인증. 치유농장 운영.
   산림치유, 원예치료, 그린케어 프로그램. 치유관광산업법(2026.4.9 시행) 관련 사업.

4. 사회적기업: 고용노동부 인증 사회적기업. 사회적경제기업, 사회적협동조합과 협력.
   행정안전부 사회연대경제 정책 관련 사업. 마을기업, 자활기업 네트워크.

5. AI/디지털전환: 농업 AI 기술 개발 (작물진단, 정밀농업, 빅데이터 분석).
   스마트팜 IoT 플랫폼 운영. 디지털전환 지원사업 참여.

6. 소공인/제조: 이끼 관련 제품 제조 (모스월 패널, 이끼 키트, 인테리어 소품).
   소공인 지원사업, 로컬크리에이터 활동.

공공조달 참여: 나라장터, 벤처나라, 학교장터
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
        matched_boost = []
        for kw in self.keywords_by_category["boost"]:
            if kw.keyword.lower() in search_text:
                matched_boost.append(kw)
                matched_keywords.append(kw.keyword)

        # Calculate boost score: 0.05 * weight per keyword, max 0.5
        if matched_boost:
            boost_score = min(sum(0.05 * kw.weight for kw in matched_boost), 0.5)

        # Total score
        total_score = min(must_match_score + boost_score, 1.0)

        # Build reason
        if total_score == 0.0:
            reason = "관련 키워드 없음"
        else:
            reason_parts = []
            if must_match_score > 0:
                reason_parts.append("필수 키워드 매칭")
            if matched_boost:
                reason_parts.append(f"추가 키워드 {len(matched_boost)}개")
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
    """Stage 2: AI-powered relevance analysis using Claude API or Ollama.

    Refines keyword-matched announcements with contextual understanding.
    Supports two backends: 'claude' (Anthropic API) and 'ollama' (OpenAI-compatible).
    Backend is selected via LLM_BACKEND env var, config llm_backend field, or auto-detection.
    """

    def __init__(self):
        """Initialize analyzer with auto-detected or configured backend."""
        self.config = get_config()
        self.api_key = self.config.analyzer.api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model = self.config.analyzer.claude_model

        # Determine backend: env var overrides config, config overrides auto
        backend = (
            os.getenv("LLM_BACKEND")
            or self.config.analyzer.llm_backend
            or "auto"
        )
        if backend == "auto":
            backend = "claude" if self.api_key else "ollama"

        self.backend = backend
        self.client = None  # Used for claude backend
        self._ollama_base_url: str = self.config.analyzer.ollama_base_url
        self._ollama_model: str = self.config.analyzer.ollama_model

        if self.backend == "claude":
            if not ANTHROPIC_AVAILABLE:
                logger.warning("Claude analysis unavailable: anthropic SDK not installed")
            elif not self.api_key:
                logger.warning("Claude analysis unavailable: ANTHROPIC_API_KEY not set")
            else:
                self.client = anthropic.Anthropic(api_key=self.api_key)
                logger.info(f"Claude analyzer initialized with model: {self.model}")
        elif self.backend == "ollama":
            logger.info(
                f"Ollama analyzer initialized: model={self._ollama_model}, "
                f"url={self._ollama_base_url}"
            )
        else:
            logger.warning(f"Unknown LLM backend '{self.backend}', analysis will be skipped")

    def _build_prompts(
        self,
        announcement: AnalyzedAnnouncement,
        user_context: str,
    ) -> tuple[str, str]:
        """Build system and user prompts for LLM analysis."""
        system_prompt = f"""당신은 정부 및 지자체 공고의 관련성을 평가하는 전문가입니다.

다음은 사용자의 상황입니다:
{user_context}

공고의 관련성을 0.0~1.0 점수로 평가하고, JSON 형식으로 응답하세요.

점수 기준:
- 0.3: 간접적 연관성 (비슷한 분야이나 직접 관련은 아님)
- 0.5: 중간 관련성 (일부 사업 영역과 관련)
- 0.7: 직접 관련성 (핵심 사업 영역과 직접 관련)
- 0.9: 완전 일치 (핵심 비즈니스 도메인과 완전 일치)

matched_aspects 필드에는 매칭된 비즈니스 도메인을 포함하세요:
- 이끼농업/스마트팜
- 조경/도시녹화
- 치유농업
- 사회적기업
- AI/디지털전환
- 소공인/제조

응답 형식:
{{
  "score": 0.0~1.0,
  "reason": "평가 이유 (한국어, 2-3문장)",
  "matched_aspects": ["매칭된 도메인1", "매칭된 도메인2"]
}}"""

        user_prompt = f"""다음 공고를 평가해주세요:

제목: {announcement.title}
요약: {announcement.summary}
대상: {announcement.target}
분류: {announcement.category}
기간: {announcement.period_start or ''} ~ {announcement.period_end or ''}

키워드 매칭 점수: {announcement.relevance_score:.2f}
매칭된 키워드: {', '.join(announcement.matched_keywords)}"""

        return system_prompt, user_prompt

    def _call_claude(self, system_prompt: str, user_prompt: str) -> str:
        """Call Claude API and return response text."""
        message = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        return message.content[0].text

    def _call_ollama(self, system_prompt: str, user_prompt: str) -> str:
        """Call Ollama OpenAI-compatible API and return response text."""
        response = requests.post(
            f"{self._ollama_base_url}/v1/chat/completions",
            json={
                "model": self._ollama_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
        response.raise_for_status()
        result = response.json()
        return result["choices"][0]["message"]["content"]

    def analyze(
        self,
        announcement: AnalyzedAnnouncement,
        user_context: str = USER_CONTEXT
    ) -> AnalyzedAnnouncement:
        """Analyze a single announcement using the configured LLM backend.

        Args:
            announcement: Announcement with keyword score.
            user_context: Description of user's situation and interests.

        Returns:
            Updated announcement with LLM score and reason.
            Falls back to keyword score if API fails.
        """
        # Check availability per backend
        if self.backend == "claude" and not self.client:
            logger.debug(f"Skipping Claude analysis for: {announcement.title}")
            return announcement
        if self.backend not in ("claude", "ollama"):
            logger.debug(f"Skipping LLM analysis (unknown backend): {announcement.title}")
            return announcement

        try:
            system_prompt, user_prompt = self._build_prompts(announcement, user_context)

            if self.backend == "claude":
                response_text = self._call_claude(system_prompt, user_prompt)
            else:
                response_text = self._call_ollama(system_prompt, user_prompt)

            # Parse response
            result = json.loads(response_text)

            # Update announcement
            announcement.relevance_score = float(result.get("score", announcement.relevance_score))
            announcement.relevance_reason = result.get("reason", announcement.relevance_reason)

            # Add matched aspects to reason if provided
            matched_aspects = result.get("matched_aspects", [])
            if matched_aspects:
                announcement.relevance_reason += f" (매칭: {', '.join(matched_aspects)})"

            logger.debug(
                f"{self.backend} analysis complete: {announcement.title} -> "
                f"score={announcement.relevance_score:.2f}"
            )

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response: {e}")
        except requests.RequestException as e:
            logger.error(f"Ollama connection error for '{announcement.title}': {e}")
        except Exception as e:
            logger.error(f"LLM API error for '{announcement.title}': {e}")

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
        # Skip if backend is unavailable
        if self.backend == "claude" and not self.client:
            logger.info("LLM analysis skipped (Claude client not available)")
            return announcements
        if self.backend not in ("claude", "ollama"):
            logger.info(f"LLM analysis skipped (unknown backend: {self.backend})")
            return announcements

        threshold = self.config.analyzer.claude_threshold
        max_calls = self.config.analyzer.max_claude_calls_per_run

        # Filter candidates above threshold
        candidates = [ann for ann in announcements if ann.relevance_score >= threshold]

        # Respect max calls limit
        to_analyze = candidates[:max_calls]

        logger.info(
            f"{self.backend} analysis: {len(to_analyze)} candidates "
            f"(threshold={threshold}, max_calls={max_calls})"
        )

        # Analyze each candidate
        results = []
        for i, ann in enumerate(to_analyze, 1):
            logger.debug(f"{self.backend} analyzing {i}/{len(to_analyze)}: {ann.title}")
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

        logger.info(f"{self.backend} analysis complete: {len(to_analyze)} analyzed")
        return results
