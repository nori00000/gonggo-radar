"""크롤러 추상 베이스 클래스"""
import abc
import json
import subprocess
import sys
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from ..models import RawAnnouncement
from ..config import get_config
from ..utils.logger import setup_logger
from .detail_quotes import (
    ALWAYS_OPEN,
    DETAIL_BUDGET_SEC,
    DETAIL_DELAY_SEC,
    DETAIL_REQUEST_DEADLINE,
    DETAIL_TRUNCATED,
    EARLY_CLOSE,
    MAX_DETAIL_REQUESTS,
    QUOTES_ATTEMPTED_AT,
    WORKER_MODULE,
    apply_quote_period,
    has_quote_keys,
)


class BaseCrawler(abc.ABC):
    """All crawlers inherit from this."""

    # 12차 choke point: 접수기간은 **이 이름의 메서드를 선언한 크롤러만**
    # 만들 수 있다. 선언이 없으면 ``safe_fetch`` 가 기간을 무조건 None 으로
    # 되돌린다 - 하위 클래스의 파서가 무엇을 계산했든 소용없다. 환각
    # 마감은 열 번의 게이트에서 늘 "라벨을 잘못 읽었다" 로 들어왔으므로
    # 읽기를 고치는 대신 **쓸 수 있는 소스를 선언으로 제한**한다.
    PERIOD_EXTRACTOR: Optional[str] = None

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
        # source_id -> 마지막 상세 시도 시각. 오래된 것부터 다시 시도해
        # 목록 뒤쪽이 영구히 미수집으로 남지 않게 한다 (4차 게이트 #6).
        self._quote_attempts: Dict[str, str] = {}

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

    def set_quoted_source_ids(self, source_ids: Iterable[str]) -> None:
        """이미 인용을 받은 공고의 source_id를 알려 준다."""
        self._quoted_source_ids = {str(value) for value in source_ids}

    def set_quote_attempts(self, attempts: Dict[str, str]) -> None:
        """source_id -> 마지막 시도 시각. 오래된 것부터 다시 시도한다."""
        self._quote_attempts = {str(k): str(v or "") for k, v in (attempts or {}).items()}

    @staticmethod
    def _worker_cwd() -> str:
        """``alert`` 패키지를 import 할 수 있는 작업 디렉터리."""
        return str(Path(__file__).resolve().parent.parent.parent)

    def run_detail_worker(
        self, items: List[dict]
    ) -> Tuple[List[dict], bool]:
        """상세 수집을 **자식 프로세스**에 맡긴다.

        프로세스 경계가 유일하게 협조를 요구하지 않는 시간 상한이다.
        ``subprocess.run(timeout=…)`` 이 프로세스를 죽이므로 느린 소켓,
        동기 ``close()``, 폭주하는 파서 무엇도 예산을 넘길 수 없다. 자식이
        항목마다 한 줄씩 flush 하므로 **부분 결과는 살아남는다**.

        Args:
            items: ``[{"source_id":…, "url":…}, …]``

        Returns:
            ``(결과 줄 리스트, 시간초과 여부)``
        """
        if not items:
            return [], False

        command = [
            sys.executable, "-m", WORKER_MODULE,
            "--source", self.source_name,
            "--item-deadline", str(DETAIL_REQUEST_DEADLINE),
            "--delay", str(DETAIL_DELAY_SEC),
        ]
        payload = json.dumps(items, ensure_ascii=False)
        stdout = ""
        timed_out = False
        began = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                input=payload,
                capture_output=True,
                text=True,
                timeout=DETAIL_BUDGET_SEC,
                cwd=self._worker_cwd(),
            )
            stdout = completed.stdout or ""
            if completed.returncode != 0:
                self.logger.warning(
                    f"{self.source_name}: detail worker exited "
                    f"{completed.returncode}: {(completed.stderr or '')[-300:]}"
                )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            stdout = partial
            self.logger.warning(
                f"{self.source_name}: detail worker killed at "
                f"{DETAIL_BUDGET_SEC}s budget after {time.monotonic() - began:.1f}s "
                f"(부분 결과 {len(stdout.splitlines())}줄 보존)"
            )
        except (OSError, ValueError) as exc:
            self.logger.warning(
                f"{self.source_name}: detail worker could not run: {exc}"
            )
            return [], False

        results = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                results.append(parsed)
        self.logger.info(
            f"{self.source_name}: detail worker returned {len(results)}/"
            f"{len(items)} items in {time.monotonic() - began:.1f}s"
        )
        return results, timed_out

    def _detail_candidates(
        self, announcements: List[RawAnnouncement], skip: set
    ) -> List[RawAnnouncement]:
        """상세를 받아야 하는 항목을 **오래 안 본 것부터** 고른다.

        인용이 이미 있는 항목과 건너뛰라고 받은 항목은 예산을 쓰지 않는다.
        나머지는 마지막 시도 시각 오름차순(시도 없음이 먼저)으로 정렬해
        목록이 상한보다 길어도 뒤쪽이 영구히 미수집으로 남지 않게 한다
        (4차 게이트 #6).
        """
        candidates = []
        for announcement in announcements:
            url = announcement.url or ""
            if not url or url.endswith("#void"):
                continue
            if str(announcement.source_id) in skip:
                continue
            payload = self._load_raw(announcement)
            if has_quote_keys(payload):
                continue
            attempted = str(
                payload.get(QUOTES_ATTEMPTED_AT)
                or self._quote_attempts.get(str(announcement.source_id), "")
            )
            candidates.append((attempted, announcement))

        candidates.sort(key=lambda pair: pair[0])
        return [announcement for _attempted, announcement in candidates]

    def enrich_with_quotes(
        self,
        announcements: List[RawAnnouncement],
        skip_source_ids: Optional[Iterable[str]] = None,
    ) -> List[RawAnnouncement]:
        """상세 페이지 인용을 채운다 (자식 프로세스 격리 실행).

        ``fetch_detail`` 이 꺼져 있으면 아무것도 하지 않는다. 인용이 없으면
        키를 만들지 않는다 - 값을 지어내지 않는 것이 계약이다. 인용을 얻지
        못한 항목도 **시도 시각을 남겨** 다음 실행이 다른 항목을 먼저 보게
        한다.

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
        candidates = self._detail_candidates(announcements, skip)[
            :MAX_DETAIL_REQUESTS
        ]
        if not candidates:
            return announcements

        items = [
            {"source_id": str(a.source_id), "url": a.url} for a in candidates
        ]
        results, _timed_out = self.run_detail_worker(items)
        by_source_id = {str(r.get("source_id", "")): r for r in results}

        for announcement in candidates:
            result = by_source_id.get(str(announcement.source_id))
            if result is None:
                continue  # 자식이 여기까지 오지 못했다 - 시도로 치지 않는다
            quotes = result.get("quotes") or {}
            if not isinstance(quotes, dict):
                quotes = {}
            self._apply_quotes(announcement, quotes, result=result)

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
        self,
        announcement: RawAnnouncement,
        quotes: Dict[str, str],
        result: Optional[dict] = None,
    ) -> None:
        """인용을 raw_data에 싣고, 확신할 수 있는 날짜만 기간 필드에 반영한다.

        **명시된 날짜가 "상시" 보다 우선한다**. 인용에서 종료일이 잡히면 그
        값을 쓰고, "예산 소진 시 조기마감" 은 마감이 있는 공고이므로
        ``early_close`` 로만 표시한다. 종료일이 전혀 없고 "상시/수시/연중" 만
        있을 때 ``always_open`` 이며, 그때도 이미 확정된 마감은 지우지 않는다.

        인용이 없어도 **시도 시각**을 남긴다 - 다음 실행이 아직 안 본 항목을
        먼저 보게 하는 근거다 (4차 게이트 #6).
        """
        result = result or {}
        start, end, always_open, early_close = apply_quote_period(quotes)
        if start:
            announcement.period_start = start
        if end:
            announcement.period_end = end

        payload = self._load_raw(announcement)
        payload[QUOTES_ATTEMPTED_AT] = datetime.now().isoformat()
        if result.get("truncated"):
            payload[DETAIL_TRUNCATED] = True
        else:
            payload.pop(DETAIL_TRUNCATED, None)

        if quotes:
            payload.update(quotes)
            # 인용이 교체되면 예전 파생값을 남기지 않는다
            payload.pop("quote_period_start", None)
            payload.pop("quote_period_end", None)
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
            else:
                payload.pop(EARLY_CLOSE, None)

        announcement.raw_data = json.dumps(payload, ensure_ascii=False)

    def declares_period_extractor(self) -> bool:
        """이 크롤러가 기간 추출기를 선언했는가."""
        name = type(self).PERIOD_EXTRACTOR
        return bool(name) and callable(getattr(self, name, None))

    def resolve_period(self, item: dict) -> Tuple[Optional[str], Optional[str]]:
        """허용목록 소스만 기간을 만든다. 그 밖은 항상 ``(None, None)``.

        하위 클래스의 ``_to_announcement`` 는 기간을 직접 계산하지 않고
        이 메서드만 부른다.
        """
        if not self.declares_period_extractor():
            return None, None
        try:
            result = getattr(self, type(self).PERIOD_EXTRACTOR)(item)
        except Exception as exc:                       # noqa: BLE001
            self.logger.error(f"기간 추출 실패: {exc}")
            return None, None
        if not result:
            return None, None
        start, end = result
        return start or None, end or None

    def _enforce_period_whitelist(
        self, results: List[RawAnnouncement]
    ) -> List[RawAnnouncement]:
        """choke point - 선언 없는 소스가 만든 기간을 지운다.

        ``_to_announcement`` 를 고쳐도 새 크롤러가 다시 기간을 채울 수 있다.
        마지막 관문을 베이스에 두면 **선언하지 않은 소스는 구조적으로**
        기간을 저장할 수 없다.
        """
        if self.declares_period_extractor():
            return results
        stripped = 0
        for announcement in results:
            if announcement.period_start or announcement.period_end:
                announcement.period_start = None
                announcement.period_end = None
                stripped += 1
        if stripped:
            self.logger.warning(
                f"{self.source_name}: PERIOD_EXTRACTOR 미선언 - "
                f"기간 {stripped} 건을 지웠다"
            )
        return results

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
            return self._enforce_period_whitelist(results)
        except Exception as e:
            self.logger.error(
                f"{self.source_name} crawl failed: {e}",
                exc_info=True
            )
            return []
