"""评分运行（#57）：估算纯函数、冻结快照、拒绝条件、取消与逐条读取。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from replay import judge, run_worker, scoring
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

    def update_metrics(self, run_id, *, score=None, p0_pass_rate=None,
                       coverage_rate=None, scored_cases=None, metrics=None):
        row = self.require(run_id)
        row["score"] = score
        row["p0_pass_rate"] = p0_pass_rate
        row["coverage_rate"] = coverage_rate
        row["scored_cases"] = scored_cases
        if metrics is not None:
            row["metrics_json"] = json.dumps(metrics)
        return 1

    def list(self, **kwargs):
        return list(self.rows.values())


class _FakeOverrideRepo:
    def __init__(self):
        self.saved = {}
        self.history = []

    def get(self, run_id, case_id):
        return self.saved.get((int(run_id), int(case_id)))

    def list(self, run_id):
        return {case: row for (rid, case), row in self.saved.items()
                if rid == int(run_id)}

    def log(self, run_id, case_id, limit=50):
        return [row for row in self.history
                if (row["runId"], row["caseId"]) == (int(run_id), int(case_id))]

    def save(self, *, run_id, case_id, actor, note, override, before):
        row = {"runId": int(run_id), "caseId": int(case_id), "actor": actor,
               "note": note, "scores": override.get("scores") or {},
               "hardFail": override.get("hardFail"), "machine": before or {}}
        self.saved[(int(run_id), int(case_id))] = row
        self.history.insert(0, {**row, "id": len(self.history) + 1,
                                "before": before or {}, "after": override})
        return row

    def clear(self, *, run_id, case_id, actor, note, before):
        self.saved.pop((int(run_id), int(case_id)), None)
        self.history.insert(0, {"runId": int(run_id), "caseId": int(case_id),
                                "id": len(self.history) + 1, "actor": actor,
                                "note": note, "before": before or {},
                                "after": None})


class _FakeRunItemRepo:
    def __init__(self, rows=None):
        self.rows = rows or []

    def public_item(self, row):
        return {"caseId": row["case_id"],
                "repeatIndex": row["repeat_index"],
                "score": row.get("score"),
                "scores": json.loads(row.get("scores_json") or "{}"),
                "hardFail": row.get("hard_fail")}

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
             monkeypatch=None, price_table=None):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-judge")
    service = ReplayService.__new__(ReplayService)
    service.settings = SimpleNamespace(
        max_calls_per_job=2000, console_user_id=1,
        price_table=price_table or {}, judge_model="judge-pro",
        judge_base_url="https://judge.example/v1",
        judge_api_key_ref="TEST_JUDGE_KEY", judge_timeout=30.0,
        judge_thinking="disabled")
    service.case_repo = _FakeCaseRepo(cases, frozen=frozen)
    service.spec_repo = _FakeSpecRepo(
        specs if specs is not None else [
            _spec_row(11, {"street": ["flop"]}),
            _spec_row(12, {"street": ["river"]})])
    service.version_repo = _FakeVersionRepo(version or _version_row())
    service.run_repo = _FakeRunRepo()
    service.run_item_repo = _FakeRunItemRepo()
    service.override_repo = _FakeOverrideRepo()
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


def test_estimate_uses_configured_price_table(monkeypatch):
    cases = [_case(1, priority=0)]
    service = _service(cases, monkeypatch=monkeypatch,
                       price_table={"deepseek-flash": {"in": 1.0, "out": 2.0}})
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


def test_create_freezes_spec_freq_into_snapshot(monkeypatch):
    """频次目标要冻进快照：热点报表的口径必须可复现（设计 §6/§8）。"""
    cases = [_case(1)]
    specs = [{**_spec_row(11, {"street": ["flop"]}),
              "spec_json": json.dumps({
                  "schema": "spot_spec.v1",
                  "freq": {"action": "raise", "min": 0.3, "max": 0.5}})}]
    service = _service(cases, specs=specs, monkeypatch=monkeypatch)
    service.create_score_run({"caseSetId": 4, "versionId": 7})
    _, kwargs = service.run_repo.created[0]
    assert kwargs["config_snapshot"]["specs"][0]["freq"] == {
        "action": "raise", "min": 0.3, "max": 0.5}


def test_create_freezes_snapshot_and_plan(monkeypatch):
    cases = [_case(1, priority=0, spec_id=11), _case(2, priority=1)]
    service = _service(cases, monkeypatch=monkeypatch)
    out = service.create_score_run({"caseSetId": 4, "versionId": 7})
    run_id, kwargs = service.run_repo.created[0]
    snapshot = kwargs["config_snapshot"]
    assert kwargs["spec_ids"] == [11]
    # 判定型号写进 judge_version（设计 §13-2），并随 run 冻结。
    assert kwargs["judge_version"] == judge.judge_version("judge-pro")
    assert snapshot["judge"]["model"] == "judge-pro"
    assert snapshot["judge"]["temperature"] == 0.0
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


def test_create_rejects_runs_over_call_limit_without_confirm(monkeypatch):
    cases = [_case(i, priority=0) for i in range(1, 5)]
    service = _service(cases, monkeypatch=monkeypatch)
    service.settings.max_calls_per_job = 10
    with pytest.raises(ApiError) as err:
        service.create_score_run({"caseSetId": 4, "versionId": 7})
    assert err.value.code == "RUN_TOO_LARGE"
    assert err.value.status_code == 409
    assert err.value.detail["maxCalls"] == 10


def test_create_allows_over_limit_when_confirmed(monkeypatch):
    cases = [_case(i, priority=0) for i in range(1, 5)]
    service = _service(cases, monkeypatch=monkeypatch)
    service.settings.max_calls_per_job = 10
    created = service.create_score_run(
        {"caseSetId": 4, "versionId": 7, "confirmLarge": True})
    assert created["id"]
    _run_id, kwargs = service.run_repo.created[0]
    assert kwargs["config_snapshot"]["plan"]["calls"] == 12


def test_estimate_reports_call_limit(monkeypatch):
    cases = [_case(i, priority=0) for i in range(1, 5)]
    service = _service(cases, monkeypatch=monkeypatch)
    service.settings.max_calls_per_job = 10
    est = service.estimate_score_run({"caseSetId": 4, "versionId": 7})
    assert est["calls"] == 12
    assert est["maxCalls"] == 10
    assert est["overLimit"] is True


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


def test_bad_query_numbers_fall_back_instead_of_500(monkeypatch):
    """回归：?limit=abc / ?caseSetId=oops 不能把页面打到 500。"""
    service = _service([_case(1)], monkeypatch=monkeypatch)
    created = service.create_score_run({"caseSetId": 4, "versionId": 7})
    out = service.list_score_runs({"limit": "abc", "caseSetId": "oops"})
    assert len(out["items"]) == 1
    items = service.score_run_items(created["id"], {"limit": "x", "offset": "y"})
    assert items["limit"] == 200 and items["offset"] == 0


def test_empty_case_set_is_not_runnable(monkeypatch):
    service = _service([], monkeypatch=monkeypatch)
    out = service.estimate_score_run({"caseSetId": 4, "versionId": 7})
    assert out["runnable"] is False
    assert out["calls"] == 0


def test_settings_reject_bad_price_table(monkeypatch):
    """新配置项 fail fast：配错就不启动，别静默当「没配」。"""
    from replay.config import Settings

    monkeypatch.setenv("REPLAY_PRICE_TABLE", "{not json")
    with pytest.raises(ValueError):
        Settings.from_env(load=False)
    monkeypatch.setenv("REPLAY_PRICE_TABLE",
                       json.dumps({"m": {"in": 1.0}}))
    with pytest.raises(ValueError):
        Settings.from_env(load=False)
    monkeypatch.setenv("REPLAY_PRICE_TABLE",
                       json.dumps({"m": {"in": 1.0, "out": 2.0}}))
    assert Settings.from_env(load=False).price_table == {
        "m": {"in": 1.0, "out": 2.0}}

# ─────────────── judge / 人工改判（#58） ───────────────

MACHINE_SCORES = {"action": 1.0, "size": 1.0, "reason": 0.5,
                  "fact": 1.0, "stable": 1.0}


def _finished_service(monkeypatch, *, status="succeeded", model="deepseek-flash"):
    version = _version_row(model=model)
    service = _service([_case(1, priority=0, spec_id=11)],
                       version=version, monkeypatch=monkeypatch)
    run_id = service.create_score_run({"caseSetId": 4, "versionId": 7})["id"]
    run = service.run_repo.rows[run_id]
    run["status"] = status
    run["metrics_json"] = json.dumps({
        "cases": 1, "scored": 1, "score": {"mean": 90.0},
        "p0": {"total": 1, "passed": 1, "rate": 1.0},
        "coverage": {"bucketTotal": 1, "reached": 1, "rate": 1.0},
    })
    rows = [{
        "run_id": run_id, "case_id": 1, "repeat_index": 0,
        "spec_id": 11, "case_priority": 0, "source": "model",
        "score": 90.0, "scores_json": json.dumps(MACHINE_SCORES),
        "hard_fail": None, "judge_json": json.dumps({
            "mode": "llm", "status": "ok", "temperature": 0.0,
            "requiredPoints": [], "forbiddenPoints": [], "citedFacts": [],
            "hallucinated": False}),
        "option_id": "o1", "kind": "call", "amount": None,
        "reason": "我在按钮位，跟注", "facts_json": json.dumps({
            "options": [{"id": "o1", "kind": "call"},
                        {"id": "o2", "kind": "raise"}]}),
        "error_msg": "",
    }]
    service.run_item_repo.rows = rows
    return service, run_id, rows


def test_meta_exposes_judge_model_and_blockers(monkeypatch):
    service = _service([_case(1)], monkeypatch=monkeypatch)
    out = service.score_run_meta()
    assert out["judge"]["model"] == "judge-pro"
    assert out["judge"]["temperature"] == 0.0
    assert out["judgeVersion"] == judge.judge_version("judge-pro")
    assert all(v["judgeRunnable"] for v in out["versions"])


def test_create_rejects_when_judge_model_equals_model_under_test(monkeypatch):
    service = _service([_case(1)], version=_version_row(model="judge-pro"),
                       monkeypatch=monkeypatch)
    with pytest.raises(ApiError) as err:
        service.create_score_run({"caseSetId": 4, "versionId": 7})
    assert err.value.code == "JUDGE_NOT_READY"
    assert err.value.detail[0]["code"] == "JUDGE_SAME_MODEL"


def test_override_changes_run_score_but_not_item_score(monkeypatch):
    service, run_id, rows = _finished_service(monkeypatch)
    out = service.save_score_run_override(
        run_id, 1, {"scores": {"reason": 0.0}, "actor": "weiwei",
                    "note": "理由其实没提对手范围"})
    assert out["score"] == 90.0            # 机器原值不变
    assert out["effective"]["score"] == 70.0
    assert out["effective"]["overridden"] is True
    assert out["override"]["actor"] == "weiwei"
    assert out["overrideHistory"][0]["note"] == "理由其实没提对手范围"
    assert rows[0]["score"] == 90.0
    run = service.run_repo.rows[run_id]
    assert run["score"] == 70.0            # 聚合分随改判变化
    assert run["metrics_json"] and json.loads(
        run["metrics_json"])["overrideCount"] == 1


def test_override_rejects_unknown_dimension_and_bad_value(monkeypatch):
    service, run_id, _rows = _finished_service(monkeypatch)
    with pytest.raises(ApiError) as err:
        service.save_score_run_override(run_id, 1, {"scores": {"nope": 1}})
    assert err.value.code == "INVALID_OVERRIDE"
    with pytest.raises(ApiError):
        service.save_score_run_override(run_id, 1, {"scores": {"reason": 2}})
    with pytest.raises(ApiError):
        service.save_score_run_override(run_id, 1,
                                        {"hardFail": "HF_UNKNOWN"})


def test_override_rejected_while_run_is_running(monkeypatch):
    service, run_id, _rows = _finished_service(monkeypatch, status="running")
    with pytest.raises(ApiError) as err:
        service.save_score_run_override(run_id, 1, {"scores": {"reason": 0}})
    assert err.value.code == "RUN_NOT_FINISHED"


def test_delete_override_restores_machine_score(monkeypatch):
    service, run_id, _rows = _finished_service(monkeypatch)
    service.save_score_run_override(run_id, 1, {"scores": {"reason": 0.0}})
    out = service.delete_score_run_override(run_id, 1, {"note": "撤回"})
    assert out["override"] is None
    assert out["effective"]["overridden"] is False
    assert out["effective"]["score"] == 90.0
    assert service.run_repo.rows[run_id]["score"] == 90.0
    assert out["overrideHistory"][0]["after"] is None


def test_item_exposes_effective_and_history(monkeypatch):
    service, run_id, _rows = _finished_service(monkeypatch)
    service.save_score_run_override(run_id, 1, {"scores": {"fact": 0.0}})
    out = service.score_run_item(run_id, 1, 0)
    assert out["judge"]["temperature"] == 0.0
    assert out["effective"]["scores"]["fact"] == 0.0
    assert out["effective"]["scores"]["reason"] == 0.5
    assert out["override"]["scores"]["fact"] == 0.0
    assert out["overrideHistory"]


def test_rejudge_compares_same_input_without_writing(monkeypatch):
    service, run_id, rows = _finished_service(monkeypatch)
    service.spec_repo.rows[11]["spec_json"] = json.dumps({
        "required_points": [{"key": "position", "text": "按钮位", "weight": 1}],
        "forbidden_points": []})
    rows[0]["judge_json"] = json.dumps({
        "mode": "llm", "status": "ok", "temperature": 0.0,
        "requiredPoints": [{"key": "position", "text": "按钮位", "weight": 1,
                            "status": "hit", "quote": "我在按钮位",
                            "quoteFound": True}],
        "forbiddenPoints": [], "citedFacts": [], "hallucinated": False})
    import poker_ocr.advice as advice_mod
    monkeypatch.setattr(advice_mod, "complete_json", lambda *a, **k: {
        "requiredPoints": [{"key": "position", "status": "hit",
                            "quote": "我在按钮位"}],
        "forbiddenPoints": [], "citedFacts": [], "hallucinated": False})
    out = service.rejudge_score_run_item(run_id, 1, 0)
    assert out["consistent"] is True
    assert out["after"]["temperature"] == 0.0
    assert out["judgeVersion"] == judge.judge_version("judge-pro")
    assert out["inputDigest"] == out["after"]["inputDigest"]
    # 重判只读：原始行与聚合分都不动。
    assert rows[0]["score"] == 90.0
    assert service.run_repo.rows[run_id]["score"] is None
