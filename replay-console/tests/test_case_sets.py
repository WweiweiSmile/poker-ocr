"""案例集服务层（#55）：冻结、批量、统计、整手全建、seed 幂等。"""
from __future__ import annotations

import json

import pytest

from replay import case_seed
from replay.errors import ApiError
from replay.service import ReplayService


def _set_row(row):
    return {
        "id": row["id"], "name": row["name"], "revision": row["revision"],
        "purpose": row["purpose"], "frozen_at": row["frozen_at"],
        "created_at": None,
        "case_count": sum(1 for c in row.get("cases", [])),
        "p0_count": sum(1 for c in row.get("cases", [])
                        if int(c.get("priority") or 0) == 0),
    }


class _FakeCaseRepo:
    CASE_SET_PURPOSES = ("golden", "regression", "exploration", "coverage")

    def __init__(self):
        self.sets = {}
        self.cases = []
        self.created = []
        self.bucket_rows = []
        self._next_set = 1
        self._next_case = 1

    # ── 案例集 ──
    def _public(self, row):
        out = _set_row({**row, "cases": [
            c for c in self.cases if c["case_set_id"] == row["id"]]})
        return out

    def create_set(self, *, name, purpose="exploration"):
        set_id = self._next_set
        self._next_set += 1
        self.sets[set_id] = {"id": set_id, "name": name, "revision": 1,
                             "purpose": purpose, "frozen_at": None}
        return self._public(self.sets[set_id])

    def set_by_name_revision(self, name, revision=1):
        for row in self.sets.values():
            if row["name"] == name and row["revision"] == revision:
                return self._public(row)
        return None

    def list_sets(self, *, limit=200, name=None):
        rows = [self._public(r) for r in self.sets.values()
                if name is None or r["name"] == name]
        return rows[:limit]

    def require_set(self, set_id):
        row = self.sets.get(int(set_id))
        if row is None:
            raise ApiError("CASE_SET_NOT_FOUND", f"案例集 {set_id} 不存在", 404)
        return self._public(row)

    def _frozen_error(self, row):
        return ApiError(
            "CASE_SET_FROZEN",
            f"案例集 {row.get('name')} v{row.get('revision')} 已冻结，"
            "不能再改；请从它建新版本（POST /api/case-sets/{id}/revisions）",
            409)

    def require_unfrozen(self, set_id):
        row = self.require_set(set_id)
        if row.get("frozen_at"):
            raise self._frozen_error(row)
        return row

    def update_set(self, set_id, *, name=None, purpose=None):
        self.require_unfrozen(set_id)
        row = self.sets[int(set_id)]
        if name is not None:
            row["name"] = name
        if purpose is not None:
            row["purpose"] = purpose
        return self._public(row)

    def freeze_set(self, set_id):
        self.require_unfrozen(set_id)
        self.sets[int(set_id)]["frozen_at"] = "2026-11-17T00:00:00"
        return self.require_set(set_id)

    def copy_set_to_new_revision(self, set_id):
        row = self.require_set(set_id)
        revision = max(r["revision"] for r in self.sets.values()
                       if r["name"] == row["name"]) + 1
        set_id_new = self._next_set
        self._next_set += 1
        self.sets[set_id_new] = {"id": set_id_new, "name": row["name"],
                                 "revision": revision,
                                 "purpose": row["purpose"],
                                 "frozen_at": None}
        for case in [c for c in self.cases
                     if c["case_set_id"] == int(set_id)]:
            self.cases.append({**case, "id": self._next_case,
                               "case_set_id": set_id_new,
                               "set_revision": revision})
            self._next_case += 1
        return self.require_set(set_id_new)

    # ── 案例 ──
    def items(self, case_set_id):
        return [dict(c) for c in self.cases
                if c["case_set_id"] == int(case_set_id)]

    def create(self, *, canonical_id, decision_key, spec_id,
               case_set_id=None, priority=1, overrides=None, note=None):
        row = self.sets.get(int(case_set_id))
        if row is None:
            raise ApiError("CASE_SET_NOT_FOUND", "案例集不存在", 404)
        if row.get("frozen_at"):
            raise self._frozen_error(row)
        for case in self.cases:
            if (case["case_set_id"] == int(case_set_id)
                    and case["canonical_id"] == int(canonical_id)
                    and case["decision_key"] == decision_key):
                return case, False
        self.created.append({"canonical_id": canonical_id,
                             "decision_key": decision_key,
                             "spec_id": spec_id, "case_set_id": case_set_id,
                             "priority": priority, "note": note})
        case = {"id": self._next_case, "case_set_id": int(case_set_id),
                "canonical_id": int(canonical_id),
                "decision_key": decision_key, "spec_id": int(spec_id),
                "priority": int(priority), "status": "draft",
                "note": note or "", "overrides_json": None,
                "created_at": None, "set_name": row["name"],
                "set_revision": row["revision"], "set_frozen_at": None,
                "action_index": int(decision_key.split(":")[1])}
        self._next_case += 1
        self.cases.append(case)
        return case, True

    def update_case(self, set_id, case_id, **kwargs):
        self.require_unfrozen(set_id)
        for case in self.cases:
            if case["id"] == int(case_id):
                for key, value in kwargs.items():
                    if value is not None:
                        case[key] = value
                return case
        raise ApiError("CASE_NOT_FOUND", f"案例 {case_id} 不存在", 404)

    def delete_case(self, set_id, case_id):
        self.require_unfrozen(set_id)
        self.cases = [c for c in self.cases if c["id"] != int(case_id)]

    def decisions_for_bucket(self, bucket, *, limit=50, exclude=None):
        return list(self.bucket_rows)


