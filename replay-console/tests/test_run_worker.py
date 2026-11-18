"""评分运行 worker（#57）：执行单元展开、稳定维、中断标 partial。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from replay import run_worker, scoring
from replay.run_worker import RunWorker


class _FakeItems:
    def __init__(self, rows):
        self.rows = rows
        self.updated = []

    def list(self, run_id, reconstruct_version, **kwargs):
        return [dict(row) for row in self.rows]

    def update_scores(self, *, run_id, case_id, repeat_index, scores, score,
                      hard_fail):
        self.updated.append({"case_id": case_id,
                             "repeat_index": repeat_index,
                             "scores": scores, "score": score,
                             "hard_fail": hard_fail})
        for row in self.rows:
            if (row["case_id"], row["repeat_index"]) == (case_id,
                                                         repeat_index):
                row["scores_json"] = json.dumps(scores)
                row["score"] = score


class _FakeRuns:
    def __init__(self):
        self.finished = []

    def finish(self, run_id, status, **kwargs):
        self.finished.append({"run_id": run_id, "status": status, **kwargs})
        return 1


def _row(case_id, repeat_index=0, kind="call", score=95.0, source="model",
         priority=0, hard_fail=None, scores=None):
    return {
        "case_id": case_id,
        "repeat_index": repeat_index,
        "option_kind": kind,
        "scores_json": json.dumps(scores or {"action": 1.0, "size": 1.0,
                                             "reason": 1.0, "fact": 1.0}),
        "source": source,
        "score": score,
        "case_priority": priority,
        "hard_fail": hard_fail,
        "decision_key": "flop:2",
        "position_bucket": "IP", "hand_tier": "strong",
        "board_texture": "dry", "pot_type": "srp", "facing": "bet",
        "opponent_profile": "unknown",
    }


class _FakeOverrides:
    def list(self, run_id):
        return {}


def _worker(rows):
    worker = RunWorker.__new__(RunWorker)
    worker.items = _FakeItems(rows)
    worker.runs = _FakeRuns()
    worker.overrides = _FakeOverrides()
    worker.settings = SimpleNamespace()
    return worker


def _run():
    return {"id": 900, "case_set_id": 4, "version_id": 7,
            "reconstruct_version": "recon-4"}


def test_finalize_adds_stable_dimension_and_aggregates():
    rows = [_row(1, 0, "call"), _row(1, 1, "call"), _row(1, 2, "fold")]
    worker = _worker(rows)
    worker._finalize(_run(), "recon-4", {}, total_cases=1, done_cases=1)

    assert worker.items.updated, "稳定维应写回 run_item"
    assert all(item["scores"]["stable"] == round(2 / 3, 4)
               for item in worker.items.updated)
    # (35 + 15 + 30 + 15 + 5 * 2/3) / 100 → 98.33
    assert worker.items.updated[0]["score"] == 98.33

    finished = worker.runs.finished[-1]
    assert finished["status"] == "succeeded"
    assert finished["score"] == 98.33
    assert finished["metrics"]["canceled"] is False


def test_finalize_single_repeat_has_no_stable_dimension():
    worker = _worker([_row(1)])
    worker._finalize(_run(), "recon-4", {}, total_cases=1, done_cases=1)
    assert worker.items.updated == []
    assert worker.runs.finished[-1]["status"] == "succeeded"


def test_same_rows_score_same_regardless_of_order():
    # AC②：同集同版本重跑，除模型采样外分数一致——评分聚合本身必须与行顺序无关。
    rows = [_row(1, 0, "call"), _row(1, 1, "call"), _row(1, 2, "fold"),
            _row(2, 0, "raise", priority=0), _row(3, 0, "fold")]
    first = _worker([dict(row) for row in rows])
    first._finalize(_run(), "recon-4", {}, total_cases=2, done_cases=2)
    second = _worker([dict(row) for row in reversed(rows)])
    second._finalize(_run(), "recon-4", {}, total_cases=2, done_cases=2)

    left, right = first.runs.finished[-1], second.runs.finished[-1]
    assert left["score"] == right["score"]
    assert left["metrics"] == right["metrics"]
    assert [item["score"] for item in first.items.updated] == \
        [item["score"] for item in second.items.updated]


def test_partial_stable_samples_not_counted():
    # 只跑到 2 次：稳定维不进分母（固定 3 次样本），否则两次 run 不可比。
    rows = [_row(1, 0, "call"), _row(1, 1, "call")]
    worker = _worker(rows)
    worker._finalize(_run(), "recon-4", {}, canceled=True,
                     total_cases=1, done_cases=1)
    assert worker.items.updated == []


def test_canceled_run_is_partial_not_succeeded():
    rows = [_row(1), _row(2, priority=1)]
    worker = _worker(rows)
    worker._finalize(_run(), "recon-4", {}, canceled=True,
                     total_cases=3, done_cases=2)
    finished = worker.runs.finished[-1]
    assert finished["status"] == "partial"
    assert finished["metrics"]["canceled"] is True
    assert "中断" in finished["error_msg"]
    assert "完成 2/3 个案例" in finished["error_msg"]


def test_all_errors_mark_run_failed():
    rows = [_row(1, source="error"), _row(2, source="error")]
    worker = _worker(rows)
    worker._finalize(_run(), "recon-4", {}, total_cases=2, done_cases=2)
    finished = worker.runs.finished[-1]
    assert finished["status"] == "failed"
    assert finished["error_msg"] == "全部调用失败"


def test_partial_errors_mark_run_partial():
    rows = [_row(1), _row(2, source="error")]
    worker = _worker(rows)
    worker._finalize(_run(), "recon-4", {}, total_cases=2, done_cases=2)
    finished = worker.runs.finished[-1]
    assert finished["status"] == "partial"
    assert "1 次调用失败" in finished["error_msg"]


def test_hard_fail_caps_score_at_40():
    rows = [_row(1, 0, hard_fail=scoring.HF_FALLBACK),
            _row(1, 1, hard_fail=scoring.HF_FALLBACK),
            _row(1, 2, hard_fail=scoring.HF_FALLBACK)]
    worker = _worker(rows)
    worker._finalize(_run(), "recon-4", {}, total_cases=1, done_cases=1)
    assert worker.items.updated[0]["score"] == 40.0
    assert worker.runs.finished[-1]["score"] == 40.0


class _FakeLiveAdvice:
    def __init__(self):
        self.prompts = {5: {("flop", 2): {"system": "sys", "user": "usr",
                                          "sha": "abc",
                                          "options": [{"id": "a1"}]}}}
        self.context = {5: {("flop", 2): {"mark": "x"}}}
        self.advices = {5: [{"street": "preflop", "seq": 0,
                             "action_index": 0, "option_id": "p1",
                             "option_label": "弃牌", "option_kind": "fold",
                             "reason": "太弱"}]}

    def baseline_prompts_for_hand(self, hand_id):
        return self.prompts.get(hand_id, {})

    def context_for_hand(self, hand_id):
        return self.context.get(hand_id, {})

    def list_for_hand(self, hand_id):
        return self.advices.get(hand_id, [])


def _decision():
    return SimpleNamespace(hand_id="5", street="flop", seq=0,
                           action_index=2, pot=12.0,
                           snap=SimpleNamespace(blinds=None))


def test_build_units_expands_p0_three_times():
    worker = RunWorker.__new__(RunWorker)
    worker.live_advice_repo = _FakeLiveAdvice()
    worker._decision_row = lambda *args: {"hand_id": 5}
    import replay.run_worker as module
    original = module.decision_from_row
    module.decision_from_row = lambda row: _decision()
    try:
        cases = [
            {"id": 1, "canonical_id": 100, "decision_key": "flop:2",
             "priority": 0, "bucket": {"street": "flop"}},
            {"id": 2, "canonical_id": 101, "decision_key": "flop:2",
             "priority": 1, "bucket": {"street": "flop"}},
        ]
        specs = {11: {"id": 11, "bucket": {"street": ["flop"]}}}
        units = worker._build_units(cases, specs, list(specs.values()),
                                    "recon-4", {1: 11, 2: 11})
    finally:
        module.decision_from_row = original

    repeats = {}
    for unit in units:
        repeats.setdefault(unit["case"]["id"], []).append(unit["repeat"])
    assert repeats == {1: [0, 1, 2], 2: [0]}
    assert all(unit["specId"] == 11 for unit in units)
    assert units[0]["prompt"] == ("sys", "usr")


def test_build_units_does_not_fall_back_when_frozen_spec_deleted():
    """冻结的 spec 被删了：报错，不偷偷换成本轮 active spec（跨 run 口径漂移）。"""
    worker = RunWorker.__new__(RunWorker)
    worker.live_advice_repo = _FakeLiveAdvice()
    worker._decision_row = lambda *args: {"hand_id": 5}
    import replay.run_worker as module
    original = module.decision_from_row
    module.decision_from_row = lambda row: _decision()
    try:
        cases = [{"id": 1, "canonical_id": 100, "decision_key": "flop:2",
                  "priority": 0, "bucket": {"street": "flop"}}]
        active = [{"id": 11, "bucket": {"street": ["flop"]}}]
        units = worker._build_units(cases, {}, active, "recon-4", {1: 11})
    finally:
        module.decision_from_row = original
    assert units and all(unit["spec"] is None for unit in units)
    assert all("冻结的 spec #11" in unit["specError"] for unit in units)


def test_build_units_falls_back_to_best_spec():
    worker = RunWorker.__new__(RunWorker)
    worker.live_advice_repo = _FakeLiveAdvice()
    worker._decision_row = lambda *args: {"hand_id": 5}
    import replay.run_worker as module
    original = module.decision_from_row
    module.decision_from_row = lambda row: _decision()
    try:
        cases = [{"id": 3, "canonical_id": 102, "decision_key": "flop:2",
                  "priority": 1, "bucket": {"street": "flop"}}]
        active = [{"id": 21, "bucket": {"street": ["flop"]}},
                  {"id": 22, "bucket": {"street": ["river"]}}]
        units = worker._build_units(cases, {}, active, "recon-4", {})
    finally:
        module.decision_from_row = original
    assert units[0]["specId"] == 21


def test_worker_builds_repositories_with_settings_db_only():
    """回归：CaseRepository / SpotSpecRepository 只接 db，别多传 user_id。"""
    from replay.config import Settings

    worker = RunWorker(Settings(worker_enabled=False), object())
    assert worker.runs is not None
    assert worker.items is not None
    assert worker.case_repo is not None
    assert worker.spec_repo is not None
    assert worker.version_repo is not None
    assert worker.limiter is not None


def test_finalize_coverage_counts_spec_buckets_with_array_values(monkeypatch):
    """回归：覆盖率按 spec 的桶划（每维是数组），别用更细的案例桶。"""
    from poker_ocr.buckets import bucket_label

    captured = {}
    real_aggregate = scoring.aggregate

    def spy(items, **kwargs):
        captured["items"] = [dict(item) for item in items]
        return real_aggregate(items, **kwargs)

    monkeypatch.setattr(scoring, "aggregate", spy)
    worker = _worker([
        {"case_id": 1, "spec_id": 1, "repeat_index": 0, "case_priority": 0,
         "score": 80.0, "scores_json": json.dumps({"action": 1.0}),
         "hard_fail": None, "source": "model", "street": "turn",
         "position_bucket": "ip", "hand_tier": "strong",
         "board_texture": "dry", "pot_type": "single_raised",
         "facing": "bet", "opponent_profile": "unknown"},
    ])
    specs = {
        1: {"id": 1,
            "bucket": {"street": ["flop"], "pot_type": ["single_raised"]}},
        2: {"id": 2, "bucket": {"street": ["river"]}},
    }
    worker._finalize(_run(), "recon-4", specs, total_cases=1, done_cases=1)
    finished = worker.runs.finished[-1]
    assert finished["metrics"]["coverage"]["bucketTotal"] == 2
    assert captured["items"][0]["bucket"] == bucket_label(
        {"street": ["flop"], "pot_type": ["single_raised"]})
    assert finished["status"] == "succeeded"

# ─────────────── 人工改判叠加（#58） ───────────────

def test_apply_override_recomputes_weighted_score():
    scores = {"action": 1.0, "size": 1.0, "reason": 1.0,
              "fact": 1.0, "stable": 1.0}
    score, merged, hard = run_worker.apply_override(
        scores, None, 100.0, {"scores": {"reason": 0.0}, "hardFail": None})
    assert merged["reason"] == 0.0
    assert merged["action"] == 1.0
    assert score == 70.0 and hard is None


def test_apply_override_can_add_hard_fail_and_is_capped():
    scores = {"action": 1.0, "size": 1.0, "reason": 1.0,
              "fact": 1.0, "stable": 1.0}
    score, _merged, hard = run_worker.apply_override(
        scores, None, 100.0,
        {"scores": {"fact": 0.0}, "hardFail": "HF_HALLUCINATION"})
    assert hard == "HF_HALLUCINATION"
    assert score <= scoring.HF_CAP


def test_apply_override_without_override_returns_machine_values():
    scores = {"action": 1.0}
    assert run_worker.apply_override(scores, None, 88.0, None) == (
        88.0, scores, None)


def test_aggregate_rows_applies_overrides_but_keeps_machine_rows():
    rows = [
        {"case_id": 1, "repeat_index": 0, "case_priority": 0, "spec_id": 1,
         "source": "model", "score": 100.0,
         "scores_json": json.dumps({"action": 1.0, "size": 1.0,
                                    "reason": 1.0, "fact": 1.0,
                                    "stable": 1.0}),
         "hard_fail": None},
        {"case_id": 2, "repeat_index": 0, "case_priority": 1, "spec_id": 1,
         "source": "model", "score": 40.0,
         "scores_json": json.dumps({"action": 0.0, "size": 0.0,
                                    "reason": 0.0, "fact": 0.0,
                                    "stable": 0.0}),
         "hard_fail": None},
    ]
    overrides = {1: {"scores": {"reason": 0.0}, "hardFail": None}}
    agg, agg_items, errors = run_worker.aggregate_rows(
        rows, spec_buckets={1: "flop|srp"}, overrides=overrides)
    assert errors == 0
    assert [item["score"] for item in agg_items] == [70.0, 40.0]
    assert agg["scored"] == 2 and agg["score"]["mean"] == 55.0
    # 机器行本身没被改写
    assert rows[0]["score"] == 100.0


def test_aggregate_rows_counts_errors_and_skips_repeats():
    rows = [
        {"case_id": 1, "repeat_index": 0, "case_priority": 0, "spec_id": 1,
         "source": "model", "score": 100.0,
         "scores_json": json.dumps({"action": 1.0}), "hard_fail": None},
        {"case_id": 1, "repeat_index": 1, "case_priority": 0, "spec_id": 1,
         "source": "error", "score": None,
         "scores_json": "{}", "hard_fail": "HF_ERROR"},
    ]
    agg, agg_items, errors = run_worker.aggregate_rows(
        rows, spec_buckets={1: "flop|srp"})
    assert errors == 1
    assert len(agg_items) == 1 and agg["scored"] == 1
