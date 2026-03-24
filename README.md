# 농업공고알림봇 (Agricultural Announcement Alert Bot)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests: 53 passed](https://img.shields.io/badge/tests-53%20passed-brightgreen.svg)](tests/)

농업/조경/치유산업 관련 정부 지원사업 공고를 자동으로 수집, 분석, 알림하는 시스템입니다. 키워드 기반 필터링과 Claude AI 분석을 통해 관련성 높은 공고만 선별하여 알려줍니다.

**Owner**: 이상민, (주)어반정글 (조경/치유산업/제조/공공조달), 네이처커뮤니티 (이끼재배/스마트팜)

## 주요 기능

- **자동 수집**: 5개 공고 출처(bizinfo API, 농림축산식품부 RSS, 스마트팜, 고양시, 농사로)에서 동시 크롤링
- **다단계 분석**: 키워드 매칭(로컬, 빠름) → Claude API 분석(고정도)
- **실시간 알림**: Telegram 개별 알림 + 이메일 다이제스트
- **지능형 분류**: 6개 도메인(이끼농업, 조경, 치유산업, 제조, 공공조달, AI/디지털) 자동 분류
- **지식 관리**: Vector DB 유사도 검색, Obsidian 자동 동기화, 자동 리서치 보고서 생성
- **유연한 실행**: 단일 실행, 데몬 모드, Telegram 봇 모드, 테스트 모드

## 빠른 시작

### 설치

**Python 3.11 이상 필요**

```bash
# 1. 저장소 클론
git clone <repo-url>
cd agrion-automation

# 2. 가상환경 생성 및 활성화
python -m venv venv
source venv/bin/activate  # macOS/Linux
# 또는
venv\Scripts\activate  # Windows

# 3. 의존성 설치
pip install -r requirements.txt

# 4. 설정 파일 준비
cp alert/.env.example alert/.env
# 필수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, EMAIL_USER, EMAIL_PASSWORD
# 선택: ANTHROPIC_API_KEY, OPENAI_API_KEY, OBSIDIAN_VAULT_PATH
```

### 환경변수 설정 (.env)

`alert/.env` 파일에 다음을 설정합니다:

```bash
# 텔레그램 알림
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# 이메일 알림 (Gmail 기준)
EMAIL_USER=your_email@gmail.com
EMAIL_PASSWORD=your_app_password_here

# AI 분석 (선택사항)
ANTHROPIC_API_KEY=your_anthropic_key  # Claude API

# 벡터 DB & 리서치 (선택사항)
OPENAI_API_KEY=your_openai_key        # 임베딩용

# 지식 관리 (선택사항)
OBSIDIAN_VAULT_PATH=/path/to/obsidian  # Obsidian 동기화용
N8N_WEBHOOK_URL=https://...            # n8n 이벤트 연동용
```

### 기본 사용

```bash
# 1회 실행 (수집 → 분석 → 알림)
python -m alert.main

# 테스트 모드 (첫 번째 크롤러만, 1개 알림)
python -m alert.main --test

# 데몬 모드 (config.yaml의 cron_hours에 맞춰 자동 실행)
python -m alert.main --daemon

# Telegram 봇 모드 (상호작용 커맨드)
python -m alert.main --bot

# 분기별 리서치 문서 생성
python -m alert.main --research

# 테스트 실행
pytest tests/
```

## 아키텍처

### 파이프라인 흐름

```
[cron: 09:00, 15:00]
         ↓
    main.py (진입점)
         ↓
[Stage 1: Crawling]
   ├─ bizinfo API (정부 지원사업)
   ├─ mafra RSS (농림축산식품부)
   ├─ smartfarm HTML (스마트팜코리아)
   ├─ goyang HTML (고양시 농업기술센터)
   └─ nongsaro HTML (농사로)
         ↓
    SQLite DB (중복 체크 + 저장)
         ↓
[Stage 2: Keyword Analysis]
    로컬 키워드 매칭 (빠름, 무료)
    threshold >= 0.3
         ↓
[Stage 3: Claude Analysis]  (선택사항)
    Claude API 관련성 평가
    threshold >= 0.3
         ↓
[Stage 4: Knowledge Layer]  (선택사항)
    ├─ 도메인 분류 (6개 카테고리)
    ├─ 벡터 DB 유사도 검색
    ├─ Obsidian 동기화
    └─ 이력 관리
         ↓
[Stage 5: Notification]
    ├─ Telegram (개별 알림)
    └─ Email (다이제스트)
         ↓
[Stage 6: History]
    실행 이력 기록
```

