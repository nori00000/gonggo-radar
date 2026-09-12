"""목록 날짜의 **라벨 분류** - 게시일을 접수기간으로 쓰지 않기 위한 공용 규칙.

6차 게이트 #1: 목록 HTML의 ``게시일=2026.09.11`` 이
``period_start=period_end=2026-09-11`` 로 저장됐다. 게시일이 마감이 되면
브리핑이 게시 다음 날 만료된 공고를 말하거나 존재하지 않는 접수기간을
말한다.

같은 결함이 **여섯 개 크롤러의 같은 템플릿**에 있었다(seis, fowi,
nongup_gg, forest_service, ipet, 그리고 앞서 고친 socialenterprise/coop).
그래서 규칙을 한 곳에 두고 모두 여기로 위임한다 - 사본이 여섯 개면
조금씩 다르게 틀릴 자리가 여섯 개다.

판정 순서:

1. 라벨이 게시일/작성일/등록일/공고일 → **게시일** (기간 아님)
2. 값에 범위 표기(``~``)가 있으면 → 접수기간
3. 라벨이 접수/신청/모집/마감/기간/일정 → 접수기간
   (``마감`` 만 말하는 라벨은 **종료일만** - 시작일을 지어내지 않는다)
4. 그 밖의 단일 날짜 → **게시일** (모르면 마감을 만들지 않는다)
"""
import re
from typing import List, Optional, Sequence, Tuple

# 게시 시각을 말하는 라벨 - 절대 기간이 아니다
POSTED_LABELS = re.compile(r"게시일|작성일|등록일|등록날짜|작성날짜|공고일|게시날짜|작성자")
# 접수 일정을 말하는 라벨
PERIOD_LABELS = re.compile(r"접수|신청|모집|마감|공모|기간|일정")
# "마감" 만 말하는 라벨 - 종료일만 준다
DEADLINE_ONLY_LABELS = re.compile(r"마감")

_RANGE_MARK = re.compile(r"[~∼〜]")
_FULL_DATE = re.compile(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")


def normalize_date(text: str) -> Optional[str]:
    """날짜 문자열을 ISO(YYYY-MM-DD)로 정규화한다.

    ``2026-09-11`` · ``2026.9.11`` · ``2026. 9. 11.`` · ``20260911`` 을 받는다.

    Args:
        text: 날짜 문자열

    Returns:
        ISO 날짜. 알아볼 수 없으면 None
    """
    if not text:
        return None
    match = _FULL_DATE.search(text)
    if match:
        year, month, day = (int(group) for group in match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
        return None
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) == 8:
        year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def parse_period(text: str) -> Tuple[Optional[str], Optional[str]]:
    """기간 문자열을 시작일·종료일로 나눈다.

    범위 표기가 없으면 단일 날짜를 시작=종료로 본다 - 호출자는
    ``classify_date`` 를 거쳐 이 결과를 쓰는 것이 안전하다.
    """
    if not text:
        return None, None
    if _RANGE_MARK.search(text):
        parts = _RANGE_MARK.split(text)
        if len(parts) >= 2:
            return normalize_date(parts[0]), normalize_date(parts[1])
    single = normalize_date(text)
    return single, single


def classify_date(
    text: str, label: str = ""
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """목록의 날짜가 **접수기간**인지 **게시일**인지 가른다.

    Args:
        text: 목록에서 읽은 날짜 문자열
        label: 그 날짜의 컬럼 라벨 (표 헤더·클래스명·앞말)

    Returns:
        ``(period_start, period_end, posted)``
    """
    value = (text or "").strip()
    if not value:
        return None, None, None

    label = (label or "").strip()
    if label and POSTED_LABELS.search(label):
        return None, None, normalize_date(value)

    if _RANGE_MARK.search(value):
        start, end = parse_period(value)
        return start, end, None

    if label and PERIOD_LABELS.search(label):
        start, end = parse_period(value)
        if DEADLINE_ONLY_LABELS.search(label) and start == end:
            # "접수마감 2026.09.30" 은 마감일 하나다 (시작일 아님)
            return None, end, None
        return start, end, None

    # 라벨이 없거나 모르는 단일 날짜는 게시일로 본다
    return None, None, normalize_date(value)


def header_labels(table) -> List[str]:
    """표의 컬럼 라벨(헤더 텍스트)을 순서대로 읽는다."""
    if table is None:
        return []
    head = table.find("thead")
    header_row = head.find("tr") if head is not None else None
    if header_row is None:
        for row in table.find_all("tr"):
            if row.find("th") is not None:
                header_row = row
                break
    if header_row is None:
        return []
    return [
        re.sub(r"\s+", " ", cell.get_text(strip=True))
        for cell in header_row.find_all(["th", "td"])
    ]


def label_for(cell, cells: Sequence, headers: Sequence[str], css_class: str = "") -> str:
    """이 셀의 컬럼 라벨을 찾는다 (표 헤더 → 행의 th → class 이름)."""
    try:
        index = list(cells).index(cell)
    except ValueError:
        index = -1
    if 0 <= index < len(headers) and headers[index]:
        return headers[index]
    parent = getattr(cell, "parent", None)
    if parent is not None:
        own_header = parent.find("th")
        if own_header is not None:
            return re.sub(r"\s+", " ", own_header.get_text(strip=True))
    return css_class or ""


def label_before(text: str, position: int, width: int = 12) -> str:
    """날짜 앞에 붙은 말을 라벨로 본다 ("게시일 2026.09.11")."""
    return text[max(0, position - width):position]
