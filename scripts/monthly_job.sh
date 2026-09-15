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
#
# 라운드 2 (Codex MEDIUM): 월간 잡은 `exec` 하지 **않는다**. 주간 잡이 23:00~23:55
# 돌면 월간(23:30)은 대기하다 exit 75 로 끝나고, launchd 는 다음 목요일에야 다시
# 깨운다 — 그 달의 월간호가 통째로 사라진다. 자식으로 돌려 75 를 잡아 **재예약
# 표식**(digests/.monthly_retry)을 남기고 운영자에게 한 줄 알린다.
RETRY_FILE="${ROOT}/digests/.monthly_retry"
RETRY_MAX_DAYS="${GONGGO_MONTHLY_RETRY_DAYS:-7}"

if [ -z "${GONGGO_JOB_LOCK_HELD:-}" ]; then
  export GONGGO_JOB_LOCK_HELD=1
  LOCK_RC=0
  "${GONGGO_LOCK_PYTHON:-/usr/bin/python3}" "${ROOT}/scripts/job_lock.py" \
    --lock "${ROOT}/digests/.job.lock" \
    --timeout "${GONGGO_JOB_LOCK_TIMEOUT:-1200}" \
    -- /bin/bash "${SELF}" "$@" || LOCK_RC=$?
  if [ "${LOCK_RC}" -eq 75 ]; then
    # MONTHLY_JOB_NOW 는 테스트 전용 손잡이다 (운영에서는 비어 있다).
    if [ -n "${MONTHLY_JOB_NOW:-}" ]; then
      RETRY_TODAY="${MONTHLY_JOB_NOW}"
      RETRY_DEFAULT="$(date -j -v-1m -f "%Y-%m-%d" "${MONTHLY_JOB_NOW}" "+%Y-M%m")"
    else
      RETRY_TODAY="$(date "+%Y-%m-%d")"
      RETRY_DEFAULT="$(date -v-1m "+%Y-M%m")"
    fi
    RETRY_MONTH="${1:-${RETRY_DEFAULT}}"
    mkdir -p "${ROOT}/digests"
    printf '%s %s\n' "${RETRY_TODAY}" "${RETRY_MONTH}" > "${RETRY_FILE}"
    echo "잡 잠금 대기 초과 — ${RETRY_FILE} 에 재예약(${RETRY_MAX_DAYS}일 안에 다음 실행이 집행)" >&2
    NOTE="$(mktemp)"
    printf '%s\n' "⏳ 월간 종합호 ${RETRY_MONTH} 이 잡 잠금 대기로 이번 회차를 건너뛰었습니다 — ${RETRY_MAX_DAYS}일 안의 다음 실행이 이어받습니다" > "${NOTE}"
    "${PY}" "${ROOT}/scripts/notify_health.py" "${NOTE}" || \
      echo "재예약 안내 전송 실패(진행함)" >&2
    rm -f "${NOTE}"
  fi
  exit "${LOCK_RC}"
fi

# 라운드 2: 재예약 표식이 살아 있으면(기록일로부터 RETRY_MAX_DAYS 안) **요일·일자
# 가드를 건너뛰고** 그때 못 돈 달을 조립한다. 표식이 오래됐으면 지우고 평소대로 간다.
RETRY_MONTH=""
if [ "$#" -eq 0 ] && [ -f "${RETRY_FILE}" ]; then
  RETRY_STAMP=""; RETRY_KEY=""
  read -r RETRY_STAMP RETRY_KEY < "${RETRY_FILE}" || true
  if [ -n "${MONTHLY_JOB_NOW:-}" ]; then
    NOW_EPOCH="$(date -j -f "%Y-%m-%d" "${MONTHLY_JOB_NOW}" "+%s")"
  else
    NOW_EPOCH="$(date "+%s")"
  fi
  STAMP_EPOCH="$(date -j -f "%Y-%m-%d" "${RETRY_STAMP:-1970-01-01}" "+%s" 2>/dev/null || echo 0)"
  AGE_DAYS=$(( (NOW_EPOCH - STAMP_EPOCH) / 86400 ))
  if [ -n "${RETRY_KEY}" ] && [ "${AGE_DAYS}" -ge 0 ] && [ "${AGE_DAYS}" -le "$((10#${RETRY_MAX_DAYS}))" ]; then
    RETRY_MONTH="${RETRY_KEY}"
    echo "=== monthly_job 재예약 집행: ${RETRY_MONTH} (표식 ${RETRY_STAMP}, ${AGE_DAYS}일 전) ==="
  else
    echo "재예약 표식이 만료됐습니다(${RETRY_STAMP}) — 지우고 평소 가드로 진행" >&2
    rm -f "${RETRY_FILE}"
  fi
fi

# 인자로 호 키를 직접 주면(재실행·수동 조립) 날짜 가드를 건너뛴다.
if [ -n "${RETRY_MONTH}" ]; then
  MONTH="${RETRY_MONTH}"
elif [ "$#" -eq 0 ]; then
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

# 라운드 2: 여기까지 왔으면 이 달은 조립·게시를 마쳤다 — 재예약 표식을 지운다.
if [ "${NOTIFY_RC}" -eq 0 ] && [ -f "${RETRY_FILE}" ]; then
  rm -f "${RETRY_FILE}"
  echo "재예약 표식 해제: ${RETRY_FILE}"
fi
exit "${NOTIFY_RC}"
