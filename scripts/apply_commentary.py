#!/usr/bin/env python3
"""발송본의 사람 확정 필드를 채운다 (계약 W10 + 사이클 7).

CLI 는 **플래그 인터페이스만** 받는다 — 위치 인자로 "무엇을 채우는지" 추론하지
않는다(플래그가 하나도 없으면 exit 2). 확정 자리는 **둘**이고 서로 다른 자리다
(Codex 3차 MEDIUM #3: 예전에는 위치 인자 하나를 받아 "이번 주 한 줄"에 넣고
상태에는 commentary 로 저장해, 의견을 넣으면 상단이 확정되고 의견은 비었다):

  `--headline "<한 줄>"`    → `이번 주 한 줄:` 자리 (확정 마커 치환)
  `--commentary "<본문>"`  → `## 🤝 협의회에서` 섹션 본문

둘을 한 번에 줄 수도 있다 — 서로 다른 자리이므로 섞이지 않는다.
둘 다 **멱등**이다. 이미 채워진 자리에 다시 적용하면 덮어쓰고 그 사실을 로그로
남긴다 — 조용한 no-op 은 없다(편집자가 "적용됐다"고 믿게 만들기 때문이다).

본문은 원문 그대로 저장한다 — HTML 이스케이프는 발송 단계(send_digest)가 한다.
잠금을 먼저 쥐고 상태를 확인한 뒤에만 파일을 쓴다 (사이클3 #6).
"""

import sys
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest import prune
from alert.digest import state as state_mod
from alert.digest.composer import (
    SECTION_COUNCIL,
    SECTION_HEADINGS,
    SECTION_MEMBER,
    kakao_file_text_from_markdown,
    refresh_manifest_binding,
)
from alert.digest.preview import MARKER
from alert.utils.redact import redact
from alert.utils.safe_argparse import (
    RedactingArgumentParser,
    reject_secret_argv,
)

COUNCIL_HEADING = f"## {SECTION_HEADINGS[SECTION_COUNCIL]}"
MEMBER_HEADING = f"## {SECTION_HEADINGS[SECTION_MEMBER]}"
HEADLINE_PREFIX = "이번 주 한 줄:"

# 본문을 써도 되는 상태 (사이클3 #6). sending·sent 는 파일을 건드리지 않는다.
WRITABLE_STATUSES = ("draft", "annotated", "held")

# 계약 W10 크리틱 #7: 확정 본문에 HTML 주석 구분자가 들어오면 거부한다.
# 발송 렌더러(send_digest.markdown_to_html)는 `<!--` 로 시작하는 줄을 버리므로,
# 주석을 품은 본문은 승인 마커까지 삼키면서 메일에서 조용히 사라진다.
COMMENT_TOKENS = ("<!--", "-->")
COMMENT_REJECT = (
    "✗ 확정 본문에 HTML 주석 구분자(<!-- 또는 -->)를 쓸 수 없습니다 — "
    "발송 렌더러가 해당 줄을 버려 본문이 조용히 사라집니다"
)
NO_FIELD_MESSAGE = (
    "✗ --headline 또는 --commentary 중 하나는 있어야 합니다 "
    "(어느 자리를 확정할지 추론하지 않습니다)"
)
# 사이클 8 #5: "이번 주 한 줄"은 **한 줄**이다. 여러 줄을 받으면 둘째 줄부터가
# 별도 산문 줄로 남아, 다시 적용할 때 꼬리가 누적됐다(멱등 깨짐).
HEADLINE_MULTILINE_REJECT = (
    "✗ --headline 은 한 줄이어야 합니다 (개행이 들어오면 둘째 줄부터가 "
    "본문에 남아 재적용 시 누적됩니다) — 여러 줄은 --commentary 를 쓰세요"
)

# 본문을 써도 되는 상태 (사이클3 #6). sending·sent 는 파일을 건드리지 않는다.
WRITABLE_STATUSES = ("draft", "annotated", "held")

def _err(message) -> None:
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    print(redact(message))


def text_error(text: str, label: str = "확정 본문") -> str:
    """확정 입력으로 받아들일 수 없는 이유. 문제없으면 빈 문자열."""
    if not (text or "").strip():
        return f"✗ {label}이 비었습니다"
    if any(token in text for token in COMMENT_TOKENS):
        return COMMENT_REJECT
    if prune.control_chars(text):
        return "✗ {}에 제어 문자가 있습니다 ({})".format(
            label, prune.control_chars_label(text))
    return ""


