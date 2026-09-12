#!/usr/bin/env python3
"""주간 정책브리핑 다이제스트 발송 스크립트."""

import argparse
import html as html_module
import hmac
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple
from urllib.parse import urlparse

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.notifiers.email_sender import UNSENT_STAGES, EmailNotifier
from alert.digest import prune
from alert.digest import state as state_mod
from alert.digest.checker import markdown_sha256
from alert.utils.redact import redact

# 상태 기록(status=sent) 저장 재시도 — 여기서 실패하면 "발송했는데 기록이 없는" 창이 열린다.
STATE_SAVE_ATTEMPTS = 3
STATE_SAVE_BACKOFF = 0.5

# 승인 세대 id 형식 (사이클4 #2). 접두 비교를 폐지했으므로 지문 대신 세대 id 를 받는다.
APPROVAL_ID_RE = re.compile(r"^[0-9a-f]{%d}$" % state_mod.APPROVAL_ID_LEN)


def _err(message) -> None:
    """발송기의 **모든** stderr 출력 (사이클5 #4).

    check.json 의 reason 이나 예외 문자열에 토큰이 섞여 launchd 로그·봇 카드 편집으로
    새지 않도록 한 곳에서 redact 한다.
    """
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    """발송기의 stdout 출력 (봇이 수신자 수를 파싱한다)."""
    print(redact(message))

# [텍스트](URL) — URL 안의 괄호 한 단계까지 균형 있게 소비 (javascript:alert(1) 대응)
LINK_PATTERN = r"\[([^\]]+)\]\(([^()\s]*(?:\([^()]*\)[^()\s]*)*)\)"


def markdown_to_html(markdown_text: str) -> str:
    """간단한 마크다운을 HTML로 변환 (이스케이프 우선).

    Args:
        markdown_text: 마크다운 텍스트

    Returns:
        HTML 문자열
    """
    # HTML 기본 구조
    html_body = "<html><head><meta charset='UTF-8'></head><body>"

    # 마크다운 변환
    lines = markdown_text.split("\n")
    in_list = False
    in_code = False

    for line in lines:
        # 주석 라인 스킵
        if line.strip().startswith("<!--"):
            continue

        # 코드 블록
        if line.strip().startswith("```"):
            in_code = not in_code
            if in_code:
                html_body += "<pre><code>"
            else:
                html_body += "</code></pre>"
            continue

        if in_code:
            html_body += html_module.escape(line) + "\n"
            continue

        # 제목 처리
        if line.startswith("# "):
            html_body += f"<h1>{html_module.escape(line[2:])}</h1>"
            continue
        elif line.startswith("## "):
            html_body += f"<h2>{html_module.escape(line[3:])}</h2>"
            continue
        elif line.startswith("### "):
            html_body += f"<h3>{html_module.escape(line[4:])}</h3>"
            continue

        # 순서 없는 목록
        if line.startswith("- "):
            if not in_list:
                html_body += "<ul>"
                in_list = True
            html_body += f"<li>{html_module.escape(line[2:])}</li>"
            continue

        # 목록 종료 처리
        if in_list and line.strip() and not line.startswith("- "):
            html_body += "</ul>"
            in_list = False

        # 빈 줄
        if not line.strip():
            continue

        # 링크는 이스케이프 전에 처리해야 하므로, 원문에 등장할 수 없는
        # sentinel(\x00LINK{n}\x00)로 먼저 치환한다.
        link_placeholders = {}
        modified_line = line
        for i, match in enumerate(re.finditer(LINK_PATTERN, line)):
            url = match.group(2)
            text = match.group(1)
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https"):
                placeholder = f"\x00LINK{i}\x00"
                link_placeholders[placeholder] = (
                    f'<a href="{html_module.escape(url, quote=True)}">'
                    f"{html_module.escape(text)}</a>"
                )
                modified_line = modified_line.replace(match.group(0), placeholder, 1)
            else:
                # javascript: 등 허용 안 함 - 링크 전체 제거, 텍스트만 남김
                # (뒤에서 라인 전체를 이스케이프하므로 여기서는 원문 그대로)
                modified_line = modified_line.replace(match.group(0), text, 1)

        # 이제 라인 전체 이스케이프 (sentinel은 이스케이프 영향 없음)
        escaped_line = html_module.escape(modified_line)

        # sentinel을 실제 링크로 복원
        for placeholder, link_html in link_placeholders.items():
            escaped_line = escaped_line.replace(placeholder, link_html)

        # **굵은텍스트** 처리 (이스케이프 후: \*\*...\*\*)
        escaped_line = re.sub(
            r"\*\*([^*]+)\*\*",
            r"<strong>\1</strong>",
            escaped_line
        )

        html_body += f"<p>{escaped_line}</p>"

    if in_list:
        html_body += "</ul>"

    html_body += "</body></html>"
    return html_body


