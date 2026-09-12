"""`핀 n` 승격 (V4 계약 ③) — compose 단계의 결정론 검사.

승격은 편집자의 명시적 선택이지만 **무제한 권한이 아니다**. 마감이 지난 공고,
카톡 한 조각에 안 들어가는 URL 은 핀으로도 되살아나지 않는다 — 죽은 정보를 보내지
않는 규율(위협 모델 ②)이 편집자 선택보다 위다. 그 경계가 이 파일의 주제다.
"""

from pathlib import Path

from alert.digest.composer import (
    HOLD_REASON_DIVERSITY,
    HOLD_REASON_PIN_BUMPED,
    HOLD_REASON_PIN_CAP,
    HOLD_REASON_SECTION_CAP,
    HOLD_REASON_URL_TOO_LONG,
    SECTION_LIMITS,
    SOURCE_DIVERSITY_LIMIT,
    VERDICT_APPLY,
    VERDICT_HOLD,
    VERDICT_NOTICE,
    classify_item,
    compose_digest_data,
)
from alert.digest import state as state_mod

from tests.test_digest import (
    W13,
    W13_TODAY,
    _create_announcements_table,
    _insert_one,
)

B2C_TITLE = "숲속 힐링 캠프 참가자 모집 공고"
APPLY_TITLE = "산림 분야 지원사업 참여기업 모집 공고"


def _ids(items):
    return [item["id"] for item in items]


def _hold_reason(data, item_id):
    for item in data["holds"]:
        if item["id"] == item_id:
            return item["reason"]
    return None


class TestPinSectionTarget:
    """분류가 보류 항목의 **승격 목적지**를 미리 정한다 (편집자는 번호만 고른다)."""

    def test_b2c_hold_targets_apply(self):
        classification = classify_item(B2C_TITLE, "요약", "kofpi")
        assert classification.verdict == VERDICT_HOLD
        assert classification.pin_section == VERDICT_APPLY

    def test_unclassified_hold_targets_notice(self):
        classification = classify_item(
            "국유림 산림경영 현장 이야기", "요약", "forest_press"
        )
        assert classification.verdict == VERDICT_HOLD
        assert classification.pin_section == VERDICT_NOTICE

    def test_section_verdicts_have_no_pin_target(self):
        """섹션에 실리는 항목은 승격 대상이 아니다 — 목적지를 갖지 않는다."""
        assert classify_item(APPLY_TITLE, "요약", "kofpi").pin_section is None


