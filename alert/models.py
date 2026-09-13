"""데이터 모델 -- 공고, 분석 결과, 키워드."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RawAnnouncement:
    """크롤러에서 수집한 원본 공고 데이터."""

    source: str               # "bizinfo", "mafra", "smartfarm", "goyang", "nongsaro"
    source_id: str            # 원본 사이트 고유 ID
    title: str
    url: str
    summary: str = ""
    author: str = ""          # 주관기관
    category: str = ""        # 분류
    target: str = ""          # 지원대상
    period_start: Optional[str] = None  # 접수시작일
    period_end: Optional[str] = None    # 접수마감일
    raw_data: str = ""        # JSON string of full original data
    fetched_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class AnalyzedAnnouncement(RawAnnouncement):
    """키워드/Claude 분석이 완료된 공고."""

    relevance_score: float = 0.0    # 0.0 ~ 1.0
    relevance_reason: str = ""
    matched_keywords: list = field(default_factory=list)
    id: Optional[int] = None

    # LLM 2차 분석이 **이 항목을 실제로 봤는가** (실행 중 상태, DB 컬럼 아님).
    # `max_claude_calls_per_run` 상한을 넘겼거나 백엔드가 실패하면 키워드 점수가
    # 그대로 남는데, 그 점수는 LLM 관문을 통과한 근거가 아니다 (계약 §A 라운드 5).
    llm_evaluated: bool = False

    # 협의회 적재 프로파일 측정값 (P0 계약 §A). 회사 점수와 독립이다.
    # 기본값 None = **미측정** (프로파일이 이 항목을 채점한 적이 없음).
    # council_only 만 가드라서 기본값이 0 이다 - 자세한 이유는 migration 7 주석.
    council_score: Optional[float] = None
    council_tags: Optional[str] = None
    council_match: Optional[int] = None
    council_only: int = 0


@dataclass
class Keyword:
    """필터링에 사용되는 키워드."""

    keyword: str
    category: str = "boost"   # must_match, boost, exclude
    weight: float = 1.0
    is_active: bool = True


@dataclass
class ClassifiedAnnouncement(AnalyzedAnnouncement):
    """도메인 분류가 완료된 공고."""
    business_domain: str = ""
    domain_confidence: float = 0.0
    similar_announcements: list = field(default_factory=list)


@dataclass
class ApplicationRecord:
    """공고별 신청/대응 이력."""
    id: Optional[int] = None
    announcement_id: int = 0
    status: str = "discovered"
    applied_date: str = ""
    result_date: str = ""
    result: str = ""
    prepared_docs: list = field(default_factory=list)
    notes: str = ""
    assigned_domain: str = ""
    priority: int = 2


@dataclass
class ResearchDocument:
    """자동 생성된 리서치/분석 문서."""
    id: Optional[int] = None
    title: str = ""
    doc_type: str = "quarterly"
    period_start: str = ""
    period_end: str = ""
    content: str = ""
    metadata: dict = field(default_factory=dict)
    obsidian_path: str = ""
    version: int = 1
