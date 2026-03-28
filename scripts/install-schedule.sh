#!/bin/bash
# 공고레이더 스케줄 설치
set -e

PLIST_NAME="com.gonggo-radar.schedule.plist"
PLIST_SRC="$(dirname "$0")/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LOG_DIR="$HOME/gonggo-radar/logs"

mkdir -p "$LOG_DIR"

# 기존 서비스 해제
launchctl unload "$PLIST_DST" 2>/dev/null || true

# plist 복사 및 로드
cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"

echo "✅ 공고레이더 스케줄 설치 완료"
echo "   실행 시간: 매일 09:00, 14:00, 19:00"
echo "   로그: $LOG_DIR/schedule.log"
echo ""
echo "상태 확인: launchctl list | grep gonggo"
echo "수동 실행: launchctl start com.gonggo-radar.schedule"
echo "해제: launchctl unload ~/Library/LaunchAgents/$PLIST_NAME"