def _check_fail_closed_bytes(
    markdown_bytes: bytes, check_json_path: Path
) -> Tuple[bool, str]:
    """이미 읽어 둔 **원시 바이트**로 fail-closed 판정 (계약 W10 · PR #1).

    파일을 다시 읽지 않는 것이 핵심이다 — 게이트가 본 바이트와 발송하는 바이트가
    같아야 TOCTOU(검사 통과 후 본문 교체)가 닫힌다. 해시 기준도 이 바이트다.
    """
    try:
        markdown_text = markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return False, f"마크다운 읽기 실패: {exc}"

    if "<!-- 상민 확정 필요 -->" in markdown_text:
        return False, "마크다운에 미확정 마커가 있습니다"

    # check.json 확인 (부재도 fail-closed)
    if not check_json_path.exists():
        return False, "검증 파일 없음"

    try:
        with open(check_json_path, "r", encoding="utf-8") as f:
            check_result = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return False, f"검증 파일 손상: {e}"

    # check.json은 검증한 바로 그 마크다운 바이트에만 유효하다. 사람이 검증 후
    # 본문을 바꾸거나 다른 주차의 결과 파일을 복사해도 발송하면 안 된다.
    recorded_hash = check_result.get("markdown_sha256")
    if not isinstance(recorded_hash, str) or not recorded_hash:
        return False, "검증 파일에 마크다운 SHA-256이 없음"
    if not hmac.compare_digest(recorded_hash, markdown_sha256(markdown_bytes)):
        return False, "마크다운이 검증 후 변경됨"

    # pass=false 확인
    if not check_result.get("pass", False):
        failed_items = [
            item["url"]
            for item in check_result.get("items", [])
            if not item.get("passed", False)
        ]
        if failed_items:
            msg = f"검증 실패: {len(failed_items)}개 항목"
            return False, msg
        else:
            reason = check_result.get("reason", "알 수 없는 검증 실패")
            return False, reason

    # network_checked 확인
    if not check_result.get("network_checked", False):
        return False, "네트워크 검증이 실행되지 않음"

    # 사이클4 #5: 빈 본문은 pass 여도 발송하지 않는다 — 계약상 독립 조건이다.
    if len(check_result.get("items") or []) < 1:
        return False, "검증에 항목이 0건"
    if int(check_result.get("item_blocks") or 0) < 1:
        return False, "항목 0건 (공고 블록 없음)"

    return True, ""


def check_fail_closed(markdown_path: Path, check_json_path: Path) -> Tuple[bool, str]:
    """Read a Markdown file and check its fail-closed conditions.

    Args:
        markdown_path: 마크다운 파일 경로
        check_json_path: 검증 JSON 파일 경로

    Returns:
        (통과 여부, 실패 메시지)
    """
    try:
        markdown_bytes = markdown_path.read_bytes()
    except OSError as e:
        return False, f"마크다운 읽기 실패: {e}"
    return _check_fail_closed_bytes(markdown_bytes, check_json_path)


