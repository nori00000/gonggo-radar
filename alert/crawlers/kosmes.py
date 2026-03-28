"""중소벤처기업진흥공단 크롤러 - 공지사항/사업안내 수집"""
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
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class KosmesCrawler(BaseCrawler):
    """중소벤처기업진흥공단(kosmes.or.kr) 공고 게시판 크롤러.

    대상 URL:
        - https://www.kosmes.or.kr/nsh/SH/NTS/SHNTS001M0.do (공지사항)

    Playwright 기반:
        SPA 사이트이므로 JS 렌더링 후 tbody#AXGridTarget1 파싱.
        Playwright 미설치 시 requests 폴백을 시도하나 결과가 없을 수 있다.
    """

    BASE_URL = "https://www.kosmes.or.kr"
    NOTICE_PAGE_URL = "/nsh/SH/NTS/SHNTS001M0.do"
    DETAIL_URL_PATTERN = "/nsh/SH/NTS/SHNTS001M0.do?seqNo={seq_no}"

    def __init__(self):
        super().__init__(source_name="kosmes")
        if not PLAYWRIGHT_AVAILABLE:
            self.logger.warning(
                "Playwright is not installed. Install it with: "
                "pip install playwright && playwright install chromium"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """중소벤처기업진흥공단 공고를 수집한다.

        KOSMES는 SPA 기반 사이트이므로 Playwright로 JS 렌더링 후 파싱한다.
        Playwright가 없으면 requests 폴백을 시도한다.

        Returns:
            RawAnnouncement 리스트
        """
        base_url = self.get_base_url() or self.BASE_URL

        if PLAYWRIGHT_AVAILABLE:
            items = self._fetch_with_playwright(base_url)
            if items:
                self.logger.info(
                    f"Successfully fetched {len(items)} items via Playwright"
                )
                return self._to_announcements(items, base_url)

        # Playwright 미설치 또는 실패 시 requests 폴백
        self.logger.info("Falling back to requests-based fetch")
        items = self._fetch_with_requests(base_url)
        if items:
            self.logger.info(
                f"Fetched {len(items)} items via requests fallback"
            )
            return self._to_announcements(items, base_url)

        self.logger.warning(
            "KOSMES is SPA-based and requires JavaScript execution. "
            "Install Playwright for reliable results: "
            "pip install playwright && playwright install chromium"
        )
        return []

    # ------------------------------------------------------------------
    # Playwright 기반 수집
    # ------------------------------------------------------------------

    def _fetch_with_playwright(self, base_url: str) -> List[dict]:
        """Playwright로 공지사항 페이지를 렌더링하고 목록을 파싱한다."""
        url = f"{base_url}{self.NOTICE_PAGE_URL}"
        self.logger.info(f"Fetching KOSMES via Playwright: {url}")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(url, timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=10000)
                    html = page.content()
                finally:
                    browser.close()
        except Exception as e:
            self.logger.error(f"Playwright fetch failed: {e}")
            return []

        return self._parse_notice_html(html)

    def _parse_notice_html(self, html: str) -> List[dict]:
        """렌더링된 HTML에서 공지사항 목록을 파싱한다.

        구조:
            tbody#AXGridTarget1 > tr
                td.mobNone[data-bind=PAGE_NO]   - 번호
                td.mobNone[data-bind=CATG_CD]   - 카테고리 (선택적)
                td.bbs_tit[data-bind=TITL_NM]  - 제목 (a 태그, onclick=fn_detail(ID))
                td[data-bind=UPDT_DTM]          - 날짜
        """
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return self._parse_notice_html_regex(html)

        soup = BeautifulSoup(html, "html.parser")
        tbody = soup.find("tbody", id="AXGridTarget1")
        if tbody is None:
            self.logger.warning("tbody#AXGridTarget1 not found in rendered HTML")
            return self._parse_notice_html_regex(html)

        items: List[dict] = []
        for row in tbody.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue

            # 제목 셀: class="bbs_tit"
            title_cell = row.find("td", class_="bbs_tit")
            if title_cell is None:
                continue

            a_tag = title_cell.find("a")
            if a_tag is None:
                continue

            title = a_tag.get_text(strip=True)
            if not title:
                continue

            # fn_detail(ID) 에서 seq_no 추출
            onclick = a_tag.get("onclick", "")
            seq_no = self._extract_seq_no(onclick)

            # 카테고리 셀: data-bind="CATG_CD"
            category = ""
            cat_cell = row.find("td", attrs={"data-bind": "CATG_CD"})
            if cat_cell:
                category = cat_cell.get_text(strip=True)

            # 날짜 셀: data-bind="UPDT_DTM"
            date_str = ""
            date_cell = row.find("td", attrs={"data-bind": "UPDT_DTM"})
            if date_cell:
                date_str = date_cell.get_text(strip=True)

            link = (
                self.DETAIL_URL_PATTERN.format(seq_no=seq_no)
                if seq_no
                else self.NOTICE_PAGE_URL
            )

            items.append({
                "title": title,
                "link": link,
                "author": "",
                "category": category,
                "date": date_str,
                "seq_no": seq_no,
            })

        return items

    def _parse_notice_html_regex(self, html: str) -> List[dict]:
        """BeautifulSoup 없을 때 정규식으로 파싱하는 폴백."""
        items: List[dict] = []
        # <td class="bbs_tit" ...><a href="..." onclick="fn_detail(ID);...">TITLE</a></td>
        row_pattern = re.compile(
            r'onclick="fn_detail\((\d+)\)[^"]*"[^>]*>([^<]+)</a>',
            re.S
        )
        date_pattern = re.compile(
            r'data-bind="UPDT_DTM">(\d{4}-\d{2}-\d{2})<'
        )
        catg_pattern = re.compile(
            r'data-bind="CATG_CD">([^<]*)<'
        )

        dates = date_pattern.findall(html)
        categories = catg_pattern.findall(html)

        for i, match in enumerate(row_pattern.finditer(html)):
            seq_no = match.group(1)
            title = match.group(2).strip()
            if not title:
                continue
            date_str = dates[i] if i < len(dates) else ""
            category = categories[i] if i < len(categories) else ""
            items.append({
                "title": title,
                "link": self.DETAIL_URL_PATTERN.format(seq_no=seq_no),
                "author": "",
                "category": category,
                "date": date_str,
                "seq_no": seq_no,
            })

        return items

    # ------------------------------------------------------------------
    # requests 폴백 수집
    # ------------------------------------------------------------------

    def _fetch_with_requests(self, base_url: str) -> List[dict]:
        """requests로 공지사항 페이지를 가져온다.

        SPA 특성상 JS 렌더링 없이는 목록 데이터가 없을 수 있다.
        """
        url = f"{base_url}{self.NOTICE_PAGE_URL}"
        response = self.get(url)
        if response is None:
            return []

        response.encoding = response.apparent_encoding or "utf-8"
        return self._parse_notice_html(response.text)

    # ------------------------------------------------------------------
    # 공통 유틸리티
    # ------------------------------------------------------------------

    def _extract_seq_no(self, onclick: str) -> str:
        """onclick 속성에서 fn_detail(ID) 의 ID를 추출한다."""
        match = re.search(r"fn_detail\((\d+)\)", onclick)
        if match:
            return match.group(1)
        # seqNo= 파라미터 형식도 시도
        match = re.search(r"seqNo=(\d+)", onclick)
        if match:
            return match.group(1)
        return ""

    def _to_announcements(
        self, items: List[dict], base_url: str
    ) -> List[RawAnnouncement]:
        """파싱된 공고 딕셔너리 리스트를 RawAnnouncement 리스트로 변환한다."""
        announcements: List[RawAnnouncement] = []
        for item in items:
            announcement = self._to_announcement(item, base_url)
            if announcement:
                announcements.append(announcement)
        return announcements

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
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"

        return None

    def _to_announcement(self, item: dict, base_url: str) -> Optional[RawAnnouncement]:
        """파싱된 공고 데이터를 RawAnnouncement로 변환한다."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            link = self._normalize_url(item.get("link", ""), base_url)

            # source_id: seq_no 우선, 없으면 URL 해시
            seq_no = item.get("seq_no", "")
            if seq_no:
                source_id = seq_no
            elif link:
                source_id = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
            else:
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            author = item.get("author", "").strip()
            category = item.get("category", "").strip()

            date_str = item.get("date", "").strip()
            period_start = self._normalize_date(date_str)

            raw_data = json.dumps(item, ensure_ascii=False)

            return RawAnnouncement(
                source="kosmes",
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author=author or "중소벤처기업진흥공단",
                category=category,
                target="",
                period_start=period_start,
                period_end=None,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
