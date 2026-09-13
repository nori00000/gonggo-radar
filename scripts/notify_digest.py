#!/usr/bin/env python3
"""다이제스트 미리보기를 텔레그램 협의회 토픽으로 전송 (계약 W10).

토픽 좌표 정본: `~/.config/homelab/topic-group.json` (chat_id + topics.council)
봇 토큰 정본: `~/.config/homelab/notify.env` (TELEGRAM_BOT_TOKEN)
토큰은 어떤 경로로도 출력하지 않는다.
"""

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
from alert.digest.checker import (
    CHECK_EXPIRED_REASON,
    check_expired,
    liveness_problem,
    liveness_snapshot,
    markdown_sha256,
    recheck_manifest,
)
from alert.digest.composer import load_items_manifest
from alert.utils.redact import redact
from alert.utils.safe_argparse import (
    RedactingArgumentParser,
    reject_secret_argv,
)

# 정본 재대조·마감 재판정이 쓰는 DB (통합 1 #1·#2). --db 로 바꿀 수 있다.
DEFAULT_DB_PATH = "alert/data/announcements.db"


def _err(message) -> None:
    """notify 의 **모든** stderr 출력 (사이클6 #7)."""
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    """notify 의 모든 stdout 출력 (드라이런 본문 포함)."""
    print(redact(message))

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


def delete_message(token, chat_id, message_id):
    """옛 미리보기·안내 삭제 (V4 계약 ②). 반환: (ok, 오류요지).

    실패해도 호출자는 진행한다 — 텔레그램은 48시간이 지난 메시지나 권한이 없는
    메시지의 삭제를 거부한다. 지우지 못한 사실은 상태(`superseded_message_ids`)에
    남으므로, "왜 옛 미리보기가 아직 있나" 를 사후에 설명할 수 있다.
    """
    ok, _, error = _post(token, "deleteMessage", {
        "chat_id": chat_id,
        "message_id": message_id,
    })
    return ok, error


def clear_card(token, chat_id, message_id):
    """이전 승인 카드의 버튼 제거 (계약 W10 크리틱 #6). 실패는 경고만."""
    ok, _, error = _post(token, "editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps({"inline_keyboard": []}),
    })
    return ok, error


def load_check(markdown_path):
    """같은 주차의 check.json → (바이트, dict). 부재·손상은 (None, {}).

    바이트를 함께 돌려주는 이유: 승인 세대가 **검증 파일 바이트 SHA** 로도 결속된다
    (사이클5 #2).
    """
    check_path = Path(markdown_path).with_suffix(".check.json")
    if not check_path.exists():
        return None, {}
    try:
        raw = check_path.read_bytes()
    except OSError:
        return None, {}
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw, {}


