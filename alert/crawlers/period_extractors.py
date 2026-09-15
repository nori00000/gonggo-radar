"""소스별 **전용** 기간 추출기 - 순수 함수만 둔다 (13차 게이트).

열두 차례의 게이트에서 환각 마감은 늘 같은 모양으로 들어왔다: 목록의
어떤 날짜를 **접수기간으로 잘못 읽는다**. 라벨 추론을 좁히는 방식(12차의
``classify_date`` 허용목록)은 구조 라벨·제목 라벨·인접 셀로 계속 새어
들어왔다. 그래서 13차는 추론을 **전부 버리고** 소스마다 "이 필드의 이
모양만 기간이다" 를 한 함수로 못박는다.

계약:

- 기간을 만들 수 있는 소스는 ``PERIOD_EXTRACTORS`` 의 키 두 개뿐이다.
  그 밖의 모든 소스는 ``period_start``/``period_end`` 가 **항상 None**이다.
- 두 함수는 **순수 함수**다. HTML·네트워크·크롤러 상태를 보지 않고
  ``raw_data`` 딕셔너리만 읽는다. 그래서 크롤러가 무엇을 반환하든
  ``alert/main.py`` 의 관문이 이 함수만 다시 적용하면 결과가 같다.
- 달력에 없는 날짜(``2027.2.30``)는 버린다. 연도 추정은 하지 않는다 -
  종료일에 연도가 없을 때만 **시작일의 연도**를 쓴다.
- kofpi 는 추출기가 없다. 제목 괄호 ``(~9.30)`` 패턴은 12차까지 계속
  과거 결과 공고를 마감으로 만들었으므로 **폐기**했다.
"""
import re
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

Period = Tuple[Optional[str], Optional[str]]

# 완전 날짜(연·월·일) 와 월·일만 있는 날짜. 구분자는 **점**만 받는다
# (두 소스의 실제 표기: ``2026.09.01`` · ``2026. 9. 7.``).
_FULL_DATE = r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?"
_MONTH_DAY = r"\d{1,2}\s*\.\s*\d{1,2}\s*\.?"

# 필드 **전체**가 범위 하나여야 한다 (10차 게이트 HIGH).
#
# 예전에는 텍스트 안에서 범위를 **검색**했다. 그래서 매치가 하나면 통과해,
# 다른 일정이 섞인 셀이 그 일정의 날짜를 접수기간으로 저장했다:
#   - ``2026.09.01부터 2026.09.30까지 / 심사기간 2026.10.01 ~ 2026.10.31``
#     -> 심사기간 10-01/10-31 저장
#   - ``접수기간 미정 / 교육기간 2026.10.01 ~ 2026.10.31`` -> 교육기간 저장
# 전체 일치는 **날짜 토큰 경계**도 함께 지킨다 (10차 MEDIUM):
# ``2026.09.300`` 은 남는 문자 때문에 매치가 실패한다.
#
# 앞에 붙일 수 있는 것은 접수기간 라벨 하나뿐이다.
_RANGE_ONLY = re.compile(
    rf"^\s*(?:접수\s*기간\s*[:：]?\s*)?({_FULL_DATE})"
    rf"\s*[~∼〜-]\s*({_FULL_DATE}|{_MONTH_DAY})\s*$"
)

# SEIS 메인 카드의 **접수기간 자리**. 파서가 이 셀렉터에서 실제로 읽은
# 값에만 붙이는 출처 표시이며, 자유 텍스트 라벨과 달리 지어낼 수 없다.
#
# 이 자리를 접수기간으로 인정하는 근거(2026-09-13 라이브·픽스처 실측):
#   - 사이트가 **클래스로 분기**한다: 공고 카드(인·지정/사업공고/재정지원)만
#     ``p.date`` 를 채우고(22/22 전부 ``A ~ B`` 범위), 공지사항 카드 12건은
#     빈 ``p.date-temp`` 를 쓴다.
#   - 같은 카드 ``ul.info`` 의 **D-day 배지가 ``p.date`` 종료일까지의 남은
#     날짜와 22/22 일치**한다(종료일 - 2026-09-13 = D-N). 사이트 자신이 그
#     종료일을 마감으로 세고 있다.
# 텍스트 라벨(``접수기간``)은 이 페이지 전체에 **0건**이므로 텍스트만으로는
# 이 22건을 영원히 읽을 수 없다.
# 값은 ``SeisCrawler.CARD_DATE_SELECTOR`` (카드 직속 ``p.date``)에서
# 읽었을 때만 이 토큰이 붙는다 - 임의 부모의 ``p.date`` 는 붙지 않는다.
SEIS_CARD_DATE_FIELD = "li.swiper-slide p.date"

