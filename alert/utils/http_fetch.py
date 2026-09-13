"""외부 URL 을 읽는 공용 헬퍼 — UA·헤더·본문 추출의 정본.

checker 의 생존 확인(HEAD/GET 프로브)과 glm_enrich 의 상세 텍스트 수집이 **같은
UA·헤더**를 쓴다. 공공기관 사이트 다수가 기본 python-requests UA 를 차단하거나
HEAD 를 지원하지 않으므로, 둘이 갈라지면 "게이트는 살아 있다고 보는데 보강은
빈손" 이라는 조용한 불일치가 생긴다.

여기서 가져오는 본문은 **외부 입력**이다 — 상세 텍스트는 그 자체로 발송본에
들어가지 않는다. GLM 이 그것을 읽고 낸 문장은 glm_enrich 의 결정론 게이트
(추출형 문법·근거 토큰 경계 대조·n 집합)와 사람의 미리보기 승인을 지난 뒤에만
md 로 들어간다 (alert/digest/checker.py 의 THREAT_MODEL 참고).

시간·크기 규율 (Codex v3.1 MEDIUM): 항목 하나는 **벽시계 기준** DETAIL_TIMEOUT
안에 끝나야 하고(느리게 찔끔찔끔 보내는 서버가 read timeout 을 영원히 리셋하는
경로를 막는다), 잡 전체는 FetchBudget 을 넘기지 못한다. 리다이렉트는 직접
따라간다 — requests 의 자동 추적은 중간 응답 본문을 전부 소비해 크기 상한 밖이다.
"""

import codecs
import re
import time
from collections import deque
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.parse import urljoin

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

# 상세 텍스트 수집의 경계값. 넘기면 그 항목은 빈 문자열이다 — 전체 잡은 절대
# 멈추지 않는다(항목 단위 fail-open).
DETAIL_TIMEOUT = 10          # 항목 하나의 **벽시계** 상한(초)
JOB_BUDGET_SECONDS = 120     # 잡 전체의 수집 예산(초). 소진 뒤 항목은 빈 문자열
MAX_DETAIL_BYTES = 512 * 1024
MAX_REDIRECTS = 3
DETAIL_TEXT_CHARS = 2000
# 앵커(제목)를 통째로 못 찾을 때 다시 시도하는 앞부분 길이. 목록 페이지의 제목이
# 말줄임(`…`)·괄호 정리로 정본 제목과 달라지는 경우가 흔하다.
ANCHOR_PARTIAL_CHARS = 20

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_READ_CHUNK = 16384

# 제로폭·BOM 은 공백 정규화로 사라지지 않는다 — 여기서 지운다.
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]*charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)


# ─── HTML → 보이는 텍스트 (정규식이 아니라 파서) ─────────────────────────
# 정규식 해체는 `"<" * 32000` 에서 약 0.94초로 제곱 증가했고(Codex 실측), 닫히지
# 않은 <script> 내용과 <div hidden> 을 근거로 남겼다. 파서는 선형이고, 숨김·메뉴
# 판정을 요소 단위로 할 수 있다.
_VOID_TAGS = frozenset({
    "area", "base", "basefont", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
# 이 요소의 **하위 전체**가 근거에서 빠진다.
_DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "nav", "header", "footer",
    "aside", "svg", "iframe", "select", "option", "button",
})
# 이 요소는 앞뒤에 공백을 넣지 않는다 — `9월 <b>22</b>일` 이 `9월 22 일` 로
# 쪼개지면 근거 토큰 대조가 깨진다.
_INLINE_TAGS = frozenset({
    "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "font",
    "i", "kbd", "label", "mark", "q", "s", "samp", "small", "span", "strike",
    "strong", "sub", "sup", "time", "u", "var",
})
# 줄바꿈 구실을 하는 빈 요소 — 공백 하나로 남긴다.
_BREAK_TAGS = frozenset({"br", "hr"})
# 사이트별 본문 컨테이너 힌트 (id/class).
_CONTENT_HINT_RE = re.compile(r"content|board|view|article|bbs|detail", re.IGNORECASE)

_DOCUMENT_TAG = "[document]"


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: List = []


