"""scripts/source_health.py 테스트.

프로브는 **전부 스텁**이다 — 이 파일은 네트워크를 타지 않는다
(tests/test_policy_crawlers.py 의 관행과 같다).
"""
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.source_health import (  # noqa: E402
    MAX_REDIRECT_HOPS,
    PROBE_CAVEATS,
    PROBE_DEADLINE_SEC,
    PROBE_MAX_BYTES,
    PROBE_TIMEOUT_SEC,
    Probe,
    SourceHealth,
    VERDICT_EMPTY,
    VERDICT_OK,
    VERDICT_PARSE_FAIL,
    VERDICT_SKIPPED,
    VERDICT_STALE,
    VERDICT_UNREACHABLE,
    VERDICT_UNTESTABLE,
    _connect_ro,
    _read_only_uri,
    build_report,
    classify,
    collect_db_stats,
    dry_parse,
    probe_source,
    probe_url_for,
    render_markdown,
    silence_crawler_logging,
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
    """crawler.session 대역. 응답을 순서대로 돌려준다 (리다이렉트 홉 재현)."""

    def __init__(self, response=None, responses=None, exc=None):
        self.responses = list(responses) if responses is not None else (
            [response] if response is not None else []
        )
        self.exc = exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        if not self.responses:
            raise AssertionError(f"예상하지 못한 추가 GET: {url}")
        return self.responses.pop(0)


class _FakeResponse:
    """requests.Response 의 **스트리밍 계약** 부분만 흉내 낸다."""

    def __init__(self, status_code=200, text="", encoding="utf-8",
                 headers=None, chunks=None, body=None):
        self.status_code = status_code
        self.encoding = encoding
        self.headers = headers or {}
        self._body = body if body is not None else text.encode("utf-8")
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=8192):
        if self._chunks is not None:
            for chunk in self._chunks:
                yield chunk
            return
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class _FakeCrawler:
    """목록 파서 1개를 가진 최소 크롤러."""

    BASE_URL = "https://example.com"
    BOARD_PATHS = ["/board/list.do?id=1"]

    def __init__(self, parsed=3, response=None, responses=None, exc=None):
        self._parsed = parsed
        self.session = _FakeSession(
            response=response, responses=responses, exc=exc)

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
        """실제 크롤러 36종 전부에서 프로브 URL이 나온다.

        32 -> 36: 2차 미디어 RSS 4종(lifein·eroun·senews·kfnews, P1-R 계약).
        """
        from alert.main import _import_crawlers

        crawlers = _import_crawlers()
        assert len(crawlers) == 36
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
        assert probe.gets == 1

    def test_redirects_are_not_followed_by_requests(self):
        """requests 에게 맡기지 않는다 — 몇 번을 더 가는지 몰라지기 때문."""
        crawler = _FakeCrawler(response=_FakeResponse(text="<html></html>"))
        probe_source(crawler)

        _, kwargs = crawler.session.calls[0]
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True

    def test_response_is_closed(self):
        response = _FakeResponse(text="<html></html>")
        probe_source(_FakeCrawler(response=response))
        assert response.closed is True

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

    def test_euckr_body_without_charset_header_is_decoded(self):
        """헤더가 ISO-8859-1 이라고 우겨도 믿지 않는다."""
        body = "<html>공고 목록</html>".encode("cp949")
        crawler = _FakeCrawler(response=_FakeResponse(
            body=body, encoding="ISO-8859-1"))
        probe = probe_source(crawler)

        assert probe.status == 200
        assert probe.parsed == 3


