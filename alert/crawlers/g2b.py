"""나라장터(G2B) API 크롤러 - 입찰 공고 수집"""
import json
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from .base import BaseCrawler
from ..models import RawAnnouncement


class G2bCrawler(BaseCrawler):
    """나라장터(G2B) 공공데이터 Open API를 통한 입찰 공고 수집.

    API 엔드포인트: http://apis.data.go.kr/1230000/BidPublicInfoService
    문서: https://www.data.go.kr/data/15012005/openapi.do

    필수 환경변수:
        DATA_GO_KR_API_KEY: 공공데이터포털 API 인증키

    쿼리하는 공고 유형:
        1. 용역(Services): getBidPblancListInfoServc
        2. 물품(Goods): getBidPblancListInfoThng
        3. 공사(Construction): getBidPblancListInfoCnstwk
    """

    API_BASE_URL = "http://apis.data.go.kr/1230000/BidPublicInfoService"

    OPERATIONS = {
        "용역": "/getBidPblancListInfoServc",
        "물품": "/getBidPblancListInfoThng",
        "공사": "/getBidPblancListInfoCnstwk",
    }

    def __init__(self):
        super().__init__(source_name="g2b")
        self.api_key = self._get_api_key()
        self.lookback_days = self._get_lookback_days()

    def _get_api_key(self) -> str:
        """Get API key from environment or config.

        Returns:
            API key string

        Raises:
            ValueError: If API key is not found
        """
        # First try environment variable
        api_key = os.getenv("DATA_GO_KR_API_KEY", "")

        # Then try config (if it was loaded from .env)
        if not api_key:
            source_cfg = self.config.crawler.sources.get("g2b")
            if source_cfg and hasattr(source_cfg, "api_key"):
                api_key = getattr(source_cfg, "api_key")

        if not api_key:
            raise ValueError(
                "DATA_GO_KR_API_KEY not found in environment or config. "
                "Please set it in .env file."
            )

        return api_key

    def _get_lookback_days(self) -> int:
        """Get the number of days to look back for announcements.

        Returns:
            Number of days (default 7)
        """
        source_cfg = self.config.crawler.sources.get("g2b")
        if source_cfg and hasattr(source_cfg, "lookback_days"):
            return getattr(source_cfg, "lookback_days")
        return 7

    def fetch(self) -> List[RawAnnouncement]:
        """Fetch announcements from G2B API.

        Queries all 3 operation types (용역/물품/공사) and combines results.

        Returns:
            List of RawAnnouncement objects
        """
        announcements = []

        # Calculate date range (default: last 7 days)
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=self.lookback_days)

        start_dt_str = start_dt.strftime("%Y%m%d%H%M")
        end_dt_str = end_dt.strftime("%Y%m%d%H%M")

        self.logger.info(f"Fetching G2B announcements from {start_dt_str} to {end_dt_str}")

        # Query each operation type
        for category, operation in self.OPERATIONS.items():
            self.logger.debug(f"Querying {category} announcements...")

            items = self._fetch_operation(
                operation=operation,
                category=category,
                start_dt=start_dt_str,
                end_dt=end_dt_str
            )

            self.logger.info(f"Found {len(items)} {category} items")

            # Convert each item to RawAnnouncement
            for item in items:
                announcement = self._parse_item(item, category)
                if announcement:
                    announcements.append(announcement)

        return announcements

    def _fetch_operation(
        self,
        operation: str,
        category: str,
        start_dt: str,
        end_dt: str
    ) -> List[Dict[str, Any]]:
        """Fetch items from a specific operation endpoint.

        Args:
            operation: API operation path (e.g., "/getBidPblancListInfoServc")
            category: Category name (용역/물품/공사)
            start_dt: Start datetime in YYYYMMDDHHmm format
            end_dt: End datetime in YYYYMMDDHHmm format

        Returns:
            List of item dictionaries
        """
        url = f"{self.API_BASE_URL}{operation}"

        params = {
            'serviceKey': self.api_key,
            'inqryDiv': '1',
            'type': 'json',
            'inqryBgnDt': start_dt,
            'inqryEndDt': end_dt,
            'pageNo': '1',
            'numOfRows': '100',
        }

        response = self.get(url, params=params)
        if not response:
            self.logger.error(f"Failed to fetch from G2B API ({category})")
            return []

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse JSON response ({category}): {e}")
            self.logger.debug(f"Response text: {response.text[:500]}")
            return []

        # Extract items from response
        items = self._extract_items(data)
        return items

    def _extract_items(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract items array from API response.

        Expected structure:
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

            if result_code != "00":
                self.logger.warning(f"API returned non-success code: {result_code} - {result_msg}")
                return []

            body = response.get("body", {})
            items = body.get("items", [])

            # Ensure items is a list
            if not isinstance(items, list):
                items = [items] if items else []

            return items

        except Exception as e:
            self.logger.error(f"Error extracting items from response: {e}")
            self.logger.debug(f"Response structure: {json.dumps(data, indent=2, ensure_ascii=False)[:1000]}")
            return []

    def _parse_item(self, item: Dict[str, Any], category: str) -> Optional[RawAnnouncement]:
        """Parse a single item into RawAnnouncement.

        Field mapping:
            bidNtceNo -> source_id (입찰공고번호)
            bidNtceNm -> title (입찰공고명)
            bidNtceUrl -> url (입찰공고URL)
            ntceKindNm + presmptPrce -> summary (공고종류명 + 추정가격)
            ntceInsttNm -> author (공고기관명)
            category (용역/물품/공사) -> category
            dminsttNm -> target (수요기관명)
            bidBeginDt -> period_start (입찰시작일시)
            bidClseDt -> period_end (입찰마감일시)

        Args:
            item: Dictionary containing item data
            category: Category type (용역/물품/공사)

        Returns:
            RawAnnouncement object, or None if parsing fails
        """
        try:
            # Required fields
            source_id = str(item.get("bidNtceNo", "")).strip()
            title = str(item.get("bidNtceNm", "")).strip()

            if not source_id or not title:
                self.logger.warning(f"Item missing required fields: {item}")
                return None

            # URL - use provided URL or construct from notice number
            url = str(item.get("bidNtceUrl", "")).strip()
            if not url:
                # Construct URL if not provided
                url = f"http://www.g2b.go.kr:8081/ep/invitation/publish/bidInfoDtl.do?bidno={source_id}"

            # Summary - combine notice kind and estimated price
            ntce_kind = str(item.get("ntceKindNm", "")).strip()
            presmp_price = str(item.get("presmptPrce", "")).strip()

            summary_parts = []
            if ntce_kind:
                summary_parts.append(f"공고종류: {ntce_kind}")
            if presmp_price:
                summary_parts.append(f"추정가격: {presmp_price}")
            summary = " | ".join(summary_parts)

            # Author (announcement institution)
            author = str(item.get("ntceInsttNm", "")).strip()

            # Target (demand institution)
            target = str(item.get("dminsttNm", "")).strip()

            # Parse dates (YYYYMMDDHHmm format)
            bid_begin = str(item.get("bidBeginDt", "")).strip()
            bid_close = str(item.get("bidClseDt", "")).strip()

            period_start = self._parse_datetime(bid_begin)
            period_end = self._parse_datetime(bid_close)

            # Store full item as raw_data
            raw_data = json.dumps(item, ensure_ascii=False)

            announcement = RawAnnouncement(
                source="g2b",
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

    def _parse_datetime(self, dt_str: str) -> Optional[str]:
        """Parse datetime string from YYYYMMDDHHmm format to ISO format (YYYY-MM-DD).

        Args:
            dt_str: DateTime in format YYYYMMDDHHmm (e.g., "202603010000")

        Returns:
            Date in ISO format (YYYY-MM-DD), or None if invalid
        """
        if not dt_str:
            return None

        # Remove any non-digit characters
        digits = "".join(c for c in dt_str if c.isdigit())

        if len(digits) >= 8:  # At least YYYYMMDD
            year = digits[0:4]
            month = digits[4:6]
            day = digits[6:8]
            return f"{year}-{month}-{day}"

        self.logger.warning(f"Unrecognized datetime format: {dt_str}")
        return None
