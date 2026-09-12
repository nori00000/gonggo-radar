"""다이제스트 본문의 항목 블록 구조 + 죽은 URL 제거 (계약 W10).

weekly_digest 는 재조립(compose)으로 죽은 항목을 빼지만, `/digest 재검토` 경로는
재조립을 하지 않는다(손으로 고친 본문과 해설이 마커로 되돌아가기 때문이다).
그래서 재검증은 **본문을 직접 편집**한다.

삭제 범위 (사이클2 #5): **항목 섹션 안의 항목 블록만** 지운다.
항목 블록은 두 형식을 모두 읽는다 — v1.2 의 `### 제목` + `**원문:** [x](url)` 과
v2 의 `제목 — 기관 · …` + `  [원문](url)`.
협의회 의견/협의회에서·회원사 소식·이번 주 한 줄 같은 산문 섹션에서는 죽은 링크만
떼어내고 문장은 남긴다 — 사람이 쓴 의견을 URL 하나 때문에 지우면 안 된다.

항목 섹션의 정본은 composer.SECTION_LIMITS 의 키다(계약 v1.2 의 산림/지원사업/
사회연대경제, 계약 v2 의 신청하세요/알아두세요 모두 그 키로 표현된다). import 는
지연 로딩이며, 읽기만 한다 — composer 는 고치지 않는다.
"""

import re
from typing import Dict, List, Optional, Tuple

EMPTY_SECTION_LINE = "*(항목 없음)*"

# composer 를 못 읽을 때의 폴백 (계약 v1.2·v2 양쪽 이름)
FALLBACK_ITEM_SECTION_KEYS = (
    "신청하세요", "알아두세요", "산림", "지원사업", "사회연대경제",
)