class _FakeSpecRepo:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.created = []
        self._next = 100

    def get(self, spec_id):
        for row in self.rows:
            if int(row["id"]) == int(spec_id):
                return row
        return None

    def list_latest(self):
        return [row for row in self.rows if row.get("status") == "active"]

    def latest(self, name):
        rows = [row for row in self.rows if row["name"] == name]
        return max(rows, key=lambda r: r["revision"]) if rows else None

    def create(self, *, name, revision, status, content, note, supersedes):
        spec_id = self._next
        self._next += 1
        spec = {key: value for key, value in content.items()
                if key != "bucket"}
        row = {"id": spec_id, "name": name, "revision": revision,
               "status": status, "bucket_json": json.dumps(content["bucket"]),
               "spec_json": json.dumps(spec), "note": note}
        self.rows.append(row)
        self.created.append(name)
        return spec_id

    def activate(self, spec_id, name):
        for row in self.rows:
            if row["name"] == name and row["id"] != spec_id:
                row["status"] = "draft"
            if row["id"] == spec_id:
                row["status"] = "active"


class _Imports:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def find_any_by_uid(self, uid):
        return self.rows.get(uid)


class _FakeHandRepo:
    def __init__(self, rows=None):
        self.imports = _Imports(rows)


class _FakeCanonicalRepo:
    def __init__(self, rows=None):
        self.rows = {int(row["id"]): row for row in (rows or [{"id": 1}])}

    def get(self, canonical_id):
        return self.rows.get(int(canonical_id))

    def sources(self, canonical_id):
        return []

    def source_of(self, source, source_hand_id):
        # 测试里 source_hand_id 就等于 canonical_id
        return {"canonical_id": int(source_hand_id), "source": source,
                "source_hand_id": source_hand_id}


class _FakeDecisionRepo:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def for_canonical(self, canonical_id):
        return [row for row in self.rows
                if row.get("canonical_id") in (None, int(canonical_id))]


def _service(*, decisions=None, specs=None, case_repo=None, hand_rows=None,
             canonical_ids=None):
    service = object.__new__(ReplayService)
    ids = canonical_ids or {int(row.get("canonical_id") or 1)
                            for row in (decisions or [])} or {1}
    service.canonical_repo = _FakeCanonicalRepo([{"id": i} for i in ids])
    service.decision_repo = _FakeDecisionRepo(decisions)
    service.spec_repo = _FakeSpecRepo(specs)
    service.case_repo = case_repo or _FakeCaseRepo()
    service.hand_repo = _FakeHandRepo(hand_rows)
    return service


def _spec(spec_id, bucket, name="s"):
    return {"id": spec_id, "name": name, "revision": 1, "status": "active",
            "bucket_json": json.dumps(bucket), "spec_json": "{}"}


# ─────────────────────────────── 冻结 ───────────────────────────────

