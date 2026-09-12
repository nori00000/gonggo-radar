"""Main entry point for the gonggo-radar alert system.

Pipeline: crawl → analyze → notify
Modes: single run, daemon, bot, test
"""

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from .config import get_config
from .db import Database
from .analyzer import KeywordAnalyzer, ClaudeAnalyzer
from .models import RawAnnouncement, AnalyzedAnnouncement
from .notifiers.telegram_bot import TelegramNotifier
from .notifiers.email_sender import EmailNotifier
from .utils.logger import setup_logger

try:
    from .crawlers.period_extractors import EVIDENCE_KEYS, PERIOD_EXTRACTORS
except Exception as _exc:              # noqa: BLE001
    # 추출기를 못 읽어도 파이프라인은 산다 - 그때는 **모든 소스가 기간
    # 없음**이 된다(fail-closed). 없는 마감을 말하는 것보다 낫다.
    logging.getLogger(__name__).error(
        f"period extractors unavailable ({_exc}) - 모든 기간을 비운다"
    )
    PERIOD_EXTRACTORS = {}
    EVIDENCE_KEYS = {}


# ---------------------------------------------------------------------------
# Crawler imports with graceful degradation
# ---------------------------------------------------------------------------

def _import_crawlers() -> Dict[str, Any]:
    """Import all available crawlers, skipping those that fail to import.

    Returns:
        Dict mapping crawler names to their classes
    """
    crawlers = {}
    logger = logging.getLogger(__name__)

    # Try to import each crawler
    crawler_modules = [
        ("bizinfo", "alert.crawlers.bizinfo", "BizinfoCrawler"),
        ("mafra", "alert.crawlers.mafra", "MafraCrawler"),
        ("smartfarm", "alert.crawlers.smartfarm", "SmartfarmCrawler"),
        ("goyang", "alert.crawlers.goyang", "GoyangCrawler"),
        ("nongsaro", "alert.crawlers.nongsaro", "NongsaroCrawler"),
        ("g2b", "alert.crawlers.g2b", "G2bCrawler"),
        ("kstartup", "alert.crawlers.kstartup", "KStartupCrawler"),
        ("smes", "alert.crawlers.smes", "SmesCrawler"),
        ("subsidy24", "alert.crawlers.subsidy24", "Subsidy24Crawler"),
        ("forest_service", "alert.crawlers.forest_service", "ForestServiceCrawler"),
        ("gyeonggi", "alert.crawlers.gyeonggi", "GyeonggiCrawler"),
        ("gafi", "alert.crawlers.gafi", "GafiCrawler"),
        ("gbsa", "alert.crawlers.gbsa", "GbsaCrawler"),
        ("socialenterprise", "alert.crawlers.socialenterprise", "SocialenterpriseCrawler"),
        ("ekr", "alert.crawlers.ekr", "EkrCrawler"),
        ("epis", "alert.crawlers.epis", "EpisCrawler"),
        ("nongup_gg", "alert.crawlers.nongup_gg", "NongupGgCrawler"),
        ("rda", "alert.crawlers.rda", "RdaCrawler"),
        ("semas", "alert.crawlers.semas", "SemasCrawler"),
        ("kosmes", "alert.crawlers.kosmes", "KosmesCrawler"),
        ("ipet", "alert.crawlers.ipet", "IpetCrawler"),
        ("apfs", "alert.crawlers.apfs", "ApfsCrawler"),
        ("agrohealing", "alert.crawlers.agrohealing", "AgrohealingCrawler"),
        ("fowi", "alert.crawlers.fowi", "FowiCrawler"),
        ("seis", "alert.crawlers.seis", "SeisCrawler"),
        ("ggeea", "alert.crawlers.ggeea", "GgeeaCrawler"),
        ("mois_sse", "alert.crawlers.mois_sse", "MoisSseCrawler"),
        ("goyang_startup", "alert.crawlers.goyang_startup", "GoyangStartupCrawler"),
        ("kofpi", "alert.crawlers.kofpi", "KofpiCrawler"),
        ("forest_press", "alert.crawlers.forest_press", "ForestPressCrawler"),
        ("lawmaking", "alert.crawlers.lawmaking", "LawmakingCrawler"),
        ("coop", "alert.crawlers.coop", "CoopCrawler"),
    ]

    for name, module_path, class_name in crawler_modules:
        try:
            module = __import__(module_path, fromlist=[class_name])
            crawler_class = getattr(module, class_name)
            crawlers[name] = crawler_class
            logger.info(f"Loaded crawler: {name}")
        except (ImportError, AttributeError) as e:
            logger.warning(f"Could not import {name} crawler: {e}")

    return crawlers


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def _crawl_single(
    crawler_name: str,
    CrawlerClass: Any,
    logger: logging.Logger,
    quoted_source_ids: Optional[set] = None,
) -> Tuple[str, list, str]:
    """단일 크롤러 실행 (스레드에서 호출).

    Args:
        crawler_name: 소스 이름
        CrawlerClass: 크롤러 클래스
        logger: 로거
        quoted_source_ids: 이미 상세 인용을 받은 공고의 source_id.
            상세 요청 예산을 이 항목에 쓰지 않는다 (Codex 재검토 #11)

    Returns:
        (crawler_name, raw_announcements, status)
        status: "success" | "disabled" | "error"
    """
    try:
        crawler = CrawlerClass()
        if not crawler.is_enabled():
            return crawler_name, [], "disabled"
        if quoted_source_ids:
            crawler.set_quoted_source_ids(quoted_source_ids)
        raw = crawler.safe_fetch()
        return crawler_name, raw, "success"
    except Exception as e:
        logger.error(f"{crawler_name}: {e}")
        return crawler_name, [], "error"


