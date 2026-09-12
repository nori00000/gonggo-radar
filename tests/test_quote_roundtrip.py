"""인용 재수집이 DB까지 도달하는지 (최종 게이트 #6) + 지역 키 (#1).

요청 상한(20건)보다 목록이 길면 인용은 여러 실행에 걸쳐 도착한다. 중복
필터가 그걸 버리면 뒤쪽 항목은 영구히 인용을 못 받는다. 여기서는
``get_quoted_source_ids`` → ``set_quoted_source_ids`` → ``merge_quote_fields``
고리가 실제 DB에서 닫히는지 확인한다.
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.base import BaseCrawler
from alert.crawlers.dedupe_keys import group_key, keys_compatible, subject_signature
from alert.crawlers.detail_quotes import MAX_DETAIL_REQUESTS
from alert.db import Database
from alert.models import AnalyzedAnnouncement, RawAnnouncement

TOTAL_ITEMS = 25
DETAIL_HTML = (
    '<div class="board_view"><table><tr><th>접수기간</th>'
    "<td>2026.09.01 ~ 2026.09.30</td></tr></table></div>"
)


class _Stub(BaseCrawler):
    def fetch(self):  # pragma: no cover
        return []


def make_stub() -> _Stub:
    from alert.config import SourceConfig

    config = MagicMock()
    config.crawler.timeout = 30
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "AgriAlert/1.0"
    config.crawler.sources = {
        "stub": SourceConfig(
            enabled=True, base_url="https://example.com", fetch_detail=True
        )
    }
    with patch("alert.crawlers.base.get_config", return_value=config):
        return _Stub(source_name="stub")


def fake_response(html: str):
    body = html.encode("utf-8")
    response = MagicMock()
    response.encoding = "utf-8"
    response.headers = {"Content-Length": str(len(body))}
    response.iter_content.return_value = [body]
    response.raise_for_status.return_value = None
    return response


def raw_items():
    return [
        RawAnnouncement(
            source="stub",
            source_id=str(index),
            title=f"공고 {index}",
            url=f"https://example.com/view/{index}",
            raw_data=json.dumps({"title": f"공고 {index}"}, ensure_ascii=False),
        )
        for index in range(TOTAL_ITEMS)
    ]


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "announcements.db")
    now = datetime.now().isoformat()
    for item in raw_items():
        database.insert_announcement(
            AnalyzedAnnouncement(
                source=item.source,
                source_id=item.source_id,
                title=item.title,
                url=item.url,
                raw_data=item.raw_data,
                fetched_at=now,
            )
        )
    yield database
    database.close() if hasattr(database, "close") else None


def quoted_count(database: Database) -> int:
    return len(database.get_quoted_source_ids("stub"))


class TestQuoteReCollectionReachesTheDatabase:
    """#6: 두 번째 실행에서 얻은 인용이 저장되어야 한다."""

    def run_once(self, database, crawler=None):
        """한 번의 수집 - DB에서 인용 보유 목록을 받아 상세를 훑고 다시 저장."""
        crawler = crawler or make_stub()
        crawler.set_quoted_source_ids(database.get_quoted_source_ids("stub"))
        items = raw_items()
        with patch.object(
            crawler.session, "get", return_value=fake_response(DETAIL_HTML)
        ) as mock_get:
            with patch("alert.crawlers.base.DETAIL_DELAY_SEC", 0.0):
                crawler.enrich_with_quotes(items)
        merged = 0
        for item in items:
            if database.is_duplicate(item.source, item.source_id):
                if database.merge_quote_fields(item):
                    merged += 1
        return mock_get.call_count, merged

    def test_three_runs_cover_every_item(self, db):
        """25건 목록: 요청 20/5/0, DB 인용 20/25/25."""
        assert quoted_count(db) == 0

        requests_1, merged_1 = self.run_once(db)
        assert requests_1 == MAX_DETAIL_REQUESTS
        assert merged_1 == MAX_DETAIL_REQUESTS
        assert quoted_count(db) == MAX_DETAIL_REQUESTS

        requests_2, merged_2 = self.run_once(db)
        assert requests_2 == TOTAL_ITEMS - MAX_DETAIL_REQUESTS
        assert merged_2 == TOTAL_ITEMS - MAX_DETAIL_REQUESTS
        assert quoted_count(db) == TOTAL_ITEMS

        requests_3, merged_3 = self.run_once(db)
        assert requests_3 == 0            # 더 요청할 것이 없다
        assert merged_3 == 0
        assert quoted_count(db) == TOTAL_ITEMS

    def test_merged_quote_lands_with_its_period(self, db):
        """인용과 함께 파생된 기간이 저장된다."""
        self.run_once(db)
        row = db.get_quoted_source_ids("stub")
        assert "0" in row

        stored = db._conn.execute(
            "SELECT raw_data, period_start, period_end FROM announcements"
            " WHERE source = 'stub' AND source_id = '0'"
        ).fetchone()
        payload = json.loads(stored["raw_data"])
        assert payload["quote_deadline"] == "접수기간 2026.09.01 ~ 2026.09.30"
        assert payload["title"] == "공고 0"          # 목록 정보 보존
        assert stored["period_start"] == "2026-09-01"
        assert stored["period_end"] == "2026-09-30"

    def test_merge_without_quotes_is_a_no_op(self, db):
        """인용이 없는 결과는 저장을 건드리지 않는다."""
        plain = raw_items()[0]
        assert db.merge_quote_fields(plain) is False

    def test_merge_replaces_stale_flags(self, db):
        """새 인용이 예전 조기마감·상시 플래그를 남기지 않는다."""
        item = raw_items()[0]
        item.raw_data = json.dumps(
            {"quote_deadline": "접수기간 상시", "always_open": True,
             "early_close": True},
            ensure_ascii=False,
        )
        assert db.merge_quote_fields(item) is True

        item.raw_data = json.dumps(
            {"quote_deadline": "접수기간 2026.10.01 ~ 2026.10.31",
             "quote_period_start": "2026-10-01", "quote_period_end": "2026-10-31"},
            ensure_ascii=False,
        )
        assert db.merge_quote_fields(item) is True

        stored = db._conn.execute(
            "SELECT raw_data, period_end FROM announcements"
            " WHERE source = 'stub' AND source_id = '0'"
        ).fetchone()
        payload = json.loads(stored["raw_data"])
        assert "always_open" not in payload
        assert "early_close" not in payload
        assert stored["period_end"] == "2026-10-31"


