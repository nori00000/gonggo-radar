#!/usr/bin/env bash
# digest_job.sh — launchd(com.gonggo-radar.digest)가 실행하는 주간 다이제스트 잡.
#
# 왜 래퍼인가: launchd는 "생성 후 미리보기 전송"이라는 두 단계를 한 번에 돌려야 한다
# (계약 W10). 생성이 실패(pass=false)하면 미리보기도 실패 사유를 보여야 하므로
# notify는 생성 결과와 무관하게 실행하고, 잡의 종료 코드는 생성 결과를 따른다.
#
# 발송은 이 잡이 하지 않는다 — 사람이 텔레그램 [발송] 버튼으로만 한다.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}" || exit 1
PY="${ROOT}/venv/bin/python"
DB="${GONGGO_DB:-alert/data/announcements.db}"

WEEK="${1:-$("${PY}" -c 'import datetime; y, w, _ = datetime.date.today().isocalendar(); print(f"{y}-W{w:02d}")')}"
MD="digests/${WEEK}.md"

echo "=== digest_job ${WEEK} ($(date "+%Y-%m-%d %H:%M:%S")) ==="

"${PY}" scripts/weekly_digest.py --db "${DB}" --week "${WEEK}" --exclude-state
GEN_RC=$?
echo "weekly_digest exit=${GEN_RC}"

if [ ! -f "${MD}" ]; then
  echo "다이제스트 파일 없음: ${MD} — 미리보기 생략" >&2
  exit "${GEN_RC}"
fi

"${PY}" scripts/notify_digest.py "${MD}"
NOTIFY_RC=$?
echo "notify_digest exit=${NOTIFY_RC}"

if [ "${GEN_RC}" -ne 0 ]; then
  exit "${GEN_RC}"
fi
exit "${NOTIFY_RC}"