def test_frozen_set_rejects_every_write_with_new_version_hint():
    service = _service()
    created = service.create_case_set({"name": "P0 回归", "purpose": "golden"})
    set_id = created["id"]
    service.freeze_case_set(set_id)
    assert service.get_case_set(set_id)["frozen"] is True

    with pytest.raises(ApiError) as add:
        service.add_case_set_cases(set_id, {"cases": [{
            "canonicalId": 1, "decisionKey": "flop:2", "specId": 7}]})
    assert add.value.code == "CASE_SET_FROZEN"
    assert "建新版本" in add.value.message

    with pytest.raises(ApiError) as patch:
        service.patch_case_set(set_id, {"name": "改名"})
    assert patch.value.code == "CASE_SET_FROZEN"

    with pytest.raises(ApiError) as freeze_again:
        service.freeze_case_set(set_id)
    assert freeze_again.value.code == "CASE_SET_FROZEN"


def test_create_revision_copies_cases_and_stays_editable():
    service = _service(decisions=[{"street": "flop", "seq": 2}],
                       specs=[{"id": 7, "name": "顶对", "revision": 1}])
    set_id = service.create_case_set({"name": "P0"})["id"]
    service.add_case_set_cases(set_id, {"cases": [{
        "canonicalId": 1, "decisionKey": "flop:2", "specId": 7,
        "priority": 0}]})
    service.freeze_case_set(set_id)

    revision = service.create_case_set_revision(set_id, {"name": "P0 v2"})
    assert revision["revision"] == 2
    assert revision["frozen"] is False
    assert revision["name"] == "P0 v2"
    assert revision["caseCount"] == 1
    # 新版本可以继续加案例
    service.add_case_set_cases(revision["id"], {"cases": [{
        "canonicalId": 1, "decisionKey": "flop:2", "specId": 7}]})
    # 原版本仍是冻结的
    assert service.get_case_set(set_id)["frozen"] is True


# ─────────────────────────────── 批量 / 统计 ───────────────────────────────

def test_add_cases_reports_partial_skip_without_rollback():
    service = _service(decisions=[{"street": "flop", "seq": 2},
                                  {"street": "turn", "seq": 4}],
                       specs=[{"id": 7, "name": "顶对", "revision": 1}])
    set_id = service.create_case_set({"name": "批量"})["id"]
    result = service.add_case_set_cases(set_id, {"cases": [
        {"canonicalId": 1, "decisionKey": "flop:2", "specId": 7},
        {"canonicalId": 1, "decisionKey": "turn:4", "specId": 7},
        {"canonicalId": 1, "decisionKey": "turn:4", "specId": 999},
        {"canonicalId": 1, "decisionKey": "river:9", "specId": 7},
    ]})
    assert (result["created"], result["existing"]) == (2, 0)
    codes = [item["code"] for item in result["skipped"]]
    assert codes == ["SPEC_NOT_FOUND", "DECISION_NOT_FOUND"]
    assert result["caseCount"] == 2


def test_case_set_detail_reports_p0_ratio_and_bucket_spread():
    repo = _FakeCaseRepo()
    service = _service(specs=[{"id": 7, "name": "顶对", "revision": 1}],
                       decisions=[{"street": "flop", "seq": 2},
                                  {"street": "river", "seq": 15}])
    service.case_repo = repo
    set_id = service.create_case_set({"name": "统计"})["id"]
    service.add_case_set_cases(set_id, {"cases": [
        {"canonicalId": 1, "decisionKey": "flop:2", "specId": 7,
         "priority": 0},
        {"canonicalId": 1, "decisionKey": "river:15", "specId": 7,
         "priority": 1}]})
    for case in repo.cases:
        if case["decision_key"] == "flop:2":
            case.update({"hand_tier": "TPTK", "position_bucket": "LP"})
    detail = service.get_case_set(set_id)
    assert detail["caseCount"] == 2
    assert detail["p0Count"] == 1
    assert detail["p0Ratio"] == 0.5
    assert detail["stats"]["byStreet"] == [
        {"street": "flop", "count": 1}, {"street": "river", "count": 1}]
    labels = [row["label"] for row in detail["stats"]["byBucket"]]
    assert any("顶对强踢" in label for label in labels)


# ─────────────────────────────── 整手全建 / 补样 ───────────────────────────────

