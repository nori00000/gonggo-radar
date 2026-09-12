"""본문에서 죽은 링크를 걷어낸다 (계약 W10 + 사이클 6).

weekly_digest 는 재조립(compose)으로 죽은 항목을 빼지만, `/digest 재검토` 경로는
재조립을 하지 않는다(손으로 고친 본문과 해설이 마커로 되돌아가기 때문이다).
그래서 재검증은 **본문을 직접 편집**한다.

이 모듈은 **편집만** 한다:
- 어떤 섹션이 항목 섹션인가 → `alert.digest.sections` (check.json 의 `item_sections`
  와 **정확 일치**, 계약 W10 사이클3 #7)
- 그 섹션 안에서 무엇이 항목 블록인가 → `alert.digest.blocks` (사이클 6 #1)

삭제 범위:
- 죽은 URL을 가진 **항목 블록만** 지운다 (제목 줄 + 원문 줄). 블록 경계는 blocks 가
  정하므로 다음 항목의 살아 있는 블록이나 보류 주석이 함께 사라지지 않는다
  (사이클 6 #2 / Codex 신규 #4).
- 산문 섹션·머리말·해설의 죽은 링크는 `[텍스트](url)` → `텍스트` 로 축약하고 문장은
  보존한다 — 사람이 쓴 의견을 URL 하나 때문에 지우면 안 된다.

**제목 중복 병합은 하지 않는다** (사이클 6 #3 / Codex 신규 #5). 병합 판정은 compose
단계의 몫이고, 소스 간 비병합 계약(같은 제목이라도 forest_service·forest_press 는
각자 게시)을 재검토가 뒤집으면 안 된다. 여기서 지우는 중복은 **동일 URL** 뿐이다.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

from alert.digest import blocks as blocks_mod

# 항목이 전부 빠진 항목 섹션에 남기는 표시 (composer 와 같은 문자열)
EMPTY_SECTION_LINE = blocks_mod.EMPTY_SECTION_LINE

_BLANKS_RE = re.compile(r"\n{3,}")


def _strip_dead_links(line: str, dead_set, stripped: List[str]) -> str:
    """줄 안의 죽은 링크를 `[텍스트](url)` → `텍스트` 로 축약 (문장 보존).

    치환은 **뒤에서 앞으로** 한다 — 앞에서 바꾸면 뒤 링크의 좌표가 밀린다.
    링크 탐색은 blocks.find_links(균형 괄호·스킴 필수)가 정본이다.
    """
    links = [link for link in blocks_mod.find_links(line)
             if link.url in dead_set]
    if not links:
        return line
    for link in reversed(links):
        if link.url not in stripped:
            stripped.append(link.url)
        line = line[:link.start] + link.text + line[link.end:]
    return line


def _render(
    kept: List[Dict], original: str, dead_set=frozenset()
) -> Tuple[str, List[str]]:
    """남은 블록을 본문으로. dead_set 의 링크는 텍스트만 남긴다."""
    stripped: List[str] = []
    lines = [
        _strip_dead_links(line, dead_set, stripped) if dead_set else line
        for block in kept for line in block["lines"]
    ]
    text = _BLANKS_RE.sub("\n\n", "\n".join(lines))
    if original.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text, stripped


def _mark_empty_sections(kept: List[Dict]) -> None:
    """항목이 전부 빠진 항목 섹션에 composer 와 같은 빈 표시를 남긴다."""
    for index, block in enumerate(kept):
        if block["kind"] != "section" or not block["in_item_section"]:
            continue
        following = kept[index + 1:]
        end = next(
            (i for i, nxt in enumerate(following) if nxt["kind"] == "section"),
            len(following),
        )
        body = following[:end]
        if any(nxt["kind"] == "item" for nxt in body):
            continue
        text = "\n".join(line for nxt in [block] + body for line in nxt["lines"])
        if EMPTY_SECTION_LINE not in text:
            block["lines"] = block["lines"] + ["", EMPTY_SECTION_LINE]


def strip_dead_urls(
    markdown_text: str, dead: List[str],
    item_sections: Optional[Sequence[str]] = None,
) -> Tuple[str, List[Dict], List[str]]:
    """(새 본문, 제거된 항목 [{title, url}], 링크만 떼어낸 죽은 URL 목록)."""
    dead_set = {url for url in (dead or []) if url}
    if not dead_set:
        return markdown_text, [], []

    removed: List[Dict] = []
    kept: List[Dict] = []
    for block in blocks_mod.parse_blocks(markdown_text, item_sections):
        if block["kind"] == "item" and block["url"] in dead_set:
            removed.append({"title": block["title"], "url": block["url"]})
            continue
        kept.append(block)

    _mark_empty_sections(kept)
    text, stripped = _render(kept, markdown_text, dead_set)
    return text, removed, stripped


def dedupe_urls(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> Tuple[str, List[Dict]]:
    """같은 URL을 가리키는 뒤쪽 항목 블록을 삭제 (사이클 6 #3).

    제목이 같아도 URL 이 다르면 **남긴다** — 소스 간 비병합 계약(같은 사안을
    forest_service·forest_press 가 각자 게시)을 재검토가 뒤집지 않는다.
    """
    seen = set()
    removed: List[Dict] = []
    kept: List[Dict] = []
    for block in blocks_mod.parse_blocks(markdown_text, item_sections):
        if block["kind"] == "item":
            if block["url"] in seen:
                removed.append({"title": block["title"], "url": block["url"]})
                continue
            seen.add(block["url"])
        kept.append(block)

    if not removed:
        return markdown_text, []

    _mark_empty_sections(kept)
    text, _ = _render(kept, markdown_text)
    return text, removed
