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
from alert.digest.preview import MARKER

NOOP_MESSAGE = "⚠️  마커 없음 — 변경하지 않았습니다 (이미 해설이 채워졌습니다)"


def apply_commentary(markdown_text: str, commentary: str):
    """(새 본문, 치환했는가). 마커가 없으면 원문을 그대로 돌려준다."""
    if MARKER not in markdown_text:
        return markdown_text, False
    return markdown_text.replace(MARKER, commentary.strip(), 1), True


def main():
    parser = argparse.ArgumentParser(description="협의회 의견 확정 마커 치환")
    parser.add_argument("week", help="주차 (예: 2026-W37)")
    parser.add_argument("text", help="협의회 의견 본문")
    parser.add_argument(
        "--out-dir", default="digests", help="다이제스트 디렉토리 (기본: digests)"
    )
    args = parser.parse_args()

    commentary = args.text.strip()
    if not commentary:
        print("✗ 본문이 비었습니다", file=sys.stderr)
        return 2

    markdown_path = Path(args.out_dir) / f"{args.week}.md"
    if not markdown_path.exists():
        print(f"✗ 파일 없음: {markdown_path}", file=sys.stderr)
        return 2

    markdown_text = markdown_path.read_text(encoding="utf-8")
    updated, replaced = apply_commentary(markdown_text, commentary)

    if not replaced:
        print(NOOP_MESSAGE)
        return 0

    markdown_path.write_text(updated, encoding="utf-8")
    print(f"✓ 협의회 의견 적용: {markdown_path}")

    state_path = state_mod.state_path(args.week, args.out_dir)
    try:
        state = state_mod.load_state(state_path, args.week)
        state_mod.save_state(
            state_path, state_mod.mark_annotated(state, commentary)
        )
        print(f"✓ 상태 기록: {state_path} (status=annotated)")
    except (state_mod.StateError, OSError) as exc:
        print(f"⚠️  상태 기록 실패(본문은 적용됨): {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
