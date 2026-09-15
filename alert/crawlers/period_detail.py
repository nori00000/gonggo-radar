"""상세 페이지 **근거 본문**만 받아 온다 - 기간은 여기서 만들지 않는다 (P2-D).

왜 별도 모듈인가: ``period_extractors`` 의 추출기들은 **순수 함수**여야 한다.
기간 산출은 저장 직전 관문(``alert.main._finalize_periods``)과 기존 행 재검증
(``Database.revalidate_periods``)이 **같은 함수**를 돌려 답이 같아야 하는데,
추출기가 네트워크를 타면 재검증이 실행마다 수백 번 GET 을 하고 그날의 응답에
따라 답이 달라진다. 그래서 네트워크는 이 모듈에, 판정은 저 모듈에 둔다.

계약:

- 이 모듈은 ``period_start``/``period_end`` 를 **건드리지 않는다**. 하는 일은
  ``raw_data`` 에 근거를 싣는 것뿐이고, 기간은 그 뒤 관문이 순수 추출기로 정한다.
- 항목당 HTTP 요청은 **최대 1회**다. 그 한 번의 응답에서 본문 **전체**와 제목
  기준 **창**을 함께 만든다(창만 받으면 창 밖의 정정·연장을 영원히 못 본다 -
  라운드 2 Codex HIGH ②).
- 실패·시간 초과·빈 응답이면 키를 만들지 않는다 - 근거가 없으면 기간도 없다.
- 이번 실행에 **받지 않은** 항목은 ``carry_forward`` 가 이전 근거를 그대로
  싣는다. 안 받았다는 이유로 저장된 마감이 지워지면 안 된다
  (``Database.overwrite_periods`` 는 근거가 비면 지운다 - 라운드 2 Codex MEDIUM ③).
- 요청 수 상한은 **잡 전체**(``DetailQuota``)이고, 누가 먼저 받을지는 마지막
  수집이 **오래된 것부터**다. 소스별 상한이면 목록 뒤쪽이 매 실행 굶는다.
"""
import json
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional

from ..utils.http_fetch import (
    DETAIL_TEXT_CHARS,
    FetchBudget,
    anchored_window,
    fetch_detail_text,
)
from .period_extractors import (
    DETAIL_LABEL_COUNT_FIELD,
    DETAIL_TEXT_FIELD,
    DETAIL_TEXT_TRUNCATED_FIELD,
    count_period_labels,
)

# ``raw_data`` 에 싣는 키. ``period_extractors.EVIDENCE_KEYS`` 가 이 넷을 근거로
# 선언하므로 재수집 때 **한 묶음으로** 교체된다(원자적 교체 - 라운드 2 LOW ④).
DETAIL_TEXT_KEY = DETAIL_TEXT_FIELD
DETAIL_TEXT_TRUNCATED_KEY = DETAIL_TEXT_TRUNCATED_FIELD
DETAIL_LABEL_COUNT_KEY = DETAIL_LABEL_COUNT_FIELD
DETAIL_TEXT_FETCHED_AT_KEY = "detail_text_fetched_at"

EVIDENCE_FIELDS = (
    DETAIL_TEXT_KEY,
    DETAIL_TEXT_TRUNCATED_KEY,
    DETAIL_LABEL_COUNT_KEY,
    DETAIL_TEXT_FETCHED_AT_KEY,
)

# 상세 본문을 받아야 마감을 알 수 있는 소스.
#
# 2026-09-15 실측(소스당 라이브 GET 1회, 픽스처는 ``tests/fixtures/periods/``):
#   forest_service  ``ㅇ 접수기간 : 2026. 9. 7. ~ 9. 28. 18:00까지``   → 결정론
#   kofpi           ``ㅁ 모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지`` → 결정론
#   socialenterprise ``□ (공모기간) 공고일 ~ 2026. 9. 28.(월) 13:00까지`` → 종료일만 결정론
# 같은 실측에서 **제외**한 소스는 여기 적어 둔다:
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

# **잡 전체**(소스 합산)의 새 상세 요청 수 상한. 소스별 상한이면 소스 수만큼
# 곱해져 예산이 새고, 목록이 상한보다 길 때 뒤쪽이 매 실행 굶는다.
MAX_PERIOD_DETAIL_REQUESTS = 25

# 라벨을 **본문 전체**에서 세기 위한 창 길이. 제목 기준 창(2000자)만 보면 창
# 밖의 두 번째 라벨·정정 문구를 못 본다.
FULL_TEXT_CHARS = 20000


def wants_period_detail(source: object) -> bool:
    """이 소스가 상세 근거를 받아야 하는가."""
    return str(source or "").strip() in PERIOD_DETAIL_SOURCES


class DetailQuota:
    """잡 전체의 새 상세 요청 수 상한. **실제 HTTP 요청만** 센다.

    중복·잘못된 URL·예산 소진으로 요청하지 않은 항목은 상한을 쓰지 않는다
    (라운드 2 Codex MEDIUM ③).
    """

    def __init__(self, limit: int = MAX_PERIOD_DETAIL_REQUESTS) -> None:
        self.limit = int(limit)
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def take(self) -> bool:
        """요청 직전에 부른다. 남아 있으면 하나 쓰고 True."""
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _load_raw(raw_data: Optional[str]) -> dict:
    """``raw_data`` JSON 을 딕셔너리로 읽는다 (깨져 있으면 빈 딕셔너리)."""
    if not raw_data:
        return {}
    try:
        loaded = json.loads(raw_data)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _fetchable(item) -> bool:
    """요청을 보낼 수 있는 항목인가 (상한을 쓰기 전에 본다)."""
    if not wants_period_detail(getattr(item, "source", "")):
        return False
    url = getattr(item, "url", "") or ""
    return bool(url) and not url.endswith("#void")