### 프로젝트 구조

```
agrion-automation/
├── alert/                          # 메인 패키지
│   ├── __init__.py
│   ├── __main__.py                 # python -m alert 진입점
│   ├── main.py                     # 파이프라인 로직 (5단계)
│   ├── config.py                   # 설정 로더 (config.yaml + .env)
│   ├── config.yaml                 # 모든 설정 (크롤러, 키워드, 분석 등)
│   ├── db.py                       # SQLite CRUD, WAL 모드, 마이그레이션
│   ├── models.py                   # 데이터모델 (RawAnnouncement, AnalyzedAnnouncement 등)
│   ├── analyzer.py                 # 2단계 분석 (키워드 + Claude)
│   ├── classifier.py               # 6개 도메인 분류기
│   ├── knowledge.py                # 지식층 오케스트레이터
│   ├── vectordb.py                 # sqlite-vec 벡터 DB
│   ├── obsidian.py                 # Obsidian 동기화 (CMDS 포맷)
│   ├── research_generator.py        # 자동 리서치 보고서 생성
│   ├── n8n_hook.py                 # n8n 웹훅 통합
│   ├── migrations.py               # DB 스키마 마이그레이션
│   │
│   ├── crawlers/                   # 5개 크롤러
│   │   ├── base.py                 # BaseCrawler 추상클래스
│   │   ├── bizinfo.py              # 비즈인포 API
│   │   ├── mafra.py                # 농림축산식품부 RSS
│   │   ├── smartfarm.py            # 스마트팜코리아 HTML
│   │   ├── goyang.py               # 고양시 농업기술센터 HTML
│   │   └── nongsaro.py             # 농사로 HTML
│   │
│   ├── notifiers/                  # 알림 모듈
│   │   ├── telegram_bot.py         # Telegram 개별 알림 + 봇 모드
│   │   └── email_sender.py         # 이메일 다이제스트
│   │
│   └── utils/
│       └── logger.py               # 로깅 설정
│
├── tests/                          # pytest 테스트 (53개)
│   ├── conftest.py
│   ├── test_analyzer.py
│   ├── test_classifier.py
│   ├── test_db.py
│   ├── test_migrations.py
│   ├── test_obsidian.py
│   └── test_config.py
│
├── docs/
│   └── n8n-setup.md               # n8n 웹훅 설정 가이드
│
├── requirements.txt                # 의존성
├── setup.sh                        # 초기 설치 스크립트
├── setup_cron.sh                   # Cron 스케줄 설정
├── config.yaml                     # (alert/ 내부에도 존재)
└── README.md                       # 이 파일
```

## 설정 (config.yaml)

### 크롤러 설정

```yaml
crawler:
  timeout: 30              # 요청 타임아웃 (초)
  retry_count: 3           # 재시도 횟수
  retry_delay: 5           # 재시도 간격 (초)
  user_agent: "AgriAlert/1.0"

  sources:
    bizinfo:
      enabled: true
      base_url: "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
    mafra:
      enabled: true
      base_url: "https://www.mafra.go.kr/bbs/mafra/71/rss.xml"
    smartfarm:
      enabled: true
      base_url: "https://www.smartfarmkorea.net"
    # ... 기타 크롤러
```

### 분석 설정

```yaml
analyzer:
  keyword_threshold: 0.3             # 키워드 분석 통과 점수
  claude_threshold: 0.3              # Claude 분석 통과 점수
  claude_model: "claude-sonnet-4-5-20250929"
  max_claude_calls_per_run: 50       # 한 번 실행당 Claude 호출 제한
```

### 키워드 필터 (config.yaml의 keywords)

3가지 카테고리:

- **must_match**: 이끼, 스마트팜, 조경, 치유농업, 사회적기업 등 (필수 키워드)
- **boost**: 농업, 청년농, 보조금, 경기도, ICT, 정원 등 (가중치 증가)
- **exclude**: 축산, 수산 등 (제외 키워드)

키워드는 `config.yaml`에서 자유롭게 추가/제거 가능합니다.

### 알림 설정

```yaml
notifier:
  telegram:
    enabled: true
    parse_mode: "HTML"
    max_message_length: 4096

  email:
    enabled: true
    smtp_server: "smtp.gmail.com"
    smtp_port: 587
    use_tls: true
    digest_hour: 18  # 매일 18시 다이제스트

schedule:
  cron_hours: [9, 15]    # 매일 09:00, 15:00 실행
  timezone: "Asia/Seoul"
```

