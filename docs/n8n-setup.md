# n8n 워크플로우 연동 가이드

## 개요

agrion-automation의 Knowledge Layer가 관련성 높은 공고를 발견하면 n8n 웹훅으로 이벤트를 전송합니다.

## 사전 요구사항

- n8n 서버가 실행 중이어야 합니다 (M1 Mac Mini #3)
- agrion-automation의 `.env`에 `N8N_WEBHOOK_URL` 설정 필요

## 1단계: n8n 웹훅 노드 생성

1. n8n 대시보드에서 새 워크플로우 생성
2. **Webhook** 트리거 노드 추가
   - HTTP Method: `POST`
   - Path: `/webhook/agrion`
   - Authentication: None (내부망)
3. 웹훅 URL 복사 (예: `http://192.168.x.x:5678/webhook/agrion`)

## 2단계: 워크플로우 설계

### 추천 워크플로우 구조

```
Webhook Trigger
  → Switch (event 필드 기준)
    → "new_relevant_announcement"
      → Telegram/Slack 추가 알림
      → Google Sheets 기록
    → "application_status_changed"
      → 이력 업데이트 알림
    → "research_report_generated"
      → 리포트 공유
```

### 수신 페이로드 구조

```json
{
  "event": "new_relevant_announcement",
  "timestamp": "2026-03-24T09:00:00.000000",
  "announcement": {
    "id": 42,
    "title": "2026년 스마트팜 혁신 지원사업",
    "source": "bizinfo",
    "business_domain": "moss_agriculture",
    "relevance_score": 0.85,
    "url": "https://www.bizinfo.go.kr/...",
    "period_end": "2026-04-30"
  },
  "obsidian_path": "11.(주)어반정글/영업/공고/2026-03-24-bizinfo-스마트팜-혁신-지원사업.md"
}
```

### 이벤트 종류

| 이벤트 | 설명 | notify_on 카테고리 |
|--------|------|-------------------|
| `new_relevant_announcement` | 관련성 높은 새 공고 발견 | `new_relevant` |
| `application_status_changed` | 신청 상태 변경 | `status_change` |
| `research_report_generated` | 리서치 보고서 생성 | `research_generated` |

## 3단계: 환경 설정

### `.env` 파일

```bash
N8N_WEBHOOK_URL=http://192.168.x.x:5678/webhook/agrion
```

### `config.yaml` (이미 설정됨)

```yaml
knowledge:
  n8n:
    enabled: true      # false → true로 변경
    webhook_url: ""    # .env에서 오버라이드
    notify_on: ["new_relevant", "status_change"]
```

## 4단계: 연동 테스트

```bash
source venv/bin/activate
python -c "
from alert.config import get_config
from alert.n8n_hook import N8nHook

cfg = get_config(reload=True)
hook = N8nHook(cfg.knowledge.n8n)
print(f'Webhook URL: {hook._webhook_url}')
print(f'Notify on: {hook._notify_on}')

# 테스트 이벤트 전송
result = hook.send_event('new_relevant_announcement')
print(f'Send result: {result}')
hook.close()
"
```

## 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `send_event` returns False | webhook_url 비어있음 | `.env`에 URL 설정 |
| Connection refused | n8n 서버 미실행 | M1 Mini에서 n8n 시작 |
| 404 Not Found | 웹훅 경로 불일치 | n8n에서 경로 확인 |
| 이벤트 미수신 | notify_on 설정 | `config.yaml`에서 카테고리 확인 |
