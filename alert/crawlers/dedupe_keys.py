"""중복 판별 키 - 크롤러와 정리 스크립트가 **같은 규칙**을 쓰도록 모아 둔다.

2026-09-13 Codex 재검토 #4: seis 카드의 지역은 ``span.sub`` 에만 있는 것이
아니라 ``ul.info`` 에 있을 때도 있다. 한쪽만 보면 같은 제목·같은 사업명에
지역만 다른 공고(서울/부산)가 한 건으로 병합된다. 재검토 #5·#6: 정리
스크립트의 규칙 A·B·C도 같은 키로 판단해야 크롤러와 어긋나지 않는다.

키는 세 조각이다::

    (정규화 제목, 지역, 마감 키)

- **정규화 제목**: 공백·구분기호 제거
- **지역**: 후보 문자열(``sub`` + ``info``) 중 **지역 토큰을 담은 첫 값**을
  그대로 쓴다. 토큰만 잘라 쓰지 않는 이유는 "경기 광주시" 와 "광주광역시"
  처럼 다른 지역이 같은 토큰을 공유하기 때문이다 - 값 전체를 쓰면 최악의
  경우 병합을 놓칠 뿐이고, 토큰만 쓰면 다른 공고를 합쳐 버린다.
- **마감 키**: 접수 종료일. 종료일이 없으면 적재 월(``YYYY-MM``)로 갈라
  회차가 다른 공고가 합쳐지지 않게 한다.
"""
import re
from typing import Iterable, Optional, Sequence, Tuple

# 광역 지자체와 자주 쓰이는 축약형. 값 전체를 키로 쓰므로 이 목록은
# "지역 이야기를 하는 값인지" 판별하는 데만 쓴다.
REGION_TOKENS: Tuple[str, ...] = (
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충청북도", "충남", "충청남도",
    "전북", "전라북도", "전남", "전라남도",
    "경북", "경상북도", "경남", "경상남도", "제주",
)

# "경기남부", "서울인천센터" 처럼 지역명이 붙은 형태도 잡는다
_REGION_RE = re.compile("|".join(re.escape(token) for token in REGION_TOKENS))

_TITLE_NOISE = re.compile(r"[\s·.,()\[\]{}「」『』\-~/]+")


def normalize_title(title: str) -> str:
    """공백·구분기호를 없앤 비교용 제목."""
    return _TITLE_NOISE.sub("", title or "")


def has_region_token(value: str) -> bool:
    """이 문자열이 지역을 말하고 있는지 본다."""
    return bool(value) and bool(_REGION_RE.search(value))


def extract_region(candidates: Iterable[str]) -> str:
    """후보 문자열 중 지역 토큰을 담은 첫 값을 지역으로 본다.

    Args:
        candidates: ``span.sub`` 와 ``ul.info`` 값들 (순서대로)

    Returns:
        지역을 말하는 값 그대로. 없으면 빈 문자열
    """
    for value in candidates:
        text = (value or "").strip()
        if has_region_token(text):
            return text
    return ""


def deadline_key(period_end: Optional[str], ingested_month: str = "") -> str:
    """마감 키 - 종료일이 없으면 적재 월로 갈라 회차를 구분한다.

    Args:
        period_end: 접수 종료일 (ISO) 또는 None
        ingested_month: ``YYYY-MM`` 적재 월. 종료일이 없을 때만 쓴다

    Returns:
        그룹 키로 쓸 문자열
    """
    if period_end:
        return period_end
    if ingested_month:
        return f"month:{ingested_month}"
    return ""


def group_key(
    title: str,
    region_candidates: Sequence[str],
    period_end: Optional[str],
    ingested_month: str = "",
) -> Tuple[str, str, str]:
    """중복 판별 키를 만든다.

    Args:
        title: 공고 제목
        region_candidates: 지역 후보 문자열들 (sub, info…)
        period_end: 접수 종료일 (ISO) 또는 None
        ingested_month: ``YYYY-MM`` 적재 월 (종료일 없을 때 회차 구분용)

    Returns:
        ``(정규화 제목, 지역, 마감 키)``
    """
    return (
        normalize_title(title),
        extract_region(region_candidates),
        deadline_key(period_end, ingested_month),
    )
