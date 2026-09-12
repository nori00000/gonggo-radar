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

# "전국" 은 특정 지역이 아니라 **모든 지역** 을 뜻한다. 같은 공고를
# 한쪽은 "서울", 한쪽은 "전국" 으로 적어 두면 별개 공고로 갈렸다
# (4차 게이트 #8). 와일드카드로 다룬다.
NATIONWIDE_TOKENS = ("전국", "전지역", "전 지역")


def normalize_title(title: str) -> str:
    """공백·구분기호를 없앤 비교용 제목."""
    return _TITLE_NOISE.sub("", title or "")


def has_region_token(value: str) -> bool:
    """이 문자열이 지역을 말하고 있는지 본다."""
    return bool(value) and bool(_REGION_RE.search(value))


def extract_region(candidates: Iterable[str]) -> str:
    """후보 중 지역을 말하는 첫 값 (표시·로그용).

    **그룹 키에는 쓰지 않는다** - 첫 값만 쓰면 다른 필드의 지역 차이를
    놓친다(최종 게이트 #1). 키는 ``subject_signature`` 를 쓴다.
    """
    for value in candidates:
        text = (value or "").strip()
        if has_region_token(text):
            return text
    return ""


def subject_signature(candidates: Iterable[str]) -> Tuple[str, ...]:
    """주체·지역 메타데이터 **전체**를 키로 쓸 형태로 만든다.

    최종 게이트 #1: 첫 후보만 지역으로 삼으면
    ``sub="서울 본부"`` 가 같고 ``info=서울/부산`` 만 다른 공고가 합쳐지고,
    지역 토큰 목록에 없는 시설명(``수원센터``/``성남센터``)도 구분되지
    않았다. 그래서 **두 필드의 값을 모두** 서명에 넣는다.

    값 전체를 쓰므로 갈라질 때는 최악의 경우 병합을 놓치고, 합쳐질 때는
    메타데이터가 완전히 같을 때뿐이다 - 안전한 방향이다.

    Args:
        candidates: ``span.sub`` 와 ``ul.info`` 값들

    Returns:
        정렬·중복제거된 값 튜플. 메타데이터가 없으면 빈 튜플
    """
    cleaned = {
        re.sub(r"\s+", " ", (value or "").strip())
        for value in candidates
        if (value or "").strip()
    }
    return tuple(sorted(cleaned))


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
) -> Tuple[str, Tuple[str, ...], str]:
    """중복 판별 키를 만든다.

    Args:
        title: 공고 제목
        region_candidates: 주체·지역 후보 문자열들 (sub, info…)
        period_end: 접수 종료일 (ISO) 또는 None
        ingested_month: ``YYYY-MM`` 적재 월 (종료일 없을 때 회차 구분용)

    Returns:
        ``(정규화 제목, 주체 서명, 마감 키)``
    """
    return (
        normalize_title(title),
        subject_signature(region_candidates),
        deadline_key(period_end, ingested_month),
    )


def is_nationwide(signature: Sequence[str]) -> bool:
    """주체 서명이 "전국" 을 말하는지 본다."""
    joined = " ".join(signature or ())
    return any(token in joined for token in NATIONWIDE_TOKENS)


def keys_compatible(
    first: Tuple[str, Tuple[str, ...], str],
    second: Tuple[str, Tuple[str, ...], str],
    ignore_deadline: bool = False,
) -> bool:
    """두 키가 **같은 공고**를 가리킬 수 있는지 본다.

    그룹핑에는 완전 일치를 쓰지만, 이미 기록된 병합(규칙 A)이나 같은 URL을
    확인할 때는 조금 느슨해야 한다:

    - 한쪽에 주체 메타데이터가 **없으면** 주체를 비교하지 않는다 - 대표에만
      메타데이터가 추가된 정상 병합을 거부하면 안 된다.
    - 한쪽이 **"전국"** 이면 어떤 지역과도 호환된다 (4차 게이트 #8).
    - ``ignore_deadline`` 이면 **마감을 모르는 경우에만** 마감 키를 넘긴다.
      실제 종료일이 서로 다르면 같은 공고가 아니다.

    Args:
        first: 키 하나
        second: 키 둘
        ignore_deadline: 마감을 모를 때 마감 키 비교를 건너뛴다

    Returns:
        같은 공고로 볼 수 있으면 True
    """
    if first[0] != second[0]:
        return False
    subjects_differ = bool(first[1]) and bool(second[1]) and first[1] != second[1]
    if subjects_differ and not (is_nationwide(first[1]) or is_nationwide(second[1])):
        return False
    if first[2] == second[2]:
        return True
    if ignore_deadline and (
        first[2].startswith("month:") or second[2].startswith("month:")
        or not first[2] or not second[2]
    ):
        return True
    return False
