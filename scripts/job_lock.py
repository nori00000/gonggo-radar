#!/usr/bin/env python3
"""잡 전역 뮤텍스 — 잠금을 쥔 채 명령을 exec 한다 (P2-X H4).

왜 필요한가: 감사 V M-4. 주간호 잡(목 23:00)과 월간호 잡(목 23:30)은 서로 다른
호 키의 잠금(`digests/2026-W40.lock` vs `digests/2026-M09.lock`)만 잡는다.
그 둘은 **상호배제를 제공하지 못한다**. launchd 도 라벨이 달라 직렬화하지 않고,
`glm_enrich.py` 는 잠금을 전혀 잡지 않는다(`acquire_lock|lock_path` 0히트).
관측 소요는 3~4분이라 30분 간격이 지금은 충분하지만 **보장이 아니다** —
`DS_SUBPROCESS_TIMEOUT=630` 은 **배치당**이고, 배치가 늘면 30분을 넘는다.
겹치면 두 잡이 z.ai 레인을 동시에 부르고 미리보기가 경합한다.

왜 `flock(1)` 이 아닌가: macOS 에 없다(`which flock` → not found). 그래서
`fcntl.flock` 을 쥔 뒤 **그 fd 를 닫지 않고** 명령을 exec 한다. 잠금은 열린
파일 서술자에 붙으므로 exec 후에도 살아 있고, 프로세스가 어떻게 죽든 커널이
해제한다 — PID 검사·TTL·stale 회수가 전부 불필요하다(`alert/digest/state.py`
가 호 잠금에 대해 같은 논거를 편다).

잠금 파일은 **지우지 않는다**. 지우면 다음 실행이 새 inode 를 만들어 상호배제가
조용히 사라진다.

사용:
    job_lock.py --lock digests/.job.lock --timeout 1200 -- /bin/bash scripts/digest_job.sh

종료 코드:
    75 (EX_TEMPFAIL) — 대기 한도 안에 잠금을 얻지 못했다.
    그 외 — exec 한 명령의 종료 코드.
"""

import argparse
import errno
import fcntl
import os
import sys
import time

EXIT_LOCK_BUSY = 75         # EX_TEMPFAIL — "지금은 안 되지만 다음에 다시"
POLL_SECONDS = 1.0


def acquire(path, timeout, poll=POLL_SECONDS, now=time.monotonic,
            sleep=time.sleep):
    """잠금을 얻을 때까지 폴링한다. 성공하면 fd, 실패하면 None."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = now() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                raise
        if now() >= deadline:
            os.close(fd)
            return None
        sleep(min(poll, max(0.0, deadline - now())))


def hold_and_exec(fd, argv):
    """진단용 pid 를 남기고, fd 를 연 채로 명령을 exec 한다."""
    # exec 후에도 살아 있어야 잠금이 유지된다.
    os.set_inheritable(fd, True)
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    except OSError:
        pass                # 소유권은 flock 이 증명한다 — 기록은 편의일 뿐
    os.execvp(argv[0], argv)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="잡 전역 뮤텍스를 쥔 채 명령을 실행한다")
    parser.add_argument("--lock", required=True, help="잠금 파일 경로")
    parser.add_argument("--timeout", type=float, default=1200.0,
                        help="대기 한도 초 (기본 1200 = 20분)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- 뒤에 실행할 명령")
    args = parser.parse_args(argv)

    command = [token for token in args.command if token != "--"]
    if not command:
        print("✗ 실행할 명령이 없습니다", file=sys.stderr)
        return 2

    fd = acquire(args.lock, args.timeout)
    if fd is None:
        print(
            f"✗ 잡 잠금 대기 한도 초과({args.timeout:.0f}s): {args.lock} — "
            "다른 다이제스트 잡이 아직 돌고 있습니다. 이번 회차는 건너뜁니다.",
            file=sys.stderr,
        )
        return EXIT_LOCK_BUSY
    hold_and_exec(fd, command)
    return 0                # pragma: no cover — execvp 가 돌아오지 않는다


if __name__ == "__main__":
    sys.exit(main())
