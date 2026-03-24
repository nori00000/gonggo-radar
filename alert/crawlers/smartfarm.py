"""스마트팜코리아 크롤러 - 공지사항/사업안내 수집"""
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


class SmartfarmCrawler(BaseCrawler):
    """스마트팜코리아(smartfarmkorea.net) 웹사이트 크롤러.

    공지사항/사업안내 게시판에서 게시물 목록을 스크래핑한다.

    대상 URL:
        - 공지사항: https://www.smartfarmkorea.net/board/boardList.do?menuId=M010601
        - 사업안내: https://www.smartfarmkorea.net/board/boardList.do?menuId=M010602

    예상 HTML 구조 (일반적인 한국 공공기관 게시판):
        <div class="board_list"> 또는 <table class="board-list">
          <tbody>
            <tr>
              <td class="no">1</td>
              <td class="title"><a href="boardView.do?...">제목</a></td>
              <td class="date">2024-01-01</td>
            </tr>
            ...
          </tbody>
        </table>

    또는 div 기반 레이아웃:
        <div class="bbs_list">
          <ul>
            <li>
              <a href="...">제목</a>
              <span class="date">2024-01-01</span>
            </li>
          </ul>
        </div>
    """

    BASE_URL = "https://www.smartfarmkorea.net"

    # 게시판 URL 패턴 (공지사항, 사업안내 등)
    BOARD_URLS = [
        "/board/boardList.do?menuId=M010601",  # 공지사항
        "/board/boardList.do?menuId=M010602",  # 사업안내
    ]

    def __init__(self):
        super().__init__(source_name="smartfarm")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """게시판 목록 페이지에서 공고를 수집한다.

        Returns:
            RawAnnouncement 리스트
        """
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL

        for board_path in self.BOARD_URLS:
            url = f"{base_url}{board_path}"
            self.logger.info(f"Fetching board listing from: {url}")

            board_items = self._fetch_board_listing(url)
            for item in board_items:
                announcement = self._to_announcement(item, base_url)
                if announcement:
                    # 상세 페이지에서 요약 정보 가져오기 시도
                    if announcement.url and not announcement.summary:
                        summary = self._fetch_detail_summary(announcement.url)
                        if summary:
                            announcement.summary = summary
                    announcements.append(announcement)

        return announcements

    def _fetch_board_listing(self, url: str) -> List[dict]:
        """게시판 목록 페이지를 파싱하여 게시물 목록을 추출한다.

        여러 가지 일반적인 한국 게시판 HTML 패턴을 시도한다.

        Args:
            url: 게시판 URL

        Returns:
            게시물 딕셔너리 리스트 (title, link, date, category 등)
        """
        response = self.get(url)
        if response is None:
            self.logger.error(f"Failed to fetch board listing from {url}")
            return []

        # 인코딩 처리
        response.encoding = response.apparent_encoding or "utf-8"
        html = response.text

        soup = BeautifulSoup(html, "html.parser")
        items: List[dict] = []

        # 전략 1: table 기반 게시판 (가장 일반적)
        items = self._parse_table_board(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using table strategy")
            return items

        # 전략 2: div.board_list / div.bbs_list 기반
        items = self._parse_div_board(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using div strategy")
            return items

        # 전략 3: ul/li 기반 게시판
        items = self._parse_list_board(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using list strategy")
            return items

        # 전략 4: 범용 링크 추출 (폴백)
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
        """table 기반 게시판 파싱.

        예상 구조:
            <table class="board*" | class="list*" | class="tbl*">
              <tbody>
                <tr>
                  <td>번호</td>
                  <td class="title|subject"><a href="...">제목</a></td>
                  <td>작성자</td>
                  <td class="date">날짜</td>
                </tr>
              </tbody>
            </table>
        """
        items = []

        # 게시판 테이블 찾기: 다양한 클래스 패턴
        table = None
        for selector in [
            "table.board_list", "table.board-list", "table.bbs_list",
            "table.tbl_list", "table.tbl_board", "table.list_table",
            "table.bbsList", "table.table_list",
        ]:
            table = soup.select_one(selector)
            if table:
                break

        # 클래스 기반으로 못 찾으면 tbody가 있는 table 중 행이 가장 많은 것 선택
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

            # 제목 + 링크 찾기
            title_link = None
            title_text = ""

            # title/subject 클래스 셀 우선
            for cell in cells:
                css_class = " ".join(cell.get("class", []))
                if any(kw in css_class for kw in ["title", "subject", "sbj"]):
                    a_tag = cell.find("a")
                    if a_tag:
                        title_link = a_tag.get("href", "")
                        title_text = a_tag.get_text(strip=True)
                    break

            # 클래스 없으면 첫 번째 a 태그
            if not title_text:
                for cell in cells:
                    a_tag = cell.find("a")
                    if a_tag and a_tag.get_text(strip=True):
                        title_link = a_tag.get("href", "")
                        title_text = a_tag.get_text(strip=True)
                        break

            if not title_text:
                continue

            # 날짜 찾기
            date_str = ""
            for cell in cells:
                css_class = " ".join(cell.get("class", []))
                cell_text = cell.get_text(strip=True)
                if any(kw in css_class for kw in ["date", "regdate", "reg_date"]):
                    date_str = cell_text
                    break
                # 날짜 패턴 탐지: YYYY-MM-DD 또는 YYYY.MM.DD
                if re.match(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", cell_text):
                    date_str = cell_text

            items.append({
                "title": title_text,
                "link": title_link or "",
                "date": date_str,
                "category": "",
            })

        return items

    def _parse_div_board(self, soup: "BeautifulSoup") -> List[dict]:
        """div 기반 게시판 파싱.

        예상 구조:
            <div class="board_list|bbs_list|list_wrap">
              <div class="item|row">
                <a href="...">제목</a>
                <span class="date">2024-01-01</span>
              </div>
            </div>
        """
        items = []

        container = None
        for selector in [
            "div.board_list", "div.board-list", "div.bbs_list",
            "div.list_wrap", "div.list-wrap", "div.bbsList",
        ]:
            container = soup.select_one(selector)
            if container:
                break

        if container is None:
            return []

        # div.item 또는 div.row 패턴
        rows = container.select("div.item, div.row, div.list_item, li")
        if not rows:
            rows = container.find_all("div", recursive=False)

        for row in rows:
            a_tag = row.find("a")
            if not a_tag or not a_tag.get_text(strip=True):
                continue

            title_text = a_tag.get_text(strip=True)
            link = a_tag.get("href", "")

            # 날짜 찾기
            date_str = ""
            date_elem = row.find(
                ["span", "em", "p"],
                class_=re.compile(r"date|regdate|time|day", re.I)
            )
            if date_elem:
                date_str = date_elem.get_text(strip=True)
            else:
                # 텍스트에서 날짜 패턴 탐지
                text = row.get_text()
                date_match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
                if date_match:
                    date_str = date_match.group()

            items.append({
                "title": title_text,
                "link": link,
                "date": date_str,
                "category": "",
            })

        return items

    def _parse_list_board(self, soup: "BeautifulSoup") -> List[dict]:
        """ul/li 기반 게시판 파싱."""
        items = []

        list_container = None
        for selector in [
            "ul.board_list", "ul.bbs_list", "ul.list",
            "ul.notice_list", "ul.noti_list",
        ]:
            list_container = soup.select_one(selector)
            if list_container:
                break

        if list_container is None:
            return []

        for li in list_container.find_all("li"):
            a_tag = li.find("a")
            if not a_tag or not a_tag.get_text(strip=True):
                continue

            title_text = a_tag.get_text(strip=True)
            link = a_tag.get("href", "")

            date_str = ""
            text = li.get_text()
            date_match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
            if date_match:
                date_str = date_match.group()

            items.append({
                "title": title_text,
                "link": link,
                "date": date_str,
                "category": "",
            })

        return items

    def _parse_generic_links(self, soup: "BeautifulSoup") -> List[dict]:
        """범용 링크 추출 (폴백 전략).

        boardView, articleView 등 게시판 상세 페이지 링크를 찾는다.
        """
        items = []
        seen_links = set()

        # 게시판 상세 보기 링크 패턴
        view_patterns = [
            re.compile(r"boardView", re.I),
            re.compile(r"articleView", re.I),
            re.compile(r"view\.do", re.I),
            re.compile(r"bbsView", re.I),
        ]

        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            title_text = a_tag.get_text(strip=True)

            if not title_text or len(title_text) < 5:
                continue

            # 상세 보기 링크인지 확인
            is_view_link = any(p.search(href) for p in view_patterns)
            if not is_view_link:
                continue

            if href in seen_links:
                continue
            seen_links.add(href)

            items.append({
                "title": title_text,
                "link": href,
                "date": "",
                "category": "",
            })

        return items

    def _fetch_detail_summary(self, detail_url: str) -> str:
        """상세 페이지에서 요약/본문을 가져온다.

        Args:
            detail_url: 상세 페이지 URL

        Returns:
            요약 텍스트 (최대 500자), 실패 시 빈 문자열
        """
        try:
            response = self.get(detail_url)
            if response is None:
                return ""

            response.encoding = response.apparent_encoding or "utf-8"
            soup = BeautifulSoup(response.text, "html.parser")

            # 본문 영역 찾기: 다양한 클래스 패턴
            content = None
            for selector in [
                "div.board_view", "div.board-view", "div.view_cont",
                "div.view_content", "div.bbs_view", "div.content_view",
                "div.article_body", "div.bbsView",
                "td.content", "div.board_content",
            ]:
                content = soup.select_one(selector)
                if content:
                    break

            if content is None:
                return ""

            text = content.get_text(separator=" ", strip=True)
            # 최대 500자
            return text[:500] if text else ""

        except Exception as e:
            self.logger.debug(f"Failed to fetch detail summary from {detail_url}: {e}")
            return ""

    def _extract_post_id(self, link: str) -> str:
        """URL에서 게시물 ID를 추출한다.

        시도 패턴:
            - boardSeq=12345
            - nttId=12345
            - artclId=12345
            - seq=12345
            - /view/12345
            - URL 해시 (폴백)

        Args:
            link: 게시물 URL

        Returns:
            게시물 ID 문자열
        """
        if not link:
            return ""

        # 파라미터 기반 ID 추출
        id_params = [
            r"boardSeq=(\d+)", r"nttId=(\d+)", r"artclId=(\d+)",
            r"seq=(\d+)", r"idx=(\d+)", r"no=(\d+)",
            r"bbsId=(\d+)", r"articleId=(\d+)",
        ]
        for pattern in id_params:
            match = re.search(pattern, link, re.I)
            if match:
                return match.group(1)

        # 경로 기반 ID 추출: /view/12345 또는 /12345/
        path_match = re.search(r"/(\d{3,})", link)
        if path_match:
            return path_match.group(1)

        # 폴백: URL 해시
        return hashlib.md5(link.encode("utf-8")).hexdigest()[:16]

    def _normalize_url(self, link: str, base_url: str) -> str:
        """상대 URL을 절대 URL로 변환한다.

        Args:
            link: 상대 또는 절대 URL
            base_url: 기본 URL

        Returns:
            절대 URL
        """
        if not link:
            return ""
        if link.startswith("http://") or link.startswith("https://"):
            return link
        if link.startswith("/"):
            return f"{base_url}{link}"
        return f"{base_url}/{link}"

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """날짜 문자열을 ISO 형식(YYYY-MM-DD)으로 정규화한다.

        Args:
            date_str: 다양한 형식의 날짜 문자열

        Returns:
            ISO 형식 날짜 문자열, 실패 시 None
        """
        if not date_str:
            return None

        match = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", date_str)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        return None

    def _to_announcement(self, item: dict, base_url: str) -> Optional[RawAnnouncement]:
        """파싱된 게시물 데이터를 RawAnnouncement로 변환한다.

        Args:
            item: 게시물 데이터 딕셔너리
            base_url: 기본 URL

        Returns:
            RawAnnouncement 객체, 실패 시 None
        """
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            link = self._normalize_url(item.get("link", ""), base_url)
            source_id = self._extract_post_id(link)

            if not source_id:
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            date_str = self._normalize_date(item.get("date", ""))
            category = item.get("category", "").strip()

            raw_data = json.dumps(item, ensure_ascii=False)

            return RawAnnouncement(
                source="smartfarm",
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author="스마트팜코리아",
                category=category,
                period_start=date_str,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
