#!/usr/bin/env python3
"""주간 정책브리핑 다이제스트 발송 스크립트."""

import argparse
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from alert.config import get_config


def markdown_to_html(markdown_text: str) -> str:
    """간단한 마크다운을 HTML로 변환.

    Args:
        markdown_text: 마크다운 텍스트

    Returns:
        HTML 문자열
    """
    # HTML 기본 구조
    html = "<html><head><meta charset='UTF-8'></head><body>"

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
                html += "<pre><code>"
            else:
                html += "</code></pre>"
            continue

        if in_code:
            html += line + "\n"
            continue

        # 제목
        if line.startswith("# "):
            html += f"<h1>{line[2:]}</h1>"
        elif line.startswith("## "):
            html += f"<h2>{line[3:]}</h2>"
        elif line.startswith("### "):
            html += f"<h3>{line[4:]}</h3>"
        # 링크
        elif "[" in line and "](" in line:
            # [텍스트](URL) -> <a href="URL">텍스트</a>
            line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', line)
            html += f"<p>{line}</p>"
        # 굵은 텍스트
        elif "**" in line:
            line = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", line)
            html += f"<p>{line}</p>"
        # 순서 없는 목록
        elif line.startswith("- "):
            if not in_list:
                html += "<ul>"
                in_list = True
            html += f"<li>{line[2:]}</li>"
        # 목록 종료
        elif in_list and line.strip() and not line.startswith("- "):
            html += "</ul>"
            in_list = False
            if line.strip() and not line.startswith(("*", "#", "[")):
                html += f"<p>{line}</p>"
        # 빈 줄
        elif not line.strip():
            continue
        # 일반 텍스트
        else:
            if line.strip():
                html += f"<p>{line}</p>"

    if in_list:
        html += "</ul>"

    html += "</body></html>"
    return html


def check_fail_closed(markdown_path: Path, check_json_path: Path) -> Tuple[bool, str]:
    """fail-closed 조건 확인.

    Args:
        markdown_path: 마크다운 파일 경로
        check_json_path: 검증 JSON 파일 경로

    Returns:
        (통과 여부, 실패 메시지)
    """
    # 마크다운에서 마커 확인
    with open(markdown_path, "r", encoding="utf-8") as f:
        markdown_text = f.read()

    if "<!-- 상민 확정 필요 -->" in markdown_text:
        return False, "마크다운에 미확정 마커가 있습니다"

    # check.json 확인
    if check_json_path.exists():
        with open(check_json_path, "r", encoding="utf-8") as f:
            check_result = json.load(f)

        if not check_result.get("pass", False):
            failed_items = [
                item["url"]
                for item in check_result.get("items", [])
                if not item.get("passed", False)
            ]
            msg = f"검증 실패: {len(failed_items)}개 항목"
            if failed_items:
                msg += f" ({failed_items[0][:50]}...)"
            return False, msg

    return True, ""


def send_digest(
    markdown_path: Path,
    to_email: str = None,
    dry_run: bool = True,
) -> int:
    """다이제스트 발송.

    Args:
        markdown_path: 마크다운 파일 경로
        to_email: 수신자 이메일 (기본: config에서)
        dry_run: 드라이런 모드 (기본: True)

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

    # 수신자 결정
    if not to_email:
        cfg = get_config()
        recipients = cfg.notifier.email.recipients
        if not recipients:
            print("✗ 수신자가 설정되지 않았습니다", file=sys.stderr)
            return 2
        to_email = recipients[0] if isinstance(recipients, list) else recipients

    # 제목 추출 (첫 번째 # 제목)
    title_match = re.search(r"^# (.+)$", markdown_text, re.MULTILINE)
    subject = title_match.group(1) if title_match else "협의회 주간 정책브리핑"

    # 드라이런
    if dry_run:
        html_text = markdown_to_html(markdown_text)
        print(f"[DRY-RUN] 발송 대상: {to_email}")
        print(f"[DRY-RUN] 제목: {subject}")
        print(f"[DRY-RUN] 본문 길이: {len(markdown_text)} bytes (마크다운), {len(html_text)} bytes (HTML)")
        return 0

    # 실제 발송
    cfg = get_config()
    email_config = cfg.notifier.email

    sender = os.getenv("EMAIL_SENDER", email_config.sender)
    password = os.getenv("EMAIL_PASSWORD", email_config.password)

    if not sender or not password:
        print("✗ 이메일 인증 정보가 설정되지 않았습니다", file=sys.stderr)
        return 2

    # 이메일 구성
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject

    # HTML 본문
    html_body = markdown_to_html(markdown_text)
    html_part = MIMEText(html_body, "html", "utf-8")
    msg.attach(html_part)

    # SMTP 전송
    try:
        with smtplib.SMTP(
            email_config.smtp_server,
            email_config.smtp_port,
            timeout=30
        ) as server:
            if email_config.use_tls:
                server.starttls()

            server.login(sender, password)
            server.send_message(msg)

        print(f"✓ 발송 성공: {to_email}")
        return 0

    except smtplib.SMTPException as exc:
        print(f"✗ SMTP 오류: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"✗ 발송 실패: {exc}", file=sys.stderr)
        return 2


from typing import Tuple


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
        default=True,
        help="드라이런 모드 (기본: True)",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="실제 발송 (이 플래그가 없으면 드라이런)",
    )

    args = parser.parse_args()

    # --send 플래그가 있으면 드라이런 비활성화
    dry_run = not args.send

    return send_digest(
        markdown_path=args.markdown,
        to_email=args.to,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
