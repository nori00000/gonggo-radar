"""상세 페이지 인용 추출 - 접수기간·자격·금액 문구를 원문 그대로 뽑는다.

계약 v2.1 판정 4: 마감·자격·금액은 **원문에서 문자 그대로 인용 가능한 경우에만**
표기한다. 이 모듈은 어떤 값도 지어내지 않는다 - 페이지에 해당 문구가 없으면
결과 딕셔너리에 키 자체를 넣지 않는다.

"문자 그대로"의 범위: HTML은 태그 경계마다 공백이 끼므로 본문 텍스트를
공백 1칸으로 정규화한 뒤(``normalize_text``) 그 문자열의 **연속 부분열**만
인용으로 반환한다. 즉 공백 정규화 외의 가공은 없다.
"""
import re
from typing import Dict, List, Optional, Tuple

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - bs4 없는 환경
    BeautifulSoup = None  # type: ignore

# 상세 페이지 요청 규약 (계약 v2.1 V2): 1초 1요청, 10초 타임아웃, 브라우저 UA
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DETAIL_TIMEOUT = 10
DETAIL_DELAY_SEC = 1.0

QUOTE_DEADLINE = "quote_deadline"
QUOTE_ELIGIBILITY = "quote_eligibility"
QUOTE_AMOUNT = "quote_amount"

# 인용 최대 길이 - 항목당 2줄 브리핑에 들어갈 수 있는 상한
MAX_QUOTE_LEN = 300

# 본문 영역 후보 (넓은 것부터가 아니라 구체적인 것부터)
CONTENT_SELECTORS = [
    "div.board_view", "div.board-view", "div.view_cont", "div.view_content",
    "div.bbs_view", "div.content_view", "div.bbsView", "div.board_content",
    "div.view_area", "div.contents", "div.sub_content", "div.sub_contents",
    "td.content", "div#content", "div#contents",
]

# 글머리표 - 인용의 끝 경계로 쓴다. ※/* 는 보충설명이라 경계로 쓰지 않는다.
_BOUNDARY_CHARS = "ㅁ□ㅇ○◦▶■●◆"
# 같은 낱말이 잇달아 나오면 표의 다음 라벨로 본다 (예: "공고상태 공고상태 진행중")
_REPEATED_TOKEN = re.compile(r"(?<![^\s])(\S{2,})\s+\1(?![^\s])")
# 본문 텍스트에 이스케이프된 HTML이 그대로 실려 나오는 경우
_LEAKED_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z]")
_OPENER_CHARS = r"([【<〔「"

# (label, strict) - strict=True 는 글머리표/콜론/라벨중복 문맥에서만 인정한다.
# 일반 명사(대상, 마감, 지원내용)는 본문 아무 곳에나 나오므로 strict.
_LABELS: Dict[str, List[Tuple[str, bool]]] = {
    QUOTE_DEADLINE: [
        ("접수기간", False), ("신청기간", False), ("모집기간", False),
        ("공모기간", False), ("접수일정", False), ("신청기한", False),
        ("접수기한", False), ("마감일시", False), ("마감기한", False),
        ("마감일", False), ("마감", True),
    ],
    QUOTE_ELIGIBILITY: [
        ("신청자격", False), ("지원자격", False), ("참가자격", False),
        ("응모자격", False), ("신청대상", False), ("지원대상", False),
        ("모집대상", False), ("참여대상", False), ("대상", True),
    ],
    QUOTE_AMOUNT: [
        ("지원금액", False), ("지원규모", False), ("모집규모", False),
        ("지원사항", False), ("지원내역", False), ("사업규모", False),
        ("지원내용", True),
    ],
}


def normalize_text(html: str) -> str:
    """상세 페이지 HTML에서 본문 텍스트를 뽑아 공백을 1칸으로 정규화한다.

    Args:
        html: 상세 페이지 HTML 문자열

    Returns:
        공백 정규화된 본문 텍스트. 파싱 불가 시 빈 문자열
    """
    if BeautifulSoup is None or not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    element = None
    for selector in CONTENT_SELECTORS:
        element = soup.select_one(selector)
        if element is not None:
            break
    if element is None:
        element = soup.body or soup

    text = element.get_text(" ", strip=True)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _find_label(text: str, label: str, strict: bool) -> Optional[int]:
    """라벨의 시작 위치를 찾는다. 문맥 조건을 만족하지 못하면 None."""
    escaped = re.escape(label)
    contextual = [
        # 글머리표/괄호 뒤 (예: "ㅁ 모집기간", "□ ( 대상 )")
        rf"[{_BOUNDARY_CHARS}{_OPENER_CHARS}]\s*\(?\s*{escaped}",
        # "라벨 :" / "라벨："
        rf"{escaped}\s*[:：]",
        # th/td 라벨이 두 번 렌더되는 표 (예: "신청기간 신청기간 2026.09.01 ~ ...")
        rf"{escaped}\s+{escaped}",
    ]
    for pattern in contextual:
        match = re.search(pattern, text)
        if match:
            first = text.index(label, match.start())
            if pattern is contextual[-1]:
                # 표 라벨 중복이면 두 번째 사본부터가 값에 붙은 인용이다
                return text.index(label, first + len(label))
            return first

    if strict:
        return None

    match = re.search(escaped, text)
    return match.start() if match else None


