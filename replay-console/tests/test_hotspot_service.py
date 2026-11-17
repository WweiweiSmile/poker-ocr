"""偏差热点服务层（#59）：run_items / 改判 / 冻结 spec 快照 → 报表。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from replay.errors import ApiError
from replay.service import ReplayService


class _FakeSpecRepo:
    def __init__(self, rows):
        self.rows = {int(row["id"]): row for row in rows}

    def get(self, spec_id):
        return self.rows.get(int(spec_id))

    def list_latest(self):
        return list(self.rows.values())


class _FakeRunRepo:
    def __init__(self, row):
        self.row = row

    def require(self, run_id):
        if int(run_id) != int(self.row["id"]):
            raise ApiError("RUN_NOT_FOUND", "运行不存在", 404)
        return self.row


class _FakeRunItemRepo:
    def __init__(self, rows):
        self.rows = rows

    def list(self, run_id, reconstruct_version, **kwargs):
        return list(self.rows)

    def counts(self, run_id):
        return {"items": len(self.rows),
                "cases": len({row["case_id"] for row in self.rows}),
                "fails": 0, "errors": 0}

    def public_item(self, row):
        return dict(row)

    def get(self, run_id, case_id, repeat_index, reconstruct_version):
        for row in self.rows:
            if (row["case_id"], row["repeat_index"]) == (case_id,
                                                         repeat_index):
                return row
        return None


class _FakeOverrideRepo:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def list(self, run_id):
        return dict(self.rows)


class _FakeCaseRepo:
    def __init__(self, candidates=None):
        self.candidates = candidates or []

    def require_set(self, set_id):
        return {"id": int(set_id), "name": "seed", "revision": 1,
                "frozen_at": None}

    def candidates_for_bucket(self, set_id, bucket, *, limit=50, offset=0):
        self.last_bucket = bucket
        return list(self.candidates)


def _spec_row(spec_id, *, name="s", bucket=None, freq=None, status="active"):
    spec = {"schema": "spot_spec.v1", "allowed_actions": [], "freq": freq}
    return {
        "id": spec_id, "name": name, "revision": 1, "status": status,
        "bucket_json": json.dumps(bucket or {}),
        "spec_json": json.dumps(spec),
    }


def _item_row(case_id, *, spec_id, score, kind="raise", hard_fail=None,
              repeat=0, source="model"):
    return {
        "case_id": case_id, "spec_id": spec_id, "repeat_index": repeat,
        "score": score, "scores_json": json.dumps({"action": 0.5}),
        "hard_fail": hard_fail, "option_kind": kind, "source": source,
        "case_priority": 0,
    }


def _run_row(specs, *, metrics=None):
    return {
        "id": 5, "status": "succeeded", "case_set_id": 4,
        "case_set_revision": 1, "set_name": "seed", "set_frozen_at": "x",
        "version_id": 7, "version_name": "v", "version_model": "m",
        "spec_ids_json": json.dumps([s["id"] for s in specs]),
        "judge_version": "rules-v1",
        "reconstruct_version": "canonical-v1",
        "total_cases": 1, "done_cases": 1, "scored_cases": 1,
        "score": 50.0, "p0_pass_rate": 1.0, "coverage_rate": 1.0,
        "metrics_json": json.dumps(metrics) if metrics else None,
        "config_snapshot": json.dumps({"specs": specs}),
        "cancel_requested": 0, "error_msg": "",
        "created_at": "2026-11-17 10:00:00",
    }


def _service(*, specs, items, overrides=None, metrics=None,
             candidates=None):
    service = ReplayService.__new__(ReplayService)
    service.settings = SimpleNamespace(console_user_id=1, price_table={})
    service.spec_repo = _FakeSpecRepo(specs)
    service.run_repo = _FakeRunRepo(_run_row(specs, metrics=metrics))
    service.run_item_repo = _FakeRunItemRepo(items)
    service.override_repo = _FakeOverrideRepo(overrides)
    service.case_repo = _FakeCaseRepo(candidates)
    return service


def test_hotspots_groups_by_spec_and_uses_first_repeat_only():
    specs = [
        {"id": 11, "name": "flop cbet", "bucket": {"street": ["flop"]}},
        {"id": 12, "name": "river thin", "bucket": {"street": ["river"]}},
    ]
    items = [_item_row(i, spec_id=11, score=40) for i in range(1, 21)]
    items += [_item_row(i, spec_id=12, score=90) for i in range(21, 41)]
    items += [_item_row(1, spec_id=11, score=0, repeat=1)]  # P0 重跑不算
    service = _service(specs=specs, items=items,
                       metrics={"score": {"n": 40, "mean": 65.0,
                                          "lo": 50, "hi": 80}})
    out = service.score_run_hotspots(5)
    assert [row["specId"] for row in out["rows"]] == [11, 12]
    assert out["rows"][0]["n"] == 20
    assert out["rows"][0]["mean"] == 40.0
    assert out["rows"][0]["delta"] == -25.0
    assert out["summary"]["cases"] == 40
    assert out["summary"]["scored"] == 40
    assert out["run"]["id"] == 5


def test_hotspots_use_frozen_spec_freq_and_override_scores():
    specs = [{"id": 11, "name": "flop cbet", "bucket": {"street": ["flop"]}}]
    spec_rows = [_spec_row(11, bucket={"street": ["flop"]},
                           freq={"action": "raise", "min": 0.3, "max": 0.5})]
    items = [_item_row(i, spec_id=11, score=20) for i in range(1, 21)]
    overrides = {1: {"scores": {"action": 1.0}, "hardFail": None}}
    service = _service(specs=specs, items=items)
    service.spec_repo = _FakeSpecRepo(spec_rows)
    service.override_repo = _FakeOverrideRepo(overrides)
    out = service.score_run_hotspots(5)
    row = out["rows"][0]
    assert row["freq"]["action"] == "raise"
    assert row["freq"]["status"] == "missed"
    assert row["freq"]["n"] == 20
    # 改判只影响该条的有效分；19×20 + 1×100 → 24.0
    assert row["mean"] == 24.0
    assert out["summary"]["attainment"]["missed"] == 1


def test_drilldown_orders_by_effective_score_after_override():
    """改判过的条目要按有效分重排，否则「最差优先」和报表自相矛盾。"""
    specs = [{"id": 11, "name": "s", "bucket": {"street": ["flop"]}}]
    items = [
        _item_row(1, spec_id=11, score=90),
        _item_row(2, spec_id=11, score=10, hard_fail="HF_OPTION"),
        _item_row(3, spec_id=11, score=50),
    ]
    # 第 2 条人工改判为满分、无硬失败：它不该再排在第一位
    overrides = {2: {"scores": {"action": 1.0, "size": 1.0, "reason": 1.0,
                                "fact": 1.0, "stable": 1.0},
                     "hardFail": None}}
    service = _service(specs=specs, items=items, overrides=overrides)
    out = service.score_run_items(5, {"specId": 11, "repeat": 0,
                                      "sort": "score"})
    assert [item["case_id"] for item in out["items"]] == [3, 1, 2]
    assert out["total"] == 3 and out["truncated"] is False


def test_hotspots_prefer_frozen_snapshot_freq():
    """频次目标也冻进 run 快照：revision 被删了，旧 run 的口径不能变。"""
    specs = [{"id": 11, "name": "s", "bucket": {"street": ["flop"]},
              "freq": {"action": "raise", "min": 0.3, "max": 0.5}}]
    items = [_item_row(i, spec_id=11, score=20) for i in range(1, 21)]
    service = _service(specs=specs, items=items)
    service.spec_repo = _FakeSpecRepo([])  # 按 id 已读不到 revision
    row = service.score_run_hotspots(5)["rows"][0]
    assert row["freq"]["status"] == "missed"
    assert row["freq"]["observed"] == 1.0
    assert row["basis"] == "freq"


def test_hotspots_fallback_global_mean_when_metrics_missing():
    specs = [{"id": 11, "name": "s", "bucket": {"street": ["flop"]}}]
    items = [_item_row(i, spec_id=11, score=50) for i in range(1, 4)]
    service = _service(specs=specs, items=items)
    out = service.score_run_hotspots(5)
    assert out["summary"]["score"]["mean"] == 50.0
    assert out["rows"][0]["delta"] == 0.0


def test_case_candidates_marks_membership_and_matches_spec():
    service = _service(specs=[], items=[])
    service.spec_repo = _FakeSpecRepo([
        _spec_row(11, bucket={"street": ["flop"]}, name="flop cbet"),
    ])
    service.case_repo = _FakeCaseRepo([
        {"canonical_id": 1001, "street": "flop", "seq": 2, "action_index": 2,
         "position_bucket": "IP", "hand_tier": "STRONG_MADE",
         "board_texture": "干燥", "pot_type": "single", "facing": 1,
         "opponent_profile": "未知", "hero_cards": "AhAd", "board": "Kd7c2s",
         "hero_position": "BTN", "case_id": 9},
        {"canonical_id": 1002, "street": "flop", "seq": 3, "action_index": 3,
         "position_bucket": "IP", "hand_tier": "AIR",
         "board_texture": "干燥", "pot_type": "single", "facing": 1,
         "opponent_profile": "未知", "hero_cards": "9h8h", "board": "Kd7c2s",
         "hero_position": "BB", "case_id": None},
    ])
    out = service.case_candidates(4, {"street": "flop,", "hand_tier": ""})
    assert service.case_repo.last_bucket == {"street": ["flop"]}
    assert out["items"][0]["decisionKey"] == "flop:2"
    assert out["items"][0]["inSet"] is True
    assert out["items"][0]["specId"] == 11
    assert out["items"][0]["hand"]["heroCards"] == "AhAd"
    assert out["items"][1]["inSet"] is False
    assert out["items"][1]["specId"] == 11
