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
- **마감 키**: 접수 종료일. 13차부터는 대부분의 소스가 종료일이 없으므로
  ``slot_key`` 가 **목록에 적힌 마지막 날짜**를 슬롯으로 쓴다(저장하지
  않는다 - 기간이라고 주장하지 않고 그룹만 가른다). 날짜도 없으면 적재
  월(``YYYY-MM``)·회차로 갈라 회차가 다른 공고가 합쳐지지 않게 한다.
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

# 회차 표기 (6차 게이트 #2). 마감을 모르는 공고에서는 회차가 유일한
# 구분자다 - 같은 제목·같은 센터의 1차와 2차는 별개 공고다.
#
# **"수시" 는 뺐다** (7차 게이트 #5): 기관명 "여수시청" 안에 들어 있어
# "여수시청 지원사업" 은 회차 "수시", "여수 시청 지원사업" 은 빈 값이 되어
# 같은 공고가 갈렸다. 부분 문자열로 회차를 만들면 안 된다.
#   - ``\d+차`` 는 뒤에 한글이 이어지면(차수·차량…) 회차가 아니다
#   - ``상시/추가/연장`` 은 앞에 한글이 붙어 있으면 다른 낱말의 일부다
_ROUND_TOKENS = re.compile(
    r"(?<![가-힣])(?:(\d+)\s*차(?![가-힣])|상시|추가|연장)"
)
# 상시 접수를 뜻하는 표기 (크롤러가 raw_data.always_open 에 쓴다)
ALWAYS_OPEN_TOKENS = re.compile(r"(?<![가-힣])상시")

# "전국" 와일드카드는 **철회했다** (5차 게이트 #3 REGRESSED).
# 지역 하나를 넘기려던 완화가 주체 서명 전체를 무효화해, 대표가
# ``sub=서울센터, info=[전국]`` 이면 ``sub=부산센터, info=[부산]`` 인
# **정상 공고까지 삭제 승인**됐다. 서울/전국 표기가 갈리는 비용(병합을
# 놓침)이 정상 행을 지우는 비용보다 싸다. 지역은 엄격 비교한다.


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


def extract_round(candidates: Iterable[str]) -> str:
    """제목·주체·분류에서 회차 표기를 모아 하나의 키로 만든다.

    ``2차``, ``상시``, ``추가``, ``연장`` 같은 말이 회차를 가른다. 여러 개가
    보이면 모두 담는다(정렬해 순서 무관).

    Args:
        candidates: 제목·``sub``·``info`` 등 문자열들

    Returns:
        ``"2차"`` / ``"상시"`` / ``"1차|추가"`` 형태. 없으면 빈 문자열
    """
    found = set()
    for value in candidates:
        for match in _ROUND_TOKENS.finditer(value or ""):
            number = match.group(1)
            found.add(f"{int(number)}차" if number else match.group(0).strip())
    return "|".join(sorted(found))


# 목록에 **적혀 있는** 날짜 (기간으로 승격하지 않고 병합 슬롯으로만 쓴다)
_LISTED_DATE = re.compile(r"\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}")


def slot_key(period_end: Optional[str], date_text: str = "") -> str:
    """그룹 키의 **회차 슬롯**.

    13차부터 기간은 소스 전용 추출기만 만들므로 대부분의 소스는 종료일이
    없다. 그래도 같은 제목·같은 기관의 **다른 회차**를 합치면 공고가
    사라지므로, 종료일이 없으면 **목록에 적힌 마지막 날짜**를 슬롯으로
    쓴다 - 이 값은 저장되지 않고 그룹을 가르는 데만 쓴다(기간이라고
    주장하지 않는다).

    Args:
        period_end: 확정 종료일 (있으면 그것이 슬롯이다)
        date_text: 목록 날짜 원문

    Returns:
        슬롯 문자열. 날짜가 전혀 없으면 빈 문자열
    """
    if period_end:
        return period_end
    listed = [match.group(0) for match in _LISTED_DATE.finditer(date_text or "")]
    if not listed:
        return ""
    return "listed:" + re.sub(r"[^0-9]", "", listed[-1])


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
    round_candidates: Optional[Sequence[str]] = None,
    date_text: str = "",
) -> Tuple[str, Tuple[str, ...], str, str]:
    """중복 판별 키를 만든다.

    회차는 **마감을 모를 때만** 키에 들어간다 (6차 게이트 #2):

    - 마감이 같고 알려져 있으면 회차별로 다시 걸린 **같은 공고**다
      (SEIS 메인이 사회보험료 지원사업을 1~9차로 9번 나열하는 경우).
    - 마감이 없으면 회차가 유일한 구분자다 - 같은 제목·같은 센터의
      1차와 2차를 합치면 별개 공고가 사라진다.

    Args:
        title: 공고 제목
        region_candidates: 주체·지역 후보 문자열들 (sub, info…)
        period_end: 접수 종료일 (ISO) 또는 None
        ingested_month: ``YYYY-MM`` 적재 월 (종료일 없을 때 회차 구분용)
        round_candidates: 회차 후보 문자열들 (제목·sub·info…)

    Returns:
        ``(정규화 제목, 주체 서명, 마감 키, 회차 키)``
    """
    slot = slot_key(period_end, date_text)
    round_key = ""
    if not slot:
        round_key = extract_round(list(round_candidates or ()) + [title])
    return (
        normalize_title(title),
        subject_signature(region_candidates),
        deadline_key(slot, ingested_month),
        round_key,
    )


def keys_compatible(
    first: Tuple[str, Tuple[str, ...], str, str],
    second: Tuple[str, Tuple[str, ...], str, str],
    ignore_deadline: bool = False,
) -> bool:
    """두 키가 **같은 공고**를 가리킬 수 있는지 본다.

    그룹핑에는 완전 일치를 쓰지만, 이미 기록된 병합(규칙 A)이나 같은 URL을
    확인할 때는 조금 느슨해야 한다:

    - 한쪽에 주체 메타데이터가 **없으면** 주체를 비교하지 않는다 - 대표에만
      메타데이터가 추가된 정상 병합을 거부하면 안 된다.
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
    if bool(first[1]) and bool(second[1]) and first[1] != second[1]:
        return False
    # 회차가 둘 다 있고 다르면 별개 공고다 (6차 게이트 #2)
    first_round = first[3] if len(first) > 3 else ""
    second_round = second[3] if len(second) > 3 else ""
    if first_round and second_round and first_round != second_round:
        return False
    if first[2] == second[2]:
        return True
    if ignore_deadline and (
        first[2].startswith("month:") or second[2].startswith("month:")
        or not first[2] or not second[2]
    ):
        return True
    return False
