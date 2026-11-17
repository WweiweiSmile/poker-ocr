"""CaseRepository 的案例集分支：冻结、重名、复制新版本、按桶抽点。

用按 SQL 形状分派的替身库：冻结/重名这类错误路径要在不进 MySQL 的情况下
逐字校验错误码与文案（前端按 code 决定提示与按钮）。
"""
from __future__ import annotations

import pytest
from pymysql.err import IntegrityError

from replay.errors import ApiError
from replay.repository import CaseRepository


class FakeDB:
    def __init__(self, sets=(), cases=()):
        self.sets = {int(row["id"]): dict(row) for row in sets}
        self.cases = [dict(row) for row in cases]
        self.next_id = max(self.sets) + 1 if self.sets else 1
        self.calls: list[tuple[str, str, tuple | None]] = []
        self.execute_error: Exception | None = None
        self.decisions: list[dict] = []

    def _set_row(self, row):
        out = dict(row)
        out["case_count"] = sum(1 for case in self.cases
                                if case["case_set_id"] == row["id"])
        out["p0_count"] = sum(1 for case in self.cases
                              if case["case_set_id"] == row["id"]
                              and int(case.get("priority") or 0) == 0)
        return out

    def one(self, sql, params=None):
        text = " ".join(sql.split())
        self.calls.append(("one", text, params))
        if "FROM replay_case_sets s WHERE s.id = %s" in text:
            row = self.sets.get(int(params[0]))
            return self._set_row(row) if row else None
        if "FROM replay_case_sets s WHERE s.name = %s AND s.revision = %s" in text:
            for row in self.sets.values():
                if row["name"] == params[0] and row["revision"] == int(params[1]):
                    return self._set_row(row)
            return None
        if "SELECT * FROM replay_case_sets WHERE id=%s" in text:
            return self.sets.get(int(params[0]))
        if "FROM replay_cases c" in text:
            for case in self.cases:
                if case["id"] == int(params[0]):
                    row = self.sets[case["case_set_id"]]
                    return {**case, "set_name": row["name"],
                            "set_revision": row["revision"],
                            "set_frozen_at": row["frozen_at"]}
            return None
        raise AssertionError(f"unexpected one(): {text}")

    def query(self, sql, params=None):
        text = " ".join(sql.split())
        self.calls.append(("query", text, params))
        if "FROM replay_case_sets s" in text:
            rows = [self._set_row(row) for row in self.sets.values()]
            if "WHERE s.name = %s" in text:
                rows = [row for row in rows if row["name"] == params[0]]
            return rows
        if "FROM replay_decisions d" in text:
            return list(self.decisions)
        if "FROM replay_cases c" in text:
            return [case for case in self.cases
                    if case["case_set_id"] == int(params[-1])]
        raise AssertionError(f"unexpected query(): {text}")

    def scalar(self, sql, params=None):
        text = " ".join(sql.split())
        self.calls.append(("scalar", text, params))
        if "MAX(revision)" in text:
            revisions = [row["revision"] for row in self.sets.values()
                         if row["name"] == params[0]]
            return max(revisions, default=0) + 1
        raise AssertionError(f"unexpected scalar(): {text}")

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.calls.append(("execute", text, params))
        if self.execute_error is not None:
            raise self.execute_error
        if text.startswith("INSERT INTO replay_case_sets"):
            row = {"id": self.next_id, "revision": params[0], "name": params[1],
                   "purpose": params[2], "frozen_at": None}
            self.sets[self.next_id] = row
            self.next_id += 1
            return row["id"]
        if text.startswith("UPDATE replay_case_sets SET frozen_at"):
            self.sets[int(params[1])]["frozen_at"] = params[0]
            return 1
        if text.startswith("UPDATE replay_case_sets SET"):
            row = self.sets[int(params[-1])]
            for column, value in zip(("name", "purpose"), params[:-1]):
                row[column] = value
            return 1
        if text.startswith("INSERT INTO replay_cases"):
            source = [case for case in self.cases
                      if case["case_set_id"] == int(params[1])]
            for case in source:
                self.cases.append({**case, "id": self.next_id,
                                   "case_set_id": int(params[0])})
                self.next_id += 1
            return len(source)
        raise AssertionError(f"unexpected execute(): {text}")


