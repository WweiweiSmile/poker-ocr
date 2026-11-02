"""Prompt 捕获：Advice 必须保留真正发给模型的 system/user/raw。"""

from __future__ import annotations

import hashlib

from poker_ocr.advice import AISettings, advise, build_situation
from poker_ocr.hand_track import (
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds


def make_sit():
    snap = HandSnapshot(
        hand_id="capture-1", hand_index=0, street="flop",
        board=("Ks", "7d", "2c"), hero_cards=("Kh", "Qh"),
        hero_position="BTN", hero_stack=92.0, hero_bet=0.0,
        hero_invested=8.0, to_call=4.0, second_bet=None,
        pot=12.5, pot_before_street=8.0, table_size=6,
        blinds=Blinds(0.5, 1.0, None, "0.5/1.0"),
        opponents=(
            OpponentSnapshot("CO", "Villain", "CO", 100.0, 4.0, False),
            OpponentSnapshot("BB", None, "BB", 100.0, 1.0, False),
        ),
        streets=(
            StreetRecord("preflop", 1.5, (
                ActionRecord("preflop", "hero", None, "BTN", "raise", 8.0,
                             False, True),
                ActionRecord("preflop", "BB", None, "BB", "call", 8.0,
                             False, False),
            )),
            StreetRecord("flop", 8.0, (
                ActionRecord("flop", "CO", "Villain", "CO", "bet", 4.0,
                             False, False),
            )),
        ),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("capture-1", "flop", 3, 12.5, 4.0), warnings=(),
        bomb_pot=False, players_dealt=6,
    )
    return build_situation(snap, table_dynamics="")


def settings():
    return AISettings(api_key="test", base_url="http://example.invalid",
                      model="deepseek-flash", source="test")


def test_prompt_and_raw_are_captured():
    sit = make_sit()
    long_reason = "这是一段超过四十个字的完整理由。" * 4

    def complete(_settings, system, user, timeout=20.0):
        assert system and user
        return {"option": "call", "reason": long_reason,
                "confidence": 0.72,
                "_meta": {"usage": {"prompt_tokens": 321,
                                    "completion_tokens": 12},
                          "finish_reason": "stop"}}

    advice = advise(sit, settings(), complete=complete)
    assert advice.source == "model"
    assert advice.prompt_system
    assert advice.prompt_user
    expected = hashlib.sha256(
        (advice.prompt_system + "\n---\n" + advice.prompt_user).encode()
    ).hexdigest()
    assert advice.prompt_sha256 == expected
    assert advice.raw_response and '"option": "call"' in advice.raw_response
    assert advice.reason_full == long_reason
    assert len(advice.reason) <= 41
    assert advice.prompt_tokens == 321
    data = advice.to_dict()
    assert data["prompt_sha256"] == expected
    assert data["raw_response"]
    assert data["model_params"]["model"] == "deepseek-flash"
    assert data["facts"]["options"]


def test_no_settings_is_explicitly_missing():
    advice = advise(make_sit(), None)
    assert advice.source == "fallback"
    assert advice.prompt_missing is True
    assert advice.prompt_missing_reason == "no_settings"
    data = advice.to_dict()
    assert data["prompt_missing"] is True
    assert "prompt_system" not in data


def test_prompt_override_skips_render():
    sit = make_sit()
    seen = {}

    def complete(_settings, system, user, timeout=20.0):
        seen["system"] = system
        seen["user"] = user
        return {"option": "call", "reason": "ok"}

    advice = advise(sit, settings(), complete=complete,
                    prompt_override=("STORED SYSTEM", "STORED USER"))
    assert seen == {"system": "STORED SYSTEM", "user": "STORED USER"}
    assert advice.prompt_system == "STORED SYSTEM"
    assert advice.prompt_user == "STORED USER"
    expected = hashlib.sha256(b"STORED SYSTEM\n---\nSTORED USER").hexdigest()
    assert advice.prompt_sha256 == expected
