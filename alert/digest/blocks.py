"""발송본(형식 v2)의 블록 구조 — 항목 판정과 URL 추출의 **단일 정본** (사이클 6).

이 모듈이 생긴 이유: 항목 블록 파서가 네 곳(preview·prune·checker·카톡 렌더)에
따로 있었고, 서로 다른 개수를 세는 순간 게이트가 조용히 뚫렸다. Codex 최종 게이트
신규 #6("preview=0, prune=1, pass=True")·#4("live 블록 동반 삭제")·#7("URL 문법
불일치")가 모두 그 불일치의 증상이다. 그래서 **파서는 하나다** — 이 모듈을 우회해
항목을 세거나 URL을 뽑는 코드는 그 자체가 결함이다.

역할 분담 (병합 2회차):
- **어떤 섹션이 항목 섹션인가** = `alert/digest/sections.py`. check.json 의
  `item_sections` 가 정본이고, 판정은 언제나 **정확한 헤딩 문자열 완전 일치**다
  (계약 W10 사이클3 #7 — 부분 일치로 하면 `## 협의회에서 — 지원사업 의견` 이
  공고 섹션으로 오분류돼 사람이 쓴 해설이 항목으로 세어지고 삭제된다).
- **그 섹션 안에서 무엇이 항목인가** = 이 모듈.

항목 블록의 정의 (세 조건을 **모두** 만족해야 항목이다 — 사이클 7):

1. **직전 줄이 구조 마커 `<!-- item id=<announcement id> -->`** 다.
   composer 만 이 마커를 쓴다(`composer.ITEM_MARKER_RE` 가 정본). 발송 HTML·카톡
   렌더는 주석 줄을 버리므로 사람 눈에는 보이지 않는다.
2. 항목 줄이 composer 가 만드는 형식이다 (`composer.ITEM_LINE_RE`, 보조 조건)
   - 신청하세요: `[라벨] 제목 — 기관 · 대상: … · 마감 …` (라벨 = D-n / 새 소식 / 상시 / 마감 미정)
   - 알아두세요: `제목 — 기관 · 대상: … · 의견 …까지`
3. 그 다음 줄이 `  [원문](URL)` 이다 (보조 조건).

**마커를 요구하는 이유** (Codex 3차 HIGH #2): 정규식만으로는 양방향으로 틀린다.
`자료를 참고해 주세요 — 자세한 내용은 원문에 있습니다.` + 원문 줄은 공고가 아닌데
항목으로 세어져 "공고 0건인데 pass" 가 났고, 반대로 `*산림 제도 개정 — 산림청` 같은
위조 항목은 항목으로 세어지지 않으면서 발송 HTML 에는 링크가 실려 상한을 우회했다.
구조 마커는 **누가 이 줄을 만들었는지**를 묻기 때문에 양쪽을 동시에 닫는다.

항목 섹션 안에서 마커 없는 비어 있지 않은 줄(산문·떠돌이 링크·항목 흉내)은
`prose_lines_in_item_sections()` 가 잡아내고 checker 가 pass=false 로 떨어뜨린다.
구형 형식 v1.2(`### 제목` + `**원문:** [x](url)`)는 지원하지 않는다.

블록 경계 (사이클 6 #2): 항목 블록은 **제목 줄 + 원문 줄(+ 뒤따르는 빈 줄)** 뿐이다.
다음 항목 줄·헤딩·주석은 각자 새 블록을 연다. 그래서 죽은 항목을 지워도 이어지는
산문이나 살아 있는 항목, 보류 주석이 함께 사라지지 않는다.
"""

import re
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from alert.digest import sections as sections_mod

# 링크 후보로 인정하는 스킴 (사이클 6 #4). `~9.30`·`산림사업자` 같은 괄호는 URL이 아니다.
# 사이클 11 #6: **호스트가 있어야** URL 이다 — `"http://"` 같은 스킴 설명은 주소가
# 아니므로 생존 검사 대상에서 뺀다(따옴표·괄호는 호스트 문자가 아니다).
_HOST_CHARS = r"[^\s\"'<>()\[\]]"
_SCHEME_RE = re.compile(
    r"^(?:https?://" + _HOST_CHARS + r"|www\." + _HOST_CHARS + r")",
    re.IGNORECASE,
)
# 맨몸 URL 의 시작 (사이클 10 #3). 끝은 _bare_url_at 이 균형 괄호로 찾는다.
_BARE_URL_START_RE = re.compile(r"https?://", re.IGNORECASE)
# URL 을 끊는 문자 (공백·따옴표·꺾쇠·대괄호)
_URL_STOP_CHARS = set(" \t\n\r\"'<>[]")
# 괄호 **밖**에 있을 때만 URL 끝에서 떼어내는 구두점
_URL_TRAILING_PUNCT = ".,;:!?·"

