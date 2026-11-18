"""LLM judge（#58）：提示词逐条覆盖、判定归一化、evidence、可复现与 fail open。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from replay import judge, scoring


SPEC = {
    "required_points": [
        {"key": "position", "text": "说明自己在按钮位", "weight": 2},
        {"key": "range", "text": "引用对手的持续下注范围", "weight": 1},
    ],
    "forbidden_points": [
        {"key": "result", "text": "用结果倒推（我上次这么打输了）", "weight": 1},
    ],
    "forbidden_actions": ["fold"],
}

ADVICE = {
    "option_id": "o1",
    "kind": "call",
    "amount": None,
    "reason": "我在按钮位，对手持续下注范围偏宽，跟注即可。",
    "source": "model",
}

FACTS = {
    "options": [{"id": "o1", "kind": "call"}, {"id": "o2", "kind": "raise"}],
    "hero_position": "BTN",
    "pot": 6.0,
}


def _cfg(model="judge-pro"):
    return judge.judge_config(SimpleNamespace(judge_model=model,
                                             judge_base_url="https://j.example/v1",
                                             judge_api_key_ref="TEST_JUDGE_KEY"))


# ---------------- 版本与配置 ----------------

def test_judge_version_embeds_model_and_fits_column():
    version = judge.judge_version("deepseek-v4-pro")
    assert version == "llm-j1:deepseek-v4-pro"
    assert len(version) <= 32
    assert len(judge.judge_version("x" * 80)) <= 32


def test_judge_config_pins_temperature_zero_and_prompt_version():
    cfg = _cfg()
    assert cfg["temperature"] == 0.0
    assert cfg["promptVersion"] == judge.JUDGE_PROMPT_VERSION
    assert cfg["version"] == judge.judge_version("judge-pro")


def test_judge_blockers_flag_same_model_and_missing_key(monkeypatch):
    monkeypatch.delenv("TEST_JUDGE_KEY", raising=False)
    codes = {b["code"] for b in judge.judge_blockers(
        _cfg(), model_under_test="judge-pro")}
    assert "JUDGE_SAME_MODEL" in codes
    assert "JUDGE_KEY_MISSING" in codes
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-judge")
    codes = {b["code"] for b in judge.judge_blockers(
        _cfg(), model_under_test="deepseek-flash")}
    assert codes == set()


# ---------------- 提示词 ----------------

def test_build_messages_lists_every_point_and_forbids_made_up_quotes():
    system, user = judge.build_messages(SPEC, ADVICE, FACTS, ["o1", "o2"])
    for point in SPEC["required_points"] + SPEC["forbidden_points"]:
        assert point["text"] in user
        assert point["key"] in user
    # 命中必须摘 reason 原文；facts 里没有的引用必须标 missing。
    assert "reason" in system and "quote" in system
    assert "supported" in system and "missing" in system
    assert "hallucinated" in system
    assert ADVICE["reason"] in user


# ---------------- 判定归一化 ----------------

def test_parse_verdict_forces_miss_when_quote_not_in_reason():
    raw = {"requiredPoints": [
        {"key": "position", "status": "hit", "quote": "我在大盲位"},
    ]}
    out = judge.parse_verdict(raw, spec=SPEC, advice=ADVICE)
    by_key = {p["key"]: p for p in out["requiredPoints"]}
    assert by_key["position"]["status"] == "miss"
    assert by_key["position"]["quote"] == ""
    assert by_key["position"]["quoteFound"] is False
    assert out["matchedPoints"] == []


def test_parse_verdict_hits_only_points_with_matching_quote():
    raw = {"requiredPoints": [
        {"key": "position", "status": "hit", "quote": "我在按钮位"},
        {"key": "range", "status": "hit", "quote": "对手持续下注范围偏宽"},
    ]}
    out = judge.parse_verdict(raw, spec=SPEC, advice=ADVICE)
    assert out["matchedPoints"] == ["position", "range"]
    assert out["missingPoints"] == []


def test_parse_verdict_drops_unknown_keys_and_marks_unmentioned_miss():
    raw = {"requiredPoints": [
        {"key": "position", "status": "hit", "quote": "我在按钮位"},
        {"key": "not-in-spec", "status": "hit", "quote": "我在按钮位"},
    ]}
    out = judge.parse_verdict(raw, spec=SPEC, advice=ADVICE)
    assert [p["key"] for p in out["requiredPoints"]] == ["position", "range"]
    assert out["missingPoints"] == ["range"]


def test_hallucination_requires_a_missing_cited_fact():
    raw = {"hallucinated": True}
    assert judge.parse_verdict(raw, spec=SPEC, advice=ADVICE)["hallucinated"] is False
    raw = {"hallucinated": True, "citedFacts": [
        {"quote": "我在按钮位", "status": "missing", "fact": "hero_position"},
        {"quote": "对手持续下注范围偏宽", "status": "supported", "fact": "opp"},
    ]}
    out = judge.parse_verdict(raw, spec=SPEC, advice=ADVICE)
    assert out["hallucinated"] is True
    assert out["wrongFacts"] == [
        {"quote": "我在按钮位", "status": "missing", "fact": "hero_position"}]


def test_parse_verdict_detects_forbidden_point_quote():
    raw = {"forbiddenPoints": [
        {"key": "result", "status": "hit", "quote": "我上次这么打输了"}]}
    advice = {**ADVICE, "reason": ADVICE["reason"] + "我上次这么打输了。"}
    out = judge.parse_verdict(raw, spec=SPEC, advice=advice)
    assert out["forbiddenHits"] == ["result"]


# ---------------- evidence ----------------

def _verdict_with_evidence():
    return judge.parse_verdict({
        "requiredPoints": [{"key": "position", "status": "hit",
                            "quote": "我在按钮位"}],
        "forbiddenPoints": [{"key": "result", "status": "hit",
                             "quote": "我上次这么打输了"}],
        "citedFacts": [{"quote": "对手持续下注范围偏宽", "status": "missing",
                        "fact": "opponent_range"}],
        "hallucinated": True,
    }, spec=SPEC, advice={**ADVICE,
                          "reason": ADVICE["reason"] + "我上次这么打输了。"})


def test_evidence_covers_all_six_hard_fail_codes():
    verdict = _verdict_with_evidence()
    for code in scoring.HARD_FAIL_CODES:
        out = judge.evidence(code, verdict, advice=ADVICE, option_ids=["o1"])
        assert out["code"] == code
        assert out["hint"]
        assert out["optionId"] == "o1"
        assert out["optionIds"] == ["o1"]
    hallucination = judge.evidence(
        scoring.HF_HALLUCINATION, verdict, advice=ADVICE)
    assert hallucination["quotes"] == ["对手持续下注范围偏宽"]
    assert hallucination["missingFacts"] == ["opponent_range"]
    forbidden = judge.evidence(
        scoring.HF_FORBIDDEN_POINT, verdict, advice=ADVICE)
    assert forbidden["quotes"] == ["我上次这么打输了"]
    assert forbidden["forbiddenPoints"][0]["key"] == "result"
    action = judge.evidence(
        scoring.HF_FORBIDDEN_ACTION, verdict, advice=ADVICE)
    assert action["forbiddenActions"] == ["call"]
    error = judge.evidence(scoring.HF_ERROR, verdict, advice=ADVICE,
                           error="RuntimeError: boom")
    assert error["error"] == "RuntimeError: boom"
    assert judge.evidence(None, verdict, advice=ADVICE) == {}


# ---------------- 判定 + 打分 ----------------

def test_score_verdict_hallucination_is_hard_fail_and_capped():
    verdict = judge.parse_verdict({
        "requiredPoints": [{"key": "position", "status": "hit",
                            "quote": "我在按钮位"}],
        "citedFacts": [{"quote": "对手持续下注范围偏宽", "status": "missing",
                        "fact": "opponent_range"}],
        "hallucinated": True,
    }, spec=SPEC, advice=ADVICE)
    verdict["mode"] = judge.MODE_LLM
    result = judge.score_verdict(verdict, spec=SPEC, advice=ADVICE,
                                 option_ids=["o1", "o2"], pot=6.0)
    assert result["hardFail"] == scoring.HF_HALLUCINATION
    assert result["score"] <= scoring.HF_CAP


def test_score_verdict_forbidden_point_is_hard_fail():
    advice = {**ADVICE, "reason": ADVICE["reason"] + "我上次这么打输了。"}
    verdict = judge.parse_verdict({
        "forbiddenPoints": [{"key": "result", "status": "hit",
                             "quote": "我上次这么打输了"}],
    }, spec=SPEC, advice=advice)
    verdict["mode"] = judge.MODE_LLM
    result = judge.score_verdict(verdict, spec=SPEC, advice=advice,
                                 option_ids=["o1", "o2"])
    assert result["hardFail"] == scoring.HF_FORBIDDEN_POINT


def test_score_verdict_rules_mode_falls_back_to_action_support():
    fallback = judge.fallback_verdict(_cfg(), error="boom")
    result = judge.score_verdict(fallback, spec=SPEC, advice=ADVICE,
                                 option_ids=["o1", "o2"])
    assert result["hardFail"] is None
    assert result["scores"]["reason"] is not None


# ---------------- 可复现 / fail open ----------------

def _stub_model(monkeypatch, reply):
    import poker_ocr.advice as advice_mod

    calls = []

    def fake(settings, system, user, timeout=None):
        calls.append((system, user))
        return dict(reply)

    monkeypatch.setattr(advice_mod, "complete_json", fake)
    return calls


def test_evaluate_twice_with_same_input_is_byte_identical(monkeypatch):
    reply = {
        "requiredPoints": [
            {"key": "position", "status": "hit", "quote": "我在按钮位"},
            {"key": "range", "status": "miss", "quote": ""},
        ],
        "forbiddenPoints": [{"key": "result", "status": "clean", "quote": ""}],
        "citedFacts": [], "hallucinated": False,
    }
    _stub_model(monkeypatch, reply)
    cfg = _cfg()
    first = judge.evaluate(cfg, settings=object(), spec=SPEC, advice=ADVICE,
                           facts=FACTS, option_ids=["o1", "o2"])
    second = judge.evaluate(cfg, settings=object(), spec=SPEC, advice=ADVICE,
                            facts=FACTS, option_ids=["o1", "o2"])
    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                          sort_keys=True)
    assert first["temperature"] == 0.0
    assert first["inputDigest"] == second["inputDigest"]
    assert first["mode"] == judge.MODE_LLM
    assert judge.compare_verdict(first, second)["consistent"] is True


def test_evaluate_fails_open_without_faking_hard_fail(monkeypatch):
    import poker_ocr.advice as advice_mod

    def boom(*args, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(advice_mod, "complete_json", boom)
    out = judge.evaluate(_cfg(), settings=object(), spec=SPEC, advice=ADVICE,
                         facts=FACTS, option_ids=["o1"])
    assert out["mode"] == judge.MODE_RULES
    assert out["status"] == judge.STATUS_ERROR
    assert "upstream down" in out["error"]
    result = judge.score_verdict(out, spec=SPEC, advice=ADVICE,
                                 option_ids=["o1"])
    assert result["hardFail"] is None


def test_evaluate_without_settings_marks_unavailable(monkeypatch):
    import poker_ocr.advice as advice_mod
    monkeypatch.setattr(advice_mod, "complete_json",
                        lambda *a, **k: pytest.fail("不该调用模型"))
    out = judge.evaluate(_cfg(), settings=None, spec=SPEC, advice=ADVICE,
                         facts=FACTS, option_ids=["o1"])
    assert out["status"] == judge.STATUS_UNAVAILABLE
    assert out["mode"] == judge.MODE_RULES


def test_compare_verdict_reports_status_changes():
    before = judge.parse_verdict({"requiredPoints": [
        {"key": "position", "status": "hit", "quote": "我在按钮位"}]},
        spec=SPEC, advice=ADVICE)
    after = judge.parse_verdict({"requiredPoints": [
        {"key": "position", "status": "miss", "quote": ""}]},
        spec=SPEC, advice=ADVICE)
    diff = judge.compare_verdict(before, after)
    assert diff["consistent"] is False
    assert any(d["key"] == "position" for d in diff["diffs"])


def test_input_digest_changes_when_advice_changes():
    a = judge.input_digest(SPEC, ADVICE, FACTS, ["o1"])
    b = judge.input_digest(SPEC, {**ADVICE, "reason": "别的理由"}, FACTS, ["o1"])
    assert a != b and len(a) == 16

# ─────────────── 复核补强（#58 code-review 后） ───────────────

def test_judge_version_stays_unique_for_long_models():
    a = judge.judge_version("provider-a/super-long-model-name-2026-11")
    b = judge.judge_version("provider-b/super-long-model-name-2026-11")
    assert len(a) <= 32 and len(b) <= 32
    assert a != b
    assert a.startswith("llm-j1:")


def test_clip_facts_keeps_valid_json_and_names_dropped_keys():
    facts = {
        "options": [{"id": "o1", "kind": "call"}],
        "board": "AhKd7c2s9h" * 300,
        "notes": "x" * 9000,
        "pot": 6.0,
    }
    clipped = judge._clip_facts(facts)
    text = json.dumps(clipped, ensure_ascii=False)
    assert len(text) <= judge.FACTS_CHAR_LIMIT
    assert "truncatedKeys" in clipped
    assert "options" in clipped  # 判定锚点最后才丢
    assert json.loads(text)["pot"] == 6.0


def test_build_messages_never_cuts_facts_json_in_half():
    facts = dict(FACTS, board="AhKd7c2s9h" * 900)
    _system, user = judge.build_messages(SPEC, ADVICE, facts, ["o1", "o2"])
    payload = user.split("【facts】", 1)[1].strip()
    parsed = json.loads(payload)
    assert parsed["truncatedKeys"]
    assert parsed["options"][0]["id"] == "o1"


def test_error_judge_json_carries_hf_error_evidence():
    data = judge.error_judge_json("案例冻结的 spec #9 已不存在")
    assert data["status"] == judge.STATUS_ERROR
    assert data["mode"] == judge.MODE_RULES
    assert data["evidence"]["code"] == scoring.HF_ERROR
    assert "spec #9" in data["evidence"]["error"]
    json.dumps(data, ensure_ascii=False)


def test_evidence_forbidden_action_names_the_banned_action():
    verdict = {"requiredPoints": [], "citedFacts": [], "hallucinated": False}
    data = judge.evidence(scoring.HF_FORBIDDEN_ACTION, verdict,
                          advice=dict(ADVICE, kind="fold"),
                          option_ids=["o1"], spec=SPEC)
    assert data["forbiddenActions"] == ["fold"]
    # 没给 spec 时退化成实际动作，仍能看出违反了哪一步
    fallback = judge.evidence(scoring.HF_FORBIDDEN_ACTION, verdict,
                              advice=dict(ADVICE, kind="fold"))
    assert fallback["forbiddenActions"] == ["fold"]


def test_judge_timeout_env_fails_fast_on_garbage(monkeypatch):
    from replay.config import Settings

    monkeypatch.setenv("REPLAY_JUDGE_TIMEOUT", "abc")
    with pytest.raises(ValueError, match="REPLAY_JUDGE_TIMEOUT"):
        Settings.from_env(load=False)
    monkeypatch.setenv("REPLAY_JUDGE_TIMEOUT", "0")
    with pytest.raises(ValueError, match="REPLAY_JUDGE_TIMEOUT"):
        Settings.from_env(load=False)

def test_judge_ai_settings_are_temperature_zero(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-judge")
    cfg = _cfg()
    cfg["apiKeyRef"] = "TEST_JUDGE_KEY"
    settings = judge.build_settings(cfg)
    assert settings.temperature == 0.0
    assert settings.model == "judge-pro"
    assert settings.base_url == "https://j.example/v1"
