from __future__ import annotations

from replay import versioning as v


def payload(**overrides):
    base = dict(
        provider="deepseek", base_url="https://api.deepseek.com",
        model="deepseek-flash", temperature=0.2, top_p=None, max_tokens=200,
        thinking="disabled", reasoning_effort=None, prompt_style="lean",
        api_key_ref="AI_SERVER_API_KEY", dynamics_sha="abc",
    )
    base.update(overrides)
    return v.config_payload(**base)


def test_config_hash_is_stable_and_sensitive():
    first = v.config_hash(payload())
    assert first == v.config_hash(payload())
    assert first != v.config_hash(payload(model="pro"))
    assert first != v.config_hash(payload(dynamics_sha="def"))
    assert first != v.config_hash(payload(advice_version="older"))
    # engine commit 不在配置字段里：只做追溯，不应让所有版本失效
    assert "engine_commit" not in payload()


def test_table_dynamics_sha_and_empty():
    assert v.table_dynamics_sha("") is None
    assert v.table_dynamics_sha("abc") is not None


def test_run_blockers(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret")
    row = {
        "archived": 0, "name": "v1",
        "advice_version": v.CURRENT_ADVICE_VERSION,
        "reconstruct_version": v.CURRENT_RECONSTRUCT_VERSION,
        "api_key_ref": "MY_KEY",
    }
    assert v.run_blockers(row) == []
    assert v.run_blockers({**row, "archived": 1})[0]["code"] == "VERSION_ARCHIVED"
    assert v.run_blockers({**row, "advice_version": "old"})[0]["code"] == "VERSION_STALE"
    monkeypatch.delenv("MY_KEY")
    assert v.run_blockers(row)[0]["code"] == "AI_KEY_MISSING"


def test_public_version_never_returns_key(monkeypatch):
    monkeypatch.setenv("MY_KEY", "super-secret")
    row = {
        "id": 1, "name": "v1", "provider": "deepseek",
        "base_url": "https://api.deepseek.com", "model": "m",
        "api_key_ref": "MY_KEY", "temperature": 0.2, "top_p": None,
        "max_tokens": 200, "thinking": "disabled", "reasoning_effort": None,
        "prompt_style": "lean",
        "advice_version": v.CURRENT_ADVICE_VERSION,
        "reconstruct_version": v.CURRENT_RECONSTRUCT_VERSION,
        "engine_commit": "abc", "engine_dirty": 0,
        "table_dynamics": "x", "table_dynamics_sha": "sha",
        "config_hash": "hash", "notes": None, "archived": 0,
        "created_at": None,
    }
    data = v.public_version(row)
    assert data["hasKey"] is True
    assert "super-secret" not in str(data)
    assert data["tableDynamicsLength"] == 1
    assert data["runnable"] is True
