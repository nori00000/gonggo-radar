"""argparse 출력까지 redact 를 지나게 한다 (계약 W10 사이클8 #3).

왜 필요한가: argparse 는 `guarded_main` 보다 **먼저** 출력한다. 잘못된 인자를
만나면 `parser.error()` 가 stderr 에 "unrecognized arguments: --invalid=<토큰>"
을 그대로 찍고 `SystemExit(2)` 를 올리므로, 최상위 traceback 가림으로는 막히지
않는다. 다섯 CLI(send·notify·recheck·weekly·apply) 가 모두 이 모듈을 쓴다.

두 겹이다:
 ① `reject_secret_argv()` — parse_args **전에** argv 를 훑어, 토큰 형태가 있으면
    내용을 출력하지 않고 일반 오류로 끝낸다(exit 2). 값 자체가 로그·프로세스
    목록에 남을 명령은 애초에 실행하지 않는다.
 ② `RedactingArgumentParser` — 그래도 argparse 가 말을 하게 되는 모든 경로
    (`error`·`exit`·usage·help)를 redact 로 통과시킨다.
"""

import argparse
import sys

from alert.utils.redact import redact

SECRET_ARGV_REASON = (
    "✗ 인자에 토큰 형태 문자열이 있습니다 — 명령을 취소했습니다"
    " (안전을 위해 내용은 출력하지 않습니다)"
)


def argv_has_secret(argv) -> bool:
    """argv 원소 중 redact 가 바꿔 쓰는 것이 있는가 (= 토큰 형태)."""
    for arg in argv or ():
        text = str(arg)
        if redact(text) != text:
            return True
    return False


def reject_secret_argv(argv, err) -> bool:
    """토큰 형태 인자가 있으면 일반 오류를 내고 True. 없으면 False.

    `err` 는 호출 CLI 의 redact 경유 stderr 출력 함수다.
    """
    if not argv_has_secret(argv):
        return False
    err(SECRET_ARGV_REASON)
    return True


class RedactingArgumentParser(argparse.ArgumentParser):
    """`error`·`exit`·usage·help 출력을 전부 redact 로 통과시키는 파서."""

    def _print_message(self, message, file=None):     # noqa: D102
        if not message:
            return
        (file or sys.stderr).write(redact(message))

    def error(self, message):                          # noqa: D102
        self.print_usage(sys.stderr)
        self.exit(2, redact("{}: error: {}\n".format(self.prog, message)))

    def exit(self, status=0, message=None):            # noqa: D102
        if message:
            self._print_message(message, sys.stderr)
        sys.exit(status)