def apply_headline(markdown_text: str, headline: str):
    """(새 본문, 바꿨는가) — `이번 주 한 줄:` 자리를 확정한다 (멱등).

    확정 마커가 남아 있으면 치환하고, 이미 확정돼 있으면 **덮어쓴다**.
    그 줄 자체가 없으면 바꿀 자리가 없으므로 False 를 돌려준다.
    """
    # 사이클 8 #5: 한 줄만 받는다 — 개행이 들어오면 통째로 공백으로 접는다
    # (호출자는 CLI 에서 이미 거부되지만, 함수 자체도 멱등해야 한다).
    text = " ".join((headline or "").split())
    lines = markdown_text.split("\n")
    for index, line in enumerate(lines):
        if not line.startswith(HEADLINE_PREFIX):
            continue
        replacement = f"{HEADLINE_PREFIX} {text}"
        if line == replacement:
            return markdown_text, False
        lines[index] = replacement
        return "\n".join(lines), True
    return markdown_text, False


def _council_body(commentary: str):
    """협의회에서 섹션 본문 줄 (composer 와 같은 `· ` 글머리)."""
    return [
        f"· {line.strip()}"
        for line in (commentary or "").strip().split("\n")
        if line.strip()
    ]


def apply_commentary(markdown_text: str, commentary: str):
    """(새 본문, 바꿨는가) — `## 🤝 협의회에서` 섹션 본문을 확정한다 (멱등).

    섹션이 없으면 만든다 (내용이 없는 주차는 composer 가 섹션 자체를 생략한다).
    있으면 본문을 통째로 갈아 끼운다 — 누적이 아니라 확정이다.
    """
    body = _council_body(commentary)
    if not body:
        return markdown_text, False

    lines = markdown_text.split("\n")
    trailing_newline = markdown_text.endswith("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines = lines[:-1]

    if COUNCIL_HEADING in lines:
        start = lines.index(COUNCIL_HEADING)
        end = start + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped.startswith("## ") or stripped.startswith("<!--"):
                break
            end += 1
        updated = lines[:start + 1] + [""] + body + [""] + lines[end:]
    else:
        insert_at = _insert_position(lines)
        updated = (
            lines[:insert_at]
            + [COUNCIL_HEADING, ""] + body + [""]
            + lines[insert_at:]
        )

    text = "\n".join(updated)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if trailing_newline and not text.endswith("\n"):
        text += "\n"
    if text == markdown_text:
        return markdown_text, False
    return text, True


def _insert_position(lines):
    """협의회에서 섹션을 끼울 자리 — 회원사 소식 앞, 없으면 보류 주석 앞, 없으면 끝."""
    if MEMBER_HEADING in lines:
        return lines.index(MEMBER_HEADING)
    for index, line in enumerate(lines):
        if line.strip().startswith("<!-- 보류:"):
            return index
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return end


def main(argv=None):
    parser = RedactingArgumentParser(
        description="발송본의 사람 확정 필드(이번 주 한 줄 · 협의회에서) 적용"
    )
    parser.add_argument("week", help="주차 (예: 2026-W37)")
    parser.add_argument("--headline", help="이번 주 한 줄 (확정 마커 치환·덮어쓰기)")
    parser.add_argument("--commentary", help="협의회에서 섹션 본문")
    parser.add_argument(
        "--out-dir", default="digests", help="다이제스트 디렉토리 (기본: digests)"
    )
    # 사이클8 #3: argparse 는 guarded_main 보다 먼저 말한다 — argv 에 토큰 형태가
    # 있으면 **내용을 출력하지 않고** 일반 오류로 끝낸다.
    if reject_secret_argv(sys.argv[1:] if argv is None else argv, _err):
        return 2
    args = parser.parse_args(argv)

    # 사이클6 #9: 주차는 엄격 정규식이다 — 경로가 되는 값을 느슨하게 받지 않는다.
    if not state_mod.valid_week(args.week):
        _err(f"✗ 주차 형식이 아닙니다: {args.week!r}")
        return 2

    if not args.headline and not args.commentary:
        _err(NO_FIELD_MESSAGE)
        return 2
    for label, value in (("이번 주 한 줄", args.headline),
                         ("협의회 의견", args.commentary)):
        if value is None:
            continue
        error = text_error(value, label)
        if error:
            _err(error)
            return 2
    if args.headline and "\n" in args.headline.strip():
        _err(HEADLINE_MULTILINE_REJECT)

        return 2

    markdown_path = Path(args.out_dir) / f"{args.week}.md"
    if not markdown_path.exists():
        _err(f"✗ 파일 없음: {markdown_path}")
        return 2

    # 계약 W10 사이클3 #6: **잠금을 먼저 쥐고 상태를 확인한 뒤** 본문을 쓴다.
    # 이전 구현은 파일부터 써서, sending 주차에서 rc=1 을 내면서도 마커가 이미
    # 새 의견으로 바뀌어 있었다(거부가 본문을 보호하지 못했다).
    # 카톡 재생성도 이 잠금 안에서, md 를 쓴 직후에 한다 (사이클 6 #5).
    state_path = state_mod.state_path(args.week, args.out_dir)
    lock_path = state_mod.lock_path(args.week, args.out_dir)
    # 사이클7 #1: 발송기만 즉시 거부(LOCK_NB)다. 그 밖의 작성자는 블로킹 대기 후
    # 한도를 넘기면 본문을 건드리지 않고 실패한다.
    try:
        handle = state_mod.acquire_lock(lock_path, blocking=True)
    except (state_mod.LockBusy, OSError) as exc:
        _err(f"✗ 확정 적용 거부: {exc}")
        return 2

    try:
        try:
            state = state_mod.load_state(state_path, args.week)
        except state_mod.StateError as exc:
            _err(f"✗ 확정 적용 거부: {exc}")
            return 2

        if state.get("status") not in WRITABLE_STATUSES:
            _err(
                f"✗ 확정 적용 거부: status={state.get('status')} "
                "— 본문을 바꾸지 않았습니다"
            )
            return 2

        markdown_text = markdown_path.read_text(encoding="utf-8")
        # 계약 W10 사이클6 #2: 본문에 제어 문자가 있으면 손대지 않는다 —
        # 줄 나눔이 파서마다 달라지는 본문을 확정 입력으로 덮어쓰면 안 된다.
        if prune.control_chars(markdown_text):
            _err("✗ 확정 적용 거부: 본문에 제어 문자 포함 ({})".format(
                prune.control_chars_label(markdown_text)))
            return 2

        updated = markdown_text
        applied = []

        if args.headline:
            had_marker = MARKER in updated
            updated, changed = apply_headline(updated, args.headline)
            if changed:
                applied.append(
                    "이번 주 한 줄" + ("" if had_marker else " (덮어씀)")
                )
            else:
                _err("⚠️  이번 주 한 줄: 이미 같은 문구입니다 (변경 없음)")

        if args.commentary:
            had_section = COUNCIL_HEADING in updated
            updated, changed = apply_commentary(updated, args.commentary)
            if changed:
                applied.append(
                    "협의회에서" + (" (덮어씀)" if had_section else " (섹션 생성)")
                )
            else:
                _err("⚠️  협의회에서: 이미 같은 본문입니다 (변경 없음)")

        if updated == markdown_text:
            _out("⚠️  바뀐 내용이 없습니다 — 파일을 건드리지 않았습니다")
            return 0

        markdown_path.write_text(updated, encoding="utf-8")
        _out(f"✓ 확정 적용: {markdown_path} ({', '.join(applied)})")

        # 개정 v2.5 (#12) + 사이클 6 #5: 카톡 평문도 같이 확정한다 — MD만 고치면
        # 카톡본에 "(확정 필요)"가 남아 서로 다른 두 발송본이 생긴다. 카톡은
        # 자기 파일을 손보지 않고 **갱신된 md 에서 통째로 재생성**한다(항목
        # 덩어리·조각 경계가 md 와 갈라지지 않는다).
        kakao_path = markdown_path.with_name(f"{markdown_path.stem}.kakao.txt")
        kakao_path.write_text(
            kakao_file_text_from_markdown(updated), encoding="utf-8"
        )
        _out(f"✓ 카톡 평문 동기화: {kakao_path}")

        # 사이클 8 #1: 본문을 고쳤으므로 정본 파일의 결속 해시를 다시 맞춘다.
        # 항목 목록은 그대로 — 확정 입력은 항목을 건드리지 않는다.
        if refresh_manifest_binding(markdown_path) is not None:
            _out("✓ 항목 정본 결속 갱신")

        # 상태 기록은 **해설(협의회 의견)만** annotated 로 올린다 (계약 W10) —
        # 한 줄은 본문 편집이지 승인 대상 해설이 아니다.
        if not args.commentary:
            return 0
        record = args.commentary.strip()
        try:
            # 사이클7 #1: 잠금을 이미 쥐고 있으므로 update_state_locked 로 읽고 쓴다.
            state_mod.update_state_locked(
                state_path, args.week,
                lambda current: state_mod.mark_annotated(current, record),
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