class TestFinalGateRegionKey:
    """#1: 두 필드의 지역 정보를 함께 봐야 한다."""

    def test_facility_names_split(self):
        """지역 토큰 목록에 없는 시설명도 구분한다 (수원센터/성남센터)."""
        first = group_key("상주기업 모집", ["수원센터"], "2026-09-30")
        second = group_key("상주기업 모집", ["성남센터"], "2026-09-30")
        assert first != second

    def test_info_difference_splits_even_when_sub_matches(self):
        """``sub`` 가 같고 ``info`` 지역만 달라도 별개 공고다."""
        first = group_key("모집 공고", ["서울 본부", "서울"], "2026-09-30")
        second = group_key("모집 공고", ["서울 본부", "부산"], "2026-09-30")
        assert first != second

    def test_identical_metadata_merges(self):
        """메타데이터가 완전히 같으면 같은 공고다 (회차 중복)."""
        first = group_key("사회보험료 지원사업", ["사회보험료 지원 사업", "경기도"], "2026-12-31")
        second = group_key("사회보험료 지원사업", ["경기도", "사회보험료 지원 사업"], "2026-12-31")
        assert first == second          # 순서는 무관하다

    def test_subject_signature_is_order_independent(self):
        assert subject_signature(["서울", "본부"]) == subject_signature(["본부", "서울"])
        assert subject_signature(["", "  "]) == ()

    def test_missing_metadata_is_compatible_with_anything(self):
        """구 파서가 메타데이터를 안 남긴 행은 대표와 대조할 수 있다 (#7)."""
        rich = group_key("공고", ["경기도"], "2026-12-31")
        bare = group_key("공고", [], "2026-12-31")
        assert rich != bare
        assert keys_compatible(rich, bare) is True

    def test_real_deadlines_never_compatible_when_different(self):
        """실제 종료일이 다르면 같은 URL이라도 같은 공고가 아니다."""
        first = group_key("공고", [], "2026-09-30")
        second = group_key("공고", [], "2026-10-31")
        assert keys_compatible(first, second, ignore_deadline=True) is False

    def test_unknown_deadline_is_compatible_across_months(self):
        """마감을 모르는 행(month: 대체값)은 적재월이 달라도 합칠 수 있다."""
        first = group_key("공고", [], None, "2026-08")
        second = group_key("공고", [], None, "2026-09")
        assert first != second
        assert keys_compatible(first, second, ignore_deadline=True) is True
