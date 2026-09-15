#!/usr/bin/env python3
"""소스 상태 점검 요약 한 줄을 협의회 토픽에 안내한다 (P2-X H1).

**발송본이 아니다.** 다이제스트 발송 게이트(마커·check.json·승인 세대)와는 아무
관계가 없고, 승인 카드도 붙지 않는다 — 운영자에게 "이번 주 소스가 이렇다" 를
말하는 안내 메시지 한 건이다. 그래서 상태 파일도 잠금도 쓰지 않는다.

전송 인프라는 ``scripts/notify_digest.py`` 의 것을 그대로 쓴다(토큰 해석·토픽
해석·redact·오류 처리). 같은 일을 두 번 구현하면 한쪽만 고쳐진다.

감사 V H-1 이 이 스크립트의 이유다: 유일한 탐지기인 ``source_health.py`` 가
어디에도 예약돼 있지 않아 사람이 손으로 돌려야만 죽은 소스가 보였다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.utils.redact import redact                       # noqa: E402
from alert.utils.safe_argparse import (                     # noqa: E402
    RedactingArgumentParser,
    reject_secret_argv,
)
from scripts.notify_digest import (                         # noqa: E402
    resolve_target,
    resolve_token,
    send_chunk,
)

HEADER = "🩺 협의회 소스 상태 점검"
# 텔레그램 한 메시지 한도보다 훨씬 짧게 자른다 — 이건 안내지 보고서가 아니다.
MAX_SUMMARY_CHARS = 1200


def _err(message) -> None:
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    print(redact(message))


def main(argv=None) -> int:
    parser = RedactingArgumentParser(
        description="소스 상태 점검 요약을 협의회 토픽에 안내 (발송본 아님)")
    parser.add_argument("summary", help="요약 한 줄이 든 파일 경로")
    parser.add_argument("--report", help="보고서 파일 경로 (안내문에 이름만 싣는다)")
    parser.add_argument("--topic-key", default="council",
                        help="토픽 키 (기본: council)")
    parser.add_argument("--dry-run", action="store_true",
                        help="보낼 문구만 출력하고 전송하지 않는다")
    if reject_secret_argv(sys.argv[1:] if argv is None else argv, _err):
        return 2
    args = parser.parse_args(argv)

    summary_path = Path(args.summary)
    try:
        summary = summary_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _err(f"✗ 요약 파일을 읽지 못했습니다: {redact(exc)}")
        return 2
    if not summary:
        _err("✗ 요약이 비어 있습니다 — 안내를 보내지 않습니다")
        return 2

    body = f"{HEADER}\n\n{summary[:MAX_SUMMARY_CHARS]}"
    if args.report:
        # 라운드 2 (Codex LOW): 텔레그램 본문에 **절대경로를 싣지 않는다**.
        # `redact()` 는 경로를 가리지 않으므로 여기서 파일명만 남긴다.
        body += f"\n\n자세히: digests/observe/{Path(args.report).name}"

    if args.dry_run:
        _out("[DRY-RUN] 안내 1건")
        _out(body)
        return 0

    try:
        chat_id, thread_id = resolve_target(args.topic_key)
        token = resolve_token()
    except Exception as exc:                # noqa: BLE001
        _err(f"✗ 전송 대상 확인 실패: {redact(exc)}")
        return 2

    ok, message_id, error = send_chunk(token, chat_id, thread_id, redact(body))
    if not ok:
        _err(f"✗ 안내 전송 실패: {error}")
        return 1
    _out(f"✓ 안내 전송 message_id={message_id}")
    return 0


def guarded_main() -> int:
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
