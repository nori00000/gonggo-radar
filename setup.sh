#!/bin/bash
# 농업공고알림봇 설치 스크립트
set -e

echo "=== 농업공고알림봇 설치 ==="

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create data directory
mkdir -p alert/data/logs

# Copy env example if .env doesn't exist
if [ ! -f alert/.env ]; then
    cp alert/.env.example alert/.env
    echo "⚠️  alert/.env 파일을 수정하세요 (API 키, 토큰 등)"
fi

echo ""
echo "=== 설치 완료 ==="
echo ""
echo "다음 단계:"
echo "1. alert/.env 파일에 API 키와 토큰을 설정하세요"
echo "2. 테스트 실행: python -m alert.main --test"
echo "3. 1회 실행: python -m alert.main"
echo "4. 데몬 모드: python -m alert.main --daemon"
echo "5. 봇 모드: python -m alert.main --bot"
