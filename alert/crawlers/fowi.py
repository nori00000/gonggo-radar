"""한국산림복지진흥원 크롤러 - 한국산림복지진흥원 지원사업 공고 수집"""
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


class FowiCrawler(BaseCrawler):
    """한국산림복지진흥원(fowi.or.kr) 공고 게시판 크롤러.

    대상 URL:
        - https://fowi.or.kr/user/bbs/bbsList.do?bbsManageId=12 (공지사항)
        - https://fowi.or.kr/user/bbs/bbsList.do?bbsManageId=25 (입찰공고)

    HTML 파싱 방식:
        GET 요청을 통해 공고 목록 페이지를 가져오고, 테이블 또는 리스트 구조를
        파싱하여 공고 정보를 추출한다.
    """

    BASE_URL = "https://fowi.or.kr"
    BOARD_PATHS = [
        "/user/bbs/bbsList.do?bbsManageId=12",  # 공지사항
        "/user/bbs/bbsList.do?bbsManageId=25",   # 입찰공고
    ]

    def __init__(self):
        super().__init__(source_name="fowi")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """한국산림복지진흥원 공고 게시판에서 공고를 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL

        for board_path in self.BOARD_PATHS:
            url = f"{base_url}{board_path}"
            self.logger.info(f"Fetching from Fowi announcement board: {url}")

            items = self._fetch_board_listing(url)
            if items:
                self.logger.info(f"Successfully fetched {len(items)} items from {url}")
                for item in items:
                    announcement = self._to_announcement(item, base_url)
                    if announcement:
                        announcements.append(announcement)
                break

        return announcements

    def _fetch_board_listing(self, url: str) -> List[dict]:
        """공고 목록 페이지를 파싱하여 공고 목록을 추출한다."""
        response = self.get(url)
        if response is None:
            self.logger.error(f"Failed to fetch board listing from {url}")
            return []

        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        items: List[dict] = []

        # 전략 1: table 기반 게시판
        items = self._parse_table_board(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using table strategy")
            return items

        # 전략 2: div/ul 기반 게시판
        items = self._parse_list_board(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using list strategy")
            return items

        # 전략 3: 범용 링크 추출
        items = self._parse_generic_links(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using generic link strategy")
            return items

        self.logger.warning(
            f"Could not parse board listing from {url}. "
            "HTML structure may have changed."
        )
        return []

    def _parse_table_board(self, soup: "BeautifulSoup") -> List[dict]:
        """table 기반 게시판 파싱."""
        items = []

        table = None
        for selector in [
            "table.board_list", "table.board-list", "table.tbl_list",
            "table.list_table", "table.tbl_board", "table.bbsList",
            "table.table_list", "table.list_tbl",
        ]:
            table = soup.select_one(selector)
            if table:
                break

        if table is None:
            tables = soup.find_all("table")
            max_rows = 0
            for t in tables:
                tbody = t.find("tbody")
                if tbody:
                    rows = tbody.find_all("tr")
                    if len(rows) > max_rows:
                        max_rows = len(rows)
                        table = t

        if table is None:
            return []

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            title_link = None
            title_text = ""
            author = ""
            category = ""
            date_str = ""

            for cell in cells:
                css_class = " ".join(cell.get("class", []))

                if any(kw in css_class for kw in ["title", "subject", "sbj"]):
                    a_tag = cell.find("a")
                    if a_tag:
                        title_link = a_tag.get("href", "")
                        title_text = a_tag.get_text(strip=True)

                elif any(kw in css_class for kw in ["author", "writer", "organ", "dept"]):
                    author = cell.get_text(strip=True)

                elif any(kw in css_class for kw in ["category", "cate", "type", "kind"]):
                    category = cell.get_text(strip=True)

                elif any(kw in css_class for kw in ["date", "period", "term"]):
                    date_str = cell.get_text(strip=True)

            if not title_text:
                for cell in cells:
                    a_tag = cell.find("a")
                    if a_tag and a_tag.get_text(strip=True):
                        title_link = a_tag.get("href", "")
                        title_text = a_tag.get_text(strip=True)
                        break

            if not title_text:
                continue

            if not date_str:
                for cell in cells:
                    cell_text = cell.get_text(strip=True)
                    if re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", cell_text):
                        date_str = cell_text
                        break

            items.append({
                "title": title_text,
                "link": title_link or "",
                "author": author,
                "category": category,
                "date": date_str,
            })

        return items

    def _parse_list_board(self, soup: "BeautifulSoup") -> List[dict]:
        """div/ul 기반 게시판 파싱."""
        items = []

        container = None
        for selector in [
            "div.board_list", "div.board-list", "div.list_area",
            "div.list_wrap", "ul.board_list", "ul.list_area",
        ]:
            container = soup.select_one(selector)
            if container:
                break

        if container is None:
            return []

        list_items = container.select("li, div.item, div.list_item")
        if not list_items:
            list_items = container.find_all("div", recursive=False)

        for item_elem in list_items:
            a_tag = item_elem.find("a")
            if not a_tag or not a_tag.get_text(strip=True):
                continue

            title_text = a_tag.get_text(strip=True)
            link = a_tag.get("href", "")

            author = ""
            author_elem = item_elem.find(
                ["span", "em", "div"],
                class_=re.compile(r"author|writer|organ|dept", re.I)
            )
            if author_elem:
                author = author_elem.get_text(strip=True)

            category = ""
            cat_elem = item_elem.find(
                ["span", "em"],
                class_=re.compile(r"cate|category|type|badge|label", re.I)
            )
            if cat_elem:
                category = cat_elem.get_text(strip=True)

            date_str = ""
            date_elem = item_elem.find(
                ["span", "em", "div"],
                class_=re.compile(r"date|period|term|time", re.I)
            )
            if date_elem:
                date_str = date_elem.get_text(strip=True)
            else:
                text = item_elem.get_text()
                date_match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
                if date_match:
                    date_str = date_match.group()

            items.append({
                "title": title_text,
                "link": link,
                "author": author,
                "category": category,
                "date": date_str,
            })

        return items

    def _parse_generic_links(self, soup: "BeautifulSoup") -> List[dict]:
        """범용 링크 추출 (폴백 전략)."""
        items = []
        seen_links = set()

        view_patterns = [
            re.compile(r"boardView", re.I),
            re.compile(r"view\.do", re.I),
            re.compile(r"nttId=", re.I),
            re.compile(r"detail", re.I),
        ]

        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            title_text = a_tag.get_text(strip=True)

            if not title_text or len(title_text) < 5:
                continue

            is_view_link = any(p.search(href) for p in view_patterns)
            if not is_view_link:
                continue

            if href in seen_links:
                continue
            seen_links.add(href)

            items.append({
                "title": title_text,
                "link": href,
                "author": "",
                "category": "",
                "date": "",
            })

        return items

    def _extract_post_id(self, link: str) -> str:
        """URL에서 공고 ID를 추출한다."""
        if not link:
            return ""

        id_params = [
            r"announcementId=(\d+)", r"notifyId=(\d+)", r"nttId=(\d+)",
            r"seq=(\d+)", r"idx=(\d+)", r"no=(\d+)",
            r"articleId=(\d+)", r"artclId=(\d+)",
        ]
        for pattern in id_params:
            match = re.search(pattern, link, re.I)
            if match:
                return match.group(1)

        path_match = re.search(r"/(\d{3,})", link)
        if path_match:
            return path_match.group(1)

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
        """날짜 문자열을 ISO 형식(YYYY-MM-DD)으로 정규화한다."""
        if not date_str:
            return None

        match = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", date_str)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        digits = "".join(c for c in date_str if c.isdigit())
        if len(digits) == 8:
            year = digits[0:4]
            month = digits[4:6]
            day = digits[6:8]
            return f"{year}-{month}-{day}"

        self.logger.warning(f"Unrecognized date format: {date_str}")
        return None

    def _parse_period(self, period_str: str) -> tuple[Optional[str], Optional[str]]:
        """기간 문자열을 시작일과 종료일로 파싱한다."""
        if not period_str:
            return None, None

        try:
            if "~" in period_str or "\u223c" in period_str:
                parts = re.split(r"[~\u223c]", period_str)
                if len(parts) == 2:
                    start = self._normalize_date(parts[0].strip())
                    end = self._normalize_date(parts[1].strip())
                    return start, end

            normalized = self._normalize_date(period_str.strip())
            return normalized, normalized

        except Exception as e:
            self.logger.warning(f"Failed to parse period '{period_str}': {e}")
            return None, None

    def _to_announcement(self, item: dict, base_url: str) -> Optional[RawAnnouncement]:
        """파싱된 공고 데이터를 RawAnnouncement로 변환한다."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            link = self._normalize_url(item.get("link", ""), base_url)
            source_id = self._extract_post_id(link)

            if not source_id:
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            author = item.get("author", "").strip()
            category = item.get("category", "").strip()

            date_str = item.get("date", "").strip()
            period_start, period_end = self._parse_period(date_str)

            raw_data = json.dumps(item, ensure_ascii=False)

            return RawAnnouncement(
                source="fowi",
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author=author or "한국산림복지진흥원",
                category=category,
                target="",
                period_start=period_start,
                period_end=period_end,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
