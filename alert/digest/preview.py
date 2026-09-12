"""텔레그램 미리보기 렌더 (계약 W10 + 형식 v2.1).

`digests/YYYY-Www.md`를 파싱해 협의회 토픽에 보낼 평문 미리보기를 만든다.
파싱·렌더는 순수 함수다 — 네트워크·파일 IO는 scripts/notify_digest.py가 한다.

판정 ⑦(편집자 UX 10분 한도): 미리보기는 **발송본 그대로** 보여준다. 선정 이유는
표시하지 않는다(대상 태그가 이유다). 맨 아래에 "보류 N건" 한 줄만 덧붙인다.

항목 번호는 **문서 순서 1..N**이다. 이 번호가 봇의 `제외 2,5` 명령이 URL을 찾는
유일한 좌표이므로, 번호 규칙은 마크다운의 항목 등장 순서와 언제나 같아야 한다.
"""

import re
from typing import Dict, List, Optional

from alert.digest.composer import (
    HEADING_TO_SECTION,
    ITEM_SECTIONS,
    KAKAO_CHUNK_LIMIT,
    MARKER,
    chunk_plaintext,
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

# 항목 링크 줄: 줄 전체가 마크다운 링크 하나
_ITEM_LINK_RE = re.compile(r"^\[([^\]]+)\]\((\S+)\)$")
# 항목 본문 줄: `[라벨] 제목 — 기관 · 대상: … · 마감 …`
_ITEM_LINE_RE = re.compile(
    r"^(?:\[(?P<label>[^\]]{1,24})\]\s*)?(?P<title>.+?)\s+—\s+(?P<rest>.+)$"
)


def item_urls(markdown_text: str) -> List[str]:
    """문서 순서대로 항목 URL만 뽑는다 (번호 → URL 좌표의 정본)."""
    return [item["url"] for item in parse_digest(markdown_text)["items"]]


def _segment(rest: str, prefixes) -> str:
    """`· `로 나뉜 꼬리에서 주어진 접두사로 시작하는 조각을 찾는다."""
    for chunk in rest.split(" · ")[1:]:
        chunk = chunk.strip()
        for prefix in prefixes:
            if chunk.startswith(prefix):
                return chunk
    return ""


def parse_digest(markdown_text: str) -> Dict:
    """미리보기에 필요한 만큼만 파싱.

    Returns:
        {"title": str, "period": str, "sections": [{"name", "heading", "items"}],
         "items": [{"number", "section", "raw", "label", "title", "author",
                    "target", "deadline", "url"}],
         "holds": [{"number", "title", "reason"}],
         "has_marker": bool, "headline": str, "commentary": str}
    """
    title = ""
    period = ""
    headline = ""
    sections: List[Dict] = []
    items: List[Dict] = []
    holds: List[Dict] = []
    current_section: Optional[Dict] = None
    pending: Optional[Dict] = None

    for raw in markdown_text.splitlines():
        line = raw.strip()

        matched = _HOLD_RE.match(line)
        if matched:
            holds.append({
                "number": int(matched.group(1)),
                "title": matched.group(2),
                "reason": matched.group(3),
                "id": int(matched.group(4)) if matched.group(4) else None,
            })
            continue

        if line.startswith("## "):
            heading = line[3:].strip()
            name = HEADING_TO_SECTION.get(heading, heading)
            pending = None
            if name in ITEM_SECTIONS:
                current_section = {"name": name, "heading": heading, "items": []}
                sections.append(current_section)
            else:
                current_section = None
            continue

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

        if current_section is None:
            continue

        matched = _ITEM_LINK_RE.match(line)
        if matched and pending is not None:
            pending["url"] = matched.group(2).strip()
            pending["number"] = len(items) + 1
            items.append(pending)
            current_section["items"].append(pending)
            pending = None
            continue

        matched = _ITEM_LINE_RE.match(line)
        if matched:
            rest = matched.group("rest")
            pending = {
                "number": 0,
                "section": current_section["name"],
                "raw": line,
                "label": (matched.group("label") or "").strip(),
                "title": matched.group("title").strip(),
                "author": rest.split(" · ")[0].strip(),
                "target": _segment(rest, ("대상:",)),
                "deadline": _segment(rest, ("마감", "의견", "접수")),
                "url": "",
            }
            continue

        pending = None

    has_marker = MARKER in markdown_text
    return {
        "title": title,
        "period": period,
        "sections": sections,
        "items": items,
        "holds": holds,
        "has_marker": has_marker,
        "headline": headline,
        "commentary": "" if has_marker else headline,
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

    pending = list(parsed["items"])
    for raw in markdown_text.splitlines():
        line = raw.strip()
        if _HOLD_RE.match(line) or _LANE_RE.match(line):
            continue
        if line.startswith("# "):
            # 제목은 미리보기 머리에 이미 있다
            continue
        if line.startswith("## "):
            lines.append(f"■ {line[3:].strip()}")
            continue
        if pending and line == pending[0]["raw"]:
            lines.append(f"{pending[0]['number']}. {line}")
            continue
        if pending and line == f"[원문]({pending[0]['url']})":
            lines.append(f"   {pending[0]['url']}")
            pending.pop(0)
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
