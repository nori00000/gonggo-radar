"""다이제스트 승인 게이트 상태 파일 (계약 W10).

상태 파일은 `digests/YYYY-Www.state.json` 하나다. 텔레그램 미리보기·제외·해설·발송이
같은 파일을 읽고 쓴다. 발송이 끝난 주(status=sent)는 불변이며 재발송을 거부한다.
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

STATUSES = ("draft", "annotated", "sent", "held")

# 계약 W10의 상태 파일 스키마 (이 키 집합이 정본)
STATE_KEYS = (
    "week",
    "status",
    "excluded_urls",
    "commentary",
    "preview_message_ids",
    "approved_by",
    "approved_at",
    "sent_at",
    "recipients_count",
)


class StateError(RuntimeError):
    """상태 파일이 손상돼 게이트 판정을 할 수 없음 (fail-closed)."""


def state_path(week: str, out_dir="digests") -> Path:
    """주차 → 상태 파일 경로."""
    return Path(out_dir) / f"{week}.state.json"


def state_path_for_markdown(markdown_path) -> Path:
    """다이제스트 마크다운 경로 → 같은 주차의 상태 파일 경로."""
    markdown_path = Path(markdown_path)
    return markdown_path.with_name(f"{markdown_path.stem}.state.json")


def week_from_markdown(markdown_path) -> str:
    """`digests/2026-W37.md` → `2026-W37`."""
    return Path(markdown_path).stem


def default_state(week: str) -> Dict:
    """초기 상태 (파일이 아직 없을 때)."""
    return {
        "week": week,
        "status": "draft",
        "excluded_urls": [],
        "commentary": "",
        "preview_message_ids": [],
        "approved_by": None,
        "approved_at": None,
        "sent_at": None,
        "recipients_count": 0,
    }


def normalize_state(data, week: str) -> Dict:
    """읽어온 dict에 빠진 키를 기본값으로 채운다 (미지의 키는 보존)."""
    if not isinstance(data, dict):
        raise StateError("상태 파일이 객체가 아님")
    state = default_state(week)
    state.update(data)
    state["week"] = data.get("week") or week
    if state.get("status") not in STATUSES:
        raise StateError(f"알 수 없는 status: {state.get('status')!r}")
    for key in ("excluded_urls", "preview_message_ids"):
        if not isinstance(state.get(key), list):
            raise StateError(f"{key}가 리스트가 아님")
    return state


def load_state(path, week: str) -> Dict:
    """상태 파일 로드. 부재는 기본값, 손상은 StateError(fail-closed)."""
    path = Path(path)
    if not path.exists():
        return default_state(week)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"상태 파일 손상: {exc}") from exc
    return normalize_state(data, week)


def save_state(path, state: Dict) -> None:
    """원자적 저장 (임시 파일 + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def can_send(state: Dict) -> Tuple[bool, str]:
    """발송 가능한가. status=sent는 불변이므로 재발송을 거부한다."""
    status = state.get("status")
    if status == "sent":
        return False, "이미 발송됨"
    return True, ""


def mark_sent(
    state: Dict,
    approved_by,
    recipients_count: int,
    now_iso: str,
) -> Dict:
    """발송 성공을 기록한 새 상태를 반환 (원본은 건드리지 않는다)."""
    updated = dict(state)
    updated["status"] = "sent"
    updated["approved_by"] = approved_by
    updated["approved_at"] = now_iso
    updated["sent_at"] = now_iso
    updated["recipients_count"] = int(recipients_count)
    return updated


def mark_held(state: Dict) -> Dict:
    """보류 상태로 표시한 새 상태를 반환 (발송된 주는 그대로 둔다)."""
    updated = dict(state)
    if updated.get("status") != "sent":
        updated["status"] = "held"
    return updated


def mark_annotated(state: Dict, commentary: str) -> Dict:
    """해설 적용을 기록한 새 상태를 반환."""
    updated = dict(state)
    updated["commentary"] = commentary
    if updated.get("status") != "sent":
        updated["status"] = "annotated"
    return updated


def add_excluded_urls(state: Dict, urls) -> Dict:
    """제외 URL을 중복 없이 덧붙인 새 상태를 반환 (입력 순서 보존)."""
    updated = dict(state)
    existing: List[str] = list(updated.get("excluded_urls") or [])
    for url in urls:
        if url and url not in existing:
            existing.append(url)
    updated["excluded_urls"] = existing
    return updated


def excluded_urls(state: Optional[Dict]) -> List[str]:
    """상태의 제외 URL 목록 (없으면 빈 목록)."""
    if not state:
        return []
    return list(state.get("excluded_urls") or [])


def record_preview(state: Dict, message_ids) -> Dict:
    """이번 미리보기의 message_id 목록을 기록한 새 상태를 반환."""
    updated = dict(state)
    updated["preview_message_ids"] = [int(mid) for mid in message_ids]
    return updated