def test_build_hand_cases_picks_most_specific_spec():
    decisions = [
        {"street": "flop", "seq": 2, "hand_tier": "TPTK",
         "pot_type": "single"},
        {"street": "turn", "seq": 4, "hand_tier": "AIR"},
    ]
    service = _service(decisions=decisions, specs=[
        _spec(10, {}, "全局"),
        _spec(11, {"street": ["flop"], "hand_tier": ["TPTK"]}, "顶对强踢"),
    ])
    set_id = service.create_case_set({"name": "整手"})["id"]
    result = service.build_hand_cases(set_id, {"canonicalId": 1})
    assert result["created"] == 2
    by_key = {case["decision_key"]: case["spec_id"]
              for case in service.case_repo.cases}
    assert by_key["flop:2"] == 11  # 更具体的 spec 胜出
    assert by_key["turn:4"] == 10


def test_build_hand_cases_requires_decisions():
    service = _service(decisions=[])
    set_id = service.create_case_set({"name": "空"})["id"]
    with pytest.raises(ApiError) as err:
        service.build_hand_cases(set_id, {"canonicalId": 1})
    assert err.value.code == "DECISION_NOT_FOUND"


def test_sample_cases_fills_from_bucket_pool():
    repo = _FakeCaseRepo()
    repo.bucket_rows = [
        {"canonical_id": 1, "street": "flop", "seq": 7,
         "action_index": 7, "hand_tier": "TPTK"},
    ]
    service = _service(specs=[_spec(10, {"street": ["flop"]})],
                       case_repo=repo)
    set_id = service.create_case_set({"name": "补样"})["id"]
    result = service.sample_case_set_cases(
        set_id, {"bucket": {"street": ["flop"]}, "count": 3})
    assert result["created"] == 1
    assert repo.cases[0]["note"] == "按桶补样"


# ─────────────────────────────── seed ───────────────────────────────

def _seed_service(*, specs=None):
    plan = case_seed.plan_seed(case_seed.load_ab_set())
    by_uid = {}
    decisions = []
    for case in plan["cases"]:
        canonical_id = by_uid.setdefault(case["handUid"], len(by_uid) + 1)
        decisions.append({"canonical_id": canonical_id,
                          "street": case["street"],
                          "seq": case["actionIndex"],
                          "action_index": case["actionIndex"]})
    hand_rows = {uid: {"id": cid, "source": "hhm"}
                 for uid, cid in by_uid.items()}
    return _service(decisions=decisions, specs=specs, hand_rows=hand_rows,
                    canonical_ids=range(1, len(by_uid) + 1))


def test_seed_dry_run_writes_nothing():
    service = _seed_service()
    report = service.seed_case_sets({"dryRun": True})
    assert report["dryRun"] is True
    assert report["set"]["wouldCreate"] is True
    assert len(report["specs"]["wouldCreate"]) == 22
    assert len(report["cases"]["wouldCreate"]) == 13
    assert service.case_repo.created == []
    assert service.spec_repo.created == []


def test_seed_is_idempotent_on_second_run():
    service = _seed_service()
    first = service.seed_case_sets({})
    assert len(first["specs"]["created"]) == 22
    assert len(first["cases"]["created"]) == 13
    assert len(first["cases"]["skippedPlan"]) == 9

    second = service.seed_case_sets({})
    assert second["specs"]["created"] == []
    assert len(second["specs"]["reused"]) == 22
    assert second["cases"]["created"] == []
    assert len(second["cases"]["existing"]) == 13
    assert service.case_repo.sets[first["set"]["id"]]["frozen_at"] is None


def test_seed_never_demotes_a_human_spec():
    """同名已有内容不同的期望：只追加 draft，不动人工置的 active。"""
    plan = case_seed.plan_seed(case_seed.load_ab_set())
    name = plan["specs"][0]["name"]
    existing = {"id": 1, "name": name, "revision": 1, "status": "active",
                "bucket_json": json.dumps({}),
                "spec_json": json.dumps({"schema": "spot_spec.v1"}),
                "note": "人工写的"}
    service = _seed_service(specs=[existing])
    report = service.seed_case_sets({})

    assert report["specs"]["diverged"] == [name]
    assert service.spec_repo.get(1)["status"] == "active"
    latest = service.spec_repo.latest(name)
    assert latest["revision"] == 2 and latest["status"] == "draft"
def test_seed_reports_frozen_set_as_skipped_cases():
    service = _seed_service()
    first = service.seed_case_sets({})
    service.freeze_case_set(first["set"]["id"])
    again = service.seed_case_sets({})
    assert again["cases"]["created"] == []
    assert all("建新版本" in item["reason"] for item in again["cases"]["skipped"])
