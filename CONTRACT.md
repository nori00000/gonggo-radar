# 레인 간 계약 (SSOT) — 협의회 주간 정책브리핑
- 레포: gonggo-radar (origin/main 기준). 새 레포 금지. Anthropic API 호출 금지(ANTHROPIC_API_KEY 미설정 전제).
- 입력 DB: `alert/data/announcements.db` (SQLite, 테이블 announcements: id source source_id title summary url author category target period_start period_end relevance_score relevance_reason matched_keywords is_notified raw_data created_at updated_at business_domain domain_confidence obsidian_path embedding_id). 스키마 변경 금지.
- 폼 입력: `forms/responses.csv` (열: 접수일, 회원사, 유형[동정|의견], 내용, 관련정책). 없으면 빈 섹션.
- 다이제스트 출력: `digests/YYYY-Www.md` — 섹션 순서 고정: ## 산림 정책 동향 / ## 지원사업 공고 / ## 사회연대경제 동향 / ## 회원사 동정 / ## 협의회 의견. 항목 = 제목·기관·마감일·원문 URL·3줄 요약(요약은 DB summary 그대로, 생성 안 함). 회당 최대 5건(공고 섹션 합산). "협의회 의견" 섹션은 정확히 `<!-- 상민 확정 필요 -->` 한 줄로 시작.
- 팩트 게이트 출력: `digests/YYYY-Www.check.json` — {"items":[{"url":..., "url_alive":bool, "deadline_parsed":bool}], "pass":bool}. 
- 발송 스크립트: 마크다운에 `<!-- 상민 확정 필요 -->`가 남아 있거나 check.json의 pass=false면 exit 2로 거부(fail-closed). 발송은 기존 `alert/notifiers/email_sender.py`의 SMTP 경로 재사용. `--dry-run`이 기본, `--send`를 명시해야 실제 발송.
- 폴백 표기: 초안 생성 레인이 바뀌면 파일 머리 HTML 주석에 레인명.

## 개정 v1.1 (2026-09-12, 게이트 판정 — W3 크리틱 28건 수렴)
- **상한**: 지원사업 공고 ≤5건, 산림 정책 동향 ≤3건, 사회연대경제 동향 ≤3건 (섹션별 상한, 전역 상한 없음). 제목 정규화 기준 중복 제거 후 상한 적용.
- **주간 창**: `--week`의 월~일 양끝 포함(`week_start <= DATE(created_at) <= week_end`).
- **fail-closed 정의**: check.json 부재·손상·`pass=false`·항목 0건·`network_checked=false` 중 하나라도 해당하면 발송 거부 exit 2. `--skip-check` 옵션 금지. `--dry-run`과 `--send` 동시 지정 시 dry-run 우선(발송 안 함).
- **check.json 필드**: `{"items":[...], "pass":bool, "network_checked":bool, "reason":str}`.
- **렌더**: 마감일 없으면 `**마감:** 미정`, 요약 없으면 `*(요약 없음)*` 명시. 요약은 최대 3줄. 제목은 공백 정규화(개행·탭 제거). HTML 변환 시 모든 텍스트 `html.escape`, href는 http/https만 허용.
- **폼 CSV**: `utf-8-sig`. `유형=동정`만 회원사 동정 섹션에. `유형=의견` 행은 `digests/YYYY-Www.opinions.md`에 별도 저장(상민 해설 입력용, 브리핑 본문에 넣지 않음). 로드 실패는 stderr + check.json `reason`에 기록.
- **발송**: `alert/notifiers/email_sender.py`의 기존 SMTP 경로를 반드시 재사용, 수신자 전체에 발송. 
- **레인 표기**: 생성 파일 머리에 `<!-- lane: <이름> -->` 항상 표기(조건부 아님).
- **실행**: 스크립트는 `sys.path` 보정으로 `venv/bin/python scripts/x.py` 그대로 실행 가능해야 함. `weekly_digest.py`는 pass=false면 exit 1.
- **산출물**: `digests/` 는 .gitignore (생성물, 커밋 안 함).
- **호환**: CI = Python 3.13, ruff `--select E,F,W` 통과 필수.