def _cut_quote(text: str, start: int, label: str) -> str:
    """라벨 시작 위치부터 다음 글머리표 또는 길이 상한까지 잘라낸다."""
    scan_from = start + len(label)
    limit = min(len(text), start + MAX_QUOTE_LEN)

    end = limit
    for index in range(scan_from, limit):
        if text[index] in _BOUNDARY_CHARS:
            end = index
            break

    window = text[scan_from:end]

    # 표에서 다음 라벨이 시작되면(th/td 중복 렌더) 그 앞에서 끊는다
    repeat = _REPEATED_TOKEN.search(window)
    if repeat:
        end = scan_from + repeat.start()

    # 본문에 이스케이프된 마크업이 섞여 나오면 그 앞에서 끊는다
    markup = _LEAKED_MARKUP.search(text, scan_from, end)
    if markup:
        end = markup.start()

    return text[start:end].strip()


def extract_quotes(text: str) -> Dict[str, str]:
    """정규화된 본문에서 마감/자격/금액 인용을 뽑는다.

    Args:
        text: ``normalize_text`` 결과

    Returns:
        ``quote_deadline`` / ``quote_eligibility`` / ``quote_amount`` 중
        **찾은 것만** 담은 딕셔너리. 못 찾은 키는 넣지 않는다.
    """
    quotes: Dict[str, str] = {}
    if not text:
        return quotes

    for key, labels in _LABELS.items():
        for label, strict in labels:
            start = _find_label(text, label, strict)
            if start is None:
                continue
            quote = _cut_quote(text, start, label)
            # 라벨만 남은 조각은 인용이 아니다
            if len(quote) > len(label) + 2:
                quotes[key] = quote
                break

    return quotes


_FULL_DATE = re.compile(r"(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")
_APOS_DATE = re.compile(r"['’]\s*(\d{2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")
_KOR_FULL_DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_KOR_SHORT_DATE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_SHORT_DATE = re.compile(r"(?<![\d:])(\d{1,2})\s*[.\-/]\s*(\d{1,2})(?![\d:])")
_RANGE_SEP = re.compile(r"[~∼〜―-]|부터")
_DEADLINE_ONLY = re.compile(r"까지|마감|이내|이전")


def _safe_iso(year: int, month: int, day: int) -> Optional[str]:
    """범위를 벗어난 날짜는 버린다."""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if not (2000 <= year <= 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _scan_dates(quote: str) -> List[Tuple[int, int, Optional[str], int, int]]:
    """인용 안의 날짜 토큰을 등장 순서대로 모은다.

    지원 형식: ``2026.09.01`` / ``2026-09-01`` / ``2026. 9. 1.`` /
    ``'26. 9. 1.`` / ``2026년 9월 1일`` / 연도 생략(``9. 30.``, ``9월 30일``).

    Returns:
        ``(start, end, iso_or_None, month, day)`` 리스트.
        연도가 생략된 토큰은 ``iso`` 가 None 이고 month/day만 채워진다.
    """
    found: List[Tuple[int, int, Optional[str], int, int]] = []
    consumed: List[Tuple[int, int]] = []

    def add(match, year: Optional[int], month: int, day: int) -> None:
        iso = _safe_iso(year, month, day) if year is not None else None
        if year is not None and iso is None:
            return
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return
        found.append((match.start(), match.end(), iso, month, day))
        consumed.append((match.start(), match.end()))

    # 연도가 있는 형식을 먼저 소비한다
    for match in _FULL_DATE.finditer(quote):
        year, month, day = (int(g) for g in match.groups())
        add(match, year, month, day)
    for match in _APOS_DATE.finditer(quote):
        year, month, day = (int(g) for g in match.groups())
        add(match, 2000 + year, month, day)
    for match in _KOR_FULL_DATE.finditer(quote):
        year, month, day = (int(g) for g in match.groups())
        add(match, year, month, day)

    def overlaps(match) -> bool:
        return any(s < match.end() and match.start() < e for s, e in consumed)

    for pattern in (_KOR_SHORT_DATE, _SHORT_DATE):
        for match in pattern.finditer(quote):
            if overlaps(match):
                continue
            month, day = int(match.group(1)), int(match.group(2))
            add(match, None, month, day)

    found.sort(key=lambda item: item[0])
    return found


def period_from_quote(quote: str) -> Tuple[Optional[str], Optional[str]]:
    """인용에서 접수 시작/종료일을 ISO 날짜로 뽑는다.

    보수적으로만 판단한다 - 지어내지 않기 위해 아래 두 경우에만 값을 낸다:

    1. ``~`` 로 이어진 **두 날짜** (뒤쪽 날짜의 연도가 생략되면 앞 연도를 물려준다)
    2. 날짜가 **정확히 하나**이고 "까지/마감/이내" 가 함께 있을 때 (종료일만)

    날짜가 여러 개 흩어져 있으면(회차별 일정표 등) 아무 값도 내지 않는다.

    Args:
        quote: ``extract_quotes`` 가 낸 인용 문자열

    Returns:
        ``(period_start, period_end)`` - 판단 불가 항목은 None
    """
    if not quote:
        return None, None

    dates = _scan_dates(quote)

    if len(dates) >= 2:
        first, second = dates[0], dates[1]
        if not _RANGE_SEP.search(quote[first[1]:second[0]]):
            return None, None
        start, end = first[2], second[2]
        if end is None and start is not None:
            # "2026. 9. 7.(월) ~ 9. 30.(수)" - 앞 연도를 물려받는다
            end = _safe_iso(int(start[:4]), second[3], second[4])
        return start, end

    if len(dates) == 1 and _DEADLINE_ONLY.search(quote):
        return None, dates[0][2]

    return None, None
