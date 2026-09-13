"""외부 URL 을 읽는 공용 헬퍼 — UA·헤더·본문 추출의 정본.

checker 의 생존 확인(HEAD/GET 프로브)과 glm_enrich 의 상세 텍스트 수집이 **같은
UA·헤더**를 쓴다. 공공기관 사이트 다수가 기본 python-requests UA 를 차단하거나
HEAD 를 지원하지 않으므로, 둘이 갈라지면 "게이트는 살아 있다고 보는데 보강은
빈손" 이라는 조용한 불일치가 생긴다.

여기서 가져오는 본문은 **외부 입력**이다 — 상세 텍스트는 그 자체로 발송본에
들어가지 않는다. GLM 이 그것을 읽고 낸 문장은 glm_enrich 의 결정론 게이트
(구절 하나를 그대로 옮긴 «인용» + 경계 대조)와 사람의 미리보기 승인을 지난
뒤에만 md 로 들어간다 (alert/digest/checker.py 의 THREAT_MODEL 참고).

**requests 를 쓰지 않는 이유** (Codex r3 실측): `allow_redirects=False,
stream=True` 여도 requests 는 `Response.next` 를 준비하며 3xx 의 본문을 **get 이
반환하기 전에 전부 읽는다**(1MiB 리다이렉트 본문이 크기 상한 밖에서 소비됐다).
그래서 urllib3 을 직접 쓰고 리다이렉트도 직접 따라간다. 또 첫 청크가 오기
전에는 어떤 청크 루프도 데드라인을 검사할 수 없으므로, 항목 수집 전체를
워커 스레드에 넣고 **벽시계로 잘라낸다**.
"""

import codecs
import re
import time
from collections import deque
import threading
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import urllib3

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
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 5
MAX_DETAIL_BYTES = 512 * 1024
MAX_REDIRECTS = 3
DETAIL_TEXT_CHARS = 2000
# 앵커(제목)를 통째로 못 찾을 때 다시 시도하는 앞부분 길이.
ANCHOR_PARTIAL_CHARS = 20

# DOM 상한 (Codex r3 MEDIUM: 512KB 입력 제한은 DOM 메모리 제한이 아니다).
MAX_DOM_NODES = 100_000
MAX_DOM_DEPTH = 256
_END_TAG_SEARCH_FRAMES = 32
_FEED_CHUNK = 65536

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_READ_CHUNK = 16384

_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]*charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)
# 정규식 `/\*.*?\*/` 은 미종결 주석(`"/*x"*8000`)에서 역추적으로 제곱 증가했다
# (Codex r3 실측 0.9초). find 루프는 선형이다.
_CSS_COMMENT_OPEN = "/*"
_CSS_COMMENT_CLOSE = "*/"
# style 속성은 아무리 길어도 이만큼만 본다 — 숨김 판정에 그 이상은 필요 없다.
MAX_STYLE_CHARS = 4096


# ─── HTML → 보이는 텍스트 (정규식이 아니라 파서) ─────────────────────────
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
# 쪼개지면 근거 경계 대조가 깨진다.
_INLINE_TAGS = frozenset({
    "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "font",
    "i", "kbd", "label", "mark", "q", "s", "samp", "small", "span", "strike",
    "strong", "sub", "sup", "time", "u", "var",
})
_BREAK_TAGS = frozenset({"br", "hr"})
# 이 요소들은 **평탄화하면 뜻이 바뀐다**: `10<sup>4</sup>㎡` → `104㎡`(지수 소실),
# `<del>취소</del>` → 취소선이 사라져 삭제된 문구가 근거가 된다. 내용을 지우지
# 않고 **표시**만 해 두고, 후보 추출이 표시된 단위를 통째로 버린다 (r9 HIGH).
MARKED_SENTINEL = "\ufffc"
_MARKED_TAGS = frozenset({"sup", "sub", "del", "s", "strike"})
# 열린 `<p>` 를 암시적으로 닫는 블록 요소들 (HTML 파싱 규칙 "in body").
_P_CLOSING_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "details", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol",
    "p", "pre", "section", "table", "ul",
})
_CONTENT_HINT_RE = re.compile(r"content|board|view|article|bbs|detail", re.IGNORECASE)

_DOCUMENT_TAG = "[document]"


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: List = []


