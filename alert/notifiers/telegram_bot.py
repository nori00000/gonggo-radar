"""Telegram 알림 전송 모듈 -- Bot API를 통한 메시지 전송."""

import os
import time
from typing import List, Dict, Any, Optional

import requests

from alert.models import AnalyzedAnnouncement, Keyword
from alert.utils.logger import setup_logger


class TelegramNotifier:
    """Telegram Bot API를 사용한 알림 발송."""

    def __init__(self, db=None) -> None:
        """환경변수에서 Telegram 설정을 로드하고 로거 초기화.

        Args:
            db: Optional Database instance for fetching domain information.
        """
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.db = db
        self.logger = setup_logger("telegram_notifier")

        if not self.bot_token or not self.chat_id:
            self.logger.warning(
                "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다. "
                "Telegram 알림이 비활성화됩니다."
            )

    def _send_api_request(self, text: str, parse_mode: str = "HTML") -> bool:
        """Telegram Bot API로 메시지 전송.

        Args:
            text: 전송할 메시지 본문
            parse_mode: 파싱 모드 (HTML 또는 Markdown)

        Returns:
            전송 성공 여부
        """
        if not self.bot_token or not self.chat_id:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        # Telegram 메시지 길이 제한: 4096자
        if len(text) > 4096:
            text = text[:4090] + "\n... (생략)"
            self.logger.warning("메시지가 4096자를 초과하여 잘렸습니다.")

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            self.logger.info("Telegram 메시지 전송 성공")
            return True
        except requests.exceptions.RequestException as exc:
            self.logger.error(f"Telegram 메시지 전송 실패: {exc}")
            return False

    def send_announcement(self, ann: AnalyzedAnnouncement) -> bool:
        """단일 공고를 Telegram으로 전송.

        Args:
            ann: 분석된 공고 객체

        Returns:
            전송 성공 여부
        """
        # HTML 포맷 메시지 생성
        period = ""
        if ann.period_start or ann.period_end:
            start = ann.period_start or "미정"
            end = ann.period_end or "미정"
            period = f"{start} ~ {end}"
        else:
            period = "미정"

        # 요약이 너무 길면 200자로 제한
        summary = ann.summary[:200] if ann.summary else "요약 없음"
        if len(ann.summary) > 200:
            summary += "..."

        # 관련도 이유도 100자로 제한
        reason = ann.relevance_reason[:100] if ann.relevance_reason else "미상"
        if len(ann.relevance_reason) > 100:
            reason += "..."

        score_pct = ann.relevance_score * 100

        message = f"""🌾 <b>{ann.title}</b>

📋 {summary}
🏛 주관: {ann.author or "미상"}
🎯 대상: {ann.target or "미상"}
📅 기간: {period}
📊 관련도: {score_pct:.0f}% ({reason})
🔗 <a href="{ann.url}">상세보기</a>"""

        # Add domain tag if available
        if self.db and hasattr(ann, 'id') and ann.id:
            domain, confidence = self.db.get_announcement_domain(ann.id)
            if domain:
                # Map domain key to Korean label
                domain_labels = {
                    "moss_agriculture": "이끼농업/스마트팜",
                    "landscape": "조경/정원",
                    "healing": "치유산업",
                    "manufacturing": "제조/굿즈",
                    "public_procurement": "공공조달",
                    "ai_digital": "AI/디지털",
                }
                label = domain_labels.get(domain, domain)
                message += f"\n🏷 도메인: {label}"

        return self._send_api_request(message)

    def send_batch(self, announcements: List[AnalyzedAnnouncement]) -> int:
        """여러 공고를 순차적으로 전송.

        Args:
            announcements: 분석된 공고 리스트

        Returns:
            성공적으로 전송된 메시지 개수
        """
        success_count = 0

        for ann in announcements:
            if self.send_announcement(ann):
                success_count += 1

            # Rate limiting 방지를 위한 1초 대기
            if len(announcements) > 1:
                time.sleep(1)

        self.logger.info(
            f"Telegram 일괄 전송 완료: {success_count}/{len(announcements)} 건 성공"
        )
        return success_count

    def send_text(self, text: str) -> bool:
        """일반 텍스트 메시지 전송 (상태 업데이트 등).

        Args:
            text: 전송할 텍스트

        Returns:
            전송 성공 여부
        """
        return self._send_api_request(text, parse_mode="HTML")


