"""2차 미디어 RSS 소스 (P1-R 계약) — 네트워크를 타지 않는다.

피드는 전부 **저장된 XML 문자열**로 대신한다. 라이브 검증(피드당 1회 GET)은
보고서의 파싱 표로만 남고 테스트에는 들어오지 않는다 — 테스트가 네트워크를
타면 매체 사이트가 느린 날 스위트가 빨개진다.

이 파일이 지키는 것 네 가지:

1. 파싱 — RSS 2.0 item → RawAnnouncement (mafra 파서 재사용)
2. 저작권 — 제목·링크·pubDate·요약(200자)만 저장, license_note 기록
3. 회사 알림 무영향 — media 는 회사 선택 경로에 **한 건도** 들어가지 않고
   ``get_unnotified`` 에 0건이다
4. 주간호 무변화 — media 행은 주간 후보 쿼리(``compose_digest_data``)에
   잡히지 않는다
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from alert.config import get_config, is_media_source
from alert.council import score_item
from alert.crawlers.media_rss import (
    LICENSE_NOTE,
    MEDIA_CATEGORY,
    SUMMARY_MAX_CHARS,
    ErounCrawler,
    KfnewsCrawler,
    LifeinCrawler,
    SenewsCrawler,
)
from alert.db import Database
from alert.digest.composer import compose_digest_data
from alert.main import _import_crawlers, apply_council_profile, select_for_storage
from alert.models import SOURCE_KIND_MEDIA, AnalyzedAnnouncement, RawAnnouncement

MEDIA_SOURCES = ("lifein", "eroun", "senews", "kfnews")

# 실측 피드의 모양만 줄인 것(라이프인·이로운넷·한국임업신문 공통 형태).
FEED_XML = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>라이프인 - 전체기사</title>
  <link>https://www.lifein.news</link>
  <item>
    <title>행안부, '사회연대경제기본법' 시행 앞두고 워크숍 개최</title>
    <link>https://www.lifein.news/news/articleView.html?idxno=20429</link>
    <description>&lt;p&gt;행정안전부가 &lt;b&gt;사회연대경제기본법&lt;/b&gt;
      시행을 앞두고    워크숍을 열었다.&lt;/p&gt;</description>
    <pubDate>2026-09-11 22:40:00</pubDate>
    <author>기자</author>
  </item>
  <item>
    <title>고양시, 가을맞이 거리 청소 실시</title>
    <link>https://www.lifein.news/news/articleView.html?idxno=20428</link>
    <description>지역 소식</description>
    <pubDate>2026-09-11 22:00:21</pubDate>
  </item>
</channel>
</rss>
"""

# 사회적경제뉴스는 링크가 경로 숫자다 (https://www.senews.kr/41052).
SENEWS_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel>
  <title>사회적경제뉴스</title>
  <item>
    <title><![CDATA[산천초목팜, 산림 사회적경제 모델 구축]]></title>
    <link>https://www.senews.kr/41052</link>
    <description><![CDATA[도심 국유림 임산물 재배]]></description>
    <pubDate>2026-09-13 17:14:00</pubDate>
  </item>
