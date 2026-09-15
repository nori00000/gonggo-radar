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
# 싣고(``detail_text``), 아래 함수들은 그 문자열만 읽는 **순수 함수**다.
#
# 근거 본문을 자르지 않고 창 그대로 저장하는 이유: 자르는 규칙을 나중에
# 좁혀도 재검증이 옛 행에 새 규칙을 다시 적용할 수 있어야 한다.

DETAIL_TEXT_FIELD = "detail_text"
DETAIL_TEXT_TRUNCATED_FIELD = "detail_text_truncated"

# 기간 라벨 - 이 넷만 접수 마감을 뜻한다. ``일시``·``행사``·``교육기간`` 처럼
# 다른 일정을 가리키는 라벨은 여기 없으므로 영원히 매치되지 않는다.
_PERIOD_LABEL = r"(?:접수|모집|공모|신청)\s*기간"
_LABEL_SCAN = re.compile(_PERIOD_LABEL)

# 본문 창에서 필드 경계로 쓰는 글머리표. 이 문자들 사이가 한 항목이다.
# ``*``·``※`` 는 각주 표시다 - 각주가 시작되면 그 라벨의 **값은 끝났다**
# (각주가 "예산 소진 시 조기마감" 을 덧붙여도 적힌 종료일은 그대로다).
_BULLETS = "ㅁㅇ□■○◦●▶◆※•*"
# 두 자리 연도 표기 앞에 오는 아포스트로피 (``’26. 9. 28.``)
_APOS = "'’‘`"

# 값 뒤에 이것이 오면 **다른 절이 시작된 것**이다 - 라벨의 값은 여기서 끝난다.
# 날짜를 품을 수 있는 말(심사·선정·발표·교육 …)은 **일부러 넣지 않았다**:
# 그런 말이 뒤따르면 어디까지가 접수기간인지 알 수 없으므로 거절이 맞다.
_SECTION_WORDS = (
    "첨부파일", "첨부", "붙임", "문의처", "문의", "담당부서", "담당자", "담당",
    "신청방법", "접수방법", "제출방법", "제출서류", "이전글", "다음글", "목록",
)
_TAIL = rf"(?=$|[{_BULLETS}]|(?:{'|'.join(_SECTION_WORDS)}))"

# 마감을 뜻하는 문맥 낱말. 날짜 하나만 있을 때는 **필수**다 - 이 말이 없으면
# 그 날짜가 시작인지 끝인지 행사일인지 페이지가 말하지 않은 것이다.
_TERMINATOR = r"(?:\s*(?:까지|접수\s*마감|마감))"

_SHORT_DATE = rf"[{_APOS}]\s*\d{{2}}\s*\.\s*\d{{1,2}}\s*\.\s*\d{{1,2}}\s*\.?"
_WEEKDAY = r"(?:\s*\(\s*[월화수목금토일]\s*\))?"
# 요일 뒤에 쉼표가 오는 표기 (``2026. 9. 11.(금), 18:00까지``)
_CLOCK = r"\s*,?(?:\s*\d{1,2}\s*:\s*\d{2})?"

# 라벨 **한 자리 전체**가 범위 하나(또는 "…까지" 한 날짜)여야 한다 -
# ``_RANGE_ONLY`` 와 같은 규율을 본문 창에 적용한 것이다. 뒤는 다음 글머리표·
# 다른 절·창의 끝으로 막아 **옆 항목의 날짜가 들어오지 못하게** 한다.
#
# 라벨 앞에 한글이 붙은 말(``사업기간``·``운영기간``)은 다른 라벨이므로
# ``(?<![가-힣])`` 로 막는다.
#
# 시작이 ``공고일`` 인 형태(socialenterprise 실측)는 **종료일만** 만든다 -
# "공고일" 은 날짜가 아니므로 시작일을 지어내지 않는다.
_DETAIL_PERIOD = re.compile(
    rf"(?<![가-힣])[(（]?\s*{_PERIOD_LABEL}\s*[)）]?\s*[:：]?\s*"
    rf"(?:"
    rf"(?:(?P<start>{_FULL_DATE}|{_SHORT_DATE}){_WEEKDAY}{_CLOCK}|공고일)"
    rf"\s*[~∼〜-]\s*"
    rf"(?P<end>{_FULL_DATE}|{_SHORT_DATE}|{_MONTH_DAY}){_WEEKDAY}{_CLOCK}"
    rf"{_TERMINATOR}?"
    rf"|"
    rf"(?P<only>{_FULL_DATE}|{_SHORT_DATE}){_WEEKDAY}{_CLOCK}{_TERMINATOR}"
    rf")"
    rf"\s*{_TAIL}"
)

_APOS_PREFIX = re.compile(rf"^[{_APOS}]")


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


def _detail_period(raw: Dict[str, object]) -> Period:
    """``detail_text`` 창에 기간 라벨이 **정확히 하나** 있고 그 자리 전체가
    범위 하나일 때만 (시작, 종료)를 돌려준다.

    라벨이 둘 이상이면 어느 쪽이 이 공고의 접수인지 페이지가 말하지 않으므로
    아무 것도 만들지 않는다 (9차 게이트 HIGH 와 같은 규율). 잘린 창의 마지막
    조각도 쓰지 않는다 - 뒤가 잘렸으면 그 범위가 끝났는지 알 수 없다.
    """
    text = _normalize(raw.get(DETAIL_TEXT_FIELD))
    if not text:
        return None, None
    if len(_LABEL_SCAN.findall(text)) != 1:
        return None, None

    match = _DETAIL_PERIOD.search(text)
    if not match:
        return None, None
    if raw.get(DETAIL_TEXT_TRUNCATED_FIELD) and match.end() >= len(text):
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


# 16차 게이트: bizinfo·g2b 추출기는 **등록 해제**했다. 두 소스는 회사용
# 경로여서 협의회 브리핑과 무관하고, 그 기간을 유지하려다 식별자 설계가
# 사이클마다 새 경합을 만들었다. 지금은 정규화가 두 소스의 기간을 비운다.


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
    "forest_service": (DETAIL_TEXT_FIELD, DETAIL_TEXT_TRUNCATED_FIELD),
    "kofpi": (DETAIL_TEXT_FIELD, DETAIL_TEXT_TRUNCATED_FIELD),
    "socialenterprise": (DETAIL_TEXT_FIELD, DETAIL_TEXT_TRUNCATED_FIELD),
}
