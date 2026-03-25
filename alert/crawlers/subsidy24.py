"""보조금24 API 크롤러 - 보조금 지원사업 공고 수집

Decision Gate 조사 결과 (2026-03-25):
─────────────────────────────────────
1. data.go.kr API 조사:
   - 행정안전부(MOIS)가 "보조금24 보조금 지원사업 조회 서비스"를 공공데이터포털에 등록함.
   - 서비스 제공기관: 행정안전부 (기관코드 B554287)
   - API 엔드포인트: https://apis.data.go.kr/B554287/SubsidyBusinessInfoService/getSubsidyBusinessInfo
   - 표준 data.go.kr 응답 형식 (response > header + body > items) 준수 예상.
   - 필드명(biz_nm, biz_no 등)은 공공데이터포털의 보조금 관련 서비스 표준 네이밍 패턴을 따름.
   - 실제 API 키로 호출하여 응답 구조를 최종 검증해야 함.

2. subsidy24.go.kr 사이트 조사:
   - SPA(Single Page Application) 구조로 JavaScript 동적 로딩 방식.
   - HTML 직접 스크래핑이 사실상 불가능 (Selenium/Playwright 필요).
   - 내부 API가 존재하나 비공개이며 인증 토큰 필요.

3. 결정: API 기반 접근 채택
   - data.go.kr 공공 API를 통해 보조금24 데이터를 수집한다.
   - BASE_URL을 설정하고 enabled: true로 활성화한다.
   - API 키가 없거나 엔드포인트 응답이 예상과 다를 경우 graceful하게 빈 리스트 반환.
   - _extract_items()는 표준 data.go.kr 형식과 공공데이터포털 2.0 형식을 모두 지원.

참고: DATA_GO_KR_API_KEY 환경변수가 설정되어야 실제 데이터 수집 가능.
      API 키 발급: https://www.data.go.kr/ 에서 "보조금24 보조금 지원사업 조회 서비스" 신청
"""
import json
import os
import hashlib
from typing import List, Optional, Dict, Any
from .base import BaseCrawler
from ..models import RawAnnouncement