### 지식층 설정 (Knowledge Layer)

```yaml
knowledge:
  enabled: true

  classifier:
    enabled: true
    method: "keyword"    # 또는 "claude"

  vector:
    enabled: true
    embedding_provider: "openai"
    embedding_model: "text-embedding-3-small"
    similarity_threshold: 0.75

  obsidian:
    enabled: true
    vault_path: "/path/to/vault"
    announcement_folder: "11.(주)어반정글/영업/공고"

  research:
    enabled: true
    schedule: "quarterly"
```

## 사용 모드 상세

### 1. 단일 실행 (기본)

```bash
python -m alert.main
```

1회 파이프라인 실행:
- 5개 크롤러에서 동시 수집
- 키워드 분석 + Claude 분석
- Telegram/이메일 알림
- DB에 저장

**로그 예시**:
```
Starting 농업공고알림봇 v1.0.0
========================================
--- Crawling: bizinfo ---
bizinfo: Fetched 150 announcements
bizinfo: 12 new, 138 duplicates
bizinfo: Running keyword analysis on 12 announcements
bizinfo: 8/12 passed keyword threshold (>= 0.3)
bizinfo: Running Claude analysis on 8 announcements
bizinfo: 5/8 passed Claude threshold (>= 0.3)
...
PIPELINE SUMMARY
========================================
Total announcements fetched: 420
Total new announcements: 45
Total relevant announcements: 18
Notifications sent: 18
```

### 2. 테스트 모드

```bash
python -m alert.main --test
```

- 첫 번째 사용 가능 크롤러만 실행
- 첫 번째 결과만 알림 (Telegram/이메일 테스트)
- 알림 표시 안 함 (다시 크롤 가능)
- 설정 검증 및 통합 테스트에 유용

### 3. 데몬 모드

```bash
python -m alert.main --daemon
```

백그라운드에서 계속 실행, `config.yaml`의 `cron_hours`에 맞춰 자동 실행:

```yaml
schedule:
  cron_hours: [9, 15]    # 매일 09:00, 15:00
  timezone: "Asia/Seoul"
```

**특징**:
- 60초마다 현재 시간 확인
- 지정된 시간이 되면 파이프라인 자동 실행
- 같은 시간에 2번 실행되지 않도록 방지
- Ctrl+C로 종료

**systemd 서비스 등록**:
```bash
# /etc/systemd/system/agrion-alert.service
[Unit]
Description=Agrion Automation Alert System
After=network.target

[Service]
Type=simple
User=agrion
WorkingDirectory=/opt/agrion-automation
ExecStart=/opt/agrion-automation/venv/bin/python -m alert.main --daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4. Telegram 봇 모드

```bash
python -m alert.main --bot
```

Telegram 봇으로서 실행되며 다음 커맨드 지원:

| 커맨드 | 설명 |
|--------|------|
| `/keywords` | 현재 필터링 키워드 목록 표시 |
| `/add` | 새 키워드 추가 (예: `/add 이끼`) |
| `/remove` | 키워드 제거 (예: `/remove 이끼`) |
| `/status` | 시스템 상태 (마지막 실행, DB 통계) |
| `/search <검색어>` | 공고 검색 (예: `/search 스마트팜`) |
| `/run` | 즉시 파이프라인 실행 |

**필수 환경변수**:
- `TELEGRAM_BOT_TOKEN`: 봇 토큰
- `TELEGRAM_CHAT_ID`: 채팅 ID

### 5. 리서치 모드

```bash
python -m alert.main --research
```

분기별 리서치 문서 자동 생성:
- 해당 분기의 공고 분석
- 도메인별 통계
- 트렌드 및 인사이트
- Obsidian에 자동 저장

## 크롤러

### 5개 크롤링 출처

| 크롤러 | URL | 타입 | 주기 |
|--------|-----|------|------|
| **bizinfo** | bizinfo.go.kr | REST API | 실시간 |
| **mafra** | mafra.go.kr | RSS Feed | 매일 |
| **smartfarm** | smartfarmkorea.net | HTML 파싱 | 매일 |
| **goyang** | goyang.go.kr/agri | HTML 파싱 | 매일 |
| **nongsaro** | nongsaro.go.kr | HTML 파싱 | 매일 |

### 크롤러 구현 패턴

모든 크롤러는 `BaseCrawler`를 상속:

```python
from alert.crawlers.base import BaseCrawler

