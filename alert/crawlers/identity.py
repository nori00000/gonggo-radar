"""행 식별 - **DB 한 행이 무엇인가**를 정하는 단일 함수 (13차 게이트).

식별자를 크롤러마다 다르게 만들면 같은 번호를 쓰는 다른 공고가 한 행으로
합쳐진다. 실제로 그렇게 사라졌다:

- SEIS ``boardId=A&nttId=42`` 와 ``boardId=B&nttId=42`` → 둘 다 ``ntt:42``
- G2B 같은 공고번호의 ``bidNtceOrd=00/01`` (차수) → 둘 다 같은 번호
- Bizinfo ``detailUrl`` 이 없어 URL 이 문자열 ``"None"`` 이 된 두 공고

그래서 식별은 **이 모듈 하나**로 한다. 저장·조회·갱신 경로(``insert``,
``exists``, ``overwrite_periods``, ``merge_quote_fields``)는 전부 여기서
나온 키만 쓴다.

규칙 (우선순위):

1. 소스가 **식별 필드를 선언**했고 그 필드가 **전부** 있으면 그 조합이
   식별자다. API 가 주는 자기 ID 는 URL 보다 권위 있고, URL 이 없을 때
   만들어 내는 상세 링크(템플릿)가 식별자가 되는 것을 막는다.
   하나라도 없으면 URL 로 넘어간다 - 반쪽 키는 다른 공고와 겹친다.
2. 아니면 **정규화 URL** 이 곧 식별자다 (쿼리 정렬·세션 파라미터 제거).
3. URL 도 식별 필드도 없으면 크롤러가 만든 ``source_id`` 를 그대로 쓴다.
"""
import hashlib
import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 소스가 선언한 식별 필드 (``raw_data`` 키). 하나라도 빠지면 다른 공고가
# 같은 행이 된다 - G2B 차수(``bidNtceOrd``)가 그 사례다.
IDENTITY_FIELDS: Dict[str, Tuple[str, ...]] = {
    "g2b": ("bidNtceNo", "bidNtceOrd"),
    "bizinfo": ("pblancId",),
}

# 제거해도 **같은 페이지**임이 알려진 파라미터만 지운다.
#
# 14차 게이트: ``sid`` 처럼 의미가 확인되지 않은 파라미터를 지웠더니
# ``/boardView.do?sid=A&nttId=42`` 와 ``sid=B`` 가 한 행으로 합쳐져,
# A 제목에 B 의 마감이 저장·전달됐다. 모르면 **남긴다** - 지우는 쪽이
# 공고를 잃는다.
VOLATILE_QUERY_PARAMS = frozenset({
    "jsessionid", "phpsessid", "_", "fbclid",
})
# 접두사로만 알 수 있는 추적 파라미터
VOLATILE_QUERY_PREFIXES = ("utm_",)


def _is_volatile(name: str) -> bool:
    """이 쿼리 파라미터가 **페이지를 가르지 않는** 것으로 알려져 있는가."""
    lowered = (name or "").lower()
    return (
        lowered in VOLATILE_QUERY_PARAMS
        or lowered.startswith(VOLATILE_QUERY_PREFIXES)
    )

# 경로에 붙는 세션 표기 (``;jsessionid=…``)
_PATH_SESSION = re.compile(r";jsessionid=[^/?#]*", re.I)


def clean_text(value: Any) -> str:
    """``None`` 이 문자열 ``"None"`` 으로 새는 것을 막는다.

    ``str(item.get("detailUrl"))`` 은 값이 없을 때 ``"None"`` 을 만든다.
    그 문자열이 URL 자리에 들어가면 **서로 다른 공고가 같은 URL** 을 갖는다.
    """
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "null") else text


def normalize_url(url: Any) -> Optional[str]:
    """비교 가능한 형태로 URL 을 정규화한다 (없으면 None).

    - 스킴·호스트 소문자, 조각(fragment) 제거, 끝 슬래시 제거
    - 세션·캐시버스터 쿼리 파라미터 제거 후 **정렬**
    """
    text = clean_text(url)
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None

    path = _PATH_SESSION.sub("", parts.path or "")
    path = path.rstrip("/") or "/"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_volatile(key)
    )
    return urlunsplit((
        (parts.scheme or "").lower(),
        (parts.netloc or "").lower(),
        path,
        urlencode(query),
        "",
    ))


def _url_and_raw(item: Any) -> Tuple[Optional[str], Dict[str, Any], str]:
    """``(url, raw_data 딕셔너리, 크롤러 source_id)`` 를 꺼낸다."""
    if isinstance(item, Mapping):
        url = item.get("url")
        raw = item.get("raw_data")
        source_id = item.get("source_id")
    else:
        url = getattr(item, "url", None)
        raw = getattr(item, "raw_data", None)
        source_id = getattr(item, "source_id", None)

    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return url, raw, clean_text(source_id)


def identity_key(source: str, item: Any) -> str:
    """이 항목이 가리키는 **DB 한 행**의 키.

    Args:
        source: 소스 이름
        item: ``RawAnnouncement`` 또는 ``{"url":…, "raw_data":…}`` 매핑

    Returns:
        ``fld:…`` / ``url:…`` / 크롤러 ``source_id`` / ``raw:…``
    """
    url, raw, source_id = _url_and_raw(item)

    fields = IDENTITY_FIELDS.get((source or "").strip(), ())
    if fields:
        values = [clean_text(raw.get(name)) for name in fields]
        # **전부** 있을 때만 필드 키다. 하나라도 비면 그 키는 다른 공고와
        # 같아질 수 있으므로(``fld:번호|``) URL 로 넘어간다 (14차 게이트).
        if all(values):
            return "fld:" + "|".join(values)

    normalized = normalize_url(url)
    if normalized:
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
        return f"url:{digest}"

    if source_id:
        return source_id           # 레거시·수동 입력: 크롤러가 만든 값 유지

    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return "raw:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
