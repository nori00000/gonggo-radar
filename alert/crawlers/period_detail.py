"""상세 페이지 **근거 본문**만 받아 온다 - 기간은 여기서 만들지 않는다 (P2-D).

왜 별도 모듈인가: ``period_extractors`` 의 추출기들은 **순수 함수**여야 한다.
기간 산출은 저장 직전 관문(``alert.main._finalize_periods``)과 기존 행 재검증
(``Database.revalidate_periods``)이 **같은 함수**를 돌려 답이 같아야 하는데,
추출기가 네트워크를 타면 재검증이 실행마다 수백 번 GET 을 하고 그날의 응답에
따라 답이 달라진다. 그래서 네트워크는 이 모듈에, 판정은 저 모듈에 둔다.

계약:

- 이 모듈은 ``period_start``/``period_end`` 를 **건드리지 않는다**. 하는 일은
  ``raw_data`` 에 근거를 싣는 것뿐이고, 기간은 그 뒤 관문이 순수 추출기로 정한다.
- 항목당 HTTP 요청은 **최대 1회**다. 그 한 번의 응답에서 본문 전체와 제목 기준
  창을 함께 만든다(창만 받으면 창 밖의 정정·연장을 영원히 못 본다).
- 결과는 **세 갈래**다 (라운드 3 Codex HIGH ③):
    ``STORED``   받아서 근거를 실었다
    ``LOST``     **받았는데** 본문이 비었다/실패했다 → 근거를 비운다. 옛 근거를
                 물려주지 않는다 - 페이지가 사라졌는데 옛 마감을 계속 말하면
                 안 된다. 판정 사유는 ``근거 소실``
    ``SKIPPED``  이번 실행에 **안 받았다**(상한·예산·대상 아님) → 옛 근거를
                 그대로 물려준다(``carry_forward``)
- 물려주기는 **URL 이 같을 때만** 한다 (라운드 3 Codex HIGH ②). 같은
  ``source_id`` 라도 링크가 바뀌었으면 그 근거는 다른 공고의 것이다.
- 요청 수 상한은 **잡 전체**(``DetailQuota``)이고, 순서는 소스를 가로질러
  마지막 수집이 **오래된 것부터**다 (라운드 3 Codex MEDIUM ④). 소스별로 돌면
  첫 소스가 매 실행 상한을 다 쓴다.
"""
import json
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..utils.http_fetch import (
    DETAIL_TEXT_CHARS,
    FetchBudget,
    anchored_window,
    fetch_detail_text,
)
from .period_extractors import (
    DETAIL_CONFLICT_CUE_FIELD,
    DETAIL_LABEL_COUNT_FIELD,
    DETAIL_TEXT_FETCHED_AT_FIELD,
    DETAIL_TEXT_FIELD,
    DETAIL_TEXT_TRUNCATED_FIELD,
    DETAIL_TEXT_URL_FIELD,
    count_period_labels,
    find_conflict_cue,
)

DETAIL_TEXT_KEY = DETAIL_TEXT_FIELD
DETAIL_TEXT_TRUNCATED_KEY = DETAIL_TEXT_TRUNCATED_FIELD
DETAIL_LABEL_COUNT_KEY = DETAIL_LABEL_COUNT_FIELD
DETAIL_CONFLICT_CUE_KEY = DETAIL_CONFLICT_CUE_FIELD
DETAIL_TEXT_URL_KEY = DETAIL_TEXT_URL_FIELD
DETAIL_TEXT_FETCHED_AT_KEY = DETAIL_TEXT_FETCHED_AT_FIELD

# ``period_extractors.EVIDENCE_KEYS`` 가 선언하는 것과 **같은 묶음**이어야
# 한다. 하나만 남으면 옛 근거로 판정이 되살아난다 (12차 게이트 HIGH).
EVIDENCE_FIELDS = (
    DETAIL_TEXT_KEY,
    DETAIL_TEXT_TRUNCATED_KEY,
    DETAIL_LABEL_COUNT_KEY,
    DETAIL_CONFLICT_CUE_KEY,
    DETAIL_TEXT_URL_KEY,
    DETAIL_TEXT_FETCHED_AT_KEY,
)

# ``attach_detail_text`` 의 결과
STORED = "stored"
LOST = "lost"
SKIPPED = "skipped"

# 상세 본문을 받아야 마감을 알 수 있는 소스.
#
# 2026-09-15 실측(소스당 라이브 GET 1회, 픽스처는 ``tests/fixtures/periods/``):
#   forest_service  ``ㅇ 접수기간 : 2026. 9. 7. ~ 9. 28. 18:00까지``   → 결정론
#   kofpi           ``ㅁ 모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지`` → 결정론
#   socialenterprise ``□ (공모기간) 공고일 ~ 2026. 9. 28.(월) 13:00까지`` → 종료일만
# 같은 실측에서 **제외**한 소스:
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

# **잡 전체**(소스 합산)의 새 상세 요청 수 상한.
MAX_PERIOD_DETAIL_REQUESTS = 25

# 라벨·충돌 단서를 **본문 전체**에서 보기 위한 창 길이.
FULL_TEXT_CHARS = 20000

EvidenceKey = Tuple[str, str]


def wants_period_detail(source: object) -> bool:
    """이 소스가 상세 근거를 받아야 하는가."""
    return str(source or "").strip() in PERIOD_DETAIL_SOURCES


