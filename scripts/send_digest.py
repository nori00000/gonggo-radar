#!/usr/bin/env python3
"""주간 정책브리핑 다이제스트 발송 스크립트."""

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
from alert.digest import prune
from alert.digest import sections as sections_mod
from alert.digest.composer import fit_prose_urls, load_items_manifest
from alert.digest import state as state_mod
from alert.digest.preview import MARKER
from alert.digest.checker import (
    check_expired,
    liveness_problem,
    liveness_snapshot,
    markdown_sha256,
    recheck_manifest,
)
from alert.digest.checker import CHECK_EXPIRED_REASON
from alert.utils.redact import redact
from alert.utils.safe_argparse import (
    RedactingArgumentParser,
    reject_secret_argv,
)

# 상태 기록(status=sent) 저장 재시도 — 여기서 실패하면 "발송했는데 기록이 없는" 창이 열린다.
STATE_SAVE_ATTEMPTS = 3
STATE_SAVE_BACKOFF = 0.5

# 승인 세대 id 형식 (사이클4 #2). 접두 비교를 폐지했으므로 지문 대신 세대 id 를 받는다.
APPROVAL_ID_RE = re.compile(r"\A[0-9a-f]{%d}\Z" % state_mod.APPROVAL_ID_LEN)


def _err(message) -> None:
    """발송기의 **모든** stderr 출력 (사이클5 #4).

    check.json 의 reason 이나 예외 문자열에 토큰이 섞여 launchd 로그·봇 카드 편집으로
    새지 않도록 한 곳에서 redact 한다.
    """
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    """발송기의 stdout 출력 (봇이 수신자 수를 파싱한다)."""
    print(redact(message))


# 링크 파서는 **하나**다 (사이클 6 #1·#4 + 계약 W10 사이클7 #3):
# `alert.digest.blocks.find_links` — 렌더러가 만드는 href 와 검사기(body_urls)가
# 보는 URL 이 같은 함수에서 나와야 "검사한 곳과 다른 데로 가는" 링크가 없다.
# 자체 정규식(LINK_PATTERN)은 폐지했다 — 두면 ①`…/report(2026)` 같은 괄호 포함
# URL 을 잘라 먹고 ②checker·prune 과 다른 URL 을 보게 된다.
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
        # sentinel 에는 `*`·`_` 가 없다 — 굵게 정규식이 sentinel 을 씹으면 앵커가
        # 깨진다 (계약 W10 사이클8 #2).
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

        # 순서가 곧 안전이다 (사이클8 #2): 이스케이프 → 굵게(**텍스트만**) →
        # 앵커 복원. 예전에는 앵커를 먼저 복원하고 HTML 전체에 굵게를 걸어서,
        # href 안의 `**report**` 가 `<strong>report</strong>` 로 바뀌었다 —
        # 검사한 URL 과 실제 목적지가 갈리는 경로였다.
        escaped_line = html_module.escape(modified_line)

        # **굵은텍스트** 처리 — sentinel 복원 **전에** 한다 (사이클 6 #4 /
        # Codex 신규 #7 · 계약 W10 사이클8 #2). 뒤에 하면 href 안의 `**` 가
        # <strong> 으로 바뀌어 링크가 깨진다. 정규식은 sentinel(\x00)을 넘지
        # 못한다 — 링크를 걸친 굵게는 렌더하지 않는다.
        escaped_line = re.sub(
            r"\*\*([^*\x00]+)\*\*",
            r"<strong>\1</strong>",
            escaped_line
        )

        # 마지막에 sentinel 을 실제 링크로 복원 — href 는 어떤 치환도 지나지 않는다.
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


DEFAULT_DB_PATH = "alert/data/announcements.db"