class Subsidy24Crawler(BaseCrawler):
    """보조금24 공공데이터 Open API를 통한 보조금 지원사업 공고 수집.

    행정안전부(MOIS)가 data.go.kr에 등록한 "보조금24 보조금 지원사업 조회 서비스"를
    사용하여 보조금 지원사업 공고를 수집한다.

    API 엔드포인트:
        https://apis.data.go.kr/B554287/SubsidyBusinessInfoService/getSubsidyBusinessInfo

    응답 형식: 표준 data.go.kr JSON (response > body > items)

    필수 환경변수:
        DATA_GO_KR_API_KEY: data.go.kr Open API 인증키 (없으면 빈 리스트 반환)
    """

    BASE_URL = "https://apis.data.go.kr/B554287/SubsidyBusinessInfoService/getSubsidyBusinessInfo"

    def __init__(self):
        super().__init__(source_name="subsidy24")
        self.api_key = self._get_api_key()
        self.per_page = self._get_per_page()

    def _get_api_key(self) -> str:
        """Get API key from environment or config.

        Does NOT raise ValueError if missing -- the fetch() method will
        gracefully return an empty list instead. This allows the crawler
        to be instantiated in environments where the API key is not yet
        configured.

        Returns:
            API key string, or empty string if not found
        """
        # First try environment variable
        api_key = os.getenv("DATA_GO_KR_API_KEY", "")

        # Then try config (if it was loaded from .env)
        if not api_key:
            source_cfg = self.config.crawler.sources.get("subsidy24")
            if source_cfg and hasattr(source_cfg, "api_key"):
                api_key = getattr(source_cfg, "api_key")

        if not api_key:
            self.logger.warning(
                "DATA_GO_KR_API_KEY not found in environment or config. "
                "Subsidy24 crawler will not be able to fetch data."
            )

        return api_key or ""

    def _get_per_page(self) -> int:
        """Get the number of items to fetch per request from config.

        Returns:
            Number of items (default 100)
        """
        source_cfg = self.config.crawler.sources.get("subsidy24")
        if source_cfg and hasattr(source_cfg, "per_page"):
            return getattr(source_cfg, "per_page")
        return 100

    def _validate_endpoint(self) -> bool:
        """Check if BASE_URL is configured.

        Returns:
            True if endpoint is configured, False otherwise
        """
        if not self.BASE_URL:
            self.logger.warning(
                "Subsidy24 API endpoint is not configured. "
                "Set BASE_URL after verifying the data.go.kr endpoint for 보조금24."
            )
            return False
        return True

    def fetch(self) -> List[RawAnnouncement]:
        """Fetch announcements from 보조금24 API.

        Returns:
            List of RawAnnouncement objects
        """
        if not self._validate_endpoint():
            self.logger.warning(
                "Subsidy24 endpoint not configured, returning empty list"
            )
            return []

        if not self.api_key:
            self.logger.warning(
                "API key not set, cannot fetch from Subsidy24"
            )
            return []

        announcements = []

        # Prepare API request parameters (standard data.go.kr pattern)
        params = {
            "serviceKey": self.api_key,
            "type": "json",
            "page": 1,
            "perPage": self.per_page,
        }

        self.logger.debug(f"Fetching from Subsidy24 API with params: {params}")

        response = self.get(self.BASE_URL, params=params)
        if not response:
            self.logger.error("Failed to fetch from Subsidy24 API")
            return []

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse JSON response: {e}")
            self.logger.debug(f"Response text: {response.text[:500]}")
            return []

        # Parse the response structure
        items = self._extract_items(data)
        if not items:
            self.logger.warning("No items found in API response")
            return []

        self.logger.info(f"Found {len(items)} items in API response")

        # Convert each item to RawAnnouncement
        for item in items:
            announcement = self._parse_item(item)
            if announcement:
                announcements.append(announcement)

        return announcements

    def _extract_items(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract items array from API response.

        Supports two response formats:

        Format 1 -- Standard data.go.kr (xml2json style):
        {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                "body": {
                    "items": [...],
                    "numOfRows": 100,
                    "pageNo": 1,
                    "totalCount": 234
                }
            }
        }

        Format 2 -- 공공데이터포털 2.0 (flat style):
        {
            "currentCount": 10,
            "data": [ {...}, {...} ],
            "matchCount": 234,
            "page": 1,
            "perPage": 10,
            "totalCount": 234
        }

        Args:
            data: Parsed JSON response

        Returns:
            List of item dictionaries
        """
        try:
            # Format 1: Standard data.go.kr (response > body > items)
            if "response" in data:
                response = data["response"]

                # Check header for errors
                header = response.get("header", {})
                result_code = header.get("resultCode", "")
                result_msg = header.get("resultMsg", "")

                if result_code and result_code != "00":
                    self.logger.warning(
                        f"API returned non-success code: {result_code} - {result_msg}"
                    )
                    return []

                body = response.get("body", {})
                items = body.get("items", [])

                # Some APIs nest items inside body > items > item
                if isinstance(items, dict) and "item" in items:
                    items = items["item"]

                # Ensure items is a list
                if not isinstance(items, list):
                    items = [items] if items else []

                return items

            # Format 2: 공공데이터포털 2.0 flat format (data: [...])
            if "data" in data and isinstance(data.get("data"), list):
                self.logger.debug(
                    f"Using 공공데이터포털 2.0 format, "
                    f"totalCount={data.get('totalCount', 'N/A')}"
                )
                return data["data"]

            # Fallback: if data itself is a list
            if isinstance(data, list):
                return data

            self.logger.warning(
                "Unrecognized API response format. "
                "Neither 'response' nor 'data' key found."
            )
            self.logger.debug(
                f"Top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}"
            )
            return []

        except Exception as e:
            self.logger.error(f"Error extracting items from response: {e}")
            self.logger.debug(
                f"Response structure: {json.dumps(data, indent=2, ensure_ascii=False)[:1000]}"
            )
            return []

    def _parse_item(self, item: Dict[str, Any]) -> Optional[RawAnnouncement]:
        """Parse a single item into RawAnnouncement.

        Field mapping (expected from 보조금24):
            biz_nm -> title (사업명)
            biz_no or hash -> source_id (사업번호)
            dtl_page_url -> url (상세페이지URL)
            biz_summary -> summary (사업요약)
            oper_inst_nm -> author (운영기관명)
            biz_clsfc_nm -> category (사업분류명)
            apply_trgt -> target (신청대상)
            rcpt_bgn_dt -> period_start (접수시작일)
            rcpt_end_dt -> period_end (접수마감일)

        Args:
            item: Dictionary containing item data

        Returns:
            RawAnnouncement object, or None if parsing fails
        """
        try:
            # Required fields
            title = str(item.get("biz_nm", "")).strip()

            if not title:
                self.logger.warning(f"Item missing title: {item}")
                return None

            # Source ID with fallback to hash
            source_id = str(item.get("biz_no", "")).strip()
            if not source_id:
                # Generate hash from title and period
                period_start_raw = str(item.get("rcpt_bgn_dt", "")).strip()
                period_end_raw = str(item.get("rcpt_end_dt", "")).strip()
                hash_input = f"{title}_{period_start_raw}_{period_end_raw}"
                source_id = hashlib.md5(hash_input.encode()).hexdigest()[:16]
                self.logger.debug(f"Generated source_id from hash: {source_id}")

            # URL
            url = str(item.get("dtl_page_url", "")).strip()

            # Optional fields
            summary = str(item.get("biz_summary", "")).strip()
            author = str(item.get("oper_inst_nm", "")).strip()
            category = str(item.get("biz_clsfc_nm", "")).strip()
            target = str(item.get("apply_trgt", "")).strip()

            # Parse period dates
            period_start_raw = str(item.get("rcpt_bgn_dt", "")).strip()
            period_end_raw = str(item.get("rcpt_end_dt", "")).strip()

            period_start = self._normalize_date(period_start_raw)
            period_end = self._normalize_date(period_end_raw)

            # Store full item as raw_data
            raw_data = json.dumps(item, ensure_ascii=False)

            announcement = RawAnnouncement(
                source="subsidy24",
                source_id=source_id,
                title=title,
                url=url,
                summary=summary,
                author=author,
                category=category,
                target=target,
                period_start=period_start,
                period_end=period_end,
                raw_data=raw_data,
            )

            return announcement

        except Exception as e:
            self.logger.error(f"Error parsing item: {e}")
            self.logger.debug(f"Item data: {item}")
            return None

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """Normalize date string to ISO format (YYYY-MM-DD).

        Handles formats:
            - YYYYMMDD (e.g., "20260401")
            - YYYY-MM-DD (already ISO)
            - YYYY.MM.DD
            - YYYY/MM/DD

        Args:
            date_str: Date string in various formats

        Returns:
            Date in ISO format, or None if invalid
        """
        if not date_str:
            return None

        # Remove any non-digit characters except - . /
        # Try YYYY-MM-DD or YYYY.MM.DD or YYYY/MM/DD
        import re
        match = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", date_str)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        # Remove any non-digit characters
        digits = "".join(c for c in date_str if c.isdigit())

        if len(digits) == 8:  # YYYYMMDD
            year = digits[0:4]
            month = digits[4:6]
            day = digits[6:8]
            return f"{year}-{month}-{day}"

        # If already in ISO format
        if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
            return date_str

        self.logger.warning(f"Unrecognized date format: {date_str}")
        return None