class _DomBuilder(HTMLParser):
    """관대한 트리 빌더 — 노드 수·깊이·종료 태그 탐색을 전부 상한으로 묶는다."""

    def __init__(self, max_nodes: int = MAX_DOM_NODES, max_depth: int = MAX_DOM_DEPTH):
        super().__init__(convert_charrefs=True)
        self.root = _Node(_DOCUMENT_TAG)
        self._stack = [self.root]
        self._nodes = 0
        self._max_nodes = max_nodes
        self._max_depth = max_depth
        self._form_depth = 0
        # 상한을 넘긴 문서는 **수집 실패**다. 예전에는 한도에서 노드를 스택에
        # 넣지 않아, 그 아래 `<div hidden>` 의 텍스트가 보이는 부모로 새어
        # 나갔다(Codex r3: 255단 중첩에서 숨김 문구가 근거가 됐다).
        self.overflowed = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _BREAK_TAGS:
            self._stack[-1].children.append(" ")
            return
        if tag in _VOID_TAGS:
            return
        # 중첩 <form> 시작 태그는 무시한다 (브라우저와 같게) — 무시하지 않으면
        # `<form hidden><form></form>본문` 에서 </form> 이 안쪽만 닫아
        # 공개 본문이 숨김 폼 안에 남는다.
        if tag == "form" and self._form_depth:
            return
        if self._nodes >= self._max_nodes:
            self.overflowed = True
            return
        # 열린 <p> 는 블록 요소가 시작되면 암시적으로 닫힌다.
        if tag in _P_CLOSING_TAGS and self._stack[-1].tag == "p":
            self._stack.pop()
        node = _Node(tag, {key.lower(): (value or "") for key, value in attrs})
        self._nodes += 1
        self._stack[-1].children.append(node)
        if len(self._stack) < self._max_depth:
            self._stack.append(node)
            if tag == "form":
                self._form_depth += 1
        else:
            # 깊이 상한 초과 — 숨김 상속을 보장할 수 없으므로 문서를 버린다.
            self.overflowed = True

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in _BREAK_TAGS:
            self._stack[-1].children.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        # 스택 전체를 뒤지면 종료 태그마다 O(depth) 다 — 위쪽 몇 프레임만 본다.
        lowest = max(1, len(self._stack) - _END_TAG_SEARCH_FRAMES)
        for index in range(len(self._stack) - 1, lowest - 1, -1):
            if self._stack[index].tag == tag:
                for node in self._stack[index:]:
                    if node.tag == "form" and self._form_depth:
                        self._form_depth -= 1
                del self._stack[index:]
                return
        # 짝 없는 종료 태그는 무시한다 (fail-open 파싱)

    def handle_data(self, data):
        self._stack[-1].children.append(data)

    def unknown_decl(self, data):
        # XHTML 의 `<![CDATA[…]]>` 본문은 텍스트다 — 버리면 근거가 사라진다.
        if data.startswith("CDATA["):
            self._stack[-1].children.append(data[6:])


def strip_css_comments(style: str) -> str:
    """CSS 주석 제거 — **선형**. 미종결 주석은 그 뒤 전체가 주석이다."""
    parts: List[str] = []
    index = 0
    length = len(style)
    while index < length:
        start = style.find(_CSS_COMMENT_OPEN, index)
        if start < 0:
            parts.append(style[index:])
            break
        parts.append(style[index:start])
        end = style.find(_CSS_COMMENT_CLOSE, start + 2)
        if end < 0:
            break
        index = end + 2
    return "".join(parts)


def _is_hidden(node: _Node) -> bool:
    attrs = node.attrs
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    # `display:\nnone` · `display:/**/none` 도 숨김이다 — 주석을 걷고 공백을
    # 전부 지운 뒤 본다. 아주 긴 style 은 앞부분만 본다(상한).
    style = strip_css_comments((attrs.get("style") or "")[:MAX_STYLE_CHARS])
    style = "".join(style.split()).lower()
    return "display:none" in style or "visibility:hidden" in style


def _text_of(node: _Node) -> str:
    """요소의 보이는 텍스트 (숨김·드롭 요소는 하위까지 제외). 선형 시간."""
    parts: List[str] = []
    stack: List = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            parts.append(current)
            continue
        if current.tag in _DROP_TAGS or _is_hidden(current):
            continue
        if current.tag in _MARKED_TAGS:
            # r10: 내용을 **남기지 않는다**. `10<sup>4</sup>㎡` → `10￼㎡`,
            # 세 문장짜리 `<del>` → 센티넬 하나. 표시만 남기고 지우면 삭제된
            # 문구가 어떤 경로로도 근거가 될 수 없다.
            parts.append(MARKED_SENTINEL)
            continue
        items = list(current.children)
        if current.tag not in _INLINE_TAGS:
            items = [" "] + items + [" "]
        for child in reversed(items):
            stack.append(child)
    return "".join(parts)


