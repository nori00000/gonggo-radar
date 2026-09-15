#!/usr/bin/env bash
# monthly_job.sh — 월간 종합호 잡 (P1' 계약 §2).
#
# digest_job.sh 와 **같은 순서**다: 생성 → glm_enrich → recheck → notify.
# 발송은 이 잡이 하지 않는다 — 사람이 텔레그램 [발송] 버튼으로만 한다.
#
# 왜 날짜 가드가 스크립트에 있나: launchd 는 "매월 첫째 목요일"을 표현하지 못한다.
# plist 는 **매주 목요일 23:00** 에 깨우고, 여기서 "요일 = 목 **그리고** 일자 ≤ 7"
# 일 때만 진행한다(그 두 조건을 함께 만족하는 날이 곧 그 달의 첫째 목요일이다).
#
# 라운드 3 (Codex MEDIUM): 일자만 보면 **2026-10-02 금요일**에도 진행됐다.
# launchd 스케줄을 사람이 고치거나 손으로 실행하는 순간 조건이 깨진다 — 잡 자신이
# 두 조건을 모두 확인한다. 시간대도 실행 환경에 맡기지 않고 KST 로 고정한다:
# 23:00 잡은 UTC 로 읽으면 **전날**이 되어 요일·일자가 함께 어긋난다.
set -euo pipefail

# 시간대 고정 — `date` 와 아래 Python 단계가 같은 달력을 본다.
export TZ="Asia/Seoul"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 잠금 재실행에서 쓸 자기 경로 (cd 전에 절대경로로 굳힌다).
SELF="${ROOT}/scripts/$(basename "${BASH_SOURCE[0]}")"
cd "${ROOT}" || exit 1
# 운영 기본값은 워크트리의 venv. 테스트는 이 손잡이로 파이썬 단계를 대체한다.
PY="${GONGGO_PYTHON:-${ROOT}/venv/bin/python}"
DB="${GONGGO_DB:-alert/data/announcements.db}"

# ── 잡 전역 뮤텍스 (P2-X H4) ─────────────────────────────────────────────
# 감사 V M-4: 주간(23:00)·월간(23:30) 잡은 **서로 다른 호 잠금**만 잡아 상호배제가
# 없었다. 겹치면 z.ai 레인을 동시에 부르고 미리보기가 경합한다. 두 잡이 **같은**
# digests/.job.lock 을 쥔다. macOS 에 flock(1) 이 없어 scripts/job_lock.py 가
# fcntl.flock 을 쥔 채 이 스크립트를 다시 exec 한다(잠금은 exec 를 넘어 산다).
# 대기 한도 20분, 못 얻으면 로그 한 줄 + exit 75(EX_TEMPFAIL).
# 잠금 파이썬은 venv 가 아니라 시스템 파이썬이다 — venv 가 깨져도 뮤텍스는 선다.
if [ -z "${GONGGO_JOB_LOCK_HELD:-}" ]; then
  export GONGGO_JOB_LOCK_HELD=1
  exec "${GONGGO_LOCK_PYTHON:-/usr/bin/python3}" "${ROOT}/scripts/job_lock.py" \
    --lock "${ROOT}/digests/.job.lock" \
    --timeout "${GONGGO_JOB_LOCK_TIMEOUT:-1200}" \
    -- /bin/bash "${SELF}" "$@"
fi

# 인자로 호 키를 직접 주면(재실행·수동 조립) 날짜 가드를 건너뛴다.
if [ "$#" -eq 0 ]; then
  # MONTHLY_JOB_NOW 는 **테스트 전용** 손잡이다 (YYYY-MM-DD). 운영에서는 비어 있다.
  NOW="${MONTHLY_JOB_NOW:-}"
  if [ -n "${NOW}" ]; then
    DOW="$(date -j -f "%Y-%m-%d" "${NOW}" "+%u")"
    DOM="$(date -j -f "%Y-%m-%d" "${NOW}" "+%d")"
    MONTH="$(date -j -v-1m -f "%Y-%m-%d" "${NOW}" "+%Y-M%m")"
  else
    NOW="$(date "+%Y-%m-%d")"
    DOW="$(date "+%u")"
    DOM="$(date "+%d")"
    MONTH="$(date -v-1m "+%Y-M%m")"
  fi
  # 10진수 강제 (08·09 를 8진수로 읽지 않게)
  if [ "$((10#${DOW}))" -ne 4 ]; then
    echo "=== monthly_job skip (${NOW} KST) — 목요일이 아님(요일 ${DOW}) ==="
    exit 0
  fi
  if [ "$((10#${DOM}))" -gt 7 ]; then
    echo "=== monthly_job skip (${NOW} KST) — 첫째 주가 아님(일자 ${DOM}) ==="
    exit 0
  fi
else
  MONTH="$1"
fi

MD="digests/${MONTH}.md"

echo "=== monthly_job ${MONTH} ($(date "+%Y-%m-%d %H:%M:%S") KST) ==="

GEN_RC=0
"${PY}" scripts/monthly_digest.py "${MONTH}" --db "${DB}" --exclude-state || GEN_RC=$?
echo "monthly_digest exit=${GEN_RC}"

if [ "${GEN_RC}" -ne 0 ]; then
  echo "월간호 생성 실패(exit ${GEN_RC}) — 미리보기 전송 생략(이전 판 재전송 방지)" >&2
  exit "${GEN_RC}"
fi

if [ ! -f "${MD}" ]; then
  echo "월간호 파일 없음: ${MD} — 미리보기 생략" >&2
  exit 1
fi

# V3(GLM 야간 요약 레인): 항목 보강 줄(→ 한 줄 의미). 실패해도 잡은 계속한다.
GLM_RC=0
"${PY}" scripts/glm_enrich.py "${MONTH}" --db "${DB}" || GLM_RC=$?
echo "glm_enrich exit=${GLM_RC} (실패해도 계속)"

RECHECK_RC=0
"${PY}" scripts/recheck_digest.py "${MD}" --db "${DB}" || RECHECK_RC=$?
echo "recheck_digest exit=${RECHECK_RC}"

if [ "${RECHECK_RC}" -ne 0 ]; then
  echo "재검증 실패(exit ${RECHECK_RC}) — 미리보기 전송 생략" >&2
  exit "${RECHECK_RC}"
fi

NOTIFY_RC=0
"${PY}" scripts/notify_digest.py "${MD}" --db "${DB}" || NOTIFY_RC=$?
echo "notify_digest exit=${NOTIFY_RC}"
exit "${NOTIFY_RC}"
