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
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from bs4 import BeautifulSoup, Comment, NavigableString
except ImportError:  # pragma: no cover - bs4 없는 환경
    BeautifulSoup = None  # type: ignore
    Comment = None  # type: ignore
    NavigableString = None  # type: ignore

# 상세 페이지 요청 규약 (계약 v2.1 V2 + Codex 크리틱 #8)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DETAIL_TIMEOUT = 10
DETAIL_CONNECT_TIMEOUT = 10         # 연결 상한
DETAIL_READ_TIMEOUT = 10            # 청크 사이 간격 상한
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
QUOTES_ATTEMPTED_AT = "quotes_attempted_at"

# 인용 최대 길이 - 항목당 2줄 브리핑에 들어갈 수 있는 상한
MAX_QUOTE_LEN = 300

# 본문 영역 후보 (구체적인 것부터)
CONTENT_SELECTORS = [
    "div.board_view", "div.board-view", "div.view_cont", "div.view_content",
    "div.bbs_view", "div.content_view", "div.bbsView", "div.board_content",
    "div.view_area", "div.contents", "div.sub_content", "div.sub_contents",
    "td.content", "div#content", "div#contents",
]

# 경계는 세 등급이다 (Codex 재검토 #7 + 최종 게이트 #4).
#   ROW  : 표의 행 / 셀 밖의 문단이 끝났다 - 값이 없으면 그 라벨은 값이 없다
#   CELL : 셀이 끝났다 - 라벨과 값이 <th>/<td> 로 갈린 경우를 위해
#          값을 만나기 전 **한 번만** 건너뛴다
#   INNER: 셀 **안쪽** 의 div/p/br - 한 셀에 1·2차가 나열된 경우이므로 끊지
#          않는다. 셀 밖의 div/p/br 은 ROW 로 취급한다 (문단이 다르면 다른
#          이야기다 - `<p>접수기간 …</p><p>교육기간 …</p>` 누출 방지)
_ROW_TAGS = frozenset({"tr", "table", "tbody", "thead", "tfoot", "caption"})
_CELL_TAGS = frozenset({"td", "th"})
_INNER_TAGS = frozenset({
    "div", "p", "li", "br", "dl", "dd", "dt", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote",
})
_SKIP_TAGS = frozenset({"script", "style", "noscript"})

# 경계 표시는 사용자 영역 문자를 쓴다 - 원문 HTML의 자연 공백(\n, \r\n, 탭)과
# 절대 겹치지 않아야 경계 등급을 신뢰할 수 있다.
_ROW_MARK, _CELL_MARK, _INNER_MARK = "\uE000", "\uE001", "\uE002"
_MARKS = _ROW_MARK + _CELL_MARK + _INNER_MARK
# 공백과 경계 표시를 한데 묶어 "낱말 사이" 를 뜻하는 패턴
_GAP = r"[\s\uE000-\uE002]"

# 절단된 응답은 **파싱하지 않는다** (4차 게이트 #2·#9).
# 어디서 잘렸는지 정규식으로 알 수 없다는 것이 세 번 실증됐다:
#   - 속성 안쪽에서 잘려도 `</p>` 가 뒤에 있으면 완결로 보였다
#   - 셀 안 `2026.09.<div>3</div>` 뒤 절단이 09-03을 만들었다
# 그래서 "안전한 절단 지점" 을 찾는 대신 절단 자체를 실패로 다룬다.
# 인용을 잃는 것은 안전하고, 없는 마감을 만드는 것은 안전하지 않다.

# 글머리표 - 인용의 끝 경계. ※/* 는 보충설명이라 경계로 쓰지 않는다.
_BULLET_BOUNDARY = "ㅁ□ㅇ○◦▶■●◆"
_OPENER_CHARS = r"([【<〔「"

# 같은 낱말이 잇달아 나오면 표의 다음 라벨로 본다 ("공고상태 공고상태 진행중")
_REPEATED_TOKEN = re.compile(
    rf"(?<!{_GAP[:-1]}])([^\s\uE000-\uE002]{{2,}}){_GAP}+\1(?!{_GAP[:-1]}])"
)
# 본문 텍스트에 이스케이프된 HTML이 그대로 실려 나오는 경우
_LEAKED_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z]")