def send_digest(
    markdown_path: Path,
    to_email: str = None,
    dry_run: bool = True,
    approved_by=None,
    approval_id: str = None,
) -> int:
    """다이제스트 발송.

    Args:
        markdown_path: 마크다운 파일 경로
        to_email: 수신자 이메일 (기본: config에서)
        dry_run: 드라이런 모드 (기본: True)
        approved_by: 발송을 승인한 텔레그램 user_id (계약 W10, 상태 파일에 기록)
        approval_id: 승인 카드의 세대 id — `--send` 에는 필수다 (사이클4 #2).
            잠금 안에서 state.approval.id 와, approval.sha == 현재 원시 바이트
            전체 SHA == check.json.markdown_sha256 을 모두 대조한다.

    Returns:
        종료 코드 (0: 성공, 2: 실패, 1: 발송 후 기록 실패)
    """
    markdown_path = Path(markdown_path)

    if not markdown_path.exists():
        _err(f"✗ 파일 없음: {markdown_path}")
        return 2

    check_json_path = markdown_path.with_suffix(".check.json")

    # 사이클5 #3: 검증 파괴 표식이 있으면 무조건 거부 (드라이런도).
    # 제거는 `/digest 재검토` 가 재조립+재검증에 성공했을 때만.
    broken = state_mod.tombstone_reason(
        state_mod.tombstone_path_for_markdown(markdown_path))
    if broken:
        _err(f"✗ 발송 거부: {state_mod.TOMBSTONE_REASON} [{broken}]")
        return 2

    if dry_run:
        # 드라이런도 게이트가 본 바이트를 그대로 렌더한다 (해시 기준 일치)
        try:
            markdown_bytes = markdown_path.read_bytes()
        except OSError as exc:
            _err(f"✗ 마크다운 읽기 실패: {exc}")
            return 2
        passed, msg = _check_fail_closed_bytes(markdown_bytes, check_json_path)
        if not passed:
            _err(f"✗ 발송 거부: {msg}")
            return 2
        markdown_text = markdown_bytes.decode("utf-8")
        html_text = markdown_to_html(markdown_text)
        recipients = to_email if to_email else "[config에서 설정]"
        _out(f"[DRY-RUN] 발송 대상: {recipients}")
        _out(f"[DRY-RUN] 제목: {_subject(markdown_text)}")
        _out(
            f"[DRY-RUN] 본문 길이: {len(markdown_text)} bytes (마크다운), "
            f"{len(html_text)} bytes (HTML)"
        )
        return 0

    # 사이클4 #2: 승인 세대 id 없는 실발송은 없다. 형식도 검증한다.
    approval = (approval_id or "").strip().lower()
    if not APPROVAL_ID_RE.match(approval):
        problem = "없습니다" if not approval else f"형식이 아닙니다: {approval!r}"
        _err(
            f"✗ 발송 거부: --approval-id 가 {problem} "
            f"({state_mod.APPROVAL_ID_LEN}자 16진수)"
        )
        return 2

    week = state_mod.week_from_markdown(markdown_path)
    state_path = state_mod.state_path_for_markdown(markdown_path)
    lock_path = state_mod.lock_path_for_markdown(markdown_path)

    try:
        lock_handle = state_mod.acquire_lock(lock_path)
    except state_mod.LockBusy as exc:
        _err(f"✗ 발송 거부: {exc}")
        return 2
    except OSError as exc:
        _err(f"✗ 발송 거부: 잠금 생성 실패 — {exc}")
        return 2

    try:
        return _send_locked(
            markdown_path=markdown_path,
            check_json_path=check_json_path,
            to_email=to_email,
            approved_by=approved_by,
            approval_id=approval,
            week=week,
            state_path=state_path,
        )
    finally:
        state_mod.release_lock(lock_handle)


def _subject(markdown_text: str) -> str:
    """첫 `# 제목` (없으면 기본 제목)."""
    matched = re.search(r"^# (.+)$", markdown_text, re.MULTILINE)
    return matched.group(1) if matched else "협의회 주간 정책브리핑"


