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
    DETAIL_REQUEST_DEADLINE,
    DETAIL_TIMEOUT,
    DETAIL_TRUNCATED,
    EARLY_CLOSE,
    MAX_DETAIL_BYTES,
    MAX_DETAIL_REQUESTS,
    apply_quote_period,
    complete_html_prefix,
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

        # 이미 인용을 받은 공고의 source_id. 파이프라인이 DB에서 읽어 넣어
        # 주면 그 항목은 요청 예산을 쓰지 않는다 (Codex 재검토 #11).
        self._quoted_source_ids: set = set()

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

    def fetch_detail_quotes(
        self, detail_url: str, deadline: Optional[float] = None
    ) -> Dict[str, str]:
        """상세 페이지에서 마감/자격/금액 인용을 가져온다.

        브라우저 User-Agent, 10초 연결/읽기 타임아웃, **재시도 없음**,
        본문 ``MAX_DETAIL_BYTES`` 상한으로 요청한다.

        **읽는 도중에도 시간을 검사한다** (Codex 재검토 #8 NOT FIXED):
        ``requests`` 의 timeout은 "청크 사이 간격" 만 보므로 8KB를 9초마다
        흘려보내는 서버에는 걸리지 않는다(실측 단일 요청 1,152초). 그래서
        청크마다 벽시계를 확인해 요청당 ``DETAIL_REQUEST_DEADLINE`` 초,
        그리고 호출자가 준 소스 예산 ``deadline`` 을 넘기면 즉시 중단한다.

        상한을 넘으면 거부하지 않고 거기까지만 읽어 파싱한다. 다만 **절단된
        꼬리는 버린다**(``complete_html_prefix``) - ``2026.09.30`` 이
        ``2026.09.3`` 으로 잘려 09-03이 되거나 끊긴 속성이 본문으로 새는
        것을 막는다 (재검토 #8).

        Args:
            detail_url: 상세 페이지 URL
            deadline: 이 monotonic 시각을 넘기면 중단한다 (소스 예산)

        Returns:
            찾은 인용만 담은 딕셔너리. 실패하거나 문구가 없으면 빈 딕셔너리.
            절단된 경우 ``detail_truncated`` 키가 함께 들어간다
        """
        if not detail_url:
            return {}

        started = time.monotonic()
        hard_deadline = started + DETAIL_REQUEST_DEADLINE
        if deadline is not None:
            hard_deadline = min(hard_deadline, deadline)

        response = None
        truncated = False
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
                self.logger.info(
                    f"Detail body declared {declared} bytes, reading first "
                    f"{MAX_DETAIL_BYTES} for {detail_url}"
                )

            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=8192):
                if time.monotonic() > hard_deadline:
                    self.logger.warning(
                        f"Detail read exceeded its wall-clock limit after "
                        f"{time.monotonic() - started:.1f}s, giving up on {detail_url}"
                    )
                    truncated = True
                    break
                if not chunk:
                    continue
                remaining = MAX_DETAIL_BYTES - total
                if len(chunk) >= remaining:
                    chunks.append(chunk[:remaining])
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)
            body = b"".join(chunks)
        except requests.RequestException as exc:
            self.logger.warning(f"Detail fetch failed for {detail_url}: {exc}")
            return {}
        finally:
            if response is not None:
                response.close()

        html = self._decode(body, response.encoding)
        if truncated:
            html = complete_html_prefix(html)
            self.logger.warning(
                f"Detail body truncated for {detail_url} "
                f"(불완전한 꼬리는 버렸다; 인용 없으면 원문 확인으로 남는다)"
            )
        quotes = extract_quotes(normalize_text(html))
        if quotes and truncated:
            quotes = dict(quotes)
            quotes[DETAIL_TRUNCATED] = True
        return quotes

    def set_quoted_source_ids(self, source_ids: Iterable[str]) -> None:
        """이미 인용을 받은 공고의 source_id를 알려 준다.

        파이프라인이 DB에서 읽어 넣어 주면 그 항목은 요청 예산을 쓰지 않아
        목록 뒤쪽 항목이 상한 때문에 영구히 미수집되는 일이 없어진다
        (Codex 재검토 #11).
        """
        self._quoted_source_ids = {str(value) for value in source_ids}

    def enrich_with_quotes(
        self,
        announcements: List[RawAnnouncement],
        skip_source_ids: Optional[Iterable[str]] = None,
    ) -> List[RawAnnouncement]:
        """각 공고의 상세 페이지를 1초 1건으로 훑어 인용을 채운다.

        ``fetch_detail`` 이 꺼져 있으면 아무것도 하지 않는다. 인용이 없으면
        키를 만들지 않는다 - 값을 지어내지 않는 것이 계약이다.

        실행시간 상한: 소스·실행당 새 요청 ``MAX_DETAIL_REQUESTS`` 건,
        총 ``DETAIL_BUDGET_SEC`` 초(읽는 도중에도 검사), 요청당
        ``DETAIL_REQUEST_DEADLINE`` 초. 한도에 걸리면 경고를 남기고
        멈춘다(수집 자체는 실패시키지 않는다).

        이미 인용이 있는 항목, ``skip_source_ids``, DB에서 받은
        ``set_quoted_source_ids`` 항목은 **예산을 쓰지 않는다**.

        Args:
            announcements: 목록 단계에서 만든 공고 리스트 (제자리에서 수정)
            skip_source_ids: 이 실행에서 건너뛸 source_id

        Returns:
            같은 리스트
        """
        if not self.wants_detail():
            return announcements

        skip = set(self._quoted_source_ids)
        skip.update(str(value) for value in (skip_source_ids or ()))
        started = time.monotonic()
        budget_deadline = started + DETAIL_BUDGET_SEC
        made = 0

        for announcement in announcements:
            url = announcement.url or ""
            if not url or url.endswith("#void"):
                continue
            if str(announcement.source_id) in skip:
                continue
            if has_quote_keys(self._load_raw(announcement)):
                continue

            if made >= MAX_DETAIL_REQUESTS:
                self.logger.warning(
                    f"{self.source_name}: detail request cap "
                    f"({MAX_DETAIL_REQUESTS}) reached, skipping the rest"
                )
                break
            if time.monotonic() > budget_deadline:
                self.logger.warning(
                    f"{self.source_name}: detail time budget "
                    f"({DETAIL_BUDGET_SEC}s) exhausted after {made} requests"
                )
                break

            if made:
                time.sleep(DETAIL_DELAY_SEC)
            quotes = self.fetch_detail_quotes(url, deadline=budget_deadline)
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

        **명시된 날짜가 "상시" 보다 우선한다** (Codex 재검토 #9). 인용에서
        종료일이 잡히면 그 값을 쓰고, "예산 소진 시 조기마감" 은 마감이 있는
        공고이므로 ``early_close`` 로만 표시한다. 종료일이 전혀 없고
        "상시/수시/연중" 만 있을 때 ``always_open`` 으로 표시하며, 이 경우에도
        제목 등에서 이미 확정된 마감은 **지우지 않는다** - 명시된 날짜가 이긴다.
        """
        truncated = bool(quotes.pop(DETAIL_TRUNCATED, False))
        start, end, always_open, early_close = apply_quote_period(quotes)
        if start:
            announcement.period_start = start
        if end:
            announcement.period_end = end

        payload = self._load_raw(announcement)
        payload.update(quotes)
        if start:
            payload["quote_period_start"] = start
        if end:
            payload["quote_period_end"] = end
        if always_open:
            payload[ALWAYS_OPEN] = True
        else:
            payload.pop(ALWAYS_OPEN, None)
        if early_close:
            payload[EARLY_CLOSE] = True
        if truncated:
            payload[DETAIL_TRUNCATED] = True

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
