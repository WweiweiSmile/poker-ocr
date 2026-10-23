from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import replay.api as api_module
from replay.config import Settings
from replay.errors import ApiError


class FakeService:
    def __init__(self):
        self.calls = []
        self.fail = False

    def _check(self):
        if self.fail:
            raise ApiError("HAND_NOT_FOUND", "手牌 1 不存在", 404)

    def meta(self):
        self._check()
        return {"handCount": 3, "favoriteCount": 1, "versionCount": 1,
                "lastJob": None, "defaults": {"versionIds": [1], "repeat": 1},
                "current": {"adviceVersion": "a", "reconstructVersion": "r",
                            "promptStyles": ["lean"], "engine": {"commit": None, "dirty": False}},
                "tags": ["典型"], "markStatuses": ["todo"], "runStatuses": [],
                "limits": {"maxCallsPerJob": 2000, "maxQueueDepth": 3,
                           "repeatMax": 5, "pageSize": 50},
                "settings": {}, "health": {"ok": True}}

    def health(self):
        self._check()
        return {"ok": True}

    def list_hands(self, query):
        self._check()
        self.calls.append(("list_hands", query))
        return {"total": 0, "page": 1, "pageSize": 50, "items": []}

    def get_hand(self, hand_id):
        self._check()
        return {"hand": {"id": hand_id}}

    def hand_comparison(self, hand_id, query):
        self._check()
        return {"handId": hand_id, "decisions": []}

    def patch_mark(self, hand_id, payload):
        self._check()
        self.calls.append(("patch_mark", hand_id, payload))
        return {"handId": hand_id, "favorite": payload.get("favorite", False),
                "tags": payload.get("tags", []), "status": "todo", "note": ""}

    def list_versions(self, include_archived):
        self._check()
        return [{"id": 1, "name": "v1", "archived": include_archived}]

    def create_version(self, payload):
        self._check()
        return {"id": 2, "name": payload.get("name")}

    def import_env(self):
        self._check()
        return {"version": {"id": 1, "name": "v1"}, "reused": False}

    def clone_version(self, version_id, payload):
        self._check()
        return {"id": 3, "name": payload.get("name")}

    def update_version(self, version_id, payload):
        self._check()
        return {"id": version_id, **payload}

    def preview_run(self, payload):
        self._check()
        return {"handCount": 1, "totalUnits": 2}

    def create_run(self, payload):
        self._check()
        self.calls.append(("create_run", payload))
        return {"jobId": 9, "status": "queued", "totalUnits": 2}

    def list_runs(self, query):
        self._check()
        return {"total": 0, "items": []}

    def get_run(self, job_id):
        self._check()
        return {"id": job_id, "status": "running"}

    def job_results(self, job_id, query):
        self._check()
        return {"job": {"id": job_id}, "hands": [], "versions": []}

    def cancel_run(self, job_id):
        self._check()
        return {"canceled": True, "jobId": job_id}

    def retry_failed(self, job_id):
        self._check()
        return {"jobId": 10}

    def export(self, query):
        self._check()
        return ("csv", "replay.csv", "a,b\n1,2\n")


@pytest.fixture()
def client(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(api_module, "ReplayService", lambda *args, **kwargs: fake)
    settings = Settings(auto_migrate=False, worker_enabled=False,
                        db_user="nobody", db_password="", db_name="nodb")
    app = api_module.create_app(settings, start_worker=False)
    with TestClient(app) as test_client:
        app.state.service = fake
        yield test_client, fake


def test_meta_and_validation_envelope(client):
    test_client, _fake = client
    response = test_client.get("/api/meta")
    assert response.status_code == 200
    assert response.json()["handCount"] == 3
    bad = test_client.post("/api/runs", json={"scope": "hands", "repeat": 9})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_mark_and_run_roundtrip(client):
    test_client, fake = client
    response = test_client.patch("/api/hands/1/mark",
                                 json={"favorite": True, "tags": ["典型"]})
    assert response.status_code == 200
    assert response.json()["favorite"] is True
    assert ("patch_mark", 1, {"favorite": True, "tags": ["典型"]}) in fake.calls

    created = test_client.post("/api/runs", json={
        "versionIds": [1], "scope": "hands", "handIds": [1],
        "repeat": 2, "dedupe": True,
    })
    assert created.status_code == 200
    assert created.json()["jobId"] == 9
    payload = [call for call in fake.calls if call[0] == "create_run"][0][1]
    assert payload["repeat"] == 2


def test_api_error_envelope(client):
    test_client, fake = client
    fake.fail = True
    response = test_client.get("/api/hands/1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HAND_NOT_FOUND"


def test_export_headers(client):
    test_client, _fake = client
    response = test_client.get("/api/export?jobId=9&format=csv")
    assert response.status_code == 200
    assert response.text.startswith("a,b")
    assert "attachment" in response.headers["content-disposition"]
