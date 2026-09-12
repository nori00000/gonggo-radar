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
from alert.digest import blocks as blocks_mod
from alert.digest.composer import fit_prose_urls
from alert.digest import state as state_mod
from alert.digest.checker import markdown_sha256

# 상태 기록(status=sent) 저장 재시도 — 여기서 실패하면 "발송했는데 기록이 없는" 창이 열린다.
STATE_SAVE_ATTEMPTS = 3
STATE_SAVE_BACKOFF = 0.5

# 승인 지문은 8자 이상 64자 이하의 16진수만 받는다 (사이클3 #4).
# 빈 문자열·공백·1자를 받으면 startswith("") 가 언제나 참이 되어 게이트가 뚫린다.
APPROVED_SHA_RE = re.compile(r"^[0-9a-f]{8,64}$")

# 링크 추출은 alert.digest.blocks.find_links 가 정본이다 (사이클 6 #1·#4).
# 자체 정규식(LINK_PATTERN)은 폐지했다 — 두면 ①`…/report(2026)` 같은 괄호 포함
# URL을 잘라 먹고 ②checker·prune 과 다른 URL을 보게 되어 "완전한 주소는 살아
# 있는데 잘린 주소가 죽어서" 정상 링크가 삭제된다.
# 스킴이 없는 괄호(`[모집](~9.30)`)는 링크가 아니다.


