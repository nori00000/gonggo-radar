"""상세 페이지 인용 추출 - 접수기간·자격·금액 문구를 원문 그대로 뽑는다.

계약 v2.1 판정 4: 마감·자격·금액은 **원문에서 문자 그대로 인용 가능한 경우에만**
표기한다. 이 모듈은 어떤 값도 지어내지 않는다 - 페이지에 해당 문구가 없으면
결과 딕셔너리에 키 자체를 넣지 않는다.

"문자 그대로"의 범위: HTML은 태그 경계마다 공백이 끼므로 본문을 정규화한 뒤
(``normalize_text``) 그 문자열의 **연속 부분열**만 인용으로 반환한다. 인용은
한 줄로 평탄화(``flatten``)해 돌려주므로 검증은 ``quote in flatten(text)`` 로
한다. 공백 정규화 외의 가공은 없다.

**표/행 경계를 지킨다** (Codex 크리틱 #3): 정규화는 셀·행·블록이 끝나는
자리에 줄바꿈을 남기고, 인용은 값을 한 조각 읽은 뒤 만나는 첫 경계에서
끊는다. 그래서 ``<th>접수기간</th><td>상시</td></tr><tr><th>작성일</th>
<td>2026.09.11</td>`` 에서 게시일이 마감으로 새어 들어오지 않는다.
"""
import re
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - bs4 없는 환경
    BeautifulSoup = None  # type: ignore

# 상세 페이지 요청 규약 (계약 v2.1 V2 + Codex 크리틱 #8)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DETAIL_TIMEOUT = 10
DETAIL_DELAY_SEC = 1.0
# 실행시간 상한 - 상세 수집이 크롤 주기를 잡아먹지 못하게 한다
# (2026-09-13 조정자 판정: kofpi 20건 전부 훑고 698KB 상세도 읽도록 상향)
MAX_DETAIL_REQUESTS = 20            # 소스·실행당 새 상세 요청 수
DETAIL_BUDGET_SEC = 240.0           # 소스·실행당 총 예산
DETAIL_REQUEST_DEADLINE = 30.0      # 요청 하나의 벽시계 상한 (읽는 도중에도 검사)
MAX_DETAIL_BYTES = 1024 * 1024      # 응답 본문 상한 - 넘으면 자른다(거부 아님)

QUOTE_DEADLINE = "quote_deadline"
QUOTE_ELIGIBILITY = "quote_eligibility"
QUOTE_AMOUNT = "quote_amount"
ALWAYS_OPEN = "always_open"
EARLY_CLOSE = "early_close"
DETAIL_TRUNCATED = "detail_truncated"

# 인용 최대 길이 - 항목당 2줄 브리핑에 들어갈 수 있는 상한
MAX_QUOTE_LEN = 300

# 본문 영역 후보 (구체적인 것부터)
CONTENT_SELECTORS = [
    "div.board_view", "div.board-view", "div.view_cont", "div.view_content",
    "div.bbs_view", "div.content_view", "div.bbsView", "div.board_content",
    "div.view_area", "div.contents", "div.sub_content", "div.sub_contents",
    "td.content", "div#content", "div#contents",
]

# 경계는 세 등급이다 (Codex 재검토 #7).
#   ROW  (\n): 표의 행이 끝났다 - 값이 없으면 그 라벨은 값이 없는 것이다
#   CELL (\r): 셀이 끝났다 - 라벨과 값이 <th>/<td> 로 갈린 경우를 위해
#              값을 만나기 전 **한 번만** 건너뛴다
#   INNER(\v): 셀 **안쪽** 의 div/p/br/li - 값의 일부이므로 끊지 않는다
#              (한 셀에 1차·2차 회차가 div 로 나열되는 경우)
_ROW_END = re.compile(r"</(?:tr|table|tbody|thead)\s*>", re.I)
_CELL_END = re.compile(r"</(?:td|th)\s*>", re.I)
_INNER_END = re.compile(
    r"(</(?:div|p|li|h[1-6]|dl|dd|dt|section|article)\s*>|<br\s*/?>)", re.I
)
# 사용자 영역 문자를 쓴다 - 원문 HTML의 자연 공백(\n, \r\n, 탭)과 절대
# 겹치지 않아야 경계 등급을 신뢰할 수 있다.
_ROW_MARK, _CELL_MARK, _INNER_MARK = "\uE000", "\uE001", "\uE002"
_MARKS = _ROW_MARK + _CELL_MARK + _INNER_MARK
# 공백과 경계 표시를 한데 묶어 "낱말 사이" 를 뜻하는 패턴
_GAP = r"[\s\uE000-\uE002]"