class TestProbeRedirects:
    """게이트 4: 3xx 는 **직접** 1홉만 따라가고 도착지를 적는다."""

    def test_one_hop_is_followed_and_recorded(self):
        crawler = _FakeCrawler(responses=[
            _FakeResponse(status_code=302,
                          headers={"Location": "/board/real.do"}),
            _FakeResponse(text="<html></html>"),
        ])
        probe = probe_source(crawler)

        assert probe.gets == 1 + MAX_REDIRECT_HOPS == 2
        assert probe.redirect_status == 302
        assert probe.url == "https://example.com/board/list.do?id=1"
        assert probe.final_url == "https://example.com/board/real.do"
        assert probe.status == 200
        assert probe.parsed == 3

    def test_second_redirect_stops_and_reports_both_urls(self):
        crawler = _FakeCrawler(responses=[
            _FakeResponse(status_code=302, headers={"Location": "/a"}),
            _FakeResponse(status_code=302, headers={"Location": "/b"}),
        ])
        probe = probe_source(crawler)

        assert probe.gets == 2
        assert probe.status == 302
        assert "1홉" in probe.error
        assert "https://example.com/a" in probe.error

    def test_redirect_without_location_is_an_error(self):
        crawler = _FakeCrawler(responses=[_FakeResponse(status_code=302)])
        probe = probe_source(crawler)

        assert probe.gets == 1
        assert "Location" in probe.error

    def test_terminal_redirect_classifies_as_unreachable(self):
        crawler = _FakeCrawler(responses=[
            _FakeResponse(status_code=302, headers={"Location": "/a"}),
            _FakeResponse(status_code=302, headers={"Location": "/b"}),
        ])
        health = SourceHealth(source="s", probe=probe_source(crawler))
        verdict, _ = classify(health, today=TODAY)
        assert verdict == VERDICT_UNREACHABLE


class TestProbeDeadlineAndCap:
    """게이트 3: 느린 서버·거대 본문이 표 전체를 인질로 잡지 못한다."""

    def test_slow_trickle_is_cut_at_the_deadline(self):
        """소켓 타임아웃에 안 걸리는 8바이트씩 흘리기 — 벽시계가 끊는다."""
        ticks = iter([0.0] + [float(n) for n in range(1, 500)])
        chunks = [b"<div></div>" for _ in range(100)]
        crawler = _FakeCrawler(response=_FakeResponse(chunks=chunks))

        probe = probe_source(crawler, deadline_sec=3.0,
                             clock=lambda: next(ticks))

        assert probe.truncated is True
        assert probe.size < len(b"".join(chunks))

    def test_body_is_capped(self):
        big = b"x" * (PROBE_MAX_BYTES * 2)
        crawler = _FakeCrawler(response=_FakeResponse(body=big))
        probe = probe_source(crawler)

        assert probe.size == PROBE_MAX_BYTES
        assert probe.truncated is True

    def test_deadline_before_first_get_skips_the_request(self):
        ticks = iter([0.0, 99.0, 99.0, 99.0])
        crawler = _FakeCrawler(response=_FakeResponse(text="<html></html>"))
        probe = probe_source(crawler, deadline_sec=5.0,
                             clock=lambda: next(ticks))

        assert crawler.session.calls == []
        assert "시한 초과" in probe.error

    def test_truncated_parse_failure_says_so(self):
        health = SourceHealth(
            source="s",
            probe=Probe(url="u", status=200, size=PROBE_MAX_BYTES,
                        parsed=0, truncated=True),
        )
        verdict, note = classify(health, today=TODAY)
        assert verdict == VERDICT_PARSE_FAIL
        assert "끊었다" in note


class TestJobBudget:
    """게이트 3(작업 예산): 예산을 넘으면 남은 소스는 재지 않는다."""

    def test_sources_after_budget_are_marked_unmeasured(self, tmp_path):
        db_path = _make_db(tmp_path)
        probed = []
        ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])

        rows = build_report(
            str(db_path),
            crawlers={"alpha": _FakeCrawler, "beta": _FakeCrawler},
            prober=lambda crawler: probed.append(crawler) or Probe(
                url="u", status=200, size=1, parsed=1, parser="p"),
            today=TODAY,
            job_budget_sec=10.0,
            clock=lambda: next(ticks),
        )

        assert len(probed) == 1
        verdicts = {row.source: row.verdict for row in rows}
        assert verdicts["alpha"] != VERDICT_SKIPPED   # 예산 안에서 실제로 쟀다
        assert verdicts["beta"] == VERDICT_SKIPPED
        assert "예산 초과" in [r for r in rows if r.source == "beta"][0].note


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

