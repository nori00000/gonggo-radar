"""다이제스트 승인 게이트 상태 파일 (계약 W10).

상태 파일은 `digests/YYYY-Www.state.json` 하나다. 텔레그램 미리보기·제외·해설·발송이
같은 파일을 읽고 쓴다.

상태 기계 (사이클2 #1): 쓰기는 **전부 잠금 하 read-modify-write** 다. 잠금 없이
읽고 나중에 쓰면 "notify 가 sent 를 draft 로 덮어쓰는" 경합이 생긴다.
잠금은 `fcntl.flock` 이다 (사이클4 #1) — 파일 회수 기반 잠금은 세 사이클 동안
경합이 남았다. flock 은 커널이 배타성을 보장하고 프로세스 종료 시 자동 해제되므로
nonce·stale 회수·pid 검사·유예 시간이 전부 불필요하다.
허용 전이는 TRANSITIONS 가 정본이며, `sending` 은 `sent` 로만 나아간다 —
되돌리는 길은 두 개뿐이다:
  · release_sending() — 사람의 `/digest 해제` (소유자 전용, 사유 로그)
  · revert_sending()  — send_digest 가 "확정적 미발송"(연결 전 실패)을 확인했을 때
notify·보류·해설은 어떤 경우에도 sending 을 풀지 못한다.
"""

import fcntl
import json
import os
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

STATUSES = ("draft", "annotated", "sending", "sent", "held")

# 허용 전이표 (계약 W10 사이클2 #1). 값은 "그 상태에서 저장 가능한 status" 집합이며,
# 자기 자신이 들어 있는 것은 status 를 바꾸지 않는 쓰기(미리보기 기록 등)를 위해서다.
TRANSITIONS = {
    "draft": ("draft", "annotated", "sending", "held"),
    "annotated": ("annotated", "sending", "held"),
    "sending": ("sending", "sent"),
    "sent": ("sent",),
    # 사이클3 #5: 보류에서 돌아오는 길 — `/digest 재검토` 는 draft, 해설은 annotated.
    "held": ("held", "draft", "annotated"),
}
# sending 을 sent 아닌 곳으로 되돌리는 유일한 두 경로(escape=True)에서만 허용한다.
SENDING_ESCAPES = ("draft", "annotated")

# 이 상태에서는 본문 관련 내용(해설·제외 목록)을 바꿀 수 없다.
FROZEN_STATUSES = ("sending", "sent")

# 계약 W10의 상태 파일 스키마 (이 키 집합이 정본).
# homelab-orchestration 의 bin/hq_digest_gate.py STATE_KEYS 와 같아야 한다.
STATE_KEYS = (
    "week",
    "status",
    "excluded_urls",
    "commentary",
    "preview_message_ids",
    "preview_items",
    "approval",
    "rebuild_failed",
    "verification_broken",
    "approved_by",
    "approved_at",
    "sending_at",
    "sent_at",
    "recipients_count",
)

# 미리보기별 항목 목록을 몇 회분까지 보관할지 (오래된 번호 좌표는 버린다)
PREVIEW_ITEMS_MAX = 30

SENDING_REASON = "발송 중/미확정 상태 — 사람 확인 필요 (`/digest 해제 <주차>`)"
NO_APPROVAL_REASON = "승인 세대가 없습니다 — 미리보기를 먼저 보내세요"
STALE_APPROVAL_REASON = "오래된 승인 카드입니다 — 새 미리보기로 다시 승인하세요"
STALE_PREVIEW_BODY_REASON = "미리보기와 본문이 다릅니다 — 재검토 필요"
EXCLUDED_NOT_APPLIED_REASON = "제외 미반영 — 제외한 항목이 본문에 남아 있습니다"
REBUILD_FAILED_REASON = "재조립 실패 상태 — 다시 조립해야 합니다"
VERIFICATION_BROKEN_REASON = "검증 파일 무효화 실패 상태 — `/digest 재검토` 필요"
TOMBSTONE_REASON = "검증 파괴 표식(.broken) 존재 — `/digest 재검토` 성공 전까지 발송 불가"
STALE_CHECK_APPROVAL_REASON = "승인 이후 검증 파일이 바뀌었습니다 — 재검토 필요"

