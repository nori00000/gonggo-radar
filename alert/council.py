"""협의회 적재 프로파일 — 회사 프로파일과 독립된 순수 점수 계산 (P0 계약 §A).

이 모듈은 DB·네트워크·크롤러 상태를 보지 않는다. 입력은 설정에서 온 어휘
묶음과 항목 텍스트뿐이고, 출력은 :class:`CouncilVerdict` 하나다.

**회사 알림 경로(``alert.analyzer.KeywordAnalyzer``)는 이 모듈을 import 하지
않는다.** 두 프로파일이 한 함수를 공유하면 협의회 어휘를 넓힐 때마다 회사
알림이 같이 흔들린다 — 계약 불변 조건 1(회사용 알림 경로 무변경)은 코드
분리로 지킨다.

매칭 규율 두 가지 (Codex 게이트 2R MEDIUM):

1. **필드별 판정.** 제목·요약·대상·분류를 **따로** 본다. 이어 붙이면 제목 끝
   `협동` + 요약 머리 `조합 지원사업` 이 `협동조합` 으로 잡힌다.
2. **제외어는 토큰 경계.** 나머지 어휘는 공백을 지우고(``normalize``) 부분
   문자열로 보지만, 제외어만은 공백을 남긴 토큰열(``tokens``)에 **정확히**
   맞아야 한다. 부분 문자열이면 `인사` 가 `인사이트`·`법인사업자` 를 죽인다.
   제외는 항목을 통째로 버리는 유일한 판정이라 다른 어휘보다 좁게 잡는다 —
   관찰 모드에서 과잉 제외는 조용하고, 과잉 포함은 표본에 보인다.

어휘 정본은 ``alert/config.yaml`` 의 ``council_profile:`` 블록이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "CouncilDrop",
    "CouncilVerdict",
    "KIND_GONGGO",
    "KIND_MEDIA",
    "SCORE_MUST_MATCH",
    "SCORE_PER_TAG",
    "TAG_SCORE_CAP",
    "normalize",
    "score_item",
    "tokens",
]

# ---------------------------------------------------------------------------
# 점수 규칙 — 회사 analyzer 와 **같은 모양, 다른 저장소**
# ---------------------------------------------------------------------------

# 소스 종류 (alert.main.SOURCE_KIND_* 와 같은 문자열). 여기에 둔 것은 이 모듈이
# 순수 함수 묶음이라 alert.main 을 import 할 수 없기 때문이다 — 값이 갈라지면
# ``test_council_profile`` 의 대조 테스트가 red 가 된다.
KIND_GONGGO = "gonggo"
KIND_MEDIA = "media"

SCORE_MUST_MATCH = 0.5   # must_match 가 하나라도 맞으면 기본점
SCORE_PER_TAG = 0.05     # eligibility/region 태그 1개당 가산
TAG_SCORE_CAP = 0.25     # 태그 계열(자격·지역)별 가산 상한

# 괄호·인용 부호류 — 어휘 판정에서 지운다.
# `(예비)사회적기업` → `예비사회적기업`, `[공모결과]` → `공모결과`.
_BRACKET_CHARS = "()[]{}<>（）［］「」『』"

# 공백까지 지우는 정규화가 추가로 먹는 문자 (어휘 판정 전용)
_SPACE_CHARS = " \t\r\n"

# 제외어 토큰화에서 **공백과 같이 취급**하는 구두점 (Codex 게이트 3R MEDIUM).
# 공백만 분리자로 보면 `인사·발령`·`인사, 사회적기업` 의 `인사` 가 토큰이
# 되지 못해 제외어를 빠져나간다. 이 목록은 ``tokens`` 에만 쓰이고
# ``normalize`` 에는 쓰지 않는다 — must_match 판정은 건드리지 않는다.
_PUNCT_CHARS = "·,./:;、。…!?~|\"\'“”‘’"


def normalize(text: Any) -> str:
    """어휘 판정용 정규화 — 괄호·공백을 지우고 소문자로 내린다.

    council-vocab.md §1 메모 "공백·괄호 무시 정규화 권장"을 구현한다.
    **한 필드 안에서만** 쓴다 (필드를 이어 붙인 문자열에 쓰면 안 된다).
    """
    if not text:
        return ""
    out = str(text)
    for ch in _BRACKET_CHARS + _SPACE_CHARS:
        out = out.replace(ch, "")
    return out.lower()


def tokens(text: Any) -> List[str]:
    """제외어 판정용 토큰 — 괄호·구두점을 공백으로 바꾸고 공백으로 자른다.

    ``normalize`` 와 달리 **공백을 지우지 않는다**. 제외어는 항목을 통째로
    버리는 유일한 판정이라 토큰 경계로만 맞아야 한다.
    """
    if not text:
        return []
    out = str(text)
    for ch in _BRACKET_CHARS + _PUNCT_CHARS:
        out = out.replace(ch, " ")
    return [tok for tok in out.lower().split() if tok]


@dataclass
class CouncilVerdict:
    """한 항목에 대한 협의회 프로파일 판정.

    Attributes:
        score: 협의회 프로파일 점수 (0.0~1.0). 회사 relevance_score 와 무관.
        tags: ``{"eligibility": [...], "region": [...]}`` — 매치된 태그만 담는다.
        match: 협의회 적재 대상이면 1, 아니면 0.
        reason: 판정 근거 한 줄 (사람이 읽는 용도 + 탈락 원장에 남는 값).
    """

    score: float = 0.0
    tags: Dict[str, List[str]] = field(default_factory=dict)
    match: int = 0
    reason: str = ""

    def tags_json(self) -> str:
        """DB ``council_tags`` 컬럼에 넣을 JSON 문자열."""
        return json.dumps(self.tags, ensure_ascii=False)


@dataclass
class CouncilDrop:
    """두 프로파일 **모두** 탈락한 협의회 소스 항목 (관찰 전용 원장).

    저장되지 않는 항목은 표본에 나타날 수 없어 오탈락이 영원히 조용하다
    (Codex 게이트 2R MEDIUM). 이 레코드는 ``council_dropped`` 테이블에만
    들어가고, 알림·브리핑 어느 쪽도 이 테이블을 읽지 않는다.
    """

    source: str
    source_id: str
    title: str
    url: str = ""
    posted_at: str = ""
    company_score: float = 0.0
    council_score: float = 0.0
    reason: str = ""


def _matched_terms(haystacks: Sequence[str], terms: Sequence[str]) -> List[str]:
    """``terms`` 중 **어느 한 필드**에 있는 것을 설정 순서대로 (중복 없이)."""
    hits: List[str] = []
    for term in terms:
        needle = normalize(term)
        if not needle:
            continue
        if any(needle in hay for hay in haystacks) and term not in hits:
            hits.append(term)
    return hits


def _matched_tags(
    haystacks: Sequence[str], table: Mapping[str, Sequence[str]]
) -> List[str]:
    """표기 변형 표(``태그 → 표기들``)에서 걸린 **태그**를 설정 순서대로."""
    hits: List[str] = []
    for tag, aliases in table.items():
        for alias in aliases:
            needle = normalize(alias)
            if needle and any(needle in hay for hay in haystacks):
                hits.append(tag)
                break
    return hits


def _token_sequence_in(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """``needle`` 토큰열이 ``haystack`` 토큰열에 **연속으로 정확히** 있는가."""
    if not needle or len(needle) > len(haystack):
        return False
    span = len(needle)
    return any(
        list(haystack[i:i + span]) == list(needle)
        for i in range(len(haystack) - span + 1)
    )


def _matched_exclude(
    token_fields: Sequence[Sequence[str]], terms: Sequence[str]
) -> Optional[str]:
    """토큰 경계로 걸린 첫 제외어. 없으면 ``None``."""
    for term in terms:
        needle = tokens(term)
        if not needle:
            continue
        if any(_token_sequence_in(field_tokens, needle) for field_tokens in token_fields):
            return term
    return None


def score_item(
    profile: Any,
    source: str,
    title: str,
    summary: str = "",
    target: str = "",
    category: str = "",
    kind: str = KIND_GONGGO,
) -> CouncilVerdict:
    """협의회 프로파일로 한 항목을 채점한다 — 순수 함수.

    판정 순서는 council-vocab.md 운영 메모와 같다:
    소스 풀 → 제외어(토큰 경계) → must_match → eligibility/region 태깅.
    모든 어휘 판정은 **필드별**로 이뤄진다.

    Args:
        profile: ``alert.config.CouncilProfileConfig`` (또는 같은 속성을 가진 객체).
        source: 항목의 소스 이름.
        title: 제목.
        summary: 요약.
        target: 지원대상.
        category: 분류.
        kind: 소스 종류 (``"gonggo"`` 또는 ``"media"``). ``media`` 면 must_match
            를 **제목에서만** 본다 (아래 참조).

    Returns:
        :class:`CouncilVerdict`. ``match=1`` 이면 협의회 적재 대상이다.
    """
    if not profile or source not in (getattr(profile, "sources", None) or ()):
        return CouncilVerdict(reason="협의회 소스 아님")

    fields = [title or "", summary or "", target or "", category or ""]
    haystacks = [normalize(f) for f in fields]
    token_fields = [tokens(f) for f in fields]

    excluded = _matched_exclude(token_fields, getattr(profile, "exclude", None) or ())
    if excluded:
        return CouncilVerdict(reason=f"제외 키워드: {excluded}")

    # H6 (감사 V §2·H-3): 2차 미디어의 must_match 는 **제목만** 본다.
    # media RSS 의 ``summary`` 는 전원 정확히 200자 기사 리드다(실측). 그 안에
    # 협의회 어휘가 하나만 있어도 0.5점이 붙어 kfnews 적재분 73/73(100%)이
    # 통과했다 — 인사기사의 직함 문자열(`산림복지국 산지정책과장`)까지 가산점을
    # 벌었다. 기사 리드는 "이 기사가 협의회 것인가" 의 근거가 아니다.
    # 제목만 보는 것은 월간호 렌더 규율과도 같다(요약은 렌더되지 않는다).
    # 제외어·태그는 종전대로 네 필드를 본다 — 제외는 넓게, 포함은 좁게.
    must_haystacks = [haystacks[0]] if kind == KIND_MEDIA else haystacks
    must_hits = _matched_terms(must_haystacks, getattr(profile, "must_match", None) or ())
    eligibility = _matched_tags(haystacks, getattr(profile, "eligibility", None) or {})
    region = _matched_tags(haystacks, getattr(profile, "region", None) or {})

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

    extra = len(eligibility) + len(region)
    return CouncilVerdict(
        score=score,
        tags=tags,
        match=1,
        reason=f"협의회 어휘 {len(must_hits)}개" + (f" + 태그 {extra}개" if extra else ""),
    )
