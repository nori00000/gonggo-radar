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
from alert.digest import prune
from alert.digest import sections as sections_mod
from alert.digest import state as state_mod
from alert.digest.checker import markdown_sha256
from alert.utils.redact import redact

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


def _api(token, method):
    return f"https://api.telegram.org/bot{token}/{method}"


def _post(token, method, payload):
    """Telegram POST 1건. 반환: (ok, result, 오류요지).

    오류요지에서 토큰을 가린다 (계약 W10 크리틱 #4) — requests 의 연결 오류
    문자열에는 `/bot<TOKEN>/sendMessage` URL 이 그대로 들어 있다.
    """
    try:
        response = requests.post(_api(token, method), data=payload,
                                 timeout=API_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return False, None, redact(f"네트워크 오류: {exc}", secrets=(token,))
    try:
        body = response.json()
    except ValueError:
        return False, None, f"응답 파싱 실패 (HTTP {response.status_code})"
    if not body.get("ok"):
        return False, None, redact(
            "API 실패: {}".format(str(body.get("description"))[:200]),
            secrets=(token,),
        )
    return True, body.get("result") or {}, ""


def send_chunk(token, chat_id, thread_id, text):
    """sendMessage 1건. 반환: (ok, message_id, 오류요지)."""
    ok, result, error = _post(token, "sendMessage", {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "text": text,
        "disable_web_page_preview": True,
    })
    if not ok:
        return False, None, error
    return True, result.get("message_id"), ""


def clear_card(token, chat_id, message_id):
    """이전 승인 카드의 버튼 제거 (계약 W10 크리틱 #6). 실패는 경고만."""
    ok, _, error = _post(token, "editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps({"inline_keyboard": []}),
    })
    return ok, error


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


def _blocking_reason(markdown_path, week, check, current_sha, markdown_text):
    """미리보기를 "검증 필요" 안내로 대체해야 하는 이유. 없으면 None (사이클4 #3·#4·#6)."""
    recorded = (check or {}).get("markdown_sha256")
    if not recorded:
        return "검증 파일이 이 본문을 본 기록이 없습니다"
    if recorded != current_sha:
        return "검증 파일이 다른 본문의 것입니다 (재검증 필요)"
    try:
        state = state_mod.load_state(
            state_mod.state_path_for_markdown(markdown_path), week)
    except state_mod.StateError as exc:
        return f"상태 파일 손상: {exc}"
    if state.get("verification_broken"):
        return state_mod.VERIFICATION_BROKEN_REASON
    if state.get("rebuild_failed"):
        return state_mod.REBUILD_FAILED_REASON
    item_sections, _ = sections_mod.resolve(check, markdown_text)
    leftover = state_mod.excluded_still_present(
        state,
        [block["url"] for block in prune.item_blocks(markdown_text, item_sections)],
    )
    if leftover:
        return "{} ({}건)".format(
            state_mod.EXCLUDED_NOT_APPLIED_REASON, len(leftover))
    return None


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
    # 승인 지문(approval.sha)은 발송 게이트와 같은 **원시 바이트** 해시다.
    # 렌더에 쓰는 텍스트도 같은 읽기에서 나와야 지문과 화면이 어긋나지 않는다.
    try:
        markdown_bytes = markdown_path.read_bytes()
        markdown_text = markdown_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"✗ 마크다운 읽기 실패: {exc}", file=sys.stderr)
        return 2
    check = load_check(markdown_path)
    current_sha = markdown_sha256(markdown_bytes)

    # 사이클4 #6: 렌더 **전에** check.json 이 이 바이트를 본 것인지 확인한다.
    # 불일치면 항목 미리보기를 만들지 않는다 — 항목 0건 화면이 승인 대상이 되던
    # 경로(다른 계약의 check 로 렌더)가 닫힌다. 섹션 목록도 그 check.json 의 것을 쓴다.
    blocked = _blocking_reason(markdown_path, week, check, current_sha,
                               markdown_text)
    if blocked:
        body = "🏛 협의회 주간 정책브리핑 {}\n\n⚠️ {}\n\n`/digest 재검토` 로 다시 검증하세요.".format(
            week, blocked)
        chunks = [body]
        item_urls = []
        approval_sha = None
    else:
        item_sections, _ = sections_mod.resolve(check, markdown_text)
        body = preview_mod.render_preview(week, markdown_text, check)
        chunks = preview_mod.chunk_text(body)
        item_urls = preview_mod.item_urls(markdown_text, item_sections)
        approval_sha = current_sha

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
        print(f"✗ 전송 대상 확인 실패: {redact(exc)}", file=sys.stderr)
        return 2

    # 이전 승인 카드의 버튼을 먼저 지운다 (계약 W10 크리틱 #6) — 새 미리보기가
    # 올라간 뒤에도 옛 카드가 남아 있으면 사람이 낡은 승인을 누를 수 있다.
    # (최종 방어는 카드 callback_data 의 본문 해시다 — 이건 UX 상의 정리다.)
    state_path = state_mod.state_path_for_markdown(markdown_path)
    lock_path = state_mod.lock_path_for_markdown(markdown_path)
    try:
        stale_card = state_mod.card_message_id(
            state_mod.load_state(state_path, week))
    except state_mod.StateError as exc:
        print(f"⚠️  상태 확인 실패(미리보기는 계속 전송): {exc}", file=sys.stderr)
        stale_card = None
    if stale_card:
        ok, error = clear_card(token, chat_id, stale_card)
        if not ok:
            print(f"⚠️  이전 승인 카드 버튼 제거 실패: {error}", file=sys.stderr)

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

    # 계약 W10 사이클2 #1: 상태 쓰기는 **잠금 하 read-modify-write** 다.
    # 텔레그램 왕복 동안 다른 발송이 sent 를 썼을 수 있으므로, 여기서 다시 읽고
    # message_id 만 더한다(record_preview 는 status 를 건드리지 않는다).
    #
    # 사이클4 #2: 사람이 **본** 본문의 지문으로 **새 승인 세대**를 발급한다.
    # 차단 상태(검증 불일치·제외 미반영 등)면 승인 세대를 발급하지 않는다 —
    # approval=None 이므로 어떤 카드도 발송 게이트를 통과하지 못한다.
    try:
        new_state = state_mod.update_state(
            state_path, lock_path, week,
            lambda current: state_mod.record_preview(
                current, message_ids, item_urls, approval_sha
            ),
        )
        approval = state_mod.approval_of(new_state)
        print("✓ 상태 기록: {} (approval={})".format(
            state_path, approval.get("id") or "없음(검증 필요)"))
    except (state_mod.StateError, state_mod.TransitionError,
            state_mod.LockBusy, OSError) as exc:
        print(f"⚠️  상태 기록 실패(미리보기는 전송됨): {redact(exc)}",
              file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