# 값이 **접수기간 라벨로 시작**하면 자리와 무관하게 인정한다(표·목록 경로).
# 파서가 붙이는 자유 라벨(``date_label``)은 믿지 않는다: 같은 자리에
# 교육기간·행사일정·무라벨 범위가 함께 들어온다.
_SEIS_RECEPTION_LABEL = re.compile(r"^\s*접수\s*기간\s*[:：]?\s*\S")

_DIGITS = re.compile(r"\d+")


def _normalize(text: object) -> str:
    """**공백만** 정리한다 - 다른 전처리는 하지 않는다 (11차 게이트 HIGH).

    예전에는 숫자 없는 괄호 주석을 벗겼다. 그 전처리가 의미를 지워
    ``(교육기간) 2026.10.01 ~ 2026.10.31`` 이 전체 일치를 통과했다.
    괄호 안의 말도 **필드가 무엇을 말하는지**의 일부다.
    """
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _calendar_date(year: int, month: int, day: int) -> Optional[str]:
    """달력에 실제로 있는 날짜만 ISO 로 돌려준다 (``2027-02-30`` → None)."""
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _full_date(text: str) -> Optional[str]:
    """``2026.09.30`` 처럼 연도가 있는 날짜."""
    numbers = _DIGITS.findall(text)
    if len(numbers) < 3 or len(numbers[0]) != 4:
        return None
    return _calendar_date(int(numbers[0]), int(numbers[1]), int(numbers[2]))


def _month_day(text: str, year: int) -> Optional[str]:
    """``9.30`` 처럼 연도가 없는 날짜 - 연도는 **시작일에서만** 가져온다."""
    numbers = _DIGITS.findall(text)
    if len(numbers) != 2:
        return None
    return _calendar_date(year, int(numbers[0]), int(numbers[1]))


def _range_only(text: str) -> Period:
    """필드 **전체**가 범위 하나일 때만 (시작, 종료)를 돌려준다.

    다른 일정·문구가 섞여 있으면 어느 쪽이 접수기간인지 알 수 없으므로
    아무 것도 만들지 않는다 (10차 게이트 HIGH).
    """
    if not text:
        return None, None
    match = _RANGE_ONLY.match(text)
    if not match:
        return None, None

    start_text, end_text = match.group(1), match.group(2)
    start = _full_date(start_text)
    if not start:
        return None, None
    end = _full_date(end_text) or _month_day(end_text, int(start[:4]))
    if not end or end < start:
        return None, None      # 뒤집힌 범위는 읽은 것이 아니라 지어낸 것
    return start, end


def seis_period(raw: Dict[str, object]) -> Period:
    """SEIS - 근거가 **두 가지 중 하나**일 때만 단일 범위를 기간으로 읽는다.

    1. **구조 근거**: 값을 카드 컨테이너 직속 ``p.date`` 에서 읽었다
       (``date_field``). 위 상수의 실측 근거 참조 - 사이트가 이 자리를
       클래스로 분기하고 D-day 로 카운트다운한다.
    2. **텍스트 근거**: 값 자체가 ``접수기간 …`` 으로 시작한다.

    어느 쪽이든 필드 **전체**가 범위 하나여야 한다 - 구조 근거가 있는
    카드라도 ``교육기간 2026.10.01 ~ 2026.10.31`` 은 기간이 아니다.

    둘 다 아니면 기간이 아니다 - 목록의 무라벨 범위, 교육기간, 제목 라벨은
    전부 여기서 걸린다 (9차 게이트 HIGH ①·②).

    Args:
        raw: 크롤러가 남긴 ``raw_data``. ``date_field`` 와 ``date`` 만 읽는다

    Returns:
        ``(period_start, period_end)`` - 조건을 못 채우면 ``(None, None)``
    """
    value = _normalize(raw.get("date"))
    if raw.get("date_field") == SEIS_CARD_DATE_FIELD:
        return _range_only(value)          # 구조 근거: 라벨이 없어도 된다

    if not _SEIS_RECEPTION_LABEL.match(value):
        return None, None                  # 텍스트 근거도 없다
    return _range_only(value)