class CustomCrawler(BaseCrawler):
    SOURCE_NAME = "custom_source"

    def fetch_announcements(self):
        """공고 수집 로직"""
        # 반환: List[RawAnnouncement]
        pass
```

## 분석 시스템

### 1단계: 키워드 분석

로컬 키워드 매칭 (빠름, 무료):

```python
# config.yaml에서 읽음
keywords:
  must_match:      # 반드시 포함 (weight 2)
    - "이끼"
    - "스마트팜"
  boost:           # 가중치 증가 (weight 1)
    - "농업"
    - "청년농"
  exclude:         # 제외 (score 0)
    - "축산"

# 점수 = (must_match_count * 2 + boost_count * 1) / 총키워드
# threshold >= 0.3 통과
```

**장점**: 빠름, 비용 없음, 제어 가능
**단점**: 의미 이해 불가능

### 2단계: Claude 분석

Claude API를 통한 고정도 분석 (선택사항):

```python
# 키워드 통과한 공고에 대해
# Claude에게 물어봄:
#   "다음 공고가 '이끼 재배, 스마트팜, 조경, 치유산업' 관련인가?"
#   점수: 0.0 ~ 1.0

# threshold >= 0.3 통과
```

**장점**: 의미 기반, 정확도 높음
**단점**: API 비용, 느림

### 3단계: 도메인 분류 (선택사항)

6개 도메인 자동 분류:

```
moss_agriculture      이끼농업/스마트팜
landscape             조경/정원
healing               치유산업
manufacturing         제조/굿즈
public_procurement    공공조달
ai_digital            AI/디지털
```

키워드 기반 또는 Claude 기반 분류 가능.

## 지식층 (Knowledge Layer)

선택사항이지만 강력한 기능들:

### 벡터 DB (sqlite-vec)

OpenAI 임베딩을 사용하여 유사 공고 자동 검색:

```python
# 새 공고가 들어오면
# 벡터 DB에서 유사 공고 5개 자동 추천
# similarity_threshold: 0.75
```

### Obsidian 동기화

매 공고마다 Obsidian 노트 자동 생성:

```markdown
---
title: "[공고] 스마트팜 기술 개발 지원사업"
source: bizinfo
date: 2024-03-25
domain: moss_agriculture
relevance: 0.85
url: https://...
---

## 개요
[공고 요약]

## 지원 대상
[대상]

## 신청 기간
2024-04-01 ~ 2024-05-31
```

### 자동 리서치 생성

분기별 자동 리서차 문서:
- 공고 통계 (도메인별, 시간별)
- 트렌드 분석
- 주요 기회 식별
- Obsidian에 저장

## 테스트

```bash
# 전체 테스트 실행
pytest tests/

# 특정 테스트 실행
pytest tests/test_analyzer.py

# Coverage 포함
pytest --cov=alert tests/

# Verbose 모드
pytest -v tests/
```

**포함된 테스트** (53개):
- 설정 로딩
- DB CRUD, 마이그레이션
- 키워드 분석
- 도메인 분류
- Obsidian 동기화
- 데이터 모델

## 데이터베이스

SQLite with WAL (Write-Ahead Logging) 모드:

**테이블**:
- `announcements`: 수집된 공고
- `keywords`: 필터링 키워드
- `run_history`: 실행 이력
- `application_records`: 신청/대응 이력 (Knowledge Layer)
- `research_documents`: 생성된 리서치 문서 (Knowledge Layer)
- `announcements_vector`: 벡터 임베딩 (Knowledge Layer, sqlite-vec)

**위치**: `alert/data/alerts.db`

**마이그레이션**: `migrations.py`에서 자동 관리

## 알림

### Telegram

**개별 알림**:
```
🔔 새로운 공고

제목: [공고] 경기도 청년농 스마트팜 보조금
도메인: 이끼농업/스마트팜
점수: 0.87
마감: 2024-05-31
URL: https://...
```

**특징**:
- HTML 포맷 지원
- 최대 4096자 자동 분할
- 배치 전송 지원

### 이메일

**다이제스트** (매일 18시):
```
제목: [농업공고] 2024-03-25 요약 (18개)

오늘의 공고:
1. [공고] 경기도 청년농 스마트팜 보조금 (점수: 0.87)
2. ...