# 인용을 **끝내야 하는** 다른 라벨들 (최종 게이트 #4).
# "접수기간 …부터 교육기간 2026.09.20~09.30" 에서 교육기간의 날짜가 접수
# 마감으로 새어 들어오던 원인이다. 라벨의 값은 **그 라벨의 블록 안**에서만
# 찾는다 - 다른 라벨이 시작되면 거기서 끝난다.
_FOREIGN_LABEL_WORDS = (
    "교육기간", "운영기간", "사업기간", "협약기간", "수행기간", "활동기간",
    "심사기간", "발표", "선정", "문의처", "문의", "담당부서", "담당자",
    "작성일", "등록일", "게시일", "조회수", "첨부파일", "첨부",
    "추진 일정", "추진일정", "신청방법", "제출방법", "제출서류", "유의사항",
)

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


def _strip_noise(soup) -> None:
    """주석·스크립트·스타일을 **먼저** 제거한다 (4차 게이트 #3).

    ``Comment`` 는 ``NavigableString`` 의 하위 클래스라 트리 순회가 본문
    텍스트로 읽어 버렸다. 주석에 들어 있는 옛 접수기간이 실제 마감을
    덮어쓰는 것이 실측됐다.
    """
    for tag in soup(list(_SKIP_TAGS)):
        tag.decompose()
    if Comment is not None:
        for node in soup.find_all(string=lambda text: isinstance(text, Comment)):
            node.extract()


def _walk(root, root_in_cell: bool, out: list) -> None:
    """트리를 훑어 텍스트와 경계 표시를 순서대로 모은다 (**재귀 없음**).

    재귀 순회는 1,100단 중첩 div(12KB)에서 ``RecursionError`` 를 내고 그
    소스의 수집 **전체**를 잃게 만들었다 (4차 게이트 #5). 명시적 스택을
    쓰면 깊이에 상한이 없다.

    셀 안쪽인지에 따라 div/p/br 의 등급이 달라진다 - 한 셀 안의 나열은
    값의 일부이고, 셀 밖의 문단은 다른 이야기다.
    """
    # (node, in_cell, closing) - closing=True 면 그 태그의 경계를 적을 차례
    stack = [(root, root_in_cell, False)]
    while stack:
        node, in_cell, closing = stack.pop()
        if closing:
            name = (getattr(node, "name", "") or "").lower()
            if name in _ROW_TAGS:
                out.append(_INNER_MARK if in_cell else _ROW_MARK)
            elif name in _CELL_TAGS:
                out.append(_INNER_MARK if in_cell else _CELL_MARK)
            elif name in _INNER_TAGS:
                cell = in_cell or name in _CELL_TAGS
                out.append(_INNER_MARK if cell else _ROW_MARK)
            continue

        if NavigableString is not None and isinstance(node, NavigableString):
            if Comment is not None and isinstance(node, Comment):
                continue
            out.append(str(node))
            continue

        name = (getattr(node, "name", "") or "").lower()
        if not name or name in _SKIP_TAGS:
            continue

        cell = in_cell or name in _CELL_TAGS
        # 닫는 경계를 먼저 넣고(스택이므로 나중에 처리됨) 자식을 역순으로 쌓는다
        stack.append((node, in_cell, True))
        children = list(getattr(node, "children", ()))
        for child in reversed(children):
            stack.append((child, cell, False))


def normalize_text(html: str) -> str:
    """상세 페이지 HTML에서 본문 텍스트를 뽑아 정규화한다.

    셀·행·문단이 끝나는 자리에 등급별 경계 표시를 남긴다. 표시는 사용자
    영역 문자(U+E000~E002)라 원문의 자연 공백과 겹치지 않는다.

    **속성 값은 절대 텍스트가 되지 않는다** - BeautifulSoup 트리를 직접
    훑으므로 태그 속성은 순회 대상이 아니다(최종 게이트 #3).

    Args:
        html: 상세 페이지 HTML 문자열

    Returns:
        경계 표시가 들어간 본문 텍스트. 파싱 불가 시 빈 문자열
    """
    if BeautifulSoup is None or not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    _strip_noise(soup)

    element = None
    for selector in CONTENT_SELECTORS:
        element = soup.select_one(selector)
        if element is not None:
            break
    if element is None:
        element = soup.body or soup

    pieces: list = []
    in_cell = bool(getattr(element, "name", "") in _CELL_TAGS)
    _walk(element, in_cell, pieces)
    text = "".join(pieces).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
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


