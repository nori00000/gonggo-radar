"""농사로 크롤러 - 지원사업/보조금 공고 수집"""
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


class NongsaroCrawler(BaseCrawler):
    """농사로(nongsaro.go.kr) 웹사이트 크롤러.

    농촌진흥청에서 운영하는 농사로 포털에서 지원사업/보조금 관련 공고를 수집한다.

    대상 URL:
        - https://www.nongsaro.go.kr (메인)
        - 공지사항/사업안내 게시판

    예상 게시판 URL 패턴:
        - /portal/contentsFileView.do?cntntsNo=...
        - /portal/portalMain.ps?menuId=PS00001&pageIndex=1
        - /portal/ps/psb/psbx/selectNewsList.ps (알림마당 > 공지사항)

    예상 HTML 구조 (일반적인 공공기관 게시판):
        <div class="board_list" | class="tb_list">
          <table>
            <tbody>
              <tr>
                <td>번호</td>
                <td class="title"><a href="...">제목</a></td>
                <td>작성자</td>
                <td>작성일</td>
                <td>조회</td>
              </tr>
            </tbody>
          </table>
        </div>

    또는 카드형 레이아웃:
        <div class="card_list">
          <div class="card_item">
            <a href="...">
              <strong class="title">제목</strong>
              <span class="desc">설명</span>
              <span class="date">2024.01.01</span>
            </a>
          </div>
        </div>
    """

    BASE_URL = "https://www.nongsaro.go.kr"

    # 게시판 URL 후보 목록
    BOARD_PATHS = [
        # 알림마당 > 공지사항
        "/portal/ps/psb/psbx/selectNewsList.ps",
        # 알림마당 > 사업공고
        "/portal/ps/psb/psbx/selectAnnoList.ps",
        # 농업기술 > 사업안내
        "/portal/ps/psn/psnb/selectBizAnnoList.ps",
        # 대체 경로
        "/portal/contentsFileList.do?menuId=PS03010",
        "/portal/contentsFileList.do?menuId=PS03020",
    ]

    def __init__(self):
        super().__init__(source_name="nongsaro")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """게시판에서 공고 목록을 수집한다.

        Returns:
            RawAnnouncement 리스트
        """
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL
        seen_ids: set = set()  # 중복 제거용

        for board_path in self.BOARD_PATHS:
            url = f"{base_url}{board_path}"
            self.logger.info(f"Trying board URL: {url}")

            items = self._fetch_board_listing(url)
            if items:
                self.logger.info(
                    f"Fetched {len(items)} items from {url}"
                )
                for item in items:
                    announcement = self._to_announcement(item, base_url)
                    if announcement and announcement.source_id not in seen_ids:
                        seen_ids.add(announcement.source_id)
                        announcements.append(announcement)

        if not announcements:
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

        # 인코딩 처리
        if response.apparent_encoding:
            response.encoding = response.apparent_encoding
        else:
            content_type = response.headers.get("Content-Type", "")
            if "euc-kr" in content_type.lower():
                response.encoding = "euc-kr"
            else:
                response.encoding = "utf-8"

        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        # 전략 1: 테이블 기반 파싱
        items = self._parse_table_board(soup)
        if items:
            self.logger.debug(f"Parsed {len(items)} items with table strategy")
            return items

        # 전략 2: 카드형 레이아웃 파싱
        items = self._parse_card_board(soup)
        if items:
            self.logger.debug(f"Parsed {len(items)} items with card strategy")
            return items

        # 전략 3: div/ul 기반 파싱
        items = self._parse_div_board(soup)
        if items:
            self.logger.debug(f"Parsed {len(items)} items with div strategy")
            return items

        # 전략 4: 범용 링크 추출
        items = self._parse_generic_links(soup)
        if items:
            self.logger.debug(f"Parsed {len(items)} items with generic strategy")
            return items

        return []

    def _parse_table_board(self, soup: "BeautifulSoup") -> List[dict]:
        """테이블 기반 게시판 파싱.

        일반적인 공공기관 게시판 테이블 구조를 파싱한다.
        """
        items = []

        # 게시판 테이블 찾기
        table = None
        for selector in [
            "table.board_list", "table.tb_list", "table.tbl_list",
            "table.bbs_list", "table.list_table", "table.tbl_board",
            "table.board-list", "table.bbsList", "table.data_table",
        ]:
            table = soup.select_one(selector)
            if table:
                break

        # div 컨테이너 내부 테이블 탐색
        if table is None:
            for div_selector in [
                "div.board_list", "div.tb_list", "div.bbs_list",
                "div.list_area", "div.board_area", "div.table_wrap",
            ]:
                container = soup.select_one(div_selector)
                if container:
                    table = container.find("table")
                    if table:
                        break

        # 행이 가장 많은 테이블 (폴백)
        if table is None:
            tables = soup.find_all("table")
            best = None
            max_rows = 0
            for t in tables:
                tbody = t.find("tbody")
                target = tbody if tbody else t
                rows = target.find_all("tr")
                if len(rows) > max_rows:
                    max_rows = len(rows)
                    best = t
            if max_rows >= 3:
                table = best

        if table is None:
            return []

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            title_text, title_link = self._extract_title_from_cells(cells)
            if not title_text:
                continue

            date_str = self._extract_date_from_cells(cells)
            description = self._extract_description_from_cells(cells)

            items.append({
                "title": title_text,
                "link": title_link,
                "date": date_str,
                "description": description,
            })

        return items

    def _parse_card_board(self, soup: "BeautifulSoup") -> List[dict]:
        """카드형 레이아웃 파싱.

        농사로가 카드형 UI를 사용하는 경우를 처리한다.
        """
        items = []

        container = None
        for selector in [
            "div.card_list", "div.cardList", "div.card_wrap",
            "ul.card_list", "div.thumb_list", "div.gallery_list",
        ]:
            container = soup.select_one(selector)
            if container:
                break

        if container is None:
            return []

        cards = container.select(
            "div.card_item, div.card, li.card_item, li, div.item"
        )
        if not cards:
            cards = container.find_all("div", recursive=False)

        for card in cards:
            a_tag = card.find("a")
            if not a_tag:
                continue

            # 제목 추출
            title_elem = card.find(
                ["strong", "h3", "h4", "span", "p"],
                class_=re.compile(r"title|tit|subject|sbj", re.I)
            )
            if title_elem:
                title_text = title_elem.get_text(strip=True)
            else:
                title_text = a_tag.get_text(strip=True)

            if not title_text or len(title_text) < 3:
                continue

            link = a_tag.get("href", "")

            # 설명 추출
            desc_elem = card.find(
                ["span", "p", "div"],
                class_=re.compile(r"desc|summary|cont|txt", re.I)
            )
            description = desc_elem.get_text(strip=True) if desc_elem else ""

            # 날짜 추출
            date_str = ""
            date_elem = card.find(
                ["span", "em", "p"],
                class_=re.compile(r"date|day|time|regdate", re.I)
            )
            if date_elem:
                date_str = date_elem.get_text(strip=True)
            else:
                text = card.get_text()
                date_match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
                if date_match:
                    date_str = date_match.group()

            items.append({
                "title": title_text,
                "link": link,
                "date": date_str,
                "description": description,
            })

        return items

    def _parse_div_board(self, soup: "BeautifulSoup") -> List[dict]:
        """div/ul 기반 게시판 파싱."""
        items = []

        container = None
        for selector in [
            "div.board_list", "div.bbs_list", "div.list_wrap",
            "ul.board_list", "ul.bbs_list", "div.news_list",
        ]:
            container = soup.select_one(selector)
            if container:
                break

        if container is None:
            return []

        list_items = container.find_all("li")
        if not list_items:
            list_items = container.find_all("div", recursive=False)

        for li in list_items:
            a_tag = li.find("a")
            if not a_tag or not a_tag.get_text(strip=True):
                continue

            title_text = a_tag.get_text(strip=True)
            link = a_tag.get("href", "")

            date_str = ""
            date_elem = li.find(
                ["span", "em", "p"],
                class_=re.compile(r"date|day|time|regdate", re.I)
            )
            if date_elem:
                date_str = date_elem.get_text(strip=True)
            else:
                text = li.get_text()
                date_match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
                if date_match:
                    date_str = date_match.group()

            description = ""
            desc_elem = li.find(
                ["span", "p"],
                class_=re.compile(r"desc|summary|cont", re.I)
            )
            if desc_elem:
                description = desc_elem.get_text(strip=True)

            items.append({
                "title": title_text,
                "link": link,
                "date": date_str,
                "description": description,
            })

        return items

    def _parse_generic_links(self, soup: "BeautifulSoup") -> List[dict]:
        """범용 게시판 링크 추출 (폴백)."""
        items = []
        seen = set()

        view_patterns = [
            re.compile(r"View\.do", re.I),
            re.compile(r"view\.ps", re.I),
            re.compile(r"Detail\.do", re.I),
            re.compile(r"selectNews", re.I),
            re.compile(r"contentsFileView", re.I),
            re.compile(r"cntntsNo=", re.I),
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
                "description": "",
            })

        return items

    def _extract_title_from_cells(self, cells) -> tuple:
        """테이블 셀에서 제목과 링크를 추출한다.

        Returns:
            (title_text, link) 튜플
        """
        # subject/title 클래스 셀 우선
        for cell in cells:
            css_classes = " ".join(cell.get("class", []))
            if any(kw in css_classes.lower() for kw in [
                "subject", "title", "sbj", "tit"
            ]):
                a_tag = cell.find("a")
                if a_tag:
                    return a_tag.get_text(strip=True), a_tag.get("href", "")

        # 클래스 없으면 첫 번째 유의미한 링크
        for cell in cells:
            a_tag = cell.find("a")
            if a_tag and len(a_tag.get_text(strip=True)) > 2:
                return a_tag.get_text(strip=True), a_tag.get("href", "")

        return "", ""

    def _extract_date_from_cells(self, cells) -> str:
        """테이블 셀에서 날짜를 추출한다."""
        for cell in cells:
            css_classes = " ".join(cell.get("class", []))
            cell_text = cell.get_text(strip=True)

            if any(kw in css_classes.lower() for kw in [
                "date", "regdate", "reg_date", "day", "write"
            ]):
                return cell_text

            if re.match(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$", cell_text):
                return cell_text

        return ""

    def _extract_description_from_cells(self, cells) -> str:
        """테이블 셀에서 설명/요약을 추출한다."""
        for cell in cells:
            css_classes = " ".join(cell.get("class", []))
            if any(kw in css_classes.lower() for kw in [
                "desc", "summary", "content", "cont"
            ]):
                return cell.get_text(strip=True)
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
            r"cntntsNo=(\d+)", r"nttId=(\d+)", r"artclId=(\d+)",
            r"seq=(\d+)", r"idx=(\d+)", r"no=(\d+)",
            r"boardSeq=(\d+)", r"newsId=(\d+)",
            r"annoId=(\d+)", r"bbsSeq=(\d+)",
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

        # JavaScript 링크 처리
        if link.startswith("javascript:"):
            # JavaScript 호출에서 파라미터 추출 시도
            match = re.search(r"'([^']+)'", link)
            if match:
                param = match.group(1)
                if param.startswith("/"):
                    return f"{base_url}{param}"
            return ""

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
            description = item.get("description", "").strip()

            raw_data = json.dumps(item, ensure_ascii=False)

            return RawAnnouncement(
                source="nongsaro",
                source_id=source_id,
                title=title,
                url=link,
                summary=description,
                author="농사로",
                category="",
                period_start=date_str,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
