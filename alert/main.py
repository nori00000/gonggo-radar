"""Main entry point for the agrion-automation alert system.

Pipeline: crawl → analyze → notify
Modes: single run, daemon, bot, test
"""

import argparse
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from .config import get_config
from .db import Database
from .analyzer import KeywordAnalyzer, ClaudeAnalyzer
from .models import RawAnnouncement, AnalyzedAnnouncement
from .notifiers.telegram_bot import TelegramNotifier
from .notifiers.email_sender import EmailNotifier
from .utils.logger import setup_logger


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
# Pipeline
# ---------------------------------------------------------------------------

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

    for crawler_name, CrawlerClass in available_crawlers.items():
        logger.info(f"\n--- Crawling: {crawler_name} ---")

        try:
            crawler = CrawlerClass()

            # Check if enabled
            if not crawler.is_enabled():
                logger.info(f"{crawler_name} is disabled in config, skipping")
                run_stats[crawler_name] = {
                    "total_fetched": 0,
                    "new_count": 0,
                    "relevant_count": 0,
                    "status": "disabled",
                }
                continue

            # Fetch announcements
            raw_announcements = crawler.safe_fetch()
            total_fetched = len(raw_announcements)

            logger.info(f"{crawler_name}: Fetched {total_fetched} announcements")

            # Filter out duplicates
            new_raw: List[RawAnnouncement] = []
            duplicate_count = 0

            for raw_ann in raw_announcements:
                if db.is_duplicate(raw_ann.source, raw_ann.source_id):
                    duplicate_count += 1
                    logger.debug(f"Duplicate: {raw_ann.title}")
                else:
                    new_raw.append(raw_ann)

            new_count = len(new_raw)
            logger.info(f"{crawler_name}: {new_count} new, {duplicate_count} duplicates")

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
            analyzed = keyword_analyzer.analyze_batch(new_raw)
            relevant_count = len(analyzed)

            logger.info(
                f"{crawler_name}: {relevant_count}/{new_count} passed keyword threshold "
                f"(>= {config.analyzer.keyword_threshold})"
            )

            # ---------------------------------------------------------------------------
            # Stage 3: Claude analysis (if available and configured)
            # ---------------------------------------------------------------------------

            if relevant_count > 0 and claude_analyzer.client:
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

    if not unnotified:
        logger.info("No new announcements to notify")
    else:
        logger.info(f"Found {len(unnotified)} unnotified announcements")

        # Send Telegram notifications
        if config.notifier.telegram.enabled:
            logger.info("Sending Telegram notifications...")
            if test_mode:
                # In test mode, only send first announcement
                test_batch = unnotified[:1]
                telegram.send_batch(test_batch)
            else:
                telegram.send_batch(unnotified)
        else:
            logger.info("Telegram notifications disabled")

        # Send Email digest
        if config.notifier.email.enabled:
            logger.info("Sending Email digest...")
            today = datetime.now().strftime("%Y-%m-%d")
            if test_mode:
                # In test mode, only send first announcement
                test_batch = unnotified[:1]
                email.send_digest(test_batch, today)
            else:
                email.send_digest(unnotified, today)
        else:
            logger.info("Email notifications disabled")

        # Mark all as notified
        if not test_mode:
            for ann in unnotified:
                db.mark_notified(ann.source_id)
            logger.info(f"Marked {len(unnotified)} announcements as notified")

    # ---------------------------------------------------------------------------
    # Stage 6: Record run history
    # ---------------------------------------------------------------------------

    for crawler_name, stats in run_stats.items():
        db.insert_run(
            source=crawler_name,
            total=stats["total_fetched"],
            new=stats["new_count"],
            relevant=stats["relevant_count"],
            notified=len(unnotified) if stats["status"] == "success" else 0,
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
    logger.info(f"Notifications sent: {len(unnotified)}")

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
        telegram.send_text("🧪 Test message from agrion-automation alert system")
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
  python -m alert.main --bot            # 텔레그램 봇 모드 (polling, Phase 5에서 구현)
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
