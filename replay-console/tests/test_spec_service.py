"""期望库的版本语义：保存即新 revision、旧 revision 只读、active/draft 切换。

用内存版仓储替换 MySQL 仓储，测的是 `ReplayService` 的公开方法。
"""

from __future__ import annotations

import itertools

import pytest

from replay.config import Settings
from replay.errors import ApiError
from replay.service import ReplayService


class FakeSpecRepo:
    """内存版 SpotSpecRepository：只实现 service 用到的方法。"""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self._ids = itertools.count(1)

    def create(self, *, name, revision, status, content, note, supersedes):
        spec_id = next(self._ids)
        self.rows[spec_id] = {
            "id": spec_id,
            "name": name,
            "revision": revision,
            "status": status,
            "bucket_json": content["bucket"],
            "spec_json": {key: value for key, value in content.items()
                          if key != "bucket"},
            "note": note,
            "supersedes": supersedes,
            "created_at": f"2026-11-17T03:0{spec_id}:00",
        }
        return spec_id

    def get(self, spec_id):
        row = self.rows.get(int(spec_id))
        if row is None:
            return None
        return self._with_counts(row)

    def latest(self, name):
        rows = [row for row in self.rows.values() if row["name"] == name]
        if not rows:
            return None
        return self._with_counts(max(rows, key=lambda row: row["revision"]))

    def by_name(self, name):
        rows = sorted((row for row in self.rows.values()
                       if row["name"] == name),
                      key=lambda row: row["revision"], reverse=True)
        return [self._with_counts(row) for row in rows]

    def list_latest(self):
        latest: dict[str, dict] = {}
        for row in self.rows.values():
            current = latest.get(row["name"])
            if current is None or row["revision"] > current["revision"]:
                latest[row["name"]] = row
        return [self._with_counts(row) for row in latest.values()]

    def activate(self, spec_id, name):
        for row in self.rows.values():
            if row["name"] != name:
                continue
            if row["id"] == int(spec_id):
                row["status"] = "active"
            elif row["status"] == "active":
                row["status"] = "draft"
        return 1

    def set_status(self, spec_id, status):
        self.rows[int(spec_id)]["status"] = status
        return 1

    def delete(self, spec_id):
        self.rows.pop(int(spec_id), None)
        return 1

    def _with_counts(self, row):
        rows = [item for item in self.rows.values()
                if item["name"] == row["name"]]
        active = [item for item in rows if item["status"] == "active"]
        out = dict(row)
        out["revision_count"] = len(rows)
        out["active_revision_id"] = (
            max(active, key=lambda item: item["revision"])["id"]
            if active else None)
        return out


def make_service():
    service = ReplayService(
        Settings(db_user="", db_password="", db_name="nodb",
                 worker_enabled=False), db=object())
    service.spec_repo = FakeSpecRepo()
    return service


def payload(**overrides):
    base = {
        "name": "flop EP check TPTK",
        "bucket": {"street": ["flop"], "position_bucket": ["EP"],
                   "hand_tier": ["TPTK"]},
        "allowedActions": ["check", "bet"],
        "forbiddenActions": ["fold"],
        "sizing": {"unit": "pot_fraction", "min": 0.33, "max": 0.75},
        "requiredPoints": [{"key": "protect", "text": "保护过牌范围"}],
        "rationale": "v1",
    }
    base.update(overrides)
    return base


def code_of(exc: pytest.ExceptionInfo) -> str:
    assert isinstance(exc.value, ApiError)
    return exc.value.code


def test_create_makes_revision_one_as_draft():
    service = make_service()
    spec = service.create_spec(payload())
    assert spec["revision"] == 1
    assert spec["status"] == "draft"
    assert spec["supersedes"] is None
    assert spec["revisionCount"] == 1
    assert spec["bucketLabel"] == "翻牌 · 前位 · 顶对强踢"
    assert spec["allowedActions"] == ["check", "bet"]


