"""중소벤처기업부 사업공고 크롤러 - mss.go.kr 사업공고 수집 (P2-S 계약 §3)"""
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


class MssCrawler(BaseCrawler):
    """중소벤처기업부(mss.go.kr) 사업공고 게시판 크롤러.

    대상 URL:
        - https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=310 (사업공고)

    2026-09-15 실측: 목록은 서버 렌더링 정적 HTML이다. 게시글 행은
    ``<tr onclick="doBbsFView('310','1071161','16010100','1071161');" title="…">``
    이고, 제목 칸의 ``div.tableInfoBox`` 에 담당부서·공고번호·신청기간이
    ``dl/dt/dd`` 로 붙는다. 행이 모바일용으로 한 번 더 그려지므로
    (``td.mobile``) 칸 순서로 읽으면 중복이 섞인다 - 속성·클래스로만 읽는다.
    기간: **전용 추출기가 없다**. 목록에 ``신청기간`` 이 보여도 기간 두 필드를
    만들지 않고 ``raw_data.apply_period_text`` 근거로만 남긴다.
    """

    BASE_URL = "https://www.mss.go.kr"

    CB_IDX = "310"
    LIST_PATH = "/site/smba/ex/bbs/List.do?cbIdx={cb}"
    VIEW_PATH = "/site/smba/ex/bbs/View.do?cbIdx={cb}&bcIdx={bc}&parentSeq={parent}"

    # doBbsFView('<cbIdx>','<bcIdx>','<tgtTypeCd>','<parentSeq>')
    ONCLICK_PATTERN = re.compile(
        r"doBbsFView\(\s*'([^']*)'\s*,\s*'(\d+)'\s*,\s*'([^']*)'\s*,\s*'(\d+)'\s*\)"
    )
    FULL_DATE_PATTERN = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}")
    # 모바일 중복 마크업과 첨부 칸은 제목·날짜 후보에서 뺀다
    SKIP_CELL_CLASSES = frozenset({"subject", "mobile", "attached-files"})

    def __init__(self):
        super().__init__(source_name="mss")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """중소벤처기업부 사업공고를 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        base_url = self.get_base_url() or self.BASE_URL
        url = f"{base_url}{self.LIST_PATH.format(cb=self.CB_IDX)}"
        self.logger.info(f"Fetching from MSS business announcement board: {url}")

        response = self.get(url)
        if response is None:
            self.logger.error(f"Failed to fetch board listing from {url}")
            return []

        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        items = self.parse_list(soup)
        if not items:
            self.logger.warning(
                f"Could not parse board listing from {url}. "
                "HTML structure may have changed."
            )
            return []

        self.logger.info(f"Parsed {len(items)} items from {url}")

        announcements: List[RawAnnouncement] = []
        for item in items:
            announcement = self._to_announcement(item, base_url)
            if announcement:
                announcements.append(announcement)
        return announcements

    def parse_list(self, soup: "BeautifulSoup") -> List[dict]:
        """사업공고 목록 표를 파싱한다 (``soup``: 목록 페이지). 항목 키는
        bc_idx·parent_seq·title·author·date·notice_no·apply_period_text.
        """
        items: List[dict] = []
        seen_ids = set()

        for row in soup.find_all("tr", onclick=True):
            match = self.ONCLICK_PATTERN.search(row.get("onclick", ""))
            if not match:
                continue

            cb_idx, bc_idx, _tgt_type, parent_seq = match.groups()
            if bc_idx in seen_ids:
                continue
            seen_ids.add(bc_idx)

            title = (row.get("title") or "").strip()
            if not title:
                link = row.select_one("td.subject a")
                title = (link.get_text(" ", strip=True) if link else "").strip()
            if not title:
                continue

            info = self._info_box(row)
            items.append({
                "cb_idx": cb_idx or self.CB_IDX,
                "bc_idx": bc_idx,
                "parent_seq": parent_seq,
                "title": title,
                "author": info.get("담당부서", ""),
                "notice_no": info.get("공고번호", ""),
                # 신청기간은 **증거 문자열로만** 싣는다 - 기간 필드가 되지 않는다
                "apply_period_text": info.get("신청기간", ""),
                "date": self._posted_cell(row),
                "date_label": "등록일",
            })

        return items

    @staticmethod
    def _info_box(row) -> dict:
        """``div.tableInfoBox`` 의 ``dt`` -> ``dd`` 쌍을 읽는다."""
        info: dict = {}
        box = row.select_one("td.subject div.tableInfoBox")
        if box is None:
            return info
        for definition in box.find_all("dl"):
            term = definition.find("dt")
            value = definition.find("dd")
            if term is None or value is None:
                continue
            key = term.get_text(" ", strip=True)
            if key and key not in info:
                info[key] = value.get_text(" ", strip=True)
        return info

    @classmethod
    def _posted_cell(cls, row) -> str:
        """등록일 칸을 고른다 - 제목 칸(신청기간 포함)과 모바일 중복 칸을
        건너뛴 뒤 **날짜 하나만** 든 칸을 쓴다. 신청기간
        (``2026-09-14 ~ 2026-10-13``)은 ``fullmatch`` 에 걸리지 않는다.
        """
        for cell in row.find_all("td", recursive=False):
            classes = set(cell.get("class", []))
            if classes & cls.SKIP_CELL_CLASSES:
                continue
            text = cell.get_text(" ", strip=True)
            if cls.FULL_DATE_PATTERN.fullmatch(text):
                return text
        return ""

    def _to_announcement(self, item: dict, base_url: str) -> Optional[RawAnnouncement]:
        """파싱된 항목을 RawAnnouncement로 변환한다."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            bc_idx = item.get("bc_idx", "")
            if bc_idx:
                url = f"{base_url}{self.VIEW_PATH.format(cb=item.get('cb_idx') or self.CB_IDX, bc=bc_idx, parent=item.get('parent_seq', ''))}"
                source_id = bc_idx
            else:
                url = f"{base_url}{self.LIST_PATH.format(cb=self.CB_IDX)}"
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            # 전용 추출기가 없는 소스 = 기간 두 필드는 항상 None. 목록의
            # 신청기간은 raw_data 근거로만 남고, 등록일은 raw_data.posted 다.
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
                author=item.get("author", "").strip() or "중소벤처기업부",
                category="사업공고",
                target="",
                period_start=None,
                period_end=None,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
