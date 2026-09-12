"""산림청 보도자료 크롤러 - 산림 정책/보도 수집"""
import re
from typing import List
from .forest_service import ForestServiceCrawler
from ..models import RawAnnouncement

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore


class ForestPressCrawler(ForestServiceCrawler):
    """산림청(forest.go.kr) 보도자료 게시판 크롤러.

    대상 URL:
        - https://www.forest.go.kr/kfsweb/cop/bbs/selectBoardList.do?bbsId=BBSMSTR_1036&mn=NKFS_04_02_01

    ForestServiceCrawler와 동일한 eGov 게시판 엔진이므로 URL/날짜/ID 처리 로직을
    상속해서 재사용하고, 보도자료 특유의 카드형 목록(``li > a > div.list_item``)
    파싱만 추가한다. 수집 결과는 category "정책/보도"로 구분한다.
    """

    BOARD_PATHS = [
        "/kfsweb/cop/bbs/selectBoardList.do?bbsId=BBSMSTR_1036&mn=NKFS_04_02_01",
    ]

    DEFAULT_CATEGORY = "정책/보도"

    def __init__(self):
        super().__init__(source_name="forest_press")

    def fetch(self) -> List[RawAnnouncement]:
        """산림청 보도자료 게시판에서 보도자료를 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL

        for board_path in self.BOARD_PATHS:
            url = f"{base_url}{board_path}"
            self.logger.info(f"Fetching from Forest Service press board: {url}")

            response = self.get(url)
            if response is None:
                self.logger.error(f"Failed to fetch board listing from {url}")
                continue

            response.encoding = response.apparent_encoding or "utf-8"
            soup = BeautifulSoup(response.text, "html.parser")

            # 전략 1: 보도자료 카드형 목록 (이 게시판의 실제 구조)
            items = self._parse_press_list(soup)
            if items:
                self.logger.info(f"Parsed {len(items)} items using press list strategy")
            else:
                # 전략 2~4: 산림청 공고 게시판과 동일한 폴백 체인
                items = (
                    self._parse_table_board(soup)
                    or self._parse_list_board(soup)
                    or self._parse_generic_links(soup)
                )

            if not items:
                self.logger.warning(
                    f"Could not parse board listing from {url}. "
                    "HTML structure may have changed."
                )
                continue

            for item in items:
                announcement = self._to_announcement(item, base_url)
                if announcement:
                    announcements.append(announcement)

        return announcements

    def _parse_press_list(self, soup: "BeautifulSoup") -> List[dict]:
        """보도자료 카드형 목록 파싱.

        구조:
            <li><a href="...selectBoardArticle.do;jsessionid=...?nttId=..."
                   title="전체 제목">
                <div class="list_item">
                  <div class="list_info">
                    <div class="info_title"><strong>[본청] 제목</strong>
                      <span class="sa_date">2026-09-11</span></div>
                    <p>요약</p>

        ``<strong>`` 안의 제목은 말줄임 처리되는 경우가 있어 ``a[title]``을 우선한다.

        Args:
            soup: BeautifulSoup 객체

        Returns:
            공고 딕셔너리 리스트
        """
        items: List[dict] = []
        seen_links = set()

        for card in soup.select("div.list_item"):
            a_tag = card.find_parent("a")
            if a_tag is None:
                continue

            href = self._strip_session_id(a_tag.get("href", ""))
            if not href or href in seen_links:
                continue

            title = (a_tag.get("title") or "").strip()
            if not title:
                strong = card.find("strong")
                title = strong.get_text(" ", strip=True) if strong else ""
            if not title:
                continue

            date_elem = card.find(class_="sa_date")
            date_str = date_elem.get_text(strip=True) if date_elem else ""

            summary_elem = card.find("p")
            summary = summary_elem.get_text(" ", strip=True) if summary_elem else ""

            author = ""
            strong = card.find("strong")
            if strong:
                bracket = re.search(r"\[([^\]]+)\]", strong.get_text(" ", strip=True))
                if bracket:
                    author = bracket.group(1).strip()

            seen_links.add(href)
            items.append({
                "title": title,
                "link": href,
                "author": f"산림청 {author}".strip() if author else "",
                "category": self.DEFAULT_CATEGORY,
                "date": date_str,
                "summary": summary,
            })

        return items

    def _to_announcement(self, item: dict, base_url: str):
        """상속받은 변환 로직에 보도자료 요약문을 덧붙인다.

        보도자료에는 접수 마감일이 없으므로, 게시일을 마감일로 오인하지 않도록
        period_end는 비운다(상속 로직은 단일 날짜를 시작=종료로 채운다).
        """
        announcement = super()._to_announcement(item, base_url)
        if announcement is not None:
            announcement.summary = item.get("summary", "").strip()
            announcement.period_end = None
        return announcement

    @staticmethod
    def _strip_session_id(link: str) -> str:
        """URL 경로에 붙은 ``;jsessionid=...`` 를 제거한다.

        세션 ID가 남아 있으면 같은 글이 매 수집마다 다른 URL로 저장된다.
        """
        if not link:
            return ""
        return re.sub(r";jsessionid=[^?#]*", "", link)