def send_digest(
    markdown_path: Path,
    to_email: str = None,
    dry_run: bool = True,
    approved_by=None,
    approval_id: str = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """다이제스트 발송 — **발송기가 유일한 권위**다 (계약 W10 사이클6 #1).

    잠금 밖의 선행 검사는 하나도 두지 않는다. 중복 검사는 안전감만 주고 경합을
    만든다(표식·검증을 잠금 전에 보던 구멍이 실제 재현됐다). flock 을 쥔 뒤에
    tombstone·state·md 바이트·check 바이트를 **각각 한 번만** 읽고, 모든 SHA·판정을
    그 바이트에서 다시 계산한다.

    Args:
        markdown_path: 마크다운 파일 경로
        to_email: 수신자 이메일 (기본: config에서)
        dry_run: 드라이런 모드 (기본: True) — 판정 경로는 실발송과 같다
        approved_by: 발송을 승인한 텔레그램 user_id (상태 파일에 기록)
        approval_id: 승인 카드의 세대 id (`--send` 필수)
        db_path: 정본 재대조·마감 재판정에 쓰는 announcements.db 경로

    Returns:
        종료 코드 (0: 성공, 2: 거부·실패, 1: 발송 후 기록 실패)
    """
    markdown_path = Path(markdown_path)
    week = state_mod.week_from_markdown(markdown_path)

    # 인자·이름 검증만 잠금 밖에서 한다 (디스크 상태를 보지 않는다).
    if not state_mod.valid_week(week):
        _err(f"✗ 발송 거부: 주차 형식이 아닙니다: {week!r}")
        return 2

    approval = (approval_id or "").strip().lower()
    if not dry_run and not APPROVAL_ID_RE.match(approval):
        problem = "없습니다" if not approval else f"형식이 아닙니다: {approval!r}"
        _err(
            f"✗ 발송 거부: --approval-id 가 {problem} "
            f"({state_mod.APPROVAL_ID_LEN}자 16진수)"
        )
        return 2

    lock_path = state_mod.lock_path_for_markdown(markdown_path)

    # ⓪-a 잠금이 비어 있는지 **먼저** 확인하고 곧바로 놓는다.
    # 남이 쥐고 있으면 여기서 끝난다 — 바이트도 읽지 않고 네트워크도 쓰지 않는다
    # (발송기의 "잠금 밖 판정 0" 규율, 사이클6 #1). 판정을 하지 않으므로 규율은
    # 그대로이고, 잠금 보유 시간만 짧아진다.
    try:
        state_mod.release_lock(state_mod.acquire_lock(lock_path))
    except state_mod.LockBusy as exc:
        _err(f"✗ 발송 거부: {exc}")
        return 2
    except OSError as exc:
        _err(f"✗ 발송 거부: 잠금 생성 실패 — {exc}")
        return 2

    # ⓪-b 잠금 **밖** 1단계: URL 생존 스냅샷 (통합 2 #1 · 3 #1).
    # 네트워크는 느리다 — URL 4개가 HEAD·GET 8초씩 걸리면 검사만 60초를 넘겨
    # 다른 작성자의 대기 한도를 통째로 먹는다(Codex 통합 3차 MEDIUM 실측).
    # 통합 2 에서는 이 호출이 acquire_lock **뒤**에 있었다 — 주석은 "잠금 밖" 이라고
    # 적혀 있었고 코드는 잠금 안이었다. 보고와 코드가 갈라진 자리다.
    snapshot = liveness_snapshot(markdown_path)

    # ① 여기서부터가 진짜 잠금 구간이다. 위에서 비어 있었어도 그 사이 다른
    # 작성자가 쥘 수 있다 — 그러면 LockBusy 로 끝난다(판정 없음).
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
            week=week,
            to_email=to_email,
            dry_run=dry_run,
            approved_by=approved_by,
            approval_id=approval,
            db_path=db_path,
            snapshot=snapshot,
        )
    finally:
        state_mod.release_lock(lock_handle)


def _subject(markdown_text: str) -> str:
    """첫 `# 제목` (없으면 기본 제목)."""
    matched = re.search(r"^# (.+)$", markdown_text, re.MULTILINE)
    return matched.group(1) if matched else "협의회 주간 정책브리핑"