def _foreign_words_for(key: str) -> Tuple[str, ...]:
    """이 인용을 **끝내야 하는** 다른 라벨 낱말들.

    같은 항목의 라벨끼리는 서로를 끊지 않는다 - "접수기간 … 예산 소진 시
    **마감**" 에서 "마감" 은 값의 일부이지 다음 라벨이 아니다.
    """
    words = set(_FOREIGN_LABEL_WORDS)
    for other_key, labels in _LABELS.items():
        if other_key == key:
            continue
        for label, _strict in labels:
            words.add(label)
    return tuple(sorted(words, key=len, reverse=True))


def _foreign_label_start(window: str, key: str, own_label: str) -> Optional[int]:
    """값 구간에서 **다른 라벨이 새 조각을 시작하는** 위치를 찾는다.

    라벨이 문장 중간에 우연히 나온 경우는 경계가 아니다 - 예를 들어
    "(**제출서류** 완비 기준) 선착순 접수" 의 제출서류는 값의 일부다.
    그래서 경계 표시(셀·행·문단)나 글머리표 바로 뒤에 오는 라벨만 인정한다.

    Args:
        window: 라벨 뒤의 값 구간
        key: 지금 뽑고 있는 인용 종류
        own_label: 지금 뽑고 있는 라벨

    Returns:
        다른 라벨이 조각을 시작하는 인덱스. 없으면 None
    """
    best: Optional[int] = None
    for word in _foreign_words_for(key):
        if word == own_label or word in own_label or own_label in word:
            continue
        for match in re.finditer(re.escape(word), window):
            index = match.start()
            # 라벨 앞의 괄호·대괄호·콜론·공백은 장식이므로 벗겨 낸다.
            # "(교육기간) 2026.09.20~09.30" 이 경계로 잡히지 않아 접수
            # 마감에 09-20이 새어 들어왔다 (4차 게이트 #6).
            prefix = window[:index].rstrip(" ()[]{}<>【】「」:：·-–—")
            if prefix and prefix[-1] not in _MARKS + _BULLET_BOUNDARY:
                continue  # 문장 중간에 우연히 나온 낱말
            if best is None or index < best:
                best = index
            break
    return best


def _cut_quote(text: str, start: int, label: str, key: str = "") -> str:
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

    # **다른 라벨이 시작되면 거기서 끝난다** (최종 게이트 #4).
    # 값은 그 라벨의 것만이다 - "접수기간 …부터 교육기간 09.20~09.30" 에서
    # 교육기간의 날짜가 접수 마감으로 새어 들어오던 원인.
    foreign = _foreign_label_start(window, key, label)
    if foreign is not None:
        end = scan_from + foreign
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
            quote = _cut_quote(text, start, label, key)
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

# 회차 표기 - 있으면 날짜 역전을 해 넘김으로 보지 않는다 (최종 게이트 #5)
_ROUND_LABEL = re.compile(r"\d+\s*차")
# 뒤쪽 날짜에 연도가 **명시**되어 있으면 해 넘김을 추정하지 않는다
_EXPLICIT_YEAR = re.compile(r"\d{4}")

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

    # 앞 -> 뒤: **해 넘김을 추정하지 않는다** (최종 게이트 #5).
    # 회차가 여러 개면 날짜가 뒤로 갔다 앞으로 오는 것이 정상이므로
    # ("2차 2.01~2.28 / 1차 1.01~1.31") 이를 해 넘김으로 보면 지난 공고가
    # 미래 공고로 바뀐다. 해 넘김은 **한 범위 안에서만** 따진다(_candidates).
    year: Optional[int] = None
    for index, (begin, finish, iso, month, day) in enumerate(resolved):
        if iso:
            year = int(iso[:4])
            continue
        if year is None:
            continue
        new_iso = _safe_iso(year, month, day)
        if new_iso:
            resolved[index] = (begin, finish, new_iso, month, day)

    # 뒤 -> 앞: 첫 명시 연도보다 **앞에** 있는 날짜들만 되돌려 받는다.
    # "12.20~2027.1.10" 의 12.20 은 2026년이다 (뒤 날짜보다 월이 크므로).
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

    return resolved


