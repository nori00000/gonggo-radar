"""크롤러 추상 베이스 클래스"""
import abc
import json
import time
import requests
from typing import Dict, Iterable, List, Optional
from ..models import RawAnnouncement
from ..config import get_config
from ..utils.logger import setup_logger
from .detail_quotes import (
    ALWAYS_OPEN,
    BROWSER_USER_AGENT,
    DETAIL_BUDGET_SEC,
    DETAIL_DELAY_SEC,
    DETAIL_TIMEOUT,
    MAX_DETAIL_BYTES,
    MAX_DETAIL_REQUESTS,
    apply_quote_period,
    extract_quotes,
    has_quote_keys,
    normalize_text,
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

    @staticmethod
    def _decode(body: bytes, header_encoding: Optional[str]) -> str:
        """응답 본문을 디코드한다 (헤더 우선, 그다음 utf-8/cp949 시도)."""
        candidates = [header_encoding, "utf-8", "cp949"]
        for encoding in candidates:
            if not encoding:
                continue
            try:
                return body.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return body.decode("utf-8", errors="replace")

    def fetch_detail_quotes(self, detail_url: str) -> Dict[str, str]:
        """상세 페이지에서 마감/자격/금액 인용을 가져온다.

        브라우저 User-Agent, 10초 타임아웃, **재시도 없음**, 본문
        256KB 상한으로 요청한다. 재시도를 하지 않는 이유는 인용 하나가
        재시도 3회(최악 40초 이상)를 쓸 가치가 없기 때문이고, 본문 상한은
        타임아웃보다 느리게 계속 흘려보내는 응답을 끊기 위한 것이다
        (계약 v2.1 V2 + Codex 크리틱 #8).

        Args:
            detail_url: 상세 페이지 URL

        Returns:
            찾은 인용만 담은 딕셔너리. 실패하거나 문구가 없으면 빈 딕셔너리
        """
        if not detail_url:
            return {}

        response = None
        try:
            response = self.session.get(
                detail_url,
                timeout=DETAIL_TIMEOUT,
                headers={"User-Agent": BROWSER_USER_AGENT},
                stream=True,
            )
            response.raise_for_status()

            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_DETAIL_BYTES:
                self.logger.warning(
                    f"Detail body too large ({declared} bytes), skipping {detail_url}"
                )
                return {}

            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_DETAIL_BYTES:
                    self.logger.warning(
                        f"Detail body exceeded {MAX_DETAIL_BYTES} bytes, "
                        f"skipping {detail_url}"
                    )
                    return {}
            body = b"".join(chunks)
        except requests.RequestException as exc:
            self.logger.warning(f"Detail fetch failed for {detail_url}: {exc}")
            return {}
        finally:
            if response is not None:
                response.close()

        html = self._decode(body, response.encoding)
        return extract_quotes(normalize_text(html))

    def enrich_with_quotes(
        self,
        announcements: List[RawAnnouncement],
        skip_source_ids: Optional[Iterable[str]] = None,
    ) -> List[RawAnnouncement]:
        """각 공고의 상세 페이지를 1초 1건으로 훑어 인용을 채운다.

        ``fetch_detail`` 이 꺼져 있으면 아무것도 하지 않는다. 인용이 없으면
        키를 만들지 않는다 - 값을 지어내지 않는 것이 계약이다.

        실행시간 상한(Codex 크리틱 #8): 소스·실행당 새 요청
        ``MAX_DETAIL_REQUESTS`` 건, 총 ``DETAIL_BUDGET_SEC`` 초. 한도에
        걸리면 경고를 남기고 멈춘다(수집 자체는 실패시키지 않는다).
        이미 인용이 있는 항목과 ``skip_source_ids`` 에 든 항목은 건너뛴다.

        Args:
            announcements: 목록 단계에서 만든 공고 리스트 (제자리에서 수정)
            skip_source_ids: 이미 인용을 받은 공고의 source_id (재요청 방지)

        Returns:
            같은 리스트
        """
        if not self.wants_detail():
            return announcements

        skip = set(skip_source_ids or ())
        started = time.monotonic()
        made = 0

        for announcement in announcements:
            url = announcement.url or ""
            if not url or url.endswith("#void"):
                continue
            if announcement.source_id in skip:
                continue
            if has_quote_keys(self._load_raw(announcement)):
                continue

            if made >= MAX_DETAIL_REQUESTS:
                self.logger.warning(
                    f"{self.source_name}: detail request cap "
                    f"({MAX_DETAIL_REQUESTS}) reached, skipping the rest"
                )
                break
            if time.monotonic() - started > DETAIL_BUDGET_SEC:
                self.logger.warning(
                    f"{self.source_name}: detail time budget "
                    f"({DETAIL_BUDGET_SEC}s) exhausted after {made} requests"
                )
                break

            if made:
                time.sleep(DETAIL_DELAY_SEC)
            quotes = self.fetch_detail_quotes(url)
            made += 1
            if quotes:
                self._apply_quotes(announcement, quotes)

        return announcements

    @staticmethod
    def _load_raw(announcement: RawAnnouncement) -> dict:
        """raw_data JSON을 딕셔너리로 읽는다 (깨져 있으면 감싸서 보존)."""
        if not announcement.raw_data:
            return {}
        try:
            loaded = json.loads(announcement.raw_data)
        except (ValueError, TypeError):
            return {"list_raw": announcement.raw_data}
        return loaded if isinstance(loaded, dict) else {"list_raw": announcement.raw_data}

    def _apply_quotes(
        self, announcement: RawAnnouncement, quotes: Dict[str, str]
    ) -> None:
        """인용을 raw_data에 싣고, 확신할 수 있는 날짜만 기간 필드에 반영한다.

        "상시 / 예산 소진 시" 공고는 마감을 만들지 않고 ``always_open`` 으로
        표시한다 - 게시 다음 날 만료로 처리되는 것을 막는다(크리틱 #3).
        """
        start, end, always_open = apply_quote_period(quotes)
        if start:
            announcement.period_start = start
        if always_open:
            announcement.period_end = None
        elif end:
            announcement.period_end = end

        payload = self._load_raw(announcement)
        payload.update(quotes)
        if start:
            payload["quote_period_start"] = start
        if end and not always_open:
            payload["quote_period_end"] = end
        if always_open:
            payload[ALWAYS_OPEN] = True
            payload.pop("quote_period_end", None)

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
