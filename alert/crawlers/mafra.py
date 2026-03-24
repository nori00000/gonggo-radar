"""농림축산식품부 RSS 크롤러 - 정책/공지사항 수집"""
import hashlib
import xml.etree.ElementTree as ET
from typing import List, Optional
from .base import BaseCrawler
from ..models import RawAnnouncement


class MafraCrawler(BaseCrawler):
    """농림축산식품부(MAFRA) RSS 피드를 통한 공고/정책 수집.

    RSS 피드 URL: https://www.mafra.go.kr/bbs/mafra/71/rss.xml

    RSS 피드 구조 (예상):
        <rss version="2.0">
          <channel>
            <title>농림축산식품부</title>
            <item>
              <title>공고 제목</title>
              <link>상세 URL</link>
              <description>요약</description>
              <pubDate>발행일</pubDate>
              <author>작성자</author>
              <category>분류</category>
            </item>
            ...
          </channel>
        </rss>
    """

    RSS_URL = "https://www.mafra.go.kr/bbs/mafra/71/rss.xml"

    # 대체 RSS URL 목록 (구조 변경 시 폴백)
    FALLBACK_RSS_URLS = [
        "https://www.mafra.go.kr/bbs/mafra/71/rss.xml",
        "https://www.mafra.go.kr/bbs/mafra/68/rss.xml",
    ]

    def __init__(self):
        super().__init__(source_name="mafra")

    def fetch(self) -> List[RawAnnouncement]:
        """RSS 피드에서 공고 목록을 수집한다.

        Returns:
            RawAnnouncement 리스트
        """
        announcements: List[RawAnnouncement] = []

        # 설정에 base_url이 있으면 그것을 사용, 없으면 기본 RSS URL
        rss_url = self.get_base_url() or self.RSS_URL

        xml_text = self._fetch_rss(rss_url)
        if xml_text is None:
            # 기본 URL 실패 시 폴백 URL 시도
            for fallback_url in self.FALLBACK_RSS_URLS:
                if fallback_url == rss_url:
                    continue
                self.logger.info(f"Trying fallback RSS URL: {fallback_url}")
                xml_text = self._fetch_rss(fallback_url)
                if xml_text is not None:
                    break

        if xml_text is None:
            self.logger.error("Failed to fetch RSS feed from all URLs")
            return []

        items = self._parse_rss_xml(xml_text)
        if not items:
            self.logger.warning("No items found in RSS feed")
            return []

        self.logger.info(f"Found {len(items)} items in MAFRA RSS feed")

        for item_data in items:
            announcement = self._to_announcement(item_data)
            if announcement:
                announcements.append(announcement)

        return announcements

    def _fetch_rss(self, url: str) -> Optional[str]:
        """RSS 피드 XML을 가져온다.

        Args:
            url: RSS 피드 URL

        Returns:
            XML 텍스트, 실패 시 None
        """
        response = self.get(url)
        if response is None:
            return None

        # 인코딩 처리: UTF-8 우선, EUC-KR 폴백
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def _parse_rss_xml(self, xml_text: str) -> List[dict]:
        """RSS XML을 파싱하여 item 목록을 추출한다.

        Args:
            xml_text: RSS XML 문자열

        Returns:
            item 딕셔너리 리스트
        """
        items = []

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            self.logger.error(f"Failed to parse RSS XML: {e}")
            self.logger.debug(f"XML content (first 500 chars): {xml_text[:500]}")
            return []

        # RSS 2.0 구조: rss > channel > item
        # 네임스페이스가 있을 수 있으므로 다양한 패턴 시도
        channel = root.find("channel")
        if channel is None:
            # Atom 형식이거나 다른 구조일 수 있음
            channel = root

        for item_elem in channel.findall("item"):
            item_data = {}

            # 기본 RSS 요소 추출
            for tag in ["title", "link", "description", "pubDate", "author", "category"]:
                elem = item_elem.find(tag)
                if elem is not None and elem.text:
                    item_data[tag] = elem.text.strip()
                else:
                    item_data[tag] = ""

            # link가 비어 있으면 guid 시도
            if not item_data.get("link"):
                guid_elem = item_elem.find("guid")
                if guid_elem is not None and guid_elem.text:
                    item_data["link"] = guid_elem.text.strip()

            # dc:date 네임스페이스도 시도 (pubDate가 비어 있는 경우)
            if not item_data.get("pubDate"):
                for ns_prefix in [
                    "{http://purl.org/dc/elements/1.1/}",
                    "{http://purl.org/dc/terms/}",
                ]:
                    date_elem = item_elem.find(f"{ns_prefix}date")
                    if date_elem is not None and date_elem.text:
                        item_data["pubDate"] = date_elem.text.strip()
                        break

            items.append(item_data)

        # Atom 형식 폴백: entry 요소 탐색
        if not items:
            atom_ns = "{http://www.w3.org/2005/Atom}"
            for entry in root.findall(f"{atom_ns}entry") or root.findall("entry"):
                item_data = {}
                title_elem = entry.find(f"{atom_ns}title") or entry.find("title")
                item_data["title"] = title_elem.text.strip() if title_elem is not None and title_elem.text else ""

                link_elem = entry.find(f"{atom_ns}link") or entry.find("link")
                if link_elem is not None:
                    item_data["link"] = link_elem.get("href", "").strip()
                else:
                    item_data["link"] = ""

                summary_elem = entry.find(f"{atom_ns}summary") or entry.find("summary")
                item_data["description"] = summary_elem.text.strip() if summary_elem is not None and summary_elem.text else ""

                updated_elem = entry.find(f"{atom_ns}updated") or entry.find("updated")
                item_data["pubDate"] = updated_elem.text.strip() if updated_elem is not None and updated_elem.text else ""

                item_data["author"] = ""
                item_data["category"] = ""

                items.append(item_data)

        return items

    def _generate_source_id(self, link: str, title: str) -> str:
        """링크(또는 제목)로부터 source_id를 생성한다.

        URL에서 게시물 ID를 추출하거나, 없으면 link의 해시를 사용한다.

        Args:
            link: 게시물 URL
            title: 게시물 제목 (폴백용)

        Returns:
            고유 source_id 문자열
        """
        if link:
            # URL에서 숫자 ID 패턴 추출 시도
            # 예: /bbs/mafra/71/322810/artclView.do -> 322810
            import re
            match = re.search(r"/(\d{5,})", link)
            if match:
                return match.group(1)

            # nttId 파라미터 추출 시도
            match = re.search(r"[?&]nttId=(\d+)", link)
            if match:
                return match.group(1)

            # URL 해시
            return hashlib.md5(link.encode("utf-8")).hexdigest()[:16]

        # link가 없으면 제목 해시
        return hashlib.md5(title.encode("utf-8")).hexdigest()[:16]

    def _to_announcement(self, item_data: dict) -> Optional[RawAnnouncement]:
        """RSS item 딕셔너리를 RawAnnouncement로 변환한다.

        Args:
            item_data: RSS item 데이터

        Returns:
            RawAnnouncement 객체, 파싱 실패 시 None
        """
        try:
            title = item_data.get("title", "").strip()
            link = item_data.get("link", "").strip()

            if not title:
                self.logger.warning(f"Skipping item with empty title: {item_data}")
                return None

            source_id = self._generate_source_id(link, title)
            description = item_data.get("description", "").strip()
            author = item_data.get("author", "").strip()
            category = item_data.get("category", "").strip()
            pub_date = item_data.get("pubDate", "").strip()

            # raw_data에 원본 데이터 저장
            import json
            raw_data = json.dumps(item_data, ensure_ascii=False)

            return RawAnnouncement(
                source="mafra",
                source_id=source_id,
                title=title,
                url=link,
                summary=description,
                author=author or "농림축산식품부",
                category=category,
                period_start=self._normalize_pub_date(pub_date),
                raw_data=raw_data,
            )

        except Exception as e:
            self.logger.error(f"Error converting RSS item to announcement: {e}")
            self.logger.debug(f"Item data: {item_data}")
            return None

    def _normalize_pub_date(self, pub_date: str) -> Optional[str]:
        """pubDate 문자열을 ISO 날짜 형식으로 변환한다.

        지원 형식:
            - RFC 822: "Mon, 01 Jan 2024 09:00:00 +0900"
            - ISO 8601: "2024-01-01T09:00:00+09:00"
            - 한국식: "2024.01.01", "2024-01-01"

        Args:
            pub_date: pubDate 문자열

        Returns:
            ISO 형식 날짜 문자열 (YYYY-MM-DD), 실패 시 None
        """
        if not pub_date:
            return None

        from datetime import datetime
        import re

        # ISO 8601 형식
        try:
            dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

        # RFC 822 형식: "Mon, 01 Jan 2024 09:00:00 +0900"
        rfc_formats = [
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S",
            "%d %b %Y %H:%M:%S %z",
            "%d %b %Y %H:%M:%S",
        ]
        for fmt in rfc_formats:
            try:
                dt = datetime.strptime(pub_date, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        # 한국식 날짜: "2024.01.01" 또는 "2024-01-01"
        match = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", pub_date)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        # YYYYMMDD
        match = re.match(r"^(\d{4})(\d{2})(\d{2})", pub_date)
        if match:
            year, month, day = match.groups()
            return f"{year}-{month}-{day}"

        self.logger.warning(f"Unrecognized pubDate format: {pub_date}")
        return None