# 글머리표 - 인용의 끝 경계. ※/* 는 보충설명이라 경계로 쓰지 않는다.
_BULLET_BOUNDARY = "ㅁ□ㅇ○◦▶■●◆"
_OPENER_CHARS = r"([【<〔「"

# 같은 낱말이 잇달아 나오면 표의 다음 라벨로 본다 ("공고상태 공고상태 진행중")
_REPEATED_TOKEN = re.compile(
    rf"(?<!{_GAP[:-1]}])([^\s\uE000-\uE002]{{2,}}){_GAP}+\1(?!{_GAP[:-1]}])"
)
# 본문 텍스트에 이스케이프된 HTML이 그대로 실려 나오는 경우
_LEAKED_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z]")

# (label, strict) - strict=True 는 글머리표/콜론/라벨중복 문맥에서만 인정한다.
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


def flatten(text: str) -> str:
    """경계 표시를 공백으로 바꿔 한 줄로 만든다 (인용 대조용)."""
    return re.sub(rf"{_GAP}+", " ", text or "").strip()


def _collapse_boundaries(match: "re.Match") -> str:
    """연속된 경계는 가장 강한 등급 하나로 줄인다 (ROW > CELL > INNER)."""
    chunk = match.group(0)
    if _ROW_MARK in chunk:
        return _ROW_MARK
    if _CELL_MARK in chunk:
        return _CELL_MARK
    return _INNER_MARK


def complete_html_prefix(html: str) -> str:
    """절단된 HTML에서 **완결되지 않은 꼬리**를 버린다 (Codex 재검토 #8).

    1MB 상한에서 자르면 태그나 텍스트가 중간에서 끊긴다. 그대로 파싱하면
    ``2026.09.30`` 이 ``2026.09.3`` 으로 잘려 **09-03** 이 되거나,
    ``<div title="접수기간 …`` 처럼 속성 중간에서 끊긴 조각이 본문 텍스트로
    새어 들어온다. 그래서 마지막 ``>`` 까지만 남긴다 - 그 뒤는 완결되지
    않은 태그이거나 완결되지 않은 텍스트다.

    Args:
        html: 절단된 HTML 문자열

    Returns:
        마지막 완결 태그까지의 접두사. ``>`` 가 없으면 빈 문자열
    """
    if not html:
        return ""
    last_close = html.rfind(">")
    if last_close == -1:
        return ""
    return html[:last_close + 1]


def normalize_text(html: str) -> str:
    """상세 페이지 HTML에서 본문 텍스트를 뽑아 정규화한다.

    셀·행·블록이 끝나는 자리에는 줄바꿈을 남긴다 - 표의 다음 행 값이
    앞 라벨의 인용으로 새어 들어오는 것을 막기 위한 경계다.

    Args:
        html: 상세 페이지 HTML 문자열

    Returns:
        줄 단위로 끊긴 본문 텍스트. 파싱 불가 시 빈 문자열
    """
    if BeautifulSoup is None or not html:
        return ""

    marked = _ROW_END.sub(lambda m: m.group(0) + _ROW_MARK, html)
    marked = _CELL_END.sub(lambda m: m.group(0) + _CELL_MARK, marked)
    marked = _INNER_END.sub(lambda m: m.group(0) + _INNER_MARK, marked)
    soup = BeautifulSoup(marked, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    element = None
    for selector in CONTENT_SELECTORS:
        element = soup.select_one(selector)
        if element is not None:
            break
    if element is None:
        element = soup.body or soup

    text = element.get_text(" ", strip=False).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)          # 원문의 자연 공백을 먼저 없앤다
    # 이어진 경계는 가장 강한 등급 하나로 (주변 공백까지 흡수)
    text = re.sub(rf" ?([{_MARKS}][{_MARKS} ]*)", _collapse_boundaries, text)
    return text.strip(" " + _MARKS)


