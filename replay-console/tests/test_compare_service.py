"""版本对照服务层（#60）：两次运行的 run_items / 改判 / 冻结快照 → 翻转表。

口径重点：配对按决策点身份（跨案例集 revision 稳定）、只比交集但报出被
排除数、方向先看硬失败再看有效分、跨版本必须带告警、导出含验收要求的列。
"""
from __future__ import annotations

import csv
import io
import json
from types import SimpleNamespace

import pytest

from replay.errors import ApiError
from replay.service import ReplayService

SPECS_LEFT = [{"id": 11, "name": "flop cbet", "revision": 1,
               "bucket": {"street": ["flop"]}}]
SPECS_RIGHT = [{"id": 11, "name": "flop cbet", "revision": 2,
                "bucket": {"street": ["flop"]}}]


class _FakeRunRepo:
    def __init__(self, rows):
        self.rows = {int(row["id"]): row for row in rows}

    def require(self, run_id):
        row = self.rows.get(int(run_id))
        if row is None:
            raise ApiError("RUN_NOT_FOUND", f"运行 {run_id} 不存在", 404)
        return row


class _FakeRunItemRepo:
    def __init__(self, rows):
        self.rows = rows  # {run_id: [row, ...]}

    def list(self, run_id, reconstruct_version, *, only_hardfail=False,
             spec_id=None, repeat_index=None, limit=200, offset=0):
        rows = list(self.rows.get(int(run_id)) or [])
        return rows[offset:offset + limit]

    def counts(self, run_id):
        return {"items": 0, "cases": 0, "fails": 0, "errors": 0}

    def public_item(self, row):
        from replay.run_repository import RunItemRepository

        return RunItemRepository.public_item(row)


class _FakeOverrideRepo:
    def __init__(self, rows=None):
        self.rows = rows or {}  # {run_id: {case_id: override}}

    def list(self, run_id):
        return dict(self.rows.get(int(run_id)) or {})

    def get(self, run_id, case_id):
        return (self.rows.get(int(run_id)) or {}).get(int(case_id))


def _item_row(case_id, canonical_id, decision_key, *, score, kind="raise",
              amount=6.0, hard_fail=None, repeat=0, source="model",
              spec_id=11):
    return {
        "run_id": 0, "case_id": case_id, "spec_id": spec_id,
        "repeat_index": repeat, "option_id": kind, "option_kind": kind,
        "option_amount": amount, "reason": "有位置优势",
        "source": source, "scores_json": json.dumps({"action": 1.0}),
        "score": score, "hard_fail": hard_fail, "error_msg": "",
        "duration_ms": 10, "created_at": "2026-11-17 10:00:00",
        "case_priority": 1, "canonical_id": canonical_id,
        "decision_key": decision_key, "hero_cards": "AhAd",
        "board": "Kd7c2s", "hero_position": "BTN", "action_index": 2,
        "decision_pot": 6.0, "position_bucket": "IP",
        "hand_tier": "STRONG_MADE", "board_texture": "干燥",
        "pot_type": "single", "facing": 1, "opponent_profile": "未知",
    }


def _run_row(run_id, *, case_set_id=4, revision=1, specs=None,
             judge="rules-v1", reconstruct="canonical-v1",
             status="succeeded", score=50.0):
    specs = specs if specs is not None else SPECS_LEFT
    return {
        "id": run_id, "status": status, "case_set_id": case_set_id,
        "case_set_revision": revision, "set_name": "seed",
        "set_frozen_at": "2026-11-17 09:00:00",
        "version_id": 7 if run_id % 2 else 8, "version_name": f"v{run_id}",
        "version_model": "deepseek-v4",
        "spec_ids_json": json.dumps([spec["id"] for spec in specs]),
        "judge_version": judge, "reconstruct_version": reconstruct,
        "total_cases": 2, "done_cases": 2, "scored_cases": 2,
        "score": score, "p0_pass_rate": 1.0, "coverage_rate": 1.0,
        "metrics_json": None,
        "config_snapshot": json.dumps({"specs": specs, "judge": {}}),
        "cancel_requested": 0, "error_msg": "",
        "created_at": "2026-11-17 10:00:00",
    }


def _service(runs, items, overrides=None):
    service = ReplayService.__new__(ReplayService)
    service.settings = SimpleNamespace(console_user_id=1, price_table={})
    service.run_repo = _FakeRunRepo(runs)
    service.run_item_repo = _FakeRunItemRepo(items)
    service.override_repo = _FakeOverrideRepo(overrides)
    return service


def _two_runs():
    """同两个决策点，但案例集 revision 不同 → case_id 完全不重叠。"""
    runs = [_run_row(1, revision=1, specs=SPECS_LEFT),
            _run_row(2, revision=2, specs=SPECS_RIGHT)]
    items = {
        1: [_item_row(101, 7, "flop:2", score=80.0),
            _item_row(102, 7, "turn:1", score=40.0, hard_fail=None)],
        2: [_item_row(201, 7, "flop:2", score=60.0),
            _item_row(202, 7, "turn:1", score=40.0)],
    }
    return runs, items


