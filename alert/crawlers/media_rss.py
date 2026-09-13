"""2차 미디어 RSS 크롤러 — 협의회 월간호용 (P1-R 계약).

라이프인 · 이로운넷 · 사회적경제뉴스 · 주간 한국임업신문 4종. 네 피드 모두
RSS 2.0 이라 **농림축산식품부 RSS 크롤러
(:class:`~alert.crawlers.mafra.MafraCrawler`)의 파싱을 상속해서 쓴다** —
``_fetch_rss`` · ``_parse_rss_xml`` · ``_normalize_pub_date`` 를 다시 구현하지
않는다. 상속의 뜻은 "같은 RSS 2.0 파서를 쓴다" 하나뿐이고, 소스 정체성은
:attr:`MediaRssCrawler.SOURCE_NAME` 이 단독으로 정한다 (``__init__`` 이
``BaseCrawler`` 를 직접 부른다 — ``mafra`` 라는 이름은 물려받지 않는다).

**저작권 (계약 §규칙).** 저장하는 것은 제목 · 링크 · pubDate · 요약뿐이고,
요약은 피드 ``description`` 을 :data:`SUMMARY_MAX_CHARS` 자로 자른 것이다.
본문 전문은 수집하지 않는다. ``raw_data`` 는 피드 item 을 통째로 담지 않고
**위 네 값만 골라 담는다** — 전문이 흘러들 자리를 구조로 없앤다. 그 사실을
``raw_data["license_note"] = "headline+link only"`` 로 남긴다. 월간호가 인용
형식(제목 + 링크 + 원문 1문장)을 고를 때 읽는 표식이다.

**회사 알림 무영향.** 이 소스들은 ``config.yaml`` 에서 ``kind: media`` 다.
``alert.main.select_for_storage`` 가 kind=media 를 회사 선택 경로에서 통째로
빼므로, 제목에 회사 must_match 어휘("사회적기업" 등)가 있어도 회사 알림
후보가 되지 않는다. 적재 여부는 협의회 프로파일이 단독으로 정한다
(매치 → ``council_only=1``, 미매치 → 탈락 원장).
"""

import json
import re
from html import unescape
from typing import List, Optional

from .base import BaseCrawler
from .mafra import MafraCrawler
from ..models import RawAnnouncement

# 저장 요약의 상한 (계약 §규칙: "요약(피드 description 200자 이내)").
SUMMARY_MAX_CHARS = 200

# raw_data 에 남기는 저작권 표식 (계약 §규칙).
LICENSE_NOTE = "headline+link only"

# 2차 미디어 항목의 분류. 협의회 어휘(must_match)와 겹치지 않는 값이어야
# 한다 — 분류 한 줄로 전 항목이 매치되면 노이즈 필터가 무력해진다.
MEDIA_CATEGORY = "언론보도"

_TAG_RE = re.compile(r"<[^>]+>")


class MediaRssCrawler(MafraCrawler):
    """2차 미디어 RSS 공통 베이스. 하위 클래스는 3개 상수만 선언한다."""

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
        자체는 물려받은 ``_fetch_rss`` / ``_parse_rss_xml`` 그대로다.
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
    # 변환
    # ------------------------------------------------------------------

    def _short_summary(self, description: str) -> str:
        """피드 ``description`` 을 태그 없는 :data:`SUMMARY_MAX_CHARS` 자로.

        전문 재게재 금지가 이 한 곳에서 강제된다 — 길이 상한을 넘기는 경로가
        없어야 하므로 자르기는 변환의 **마지막** 단계다.
        """
        text = unescape(_TAG_RE.sub(" ", description or ""))
        text = " ".join(text.split())
        return text[:SUMMARY_MAX_CHARS]

    def _generate_source_id(self, link: str, title: str) -> str:
        """기사 번호(``idxno``)를 우선 쓰고, 없으면 mafra 규칙으로 떨어진다.

        라이프인·이로운넷·한국임업신문은 ``articleView.html?idxno=NNNN`` 이라
        mafra 의 경로 숫자 규칙(``/(\\d{5,})``)에 걸리지 않는다. 번호를 쓰면
        같은 기사가 URL 파라미터 순서 차이로 갈리지 않는다.
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

        link = (item_data.get("link") or "").strip()
        pub_date = (item_data.get("pubDate") or "").strip()
        summary = self._short_summary(item_data.get("description", ""))

        # raw_data 는 item 전체가 아니라 **네 값 + 표식**만 담는다.
        raw_data = {
            "title": title,
            "link": link,
            "pubDate": pub_date,
            # 다른 9개 크롤러와 같은 키 이름. 탈락 원장이 이 값을 읽는다.
            "posted": self._normalize_pub_date(pub_date) or "",
            "summary": summary,
            "publisher": self.PUBLISHER,
            "license_note": LICENSE_NOTE,
        }

        return RawAnnouncement(
            source=self.SOURCE_NAME,
            source_id=self._generate_source_id(link, title),
            title=title,
            url=link,
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