def lawmaking_period(raw: Dict[str, object]) -> Period:
    """국민참여입법센터 - 의견제출 기간 셀에 범위가 **하나**일 때 그 범위.

    셀 전체가 의견제출 기간 필드이므로, 범위가 하나면 시작일·종료일이
    **같은 근거**로 적혀 있다. 둘 이상이면(``/ 심사기간 …``) 어느 쪽이
    의견접수인지 알 수 없어 None 이다 (9차 게이트 HIGH: 심사 종료일이
    의견접수 마감으로 합성됐다).

    Args:
        raw: 크롤러가 남긴 ``raw_data`` 딕셔너리. ``period`` 만 읽는다

    Returns:
        ``(period_start, period_end)`` - 조건을 못 채우면 ``(None, None)``
    """
    return _range_only(_normalize(raw.get("period")))


# ---------------------------------------------------------------------------
# 상세 본문 근거로 읽는 기간 (P2-D) - 목록·제목은 절대 보지 않는다
# ---------------------------------------------------------------------------
#
# 세 소스(forest_service·kofpi·socialenterprise)는 **목록에 마감이 없다**.
# 목록 제목의 ``(~9.30)`` 은 연도가 없어서 연도를 추정해야 하는데, 추정은 이
# 모듈이 열세 차례 거절한 바로 그 동작이다(2025년 공고가 2026년 마감이 됐다).
# 그래서 마감은 **상세 본문의 기간 라벨 한 자리**에서만 읽는다. 본문은
# ``alert.crawlers.period_detail`` 이 항목당 한 번 받아 ``raw_data`` 에
# 싣고, 아래 함수들은 그 문자열만 읽는 **순수 함수**다.
#
# 라운드 2·3 (Codex 게이트) 가 막은 자리:
#   ① 각주·연장: ``접수기간 2026. 9. 1. ~ 9. 16. ※ 2026. 9. 30.까지 연장`` 이
#      9-16 을 만들었다. 라운드 2 는 값 **뒤**만 봤고, 라운드 3 은 단서 스캔을
#      **본문 전체**로 넓혔다 - 정정은 라벨 앞에도, 다른 글머리표 아래에도 온다.
#   ② 잘린 창: 정정이 창 밖에 있을 수 있다 → 잘렸으면 아예 읽지 않는다.
#      라벨 수·충돌 단서도 창이 아니라 **본문 전체**에서 센다.
#   ③ 한 라벨 아래의 회차 표: 첫 행만 읽혔다 → 값 뒤에 남는 것이 있으면 거절.
#   ④ 근거가 **어느 URL 의 것인지**(``detail_text_url``): URL 이 바뀐 항목에
#      옛 근거를 물려주면 남의 마감을 쓰게 된다.
#   ⑤ "안 받았다" 와 "받았는데 비었다" 는 다르다 → 후자는 ``근거 소실``.

DETAIL_TEXT_FIELD = "detail_text"
DETAIL_TEXT_TRUNCATED_FIELD = "detail_text_truncated"
DETAIL_LABEL_COUNT_FIELD = "detail_label_count"
DETAIL_CONFLICT_CUE_FIELD = "detail_conflict_cue"
DETAIL_TEXT_URL_FIELD = "detail_text_url"
DETAIL_TEXT_FETCHED_AT_FIELD = "detail_text_fetched_at"

