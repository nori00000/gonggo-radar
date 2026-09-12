"""비즈인포 API 크롤러 - 정부 지원사업 공고 수집"""
import json
import os
from typing import List, Optional, Dict, Any
from .base import BaseCrawler
from .identity import clean_text
from ..models import RawAnnouncement


class BizinfoCrawler(BaseCrawler):
    """비즈인포 공공데이터 Open API를 통한 정부 지원사업 공고 수집.

    API 엔드포인트: https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do
    문서: https://www.bizinfo.go.kr/web/lay1/program/S1T122C128/openapi/openApiInfo.do

    필수 환경변수:
        BIZINFO_API_KEY: 비즈인포 Open API 인증키
    """

    API_ENDPOINT = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"

    # ``detailUrl`` 이 비어 오는 항목의 **알림 링크**를 만드는 템플릿.
    # 식별자에는 쓰지 않는다 (식별은 API 가 준 ``pblancId``).
    DETAIL_URL_TEMPLATE = (
        "https://www.bizinfo.go.kr/web/lay1/bbs/S1T122C128/AS/74/view.do"
        "?pblancId="
    )

    def __init__(self):
        super().__init__(source_name="bizinfo")
        self.api_key = self._get_api_key()
        self.search_cnt = self._get_search_count()

    def _get_api_key(self) -> str:
        """Get API key from environment or config.

        Returns:
            API key string, or empty string if not found
        """
        # First try environment variable
        api_key = os.getenv("BIZINFO_API_KEY", "")

        # Then try config (if it was loaded from .env)
        if not api_key:
            source_cfg = self.config.crawler.sources.get("bizinfo")
            if source_cfg and hasattr(source_cfg, "api_key"):
                api_key = getattr(source_cfg, "api_key")

        if not api_key:
            self.logger.warning(
                "BIZINFO_API_KEY not found. Set it in alert/.env file. "
                "Crawler will be skipped."
            )

        return api_key

    def _get_search_count(self) -> int:
        """Get the number of items to fetch per request from config.

        Returns:
            Number of items (default 50)
        """
        source_cfg = self.config.crawler.sources.get("bizinfo")
        if source_cfg and hasattr(source_cfg, "search_cnt"):
            return getattr(source_cfg, "search_cnt")
        return 50

    def fetch(self) -> List[RawAnnouncement]:
        """Fetch announcements from Bizinfo API.

        Returns:
            List of RawAnnouncement objects
        """
        if not self.api_key:
            self.logger.warning("bizinfo: No API key configured, skipping")
            return []

        announcements = []

        # Prepare API request parameters
        params = {
            "serviceKey": self.api_key,
            "dataType": "json",
            "searchCnt": self.search_cnt,
        }

        self.logger.debug(f"Fetching from Bizinfo API with params: {params}")

        response = self.get(self.API_ENDPOINT, params=params)
        if not response:
            self.logger.error("Failed to fetch from Bizinfo API")
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

        The Bizinfo API response structure varies, so we handle multiple formats.

        Typical structure:
        {
            "response": {
                "header": { ... },
                "body": {
                    "items": { "item": [...] },
                    "numOfRows": 50,
                    "pageNo": 1,
                    "totalCount": 1234
                }
            }
        }

        Args:
            data: Parsed JSON response

        Returns:
            List of item dictionaries
        """
        # Try standard OpenAPI format
        try:
            response = data.get("response", {})
            body = response.get("body", {})
            items_wrapper = body.get("items", {})

            # Items can be a dict with "item" key or directly a list
            if isinstance(items_wrapper, dict):
                items = items_wrapper.get("item", [])
            elif isinstance(items_wrapper, list):
                items = items_wrapper
            else:
                items = []

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
            pblancId -> source_id (공고 ID)
            pblancNm -> title (공고명)
            detailUrl -> url (상세 URL)
            sbjctCn -> summary (사업목적/내용)
            jrsdInsttNm -> author (주관기관)
            pldirSportRealmLclasCodeNm -> category (정책분야 대분류)
            trgetNm -> target (지원대상)
            reqstBeginEndDe -> period (접수기간)

        Args:
            item: Dictionary containing item data

        Returns:
            RawAnnouncement object, or None if parsing fails
        """
        try:
            # Required fields
            source_id = clean_text(item.get("pblancId"))
            title = clean_text(item.get("pblancNm"))
            # ``str(None)`` 이 문자열 "None" 을 만들어, URL 이 없는 서로 다른
            # 공고가 같은 URL 로 합쳐졌다 (13차 게이트 HIGH).
            url = clean_text(item.get("detailUrl"))

            # 14차 게이트: ``pblancId`` 가 없어도 **고유 상세 URL** 이 있으면
            # 공고를 버리지 않는다 - 식별은 그 URL 이 맡는다.
            if not title or not (source_id or clean_text(item.get("detailUrl"))):
                self.logger.warning(f"Item missing required fields: {item}")
                return None

            # Optional fields
            summary = str(item.get("sbjctCn", "")).strip()
            author = str(item.get("jrsdInsttNm", "")).strip()
            category = str(item.get("pldirSportRealmLclasCodeNm", "")).strip()
            target = str(item.get("trgetNm", "")).strip()

            # Parse period (format: YYYYMMDD~YYYYMMDD or YYYYMMDD)
            period_raw = str(item.get("reqstBeginEndDe", "")).strip()
            period_start, period_end = self._parse_period(period_raw)

            # URL 이 없으면 **알림 링크용**으로만 상세 URL 템플릿을 만든다.
            # 식별에는 쓰지 않는다 - 식별은 API 가 준 ``pblancId`` 다.
            payload = dict(item)
            if not url and source_id:
                url = f"{self.DETAIL_URL_TEMPLATE}{source_id}"
                payload["url_is_template"] = True

            raw_data = json.dumps(payload, ensure_ascii=False)

            announcement = RawAnnouncement(
                source="bizinfo",
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

    def _parse_period(self, period_str: str) -> tuple[Optional[str], Optional[str]]:
        """Parse period string into start and end dates.

        Expected formats:
            - "YYYYMMDD~YYYYMMDD" (start and end)
            - "YYYYMMDD" (single date, used as both start and end)
            - "YYYY-MM-DD~YYYY-MM-DD" (with hyphens)

        Args:
            period_str: Period string from API

        Returns:
            Tuple of (start_date, end_date) in ISO format, or (None, None)
        """
        if not period_str:
            return None, None

        try:
            # Handle tilde separator
            if "~" in period_str:
                parts = period_str.split("~")
                if len(parts) == 2:
                    start = self._normalize_date(parts[0].strip())
                    end = self._normalize_date(parts[1].strip())
                    return start, end

            # Single date
            normalized = self._normalize_date(period_str.strip())
            return normalized, normalized

        except Exception as e:
            self.logger.warning(f"Failed to parse period '{period_str}': {e}")
            return None, None

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """Normalize date string to ISO format (YYYY-MM-DD).

        Args:
            date_str: Date in format YYYYMMDD or YYYY-MM-DD

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