# 항목의 원문 링크 줄은 링크 하나로만 이루어진다.
ORIGIN_LINK_TEXT = "원문"

# composer 를 못 읽을 때의 항목 마커 폴백 (정본은 composer.ITEM_MARKER_RE)
_FALLBACK_ITEM_MARKER_RE = re.compile(r"^<!--\s*item\s+id=(\S+)\s*-->$")

# 항목 줄이 될 수 없는 줄머리: 헤딩(#), 주석(<!--), 강조·빈 표시(*), 산문 글머리
_NOT_ITEM_PREFIXES = ("#", "<!--", "*", "·", "-", ">", "|")

EMPTY_SECTION_LINE = "*(항목 없음)*"

# 항목 섹션 안에서 마커 없이도 허용되는 줄 (composer 가 쓰는 빈 섹션 표시)
_ALLOWED_BARE_LINES = (EMPTY_SECTION_LINE,)

class Link(NamedTuple):
    """본문에서 찾은 마크다운 링크 하나."""

    start: int
    end: int
    text: str
    url: str


def _composer():
    """composer 모듈 (없거나 깨지면 None). 지연 로딩 — import 순환을 피한다."""
    return sections_mod._composer()


# ─── 섹션 (정본은 sections.py) ───────────────────────────────────────────
def item_section_headings(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> Tuple[str, ...]:
    """판정에 쓸 항목 섹션 헤딩 목록 (정확 일치용).

    호출자가 check.json 의 목록을 주면 그것이 정본이다. 없으면 sections.resolve 가
    composer 상수 → 선언 목록 순으로 찾고, 본문에 실제로 등장한 헤딩만 남긴다
    (미지의 헤딩은 산문 취급 = fail-closed).
    """
    if item_sections is not None:
        return tuple(item_sections)
    resolved, _ = sections_mod.resolve(None, markdown_text)
    return resolved


# ─── 링크·URL 추출 ───────────────────────────────────────────────────────
def has_url_scheme(url: str) -> bool:
    """URL 후보인가 (스킴 필수 — 사이클 6 #4)."""
    return bool(_SCHEME_RE.match((url or "").strip()))


def find_links(text: str) -> List[Link]:
    """`[텍스트](URL)` 을 **균형 괄호**로 훑는다 (문서 순서).

    정규식 대신 스캐너를 쓰는 이유 (사이클 6 #4): `…/report(2026)` 처럼 URL 안에
    괄호가 있으면 정규식은 `…/report(2026` 로 자른다. 잘린 주소를 따로 검사하면
    "완전한 주소는 살아 있는데 잘린 주소가 죽어서" 정상 링크가 삭제된다.

    스킴이 없는 괄호(`[모집](~9.30)`)는 링크가 아니다 — 건너뛴다.
    """
    links: List[Link] = []
    index = 0
    length = len(text)
    while index < length:
        open_bracket = text.find("[", index)
        if open_bracket < 0:
            break
        close_bracket = text.find("]", open_bracket + 1)
        if close_bracket < 0:
            break
        if close_bracket + 1 >= length or text[close_bracket + 1] != "(":
            index = open_bracket + 1
            continue
        url, after = _balanced(text, close_bracket + 2)
        if url is None or not has_url_scheme(url):
            # 링크가 아니다. 여는 대괄호 뒤부터 다시 찾는다.
            index = open_bracket + 1
            continue
        links.append(
            Link(open_bracket, after, text[open_bracket + 1:close_bracket],
                 url.strip())
        )
        index = after
    return links


def _balanced(text: str, start: int) -> Tuple[Optional[str], int]:
    """`(` 다음 위치에서 균형 잡힌 닫는 괄호까지. (내용, 닫는 괄호 다음 위치)."""
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char in "\n\r":
            return None, start
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return None, start


def origin_url(line: str) -> Optional[str]:
    """항목의 원문 링크 줄(`  [원문](URL)`)에서 URL. 원문 줄이 아니면 None."""
    stripped = line.strip()
    links = find_links(stripped)
    if len(links) != 1:
        return None
    link = links[0]
    if link.text.strip() != ORIGIN_LINK_TEXT:
        return None
    if link.start != 0 or link.end != len(stripped):
        # 줄에 다른 내용이 붙어 있으면 항목 링크 줄이 아니다
        return None
    return link.url


def body_link_urls(markdown_text: str) -> List[str]:
    """본문(HTML 주석 줄 제외)의 **마크다운 링크** URL (문서 순서)."""
    urls: List[str] = []
    for line in (markdown_text or "").split("\n"):
        if _is_comment(line):
            continue
        for link in find_links(line):
            urls.append(link.url)
    return urls


def bare_urls(markdown_text: str) -> List[str]:
    """마크다운 링크 문법 **없이** 본문에 적힌 URL (사이클 10 #3).

    해설·회원사 소식에 사람이 주소를 그냥 붙여넣는 일이 흔하다. 렌더러는 그것을
    본문 그대로 내보내므로 죽은 링크 검사에도 들어와야 한다 — 예전에는 항목의
    "원문" 링크만 검사해서, 죽은 해설 URL 이 그대로 카톡에 실렸다.

    끝의 구두점은 렌더러가 URL 의 일부로 내보내므로 함께 잡는다(사람이 붙여넣은
    주소가 무엇인지는 렌더 결과가 정한다).
    """
    urls: List[str] = []
    for line in (markdown_text or "").split("\n"):
        if _is_comment(line):
            continue
        # 링크의 **URL 부분만** 가린다 — 표시문은 남겨서 그 안의 주소도 찾는다
        # (사이클 11 #3: `[https://dead/x](https://live/y)` 의 dead 는 사람 눈에
        # 보이는 주소이고 카톡·메일에 그대로 실린다. 목적지만 검사하면 놓친다).
        masked = _mask_link_targets(line)
        index = 0
        while True:
            match = _BARE_URL_START_RE.search(masked, index)
            if not match:
                break
            url = _bare_url_at(masked, match.start())
            index = match.start() + max(len(url), len(match.group(0)))
            if url and has_url_scheme(url):
                urls.append(url)
    return urls


def _bare_url_at(text: str, start: int) -> str:
    r"""`start` 에서 시작하는 맨몸 URL — **링크와 같은 균형 괄호 규칙** (사이클 12 #2).

    열린 괄호 수만큼 닫힌 괄호까지 URL 에 포함한다. 예전에는 `[^\s"'<>()\[\]]+` 로
    잘라서 `https://live.example/report(dead)` 의 `…/report` 만 검사했고, 실제로
    죽어 있는 **전체 주소**는 카톡에 그대로 실린 채 통과했다(Codex 8차 HIGH #2).

    끝 구두점은 **괄호 밖일 때만** 떼어낸다 — `…/report(dead)` 의 `)` 는 URL 의
    일부이고, `…/x.` 의 `.` 는 문장 부호다.
    """
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char in _URL_STOP_CHARS:
            break
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        index += 1
    url = text[start:index]
    while url and url[-1] in _URL_TRAILING_PUNCT:
        if url.count("(") != url.count(")"):
            break
        url = url[:-1]
    return url


def _mask_link_targets(line: str) -> str:
    """`[표시문](URL)` 에서 **URL 부분만** 공백으로 가린다 (길이 보존)."""
    masked = line
    for link in reversed(find_links(line)):
        text_start = link.start + 1
        text_end = text_start + len(link.text)
        masked = (
            masked[:link.start]
            + " "
            + masked[text_start:text_end]
            + " " * (link.end - text_end)
            + masked[link.end:]
        )
    return masked


def body_urls(markdown_text: str, unique: bool = True) -> List[str]:
    """본문에 실린 **모든** URL — 마크다운 링크 + 맨몸 URL (문서 순서).

    죽은 링크 검사·조각 온전성 검사가 공유하는 정본 목록이다.
    `unique=False` 면 **출현마다** 담는다 (사이클 13 #3: 같은 URL 이 여러 번 나올 때
    일부 출현만 잘린 것을 수로 잡아내기 위해).
    """
    found = [
        url for url in body_link_urls(markdown_text) + bare_urls(markdown_text)
        if url
    ]
    if not unique:
        return found
    seen = []
    for url in found:
        if url not in seen:
            seen.append(url)
    return seen


# ─── 항목 줄 판정 ────────────────────────────────────────────────────────
def item_marker_id(line: str) -> Optional[str]:
    """항목 구조 마커 줄에서 공고 id. 마커가 아니면 None."""
    composer = _composer()
    pattern = getattr(composer, "ITEM_MARKER_RE", None) if composer else None
    if pattern is None:
        pattern = _FALLBACK_ITEM_MARKER_RE
    matched = pattern.match(line.strip())
    return matched.group(1) if matched else None


def parse_item_line(line: str) -> Optional[Dict]:
    """항목 줄을 라벨·제목·기관·대상·마감으로 분해. 항목 줄이 아니면 None."""
    stripped = line.strip()
    if not stripped or stripped.startswith(_NOT_ITEM_PREFIXES):
        return None
    composer = _composer()
    pattern = getattr(composer, "ITEM_LINE_RE", None) if composer else None
    if pattern is None:
        return None
    matched = pattern.match(stripped)
    if not matched:
        return None
    rest = matched.group("rest")
    return {
        "raw": stripped,
        "label": (matched.group("label") or "").strip(),
        "title": matched.group("title").strip(),
        "author": rest.split(" · ")[0].strip(),
        "target": _segment(rest, ("대상:",)),
        "deadline": _segment(rest, ("마감", "의견", "접수")),
    }


def _segment(rest: str, prefixes) -> str:
    """`· `로 나뉜 꼬리에서 주어진 접두사로 시작하는 조각을 찾는다."""
    for chunk in rest.split(" · ")[1:]:
        chunk = chunk.strip()
        for prefix in prefixes:
            if chunk.startswith(prefix):
                return chunk
    return ""


def _is_comment(line: str) -> bool:
    return line.strip().startswith("<!--")


# ─── 블록 파서 ───────────────────────────────────────────────────────────
# 블록 종류:
#   head    — 첫 섹션 앞의 줄 (제목·이번 주 한 줄)
#   section — `## ` 헤딩
#   item    — 항목 (마커 줄 + 제목 줄 + 원문 줄)
#   comment — HTML 주석 줄 (보류 목록·레인 표기). **절대 항목과 묶이지 않는다**
#   prose   — 그 밖의 줄 (협의회에서·회원사 소식·해설)
# 모든 블록은 `in_item_section` 을 갖는다 — 그 블록을 감싼 `## ` 헤딩이 항목 섹션인가.
def parse_blocks(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Dict]:
    """본문을 블록으로 쪼갠다. 블록들의 lines 를 이으면 원문과 **정확히 같다**.

    각 블록: {kind, lines, section, in_item_section, name?(section),
              title?/url?/fields?/item_id?(item)}
    `in_item_section` = 그 블록을 감싼 헤딩이 **항목 섹션 목록과 정확히 일치**하는가.
    """
    known = item_section_headings(markdown_text, item_sections)
    lines = (markdown_text or "").split("\n")
    blocks: List[Dict] = []
    section_name = ""
    in_item_section = False
    index = 0
    total = len(lines)

    def push(block: Dict, consumed: int) -> int:
        blocks.append(block)
        # 뒤따르는 빈 줄은 그 블록의 것이다 — 블록을 지우면 빈 줄도 함께 사라진다.
        cursor = consumed
        while cursor < total and not lines[cursor].strip():
            block["lines"].append(lines[cursor])
            cursor += 1
        return cursor

    while index < total:
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("## "):
            section_name = stripped[3:].strip()
            in_item_section = section_name in known        # 정확 일치
            index = push({"kind": "section", "name": section_name,
                          "section": section_name,
                          "in_item_section": in_item_section,
                          "lines": [line]}, index + 1)
            continue

        if _is_comment(line):
            # 항목 구조 마커 + 뒤따르는 두 줄이 항목 모양이면 **한 블록**이다.
            # 마커를 블록에 포함해야 항목을 지울 때 마커가 고아로 남지 않는다
            # (고아 마커는 다음 산문을 항목으로 둔갑시킨다).
            item_id = item_marker_id(line)
            if item_id is not None and in_item_section and index + 2 < total:
                fields = parse_item_line(lines[index + 1])
                url = origin_url(lines[index + 2]) if fields else None
                if fields and url:
                    index = push({"kind": "item", "title": fields["raw"],
                                  "section": section_name,
                                  "in_item_section": True,
                                  "url": url, "fields": fields,
                                  "item_id": item_id,
                                  "lines": [line, lines[index + 1],
                                            lines[index + 2]]}, index + 3)
                    continue
            index = push({"kind": "comment", "section": section_name,
                          "in_item_section": in_item_section,
                          "lines": [line]}, index + 1)
            continue

        kind = "head" if not section_name else "prose"
        index = push({"kind": kind, "section": section_name,
                      "in_item_section": in_item_section,
                      "lines": [line]}, index + 1)

    return blocks


def item_blocks(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Dict]:
    """항목 섹션 안의 항목 블록만 (문서 순서).

    미리보기 번호 N = 이 리스트의 N번째(1-based). checker 의 항목 수, prune 의 삭제
    대상, 카톡 렌더의 덩어리가 모두 이 한 목록에서 나온다.
    """
    return [
        {"title": block["title"], "url": block["url"],
         "section": block["section"], "fields": block["fields"],
         "item_id": block["item_id"]}
        for block in parse_blocks(markdown_text, item_sections)
        if block["kind"] == "item"
    ]


def item_block_count(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> int:
    """항목 블록 수."""
    return len(item_blocks(markdown_text, item_sections))


def item_urls(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[str]:
    """항목 URL만 (문서 순서). 번호 → URL 좌표의 정본."""
    return [
        block["url"] for block in item_blocks(markdown_text, item_sections)
    ]


def section_block_counts(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> Dict[str, int]:
    """항목 섹션 헤딩 → 그 섹션의 항목 블록 수."""
    counts: Dict[str, int] = {}
    for block in parse_blocks(markdown_text, item_sections):
        if block["kind"] == "section" and block["in_item_section"]:
            counts.setdefault(block["name"], 0)
        elif block["kind"] == "item":
            counts[block["section"]] = counts.get(block["section"], 0) + 1
    return counts


def cap_violations(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Tuple[str, int, int]]:
    """상한을 넘은 (섹션 헤딩, 실제 수, 상한) 목록.

    상한을 **재적용하지는 않는다** — 넘으면 pass=false 로 알린다.
    상한 표는 sections.caps_by_heading(composer.SECTION_LIMITS)가 정본이다.
    """
    counts = section_block_counts(markdown_text, item_sections)
    caps = sections_mod.caps_by_heading(tuple(counts))
    return [
        (name, count, caps[name])
        for name, count in counts.items()
        if name in caps and count > caps[name]
    ]


def prose_lines_in_item_sections(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[str]:
    """항목 섹션 안에서 **마커 없이** 실린 비어 있지 않은 줄 (사이클 7).

    산문·떠돌이 링크·항목 흉내(`*산림 제도 개정 — 산림청`)가 여기에 걸린다.
    checker 가 이 목록이 비어 있지 않으면 pass=false 로 떨어뜨린다 — 항목 섹션은
    composer 가 만든 항목 블록만 실어야 하고, 그 밖의 줄은 사람이 끼워 넣은 것이다.
    허용은 `*(항목 없음)*` 한 줄뿐이다.
    """
    offending: List[str] = []
    for block in parse_blocks(markdown_text, item_sections):
        if block["kind"] in ("item", "section"):
            continue
        if not block.get("in_item_section"):
            continue
        for line in block["lines"]:
            stripped = line.strip()
            if not stripped or stripped in _ALLOWED_BARE_LINES:
                continue
            if _is_comment(stripped):
                # 주석은 발송본에 실리지 않는다 (보류 목록·고아 마커)
                continue
            offending.append(stripped)
    return offending


def link_audit(
    markdown_text: str,
    item_sections: Optional[Sequence[str]] = None,
    allowed_urls: Optional[Sequence[str]] = None,
) -> Dict:
    """본문 링크 내역 (사이클 8 #1: 항목 섹션의 링크는 정본 URL 집합에만 있어야 한다).

    Returns:
        {"total": 본문 전체 링크 수, "items": 항목 블록의 **정본 URL** 링크 수,
         "commentary": 항목 섹션 **밖**(머리말·산문 섹션) 링크 수,
         "stray": 항목 섹션 안에서 정본에 없는 링크 수}

    사이클 7 판은 항목 블록 안의 링크를 몇 개든 `items` 로 합산했다 — 정상 항목의
    **제목에 링크를 하나 더 끼우면** `items` 가 함께 늘어 총계가 맞아버렸다
    (항목 3 · HTML 링크 4 · pass). 이제 판정 기준은 개수가 아니라 **정본 URL 집합**이고,
    항목 섹션의 초과 링크는 1개라도 `stray` 로 남아 게이트를 떨어뜨린다.
    `allowed_urls` 가 없으면 블록의 원문 URL 자신을 정본으로 본다(정본 파일 부재 시
    checker 가 따로 fail-closed 한다).
    """
    total = 0
    items = 0
    commentary = 0
    stray = 0
    for block in parse_blocks(markdown_text, item_sections):
        if block["kind"] == "comment":
            continue                    # 주석은 발송본에 실리지 않는다
        allowed = (
            set(allowed_urls) if allowed_urls is not None
            else ({block["url"]} if block["kind"] == "item" else set())
        )
        for line in block["lines"]:
            if _is_comment(line):
                continue
            for link in find_links(line):
                total += 1
                if block["kind"] == "item" and link.url in allowed:
                    items += 1
                elif block.get("in_item_section"):
                    stray += 1
                elif block["kind"] == "item":
                    stray += 1
                else:
                    commentary += 1
    return {"total": total, "items": items,
            "commentary": commentary, "stray": stray}
