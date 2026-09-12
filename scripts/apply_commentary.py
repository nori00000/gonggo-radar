#!/usr/bin/env python3
"""협의회 의견 섹션의 확정 마커를 본문으로 치환 (계약 W10).

마커(`<!-- 상민 확정 필요 -->`)가 없으면 no-op 경고만 남긴다(파일을 건드리지 않는다).
본문은 원문 그대로 저장한다 — HTML 이스케이프는 발송 단계(send_digest)가 한다.
"""

import argparse
import sys
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest import state as state_mod
from alert.digest.composer import kakao_file_text_from_markdown
from alert.digest.preview import MARKER

NOOP_MESSAGE = "⚠️  마커 없음 — 변경하지 않았습니다 (이미 해설이 채워졌습니다)"

# 본문을 써도 되는 상태 (사이클3 #6). sending·sent 는 파일을 건드리지 않는다.
WRITABLE_STATUSES = ("draft", "annotated", "held")

# 계약 W10 크리틱 #7: 해설에 HTML 주석 구분자가 들어오면 거부한다.
# 발송 렌더러(send_digest.markdown_to_html)는 `<!--` 로 시작하는 줄을 버리므로,
# 주석을 품은 해설은 승인 마커까지 삼키면서 메일에서 조용히 사라진다.
COMMENT_TOKENS = ("<!--", "-->")
COMMENT_REJECT = (
    "✗ 해설에 HTML 주석 구분자(<!-- 또는 -->)를 쓸 수 없습니다 — "
    "발송 렌더러가 해당 줄을 버려 해설이 조용히 사라집니다"
)


def commentary_error(commentary: str) -> str:
    """해설로 받아들일 수 없는 이유. 문제없으면 빈 문자열."""
    if not (commentary or "").strip():
        return "✗ 본문이 비었습니다"
    if any(token in commentary for token in COMMENT_TOKENS):
        return COMMENT_REJECT
    return ""


def apply_commentary(markdown_text: str, commentary: str):
    """(새 본문, 치환했는가). 마커가 없으면 원문을 그대로 돌려준다."""
    if MARKER not in markdown_text:
        return markdown_text, False
    return markdown_text.replace(MARKER, commentary.strip(), 1), True


def main(argv=None):
    parser = argparse.ArgumentParser(description="협의회 의견 확정 마커 치환")
    parser.add_argument("week", help="주차 (예: 2026-W37)")
    parser.add_argument("text", help="협의회 의견 본문")
    parser.add_argument(
        "--out-dir", default="digests", help="다이제스트 디렉토리 (기본: digests)"
    )
    args = parser.parse_args(argv)

    commentary = args.text.strip()
    error = commentary_error(commentary)
    if error:
        print(error, file=sys.stderr)
        return 2

    markdown_path = Path(args.out_dir) / f"{args.week}.md"
    if not markdown_path.exists():
        print(f"✗ 파일 없음: {markdown_path}", file=sys.stderr)
        return 2

    # 계약 W10 사이클3 #6: **잠금을 먼저 쥐고 상태를 확인한 뒤** 본문을 쓴다.
    # 이전 구현은 파일부터 써서, sending 주차에서 rc=1 을 내면서도 마커가 이미
    # 새 의견으로 바뀌어 있었다(거부가 본문을 보호하지 못했다).
    # 카톡 재생성도 이 잠금 안에서, md 를 쓴 직후에 한다 (사이클 6 #5).
    state_path = state_mod.state_path(args.week, args.out_dir)
    lock_path = state_mod.lock_path(args.week, args.out_dir)
    try:
        handle = state_mod.acquire_lock(
            lock_path,
            on_reclaim=lambda reason: print(
                f"⚠️  잔존 잠금 회수: {reason}", file=sys.stderr
            ),
            on_warn=lambda why: print(f"⚠️  {why}", file=sys.stderr),
        )
    except (state_mod.LockBusy, OSError) as exc:
        print(f"✗ 해설 적용 거부: {exc}", file=sys.stderr)
        return 2

    try:
        try:
            state = state_mod.load_state(state_path, args.week)
        except state_mod.StateError as exc:
            print(f"✗ 해설 적용 거부: {exc}", file=sys.stderr)
            return 2

        if state.get("status") not in WRITABLE_STATUSES:
            print(
                f"✗ 해설 적용 거부: status={state.get('status')} "
                "— 본문을 바꾸지 않았습니다",
                file=sys.stderr,
            )
            return 2

        markdown_text = markdown_path.read_text(encoding="utf-8")
        updated, replaced = apply_commentary(markdown_text, commentary)

        if not replaced:
            print(NOOP_MESSAGE)
            return 0

        markdown_path.write_text(updated, encoding="utf-8")
        print(f"✓ 협의회 의견 적용: {markdown_path}")

        # 개정 v2.5 (#12) + 사이클 6 #5: 카톡 평문도 같이 확정한다 — MD만 고치면
        # 카톡본에 "(확정 필요)"가 남아 서로 다른 두 발송본이 생긴다. 카톡은
        # 자기 파일을 손보지 않고 **갱신된 md 에서 통째로 재생성**한다(항목
        # 덩어리·조각 경계가 md 와 갈라지지 않는다).
        kakao_path = markdown_path.with_name(f"{markdown_path.stem}.kakao.txt")
        kakao_path.write_text(
            kakao_file_text_from_markdown(updated), encoding="utf-8"
        )
        print(f"✓ 카톡 평문 동기화: {kakao_path}")

        try:
            state_mod.apply_state(
                state_path, state, state_mod.mark_annotated(state, commentary)
            )
            print(f"✓ 상태 기록: {state_path} (status=annotated)")
        except (state_mod.TransitionError, OSError) as exc:
            print(f"⚠️  상태 기록 실패(본문은 적용됨): {exc}", file=sys.stderr)
            return 1
    finally:
        if not state_mod.release_lock(handle):
            print("⚠️  잠금 해제 생략(내 잠금이 아님)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
