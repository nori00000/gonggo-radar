"""다이제스트 본문의 항목 블록 구조 + 죽은 URL 제거 (계약 W10).

weekly_digest 는 재조립(compose)으로 죽은 항목을 빼지만, `/digest 재검토` 경로는
재조립을 하지 않는다(손으로 고친 본문과 해설이 마커로 되돌아가기 때문이다).
그래서 재검증은 **본문을 직접 편집**한다.

삭제 범위 (사이클2 #5, 사이클3 #7): **항목 섹션 안의 `###` 블록만** 지운다.
섹션 판정은 alert/digest/sections.py 가 주는 **정확한 헤딩 목록과의 완전 일치**다 —
부분 일치로 하면 `## 협의회에서 — 지원사업 의견` 이 공고 섹션으로 오분류돼 해설이
항목으로 세어지고 삭제된다.

산문 섹션(협의회 의견/협의회에서·회원사 소식·이번 주 한 줄)과 목록에 없는 섹션에서는
죽은 링크만 떼어내고 문장은 남긴다 — 사람이 쓴 의견을 URL 하나 때문에 지우면 안 된다.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

from alert.digest import sections as sections_mod

EMPTY_SECTION_LINE = "*(항목 없음)*"

_ORIGIN_RE = re.compile(r"^\*\*원문:\*\*\s*(.+)$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BLANKS_RE = re.compile(r"\n{3,}")


def _item_sections(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> Tuple[str, ...]:
    """판정에 쓸 항목 섹션 헤딩 목록 (정확 일치용)."""
    if item_sections is not None:
        return tuple(item_sections)
    resolved, _ = sections_mod.resolve(None, markdown_text)
    return resolved


def _origin_url(line: str) -> Optional[str]:
    """`**원문:** [x](url)` 줄에서 URL. 원문 줄이 아니면 None."""
    matched = _ORIGIN_RE.match(line.strip())
    if not matched:
        return None
    link = _LINK_RE.search(matched.group(1))
    return link.group(2).strip() if link else matched.group(1).strip()


def parse_blocks(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Dict]:
    """본문을 덩어리로 쪼갠다.

    각 블록: {kind: head|section|item, lines, name?(section), title?(item),
              section(속한 섹션 헤딩), is_item(항목 섹션 소속 여부), url?}
    """
    known = _item_sections(markdown_text, item_sections)
    blocks: List[Dict] = []
    current_section = ""
    in_item_section = False
    for line in (markdown_text or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped[3:].strip()
            in_item_section = current_section in known      # 정확 일치
            blocks.append({"kind": "section", "name": current_section,
                           "section": current_section,
                           "is_item": in_item_section, "lines": [line]})
        elif stripped.startswith("### "):
            blocks.append({"kind": "item", "title": stripped[4:].strip(),
                           "section": current_section, "is_item": in_item_section,
                           "url": None, "lines": [line]})
        elif blocks:
            block = blocks[-1]
            block["lines"].append(line)
            if block["kind"] == "item" and block.get("url") is None:
                block["url"] = _origin_url(line)
        else:
            blocks.append({"kind": "head", "section": "", "is_item": False,
                           "lines": [line]})
    return blocks


def item_blocks(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Dict]:
    """항목 섹션 안에서 `**원문:**` 을 가진 `###` 블록만 (문서 순서).

    미리보기 번호 N = 이 리스트의 N번째(1-based). 항목 수는 **링크 수가 아니라
    블록 수**다 (사이클2 #6) — 해설의 참고 링크가 항목으로 세어지면 안 된다.
    """
    return [
        {"title": block["title"], "url": block["url"], "section": block["section"]}
        for block in parse_blocks(markdown_text, item_sections)
        if block["kind"] == "item" and block["is_item"] and block.get("url")
    ]


def body_links(markdown_text: str) -> List[str]:
    """본문에 실린 **모든** URL (항목·해설·산문·머리말, 문서 순서·중복 제거).

    제외(excluded_urls) 검사의 기준이다 (사이클5 #1): 제외한 URL 을 해설의 참고
    링크나 항목의 두 번째 링크로 옮기면 `**원문:**` 목록에서 빠져 검사를 통과했다.
    마크다운 링크 전부 + `**원문:**` 의 링크 아닌 원시값까지 센다.
    """
    found: List[str] = []

    def _add(url):
        url = (url or "").strip()
        if url and url not in found:
            found.append(url)

    for line in (markdown_text or "").split("\n"):
        for match in _LINK_RE.finditer(line):
            _add(match.group(2))
        origin = _ORIGIN_RE.match(line.strip())
        if origin and not _LINK_RE.search(origin.group(1)):
            _add(origin.group(1))
    return found


def item_block_count(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> int:
    """항목 블록 수."""
    return len(item_blocks(markdown_text, item_sections))


def section_block_counts(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> Dict[str, int]:
    """항목 섹션 헤딩 → 그 섹션의 항목 블록 수."""
    counts: Dict[str, int] = {}
    for block in parse_blocks(markdown_text, item_sections):
        if block["kind"] == "section" and block["is_item"]:
            counts.setdefault(block["name"], 0)
        elif block["kind"] == "item" and block["is_item"] and block.get("url"):
            counts[block["section"]] = counts.get(block["section"], 0) + 1
    return counts


def cap_violations(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Tuple[str, int, int]]:
    """상한을 넘은 (섹션 헤딩, 실제 수, 상한) 목록 (사이클2 #7).

    상한을 **재적용하지는 않는다** — 넘으면 pass=false 로 알린다.
    """
    counts = section_block_counts(markdown_text, item_sections)
    caps = sections_mod.caps_by_heading(tuple(counts))
    return [
        (name, count, caps[name])
        for name, count in counts.items()
        if name in caps and count > caps[name]
    ]


def _normalize_title(title: str) -> str:
    """제목 정규화 (composer.normalize_title 이 정본, 없으면 공백 정규화)."""
    composer = sections_mod._composer()
    normalize = getattr(composer, "normalize_title", None) if composer else None
    if callable(normalize):
        try:
            return normalize(title)
        except Exception:   # noqa: BLE001
            pass
    return " ".join((title or "").split())


def _render(
    kept: List[Dict], original: str, dead_set=frozenset()
) -> Tuple[str, List[str]]:
    """남은 블록을 본문으로. dead_set 의 링크는 텍스트만 남긴다."""
    stripped: List[str] = []

    def _strip_link(match):
        url = match.group(2).strip()
        if url in dead_set:
            if url not in stripped:
                stripped.append(url)
            return match.group(1)
        return match.group(0)

    lines = [
        _LINK_RE.sub(_strip_link, line) if dead_set else line
        for block in kept for line in block["lines"]
    ]
    text = _BLANKS_RE.sub("\n\n", "\n".join(lines))
    if original.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text, stripped


def _mark_empty_sections(kept: List[Dict]) -> None:
    """항목이 전부 빠진 항목 섹션에 composer 와 같은 빈 표시를 남긴다."""
    for index, block in enumerate(kept):
        if block["kind"] != "section" or not block["is_item"]:
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
    """(새 본문, 제거된 항목 [{title, url}], 링크만 떼어낸 죽은 URL 목록).

    항목 섹션의 `###` 블록만 삭제한다. 산문 섹션·머리말의 죽은 링크는
    `[텍스트](url)` → `텍스트` 로 축약하고 문장은 보존한다 (사이클2 #5).
    """
    dead_set = {url for url in (dead or []) if url}
    if not dead_set:
        return markdown_text, [], []

    removed: List[Dict] = []
    kept: List[Dict] = []
    for block in parse_blocks(markdown_text, item_sections):
        if (block["kind"] == "item" and block["is_item"]
                and block.get("url") in dead_set):
            removed.append({"title": block["title"], "url": block["url"]})
            continue
        kept.append(block)

    _mark_empty_sections(kept)
    text, stripped = _render(kept, markdown_text, dead_set)
    return text, removed, stripped


def dedupe_titles(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> Tuple[str, List[Dict]]:
    """정규화 제목이 같은 뒤쪽 항목 블록을 삭제 (사이클2 #7).

    죽은 링크를 떼어내면 `동향 [자료](죽은URL)` 이 `동향 자료` 가 되어 기존 항목과
    제목이 겹칠 수 있다 — compose 의 중복 제거를 본문 편집 뒤에 한 번 더 돈다.
    """
    seen = set()
    removed: List[Dict] = []
    kept: List[Dict] = []
    for block in parse_blocks(markdown_text, item_sections):
        if block["kind"] == "item" and block["is_item"]:
            normalized = _normalize_title(block["title"])
            if normalized in seen:
                removed.append({"title": block["title"], "url": block.get("url")})
                continue
            seen.add(normalized)
        kept.append(block)

    if not removed:
        return markdown_text, []

    _mark_empty_sections(kept)
    text, _ = _render(kept, markdown_text)
    return text, removed
