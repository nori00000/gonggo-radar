"""scripts/source_health.py 테스트.

프로브는 **전부 스텁**이다 — 이 파일은 네트워크를 타지 않는다
(tests/test_policy_crawlers.py 의 관행과 같다).
"""
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.source_health import (  # noqa: E402
    PROBE_TIMEOUT_SEC,
    Probe,
    SourceHealth,
    VERDICT_EMPTY,
    VERDICT_OK,
    VERDICT_PARSE_FAIL,
    VERDICT_SKIPPED,
    VERDICT_STALE,
    VERDICT_UNREACHABLE,
    PROBE_CAVEATS,
    build_report,
    classify,
    collect_db_stats,
    dry_parse,
    probe_source,
    probe_url_for,
    render_markdown,
    with_caveat,
)

TODAY = date(2026, 9, 13)


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

def _make_db(tmp_path, runs=(), announcements=()):
    db_path = tmp_path / "health.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE run_history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " started_at TEXT NOT NULL, finished_at TEXT, source TEXT NOT NULL,"
        " total_fetched INTEGER DEFAULT 0, new_count INTEGER DEFAULT 0,"
        " relevant_count INTEGER DEFAULT 0, notified_count INTEGER DEFAULT 0,"
        " status TEXT DEFAULT 'running', error_message TEXT DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE announcements (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " source TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL,"
        " period_start TEXT, created_at TEXT NOT NULL)"
    )
    for source, started_at, fetched, new_count, status in runs:
        conn.execute(
            "INSERT INTO run_history (started_at, source, total_fetched,"
            " new_count, status) VALUES (?, ?, ?, ?, ?)",
            (started_at, source, fetched, new_count, status),
        )
    for index, (source, period_start, created_at) in enumerate(announcements):
        conn.execute(
            "INSERT INTO announcements (source, source_id, title, period_start,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (source, f"sid_{index}", f"공고 {index}", period_start, created_at),
        )
    conn.commit()
    conn.close()
    return db_path