def _candidates(quote: str) -> List[Tuple[int, Optional[str], Optional[str]]]:
    """인용에서 (위치, 시작일, 종료일) 후보를 모은다.

    후보가 되는 경우만 담는다 - 날짜가 그냥 하나 떠 있으면 후보가 아니다.
    """
    dates = _resolve_years(_scan_dates(quote))
    candidates: List[Tuple[int, Optional[str], Optional[str]]] = []
    paired: set = set()
    has_round_labels = bool(_ROUND_LABEL.search(quote))

    # 1) "~" 로 이어진 두 날짜
    for index in range(len(dates) - 1):
        first, second = dates[index], dates[index + 1]
        between = quote[first[1]:second[0]]
        if len(between.strip()) > _RANGE_GAP_MAX:
            continue
        if not _RANGE_SEP.search(between):
            continue
        start_iso, end_iso = first[2], second[2]
        # 해 넘김은 **한 범위 안에서만**, 그리고 회차 표기가 없을 때만 따진다
        if (
            start_iso
            and end_iso
            and end_iso < start_iso
            and not has_round_labels
            and not _EXPLICIT_YEAR.search(quote[second[0]:second[1]])
        ):
            rolled = _safe_iso(int(start_iso[:4]) + 1, second[3], second[4])
            if rolled:
                end_iso = rolled
        candidates.append((first[0], start_iso, end_iso))
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

# ---------------------------------------------------------------------------
# 격리 실행 (자식 프로세스)
# ---------------------------------------------------------------------------
#
# 상세 수집은 **별도 프로세스**에서 돌린다 (4차 게이트 #1·#2).
#
# 시간 상한을 프로세스 안에서 강제하려는 시도는 세 번 실패했다:
#   1) 청크마다 벽시계 확인 -> urllib3 iterator가 chunk_size 만큼 모일 때까지
#      돌아오지 않아 검사에 진입조차 못 했다
#   2) 작업 스레드 + join(timeout) -> 이어지는 동기 ``response.close()`` 가
#      스트림 종료를 기다려 1.3초 지연, 비차단 close 에서는 스레드가 남았다
#   3) GET·close·파싱이 상한 밖에 있어 축소 예산에서도 초과했다
#
# 그래서 협조가 필요 없는 경계를 쓴다: 부모는 ``subprocess.run(timeout=…)``
# 으로 **프로세스 전체**를 죽인다. 부분 결과는 자식이 한 줄씩 flush 하므로
# 살아남는다.

WORKER_MODULE = "alert.crawlers.detail_quotes"


class _ItemTimeout(Exception):
    """항목별 상한 초과 (자식 프로세스 내부)."""


