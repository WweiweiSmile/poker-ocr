"""从决策点建案例的服务层校验（#53 最小入口）。"""
from __future__ import annotations

import pytest

from replay.errors import ApiError
from replay.service import ReplayService


class _FakeCanonicalRepo:
    def __init__(self, rows):
        self.rows = {int(row["id"]): row for row in rows}

    def get(self, canonical_id):
        return self.rows.get(int(canonical_id))

    def sources(self, canonical_id):
        return []


class _FakeDecisionRepo:
    def __init__(self, rows):
        self.rows = rows

    def for_canonical(self, canonical_id):
        return list(self.rows)


class _FakeSpecRepo:
    def __init__(self, rows):
        self.rows = {int(row["id"]): row for row in rows}

    def get(self, spec_id):
        return self.rows.get(int(spec_id))


class _FakeCaseRepo:
    def __init__(self):
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return ({"id": 1, "case_set_id": 1, **kwargs}, True)

    def get_by_id(self, case_id):
        return {"id": case_id, "case_set_id": 1, "canonical_id": 1,
                "decision_key": "flop:2", "spec_id": 7, "priority": 0,
                "status": "draft", "set_name": "待整理", "set_revision": 1,
                "set_frozen_at": None, "created_at": None}


def _service(decisions=({"street": "flop", "seq": 2},)):
    service = object.__new__(ReplayService)
    service.canonical_repo = _FakeCanonicalRepo([{"id": 1}])
    service.decision_repo = _FakeDecisionRepo(decisions)
    service.spec_repo = _FakeSpecRepo([{"id": 7, "name": "顶对强踢不弃牌"}])
    service.case_repo = _FakeCaseRepo()
    return service


def test_create_case_happy_path_and_p0():
    service = _service()
    result = service.create_case({
        "canonicalId": 1, "decisionKey": "flop:2", "specId": 7,
        "priority": 0, "note": "别弃",
    })
    assert result["created"] is True
    assert result["case"]["p0"] is True
    assert service.case_repo.created[0]["priority"] == 0
    assert service.case_repo.created[0]["decision_key"] == "flop:2"


def test_create_case_requires_canonical_and_valid_key():
    service = _service()
    with pytest.raises(ApiError) as missing:
        service.create_case({"decisionKey": "flop:2", "specId": 7})
    assert missing.value.code == "INVALID_CASE"

    with pytest.raises(ApiError) as bad_key:
        service.create_case({"canonicalId": 1, "decisionKey": "flop-x",
                             "specId": 7})
    assert bad_key.value.code == "INVALID_CASE"


def test_create_case_rejects_unknown_decision_point():
    service = _service(decisions=({"street": "preflop", "seq": 0},))
    with pytest.raises(ApiError) as unknown:
        service.create_case({"canonicalId": 1, "decisionKey": "flop:2",
                             "specId": 7})
    assert unknown.value.code == "DECISION_NOT_FOUND"


def test_create_case_rejects_bad_overrides():
    service = _service()
    with pytest.raises(ApiError) as not_object:
        service.create_case({"canonicalId": 1, "decisionKey": "flop:2",
                             "specId": 7, "overrides": ["action"]})
    assert not_object.value.code == "INVALID_CASE_OVERRIDES"

    with pytest.raises(ApiError) as unknown_key:
        service.create_case({"canonicalId": 1, "decisionKey": "flop:2",
                             "specId": 7, "overrides": {"bogus": 1}})
    assert unknown_key.value.code == "INVALID_CASE_OVERRIDES"

    service = _service()
    service.create_case({"canonicalId": 1, "decisionKey": "flop:2",
                         "specId": 7, "overrides": {"action": "fold"}})
    assert service.case_repo.created[0]["overrides"] == {"action": "fold"}
def test_create_case_requires_existing_spec():
    service = _service()
    with pytest.raises(ApiError) as missing_spec:
        service.create_case({"canonicalId": 1, "decisionKey": "flop:2",
                             "specId": 999})
    assert missing_spec.value.code == "SPEC_NOT_FOUND"