# 승인 세대 id 길이 (callback_data 64바이트 한도 안에 들어가야 한다)
APPROVAL_ID_LEN = 12


class StateError(RuntimeError):
    """상태 파일이 손상돼 게이트 판정을 할 수 없음 (fail-closed)."""


class TransitionError(RuntimeError):
    """허용되지 않은 상태 전이 (fail-closed)."""


def state_path(week: str, out_dir="digests") -> Path:
    """주차 → 상태 파일 경로."""
    return Path(out_dir) / f"{week}.state.json"


def state_path_for_markdown(markdown_path) -> Path:
    """다이제스트 마크다운 경로 → 같은 주차의 상태 파일 경로."""
    markdown_path = Path(markdown_path)
    return markdown_path.with_name(f"{markdown_path.stem}.state.json")


def lock_path(week: str, out_dir="digests") -> Path:
    """주차 → 상태·발송 잠금 파일 경로 (계약 W10 크리틱 #1)."""
    return Path(out_dir) / f"{week}.lock"


def lock_path_for_markdown(markdown_path) -> Path:
    """다이제스트 마크다운 경로 → 같은 주차의 잠금 파일 경로."""
    return Path(markdown_path).with_suffix(".lock")


def tombstone_path(week: str, out_dir="digests") -> Path:
    """검증 파괴 표식 (사이클5 #3). 존재하면 발송·미리보기를 무조건 거부한다."""
    return Path(out_dir) / f"{week}.broken"


def tombstone_path_for_markdown(markdown_path) -> Path:
    return Path(markdown_path).with_suffix(".broken")


def write_tombstone(path, reason: str, now_iso: str) -> None:
    """표식 기록. 실패는 예외로 올린다 — 호출자가 마지막 수단을 택해야 한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{now_iso}\n{reason}\n", encoding="utf-8"
    )


def tombstone_reason(path) -> Optional[str]:
    """표식이 있으면 그 내용(사유). 없으면 None."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or "(사유 미기록)"
    except OSError:
        return "(표식 읽기 실패)"