class TestPinPromotion:
    def test_pin_promotes_b2c_hold_into_apply(self, tmp_path):
        db_path = tmp_path / "pin.db"
        _create_announcements_table(db_path)
        _insert_one(db_path, source_id="b2c", title=B2C_TITLE,
                    url="https://example.com/b2c", period_end="2026-12-20")

        before = compose_digest_data(str(db_path), week_str=W13,
                                     today=W13_TODAY)
        assert _ids(before["sections"][VERDICT_APPLY]) == []
        held = before["holds"][0]
        assert held["reason"].startswith("참가자 모집(B2C)")

        after = compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY, pin_ids={held["id"]})
        assert _ids(after["sections"][VERDICT_APPLY]) == [held["id"]]
        assert after["holds"] == []

    def test_pin_restores_diversity_overflow(self, tmp_path):
        """소스 다양성으로 밀렸던 항목은 제 섹션(신청)으로 되돌아온다.

        다양성 상한 자체는 유지된다 — 승격한 항목이 그 소스의 한 칸을 차지하므로
        같은 소스의 다른 항목 하나가 대신 밀린다(상한을 넓히는 것이 아니라 **자리를
        바꾸는** 것이다).
        """
        db_path = tmp_path / "diversity.db"
        _create_announcements_table(db_path)
        titles = (
            "산림분야 오픈이노베이션 참여기업 모집 공고",
            "임산물 가공유통 지원사업 참여업체 모집",
            "목재산업 시설 개선 지원 참여기업 모집",
        )
        for index, title in enumerate(titles):
            _insert_one(db_path, source="kofpi", source_id=f"div_{index}",
                        title=title, url=f"https://example.com/div-{index}",
                        period_end=f"2026-12-2{index}")

        before = compose_digest_data(str(db_path), week_str=W13,
                                     today=W13_TODAY)
        assert len(before["sections"][VERDICT_APPLY]) == SOURCE_DIVERSITY_LIMIT
        pushed = [item for item in before["holds"]
                  if item["reason"] == HOLD_REASON_DIVERSITY][0]

        after = compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY, pin_ids={pushed["id"]})
        assert pushed["id"] in _ids(after["sections"][VERDICT_APPLY])
        assert len(after["sections"][VERDICT_APPLY]) == SOURCE_DIVERSITY_LIMIT
        # 자리를 내준 쪽은 사유와 함께 보류로 남는다 (무기록 삭제 없음)
        assert [item["reason"] for item in after["holds"]] == [
            HOLD_REASON_DIVERSITY
        ]

    def test_pin_bumps_last_item_when_section_is_full(self, tmp_path):
        """상한이 찬 섹션에 승격하면 **마지막 항목**이 보류로 내려가고 사유가 남는다."""
        db_path = tmp_path / "bump.db"
        _create_announcements_table(db_path)
        # 신청 5칸을 서로 다른 소스로 채운다 (다양성 상한을 건드리지 않게)
        sources = ("kofpi", "forest_service", "coop", "socialenterprise", "seis")
        for index, source in enumerate(sources):
            _insert_one(db_path, source=source, source_id=f"full_{index}",
                        title=f"산림 사회적기업 지원사업 참여기업 모집 {index}",
                        url=f"https://example.com/full-{index}",
                        period_end=f"2026-12-1{index}")
        _insert_one(db_path, source="fowi", source_id="b2c",
                    title=B2C_TITLE, url="https://example.com/b2c",
                    period_end="2026-12-31")

        before = compose_digest_data(str(db_path), week_str=W13,
                                     today=W13_TODAY)
        assert len(before["sections"][VERDICT_APPLY]) == \
            SECTION_LIMITS[VERDICT_APPLY]
        last = before["sections"][VERDICT_APPLY][-1]
        held = [item for item in before["holds"]
                if item["url"] == "https://example.com/b2c"][0]

        after = compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY, pin_ids={held["id"]})
        section_ids = _ids(after["sections"][VERDICT_APPLY])
        assert held["id"] in section_ids
        assert len(section_ids) == SECTION_LIMITS[VERDICT_APPLY]
        assert last["id"] not in section_ids
        assert _hold_reason(after, last["id"]) == HOLD_REASON_PIN_BUMPED

    def test_pin_keeps_section_order(self, tmp_path):
        """승격해도 섹션 안 순서는 마감순이다 (핀이 맨 앞에 끼지 않는다)."""
        db_path = tmp_path / "order.db"
        _create_announcements_table(db_path)
        _insert_one(db_path, source="kofpi", source_id="early",
                    title="산림 사회적기업 지원사업 참여기업 모집 A",
                    url="https://example.com/early", period_end="2026-04-01")
        _insert_one(db_path, source="coop", source_id="late",
                    title="산림 사회적기업 지원사업 참여기업 모집 B",
                    url="https://example.com/late", period_end="2026-12-31")
        _insert_one(db_path, source="fowi", source_id="b2c",
                    title="산림 체험 캠프 참가자 모집 공고",
                    url="https://example.com/b2c", period_end="2026-06-01")

        held = [item for item in
                compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY)["holds"]
                if item["url"] == "https://example.com/b2c"][0]
        after = compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY, pin_ids={held["id"]})
        deadlines = [item["deadline"]
                     for item in after["sections"][VERDICT_APPLY]]
        assert deadlines == sorted(deadlines)

    def test_pin_cannot_revive_expired_deadline(self, tmp_path):
        """마감 경과는 핀보다 강하다 — 죽은 정보는 편집자 선택으로도 살아나지 않는다."""
        db_path = tmp_path / "expired.db"
        _create_announcements_table(db_path)
        _insert_one(db_path, source="kofpi", source_id="old",
                    title=B2C_TITLE, url="https://example.com/old",
                    period_end="2026-03-01")       # W13_TODAY 이전

        held = compose_digest_data(str(db_path), week_str=W13,
                                   today=W13_TODAY)["holds"]
        assert [item["reason"] for item in held][0].startswith("참가자 모집")

        after = compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY,
                                    pin_ids={held[0]["id"]})
        assert after["sections"][VERDICT_APPLY] == []
        assert [item["reason"] for item in after["excluded"]] == ["마감 경과"]

    def test_pin_cannot_revive_oversize_url(self, tmp_path):
        """카톡 한 조각에 못 들어가는 URL 은 핀으로도 발송본에 오르지 않는다."""
        db_path = tmp_path / "oversize.db"
        _create_announcements_table(db_path)
        long_url = "https://example.com/" + ("z" * 4200)
        _insert_one(db_path, source="kofpi", source_id="long",
                    title=B2C_TITLE, url=long_url, period_end="2026-12-31")

        held = compose_digest_data(str(db_path), week_str=W13,
                                   today=W13_TODAY)["holds"][0]
        after = compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY, pin_ids={held["id"]})
        assert after["sections"][VERDICT_APPLY] == []
        assert _hold_reason(after, held["id"]) == HOLD_REASON_URL_TOO_LONG

    def test_pin_overflow_beyond_limit_is_recorded(self, tmp_path):
        """핀이 상한보다 많으면 넘친 핀도 무기록 삭제가 아니라 보류로 남는다."""
        db_path = tmp_path / "pinflood.db"
        _create_announcements_table(db_path)
        # 협의회 소스 풀 안의 서로 다른 소스로 채운다 (다양성 상한을 건드리지 않게)
        pool = ("kofpi", "forest_service", "forest_press", "fowi", "lawmaking",
                "coop", "socialenterprise")
        assert len(pool) == SECTION_LIMITS[VERDICT_APPLY] + 2
        for index, source in enumerate(pool):
            _insert_one(db_path, source=source, source_id=f"b2c_{index}",
                        title=f"산림 체험 캠프 참가자 모집 공고 {index}",
                        url=f"https://example.com/b2c-{index}",
                        period_end=f"2026-12-0{index + 1}")

        holds = compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY)["holds"]
        pin_ids = {item["id"] for item in holds}
        assert len(pin_ids) == SECTION_LIMITS[VERDICT_APPLY] + 2

        after = compose_digest_data(str(db_path), week_str=W13,
                                    today=W13_TODAY, pin_ids=pin_ids)
        assert len(after["sections"][VERDICT_APPLY]) == \
            SECTION_LIMITS[VERDICT_APPLY]
        overflow = [item for item in after["holds"]
                    if item["reason"] == HOLD_REASON_PIN_CAP]
        assert len(overflow) == 2

    def test_partition_invariant_holds_with_pins(self, tmp_path):
        """창 내 모든 후보 = 섹션 ∪ 보류 ∪ 배제 ∪ 병합됨 (승격 뒤에도)."""
        db_path = tmp_path / "invariant.db"
        _create_announcements_table(db_path)
        for index in range(4):
            _insert_one(db_path, source="kofpi", source_id=f"apply_{index}",
                        title=f"산림 사회적기업 지원사업 참여기업 모집 {index}",
                        url=f"https://example.com/apply-{index}",
                        period_end=f"2026-12-1{index}")
        _insert_one(db_path, source="fowi", source_id="b2c",
                    title=B2C_TITLE, url="https://example.com/b2c",
                    period_end="2026-12-31")

        base = compose_digest_data(str(db_path), week_str=W13, today=W13_TODAY)
        held = [item for item in base["holds"]
                if item["url"] == "https://example.com/b2c"][0]
        data = compose_digest_data(str(db_path), week_str=W13,
                                   today=W13_TODAY, pin_ids={held["id"]})

        placed = (
            _ids(data["sections"][VERDICT_APPLY])
            + _ids(data["sections"][VERDICT_NOTICE])
            + _ids(data["holds"])
            + _ids(data["excluded"])
            + list(data["merged_ids"])
        )
        assert len(placed) == len(set(placed)), "두 곳에 배치된 항목이 있다"
        assert set(placed) == set(data["candidate_ids"])

    def test_no_pins_is_unchanged(self, tmp_path):
        """승격이 없으면 산출물은 승격 배선 이전과 같다 (회귀 가드)."""
        db_path = tmp_path / "regress.db"
        _create_announcements_table(db_path)
        for index in range(6):
            _insert_one(db_path, source="kofpi", source_id=f"r_{index}",
                        title=f"산림 사회적기업 지원사업 참여기업 모집 {index}",
                        url=f"https://example.com/r-{index}",
                        period_end=f"2026-12-1{index}")

        data = compose_digest_data(str(db_path), week_str=W13, today=W13_TODAY)
        assert len(data["sections"][VERDICT_APPLY]) == SOURCE_DIVERSITY_LIMIT
        reasons = {item["reason"] for item in data["holds"]}
        assert reasons <= {HOLD_REASON_DIVERSITY, HOLD_REASON_SECTION_CAP}
        assert HOLD_REASON_PIN_BUMPED not in reasons


