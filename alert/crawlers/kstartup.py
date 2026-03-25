"""K-Startup API 크롤러 - 창업지원사업 공고 수집"""
import json
import os
import hashlib
from typing import List, Optional, Dict, Any
from .base import BaseCrawler
from ..models import RawAnnouncement


class KStartupCrawler(BaseCrawler):
    """K-Startup 공공데이터 Open API를 통한 창업지원사업 공고 수집.

    API 엔드포인트: https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01
    문서: https://www.data.go.kr/data/15058018/openapi.do

    필수 환경변수:
        DATA_GO_KR_API_KEY: data.go.kr Open API 인증키
    """

    API_ENDPOINT = "https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01"

    def __init__(self):
        super().__init__(source_name="kstartup")
        self.api_key = self._get_api_key()
        self.per_page = self._get_per_page()

    def _get_api_key(self) -> str:
        """Get API key from environment or config.

        Returns:
            API key string, or empty string if not found
        """
        # First try environment variable
        api_key = os.getenv("DATA_GO_KR_API_KEY", "")

        # Then try config (if it was loaded from .env)
        if not api_key:
            source_cfg = self.config.crawler.sources.get("kstartup")
            if source_cfg and hasattr(source_cfg, "api_key"):
                api_key = getattr(source_cfg, "api_key")

        if not api_key:
            self.logger.warning(
                "DATA_GO_KR_API_KEY not found. Set it in alert/.env file. "
                "Crawler will be skipped."
            )

        return api_key

    def _get_per_page(self) -> int:
        """Get the number of items to fetch per request from config.

        Returns:
            Number of items (default 100)
        """
        source_cfg = self.config.crawler.sources.get("kstartup")
        if source_cfg and hasattr(source_cfg, "per_page"):
            return getattr(source_cfg, "per_page")
        return 100

    def fetch(self) -> List[RawAnnouncement]:
        """Fetch announcements from K-Startup API.

        Returns:
            List of RawAnnouncement objects
        """
        if not self.api_key:
            self.logger.warning("kstartup: No API key configured, skipping")
            return []

        announcements = []

        # Prepare API request parameters
        params = {
            "serviceKey": self.api_key,
            "returnType": "json",
            "page": 1,
            "perPage": self.per_page,
        }

        self.logger.debug(f"Fetching from K-Startup API with params: {params}")

        response = self.get(self.API_ENDPOINT, params=params)
        if not response:
            self.logger.error("Failed to fetch from K-Startup API")
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

        The K-Startup API response structure is different from standard data.go.kr format:
        {
            "currentCount": 10,
            "matchCount": 45,
            "totalCount": 45,
            "page": 1,
            "perPage": 10,
            "data": [...]
        }

        Args:
            data: Parsed JSON response

        Returns:
            List of item dictionaries
        """
        try:
            # K-Startup uses "data" key directly
            items = data.get("data", [])

            # Ensure items is a list
            if not isinstance(items, list):
                items = [items] if items else []

            return items

        except Exception as e:
            self.logger.error(f"Error extracting items from response: {e}")
            self.logger.debug(f"Response structure: {json.dumps(data, indent=2, ensure_ascii=False)[:1000]}")
            return []

    def _parse_item(self, item: Dict[str, Any]) -> Optional[RawAnnouncement]:
        """Parse a single item into RawAnnouncement.

        Field mapping:
            pbanc_sn -> source_id (공고일련번호, fallback to hash if empty)
            biz_pbanc_nm -> title (사업공고명)
            detl_pg_url -> url (상세페이지URL, fallback to biz_gdnc_url)
            aply_trgt_ctnt -> summary (신청대상내용, fallback to supt_biz_clsfc)
            sprv_inst -> author (주관기관, fallback to pbanc_ntrp_nm)
            supt_biz_clsfc -> category (지원사업분류)
            aply_trgt -> target (신청대상)
            pbanc_rcpt_bgng_dt -> period_start (접수시작일)
            pbanc_rcpt_end_dt -> period_end (접수마감일)

        Args:
            item: Dictionary containing item data

        Returns:
            RawAnnouncement object, or None if parsing fails
        """
        try:
            # Required fields
            title = str(item.get("biz_pbanc_nm", "")).strip()

            if not title:
                self.logger.warning(f"Item missing title: {item}")
                return None

            # Source ID with fallback to hash
            source_id = str(item.get("pbanc_sn", "")).strip()
            if not source_id:
                # Generate hash from title and period
                period_start_raw = str(item.get("pbanc_rcpt_bgng_dt", "")).strip()
                period_end_raw = str(item.get("pbanc_rcpt_end_dt", "")).strip()
                hash_input = f"{title}_{period_start_raw}_{period_end_raw}"
                source_id = hashlib.md5(hash_input.encode()).hexdigest()[:16]
                self.logger.debug(f"Generated source_id from hash: {source_id}")

            # URL with fallback
            url = str(item.get("detl_pg_url", "")).strip()
            if not url:
                url = str(item.get("biz_gdnc_url", "")).strip()

            # Optional fields
            summary = str(item.get("aply_trgt_ctnt", "")).strip()
            if not summary:
                summary = str(item.get("supt_biz_clsfc", "")).strip()

            author = str(item.get("sprv_inst", "")).strip()
            if not author:
                author = str(item.get("pbanc_ntrp_nm", "")).strip()

            category = str(item.get("supt_biz_clsfc", "")).strip()
            target = str(item.get("aply_trgt", "")).strip()

            # Parse period dates (format: YYYYMMDD)
            period_start_raw = str(item.get("pbanc_rcpt_bgng_dt", "")).strip()
            period_end_raw = str(item.get("pbanc_rcpt_end_dt", "")).strip()

            period_start = self._normalize_date(period_start_raw)
            period_end = self._normalize_date(period_end_raw)

            # Store full item as raw_data
            raw_data = json.dumps(item, ensure_ascii=False)

            announcement = RawAnnouncement(
                source="kstartup",
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

        Args:
            date_str: Date in format YYYYMMDD

        Returns:
            Date in ISO format, or None if invalid
        """
        if not date_str:
            return None

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