--
농업공고알림봇 v1.0.0
```

**특징**:
- HTML 이메일
- 배치 전송
- 타임존 인식

## 트러블슈팅

### Claude API 요청 실패

```
ERROR: Claude analysis failed for announcement X: RateLimitError
```

해결책:
1. `ANTHROPIC_API_KEY` 확인
2. API 할당량 확인
3. `config.yaml`의 `max_claude_calls_per_run` 감소

### 크롤러 실패

```
WARNING: Could not import mafra crawler: [error]
```

**원인**: 특정 크롤러 의존성 누락
**해결책**:
1. `requirements.txt` 다시 설치
2. 또는 해당 크롤러 비활성화 (`config.yaml`의 `enabled: false`)

### Telegram 알림 미발송

```
ERROR: Bot configuration error: Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

해결책:
1. `.env` 파일 위치 확인 (`alert/.env`)
2. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 설정
3. `python -m alert.main --test`로 테스트

### 데이터베이스 잠금

```
ERROR: database is locked
```

**원인**: 여러 프로세스가 동시에 DB 접근
**해결책**:
1. 다른 실행 중인 프로세스 종료
2. WAL 파일 확인: `ls -la alert/data/alerts.db*`
3. 필요시 삭제: `rm alert/data/alerts.db-wal`

## 선택사항 기능 활성화

### Claude API (AI 분석)

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-...
```

`config.yaml`:
```yaml
analyzer:
  claude_threshold: 0.3
```

### Vector DB (유사도 검색)

```bash
pip install sqlite-vec openai
export OPENAI_API_KEY=sk-...
```

`config.yaml`:
```yaml
knowledge:
  vector:
    enabled: true
    embedding_provider: "openai"
```

### Obsidian 동기화

`config.yaml`:
```yaml
knowledge:
  obsidian:
    enabled: true
    vault_path: "/path/to/my/vault"
    announcement_folder: "11.(주)어반정글/영업/공고"
```

### n8n 연동

`config.yaml`:
```yaml
knowledge:
  n8n:
    enabled: true
    webhook_url: "https://n8n.example.com/webhook/..."
    notify_on: ["new_relevant", "status_change"]
```

상세: [n8n-setup.md](docs/n8n-setup.md)

## 성능 최적화

### 크롤링 성능

- 타임아웃: `config.yaml`의 `timeout` (기본 30초)
- 재시도: `retry_count` (기본 3)
- User-Agent: `user_agent` (기본 "AgriAlert/1.0")

### 분석 성능

- Claude 호출 제한: `max_claude_calls_per_run` (기본 50)
- 배치 크기: 자동 최적화

### 데이터베이스

- WAL 모드: 동시성 개선
- 인덱싱: `source`, `source_id`, `notified_at` 자동 생성
- 정기 정리: `VACUUM` (선택)

## 개발 가이드

### 새 크롤러 추가

1. `alert/crawlers/` 디렉토리에 새 파일 생성
2. `BaseCrawler` 상속
3. `fetch_announcements()` 구현

```python
# alert/crawlers/example.py
from .base import BaseCrawler
from ..models import RawAnnouncement

class ExampleCrawler(BaseCrawler):
    SOURCE_NAME = "example"

    def fetch_announcements(self):
        """공고 수집"""
        # HTTP 요청, HTML 파싱 등
        announcements = []

        # ... 크롤링 로직

        return announcements
```

4. `config.yaml`에 등록:
```yaml
crawler:
  sources:
    example:
      enabled: true
      base_url: "https://example.com"
```

5. `main.py`의 `_import_crawlers()` 업데이트:
```python
crawler_modules = [
    # ... 기존 크롤러
    ("example", "alert.crawlers.example", "ExampleCrawler"),
]
```

### 새 분석기 추가

```python
# alert/custom_analyzer.py
from .models import AnalyzedAnnouncement

class CustomAnalyzer:
    def analyze_batch(self, announcements):
        """분석 로직"""
        for ann in announcements:
            ann.relevance_score = self.calculate_score(ann)
        return announcements
```

### 로깅

```python
import logging
logger = logging.getLogger(__name__)

logger.info("message")
logger.warning("warning")
logger.error("error", exc_info=True)
```

로그 레벨은 `config.yaml`에서 설정:
```yaml
app:
  log_level: "INFO"  # DEBUG, INFO, WARNING, ERROR
```

## 라이선스

MIT License - 자유롭게 사용, 수정, 배포 가능

## 기여

Pull Request 환영합니다!

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Open a Pull Request

## 지원

이슈/질문은 GitHub Issues에 등록하세요.

---

**만든이**: 이상민
**조직**: (주)어반정글, 네이처커뮤니티
**마지막 업데이트**: 2024-03-25