class _FakeSession:
    """crawler.session 대역 — GET 1회만 받는다."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


class _FakeResponse:
    def __init__(self, status_code=200, text="", encoding="utf-8"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.encoding = encoding
        self.apparent_encoding = encoding


class _FakeCrawler:
    """목록 파서 1개를 가진 최소 크롤러."""

    BASE_URL = "https://example.com"
    BOARD_PATHS = ["/board/list.do?id=1"]

    def __init__(self, parsed=3, response=None, exc=None):
        self._parsed = parsed
        self.session = _FakeSession(response=response, exc=exc)

    def get_base_url(self):
        return self.BASE_URL

    def _parse_table_board(self, soup):
        return [{"title": f"항목 {i}"} for i in range(self._parsed)]


# ---------------------------------------------------------------------------
# 프로브 URL
# ---------------------------------------------------------------------------

class TestProbeUrl:
    def test_board_paths_first_entry_wins(self):
        assert probe_url_for(_FakeCrawler()) == (
            "https://example.com/board/list.do?id=1"
        )

    def test_falls_back_to_base_url_without_paths(self):
        class Bare(_FakeCrawler):
            BOARD_PATHS: list = []

        assert probe_url_for(Bare()) == "https://example.com"

    def test_list_path_attribute_is_used(self):
        class WithListPath(_FakeCrawler):
            BOARD_PATHS: list = []
            LIST_PATH = "/home/boardList.do?brd=2"

        assert probe_url_for(WithListPath()) == (
            "https://example.com/home/boardList.do?brd=2"
        )

    def test_absolute_path_is_not_prefixed(self):
        class Absolute(_FakeCrawler):
            BOARD_PATHS = ["https://rss.example.org/feed.xml"]

        assert probe_url_for(Absolute()) == "https://rss.example.org/feed.xml"

    def test_real_crawlers_resolve_to_a_url(self):
        """실제 크롤러 32종 전부에서 프로브 URL이 나온다."""
        from alert.main import _import_crawlers

        crawlers = _import_crawlers()
        assert len(crawlers) == 32
        for name, klass in crawlers.items():
            url = probe_url_for(klass())
            assert url.startswith("http"), name


# ---------------------------------------------------------------------------
# 건식 파싱 (네트워크 없음)
# ---------------------------------------------------------------------------

class TestDryParse:
    def test_counts_items_from_first_matching_parser(self):
        count, parser = dry_parse(_FakeCrawler(parsed=4), "<html></html>")
        assert (count, parser) == (4, "_parse_table_board")

    def test_zero_when_parser_finds_nothing(self):
        count, parser = dry_parse(_FakeCrawler(parsed=0), "<html></html>")
        assert (count, parser) == (0, "")

    def test_none_when_crawler_has_no_list_parser(self):
        class NoParser:
            pass

        assert dry_parse(NoParser(), "{}") == (None, "")

    def test_parser_exception_is_swallowed(self):
        class Boom(_FakeCrawler):
            def _parse_table_board(self, soup):
                raise ValueError("구조가 바뀌었다")

        assert dry_parse(Boom(), "<html></html>") == (0, "")


# ---------------------------------------------------------------------------
# 프로브 (세션 스텁)
# ---------------------------------------------------------------------------

class TestProbeSource:
    def test_single_get_with_timeout(self):
        crawler = _FakeCrawler(response=_FakeResponse(text="<html></html>"))
        probe = probe_source(crawler, timeout=PROBE_TIMEOUT_SEC)

        assert len(crawler.session.calls) == 1
        url, kwargs = crawler.session.calls[0]
        assert url == "https://example.com/board/list.do?id=1"
        assert kwargs["timeout"] == PROBE_TIMEOUT_SEC
        assert probe.status == 200
        assert probe.parsed == 3

    def test_exception_becomes_error_not_raise(self):
        crawler = _FakeCrawler(exc=OSError("연결 리셋"))
        probe = probe_source(crawler)

        assert probe.status is None
        assert "연결 리셋" in probe.error

    def test_http_error_skips_parsing(self):
        crawler = _FakeCrawler(response=_FakeResponse(status_code=400, text="bad"))
        probe = probe_source(crawler)

        assert probe.status == 400
        assert probe.parsed is None
        assert probe.size == len(b"bad")


# ---------------------------------------------------------------------------
# 판정 5분류
# ---------------------------------------------------------------------------

def _health(**probe_kwargs):
    db_fields = {
        key: probe_kwargs.pop(key)
        for key in ("db_count", "latest_posted")
        if key in probe_kwargs
    }
    probe_kwargs.setdefault("url", "https://example.com/list")
    return SourceHealth(
        source="s",
        probe=Probe(**probe_kwargs),
        db_count=db_fields.get("db_count", 5),
        latest_posted=db_fields.get("latest_posted", "2026-09-12"),
    )


class TestClassify:
    def test_request_exception_is_unreachable(self):
        verdict, _ = classify(_health(error="SSLError"), today=TODAY)
        assert verdict == VERDICT_UNREACHABLE

    def test_http_400_is_unreachable(self):
        verdict, note = classify(_health(status=400, size=10), today=TODAY)
        assert verdict == VERDICT_UNREACHABLE
        assert "400" in note

    def test_200_with_zero_parsed_is_parse_failure(self):
        verdict, _ = classify(_health(status=200, size=99, parsed=0), today=TODAY)
        assert verdict == VERDICT_PARSE_FAIL

    def test_old_latest_post_is_stale(self):
        verdict, note = classify(
            _health(status=200, size=99, parsed=5, latest_posted="2026-01-01"),
            today=TODAY,
        )
        assert verdict == VERDICT_STALE
        assert "일 전" in note

    def test_parsed_but_nothing_stored_is_empty(self):
        verdict, _ = classify(
            _health(status=200, size=99, parsed=5, db_count=0, latest_posted=""),
            today=TODAY,
        )
        assert verdict == VERDICT_EMPTY

    def test_healthy_source_is_ok(self):
        verdict, note = classify(
            _health(status=200, size=99, parsed=5), today=TODAY
        )
        assert (verdict, note) == (VERDICT_OK, "")

    def test_api_source_without_list_parser_is_ok_with_note(self):
        verdict, note = classify(
            _health(status=200, size=99, parsed=None), today=TODAY
        )
        assert verdict == VERDICT_OK
        assert "API" in note

    def test_skipped_probe_is_not_a_verdict(self):
        verdict, _ = classify(_health(skipped=True), today=TODAY)
        assert verdict == VERDICT_SKIPPED


# ---------------------------------------------------------------------------
# DB 집계 (읽기 전용)
# ---------------------------------------------------------------------------

class TestProbeCaveats:
    """GET 1회로 1차 전략을 재현할 수 없는 소스는 비고로 고백한다."""

    def test_caveat_is_appended_without_changing_verdict(self, tmp_path):
        db_path = _make_db(tmp_path)
        rows = build_report(
            str(db_path),
            crawlers={"socialenterprise": _FakeCrawler},
            prober=lambda crawler: Probe(url="u", status=200, size=10, parsed=0),
            today=TODAY,
        )
        assert rows[0].verdict == VERDICT_PARSE_FAIL
        assert "POST AJAX" in rows[0].note

    def test_source_without_caveat_keeps_note(self):
        assert with_caveat("coop", "원래 비고") == "원래 비고"

    def test_every_caveat_names_a_real_source(self):
        from alert.main import _import_crawlers

        assert set(PROBE_CAVEATS) <= set(_import_crawlers())


class TestCollectDbStats:
    def test_recent_runs_are_summed_and_capped(self, tmp_path):
        runs = [
            ("seis", f"2026-09-{day:02d}T09:00:00", 10, 2, "success")
            for day in range(1, 11)
        ]
        db_path = _make_db(tmp_path, runs=runs)

        stats = collect_db_stats(str(db_path), recent_runs=7)
        assert len(stats["seis"]["runs"]) == 7
        assert stats["seis"]["fetched_recent"] == 70
        assert stats["seis"]["new_recent"] == 14
        assert stats["seis"]["last_success"] == "2026-09-10T09:00:00"

    def test_latest_posted_prefers_period_start_over_created_at(self, tmp_path):
        db_path = _make_db(
            tmp_path,
            announcements=[
                ("seis", "2026-09-05", "2026-09-12T10:00:00"),
                ("seis", "", "2026-09-13T10:00:00"),
            ],
        )
        stats = collect_db_stats(str(db_path))

        assert stats["seis"]["db_count"] == 2
        # period_start 가 정본이고, 없는 행만 created_at 으로 대체된다
        assert stats["seis"]["latest_posted"] == "2026-09-13T10:00:00"
        assert stats["seis"]["latest_created"] == "2026-09-13T10:00:00"

    def test_connection_is_read_only(self, tmp_path):
        """DB 를 여는 경로가 쓰기를 **구조적으로** 막는지 확인한다."""
        from scripts.source_health import _connect_ro

        db_path = _make_db(tmp_path)
        conn = _connect_ro(str(db_path))
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO run_history (started_at, source) "
                    "VALUES ('x', 'y')"
                )
        finally:
            conn.close()

    def test_last_success_ignores_failed_runs(self, tmp_path):
        db_path = _make_db(tmp_path, runs=[
            ("seis", "2026-09-13T09:00:00", 0, 0, "failed"),
            ("seis", "2026-09-12T09:00:00", 5, 5, "success"),
        ])
        stats = collect_db_stats(str(db_path))
        assert stats["seis"]["last_success"] == "2026-09-12T09:00:00"


# ---------------------------------------------------------------------------
# 조립 + 렌더
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_report_joins_db_and_probe(self, tmp_path):
        db_path = _make_db(
            tmp_path,
            runs=[("alpha", "2026-09-13T09:00:00", 12, 3, "success")],
            announcements=[("alpha", "2026-09-12", "2026-09-12T10:00:00")],
        )
        rows = build_report(
            str(db_path),
            crawlers={"alpha": _FakeCrawler},
            prober=lambda crawler: Probe(
                url="https://example.com/list", status=200, size=1234,
                parsed=9, parser="_parse_table_board",
            ),
            today=TODAY,
        )

        assert len(rows) == 1
        row = rows[0]
        assert row.source == "alpha"
        assert row.verdict == VERDICT_OK
        assert row.fetched_recent == 12
        assert row.new_recent == 3
        assert row.db_count == 1
        assert row.last_success == "2026-09-13T09:00:00"

    def test_source_with_no_history_is_reported_not_dropped(self, tmp_path):
        db_path = _make_db(tmp_path)
        rows = build_report(
            str(db_path),
            crawlers={"alpha": _FakeCrawler},
            prober=lambda crawler: Probe(
                url="u", status=200, size=10, parsed=4, parser="p"),
            today=TODAY,
        )
        assert rows[0].verdict == VERDICT_EMPTY
        assert rows[0].fetched_recent == 0

    def test_prober_is_called_once_per_source(self, tmp_path):
        db_path = _make_db(tmp_path)
        calls = []

        build_report(
            str(db_path),
            crawlers={"alpha": _FakeCrawler, "beta": _FakeCrawler},
            prober=lambda crawler: calls.append(crawler) or Probe(
                url="u", status=200, size=1, parsed=1, parser="p"),
            today=TODAY,
        )
        assert len(calls) == 2

    def test_source_filter(self, tmp_path):
        db_path = _make_db(tmp_path)
        rows = build_report(
            str(db_path),
            crawlers={"alpha": _FakeCrawler, "beta": _FakeCrawler},
            prober=lambda crawler: Probe(url="u", status=200, size=1,
                                         parsed=1, parser="p"),
            sources=["beta"],
            today=TODAY,
        )
        assert [row.source for row in rows] == ["beta"]


class TestRenderMarkdown:
    def _rows(self, tmp_path):
        db_path = _make_db(
            tmp_path,
            runs=[("alpha", "2026-09-13T09:00:00", 12, 3, "success")],
            announcements=[("alpha", "2026-09-12", "2026-09-12T10:00:00")],
        )
        return build_report(
            str(db_path),
            crawlers={"alpha": _FakeCrawler},
            prober=lambda crawler: Probe(
                url="https://example.com/list", status=200, size=1234,
                parsed=9, parser="_parse_table_board"),
            today=TODAY,
        )

    def test_table_has_header_separator_and_one_row_per_source(self, tmp_path):
        markdown = render_markdown(self._rows(tmp_path), today=TODAY)
        lines = [line for line in markdown.splitlines() if line.startswith("|")]

        assert lines[0].startswith("| source | 판정 |")
        assert set(lines[1].strip("|").split("|")) == {"---"}
        assert lines[0].count("|") == lines[2].count("|")
        assert "alpha" in lines[2]
        assert "https://example.com/list" in lines[2]

    def test_summary_lists_all_five_verdicts(self, tmp_path):
        markdown = render_markdown(self._rows(tmp_path), today=TODAY)
        for verdict in (VERDICT_OK, VERDICT_EMPTY, VERDICT_UNREACHABLE,
                        VERDICT_PARSE_FAIL, VERDICT_STALE):
            assert f"- {verdict}: " in markdown
        assert f"- {VERDICT_OK}: 1" in markdown

    def test_pipe_in_value_is_escaped(self, tmp_path):
        rows = self._rows(tmp_path)
        rows[0].note = "a|b"
        markdown = render_markdown(rows, today=TODAY)
        assert "a\\|b" in markdown