class TelegramBot:
    """Telegram bot with command handlers for keyword management and status."""

    def __init__(self) -> None:
        """환경변수에서 Telegram 설정을 로드하고 DB 연결 및 로거 초기화."""
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.logger = setup_logger("telegram_bot")

        if not self.bot_token or not self.chat_id:
            self.logger.error(
                "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다."
            )
            raise ValueError("Telegram bot configuration missing")

        # Import DB here to avoid circular dependencies
        from alert.db import Database
        self.db = Database()

        # Track last processed update ID for getUpdates
        self.last_update_id: Optional[int] = None

        # Command handlers mapping
        self.commands: Dict[str, Any] = {
            "/start": self.cmd_help,
            "/help": self.cmd_help,
            "/keywords": self.cmd_keywords,
            "/add": self.cmd_add,
            "/remove": self.cmd_remove,
            "/status": self.cmd_status,
            "/search": self.cmd_search,
            "/run": self.cmd_run,
            "/recent": self.cmd_recent,
            "/app": self.cmd_app,
            "/apply": self.cmd_apply,
            "/appstatus": self.cmd_appstatus,
            "/apphistory": self.cmd_apphistory,
            "/submit": self.cmd_submit,
        }

    def _send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram Bot API.

        Args:
            text: Message text to send
            parse_mode: Parsing mode (HTML or Markdown)

        Returns:
            True if successful, False otherwise
        """
        if not self.bot_token or not self.chat_id:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        # Telegram message length limit: 4096 characters
        if len(text) > 4096:
            text = text[:4090] + "\n... (생략)"
            self.logger.warning("메시지가 4096자를 초과하여 잘렸습니다.")

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            self.logger.info("메시지 전송 성공")
            return True
        except requests.exceptions.RequestException as exc:
            self.logger.error(f"메시지 전송 실패: {exc}")
            return False

    def _get_updates(self, timeout: int = 30) -> List[Dict[str, Any]]:
        """Get updates from Telegram using long polling.

        Args:
            timeout: Long polling timeout in seconds

        Returns:
            List of update objects
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"

        params = {
            "timeout": timeout,
        }

        if self.last_update_id is not None:
            params["offset"] = self.last_update_id + 1

        try:
            response = requests.get(url, params=params, timeout=timeout + 5)
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                return data.get("result", [])
            else:
                self.logger.error(f"getUpdates failed: {data}")
                return []

        except requests.exceptions.RequestException as exc:
            self.logger.error(f"getUpdates request failed: {exc}")
            return []

    def start_polling(self) -> None:
        """Start bot in polling mode with infinite loop."""
        self.logger.info("Telegram Bot 시작 (polling mode)")
        self.logger.info(f"Bot Token: {self.bot_token[:20]}...")
        self.logger.info(f"Chat ID: {self.chat_id}")

        self._send_message("🤖 농업공고알림봇이 시작되었습니다. /help 를 입력하세요.")

        try:
            while True:
                updates = self._get_updates(timeout=30)

                for update in updates:
                    self._process_update(update)

                    # Update last_update_id
                    update_id = update.get("update_id")
                    if update_id is not None:
                        self.last_update_id = update_id

        except KeyboardInterrupt:
            self.logger.info("\nBot 종료됨 (Ctrl+C)")
            self._send_message("🛑 농업공고알림봇이 종료되었습니다.")
        except Exception as e:
            self.logger.error(f"Polling 중 예외 발생: {e}", exc_info=True)
            self._send_message(f"❌ 봇 에러: {e}")
        finally:
            self.db.close()

    def _process_update(self, update: Dict[str, Any]) -> None:
        """Process a single update from Telegram.

        Args:
            update: Update object from getUpdates API
        """
        message = update.get("message")
        if not message:
            return

        text = message.get("text", "").strip()
        if not text:
            return

        # Only process messages from the configured chat_id
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self.chat_id:
            self.logger.warning(f"Ignoring message from unauthorized chat: {chat_id}")
            return

        self.logger.info(f"Received command: {text}")

        # Parse command and arguments
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Route to handler
        handler = self.commands.get(command)
        if handler:
            try:
                handler(args)
            except Exception as e:
                self.logger.error(f"Command handler error: {e}", exc_info=True)
                self._send_message(f"❌ 명령 처리 중 오류가 발생했습니다: {e}")
        else:
            self._send_message(
                f"❓ 알 수 없는 명령입니다: {command}\n/help 를 입력하여 사용 가능한 명령을 확인하세요."
            )

    # -------------------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------------------

    def cmd_help(self, args: str) -> None:
        """Show available commands."""
        help_text = """🌾 <b>농업공고알림봇 커맨드</b>

/keywords - 현재 키워드 목록
/add &lt;카테고리&gt; &lt;키워드&gt; - 키워드 추가
  카테고리: must_match, boost, exclude
  예: /add boost 스마트팜
/remove &lt;키워드&gt; - 키워드 삭제
  예: /remove 축산
/status - 시스템 상태 (최근 실행, DB 통계)
/search &lt;검색어&gt; - 공고 검색
  예: /search 이끼
/run - 즉시 크롤링 실행
/recent - 최근 공고 5건
/submit &lt;URL&gt; [설명] - 수동 제보
  예: /submit https://example.com 스마트팜 지원사업

<b>신청 관리:</b>
/app &lt;id&gt; - 공고 신청 현황 조회
/apply &lt;id&gt; - 신청 완료로 표시
/appstatus &lt;id&gt; &lt;status&gt; - 신청 상태 변경
/apphistory [도메인] - 신청 이력 조회

/help - 이 도움말 보기"""

        self._send_message(help_text)

    def cmd_keywords(self, args: str) -> None:
        """Show all active keywords grouped by category."""
        keywords = self.db.get_keywords()

        if not keywords:
            self._send_message("📝 등록된 키워드가 없습니다.")
            return

        # Group by category
        by_category: Dict[str, List[str]] = {
            "must_match": [],
            "boost": [],
            "exclude": [],
        }

        for kw in keywords:
            if kw.is_active and kw.category in by_category:
                by_category[kw.category].append(kw.keyword)

        # Format message
        lines = ["🔑 <b>현재 키워드 목록</b>\n"]

        if by_category["must_match"]:
            lines.append("<b>필수 매칭 (Must Match):</b>")
            lines.append(", ".join(by_category["must_match"]))
            lines.append("")

        if by_category["boost"]:
            lines.append("<b>가중치 부여 (Boost):</b>")
            lines.append(", ".join(by_category["boost"]))
            lines.append("")

        if by_category["exclude"]:
            lines.append("<b>제외 (Exclude):</b>")
            lines.append(", ".join(by_category["exclude"]))

        self._send_message("\n".join(lines))

    def cmd_add(self, args: str) -> None:
        """Add a new keyword.

        Args:
            args: "<category> <keyword>"
        """
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            self._send_message(
                "❌ 사용법: /add &lt;카테고리&gt; &lt;키워드&gt;\n"
                "카테고리: must_match, boost, exclude\n"
                "예: /add boost 스마트팜"
            )
            return

        category, keyword = parts
        category = category.strip().lower()
        keyword = keyword.strip()

        if category not in ["must_match", "boost", "exclude"]:
            self._send_message(
                "❌ 잘못된 카테고리입니다. must_match, boost, exclude 중 하나를 선택하세요."
            )
            return

        # Determine weight based on category
        weight = 2.0 if category == "must_match" else 1.0

        kw = Keyword(keyword=keyword, category=category, weight=weight, is_active=True)

        if self.db.insert_keyword(kw):
            self._send_message(f"✅ 키워드 추가됨: <b>{keyword}</b> ({category})")
        else:
            self._send_message(f"⚠️ 이미 존재하는 키워드입니다: {keyword}")

    def cmd_remove(self, args: str) -> None:
        """Remove a keyword.

        Args:
            args: "<keyword>"
        """
        keyword = args.strip()
        if not keyword:
            self._send_message(
                "❌ 사용법: /remove &lt;키워드&gt;\n예: /remove 축산"
            )
            return

        if self.db.remove_keyword(keyword):
            self._send_message(f"✅ 키워드 삭제됨: <b>{keyword}</b>")
        else:
            self._send_message(f"⚠️ 키워드를 찾을 수 없습니다: {keyword}")

    def cmd_status(self, args: str) -> None:
        """Show system status and statistics."""
        stats = self.db.get_stats()
        recent_runs = self.db.get_recent_runs(limit=5)

        # Format statistics
        lines = ["📊 <b>시스템 상태</b>\n"]
        lines.append(f"전체 공고: {stats['total']} 건")
        lines.append(f"알림 완료: {stats['notified']} 건")
        lines.append(f"알림 대기: {stats['unnotified']} 건\n")

        # By source
        if stats.get("by_source"):
            lines.append("<b>출처별 통계:</b>")
            for source, count in stats["by_source"].items():
                lines.append(f"  • {source}: {count} 건")
            lines.append("")

        # Recent runs
        if recent_runs:
            lines.append("<b>최근 실행 내역:</b>")
            for run in recent_runs[:3]:
                status_emoji = "✅" if run["status"] == "success" else "❌"
                lines.append(
                    f"{status_emoji} {run['source']}: "
                    f"{run['new_count']}건 신규, {run['relevant_count']}건 관련"
                )
                if run.get("finished_at"):
                    lines.append(f"   {run['finished_at'][:16]}")
        else:
            lines.append("실행 내역이 없습니다.")

        self._send_message("\n".join(lines))

    def cmd_search(self, args: str) -> None:
        """Search announcements.

        Args:
            args: "<query>"
        """
        query = args.strip()
        if not query:
            self._send_message(
                "❌ 사용법: /search &lt;검색어&gt;\n예: /search 이끼"
            )
            return

        results = self.db.search_announcements(query, limit=5)

        if not results:
            self._send_message(f"🔍 '{query}'에 대한 검색 결과가 없습니다.")
            return

        lines = [f"🔍 <b>'{query}' 검색 결과</b> ({len(results)}건)\n"]

        for i, ann in enumerate(results, 1):
            score_pct = ann.relevance_score * 100
            lines.append(f"<b>{i}. {ann.title}</b>")
            lines.append(f"   📊 관련도: {score_pct:.0f}%")
            lines.append(f"   🔗 <a href=\"{ann.url}\">상세보기</a>")
            lines.append("")

        self._send_message("\n".join(lines))

    def cmd_run(self, args: str) -> None:
        """Trigger immediate crawling."""
        self._send_message("⚙️ 크롤링을 시작합니다...\n잠시 기다려 주세요.")

        try:
            # Import run_pipeline here to avoid circular dependency
            from alert.main import run_pipeline

            # Run the pipeline
            run_pipeline(test_mode=False)

            # Get statistics after run
            stats = self.db.get_stats()
            recent_run = self.db.get_recent_runs(limit=1)

            if recent_run:
                run = recent_run[0]
                summary = f"""✅ <b>크롤링 완료</b>

📥 수집: {run['total_fetched']} 건
🆕 신규: {run['new_count']} 건
🎯 관련: {run['relevant_count']} 건
📬 알림: {run['notified_count']} 건

총 공고 수: {stats['total']} 건"""
                self._send_message(summary)
            else:
                self._send_message("✅ 크롤링 완료")

        except Exception as e:
            self.logger.error(f"크롤링 실행 중 오류: {e}", exc_info=True)
            self._send_message(f"❌ 크롤링 실패: {e}")

    def cmd_recent(self, args: str) -> None:
        """Show 5 most recent announcements."""
        # Get recent announcements by searching with empty query
        # and sorting by created_at
        query_result = self.db._conn.execute(
            """
            SELECT * FROM announcements
            ORDER BY created_at DESC
            LIMIT 5
            """
        ).fetchall()

        if not query_result:
            self._send_message("📭 등록된 공고가 없습니다.")
            return

        announcements = [self.db._row_to_announcement(r) for r in query_result]

        lines = ["📰 <b>최근 공고 5건</b>\n"]

        for i, ann in enumerate(announcements, 1):
            score_pct = ann.relevance_score * 100
            lines.append(f"<b>{i}. {ann.title}</b>")
            lines.append(f"   🏛 {ann.author or '미상'}")
            lines.append(f"   📊 관련도: {score_pct:.0f}%")
            lines.append(f"   🔗 <a href=\"{ann.url}\">상세보기</a>")
            lines.append("")

        self._send_message("\n".join(lines))

    def cmd_app(self, args: str) -> None:
        """Show application status for an announcement."""
        if not args.strip():
            self._send_message("사용법: /app <공고 ID>")
            return

        try:
            ann_id = int(args.strip())
        except ValueError:
            self._send_message("올바른 공고 ID(숫자)를 입력해주세요.")
            return

        ann = self.db.get_announcement_by_id(ann_id)
        if not ann:
            self._send_message(f"공고 #{ann_id}를 찾을 수 없습니다.")
            return

        records = self.db.get_application_history(ann_id)

        msg = f"<b>📋 공고 #{ann_id}</b>\n"
        msg += f"<b>{ann.title}</b>\n\n"

        if records:
            for rec in records:
                status_emoji = {
                    "discovered": "🔍", "reviewing": "📖", "preparing": "📝",
                    "applied": "✅", "accepted": "🎉", "rejected": "❌",
                    "expired": "⏰", "not_applicable": "➖",
                }.get(rec.status, "❓")
                msg += f"{status_emoji} <b>{rec.status}</b>"
                if rec.applied_date:
                    msg += f" (신청일: {rec.applied_date})"
                if rec.notes:
                    msg += f"\n  메모: {rec.notes}"
                msg += "\n"
        else:
            msg += "신청 이력이 없습니다.\n"
            msg += "/apply 명령으로 신청 이력을 생성할 수 있습니다."

        self._send_message(msg)

    def cmd_apply(self, args: str) -> None:
        """Mark an announcement as applied."""
        if not args.strip():
            self._send_message("사용법: /apply <공고 ID>")
            return

        try:
            ann_id = int(args.strip())
        except ValueError:
            self._send_message("올바른 공고 ID(숫자)를 입력해주세요.")
            return

        from alert.models import ApplicationRecord
        from datetime import datetime

        ann = self.db.get_announcement_by_id(ann_id)
        if not ann:
            self._send_message(f"공고 #{ann_id}를 찾을 수 없습니다.")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        record = ApplicationRecord(
            announcement_id=ann_id,
            status="applied",
            applied_date=today,
        )
        rec_id = self.db.insert_application_record(record)
        self._send_message(f"✅ 공고 #{ann_id} 신청 완료로 등록했습니다.\n신청 이력 ID: {rec_id}\n신청일: {today}")

    def cmd_appstatus(self, args: str) -> None:
        """Update application status."""
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            self._send_message(
                "사용법: /appstatus <이력ID> <상태>\n\n"
                "상태 목록: discovered, reviewing, preparing, applied, "
                "accepted, rejected, expired, not_applicable"
            )
            return

        try:
            record_id = int(parts[0])
        except ValueError:
            self._send_message("올바른 이력 ID(숫자)를 입력해주세요.")
            return

        valid_statuses = {
            "discovered", "reviewing", "preparing", "applied",
            "accepted", "rejected", "expired", "not_applicable",
        }
        new_status = parts[1].strip().lower()
        if new_status not in valid_statuses:
            self._send_message(f"올바르지 않은 상태입니다.\n가능한 상태: {', '.join(sorted(valid_statuses))}")
            return

        updated = self.db.update_application_status(record_id, new_status)
        if updated:
            self._send_message(f"✅ 이력 #{record_id} 상태를 '{new_status}'로 변경했습니다.")
        else:
            self._send_message(f"이력 #{record_id}를 찾을 수 없습니다.")

    def cmd_apphistory(self, args: str) -> None:
        """Show recent application history, optionally filtered by domain."""
        domain = args.strip() if args.strip() else None

        if domain:
            records = self.db.get_applications_by_domain(domain, limit=10)
            header = f"<b>📊 신청 이력 (도메인: {domain})</b>\n\n"
        else:
            records = self.db.get_recent_applications(limit=10)
            header = "<b>📊 최근 신청 이력</b>\n\n"

        if not records:
            self._send_message(header + "신청 이력이 없습니다.")
            return

        msg = header
        for rec in records:
            ann = self.db.get_announcement_by_id(rec.announcement_id)
            title = ann.title[:40] if ann else f"공고 #{rec.announcement_id}"
            status_emoji = {
                "discovered": "🔍", "reviewing": "📖", "preparing": "📝",
                "applied": "✅", "accepted": "🎉", "rejected": "❌",
                "expired": "⏰", "not_applicable": "➖",
            }.get(rec.status, "❓")
            msg += f"{status_emoji} <b>{title}</b>\n"
            msg += f"  상태: {rec.status}"
            if rec.assigned_domain:
                msg += f" | 도메인: {rec.assigned_domain}"
            msg += "\n"

        self._send_message(msg)

    def cmd_submit(self, args: str) -> None:
        """Submit a URL as a manual tip.

        Args:
            args: "<URL> [description]"
        """
        if not args.strip():
            self._send_message(
                "사용법: /submit &lt;URL&gt; [설명]\n"
                "예시: /submit https://example.com 스마트팜 지원사업 공고"
            )
            return

        parts = args.strip().split(maxsplit=1)
        url = parts[0]
        description = parts[1] if len(parts) > 1 else ""

        # Validate URL
        if not url.startswith(("http://", "https://")):
            self._send_message("❌ 올바른 URL을 입력해주세요 (http:// 또는 https://로 시작)")
            return

        import hashlib

        source_id = hashlib.md5(url.encode()).hexdigest()[:16]

        # Check for duplicates
        if self.db.is_duplicate("manual", source_id):
            self._send_message("ℹ️ 이미 등록된 URL입니다.")
            return

        # Create AnalyzedAnnouncement object
        announcement = AnalyzedAnnouncement(
            source="manual",
            source_id=source_id,
            title=description or url,
            url=url,
            summary=description,
            author="수동입력",
            relevance_score=0.5,  # Default medium relevance
            relevance_reason="수동 제보",
        )

        # Insert into DB
        ann_id = self.db.insert_announcement(announcement)

        if ann_id:
            self._send_message(
                f"✅ 등록 완료: {description or url}\n"
                f"공고 ID: {ann_id}"
            )
        else:
            self._send_message("⚠️ 등록 중 오류가 발생했습니다.")
