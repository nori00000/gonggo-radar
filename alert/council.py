"""협의회 적재 프로파일 — 회사 프로파일과 독립된 순수 점수 계산 (P0 계약 §A).

이 모듈은 DB·네트워크·크롤러 상태를 보지 않는다. 입력은 설정에서 온 어휘
묶음과 항목 텍스트뿐이고, 출력은 :class:`CouncilVerdict` 하나다.

**회사 알림 경로(``alert.analyzer.KeywordAnalyzer``)는 이 모듈을 import 하지
않는다.** 두 프로파일이 한 함수를 공유하면 협의회 어휘를 넓힐 때마다 회사
알림이 같이 흔들린다 — 계약 불변 조건 1(회사용 알림 경로 무변경)은 코드
분리로 지킨다.

어휘 정본은 ``alert/config.yaml`` 의 ``council_profile:`` 블록이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

__all__ = [
    "CouncilVerdict",
    "SCORE_MUST_MATCH",
    "SCORE_PER_TAG",
    "TAG_SCORE_CAP",
    "normalize",
    "score_item",
]

# ---------------------------------------------------------------------------
# 점수 규칙 — 회사 analyzer 와 **같은 모양, 다른 저장소**
# ---------------------------------------------------------------------------

SCORE_MUST_MATCH = 0.5   # must_match 가 하나라도 맞으면 기본점
SCORE_PER_TAG = 0.05     # eligibility/region 태그 1개당 가산
TAG_SCORE_CAP = 0.25     # 태그 계열(자격·지역)별 가산 상한

# 표기 흔들림 흡수 — council-vocab.md §1 메모 "공백·괄호 무시 정규화 권장".
# `(예비)사회적기업` → `예비사회적기업`, `[경기강원센터]` → `경기강원센터`,
# `나눔의 숲` → `나눔의숲` 이 같은 어휘로 잡힌다.
_STRIP_CHARS = " \t\r\n()[]{}<>（）［］「」『』"


def normalize(text: Any) -> str:
    """매칭용 정규화 — 공백·괄호류를 지우고 소문자로 내린다."""
    if not text:
        return ""
    out = str(text)
    for ch in _STRIP_CHARS:
        out = out.replace(ch, "")
    return out.lower()


@dataclass
class CouncilVerdict:
    """한 항목에 대한 협의회 프로파일 판정.

    Attributes:
        score: 협의회 프로파일 점수 (0.0~1.0). 회사 relevance_score 와 무관.
        tags: ``{"eligibility": [...], "region": [...]}`` — 매치된 태그만 담는다.
        match: 협의회 적재 대상이면 1, 아니면 0.
        reason: 판정 근거 한 줄 (사람이 읽는 용도).
    """

    score: float = 0.0
    tags: Dict[str, List[str]] = field(default_factory=dict)
    match: int = 0
    reason: str = ""

    def tags_json(self) -> str:
        """DB ``council_tags`` 컬럼에 넣을 JSON 문자열."""
        return json.dumps(self.tags, ensure_ascii=False)


def _matched_terms(haystack: str, terms: Sequence[str]) -> List[str]:
    """``terms`` 중 ``haystack`` 에 있는 것을 설정 순서대로 (중복 없이)."""
    hits: List[str] = []
    for term in terms:
        needle = normalize(term)
        if needle and needle in haystack and term not in hits:
            hits.append(term)
    return hits


def _matched_tags(haystack: str, table: Mapping[str, Sequence[str]]) -> List[str]:
    """표기 변형 표(``태그 → 표기들``)에서 걸린 **태그**를 설정 순서대로."""
    hits: List[str] = []
    for tag, aliases in table.items():
        for alias in aliases:
            needle = normalize(alias)
            if needle and needle in haystack:
                hits.append(tag)
                break
    return hits


def score_item(
    profile: Any,
    source: str,
    title: str,
    summary: str = "",
    target: str = "",
    category: str = "",
) -> CouncilVerdict:
    """협의회 프로파일로 한 항목을 채점한다 — 순수 함수.

    판정 순서는 council-vocab.md 운영 메모와 같다:
    소스 풀 → 제외어 → must_match → eligibility/region 태깅.

    Args:
        profile: ``alert.config.CouncilProfileConfig`` (또는 같은 속성을 가진 객체).
        source: 항목의 소스 이름.
        title: 제목.
        summary: 요약.
        target: 지원대상.
        category: 분류.

    Returns:
        :class:`CouncilVerdict`. ``match=1`` 이면 협의회 적재 대상이다.
    """
    if not profile or source not in (getattr(profile, "sources", None) or ()):
        return CouncilVerdict(reason="협의회 소스 아님")

    haystack = normalize(" ".join([title or "", summary or "", target or "", category or ""]))

    excluded = _matched_terms(haystack, getattr(profile, "exclude", None) or ())
    if excluded:
        return CouncilVerdict(reason=f"제외 키워드: {excluded[0]}")

    must_hits = _matched_terms(haystack, getattr(profile, "must_match", None) or ())
    eligibility = _matched_tags(haystack, getattr(profile, "eligibility", None) or {})
    region = _matched_tags(haystack, getattr(profile, "region", None) or {})

    tags: Dict[str, List[str]] = {}
    if eligibility:
        tags["eligibility"] = eligibility
    if region:
        tags["region"] = region

    score = SCORE_MUST_MATCH if must_hits else 0.0
    score += min(SCORE_PER_TAG * len(eligibility), TAG_SCORE_CAP)
    score += min(SCORE_PER_TAG * len(region), TAG_SCORE_CAP)
    score = min(round(score, 4), 1.0)

    if not must_hits:
        # 태그만으로는 적재하지 않는다 — 지역어 하나로 부처 공고 전량이
        # 들어오는 것을 막는 유일한 관문이다.
        return CouncilVerdict(score=score, tags=tags, match=0, reason="협의회 어휘 없음")

    return CouncilVerdict(
        score=score,
        tags=tags,
        match=1,
        reason=f"협의회 어휘 {len(must_hits)}개" + (f" + 태그 {len(eligibility) + len(region)}개"
                                                 if (eligibility or region) else ""),
    )
