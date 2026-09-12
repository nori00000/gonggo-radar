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

from .date_labels import strip_notes

Period = Tuple[Optional[str], Optional[str]]

# 완전 날짜(연·월·일) 와 월·일만 있는 날짜.
# 끝의 마침표를 받아 준다 - 이 사이트들은 ``2026. 9. 7.`` 로 쓴다.
_FULL_DATE = r"\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}\s*일?\s*\.?"
_MONTH_DAY = r"\d{1,2}\s*[-./월]\s*\d{1,2}\s*일?\s*\.?"

# 범위 ``A ~ B`` - A 는 **완전 날짜**여야 하고, B 는 완전 날짜이거나
# 월·일(그때 연도는 A 에서 온다)이다.
_RANGE = re.compile(rf"({_FULL_DATE})\s*[~∼〜-]\s*({_FULL_DATE}|{_MONTH_DAY})")

# SEIS 카드 ``p.date`` - 값이 **접수기간 라벨로 시작**할 때만 기간이다.
# 구조 라벨(파서가 붙이는 ``date_label``)은 믿지 않는다: 같은 자리에
# 교육기간·행사일정·무라벨 범위가 함께 들어온다.
_SEIS_RECEPTION_LABEL = re.compile(r"^\s*접수\s*기간\s*[:：]?\s*(\S.*)$")

_DIGITS = re.compile(r"\d+")


def _normalize(text: object) -> str:
    """공백을 하나로 줄이고 요일 같은 **숫자 없는 괄호 주석**을 벗긴다."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", strip_notes(text)).strip()


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


def _single_range(text: str) -> Period:
    """범위가 **정확히 하나** 일 때만 (시작, 종료)를 돌려준다.

    범위가 둘 이상이면 어느 쪽이 접수기간인지 알 수 없다 - 예전에는
    ``2026.09.01 ~ 2026.09.30 / 심사기간 2026.10.01 ~ 2026.10.31`` 에서
    첫 시작일과 **마지막 종료일**을 이어 붙여 없는 기간을 만들었다.
    """
    if not text:
        return None, None
    matches = _RANGE.findall(text)
    if len(matches) != 1:
        return None, None

    start_text, end_text = matches[0]
    start = _full_date(start_text)
    if not start:
        return None, None
    end = _full_date(end_text) or _month_day(end_text, int(start[:4]))
    if not end or end < start:
        return None, None      # 뒤집힌 범위는 읽은 것이 아니라 지어낸 것
    return start, end


def seis_period(raw: Dict[str, object]) -> Period:
    """SEIS - 값이 ``접수기간 …`` 으로 시작하는 단일 범위만 기간이다.

    라벨 없는 범위는 접수 의미가 입증되지 않았으므로 기간이 아니다
    (9차 게이트 HIGH ①: 실 카드의 무라벨 범위 14건이 모두 마감이 됐다).

    Args:
        raw: 크롤러가 남긴 ``raw_data`` 딕셔너리. ``date`` 만 읽는다

    Returns:
        ``(period_start, period_end)`` - 조건을 못 채우면 ``(None, None)``
    """
    match = _SEIS_RECEPTION_LABEL.match(_normalize(raw.get("date")))
    if not match:
        return None, None
    return _single_range(match.group(1))


def lawmaking_period(raw: Dict[str, object]) -> Period:
    """국민참여입법센터 - 의견제출 기간 셀에 범위가 하나일 때 그 종료일.

    셀에 ``/ 심사기간 …`` 이 붙어 범위가 둘이면 None 이다 (9차 게이트
    HIGH: 심사 종료일이 의견접수 마감으로 합성됐다).

    Args:
        raw: 크롤러가 남긴 ``raw_data`` 딕셔너리. ``period`` 만 읽는다

    Returns:
        ``(None, period_end)`` - 조건을 못 채우면 ``(None, None)``
    """
    _start, end = _single_range(_normalize(raw.get("period")))
    if not end:
        return None, None
    return None, end


# 기간을 만들 수 있는 소스 **전부**. 여기 없는 소스는 항상 None 이다.
PERIOD_EXTRACTORS: Dict[str, Callable[[Dict[str, object]], Period]] = {
    "seis": seis_period,
    "lawmaking": lawmaking_period,
}