def _send_locked(
    markdown_path: Path,
    check_json_path: Path,
    to_email,
    approved_by,
    approval_id: str,
    week: str,
    state_path: Path,
) -> int:
    """잠금을 쥔 상태의 발송 본체.

    본문은 **잠금 안에서 한 번만** 바이트로 읽고, 그 바이트로 해시·게이트·렌더·발송을
    모두 한다 (사이클2 #2 — 검사와 발송 사이에 파일이 바뀔 틈을 없앤다).
    """
    try:
        markdown_bytes = markdown_path.read_bytes()
    except OSError as exc:
        _err(f"✗ 발송 거부: 마크다운 읽기 실패 — {exc}")
        return 2

    try:
        markdown_text = markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        _err(f"✗ 발송 거부: 마크다운 디코드 실패 — {exc}")
        return 2

    current_sha = markdown_sha256(markdown_bytes)

    passed, msg = _check_fail_closed_bytes(markdown_bytes, check_json_path)
    if not passed:
        _err(f"✗ 발송 거부: {msg}")
        return 2

    # 상태 파일이 손상돼 판정할 수 없으면 발송하지 않는다(fail-closed).
    try:
        state = state_mod.load_state(state_path, week)
    except state_mod.StateError as exc:
        _err(f"✗ 발송 거부: {exc}")
        return 2

    allowed, reason = state_mod.can_send(state)
    if not allowed:
        _err(f"✗ 발송 거부: {reason}")
        if reason == state_mod.SENDING_REASON:
            _err(
                "  이전 발송의 결과가 확정되지 않았습니다. 메일함을 확인한 뒤 "
                f"`/digest 해제 {week}` 로 풀어주세요(자동 재발송 안 함)."
            )
        return 2

    # 사이클4 #2 · 사이클5 #2: 승인 세대 검증 — id + 본문 전체 SHA + **검증 파일 SHA**.
    #   state.approval.id       == --approval-id
    #   state.approval.sha      == 현재 원시 바이트 전체 SHA (== check.markdown_sha256)
    #   state.approval.check_sha == 현재 check.json **바이트** SHA
    # 본문 ↔ check.json 항등식은 _check_fail_closed_bytes 가 이미 확인했다. 여기서
    # 검증 파일 바이트까지 묶는 이유: 본문 SHA 가 같아도 렌더에 쓴 분류가 바뀌면
    # (v2 분류로 0건 → 수정 후 pass) 사람이 본 화면과 발송물이 달라진다(Codex 재현).
    try:
        check_bytes = check_json_path.read_bytes()
    except OSError as exc:
        _err(f"✗ 발송 거부: 검증 파일 읽기 실패 — {exc}")
        return 2
    current_check_sha = markdown_sha256(check_bytes)
    ok, reason = state_mod.check_approval(
        state, approval_id, current_sha, current_check_sha
    )
    if not ok:
        _err(f"✗ 발송 거부: {reason}")
        return 2

    # 사이클4 #3 · 사이클5 #1: 제외한 URL 이 **본문 어디에라도** 남아 있으면 거부.
    # 항목의 `**원문:**` 만 보면 제외 URL 을 해설 참고 링크나 항목의 두 번째 링크로
    # 옮겨 검사를 통과시킬 수 있었다(Codex 재현).
    leftover = state_mod.excluded_still_present(
        state, prune.body_links(markdown_text)
    )
    if leftover:
        _err(
            f"✗ 발송 거부: {state_mod.EXCLUDED_NOT_APPLIED_REASON} "
            f"({len(leftover)}건, 예: {leftover[0]})"
        )
        return 2

    # SMTP 앞의 모든 준비는 sending 표시 **전**에 끝낸다 — 설정 실수로 sending 이
    # 남아 사람 개입을 요구하는 일을 막는다.
    try:
        notifier = EmailNotifier()
    except Exception as exc:  # noqa: BLE001 — 설정·의존성 오류는 발송 전 거부
        _err(f"✗ 초기화 실패: {exc}")
        return 2

    if not notifier.sender or not notifier.password:
        _err("✗ 이메일 인증 정보가 설정되지 않았습니다")
        return 2

    # --to 옵션이 지정되면 그것을 사용, 아니면 config의 모든 수신자
    recipients = [to_email] if to_email else notifier.recipients

    if not recipients:
        _err("✗ 수신자가 설정되지 않았습니다")
        return 2

    try:
        html_body = markdown_to_html(markdown_text)
    except Exception as exc:  # noqa: BLE001 — 렌더 실패는 발송 전 거부
        _err(f"✗ 본문 변환 실패: {exc}")
        return 2

    now_iso = datetime.now().isoformat(timespec="seconds")
    try:
        state = state_mod.apply_state(
            state_path, state, state_mod.mark_sending(state, now_iso)
        )
    except (OSError, state_mod.TransitionError) as exc:
        # sending 을 못 남기면 발송하지 않는다 — 기록 없는 발송이 중복 발송의 씨앗이다.
        _err(f"✗ 발송 거부: 발송 표시 기록 실패 — {exc}")
        return 2

    try:
        delivered, stage = notifier.send_html_staged(
            _subject(markdown_text), html_body, recipients
        )
    except Exception as exc:  # noqa: BLE001 — 전송 중 예외는 결과 미확정이다
        _err(f"✗ 발송 실패(결과 미확정): {exc}")
        _keep_sending_notice(state_path, week)
        return 2

    if not delivered:
        _err(f"✗ 발송 실패: SMTP 전송에 실패했습니다 (단계: {stage})")
        if stage in UNSENT_STAGES:
            # 확정적 미발송 — 재시도를 허용한다 (사이클2 #4)
            _revert_sending(state_path, state, stage)
        else:
            _keep_sending_notice(state_path, week)
        return 2

    _out(f"✓ 발송 성공: {len(recipients)}명")

    sent_state = state_mod.mark_sent(state, approved_by, len(recipients), now_iso)
    last_error = None
    for attempt in range(1, STATE_SAVE_ATTEMPTS + 1):
        try:
            state_mod.apply_state(state_path, state, sent_state)
            _out(f"✓ 상태 기록: {state_path} (status=sent)")
            return 0
        except (OSError, state_mod.TransitionError) as exc:
            last_error = exc
            _err(f"⚠️  상태 기록 실패 {attempt}/{STATE_SAVE_ATTEMPTS}: {exc}")
            if attempt < STATE_SAVE_ATTEMPTS:
                time.sleep(STATE_SAVE_BACKOFF * attempt)

    # 발송은 이미 끝났다 — status=sending 이 남으므로 이후 발송은 거부된다.
    _err(f"⚠️  상태 기록 실패(발송은 완료됨): {last_error}")
    _keep_sending_notice(state_path, week)
    return 1


