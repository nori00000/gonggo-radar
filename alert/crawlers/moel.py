"""고용노동부 공지사항 크롤러 - moel.go.kr 알림·공고 수집 (P2-S 계약 §3)"""
import hashlib
import json
import re
from typing import List, Optional
from .base import BaseCrawler
from .date_labels import header_labels, posted_date
from ..models import RawAnnouncement

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore


class MoelCrawler(BaseCrawler):
    """고용노동부(moel.go.kr) 공지사항 게시판 크롤러.

    대상: https://www.moel.go.kr/news/notice/noticeList.do (공지사항, bbs_id=9)

    2026-09-15 실측: 목록은 서버 렌더링 정적 HTML(``table.tstyle_list``)이다.
    제목 링크는 ``strong.b_tit > a[href*="bbs_seq="]`` 이고, 같은 행의 첨부
    다운로드 링크도 ``bbs_seq`` 를 쓰므로 **제목 링크만** 골라야 한다. 링크
    텍스트에는 ``[공고]`` 머리표가 붙고 ``title`` 속성에는 머리표 없는 제목이
    들어 있다 - 제목은 속성에서, 분류는 머리표에서 읽는다.
    기간: **전용 추출기가 없다**. ``등록일`` 은 게시일이므로 기간 두 필드를
    만들지 않고 ``raw_data.posted`` 로만 남긴다 (13차 규칙).
    """

    BASE_URL = "https://www.moel.go.kr"
    LIST_PATH = "/news/notice/noticeList.do"
    VIEW_PATH = "/news/notice/noticeView.do?bbs_seq={seq}"

    BBS_SEQ_PATTERN = re.compile(r"[?&]bbs_seq=(\d+)")
    # 링크 텍스트 앞머리의 분류 머리표: "[공고] ...", "[포상대상자공개] ..."
    CATEGORY_PATTERN = re.compile(r"^\s*\[([^\]]{1,20})\]\s*")

    def __init__(self):
        super().__init__(source_name="moel")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """고용노동부 공지사항을 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        base_url = self.get_base_url() or self.BASE_URL
        url = f"{base_url}{self.LIST_PATH}"
        self.logger.info(f"Fetching from MOEL notice board: {url}")

        response = self.get(url)
        if response is None:
            self.logger.error(f"Failed to fetch board listing from {url}")
            return []

        response.encoding = response.apparent_encoding or "utf-8"
        items = self.parse_list(BeautifulSoup(response.text, "html.parser"))
        if not items:
            self.logger.warning(
                f"Could not parse board listing from {url}. "
                "HTML structure may have changed."
            )
            return []
        self.logger.info(f"Parsed {len(items)} items from {url}")

        announcements = [self._to_announcement(i, base_url) for i in items]
        return [a for a in announcements if a is not None]

    def parse_list(self, soup: "BeautifulSoup") -> List[dict]:
        """공지사항 목록 표를 파싱한다 (``soup``: 목록 페이지). 항목 키는
        bbs_seq·title·category·author·date.
        """
        items: List[dict] = []
        seen_ids = set()

        table = soup.select_one("table.tstyle_list")
        if table is None:
            return items

        # 칸 번호는 **표 헤더**에서 읽는다. 라벨도 헤더도 못 찾으면 날짜를
        # 포기한다 - "날짜처럼 보이는 아무 칸" 으로 되짚으면 제목 칸이 날짜만
        # 담고 있을 때 그게 게시일이 된다 (라운드 2 MEDIUM).
        labels = header_labels(table)
        columns = {
            name: (labels.index(name) if name in labels else -1)
            for name in ("등록일", "담당부서")
        }

        for row in table.select("tbody tr"):
            a_tag = row.select_one("strong.b_tit a")
            if a_tag is None:
                continue

            raw_text = a_tag.get_text(" ", strip=True)
            category = ""
            match = self.CATEGORY_PATTERN.match(raw_text)
            if match:
                category = match.group(1).strip()

            title = (a_tag.get("title") or "").strip()
            if not title:
                title = self.CATEGORY_PATTERN.sub("", raw_text).strip()
            if not title:
                continue

            bbs_seq = self._extract_bbs_seq(a_tag.get("href", ""))
            if bbs_seq:
                if bbs_seq in seen_ids:
                    continue
                seen_ids.add(bbs_seq)

            items.append({
                "bbs_seq": bbs_seq,
                "title": title,
                "category": category,
                "author": self._cell_text(row, "담당부서", columns, a_tag),
                "date": self._cell_text(row, "등록일", columns, a_tag),
                # 이 표의 날짜 칸은 표 헤더가 "등록일" 이다 = 게시일
                "date_label": "등록일",
            })

        return items

    @staticmethod
    def _cell_text(row, aria_label: str, columns: dict, title_link) -> str:
        """``aria_label`` 이 가리키는 칸의 텍스트를 읽는다 (못 찾으면 "").

        근거는 **둘 뿐**이다: ``aria-label`` 속성(고용노동부 표는 모든 ``td``
        에 달아 둔다), 아니면 ``columns`` 가 헤더에서 센 칸 번호. 제목 칸
        (``title_link`` 이 든 칸)은 어떤 경우에도 후보가 아니다 - 제목이
        날짜 하나뿐인 공고가 있으면 "날짜처럼 보이는 첫 칸" 규칙이 그 제목을
        게시일로 만든다 (라운드 2 MEDIUM).
        """
        cells = row.find_all("td")
        title_cell = title_link.find_parent("td") if title_link is not None else None

        cell = row.find("td", attrs={"aria-label": aria_label})
        if cell is not None and cell is not title_cell:
            return cell.get_text(" ", strip=True)

        index = columns.get(aria_label, -1)
        if 0 <= index < len(cells):
            candidate = cells[index]
            if candidate is not title_cell:
                return candidate.get_text(" ", strip=True)

        return ""

    @classmethod
    def _extract_bbs_seq(cls, href: str) -> str:
        """``…/noticeView.do?bbs_seq=20260900480`` 에서 번호를 뽑는다."""
        match = cls.BBS_SEQ_PATTERN.search(href or "")
        return match.group(1) if match else ""

    def _to_announcement(self, item: dict, base_url: str) -> Optional[RawAnnouncement]:
        """파싱된 항목을 RawAnnouncement로 변환한다."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            bbs_seq = item.get("bbs_seq", "")
            if bbs_seq:
                url = f"{base_url}{self.VIEW_PATH.format(seq=bbs_seq)}"
                source_id = bbs_seq
            else:
                url = f"{base_url}{self.LIST_PATH}"
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            # 목록 날짜는 **등록일**이다 - 접수기간이 아니다. 전용 추출기가
            # 없으므로 기간 두 필드는 항상 None 이고, 날짜는 raw_data.posted 다.
            payload = dict(item)
            posted = posted_date(item.get("date", ""))
            if posted:
                payload["posted"] = posted
            raw_data = json.dumps(payload, ensure_ascii=False)

            return RawAnnouncement(
                source=self.source_name,
                source_id=source_id,
                title=title,
                url=url,
                summary="",
                author=item.get("author", "").strip() or "고용노동부",
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
