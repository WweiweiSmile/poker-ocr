"""LLM judge（#58）：提示词逐条覆盖、判定归一化、evidence、可复现与 fail open。"""
from __future__ import annotations

import json
from pathlib import Path
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
    assert version == "llm-j2:deepseek-v4-pro"
    assert len(version) <= 32
    assert len(judge.judge_version("x" * 80)) <= 32


def test_judge_config_pins_temperature_zero_and_prompt_version():
    cfg = _cfg()
    assert cfg["temperature"] == 0.0
    assert cfg["promptVersion"] == judge.JUDGE_PROMPT_VERSION
    assert cfg["version"] == judge.judge_version("judge-pro")


def test_judge_blockers_allow_same_model_but_flag_missing_key(monkeypatch):
    # #74：判官与被测同模型不再拦截，只保留缺 key 这类真实不可用。
    monkeypatch.delenv("TEST_JUDGE_KEY", raising=False)
    codes = {b["code"] for b in judge.judge_blockers(
        _cfg(), model_under_test="judge-pro")}
    assert "JUDGE_SAME_MODEL" not in codes
    assert "JUDGE_KEY_MISSING" in codes
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-judge")
    codes = {b["code"] for b in judge.judge_blockers(
        _cfg(), model_under_test="judge-pro")}
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
    assert "contradicted" in system
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
    assert out["degraded"] == "judge_error"
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
    assert a.startswith("llm-j2:")


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
    payload = user.split("【facts】", 1)[1].strip().splitlines()[-1]
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

# ─────────────── #73：judge 输入对齐模型实际看到的 prompt ───────────────

# 真实记录：run 8 / case 22（BTN 河牌明三条跟注）/ repeat 0，2026-11-18 取自
# call_game_dev 的 replay_run_items（prompt_json / facts_json / reason / judge_json）。
FIXTURE = Path(__file__).parent / "fixtures" / "run8_case22.json"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_build_messages_includes_model_prompt_as_authority():
    prompt = ("## 当前局面\n赔率门槛：63BB / 可争夺底池 261BB = 24.1%\n"
              "## 行动线\n河牌：midR 下注 63BB")
    system, user = judge.build_messages(SPEC, ADVICE, FACTS, ["o1", "o2"],
                                        model_prompt=prompt)
    assert "【模型当时看到的输入" in user
    assert prompt in user
    assert "权威事实源" in user
    assert "以它为准" in user and "以 prompt 原文为准" in system
    assert user.index("【模型当时看到的输入") < user.index("【facts】")


def test_build_messages_omits_prompt_section_when_absent():
    system, user = judge.build_messages(SPEC, ADVICE, FACTS, ["o1", "o2"])
    assert "【模型当时看到的输入" not in user
    assert "没有留存模型输入" in system


def test_build_messages_clips_long_prompt_with_visible_marker():
    prompt = "x" * (judge.PROMPT_CHAR_LIMIT + 500)
    _system, user = judge.build_messages(SPEC, ADVICE, FACTS, ["o1"],
                                         model_prompt=prompt)
    section = user.split("【模型当时看到的输入（权威事实源）】", 1)[1]
    section = section.split("【facts】", 1)[0]
    assert len(section) < judge.PROMPT_CHAR_LIMIT + 200
    assert "已截断" in section and "不要据此判 missing" in section


def test_parse_verdict_contradicted_is_wrong_fact_not_hallucination():
    advice = {**ADVICE, "reason": "对手范围权益 80%，跟注。"}
    raw = {"citedFacts": [
        {"quote": "权益 80%", "status": "contradicted", "fact": "equity"}],
        "hallucinated": True}
    out = judge.parse_verdict(raw, spec=SPEC, advice=advice)
    assert out["citedFacts"][0]["status"] == "contradicted"
    assert out["wrongFacts"] == [
        {"quote": "权益 80%", "status": "contradicted", "fact": "equity"}]
    assert out["hallucinated"] is False


def test_parse_verdict_missing_plus_contradicted_keeps_hallucination():
    advice = {**ADVICE, "reason": "对手范围权益 80%，门槛 24% 也不够，跟注。"}
    raw = {"citedFacts": [
        {"quote": "权益 80%", "status": "contradicted", "fact": "equity"},
        {"quote": "门槛 24%", "status": "missing", "fact": "odds"}],
        "hallucinated": True}
    out = judge.parse_verdict(raw, spec=SPEC, advice=advice)
    assert out["hallucinated"] is True
    assert [f["status"] for f in out["wrongFacts"]] == [
        "missing", "contradicted"]


def test_score_verdict_contradicted_lowers_fact_score_without_hard_fail():
    advice = {**ADVICE, "reason": "对手范围权益 80%，跟注。"}
    verdict = judge.parse_verdict({"citedFacts": [
        {"quote": "权益 80%", "status": "contradicted", "fact": "equity"}]},
        spec=SPEC, advice=advice)
    verdict["mode"] = judge.MODE_LLM
    result = judge.score_verdict(verdict, spec=SPEC, advice=advice,
                                 option_ids=["o1", "o2"], pot=6.0)
    assert result["hardFail"] is None
    assert result["scores"]["fact"] == 0.0


def test_evaluate_without_prompt_clears_hallucination_fail_open(monkeypatch):
    reply = {
        "requiredPoints": [{"key": "position", "status": "hit",
                            "quote": "我在按钮位"}],
        "citedFacts": [{"quote": "对手持续下注范围偏宽", "status": "missing",
                        "fact": "opponent_range"}],
        "hallucinated": True,
    }
    _stub_model(monkeypatch, reply)
    out = judge.evaluate(_cfg(), settings=object(), spec=SPEC, advice=ADVICE,
                         facts=FACTS, option_ids=["o1", "o2"])
    assert out["inputPrompt"] is False
    assert out["degraded"] == "prompt_missing"
    assert out["citedFacts"] == [] and out["wrongFacts"] == []
    assert out["hallucinated"] is False
    assert out["matchedPoints"] == ["position"]
    assert judge.score_verdict(out, spec=SPEC, advice=ADVICE,
                               option_ids=["o1", "o2"])["hardFail"] is None


