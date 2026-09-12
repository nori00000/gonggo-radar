"""외부 URL 을 읽는 공용 헬퍼 — UA·헤더·보이는 텍스트 추출의 정본.

checker 의 생존 확인(HEAD/GET 프로브)과 glm_enrich 의 상세 텍스트 수집이 **같은
UA·헤더**를 쓴다. 공공기관 사이트 다수가 기본 python-requests UA 를 차단하거나
HEAD 를 지원하지 않으므로, 둘이 갈라지면 "게이트는 살아 있다고 보는데 보강은
빈손" 이라는 조용한 불일치가 생긴다.

여기서 가져오는 본문은 **외부 입력**이다 — 상세 텍스트는 그 자체로 발송본에
들어가지 않는다. GLM 이 그것을 읽고 낸 문장은 glm_enrich 의 결정론 게이트
(근거 부분문자열·형식·n 집합)와 사람의 미리보기 승인을 지난 뒤에만 md 로 들어간다
(alert/digest/checker.py 의 THREAT_MODEL 참고).
"""

import html
import re

import requests

# 공공기관 사이트 다수가 기본 python-requests UA를 차단하거나 HEAD를 지원하지 않는다.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PROBE_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 상세 텍스트 수집의 경계값 (V3.1). 넘기면 그 항목은 빈 문자열이다 — 전체 잡은
# 절대 멈추지 않는다(항목 단위 fail-open).
DETAIL_TIMEOUT = 10
MAX_DETAIL_BYTES = 512 * 1024
MAX_REDIRECTS = 3
DETAIL_TEXT_CHARS = 2000

_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_DROP_ELEMENT_RE = re.compile(
    r"(?is)<(script|style|nav|header|footer|noscript|svg|template|iframe)\b"
    r"[^>]*>.*?</\1\s*>"
)
_TAG_RE = re.compile(r"(?s)<[^>]*>")
# 제로폭·BOM 은 공백 정규화로 사라지지 않는다 — 여기서 지운다.
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


def visible_text(html_text: str, limit: int = DETAIL_TEXT_CHARS) -> str:
    """HTML → 보이는 텍스트 (script/style/nav 제거 · 공백 정규화 · 앞 limit 자)."""
    if not html_text:
        return ""
    text = _COMMENT_RE.sub(" ", html_text)
    text = _DROP_ELEMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _INVISIBLE_RE.sub("", text)
    text = " ".join(text.split())
    return text[:limit]


def _charset_of(content_type: str):
    if "charset=" not in content_type:
        return None
    return content_type.split("charset=")[-1].split(";")[0].strip().strip("\"'")


def _decode(raw: bytes, content_type: str) -> str:
    candidates = []
    declared = _charset_of(content_type)
    if declared:
        candidates.append(declared)
    candidates.extend(["utf-8", "cp949"])
    for candidate in candidates:
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(declared or "utf-8", errors="replace")


def fetch_detail_text(
    url,
    timeout: int = DETAIL_TIMEOUT,
    max_bytes: int = MAX_DETAIL_BYTES,
    limit: int = DETAIL_TEXT_CHARS,
) -> str:
    """URL 의 보이는 본문 앞부분. **실패는 전부 빈 문자열**(fail-open).

    실패로 보는 것: URL 아님·요청 예외·4xx/5xx·비 HTML(Content-Type)·빈 본문.
    리다이렉트는 MAX_REDIRECTS 까지, 본문은 max_bytes 까지만 읽는다.
    """
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return ""
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    response = None
    try:
        response = session.get(
            str(url),
            timeout=timeout,
            allow_redirects=True,
            headers=PROBE_HEADERS,
            stream=True,
        )
        if response.status_code >= 400:
            return ""
        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type and not (
            "html" in content_type or "text/plain" in content_type
        ):
            return ""
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=16384):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size >= max_bytes:
                break
        raw = b"".join(chunks)[:max_bytes]
        if not raw:
            return ""
        return visible_text(_decode(raw, content_type), limit)
    except Exception:  # noqa: BLE001 — 항목 단위 fail-open
        return ""
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        session.close()
