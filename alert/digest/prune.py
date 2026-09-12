"""죽은 URL을 다이제스트 본문에서 걷어낸다 (계약 W10, 크리틱 #2).

weekly_digest 는 재조립(compose)으로 죽은 항목을 빼지만, `/digest 재검토` 경로는
재조립을 하지 않는다(손으로 고친 본문과 해설이 마커로 되돌아가기 때문이다).
그래서 재검증은 **본문을 직접 편집**해서 죽은 URL을 뺀다:

- 항목 블록(`### 제목` … 다음 제목까지)의 `**원문:**` URL이 죽었으면 블록째 제거
- 그 밖의 위치(협의회 의견 등)에 있는 죽은 링크는 `[텍스트](url)` → `텍스트` 로 축약
  (문장은 남기고 링크만 없앤다 — 사람이 쓴 문장을 임의로 지우지 않는다)
"""

import re
from typing import Dict, List, Tuple

ITEM_SECTIONS = ("산림 정책 동향", "지원사업 공고", "사회연대경제 동향")
EMPTY_SECTION_LINE = "*(항목 없음)*"

_ORIGIN_RE = re.compile(r"^\*\*원문:\*\*\s*(.+)$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BLANKS_RE = re.compile(r"\n{3,}")


def _origin_url(line: str):
    """`**원문:** [x](url)` 줄에서 URL. 원문 줄이 아니면 None."""
    matched = _ORIGIN_RE.match(line.strip())
    if not matched:
        return None
    link = _LINK_RE.search(matched.group(1))
    return link.group(2).strip() if link else matched.group(1).strip()


def _blocks(lines: List[str]) -> List[Dict]:
    """본문을 (섹션 헤딩 | 항목 블록 | 그 밖) 덩어리로 쪼갠다."""
    blocks: List[Dict] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            blocks.append({"kind": "section", "name": stripped[3:].strip(),
                           "lines": [line]})
        elif stripped.startswith("### "):
            blocks.append({"kind": "item", "title": stripped[4:].strip(),
                           "lines": [line]})
        elif blocks:
            blocks[-1]["lines"].append(line)
        else:
            blocks.append({"kind": "head", "lines": [line]})
    return blocks


def strip_dead_urls(
    markdown_text: str, dead: List[str]
) -> Tuple[str, List[Dict], List[str]]:
    """(새 본문, 제거된 항목 [{title, url}], 링크만 떼어낸 죽은 URL 목록).

    dead 가 비어 있으면 원문을 그대로 돌려준다.
    """
    dead_set = {url for url in (dead or []) if url}
    if not dead_set:
        return markdown_text, [], []

    blocks = _blocks((markdown_text or "").split("\n"))
    removed: List[Dict] = []
    kept: List[Dict] = []

    for block in blocks:
        if block["kind"] == "item":
            urls = [
                url for url in (_origin_url(line) for line in block["lines"])
                if url
            ]
            if any(url in dead_set for url in urls):
                removed.append({
                    "title": block["title"],
                    "url": next(url for url in urls if url in dead_set),
                })
                continue
        kept.append(block)

    # 항목이 전부 빠진 항목 섹션에는 composer 와 같은 빈 표시를 남긴다.
    for index, block in enumerate(kept):
        if block["kind"] != "section" or block["name"] not in ITEM_SECTIONS:
            continue
        following = kept[index + 1:]
        next_section = next(
            (i for i, nxt in enumerate(following) if nxt["kind"] == "section"),
            len(following),
        )
        body = following[:next_section]
        if any(nxt["kind"] == "item" for nxt in body):
            continue
        text = "\n".join(
            line for nxt in [block] + body for line in nxt["lines"]
        )
        if EMPTY_SECTION_LINE not in text:
            block["lines"] = block["lines"] + ["", EMPTY_SECTION_LINE]

    stripped: List[str] = []

    def _strip_link(match):
        url = match.group(2).strip()
        if url in dead_set:
            if url not in stripped:
                stripped.append(url)
            return match.group(1)
        return match.group(0)

    lines = [
        _LINK_RE.sub(_strip_link, line)
        for block in kept for line in block["lines"]
    ]
    text = _BLANKS_RE.sub("\n\n", "\n".join(lines))
    if markdown_text.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text, removed, stripped
