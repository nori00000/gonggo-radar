"""사회적기업포털 SEIS 크롤러 - 사회적기업 지원사업 공고 수집"""
import hashlib
import json
import re
from collections import OrderedDict
from typing import List, Optional
from .base import BaseCrawler
from .dedupe_keys import group_key, normalize_title
from ..models import RawAnnouncement

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore


class SeisCrawler(BaseCrawler):
    """사회적기업포털 SEIS(www.seis.or.kr) 공고 게시판 크롤러.

    대상 URL:
        - https://www.seis.or.kr/front/board/boardList.do?boardId=BBS_0000001

    HTML 파싱 방식:
        GET 요청을 통해 공고 목록 페이지를 가져오고, 테이블 또는 리스트 구조를
        파싱하여 공고 정보를 추출한다.
    """

    BASE_URL = "https://www.seis.or.kr"
    # SEIS는 사회적기업 포털로 사이트가 전면 개편됨.
    # 기존 /front/board/ URL은 모두 mainPage.do로 리다이렉트됨.
    # 사업공고는 /subPage.do?menuId=30200 에서 확인 가능하며,
    # 메인 페이지에 사업공고 링크가 포함되어 있음.
    BOARD_PATHS = [
        "/mainPage.do",               # 메인 페이지 (사업공고 링크 포함)
        "/subPage.do?menuId=30200",    # 사업공고
        "/subPage.do?menuId=30400",    # 통합사업신청
    ]

    # 상세 페이지로 가는 링크 판별 패턴
    VIEW_LINK_PATTERNS = [
        re.compile(r"pbancMainView", re.I),
        re.compile(r"fncPbofrSn=", re.I),
        re.compile(r"dsgnPbofrSn=", re.I),
        re.compile(r"tabId=view", re.I),
        re.compile(r"tabId=certPageView", re.I),
        re.compile(r"itgrdAplyPbancSn=", re.I),
        re.compile(r"boardView", re.I),
        re.compile(r"view\.do", re.I),
        re.compile(r"nttId=", re.I),
        re.compile(r"detail", re.I),
    ]

    # 날짜 라벨 분류 (6차 게이트 #1). 목록의 날짜가 **게시일**인지
    # **접수기간**인지 라벨로 가린다. 가리지 못하면 게시일로 본다 -
    # 마감을 지어내지 않는 쪽이 안전하다.
    _POSTED_LABELS = re.compile(r"게시일|작성일|등록일|등록날짜|작성날짜|공고일|게시날짜")
    _PERIOD_LABELS = re.compile(r"접수|신청|모집|마감|공모|기간|일정")
    # "마감" 만 말하는 라벨은 **종료일만** 준다 - 시작일을 지어내지 않는다
    _DEADLINE_ONLY_LABELS = re.compile(r"마감")

    # 목록 카드의 D-day 배지 (지역/회차와 구분하기 위해 걸러낸다)
    _DDAY_RE = re.compile(r"^D-\s*(\d+|DAY|day)$|^마감$|^상시$")
    _ROUND_RE = re.compile(r"(\d+)\s*차")

    def __init__(self):
        super().__init__(source_name="seis")
        if BeautifulSoup is None:
            self.logger.error(
                "BeautifulSoup4 is not installed. "
                "Install it with: pip install beautifulsoup4"
            )

    def fetch(self) -> List[RawAnnouncement]:
        """사회적기업포털 SEIS 공고 게시판에서 공고를 수집한다."""
        if BeautifulSoup is None:
            self.logger.error("BeautifulSoup4 is required but not installed")
            return []

        announcements: List[RawAnnouncement] = []
        base_url = self.get_base_url() or self.BASE_URL

        for board_path in self.BOARD_PATHS:
            url = f"{base_url}{board_path}"
            self.logger.info(f"Fetching from SEIS announcement board: {url}")

            items = self._fetch_board_listing(url)
            if items:
                self.logger.info(f"Successfully fetched {len(items)} items from {url}")
                for item in items:
                    announcement = self._to_announcement(item, base_url)
                    if announcement:
                        announcements.append(announcement)
                break

        return self.enrich_with_quotes(announcements)

    def _fetch_board_listing(self, url: str) -> List[dict]:
        """공고 목록 페이지를 파싱하여 공고 목록을 추출한다."""
        response = self.get(url)
        if response is None:
            self.logger.error(f"Failed to fetch board listing from {url}")
            return []

        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        items: List[dict] = []

        # 전략 1: 메인 페이지 공고 카드(li.swiper-slide > p.tit)
        # 카드에는 분류/지역/회차/접수기간이 함께 있어 중복 판별과 마감 추출이 된다.
        items = self._parse_main_cards(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using card strategy")
            return self._dedupe_items(items)

        # 전략 2: table 기반 게시판
        items = self._parse_table_board(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using table strategy")
            return self._dedupe_items(items)

        # 전략 3: div/ul 기반 게시판
        items = self._parse_list_board(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using list strategy")
            return self._dedupe_items(items)

        # 전략 4: 범용 링크 추출
        items = self._parse_generic_links(soup)
        if items:
            self.logger.info(f"Parsed {len(items)} items using generic link strategy")
            return self._dedupe_items(items)

        self.logger.warning(
            f"Could not parse board listing from {url}. "
            "HTML structure may have changed."
        )
        return []

    @staticmethod
    def _clean(text: str) -> str:
        """공백/개행/&nbsp; 를 한 칸으로 정리한다."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()

    def _is_view_link(self, href: str) -> bool:
        """상세 페이지로 가는 링크인지 판별한다."""
        if not href:
            return False
        return any(p.search(href) for p in self.VIEW_LINK_PATTERNS)

    def _parse_main_cards(self, soup: "BeautifulSoup") -> List[dict]:
        """메인 페이지 공고 카드를 파싱한다.

        카드 구조::

            <li class="swiper-slide" data-type="재정지원">
              <span class="badge cate">재정지원</span>
              <span class="sub">사회보험료 지원 사업</span>
              <p class="tit"><a href="subPage.do?...&fncPbofrSn=8371">제목</a></p>
              <ul class="info"><li>경기도</li><li>2026년도 (9차)</li><li>D-109</li></ul>
              <p class="date">2026.09.01 ~ 2026.12.31</p>
            </li>

        기존 범용 링크 추출은 제목과 링크만 봤기 때문에 같은 공고의 회차별
        링크를 각각 별건으로 적재했다(2026-09-12 실측: 동일 공고 9행).
        카드 단위로 읽으면 지역/회차/접수기간이 함께 잡혀 중복 판별이 된다.

        Args:
            soup: 목록 페이지 BeautifulSoup 객체

        Returns:
            공고 딕셔너리 리스트
        """
        items: List[dict] = []

        for tit in soup.select("p.tit"):
            a_tag = tit.find("a", href=True)
            if a_tag is None:
                continue

            href = a_tag.get("href", "")
            if not self._is_view_link(href):
                continue

            title = self._clean(a_tag.get_text(strip=True))
            if not title:
                continue

            card = tit.find_parent("li") or tit.find_parent("div", class_="link")
            if card is None:
                card = tit.parent

            badge = card.select_one("span.badge")
            sub = card.select_one("span.sub")
            date_elem = card.select_one("p.date")

            category = self._clean(badge.get_text(strip=True)) if badge else ""
            if not category:
                category = self._clean(card.get("data-type", "") or "")

            # ul.info 는 **분류**(교육/시설·공간/행사 등)와 회차·D-day가 섞여
            # 들어오는 자리다. 지역으로 오인하면 "교육" 같은 값이 지역이 되어
            # 서울센터/부산센터 공고가 한 건으로 병합된다(Codex 크리틱 #4).
            # 주체(지역·기관·사업명)는 span.sub 만 본다.
            info_values = []
            round_label = ""
            for info in card.select("ul.info li"):
                value = self._clean(info.get_text(strip=True))
                if not value or self._DDAY_RE.match(value):
                    continue
                if self._ROUND_RE.search(value):
                    round_label = round_label or value
                    continue
                info_values.append(value)

            items.append({
                "title": title,
                "link": href,
                "author": "",
                "category": category,
                "date": self._clean(date_elem.get_text(strip=True)) if date_elem else "",
                "sub": self._clean(sub.get_text(strip=True)) if sub else "",
                "info": info_values,
                "round": round_label,
            })

        return items

    @staticmethod
    def _normalize_title(title: str) -> str:
        """중복 판별용 제목 정규화 - 공백과 구분기호를 없앤다."""
        return normalize_title(title)

    def _canonical_rank(self, item: dict) -> tuple:
        """같은 공고 묶음에서 대표를 고르는 순위 - 최신 회차가 이긴다."""
        round_match = self._ROUND_RE.search(item.get("round", "") or "")
        round_no = int(round_match.group(1)) if round_match else -1
        post_id = self._extract_post_id(item.get("link", ""))
        post_no = int(post_id) if post_id.isdigit() else -1
        return (item.get("date", "") or "", round_no, post_no)

    def _group_key(self, item: dict) -> tuple:
        """중복 판별 키 - 제목·지역·접수 종료일이 **모두** 같아야 한 공고다.

        지역은 ``span.sub`` 와 ``ul.info`` 를 **함께** 후보로 넣어 그중
        지역을 말하는 값을 쓴다(Codex 재검토 #4). 한쪽만 보면 사업명이 같고
        지역만 다른 공고(서울/부산)가 한 건으로 합쳐진다.

        접수 종료일을 키에 넣는 이유: 같은 제목의 1차·2차 공고는 접수기간이
        다른 **별개 공고**다. 종료일이 다르면 병합하지 않는다.

        정리 스크립트의 규칙 B도 같은 함수(``dedupe_keys.group_key``)를 쓴다.
        """
        _, period_end, _posted = self._classify_date(
            item.get("date", ""), item.get("date_label", "")
        )
        candidates = [item.get("sub", "") or ""]
        candidates.extend(item.get("info", []) or [])
        # 회차 후보: 카드의 round 필드 + 주체 + 분류 (제목은 group_key가 본다)
        rounds = [item.get("round", "") or ""] + candidates
        return group_key(
            item.get("title", ""), candidates, period_end, round_candidates=rounds
        )

    def _dedupe_items(self, items: List[dict]) -> List[dict]:
        """같은 공고의 복수 링크를 1건으로 합친다 (계약 v2.1 판정 6-①).

        SEIS 메인은 회차마다 별개 링크(``fncPbofrSn``)를 나열하므로
        **제목·주체(span.sub)·접수 종료일이 모두 같을 때만** 한 공고로 본다.
        대표는 접수 시작일이 가장 늦고 회차 번호가 가장 큰 항목(= 현재 진행
        회차)이며, 병합된 나머지 링크의 ID와 회차는 감사할 수 있도록
        ``merged_source_ids`` 에 남긴다.

        Args:
            items: 파싱된 공고 딕셔너리 리스트

        Returns:
            공고당 1건으로 정리된 리스트 (입력 순서 유지)
        """
        groups: "OrderedDict[tuple, List[dict]]" = OrderedDict()
        for item in items:
            groups.setdefault(self._group_key(item), []).append(item)

        deduped: List[dict] = []
        for group in groups.values():
            canonical = max(group, key=self._canonical_rank)
            if len(group) > 1:
                canonical = dict(canonical)
                canonical["merged_count"] = len(group)
                canonical["merged_source_ids"] = [
                    self._extract_post_id(other.get("link", ""))
                    for other in group
                    if other.get("link", "") != canonical.get("link", "")
                ]
                canonical["merged_rounds"] = [
                    other.get("round", "") for other in group if other.get("round", "")
                ]
                self.logger.info(
                    f"Merged {len(group)} duplicate links into one announcement: "
                    f"{canonical.get('title', '')[:40]}"
                )
            deduped.append(canonical)

        return deduped

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

        headers = self._header_labels(table)

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
                    date_label = self._label_for(cell, cells, headers, css_class)

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
                        # 표 헤더에서 이 컬럼의 라벨을 찾는다 - "게시일" 이면
                        # 기간이 아니다 (6차 게이트 #1)
                        date_label = self._label_for(cell, cells, headers, "")
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

    @staticmethod
    def _header_labels(table) -> List[str]:
        """표의 컬럼 라벨(헤더 텍스트)을 순서대로 읽는다."""
        head = table.find("thead")
        header_row = None
        if head is not None:
            header_row = head.find("tr")
        if header_row is None:
            for row in table.find_all("tr"):
                if row.find("th") is not None:
                    header_row = row
                    break
        if header_row is None:
            return []
        return [
            re.sub(r"\s+", " ", cell.get_text(strip=True))
            for cell in header_row.find_all(["th", "td"])
        ]

    @staticmethod
    def _label_for(cell, cells, headers: List[str], css_class: str) -> str:
        """이 셀의 컬럼 라벨을 찾는다 (표 헤더 -> 행의 th -> class 이름)."""
        try:
            index = cells.index(cell)
        except ValueError:
            index = -1
        if 0 <= index < len(headers) and headers[index]:
            return headers[index]
        # 헤더가 없으면 같은 행의 th 를 본다 (세로 표)
        parent = getattr(cell, "parent", None)
        if parent is not None:
            own_header = parent.find("th")
            if own_header is not None:
                return re.sub(r"\s+", " ", own_header.get_text(strip=True))
        return css_class

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

            date_str = ""
            date_label = ""
            date_elem = item_elem.find(
                ["span", "em", "div"],
                class_=re.compile(r"date|period|term|time", re.I)
            )
            if date_elem:
                date_str = date_elem.get_text(strip=True)
                date_label = " ".join(date_elem.get("class", []) or [])
            else:
                text = item_elem.get_text(" ", strip=True)
                date_match = re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
                if date_match:
                    date_str = date_match.group()
                    # 날짜 앞에 붙은 말을 라벨로 본다 ("게시일 2026.09.11")
                    date_label = text[max(0, date_match.start() - 12):date_match.start()]

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

        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            title_text = self._clean(a_tag.get_text(strip=True))

            if not title_text or len(title_text) < 5:
                continue

            if not self._is_view_link(href):
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

    def _extract_post_id(self, link: str) -> str:
        """URL에서 공고 ID를 추출한다."""
        if not link:
            return ""

        # 공고 종류별 고유 ID를 먼저 본다. 범용 파라미터(seq/idx/no)는 반드시
        # ?/& 뒤에서만 인정한다 - 그렇지 않으면 "epsdNo=4" 가 "no=4" 로 잡히는
        # 접두사 오매칭이 생겨 서로 다른 공고가 같은 ID를 쓰게 된다.
        id_params = [
            r"fncPbofrSn=(\d+)", r"dsgnPbofrSn=(\d+)",
            r"itgrdAplyPbancSn=(\d+)", r"epsdNo=(\d+)",
            r"announcementId=(\d+)", r"notifyId=(\d+)", r"nttId=(\d+)",
            r"articleId=(\d+)", r"artclId=(\d+)",
            r"[?&]seq=(\d+)", r"[?&]idx=(\d+)", r"[?&]no=(\d+)",
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
        # 상대 경로 (예: subPage.do?menuId=...) -> 절대 경로로 변환
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

    def _classify_date(
        self, date_str: str, label: str = ""
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """목록의 날짜가 **접수기간**인지 **게시일**인지 가른다 (6차 게이트 #1).

        게시일을 ``period_start``/``period_end`` 에 넣으면 브리핑이 게시
        다음 날 만료된 공고를 말하거나, 존재하지 않는 접수기간을 말한다.
        실측: 목록 HTML의 ``게시일=2026.09.11`` 이
        ``period_start=period_end=2026-09-11`` 로 저장됐다.

        판정 순서:

        1. 라벨이 게시일/작성일/등록일 → **게시일** (기간 아님)
        2. 값에 범위 표기(``~``)가 있으면 → 접수기간
        3. 라벨이 접수/신청/모집/마감/기간 → 접수기간
        4. 그 밖의 단일 날짜 → **게시일** (모르면 마감을 만들지 않는다)

        Args:
            date_str: 목록에서 읽은 날짜 문자열
            label: 그 날짜의 컬럼 라벨(표 헤더 등)

        Returns:
            ``(period_start, period_end, posted)``
        """
        text = (date_str or "").strip()
        if not text:
            return None, None, None

        label = (label or "").strip()
        if label and self._POSTED_LABELS.search(label):
            return None, None, self._normalize_date(text)

        if re.search(r"[~\u223c\u301c]", text):
            start, end = self._parse_period(text)
            return start, end, None

        if label and self._PERIOD_LABELS.search(label):
            start, end = self._parse_period(text)
            if self._DEADLINE_ONLY_LABELS.search(label) and start == end:
                # "접수마감 2026.09.30" 은 마감일 하나다 (시작일 아님)
                return None, end, None
            return start, end, None

        # 라벨이 없거나 모르는 단일 날짜는 게시일로 본다
        return None, None, self._normalize_date(text)

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
            source_id = self._extract_post_id(link)

            if not source_id:
                source_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

            author = item.get("author", "").strip()
            category = item.get("category", "").strip()

            period_start, period_end, posted = self._classify_date(
                item.get("date", ""), item.get("date_label", "")
            )

            payload = dict(item)
            if posted:
                # 게시일은 기간 필드가 아니라 raw_data 에 남긴다
                payload["posted"] = posted
            raw_data = json.dumps(payload, ensure_ascii=False)

            return RawAnnouncement(
                source="seis",
                source_id=source_id,
                title=title,
                url=link,
                summary="",
                author=author or "한국사회적기업진흥원",
                category=category,
                target="",
                period_start=period_start,
                period_end=period_end,
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting item to announcement: {e}")
            self.logger.debug(f"Item data: {item}")
            return None
