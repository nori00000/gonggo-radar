"""다이제스트 승인 게이트 상태 파일 (계약 W10).

상태 파일은 `digests/YYYY-Www.state.json` 하나다. 텔레그램 미리보기·제외·해설·발송이
같은 파일을 읽고 쓴다.

상태 기계 (사이클2 #1): 쓰기는 **전부 잠금 하 read-modify-write** 다. 잠금 없이
읽고 나중에 쓰면 "notify 가 sent 를 draft 로 덮어쓰는" 경합이 생긴다.
허용 전이는 TRANSITIONS 가 정본이며, `sending` 은 `sent` 로만 나아간다 —
되돌리는 길은 두 개뿐이다:
  · release_sending() — 사람의 `/digest 해제` (소유자 전용, 사유 로그)
  · revert_sending()  — send_digest 가 "확정적 미발송"(연결 전 실패)을 확인했을 때
notify·보류·해설은 어떤 경우에도 sending 을 풀지 못한다.
"""

import json
import os
import time
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
    "preview_sha",
    "card_message_id",
    "approved_by",
    "approved_at",
    "sending_at",
    "sent_at",
    "recipients_count",
)

# 미리보기별 항목 목록을 몇 회분까지 보관할지 (오래된 번호 좌표는 버린다)
PREVIEW_ITEMS_MAX = 30

SENDING_REASON = "발송 중/미확정 상태 — 사람 확인 필요 (`/digest 해제 <주차>`)"

# 잠금 회수 규율 (사이클3 #1)
#  · 살아 있는 pid 의 잠금은 **나이와 무관하게 회수하지 않는다**. 오래 쥐고 있으면
#    경고만 한다(자동 회수가 곧 중복 발송의 문이었다).
#  · 빈/부분 파일(JSON 쓰기 전)은 생성 후 유예 시간이 지난 뒤에만 회수한다.
LOCK_PARTIAL_GRACE_SECONDS = 5.0
LOCK_WARN_SECONDS = 3600.0
# 회수 자체를 직렬화하는 마커의 수명 (회수 중 크래시가 영구 차단이 되지 않게)
LOCK_RECLAIM_MARKER_TTL = 30.0


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
        "preview_sha": None,
        "card_message_id": None,
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
                 on_reclaim=None, escape: bool = False, on_warn=None) -> Dict:
    """잠금 하 read-modify-write.

    mutate(state) → 새 상태 (None 이면 쓰지 않는다). 잠금을 쥔 뒤에 **다시 읽으므로**
    "읽고 나서 남이 바꾼 상태를 덮어쓰는" 경합이 생기지 않는다(사이클2 #1).
    """
    handle = acquire_lock(lock_path_, on_reclaim=on_reclaim, on_warn=on_warn)
    try:
        current = load_state(state_path_, week)
        new = mutate(current)
        if new is None:
            return current
        return apply_state(state_path_, current, new, escape=escape)
    finally:
        release_lock(handle)


