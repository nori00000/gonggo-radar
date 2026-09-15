#!/usr/bin/env bash
# health_job.sh — 주 1회 소스 상태 점검 잡 (P2-X H1).
#
# 감사 V H-1: 크롤러 전면 실패가 run_history 에 `status='success'` 로 남았고,
# 유일한 탐지기인 scripts/source_health.py 는 **어디에도 예약돼 있지 않았다**.
# 사람이 손으로 돌려야만 죽은 소스가 보였다.
#
# 하는 일 셋:
#   ① source_health.py 를 읽기 전용으로 1회 돌려 digests/observe/health-<date>.md
#   ② 같은 내용의 요약 **한 줄**을 .summary 파일로
#   ③ 그 한 줄을 협의회 토픽에 안내 1건 (발송본 아님 — 승인 카드 없음)
#
# 발송은 하지 않는다. DB 는 mode=ro 로만 열린다(스크립트가 보장).
set -euo pipefail

export TZ="Asia/Seoul"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}" || exit 1
PY="${GONGGO_PYTHON:-${ROOT}/venv/bin/python}"
DB="${GONGGO_DB:-alert/data/announcements.db}"

DATE="$(date "+%Y-%m-%d")"
OUT_DIR="digests/observe"
REPORT="${OUT_DIR}/health-${DATE}.md"
SUMMARY="${OUT_DIR}/health-${DATE}.summary.txt"

mkdir -p "${OUT_DIR}"

echo "=== health_job ${DATE} ($(date "+%H:%M:%S") KST) ==="

HEALTH_RC=0
"${PY}" scripts/source_health.py --db "${DB}" \
  --out "${REPORT}" --summary-out "${SUMMARY}" || HEALTH_RC=$?
echo "source_health exit=${HEALTH_RC}"

if [ "${HEALTH_RC}" -ne 0 ]; then
  echo "점검 실패(exit ${HEALTH_RC}) — 안내 생략(낡은 요약 재전송 방지)" >&2
  exit "${HEALTH_RC}"
fi

if [ ! -s "${SUMMARY}" ]; then
  echo "요약이 비었습니다: ${SUMMARY} — 안내 생략" >&2
  exit 1
fi

# 안내 실패는 잡을 실패시킨다 — 보고서만 남고 아무도 모르는 것이 이 잡이 고치려는
# 문제 그 자체다(관측 실패의 침묵).
NOTIFY_RC=0
"${PY}" scripts/notify_health.py "${SUMMARY}" --report "${REPORT}" || NOTIFY_RC=$?
echo "notify_health exit=${NOTIFY_RC}"
exit "${NOTIFY_RC}"