# 판정을 거절한 이유 (순수 함수가 돌려준다 - 저장하지 않는다)
REASON_NO_EVIDENCE = "근거 없음"
REASON_LOST = "근거 소실"
REASON_TRUNCATED = "근거 절단"
REASON_CONFLICT = "근거 충돌"
REASON_AMBIGUOUS_LABEL = "라벨 중복"
REASON_SHAPE = "형태 불일치"

# 기간 라벨 - 이 넷만 접수 마감을 뜻한다. ``일시``·``행사``·``교육기간`` 처럼
# 다른 일정을 가리키는 라벨은 여기 없으므로 영원히 매치되지 않는다.
# 앞에 한글이 붙은 말(``사업기간``·``운영기간``)도 다른 라벨이다.
_PERIOD_LABEL = r"(?<![가-힣])(?:접수|모집|공모|신청)\s*기간"
_LABEL_SCAN = re.compile(_PERIOD_LABEL)

# 블록 경계 - 여기서 한 라벨의 값이 끝난다.
#
# ``※``·``*`` 는 **경계가 아니다**: 각주가 그 값의 정정일 수 있으므로,
# 경계로 삼으면 정정을 못 보고 옛 날짜를 쓴다.
_BULLETS = "ㅁㅇ□■○◦●▶◆•"
# 두 자리 연도 표기 앞에 오는 아포스트로피 (``’26. 9. 28.``)
_APOS = "'’‘`"

# 값 뒤에 이것이 오면 **다른 절이 시작된 것**이다. 날짜를 품을 수 있는 말
# (심사·선정·발표·교육 …)은 **일부러 넣지 않았다**: 그런 말이 뒤따르면
# 어디까지가 접수기간인지 알 수 없으므로 거절이 맞다.
_SECTION_WORDS = (
    "첨부파일", "첨부", "붙임", "문의처", "문의", "담당부서", "담당자", "담당",
    "신청방법", "접수방법", "제출방법", "제출서류", "이전글", "다음글", "목록",
)
_BLOCK_END = re.compile(
    rf"[{_BULLETS}]|(?:{'|'.join(_SECTION_WORDS)})|{_PERIOD_LABEL}"
)

# 글머리표 **다음이 날짜로 이어지면** 같은 값의 둘째 줄이다 - 거기서 끊지
# 않는다(라운드 3 Codex HIGH ①). 끊어 버리면 ``○ 9월 30일까지 연장`` 같은
# 정정 줄이 블록 밖으로 나가 보이지 않는다. 블록에 들어오면 아래 잔여 검사가
# 잡는다. ``ㅁ 모집대상 … 창업 7년 이내`` 처럼 날짜가 아닌 숫자는 이어지지
# 않는다 - 숫자만으로 판단하면 멀쩡한 다음 절까지 삼킨다.
_DATE_HINT = re.compile(
    r"\d{4}\s*\.\s*\d{1,2}|\d{1,2}\s*\.\s*\d{1,2}\s*\.|"
    r"\d{1,2}\s*월\s*\d{1,2}\s*일|[~∼〜]\s*\d"
)
_CONTINUATION_LOOKAHEAD = 80

# 값 뒤에 남은 말이 이것을 품으면 **근거가 스스로 충돌한다** → 거절.
# 숫자가 남는 것도 충돌로 본다(두 번째 날짜·회차 번호·표의 다음 행).
_BLOCK_CONFLICT = re.compile(
    r"[※*\d]|연장|변경|정정|철회|폐지|상시|수시|연중|회차|차수|추가\s*모집"
)

# **본문 전체**에서 찾는 충돌 단서 (라운드 3 Codex HIGH ①).
# 정정·연장은 라벨 뒤에만 오지 않는다 - 라벨 **앞**의 각주에도, 다른 글머리표
# 아래에도 온다. 그래서 낱말 단서는 블록이 아니라 본문 전체를 훑는다.
#
# 숫자·``※`` 는 여기 없다: 어느 공고에나 있어 전역 스캔에서는 뜻이 없다.
# 그 둘은 블록 잔여 검사(``_BLOCK_CONFLICT``)의 몫이다.
CONFLICT_CUES = (
    "연장", "변경", "정정", "철회", "폐지",
    "상시", "수시", "연중", "회차", "차수", "추가모집", "재공고",
)
_CONFLICT_CUE = re.compile("|".join(CONFLICT_CUES))