def clear_tombstone(path) -> bool:
    """표식 제거 — `/digest 재검토` 가 재조립+재검증에 성공했을 때만 부른다."""
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


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
        "preview_items": {},
        "approval": None,
        "rebuild_failed": False,
        "verification_broken": False,
        "approved_by": None,
        "approved_at": None,
        "sending_at": None,
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
    if not isinstance(state.get("preview_items"), dict):
        raise StateError("preview_items가 객체가 아님")
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
    """원자적 저장 (임시 파일 + os.replace). 전이 검증은 apply_state가 한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ─── 상태 기계 ───────────────────────────────────────────────────────────
def can_transition(current: Optional[str], target: Optional[str],
                   escape: bool = False) -> bool:
    """current → target 전이가 허용되는가."""
    allowed = TRANSITIONS.get(current or "draft", ())
    if target in allowed:
        return True
    if escape and current == "sending" and target in SENDING_ESCAPES:
        return True
    return False


def assert_transition(current: Optional[str], target: Optional[str],
                      escape: bool = False) -> None:
    """허용되지 않은 전이면 TransitionError."""
    if not can_transition(current, target, escape=escape):
        raise TransitionError(f"허용되지 않은 상태 전이: {current} → {target}")


def apply_state(path, current: Dict, new: Dict, escape: bool = False) -> Dict:
    """잠금을 쥔 호출자용 — 전이 검증 후 저장."""
    assert_transition((current or {}).get("status"), (new or {}).get("status"),
                      escape=escape)
    save_state(path, new)
    return new


def update_state(state_path_, lock_path_, week: str, mutate,
                 escape: bool = False) -> Dict:
    """flock 하 read-modify-write.

    mutate(state) → 새 상태 (None 이면 쓰지 않는다). 잠금을 쥔 뒤에 **다시 읽으므로**
    "읽고 나서 남이 바꾼 상태를 덮어쓰는" 경합이 생기지 않는다(사이클2 #1).
    """
    handle = acquire_lock(lock_path_)
    try:
        current = load_state(state_path_, week)
        new = mutate(current)
        if new is None:
            return current
        return apply_state(state_path_, current, new, escape=escape)
    finally:
        release_lock(handle)


def can_send(state: Dict) -> Tuple[bool, str]:
    """발송 가능한가. status=sent는 불변이고, sending은 사람 확인 전까지 막는다.

    사이클4 #3·#4: 재조립 실패·검증 무효화 실패 플래그가 있으면 발송하지 않는다.
    """
    status = state.get("status")
    if status == "sent":
        return False, "이미 발송됨"
    if status == "sending":
        return False, SENDING_REASON
    if state.get("verification_broken"):
        return False, VERIFICATION_BROKEN_REASON
    if state.get("rebuild_failed"):
        return False, REBUILD_FAILED_REASON
    return True, ""


def mark_rebuild_failed(state: Dict, failed: bool = True) -> Dict:
    """재조립 실패 표시 (사이클4 #3). 다음 성공 재조립까지 발송·미리보기 거부."""
    updated = dict(state)
    updated["rebuild_failed"] = bool(failed)
    if failed:
        updated["approval"] = None      # 신뢰할 수 없는 본문의 승인은 폐기
    return updated


def mark_verification_broken(state: Dict, broken: bool = True) -> Dict:
    """검증 파일 무효화 실패 표시 (사이클4 #4). `/digest 재검토` 성공 시 해제."""
    updated = dict(state)
    updated["verification_broken"] = bool(broken)
    if broken:
        updated["approval"] = None
    return updated


def excluded_still_present(state: Optional[Dict], item_urls) -> List[str]:
    """제외 목록과 현재 본문 항목 URL 의 교집합 (사이클4 #3).

    비어 있지 않으면 재조립이 반영되지 않은 것이다 — 발송·미리보기를 거부한다.
    """
    excluded = set(excluded_urls(state))
    if not excluded:
        return []
    return [url for url in (item_urls or []) if url in excluded]


def mark_sending(state: Dict, now_iso: str) -> Dict:
    """SMTP 직전에 남기는 표시. 크래시·저장 실패로 남으면 이후 발송을 막는다."""
    updated = dict(state)
    updated["status"] = "sending"
    updated["sending_at"] = now_iso
    return updated


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


def revert_sending(state: Dict) -> Dict:
    """확정적 미발송(연결 전 실패)을 확인한 send_digest 만 쓰는 되돌리기 (사이클2 #4).

    `annotated` 로 되돌린다 — sending 에 도달했다는 것은 마커 게이트를 통과했다는
    뜻이므로(본문 확정) 그 상태가 정확하고, annotated 에서 재발송이 가능하다.
    apply_state(..., escape=True) 로만 저장된다.
    """
    if state.get("status") != "sending":
        raise TransitionError("sending 상태가 아닙니다")
    updated = dict(state)
    updated["status"] = "annotated"
    updated["sending_at"] = None
    return updated


def release_sending(state: Dict) -> Dict:
    """사람의 `/digest 해제` — 미확정 발송 표시를 수동으로 푼다 (사이클2 #1).

    사이클3 판정 + 사이클4 #2: **draft 로 되돌리고 승인 세대를 폐기한다.** 해제 후에는
    새 미리보기·새 카드 없이는 발송할 수 없다(approval 삭제 → 옛 카드 영구 무효).
    사유는 호출자가 로그에 남긴다(상태 파일 스키마는 늘리지 않는다).
    apply_state(..., escape=True) 로만 저장된다.
    """
    if state.get("status") != "sending":
        raise TransitionError("sending 상태가 아닙니다")
    updated = dict(state)
    updated["status"] = "draft"
    updated["sending_at"] = None
    updated["approval"] = None      # 옛 카드 영구 무효 (사이클4 #2)
    return updated


def mark_draft(state: Dict) -> Dict:
    """`/digest 재검토` — 보류를 되살린다 (held→draft, 사이클3 #5)."""
    status = state.get("status")
    if status in FROZEN_STATUSES:
        raise TransitionError(f"{status} 상태에서는 재검토로 되돌릴 수 없습니다")
    updated = dict(state)
    updated["status"] = "draft"
    return updated


def mark_held(state: Dict) -> Dict:
    """보류 표시. 발송된 주는 그대로 두고, 미확정(sending)은 거부한다."""
    status = state.get("status")
    if status == "sending":
        raise TransitionError(
            "발송 결과가 미확정입니다 — 보류로 바꿀 수 없습니다 (`/digest 해제 <주차>`)"
        )
    updated = dict(state)
    if status != "sent":
        updated["status"] = "held"
    return updated


def mark_annotated(state: Dict, commentary: str) -> Dict:
    """해설 적용을 기록한 새 상태를 반환 (발송 중·완료 주차는 거부)."""
    status = state.get("status")
    if status in FROZEN_STATUSES:
        raise TransitionError(f"{status} 상태에서는 해설을 바꿀 수 없습니다")
    updated = dict(state)
    updated["commentary"] = commentary
    updated["status"] = "annotated"
    return updated


def add_excluded_urls(state: Dict, urls) -> Dict:
    """제외 URL을 중복 없이 덧붙인 새 상태 (발송 중·완료 주차는 거부)."""
    status = state.get("status")
    if status in FROZEN_STATUSES:
        raise TransitionError(f"{status} 상태에서는 항목을 제외할 수 없습니다")
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


def new_approval_id() -> str:
    """승인 세대 id (uuid4 앞 12자) — callback_data 에 실린다."""
    return uuid.uuid4().hex[:APPROVAL_ID_LEN]


def record_preview(state: Dict, message_ids, item_urls=None,
                   approval_sha: Optional[str] = None,
                   check_sha: Optional[str] = None) -> Dict:
    """이번 미리보기의 message_id·항목 URL·**새 승인 세대**를 기록.

    **status 는 절대 건드리지 않는다** (사이클2 #1) — 미리보기 전송이 발송 상태를
    덮어쓰면 재발송이 열린다. 호출자는 잠금 하 read-modify-write 로 써야 한다.

    사이클4 #2: 새 미리보기는 **이전 승인 세대를 폐기**하고 새 id 를 발급한다.
    승인 카드의 callback_data 는 이 id 를 싣고, 발송기는 id 와 전체 SHA 를 함께
    확인한다 — 접두 충돌로 옛 카드가 되살아나는 길을 없앤다.

    사이클5 #2: 세대에 **검증 결속**(check_sha = 그 시점 check.json 바이트의 SHA)도
    싣는다. 본문 SHA 가 같아도 렌더에 쓴 검증이 바뀌면(분류 개편으로 0건 → 수정 후
    pass) 세대가 무효가 된다.
    """
    updated = dict(state)
    ids = [int(mid) for mid in message_ids]
    updated["preview_message_ids"] = ids
    items = dict(updated.get("preview_items") or {})
    urls = list(item_urls or [])
    for mid in ids:
        items.pop(str(mid), None)     # 재기록 시 순서를 최신으로
        items[str(mid)] = urls
    if len(items) > PREVIEW_ITEMS_MAX:
        for key in list(items)[: len(items) - PREVIEW_ITEMS_MAX]:
            items.pop(key)
    updated["preview_items"] = items
    updated["approval"] = (
        {
            "id": new_approval_id(),
            "sha": str(approval_sha),
            "check_sha": str(check_sha) if check_sha else None,
            "card_message_id": None,
        }
        if approval_sha
        else None
    )
    return updated


def record_card(state: Dict, card_message_id) -> Dict:
    """이번 승인 세대에 카드 message_id 를 붙인다 (status 는 건드리지 않는다)."""
    approval = state.get("approval")
    if not isinstance(approval, dict):
        return dict(state)
    updated = dict(state)
    updated["approval"] = dict(
        approval,
        card_message_id=(
            int(card_message_id) if card_message_id is not None else None
        ),
    )
    return updated


def clear_approval(state: Dict) -> Dict:
    """승인 세대 폐기 — 옛 카드는 영구 무효가 된다 (사이클4 #2)."""
    updated = dict(state)
    updated["approval"] = None
    return updated


def approval_of(state: Optional[Dict]) -> Dict:
    """승인 세대 dict (없으면 빈 dict)."""
    approval = (state or {}).get("approval")
    return approval if isinstance(approval, dict) else {}


def card_message_id(state: Optional[Dict]):
    """현재 승인 카드의 message_id (없으면 None)."""
    return approval_of(state).get("card_message_id")


def check_approval(state: Optional[Dict], approval_id: str,
                   current_sha: str,
                   current_check_sha: Optional[str] = None) -> Tuple[bool, str]:
    """승인 세대 검증 (사이클4 #2 · 사이클5 #2). (ok, 거부 사유).

    id 와 **전체 64자 본문 SHA** 를 확인하고, current_check_sha 가 오면
    **검증 파일 바이트 SHA** 까지 대조한다 — 접두 비교는 폐지했다.
    """
    approval = approval_of(state)
    if not approval.get("id"):
        return False, NO_APPROVAL_REASON
    if approval.get("id") != approval_id:
        return False, STALE_APPROVAL_REASON
    if approval.get("sha") != current_sha:
        return False, STALE_PREVIEW_BODY_REASON
    if current_check_sha is not None:
        if not approval.get("check_sha"):
            return False, STALE_CHECK_APPROVAL_REASON
        if approval["check_sha"] != current_check_sha:
            return False, STALE_CHECK_APPROVAL_REASON
    return True, ""


def preview_urls(state: Optional[Dict], message_id=None) -> Optional[List[str]]:
    """그 미리보기가 보여준 항목 URL 목록. 기록이 없으면 None."""
    items = (state or {}).get("preview_items") or {}
    if message_id is not None:
        urls = items.get(str(message_id))
        return list(urls) if isinstance(urls, list) else None
    latest = (state or {}).get("preview_message_ids") or []
    for mid in reversed(latest):
        urls = items.get(str(mid))
        if isinstance(urls, list):
            return list(urls)
    return None


# ─── 잠금 (fcntl.flock — 커널이 배타성을 보장한다) ──────────────────────
class LockBusy(RuntimeError):
    """같은 주차의 발송·상태 쓰기가 이미 진행 중 (flock 이 다른 프로세스에 있다)."""


class LockHandle:
    """flock 을 쥔 파일 디스크립터 (사이클4 #1).

    잠금 파일은 **삭제하지 않는다** — 삭제하면 같은 경로를 새로 만든 프로세스가
    다른 inode 에 flock 을 걸어 배타성이 깨진다. 파일은 남고 잠금만 오간다.
    프로세스가 죽으면 커널이 flock 을 자동 해제하므로 stale 회수 로직이 필요 없다.
    """

    __slots__ = ("path", "fd")

    def __init__(self, path, fd):
        self.path = Path(path)
        self.fd = fd

    def __repr__(self):     # pragma: no cover - 진단용
        return f"LockHandle({self.path}, fd={self.fd}, pid={os.getpid()})"


def acquire_lock(path) -> LockHandle:
    """발송·상태 잠금 (`flock(LOCK_EX|LOCK_NB)`). 실패는 즉시 LockBusy — 재시도 없음.

    파일 회수·nonce·pid 검사를 전부 버렸다 (사이클4 #1): 커널이 배타성을 보장하고,
    보유 프로세스가 죽으면 잠금이 즉시 풀린다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise LockBusy(f"잠금이 사용 중입니다: {path}") from exc
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    except OSError:
        pass        # 진단용 기록일 뿐 — 소유권은 flock 이 증명한다
    return LockHandle(path, fd)


def release_lock(handle) -> bool:
    """flock 해제 + fd 닫기. 잠금 파일은 삭제하지 않는다."""
    if handle is None:
        return False
    try:
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(handle.fd)
    except OSError:
        return False
    return True
