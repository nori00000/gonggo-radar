#!/usr/bin/env python3
"""협의회 의견·이번 주 한 줄의 확정 마커를 본문으로 치환 (계약 W10).

CLI 는 **플래그 인터페이스만** 받는다 (사이클6 #8, V1 브랜치 계약과 동일):

    apply_commentary.py <week> --commentary "<협의회 의견 본문>"
    apply_commentary.py <week> --headline   "<이번 주 한 줄>"

플래그가 없으면 exit 2 다 — 위치 인자로 "무엇을 채우는지" 추론하지 않는다.

본문은 원문 그대로 저장한다 — HTML 이스케이프는 발송 단계(send_digest)가 한다.
잠금을 먼저 쥐고 상태를 확인한 뒤에만 파일을 쓴다 (사이클3 #6).
"""

import sys
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest import prune
from alert.digest import state as state_mod
from alert.digest.preview import MARKER
from alert.utils.redact import redact
from alert.utils.safe_argparse import (
    RedactingArgumentParser,
    reject_secret_argv,
)

NOOP_MESSAGE = "⚠️  마커 없음 — 변경하지 않았습니다 (이미 채워졌습니다)"

# 계약 W10 크리틱 #7: 해설에 HTML 주석 구분자가 들어오면 거부한다.
# 발송 렌더러(send_digest.markdown_to_html)는 `<!--` 로 시작하는 줄을 버리므로,
# 주석을 품은 해설은 승인 마커까지 삼키면서 메일에서 조용히 사라진다.
COMMENT_TOKENS = ("<!--", "-->")
COMMENT_REJECT = (
    "✗ HTML 주석 구분자(<!-- 또는 -->)를 쓸 수 없습니다 — "
    "발송 렌더러가 해당 줄을 버려 내용이 조용히 사라집니다"
)

# 본문을 써도 되는 상태 (사이클3 #6). sending·sent 는 파일을 건드리지 않는다.
WRITABLE_STATUSES = ("draft", "annotated", "held")

# "이번 주 한 줄" 줄의 접두 (계약 v2). 이 줄의 마커는 --headline 이 채운다.
HEADLINE_PREFIX = "이번 주 한 줄"


def _err(message) -> None:
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    print(redact(message))


def text_error(text: str, label: str = "본문") -> str:
    """받아들일 수 없는 이유. 문제없으면 빈 문자열."""
    if not (text or "").strip():
        return f"✗ {label}이 비었습니다"
    if any(token in text for token in COMMENT_TOKENS):
        return COMMENT_REJECT
    if prune.control_chars(text):
        return "✗ {}에 제어 문자가 있습니다 ({})".format(
            label, prune.control_chars_label(text))
    return ""


def _is_headline_line(line: str) -> bool:
    return line.strip().startswith(HEADLINE_PREFIX)


def apply_headline(markdown_text: str, headline: str):
    """(새 본문, 치환했는가) — `이번 주 한 줄` 줄의 마커만 바꾼다."""
    lines = (markdown_text or "").split("\n")
    for index, line in enumerate(lines):
        if _is_headline_line(line) and MARKER in line:
            lines[index] = line.replace(MARKER, headline.strip(), 1)
            return "\n".join(lines), True
    return markdown_text, False


def apply_commentary(markdown_text: str, commentary: str):
    """(새 본문, 치환했는가) — **한 줄 줄이 아닌** 첫 마커를 바꾼다.

    계약 v1.2 본문에는 마커가 협의회 의견에 하나뿐이고, 계약 v2 본문에는
    "이번 주 한 줄" 에도 하나가 더 있다. 해설은 한 줄 줄을 건드리지 않는다.
    """
    lines = (markdown_text or "").split("\n")
    for index, line in enumerate(lines):
        if MARKER in line and not _is_headline_line(line):
            lines[index] = line.replace(MARKER, commentary.strip(), 1)
            return "\n".join(lines), True
    return markdown_text, False