# 본문 끝의 **다른 공고 제목**(이전글/다음글 네비게이션)은 이 공고의 내용이
# 아니다. 실측: 통과 9건 중 4건이 이웃 글 제목의 ``연장``·``변경`` 때문에
# 거절될 뻔했다. 잘라내는 자리는 결정론적이다.
_NAVIGATION = re.compile(r"이전글|다음글")

_TERMINATOR = r"(?:\s*(?:까지|접수\s*마감|마감))"

_SHORT_DATE = rf"[{_APOS}]\s*\d{{2}}\s*\.\s*\d{{1,2}}\s*\.\s*\d{{1,2}}\s*\.?"
_WEEKDAY = r"(?:\s*\(\s*[월화수목금토일]\s*\))?"
_CLOCK = r"\s*,?(?:\s*\d{1,2}\s*:\s*\d{2})?"

_DETAIL_PERIOD = re.compile(
    rf"\A[(（]?\s*{_PERIOD_LABEL}\s*[)）]?\s*[:：]?\s*"
    rf"(?:"
    rf"(?:(?P<start>{_FULL_DATE}|{_SHORT_DATE}){_WEEKDAY}{_CLOCK}|공고일)"
    rf"\s*[~∼〜-]\s*"
    rf"(?P<end>{_FULL_DATE}|{_SHORT_DATE}|{_MONTH_DAY}){_WEEKDAY}{_CLOCK}"
    rf"{_TERMINATOR}?"
    rf"|"
    rf"(?P<only>{_FULL_DATE}|{_SHORT_DATE}){_WEEKDAY}{_CLOCK}{_TERMINATOR}"
    rf")"
)

_APOS_PREFIX = re.compile(rf"^[{_APOS}]")


def announcement_body(text: object) -> str:
    """이 공고의 본문 - 끝의 이전글/다음글 네비게이션은 잘라낸다."""
    normalized = _normalize(text)
    match = _NAVIGATION.search(normalized)
    return normalized[:match.start()] if match else normalized


def count_period_labels(text: object) -> int:
    """기간 라벨이 몇 번 나오는가. **본문 전체**를 세라 (라운드 2 HIGH ②)."""
    return len(_LABEL_SCAN.findall(announcement_body(text)))


def find_conflict_cue(text: object) -> str:
    """본문 전체의 첫 충돌 단서. 없으면 빈 문자열 (라운드 3 HIGH ①)."""
    match = _CONFLICT_CUE.search(announcement_body(text))
    return match.group(0) if match else ""


def _iso_date(token: str, fallback_year: Optional[int] = None) -> Optional[str]:
    """날짜 토큰 하나를 ISO 로. 달력에 없는 날짜와 모호한 토큰은 None.

    ``’26. 9. 28.`` 의 두 자리 연도는 ``2026`` 으로 읽는다 - 아포스트로피가
    붙은 표기에서만이며, 맨 숫자 두 자리는 연도로 보지 않는다.
    ``9. 28.`` 처럼 연도가 없는 토큰은 ``fallback_year``(시작일의 연도)가
    있을 때만 읽는다 - 연도 추정은 하지 않는다.
    """
    token = (token or "").strip()
    numbers = _DIGITS.findall(token)
    if _APOS_PREFIX.match(token):
        if len(numbers) != 3 or len(numbers[0]) != 2:
            return None
        return _calendar_date(2000 + int(numbers[0]), int(numbers[1]), int(numbers[2]))
    if len(numbers) == 3 and len(numbers[0]) == 4:
        return _calendar_date(int(numbers[0]), int(numbers[1]), int(numbers[2]))
    if len(numbers) == 2 and fallback_year is not None:
        return _calendar_date(fallback_year, int(numbers[0]), int(numbers[1]))
    return None


