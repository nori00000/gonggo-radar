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

    # ── 사이트별 손잡이 (P2-X, 감사 V §4) ────────────────────────────────
    # 전부 **클래스 속성**이고 기본값은 "종전 그대로" 다. 한 소스의 고장을
    # 고치려고 설정 전역값(config.yaml 의 timeout/retry)을 흔들면 멀쩡한
    # 소스 30여 개의 동작이 같이 바뀐다.
    #
    # TLS: 검증을 끄는 손잡이는 없다 — `alert/crawlers/tls.py` 참조.
    TLS_LEGACY_SECURITY_LEVEL = False   # True 면 SECLEVEL=1 (semas)
    TLS_EXTRA_CA_FILE: Optional[str] = None   # alert/certs/ 안의 파일명 (ggeea)
    # 그 정책이 붙을 **호스트 접두** (라운드 2, Codex MEDIUM). 비면 정책을 쓰지
    # 않는다 — `https://` 전역 폴백은 없다. 교차 호스트 리다이렉트까지 예외가
    # 따라가는 것을 막는 유일한 자리다.
    TLS_PREFIXES: Tuple[str, ...] = ()
    # 재시도·타임아웃: None 이면 config.yaml 의 전역값을 쓴다.
    CONNECT_TIMEOUT: Optional[float] = None
    READ_TIMEOUT: Optional[float] = None
    RETRY_COUNT: Optional[int] = None
    RETRY_DELAY: Optional[float] = None

    # 13차: 기간은 **크롤러가 만들지 않는다**. ``period_start``/
    # ``period_end`` 는 DB 도달 직전의 관문
    # (``alert.main._finalize_periods``)이 단독으로 정한다 - 크롤러가
    # 무엇을 반환하든 그 관문에서 리셋되고, 전용 추출기를 가진 소스만
    # 다시 채워진다. 12차의 "선언한 소스만 쓸 수 있다" 는 하위 클래스
    # override·직접 생성 객체로 우회됐다 (9차 게이트 HIGH).

    def __init__(self, source_name: str):
        self.source_name = source_name
        self.config = get_config()
        self.logger = setup_logger(f"crawler.{source_name}")

        crawler_cfg = self.config.crawler
        self.timeout = crawler_cfg.timeout
        # P2-X: 소스별 재시도 정책 (기본은 전역값 그대로).
        self.retry_count = (crawler_cfg.retry_count if self.RETRY_COUNT is None
                            else int(self.RETRY_COUNT))
        self.retry_delay = (crawler_cfg.retry_delay if self.RETRY_DELAY is None
                            else float(self.RETRY_DELAY))
        self.user_agent = crawler_cfg.user_agent

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html, application/xml, */*",
        })

        # P2-X: 사이트별 TLS 정책 — 그 크롤러의 세션에만 붙는다.
        if self.TLS_LEGACY_SECURITY_LEVEL or self.TLS_EXTRA_CA_FILE:
            if not self.TLS_PREFIXES:
                raise ValueError(
                    f"{type(self).__name__}: TLS 정책을 선언했으면 "
                    "TLS_PREFIXES 로 적용 호스트를 한정해야 합니다"
                )
            from .tls import CERTS_DIR, apply_tls_policy
            apply_tls_policy(
                self.session,
                legacy_security_level=self.TLS_LEGACY_SECURITY_LEVEL,
                extra_ca_file=(CERTS_DIR / self.TLS_EXTRA_CA_FILE
                               if self.TLS_EXTRA_CA_FILE else None),
                prefixes=self.TLS_PREFIXES,
            )

        # 이미 인용을 받은 공고의 source_id. 파이프라인이 DB에서 읽어 넣어
        # 주면 그 항목은 요청 예산을 쓰지 않는다 (Codex 재검토 #11).
        self._quoted_source_ids: set = set()
        # source_id -> 마지막 상세 시도 시각. 오래된 것부터 다시 시도해
        # 목록 뒤쪽이 영구히 미수집으로 남지 않게 한다 (4차 게이트 #6).
        self._quote_attempts: Dict[str, str] = {}

        # P2-X H1: 이번 실행에서 **끝내 실패한** 요청의 사유. 크롤러들은 관례상
        # 실패를 로그로 남기고 ``[]`` 를 돌려주므로(semas.py 가 그렇다), 파이프라인
        # 입장에서 "전부 실패" 와 "공고가 없었다" 가 구별되지 않았다 — 감사 V H-1.
        # 이 목록이 비어 있지 않고 수집 결과도 0건이면 그 소스의 런은 오류다.
        self.fetch_errors: List[str] = []

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
        kwargs.setdefault("timeout", self.request_timeout())

        last_error: Optional[BaseException] = None
        for attempt in range(1, self.retry_count + 1):
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                last_error = e
                self.logger.warning(
                    f"Attempt {attempt}/{self.retry_count} failed for {url}: {e}"
                )
                if attempt < self.retry_count:
                    time.sleep(self.retry_delay)

        self.logger.error(f"All {self.retry_count} attempts failed for {url}")
        # P2-X H1: 로그만 남기고 None 을 돌려주면 파이프라인은 이 실패를 볼 수 없다.
        # 소스별 사유를 남겨 run_history 가 status='error' 로 기록하게 한다.
        self.record_fetch_error(f"{method} {url}: {last_error}")
        return None

    def record_fetch_error(self, message: str) -> None:
        """이번 실행의 실패 사유를 남긴다 (P2-X H1).

        ``_request_with_retry`` 가 전패했을 때와 ``safe_fetch`` 가 예외를 받았을 때
        불린다. 크롤러가 자체 재시도·대체 경로를 가진 경우에도 **결과가 0건이면**
        이 목록이 그 실행을 오류로 만든다.
        """
        text = str(message)
        if text not in self.fetch_errors:
            self.fetch_errors.append(text)

    def fetch_error_summary(self, limit: int = 3) -> str:
        """run_history.error_message 에 실릴 소스별 사유 한 줄 (P2-X H1)."""
        if not self.fetch_errors:
            return ""
        head = "; ".join(self.fetch_errors[:limit])
        extra = len(self.fetch_errors) - limit
        return head + (f" (외 {extra}건)" if extra > 0 else "")

    def request_timeout(self):
        """이 크롤러의 타임아웃 (P2-X).

        ``CONNECT_TIMEOUT``/``READ_TIMEOUT`` 을 선언한 크롤러는
        ``(연결, 읽기)`` 튜플을 쓴다. 감사 V §4 의 ipet 실측 근거:
        **응답할 때는 1초 안에 응답하고, 응답하지 않을 때는 45초를 기다려도
        오지 않는다.** 긴 단일 타임아웃 × 적은 재시도는 런 시간만 먹고
        성공률을 올리지 않는다 — 짧은 읽기 타임아웃 × 잦은 재시도가 같은
        성공률을 훨씬 싸게 낸다.
        """
        if self.CONNECT_TIMEOUT is None and self.READ_TIMEOUT is None:
            return self.timeout
        return (
            float(self.CONNECT_TIMEOUT if self.CONNECT_TIMEOUT is not None
                  else self.timeout),
            float(self.READ_TIMEOUT if self.READ_TIMEOUT is not None
                  else self.timeout),
        )

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

        **명시된 날짜가 "상시" 보다 우선한다**. 인용에서 종료일이 잡히면
        ``quote_period_end`` 증거로 남기고, "예산 소진 시 조기마감" 은 마감이
        있는 공고이므로 ``early_close`` 로만 표시한다. 종료일이 전혀 없고
        "상시/수시/연중" 만 있을 때 ``always_open`` 이다.

        기간 두 필드는 **여기서 쓰지 않는다** - 관문이 정한다 (13차).

        인용이 없어도 **시도 시각**을 남긴다 - 다음 실행이 아직 안 본 항목을
        먼저 보게 하는 근거다 (4차 게이트 #6).
        """
        result = result or {}
        start, end, always_open, early_close = apply_quote_period(quotes)

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

        # P2-X H1: 실행마다 초기화한다 — 같은 객체를 재사용해도 지난 실행의 사유가
        # 이번 판정을 오염시키지 않는다.
        self.fetch_errors = []
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
            self.record_fetch_error(f"{type(e).__name__}: {e}")
            return []
