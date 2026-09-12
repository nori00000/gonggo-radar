"""한국임업진흥원 크롤러 - 공지사항 및 입찰/공모 공고 수집"""
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


class KofpiCrawler(BaseCrawler):
    """한국임업진흥원(kofpi.or.kr) 게시판 크롤러.

    대상 URL:
        - https://www.kofpi.or.kr/notice/notice_01.do (공지사항)
        - https://www.kofpi.or.kr/notice/notice_03.do (입찰/공모)

    HTML 파싱 방식:
        목록은 정적 HTML(table.table_list)에 포함되어 있다.
        상세 링크는 onclick="fnGoView('12658')" 형태이며,
        실제 상세 페이지는 ``{목록경로}view.do?bb_seq={seq}`` 로 접근 가능하다.
    """

    BASE_URL = "https://www.kofpi.or.kr"

    # (목록 경로, 상세 경로, 분류)
    BOARDS = [
        ("/notice/notice_01.do", "/notice/notice_01view.do", "공지"),
        ("/notice/notice_03.do", "/notice/notice_03view.do", "입찰/공모"),
    ]

    def __init__(self):
        super().__init__(source_name="kofpi")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """한국임업진흥원 게시판에서 공고를 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL

        for list_path, view_path, board_label in self.BOARDS:
            url = f"{base_url}{list_path}"
            self.logger.info(f"Fetching from KOFPI board: {url}")

            response = self.get(url)
            if response is None:
                self.logger.error(f"Failed to fetch board listing from {url}")
                continue

            response.encoding = response.apparent_encoding or "utf-8"
            soup = BeautifulSoup(response.text, "html.parser")

            items = self.parse_list(soup, view_path, board_label)
            if not items:
                self.logger.warning(
                    f"Could not parse board listing from {url}. "
                    "HTML structure may have changed."
                )
                continue

            self.logger.info(f"Parsed {len(items)} items from {url}")
            for item in items:
                announcement = self._to_announcement(item, base_url)
                if announcement:
                    announcements.append(announcement)

        return self.enrich_with_quotes(announcements)

    def parse_list(
        self,
        soup: "BeautifulSoup",
        view_path: str,
        board_label: str,
    ) -> List[dict]:
        """목록 테이블에서 게시글 정보를 추출한다.

        Args:
            soup: BeautifulSoup 객체
            view_path: 상세 페이지 경로 (예: /notice/notice_01view.do)
            board_label: 게시판 분류 (예: 공지, 입찰/공모)

        Returns:
            공고 딕셔너리 리스트
        """
        table = soup.select_one("table.table_list") or soup.find("table")
        if table is None:
            return []

        body = table.find("tbody") or table
        items: List[dict] = []

        for row in body.find_all("tr"):
            title_cell = row.find("td", class_="title")
            if title_cell is None:
                continue

            a_tag = title_cell.find("a")
            if a_tag is None:
                continue

            # 배지(긴급/공지)는 제목이 아니라 분류로 분리한다
            badges = [
                b.get_text(strip=True)
                for b in a_tag.find_all("span", class_="badge")
            ]
            for badge in a_tag.find_all("span", class_="badge"):
                badge.extract()

            title = a_tag.get_text(strip=True)
            if not title:
                continue

            seq = self._extract_seq(a_tag.get("onclick", "") or a_tag.get("href", ""))
            link = f"{view_path}?bb_seq={seq}" if seq else ""

            cells = row.find_all("td")
            date_str = ""
            for cell in reversed(cells):
                text = cell.get_text(strip=True)
                if re.fullmatch(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text):
                    date_str = text
                    break

            # 구분 컬럼(입찰/공모 게시판의 '공모' 등)
            category_cell = ""
            if len(cells) >= 3 and cells[1] is not title_cell:
                candidate = cells[1].get_text(strip=True)
                if candidate and not candidate.isdigit():
                    category_cell = candidate

            category_parts = [p for p in ([board_label, category_cell] + badges) if p]
            # 중복 제거(순서 유지)
            category = "/".join(dict.fromkeys(category_parts))

            items.append({
                "seq": seq,
                "title": title,
                "link": link,
                "board": board_label,
                "category": category,
                "date": date_str,
            })

        return items

    @staticmethod
    def _extract_seq(raw: str) -> str:
        """onclick="fnGoView('12658')" 에서 게시글 seq를 추출한다."""
        if not raw:
            return ""
        match = re.search(r"fnGoView\(\s*['\"]?(\d+)", raw)
        if match:
            return match.group(1)
        match = re.search(r"bb_seq=(\d+)", raw)
        if match:
            return match.group(1)
        return ""

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
            source_id = item.get("seq", "") or hashlib.md5(
                title.encode("utf-8")
            ).hexdigest()[:16]

            # 13차: 제목 괄호 ``(~9.30)`` 추출기를 **폐기**했다. 그 패턴은
            # ``2025년 사업 결과 안내(~9.30)`` 같은 과거 결과 공고와 명시
            # 과거 연도를 계속 마감으로 만들었다 (9차 게이트 HIGH).
            # kofpi 는 기간을 만들지 않는다.
            posted = self._normalize_date(item.get("date", ""))

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
                author="한국임업진흥원",
                category=item.get("category", "").strip(),
                target="",
                period_start=None,
                period_end=None,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