def _repo(db):
    return CaseRepository(db)


def _set(set_id=1, name="P0 回归", revision=1, frozen_at=None,
         purpose="golden"):
    return {"id": set_id, "name": name, "revision": revision,
            "purpose": purpose, "frozen_at": frozen_at}


def test_duplicate_set_name_maps_to_case_set_exists():
    db = FakeDB(sets=[_set()])
    db.execute_error = IntegrityError(1062, "Duplicate entry")
    with pytest.raises(ApiError) as err:
        _repo(db).create_set(name="P0 回归", purpose="regression")
    assert err.value.code == "CASE_SET_EXISTS"
    assert "建新版本" in err.value.message


def test_frozen_set_rejects_writes_with_new_version_hint():
    db = FakeDB(sets=[_set(frozen_at="2026-11-17 00:00:00")])
    repo = _repo(db)
    with pytest.raises(ApiError) as unfrozen:
        repo.require_unfrozen(1)
    assert unfrozen.value.code == "CASE_SET_FROZEN"
    assert "P0 回归 v1 已冻结" in unfrozen.value.message
    assert "建新版本" in unfrozen.value.message

    with pytest.raises(ApiError) as again:
        repo.freeze_set(1)
    assert again.value.code == "CASE_SET_FROZEN"
    assert not any("UPDATE replay_case_sets SET frozen_at" in call[1]
                   for call in db.calls)


def test_freeze_and_copy_new_revision():
    db = FakeDB(sets=[_set()],
                cases=[{"id": 1, "case_set_id": 1, "canonical_id": 9,
                        "decision_key": "flop:2", "spec_id": 7,
                        "priority": 0, "status": "draft", "note": None,
                        "overrides_json": None}])
    repo = _repo(db)
    repo.freeze_set(1)
    assert db.sets[1]["frozen_at"]

    copied = repo.copy_set_to_new_revision(1)
    assert copied["id"] == 2
    assert copied["revision"] == 2
    assert copied["frozen_at"] is None
    assert [case["case_set_id"] for case in db.cases] == [1, 2]
    insert_cases = [call for call in db.calls
                    if call[1].startswith("INSERT INTO replay_cases")][0]
    assert insert_cases[2] == (2, 1)  # (新 id, 源案例集 id)


def test_decisions_for_bucket_excludes_taken_and_orders_by_recency():
    db = FakeDB()
    db.decisions = [{"canonical_id": 9, "street": "flop", "seq": 2}]
    rows = _repo(db).decisions_for_bucket(
        {"street": ["flop"], "hand_tier": ["TPTK"]}, limit=5,
        exclude=[(9, "flop", 2)])
    assert rows == db.decisions
    query = [call for call in db.calls if call[0] == "query"][0]
    assert "d.street IN (%s)" in query[1]
    assert "d.hand_tier IN (%s)" in query[1]
    assert "NOT ((d.canonical_id, d.street, d.seq) IN ((%s,%s,%s)))" in query[1]
    assert "ORDER BY d.id DESC" in query[1]
    assert query[2][-1] == 5


def test_decisions_for_bucket_matches_any_value_in_a_dimension():
    db = FakeDB()
    db.decisions = []
    _repo(db).decisions_for_bucket(
        {"hand_tier": ["TPTK", "AIR"], "street": ["flop", "turn"]}, limit=3)
    query = [call for call in db.calls if call[0] == "query"][0]
    assert "d.hand_tier IN (%s, %s)" in query[1]
    assert "d.street IN (%s, %s)" in query[1]
    assert query[2][1:] == ("flop", "turn", "TPTK", "AIR", 3)
