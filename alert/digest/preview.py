"""텔레그램 미리보기 렌더 (계약 W10).

`digests/YYYY-Www.md`를 파싱해 협의회 토픽에 보낼 평문 미리보기를 만든다.
파싱·렌더는 순수 함수다 — 네트워크·파일 IO는 scripts/notify_digest.py가 한다.

항목 번호는 **문서 순서 1..N**이다. 이 번호가 봇의 `제외 2,5` 명령이 URL을 찾는
유일한 좌표이므로, 번호 규칙은 마크다운의 `**원문:**` 등장 순서와 언제나 같아야 한다.
"""

import re
from typing import Dict, List, Optional

from alert.digest import prune

MARKER = "<!-- 상민 확정 필요 -->"
TELEGRAM_LIMIT = 4096

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_ORIGIN_RE = re.compile(r"^\*\*원문:\*\*\s*(.+)$")
_AUTHOR_RE = re.compile(r"^\*\*기관:\*\*\s*(.*)$")
_DEADLINE_RE = re.compile(r"^\*\*마감:\*\*\s*(.*)$")
_PERIOD_RE = re.compile(r"^\*\*기간:\*\*\s*(.+)$")
_TITLE_RE = re.compile(r"^#\s+(.+)$")


def item_urls(markdown_text: str) -> List[str]:
    """문서 순서대로 항목 URL만 뽑는다 (번호 → URL 좌표의 정본).

    항목 섹션 안의 블록만 센다 (사이클2 #5·#6) — 해설의 참고 링크가 번호를
    차지하면 `제외 N` 이 엉뚱한 항목을 지운다.
    """
    return [block["url"] for block in prune.item_blocks(markdown_text)]


def parse_digest(markdown_text: str) -> Dict:
    """미리보기에 필요한 만큼만 파싱.

    Returns:
        {"title": str, "period": str, "sections": [{"name", "items": [...]}],
         "items": [{"number", "section", "title", "author", "deadline", "url"}],
         "has_marker": bool, "commentary": str}
    """
    title = ""
    period = ""
    sections: List[Dict] = []
    items: List[Dict] = []
    current_section: Optional[Dict] = None
    current_item: Optional[Dict] = None
    commentary_lines: List[str] = []
    in_commentary = False

    for raw in markdown_text.splitlines():
        line = raw.strip()

        if line.startswith("## "):
            name = line[3:].strip()
            in_commentary = name == "협의회 의견"
            current_item = None
            if prune.section_key(name) is not None:
                current_section = {"name": name, "items": []}
                sections.append(current_section)
            else:
                current_section = None
            continue

        if line.startswith("### "):
            if current_section is None:
                current_item = None
                continue
            current_item = {
                "number": 0,
                "section": current_section["name"],
                "title": line[4:].strip(),
                "author": "",
                "deadline": "",
                "url": "",
            }
            continue

        if not title:
            matched = _TITLE_RE.match(line)
            if matched and not line.startswith("##"):
                title = matched.group(1).strip()
                continue

        if not period:
            matched = _PERIOD_RE.match(line)
            if matched:
                period = matched.group(1).strip()
                continue

        if in_commentary and line and not line.startswith("<!--"):
            commentary_lines.append(line)
            continue

        if current_item is None:
            continue

        matched = _AUTHOR_RE.match(line)
        if matched:
            current_item["author"] = matched.group(1).strip()
            continue
        matched = _DEADLINE_RE.match(line)
        if matched:
            current_item["deadline"] = matched.group(1).strip()
            continue
        matched = _ORIGIN_RE.match(line)
        if matched:
            link = _LINK_RE.search(matched.group(1))
            current_item["url"] = (
                link.group(2).strip() if link else matched.group(1).strip()
            )
            current_item["number"] = len(items) + 1
            items.append(current_item)
            current_section["items"].append(current_item)
            current_item = None
            continue

    return {
        "title": title,
        "period": period,
        "sections": sections,
        "items": items,
        "has_marker": MARKER in markdown_text,
        "commentary": "\n".join(commentary_lines),
    }


def status_line(parsed: Dict, check_pass: bool) -> str:
    """상태줄 — 해설이 아직 없으면 "해설 대기", 있고 검증 통과면 "발송 가능"."""
    if parsed.get("has_marker"):
        return "상태: 해설 대기 (협의회 의견 미확정)"
    if not check_pass:
        return "상태: 발송 불가 (팩트 게이트 실패)"
    return "상태: 발송 가능"


USAGE_LINES = (
    "사용법:",
    "  제외 2,5 — 해당 번호 항목을 빼고 다시 조립",
    '  해설: <본문> — 협의회 의견을 채우고 다시 검증',
    "  /digest 상태 — 현재 상태 · /digest 재검토 — 파일 수정 후 재검증",
)


def render_preview(
    week: str,
    markdown_text: str,
    check: Optional[Dict] = None,
) -> str:
    """미리보기 본문 전체 (분할 전)."""
    parsed = parse_digest(markdown_text)
    check = check or {}
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

    for section in parsed["sections"]:
        lines.append(f"■ {section['name']}")
        if not section["items"]:
            lines.append("  (항목 없음)")
        for item in section["items"]:
            lines.append(
                "{}. [{}] {} — {} — {}".format(
                    item["number"],
                    item["author"] or "기관 미상",
                    item["title"],
                    item["deadline"] or "마감 미정",
                    item["url"],
                )
            )
        lines.append("")

    if parsed["commentary"]:
        lines.append("■ 협의회 의견")
        lines.append(parsed["commentary"])
        lines.append("")

    lines.append(status_line(parsed, check_pass))
    lines.append("")
    lines.extend(USAGE_LINES)
    return "\n".join(lines)


def chunk_text(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """텔레그램 한도(4096자)로 분할. 줄 경계를 지키고, 한 줄이 한도를 넘으면 자른다."""
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.split("\n"):
        pieces = [line] if len(line) <= limit else [
            line[i:i + limit] for i in range(0, len(line), limit)
        ]
        for piece in pieces:
            extra = len(piece) + (1 if current else 0)
            if size + extra > limit and current:
                chunks.append("\n".join(current))
                current = [piece]
                size = len(piece)
            else:
                current.append(piece)
                size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]
