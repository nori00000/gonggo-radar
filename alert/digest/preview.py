"""텔레그램 미리보기 렌더 (계약 W10 + 형식 v2.1).

`digests/YYYY-Www.md`를 파싱해 협의회 토픽에 보낼 평문 미리보기를 만든다.
파싱·렌더는 순수 함수다 — 네트워크·파일 IO는 scripts/notify_digest.py가 한다.

판정 ⑦(편집자 UX 10분 한도): 미리보기는 **발송본 그대로** 보여준다. 선정 이유는
표시하지 않는다(대상 태그가 이유다). 맨 아래에 "보류 N건" 한 줄만 덧붙인다.

항목 번호는 **문서 순서 1..N**이다. 이 번호가 봇의 `제외 2,5` 명령이 URL을 찾는
유일한 좌표이므로, 번호 규칙은 마크다운의 항목 등장 순서와 언제나 같아야 한다.
그래서 항목 판정은 `alert.digest.blocks` 하나만 쓴다 (사이클 6 #1) — 미리보기가
자기 파서로 항목을 세면 게이트의 계수와 조용히 갈라진다. 어떤 섹션이 항목 섹션인가는
`alert.digest.sections` 가 정본이고 판정은 **정확 일치**다 (계약 W10 사이클3 #7) —
check.json 의 `item_sections` 를 최우선으로 본다.
"""

import re
from typing import Dict, List, Optional

from alert.digest import blocks as blocks_mod
from alert.digest import sections as sections_mod
from alert.digest.composer import (
    HEADING_TO_SECTION,
    ITEM_SECTIONS,
    KAKAO_CHUNK_LIMIT,
    MARKER,
    URL_TOO_LONG_NOTICE,
    chunk_plaintext,
    fit_prose_urls,
)

__all__ = [
    "MARKER",
    "TELEGRAM_LIMIT",
    "ITEM_SECTIONS",
    "item_urls",
    "parse_digest",
    "status_line",
    "render_preview",
    "chunk_text",
]

TELEGRAM_LIMIT = KAKAO_CHUNK_LIMIT

_TITLE_RE = re.compile(r"^#\s+(.+)$")
_HEADLINE_RE = re.compile(r"^이번 주 한 줄:\s*(.*)$")
_PERIOD_IN_TITLE_RE = re.compile(r"\(([^()]*~[^()]*)\)\s*$")
_HOLD_RE = re.compile(
    r"^<!--\s*보류:\s*(\d+)\.\s*(.*?)\s*\|\s*(.*?)\s*"
    r"(?:\|\s*id=(\d+)\s*)?-->$"
)
_LANE_RE = re.compile(r"^<!--\s*lane:")


def item_urls(markdown_text: str, item_sections=None) -> List[str]:
    """문서 순서대로 항목 URL만 뽑는다 (번호 → URL 좌표의 정본).

    `item_sections` 는 check.json 이 기록한 항목 섹션 헤딩 목록이다 (정확 일치).
    """
    return blocks_mod.item_urls(markdown_text, item_sections)


