"""목록 날짜의 **텍스트 유틸** - 날짜를 읽고, 정규화하고, 게시일로만 남긴다.

6차 게이트 #1: 목록 HTML의 ``게시일=2026.09.11`` 이
``period_start=period_end=2026-09-11`` 로 저장됐다. 게시일이 마감이 되면
브리핑이 게시 다음 날 만료된 공고를 말하거나 존재하지 않는 접수기간을
말한다.

**13차에서 라벨 분류(``classify_date``)를 삭제했다.** 여섯 차례 게이트에서
라벨로 기간을 추론하는 방식은 계속 없는 마감을 만들어 냈다: 구조 라벨
(카드 파서가 붙인 ``date_label="접수기간"``), 제목 라벨
(``<a>신청기한 변경 안내</a>2026.09.11``), 인접 셀(``/ 심사기간 …``).
허용목록을 좁히는 것으로는 막히지 않았으므로 **기간 경로를 없앴다**.

이제 기간은 소스별 전용 추출기(``period_extractors``)에서만 나오고, 이
모듈은 날짜를 잃지 않게 **게시일 증거**로만 남긴다.
"""
import re
from typing import List, Optional, Sequence, Tuple

# 게시 시각을 말하는 라벨 - 정리 스크립트가 "이 날짜는 게시일인가" 를
# 판단할 때 쓴다 (기간을 만드는 데는 어떤 라벨도 쓰지 않는다).
POSTED_LABELS = re.compile(r"게시일|작성일|등록일|등록날짜|작성날짜|공고일|게시날짜|작성자")

# 요일·괄호 주석 - 숫자가 없는 괄호는 날짜 파싱 전에 벗긴다
# (8차 게이트 #3: "2026.09.01(화) ~ 2026.09.30(수)" 가 시작=마감=09-01 이 됐다)
_PAREN_NOTE = re.compile(r"\([^)0-9]*\)")
_FULL_DATE = re.compile(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")
# 날짜 하나 / 범위 - 범위를 **먼저** 찾는다. 첫 날짜만 집으면
# "접수 기간 2026.09.01 ~ 2026.09.30" 의 종료일이 09-01이 된다 (7차 게이트 #3).
_DATE_TEXT = r"\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}\s*일?\.?"
_RANGE_TEXT = re.compile(
    rf"({_DATE_TEXT})\s*(?:[~∼〜]|부터|\s[-–—]\s)\s*({_DATE_TEXT})"
)
_SINGLE_TEXT = re.compile(_DATE_TEXT)


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


def strip_notes(text: str) -> str:
    """요일 같은 **숫자 없는 괄호 주석**을 벗긴다.

    ``2026.09.01(화) ~ 2026.09.30(수)`` 에서 ``(화)`` 를 남겨 두면 범위
    분리가 어긋난다 (8차 게이트 #3).
    """
    return _PAREN_NOTE.sub(" ", text or "")


def posted_date(text: str) -> Optional[str]:
    """라벨과 무관하게 값의 첫 날짜를 **게시일로** 돌려준다.

    기간은 전용 추출기만 만들 수 있으므로, 목록 날짜는 잃지 않게
    게시일 증거로만 남긴다.
    """
    value = (text or "").strip()
    if not value:
        return None
    return normalize_date(strip_notes(value))


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
    """이 셀의 컬럼 라벨을 찾는다 (표 헤더 → 행의 th → class 이름).

    라벨은 ``raw_data`` 증거와 정리 스크립트의 판단 재료로만 쓴다 -
    기간을 만드는 데는 쓰지 않는다 (13차).
    """
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


def extract_date_and_label(container) -> Tuple[str, str]:
    """컨테이너에서 **날짜와 그 날짜의 라벨**을 뽑는다 (보수 규칙).

    8차 게이트 #2·#3·#4로 규칙을 좁혔다. **환각 0** 이 우선이므로, 라벨을
    확신할 수 없으면 라벨 없이 돌려준다 - 커버리지 손실은 받아들인다.

    규칙:

    1. 라벨은 **날짜와 같은 요소의 텍스트 중 날짜 앞부분** 에서만 읽는다.
       인접 형제·부모·링크 텍스트에서 추론하지 않는다.
    2. **범위를 품은 요소를 우선**한다. 가장 짧은 요소만 고르면
       ``<span>접수기간 <em>A</em> ~ <em>B</em></span>`` 에서 라벨과 범위를
       모두 잃는다.
    3. 요일 괄호는 벗겨서 판단한다.

    Args:
        container: 목록 항목 요소 (BeautifulSoup Tag)

    Returns:
        ``(날짜 문자열, 라벨)`` - 날짜가 없으면 ``("", "")``
    """
    if container is None:
        return "", ""

    ranged: list = []
    single: list = []
    for element in container.find_all(True):
        text = re.sub(r"\s+", " ", element.get_text(" ", strip=True))
        cleaned = strip_notes(text)
        if _RANGE_TEXT.search(cleaned):
            ranged.append((len(text), element, text))
        elif _SINGLE_TEXT.search(cleaned):
            single.append((len(text), element, text))

    # 범위를 품은 요소가 있으면 그중 가장 짧은 것, 없으면 단일 날짜 요소
    pool = ranged or single
    if pool:
        pool.sort(key=lambda item: item[0])
        _length, _element, text = pool[0]
        cleaned = re.sub(r"\s+", " ", strip_notes(text)).strip()
        match = _RANGE_TEXT.search(cleaned) or _SINGLE_TEXT.search(cleaned)
        value = match.group(0).strip()
        # 라벨: **같은 요소 텍스트의 날짜 앞부분만**
        label = cleaned[:match.start()].strip(" :：·-–—()[]")
        return value, label

    # 자식 요소에 날짜가 없다 = 컨테이너 직접 텍스트에 있다 -> 라벨 없음
    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
    cleaned = strip_notes(text)
    match = _RANGE_TEXT.search(cleaned) or _SINGLE_TEXT.search(cleaned)
    if match is None:
        return "", ""
    return match.group(0).strip(), ""
