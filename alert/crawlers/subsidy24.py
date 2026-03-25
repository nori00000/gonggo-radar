"""보조금24 API 크롤러 - 보조금 지원사업 공고 수집"""
import json
import os
import hashlib
from typing import List, Optional, Dict, Any
from .base import BaseCrawler
from ..models import RawAnnouncement


class Subsidy24Crawler(BaseCrawler):
    """보조금24 공공데이터 Open API를 통한 보조금 지원사업 공고 수집.

    NOTE: 이 크롤러는 stub 상태입니다. data.go.kr 엔드포인트가 확인되지 않았습니다.
    엔드포인트가 확인되면 BASE_URL을 설정하고 enabled: true로 변경하세요.

    필수 환경변수:
        DATA_GO_KR_API_KEY: data.go.kr Open API 인증키 (없어도 초기화 가능)
    """

    BASE_URL = ""  # TODO: verify data.go.kr endpoint for 보조금24 API

    def __init__(self):
        super().__init__(source_name="subsidy24")
        self.api_key = self._get_api_key()
        self.per_page = self._get_per_page()

    def _get_api_key(self) -> str:
        """Get API key from environment or config.

        Unlike other crawlers, this does NOT raise ValueError if missing,
        because this is a stub crawler with unverified endpoint.

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

        Expected data.go.kr response structure:
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

        Args:
            data: Parsed JSON response

        Returns:
            List of item dictionaries
        """
        try:
            response = data.get("response", {})

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

            # Ensure items is a list
            if not isinstance(items, list):
                items = [items] if items else []

            return items

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