def main():
    parser = RedactingArgumentParser(
        description="협의회 의견·이번 주 한 줄 확정 마커 치환")
    parser.add_argument("week", help="주차 (예: 2026-W37)")
    parser.add_argument("--commentary", help="협의회 의견 본문")
    parser.add_argument("--headline", help="이번 주 한 줄")
    parser.add_argument(
        "--out-dir", default="digests", help="다이제스트 디렉토리 (기본: digests)"
    )
    # 사이클8 #3: argparse 는 guarded_main 보다 먼저 말한다 — argv 에 토큰 형태가
    # 있으면 **내용을 출력하지 않고** 일반 오류로 끝낸다.
    if reject_secret_argv(sys.argv[1:], _err):
        return 2
    args = parser.parse_args()

    if not state_mod.valid_week(args.week):
        _err(f"✗ 주차 형식이 아닙니다: {args.week!r}")
        return 2

    chosen = [
        (kind, value)
        for kind, value in (("commentary", args.commentary),
                            ("headline", args.headline))
        if value is not None
    ]
    if len(chosen) != 1:
        _err("✗ --commentary 또는 --headline 중 **하나**를 지정하세요")
        return 2
    kind, raw_text = chosen[0]
    label = "협의회 의견" if kind == "commentary" else "이번 주 한 줄"

    text = raw_text.strip()
    error = text_error(text, label)
    if error:
        _err(error)
        return 2

    markdown_path = Path(args.out_dir) / f"{args.week}.md"
    if not markdown_path.exists():
        _err(f"✗ 파일 없음: {markdown_path}")
        return 2

    # 계약 W10 사이클3 #6: **잠금을 먼저 쥐고 상태를 확인한 뒤** 본문을 쓴다.
    state_path = state_mod.state_path(args.week, args.out_dir)
    lock_path = state_mod.lock_path(args.week, args.out_dir)
    # 사이클7 #1: 발송기만 즉시 거부(LOCK_NB)다. 그 밖의 작성자는 블로킹 대기 후
    # 한도를 넘기면 본문을 건드리지 않고 실패한다.
    try:
        handle = state_mod.acquire_lock(lock_path, blocking=True)
    except (state_mod.LockBusy, OSError) as exc:
        _err(f"✗ {label} 적용 거부: {exc}")
        return 2

    try:
        try:
            state = state_mod.load_state(state_path, args.week)
        except state_mod.StateError as exc:
            _err(f"✗ {label} 적용 거부: {exc}")
            return 2

        if state.get("status") not in WRITABLE_STATUSES:
            _err(
                f"✗ {label} 적용 거부: status={state.get('status')} "
                "— 본문을 바꾸지 않았습니다"
            )
            return 2

        markdown_text = markdown_path.read_text(encoding="utf-8")
        if prune.control_chars(markdown_text):
            _err("✗ {} 적용 거부: 본문에 제어 문자 포함 ({})".format(
                label, prune.control_chars_label(markdown_text)))
            return 2

        apply_fn = apply_commentary if kind == "commentary" else apply_headline
        updated, replaced = apply_fn(markdown_text, text)

        if not replaced:
            _out(NOOP_MESSAGE)
            return 0

        markdown_path.write_text(updated, encoding="utf-8")
        _out(f"✓ {label} 적용: {markdown_path}")

        # 상태 기록은 해설(협의회 의견)만 annotated 로 올린다 — 한 줄은 본문 편집이다.
        if kind != "commentary":
            return 0
        try:
            state_mod.update_state_locked(
                state_path, args.week,
                lambda current: state_mod.mark_annotated(current, text),
            )
            _out(f"✓ 상태 기록: {state_path} (status=annotated)")
        except (state_mod.StateError, state_mod.TransitionError, OSError) as exc:
            _err(f"⚠️  상태 기록 실패(본문은 적용됨): {exc}")
            return 1
    finally:
        state_mod.release_lock(handle)

    return 0


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