# 형식 v1.2 의 원문 줄: `**원문:** [텍스트](URL)`
_ORIGIN_RE = re.compile(r"^\*\*원문:\*\*\s*(.+)$")
# 형식 v2.1 의 원문 줄: `  [원문](URL)` — 줄 전체가 링크 하나
_ORIGIN_LINK_RE = re.compile(r"^\[원문\]\((\S+)\)$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BLANKS_RE = re.compile(r"\n{3,}")

# 형식 v2.1 의 항목에는 `### ` 머리글이 없다 — 제목 줄 + 원문 줄 두 줄이 한 블록이다.
# 항목 제목 줄이 될 수 없는 것들: 머리글(#), 주석(<!--), 강조·빈 표시(*),
# 산문 글머리(·, -, >, |).
_NOT_ITEM_HEAD_PREFIXES = ("#", "<!--", "*", "·", "-", ">", "|")


def _composer():
    """composer 모듈 (없거나 깨지면 None). 지연 로딩 — import 순환을 피한다."""
    try:
        from alert.digest import composer
    except Exception:       # noqa: BLE001 — composer 가 개편 중이어도 prune 은 산다
        return None
    return composer


def item_section_keys() -> Tuple[str, ...]:
    """항목 섹션 판정 키 (composer.SECTION_LIMITS 가 정본)."""
    composer = _composer()
    limits = getattr(composer, "SECTION_LIMITS", None) if composer else None
    if isinstance(limits, dict) and limits:
        return tuple(str(key) for key in limits)
    return FALLBACK_ITEM_SECTION_KEYS


def section_caps() -> Dict[str, int]:
    """섹션 키 → 상한 (composer.SECTION_LIMITS). 없으면 빈 dict."""
    composer = _composer()
    limits = getattr(composer, "SECTION_LIMITS", None) if composer else None
    if not isinstance(limits, dict):
        return {}
    return {str(key): int(value) for key, value in limits.items()}


def section_key(section_name: Optional[str]) -> Optional[str]:
    """섹션 헤딩 → 항목 섹션 키. 산문 섹션·미지의 섹션이면 None.

    미지의 섹션을 산문으로 보는 것은 의도적이다 — 모르는 섹션의 블록을 지우는 것보다
    죽은 URL 을 남겨 pass=false 로 크게 실패하는 편이 안전하다(fail-closed).
    """
    name = (section_name or "").strip()
    if not name:
        return None
    for key in item_section_keys():
        if key and key in name:
            return key
    return None


def _origin_url(line: str) -> Optional[str]:
    """원문 줄에서 URL. 원문 줄이 아니면 None.

    두 형식을 모두 읽는다 — v1.2 의 `**원문:** [x](url)` 과 v2.1 의 `[원문](url)`.
    형식이 바뀔 때마다 게이트가 조용히 "항목 0건"을 세는 일을 막기 위한 것이다.
    """
    stripped = line.strip()
    matched = _ORIGIN_RE.match(stripped)
    if matched:
        link = _LINK_RE.search(matched.group(1))
        return link.group(2).strip() if link else matched.group(1).strip()
    matched = _ORIGIN_LINK_RE.match(stripped)
    if matched:
        return matched.group(1).strip()
    return None


def _is_item_head(line: str, next_line: str) -> bool:
    """형식 v2.1 항목 블록의 첫 줄인가 (항목 섹션 안에서만 묻는다).

    판정은 **다음 줄**로 한다 — `[원문](URL)` 이 따라오는 줄만 항목 제목이다.
    이 조건이 없으면 산문 한 줄이나 v1.2 블록 안의 설명 줄이 항목으로 둔갑한다.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(_NOT_ITEM_HEAD_PREFIXES):
        return False
    return _ORIGIN_LINK_RE.match(next_line.strip()) is not None


def parse_blocks(markdown_text: str) -> List[Dict]:
    """본문을 덩어리로 쪼갠다.

    각 블록: {kind: head|section|item, lines, name?(section), title?(item),
              section(항목이 속한 섹션 헤딩), key(항목 섹션 키 or None), url?}
    """
    blocks: List[Dict] = []
    current_section = ""
    current_key: Optional[str] = None
    lines = (markdown_text or "").split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if stripped.startswith("## "):
            current_section = stripped[3:].strip()
            current_key = section_key(current_section)
            blocks.append({"kind": "section", "name": current_section,
                           "section": current_section, "key": current_key,
                           "lines": [line]})
        elif stripped.startswith("### "):
            blocks.append({"kind": "item", "title": stripped[4:].strip(),
                           "section": current_section, "key": current_key,
                           "url": None, "lines": [line]})
        elif current_key and blocks and _is_item_head(line, next_line):
            # 형식 v2.1: 항목 섹션 안에서 `[원문](URL)` 이 따라오는 줄이 항목 제목
            blocks.append({"kind": "item", "title": stripped,
                           "section": current_section, "key": current_key,
                           "url": None, "lines": [line]})
        elif blocks:
            block = blocks[-1]
            block["lines"].append(line)
            if block["kind"] == "item" and block.get("url") is None:
                block["url"] = _origin_url(line)
        else:
            blocks.append({"kind": "head", "section": "", "key": None,
                           "lines": [line]})
    return blocks


def item_blocks(markdown_text: str) -> List[Dict]:
    """항목 섹션 안에서 원문 URL 을 가진 항목 블록만 (문서 순서).

    미리보기 번호 N = 이 리스트의 N번째(1-based). 항목 수는 **링크 수가 아니라
    블록 수**다 (사이클2 #6) — 해설의 참고 링크가 항목으로 세어지면 안 된다.
    """
    return [
        {"title": block["title"], "url": block["url"],
         "section": block["section"], "key": block["key"]}
        for block in parse_blocks(markdown_text)
        if block["kind"] == "item" and block["key"] and block.get("url")
    ]


def item_block_count(markdown_text: str) -> int:
    """항목 블록 수."""
    return len(item_blocks(markdown_text))


def section_block_counts(markdown_text: str) -> Dict[str, int]:
    """항목 섹션 헤딩 → 그 섹션의 항목 블록 수."""
    counts: Dict[str, int] = {}
    for block in parse_blocks(markdown_text):
        if block["kind"] == "section" and block["key"]:
            counts.setdefault(block["name"], 0)
        elif block["kind"] == "item" and block["key"] and block.get("url"):
            counts[block["section"]] = counts.get(block["section"], 0) + 1
    return counts


def cap_violations(markdown_text: str) -> List[Tuple[str, int, int]]:
    """상한을 넘은 (섹션 헤딩, 실제 수, 상한) 목록 (사이클2 #7).

    상한을 **재적용하지는 않는다** — 넘으면 pass=false 로 알린다.
    """
    caps = section_caps()
    violations = []
    for name, count in section_block_counts(markdown_text).items():
        key = section_key(name)
        cap = caps.get(key) if key else None
        if cap is not None and count > cap:
            violations.append((name, count, cap))
    return violations


def _normalize_title(title: str) -> str:
    """제목 정규화 (composer.normalize_title 이 정본, 없으면 공백 정규화)."""
    composer = _composer()
    normalize = getattr(composer, "normalize_title", None) if composer else None
    if callable(normalize):
        try:
            return normalize(title)
        except Exception:   # noqa: BLE001
            pass
    return " ".join((title or "").split())


def _render(kept: List[Dict], original: str, dead_set=frozenset()) -> Tuple[str, List[str]]:
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
        if block["kind"] != "section" or not block["key"]:
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
    markdown_text: str, dead: List[str]
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
    for block in parse_blocks(markdown_text):
        if (block["kind"] == "item" and block["key"]
                and block.get("url") in dead_set):
            removed.append({"title": block["title"], "url": block["url"]})
            continue
        kept.append(block)

    _mark_empty_sections(kept)
    text, stripped = _render(kept, markdown_text, dead_set)
    return text, removed, stripped


def dedupe_titles(markdown_text: str) -> Tuple[str, List[Dict]]:
    """정규화 제목이 같은 뒤쪽 항목 블록을 삭제 (사이클2 #7).

    죽은 링크를 떼어내면 `동향 [자료](죽은URL)` 이 `동향 자료` 가 되어 기존 항목과
    제목이 겹칠 수 있다 — compose 의 중복 제거를 본문 편집 뒤에 한 번 더 돈다.
    """
    seen = set()
    removed: List[Dict] = []
    kept: List[Dict] = []
    for block in parse_blocks(markdown_text):
        if block["kind"] == "item" and block["key"]:
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