def test_compare_pairs_across_case_set_revisions():
    runs, items = _two_runs()
    out = _service(runs, items).compare_score_runs(1, 2)
    assert out["cases"] == {"left": 2, "right": 2, "common": 2,
                            "excludedLeft": 0, "excludedRight": 0}
    assert [row["decisionKey"] for row in out["rows"]] == ["flop:2", "turn:1"]
    first = out["rows"][0]
    assert first["direction"] == "worse" and first["delta"] == -20.0
    assert first["left"]["caseId"] == 101
    assert first["right"]["caseId"] == 201
    assert first["left"]["specRevision"] == 1
    assert first["right"]["specRevision"] == 2
    assert first["hand"]["heroCards"] == "AhAd"


def test_compare_uses_effective_score_after_override():
    runs, items = _two_runs()
    overrides = {2: {201: {"scores": {"action": 0.0}, "hardFail": None}}}
    out = _service(runs, items, overrides).compare_score_runs(1, 2)
    first = out["rows"][0]
    assert first["right"]["overridden"] is True
    # 改判把 flop:2 从 60 改成 0：80 → 0
    assert first["right"]["score"] == 0.0
    assert first["delta"] == -80.0


def test_compare_reports_excluded_and_filters_direction():
    runs = [_run_row(1), _run_row(2)]
    items = {
        1: [_item_row(101, 7, "flop:2", score=80.0),
            _item_row(102, 8, "flop:1", score=50.0)],
        2: [_item_row(201, 7, "flop:2", score=60.0),
            _item_row(203, 9, "river:1", score=30.0)],
    }
    service = _service(runs, items)
    out = service.compare_score_runs(1, 2)
    assert out["cases"] == {"left": 2, "right": 2, "common": 1,
                            "excludedLeft": 1, "excludedRight": 1}
    assert len(out["rows"]) == 1
    only = service.compare_score_runs(1, 2, {"only": "improved"})
    assert only["rows"] == [] and only["total"] == 0
    assert only["directions"] == out["directions"]


def test_compare_rejects_same_run_and_unknown_filter():
    runs = [_run_row(1)]
    service = _service(runs, {1: []})
    with pytest.raises(ApiError) as same:
        service.compare_score_runs(1, 1)
    assert same.value.status_code == 422 and same.value.code == "COMPARE_SAME_RUN"
    with pytest.raises(ApiError) as bad:
        service.compare_score_runs(1, 2, {"only": "flip"})
    assert bad.value.code == "INVALID_DIRECTION"
    assert bad.value.status_code == 422


def test_compare_flags_cross_version_and_unfinished_run():
    runs = [_run_row(1, status="partial"),
            _run_row(2, revision=2, specs=SPECS_RIGHT)]
    items = {1: [], 2: []}
    out = _service(runs, items).compare_score_runs(1, 2)
    codes = [warning["code"] for warning in out["warnings"]]
    assert codes == ["CASE_SET_CHANGED", "SPEC_CHANGED", "RUN_NOT_FINISHED"]
    assert out["versions"]["crossVersion"] is True
    assert out["versions"]["caseSet"]["same"] is False


def test_compare_ignores_repeat_runs():
    runs = [_run_row(1), _run_row(2)]
    items = {
        1: [_item_row(101, 7, "flop:2", score=80.0)],
        2: [_item_row(201, 7, "flop:2", score=60.0),
            _item_row(201, 7, "flop:2", score=10.0, repeat=1)],
    }
    out = _service(runs, items).compare_score_runs(1, 2)
    assert out["cases"]["right"] == 1
    assert out["rows"][0]["right"]["score"] == 60.0


def test_compare_marks_error_side_incomparable():
    runs = [_run_row(1), _run_row(2)]
    items = {
        1: [_item_row(101, 7, "flop:2", score=None, kind=None,
                      source="error")],
        2: [_item_row(201, 7, "flop:2", score=60.0)],
    }
    out = _service(runs, items).compare_score_runs(1, 2)
    row = out["rows"][0]
    assert row["direction"] == "incomparable" and row["delta"] is None
    assert out["summary"]["scored"] == 0
    assert out["summary"]["left"]["mean"] is None


def test_export_csv_carries_acceptance_columns_and_all_rows():
    runs, items = _two_runs()
    service = _service(runs, items)
    fmt, filename, body = service.export_score_run_comparison(
        1, 2, {"limit": "1"})
    assert fmt == "csv"
    assert filename == "replay-compare-1-vs-2.csv"
    assert body.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(body[1:])))
    assert len(rows) == 2  # 导出不受分页 limit 影响
    header = set(rows[0])
    assert {"case_id_left", "case_id_right", "canonical_id", "decision_key",
            "bucket_label", "score_left", "score_right", "delta",
            "direction", "direction_label", "case_set_left", "case_set_right",
            "spec_revision_left", "spec_revision_right"} <= header
    assert rows[0]["direction"] == "worse"
    assert rows[0]["direction_label"] == "改坏"
    assert rows[0]["case_set_left"] == "#4 rev1"
    assert rows[0]["case_set_right"] == "#4 rev2"
    assert rows[0]["score_left"] == "80.0" and rows[0]["score_right"] == "60.0"
    # 页面筛选跟着导出走：不会导出看不见的行
    _, _, only_body = service.export_score_run_comparison(
        1, 2, {"only": "unchanged"})
    only_rows = list(csv.DictReader(io.StringIO(only_body[1:])))
    assert [row["direction"] for row in only_rows] == ["unchanged"]


def test_export_rejects_unknown_format():
    runs, items = _two_runs()
    with pytest.raises(ApiError) as err:
        _service(runs, items).export_score_run_comparison(1, 2,
                                                          {"format": "pdf"})
    assert err.value.status_code == 422
