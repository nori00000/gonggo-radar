"""농림수산식품교육문화정보원 크롤러 - 공고/게시판 수집"""
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


class EpisCrawler(BaseCrawler):
    """농림수산식품교육문화정보원(epis.or.kr) 공고 게시판 크롤러.

    대상 URL (2026-09-15 교체, P2-S 계약 §2):
        - https://www.epis.or.kr/bbs/list.do?key=2604210075 (공지사항)
        - https://www.epis.or.kr/bbs/list.do?key=2604210073 (입찰/공모)

    옛 경로 ``/home/kor/M373320876/board.do`` 는 사이트 개편으로 사라졌다.
    새 게시판은 ``table.table_basics_area`` 정적 HTML 이고, 제목 링크의
    href 는 ``javascript:void(0);`` 이며 실제 이동은
    ``onclick="goView('<pstSn>')"`` 가 ``/bbs/view.do?key=…&pstSn=…`` 로
    폼 전송한다 - href 기반 전략으로는 게시글을 하나도 찾을 수 없으므로
    전용 전략(``_parse_goview_board``)이 먼저 돈다.

    기간: epis 는 ``PERIOD_EXTRACTORS`` 에 없다 = 기간 두 필드가 항상
    None 이다. 그래서 새 전략은 목록 날짜를 ``date`` 로 올리지 않고
    ``posted`` 로만 싣는다 - 게시일이 접수기간으로 둔갑하지 않게.
    """

    BASE_URL = "https://www.epis.or.kr"
    BOARD_PATHS = [
        "/bbs/list.do?key=2604210075",   # 공지사항
        "/bbs/list.do?key=2604210073",   # 입찰/공모
    ]

    # goView('2609080002') -> /bbs/view.do?key=<board>&pstSn=2609080002
    GOVIEW_PATTERN = re.compile(r"goView\(\s*'?(\w+)'?\s*\)", re.I)
    BOARD_KEY_PATTERN = re.compile(r"[?&]key=(\d+)", re.I)

    def __init__(self):
        super().__init__(source_name="epis")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """농림수산식품교육문화정보원 공고 게시판에서 공고를 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL

        for board_path in self.BOARD_PATHS:
            url = f"{base_url}{board_path}"
            self.logger.info(f"Fetching from EPIS announcement board: {url}")

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
        # GET 요청 (page 파라미터 포함)
        params = {"page": "1"}
        response = self.get(url, params=params)

        if response is None:
            # 파라미터 없이 재시도
            response = self.get(url)

        if response is None:
            self.logger.error(f"Failed to fetch board listing from {url}")
            return []

        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        items: List[dict] = []

        # 전략 0: 개편된 epis 게시판 (onclick goView)
        items = self._parse_goview_board(soup, self._extract_board_key(url))
        if items:
            self.logger.info(f"Parsed {len(items)} items using goView strategy")
            return items

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

    @classmethod
    def _extract_board_key(cls, url: str) -> str:
        """목록 URL 에서 게시판 ``key`` 를 뽑는다 (본문 URL 조립에 쓴다)."""
        match = cls.BOARD_KEY_PATTERN.search(url or "")
        return match.group(1) if match else ""

    def _parse_goview_board(
        self, soup: "BeautifulSoup", board_key: str
    ) -> List[dict]:
        """개편된 epis 게시판(``table.table_basics_area``)을 파싱한다.

        칸을 **클래스로만** 고른다. 입찰/공모 게시판에는 등록일 칸이 없고
        기간 칸(``2026-09-07 ~ 2026-09-18``)만 있는데, 칸 순서나 "날짜처럼
        보이는 첫 칸" 으로 읽으면 그 기간이 게시일이 된다. ``td.date`` 만
        게시일로 인정하고, 기간 문자열은 ``period_text`` 근거로만 남긴다.

        Args:
            soup: 목록 페이지
            board_key: ``/bbs/list.do?key=…`` 의 게시판 키

        Returns:
            공고 항목 목록 (``date`` 는 항상 빈 문자열 - 기간을 만들지 않는다)
        """
        items: List[dict] = []
        seen_ids = set()

        for row in soup.select("table.table_basics_area tbody tr"):
            title_cell = row.find("td", class_="tit")
            if title_cell is None:
                continue
            a_tag = title_cell.find("a")
            if a_tag is None:
                continue

            match = self.GOVIEW_PATTERN.search(a_tag.get("onclick", "") or "")
            if not match:
                continue
            pst_sn = match.group(1)
            if pst_sn in seen_ids:
                continue
            seen_ids.add(pst_sn)

            title = a_tag.get_text(" ", strip=True)
            if not title:
                continue

            sort_cell = row.find("td", class_="sort")
            date_cell = row.find("td", class_="date")

            link = f"/bbs/view.do?pstSn={pst_sn}"
            if board_key:
                link = f"/bbs/view.do?key={board_key}&pstSn={pst_sn}"

            item = {
                "title": title,
                "link": link,
                "author": "",
                "category": sort_cell.get_text(strip=True) if sort_cell else "",
                # 기간을 만들 수 있는 소스가 아니다 - 날짜를 date 로 올리지
                # 않는다 (``_to_announcement`` 가 date 로 기간을 만든다)
                "date": "",
                "date_label": "등록일",
            }
            posted = posted_date(
                date_cell.get_text(strip=True) if date_cell else ""
            )
            if posted:
                item["posted"] = posted

            period_text = self._row_period_text(row)
            if period_text:
                item["period_text"] = period_text

            items.append(item)

        return items

    @staticmethod
    def _row_period_text(row) -> str:
        """행에서 기간 문자열(``… ~ …``)을 근거로만 읽는다."""
        for cell in row.find_all("td"):
            if cell.get("class"):
                continue
            text = cell.get_text(" ", strip=True)
            if "~" in text and re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text):
                return text
        return ""

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
            re.compile(r"board/read", re.I),
            re.compile(r"boardNo=", re.I),
            re.compile(r"view", re.I),
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
            r"pstSn=(\w+)",
            r"boardNo=(\d+)", r"announcementId=(\d+)", r"notifyId=(\d+)",
            r"nttId=(\d+)", r"seq=(\d+)", r"idx=(\d+)", r"no=(\d+)",
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
                source="epis",
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author=author or "농림수산식품교육문화정보원",
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
