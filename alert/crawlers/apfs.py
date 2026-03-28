"""농업정책보험금융원 크롤러 - Playwright 기반 동적 크롤링"""
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

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    PlaywrightTimeoutError = Exception  # type: ignore


class ApfsCrawler(BaseCrawler):
    """농업정책보험금융원(apfs.kr) 공고 게시판 크롤러.

    대상 URL:
        - boardId=10026, menuId=41  (공지사항)
        - boardId=12,    menuId=16  (사업실명제)
        - boardId=43,    menuId=43  (채용공고)

    동작 방식:
        사이트가 jqGrid + AJAX로 게시물 목록을 동적 로드하므로
        Playwright(headless Chromium)로 페이지를 열어 JS 실행 후 DOM을 파싱한다.
        Playwright 미설치 시에는 경고 후 빈 리스트를 반환한다.
    """

    BASE_URL = "https://www.apfs.kr"
    # (boardId, menuId, 설명) 목록
    BOARDS = [
        ("10026", "41", "공지사항"),
        ("12",    "16", "사업실명제"),
        ("43",    "43", "채용공고"),
    ]
    # 게시판 목록 페이지 URL 패턴
    LIST_PAGE_URL = (
        "{base}/front/board/boardContentsListPage.do"
        "?boardId={boardId}&menuId={menuId}"
    )
    # 게시물 상세 URL 패턴 (JS가 사용하는 방식과 동일)
    VIEW_URL = (
        "{base}/front/board/boardContentsView.do"
        "?boardId={boardId}&menuId={menuId}&contId={contId}"
    )

    def __init__(self):
        super().__init__(source_name="apfs")
        if not _PLAYWRIGHT_AVAILABLE:
            self.logger.warning(
                "playwright is not installed. "
                "Install it with: pip install playwright && playwright install chromium"
            )
        if BeautifulSoup is None:
            self.logger.warning(
                "beautifulsoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch(self) -> List[RawAnnouncement]:
        """모든 대상 게시판에서 공고를 수집한다."""
        if not _PLAYWRIGHT_AVAILABLE:
            self.logger.error("playwright is required but not installed")
            return []
        if BeautifulSoup is None:
            self.logger.error("beautifulsoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL  # get_base_url returns "" if unconfigured

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    for board_id, menu_id, board_name in self.BOARDS:
                        url = self.LIST_PAGE_URL.format(
                            base=base_url,
                            boardId=board_id,
                            menuId=menu_id,
                        )
                        self.logger.info(
                            f"Fetching APFS board [{board_name}]: {url}"
                        )
                        items = self._fetch_board_with_browser(
                            browser, url, board_id, menu_id, base_url
                        )
                        self.logger.info(
                            f"  -> {len(items)} items from [{board_name}]"
                        )
                        for item in items:
                            announcement = self._to_announcement(item, base_url)
                            if announcement:
                                announcements.append(announcement)
                finally:
                    browser.close()
        except Exception as e:
            self.logger.error(f"Playwright fetch failed: {e}")

        return announcements

    # ------------------------------------------------------------------
    # Playwright helpers
    # ------------------------------------------------------------------

    def _fetch_board_with_browser(
        self,
        browser,
        url: str,
        board_id: str,
        menu_id: str,
        base_url: str,
    ) -> List[dict]:
        """브라우저 페이지로 게시판 목록을 가져온다."""
        page = browser.new_page()
        try:
            page.goto(url, timeout=15000)
            # jqGrid AJAX 로딩 완료를 기다린다
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                self.logger.warning(
                    f"networkidle timeout for {url}, proceeding with partial content"
                )
            html = page.content()
        except Exception as e:
            self.logger.error(f"Failed to load {url}: {e}")
            return []
        finally:
            page.close()

        return self._parse_board_html(html, board_id, menu_id, base_url)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_board_html(
        self,
        html: str,
        board_id: str,
        menu_id: str,
        base_url: str,
    ) -> List[dict]:
        """렌더링된 HTML에서 게시물 목록을 파싱한다.

        DOM 구조:
            table.tstyle_list > tbody > tr
              td.c_number   : 번호 (공지 아이콘 또는 숫자)
              td.subject / td.c_title : 제목 <a class="detail_view" viewid="...">
              td.c_reg_mem_nm : 작성자
              td.c_reg_dt   : 작성일 (YYYY-MM-DD)
        """
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.tstyle_list")
        if table is None:
            self.logger.warning("table.tstyle_list not found in page HTML")
            return []

        items = []
        tbody = table.find("tbody") or table
        for row in tbody.find_all("tr"):
            item = self._parse_row(row, board_id, menu_id, base_url)
            if item:
                items.append(item)

        return items

    def _parse_row(
        self,
        row,
        board_id: str,
        menu_id: str,
        base_url: str,
    ) -> Optional[dict]:
        """tr 한 행을 파싱해 공고 dict를 반환한다."""
        # 제목 셀: class에 'subject' 또는 'c_title' 포함
        title_cell = row.find(
            "td",
            class_=re.compile(r"\bsubject\b|\bc_title\b"),
        )
        if title_cell is None:
            return None

        a_tag = title_cell.find("a", class_="detail_view")
        if a_tag is None:
            return None

        title = a_tag.get_text(strip=True)
        if not title:
            return None

        # viewid (BS4가 소문자로 정규화)
        view_id = a_tag.get("viewid", "").strip()
        if view_id:
            link = self.VIEW_URL.format(
                base=base_url,
                boardId=board_id,
                menuId=menu_id,
                contId=view_id,
            )
        else:
            link = ""

        # 작성자
        author_cell = row.find("td", class_=re.compile(r"\bc_reg_mem_nm\b"))
        author = author_cell.get_text(strip=True) if author_cell else ""

        # 작성일
        date_cell = row.find("td", class_=re.compile(r"\bc_reg_dt\b"))
        date_str = date_cell.get_text(strip=True) if date_cell else ""

        return {
            "title": title,
            "link": link,
            "view_id": view_id,
            "author": author,
            "date": date_str,
            "category": "",
            "board_id": board_id,
            "menu_id": menu_id,
        }

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

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
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        return None

    def _to_announcement(self, item: dict, base_url: str) -> Optional[RawAnnouncement]:
        """파싱된 공고 dict를 RawAnnouncement로 변환한다."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            link = item.get("link", "").strip()
            view_id = item.get("view_id", "").strip()

            # source_id: viewId 우선, 없으면 URL 해시
            if view_id:
                source_id = f"apfs_{view_id}"
            elif link:
                source_id = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
            else:
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            author = item.get("author", "").strip() or "농업정책보험금융원"
            date_str = item.get("date", "").strip()
            published_date = self._normalize_date(date_str)

            raw_data = json.dumps(item, ensure_ascii=False)

            return RawAnnouncement(
                source="apfs",
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author=author,
                category=item.get("category", ""),
                target="",
                period_start=published_date,
                period_end=None,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None