def _send_locked(
    markdown_path: Path,
    week: str,
    to_email,
    dry_run: bool,
    approved_by,
    approval_id: str,
    db_path: str = DEFAULT_DB_PATH,
    snapshot=None,
) -> int:
    """flock 을 쥔 상태의 전 과정 (사이클6 #1).

    순서: tombstone → state → md 바이트 → check 바이트 → SHA → 제어 문자 →
    pass/items/blocks → 마커 → 제외(본문 전체 URL) → 승인 세대 → 렌더 → 발송 → 기록.
    """
    check_json_path = markdown_path.with_suffix(".check.json")
    state_path = state_mod.state_path_for_markdown(markdown_path)

    # ① 검증 파괴 표식 (잠금 안에서 본다 — 잠금 전 검사는 경합이었다)
    broken = state_mod.tombstone_reason(
        state_mod.tombstone_path_for_markdown(markdown_path))
    if broken:
        _err(f"✗ 발송 거부: {state_mod.TOMBSTONE_REASON} [{broken}]")
        return 2

    # ② 상태 (손상은 fail-closed)
    try:
        state = state_mod.load_state(state_path, week)
    except state_mod.StateError as exc:
        _err(f"✗ 발송 거부: {exc}")
        return 2

    # ③ 본문 바이트 — 한 번만 읽는다. 이 바이트가 해시·판정·렌더·발송의 유일한 원본.
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

    # ④ 검증 파일 바이트 — 한 번만 읽는다 (pass 판정과 SHA 가 같은 읽기에서 나온다)
    try:
        check_bytes = check_json_path.read_bytes()
    except FileNotFoundError:
        _err("✗ 발송 거부: 검증 파일 없음")
        return 2
    except OSError as exc:
        _err(f"✗ 발송 거부: 검증 파일 읽기 실패 — {exc}")
        return 2
    try:
        check_result = json.loads(check_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _err(f"✗ 발송 거부: 검증 파일 손상: {exc}")
        return 2
    if not isinstance(check_result, dict):
        _err("✗ 발송 거부: 검증 파일이 객체가 아님")
        return 2

    # ⑤ 모든 SHA 를 그 바이트에서 계산
    current_sha = markdown_sha256(markdown_bytes)
    current_check_sha = markdown_sha256(check_bytes)

    # ⑥ 제어 문자 (사이클6 #2) — 파서마다 줄 수가 달라지는 본문은 받지 않는다
    if prune.control_chars(markdown_text):
        _err("✗ 발송 거부: 본문에 제어 문자 포함 ({})".format(
            prune.control_chars_label(markdown_text)))
        return 2

    # ⑦ 마커 (기존 게이트 불변)
    if MARKER in markdown_text:
        _err("✗ 발송 거부: 마크다운에 미확정 마커가 있습니다")
        return 2

    # ⑧ 검증 결과 — 본문 결속·pass·항목 수
    recorded_hash = check_result.get("markdown_sha256")
    if not isinstance(recorded_hash, str) or not recorded_hash:
        _err("✗ 발송 거부: 검증 파일에 마크다운 SHA-256이 없음")
        return 2
    if not hmac.compare_digest(recorded_hash, current_sha):
        _err("✗ 발송 거부: 마크다운이 검증 후 변경됨")
        return 2
    if not check_result.get("pass", False):
        failed = [
            item.get("url")
            for item in check_result.get("items", [])
            if not item.get("passed", False)
        ]
        reason = (f"검증 실패: {len(failed)}개 항목" if failed
                  else check_result.get("reason", "알 수 없는 검증 실패"))
        _err(f"✗ 발송 거부: {reason}")
        return 2
    if not check_result.get("network_checked", False):
        _err("✗ 발송 거부: 네트워크 검증이 실행되지 않음")
        return 2
    if len(check_result.get("items") or []) < 1:
        _err("✗ 발송 거부: 검증에 항목이 0건")
        return 2
    if int(check_result.get("item_blocks") or 0) < 1:
        _err("✗ 발송 거부: 항목 0건 (공고 블록 없음)")
        return 2

    # ⑨ 렌더된 항목 수가 검증과 같은가 (사이클6 — \\v 로 항목을 숨기던 경로 차단)
    # 병합 3회차: 항목 수의 근거는 **정본(items.json)** 이다 — checker 가 정본↔본문↔
    # DB 를 1:1 로 묶어 두었으므로, 발송기가 볼 "그 주차의 항목" 은 정본의 항목이다.
    # 정본이 없으면 check.json 의 숫자로 물러선다(정본을 요구하는 것은 checker 의
    # 몫이고 그 실패는 위 ⑧ 에서 pass=false 로 이미 걸린다).
    item_sections, _ = sections_mod.resolve(check_result, markdown_text)
    rendered_blocks = prune.item_block_count(markdown_text, item_sections)
    manifest = load_items_manifest(markdown_path)
    expected_blocks = (
        len(manifest.get("items") or []) if manifest is not None
        else int(check_result.get("item_blocks") or 0)
    )
    if rendered_blocks != expected_blocks:
        _err(
            "✗ 발송 거부: 본문의 항목 수({})와 검증의 항목 수({})가 다릅니다".format(
                rendered_blocks, expected_blocks)
        )
        return 2

    # ⑨-2 정본 재대조 (통합 1 #1·#2) — **checker 와 같은 함수**를 잠금 안에서 다시
    # 돌린다. 개수만 보면 items.json 의 순서만 바꾼 본문이 통과했고(번호 좌표가
    # 미리보기와 갈렸다), 검증 뒤 DB 가 바뀐 것·오늘 기준 마감 경과도 놓쳤다.
    # 판정은 하나뿐이어야 한다 — 여기서 재구현하지 않는다.
    manifest_issues = recheck_manifest(
        markdown_path, markdown_bytes, markdown_text, check_result, db_path)
    if manifest_issues:
        _err("✗ 발송 거부: 정본 대조 실패 — {}".format(manifest_issues[0]))
        return 2

    # ⑨-3 생존 판정의 나이 (통합 2 #1). 검증은 "그때 살아 있었다" 는 기록이다 —
    # 하루가 지난 판정을 근거로 보내면 죽은 링크가 실린 메일이 나간다.
    if check_expired(check_result):
        _err(f"✗ 발송 거부: {CHECK_EXPIRED_REASON}")
        return 2

    # ⑨-4 발송 직전 URL 생존 (통합 2 #1). 스냅샷은 잠금 밖에서 찍었고, 여기서
    # **지금 바이트와 같은지** 확인한다 — 다르면 스냅샷은 무효다.
    dead_problem = liveness_problem(snapshot, current_sha)
    if dead_problem:
        _err(f"✗ 발송 거부: {dead_problem}")
        return 2

    # ⑩ 제외 미반영 — 본문 **전체** URL 기준 (사이클6 #3)
    leftover = state_mod.excluded_still_present(
        state, prune.body_urls(markdown_text))
    if leftover:
        _err(
            f"✗ 발송 거부: {state_mod.EXCLUDED_NOT_APPLIED_REASON} "
            f"({len(leftover)}건, 예: {leftover[0]})"
        )
        return 2

    subject = _subject(markdown_text)

    if dry_run:
        html_text = markdown_to_html(markdown_text)
        recipients = to_email if to_email else "[config에서 설정]"
        _out(f"[DRY-RUN] 발송 대상: {recipients}")
        _out(f"[DRY-RUN] 제목: {subject}")
        _out(
            f"[DRY-RUN] 본문 길이: {len(markdown_text)} bytes (마크다운), "
            f"{len(html_text)} bytes (HTML)"
        )
        return 0

    # ⑪ 상태 — 발송 가능 여부(sent·sending·만료·차단 플래그)
    # 라운드 2 (Codex MEDIUM): 상태만 보면 "만료됐어야 하는데 잠금 경합으로 만료를
    # 못 한 호" 가 통과한다. **디스크에 더 새로운 주간호가 있으면** 이 호는 지면을
    # 대표하지 않는다 — 상태와 무관하게 막는다.
    allowed, reason = state_mod.can_send(
        state, newer_issue=state_mod.newer_weekly_issue(
            markdown_path.parent, week))
    if not allowed:
        _err(f"✗ 발송 거부: {reason}")
        if reason == state_mod.SENDING_REASON:
            _err(
                "  이전 발송의 결과가 확정되지 않았습니다. 메일함을 확인한 뒤 "
                f"`/digest 해제 {week}` 로 풀어주세요(자동 재발송 안 함)."
            )
        return 2

    # ⑫ 승인 세대 — id + 본문 전체 SHA + 검증 파일 SHA
    ok, reason = state_mod.check_approval(
        state, approval_id, current_sha, current_check_sha
    )
    if not ok:
        _err(f"✗ 발송 거부: {reason}")
        return 2

    # ⑬ SMTP 앞의 준비는 sending 표시 **전**에 끝낸다
    try:
        notifier = EmailNotifier()
    except Exception as exc:  # noqa: BLE001 — 설정·의존성 오류는 발송 전 거부
        _err(f"✗ 초기화 실패: {exc}")
        return 2
    if not notifier.sender or not notifier.password:
        _err("✗ 이메일 인증 정보가 설정되지 않았습니다")
        return 2
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
        _err(f"✗ 발송 거부: 발송 표시 기록 실패 — {exc}")
        return 2

    # ⑭ 발송
    try:
        delivered, stage = notifier.send_html_staged(
            subject, html_body, recipients
        )
    except Exception as exc:  # noqa: BLE001 — 전송 중 예외는 결과 미확정이다
        _err(f"✗ 발송 실패(결과 미확정): {exc}")
        _keep_sending_notice(state_path, week)
        return 2

    if not delivered:
        _err(f"✗ 발송 실패: SMTP 전송에 실패했습니다 (단계: {stage})")
        if stage in UNSENT_STAGES:
            _revert_sending(state_path, state, stage)
        else:
            _keep_sending_notice(state_path, week)
        return 2

    _out(f"✓ 발송 성공: {len(recipients)}명")

    # ⑮ 기록
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
    parser = RedactingArgumentParser(
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
        "--db", default=DEFAULT_DB_PATH,
        help=f"announcements.db 경로 (기본: {DEFAULT_DB_PATH})",
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

    # 사이클8 #3: argparse 는 guarded_main 보다 먼저 말한다 — argv 에 토큰 형태가
    # 있으면 **내용을 출력하지 않고** 일반 오류로 끝낸다.
    if reject_secret_argv(sys.argv[1:], _err):
        return 2
    args = parser.parse_args()

    # --dry-run이 --send보다 우선 (명시적 안전 플래그)
    dry_run = args.dry_run or not args.send

    return send_digest(
        markdown_path=args.markdown,
        to_email=args.to,
        dry_run=dry_run,
        approved_by=args.approved_by,
        approval_id=args.approval_id,
        db_path=args.db,
        )


def guarded_main():
    """예외·traceback 까지 redact 해서 내보낸다 (사이클6 #7).

    발송기의 stderr 는 봇 카드 편집·launchd 로그로 흘러간다 — 최상위 traceback 이
    가려지지 않으면 check.reason 이나 설정 값의 토큰이 그대로 노출된다.
    """
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:       # noqa: BLE001 — 어떤 예외도 원문으로 새지 않게
        import traceback
        _err(traceback.format_exc())
        return 70


if __name__ == "__main__":
    sys.exit(guarded_main())
