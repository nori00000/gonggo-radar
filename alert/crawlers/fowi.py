"""한국산림복지진흥원 크롤러 - 한국산림복지진흥원 지원사업 공고 수집"""
import hashlib
import json
import re
from typing import List, Optional
from .base import BaseCrawler
from .date_labels import (
    posted_date,
    extract_date_and_label,
    header_labels,
    label_for,
)
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

    # 게시글이 아닌 안내/내비게이션 페이지 패턴.
    # 2026-09-12 실측: 게시판 목록 페이지의 메뉴 링크(contentsView.do?cntntsId=...)가
    # 범용 링크 추출 전략의 `view.do` 패턴에 걸려 공고로 적재됐다(10건 전부).
    NAV_LINK_PATTERNS = (
        re.compile(r"contentsView\.do", re.I),
        re.compile(r"[?&]cntntsId=", re.I),
    )

    # 실제 게시글 링크는 href가 아니라 onclick="javascript:goView('<bbsId>')" 이고,
    # 본문 URL은 /user/bbs/bbsView.do?bbsManageId=<board>&bbsId=<bbsId> 다.
    # (2026-09-12 실측: fowi.or.kr 게시판이 POST 폼 submit 방식, GET도 동일 본문 응답)
    GOVIEW_PATTERN = re.compile(r"goView\(\s*'?(\d+)'?\s*\)", re.I)
    BBS_MANAGE_ID_PATTERN = re.compile(r"[?&]bbsManageId=(\d+)", re.I)
    DATE_PATTERN = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}")

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

        # 전략 0: fowi 실제 게시판 구조 (onclick goView)
        bbs_manage_id = self._extract_bbs_manage_id(url)
        items = self._parse_goview_board(soup, bbs_manage_id)
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
    def _extract_bbs_manage_id(cls, url: str) -> str:
        """목록 URL에서 bbsManageId를 추출한다 (없으면 기본 게시판 12)."""
        match = cls.BBS_MANAGE_ID_PATTERN.search(url or "")
        return match.group(1) if match else "12"

    def _parse_goview_board(
        self, soup: "BeautifulSoup", bbs_manage_id: str
    ) -> List[dict]:
        """fowi 게시판의 실제 구조를 파싱한다.

        게시글 행은 `<a class="pList" onclick="javascript:goView('9191')">제목</a>`
        형태이고 같은 `<tr>`에 부서·등록일이 들어 있다. href가 `#pList`라서
        href 기반 전략으로는 게시글을 찾을 수 없다.

        Args:
            soup: 목록 페이지
            bbs_manage_id: 게시판 번호 (본문 URL 조립에 사용)

        Returns:
            공고 항목 목록
        """
        items: List[dict] = []
        seen_ids = set()

        for a_tag in soup.find_all("a", onclick=True):
            match = self.GOVIEW_PATTERN.search(a_tag.get("onclick", ""))
            if not match:
                continue

            bbs_id = match.group(1)
            title = a_tag.get_text(strip=True)
            if not title or bbs_id in seen_ids:
                continue
            seen_ids.add(bbs_id)

            author, date_str = self._row_metadata(a_tag)

            items.append({
                "title": title,
                "link": (
                    f"/user/bbs/bbsView.do"
                    f"?bbsManageId={bbs_manage_id}&bbsId={bbs_id}"
                ),
                "author": author,
                "category": "",
                "date": date_str,
                # 이 게시판의 날짜는 **등록일**이다 (_row_metadata 참조) -
                # 기간이 아니라 게시일로 분류되게 라벨을 명시한다
                "date_label": "등록일",
            })

        return items

    def _row_metadata(self, a_tag) -> tuple[str, str]:
        """게시글 링크가 속한 행에서 담당부서와 등록일을 추출한다."""
        row = a_tag.find_parent("tr")
        if row is None:
            return "", ""

        cells = row.find_all("td")
        texts = [cell.get_text(strip=True) for cell in cells]

        date_str = next(
            (t for t in texts if self.DATE_PATTERN.search(t)), ""
        )

        author = ""
        title_idx = next(
            (i for i, cell in enumerate(cells) if a_tag in cell.find_all("a")),
            None,
        )
        if title_idx is not None and title_idx + 1 < len(texts):
            candidate = texts[title_idx + 1]
            is_number = candidate.replace(",", "").isdigit()
            if candidate and not is_number and not self.DATE_PATTERN.search(candidate):
                author = candidate

        return author, date_str

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

        headers = header_labels(table)

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            date_label = ""

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
                    date_label = label_for(cell, cells, headers, css_class)

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
                        # 표 헤더에서 컬럼 라벨을 읽는다 - "게시일" 이면
                        # 기간이 아니다 (6차 게이트 #1)
                        date_label = label_for(cell, cells, headers, "")
                        break

            items.append({
                "title": title_text,
                "link": title_link or "",
                "author": author,
                "category": category,
                "date": date_str,
                "date_label": date_label,
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

            # 날짜와 라벨은 **날짜가 든 가장 작은 요소** 안에서만 읽는다.
            # 항목 전체 텍스트에서 앞말을 자르면 제목이 라벨로 새어 들어와
            # 게시일이 접수기간이 된다 (7차 게이트 #3).
            date_str, date_label = extract_date_and_label(item_elem)

            items.append({
                "title": title_text,
                "link": link,
                "author": author,
                "category": category,
                "date": date_str,
                "date_label": date_label,
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

            # 안내 페이지는 게시글이 아니다 (contentsView.do 등)
            if self._is_navigation_link(href):
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

    @classmethod
    def _is_navigation_link(cls, href: str) -> bool:
        """게시글이 아닌 안내 페이지 링크인지 판정한다.

        Args:
            href: 링크 URL (상대/절대 무관)

        Returns:
            안내/내비게이션 페이지면 True
        """
        if not href:
            return False
        return any(p.search(href) for p in cls.NAV_LINK_PATTERNS)

    def _extract_post_id(self, link: str) -> str:
        """URL에서 공고 ID를 추출한다."""
        if not link:
            return ""

        id_params = [
            r"[?&]bbsId=(\d+)",
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

            # 어느 파싱 전략을 타든 안내 페이지는 공고로 적재하지 않는다
            if self._is_navigation_link(link):
                self.logger.debug(f"Skipping navigation link: {link}")
                return None

            source_id = self._extract_post_id(link)

            if not source_id:
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            author = item.get("author", "").strip()
            category = item.get("category", "").strip()

            # 13차: 이 소스는 전용 추출기가 없다 = 기간이 없다. 목록
            # 날짜는 게시일 증거로만 남는다 (관문이 두 필드를 None 으로
            # 확정한다).
            posted = posted_date(item.get("date", ""))

            payload = dict(item)
            if posted:
                payload["posted"] = posted
            raw_data = json.dumps(payload, ensure_ascii=False)

            return RawAnnouncement(
                source="fowi",
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author=author or "한국산림복지진흥원",
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