def parse_digest(markdown_text: str, item_sections=None) -> Dict:
    """미리보기에 필요한 만큼만 파싱.

    Returns:
        {"title": str, "period": str, "sections": [{"name", "heading", "items"}],
         "items": [{"number", "section", "raw", "label", "title", "author",
                    "target", "deadline", "url"}],
         "holds": [{"number", "title", "reason"}],
         "has_marker": bool, "headline": str, "commentary": str}

        `commentary` 는 협의회 의견 섹션(정확 일치 — sections.OPINION_SECTIONS)의
        본문이다. `headline` 은 "이번 주 한 줄"이며 둘은 서로 다른 자리다.
    """
    title = ""
    period = ""
    headline = ""
    sections: List[Dict] = []
    items: List[Dict] = []
    holds: List[Dict] = []
    commentary_lines: List[str] = []
    section_by_heading: Dict[str, Dict] = {}

    # 계약 W10 사이클6 #2(splitlines 금지)는 blocks.py 가 이미 충족한다 —
    # 파서는 split("\n") 하나뿐이고, 제어 문자는 checker 가 fail-closed 로 막는다.
    for block in blocks_mod.parse_blocks(markdown_text, item_sections):
        if block["kind"] == "comment":
            matched = _HOLD_RE.match(block["lines"][0].strip())
            if matched:
                holds.append({
                    "number": int(matched.group(1)),
                    "title": matched.group(2),
                    "reason": matched.group(3),
                    "id": int(matched.group(4)) if matched.group(4) else None,
                })
            continue

        if block["kind"] == "section":
            if not block["in_item_section"]:
                continue
            heading = block["name"]
            current = {
                "name": HEADING_TO_SECTION.get(heading, heading),
                "heading": heading,
                "items": [],
            }
            sections.append(current)
            section_by_heading[heading] = current
            continue

        if block["kind"] == "item":
            fields = block["fields"]
            item = {
                "number": len(items) + 1,
                "section": HEADING_TO_SECTION.get(block["section"],
                                                  block["section"]),
                "raw": fields["raw"],
                "label": fields["label"],
                "title": fields["title"],
                "author": fields["author"],
                "target": fields["target"],
                "deadline": fields["deadline"],
                "url": block["url"],
            }
            items.append(item)
            owner = section_by_heading.get(block["section"])
            if owner is not None:
                owner["items"].append(item)
            continue

        is_opinion = sections_mod.is_opinion_section(block["section"])
        for raw in block["lines"]:
            line = raw.strip()
            if not title:
                matched = _TITLE_RE.match(line)
                if matched:
                    title = matched.group(1).strip()
                    in_title = _PERIOD_IN_TITLE_RE.search(title)
                    if in_title:
                        period = in_title.group(1).strip()
                    continue
            if not headline:
                matched = _HEADLINE_RE.match(line)
                if matched:
                    headline = matched.group(1).strip()
                    continue
            # 사이클3 #7: 협의회 의견 섹션은 정확 일치로 알아본다.
            if is_opinion and line and not line.startswith("<!--"):
                commentary_lines.append(line)

    return {
        "title": title,
        "period": period,
        "sections": sections,
        "items": items,
        "holds": holds,
        "has_marker": MARKER in markdown_text,
        "headline": headline,
        "commentary": "\n".join(commentary_lines),
    }


def status_line(parsed: Dict, check_pass: bool) -> str:
    """상태줄 — 한 줄이 아직 없으면 "해설 대기", 있고 검증 통과면 "발송 가능"."""
    if parsed.get("has_marker"):
        return "상태: 해설 대기 (이번 주 한 줄 미확정)"
    if not check_pass:
        return "상태: 발송 불가 (팩트 게이트 실패)"
    return "상태: 발송 가능"


USAGE_LINES = (
    "사용법:",
    "  제외 2,5 — 해당 번호 항목을 빼고 다시 조립",
    "  /digest 보류 — 보류 목록 펼치기",
    "  핀 n — 보류 항목을 발송본으로 승격",
    "  상단: <한 줄> — 이번 주 한 줄 확정",
    "  해설: <본문> — 협의회 의견을 채우고 다시 검증",
    "  /digest 상태 — 현재 상태 · /digest 재검토 — 파일 수정 후 재검증",
)


