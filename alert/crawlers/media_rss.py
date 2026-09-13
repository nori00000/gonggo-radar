"""2차 미디어 RSS 크롤러 — 협의회 월간호용 (P1-R 계약).

라이프인 · 이로운넷 · 사회적경제뉴스 · 주간 한국임업신문 4종. 네 피드 모두
RSS 2.0 이라 **농림축산식품부 RSS 크롤러
(:class:`~alert.crawlers.mafra.MafraCrawler`)의 파싱을 상속해서 쓴다** —
``_parse_rss_xml`` 을 다시 구현하지 않는다. 상속의 뜻은 "같은 RSS 2.0 파서를
쓴다" 하나뿐이고, 소스 정체성은 :attr:`MediaRssCrawler.SOURCE_NAME` 이 단독으로
정한다 (``__init__`` 이 ``BaseCrawler`` 를 직접 부른다 — ``mafra`` 라는 이름은
물려받지 않는다).

**종류 선언은 클래스가 한다 (라운드 2 HIGH).** :attr:`MediaRssCrawler.KIND`
= ``"media"`` 가 정본이다. ``config.yaml`` 의 ``kind:`` 는 **확인용**이고,
소스 블록이 통째로 없거나 오타가 나도 이 클래스가 media 인 사실은 변하지
않는다 — 설정 한 줄이 빠졌다고 2차 보도가 회사 알림으로 새면 안 된다
(:func:`alert.main.resolve_source_kind` 참조).

**저작권 (계약 §규칙).** 저장하는 것은 제목 · 링크 · pubDate · 요약뿐이고,
요약은 피드 ``description`` 을 :data:`SUMMARY_MAX_CHARS` 자로 자른 것이다.
본문 전문은 수집하지 않는다. ``raw_data`` 는 피드 item 을 통째로 담지 않고
**위 네 값만 골라 담는다** — 전문이 흘러들 자리를 구조로 없앤다. 그 사실을
``raw_data["license_note"] = "headline+link only"`` 로 남긴다. 월간호가 인용
형식(제목 + 링크 + 원문 1문장)을 고를 때 읽는 표식이다.

**회사 알림 무영향.** ``alert.main.select_for_storage`` 가 kind=media 를 회사
선택 경로에서 통째로 빼므로, 제목에 회사 must_match 어휘("사회적기업" 등)가
있어도 회사 알림 후보가 되지 않는다. 적재 여부는 협의회 프로파일이 단독으로
정한다 (매치 → ``council_only=1``, 미매치 → 탈락 원장).
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from html import unescape
from typing import List, Optional
from urllib.parse import urlparse

from .base import BaseCrawler
from .mafra import MafraCrawler
from ..models import SOURCE_KIND_MEDIA, RawAnnouncement

# 저장 요약의 상한 (계약 §규칙: "요약(피드 description 200자 이내)").
SUMMARY_MAX_CHARS = 200

# raw_data 에 남기는 저작권 표식 (계약 §규칙).
LICENSE_NOTE = "headline+link only"

# 2차 미디어 항목의 분류. 협의회 어휘(must_match)와 겹치지 않는 값이어야
# 한다 — 분류 한 줄로 전 항목이 매치되면 노이즈 필터가 무력해진다.
MEDIA_CATEGORY = "언론보도"

# 피드 본문 상한 (라운드 2 LOW). http_fetch.MAX_DETAIL_BYTES 와 같은 값·같은
# 뜻이다: 상한에서 **끊고 남은 본문을 drain 하지 않는다**. 잘린 XML 은
# 파서가 거부하므로 그 실행이 빈손이 될 뿐, 메모리가 터지지 않는다.
MAX_FEED_BYTES = 512 * 1024
_FEED_READ_CHUNK = 16384

_TAG_RE = re.compile(r"<[^>]+>")
# XML 선언의 encoding= (헤더에 charset 이 없을 때만 본다)
_XML_ENCODING_RE = re.compile(rb"""<\?xml[^>]*encoding\s*=\s*["']([A-Za-z0-9_.:-]+)""")