def _labelled_block(text: str) -> Optional[str]:
    """기간 라벨로 시작해 **다음 절이 시작되기 전까지**의 한 덩어리.

    ``※``·``*`` 는 덩어리를 끝내지 않는다 - 각주는 이 값의 정정일 수 있다.
    글머리표도 **그 뒤가 날짜로 이어지면** 끝내지 않는다 - 둘째 줄에 적힌
    정정(``○ 9월 30일까지 연장``)이 덩어리 밖으로 새면 보이지 않는다.
    """
    match = _LABEL_SCAN.search(text)
    if match is None:
        return None
    body_start = match.end()
    cursor = body_start
    while True:
        end = _BLOCK_END.search(text, cursor)
        if end is None:
            return text[match.start():]
        following = text[end.end():end.end() + _CONTINUATION_LOOKAHEAD]
        is_bullet = end.group(0) in _BULLETS
        if is_bullet and _DATE_HINT.search(following):
            cursor = end.end()          # 같은 값의 둘째 줄이다 - 계속 읽는다
            continue
        return text[match.start():end.start()]


def detail_period_reason(raw: Dict[str, object]) -> Optional[str]:
    """``_detail_period`` 가 거절한 이유. 읽어냈으면 None.

    순수 함수이며 저장되지 않는다 - 진단·테스트용이다.
    """
    text = _normalize(raw.get(DETAIL_TEXT_FIELD))
    if not text:
        # 받으러 갔는데 본문이 비어 온 것과, 아예 안 받은 것은 다르다.
        if raw.get(DETAIL_TEXT_FETCHED_AT_FIELD):
            return REASON_LOST
        return REASON_NO_EVIDENCE
    if raw.get(DETAIL_TEXT_TRUNCATED_FIELD):
        return REASON_TRUNCATED          # 정정이 창 밖에 있을 수 있다

    full_labels = raw.get(DETAIL_LABEL_COUNT_FIELD)
    if not isinstance(full_labels, int) or isinstance(full_labels, bool):
        return REASON_NO_EVIDENCE        # 본문 전체의 라벨 수를 모른다
    if full_labels == 0:
        return REASON_NO_EVIDENCE        # 본문에 기간 라벨 자체가 없다
    if full_labels != 1:
        return REASON_AMBIGUOUS_LABEL    # 창 밖에 두 번째 라벨이 있다
    if len(_LABEL_SCAN.findall(text)) != 1:
        return REASON_AMBIGUOUS_LABEL

    cue = raw.get(DETAIL_CONFLICT_CUE_FIELD)
    if not isinstance(cue, str):
        return REASON_NO_EVIDENCE        # 본문 전체를 훑은 적이 없다
    if cue:
        return REASON_CONFLICT           # 본문 어딘가에 정정·연장·상시가 있다

    block = _labelled_block(text)
    if not block:
        return REASON_NO_EVIDENCE
    match = _DETAIL_PERIOD.match(block)
    if not match:
        return REASON_SHAPE
    if _BLOCK_CONFLICT.search(block[match.end():]):
        return REASON_CONFLICT           # 각주·회차·두 번째 날짜
    return None


def _detail_period(raw: Dict[str, object]) -> Period:
    """기간 라벨이 **본문 전체에 정확히 하나** 있고, 본문에 정정 단서가 없고,
    그 블록 첫머리 전체가 범위 하나(또는 "…까지" 한 날짜)이며, 블록에
    **남는 것이 없을 때만** (시작, 종료)를 돌려준다.

    거절 사유는 ``detail_period_reason`` 이 말한다.
    """
    if detail_period_reason(raw) is not None:
        return None, None

    block = _labelled_block(_normalize(raw.get(DETAIL_TEXT_FIELD)))
    match = _DETAIL_PERIOD.match(block or "")
    if match is None:                    # pragma: no cover - reason 이 먼저 막는다
        return None, None

    only_token = match.group("only")
    if only_token:
        # "…까지" 한 날짜 - 시작일은 페이지에 없다
        end = _iso_date(only_token)
        return (None, end) if end else (None, None)

    start_token = match.group("start")
    start = _iso_date(start_token) if start_token else None
    if start_token and not start:
        return None, None          # 달력에 없는 시작일 - 읽은 것이 아니다

    end = _iso_date(
        match.group("end"), fallback_year=int(start[:4]) if start else None
    )
    if not end:
        return None, None
    if start and end < start:
        return None, None          # 뒤집힌 범위는 지어낸 것이다
    return start, end


