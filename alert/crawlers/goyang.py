"""고양시 농업기술센터 크롤러 - 공지사항/사업안내 수집"""
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


class GoyangCrawler(BaseCrawler):
    """고양시 농업기술센터 게시판 크롤러.

    대상 URL:
        - https://www.goyang.go.kr/agri/index.do (메인)
        - 공지사항 게시판 (일반적인 경로 패턴)

    고양시 웹사이트는 전형적인 한국 공공기관 게시판 구조를 따른다.

    예상 HTML 구조:
        <div class="board_list" | class="bdList">
          <table>
            <thead>
              <tr><th>번호</th><th>제목</th><th>작성일</th>...</tr>
            </thead>
            <tbody>
              <tr>
                <td>1</td>
                <td class="subject"><a href="...&nttId=12345">공고 제목</a></td>
                <td>2024-01-01</td>
              </tr>
            </tbody>
          </table>
        </div>

    또는 최신 반응형 레이아웃:
        <div class="bbs_list_area">
          <ul>
            <li>
              <div class="cont">
                <a href="...">제목</a>
                <span class="info"><span class="date">2024.01.01</span></span>
              </div>
            </li>
          </ul>
        </div>
    """

    BASE_URL = "https://www.goyang.go.kr"

    # 게시판 경로 후보 목록
    # 실제 URL은 사이트 구조에 따라 다를 수 있으므로 여러 패턴 시도
    BOARD_PATHS = [
        "/agri/bbs/BBSMSTR_000000000071/list.do",    # 농업기술센터 공지사항
        "/agri/bbs/BBSMSTR_000000000072/list.do",    # 농업기술센터 사업안내
        "/www/bbs/BBSMSTR_000000000071/list.do",     # 대체 경로
        "/agri/board/list.do?boardId=BBS_0000071",   # 대체 경로 2
    ]

    def __init__(self):
        super().__init__(source_name="goyang")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """게시판에서 공고 목록을 수집한다.

        여러 게시판 URL을 순차적으로 시도하여 첫 번째 성공하는 URL에서
        데이터를 가져온다.

        Returns:
            RawAnnouncement 리스트
        """
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL

        successful = False
        for board_path in self.BOARD_PATHS:
            url = f"{base_url}{board_path}"
            self.logger.info(f"Trying board URL: {url}")

            items = self._fetch_board_listing(url)
            if items:
                self.logger.info(
                    f"Successfully fetched {len(items)} items from {url}"
                )
                for item in items:
                    announcement = self._to_announcement(item, base_url)
                    if announcement:
                        announcements.append(announcement)
                successful = True
                break  # 성공하면 다음 URL은 시도하지 않음

        if not successful:
            self.logger.warning(
                "Could not fetch from any known board URL. "
                "Site structure may have changed."
            )

        return announcements

    def _fetch_board_listing(self, url: str) -> List[dict]:
        """게시판 목록 페이지를 파싱한다.

        Args:
            url: 게시판 URL

        Returns:
            게시물 딕셔너리 리스트
        """
        response = self.get(url)
        if response is None:
            return []

        # 인코딩 처리: 고양시 사이트는 UTF-8 또는 EUC-KR 가능
        if response.apparent_encoding:
            response.encoding = response.apparent_encoding
        else:
            # Content-Type 헤더에서 인코딩 확인
            content_type = response.headers.get("Content-Type", "")
            if "euc-kr" in content_type.lower():
                response.encoding = "euc-kr"
            else:
                response.encoding = "utf-8"

        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        # 전략 1: table 기반 파싱
        items = self._parse_table_board(soup)
        if items:
            return items

        # 전략 2: div/ul 기반 파싱 (반응형 레이아웃)
        items = self._parse_responsive_board(soup)
        if items:
            return items

        # 전략 3: 범용 링크 추출
        items = self._parse_generic_board(soup)
        if items:
            return items

        return []

    def _parse_table_board(self, soup: "BeautifulSoup") -> List[dict]:
        """테이블 기반 게시판 파싱.

        한국 공공기관 사이트의 가장 일반적인 게시판 구조.
        """
        items = []

        # 게시판 테이블 찾기
        table = None
        for selector in [
            "table.board_list", "table.bdList", "table.bbs_list",
            "table.tbl_list", "table.list_table", "table.tbl_board",
            "table.board-list", "table.bbsList",
        ]:
            table = soup.select_one(selector)
            if table:
                break

        # 클래스 없이 div 내부 table 탐색
        if table is None:
            for div_selector in [
                "div.board_list", "div.bdList", "div.bbs_list",
                "div.list_wrap", "div.board_area",
            ]:
                container = soup.select_one(div_selector)
                if container:
                    table = container.find("table")
                    if table:
                        break

        # 여전히 없으면 행이 많은 table 사용
        if table is None:
            tables = soup.find_all("table")
            best_table = None
            max_rows = 0
            for t in tables:
                tbody = t.find("tbody")
                target = tbody if tbody else t
                rows = target.find_all("tr")
                if len(rows) > max_rows:
                    max_rows = len(rows)
                    best_table = t
            if max_rows >= 3:
                table = best_table

        if table is None:
            return []

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            # 제목 + 링크 추출
            title_text = ""
            title_link = ""
            category = ""

            # subject/title 클래스 셀 우선 탐색
            for cell in cells:
                css_classes = " ".join(cell.get("class", []))
                if any(kw in css_classes.lower() for kw in [
                    "subject", "title", "sbj", "tit"
                ]):
                    a_tag = cell.find("a")
                    if a_tag:
                        title_link = a_tag.get("href", "")
                        title_text = a_tag.get_text(strip=True)

                        # 카테고리 뱃지 추출 (있는 경우)
                        badge = cell.find(
                            ["span", "em"],
                            class_=re.compile(r"cate|badge|label|tag", re.I)
                        )
                        if badge:
                            category = badge.get_text(strip=True)
                    break

            # 클래스 없으면 첫 번째 링크
            if not title_text:
                for cell in cells:
                    a_tag = cell.find("a")
                    if a_tag and len(a_tag.get_text(strip=True)) > 2:
                        title_link = a_tag.get("href", "")
                        title_text = a_tag.get_text(strip=True)
                        break

            if not title_text:
                continue

            # 날짜 추출
            date_str = self._extract_date_from_cells(cells)

            items.append({
                "title": title_text,
                "link": title_link,
                "date": date_str,
                "category": category,
            })

        return items

    def _parse_responsive_board(self, soup: "BeautifulSoup") -> List[dict]:
        """반응형 레이아웃 게시판 파싱 (div/ul/li 기반)."""
        items = []

        container = None
        for selector in [
            "div.bbs_list_area", "div.board_list_area", "div.bdListWrap",
            "ul.board_list", "ul.bbs_list", "div.list_area",
        ]:
            container = soup.select_one(selector)
            if container:
                break

        if container is None:
            return []

        # li 요소 또는 직접 하위 div 요소 탐색
        list_items = container.find_all("li")
        if not list_items:
            list_items = container.find_all("div", recursive=False)

        for li in list_items:
            a_tag = li.find("a")
            if not a_tag or not a_tag.get_text(strip=True):
                continue

            title_text = a_tag.get_text(strip=True)
            link = a_tag.get("href", "")

            # 날짜 추출
            date_str = ""
            date_elem = li.find(
                ["span", "em", "p", "div"],
                class_=re.compile(r"date|regdate|time|day|info_date", re.I)
            )
            if date_elem:
                date_str = date_elem.get_text(strip=True)
            else:
                text = li.get_text()
                date_match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
                if date_match:
                    date_str = date_match.group()

            # 카테고리 추출
            category = ""
            cat_elem = li.find(
                ["span", "em"],
                class_=re.compile(r"cate|badge|label|tag", re.I)
            )
            if cat_elem:
                category = cat_elem.get_text(strip=True)

            items.append({
                "title": title_text,
                "link": link,
                "date": date_str,
                "category": category,
            })

        return items

    def _parse_generic_board(self, soup: "BeautifulSoup") -> List[dict]:
        """범용 게시판 링크 추출 (폴백).

        게시판 상세 보기 링크 패턴을 찾아 추출한다.
        """
        items = []
        seen = set()

        view_patterns = [
            re.compile(r"view\.do", re.I),
            re.compile(r"nttId=", re.I),
            re.compile(r"artclView", re.I),
            re.compile(r"boardView", re.I),
            re.compile(r"bbsView", re.I),
        ]

        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            text = a_tag.get_text(strip=True)

            if not text or len(text) < 5:
                continue

            is_view = any(p.search(href) for p in view_patterns)
            if not is_view:
                continue

            if href in seen:
                continue
            seen.add(href)

            items.append({
                "title": text,
                "link": href,
                "date": "",
                "category": "",
            })

        return items

    def _extract_date_from_cells(self, cells) -> str:
        """테이블 셀 목록에서 날짜를 추출한다.

        Args:
            cells: td 요소 목록

        Returns:
            날짜 문자열 (YYYY-MM-DD 등), 못 찾으면 빈 문자열
        """
        for cell in cells:
            css_classes = " ".join(cell.get("class", []))
            cell_text = cell.get_text(strip=True)

            # 날짜 관련 클래스
            if any(kw in css_classes.lower() for kw in [
                "date", "regdate", "reg_date", "day", "write"
            ]):
                return cell_text

            # 날짜 패턴 매칭
            if re.match(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$", cell_text):
                return cell_text

        return ""

    def _extract_post_id(self, link: str) -> str:
        """URL에서 게시물 ID를 추출한다.

        Args:
            link: 게시물 URL

        Returns:
            게시물 ID 문자열
        """
        if not link:
            return ""

        # 파라미터 기반 ID 추출
        id_patterns = [
            r"nttId=(\d+)", r"artclId=(\d+)", r"seq=(\d+)",
            r"idx=(\d+)", r"boardSeq=(\d+)", r"no=(\d+)",
            r"bbsSeq=(\d+)", r"articleId=(\d+)",
        ]
        for pattern in id_patterns:
            match = re.search(pattern, link, re.I)
            if match:
                return match.group(1)

        # 경로 기반 ID
        path_match = re.search(r"/(\d{4,})", link)
        if path_match:
            return path_match.group(1)

        # 폴백: URL 해시
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

        # YYYYMMDD 형식
        match = re.match(r"^(\d{4})(\d{2})(\d{2})$", date_str.strip())
        if match:
            year, month, day = match.groups()
            return f"{year}-{month}-{day}"

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
                source="goyang",
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author="고양시 농업기술센터",
                category=category,
                period_start=date_str,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