def test_evaluate_passes_prompt_to_model_and_digest(monkeypatch):
    reply = {"requiredPoints": [], "forbiddenPoints": [], "citedFacts": [],
             "hallucinated": False}
    calls = _stub_model(monkeypatch, reply)
    prompt = "## 当前局面\n赔率门槛：63BB / 可争夺底池 261BB = 24.1%"
    out = judge.evaluate(_cfg(), settings=object(), spec=SPEC, advice=ADVICE,
                         facts=FACTS, option_ids=["o1", "o2"],
                         model_prompt=prompt, model_prompt_sha="sha-1")
    assert out["inputPrompt"] is True and out["degraded"] == ""
    assert prompt in calls[0][1]
    without = judge.evaluate(_cfg(), settings=object(), spec=SPEC,
                             advice=ADVICE, facts=FACTS,
                             option_ids=["o1", "o2"])
    assert out["inputDigest"] != without["inputDigest"]


def test_evaluate_prompt_present_call_failure_keeps_prompt_flag(monkeypatch):
    import poker_ocr.advice as advice_mod

    def boom(*args, **kwargs):
        raise RuntimeError("judge down")

    monkeypatch.setattr(advice_mod, "complete_json", boom)
    out = judge.evaluate(_cfg(), settings=object(), spec=SPEC, advice=ADVICE,
                         facts=FACTS, option_ids=["o1"],
                         model_prompt="## 当前局面\n底池 198BB")
    assert out["status"] == judge.STATUS_ERROR
    assert out["inputPrompt"] is True
    assert out["degraded"] == "judge_error"


def test_input_digest_changes_when_prompt_changes():
    a = judge.input_digest(SPEC, ADVICE, FACTS, ["o1"], model_prompt="A")
    b = judge.input_digest(SPEC, ADVICE, FACTS, ["o1"], model_prompt="B")
    assert a != b and len(a) == 16


def test_fixture_run8_case22_prompt_reaches_judge_and_absence_fails_open(
        monkeypatch):
    data = _fixture()
    prompt = data["prompt_json"]["user"]
    facts = data["facts_json"]
    advice = {"option_id": "call", "kind": "call", "amount": 63,
              "reason": data["reason"], "source": "model"}
    spec = {"required_points": [], "forbidden_points": [],
            "forbidden_actions": ["fold"]}
    reply = {"requiredPoints": [], "forbiddenPoints": [], "citedFacts": [
        {"quote": "24%门槛", "status": "missing", "fact": "odds"}],
        "hallucinated": True}
    calls = _stub_model(monkeypatch, reply)

    with_prompt = judge.evaluate(_cfg(), settings=object(), spec=spec,
                                 advice=advice, facts=facts,
                                 model_prompt=prompt,
                                 model_prompt_sha=data["prompt_json"]["sha"])
    assert "63BB / 可争夺底池 261BB = 24.1%" in calls[0][1]
    assert "## 行动线" in calls[0][1]
    assert with_prompt["inputPrompt"] is True

    without_prompt = judge.evaluate(_cfg(), settings=object(), spec=spec,
                                    advice=advice, facts=facts)
    assert without_prompt["hallucinated"] is False
    assert without_prompt["citedFacts"] == []
    assert judge.score_verdict(without_prompt, spec=spec, advice=advice,
                               option_ids=["call", "fold"])["hardFail"] is None


def test_run_worker_passes_stored_prompt_to_judge(monkeypatch):
    from replay import run_worker

    captured = []

    def fake_evaluate(cfg, **kwargs):
        captured.append(kwargs)
        return judge.fallback_verdict(cfg)

    class _Items:
        def __init__(self):
            self.written = []

        def upsert(self, **kwargs):
            self.written.append(kwargs)

    worker = SimpleNamespace(
        settings=SimpleNamespace(model_timeout=1.0, transient_retries=0,
                                 store_prompts=True),
        limiter=None, items=_Items())
    data = _fixture()
    prompt_user = data["prompt_json"]["user"]
    payload = {
        "facts_json": json.dumps(FACTS), "option_id": "o1",
        "option_kind": "call", "option_amount": None,
        "reason_full": ADVICE["reason"], "source": "model",
        "prompt_user": prompt_user,
        "prompt_sha256": data["prompt_json"]["sha"],
    }
    unit = {"case": {"id": 3}, "decision": SimpleNamespace(pot=6.0),
            "spec": SPEC, "specId": 11, "repeat": 0}

    monkeypatch.setattr(run_worker, "execute_unit",
                        lambda **kwargs: SimpleNamespace(payload=payload))
    monkeypatch.setattr(run_worker.judge, "evaluate", fake_evaluate)
    ok = run_worker.RunWorker._run_unit(worker, {"id": 1}, unit, object(),
                                        {"model": "deepseek-flash"}, [11],
                                        {"model": "judge-pro"}, object())
    assert ok is True
    assert captured[0]["model_prompt"] == prompt_user
    assert "63BB / 可争夺底池 261BB = 24.1%" in captured[0]["model_prompt"]
    assert "## 行动线" in captured[0]["model_prompt"]
    assert captured[0]["model_prompt_sha"] == data["prompt_json"]["sha"]
    stored = json.loads(worker.items.written[0]["item"]["promptJson"])
    assert stored["user"] == prompt_user