# ---------------------------------------------------------------------------
# 기간 관문 (13차) - period_start/period_end 는 **여기서만** 정해진다
# ---------------------------------------------------------------------------

def _finalize_periods(source: str, item: RawAnnouncement) -> RawAnnouncement:
    """DB 에 닿기 직전 기간 두 필드를 확정한다 - 저장 경로의 단일 관문.

    크롤러가 무엇을 반환했든(하위 클래스 override, ``fetch()`` 가 직접
    만든 객체, ``safe_fetch`` 경유 여부 무관) 이 함수가 두 필드를 **None
    으로 리셋**한 뒤, 전용 추출기를 가진 소스만 ``raw_data`` 원문에서
    다시 채운다.

    열두 차례의 게이트에서 관문을 크롤러 쪽(베이스 클래스)에 두면 계속
    우회됐다: 선언을 상속하거나 override 하거나, 근거 없이 만든 객체를
    그대로 반환하면 통과했다. 그래서 관문을 **저장 직전 한 곳**에 둔다.

    Args:
        source: 저장될 소스 이름 (``announcement.source``)
        item: 저장 대상 - **제자리에서** 고친다

    Returns:
        같은 객체 (호출 편의)
    """
    start, end = _periods_from_raw(source, item.raw_data)
    item.period_start = start
    item.period_end = end
    return item


