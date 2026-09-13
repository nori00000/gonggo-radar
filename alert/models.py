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

    # 협의회 적재 프로파일 측정값 (P0 계약 §A). 회사 점수와 독립이며,
    # council_only=1 은 "회사 경로가 고르지 않은 행" 이라는 뜻이다.
    council_score: float = 0.0
    council_tags: str = "{}"
    council_match: int = 0
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