def _revert_sending(state_path: Path, state, stage: str) -> None:
    """확정적 미발송이면 sending 을 풀어 재시도를 허용한다 (사이클2 #4)."""
    try:
        reverted = state_mod.apply_state(
            state_path, state, state_mod.revert_sending(state), escape=True
        )
    except (OSError, state_mod.TransitionError) as exc:
        _err(f"⚠️  발송 표시 되돌리기 실패: {exc}")
        return
    _err(
        f"  전송 전 단계({stage}) 실패 — 발송되지 않았습니다. "
        f"status={reverted['status']} 로 되돌렸습니다(재시도 가능)."
        )


def _keep_sending_notice(state_path: Path, week: str) -> None:
    """전달 여부가 불확실한 실패 — sending 을 남기고 사람을 부른다."""
    _err(
        f"  전달 여부가 확인되지 않았습니다 — status=sending 유지. 메일함 확인 후 "
        f"`/digest 해제 {week}` 로 풀어주세요(자동 재발송 안 함). 상태: {state_path}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="협의회 주간 정책브리핑 다이제스트 발송"
    )
    parser.add_argument(
        "markdown",
        help="다이제스트 마크다운 파일 경로",
    )
    parser.add_argument(
        "--to",
        help="수신자 이메일 (기본: config에서)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="드라이런 모드 (명시해야 활성화, --send보다 우선)",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="실제 발송 (기본은 드라이런)",
    )
    parser.add_argument(
        "--approved-by",
        help="발송을 승인한 텔레그램 user_id (상태 파일에 기록)",
    )
    parser.add_argument(
        "--approval-id",
        help=f"승인 카드의 세대 id(16진수 {state_mod.APPROVAL_ID_LEN}자). --send 에 필수",
        )

    args = parser.parse_args()

    # --dry-run이 --send보다 우선 (명시적 안전 플래그)
    dry_run = args.dry_run or not args.send

    return send_digest(
        markdown_path=args.markdown,
        to_email=args.to,
        dry_run=dry_run,
        approved_by=args.approved_by,
        approval_id=args.approval_id,
        )


if __name__ == "__main__":
    sys.exit(main())