def build_session(user_agent: str = BROWSER_USER_AGENT):
    """상세 수집용 세션 (브라우저 User-Agent)."""
    import requests

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,*/*",
    })
    return session


def fetch_detail_quotes(session, url: str, deadline: Optional[float] = None) -> dict:
    """상세 페이지 하나에서 인용을 뽑는다 (자식 프로세스에서 호출).

    **절단되면 인용을 내지 않는다** (4차 게이트 #2). 본문이 상한을 넘거나
    읽기가 끊기면 어디서 잘렸는지 알 수 없고, "안전한 절단 지점" 을 찾으려는
    시도는 속성 안쪽·중첩 블록에서 거짓 날짜를 세 번 만들었다. 인용을 잃는
    것은 안전하고 없는 마감을 만드는 것은 안전하지 않다.

    Args:
        session: requests 세션
        url: 상세 페이지 URL
        deadline: 이 monotonic 시각을 넘기면 포기한다 (항목별 상한)

    Returns:
        ``{"quotes": {...}}`` 또는 ``{"truncated": True}`` /
        ``{"error": "..."}`` / ``{"skipped": "deadline"}``
    """
    import requests

    if not url:
        return {"error": "empty url"}
    if deadline is not None and time.monotonic() > deadline:
        return {"skipped": "deadline"}

    response = None
    try:
        response = session.get(
            url, timeout=(DETAIL_CONNECT_TIMEOUT, DETAIL_READ_TIMEOUT), stream=True
        )
        response.raise_for_status()

        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_DETAIL_BYTES:
            return {"truncated": True, "reason": f"content-length {declared}"}

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            if deadline is not None and time.monotonic() > deadline:
                return {"skipped": "deadline"}
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_DETAIL_BYTES:
                return {"truncated": True, "reason": f"body over {MAX_DETAIL_BYTES}"}
            chunks.append(chunk)
        body = b"".join(chunks)
    except _ItemTimeout:
        raise                       # 항목 상한은 호출자가 "skipped" 로 분류한다
    except requests.RequestException as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # 파싱 밖의 예상 못 한 실패도 한 항목에 가둔다
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    encoding = None
    if response is not None:
        encoding = response.encoding
    html = decode_body(body, encoding)
    try:
        quotes = extract_quotes(normalize_text(html))
    except Exception as exc:
        # 한 페이지의 파싱 실패가 다른 항목에 번지지 않게 한다 (게이트 #5)
        return {"error": f"parse {type(exc).__name__}: {exc}"}
    return {"quotes": quotes}


def decode_body(body: bytes, header_encoding: Optional[str]) -> str:
    """응답 본문을 디코드한다 (헤더 우선, 그다음 utf-8/cp949)."""
    for encoding in (header_encoding, "utf-8", "cp949"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _install_item_alarm(seconds: float) -> bool:
    """항목별 상한을 SIGALRM으로 걸어 둔다.

    ``iter_content`` 는 ``chunk_size`` 만큼 모일 때까지 돌아오지 않으므로
    루프 안의 시간 검사만으로는 한 항목에 갇힐 수 있다. 알람을 걸면 그
    항목만 버리고 **다음 항목으로 넘어갈 수 있다** - 부모의 프로세스 kill이
    최후의 경계이고, 이 알람은 부분 결과를 더 건지기 위한 것이다.

    Returns:
        알람을 걸었으면 True (걸 수 없는 환경이면 False)
    """
    try:
        import signal

        def _raise(_signum, _frame):
            raise _ItemTimeout()

        signal.signal(signal.SIGALRM, _raise)
        signal.setitimer(signal.ITIMER_REAL, max(0.1, seconds))
        return True
    except (ImportError, ValueError, AttributeError, OSError):
        return False


def _clear_item_alarm() -> None:
    """걸어 둔 알람을 해제한다."""
    try:
        import signal

        signal.setitimer(signal.ITIMER_REAL, 0)
    except (ImportError, ValueError, AttributeError, OSError):
        pass


def _worker_main(argv: Optional[Sequence[str]] = None) -> int:
    """자식 프로세스 진입점.

    표준입력(또는 ``--items-file``)으로 ``[{"source_id":…, "url":…}, …]`` 을
    받아 항목마다 JSON 한 줄을 **즉시 flush** 해 내보낸다. 부모가 프로세스를
    죽여도 그때까지의 줄은 살아남는다.
    """
    import argparse

    parser = argparse.ArgumentParser(description="상세 인용 수집 워커")
    parser.add_argument("--source", default="", help="소스 이름 (로그용)")
    parser.add_argument(
        "--items-file", default="-",
        help="항목 JSON 경로. '-' 이면 표준입력 (기본)",
    )
    parser.add_argument(
        "--item-deadline", type=float, default=DETAIL_REQUEST_DEADLINE,
        help="항목당 벽시계 상한(초)",
    )
    parser.add_argument("--delay", type=float, default=DETAIL_DELAY_SEC)
    args = parser.parse_args(argv)

    if args.items_file == "-":
        payload = sys.stdin.read()
    else:
        payload = Path(args.items_file).read_text(encoding="utf-8")
    try:
        items = json.loads(payload or "[]")
    except ValueError as exc:
        print(f"invalid items json: {exc}", file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print("items must be a list", file=sys.stderr)
        return 2

    session = build_session()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id", ""))
        url = str(item.get("url", ""))
        if index and args.delay:
            time.sleep(args.delay)
        deadline = time.monotonic() + max(0.1, args.item_deadline)
        armed = _install_item_alarm(args.item_deadline)
        try:
            result = fetch_detail_quotes(session, url, deadline=deadline)
        except _ItemTimeout:
            result = {"skipped": "item-deadline"}
            # 소켓이 매달려 있을 수 있으니 세션을 새로 만든다
            try:
                session.close()
            except Exception:
                pass
            session = build_session()
        except Exception as exc:  # pragma: no cover - 최후의 그물
            result = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            if armed:
                _clear_item_alarm()
        result["source_id"] = source_id
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - 자식 프로세스 경로
    sys.exit(_worker_main())
