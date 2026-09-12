"""다이제스트 섹션 헤딩의 정본 (계약 W10 사이클3 #7).

섹션 판정을 **부분 일치**로 하면 `## 협의회에서 — 지원사업 의견` 이 공고 섹션으로
오분류돼 해설이 항목으로 세어지고 삭제 대상이 된다. 그래서 판정은 언제나
**정확한 헤딩 문자열 목록과의 완전 일치**다.

목록의 출처 우선순위:
  1. check.json 의 `item_sections` / `commentary_sections` — weekly_digest(checker)가
     그 본문을 만들 때 기록한 값. 소비자(prune·preview·봇)는 이것을 먼저 본다.
  2. composer 의 상수 — `SECTION_HEADINGS` + `ITEM_SECTIONS` (계약 v2 형태)가 있으면
     그것이 정본이다. 지연 import 로 **읽기만** 한다.
  3. 아래 선언 목록 (계약 v1.2 현재 본문 + 계약 v2 개편본) — 최후 폴백.

composer 가 헤딩을 리터럴로 쓰고 상수가 없는 동안(계약 v1.2) 선언 목록이 정본 역할을
하므로, tests/test_digest_gate.py 의 정합 테스트가 "선언 목록 == 실제 본문의 `##`"
을 매번 확인한다 — 섹션 이름이 바뀌면 조용히 오분류되는 대신 테스트가 깨진다.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

# 계약 v1.2 (현재 main 의 composer 리터럴)
V1_ITEM_SECTIONS = ("산림 정책 동향", "지원사업 공고", "사회연대경제 동향")
V1_COMMENTARY_SECTIONS = ("회원사 동정", "협의회 의견")

# 계약 v2 (독자 행동 축 개편본 — composer.SECTION_HEADINGS 의 값)
V2_ITEM_SECTIONS = ("✅ 신청하세요 (마감순)", "👀 알아두세요")
V2_COMMENTARY_SECTIONS = ("🤝 협의회에서", "🏢 회원사 소식")

# 미리보기가 본문으로 실어야 하는 "협의회 의견" 섹션 (정확 일치)
OPINION_SECTIONS = ("협의회 의견", "🤝 협의회에서", "협의회에서")

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


def _composer():
    """composer 모듈 (없거나 깨지면 None). 지연 로딩 — import 순환을 피한다."""
    try:
        from alert.digest import composer
    except Exception:       # noqa: BLE001 — composer 가 개편 중이어도 판정은 산다
        return None
    return composer


def from_composer() -> Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """composer 상수에서 (항목 헤딩, 산문 헤딩). 상수가 없으면 None."""
    composer = _composer()
    headings_map = getattr(composer, "SECTION_HEADINGS", None) if composer else None
    item_names = getattr(composer, "ITEM_SECTIONS", None) if composer else None
    if not isinstance(headings_map, dict) or not headings_map:
        return None
    if not isinstance(item_names, (list, tuple)) or not item_names:
        return None
    items = tuple(
        str(headings_map[name]) for name in item_names if name in headings_map
    )
    if not items:
        return None
    commentary = tuple(
        str(value) for key, value in headings_map.items()
        if key not in item_names
    )
    return items, commentary


def declared() -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """composer 상수 → 없으면 선언 목록(v1.2 + v2 합집합)."""
    from_mod = from_composer()
    if from_mod is not None:
        return from_mod
    return (
        V1_ITEM_SECTIONS + V2_ITEM_SECTIONS,
        V1_COMMENTARY_SECTIONS + V2_COMMENTARY_SECTIONS,
    )


def _string_list(value) -> Optional[Tuple[str, ...]]:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def from_check(check) -> Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """check.json 이 기록한 (항목 헤딩, 산문 헤딩). 없으면 None."""
    if not isinstance(check, dict):
        return None
    items = _string_list(check.get("item_sections"))
    if items is None:
        return None
    commentary = _string_list(check.get("commentary_sections")) or ()
    return items, commentary


def resolve(check=None, markdown_text=None) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """이 본문에 쓸 (항목 헤딩, 산문 헤딩). 정확 일치 판정용.

    check.json 의 기록이 최우선 — 그 본문을 만든 쪽이 남긴 값이다.
    """
    from_json = from_check(check)
    if from_json is not None:
        return from_json
    item_sections, commentary_sections = declared()
    if markdown_text is None:
        return item_sections, commentary_sections
    # 본문에 실제로 등장한 헤딩만 남긴다 (미지의 헤딩은 산문 취급 = fail-closed)
    present = headings(markdown_text)
    items = tuple(name for name in present if name in item_sections)
    prose = tuple(name for name in present if name not in item_sections)
    return items, prose


def headings(markdown_text: str) -> List[str]:
    """본문의 `## ` 헤딩 문자열을 등장 순서로 (정확한 원문)."""
    found = []
    for line in (markdown_text or "").split("\n"):
        matched = _HEADING_RE.match(line.strip())
        if matched:
            found.append(matched.group(1).strip())
    return found


def classify(markdown_text: str) -> Tuple[List[str], List[str]]:
    """본문의 헤딩을 (항목, 산문)으로 분류 — checker 가 check.json 에 기록할 값."""
    item_sections, _ = declared()
    present = headings(markdown_text)
    items = [name for name in present if name in item_sections]
    prose = [name for name in present if name not in item_sections]
    return items, prose


def caps_by_heading(item_sections: Sequence[str]) -> Dict[str, int]:
    """항목 섹션 헤딩 → 상한 (composer.SECTION_LIMITS 가 정본)."""
    composer = _composer()
    limits = getattr(composer, "SECTION_LIMITS", None) if composer else None
    if not isinstance(limits, dict) or not limits:
        return {}
    heading_to_name = getattr(composer, "HEADING_TO_SECTION", None) if composer else None
    declared_items, _ = declared()
    caps: Dict[str, int] = {}
    for heading in item_sections:
        name = None
        if isinstance(heading_to_name, dict):
            name = heading_to_name.get(heading)
        if name is None and heading in declared_items:
            # 계약 v1.2 의 composer 는 헤딩↔키 매핑이 없다 — **선언된 항목 헤딩**에
            # 대해서만 키 포함 여부로 상한을 찾는다(임의 문자열 오분류 방지).
            name = next((key for key in limits if key and key in heading), None)
        if name is not None and name in limits:
            caps[heading] = int(limits[name])
    return caps


def is_opinion_section(section_name: Optional[str]) -> bool:
    """미리보기가 본문으로 실어야 하는 협의회 의견 섹션인가 (정확 일치)."""
    return (section_name or "").strip() in OPINION_SECTIONS
