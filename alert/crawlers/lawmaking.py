"""국민참여입법센터 크롤러 - 산림청 소관 입법예고 수집"""
import hashlib
import json
import re
from typing import List, Optional
from .base import BaseCrawler
from .date_labels import posted_date
from ..models import RawAnnouncement

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore


class LawmakingCrawler(BaseCrawler):
    """국민참여입법센터(opinion.lawmaking.go.kr) 산림청 소관 입법예고 크롤러.

    대상 URL:
        - https://opinion.lawmaking.go.kr/gcom/ogLmPp?cptOfiOrgCd=1400000&isOgYn=Y&opYn=Y

    ``cptOfiOrgCd=1400000`` 이 산림청 소관 필터다.
    목록 테이블의 각 행은 ``data-th`` 속성으로 컬럼 의미를 표시하므로
    이를 기준으로 법령제명/소관부처/법령분야/의견접수기간을 추출한다.
    """

    BASE_URL = "https://opinion.lawmaking.go.kr"

    LIST_PATH = "/gcom/ogLmPp?cptOfiOrgCd=1400000&isOgYn=Y&opYn=Y"

    def __init__(self):
        super().__init__(source_name="lawmaking")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """산림청 소관 입법예고 목록을 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        base_url = self.get_base_url() or self.BASE_URL
        url = f"{base_url}{self.LIST_PATH}"
        self.logger.info(f"Fetching from Lawmaking notice board: {url}")

        response = self.get(url)
        if response is None:
            self.logger.error(f"Failed to fetch board listing from {url}")
            return []

        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        items = self.parse_list(soup)
        if not items:
            self.logger.warning(
                f"Could not parse board listing from {url}. "
                "HTML structure may have changed."
            )
            return []

        self.logger.info(f"Parsed {len(items)} items from {url}")

        announcements: List[RawAnnouncement] = []
        for item in items:
            announcement = self._to_announcement(item, base_url)
            if announcement:
                announcements.append(announcement)
        return self.enrich_with_quotes(announcements)

    def parse_list(self, soup: "BeautifulSoup") -> List[dict]:
        """입법예고 목록 테이블을 파싱한다.

        Args:
            soup: BeautifulSoup 객체

        Returns:
            입법예고 딕셔너리 리스트
        """
        items: List[dict] = []

        for row in soup.select("table tr"):
            subject_cell = row.find("td", class_="subject")
            if subject_cell is None:
                continue

            a_tag = subject_cell.find("a")
            if a_tag is None:
                continue

            title = (a_tag.get("title") or a_tag.get_text(strip=True) or "").strip()
            if not title:
                continue

            link = a_tag.get("href", "")

            organ = self._cell_text(row, "소관부처")
            field = self._cell_text(row, "법령분야")
            period = self._cell_text(row, "입법의견 접수기간", separator=" ")

            # 소관부처 셀은 "산림청 (대통령령)" 형태 - 부처와 법령종류를 분리
            author = organ
            law_type = ""
            type_match = re.search(r"\(([^)]+)\)", organ)
            if type_match:
                law_type = type_match.group(1).strip()
                author = organ[: type_match.start()].strip()

            items.append({
                "title": title,
                "link": link,
                "author": author,
                "law_type": law_type,
                "field": field,
                "period": period,
            })

        return items

    @staticmethod
    def _cell_text(row, data_th_prefix: str, separator: str = "") -> str:
        """``data-th`` 속성 접두사로 셀을 찾아 텍스트를 반환한다."""
        for cell in row.find_all("td"):
            data_th = cell.get("data-th", "")
            if data_th and data_th.startswith(data_th_prefix):
                return cell.get_text(separator, strip=True).strip()
        return ""

    def _extract_law_id(self, link: str) -> str:
        """상세 URL(``/gcom/ogLmPp/88388?...``)에서 입법예고 ID를 추출한다."""
        if not link:
            return ""
        match = re.search(r"/ogLmPp/(\d+)", link)
        if match:
            return match.group(1)
        match = re.search(r"/(\d{3,})(?:[?#]|$)", link)
        if match:
            return match.group(1)
        return hashlib.md5(link.encode("utf-8")).hexdigest()[:16]

    def _normalize_url(self, link: str, base_url: str) -> str:
        """상대 URL을 절대 URL로 변환한다."""
        if not link:
            return ""
        if link.startswith("http://") or link.startswith("https://"):
            return link
        if link.startswith("//"):
            return f"https:{link}"
        if link.startswith("/"):
            return f"{base_url}{link}"
        return f"{base_url}/{link}"

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """날짜 문자열을 ISO 형식(YYYY-MM-DD)으로 정규화한다.

        이 사이트는 "2026. 9. 7." 처럼 구분자 뒤에 공백이 오는 형식을 쓴다.
        """
        if not date_str:
            return None

        match = re.search(
            r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})", date_str
        )
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        digits = "".join(c for c in date_str if c.isdigit())
        if len(digits) == 8:
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"

        return None

    def _parse_period(self, period_str: str) -> tuple[Optional[str], Optional[str]]:
        """의견 접수기간 "2026. 9. 7. ~2026. 10. 19." 를 시작/종료일로 분리한다."""
        if not period_str:
            return None, None

        parts = re.split(r"[~∼]", period_str)
        if len(parts) >= 2:
            return (
                self._normalize_date(parts[0]),
                self._normalize_date(parts[1]),
            )

        single = self._normalize_date(period_str)
        return single, single

    def _to_announcement(self, item: dict, base_url: str) -> Optional[RawAnnouncement]:
        """파싱된 입법예고 데이터를 RawAnnouncement로 변환한다."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            link = self._normalize_url(item.get("link", ""), base_url)
            source_id = self._extract_law_id(item.get("link", ""))
            if not source_id:
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            # 기간은 크롤러가 만들지 않는다 - DB 도달 직전 관문
            # (``alert.main._finalize_periods`` → ``lawmaking_period``)이
            # 정한다. 목록 셀은 raw_data 증거로만 남긴다.
            posted = posted_date(item.get("period", ""))

            category_parts = [
                p for p in ["입법예고", item.get("law_type", ""), item.get("field", "")]
                if p
            ]
            category = "/".join(dict.fromkeys(category_parts))

            payload = dict(item)
            if posted:
                payload["posted"] = posted
            raw_data = json.dumps(payload, ensure_ascii=False)

            return RawAnnouncement(
                source=self.source_name,
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author=item.get("author", "").strip() or "산림청",
                category=category,
                target="",
                period_start=None,
                period_end=None,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
