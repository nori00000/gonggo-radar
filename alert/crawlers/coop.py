"""협동조합 포털 크롤러 - 사회적협동조합 관련 공지 수집"""
import hashlib
import json
import re
from typing import List, Optional
from .base import BaseCrawler
from ..models import RawAnnouncement

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore


class CoopCrawler(BaseCrawler):
    """협동조합 포털(coop.go.kr) 공지사항 크롤러.

    대상 URL:
        - https://www.coop.go.kr/home/boardList.do?brd_mgrno=2&menu_no=2038 (공지사항)

    목록은 정적 HTML 테이블에 포함되어 있고, 상세 링크는
    ``href="javascript:fView('14893')"`` 형태다.
    실제 상세 페이지는 ``/home/boardView.do?brd_mgrno=2&menu_no=2038&brd_no={no}``.
    """

    BASE_URL = "https://www.coop.go.kr"

    LIST_PATH = "/home/boardList.do?brd_mgrno=2&menu_no=2038"
    VIEW_PATH = "/home/boardView.do?brd_mgrno=2&menu_no=2038&brd_no={no}"

    def __init__(self):
        super().__init__(source_name="coop")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """협동조합 포털 공지사항을 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        base_url = self.get_base_url() or self.BASE_URL
        url = f"{base_url}{self.LIST_PATH}"
        self.logger.info(f"Fetching from Coop notice board: {url}")

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
        """공지사항 목록 테이블을 파싱한다.

        Args:
            soup: BeautifulSoup 객체

        Returns:
            공고 딕셔너리 리스트
        """
        items: List[dict] = []
        seen_ids = set()

        for row in soup.select("table tr"):
            title_cell = row.find("td", class_="title")
            if title_cell is None:
                continue

            a_tag = title_cell.find("a")
            if a_tag is None:
                continue

            title = (a_tag.get("title") or a_tag.get_text(strip=True) or "").strip()
            if not title:
                continue

            brd_no = self._extract_brd_no(a_tag.get("href", ""))
            if brd_no and brd_no in seen_ids:
                continue
            if brd_no:
                seen_ids.add(brd_no)

            date_str = ""
            for cell in row.find_all("td", class_="date"):
                text = cell.get_text(strip=True)
                if re.fullmatch(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text):
                    date_str = text
                    break

            items.append({
                "brd_no": brd_no,
                "title": title,
                "date": date_str,
            })

        return items

    @staticmethod
    def _extract_brd_no(raw: str) -> str:
        """``javascript:fView('14893')`` 에서 게시글 번호를 추출한다."""
        if not raw:
            return ""
        match = re.search(r"fView\(\s*['\"]?(\d+)", raw)
        if match:
            return match.group(1)
        match = re.search(r"brd_no=(\d+)", raw)
        if match:
            return match.group(1)
        return ""

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """날짜 문자열을 ISO 형식(YYYY-MM-DD)으로 정규화한다."""
        if not date_str:
            return None

        match = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", date_str)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        digits = "".join(c for c in date_str if c.isdigit())
        if len(digits) == 8:
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"

        return None

    def _to_announcement(self, item: dict, base_url: str) -> Optional[RawAnnouncement]:
        """파싱된 공고 데이터를 RawAnnouncement로 변환한다."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            brd_no = item.get("brd_no", "")
            if brd_no:
                url = f"{base_url}{self.VIEW_PATH.format(no=brd_no)}"
                source_id = brd_no
            else:
                url = f"{base_url}{self.LIST_PATH}"
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            posted = self._normalize_date(item.get("date", ""))
            raw_data = json.dumps(item, ensure_ascii=False)

            return RawAnnouncement(
                source=self.source_name,
                source_id=source_id,
                title=title,
                url=url,
                summary="",
                author="기획재정부 협동조합 포털",
                category="공지",
                target="",
                period_start=posted,
                period_end=None,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