def _blocking_reason(markdown_path, week, check, current_sha, markdown_text,
                     markdown_bytes=b"", db_path=DEFAULT_DB_PATH,
                     snapshot=None):
    """미리보기를 "검증 필요" 안내로 대체해야 하는 이유. 없으면 None (사이클4 #3·#4·#6)."""
    broken = state_mod.tombstone_reason(
        state_mod.tombstone_path_for_markdown(markdown_path))
    if broken:
        return "{} [{}]".format(state_mod.TOMBSTONE_REASON, broken)
    if prune.control_chars(markdown_text):
        return "본문에 제어 문자 포함 ({})".format(
            prune.control_chars_label(markdown_text))
    recorded = (check or {}).get("markdown_sha256")
    if not recorded:
        return "검증 파일이 이 본문을 본 기록이 없습니다"
    if recorded != current_sha:
        return "검증 파일이 다른 본문의 것입니다 (재검증 필요)"
    # 사이클5 #2: pass=false·항목 0건이면 승인 세대를 발급하지 않는다.
    if not (check or {}).get("pass"):
        return "검증 미통과 — {}".format(
            (check or {}).get("reason") or "사유 미기록")
    if len((check or {}).get("items") or []) < 1:
        return "검증에 항목이 0건"
    if int((check or {}).get("item_blocks") or 0) < 1:
        return "항목 0건 (공고 블록 없음)"
    try:
        state = state_mod.load_state(
            state_mod.state_path_for_markdown(markdown_path), week)
    except state_mod.StateError as exc:
        return f"상태 파일 손상: {exc}"
    if state.get("verification_broken"):
        return state_mod.VERIFICATION_BROKEN_REASON
    if state.get("rebuild_failed"):
        return state_mod.REBUILD_FAILED_REASON
    # 사이클6: 렌더될 항목 수가 검증과 다르면 승인 대상이 아니다.
    item_sections, _ = sections_mod.resolve(check, markdown_text)
    rendered = prune.item_block_count(markdown_text, item_sections)
    if rendered != int((check or {}).get("item_blocks") or 0):
        return "본문 항목 수({})와 검증 항목 수({})가 다릅니다".format(
            rendered, (check or {}).get("item_blocks"))
    # 사이클5 #1 · 사이클6 #3: 제외 검사는 본문 **전체** URL 기준이다.
    leftover = state_mod.excluded_still_present(
        state, prune.body_urls(markdown_text))
    if leftover:
        return "{} ({}건)".format(
            state_mod.EXCLUDED_NOT_APPLIED_REASON, len(leftover))
    # 통합 1 #1·#2: 승인 세대를 발급하기 전에 **checker 와 같은 정본 대조**를 한다.
    # 번호 좌표(제외 n)가 미리보기와 같은 항목을 가리킨다는 보장은 개수가 아니라
    # 정본 대조에서 나온다 — 순서·URL·제목·줄 동일성·DB 재계산·마감 경과까지.
    issues = recheck_manifest(
        markdown_path, markdown_bytes, markdown_text, check, db_path)
    if issues:
        return "정본 대조 실패 — {}".format(issues[0])
    # 통합 2 #1: 승인 세대를 발급하기 전에 **지금** 링크가 살아 있는지 본다.
    # 승인은 "이 본문을 보내도 좋다" 는 사람의 판단이고, 그 판단의 전제가
    # 생존이다 — 죽은 링크가 있으면 승인 자체를 만들지 않는다.
    if check_expired(check):
        return CHECK_EXPIRED_REASON
    dead = liveness_problem(snapshot, current_sha)
    if dead:
        return dead
    return None



def _hold_ids(markdown_text, item_sections):
    """발송본 보류 주석의 공고 id (번호 순서) — `핀 n` 의 좌표 (V4 계약 ①).

    id 가 없는 구버전 주석은 **번호 자리를 지키려고** None 이 아니라 0 으로 둔다면
    엉뚱한 항목을 승격시킨다. 그래서 id 가 하나라도 없으면 좌표 자체를 포기하고
    (None) 봇이 현재 본문을 직접 파싱하는 길로 떨어진다(fail-closed).
    """
    holds = preview_mod.parse_digest(markdown_text, item_sections)["holds"]
    ids = [hold.get("id") for hold in holds]
    if any(value is None for value in ids):
        return None
    return [int(value) for value in ids]


def _manifest_item_urls(markdown_path):
    """항목 정본(items.json)이 정한 번호 순서의 URL 목록. 정본이 없으면 None."""
    manifest = load_items_manifest(markdown_path)
    if manifest is None:
        return None
    return [
        entry.get("url") for entry in (manifest.get("items") or [])
        if isinstance(entry, dict) and entry.get("url")
    ]