def can_send(state: Dict) -> Tuple[bool, str]:
    """발송 가능한가. status=sent는 불변이고, sending은 사람 확인 전까지 막는다."""
    status = state.get("status")
    if status == "sent":
        return False, "이미 발송됨"
    if status == "sending":
        return False, SENDING_REASON
    return True, ""


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

    사유는 호출자가 로그에 남긴다(상태 파일 스키마는 늘리지 않는다).
    apply_state(..., escape=True) 로만 저장된다.
    """
    return revert_sending(state)


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


def record_preview(state: Dict, message_ids, item_urls=None,
                   preview_sha: Optional[str] = None) -> Dict:
    """이번 미리보기의 message_id 목록 + 그 미리보기가 보여준 항목 URL을 기록.

    **status 는 절대 건드리지 않는다** (사이클2 #1) — 미리보기 전송이 발송 상태를
    덮어쓰면 재발송이 열린다. 호출자는 잠금 하 read-modify-write 로 써야 한다.
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
    if preview_sha is not None:
        # 사이클3 #3: 사람이 **본** 본문의 지문. 승인 카드는 이 값으로 발급되고,
        # 발송 게이트는 approved-sha·현재 본문 해시·이 값 셋의 일치를 요구한다.
        updated["preview_sha"] = str(preview_sha)
    updated["card_message_id"] = None   # 새 미리보기 → 이전 승인 카드는 무효
    return updated


def record_card(state: Dict, card_message_id) -> Dict:
    """이번에 띄운 승인 카드의 message_id (status 는 건드리지 않는다)."""
    updated = dict(state)
    updated["card_message_id"] = (
        int(card_message_id) if card_message_id is not None else None
    )
    return updated


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


# ─── 잠금 (소유권 nonce + 원자적 stale 회수) ────────────────────────────
class LockBusy(RuntimeError):
    """같은 주차의 발송·상태 쓰기가 이미 진행 중 (살아 있는 잠금)."""


class LockHandle:
    """획득한 잠금. nonce 가 소유권 증명이다 (사이클3 #1).

    release 는 파일에 적힌 nonce 가 **내 것일 때만** 삭제한다 — 회수 경합 후
    남의 잠금을 지워 두 프로세스가 동시에 발송에 들어가는 경로를 닫는다.
    """

    __slots__ = ("path", "fd", "nonce")

    def __init__(self, path, fd, nonce):
        self.path = Path(path)
        self.fd = fd
        self.nonce = nonce

    def __repr__(self):     # pragma: no cover - 진단용
        return f"LockHandle({self.path}, pid={os.getpid()}, nonce={self.nonce[:8]})"


def _lock_holder(path) -> Tuple[Optional[int], Optional[str], Optional[float]]:
    """잠금 파일의 (pid, nonce, started_at). 못 읽으면 (None, None, None)."""
    try:
        with open(str(path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    pid = data.get("pid")
    nonce = data.get("nonce")
    started = data.get("started_at")
    return (
        pid if isinstance(pid, int) else None,
        nonce if isinstance(nonce, str) and nonce else None,
        float(started) if isinstance(started, (int, float)) else None,
    )


def _pid_alive(pid: int) -> bool:
    """pid 생존 확인. 권한 문제로 못 보내면 살아 있다고 본다."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _file_age(path) -> float:
    try:
        return max(0.0, time.time() - os.path.getmtime(str(path)))
    except OSError:
        return 0.0


def stale_reason(
    path,
    partial_grace: float = LOCK_PARTIAL_GRACE_SECONDS,
) -> Optional[str]:
    """잠금을 회수해도 되는 이유. 회수하면 안 되면 None (사이클3 #1).

    살아 있는 pid 의 잠금은 나이와 무관하게 회수하지 않는다. 빈/부분 파일은
    생성 직후 유예를 준다(O_EXCL 생성과 JSON 쓰기 사이의 창을 회수하지 않기 위해).
    """
    pid, _nonce, _started = _lock_holder(path)
    if pid is None:
        age = _file_age(path)
        if age < partial_grace:
            return None
        return f"잠금 파일 손상/미완성(생성 후 {age:.0f}초)"
    if not _pid_alive(pid):
        return f"보유 프로세스 없음(pid={pid})"
    return None


def long_held_warning(path, warn_seconds: float = LOCK_WARN_SECONDS) -> Optional[str]:
    """살아 있는 pid 가 오래 쥐고 있다 — 경고만 한다(회수 금지, 사이클3 #1)."""
    pid, _nonce, started = _lock_holder(path)
    if pid is None or not _pid_alive(pid) or started is None:
        return None
    held = time.time() - started
    if held <= warn_seconds:
        return None
    return f"잠금을 오래 보유 중(pid={pid}, {int(held)}초) — 회수하지 않음"


def _create_lock(path) -> Optional[LockHandle]:
    """O_EXCL 생성 + 소유권 기록. 이미 있으면 None."""
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return None
    nonce = uuid.uuid4().hex
    try:
        os.write(fd, json.dumps({
            "pid": os.getpid(), "nonce": nonce, "started_at": time.time(),
        }).encode("utf-8") + b"\n")
    except OSError:
        pass
    return LockHandle(path, fd, nonce)


def _claim_reclaim_marker(marker, ttl: float = LOCK_RECLAIM_MARKER_TTL):
    """회수 권한을 O_EXCL 로 한 명에게만 준다. 못 얻으면 None.

    회수 중 크래시로 남은 마커는 ttl 이 지나면 치운다.
    """
    for attempt in (1, 2):
        try:
            return os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt == 2 or _file_age(marker) <= ttl:
                return None
            try:
                os.unlink(str(marker))
            except OSError:
                return None
        except OSError:
            return None
    return None


def acquire_lock(path, on_reclaim=None, on_warn=None,
                 partial_grace: float = LOCK_PARTIAL_GRACE_SECONDS) -> LockHandle:
    """발송·상태 잠금. 반환은 LockHandle (release_lock 에 그대로 넘긴다).

    stale 회수는 **원자적 rename** 으로 한 명만 이긴다 (사이클3 #1):
    `.lock` → `.lock.stale-<ts>` 로 옮긴 쪽만 재생성을 시도하고, rename 에 실패한
    쪽은 포기한다. 이전 구현은 두 회수자가 각각 unlink+create 해서 **둘 다** 잠금을
    쥐었다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    handle = _create_lock(path)
    if handle is not None:
        return handle

    reason = stale_reason(path, partial_grace)
    if reason is None:
        warning = long_held_warning(path)
        if warning and on_warn is not None:
            on_warn(warning)
        pid, _nonce, _started = _lock_holder(path)
        raise LockBusy(
            f"잠금이 살아 있습니다: {path}"
            + (f" (pid={pid})" if pid is not None else " (내용 확인 불가)")
        )

    # 회수는 한 명만 한다. 회수 마커(O_EXCL)가 회수 구간 전체를 직렬화한다 —
    # 마커 없이 rename 만 쓰면 "A 회수·재생성 → B 가 A 의 **새** 잠금을 rename" 으로
    # 둘 다 잠금을 쥔다(Codex 사이클3 #1 재현). 마커 안에서 관측값을 재확인한다.
    observed = _lock_holder(path)
    marker = path.with_name(path.name + ".reclaim")
    marker_fd = _claim_reclaim_marker(marker)
    if marker_fd is None:
        raise LockBusy(f"잠금 회수 경합에서 밀렸습니다: {path}")

    try:
        if _lock_holder(path) != observed:
            # 우리가 본 잠금이 아니다 (그 사이 회수·재생성됐다) → 포기.
            raise LockBusy(f"회수 중 잠금이 교체되었습니다: {path}")

        stale_path = path.with_name(
            f"{path.name}.stale-{int(time.time() * 1000)}-{os.getpid()}"
        )
        try:
            os.rename(str(path), str(stale_path))
        except OSError as exc:
            # rename 에 실패한 쪽은 포기한다.
            raise LockBusy(f"잠금 회수 경합에서 밀렸습니다: {path}") from exc

        if on_reclaim is not None:
            on_reclaim(reason)

        handle = _create_lock(path)
        if handle is None:
            raise LockBusy(f"회수 직후 다른 프로세스가 잠금을 잡았습니다: {path}")
        return handle
    finally:
        try:
            os.close(marker_fd)
        except OSError:
            pass
        try:
            os.unlink(str(marker))
        except OSError:
            pass


def release_lock(handle: Optional[LockHandle]) -> bool:
    """내 잠금만 해제한다. 남의 잠금(nonce 불일치)은 건드리지 않는다.

    Returns:
        실제로 삭제했으면 True.
    """
    if handle is None:
        return False
    try:
        os.close(handle.fd)
    except OSError:
        pass
    _pid, nonce, _started = _lock_holder(handle.path)
    if nonce is not None and nonce != handle.nonce:
        # 회수 경합 뒤 남의 잠금이 들어섰다 — 지우면 그쪽이 무방비가 된다.
        return False
    try:
        os.unlink(str(handle.path))
    except OSError:
        return False
    return True