def _measure(root: _Node) -> Dict[int, int]:
    """노드별 보이는 글자 수를 **한 번의 상향 순회**로 센다 (id(node) → 길이).

    예전에는 후보마다 하위 트리 텍스트를 다시 만들어 이어 붙였다 — 20,000단
    중첩에서 O(N²)였다(Codex r3 실측). 순위 판정에는 길이만 있으면 되고,
    텍스트는 **고른 노드 하나**에 대해서만 만든다.
    """
    lengths: Dict[int, int] = {}
    stack: List = [(root, False)]
    while stack:
        node, done = stack.pop()
        if isinstance(node, str):
            continue
        if node.tag in _DROP_TAGS or _is_hidden(node):
            lengths[id(node)] = 0
            continue
        if node.tag in _MARKED_TAGS:
            lengths[id(node)] = 1          # 센티넬 한 글자 (r10)
            continue
        if done:
            total = 0
            for child in node.children:
                if isinstance(child, str):
                    total += sum(1 for char in child if not char.isspace())
                else:
                    total += lengths.get(id(child), 0)
            lengths[id(node)] = total
            continue
        stack.append((node, True))
        for child in node.children:
            if not isinstance(child, str):
                stack.append((child, False))
    return lengths


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

    우선순위: `<article>` → `<main>`/`role=main` → id·class 가 본문 힌트에
    걸리는 컨테이너 중 **글자 수가 가장 많은 것** → body 전체.
    """
    lengths = _measure(root)
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
        if lengths.get(id(node), 0) > 0:
            return _clean(_text_of(node))

    best = None
    best_length = 0
    for node in containers:
        length = lengths.get(id(node), 0)
        if length > best_length:
            best, best_length = node, length
    if best is not None:
        return _clean(_text_of(best))

    return _clean(_text_of(body if body is not None else root))


class DetailText(str):
    """수집한 상세 텍스트 + **창이 잘렸는지** 여부.

    `str` 그대로 쓰이지만 `.truncated` 를 달고 다닌다. 잘린 창의 **마지막
    단위**는 문장이 끝나기 전에 끊긴 것일 수 있고, 그 자리에 부정이 있으면
    뜻이 뒤집힌다(`…신청 가능|하지 않습니다.`) — 호출자가 그 단위를 버린다.
    """

    truncated = False

    def __new__(cls, value: str = "", truncated: bool = False):
        text = super().__new__(cls, value)
        text.truncated = bool(truncated)
        return text


def normalize_space(text: str) -> str:
    """공백 정규화 — 앵커·근거 대조는 양쪽이 **같은 규칙**을 지나야 맞는다."""
    return " ".join((text or "").split())


def anchored_window(
    text: str, anchor: str = "", limit: int = DETAIL_TEXT_CHARS
) -> DetailText:
    """본문에서 `anchor`(제목)가 **마지막으로** 나오는 자리부터 limit 자.

    `.truncated` 는 **창이 본문 끝에 닿지 못했는지**다 (r9 HIGH).
    """
    if not text:
        return DetailText("")
    start = 0
    needle = normalize_space(anchor)
    if needle:
        index = text.rfind(needle)
        if index < 0:
            partial = needle[:ANCHOR_PARTIAL_CHARS]
            index = text.rfind(partial) if partial else -1
        if index >= 0:
            start = index
    window = text[start:start + limit]
    return DetailText(window, truncated=start + limit < len(text))


def visible_text(
    html_text: str,
    limit: int = DETAIL_TEXT_CHARS,
    anchor: str = "",
    deadline: Optional[float] = None,
    clock=time.monotonic,
) -> DetailText:
    """HTML → 본문 컨테이너의 보이는 텍스트 중 앵커 기준 limit 자.

    `deadline` 이 주어지면 **파싱 도중에도** 벽시계를 본다 — 거대한 입력이
    파서 안에서 시간을 다 쓰는 경로를 막는다(넘기면 빈 문자열).
    """
    if not html_text:
        return DetailText("")
    builder = _DomBuilder()
    try:
        for start in range(0, len(html_text), _FEED_CHUNK):
            if deadline is not None and clock() >= deadline:
                return DetailText("")
            builder.feed(html_text[start:start + _FEED_CHUNK])
        builder.close()
    except Exception:  # noqa: BLE001 — 깨진 HTML 은 근거 없음으로 본다
        return DetailText("")
    if builder.overflowed:
        return DetailText("")
    if deadline is not None and clock() >= deadline:
        return DetailText("")
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
    """BOM → meta charset → 헤더 charset → utf-8 순. 마지막은 errors=replace."""
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
    """잡 전체의 수집 시간 예산. 소진 뒤의 항목은 `detail_text` 를 받지 못한다."""

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


_POOL: Optional[urllib3.PoolManager] = None


def _pool() -> urllib3.PoolManager:
    global _POOL
    if _POOL is None:
        _POOL = urllib3.PoolManager(retries=False)
    return _POOL


def _open(url: str):
    """GET 한 번 — 리다이렉트를 따라가지 않고 본문을 미리 읽지 않는다.

    테스트는 이 이름을 갈아끼운다(실호출 금지).
    """
    return _pool().request(
        "GET",
        url,
        headers=PROBE_HEADERS,
        redirect=False,
        preload_content=False,
        timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
    )


def _header(response, name: str) -> str:
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.title())
    except AttributeError:
        value = None
    return value or ""


def _read_capped(
    response, max_bytes: int, deadline: float, clock
) -> Tuple[Optional[bytes], bool]:
    """본문을 max_bytes 까지, deadline 까지만 읽는다.

    Returns:
        (바이트, 상한에서 끊겼는가). 시간 초과면 (None, False).

    r10: **바이트 상한도 절단이다.** 512KiB 경계가 `…신청 가능|하지
    않습니다.` 한가운데 떨어지면 글자 수 창을 넘지 않았는데도 마지막 단위가
    불완전하다 — 그 사실을 `DetailText.truncated` 까지 전달해야 한다.
    """
    chunks: List[bytes] = []
    size = 0
    capped = False
    for chunk in response.stream(_READ_CHUNK, decode_content=True):
        if clock() >= deadline:
            return None, False
        if not chunk:
            continue
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes:
            capped = True
            break  # 상한에서 멈춘다 — 남은 본문을 drain 하지 않는다
    raw = b"".join(chunks)
    return raw[:max_bytes], capped or len(raw) > max_bytes


def _fetch(url, anchor, deadline, max_bytes, limit, clock) -> DetailText:
    current = str(url)
    for _hop in range(MAX_REDIRECTS + 1):
        if clock() >= deadline:
            return DetailText("")
        response = _open(current)
        try:
            status = getattr(response, "status", 0)
            if status in _REDIRECT_STATUS:
                location = _header(response, "location").strip()
                # Location 없는 3xx 는 실패다 — 같은 URL 을 다시 부르면 홉을
                # 낭비하며 같은 응답을 반복한다(Codex r3).
                if not location:
                    return DetailText("")
                target = urljoin(current, location)
                if not _is_http_url(target):
                    return DetailText("")
                current = target
                continue
            if not 200 <= status < 300:
                return DetailText("")
            content_type = _header(response, "content-type").lower()
            if "html" not in content_type:
                return DetailText("")
            raw, byte_capped = _read_capped(response, max_bytes, deadline, clock)
            if not raw:
                return DetailText("")
            text = visible_text(
                _decode(raw, content_type), limit, anchor, deadline, clock
            )
            # 절단 원인은 둘 — 글자 수 창(anchored_window)과 바이트 상한.
            # 어느 쪽이든 마지막 단위는 믿을 수 없다.
            return DetailText(
                text, truncated=bool(text.truncated) or byte_capped
            )
        finally:
            # **close 가 먼저다**: 미소비 응답에 release_conn 을 먼저 부르면
            # 연결이 풀로 돌아가고 `_connection=None` 이 되어 뒤따르는 close 가
            # 그 연결을 닫지 못한다(Codex r3 실측).
            for closer in ("close", "release_conn"):
                method = getattr(response, closer, None)
                if callable(method):
                    try:
                        method()
                    except Exception:  # noqa: BLE001
                        pass
    return DetailText("")


def fetch_detail_text(
    url,
    anchor: str = "",
    timeout: float = DETAIL_TIMEOUT,
    max_bytes: int = MAX_DETAIL_BYTES,
    limit: int = DETAIL_TEXT_CHARS,
    budget: Optional[FetchBudget] = None,
    clock=time.monotonic,
) -> DetailText:
    """URL 의 본문 중 `anchor`(항목 제목) 기준 한 창. **실패는 빈 문자열**.

    수집 전체를 **데몬 스레드**에 넣고 벽시계로 잘라낸다 — 첫 청크가 오기
    전에는 어떤 청크 루프도 데드라인을 볼 수 없기 때문이다(9초마다 1바이트를
    보내는 서버가 read timeout 을 영원히 리셋한다). 시간이 끝나면 스레드는
    **버린다**: 데몬이라 인터프리터 종료를 막지 않는다(연결이 끊길 때까지
    남아 돌 수는 있다).
    """
    if not _is_http_url(url):
        return DetailText("")
    if budget is not None and budget.exhausted():
        return DetailText("")
    deadline = clock() + timeout
    if budget is not None:
        deadline = min(deadline, budget.deadline)
    holder: Dict[str, str] = {}

    def worker() -> None:
        try:
            holder["text"] = _fetch(url, anchor, deadline, max_bytes, limit, clock)
        except Exception:  # noqa: BLE001 — 항목 단위 fail-open
            holder["text"] = ""

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=max(0.0, deadline - clock()))
    if thread.is_alive():
        # 시간이 끝났다. 스레드는 **버린다** — 데몬이므로 인터프리터 종료를
        # 막지 않는다(ThreadPoolExecutor 는 shutdown(wait=False) 여도 atexit
        # 에서 join 해 프로세스가 워커를 기다렸다 — Codex r3 실측).
        return DetailText("")
    return holder.get("text") or DetailText("")
