#!/usr/bin/env python3
"""주간 정책브리핑 다이제스트 발송 스크립트."""

import argparse
import html as html_module
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Tuple
from urllib.parse import urlparse

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.notifiers.email_sender import EmailNotifier
from alert.digest import state as state_mod

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


def check_fail_closed(markdown_path: Path, check_json_path: Path) -> Tuple[bool, str]:
    """fail-closed 조건 확인.

    Args:
        markdown_path: 마크다운 파일 경로
        check_json_path: 검증 JSON 파일 경로

    Returns:
        (통과 여부, 실패 메시지)
    """
    # 마크다운에서 마커 확인
    try:
        with open(markdown_path, "r", encoding="utf-8") as f:
            markdown_text = f.read()
    except Exception as e:
        return False, f"마크다운 읽기 실패: {e}"

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


def send_digest(
    markdown_path: Path,
    to_email: str = None,
    dry_run: bool = True,
    approved_by=None,
) -> int:
    """다이제스트 발송.

    Args:
        markdown_path: 마크다운 파일 경로
        to_email: 수신자 이메일 (기본: config에서)
        dry_run: 드라이런 모드 (기본: True)
        approved_by: 발송을 승인한 텔레그램 user_id (계약 W10, 상태 파일에 기록)

    Returns:
        종료 코드 (0: 성공, 2: 실패)
    """
    markdown_path = Path(markdown_path)

    if not markdown_path.exists():
        print(f"✗ 파일 없음: {markdown_path}", file=sys.stderr)
        return 2

    # Fail-closed 검증
    check_json_path = markdown_path.with_suffix(".check.json")
    passed, msg = check_fail_closed(markdown_path, check_json_path)

    if not passed:
        print(f"✗ 발송 거부: {msg}", file=sys.stderr)
        return 2

    # 마크다운 읽기
    with open(markdown_path, "r", encoding="utf-8") as f:
        markdown_text = f.read()

    # 제목 추출 (첫 번째 # 제목)
    title_match = re.search(r"^# (.+)$", markdown_text, re.MULTILINE)
    subject = title_match.group(1) if title_match else "협의회 주간 정책브리핑"

    # 드라이런
    if dry_run:
        html_text = markdown_to_html(markdown_text)
        recipients = to_email if to_email else "[config에서 설정]"
        print(f"[DRY-RUN] 발송 대상: {recipients}")
        print(f"[DRY-RUN] 제목: {subject}")
        print(f"[DRY-RUN] 본문 길이: {len(markdown_text)} bytes (마크다운), {len(html_text)} bytes (HTML)")
        return 0

    # 계약 W10: status=sent 는 불변이다 — 같은 주차 재발송을 거부한다.
    # 상태 파일이 손상돼 판정할 수 없으면 발송하지 않는다(fail-closed).
    week = state_mod.week_from_markdown(markdown_path)
    state_path = state_mod.state_path_for_markdown(markdown_path)
    try:
        state = state_mod.load_state(state_path, week)
    except state_mod.StateError as exc:
        print(f"✗ 발송 거부: {exc}", file=sys.stderr)
        return 2
    allowed, reason = state_mod.can_send(state)
    if not allowed:
        print(f"✗ 발송 거부: {reason}", file=sys.stderr)
        return 2

    # 실제 발송 - EmailNotifier 재사용
    try:
        notifier = EmailNotifier()

        if not notifier.sender or not notifier.password:
            print("✗ 이메일 인증 정보가 설정되지 않았습니다", file=sys.stderr)
            return 2

        # --to 옵션이 지정되면 그것을 사용, 아니면 config의 모든 수신자
        recipients = [to_email] if to_email else notifier.recipients

        if not recipients:
            print("✗ 수신자가 설정되지 않았습니다", file=sys.stderr)
            return 2

        # HTML 본문
        html_body = markdown_to_html(markdown_text)

        # 기존 EmailNotifier의 SMTP 경로 재사용
        if not notifier.send_html(subject, html_body, recipients):
            print("✗ 발송 실패: SMTP 전송에 실패했습니다", file=sys.stderr)
            return 2

        print(f"✓ 발송 성공: {len(recipients)}명")

        try:
            state_mod.save_state(
                state_path,
                state_mod.mark_sent(
                    state,
                    approved_by,
                    len(recipients),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            print(f"✓ 상태 기록: {state_path} (status=sent)")
        except OSError as exc:
            # 발송은 이미 끝났다 — 상태 기록 실패는 크게 알리고 비정상 종료한다.
            print(f"⚠️  상태 기록 실패(발송은 완료됨): {exc}", file=sys.stderr)
            return 1

        return 0

    except Exception as exc:
        print(f"✗ 초기화 실패: {exc}", file=sys.stderr)
        return 2


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

    args = parser.parse_args()

    # --dry-run이 --send보다 우선 (명시적 안전 플래그)
    dry_run = args.dry_run or not args.send

    return send_digest(
        markdown_path=args.markdown,
        to_email=args.to,
        dry_run=dry_run,
        approved_by=args.approved_by,
    )


if __name__ == "__main__":
    sys.exit(main())