def markdown_to_html(markdown_text: str) -> str:
    """간단한 마크다운을 HTML로 변환 (이스케이프 우선).

    Args:
        markdown_text: 마크다운 텍스트

    Returns:
        HTML 문자열
    """
    # HTML 기본 구조
    html_body = "<html><head><meta charset='UTF-8'></head><body>"

    # 사이클 9 #5: 긴 URL 치환은 **모든 렌더러가 같은 함수**를 쓴다 — 채널마다
    # 다른 본문이 나가면 "메일에는 있는데 카톡에는 없는 링크" 가 생긴다.
    markdown_text, _ = fit_prose_urls(markdown_text)

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
        # sentinel(\x00LINK{n}\x00)로 먼저 치환한다. 치환은 **위치 기반**이다 —
        # 문자열 replace 는 같은 링크가 두 번 나오면 엉뚱한 곳을 바꾼다.
        link_placeholders = {}
        pieces = []
        cursor = 0
        for i, link in enumerate(blocks_mod.find_links(line)):
            pieces.append(line[cursor:link.start])
            if urlparse(link.url).scheme in ("http", "https"):
                placeholder = f"\x00LINK{i}\x00"
                link_placeholders[placeholder] = (
                    f'<a href="{html_module.escape(link.url, quote=True)}">'
                    f"{html_module.escape(link.text)}</a>"
                )
                pieces.append(placeholder)
            else:
                # http/https 가 아닌 링크(www. 등)는 앵커를 만들지 않고 텍스트만 남긴다
                pieces.append(link.text)
            cursor = link.end
        pieces.append(line[cursor:])
        modified_line = "".join(pieces)

        # 이제 라인 전체 이스케이프 (sentinel은 이스케이프 영향 없음)
        escaped_line = html_module.escape(modified_line)

        # **굵은텍스트** 처리 — sentinel 복원 **전에** 한다 (사이클 6 #4 /
        # Codex 신규 #7). 뒤에 하면 href 안의 `**` 가 <strong> 으로 바뀌어
        # 링크가 깨진다. href 에는 이스케이프만 적용되고 마크업은 해석되지 않는다.
        escaped_line = re.sub(
            r"\*\*([^*]+)\*\*",
            r"<strong>\1</strong>",
            escaped_line
        )

        # sentinel을 실제 링크로 복원
        for placeholder, link_html in link_placeholders.items():
            escaped_line = escaped_line.replace(placeholder, link_html)

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
    approved_sha: str = None,
) -> int:
    """다이제스트 발송.

    Args:
        markdown_path: 마크다운 파일 경로
        to_email: 수신자 이메일 (기본: config에서)
        dry_run: 드라이런 모드 (기본: True)
        approved_by: 발송을 승인한 텔레그램 user_id (계약 W10, 상태 파일에 기록)
        approved_sha: 승인 카드가 본 본문의 해시(접두) — `--send` 에는 필수다
            (계약 W10 사이클2 #2). 잠금 안에서 현재 본문 해시와 다시 대조한다.

    Returns:
        종료 코드 (0: 성공, 2: 실패, 1: 발송 후 기록 실패)
    """
    markdown_path = Path(markdown_path)

    if not markdown_path.exists():
        print(f"✗ 파일 없음: {markdown_path}", file=sys.stderr)
        return 2

    check_json_path = markdown_path.with_suffix(".check.json")

    if dry_run:
        # 드라이런도 게이트가 본 바이트를 그대로 렌더한다 (해시 기준 일치)
        try:
            markdown_bytes = markdown_path.read_bytes()
        except OSError as exc:
            print(f"✗ 마크다운 읽기 실패: {exc}", file=sys.stderr)
            return 2
        passed, msg = _check_fail_closed_bytes(markdown_bytes, check_json_path)
        if not passed:
            print(f"✗ 발송 거부: {msg}", file=sys.stderr)
            return 2
        markdown_text = markdown_bytes.decode("utf-8")
        html_text = markdown_to_html(markdown_text)
        recipients = to_email if to_email else "[config에서 설정]"
        print(f"[DRY-RUN] 발송 대상: {recipients}")
        print(f"[DRY-RUN] 제목: {_subject(markdown_text)}")
        print(
            f"[DRY-RUN] 본문 길이: {len(markdown_text)} bytes (마크다운), "
            f"{len(html_text)} bytes (HTML)"
        )
        return 0

    # 사이클2 #2·사이클3 #4: 승인 지문 없는 실발송은 없다. 형식도 검증한다 —
    # 공백·1자를 허용하면 startswith 가 언제나 참이 되어 게이트가 통째로 뚫린다.
    approved = (approved_sha or "").strip().lower()
    if not APPROVED_SHA_RE.match(approved):
        try:
            current = markdown_sha256(markdown_path.read_bytes())
        except OSError:
            current = "?"
        problem = "없습니다" if not approved else f"형식이 아닙니다: {approved!r}"
        print(
            f"✗ 발송 거부: --approved-sha 가 {problem} "
            f"(8자 이상 16진수 · 현재 본문 지문: {current[:8]})",
            file=sys.stderr,
        )
        return 2

    week = state_mod.week_from_markdown(markdown_path)
    state_path = state_mod.state_path_for_markdown(markdown_path)
    lock_path = state_mod.lock_path_for_markdown(markdown_path)

    def _reclaimed(reason):
        print(f"⚠️  잔존 잠금 회수: {reason}", file=sys.stderr)

    try:
        lock_handle = state_mod.acquire_lock(
            lock_path, on_reclaim=_reclaimed,
            on_warn=lambda why: print(f"⚠️  {why}", file=sys.stderr),
        )
    except state_mod.LockBusy as exc:
        print(f"✗ 발송 거부: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"✗ 발송 거부: 잠금 생성 실패 — {exc}", file=sys.stderr)
        return 2

    try:
        return _send_locked(
            markdown_path=markdown_path,
            check_json_path=check_json_path,
            to_email=to_email,
            approved_by=approved_by,
            approved_sha=approved,
            week=week,
            state_path=state_path,
        )
    finally:
        if not state_mod.release_lock(lock_handle):
            print("⚠️  잠금 해제 생략(내 잠금이 아님)", file=sys.stderr)


def _subject(markdown_text: str) -> str:
    """첫 `# 제목` (없으면 기본 제목)."""
    matched = re.search(r"^# (.+)$", markdown_text, re.MULTILINE)
    return matched.group(1) if matched else "협의회 주간 정책브리핑"


def _send_locked(
    markdown_path: Path,
    check_json_path: Path,
    to_email,
    approved_by,
    approved_sha: str,
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
        print(f"✗ 발송 거부: 마크다운 읽기 실패 — {exc}", file=sys.stderr)
        return 2

    try:
        markdown_text = markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"✗ 발송 거부: 마크다운 디코드 실패 — {exc}", file=sys.stderr)
        return 2

    current_sha = markdown_sha256(markdown_bytes)
    approved = str(approved_sha).strip().lower()
    if not current_sha.startswith(approved):
        print(
            "✗ 발송 거부: 승인된 본문이 아닙니다 "
            f"(승인 {approved} ≠ 현재 {current_sha[:len(approved)]})",
            file=sys.stderr,
        )
        return 2

    passed, msg = _check_fail_closed_bytes(markdown_bytes, check_json_path)
    if not passed:
        print(f"✗ 발송 거부: {msg}", file=sys.stderr)
        return 2

    # 상태 파일이 손상돼 판정할 수 없으면 발송하지 않는다(fail-closed).
    try:
        state = state_mod.load_state(state_path, week)
    except state_mod.StateError as exc:
        print(f"✗ 발송 거부: {exc}", file=sys.stderr)
        return 2

    allowed, reason = state_mod.can_send(state)
    if not allowed:
        print(f"✗ 발송 거부: {reason}", file=sys.stderr)
        if reason == state_mod.SENDING_REASON:
            print(
                "  이전 발송의 결과가 확정되지 않았습니다. 메일함을 확인한 뒤 "
                f"`/digest 해제 {week}` 로 풀어주세요(자동 재발송 안 함).",
                file=sys.stderr,
            )
        return 2

    # 사이클3 #3: 사람이 **본** 본문(최신 미리보기)과 지금 발송할 본문이 같아야 한다.
    #   approved-sha == 현재 본문 해시 == 최신 preview_sha — 셋이 일치할 때만 발송.
    preview_sha = state.get("preview_sha")
    if not preview_sha:
        print(
            "✗ 발송 거부: 미리보기 기록이 없습니다 — 미리보기를 먼저 보내세요",
            file=sys.stderr,
        )
        return 2
    if preview_sha != current_sha:
        print(
            "✗ 발송 거부: 미리보기와 본문이 다릅니다 "
            f"(미리보기 {str(preview_sha)[:8]} ≠ 현재 {current_sha[:8]}) — 재검토 필요",
            file=sys.stderr,
        )
        return 2

    # SMTP 앞의 모든 준비는 sending 표시 **전**에 끝낸다 — 설정 실수로 sending 이
    # 남아 사람 개입을 요구하는 일을 막는다.
    try:
        notifier = EmailNotifier()
    except Exception as exc:  # noqa: BLE001 — 설정·의존성 오류는 발송 전 거부
        print(f"✗ 초기화 실패: {exc}", file=sys.stderr)
        return 2

    if not notifier.sender or not notifier.password:
        print("✗ 이메일 인증 정보가 설정되지 않았습니다", file=sys.stderr)
        return 2

    # --to 옵션이 지정되면 그것을 사용, 아니면 config의 모든 수신자
    recipients = [to_email] if to_email else notifier.recipients

    if not recipients:
        print("✗ 수신자가 설정되지 않았습니다", file=sys.stderr)
        return 2

    try:
        html_body = markdown_to_html(markdown_text)
    except Exception as exc:  # noqa: BLE001 — 렌더 실패는 발송 전 거부
        print(f"✗ 본문 변환 실패: {exc}", file=sys.stderr)
        return 2

    now_iso = datetime.now().isoformat(timespec="seconds")
    try:
        state = state_mod.apply_state(
            state_path, state, state_mod.mark_sending(state, now_iso)
        )
    except (OSError, state_mod.TransitionError) as exc:
        # sending 을 못 남기면 발송하지 않는다 — 기록 없는 발송이 중복 발송의 씨앗이다.
        print(f"✗ 발송 거부: 발송 표시 기록 실패 — {exc}", file=sys.stderr)
        return 2

    try:
        delivered, stage = notifier.send_html_staged(
            _subject(markdown_text), html_body, recipients
        )
    except Exception as exc:  # noqa: BLE001 — 전송 중 예외는 결과 미확정이다
        print(f"✗ 발송 실패(결과 미확정): {exc}", file=sys.stderr)
        _keep_sending_notice(state_path, week)
        return 2

    if not delivered:
        print(f"✗ 발송 실패: SMTP 전송에 실패했습니다 (단계: {stage})",
              file=sys.stderr)
        if stage in UNSENT_STAGES:
            # 확정적 미발송 — 재시도를 허용한다 (사이클2 #4)
            _revert_sending(state_path, state, stage)
        else:
            _keep_sending_notice(state_path, week)
        return 2

    print(f"✓ 발송 성공: {len(recipients)}명")

    sent_state = state_mod.mark_sent(state, approved_by, len(recipients), now_iso)
    last_error = None
    for attempt in range(1, STATE_SAVE_ATTEMPTS + 1):
        try:
            state_mod.apply_state(state_path, state, sent_state)
            print(f"✓ 상태 기록: {state_path} (status=sent)")
            return 0
        except (OSError, state_mod.TransitionError) as exc:
            last_error = exc
            print(
                f"⚠️  상태 기록 실패 {attempt}/{STATE_SAVE_ATTEMPTS}: {exc}",
                file=sys.stderr,
            )
            if attempt < STATE_SAVE_ATTEMPTS:
                time.sleep(STATE_SAVE_BACKOFF * attempt)

    # 발송은 이미 끝났다 — status=sending 이 남으므로 이후 발송은 거부된다.
    print(f"⚠️  상태 기록 실패(발송은 완료됨): {last_error}", file=sys.stderr)
    _keep_sending_notice(state_path, week)
    return 1


def _revert_sending(state_path: Path, state, stage: str) -> None:
    """확정적 미발송이면 sending 을 풀어 재시도를 허용한다 (사이클2 #4)."""
    try:
        reverted = state_mod.apply_state(
            state_path, state, state_mod.revert_sending(state), escape=True
        )
    except (OSError, state_mod.TransitionError) as exc:
        print(f"⚠️  발송 표시 되돌리기 실패: {exc}", file=sys.stderr)
        return
    print(
        f"  전송 전 단계({stage}) 실패 — 발송되지 않았습니다. "
        f"status={reverted['status']} 로 되돌렸습니다(재시도 가능).",
        file=sys.stderr,
    )


def _keep_sending_notice(state_path: Path, week: str) -> None:
    """전달 여부가 불확실한 실패 — sending 을 남기고 사람을 부른다."""
    print(
        f"  전달 여부가 확인되지 않았습니다 — status=sending 유지. 메일함 확인 후 "
        f"`/digest 해제 {week}` 로 풀어주세요(자동 재발송 안 함). 상태: {state_path}",
        file=sys.stderr,
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
        "--approved-sha",
        help="승인 카드가 본 본문 해시(16진수 8~64자). --send 에 필수",
    )

    args = parser.parse_args()

    # --dry-run이 --send보다 우선 (명시적 안전 플래그)
    dry_run = args.dry_run or not args.send

    return send_digest(
        markdown_path=args.markdown,
        to_email=args.to,
        dry_run=dry_run,
        approved_by=args.approved_by,
        approved_sha=args.approved_sha,
    )


if __name__ == "__main__":
    sys.exit(main())