def test_create_can_activate_immediately():
    service = make_service()
    spec = service.create_spec(payload(status="active"))
    assert spec["status"] == "active"
    assert spec["activeRevisionId"] == spec["id"]


def test_duplicate_name_is_rejected():
    service = make_service()
    service.create_spec(payload())
    with pytest.raises(ApiError) as exc:
        service.create_spec(payload())
    assert code_of(exc) == "SPEC_NAME_TAKEN"
    assert exc.value.status_code == 409


def test_new_revision_keeps_old_revision_untouched():
    service = make_service()
    v1 = service.create_spec(payload())
    v2 = service.create_spec_revision(v1["id"], payload(rationale="v2"))
    assert v2["revision"] == 2
    assert v2["supersedes"] == v1["id"]
    assert v2["revisionCount"] == 2
    # 旧 revision 的内容原样可读
    again = service.get_spec(v1["id"])
    assert again["rationale"] == "v1"
    assert again["revision"] == 1
    assert again["status"] == "draft"


def test_revision_must_start_from_latest():
    service = make_service()
    v1 = service.create_spec(payload())
    service.create_spec_revision(v1["id"], payload(rationale="v2"))
    with pytest.raises(ApiError) as exc:
        service.create_spec_revision(v1["id"], payload(rationale="v3"))
    assert code_of(exc) == "SPEC_NOT_LATEST"
    assert exc.value.status_code == 409


def test_revision_cannot_rename_the_spec():
    service = make_service()
    v1 = service.create_spec(payload())
    with pytest.raises(ApiError) as exc:
        service.create_spec_revision(v1["id"], payload(name="other name"))
    assert code_of(exc) == "SPEC_NAME_IMMUTABLE"


def test_invalid_unit_fails_before_writing_a_revision():
    service = make_service()
    v1 = service.create_spec(payload())
    with pytest.raises(ApiError) as exc:
        service.create_spec_revision(
            v1["id"], payload(sizing={"unit": "percent", "min": 1, "max": 2}))
    assert code_of(exc) == "INVALID_SPEC_SIZING"
    assert len(service.spec_repo.rows) == 1


def test_activating_a_revision_demotes_the_previous_one():
    service = make_service()
    v1 = service.create_spec(payload(status="active"))
    v2 = service.create_spec_revision(v1["id"], payload(rationale="v2"))
    assert service.get_spec(v2["id"])["status"] == "draft"
    service.patch_spec(v2["id"], {"status": "active"})
    assert service.get_spec(v2["id"])["status"] == "active"
    assert service.get_spec(v1["id"])["status"] == "draft"
    # 回滚：把 v1 重新置 active，v2 自动退回 draft
    service.patch_spec(v1["id"], {"status": "active"})
    assert service.get_spec(v1["id"])["status"] == "active"
    assert service.get_spec(v2["id"])["status"] == "draft"


def test_status_filter_uses_the_latest_revision_status():
    service = make_service()
    v1 = service.create_spec(payload(status="active"))
    v2 = service.create_spec_revision(v1["id"], payload(rationale="v2"))
    assert service.list_specs({"status": "active"})["items"] == []
    assert [item["id"] for item in
            service.list_specs({"status": "draft"})["items"]] == [v2["id"]]


def test_deactivating_leaves_no_active_revision():
    service = make_service()
    v1 = service.create_spec(payload(status="active"))
    service.patch_spec(v1["id"], {"status": "draft"})
    assert service.get_spec(v1["id"])["activeRevisionId"] is None


def test_cannot_delete_a_spec_with_an_active_revision():
    service = make_service()
    v1 = service.create_spec(payload(status="active"))
    with pytest.raises(ApiError) as exc:
        service.delete_spec_revision(v1["id"])
    assert code_of(exc) == "SPEC_ACTIVE"
    assert exc.value.status_code == 409