def article_url(link: str) -> str:
    """기사 URL 로 쓸 수 있는 값이면 그대로, 아니면 빈 문자열.

    http(s) 이고 호스트가 있어야 한다 (라운드 2 MEDIUM). ``urn:uuid:...`` 나
    ``tag:example.com,2026:1`` 같은 비-permalink guid 가 URL 자리에 앉으면
    발송본의 "원문 링크" 가 열리지 않는 문자열이 된다.
    """
    text = (link or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text


class MediaRssCrawler(MafraCrawler):
    """2차 미디어 RSS 공통 베이스. 하위 클래스는 상수 3개만 선언한다."""

    #: 소스 종류의 **정본**. 설정이 아니라 클래스가 선언한다 (라운드 2 HIGH).
    KIND: str = SOURCE_KIND_MEDIA

    #: 소스 이름 (``config.yaml`` 의 crawler.sources 키와 같아야 한다)
    SOURCE_NAME: str = ""
    #: 매체명 — ``author`` 자리에 그대로 들어간다
    PUBLISHER: str = ""
    #: 검증된 피드 URL (results/R-monthly-sources.md §1 의 URL 그대로)
    RSS_URL: str = ""

    # 미디어 피드에는 폴백 URL 을 두지 않는다. 추측한 URL 로 조용히 다른
    # 매체를 긁는 것보다 그 실행을 비우는 쪽이 낫다 (계약 §소스: 추측 금지).
    FALLBACK_RSS_URLS: List[str] = []

    def __init__(self):
        if not self.SOURCE_NAME:
            raise ValueError("MediaRssCrawler 하위 클래스는 SOURCE_NAME 을 선언해야 한다")
        BaseCrawler.__init__(self, source_name=self.SOURCE_NAME)

    def fetch(self) -> List[RawAnnouncement]:
        """피드 1개를 받아 항목 목록으로 바꾼다.

        :class:`MafraCrawler` 의 ``fetch`` 를 쓰지 않는 이유는 폴백 URL 목록과
        로그 문구("MAFRA RSS feed")가 이 소스의 사실이 아니기 때문이다. 파싱
        자체는 물려받은 ``_parse_rss_xml`` 그대로다.
        """
        rss_url = self.get_base_url() or self.RSS_URL
        if not rss_url:
            self.logger.error(f"{self.SOURCE_NAME}: RSS URL 이 없다")
            return []

        xml_text = self._fetch_rss(rss_url)
        if xml_text is None:
            self.logger.error(f"{self.SOURCE_NAME}: RSS 수신 실패 ({rss_url})")
            return []

        items = self._parse_rss_xml(xml_text)
        if not items:
            self.logger.warning(f"{self.SOURCE_NAME}: RSS 항목 0건 ({rss_url})")
            return []

        self.logger.info(f"{self.SOURCE_NAME}: RSS 항목 {len(items)}건")

        announcements: List[RawAnnouncement] = []
        for item_data in items:
            announcement = self._to_announcement(item_data)
            if announcement:
                announcements.append(announcement)
        return announcements

    # ------------------------------------------------------------------
    # 수신 — 크기 상한
    # ------------------------------------------------------------------

    def _fetch_rss(self, url: str) -> Optional[str]:
        """RSS XML 을 :data:`MAX_FEED_BYTES` 까지만 읽는다 (라운드 2 LOW).

        부모 구현은 ``response.text`` 로 본문을 통째로 들인다. 미디어 피드는
        매체가 길이를 정하므로 상한이 필요하다 — ``http_fetch._read_capped``
        와 같은 청크 패턴이다(여기는 requests 세션을 쓰므로 ``iter_content``).

        인코딩은 헤더 → XML 선언 → utf-8 순으로 고른다.
        ``response.apparent_encoding`` 은 본문을 통째로 읽으므로 쓰지 않는다.
        """
        response = self.get(url, stream=True)
        if response is None:
            return None

        try:
            chunks: List[bytes] = []
            size = 0
            capped = False
            for chunk in response.iter_content(_FEED_READ_CHUNK):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_FEED_BYTES:
                    capped = True
                    break  # 상한에서 멈춘다 — 남은 본문을 drain 하지 않는다
            raw = b"".join(chunks)[:MAX_FEED_BYTES]
            header_encoding = response.encoding
        finally:
            response.close()

        if capped:
            self.logger.warning(
                f"{self.SOURCE_NAME}: 피드가 {MAX_FEED_BYTES} 바이트 상한에서 잘렸다 "
                f"({url}) - 잘린 XML 은 파서가 거부한다"
            )

        encoding = header_encoding
        if not encoding:
            match = _XML_ENCODING_RE.search(raw[:200])
            encoding = match.group(1).decode("ascii", "replace") if match else "utf-8"
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # 파싱 — guid 정책
    # ------------------------------------------------------------------

    def _parse_rss_xml(self, xml_text: str) -> List[dict]:
        """부모 파서 + **guid 정책** (라운드 2 MEDIUM).

        부모는 ``<link>`` 가 비면 ``<guid>`` 를 조건 없이 URL 자리에 넣는다.
        RSS 2.0 에서 guid 는 ``isPermaLink="false"`` 일 때 URL 이 아니라 그냥
        식별 문자열이다. 그 값을 URL·source_id 로 쓰면 (a) 열리지 않는 링크가
        발송본에 들어가고 (b) 같은 guid 를 돌려쓰는 CMS 에서 서로 다른 기사가
        한 행으로 합쳐진다. 여기서는 그 자리를 **비운다** — URL 이 없는 항목은
        ``_to_announcement`` 가 건너뛴다.
        """
        items = super()._parse_rss_xml(xml_text)
        self._apply_guid_policy(xml_text, items)
        return items

    def _apply_guid_policy(self, xml_text: str, items: List[dict]) -> None:
        """부모가 guid 로 채운 ``link`` 중 permalink 가 아닌 것을 비운다.

        같은 XML 을 같은 순서(``channel.findall("item")``)로 다시 훑어 인덱스로
        맞춘다. 개수가 다르면(Atom 폴백 등) 손대지 않는다 — 맞출 수 없는
        상태에서 추측으로 고치지 않는다.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return
        channel = root.find("channel")
        if channel is None:
            channel = root
        elems = channel.findall("item")
        if len(elems) != len(items):
            return

        for elem, data in zip(elems, items):
            link_elem = elem.find("link")
            if link_elem is not None and (link_elem.text or "").strip():
                continue                       # 진짜 link 가 있다 - guid 무관
            guid_elem = elem.find("guid")
            if guid_elem is None or not (guid_elem.text or "").strip():
                continue
            if (guid_elem.get("isPermaLink") or "").strip().lower() == "false":
                data["link"] = ""

    # ------------------------------------------------------------------
    # 변환
    # ------------------------------------------------------------------

    def _short_summary(self, description: str) -> str:
        """피드 ``description`` 을 태그 없는 :data:`SUMMARY_MAX_CHARS` 자로.

        순서는 **엔티티 해제 → 태그 제거 → 공백 정리 → 절단**이다 (라운드 2
        LOW). 태그를 먼저 지우면 ``&lt;p&gt;`` 처럼 한 번 더 감싸인 마크업이
        해제 뒤에 ``<p>`` 로 되살아나 요약에 그대로 남는다.

        전문 재게재 금지가 이 한 곳에서 강제된다 — 길이 상한을 넘기는 경로가
        없어야 하므로 자르기는 언제나 **마지막** 단계다.
        """
        text = _TAG_RE.sub(" ", unescape(description or ""))
        text = " ".join(text.split())
        return text[:SUMMARY_MAX_CHARS]

    def _normalize_pub_date(self, pub_date: str) -> Optional[str]:
        """부모 정규화 + **달력 검증** (라운드 2 LOW).

        부모의 마지막 갈래는 ``(\\d{4})[.\\-/](\\d{1,2})[.\\-/](\\d{1,2})`` 를
        그대로 조립하므로 ``2026-99-99`` 같은 없는 날짜가 통과한다. 월간호는
        발행일로 묶고 정렬하므로, 틀린 날짜보다 **없는 날짜**가 낫다.
        """
        value = super()._normalize_pub_date(pub_date)
        if not value:
            return None
        try:
            date.fromisoformat(value)
        except ValueError:
            self.logger.warning(
                f"{self.SOURCE_NAME}: 달력에 없는 날짜라 비운다: {pub_date!r}"
            )
            return None
        return value

    def _generate_source_id(self, link: str, title: str) -> str:
        """기사 번호(``idxno``)를 우선 쓰고, 없으면 mafra 규칙으로 떨어진다.

        입력 ``link`` 는 :func:`article_url` 을 통과한 **URL 뿐**이다 —
        permalink 가 아닌 guid 는 여기까지 오지 못한다(그 항목은 URL 이 없어
        건너뛰어진다). 라이프인·이로운넷·한국임업신문은
        ``articleView.html?idxno=NNNN`` 이라 mafra 의 경로 숫자 규칙
        (``/(\\d{5,})``)에 걸리지 않으므로 번호를 먼저 본다.
        """
        match = re.search(r"[?&]idxno=(\d+)", link or "")
        if match:
            return match.group(1)
        return super()._generate_source_id(link, title)

    def _to_announcement(self, item_data: dict) -> Optional[RawAnnouncement]:
        """RSS item → :class:`RawAnnouncement` (제목·링크·pubDate·요약만)."""
        title = (item_data.get("title") or "").strip()
        if not title:
            self.logger.warning(f"{self.SOURCE_NAME}: 제목이 빈 항목을 건너뛴다")
            return None

        url = article_url(item_data.get("link"))
        if not url:
            # URL 없는 기사는 저작권 형식(제목+링크+1문장)을 만들 수 없다.
            self.logger.warning(
                f"{self.SOURCE_NAME}: 원문 URL 이 없어 건너뛴다: {title[:40]!r}"
            )
            return None

        pub_date = (item_data.get("pubDate") or "").strip()
        summary = self._short_summary(item_data.get("description", ""))

        # raw_data 는 item 전체가 아니라 **네 값 + 표식**만 담는다.
        raw_data = {
            "title": title,
            "link": url,
            "pubDate": pub_date,
            # 다른 9개 크롤러와 같은 키 이름. 탈락 원장이 이 값을 읽는다.
            "posted": self._normalize_pub_date(pub_date) or "",
            "summary": summary,
            "publisher": self.PUBLISHER,
            "license_note": LICENSE_NOTE,
        }

        return RawAnnouncement(
            source=self.SOURCE_NAME,
            source_id=self._generate_source_id(url, title),
            title=title,
            url=url,
            summary=summary,
            author=self.PUBLISHER,
            category=MEDIA_CATEGORY,
            raw_data=json.dumps(raw_data, ensure_ascii=False),
        )


class LifeinCrawler(MediaRssCrawler):
    """라이프인 — 전체기사 피드 (R-monthly-sources.md §1 #1)."""

    SOURCE_NAME = "lifein"
    PUBLISHER = "라이프인"
    RSS_URL = "https://www.lifein.news/rss/allArticle.xml"


class ErounCrawler(MediaRssCrawler):
    """이로운넷 — 사회연대경제 섹션 피드 (§1 #2)."""

    SOURCE_NAME = "eroun"
    PUBLISHER = "이로운넷"
    RSS_URL = "https://www.eroun.net/rss/S1N1.xml"


class SenewsCrawler(MediaRssCrawler):
    """사회적경제뉴스 (§1 #3). 노이즈가 큰 피드라 협의회 어휘 필터가 필수."""

    SOURCE_NAME = "senews"
    PUBLISHER = "사회적경제뉴스"
    RSS_URL = "https://www.senews.kr/rss/rss_news.php"


class KfnewsCrawler(MediaRssCrawler):
    """주간 한국임업신문 — 전체기사 피드 (§1 #4)."""

    SOURCE_NAME = "kfnews"
    PUBLISHER = "주간 한국임업신문"
    RSS_URL = "https://www.kfnews.co.kr/rss/allArticle.xml"
