# 레인 간 계약 (SSOT) — 협의회 주간 정책브리핑
- 레포: gonggo-radar (origin/main 기준). 새 레포 금지. Anthropic API 호출 금지(ANTHROPIC_API_KEY 미설정 전제).
- 입력 DB: `alert/data/announcements.db` (SQLite, 테이블 announcements: id source source_id title summary url author category target period_start period_end relevance_score relevance_reason matched_keywords is_notified raw_data created_at updated_at business_domain domain_confidence obsidian_path embedding_id). 스키마 변경 금지.
- 폼 입력: `forms/responses.csv` (열: 접수일, 회원사, 유형[동정|의견], 내용, 관련정책). 없으면 빈 섹션.
- 다이제스트 출력: `digests/YYYY-Www.md` — 섹션 순서 고정: ## 산림 정책 동향 / ## 지원사업 공고 / ## 사회연대경제 동향 / ## 회원사 동정 / ## 협의회 의견. 항목 = 제목·기관·마감일·원문 URL·3줄 요약(요약은 DB summary 그대로, 생성 안 함). 회당 최대 5건(공고 섹션 합산). "협의회 의견" 섹션은 정확히 `<!-- 상민 확정 필요 -->` 한 줄로 시작.
- 팩트 게이트 출력: `digests/YYYY-Www.check.json` — {"items":[{"url":..., "url_alive":bool, "deadline_parsed":bool}], "pass":bool}. 
- 발송 스크립트: 마크다운에 `<!-- 상민 확정 필요 -->`가 남아 있거나 check.json의 pass=false면 exit 2로 거부(fail-closed). 발송은 기존 `alert/notifiers/email_sender.py`의 SMTP 경로 재사용. `--dry-run`이 기본, `--send`를 명시해야 실제 발송.
- 폴백 표기: 초안 생성 레인이 바뀌면 파일 머리 HTML 주석에 레인명.