def test_deleting_the_latest_draft_keeps_older_revisions():
    service = make_service()
    v1 = service.create_spec(payload())
    v2 = service.create_spec_revision(v1["id"], payload(rationale="v2"))
    result = service.delete_spec_revision(v2["id"])
    assert result["deletedRevisions"] == 1
    assert service.get_spec(v1["id"])["revision"] == 1
    with pytest.raises(ApiError) as exc:
        service.get_spec(v2["id"])
    assert code_of(exc) == "SPEC_NOT_FOUND"


def test_cannot_delete_an_old_revision():
    service = make_service()
    v1 = service.create_spec(payload())
    service.create_spec_revision(v1["id"], payload(rationale="v2"))
    with pytest.raises(ApiError) as exc:
        service.delete_spec_revision(v1["id"])
    assert code_of(exc) == "SPEC_NOT_LATEST"
    assert exc.value.status_code == 409


def test_history_is_newest_first():
    service = make_service()
    v1 = service.create_spec(payload())
    v2 = service.create_spec_revision(v1["id"], payload(rationale="v2"))
    v3 = service.create_spec_revision(v2["id"], payload(rationale="v3"))
    history = service.spec_history(v1["id"])
    assert history["name"] == "flop EP check TPTK"
    assert [item["revision"] for item in history["items"]] == [3, 2, 1]
    assert history["items"][0]["revisionCount"] == 3
    assert history["items"][2]["id"] == v1["id"]
    assert history["items"][0]["id"] == v3["id"]


def test_diff_against_previous_revision():
    service = make_service()
    v1 = service.create_spec(payload())
    v2 = service.create_spec_revision(v1["id"], payload(
        rationale="v2",
        bucket={"street": ["flop"], "position_bucket": ["EP"],
                "hand_tier": ["TPTK", "TP_WEAK"]},
        requiredPoints=[],
    ))
    diff = service.spec_diff(v2["id"])
    assert diff["from"]["id"] == v1["id"]
    assert diff["to"]["id"] == v2["id"]
    sections = {change["section"] for change in diff["changes"]}
    assert sections == {"bucket", "points", "rationale"}


def test_diff_can_compare_two_arbitrary_revisions():
    service = make_service()
    v1 = service.create_spec(payload())
    v2 = service.create_spec_revision(v1["id"], payload(rationale="v2"))
    diff = service.spec_diff(v2["id"], against_id=v1["id"])
    assert [c["section"] for c in diff["changes"]] == ["rationale"]


def test_diff_rejects_cross_name_comparison():
    service = make_service()
    v1 = service.create_spec(payload())
    other = service.create_spec(payload(name="another spec"))
    with pytest.raises(ApiError) as exc:
        service.spec_diff(other["id"], against_id=v1["id"])
    assert code_of(exc) == "INVALID_SPEC_DIFF"


def test_list_groups_by_bucket_and_filters_status():
    service = make_service()
    a = service.create_spec(payload(name="a", status="active"))
    service.create_spec(payload(
        name="b", bucket={"street": ["turn"], "hand_tier": ["AIR"]}))
    listed = service.list_specs({})
    assert [item["name"] for item in listed["items"]] == ["a", "b"]
    assert listed["items"][0]["bucketLabel"].startswith("翻牌")
    active = service.list_specs({"status": "active"})
    assert [item["id"] for item in active["items"]] == [a["id"]]
    draft = service.list_specs({"status": "draft"})
    assert [item["name"] for item in draft["items"]] == ["b"]


def test_list_shows_latest_revision_but_active_revision_id():
    service = make_service()
    v1 = service.create_spec(payload(status="active"))
    v2 = service.create_spec_revision(v1["id"], payload(rationale="v2"))
    item = service.list_specs({})["items"][0]
    assert item["id"] == v2["id"]
    assert item["revision"] == 2
    assert item["status"] == "draft"
    assert item["activeRevisionId"] == v1["id"]


def test_patch_rejects_content_edits():
    service = make_service()
    v1 = service.create_spec(payload())
    with pytest.raises(ApiError) as exc:
        service.patch_spec(v1["id"], {"status": "active",
                                      "rationale": "悄悄改内容"})
    assert code_of(exc) == "INVALID_SPEC"
