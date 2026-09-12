#!/usr/bin/env python3
"""다이제스트 미리보기를 텔레그램 협의회 토픽으로 전송 (계약 W10).

토픽 좌표 정본: `~/.config/homelab/topic-group.json` (chat_id + topics.council)
봇 토큰 정본: `~/.config/homelab/notify.env` (TELEGRAM_BOT_TOKEN)
토큰은 어떤 경로로도 출력하지 않는다.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from alert.digest import preview as preview_mod
from alert.digest import state as state_mod

TOPIC_GROUP_FILE = os.path.expanduser(
    os.environ.get("TOPIC_GROUP_FILE") or "~/.config/homelab/topic-group.json"
)
NOTIFY_ENV_FILE = os.path.expanduser(
    os.environ.get("NOTIFY_ENV_FILE") or "~/.config/homelab/notify.env"
)
DEFAULT_TOPIC_KEY = "council"
API_TIMEOUT = 20


def load_env_file(path):
    """KEY=VALUE 파일을 dict로. 없으면 빈 dict."""
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def resolve_target(topic_key=DEFAULT_TOPIC_KEY):
    """(chat_id, thread_id) 를 SSOT에서 읽는다. 실패는 예외."""
    if not os.path.isfile(TOPIC_GROUP_FILE):
        raise RuntimeError(f"토픽 그룹 설정 없음: {TOPIC_GROUP_FILE}")
    with open(TOPIC_GROUP_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    chat_id = data.get("chat_id")
    topics = data.get("topics") or {}
    if not isinstance(chat_id, int):
        raise RuntimeError("topic-group.json에 chat_id가 없음")
    if topic_key not in topics or not isinstance(topics[topic_key], int):
        raise RuntimeError(f"토픽 '{topic_key}'가 배선되지 않음 (topic-group.json)")
    return chat_id, topics[topic_key]


def resolve_token():
    """봇 토큰. 값은 절대 출력하지 않는다."""
    token = load_env_file(NOTIFY_ENV_FILE).get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(f"TELEGRAM_BOT_TOKEN 미설정: {NOTIFY_ENV_FILE}")
    return token


def send_chunk(token, chat_id, thread_id, text):
    """sendMessage 1건. 반환: (ok, message_id, 오류요지)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, data=payload, timeout=API_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return False, None, f"네트워크 오류: {exc}"
    try:
        body = response.json()
    except ValueError:
        return False, None, f"응답 파싱 실패 (HTTP {response.status_code})"
    if not body.get("ok"):
        return False, None, "API 실패: {}".format(
            str(body.get("description"))[:200]
        )
    return True, (body.get("result") or {}).get("message_id"), ""


def load_check(markdown_path):
    """같은 주차의 check.json. 부재·손상은 빈 dict."""
    check_path = Path(markdown_path).with_suffix(".check.json")
    if not check_path.exists():
        return {}
    try:
        with open(check_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def main():
    parser = argparse.ArgumentParser(
        description="다이제스트 미리보기를 텔레그램 협의회 토픽으로 전송"
    )
    parser.add_argument("markdown", help="다이제스트 마크다운 경로")
    parser.add_argument(
        "--topic-key",
        default=DEFAULT_TOPIC_KEY,
        help=f"topic-group.json의 토픽 키 (기본: {DEFAULT_TOPIC_KEY})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="전송하지 않고 본문만 출력",
    )
    args = parser.parse_args()

    markdown_path = Path(args.markdown)
    if not markdown_path.exists():
        print(f"✗ 파일 없음: {markdown_path}", file=sys.stderr)
        return 2

    week = state_mod.week_from_markdown(markdown_path)
    markdown_text = markdown_path.read_text(encoding="utf-8")
    check = load_check(markdown_path)

    body = preview_mod.render_preview(week, markdown_text, check)
    chunks = preview_mod.chunk_text(body)

    if args.dry_run:
        print(f"[DRY-RUN] {week} 미리보기 {len(chunks)}개 메시지")
        for chunk in chunks:
            print("---")
            print(chunk)
        return 0

    try:
        chat_id, thread_id = resolve_target(args.topic_key)
        token = resolve_token()
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"✗ 전송 대상 확인 실패: {exc}", file=sys.stderr)
        return 2

    message_ids = []
    for index, chunk in enumerate(chunks, start=1):
        ok, message_id, error = send_chunk(token, chat_id, thread_id, chunk)
        if not ok:
            print(
                f"✗ 전송 실패 ({index}/{len(chunks)}): {error}",
                file=sys.stderr,
            )
            return 2
        message_ids.append(message_id)
        print(f"✓ 전송 {index}/{len(chunks)} message_id={message_id}")

    state_path = state_mod.state_path_for_markdown(markdown_path)
    try:
        state = state_mod.load_state(state_path, week)
        state_mod.save_state(
            state_path, state_mod.record_preview(state, message_ids)
        )
        print(f"✓ 상태 기록: {state_path}")
    except (state_mod.StateError, OSError) as exc:
        print(f"⚠️  상태 기록 실패(미리보기는 전송됨): {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
