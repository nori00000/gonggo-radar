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

제어 문자 게이트(계약 W10 사이클7 #4)와 **파서 API 이름**도 여기 있다. 후자는
전부 `alert.digest.blocks` 로의 위임이다 — 링크 파서·URL 추출은 저장소에 하나뿐이고
(발송기의 렌더와 검사기가 같은 함수를 봐야 한다), 이 모듈은 그 이름을 계약 W10 의
호출자들에게 그대로 내어줄 뿐이다.
"""

import re
import unicodedata
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
            removed.append({"title": block["title"], "url": block["url"],
                            "item_id": block.get("item_id")})
            continue
        kept.append(block)

    _mark_empty_sections(kept)
    text, stripped = _render(kept, markdown_text, dead_set)
    return text, removed, stripped


def dedupe_duplicate_blocks(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> Tuple[str, List[Dict]]:
    """**같은 (id, URL) 블록이 두 번 실린 것**만 접는다 (사이클 9 #1).

    사이클 6~8 판은 "같은 URL" 이면 뒤 블록을 지웠다. 그것이 자동 교정이 되어,
    md 의 URL 하나를 잘못 고친 본문에서 **살아 있는 다른 공고**를 중복으로 지우고
    통과시켰다(Codex 5차 HIGH #2). 이제 접는 대상은 문자 그대로의 블록 복제
    (같은 id · 같은 URL)뿐이고, 정본(items.json)은 **건드리지 않는다** — 정본에는
    그 id 가 한 번만 있으므로 복제를 접으면 개수가 저절로 맞는다 (MEDIUM #5).

    id·URL 이 다른 불일치는 고치지 않는다. checker 가 `pass=false` 로 멈추고,
    해소는 재조립뿐이다.
    """
    seen = set()
    removed: List[Dict] = []
    kept: List[Dict] = []
    for block in blocks_mod.parse_blocks(markdown_text, item_sections):
        if block["kind"] == "item":
            key = (str(block.get("item_id")), block["url"])
            if key in seen:
                removed.append({"title": block["title"], "url": block["url"],
                                "item_id": block.get("item_id")})
                continue
            seen.add(key)
        kept.append(block)

    if not removed:
        return markdown_text, []

    _mark_empty_sections(kept)
    text, _ = _render(kept, markdown_text)
    return text, removed


def drop_from_manifest(manifest: Dict, removed: Sequence[Dict]) -> Dict:
    """항목 정본 파일에서 제거된 항목을 뺀다 (사이클 8 #1).

    본문에서만 지우고 정본을 그대로 두면 "항목 수 불일치" 로 게이트가 막힌다 —
    재검토는 본문과 정본을 **함께** 갱신한다.

    기준은 **id** 다. URL 로 지우면 같은 URL 을 가리키는 중복 항목을 접을 때
    남겨둔 쪽까지 정본에서 사라져 개수가 어긋난다(URL 중복이 바로 그 경우다).
    id 를 모르는 기록(산문 링크 등)은 URL 로 지운다.
    """
    dead_ids = {
        str(item["item_id"]) for item in (removed or [])
        if item.get("item_id") is not None
    }
    dead_urls_only = {
        item.get("url") for item in (removed or [])
        if item.get("item_id") is None and item.get("url")
    }
    if not dead_ids and not dead_urls_only:
        return manifest
    updated = dict(manifest)
    updated["items"] = [
        entry for entry in manifest.get("items") or []
        if str(entry.get("id")) not in dead_ids
        and entry.get("url") not in dead_urls_only
    ]
    return updated


# ─── 제어 문자 게이트 (계약 W10 사이클7 #4) ──────────────────────────────
# 허용은 `\n`·`\t` 뿐. 그 밖에 category 가 Cc(제어)·Cf(포맷: BOM·ZWSP·LRM·RLO·LRI…)
# ·Zl·Zp 인 코드포인트가 하나라도 있으면 fail-closed 다 — 블랙리스트로는
# U+0080~U+009F·양방향 제어를 계속 놓쳤다. 줄 나눔이 파서마다 달라지면
# "같은 본문, 다른 항목 수" 가 되고, 그 틈으로 항목이 숨는다.
_ALLOWED_CONTROLS = ("\n", "\t")
_BANNED_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")


def _is_banned(char: str) -> bool:
    return (char not in _ALLOWED_CONTROLS
            and unicodedata.category(char) in _BANNED_CATEGORIES)


def control_chars(text) -> List[str]:
    """허용 목록 밖의 문자 (등장 순서, 중복 제거)."""
    found: List[str] = []
    for char in text or "":
        if _is_banned(char) and char not in found:
            found.append(char)
    return found


def control_chars_label(text) -> str:
    """사람이 읽을 표기 (예: `\x0b, \ufeff`)."""
    return ", ".join(repr(char).strip("'") for char in control_chars(text))


def strip_control_chars(text) -> str:
    """허용 목록 밖의 문자를 제거 (composer 가 저장 시 쓴다)."""
    return "".join(char for char in (text or "") if not _is_banned(char))


# ─── 파서 API (전부 blocks.py 로의 위임, 사이클 6 #1 · 계약 W10 사이클7 #3) ──
# 계약 W10 의 호출자(checker·notify·발송기·봇 guard)는 이 이름들을 쓴다. 구현은
# **하나**여야 한다 — 렌더러가 만드는 href 와 검사기가 보는 URL 이 갈리면
# "검사한 곳과 다른 데로 보내는" 링크가 생긴다.
def markdown_links(text) -> List[Tuple[str, str]]:
    """`[표시](href)` 쌍 목록 — href 는 **그대로**(끝 구두점 포함) 돌려준다."""
    return [
        (link.text, link.url)
        for line in (text or "").split("\n")
        for link in blocks_mod.find_links(line)
    ]


def link_matches(line) -> List[blocks_mod.Link]:
    """한 줄의 링크 목록 (렌더러가 치환 좌표까지 쓴다 — `start`·`end`·`raw`)."""
    return blocks_mod.find_links(line or "")


def body_urls(text) -> List[str]:
    """본문에 실린 **모든** URL — 링크 href + 표시문 안의 URL + 산문 베어."""
    return blocks_mod.body_urls(text)


def body_links(markdown_text: str) -> List[str]:
    """본문의 마크다운 링크 href 목록."""
    return blocks_mod.body_link_urls(markdown_text)


def parse_blocks(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Dict]:
    return blocks_mod.parse_blocks(markdown_text, item_sections)


def item_blocks(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Dict]:
    return blocks_mod.item_blocks(markdown_text, item_sections)


def item_block_count(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> int:
    return blocks_mod.item_block_count(markdown_text, item_sections)


def section_block_counts(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> Dict[str, int]:
    return blocks_mod.section_block_counts(markdown_text, item_sections)


def cap_violations(
    markdown_text: str, item_sections: Optional[Sequence[str]] = None
) -> List[Tuple[str, int, int]]:
    return blocks_mod.cap_violations(markdown_text, item_sections)
