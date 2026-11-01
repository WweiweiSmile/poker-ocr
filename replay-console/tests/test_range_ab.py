"""范围模型 A/B 只读视图：清单加载与缺文件降级。"""

from __future__ import annotations

from replay import range_ab


def test_manifest_loads() -> None:
    data = range_ab.load()
    assert data["available"] is True, data.get("error")
    cases = data["cases"]
    assert cases, "清单应至少有一条用例"
    assert data["summary"]["total"] == len(cases)
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "用例 id 不能重复"
    assert any(case["status"] == "ready" for case in cases)


def test_missing_manifest_fails_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REPLAY_RANGE_AB_SET", str(tmp_path / "missing.json"))
    data = range_ab.load()
    assert data["available"] is False
    assert "找不到" in data["error"]
    assert data["hint"]