class TestReadOnlyUri:
    """게이트 1: 경로의 `#`/`?` 가 mode=ro 를 떼어내지 못한다."""

    def test_hash_and_question_marks_are_percent_encoded(self, tmp_path):
        weird = tmp_path / "a#b?c"
        weird.mkdir()
        uri = _read_only_uri(str(weird / "health.db"))

        assert uri.endswith("?mode=ro")
        assert "%23" in uri and "%3F" in uri
        # 인코딩 전이라면 여기서 fragment/query 가 시작돼 mode=ro 가 잘린다
        assert "#" not in uri
        assert uri.count("?") == 1

    def test_hash_in_path_still_refuses_insert(self, tmp_path):
        weird = tmp_path / "a#b"
        weird.mkdir()
        db_path = _make_db(weird)

        conn = _connect_ro(str(db_path))
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO run_history (started_at, source) "
                    "VALUES ('x', 'y')")
        finally:
            conn.close()

    def test_hash_in_path_creates_no_stray_file(self, tmp_path):
        """버그판은 `…/a` 를 rwc 로 열어 **새 파일을 만들어 버린다**."""
        weird = tmp_path / "a#b"
        weird.mkdir()
        db_path = _make_db(weird)
        before = sorted(item.name for item in tmp_path.iterdir())

        conn = _connect_ro(str(db_path))
        conn.execute("SELECT 1").fetchone()
        conn.close()

        assert sorted(item.name for item in tmp_path.iterdir()) == before
        assert not (tmp_path / "a").exists()

    def test_missing_db_is_not_created(self, tmp_path):
        missing = tmp_path / "nope.db"
        with pytest.raises(sqlite3.OperationalError):
            _connect_ro(str(missing))
        assert not missing.exists()


class TestCrawlerLoggingIsSilenced:
    """게이트 2: 진단이 로그 파일·디렉터리를 만들지 않는다."""

    def test_silenced_logger_writes_no_file(self, tmp_path, monkeypatch):
        import alert.crawlers.base as crawler_base

        original = crawler_base.setup_logger
        monkeypatch.chdir(tmp_path)
        try:
            silence_crawler_logging()
            logger = crawler_base.setup_logger("crawler.probe")
            logger.info("이 줄은 어디에도 남지 않는다")

            assert [type(h) for h in logger.handlers] == [logging.NullHandler]
            assert not (tmp_path / "data").exists()
        finally:
            crawler_base.setup_logger = original

    def test_build_report_creates_no_log_dir(self, tmp_path, monkeypatch):
        """실제 크롤러를 만들어도 data/logs 가 생기지 않는다."""
        import alert.crawlers.base as crawler_base
        from alert.crawlers.coop import CoopCrawler

        original = crawler_base.setup_logger
        db_path = _make_db(tmp_path)
        work = tmp_path / "cwd"
        work.mkdir()
        monkeypatch.chdir(work)
        try:
            build_report(
                str(db_path),
                crawlers={"coop": CoopCrawler},
                prober=lambda crawler: Probe(url="u", status=200, size=1,
                                             parsed=1, parser="p"),
                today=TODAY,
            )
            assert list(work.iterdir()) == []
        finally:
            crawler_base.setup_logger = original

    def test_crawler_construction_failure_is_not_a_network_verdict(
            self, tmp_path):
        """로컬 문제(생성 실패)를 남의 서버 탓으로 적지 않는다."""
        class Broken:
            def __init__(self):
                raise OSError("Read-only file system: 'data/logs'")

        db_path = _make_db(tmp_path)
        rows = build_report(
            str(db_path),
            crawlers={"broken": Broken},
            prober=lambda crawler: Probe(url="u", status=200, size=1,
                                         parsed=1, parser="p"),
            today=TODAY,
        )
        assert rows[0].verdict == VERDICT_UNTESTABLE
        assert rows[0].verdict != VERDICT_UNREACHABLE
        assert "Read-only file system" in rows[0].note


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
        assert "| GET |" in lines[0]
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

    def test_header_states_the_probe_limits(self, tmp_path):
        markdown = render_markdown(self._rows(tmp_path), today=TODAY)
        assert f"{PROBE_MAX_BYTES}바이트" in markdown
        assert f"{PROBE_DEADLINE_SEC:.0f}s" in markdown

    def test_redirect_and_truncation_are_disclosed(self, tmp_path):
        rows = self._rows(tmp_path)
        rows[0].probe.redirect_status = 302
        rows[0].probe.final_url = "https://example.com/moved"
        rows[0].probe.truncated = True
        markdown = render_markdown(rows, today=TODAY)

        assert "HTTP 302 1홉" in markdown
        assert "https://example.com/moved" in markdown
        assert "상한에서 끊음" in markdown
