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

항목 블록의 정의 (두 조건을 **모두** 만족해야 항목이다):

1. 항목 줄이 composer 가 만드는 형식이다 (`composer.ITEM_LINE_RE` 가 정본)
   - 신청하세요: `[라벨] 제목 — 기관 · 대상: … · 마감 …` (라벨 = D-n / 새 소식 / 상시 / 마감 미정)
   - 알아두세요: `제목 — 기관 · 대상: … · 의견 …까지`
2. **바로 다음 줄이 `  [원문](URL)`** 이다.

산문 줄 + 링크 줄은 항목이 아니다. 구형 형식 v1.2(`### 제목` + `**원문:** [x](url)`)는
더 이상 생성되지 않으므로 지원하지 않는다 — 지원하는 척하면 두 형식의 계수가 또 갈라진다.

블록 경계 (사이클 6 #2): 항목 블록은 **제목 줄 + 원문 줄(+ 뒤따르는 빈 줄)** 뿐이다.
다음 항목 줄·헤딩·주석은 각자 새 블록을 연다. 그래서 죽은 항목을 지워도 이어지는
산문이나 살아 있는 항목, 보류 주석이 함께 사라지지 않는다.
"""

import re
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from alert.digest import sections as sections_mod

# 링크 후보로 인정하는 스킴 (사이클 6 #4). `~9.30`·`산림사업자` 같은 괄호는 URL이 아니다.
_SCHEME_RE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)

# 항목의 원문 링크 줄은 링크 하나로만 이루어진다.
ORIGIN_LINK_TEXT = "원문"

# 항목 줄이 될 수 없는 줄머리: 헤딩(#), 주석(<!--), 강조·빈 표시(*), 산문 글머리
_NOT_ITEM_PREFIXES = ("#", "<!--", "*", "·", "-", ">", "|")

EMPTY_SECTION_LINE = "*(항목 없음)*"


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
    """본문(HTML 주석 줄 제외)에 실제로 남아 있는 모든 링크 URL (문서 순서)."""
    urls: List[str] = []
    for line in (markdown_text or "").split("\n"):
        if _is_comment(line):
            continue
        for link in find_links(line):
            urls.append(link.url)
    return urls


# ─── 항목 줄 판정 ────────────────────────────────────────────────────────
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
#   item    — 항목 (제목 줄 + 원문 줄)
#   comment — HTML 주석 줄 (보류 목록·레인 표기). **절대 항목과 묶이지 않는다**
#   prose   — 그 밖의 줄 (협의회에서·회원사 소식·해설)
def parse_blocks(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Dict]:
    """본문을 블록으로 쪼갠다. 블록들의 lines 를 이으면 원문과 **정확히 같다**.

    각 블록: {kind, lines, section, is_item, name?(section),
              title?/url?/fields?(item)}
    `is_item` = 그 블록이 속한 섹션이 **항목 섹션 목록과 정확히 일치**하는가.
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
                          "is_item": in_item_section,
                          "lines": [line]}, index + 1)
            continue

        if _is_comment(line):
            index = push({"kind": "comment", "section": section_name,
                          "is_item": False, "lines": [line]}, index + 1)
            continue

        if in_item_section and not stripped.startswith("#"):
            fields = parse_item_line(line)
            url = (
                origin_url(lines[index + 1]) if fields and index + 1 < total
                else None
            )
            if fields and url:
                index = push({"kind": "item", "title": fields["raw"],
                              "section": section_name, "is_item": True,
                              "url": url, "fields": fields,
                              "lines": [line, lines[index + 1]]}, index + 2)
                continue

        kind = "head" if not section_name else "prose"
        index = push({"kind": kind, "section": section_name,
                      "is_item": False, "lines": [line]}, index + 1)

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
         "section": block["section"], "fields": block["fields"]}
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
        if block["kind"] == "section" and block["is_item"]:
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