def evidence_key(item) -> EvidenceKey:
    """근거 사전의 키 - 소스를 가로질러 섞이지 않게 (소스, source_id)."""
    return (str(getattr(item, "source", "")),
            str(getattr(item, "source_id", "")))


class DetailQuota:
    """잡 전체의 새 상세 요청 수 상한. **실제 HTTP 요청만** 센다.

    중복·잘못된 URL·예산 소진으로 요청하지 않은 항목은 상한을 쓰지 않는다.
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
    items: Iterable, prior: Optional[Dict[EvidenceKey, dict]] = None
) -> List:
    """받을 순서 - **마지막 수집이 오래된 것부터** (없으면 맨 앞).

    소스를 가로질러 한 줄로 세운다 (라운드 3 Codex MEDIUM ④): 소스별로 돌면
    목록이 긴 첫 소스가 매 실행 상한을 다 쓰고 나머지 소스는 영영 못 받는다.
    안정 정렬이라 같은 시각끼리는 원래 순서를 지킨다.

    Args:
        items: 이번 실행에서 수집한 항목들 (여러 소스가 섞여 있어도 된다)
        prior: ``(source, source_id)`` -> 저장된 근거

    Returns:
        요청 후보를 오래된 순으로 담은 리스트
    """
    prior = prior or {}
    candidates = [item for item in items if _fetchable(item)]
    return sorted(
        candidates,
        key=lambda item: str(
            (prior.get(evidence_key(item)) or {}).get(
                DETAIL_TEXT_FETCHED_AT_KEY) or ""),
    )


def carry_forward(item, prior: Optional[Dict[EvidenceKey, dict]] = None) -> bool:
    """이번 실행에 **받지 않은** 항목에 이전 근거를 그대로 싣는다.

    ``Database.overwrite_periods`` 는 근거 키가 비어 오면 저장된 값을 **지운다**
    (12차 게이트: 옛 근거를 남겨 두면 철회된 기간이 부활한다). 그 규칙은 옳지만,
    "이번엔 안 받았다" 와 "받아 보니 근거가 사라졌다" 를 구별하지 못하면 상한에
    걸린 항목의 마감이 매 실행 지워졌다 살아난다. 그래서 안 받은 항목은 옛
    근거를 **그대로 다시 실어** 아무 것도 바뀌지 않게 한다.

    **URL 이 같을 때만** 물려준다 (라운드 3 Codex HIGH ②): 같은 source_id 라도
    링크가 바뀌었으면 저장된 근거는 다른 페이지의 것이다.

    Returns:
        이전 근거를 실었으면 True
    """
    stored = (prior or {}).get(evidence_key(item)) or {}
    if not stored.get(DETAIL_TEXT_URL_KEY):
        return False                     # 어느 URL 의 근거인지 모른다
    if str(stored[DETAIL_TEXT_URL_KEY]) != str(getattr(item, "url", "") or ""):
        return False                     # 링크가 바뀌었다 - 남의 근거다
    carried = {
        key: stored[key] for key in EVIDENCE_FIELDS if stored.get(key) is not None
    }
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
) -> str:
    """항목 하나의 상세 본문을 **한 번** 받아 ``raw_data`` 에 근거를 싣는다.

    한 번의 응답에서 셋을 만든다:

    - ``detail_text``          제목 기준 창 (추출기가 읽는 근거)
    - ``detail_label_count``   **본문 전체**의 기간 라벨 수
    - ``detail_conflict_cue``  **본문 전체**의 첫 정정·연장 단서

    기간 두 필드는 건드리지 않는다 - 관문이 정한다.

    Returns:
        ``STORED`` / ``LOST`` / ``SKIPPED``
    """
    if not _fetchable(item):
        return SKIPPED
    if budget is not None and budget.exhausted():
        return SKIPPED
    if quota is not None and not quota.take():
        return SKIPPED

    url = str(getattr(item, "url", "") or "")
    try:
        full = fetch(url, anchor="", limit=full_limit, budget=budget)
    except Exception:              # noqa: BLE001 - 항목 단위 fail-closed
        full = None

    payload = _load_raw(getattr(item, "raw_data", ""))
    for key in EVIDENCE_FIELDS:
        payload.pop(key, None)     # 근거는 **통째로** 새로 쓴다
    payload[DETAIL_TEXT_URL_KEY] = url
    payload[DETAIL_TEXT_FETCHED_AT_KEY] = now().isoformat()

    if not full:
        # 받으러 갔는데 본문이 없다. 시각·URL 만 남겨 "물어봤고 없었다" 를
        # 기록한다 - 판정은 ``근거 소실`` 이고, 옛 근거는 물려주지 않는다.
        item.raw_data = json.dumps(payload, ensure_ascii=False)
        return LOST

    window = anchored_window(str(full), getattr(item, "title", "") or "", limit)
    payload[DETAIL_TEXT_KEY] = str(window)
    payload[DETAIL_TEXT_TRUNCATED_KEY] = bool(
        getattr(full, "truncated", False)) or bool(
        getattr(window, "truncated", False))
    payload[DETAIL_LABEL_COUNT_KEY] = count_period_labels(str(full))
    payload[DETAIL_CONFLICT_CUE_KEY] = find_conflict_cue(str(full))
    item.raw_data = json.dumps(payload, ensure_ascii=False)
    return STORED
