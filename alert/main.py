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
from .council import CouncilDrop, score_item
from .db import Database
from .analyzer import KeywordAnalyzer, ClaudeAnalyzer
from .models import (
    SOURCE_KIND_DEFAULT,
    SOURCE_KIND_MEDIA,
    SOURCE_KINDS,
    RawAnnouncement,
    AnalyzedAnnouncement,
)
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

try:
    from .crawlers.period_detail import (
        MAX_PERIOD_DETAIL_REQUESTS,
        attach_detail_text,
        wants_period_detail,
    )
    from .utils.http_fetch import FetchBudget
except Exception as _detail_exc:       # noqa: BLE001
    # 상세 근거를 못 받아도 파이프라인은 산다 - 그때는 상세가 필요한 소스가
    # **기간 없음**이 된다(fail-closed). 없는 마감을 말하는 것보다 낫다.
    logging.getLogger(__name__).error(
        "period detail unavailable (%s) - 상세 근거 없이 간다", _detail_exc
    )
    MAX_PERIOD_DETAIL_REQUESTS = 0
    FetchBudget = None                 # type: ignore[assignment]

    def wants_period_detail(source: object) -> bool:   # type: ignore[misc]
        return False

    def attach_detail_text(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False


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
        # P2-S 계약 §3 신규 HTML 소스 (기간 추출기 없음)
        ("moel", "alert.crawlers.moel", "MoelCrawler"),
        ("mss", "alert.crawlers.mss", "MssCrawler"),
        # 2차 미디어 RSS (P1-R 계약, kind: media - 월간호 전용)
        ("lifein", "alert.crawlers.media_rss", "LifeinCrawler"),
        ("eroun", "alert.crawlers.media_rss", "ErounCrawler"),
        ("senews", "alert.crawlers.media_rss", "SenewsCrawler"),
        ("kfnews", "alert.crawlers.media_rss", "KfnewsCrawler"),
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


def resolve_source_kind(crawler_cls: Any, source_cfg: Optional[Any]) -> str:
    """소스 종류를 정한다 — **정본은 크롤러 클래스**다 (라운드 2 HIGH).

    설정(``config.yaml`` 의 ``kind:``)은 확인만 한다. 소스 블록이 통째로
    빠지거나 ``kind`` 를 안 적어도 종류는 변하지 않는다: 설정 한 줄이 없다고
    2차 보도가 회사 알림·주간호로 새면, 그 사고는 조용하고 되돌릴 수 없다
    (이미 보낸 뒤에 안다).

    설정이 크롤러와 **다른** 값을 주장하면 오류로 남기고 크롤러를 따른다.

    Args:
        crawler_cls: 크롤러 클래스 (또는 ``KIND`` 속성을 가진 객체). None 이면 기본값.
        source_cfg: :class:`alert.config.SourceConfig` 또는 None.

    Returns:
        ``"gonggo"`` 또는 ``"media"``.
    """
    logger = logging.getLogger(__name__)
    declared = getattr(crawler_cls, "KIND", None) or SOURCE_KIND_DEFAULT
    if declared not in SOURCE_KINDS:
        logger.error(
            f"크롤러 {crawler_cls!r} 의 KIND={declared!r} 를 모른다 - media 로 본다"
        )
        # 모르는 값은 **회사 경로 밖**으로 보낸다. 잘못 실으면 알림이 새지만,
        # 잘못 빼면 월간호 후보 하나가 빈다 - 후자가 싸다 (fail-closed).
        return SOURCE_KIND_MEDIA

    configured = getattr(source_cfg, "kind", None)
    if configured is not None and configured != declared:
        logger.error(
            f"config.yaml 의 kind={configured!r} 가 크롤러 선언 {declared!r} 과"
            f" 다르다 - 크롤러를 따른다"
        )
    return declared


def select_for_storage(
    keyword_analyzer: KeywordAnalyzer,
    raw_announcements: List[RawAnnouncement],
    source_cfg: Optional[Any],
    source_kind: str = SOURCE_KIND_DEFAULT,
) -> Tuple[List[AnalyzedAnnouncement], bool]:
    """키워드 분석 후 DB 저장 대상을 고른다.

    계약 v1.2: `bypass_threshold: true` 소스는 키워드 임계값과 무관하게 전량
    저장 대상이 된다. relevance_score는 계산값을 유지하되, 0이면
    BYPASS_DEFAULT_SCORE(0.5)로 채운다.

    Args:
        keyword_analyzer: 키워드 분석기
        raw_announcements: 중복 제거를 마친 신규 공고
        source_cfg: 해당 소스의 SourceConfig (없으면 None)
        source_kind: resolve_source_kind 가 정한 소스 종류

    P1-R 계약: `media` 소스는 **회사 경로에 한 건도 들어가지 않는다**.
    제목에 회사 must_match 어휘("사회적기업" 등)가 있어도 마찬가지다 - 2차
    미디어 보도는 회사가 신청할 공고가 아니다. 적재 여부는 협의회 프로파일이
    단독으로 정한다(매치 -> council_only=1, 미매치 -> 탈락 원장). 회사 알림
    무영향이 어휘가 아니라 **경로 분리**로 지켜지는 자리다.

    Returns:
        (저장 대상 목록, bypass 적용 여부)
    """
    if source_kind == SOURCE_KIND_MEDIA:
        return [], False

    if source_cfg is not None and getattr(source_cfg, "bypass_threshold", False):
        analyzed = [keyword_analyzer.analyze(raw) for raw in raw_announcements]
        for ann in analyzed:
            if not ann.relevance_score:
                ann.relevance_score = BYPASS_DEFAULT_SCORE
        analyzed.sort(key=lambda a: a.relevance_score, reverse=True)
        return analyzed, True

    return keyword_analyzer.analyze_batch(raw_announcements), False


def _posted_from_raw(raw_data: Optional[str]) -> str:
    """``raw_data`` 의 ``posted`` (게시일)를 꺼낸다. 없으면 빈 문자열.

    9개 크롤러가 이 키를 남긴다 (``grep -l '"posted"' alert/crawlers/*.py``).
    관찰 원장의 표시용일 뿐 기간 관문(``_finalize_periods``)과 무관하다.
    """
    try:
        raw = json.loads(raw_data or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("posted") or "")


def apply_council_profile(
    profile: Any,
    source: str,
    raw_items: List[RawAnnouncement],
    selected: List[AnalyzedAnnouncement],
    analyze: Any,
    source_kind: str = SOURCE_KIND_DEFAULT,
) -> Tuple[List[AnalyzedAnnouncement], List[CouncilDrop]]:
    """협의회 프로파일을 적용한다 - **회사 선택 결과는 건드리지 않는다**.

    세 가지 일만 한다:

    1. 회사 경로가 이미 고른 항목(``selected``)에는 측정값(council_score·
       council_tags·council_match)만 붙인다. ``relevance_score``·
       ``relevance_reason``·``matched_keywords`` 는 읽지도 쓰지도 않는다.
    2. 회사 경로가 버렸지만 ``council_match=1`` 인 항목은 **별도 목록**으로
       돌려준다. 이 항목의 회사 점수는 계산값(임계 미달) 그대로 두고
       ``council_only=1`` 을 세워 알림·브리핑 쿼리가 집지 않게 한다.
    3. 회사·협의회 **둘 다** 탈락한 협의회 소스 항목은 관찰 원장 레코드
       (:class:`alert.council.CouncilDrop`)로 돌려준다. 저장되지 않는 항목은
       표본에 나타날 수 없어 오탈락이 조용해지기 때문이다 (Codex 게이트 2R).

    계약 §A 불변 조건 1이 여기서 갈린다: 이 함수는 ``selected`` 의 원소를
    **더하거나 빼지 않는다**.

    Args:
        profile: :class:`alert.config.CouncilProfileConfig`.
        source: 소스 이름.
        raw_items: 중복 제거를 마친 신규 원본 항목 전량.
        selected: 회사 경로(:func:`select_for_storage`)가 고른 저장 대상.
        analyze: 회사 키워드 분석 함수 (``KeywordAnalyzer.analyze``).
        source_kind: resolve_source_kind 가 정한 소스 종류. ``media`` 는
            프로파일이 꺼져 있어도 **관찰 원장을 남긴다** (라운드 2):
            미디어 항목은 회사 행이 될 수 없으므로, 프로파일 밖이면
            어디에도 기록되지 않고 사라진다 - 그 침묵이 관찰 모드의 구멍이다.

    Returns:
        ``(협의회 단독 저장 대상, 양쪽 탈락 관찰 레코드, 양쪽 탈락 항목)``.

        셋째 값은 둘째 값과 **같은 항목**을 분석 객체로 담은 것이다. 신규
        항목에는 쓸 일이 없지만(저장하지 않는다), 재평가 항목에는 쓴다 -
        이미 저장된 행의 측정값을 새 판정으로 갱신해야 하기 때문이다
        (Codex 게이트 4R MEDIUM).
    """
    in_profile = bool(
        profile
        and getattr(profile, "sources", None)
        and source in profile.sources
    )
    if not in_profile and source_kind != SOURCE_KIND_MEDIA:
        # 협의회 소스가 아니면 측정도 탈락 기록도 하지 않는다.
        return [], [], []
    # 프로파일 밖의 media 는 아래 루프를 그대로 탄다: score_item 이
    # "협의회 소스 아님"(match=0)을 돌려주므로 전량이 탈락 원장으로 간다.

    selected_by_id = {ann.source_id: ann for ann in selected}
    extras: List[AnalyzedAnnouncement] = []
    drops: List[CouncilDrop] = []
    unmatched: List[AnalyzedAnnouncement] = []

    for raw in raw_items:
        verdict = score_item(
            profile, source, raw.title, raw.summary, raw.target, raw.category
        )
        chosen = selected_by_id.get(raw.source_id)
        if chosen is not None:
            chosen.council_score = verdict.score
            chosen.council_tags = verdict.tags_json()
            chosen.council_match = verdict.match
            chosen.council_only = 0
            continue

        analyzed = analyze(raw)
        analyzed.council_score = verdict.score
        analyzed.council_tags = verdict.tags_json()
        analyzed.council_match = verdict.match
        analyzed.council_only = 1

        if not verdict.match:
            drops.append(CouncilDrop(
                source=raw.source,
                source_id=raw.source_id,
                title=raw.title,
                url=raw.url,
                posted_at=_posted_from_raw(raw.raw_data),
                company_score=analyzed.relevance_score,
                council_score=verdict.score,
                reason=verdict.reason,
            ))
            unmatched.append(analyzed)
            continue

        extras.append(analyzed)

    return extras, drops, unmatched


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

    # 옛 규칙으로 저장된 행은 **표시만** 한다 (멱등, 삭제 없음).
    # 새 식별자로 재수집되면 그 새 행이 정본이 되고, 옛 행은 기간·알림에서
    # 빠진 채 조용히 남는다 - 추측 이관이 남의 기간을 덮어쓰는 것보다 안전하다.
    try:
        marked = db.mark_legacy_rows(EVIDENCE_KEYS)
        logger.info(f"레거시 식별자 행: {marked} 건 표시 (기간·알림 제외)")
    except Exception as e:
        logger.error(f"레거시 표시 실패: {e}")

    # 협의회 탈락 관찰 원장 보관 기간 적용 (멱등, 실행당 1회)
    try:
        pruned = db.prune_council_drops()
        logger.info(f"협의회 탈락 원장: {pruned} 행 정리 (보관 기간 경과)")
    except Exception as e:
        logger.error(f"탈락 원장 정리 실패 (비치명): {e}")

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
            # 협의회 단독으로 저장된 행은 중복이어도 다시 본다 (승격 경로).
            recheck_raw: List[RawAnnouncement] = []
            duplicate_count = 0
            quote_merge_count = 0
            period_reset_count = 0

            # ── 상세 근거 수집 ────────────────────────────────────
            # 목록에 마감이 없는 소스만, 항목당 **한 번**, 예산 안에서 받는다.
            # 기간은 여기서 만들지 않는다 - 아래 관문이 순수 추출기로 정한다.
            detail_budget = (
                FetchBudget()
                if FetchBudget is not None and wants_period_detail(crawler_name)
                else None
            )
            detail_quota = MAX_PERIOD_DETAIL_REQUESTS
            detail_hits = 0

            for raw_ann in raw_announcements:
                if detail_budget is not None and detail_quota > 0:
                    detail_quota -= 1
                    try:
                        if attach_detail_text(raw_ann, budget=detail_budget):
                            detail_hits += 1
                    except Exception as detail_error:      # noqa: BLE001
                        logger.debug(
                            f"detail evidence failed for "
                            f"{raw_ann.source_id}: {detail_error}"
                        )

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

                    # 협의회 단독 행은 **여기서 끝내지 않는다** (계약 §A
                    # 라운드 3 / Codex HIGH). ``exists`` 와 저장 키가 같은
                    # ``(source, source_id)`` 라서, 같은 URL 로 다시 온 항목을
                    # 여기서 버리면 회사 어휘가 나중에 맞아도 승격될 기회가
                    # 영원히 없다. 재평가 목록으로 넘겨 회사 분석을 다시
                    # 지나게 한다.
                    try:
                        if db.needs_council_recheck(raw_ann):
                            recheck_raw.append(raw_ann)
                    except Exception as e:
                        logger.debug(
                            f"council recheck lookup failed for {raw_ann.source_id}: {e}"
                        )
                    logger.debug(f"Duplicate: {raw_ann.title}")
                else:
                    new_raw.append(raw_ann)

            new_count = len(new_raw)
            logger.info(
                f"{crawler_name}: {new_count} new, {duplicate_count} duplicates"
                f"{f', {quote_merge_count} quote merges' if quote_merge_count else ''}"
                f"{f', {period_reset_count} period resets' if period_reset_count else ''}"
                f"{f', {detail_hits} detail evidence' if detail_hits else ''}"
            )

            if new_count == 0 and not recheck_raw:
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
            # 종류의 정본은 크롤러 클래스다 - 설정이 빠져도 media 는 media.
            source_kind = resolve_source_kind(
                available_crawlers.get(crawler_name), source_cfg
            )

            # 신규와 재평가를 **한 목록으로** 고른다 (Codex 게이트 4R HIGH).
            # 선택 경로가 둘이면 한쪽만 LLM 관문을 지나는 일이 생긴다 - 실제로
            # 라운드 3 의 재평가분은 키워드 임계만 넘고 Stage 3 를 건너뛰었다.
            # 통계에서만 뒤에서 갈라낸다.
            recheck_keys = {(r.source, r.source_id) for r in recheck_raw}
            selected_all, bypassed = select_for_storage(
                keyword_analyzer, new_raw + recheck_raw, source_cfg, source_kind
            )

            def _is_recheck(ann: AnalyzedAnnouncement) -> bool:
                return (ann.source, ann.source_id) in recheck_keys

            analyzed = [ann for ann in selected_all if not _is_recheck(ann)]
            relevant_count = len(analyzed)
            if recheck_raw:
                logger.info(
                    f"{crawler_name}: 협의회 단독 {len(recheck_raw)}건을 "
                    f"신규 {new_count}건과 같은 선택 경로로 보낸다"
                )

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
            llm_ran = False
            if selected_all and llm_available:
                llm_ran = True
                logger.info(
                    f"{crawler_name}: Running Claude analysis on "
                    f"{len(selected_all)} announcements"
                )
                selected_all = claude_analyzer.analyze_batch(selected_all)

                # Re-filter by Claude threshold - 재평가분도 **같은 관문**을 탄다.
                claude_relevant = [
                    ann for ann in selected_all
                    if ann.relevance_score >= config.analyzer.claude_threshold
                ]
                logger.info(
                    f"{crawler_name}: {len(claude_relevant)}/{len(selected_all)} "
                    f"passed Claude threshold (>= {config.analyzer.claude_threshold})"
                )
                selected_all = claude_relevant
                analyzed = [ann for ann in selected_all if not _is_recheck(ann)]
                relevant_count = len(analyzed)

            # 관문을 다 지난 뒤에야 재평가분을 갈라낸다.
            recheck_selected = [ann for ann in selected_all if _is_recheck(ann)]

            # LLM 관문이 켜져 있는데 **이 항목을 보지 못했다면**(호출 상한 초과·
            # 백엔드 오류·타임아웃) 승격시키지 않는다. 남아 있는 점수는 키워드
            # 점수일 뿐 LLM 관문을 통과한 근거가 아니다 (계약 §A 라운드 5).
            # 신규 항목은 종전 동작 그대로 둔다 - 회사 경로는 바뀌지 않는다.
            recheck_deferred: List[AnalyzedAnnouncement] = []
            if llm_ran:
                recheck_deferred = [
                    ann for ann in recheck_selected if not ann.llm_evaluated
                ]
                recheck_selected = [
                    ann for ann in recheck_selected if ann.llm_evaluated
                ]
            if recheck_deferred:
                logger.info(
                    f"{crawler_name}: 재평가 {len(recheck_deferred)}건은 LLM 이 "
                    f"보지 못해 승격을 미룬다 (다음 실행에서 다시 본다)"
                )
            if recheck_raw:
                logger.info(
                    f"{crawler_name}: 재평가 {len(recheck_raw)}건 중 "
                    f"회사 통과 {len(recheck_selected)}건"
                )

            # ---------------------------------------------------------------------------
            # Stage 4: Save to database
            # ---------------------------------------------------------------------------

            # ── 협의회 적재 프로파일 (관찰 모드, 계약 §A) ────────────
            # 회사 선택 결과에는 측정값만 붙고, 회사가 버린 협의회 매치만
            # 따로 저장된다. 지식 레이어·알림에는 넘기지 않는다.
            council_extra, council_drops, _new_unmatched = apply_council_profile(
                config.council_profile,
                crawler_name,
                new_raw,
                analyzed,
                keyword_analyzer.analyze,
                source_kind,
            )
            # 재평가분의 탈락 **레코드**는 버린다: 원장의 뜻은 "저장되지 않은
            # 항목" 이고, 이 항목들은 이미 announcements 에 있다. 대신 탈락
            # **항목**은 받아서 측정값을 새 판정으로 갱신한다 (게이트 4R).
            recheck_extra, _recheck_drops, recheck_unmatched = apply_council_profile(
                config.council_profile,
                crawler_name,
                recheck_raw,
                recheck_selected,
                keyword_analyzer.analyze,
                source_kind,
            )
            if council_drops:
                try:
                    db.record_council_drops(council_drops)
                    logger.info(
                        f"{crawler_name}: 양쪽 탈락 {len(council_drops)}건 관찰 원장 기록"
                    )
                except Exception as e:
                    logger.error(f"{crawler_name}: 탈락 원장 기록 실패 (비치명): {e}")

            # 저장 직전 소스 종류를 찍는다 - 저장 경로의 단일 관문이다
            # (기간 관문과 같은 자리, 같은 이유: 우회 경로를 두지 않는다).
            for ann in (
                analyzed + council_extra + recheck_selected
                + recheck_extra + recheck_unmatched
            ):
                ann.kind = source_kind

            for ann in analyzed:
                row_id = db.insert_announcement(ann)
                if row_id:  # int is truthy, None is falsy
                    ann.id = row_id
                    all_new_announcements.append(ann)
                    logger.debug(f"Saved new announcement: {ann.title} (id={row_id}, score={ann.relevance_score:.2f})")

            council_saved = 0
            for ann in council_extra:
                if db.insert_announcement(ann):
                    council_saved += 1
            if council_extra:
                logger.info(
                    f"{crawler_name}: 협의회 단독 적재 {council_saved}/"
                    f"{len(council_extra)}건 (회사 알림·브리핑 경로 제외)"
                )

            # 재평가분은 전부 UPDATE 경로로 간다 (이미 저장된 행이다).
            #   recheck_selected  회사 통과 -> council_only=0 으로 **승격**
            #   recheck_extra     협의회만 매치 -> 플래그 유지, 측정 갱신
            #   recheck_unmatched 양쪽 탈락 -> 플래그 유지, 측정을 **0 으로** 갱신
            # 셋 다 council_only 는 UPDATE 의 CASE 가 지킨다(강등 없음).
            promoted = sum(1 for ann in recheck_selected if ann.council_only == 0)
            for ann in recheck_selected + recheck_extra + recheck_unmatched:
                db.insert_announcement(ann)
            # 보지 못한 항목은 장부를 **건드리지 않는다** - 절약 장부가 서면
            # 24시간 동안 다시 보지 않게 되어, 미룬 승격이 미뤄진 채 굳는다.
            deferred_keys = {
                (ann.source, ann.source_id) for ann in recheck_deferred
            }
            for raw_ann in recheck_raw:
                if (raw_ann.source, raw_ann.source_id) in deferred_keys:
                    continue
                try:
                    db.mark_council_rechecked(raw_ann)
                except Exception as e:
                    logger.debug(
                        f"recheck bookkeeping failed for {raw_ann.source_id}: {e}"
                    )
            if recheck_raw:
                logger.info(
                    f"{crawler_name}: 재평가 결과 승격 {promoted}건, "
                    f"측정 갱신 {len(recheck_extra) + len(recheck_unmatched)}건"
                )

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
                    db.mark_notified(ann)
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