class _DomBuilder(HTMLParser):
    """관대한 트리 빌더. 닫히지 않은 태그·짝 없는 종료 태그를 그냥 흘려보낸다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node(_DOCUMENT_TAG)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _BREAK_TAGS:
            self._stack[-1].children.append(" ")
            return
        if tag in _VOID_TAGS:
            return
        node = _Node(tag, {key.lower(): (value or "") for key, value in attrs})
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in _BREAK_TAGS:
            self._stack[-1].children.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return
        # 짝 없는 종료 태그는 무시한다 (fail-open 파싱)

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def _is_hidden(node: _Node) -> bool:
    attrs = node.attrs
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    style = (attrs.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _text_of(node: _Node) -> str:
    """요소의 보이는 텍스트 (숨김·드롭 요소는 하위까지 통째로 제외). 선형 시간."""
    parts: List[str] = []
    stack: List = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            parts.append(current)
            continue
        if current.tag in _DROP_TAGS or _is_hidden(current):
            continue
        items = list(current.children)
        if current.tag not in _INLINE_TAGS:
            items = [" "] + items + [" "]
        for child in reversed(items):
            stack.append(child)
    return "".join(parts)


def _clean(text: str) -> str:
    return normalize_space(_INVISIBLE_RE.sub("", text or ""))


def _walk(root: _Node):
    """드롭·숨김 하위를 건너뛰는 문서 순서 순회."""
    queue = deque([root])
    while queue:
        node = queue.popleft()
        if isinstance(node, str):
            continue
        if node.tag in _DROP_TAGS or _is_hidden(node):
            continue
        yield node
        for child in node.children:
            queue.append(child)


def content_text(root: _Node) -> str:
    """본문 컨테이너를 골라 그 텍스트만 돌려준다 (없으면 body 전체).

    메뉴가 2,000자를 넘기는 사이트에서는 문서 앞에서부터 자른 창이 기사에
    닿지 못한다 — 그래서 자르기 **전에** 본문을 고른다.
    우선순위: `<article>` → `<main>`/`role=main` → id·class 가 본문 힌트에
    걸리는 컨테이너 중 **텍스트가 가장 긴 것** → body 전체.
    """
    articles: List[_Node] = []
    mains: List[_Node] = []
    containers: List[_Node] = []
    body: Optional[_Node] = None
    for node in _walk(root):
        if node.tag == "article":
            articles.append(node)
        elif node.tag == "main" or (node.attrs.get("role") or "").strip().lower() == "main":
            mains.append(node)
        elif _CONTENT_HINT_RE.search(
            (node.attrs.get("id") or "") + " " + (node.attrs.get("class") or "")
        ):
            containers.append(node)
        if node.tag == "body" and body is None:
            body = node

    for node in articles + mains:
        text = _clean(_text_of(node))
        if text:
            return text

    best = ""
    for node in containers:
        text = _clean(_text_of(node))
        if len(text) > len(best):
            best = text
    if best:
        return best

    return _clean(_text_of(body if body is not None else root))


def normalize_space(text: str) -> str:
    """공백 정규화 — 앵커·근거 대조는 양쪽이 **같은 규칙**을 지나야 맞는다."""
    return " ".join((text or "").split())


def anchored_window(
    text: str, anchor: str = "", limit: int = DETAIL_TEXT_CHARS
) -> str:
    """본문에서 `anchor`(제목)가 **마지막으로** 나오는 자리부터 limit 자.

    목록·빵부스러기·`<title>` 에도 제목이 박혀 있으므로 **마지막** 출현을
    고른다 — 본문 제목이 대개 그 뒤에 본문을 달고 온다. 제목 전체를 못 찾으면
    앞 `ANCHOR_PARTIAL_CHARS` 자로 한 번 더 찾고, 그래도 없으면 앞에서부터 자른다.
    """
    if not text:
        return ""
    needle = normalize_space(anchor)
    if needle:
        index = text.rfind(needle)
        if index < 0:
            partial = needle[:ANCHOR_PARTIAL_CHARS]
            index = text.rfind(partial) if partial else -1
        if index >= 0:
            return text[index:index + limit]
    return text[:limit]


def visible_text(
    html_text: str, limit: int = DETAIL_TEXT_CHARS, anchor: str = ""
) -> str:
    """HTML → 본문 컨테이너의 보이는 텍스트 중 앵커 기준 limit 자."""
    if not html_text:
        return ""
    builder = _DomBuilder()
    try:
        builder.feed(html_text)
        builder.close()
    except Exception:  # noqa: BLE001 — 깨진 HTML 은 근거 없음으로 본다
        return ""
    return anchored_window(content_text(builder.root), anchor, limit)


# ─── 디코딩 (BOM > meta charset > 헤더 > utf-8) ──────────────────────────
def _charset_of(content_type: str) -> Optional[str]:
    if "charset=" not in content_type:
        return None
    return content_type.split("charset=")[-1].split(";")[0].strip().strip("\"'") or None


def _meta_charset(raw: bytes) -> Optional[str]:
    matched = _META_CHARSET_RE.search(raw[:4096])
    if not matched:
        return None
    try:
        return matched.group(1).decode("ascii")
    except UnicodeDecodeError:
        return None


def _decode(raw: bytes, content_type: str) -> str:
    """BOM → meta charset → 헤더 charset → utf-8 순. 마지막은 errors=replace.

    헤더가 `iso-8859-1` 이라고 우겨도 문서가 meta 로 utf-8 을 선언하면 meta 가
    이긴다 — 헤더만 믿어 깨진 텍스트를 근거로 쓰던 분기다(Codex LOW).
    """
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if raw.startswith(bom):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                break

    candidates = [_meta_charset(raw), _charset_of(content_type), "utf-8", "cp949"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    fallback = next((c for c in candidates if c), "utf-8")
    try:
        return raw.decode(fallback, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ─── 수집 예산 ───────────────────────────────────────────────────────────
class FetchBudget:
    """잡 전체의 수집 시간 예산. 소진 뒤의 항목은 `detail_text` 를 받지 못한다.

    순차 8–50건이면 항목 타임아웃만으로 80–500초가 나온다(Codex 실측) — nightly
    잡 하나가 그만큼 물고 있으면 안 된다. 소진은 실패가 아니라 **빈 근거**다.
    """

    def __init__(self, seconds: float = JOB_BUDGET_SECONDS, clock=time.monotonic):
        self._clock = clock
        self.seconds = seconds
        self.deadline = clock() + seconds

    def remaining(self) -> float:
        return self.deadline - self._clock()

    def exhausted(self) -> bool:
        return self.remaining() <= 0


def _is_http_url(url) -> bool:
    return bool(url) and str(url).lower().startswith(("http://", "https://"))


def _read_capped(response, max_bytes: int, deadline: float, clock) -> Optional[bytes]:
    """본문을 max_bytes 까지, deadline 까지만 읽는다. 시간 초과면 None."""
    chunks: List[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=_READ_CHUNK):
        if clock() >= deadline:
            return None
        if not chunk:
            continue
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes:
            break
    return b"".join(chunks)[:max_bytes]


def _fetch(session, url, anchor, deadline, max_bytes, limit, clock) -> str:
    current = str(url)
    for _hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - clock()
        if remaining <= 0:
            return ""
        response = session.get(
            current,
            timeout=remaining,
            allow_redirects=False,
            headers=PROBE_HEADERS,
            stream=True,
        )
        try:
            status = response.status_code
            if status in _REDIRECT_STATUS:
                # 중간 응답 본문은 **읽지 않고** 닫는다(0바이트 — 크기 상한 안).
                target = urljoin(
                    current, (response.headers.get("Location") or "").strip()
                )
                if not _is_http_url(target):
                    return ""
                current = target
                continue
            if status >= 400:
                return ""
            content_type = (response.headers.get("Content-Type") or "").lower()
            # Content-Type 누락·text/plain 은 거절한다 — "비 HTML 거절"과 말을 맞춘다.
            if "html" not in content_type:
                return ""
            raw = _read_capped(response, max_bytes, deadline, clock)
            if not raw:
                return ""
            return visible_text(_decode(raw, content_type), limit, anchor)
        finally:
            response.close()
    return ""


def fetch_detail_text(
    url,
    anchor: str = "",
    timeout: float = DETAIL_TIMEOUT,
    max_bytes: int = MAX_DETAIL_BYTES,
    limit: int = DETAIL_TEXT_CHARS,
    budget: Optional[FetchBudget] = None,
    clock=time.monotonic,
) -> str:
    """URL 의 본문 중 `anchor`(항목 제목) 기준 한 창. **실패는 빈 문자열**.

    실패로 보는 것: URL 아님·요청 예외·4xx/5xx·비 HTML(Content-Type 누락 포함)·
    빈 본문·항목 벽시계 초과·리다이렉트 4홉 초과·잡 예산 소진.
    """
    if not _is_http_url(url):
        return ""
    if budget is not None and budget.exhausted():
        return ""
    deadline = clock() + timeout
    if budget is not None:
        deadline = min(deadline, budget.deadline)
    session = requests.Session()
    try:
        return _fetch(session, url, anchor, deadline, max_bytes, limit, clock)
    except Exception:  # noqa: BLE001 — 항목 단위 fail-open
        return ""
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