</channel></rss>
"""


def _crawl(crawler, xml_text):
    """네트워크 대신 저장된 XML 로 ``fetch()`` 를 돌린다."""
    crawler._fetch_rss = lambda url: xml_text
    return crawler.fetch()


# ---------------------------------------------------------------------------
# 1. 파싱
# ---------------------------------------------------------------------------


class TestMediaRssParsing:
    def test_fetch_parses_every_item(self):
        items = _crawl(LifeinCrawler(), FEED_XML)
        assert len(items) == 2
        assert items[0].source == "lifein"
        assert items[0].author == "라이프인"
        assert items[0].category == MEDIA_CATEGORY

    def test_source_id_uses_article_number(self):
        items = _crawl(LifeinCrawler(), FEED_XML)
        assert [a.source_id for a in items] == ["20429", "20428"]

    def test_source_id_falls_back_to_path_number(self):
        """senews 는 idxno 가 없고 경로가 번호다 - mafra 규칙이 받는다."""
        items = _crawl(SenewsCrawler(), SENEWS_XML)
        assert items[0].source_id == "41052"
        assert items[0].url == "https://www.senews.kr/41052"

    def test_cdata_title_is_read(self):
        items = _crawl(SenewsCrawler(), SENEWS_XML)
        assert items[0].title == "산천초목팜, 산림 사회적경제 모델 구축"

    def test_pubdate_is_kept_in_raw_data(self):
        items = _crawl(LifeinCrawler(), FEED_XML)
        raw = json.loads(items[0].raw_data)
        assert raw["pubDate"] == "2026-09-11 22:40:00"
        assert raw["posted"] == "2026-09-11"

    def test_empty_title_item_is_skipped(self):
        xml = FEED_XML.replace(
            "<title>고양시, 가을맞이 거리 청소 실시</title>", "<title></title>"
        )
        assert len(_crawl(LifeinCrawler(), xml)) == 1

    def test_unfetchable_feed_yields_nothing(self):
        crawler = LifeinCrawler()
        crawler._fetch_rss = lambda url: None
        assert crawler.fetch() == []

    def test_each_crawler_declares_the_verified_feed_url(self):
        """URL 정본은 config 다 - 클래스 상수와 어긋나면 안 된다."""
        sources = get_config().crawler.sources
        for crawler_cls in (LifeinCrawler, ErounCrawler, SenewsCrawler, KfnewsCrawler):
            assert crawler_cls.RSS_URL == sources[crawler_cls.SOURCE_NAME].base_url

    def test_no_fallback_urls(self):
        """추측한 URL 로 다른 매체를 긁지 않는다."""
        for crawler_cls in (LifeinCrawler, ErounCrawler, SenewsCrawler, KfnewsCrawler):
            assert crawler_cls.FALLBACK_RSS_URLS == []


# ---------------------------------------------------------------------------
# 2. 저작권
# ---------------------------------------------------------------------------


class TestMediaCopyright:
    def test_summary_strips_tags_and_collapses_space(self):
        items = _crawl(LifeinCrawler(), FEED_XML)
        assert items[0].summary == (
            "행정안전부가 사회연대경제기본법 시행을 앞두고 워크숍을 열었다."
        )

    def test_summary_is_capped(self):
        xml = FEED_XML.replace("지역 소식", "가" * 500)
        items = _crawl(LifeinCrawler(), xml)
        assert len(items[1].summary) == SUMMARY_MAX_CHARS

    def test_license_note_is_recorded(self):
        items = _crawl(LifeinCrawler(), FEED_XML)
        assert json.loads(items[0].raw_data)["license_note"] == "headline+link only"
        assert LICENSE_NOTE == "headline+link only"

    def test_raw_data_holds_only_whitelisted_keys(self):
        """피드 item 을 통째로 담지 않는다 - 본문이 흘러들 자리를 없앤다."""
        items = _crawl(LifeinCrawler(), FEED_XML)
        assert set(json.loads(items[0].raw_data)) == {
            "title", "link", "pubDate", "posted", "summary",
            "publisher", "license_note",
        }

    def test_raw_data_summary_is_the_capped_one(self):
        xml = FEED_XML.replace("지역 소식", "나" * 500)
        raw = json.loads(_crawl(LifeinCrawler(), xml)[1].raw_data)
        assert len(raw["summary"]) == SUMMARY_MAX_CHARS


# ---------------------------------------------------------------------------
# 3. 설정 정합
# ---------------------------------------------------------------------------


class TestMediaConfig:
    def test_all_four_sources_are_media_kind(self):
        sources = get_config().crawler.sources
        for name in MEDIA_SOURCES:
            assert sources[name].kind == SOURCE_KIND_MEDIA
            assert is_media_source(sources[name]) is True

    def test_media_sources_do_not_bypass_threshold(self):
        sources = get_config().crawler.sources
        for name in MEDIA_SOURCES:
            assert sources[name].bypass_threshold is False
            assert sources[name].fetch_detail is False

    def test_media_sources_are_in_council_profile(self):
        profile = get_config().council_profile
        for name in MEDIA_SOURCES:
            assert name in profile.sources

    def test_mafra_is_not_registered_twice(self):
        """mafra RSS 는 이미 있다 - 중복 등록 금지 (계약 §소스)."""
        sources = get_config().crawler.sources
        assert sources["mafra"].kind != SOURCE_KIND_MEDIA
        feed_urls = [sources[n].base_url for n in MEDIA_SOURCES]
        assert sources["mafra"].base_url not in feed_urls

    def test_crawlers_are_registered_in_the_pipeline(self):
        registered = _import_crawlers()
        for name in MEDIA_SOURCES:
            assert name in registered

    def test_non_media_sources_keep_the_default_kind(self):
        sources = get_config().crawler.sources
        for name in ("mafra", "kofpi", "forest_service", "bizinfo"):
            assert is_media_source(sources[name]) is False


# ---------------------------------------------------------------------------
# 4. 회사 알림 무영향
# ---------------------------------------------------------------------------


class _ExplodingAnalyzer:
    """회사 분석기가 **불리면** 실패한다 - 경로 분리를 증명한다."""

    def analyze(self, raw):                       # pragma: no cover - 불리면 실패
        raise AssertionError("media 는 회사 분석 경로에 들어가면 안 된다")

    def analyze_batch(self, raws):                # pragma: no cover - 불리면 실패
        raise AssertionError("media 는 회사 분석 경로에 들어가면 안 된다")


def _media_raw(source="lifein", source_id="1", title="사회적기업 지원 기사"):
    return RawAnnouncement(
        source=source, source_id=source_id, title=title,
        url=f"https://example.com/{source_id}", summary="요약",
        category=MEDIA_CATEGORY,
    )


class TestCompanyPathUntouched:
    def test_select_for_storage_returns_nothing_for_media(self):
        cfg = get_config().crawler.sources["lifein"]
        selected, bypassed = select_for_storage(
            _ExplodingAnalyzer(), [_media_raw()], cfg
        )
        assert selected == []
        assert bypassed is False

    def test_company_keyword_would_have_matched(self):
        """제외가 어휘 운이 아니라 **경로 분리**임을 보인다.

        같은 제목이 공고 소스였다면 회사 must_match("사회적기업")에 걸린다.
        """
        must = get_config().keywords.must_match
        assert any(term in "사회적기업 지원 기사" for term in must)

    def test_media_items_land_council_only(self):
        """회사 선택이 비었으므로 협의회 매치 항목은 council_only=1 이다."""
        profile = get_config().council_profile
        extras, drops, _unmatched = apply_council_profile(
            profile, "lifein",
            [_media_raw(title="산림 사회적경제 기업 이야기")],
            [],                                   # select_for_storage 가 준 빈 목록
            lambda raw: AnalyzedAnnouncement(**vars(raw)),
        )
        assert drops == []
        assert len(extras) == 1
        assert extras[0].council_only == 1
        assert extras[0].council_match == 1

    def test_council_vocab_miss_goes_to_the_drop_ledger(self):
        profile = get_config().council_profile
        extras, drops, _unmatched = apply_council_profile(
            profile, "senews",
            [_media_raw(source="senews", title="고양시, 가을맞이 거리 청소 실시")],
            [],
            lambda raw: AnalyzedAnnouncement(**vars(raw)),
        )
        assert extras == []
        assert len(drops) == 1

    def test_council_scoring_is_unchanged_for_media(self):
        """media 라고 특별 취급하지 않는다 - 같은 score_item 을 탄다."""
        profile = get_config().council_profile
        verdict = score_item(profile, "lifein", "산림 사회적경제 기업 이야기")
        assert verdict.match == 1

    def test_get_unnotified_never_returns_media(self, tmp_path):
        """회귀: 가드가 두 겹이다. council_only=0 인 media 행을 억지로 넣어도
        ``kind`` 필터가 회사 알림 후보에서 뺀다."""
        db = Database(db_path=tmp_path / "t.db")
        media = AnalyzedAnnouncement(
            source="lifein", source_id="m1", title="사회적기업 지원 기사",
            url="https://example.com/m1", relevance_score=0.9,
            council_only=0, kind=SOURCE_KIND_MEDIA,
        )
        gonggo = AnalyzedAnnouncement(
            source="kofpi", source_id="g1", title="산림 사회적기업 공모",
            url="https://example.com/g1", relevance_score=0.9,
        )
        assert db.insert_announcement(media)
        assert db.insert_announcement(gonggo)

        sources = {a.source for a in db.get_unnotified()}
        assert sources == {"kofpi"}
        db.close()

    def test_kind_is_stored_and_read_back(self, tmp_path):
        db = Database(db_path=tmp_path / "t.db")
        db.insert_announcement(AnalyzedAnnouncement(
            source="lifein", source_id="m1", title="산림 기사",
            url="https://example.com/m1", council_only=1,
            kind=SOURCE_KIND_MEDIA,
        ))
        row = db.conn.execute(
            "SELECT kind FROM announcements WHERE source_id = 'm1'"
        ).fetchone()
        assert row["kind"] == SOURCE_KIND_MEDIA
        db.close()

    def test_existing_rows_default_to_gonggo(self, tmp_path):
        db = Database(db_path=tmp_path / "t.db")
        db.insert_announcement(AnalyzedAnnouncement(
            source="kofpi", source_id="g1", title="산림 공모",
            url="https://example.com/g1",
        ))
        row = db.conn.execute(
            "SELECT kind FROM announcements WHERE source_id = 'g1'"
        ).fetchone()
        assert row["kind"] == "gonggo"
        db.close()

    def test_period_query_skips_media(self, tmp_path):
        db = Database(db_path=tmp_path / "t.db")
        db.insert_announcement(AnalyzedAnnouncement(
            source="lifein", source_id="m1", title="산림 기사",
            url="https://example.com/m1", council_only=0,
            kind=SOURCE_KIND_MEDIA,
        ))
        db.insert_announcement(AnalyzedAnnouncement(
            source="kofpi", source_id="g1", title="산림 공모",
            url="https://example.com/g1",
        ))
        found = db.get_announcements_by_period("2000-01-01", "2100-01-01")
        assert [a.source for a in found] == ["kofpi"]
        db.close()


# ---------------------------------------------------------------------------
# 5. 주간호 무변화
# ---------------------------------------------------------------------------


class TestWeeklyDigestUnchanged:
    """주간 후보 쿼리(composer)가 media 행을 집지 않는다.

    이 레인은 ``composer.py`` 를 만지지 않는다(P1-M 레인이 소유). 주간호가
    안 변하는 근거는 **파이프라인이 media 행에 세우는 council_only=1** 이고,
    composer 의 기존 가드(``COALESCE(council_only,0)=0``)가 그 행을 뺀다.
    아래 테스트가 그 사실을 실제 쿼리로 확인한다.
    """

    def _seed(self, tmp_path):
        db = Database(db_path=tmp_path / "week.db")
        media = AnalyzedAnnouncement(
            source="lifein", source_id="m1",
            title="산림 사회적기업 기사", url="https://example.com/m1",
            relevance_score=0.9, council_only=1, council_match=1,
            kind=SOURCE_KIND_MEDIA,
        )
        gonggo = AnalyzedAnnouncement(
            source="kofpi", source_id="g1",
            title="산림 사회적기업 공모", url="https://example.com/g1",
            relevance_score=0.9,
        )
        media_id = db.insert_announcement(media)
        gonggo_id = db.insert_announcement(gonggo)
        path = str(db.db_path)
        db.close()
        return path, media_id, gonggo_id

    def test_media_row_is_not_a_weekly_candidate(self, tmp_path):
        path, media_id, gonggo_id = self._seed(tmp_path)
        data = compose_digest_data(path)
        assert gonggo_id in data["candidate_ids"]
        assert media_id not in data["candidate_ids"]

    def test_media_row_is_absent_from_every_section(self, tmp_path):
        path, _media_id, _gonggo_id = self._seed(tmp_path)
        data = compose_digest_data(path)
        rendered = json.dumps(data, ensure_ascii=False, default=str)
        assert "example.com/m1" not in rendered
