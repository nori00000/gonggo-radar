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
# ---------------------------------------------------------------------------
# 허용목록 (whitelist) - 이 목록에 **라벨 선두**로 걸릴 때만 기간이 생긴다.
#
# 여덟 차례 게이트에서 "라벨을 읽어 추론" 하는 방식이 계속 없는 마감을
# 만들어 냈다(v2final7 재현: 교육기간 범위가 접수기간으로 승격, 접수시작
# 단일 날짜가 시작=마감, 제목의 "모집" 이 라벨로 유입). 그래서 추론을 버리고
# 허용목록만 남겼다. 커버리지 손실은 받아들인다 - 없는 마감을 말하는 것보다
# 마감을 모른다고 말하는 것이 낫다.
# ---------------------------------------------------------------------------
# 접수 **기간**을 말하는 라벨 - 값이 범위일 때만 (시작, 종료)
RECEPTION_RANGE_LABELS = (
    "접수기간", "접수 기간", "신청기간", "신청 기간", "모집기간", "모집 기간",
    "공모기간", "공모 기간", "접수일정", "신청일정",
    "의견제출기간", "의견 제출 기간", "입법의견 접수기간", "의견접수기간",
)
# 접수 **마감**을 말하는 라벨 - 단일 날짜면 종료일만
RECEPTION_END_LABELS = (
    "접수마감", "신청마감", "모집마감", "공모마감",
    "접수기한", "신청기한", "제출기한", "마감일시", "마감기한", "마감일",
)
# 기간을 만들 수 없는 라벨 - 회귀로 고정해 둔다
NEVER_PERIOD_LABELS = (
    "교육기간", "행사일정", "행사기간", "운영기간", "사업기간", "협약기간",
    "접수시작", "신청시작", "모집시작", "발표", "선정", "심사기간",
)

# 범위 구분: ~ 계열, 공백으로 감싼 하이픈, "부터…까지"
_RANGE_MARK = re.compile(r"[~∼〜]|\s[-–—]\s|부터")
# 요일·괄호 주석 - 숫자가 없는 괄호는 날짜 파싱 전에 벗긴다
# (8차 게이트 #3: "2026.09.01(화) ~ 2026.09.30(수)" 가 시작=마감=09-01 이 됐다)
_PAREN_NOTE = re.compile(r"\([^)0-9]*\)")
_FULL_DATE = re.compile(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")
# 날짜 하나 / 범위 - 범위를 **먼저** 찾는다. 첫 날짜만 집으면
# "접수 기간 2026.09.01 ~ 2026.09.30" 의 종료일이 09-01이 된다 (7차 게이트 #3).
_DATE_TEXT = r"\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}\s*일?\.?"
# 범위 추출 - ``~`` 계열, "부터", 공백으로 감싼 하이픈을 모두 받는다
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
    분리가 어긋나 시작=마감=09-01 이 된다 (8차 게이트 #3).
    """
    return _PAREN_NOTE.sub(" ", text or "")


def parse_period(text: str) -> Tuple[Optional[str], Optional[str]]:
    """기간 문자열을 시작일·종료일로 나눈다.

    ``~`` · 공백 하이픈 · ``부터…까지`` 를 범위로 본다. 범위 표기가 없으면
    단일 날짜를 시작=종료로 돌려주므로, 호출자는 ``classify_date`` 를 거쳐
    쓰는 것이 안전하다.
    """
    if not text:
        return None, None
    cleaned = strip_notes(text)
    if _RANGE_MARK.search(cleaned):
        parts = _RANGE_MARK.split(cleaned)
        dates = [normalize_date(part) for part in parts]
        dates = [date for date in dates if date]
        if len(dates) >= 2:
            return dates[0], dates[-1]
        if len(dates) == 1:
            # "2026.09.30까지" 처럼 한쪽만 있는 범위 - 종료일로 본다
            return None, dates[0]
    single = normalize_date(cleaned)
    return single, single


def _leading_label(label: str, allowed: Sequence[str]) -> bool:
    """라벨이 허용목록 항목으로 **시작**하는지 본다.

    선두를 요구하는 이유: 포함 검사만 하면 ``교육기간 접수 안내`` 나
    제목이 앞에 붙은 ``참여기업 모집 공고 접수기간`` 도 라벨로 인정된다.
    그건 제목이지 라벨이 아니다.
    """
    text = (label or "").strip().lstrip("[(【<「·-–— ")
    return any(text.startswith(token) for token in allowed)


def classify_date(
    text: str, label: str = ""
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """목록의 날짜를 **허용목록으로만** 분류한다.

    기간은 접수 라벨이 **라벨 선두**에 있을 때만 만든다. 그 밖의 날짜는
    모두 게시일로 돌린다 - 라벨이 없거나, 모르는 라벨이거나, 범위만
    있어도 마감을 만들지 않는다.

    Args:
        text: 목록에서 읽은 날짜 문자열
        label: 그 날짜의 라벨 (같은 요소 텍스트의 날짜 앞부분)

    Returns:
        ``(period_start, period_end, posted)``
    """
    value = (text or "").strip()
    if not value:
        return None, None, None

    cleaned = strip_notes(value)

    if _leading_label(label, RECEPTION_RANGE_LABELS):
        if _RANGE_MARK.search(cleaned):
            start, end = parse_period(cleaned)
            if start and end:
                return start, end, None
        # 접수기간 라벨인데 범위가 아니면 기간을 만들지 않는다
        return None, None, normalize_date(cleaned)

    if _leading_label(label, RECEPTION_END_LABELS):
        start, end = parse_period(cleaned)
        if end and start == end:
            return None, end, None      # 단일 날짜 = 마감일 하나
        if start and end:
            return start, end, None     # 마감 라벨에 범위가 오면 범위대로
        return None, None, normalize_date(cleaned)

    # 허용목록 밖 = 기간을 만들지 않는다 (게시일로만 남긴다)
    return None, None, normalize_date(cleaned)


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


def extract_date_and_label(container) -> Tuple[str, str]:
    """컨테이너에서 **날짜와 그 날짜의 라벨**을 뽑는다 (보수 규칙).

    8차 게이트 #2·#3·#4로 규칙을 좁혔다. **환각 0** 이 우선이므로, 라벨을
    확신할 수 없으면 라벨 없이(=게시일) 돌려준다 - 커버리지 손실은 받아들인다.

    규칙:

    1. 라벨은 **날짜와 같은 요소의 텍스트 중 날짜 앞부분** 에서만 읽는다.
       인접 형제·부모·링크 텍스트에서 추론하지 않는다.
    2. 날짜가 컨테이너의 **직접 텍스트**에 있으면(자식 요소 안이 아니면)
       라벨이 없는 것으로 본다 - 제목이 라벨로 새어 들어오던 자리다.
    3. **범위를 품은 요소를 우선**한다. 가장 짧은 요소만 고르면
       ``<span>접수기간 <em>A</em> ~ <em>B</em></span>`` 에서 라벨과 범위를
       모두 잃는다.
    4. 요일 괄호는 벗겨서 판단한다.

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
