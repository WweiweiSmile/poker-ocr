"""评分运行（#57）：估算纯函数、冻结快照、拒绝条件、取消与逐条读取。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from replay import run_worker, scoring
from replay.errors import ApiError
from replay.service import ReplayService
from replay.versioning import (
    CURRENT_ADVICE_VERSION, CURRENT_RECONSTRUCT_VERSION,
)


# ─────────────── 纯函数：估算 ───────────────

def test_plan_counts_adds_two_extra_calls_per_p0():
    plan = run_worker.plan_counts(13, 6)
    assert plan == {"cases": 13, "p0": 6, "calls": 25, "extraPerP0": 2}


def test_plan_calls_counts_priority_zero_only():
    cases = [{"id": 1, "priority": 0}, {"id": 2, "priority": 1},
             {"id": 3, "priority": 2}]
    # 3 个案例各 1 次 + 唯一 P0 额外 2 次
    assert run_worker.plan_calls(cases)["calls"] == 5


def test_price_for_unknown_model_returns_none():
    assert run_worker.price_for("nope", {"m": {"in": 1, "out": 2}}) is None
    assert run_worker.price_for("m", None) is None
    assert run_worker.price_for("m", {"m": {"in": "x", "out": 2}}) is None


def test_estimate_cost_uses_price_table_and_none_without_price():
    tokens = {"tokensIn": 2000.0, "tokensOut": 300.0}
    priced = run_worker.estimate_cost(
        25, tokens, {"in": 1.0, "out": 2.0})
    assert priced["cost"] == pytest.approx(0.065)
    assert priced["currency"] == "CNY"
    assert priced["tokensIn"] == 50000
    assert priced["tokensOut"] == 7500
    unknown = run_worker.estimate_cost(25, tokens, None)
    assert unknown["cost"] is None
    assert unknown["calls"] == 25


# ─────────────── 服务层假仓储 ───────────────

def _case(case_id, priority=0, spec_id=None):
    return {
        "id": case_id, "case_set_id": 4, "priority": priority,
        "spec_id": spec_id, "canonical_id": 100 + case_id,
        "decision_key": "flop:2", "action_index": 2,
        "position_bucket": "IP", "hand_tier": "strong",
        "board_texture": "dry", "pot_type": "srp", "facing": "bet",
        "opponent_profile": "unknown",
    }


class _FakeCaseRepo:
    def __init__(self, cases, *, frozen=True):
        self.cases = cases
        self.frozen_at = "2026-11-17 10:00:00" if frozen else None

    def _row(self):
        return {
            "id": 4, "name": "seed-2026-11", "revision": 1,
            "purpose": "golden", "frozen_at": self.frozen_at,
            "created_at": "2026-11-17 09:00:00",
            "case_count": len(self.cases),
            "p0_count": sum(1 for c in self.cases
                            if int(c.get("priority") or 0) == 0),
        }

    def list_sets(self, *, limit=200, name=None):
        return [self._row()]

    def require_set(self, set_id):
        if int(set_id) != 4:
            raise ApiError("CASE_SET_NOT_FOUND", "不存在", 404)
        return self._row()

    def items(self, set_id):
        return list(self.cases)


class _FakeSpecRepo:
    def __init__(self, rows):
        self.rows = {int(row["id"]): row for row in rows}

    def get(self, spec_id):
        return self.rows.get(int(spec_id))

    def list_latest(self):
        return list(self.rows.values())


def _spec_row(spec_id, bucket=None):
    return {
        "id": spec_id, "name": f"spec-{spec_id}", "revision": 1,
        "status": "active", "bucket_json": json.dumps(bucket or {}),
    }


class _FakeVersionRepo:
    def __init__(self, row):
        self.row = row

    def get(self, version_id):
        return self.row if int(version_id) == int(self.row["id"]) else None

    def list(self, include_archived=False):
        return [self.row]


class _FakeRunRepo:
    def __init__(self):
        self.created = []
        self.rows = {}
        self._next = 900

    def avg_tokens(self, version_id):
        return None

    def create(self, **kwargs):
        run_id = self._next
        self._next += 1
        self.created.append((run_id, kwargs))
        row = {
            "id": run_id, "status": "queued",
            "case_set_id": kwargs["case_set_id"],
            "case_set_revision": kwargs["case_set_revision"],
            "spec_ids_json": json.dumps(kwargs["spec_ids"]),
            "version_id": kwargs["version_id"],
            "judge_version": kwargs["judge_version"],
            "reconstruct_version": kwargs["reconstruct_version"],
            "total_cases": kwargs["total_cases"], "done_cases": 0,
            "scored_cases": None, "score": None,
            "p0_pass_rate": None, "coverage_rate": None,
            "metrics_json": None,
            "config_snapshot": json.dumps(kwargs["config_snapshot"]),
            "cancel_requested": 0, "error_msg": "",
            "created_at": "2026-11-17 11:00:00", "started_at": None,
            "finished_at": None, "set_name": "seed-2026-11",
            "set_frozen_at": "2026-11-17 10:00:00",
            "version_name": "v-test", "version_model": "deepseek-flash",
        }
        self.rows[run_id] = row
        return run_id

    def get(self, run_id):
        return self.rows.get(int(run_id))

    def require(self, run_id):
        row = self.get(run_id)
        if row is None:
            raise ApiError("RUN_NOT_FOUND", "运行不存在", 404)
        return row

    def request_cancel(self, run_id):
        row = self.require(run_id)
        row["cancel_requested"] = 1
        return row

    def list(self, **kwargs):
        return list(self.rows.values())


class _FakeRunItemRepo:
    def __init__(self, rows=None):
        self.rows = rows or []

    def public_item(self, row):
        return {"caseId": row["case_id"],
                "repeatIndex": row["repeat_index"]}

    def list(self, run_id, reconstruct_version, **kwargs):
        return list(self.rows)

    def counts(self, run_id):
        return {"items": 0, "cases": 0, "fails": 0, "errors": 0}

    def get(self, run_id, case_id, repeat_index, reconstruct_version):
        for row in self.rows:
            if (row["case_id"], row["repeat_index"]) == (case_id,
                                                         repeat_index):
                return row
        return None


def _version_row(**overrides):
    row = {
        "id": 7, "name": "v-test", "model": "deepseek-flash",
        "archived": 0, "prompt_style": "lean",
        "advice_version": CURRENT_ADVICE_VERSION,
        "reconstruct_version": CURRENT_RECONSTRUCT_VERSION,
        "api_key_ref": "TEST_AI_KEY", "config_hash": "abc123",
        "context_mode": "full", "table_stats_mode": "on",
    }
    row.update(overrides)
    return row


def _service(cases, *, frozen=True, specs=None, version=None,
             monkeypatch=None):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")
    service = ReplayService.__new__(ReplayService)
    service.settings = SimpleNamespace(max_calls_per_job=2000,
                                       console_user_id=1)
    service.case_repo = _FakeCaseRepo(cases, frozen=frozen)
    service.spec_repo = _FakeSpecRepo(
        specs if specs is not None else [
            _spec_row(11, {"street": ["flop"]}),
            _spec_row(12, {"street": ["river"]})])
    service.version_repo = _FakeVersionRepo(version or _version_row())
    service.run_repo = _FakeRunRepo()
    service.run_item_repo = _FakeRunItemRepo()
    service.run_worker = None
    return service


# ─────────────── 估算 ───────────────

def test_estimate_reports_calls_tokens_and_missing_price(monkeypatch):
    cases = [_case(i, priority=0 if i <= 6 else 1) for i in range(1, 14)]
    service = _service(cases, monkeypatch=monkeypatch)
    out = service.estimate_score_run({"caseSetId": 4, "versionId": 7})
    assert out["caseSet"]["name"] == "seed-2026-11"
    assert out["cases"] == 13 and out["p0"] == 6
    assert out["calls"] == 25
    assert out["tokensSource"] == "default"
    assert out["tokens"] == {"in": 2000.0, "out": 300.0}
    assert out["cost"] is None and out["priceMissing"] is True
    assert out["runnable"] is True


def test_estimate_uses_price_table_env(monkeypatch):
    cases = [_case(1, priority=0)]
    service = _service(cases, monkeypatch=monkeypatch)
    monkeypatch.setenv(
        "REPLAY_PRICE_TABLE",
        json.dumps({"deepseek-flash": {"in": 1.0, "out": 2.0}}))
    out = service.estimate_score_run({"caseSetId": 4, "versionId": 7})
    assert out["price"] == {"in": 1.0, "out": 2.0}
    assert out["priceMissing"] is False
    assert out["cost"] == pytest.approx(3 * (2000 / 1e6 + 2 * 300 / 1e6))


# ─────────────── 创建 ───────────────

def test_create_requires_frozen_case_set(monkeypatch):
    service = _service([_case(1)], frozen=False, monkeypatch=monkeypatch)
    with pytest.raises(ApiError) as err:
        service.create_score_run({"caseSetId": 4, "versionId": 7})
    assert err.value.code == "CASE_SET_NOT_FROZEN"


def test_create_rejects_version_with_blockers(monkeypatch):
    service = _service([_case(1)], monkeypatch=monkeypatch,
                       version=_version_row(advice_version="advice-old"))
    with pytest.raises(ApiError) as err:
        service.create_score_run({"caseSetId": 4, "versionId": 7})
    assert err.value.code == "VERSION_NOT_RUNNABLE"


def test_create_reports_cases_without_spec(monkeypatch):
    cases = [_case(1), _case(2)]
    # 没有 active spec 能匹配 flop → 两个案例都缺 spec
    service = _service(cases, specs=[], monkeypatch=monkeypatch)
    with pytest.raises(ApiError) as err:
        service.create_score_run({"caseSetId": 4, "versionId": 7})
    assert err.value.code == "CASE_NO_SPEC"
    assert err.value.detail["caseIds"] == [1, 2]


def test_create_freezes_snapshot_and_plan(monkeypatch):
    cases = [_case(1, priority=0, spec_id=11), _case(2, priority=1)]
    service = _service(cases, monkeypatch=monkeypatch)
    out = service.create_score_run({"caseSetId": 4, "versionId": 7})
    run_id, kwargs = service.run_repo.created[0]
    snapshot = kwargs["config_snapshot"]
    assert kwargs["spec_ids"] == [11]
    assert kwargs["judge_version"] == scoring.JUDGE_VERSION
    assert kwargs["reconstruct_version"] == CURRENT_RECONSTRUCT_VERSION
    assert kwargs["total_cases"] == 2
    assert snapshot["caseSpecs"] == {"1": 11, "2": 11}
    assert snapshot["plan"] == {"cases": 2, "p0": 1, "calls": 4,
                                "extraPerP0": 2}
    assert snapshot["caseSet"]["frozenAt"]
    assert snapshot["version"]["configHash"] == "abc123"
    assert out["id"] == run_id and out["status"] == "queued"
    assert out["specIds"] == [11]


def test_create_tracks_p0_extra_repeats_in_calls(monkeypatch):
    cases = [_case(i, priority=0) for i in range(1, 4)]
    service = _service(cases, monkeypatch=monkeypatch)
    service.create_score_run({"caseSetId": 4, "versionId": 7})
    _run_id, kwargs = service.run_repo.created[0]
    assert kwargs["config_snapshot"]["plan"]["calls"] == 3 + 3 * 2


def test_create_rejects_runs_over_call_limit(monkeypatch):
    cases = [_case(i, priority=0) for i in range(1, 5)]
    service = _service(cases, monkeypatch=monkeypatch)
    service.settings.max_calls_per_job = 10
    with pytest.raises(ApiError) as err:
        service.create_score_run({"caseSetId": 4, "versionId": 7})
    assert err.value.code == "RUN_TOO_LARGE"


# ─────────────── 读 / 取消 ───────────────

def test_cancel_marks_request_and_keeps_status(monkeypatch):
    service = _service([_case(1)], monkeypatch=monkeypatch)
    created = service.create_score_run({"caseSetId": 4, "versionId": 7})
    out = service.cancel_score_run(created["id"])
    assert out["cancelRequested"] is True
    assert out["status"] == "queued"


def test_score_run_item_missing_raises_404(monkeypatch):
    service = _service([_case(1)], monkeypatch=monkeypatch)
    created = service.create_score_run({"caseSetId": 4, "versionId": 7})
    with pytest.raises(ApiError) as err:
        service.score_run_item(created["id"], 1, 0)
    assert err.value.code == "RUN_ITEM_NOT_FOUND"


def test_list_score_runs_returns_public_shape(monkeypatch):
    service = _service([_case(1)], monkeypatch=monkeypatch)
    service.create_score_run({"caseSetId": 4, "versionId": 7})
    out = service.list_score_runs({"limit": "10"})
    assert len(out["items"]) == 1
    assert out["items"][0]["totalCases"] == 1
    assert out["items"][0]["version"]["name"] == "v-test"