def render_preview(
    week: str,
    markdown_text: str,
    check: Optional[Dict] = None,
) -> str:
    """미리보기 본문 전체 (분할 전).

    개정 v2.5 (#7): **발송본을 그대로** 렌더한다 — 협의회에서·회원사 소식·여러 줄 해설까지
    포함한다. 편집자가 미리보기에서 보지 못한 내용이 이메일로 나가면 승인 게이트가 거짓이 된다.
    HTML 주석(레인 표기·보류 목록)만 걷어내고, 항목 줄에는 `제외 n` 좌표를 붙인다.
    """
    check = check or {}
    # 섹션 정본은 check.json 이 기록한 목록이다 (정확 일치 — 사이클3 #7).
    item_sections, _ = sections_mod.resolve(check, markdown_text)
    parsed = parse_digest(markdown_text, item_sections)
    check_pass = bool(check.get("pass"))
    dropped = check.get("dropped") or []

    lines = [
        f"🏛 협의회 주간 정책브리핑 {week}",
        "기간 {} · 검증 {} · 항목 {}건{}".format(
            parsed["period"] or "미상",
            "pass" if check_pass else "fail",
            len(parsed["items"]),
            f" · 죽은 URL 제외 {len(dropped)}건" if dropped else "",
        ),
    ]
    if not check_pass and check.get("reason"):
        lines.append(f"검증 실패 사유: {check['reason']}")
    lines.append("")

    # 항목 번호는 블록 파서가 정한 순서로만 붙인다 (문자열 일치 추측 금지 —
    # 제목이 우연히 같은 두 항목이 있으면 추측이 엉뚱한 번호를 붙인다).
    document = blocks_mod.parse_blocks(markdown_text, item_sections)
    skipped_title = False
    numbers: Dict[int, int] = {}
    for index, block in enumerate(document):
        if block["kind"] == "item":
            numbers[index] = len(numbers) + 1

    for index, block in enumerate(document):
        if block["kind"] == "comment":
            # 주석(레인 표기·보류 목록)은 걷어내고 뒤따르던 빈 줄만 남긴다
            lines.extend("" for _ in block["lines"][1:])
            continue
        if block["kind"] == "item":
            lines.append(f"{numbers[index]}. {block['title']}")
            # 사이클 7·8 (#9·#4): 한 조각에 들어갈 수 없는 URL 은 표기로 대체한다 —
            # 미리보기도 오버사이즈 조각을 만들지 않는다(발송 경로와 같은 규칙).
            lines.append(
                f"   {block['url']}"
                if len(block["url"]) + 4 <= TELEGRAM_LIMIT
                else f"   {URL_TOO_LONG_NOTICE}"
            )
            lines.extend(
                "" for line in block["lines"] if not line.strip()
            )
            continue
        for raw in fit_prose_urls("\n".join(block["lines"]),
                                  TELEGRAM_LIMIT)[0].split("\n"):
            line = raw.strip()
            if _HOLD_RE.match(line) or _LANE_RE.match(line):
                continue
            # 사이클 13 #5: 생략하는 `# ` 는 **문서 제목 하나**뿐이다. 해설·회원사
            # 소식에 사람이 쓴 `# ` 줄은 카톡에는 실리고 미리보기에서만 사라져,
            # 편집자가 보지 못한 문장이 메일로 나갔다(승인 게이트가 거짓이 된다).
            if block["kind"] == "head" and line.startswith("# ") and not skipped_title:
                skipped_title = True
                continue
            if line.startswith("# "):
                # 카톡과 같은 텍스트로 — 카톡 렌더도 `#` 표기만 걷어낸다
                lines.append(line[2:].strip())
                continue
            if line.startswith("## "):
                lines.append(f"■ {line[3:].strip()}")
                continue
            lines.append(line)

    while lines and not lines[-1]:
        lines.pop()
    lines.append("")

    if parsed["holds"]:
        lines.append(f"보류 {len(parsed['holds'])}건 (핀 n으로 승격)")
        lines.append("")

    lines.append(status_line(parsed, check_pass))
    lines.append("")
    lines.extend(USAGE_LINES)
    return "\n".join(lines)


def chunk_text(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """텔레그램 한도(4096자)로 분할. 분할 규칙 정본은 composer.chunk_plaintext다 (#12)."""
    return chunk_plaintext(text, limit)