def main():
    parser = RedactingArgumentParser(
        description="다이제스트 미리보기를 텔레그램 협의회 토픽으로 전송"
    )
    parser.add_argument("markdown", help="다이제스트 마크다운 경로")
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH,
        help=f"announcements.db 경로 (기본: {DEFAULT_DB_PATH})",
    )
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
    # 사이클8 #3: argparse 는 guarded_main 보다 먼저 말한다 — argv 에 토큰 형태가
    # 있으면 **내용을 출력하지 않고** 일반 오류로 끝낸다.
    if reject_secret_argv(sys.argv[1:], _err):
        return 2
    args = parser.parse_args()

    markdown_path = Path(args.markdown)
    week = state_mod.week_from_markdown(markdown_path)
    if not state_mod.valid_week(week):
        _err(f"✗ 주차 파일명이 아닙니다: {markdown_path.name}")
        return 2
    state_path = state_mod.state_path_for_markdown(markdown_path)
    lock_path = state_mod.lock_path_for_markdown(markdown_path)

    # 전송 대상·토큰은 digests/ 를 읽지 않는다 → 잠금 **밖**에서 먼저 확인한다.
    # (잠금 대기가 초과되면 "발송 진행 중" 안내를 보내야 하므로 토큰이 필요하다)
    chat_id = thread_id = token = None
    if not args.dry_run:
        try:
            chat_id, thread_id = resolve_target(args.topic_key)
            token = resolve_token()
        except (RuntimeError, OSError, json.JSONDecodeError) as exc:
            _err(f"✗ 전송 대상 확인 실패: {redact(exc)}")
            return 2

    # 통합 2 #1: URL 생존 스냅샷도 잠금 **밖**에서 찍는다 — 네트워크 왕복이
    # 잠금을 붙들면 다른 작성자의 대기 한도를 잡아먹는다(텔레그램과 같은 이유).
    # 바이트 결속은 스냅샷의 SHA 가 맡는다: 잠금 안에서 읽은 본문과 다르면 무효다.
    snapshot = liveness_snapshot(markdown_path)

    # ── 잠금 ①: 파일 읽기·판정·승인 초안 발급까지. 텔레그램 왕복은 하지 않는다.
    # 사이클8 #4: 잠금 안에서는 파일 IO 만 한다 — 카드 제거·청크 전송을 잠금 안에서
    # 하면 다른 작성자의 60초 한도를 텔레그램 지연이 잡아먹는다.
    try:
        handle = state_mod.acquire_lock(lock_path, blocking=True)
    except state_mod.LockBusy as exc:
        if not args.dry_run:
            ok, _mid, error = send_chunk(
                token, chat_id, thread_id,
                "⏳ 발송 진행 중 — 미리보기를 보내지 않았습니다. 끝난 뒤 `/digest 재검토`.",
            )
            if not ok:
                _err(f"✗ 발송 진행 중 안내 전송 실패: {error}")
        # 사이클8 #5: 잠금 타임아웃 종료 코드는 다섯 CLI 모두 2 다.
        _err(f"✗ 미리보기 생략: 잠금 대기 실패({redact(exc)})")
        return 2

    prepared = None
    stale_card = None
    stale_previews = []
    stale_notices = []
    pending_queue = []
    hold_ids = None
    try:
        if not markdown_path.exists():
            _err(f"✗ 파일 없음: {markdown_path}")
            return 2

        # 승인 지문(approval.sha)은 발송 게이트와 같은 **원시 바이트** 해시다.
        # 렌더에 쓰는 텍스트도 같은 읽기에서 나와야 지문과 화면이 어긋나지 않는다.
        try:
            markdown_bytes = markdown_path.read_bytes()
            markdown_text = markdown_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _err(f"✗ 마크다운 읽기 실패: {redact(exc)}")
            return 2
        check_bytes, check = load_check(markdown_path)
        current_sha = markdown_sha256(markdown_bytes)
        current_check_sha = markdown_sha256(check_bytes) if check_bytes else None

        # 사이클4 #6 · 사이클5 #2: 렌더 **전에** 이 본문·이 검증으로 승인해도 되는지 본다.
        # 차단이면 항목 미리보기를 만들지 않고 승인 세대도 발급하지 않는다.
        blocked = _blocking_reason(markdown_path, week, check, current_sha,
                                   markdown_text, markdown_bytes, args.db,
                                   snapshot)
        if blocked:
            body = (
                "🏛 협의회 {} {}\n\n⚠️ {}\n\n"
                "`/digest 재검토` 로 다시 검증하세요."
            ).format(
                "월간 종합" if state_mod.issue_kind(week) == state_mod.KIND_MONTHLY
                else "주간 정책브리핑",
                week,
                blocked,
            )
            chunks = [body]
            item_urls = []
            hold_ids = None
        else:
            item_sections, _ = sections_mod.resolve(check, markdown_text)
            body = preview_mod.render_preview(week, markdown_text, check)
            chunks = preview_mod.chunk_text(body)
            # 병합 3회차: 번호 좌표(봇의 `제외 2,5`)는 **정본 순서**에서 나온다.
            # 정본과 본문의 순서가 어긋나면 checker 가 이미 멈춘다(재조립 필요).
            item_urls = _manifest_item_urls(markdown_path)
            if item_urls is None:
                item_urls = preview_mod.item_urls(markdown_text, item_sections)
            hold_ids = _hold_ids(markdown_text, item_sections)

        if args.dry_run:
            _out(f"[DRY-RUN] {week} 미리보기 {len(chunks)}개 메시지")
            for chunk in chunks:
                _out("---")
                _out(chunk)
            return 0

        try:
            current = state_mod.load_state(state_path, week)
        except state_mod.StateError as exc:
            _err(f"✗ 상태 확인 실패: {redact(exc)}")
            return 2
        # 이전 승인 카드의 message_id — 버튼 제거는 잠금 밖에서 한다.
        stale_card = state_mod.card_message_id(current)
        # V4 계약 ②: 이번 전송이 밀어낼 옛 메시지들. 삭제는 **새 메시지를 보낸
        # 뒤에** 잠금 밖에서 한다 — 먼저 지우면 전송이 깨졌을 때 편집자에게
        # 아무것도 남지 않는다.
        stale_previews = [
            int(mid) for mid in (current.get("preview_message_ids") or [])
        ]
        stale_notices = [
            int(mid) for mid in (current.get("notice_message_ids") or [])
        ]
        # 라운드 3: 지난 회차가 지우지 못하고 넘긴 정리 대기 큐.
        pending_queue = state_mod.superseded_message_ids(current)

        if blocked:
            # 차단이면 옛 승인을 **먼저 폐기한다** (사이클7 #2 규율). 못 지우면
            # 안내조차 보내지 않는다 — 옛 카드가 살아 있는 채로 끝나면 안 된다.
            try:
                state_mod.update_state_locked(
                    state_path, week, state_mod.clear_approval)
            except (state_mod.StateError, state_mod.TransitionError,
                    OSError) as exc:
                _err(f"✗ 승인 폐기 실패 — 안내를 보내지 않았습니다: {redact(exc)}")
                return 2
        else:
            # 사이클8 #4: 승인 초안을 **전송 전에** 잠금 안에서 발급한다.
            # message_id 는 전송 뒤 잠금 ②에서 붙인다.
            try:
                drafted = state_mod.update_state_locked(
                    state_path, week,
                    lambda cur: state_mod.record_preview(
                        cur, [], item_urls, current_sha, current_check_sha,
                        hold_ids),
                )
            except (state_mod.StateError, state_mod.TransitionError,
                    OSError) as exc:
                _err(f"✗ 승인 세대 발급 실패: {redact(exc)}")
                return 2
            prepared = dict(state_mod.approval_of(drafted))
    finally:
        state_mod.release_lock(handle)

    # ── 잠금 밖: 텔레그램 왕복 ────────────────────────────────────────────
    if stale_card:
        # notify 가 못 지웠으면 봇이 한 번 더 지운다. 실패는 경고만.
        ok, error = clear_card(token, chat_id, stale_card)
        if not ok:
            _err(f"⚠️  이전 승인 카드 버튼 제거 실패: {error}")

    message_ids = []
    send_error = None
    for index, chunk in enumerate(chunks, start=1):
        # 사이클5 #4: 미리보기 본문도 단일 발신 경로에서 redact 를 지난다.
        ok, message_id, error = send_chunk(
            token, chat_id, thread_id, redact(chunk))
        if not ok:
            send_error = f"전송 실패 ({index}/{len(chunks)}): {error}"
            break
        message_ids.append(message_id)
        _out(f"✓ 전송 {index}/{len(chunks)} message_id={message_id}")

    # V4 계약 ② (라운드 2, Codex MEDIUM): 새 미리보기가 **전부** 도착한 뒤에만
    # 옛 것을 지운다.
    #
    # 라운드 1 의 조건은 `if message_ids:` 였다 — 2조각 중 1조각만 성공한 부분 전송
    # 에서도 참이 되어, 반쪽 미리보기만 남기고 온전한 옛 미리보기를 지웠다.
    # 편집자에게 완전한 미리보기가 하나도 없는 상태가 만들어진다.
    #
    # 라운드 3 (Codex MEDIUM): **중복 message_id 도 전송 실패**다. 같은 id 를 두 번
    # 받으면 실제로 도착한 메시지는 하나뿐인데 개수만 맞는다 — 반쪽 미리보기를
    # 완전 전송으로 오판해 옛 미리보기를 지운다. 결과를 믿을 수 없으므로
    # send_error 를 세워 승인 폐기 경로(_finish_locked)로 보낸다: 삭제도, 승인
    # 카드도 없다.
    if send_error is None and message_ids:
        if any(mid is None for mid in message_ids):
            send_error = "전송 응답에 message_id 가 없습니다 — 결과를 믿을 수 없습니다"
        elif len(set(message_ids)) != len(message_ids):
            send_error = "중복 message_id({}) — 전송 결과를 믿을 수 없습니다".format(
                len(message_ids) - len(set(message_ids)))
        if send_error:
            _err(f"✗ {send_error}")

    # 부분 실패에서는 아무것도 지우지 않고, 도착해 버린 **새 조각들**과 상태에서
    # 사라질 옛 좌표를 정리 대기 큐로 넘긴다(승인은 어차피 폐기되므로 그 조각들은
    # 승인 대상이 아니다). 지우지 않는 이유는 실패의 증거를 편집자 화면에서
    # 없애지 않기 위해서다. 회수는 **다음 완전 전송 회차**가 한다 (라운드 3).
    delivered_all = (
        send_error is None
        and len(message_ids) == len(chunks)
        and len(set(message_ids)) == len(chunks)
    )
    superseded = []
    queue_after = list(pending_queue)
    if delivered_all and message_ids:
        if prepared is not None:
            # 새 미리보기가 통째로 도착했다 — 옛 것과 밀린 큐를 함께 회수한다.
            # 같은 id 가 두 자리(상태·큐)에 있을 수 있으므로 한 번만 지운다.
            superseded = []
            for stale in stale_previews + stale_notices + pending_queue:
                if stale not in superseded:
                    superseded.append(stale)
            queue_after = []
        else:
            # 차단 안내는 **안내만** 밀어낸다 — 미리보기를 지우면 `제외 n`·`핀 n` 의
            # 번호 좌표가 편집자 화면에서 사라진다. 큐도 건드리지 않는다.
            superseded = list(stale_notices)
        for stale in superseded:
            ok, error = delete_message(token, chat_id, stale)
            if not ok:
                _err(f"⚠️  옛 메시지 삭제 실패(진행함) message_id={stale}: {error}")
    elif message_ids or send_error:
        pending = [mid for mid in message_ids if mid is not None]
        if prepared is not None:
            # 잠금 ①의 승인 초안 발급이 preview_message_ids 를 이미 비웠다 —
            # 큐로 옮기지 않으면 옛 미리보기를 가리키는 좌표가 어디에도 남지 않는다.
            pending += stale_previews + stale_notices
        queue_after = pending_queue + [
            mid for mid in pending if mid not in pending_queue
        ]
        _err("⚠️  미완 전송({}/{}) — 아무것도 지우지 않고 {}건을 정리 대기 큐에 넘겼습니다"
             .format(len(message_ids), len(chunks), len(queue_after)))

    # ── 잠금 ②: 준비 시점의 세대가 그대로일 때만 message_id 를 기록한다 ──
    try:
        handle = state_mod.acquire_lock(lock_path, blocking=True)
    except state_mod.LockBusy as exc:
        _err(f"✗ 상태 기록 실패(미리보기는 전송됨): 잠금 대기 실패({redact(exc)})")
        return 1

    try:
        rc, drop_card, message = _finish_locked(
            state_path, week, prepared, message_ids, item_urls, send_error,
            hold_ids, queue_after)
    finally:
        state_mod.release_lock(handle)

    # 카드 회수는 잠금 밖에서 (사이클8 #4)
    if drop_card:
        ok, error = clear_card(token, chat_id, drop_card)
        if not ok:
            _err(f"⚠️  폐기한 승인 카드 버튼 제거 실패: {error}")
    if message:
        (_out if rc == 0 else _err)(message)
    return rc


