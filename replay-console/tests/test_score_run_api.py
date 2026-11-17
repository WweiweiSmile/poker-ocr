"""评分运行 API（#57）：路由顺序、camelCase 透传、错误码原样回传。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import replay.api as api_module
from replay.config import Settings
from replay.errors import ApiError


class FakeScoreRunService:
    def __init__(self):
        self.calls = []
        self.q = []

    def score_run_meta(self):
        self.calls.append(("score_run_meta",))
        return {"caseSets": [], "versions": [],
                "judgeVersion": "rules-v1", "freqMinN": 15}

    def estimate_score_run(self, payload):
        self.calls.append(("estimate_score_run", payload))
        return {"caseSetId": payload.get("caseSetId"),
                "calls": 25, "cost": None}

    def list_score_runs(self, query):
        self.calls.append(("list_score_runs", dict(query)))
        return {"items": []}

    def create_score_run(self, payload):
        if payload.get("caseSetId") == 0:
            raise ApiError("CASE_SET_NOT_FROZEN", "案例集未冻结，先冻结", 409)
        self.calls.append(("create_score_run", payload))
        return {"id": 901, "status": "queued"}

    def get_score_run(self, run_id):
        self.calls.append(("get_score_run", run_id))
        return {"id": run_id, "status": "running"}

    def cancel_score_run(self, run_id):
        self.calls.append(("cancel_score_run", run_id))
        return {"id": run_id, "cancelRequested": True}

    def score_run_items(self, run_id, query):
        self.calls.append(("score_run_items", run_id, dict(query)))
        return {"items": [], "counts": {"items": 0}}

    def score_run_item(self, run_id, case_id, repeat_index):
        self.calls.append(("score_run_item", run_id, case_id, repeat_index))
        return {"caseId": case_id, "repeatIndex": repeat_index}


@pytest.fixture()
def client(monkeypatch):
    fake = FakeScoreRunService()
    monkeypatch.setattr(api_module, "ReplayService",
                        lambda *args, **kwargs: fake)
    settings = Settings(auto_migrate=False, worker_enabled=False,
                        db_user="nobody", db_password="", db_name="nodb")
    app = api_module.create_app(settings, start_worker=False)
    with TestClient(app) as test_client:
        app.state.service = fake
        yield test_client, fake


def test_meta_is_not_shadowed_by_run_id_route(client):
    test_client, fake = client
    response = test_client.get("/api/score-runs/meta")
    assert response.status_code == 200, response.text
    assert response.json()["judgeVersion"] == "rules-v1"
    assert fake.calls == [("score_run_meta",)]


def test_estimate_takes_json_body(client):
    test_client, fake = client
    response = test_client.post("/api/score-runs/estimate",
                                json={"caseSetId": 4, "versionId": 7})
    assert response.status_code == 200, response.text
    assert fake.calls == [("estimate_score_run",
                           {"caseSetId": 4, "versionId": 7})]


def test_create_propagates_409_error_code(client):
    test_client, _fake = client
    response = test_client.post("/api/score-runs",
                                json={"caseSetId": 0, "versionId": 7})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CASE_SET_NOT_FROZEN"


def test_items_passes_only_filter(client):
    test_client, fake = client
    response = test_client.get("/api/score-runs/901/items?only=hardfail"
                               "&limit=50&offset=10")
    assert response.status_code == 200, response.text
    name, run_id, query = fake.calls[0]
    assert name == "score_run_items" and run_id == 901
    assert query["only"] == "hardfail"
    assert query["limit"] == "50" and query["offset"] == "10"


def test_item_route_parses_case_and_repeat(client):
    test_client, fake = client
    response = test_client.get("/api/score-runs/901/items/12/2")
    assert response.status_code == 200, response.text
    assert fake.calls == [("score_run_item", 901, 12, 2)]


def test_cancel_route(client):
    test_client, fake = client
    response = test_client.post("/api/score-runs/901/cancel")
    assert response.status_code == 200
    assert response.json()["cancelRequested"] is True
    assert fake.calls == [("cancel_score_run", 901)]


def test_list_filters_are_forwarded(client):
    test_client, fake = client
    response = test_client.get("/api/score-runs?status=partial&limit=5")
    assert response.status_code == 200
    assert fake.calls == [("list_score_runs",
                           {"status": "partial", "limit": "5"})]