def forest_service_period(raw: Dict[str, object]) -> Period:
    """산림청 공고 - 상세의 ``접수기간`` 한 자리.

    실측(2026-09-15, nttId=3223976):
    ``ㅇ 접수기간 : 2026. 9. 7. ~ 9. 28. 18:00까지`` → 2026-09-07 / 2026-09-28.
    목록에는 마감이 없다(``raw_data`` 에 ``date``·``posted`` 뿐).
    """
    return _detail_period(raw)


def kofpi_period(raw: Dict[str, object]) -> Period:
    """한국임업진흥원 - 상세의 ``모집기간`` 한 자리.

    실측(2026-09-15, bb_seq=12658):
    ``ㅁ 모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지`` → 2026-09-07 / 2026-09-30.

    목록 제목의 ``(~9.30)`` 은 **여기서도 쓰지 않는다**. 연도가 없어 연도를
    추정해야 하고, 그 추정이 12차까지 지난해 공고를 올해 마감으로 만들었다.
    """
    return _detail_period(raw)


def socialenterprise_period(raw: Dict[str, object]) -> Period:
    """한국사회적기업진흥원 - 상세의 ``공모기간`` 한 자리.

    실측(2026-09-15, bIdx=252623):
    ``□ (공모기간) 공고일 ~ 2026. 9. 28.(월) 13:00까지`` → (None, 2026-09-28).
    시작이 "공고일" 이면 **종료일만** 만든다 - 게시일을 시작일로 옮겨 적는 것은
    읽은 것이 아니라 지어낸 것이다.

    제목의 ``('26.09.30.(수) 14:00, 대전)`` 류는 설명회 **행사 일시**이고 상세
    에서도 ``일시`` 라벨 아래 있다. 기간 라벨이 아니므로 매치되지 않는다.
    """
    return _detail_period(raw)


# 상세 근거 소스가 공유하는 근거 키 묶음.
_DETAIL_EVIDENCE: Tuple[str, ...] = (
    DETAIL_TEXT_FIELD,
    DETAIL_TEXT_TRUNCATED_FIELD,
    DETAIL_LABEL_COUNT_FIELD,
    DETAIL_CONFLICT_CUE_FIELD,
    DETAIL_TEXT_URL_FIELD,
    DETAIL_TEXT_FETCHED_AT_FIELD,
)


# 기간을 만들 수 있는 소스 **전부**. 여기 없는 소스는 항상 None 이다.
PERIOD_EXTRACTORS: Dict[str, Callable[[Dict[str, object]], Period]] = {
    "seis": seis_period,
    "lawmaking": lawmaking_period,
    "forest_service": forest_service_period,
    "kofpi": kofpi_period,
    "socialenterprise": socialenterprise_period,
}

# 각 추출기가 **근거로 읽는** raw_data 키(+방증). 재수집 때 기간만 갱신하고
# 근거를 남겨 두면, 나중 재검증이 옛 근거로 철회된 기간을 되살린다
# (12차 게이트 HIGH). 그래서 이 키들은 새 수집 값으로 통째로 교체한다.
EVIDENCE_KEYS: Dict[str, Tuple[str, ...]] = {
    "seis": ("date", "date_field", "dday"),
    "lawmaking": ("period",),
    # 근거 넷은 **한 묶음**이다 - 하나만 남으면 옛 근거로 판정이 되살아난다
    # (12차 게이트 HIGH 와 같은 자리, 라운드 2 LOW ④).
    "forest_service": _DETAIL_EVIDENCE,
    "kofpi": _DETAIL_EVIDENCE,
    "socialenterprise": _DETAIL_EVIDENCE,
}
