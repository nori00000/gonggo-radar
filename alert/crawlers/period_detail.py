"""상세 페이지 **근거 본문**만 받아 온다 - 기간은 여기서 만들지 않는다 (P2-D).

왜 별도 모듈인가: ``period_extractors`` 의 두 함수는 **순수 함수**여야 한다.
기간 산출은 저장 직전 관문(``alert.main._finalize_periods``)과 기존 행 재검증
(``Database.revalidate_periods``)이 **같은 함수**를 돌려 답이 같아야 하는데,
추출기가 네트워크를 타면 재검증이 실행마다 수백 번 GET 을 하고 그날의 응답에
따라 답이 달라진다. 그래서 네트워크는 이 모듈에, 판정은 저 모듈에 둔다.

계약:

- 이 모듈은 ``period_start``/``period_end`` 를 **건드리지 않는다**. 하는 일은
  ``raw_data`` 에 근거 본문(``detail_text``)을 싣는 것뿐이고, 기간은 그 뒤
  관문이 순수 추출기로 정한다.
- 항목당 요청은 **최대 1회**다. 실패·시간 초과·빈 응답이면 키를 만들지
  않는다 - 근거가 없으면 기간도 없다(fail-closed).
- 요청 예산은 ``alert.utils.http_fetch.FetchBudget`` 이 잡 단위로 잡고,
  소스당 요청 수는 ``MAX_PERIOD_DETAIL_REQUESTS`` 로 한 번 더 막는다.
- 저장하는 것은 **가공하지 않은 본문 창**이다. 잘라낸 근거 문자열을 저장하면
  나중에 자르는 규칙을 좁혀도 옛 행에는 적용되지 않는다 - 창을 통째로 두면
  재검증이 새 규칙으로 다시 읽는다.
"""
import json
from datetime import datetime
from typing import Callable, Optional

from ..utils.http_fetch import DETAIL_TEXT_CHARS, FetchBudget, fetch_detail_text

# ``raw_data`` 에 싣는 키. ``period_extractors.EVIDENCE_KEYS`` 가 이 키를
# 근거로 선언하므로 재수집 때 통째로 교체된다.
DETAIL_TEXT_KEY = "detail_text"
DETAIL_TEXT_TRUNCATED_KEY = "detail_text_truncated"
DETAIL_TEXT_FETCHED_AT_KEY = "detail_text_fetched_at"

# 상세 본문을 받아야 마감을 알 수 있는 소스.
#
# 2026-09-15 실측(소스당 라이브 GET 1회, 픽스처는 ``tests/fixtures/periods/``):
#   forest_service  ``ㅇ 접수기간 : 2026. 9. 7. ~ 9. 28. 18:00까지``   → 결정론
#   kofpi           ``ㅁ 모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지`` → 결정론
#   socialenterprise ``□ (공모기간) 공고일 ~ 2026. 9. 28.(월) 13:00까지`` → 종료일만 결정론
# 같은 실측에서 **제외**한 소스는 ``docs`` 가 아니라 여기 적어 둔다:
#   coop   상세의 ``모집기간`` 이 "1~3회 컨설팅 회차별 상이" + 3행 표(8.14·
#          8.27·9.15 세 마감)다. 어느 것이 이 공고의 마감인지 페이지가 말하지
#          않는다. 목록 제목도 ``(~6.28,~8.11,~9.2)`` 처럼 여러 개다.
#   fowi   이 호스트에서 상세·목록 모두 응답이 비어 온다(curl 52 Empty reply
#          from server, urllib3 경로도 0바이트). 근거를 받을 수 없다.
#   mafra  입법예고 상세 본문은 "붙임과 같이 입법예고합니다" 한 줄이고 의견제출
#          마감은 첨부 hwpx 안에 있다. 페이지에 마감이 없다.
PERIOD_DETAIL_SOURCES = frozenset({
    "forest_service",
    "kofpi",
    "socialenterprise",
})

# 소스·실행당 새 상세 요청 수 상한. 목록이 이보다 길면 앞쪽부터 받는다 -
# 목록은 최신순이므로 마감이 남아 있는 공고가 앞에 온다.
MAX_PERIOD_DETAIL_REQUESTS = 25


def wants_period_detail(source: object) -> bool:
    """이 소스가 상세 근거를 받아야 하는가."""
    return str(source or "").strip() in PERIOD_DETAIL_SOURCES


def _load_raw(raw_data: Optional[str]) -> dict:
    """``raw_data`` JSON 을 딕셔너리로 읽는다 (깨져 있으면 빈 딕셔너리)."""
    if not raw_data:
        return {}
    try:
        loaded = json.loads(raw_data)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def attach_detail_text(
    item,
    budget: Optional[FetchBudget] = None,
    fetch: Callable[..., object] = fetch_detail_text,
    limit: int = DETAIL_TEXT_CHARS,
    now: Callable[[], datetime] = datetime.now,
) -> bool:
    """항목 하나의 상세 본문을 **한 번** 받아 ``raw_data`` 에 싣는다.

    기간 두 필드는 건드리지 않는다 - 관문이 정한다.

    Args:
        item: ``RawAnnouncement`` (제자리에서 고친다)
        budget: 잡 전체 수집 예산. 소진되면 아무 것도 하지 않는다
        fetch: 수집 함수 (테스트 주입용)
        limit: 본문 창 길이
        now: 시각 (테스트 주입용)

    Returns:
        근거를 실었으면 True
    """
    if not wants_period_detail(getattr(item, "source", "")):
        return False
    url = getattr(item, "url", "") or ""
    if not url or url.endswith("#void"):
        return False
    if budget is not None and budget.exhausted():
        return False

    try:
        text = fetch(url, anchor=getattr(item, "title", "") or "",
                     limit=limit, budget=budget)
    except Exception:              # noqa: BLE001 - 항목 단위 fail-closed
        return False
    if not text:
        return False

    payload = _load_raw(getattr(item, "raw_data", ""))
    payload[DETAIL_TEXT_KEY] = str(text)
    payload[DETAIL_TEXT_TRUNCATED_KEY] = bool(getattr(text, "truncated", False))
    payload[DETAIL_TEXT_FETCHED_AT_KEY] = now().isoformat()
    item.raw_data = json.dumps(payload, ensure_ascii=False)
    return True
