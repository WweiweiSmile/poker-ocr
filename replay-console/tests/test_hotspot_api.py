"""偏差热点 API（#59）：报表路由与「补样候选」路由的参数透传。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import replay.api as api_module
from replay.config import Settings


class FakeHotspotService:
    def __init__(self):
        self.calls = []

    def score_run_hotspots(self, run_id):
        self.calls.append(("score_run_hotspots", run_id))
        return {"run": {"id": run_id}, "summary": {"freqMinN": 15},
                "rows": []}

    def case_candidates(self, set_id, query):
        self.calls.append(("case_candidates", set_id, dict(query)))
        return {"caseSet": {"id": set_id}, "items": []}


@pytest.fixture()
def client(monkeypatch):
    fake = FakeHotspotService()
    monkeypatch.setattr(api_module, "ReplayService",
                        lambda *args, **kwargs: fake)
    settings = Settings(auto_migrate=False, worker_enabled=False,
                        db_user="nobody", db_password="", db_name="nodb")
    app = api_module.create_app(settings, start_worker=False)
    with TestClient(app) as test_client:
        app.state.service = fake
        yield test_client, fake


def test_hotspots_route_passes_run_id(client):
    test_client, fake = client
    response = test_client.get("/api/score-runs/12/hotspots")
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["freqMinN"] == 15
    assert fake.calls == [("score_run_hotspots", 12)]


def test_candidates_route_forwards_bucket_query(client):
    test_client, fake = client
    response = test_client.get(
        "/api/case-sets/4/candidates"
        "?street=flop&hand_tier=STRONG_MADE&limit=20")
    assert response.status_code == 200, response.text
    assert fake.calls == [("case_candidates", 4, {
        "street": "flop", "hand_tier": "STRONG_MADE", "limit": "20"})]
