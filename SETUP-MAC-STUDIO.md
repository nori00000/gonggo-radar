# 공고레이더 Mac Studio 설정 가이드

> 2026-03-28 작성 | M4 Studio 128GB + Ollama Qwen 2.5 70B

## 1. 프로젝트 클론 및 의존성 설치

```bash
cd ~
git clone https://github.com/nori00000/gonggo-radar.git
cd gonggo-radar
pip3 install -r requirements.txt
python3 -m playwright install chromium
```

## 2. 환경변수 설정

```bash
cp alert/.env.example alert/.env
```

`alert/.env` 파일 편집:

```env
# Telegram (필수)
TELEGRAM_BOT_TOKEN=REDACTED-ROTATED-2026-07-03
TELEGRAM_CHAT_ID=1401666801

# Claude API (선택 - 없으면 자동으로 Ollama 사용)
# ANTHROPIC_API_KEY=

# API키 (선택 - 있으면 추가 크롤러 활성화)
# BIZINFO_API_KEY=       # bizinfo.go.kr에서 발급
# DATA_GO_KR_API_KEY=    # data.go.kr에서 발급 (g2b, kstartup, subsidy24 공용)
```

## 3. Ollama 모델 확인

```bash
# 모델 확인
ollama list

# qwen2.5:70b 없으면 설치
ollama pull qwen2.5:70b

# 동작 테스트
ollama run qwen2.5:70b "안녕" --verbose
```

config에서 모델 변경하려면 `alert/config.yaml`:
```yaml
analyzer:
  llm_backend: "auto"           # auto | claude | ollama
  ollama_base_url: "http://localhost:11434"
  ollama_model: "qwen2.5:70b"   # 여기서 모델명 변경 가능
```

## 4. 테스트 실행

```bash
cd ~/gonggo-radar

# 1회 테스트 (크롤러 1개만)
python3 -m alert.main --test

# 전체 실행 (28개 크롤러)
python3 -m alert.main

# Telegram 봇 모드 (대화형)
python3 -m alert.main --bot
```

## 5. 자동 스케줄 설치 (하루 3회: 09:00, 14:00, 19:00)

```bash
bash ~/gonggo-radar/scripts/install-schedule.sh
```

확인:
```bash
# 스케줄 상태
launchctl list | grep gonggo

# 수동 실행
launchctl start com.gonggo-radar.schedule

# 로그 확인
tail -f ~/gonggo-radar/logs/schedule.log

# 스케줄 해제
launchctl unload ~/Library/LaunchAgents/com.gonggo-radar.schedule.plist
```

## 시스템 구조

```
크롤링(28소스, 병렬5x) → 키워드분석 → AI분석(Qwen 70B) → Telegram 알림
                                         ↑
                              ANTHROPIC_API_KEY 있으면 Claude
                              없으면 자동으로 Ollama/Qwen
```

## 수집 현황 (2026-03-28 기준)

- 28개 크롤러 전체 작동 (API키 미설정 4개 제외)
- 수집량: 468건/회
- 실행 시간: ~13초
- 토큰 비용: 0원 (Ollama 로컬)