def _finish_locked(state_path, week, prepared, message_ids, item_urls,
                   send_error, hold_ids=None, queue_after=()):
    """잠금 ② — **파일 IO 만** 한다. (종료 코드, 회수할 카드 id, 안내문).

    준비 시점(잠금 ①)에 발급한 승인 세대가 그대로일 때만 message_id 를 붙인다.
    전송이 깨졌거나 그 사이 다른 작성자가 세대를 바꿨으면 폐기하고 카드를 회수한다.
    """
    try:
        current = state_mod.load_state(state_path, week)
    except state_mod.StateError as exc:
        return 1, None, f"⚠️  상태 기록 실패(미리보기는 전송됨): {redact(exc)}"

    if prepared is None:
        # 차단 안내만 보냈다 — 승인은 이미 폐기됐다. 안내의 message_id 는 **별도
        # 필드**(notice_message_ids)에 남긴다: 통합 2 #2 이전에는 안내가
        # preview_message_ids 를 덮어써서, 늦게 끝난 차단 안내가 그 사이 완료된
        # 최신 미리보기의 번호 좌표를 `[]` 로 지웠다(제외 n 이 아무것도 못 가리킨다).
        if send_error:
            return 2, None, f"✗ {send_error}"
        try:
            state_mod.update_state_locked(
                state_path, week,
                lambda cur: state_mod.set_superseded(
                    state_mod.record_notice_messages(cur, message_ids),
                    queue_after),
            )
        except (state_mod.StateError, state_mod.TransitionError,
                OSError) as exc:
            return 1, None, f"⚠️  상태 기록 실패(안내는 전송됨): {redact(exc)}"
        # 통합 3 #2: 안내는 승인을 만들지도 지우지도 않는다 — 로그가 "승인 없음" 을
        # 단정하면 그 사이 다른 미리보기가 발급한 최신 승인을 오보한다.
        live_id = state_mod.approval_of(
            state_mod.load_state(state_path, week)).get("id")
        return 0, None, "✓ 안내 기록: {} (approval={})".format(
            state_path, live_id or "없음(검증 필요)")

    live = state_mod.approval_of(current)
    same_generation = (
        live.get("id") == prepared.get("id")
        and live.get("sha") == prepared.get("sha")
        and live.get("check_sha") == prepared.get("check_sha")
    )
    if not same_generation and live.get("id") != prepared.get("id"):
        # 통합 1 #3: **다른 notify 가 발급한 최신 승인**은 건드리지 않는다.
        # 예전에는 A 준비 → B 미리보기 완료 → A 완료 순서에서 A 가 B 의 승인을
        # 지웠다(Codex 통합 게이트 MEDIUM). 자기 세대만 폐기한다.
        return (2 if send_error else 1), None, (
            "⚠️  전송 중 다른 미리보기가 승인을 갱신했습니다 — "
            "이 회차의 기록은 버립니다(최신 승인은 그대로)")

    if send_error or not same_generation:
        drop_card = live.get("card_message_id")
        try:
            # 라운드 2·3: 미완 전송으로 도착해 버린 새 조각과, 상태에서 사라질
            # 옛 좌표를 정리 대기 큐에 남긴다 — 다음 완전 전송 회차가 회수한다.
            state_mod.update_state_locked(
                state_path, week,
                lambda cur: state_mod.set_superseded(
                    state_mod.clear_approval(cur), queue_after))
        except (state_mod.StateError, state_mod.TransitionError,
                OSError) as exc:
            return (2 if send_error else 1), drop_card, \
                f"⚠️  승인 폐기 실패: {redact(exc)}"
        if send_error:
            return 2, drop_card, f"✗ {send_error} — 승인 세대를 폐기했습니다"
        return 1, drop_card, (
            "✗ 전송 중 본문·검증이 바뀌었습니다 — 승인 세대를 폐기했습니다"
            " (`/digest 재검토`)")
    try:
        state_mod.update_state_locked(
            state_path, week,
            lambda cur: state_mod.set_superseded(
                state_mod.record_preview_messages(
                    cur, message_ids, item_urls, hold_ids),
                queue_after),
        )
    except (state_mod.StateError, state_mod.TransitionError, OSError) as exc:
        return 1, None, f"⚠️  상태 기록 실패(미리보기는 전송됨): {redact(exc)}"
    return 0, None, "✓ 상태 기록: {} (approval={})".format(
        state_path, prepared.get("id"))


def guarded_main():
    """예외·traceback 까지 redact 해서 내보낸다 (사이클6 #7)."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:       # noqa: BLE001
        import traceback
        _err(traceback.format_exc())
        return 70


if __name__ == "__main__":
    sys.exit(guarded_main())