def fetch_order(
    items: Iterable, prior: Optional[Dict[str, dict]] = None
) -> List:
    """받을 순서 - **마지막 수집이 오래된 것부터** (없으면 맨 앞).

    목록 순서대로만 받으면 목록이 상한보다 길 때 뒤쪽 항목이 매 실행 굶는다
    (4차 게이트 #6 이 인용 경로에서 겪은 것과 같은 자리). 안정 정렬이라 같은
    시각끼리는 목록 순서를 지킨다.

    Args:
        items: 이번 실행에서 수집한 항목들
        prior: ``source_id`` -> 저장된 근거 (``Database.get_detail_evidence``)

    Returns:
        요청 후보를 오래된 순으로 담은 리스트
    """
    prior = prior or {}
    candidates = [item for item in items if _fetchable(item)]
    return sorted(
        candidates,
        key=lambda item: str(
            (prior.get(str(getattr(item, "source_id", ""))) or {}).get(
                DETAIL_TEXT_FETCHED_AT_KEY
            )
            or ""
        ),
    )


def carry_forward(item, prior: Optional[Dict[str, dict]] = None) -> bool:
    """이번 실행에 **받지 않은** 항목에 이전 근거를 그대로 싣는다.

    ``Database.overwrite_periods`` 는 근거 키가 비어 오면 저장된 값을 **지운다**
    (12차 게이트: 옛 근거를 남겨 두면 철회된 기간이 부활한다). 그 규칙은 옳지만,
    "이번엔 안 받았다" 와 "받아 보니 근거가 사라졌다" 를 구별하지 못하면 상한에
    걸린 항목의 마감이 매 실행 지워졌다 살아난다. 그래서 안 받은 항목은 옛
    근거를 **그대로 다시 실어** 아무 것도 바뀌지 않게 한다.

    Returns:
        이전 근거를 실었으면 True
    """
    prior = prior or {}
    stored = prior.get(str(getattr(item, "source_id", ""))) or {}
    carried = {
        key: stored[key] for key in EVIDENCE_FIELDS if stored.get(key) is not None
    }
    if not carried:
        return False
    payload = _load_raw(getattr(item, "raw_data", ""))
    payload.update(carried)
    item.raw_data = json.dumps(payload, ensure_ascii=False)
    return True


def attach_detail_text(
    item,
    budget: Optional[FetchBudget] = None,
    quota: Optional[DetailQuota] = None,
    fetch: Callable[..., object] = fetch_detail_text,
    limit: int = DETAIL_TEXT_CHARS,
    full_limit: int = FULL_TEXT_CHARS,
    now: Callable[[], datetime] = datetime.now,
) -> bool:
    """항목 하나의 상세 본문을 **한 번** 받아 ``raw_data`` 에 근거를 싣는다.

    한 번의 응답에서 두 가지를 만든다:

    - ``detail_text``          제목 기준 창 (추출기가 읽는 근거)
    - ``detail_label_count``   **본문 전체**의 기간 라벨 수. 창 밖에 두 번째
      라벨이나 정정 문구가 있으면 그 창은 믿을 수 없다

    기간 두 필드는 건드리지 않는다 - 관문이 정한다.

    Args:
        item: ``RawAnnouncement`` (제자리에서 고친다)
        budget: 잡 전체 수집 시간 예산. 소진되면 요청하지 않는다
        quota: 잡 전체 요청 수 상한. 실제 요청 직전에만 하나 쓴다
        fetch: 수집 함수 (테스트 주입용)
        limit: 제목 기준 창 길이
        full_limit: 라벨을 세기 위해 읽는 본문 길이
        now: 시각 (테스트 주입용)

    Returns:
        근거를 실었으면 True
    """
    if not _fetchable(item):
        return False
    if budget is not None and budget.exhausted():
        return False
    if quota is not None and not quota.take():
        return False

    try:
        full = fetch(url := getattr(item, "url", ""), anchor="",
                     limit=full_limit, budget=budget)
    except Exception:              # noqa: BLE001 - 항목 단위 fail-closed
        return False
    del url
    if not full:
        return False

    title = getattr(item, "title", "") or ""
    window = anchored_window(str(full), title, limit)
    truncated = bool(getattr(full, "truncated", False)) or bool(
        getattr(window, "truncated", False)
    )

    payload = _load_raw(getattr(item, "raw_data", ""))
    payload[DETAIL_TEXT_KEY] = str(window)
    payload[DETAIL_TEXT_TRUNCATED_KEY] = truncated
    payload[DETAIL_LABEL_COUNT_KEY] = count_period_labels(str(full))
    payload[DETAIL_TEXT_FETCHED_AT_KEY] = now().isoformat()
    item.raw_data = json.dumps(payload, ensure_ascii=False)
    return True
