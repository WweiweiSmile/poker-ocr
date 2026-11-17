"""案例集 API（#55）：路由顺序、camelCase 透传、冻结 409 原样回传。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import replay.api as api_module
from replay.config import Settings
from replay.errors import ApiError


class FakeCaseSetService:
    def __init__(self):
        self.calls = []
        self.frozen = False

    def _check(self):
        if self.frozen:
            raise ApiError(
                "CASE_SET_FROZEN",
                "案例集 x v1 已冻结，不能再改；请从它建新版本", 409)

    def list_case_sets(self, query):
        self.calls.append(("list_case_sets", query))
        return {"items": [], "total": 0}

    def get_case_set(self, set_id):
        self.calls.append(("get_case_set", set_id))
        return {"id": set_id, "name": "x", "revision": 1, "items": []}

    def create_case_set(self, payload):
        self.calls.append(("create_case_set", payload))
        return {"id": 7, **payload}

    def patch_case_set(self, set_id, payload):
        self._check()
        self.calls.append(("patch_case_set", set_id, payload))
        return {"id": set_id, **payload}

    def freeze_case_set(self, set_id):
        self.calls.append(("freeze_case_set", set_id))
        return {"id": set_id, "frozen": True}

    def create_case_set_revision(self, set_id, payload):
        self.calls.append(("create_case_set_revision", set_id, payload))
        return {"id": set_id + 1, "revision": 2}

    def add_case_set_cases(self, set_id, payload):
        self._check()
        self.calls.append(("add_case_set_cases", set_id, payload))
        return {"created": 1, "existing": 0, "skipped": []}

    def patch_case_set_case(self, set_id, case_id, payload):
        self._check()
        self.calls.append(("patch_case_set_case", set_id, case_id, payload))
        return {"id": case_id, **payload}

    def delete_case_set_case(self, set_id, case_id):
        self._check()
        self.calls.append(("delete_case_set_case", set_id, case_id))
        return {"deleted": 1}

    def build_hand_cases(self, set_id, payload):
        self._check()
        self.calls.append(("build_hand_cases", set_id, payload))
        return {"created": 3, "existing": 0, "noSpec": []}

    def sample_case_set_cases(self, set_id, payload):
        self.calls.append(("sample_case_set_cases", set_id, payload))
        return {"created": 2, "noSpec": []}

    def seed_case_sets(self, payload):
        self.calls.append(("seed_case_sets", payload))
        return {"dryRun": True, "setName": "seed-2026-11",
                "specs": {"created": [], "reused": [], "wouldCreate": []},
                "cases": {"created": 0, "existing": 0, "skipped": [],
                          "wouldCreate": [], "skippedPlan": []}}


@pytest.fixture()
def client(monkeypatch):
    fake = FakeCaseSetService()
    monkeypatch.setattr(api_module, "ReplayService",
                        lambda *args, **kwargs: fake)
    settings = Settings(auto_migrate=False, worker_enabled=False,
                        db_user="nobody", db_password="", db_name="nodb")
    app = api_module.create_app(settings, start_worker=False)
    with TestClient(app) as test_client:
        app.state.service = fake
        yield test_client, fake


def test_create_takes_json_body_not_query_param(client):
    """回归：漏 import 的 schema 会让 body 退化成 query 参数并 422。"""
    test_client, fake = client
    response = test_client.post(
        "/api/case-sets", json={"name": "P0 回归", "purpose": "regression"})
    assert response.status_code == 200, response.text
    assert fake.calls == [
        ("create_case_set", {"name": "P0 回归", "purpose": "regression"})]


def test_create_rejects_unknown_purpose(client):
    test_client, _fake = client
    response = test_client.post(
        "/api/case-sets", json={"name": "x", "purpose": "nope"})
    assert response.status_code == 422


def test_seed_route_is_not_shadowed_by_the_id_route(client):
    test_client, fake = client
    response = test_client.post("/api/case-sets/seed", json={"dryRun": True})
    assert response.status_code == 200
    assert response.json()["dryRun"] is True
    assert fake.calls == [("seed_case_sets", {"dryRun": True})]


def test_batch_add_passes_camel_case_fields(client):
    test_client, fake = client
    body = {"cases": [{"canonicalId": 11, "decisionKey": "flop:3",
                       "specId": 5, "priority": 0}]}
    response = test_client.post("/api/case-sets/7/cases", json=body)
    assert response.status_code == 200
    _name, set_id, payload = fake.calls[0]
    assert set_id == 7
    assert payload["cases"][0] == {"canonicalId": 11,
                                   "decisionKey": "flop:3",
                                   "specId": 5, "priority": 0}


def test_frozen_set_patch_surfaces_409_with_hint(client):
    test_client, fake = client
    fake.frozen = True
    response = test_client.patch("/api/case-sets/7", json={"name": "y"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CASE_SET_FROZEN"
    assert "建新版本" in error["message"]
