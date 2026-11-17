"""期望库 API：路由顺序、camelCase 透传、422 fail-fast。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import replay.api as api_module
from replay.config import Settings
from replay.errors import ApiError


class FakeSpecService:
    def __init__(self):
        self.calls = []
        self.fail = False

    def _check(self):
        if self.fail:
            raise ApiError("SPEC_NOT_FOUND", "期望 9 不存在", 404)

    def spec_meta(self):
        return {"schema": "spot_spec.v1", "dimensions": [],
                "actions": [], "sizingUnits": [], "statuses": []}

    def list_specs(self, query):
        self._check()
        self.calls.append(("list_specs", query))
        return {"items": [], "total": 0}

    def get_spec(self, spec_id):
        self._check()
        self.calls.append(("get_spec", spec_id))
        return {"id": spec_id, "name": "x", "revision": 1}

    def create_spec(self, payload):
        self._check()
        self.calls.append(("create_spec", payload))
        return {"id": 1, **payload}

    def create_spec_revision(self, spec_id, payload):
        self._check()
        self.calls.append(("create_spec_revision", spec_id, payload))
        return {"id": spec_id + 1, **payload}

    def patch_spec(self, spec_id, payload):
        self._check()
        self.calls.append(("patch_spec", spec_id, payload))
        return {"id": spec_id, **payload}

    def delete_spec_revision(self, spec_id):
        self._check()
        self.calls.append(("delete_spec_revision", spec_id))
        return {"name": "x", "deletedRevisions": 2}

    def spec_history(self, spec_id):
        self._check()
        self.calls.append(("spec_history", spec_id))
        return {"name": "x", "items": []}

    def spec_diff(self, spec_id, against_id=None):
        self._check()
        self.calls.append(("spec_diff", spec_id, against_id))
        return {"from": None, "to": {"id": spec_id}, "changes": []}


@pytest.fixture()
def client(monkeypatch):
    fake = FakeSpecService()
    monkeypatch.setattr(api_module, "ReplayService",
                        lambda *args, **kwargs: fake)
    settings = Settings(auto_migrate=False, worker_enabled=False,
                        db_user="nobody", db_password="", db_name="nodb")
    app = api_module.create_app(settings, start_worker=False)
    with TestClient(app) as test_client:
        app.state.service = fake
        yield test_client, fake


def test_meta_is_not_shadowed_by_the_id_route(client):
    test_client, fake = client
    response = test_client.get("/api/specs/meta")
    assert response.status_code == 200
    assert response.json()["schema"] == "spot_spec.v1"
    assert fake.calls == []


def test_create_passes_camel_case_and_schema_alias(client):
    test_client, fake = client
    body = {
        "name": "flop EP",
        "schema": "spot_spec.v1",
        "bucket": {"street": ["flop"]},
        "allowedActions": ["check"],
        "forbiddenActions": ["fold"],
        "sizing": {"unit": "bb", "min": 2, "max": 4},
        "requiredPoints": [{"key": "k", "text": "t"}],
    }
    response = test_client.post("/api/specs", json=body)
    assert response.status_code == 200
    _name, payload = fake.calls[0]
    assert payload["schema"] == "spot_spec.v1"
    assert payload["allowedActions"] == ["check"]
    assert payload["sizing"] == {"unit": "bb", "min": 2, "max": 4}


def test_unknown_top_level_field_is_rejected_not_dropped(client):
    test_client, _fake = client
    response = test_client.post("/api/specs", json={
        "name": "x", "allowedActions": ["check"], "freq_min_n": 15})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_revision_route_passes_full_payload(client):
    test_client, fake = client
    response = test_client.post("/api/specs/3/revisions", json={
        "rationale": "v2", "allowedActions": ["bet"],
        "forbiddenActions": ["fold"]})
    assert response.status_code == 200
    _name, spec_id, payload = fake.calls[0]
    assert spec_id == 3
    assert payload["rationale"] == "v2"


def test_patch_is_status_only(client):
    test_client, fake = client
    ok = test_client.patch("/api/specs/3", json={"status": "active"})
    assert ok.status_code == 200
    _name, spec_id, payload = fake.calls[0]
    assert (spec_id, payload) == (3, {"status": "active"})
    bad = test_client.patch("/api/specs/3", json={
        "status": "active", "rationale": "偷偷改内容"})
    assert bad.status_code == 422


def test_diff_accepts_against_query(client):
    test_client, fake = client
    response = test_client.get("/api/specs/3/diff?against=1")
    assert response.status_code == 200
    assert fake.calls[0] == ("spec_diff", 3, 1)


def test_history_and_delete(client):
    test_client, fake = client
    assert test_client.get("/api/specs/3/history").status_code == 200
    response = test_client.delete("/api/specs/3")
    assert response.status_code == 200
    assert response.json()["deletedRevisions"] == 2
    assert ("delete_spec_revision", 3) in fake.calls


def test_api_error_envelope(client):
    test_client, fake = client
    fake.fail = True
    response = test_client.get("/api/specs/9")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SPEC_NOT_FOUND"