class TestWeeklyDigestPinsFlag:
    """`--pins` 는 상태 파일의 `pinned_ids` 를 조립에 넘기는 유일한 손잡이다."""

    def _run(self, tmp_path, monkeypatch, argv_extra, pinned):
        from scripts import weekly_digest

        out_dir = tmp_path / "digests"
        out_dir.mkdir()
        state_path = state_mod.state_path(W13, out_dir)
        state_mod.save_state(
            state_path,
            state_mod.add_pinned_ids(state_mod.default_state(W13), pinned),
        )
        seen = {}

        def fake_compose(**kwargs):
            seen["pin_ids"] = kwargs.get("pin_ids")
            Path(kwargs["output_path"]).write_text("# x\n", encoding="utf-8")
            return "# x\n"

        monkeypatch.setattr(weekly_digest, "compose_digest", fake_compose)
        monkeypatch.setattr(
            weekly_digest, "check_digest",
            lambda **kwargs: {"pass": True, "items": [1], "dropped": [],
                              "reason": ""},
        )
        monkeypatch.setattr(weekly_digest, "write_check_result",
                            lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "sys.argv",
            ["weekly_digest.py", "--week", W13, "--out-dir", str(out_dir),
             "--db", str(tmp_path / "none.db")] + argv_extra,
        )
        assert weekly_digest.main() == 0
        return seen

    def test_pins_flag_passes_state_ids(self, tmp_path, monkeypatch):
        seen = self._run(tmp_path, monkeypatch, ["--pins"], [11, 12])
        assert seen["pin_ids"] == {11, 12}

    def test_without_flag_pins_are_ignored(self, tmp_path, monkeypatch):
        seen = self._run(tmp_path, monkeypatch, [], [11, 12])
        assert seen["pin_ids"] is None