def _find_label(text: str, label: str, strict: bool) -> Optional[int]:
    """라벨의 시작 위치를 찾는다. 문맥 조건을 만족하지 못하면 None."""
    escaped = re.escape(label)
    contextual = [
        # 글머리표/괄호 뒤 (예: "ㅁ 모집기간", "□ ( 대상 )")
        rf"[{_BULLET_BOUNDARY}{_OPENER_CHARS}]\s*\(?\s*{escaped}",
        # "라벨 :" / "라벨："
        rf"{escaped}\s*[:：]",
        # th/td 라벨이 두 번 렌더되는 표 (예: "신청기간 신청기간 2026.09.01 ~")
        rf"{escaped}{_GAP}+{escaped}",
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
    """라벨부터 값이 끝나는 자리까지 잘라낸다.

    경계 등급별 처리 (Codex 재검토 #7):

    - **글머리표**: 늘 끊는다. 라벨 바로 뒤에 오면 값이 없다는 뜻이다.
    - **ROW**(행 끝): 값을 아직 못 읽었으면 그 라벨은 **값이 없다** - 다음
      행의 값을 흡수하지 않도록 여기서 끝낸다. 값을 읽은 뒤면 끊는다.
    - **CELL**(셀 끝): 라벨과 값이 ``<th>``/``<td>`` 로 갈린 경우를 위해
      값을 만나기 전 **한 번만** 건너뛴다. 두 번째 셀 경계는 값이 없다는 뜻.
    - **INNER**(셀 안쪽 div/p/br): 값의 일부다. 끊지 않는다 - 한 셀에
      1차·2차가 나열되면 둘 다 인용에 들어가야 회차 선택이 동작한다.
    """
    scan_from = start + len(label)
    limit = min(len(text), start + MAX_QUOTE_LEN)

    end = limit
    seen_content = False
    cells_skipped = 0
    for index in range(scan_from, limit):
        char = text[index]
        if char in _BULLET_BOUNDARY:
            end = index
            break
        if char == _ROW_MARK:
            end = index
            break
        if char == _CELL_MARK:
            if seen_content:
                end = index
                break
            cells_skipped += 1
            if cells_skipped > 1:
                end = index
                break
            continue
        if char == _INNER_MARK:
            continue
        if not char.isspace() and char not in "()[]:：-–—,.":
            seen_content = True

    window = text[scan_from:end]

    # 표에서 같은 낱말이 잇달아 나오면(다음 라벨) 그 앞에서 끊는다
    repeat = _REPEATED_TOKEN.search(window)
    if repeat:
        end = scan_from + repeat.start()

    # 본문에 이스케이프된 마크업이 섞여 나오면 그 앞에서 끊는다
    markup = _LEAKED_MARKUP.search(text, scan_from, end)
    if markup:
        end = markup.start()

    return flatten(text[start:end])


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


# ---------------------------------------------------------------- 기간 파싱

_FULL_DATE = re.compile(r"(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")
_APOS_DATE = re.compile(r"['’]\s*(\d{2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")
_KOR_FULL_DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_KOR_SHORT_DATE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_SHORT_DATE = re.compile(r"(?<![\d:])(\d{1,2})\s*[.\-/]\s*(\d{1,2})(?![\d:])")

# 두 날짜 사이에 있으면 범위로 보는 기호 (하이픈은 공백으로 감싼 경우만)
_RANGE_SEP = re.compile(r"[~∼〜]|부터|\s[-–—]\s")
# 날짜 뒤에 붙으면 종료일로 보는 말. "마감" 은 여기서 빼고 라벨 쪽에서만
# 본다 - "부터 … 마감" 같은 문장에서 시작일을 종료일로 뒤집던 원인이었다.
_UNTIL = re.compile(r"까지|이내")
# 날짜 **바로** 뒤에 붙으면 시작일로 보는 말 (좁은 창에서만 본다).
# "09.30까지, 이후 접수불가" 의 "이후" 를 시작 신호로 오인하지 않기 위해
# _UNTIL 을 먼저 본다 (Codex 재검토 #12).
_FROM = re.compile(r"부터|이후|개시|시작")
# 날짜 앞에 붙으면 종료일로 보는 라벨
_DEADLINE_LABEL = re.compile(r"(?:마감일시|마감기한|마감일|마감)\s*[:：]?\s*$")
# 마감이 없는 상시 공고
_ALWAYS_OPEN = re.compile(r"상시|수시|연중|별도\s*공지\s*시")
# 예산 소진 시 조기마감 - 마감일이 **있으면서** 앞당겨질 수 있다는 뜻이다.
# 무기한 접수(상시)와 구분해야 한다 (Codex 재검토 #9).
_EARLY_CLOSE = re.compile(r"예산\s*소진|소진\s*시|조기\s*마감|선착순")

_RANGE_GAP_MAX = 12    # 두 날짜 사이 간격 상한 (넘으면 범위로 보지 않는다)
_UNTIL_WINDOW = 15     # 날짜 뒤에서 "까지/이내" 를 찾는 창
_FROM_WINDOW = 6       # 날짜 뒤에서 "부터/이후" 를 찾는 창 (바로 붙은 경우만)
_LABEL_WINDOW = 15     # 날짜 앞에서 "마감:" 라벨을 찾는 창


def is_always_open(quote: str) -> bool:
    """"상시 / 수시 / 연중" 처럼 무기한 접수를 말하는지 본다.

    주의: 이것만으로 마감을 지우지는 않는다. 명시된 날짜가 있으면 **날짜가
    이긴다** (Codex 재검토 #9) - ``resolve_period`` 가 그렇게 판정한다.
    """
    return bool(quote) and bool(_ALWAYS_OPEN.search(quote))


def is_early_close(quote: str) -> bool:
    """"예산 소진 시 / 조기마감 / 선착순" 처럼 마감이 앞당겨질 수 있는지 본다.

    마감일이 **있는** 공고이므로 무기한 접수(상시)와 구분한다.
    """
    return bool(quote) and bool(_EARLY_CLOSE.search(quote))


def _safe_iso(year: Optional[int], month: int, day: int) -> Optional[str]:
    """범위를 벗어난 날짜는 버린다."""
    if year is None:
        return None
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
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return
        iso = _safe_iso(year, month, day)
        if year is not None and iso is None:
            return
        found.append((match.start(), match.end(), iso, month, day))
        consumed.append((match.start(), match.end()))

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
            add(match, None, int(match.group(1)), int(match.group(2)))

    found.sort(key=lambda item: item[0])
    return found


def _resolve_years(
    dates: List[Tuple[int, int, Optional[str], int, int]]
) -> List[Tuple[int, int, Optional[str], int, int]]:
    """연도가 생략된 날짜에 문맥의 연도를 물려준다 (Codex 재검토 #12).

    앞에서 뒤로 한 번, 뒤에서 앞으로 한 번 훑는다:

    - 앞 → 뒤: 마지막으로 본 연도를 이어받고, 월·일이 앞 날짜보다 **빠르면**
      해가 넘어간 것으로 본다 (``2026.12.20 ~ 1.10`` → ``2027-01-10``).
    - 뒤 → 앞: 첫 명시 연도보다 앞에 있는 날짜들(``12.20 ~ 2027.1.10`` 의
      ``12.20``)은 뒤쪽 연도에서 되돌려 받는다.

    Returns:
        같은 모양의 리스트. 해결된 항목은 ``iso`` 가 채워진다
    """
    resolved = list(dates)

    # 앞 -> 뒤
    year: Optional[int] = None
    previous: Optional[Tuple[int, int]] = None
    for index, (begin, finish, iso, month, day) in enumerate(resolved):
        if iso:
            year = int(iso[:4])
            previous = (month, day)
            continue
        if year is None:
            continue
        candidate_year = year
        if previous and (month, day) < previous:
            candidate_year += 1
        new_iso = _safe_iso(candidate_year, month, day)
        if new_iso:
            resolved[index] = (begin, finish, new_iso, month, day)
            year = candidate_year
            previous = (month, day)

    # 뒤 -> 앞 (첫 명시 연도보다 앞에 있는 날짜들)
    year = None
    following: Optional[Tuple[int, int]] = None
    for index in range(len(resolved) - 1, -1, -1):
        begin, finish, iso, month, day = resolved[index]
        if iso:
            year = int(iso[:4])
            following = (month, day)
            continue
        if year is None:
            continue
        candidate_year = year
        if following and (month, day) > following:
            candidate_year -= 1
        new_iso = _safe_iso(candidate_year, month, day)
        if new_iso:
            resolved[index] = (begin, finish, new_iso, month, day)
            year = candidate_year
            following = (month, day)

    return resolved


def _candidates(quote: str) -> List[Tuple[int, Optional[str], Optional[str]]]:
    """인용에서 (위치, 시작일, 종료일) 후보를 모은다.

    후보가 되는 경우만 담는다 - 날짜가 그냥 하나 떠 있으면 후보가 아니다.
    """
    dates = _resolve_years(_scan_dates(quote))
    candidates: List[Tuple[int, Optional[str], Optional[str]]] = []
    paired: set = set()

    # 1) "~" 로 이어진 두 날짜
    for index in range(len(dates) - 1):
        first, second = dates[index], dates[index + 1]
        between = quote[first[1]:second[0]]
        if len(between.strip()) > _RANGE_GAP_MAX:
            continue
        if not _RANGE_SEP.search(between):
            continue
        candidates.append((first[0], first[2], second[2]))
        paired.add(index)
        paired.add(index + 1)

    # 2) 홀로 있는 날짜 - 바로 붙어 있는 말로만 판정한다.
    #    "까지" 를 먼저 본다: "09.30까지, 이후 접수불가" 에서 "이후" 를
    #    시작 신호로 오인하면 마감이 시작일로 저장된다 (재검토 #12).
    for index, token in enumerate(dates):
        if index in paired or token[2] is None:
            continue
        until_tail = quote[token[1]:token[1] + _UNTIL_WINDOW]
        from_tail = quote[token[1]:token[1] + _FROM_WINDOW]
        head = quote[max(0, token[0] - _LABEL_WINDOW):token[0]]
        if _UNTIL.search(until_tail) or _DEADLINE_LABEL.search(head):
            candidates.append((token[0], None, token[2]))
        elif _FROM.search(from_tail):
            candidates.append((token[0], token[2], None))

    candidates.sort(key=lambda item: item[0])
    return candidates


def period_from_quote(
    quote: str, today: Optional[date] = None
) -> Tuple[Optional[str], Optional[str]]:
    """인용에서 접수 시작/종료일을 ISO 날짜로 뽑는다.

    보수적으로만 판단한다 - 날짜가 그냥 하나 떠 있다고 마감으로 쓰지 않는다.
    종료일로 확정하는 경우는 ①``~`` 범위의 뒤쪽 날짜 ②날짜 바로 뒤에
    "까지/이내" ③``마감:`` 라벨 바로 뒤 뿐이다.

    회차가 여러 개면 **오늘 이후로 가장 이른 종료일**을 쓴다. 모두 지났으면
    **가장 늦은 종료일**을 쓴다 - 본문에 적힌 순서와 무관하게 판단한다
    (Codex 재검토 #12).

    Args:
        quote: ``extract_quotes`` 가 낸 인용 문자열
        today: 기준일 (테스트 주입용, 기본은 오늘)

    Returns:
        ``(period_start, period_end)`` - 판단 불가 항목은 None
    """
    if not quote:
        return None, None

    candidates = _candidates(quote)
    if not candidates:
        return None, None

    with_end = [c for c in candidates if c[2]]
    if not with_end:
        with_start = [c for c in candidates if c[1]]
        return (with_start[0][1] if with_start else None), None

    today_iso = (today or date.today()).isoformat()
    upcoming = sorted(
        (c for c in with_end if c[2] >= today_iso),
        key=lambda c: (c[2], 0 if c[1] else 1),
    )
    if upcoming:
        chosen = upcoming[0]
    else:
        # 모두 지났으면 가장 늦은 종료일 (본문 순서에 의존하지 않는다)
        chosen = max(with_end, key=lambda c: (c[2], 0 if c[1] else 1))

    return chosen[1], chosen[2]


def resolve_period(
    quote: str, today: Optional[date] = None
) -> Tuple[Optional[str], Optional[str], bool, bool]:
    """마감 인용에서 기간과 상시/조기마감 표시를 함께 판정한다.

    **명시된 날짜가 상시보다 우선한다** (Codex 재검토 #9):

    - 날짜로 종료일이 잡히면 그 종료일을 쓰고 ``always_open`` 은 False다.
      "예산 소진 시 조기마감" 은 마감이 있는 공고이므로 ``early_close`` 로
      표시하고 마감을 지우지 않는다.
    - 종료일이 전혀 없고 "상시/수시/연중" 이 있으면 그때만 ``always_open``.

    Args:
        quote: 마감 인용
        today: 기준일 (테스트 주입용)

    Returns:
        ``(period_start, period_end, always_open, early_close)``
    """
    start, end = period_from_quote(quote, today=today)
    early_close = bool(end) and is_early_close(quote)
    always_open = not end and is_always_open(quote)
    return start, end, always_open, early_close


def apply_quote_period(
    quotes: Dict[str, str], today: Optional[date] = None
) -> Tuple[Optional[str], Optional[str], bool, bool]:
    """``extract_quotes`` 결과에서 기간·상시·조기마감을 판정한다.

    Args:
        quotes: ``extract_quotes`` 결과
        today: 기준일 (테스트 주입용)

    Returns:
        ``(period_start, period_end, always_open, early_close)``
    """
    return resolve_period(quotes.get(QUOTE_DEADLINE, ""), today=today)


def has_quote_keys(payload: Dict[str, object]) -> bool:
    """이미 인용이 채워진 항목인지 본다 (재요청 방지, 크리틱 #8)."""
    keys: Sequence[str] = (QUOTE_DEADLINE, QUOTE_ELIGIBILITY, QUOTE_AMOUNT)
    return any(payload.get(key) for key in keys)