def _periods_from_raw(
    source: str, raw_data: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """``raw_data`` 원문에서 기간을 산출한다 - **근거 산출의 단일 구현**.

    저장 직전 관문(``_finalize_periods``)과 기존 행 재검증
    (``Database.revalidate_periods``)이 같은 함수를 쓴다. 두 자리가 갈리면
    "재수집된 행" 과 "안 된 행" 의 기준이 달라진다.

    Args:
        source: 소스 이름
        raw_data: 크롤러가 남긴 JSON 문자열

    Returns:
        ``(period_start, period_end)`` - 근거가 없으면 ``(None, None)``
    """
    extractor = PERIOD_EXTRACTORS.get((source or "").strip())
    if extractor is None:
        return None, None              # 전용 추출기가 없는 소스 = 기간 없음

    try:
        raw = json.loads(raw_data or "{}")
    except (ValueError, TypeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None

    try:
        start, end = extractor(raw)
    except Exception as exc:           # noqa: BLE001
        logging.getLogger(__name__).error(
            f"{source}: 기간 추출 실패 ({exc}) - 기간 없이 둔다"
        )
        return None, None

    return start or None, end or None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

BYPASS_DEFAULT_SCORE = 0.5


def select_for_storage(
    keyword_analyzer: KeywordAnalyzer,
    raw_announcements: List[RawAnnouncement],
    source_cfg: Optional[Any],
) -> Tuple[List[AnalyzedAnnouncement], bool]:
    """키워드 분석 후 DB 저장 대상을 고른다.

    계약 v1.2: `bypass_threshold: true` 소스는 키워드 임계값과 무관하게 전량
    저장 대상이 된다. relevance_score는 계산값을 유지하되, 0이면
    BYPASS_DEFAULT_SCORE(0.5)로 채운다.

    Args:
        keyword_analyzer: 키워드 분석기
        raw_announcements: 중복 제거를 마친 신규 공고
        source_cfg: 해당 소스의 SourceConfig (없으면 None)

    Returns:
        (저장 대상 목록, bypass 적용 여부)
    """
    if source_cfg is not None and getattr(source_cfg, "bypass_threshold", False):
        analyzed = [keyword_analyzer.analyze(raw) for raw in raw_announcements]
        for ann in analyzed:
            if not ann.relevance_score:
                ann.relevance_score = BYPASS_DEFAULT_SCORE
        analyzed.sort(key=lambda a: a.relevance_score, reverse=True)
        return analyzed, True

    return keyword_analyzer.analyze_batch(raw_announcements), False


def run_pipeline(test_mode: bool = False) -> None:
    """Main pipeline: crawl → analyze → notify.

    Args:
        test_mode: If True, only runs BizinfoCrawler and sends test notifications
    """
    # Setup
    config = get_config()
    logger = setup_logger("main")

    logger.info("=" * 70)
    logger.info(f"Starting {config.name} v{config.version}")
    logger.info("=" * 70)

    # Initialize components
    db = Database()

    # 기존 오염 정규화 (멱등) - 허용목록 **여집합 전체**의 기간을 지운다.
    # 12차 게이트: 대상을 몇 개 소스로 좁혔더니 bizinfo 같은 소스의 오염이
    # 살아남아 알림까지 갔다.
    try:
        cleared = db.clear_periods_except(PERIOD_EXTRACTORS)
        logger.info(
            f"기간 정규화: {cleared} 행을 비웠다 "
            f"(추출기 있는 소스 {len(PERIOD_EXTRACTORS)}개 제외)"
        )
    except Exception as e:
        logger.error(f"기간 정규화 실패: {e}")

    # 추출기가 있는 소스도 **기존 행의 근거를 다시 본다**. 목록에서 내려간
    # 공고는 재수집되지 않아 관문을 다시 지나지 않는다 (11차 게이트).
    for extractor_source in PERIOD_EXTRACTORS:
        try:
            fixed = db.revalidate_periods(
                extractor_source,
                lambda raw, name=extractor_source: _periods_from_raw(name, raw),
            )
            logger.info(f"기간 재검증({extractor_source}): {fixed} 행을 고쳤다")
        except Exception as e:
            logger.error(f"기간 재검증 실패({extractor_source}): {e}")
    keyword_analyzer = KeywordAnalyzer(db=db)
    claude_analyzer = ClaudeAnalyzer()
    telegram = TelegramNotifier(db=db)
    email = EmailNotifier()

    # Load available crawlers
    available_crawlers = _import_crawlers()

    if not available_crawlers:
        logger.error("No crawlers available! Please implement at least one crawler.")
        return

    # Test mode: only run first available crawler
    if test_mode:
        logger.info("TEST MODE: Running first available crawler only")
        first_crawler_name = next(iter(available_crawlers))
        available_crawlers = {first_crawler_name: available_crawlers[first_crawler_name]}

    # ---------------------------------------------------------------------------
    # Stage 1: Crawl from all enabled sources
    # ---------------------------------------------------------------------------

    all_new_announcements: List[AnalyzedAnnouncement] = []
    run_stats: Dict[str, Dict[str, int]] = {}

    # ---------------------------------------------------------------------------
    # Stage 1: Crawl from all enabled sources (parallel)
    # ---------------------------------------------------------------------------

    logger.info(f"Starting parallel crawl with max_workers=5 for {len(available_crawlers)} crawlers")

    crawl_results: Dict[str, Any] = {}

    # 이미 상세 인용을 받은 공고는 상세 요청 예산을 쓰지 않는다.
    # 이걸 넘겨 주지 않으면 목록이 요청 상한보다 긴 소스에서 뒤쪽 항목이
    # 매 실행 영구히 미수집으로 남는다 (Codex 재검토 #11).
    quoted_ids: Dict[str, set] = {}
    for name in available_crawlers:
        try:
            quoted_ids[name] = db.get_quoted_source_ids(name)
        except Exception as e:
            logger.debug(f"{name}: could not load quoted source ids: {e}")
            quoted_ids[name] = set()

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(
                _crawl_single, name, cls, logger, quoted_ids.get(name)
            ): name
            for name, cls in available_crawlers.items()
        }
        for future in as_completed(futures):
            crawler_name, raw_announcements, status = future.result()
            crawl_results[crawler_name] = (raw_announcements, status)
            logger.info(f"\n--- Crawled: {crawler_name} ({status}, {len(raw_announcements)} items) ---")

    # Process crawl results sequentially (analysis/DB are not thread-safe)
    for crawler_name, (raw_announcements, status) in crawl_results.items():
        logger.info(f"\n--- Processing: {crawler_name} ---")

        if status == "disabled":
            logger.info(f"{crawler_name} is disabled in config, skipping")
            run_stats[crawler_name] = {
                "total_fetched": 0,
                "new_count": 0,
                "relevant_count": 0,
                "status": "disabled",
            }
            continue

        if status == "error":
            run_stats[crawler_name] = {
                "total_fetched": 0,
                "new_count": 0,
                "relevant_count": 0,
                "status": "error",
                "error_message": "crawl failed (see log above)",
            }
            continue

        try:
            total_fetched = len(raw_announcements)
            logger.info(f"{crawler_name}: Fetched {total_fetched} announcements")

            # Filter out duplicates
            new_raw: List[RawAnnouncement] = []
            duplicate_count = 0
            quote_merge_count = 0
            period_reset_count = 0

            for raw_ann in raw_announcements:
                # ── 기간 관문 ─────────────────────────────────────────
                # 기간 두 필드는 이 호출 뒤로만 존재한다. 신규 저장은 이
                # 객체의 복사본을 쓰고(``KeywordAnalyzer.analyze`` 가
                # ``__dict__`` 를 그대로 복사), 기존 행은 아래
                # ``overwrite_periods`` 가 같은 값으로 덮어쓴다. 그래서
                # DB 에 닿는 두 경로가 모두 이 한 호출을 지난다.
                _finalize_periods(raw_ann.source, raw_ann)

                # 같은 URL 이면 같은 공고다 - source_id 체계가 바뀐 예전
                # 행도 새 행으로 갈라지지 않는다 (11차 게이트)
                if db.exists(raw_ann):
                    duplicate_count += 1
                    # 기존 행의 기간도 **재수집 값으로 덮어쓴다** (None
                    # 포함) - 예전 실행이 심은 가짜 마감을 재수집이 지운다.
                    try:
                        if db.overwrite_periods(
                            raw_ann, EVIDENCE_KEYS.get(raw_ann.source, ())
                        ):
                            period_reset_count += 1
                    except Exception as e:
                        logger.debug(
                            f"period sync failed for {raw_ann.source_id}: {e}"
                        )
                    # 중복이라도 **새 인용은 살린다**. 상세 인용은 요청 상한
                    # 때문에 다음 실행에서 도착하기도 하는데, 그때 중복
                    # 필터가 버리면 인용이 영구히 사라진다 (최종 게이트 #6).
                    try:
                        if db.merge_quote_fields(raw_ann):
                            quote_merge_count += 1
                    except Exception as e:
                        logger.debug(f"quote merge failed for {raw_ann.source_id}: {e}")
                    logger.debug(f"Duplicate: {raw_ann.title}")
                else:
                    new_raw.append(raw_ann)

            new_count = len(new_raw)
            logger.info(
                f"{crawler_name}: {new_count} new, {duplicate_count} duplicates"
                f"{f', {quote_merge_count} quote merges' if quote_merge_count else ''}"
                f"{f', {period_reset_count} period resets' if period_reset_count else ''}"
            )

            if new_count == 0:
                run_stats[crawler_name] = {
                    "total_fetched": total_fetched,
                    "new_count": 0,
                    "relevant_count": 0,
                    "status": "success",
                }
                continue

            # ---------------------------------------------------------------------------
            # Stage 2: Keyword analysis
            # ---------------------------------------------------------------------------

            logger.info(f"{crawler_name}: Running keyword analysis on {new_count} announcements")

            source_cfg = config.crawler.sources.get(crawler_name)
            analyzed, bypassed = select_for_storage(
                keyword_analyzer, new_raw, source_cfg
            )
            relevant_count = len(analyzed)

            if bypassed:
                logger.info(
                    f"{crawler_name}: bypass_threshold=true -> {relevant_count}/{new_count} "
                    f"kept (keyword threshold ignored)"
                )
            else:
                logger.info(
                    f"{crawler_name}: {relevant_count}/{new_count} passed keyword threshold "
                    f"(>= {config.analyzer.keyword_threshold})"
                )

            # ---------------------------------------------------------------------------
            # Stage 3: LLM analysis (Claude API or Ollama fallback, if available)
            # ---------------------------------------------------------------------------

            llm_available = claude_analyzer.client or claude_analyzer.backend == "ollama"
            if relevant_count > 0 and llm_available:
                logger.info(f"{crawler_name}: Running Claude analysis on {relevant_count} announcements")
                analyzed = claude_analyzer.analyze_batch(analyzed)

                # Re-filter by Claude threshold
                claude_relevant = [
                    ann for ann in analyzed
                    if ann.relevance_score >= config.analyzer.claude_threshold
                ]
                logger.info(
                    f"{crawler_name}: {len(claude_relevant)}/{relevant_count} passed Claude threshold "
                    f"(>= {config.analyzer.claude_threshold})"
                )
                analyzed = claude_relevant
                relevant_count = len(analyzed)

            # ---------------------------------------------------------------------------
            # Stage 4: Save to database
            # ---------------------------------------------------------------------------

            for ann in analyzed:
                row_id = db.insert_announcement(ann)
                if row_id:  # int is truthy, None is falsy
                    ann.id = row_id
                    all_new_announcements.append(ann)
                    logger.debug(f"Saved new announcement: {ann.title} (id={row_id}, score={ann.relevance_score:.2f})")

            # Record run statistics
            run_stats[crawler_name] = {
                "total_fetched": total_fetched,
                "new_count": new_count,
                "relevant_count": relevant_count,
                "status": "success",
            }

            logger.info(
                f"{crawler_name}: Complete - {total_fetched} fetched, {new_count} new, "
                f"{relevant_count} relevant"
            )

        except Exception as e:
            logger.error(f"Error processing {crawler_name}: {e}", exc_info=True)
            run_stats[crawler_name] = {
                "total_fetched": 0,
                "new_count": 0,
                "relevant_count": 0,
                "status": "error",
                "error_message": str(e),
            }

    # ---------------------------------------------------------------------------
    # Stage 4.5: Knowledge Layer Processing (NEW)
    # ---------------------------------------------------------------------------

    if config.knowledge.enabled and all_new_announcements:
        logger.info(f"\n--- Knowledge Layer Processing ({len(all_new_announcements)} announcements) ---")
        try:
            from .knowledge import KnowledgeLayer
            knowledge = KnowledgeLayer(db, config)
            knowledge.process_batch(all_new_announcements)
        except Exception as e:
            logger.error(f"Knowledge layer error (non-fatal): {e}", exc_info=True)

    # ---------------------------------------------------------------------------
    # Stage 5: Notification
    # ---------------------------------------------------------------------------

    logger.info("\n--- Notification Stage ---")

    # Get unnotified announcements from database
    unnotified = db.get_unnotified()

    notified_count = 0

    if not unnotified:
        logger.info("No new announcements to notify")
    else:
        logger.info(f"Found {len(unnotified)} unnotified announcements")
        batch = unnotified[:1] if test_mode else unnotified

        # Send Telegram notifications
        telegram_ok = True
        if config.notifier.telegram.enabled:
            if telegram.bot_token and telegram.chat_id:
                logger.info("Sending Telegram notifications...")
                telegram_sent = telegram.send_batch(batch)
                telegram_ok = telegram_sent == len(batch)
            else:
                logger.info("Telegram not configured (missing token/chat_id), skipping")
        else:
            logger.info("Telegram notifications disabled")

        # Send Email digest
        email_ok = True
        if config.notifier.email.enabled:
            if email.sender and email.password:
                logger.info("Sending Email digest...")
                today = datetime.now().strftime("%Y-%m-%d")
                email_ok = email.send_digest(batch, today)
            else:
                logger.info("Email not configured (missing credentials), skipping")
        else:
            logger.info("Email notifications disabled")

        # Mark as notified only when every enabled channel actually delivered.
        # Previously this ran unconditionally, so a Telegram failure (e.g. a
        # revoked bot token returning 401) still marked announcements as
        # notified and reported them as "sent" -- silently losing them with
        # no retry and no visible signal that delivery had stopped working.
        if not test_mode:
            if telegram_ok and email_ok:
                for ann in unnotified:
                    db.mark_notified(ann.source_id)
                notified_count = len(unnotified)
                logger.info(f"Marked {notified_count} announcements as notified")
            else:
                logger.error(
                    f"Notification delivery failed (telegram_ok={telegram_ok}, "
                    f"email_ok={email_ok}) - {len(unnotified)} announcements left "
                    "unnotified for retry on next run"
                )

    # ---------------------------------------------------------------------------
    # Stage 6: Record run history
    # ---------------------------------------------------------------------------

    for crawler_name, stats in run_stats.items():
        db.insert_run(
            source=crawler_name,
            total=stats["total_fetched"],
            new=stats["new_count"],
            relevant=stats["relevant_count"],
            notified=notified_count if stats["status"] == "success" else 0,
            status=stats["status"],
            error_msg=stats.get("error_message", ""),
        )

    # ---------------------------------------------------------------------------
    # Stage 7: Summary
    # ---------------------------------------------------------------------------

    logger.info("\n" + "=" * 70)
    logger.info("PIPELINE SUMMARY")
    logger.info("=" * 70)

    total_fetched = sum(s["total_fetched"] for s in run_stats.values())
    total_new = sum(s["new_count"] for s in run_stats.values())
    total_relevant = sum(s["relevant_count"] for s in run_stats.values())

    logger.info(f"Total announcements fetched: {total_fetched}")
    logger.info(f"Total new announcements: {total_new}")
    logger.info(f"Total relevant announcements: {total_relevant}")
    logger.info(f"Notifications sent: {notified_count}")

    logger.info("\nPer-source breakdown:")
    for crawler_name, stats in run_stats.items():
        status = stats["status"]
        logger.info(
            f"  {crawler_name:15s}: fetched={stats['total_fetched']:3d}, "
            f"new={stats['new_count']:3d}, relevant={stats['relevant_count']:3d}, "
            f"status={status}"
        )

    logger.info("=" * 70)

    # Cleanup
    keyword_analyzer.close()
    db.close()


# ---------------------------------------------------------------------------
# Daemon mode
# ---------------------------------------------------------------------------

def run_daemon() -> None:
    """Daemon mode: runs pipeline on schedule (cron_hours)."""
    config = get_config()
    logger = setup_logger("daemon")

    logger.info("Starting DAEMON mode")
    logger.info(f"Schedule: Run at hours {config.schedule.cron_hours}")
    logger.info(f"Timezone: {config.schedule.timezone}")

    last_run_hour: Optional[int] = None

    try:
        while True:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(config.schedule.get("timezone", "Asia/Seoul"))
                current_hour = datetime.now(tz).hour
            except Exception:
                current_hour = datetime.now().hour

            # Check if we should run now
            if current_hour in config.schedule.cron_hours:
                # Avoid running multiple times in the same hour
                if last_run_hour != current_hour:
                    logger.info(f"\n{'='*70}")
                    logger.info(f"Scheduled run triggered at {datetime.now().isoformat()}")
                    logger.info(f"{'='*70}\n")

                    try:
                        run_pipeline()
                        last_run_hour = current_hour
                    except Exception as e:
                        logger.error(f"Pipeline error: {e}", exc_info=True)

            # Sleep for 60 seconds before next check
            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("\nDaemon stopped by user")


# ---------------------------------------------------------------------------
# Bot mode (placeholder)
# ---------------------------------------------------------------------------

def run_bot() -> None:
    """Bot mode: Telegram polling with command handlers."""
    logger = setup_logger("bot")
    logger.info("Starting BOT MODE - Telegram polling")

    try:
        from .notifiers.telegram_bot import TelegramBot
        bot = TelegramBot()
        bot.start_polling()
    except ValueError as e:
        logger.error(f"Bot configuration error: {e}")
        logger.error("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables")
    except Exception as e:
        logger.error(f"Bot error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Test mode
# ---------------------------------------------------------------------------

def run_test() -> None:
    """Test mode: runs minimal pipeline with test notifications."""
    logger = setup_logger("test")
    logger.info("Running in TEST mode")
    logger.info("- Will only crawl from first available crawler")
    logger.info("- Will send 1 test notification")

    run_pipeline(test_mode=True)

    # Send test telegram message
    db = Database()
    telegram = TelegramNotifier(db=db)
    if telegram.bot_token and telegram.chat_id:
        logger.info("Sending test Telegram message...")
        telegram.send_text("🧪 Test message from gonggo-radar alert system")
    db.close()

    # Send test email
    email = EmailNotifier()
    if email.sender and email.password and email.recipients:
        logger.info("Sending test email...")
        email.send_test()

    logger.info("Test complete!")


# ---------------------------------------------------------------------------
# Research mode
# ---------------------------------------------------------------------------

def run_research() -> None:
    """Generate a quarterly research document."""
    config = get_config()
    logger = setup_logger("research")

    if not config.knowledge.enabled:
        logger.error("Knowledge layer is disabled in config")
        return

    logger.info("Generating quarterly research document...")

    db = Database()
    try:
        from .knowledge import KnowledgeLayer
        kl = KnowledgeLayer(db, config)

        # Calculate current quarter boundaries
        now = datetime.now()
        quarter_start_month = ((now.month - 1) // 3) * 3 + 1
        period_start = f"{now.year}-{quarter_start_month:02d}-01"
        period_end = now.strftime("%Y-%m-%d") + "T23:59:59"

        doc_id = kl.generate_research(period_start, period_end)
        if doc_id:
            logger.info(f"Research document generated successfully (id={doc_id})")
        else:
            logger.info("No research document generated (no data for period)")
    except Exception as e:
        logger.error(f"Research generation error: {e}", exc_info=True)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Main entry point with CLI argument parsing."""
    parser = argparse.ArgumentParser(
        description="Agrion Automation Alert System - 농업공고 자동수집 및 알림",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m alert.main                  # 1회 실행 (crawl + analyze + notify)
  python -m alert.main --daemon         # 데몬 모드 (cron_hours에 맞춰 자동 실행)
  python -m alert.main --bot            # 텔레그램 봇 모드 (polling, 13개 커맨드 지원)
  python -m alert.main --test           # 테스트 모드 (크롤 1개만, 알림 테스트)
  python -m alert.main --sync-keywords  # config.yaml → DB 키워드 동기화 (추가만)
        """
    )

    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in daemon mode (scheduled execution)"
    )

    parser.add_argument(
        "--bot",
        action="store_true",
        help="Run in bot mode (Telegram polling with command handlers)"
    )

    parser.add_argument(
        "--test",
        action="store_true",
        help="Run in test mode (minimal pipeline + test notifications)"
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override log level from config"
    )

    parser.add_argument(
        "--research",
        action="store_true",
        help="Generate research document for the current quarter"
    )

    parser.add_argument(
        "--sync-keywords",
        action="store_true",
        help="Sync keywords from config.yaml to database (adds new, does not remove existing)"
    )

    args = parser.parse_args()

    # Override log level if specified
    if args.log_level:
        config = get_config()
        config.log_level = args.log_level

    # Determine mode
    if args.sync_keywords:
        db = Database()
        count = db.sync_keywords_from_config()
        print(f"Synced {count} new keywords to database")
        db.close()
        return
    elif args.research:
        run_research()
    elif args.daemon:
        run_daemon()
    elif args.bot:
        run_bot()
    elif args.test:
        run_test()
    else:
        # Single run mode
        run_pipeline()


if __name__ == "__main__":
    main()
