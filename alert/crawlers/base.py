"""크롤러 추상 베이스 클래스"""
import abc
import json
import time
import requests
from typing import Dict, List, Optional
from ..models import RawAnnouncement
from ..config import get_config
from ..utils.logger import setup_logger
from .detail_quotes import (
    BROWSER_USER_AGENT,
    DETAIL_DELAY_SEC,
    DETAIL_TIMEOUT,
    QUOTE_DEADLINE,
    extract_quotes,
    normalize_text,
    period_from_quote,
)


class BaseCrawler(abc.ABC):
    """All crawlers inherit from this."""

    def __init__(self, source_name: str):
        self.source_name = source_name
        self.config = get_config()
        self.logger = setup_logger(f"crawler.{source_name}")

        crawler_cfg = self.config.crawler
        self.timeout = crawler_cfg.timeout
        self.retry_count = crawler_cfg.retry_count
        self.retry_delay = crawler_cfg.retry_delay
        self.user_agent = crawler_cfg.user_agent

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html, application/xml, */*",
        })

    @abc.abstractmethod
    def fetch(self) -> List[RawAnnouncement]:
        """Fetch announcements from the source. Must be implemented by subclass."""
        pass

    def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> Optional[requests.Response]:
        """HTTP request with retry logic.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Target URL
            **kwargs: Additional arguments passed to requests.request()

        Returns:
            Response object if successful, None if all retries failed
        """
        kwargs.setdefault("timeout", self.timeout)

        for attempt in range(1, self.retry_count + 1):
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                self.logger.warning(
                    f"Attempt {attempt}/{self.retry_count} failed for {url}: {e}"
                )
                if attempt < self.retry_count:
                    time.sleep(self.retry_delay)

        self.logger.error(f"All {self.retry_count} attempts failed for {url}")
        return None

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        """Perform GET request with retry logic."""
        return self._request_with_retry("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Optional[requests.Response]:
        """Perform POST request with retry logic."""
        return self._request_with_retry("POST", url, **kwargs)

    def is_enabled(self) -> bool:
        """Check if this crawler source is enabled in configuration."""
        sources = self.config.crawler.sources
        source_cfg = sources.get(self.source_name)
        if source_cfg is None:
            return True  # Default to enabled if not specified
        return source_cfg.enabled

    def get_base_url(self) -> str:
        """Get the base URL for this source from configuration."""
        sources = self.config.crawler.sources
        source_cfg = sources.get(self.source_name)
        if source_cfg is None:
            return ""
        return source_cfg.base_url

    def wants_detail(self) -> bool:
        """이 소스에 상세 페이지 인용 추출이 켜져 있는지 확인한다.

        기본값은 꺼짐이다. ``config.yaml`` 의 소스별 ``fetch_detail: true`` 로만
        켠다. ``is True`` 비교는 목(mock) 설정이 우연히 truthy가 되어 테스트가
        네트워크를 타는 것을 막는다.
        """
        source_cfg = self.config.crawler.sources.get(self.source_name)
        return getattr(source_cfg, "fetch_detail", False) is True

    def fetch_detail_quotes(self, detail_url: str) -> Dict[str, str]:
        """상세 페이지에서 마감/자격/금액 인용을 가져온다.

        브라우저 User-Agent와 10초 타임아웃으로 요청한다(계약 v2.1 V2).

        Args:
            detail_url: 상세 페이지 URL

        Returns:
            찾은 인용만 담은 딕셔너리. 실패하거나 문구가 없으면 빈 딕셔너리
        """
        if not detail_url:
            return {}

        response = self.get(
            detail_url,
            timeout=DETAIL_TIMEOUT,
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        if response is None:
            return {}

        response.encoding = response.apparent_encoding or "utf-8"
        return extract_quotes(normalize_text(response.text))

    def enrich_with_quotes(
        self, announcements: List[RawAnnouncement]
    ) -> List[RawAnnouncement]:
        """각 공고의 상세 페이지를 1초 1건으로 훑어 인용을 채운다.

        ``fetch_detail`` 이 꺼져 있으면 아무것도 하지 않는다. 인용이 없으면
        키를 만들지 않는다 - 값을 지어내지 않는 것이 계약이다.

        Args:
            announcements: 목록 단계에서 만든 공고 리스트 (제자리에서 수정)

        Returns:
            같은 리스트
        """
        if not self.wants_detail():
            return announcements

        for index, announcement in enumerate(announcements):
            url = announcement.url or ""
            if not url or url.endswith("#void"):
                continue
            if index:
                time.sleep(DETAIL_DELAY_SEC)
            quotes = self.fetch_detail_quotes(url)
            if quotes:
                self._apply_quotes(announcement, quotes)

        return announcements

    def _apply_quotes(
        self, announcement: RawAnnouncement, quotes: Dict[str, str]
    ) -> None:
        """인용을 raw_data에 싣고, 확신할 수 있는 날짜만 기간 필드에 반영한다."""
        start, end = period_from_quote(quotes.get(QUOTE_DEADLINE, ""))
        if start:
            announcement.period_start = start
        if end:
            announcement.period_end = end

        payload: dict = {}
        if announcement.raw_data:
            try:
                loaded = json.loads(announcement.raw_data)
            except (ValueError, TypeError):
                loaded = None
            payload = loaded if isinstance(loaded, dict) else {"list_raw": announcement.raw_data}

        payload.update(quotes)
        if start:
            payload["quote_period_start"] = start
        if end:
            payload["quote_period_end"] = end

        announcement.raw_data = json.dumps(payload, ensure_ascii=False)

    def safe_fetch(self) -> List[RawAnnouncement]:
        """Wrapper that catches exceptions and logs them.

        This is the main entry point for running a crawler safely.
        It checks if the crawler is enabled, runs fetch(), and handles
        any exceptions that occur.

        Returns:
            List of RawAnnouncement objects, or empty list on failure
        """
        if not self.is_enabled():
            self.logger.info(f"{self.source_name} crawler is disabled, skipping")
            return []

        try:
            self.logger.info(f"Starting {self.source_name} crawl...")
            results = self.fetch()
            self.logger.info(
                f"{self.source_name}: fetched {len(results)} announcements"
            )
            return results
        except Exception as e:
            self.logger.error(
                f"{self.source_name} crawl failed: {e}",
                exc_info=True
            )
            return []
