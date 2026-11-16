"""提示词 golden test。

改了系统/用户提示词、事实块或选项生成规则却没 bump ADVICE_VERSION 时，
这个测试会失败。更新提示词后请同时：
  1. bump replay/versioning.py 的 CURRENT_ADVICE_VERSION
  2. 更新下面的 EXPECTED_PROMPT_HASH（改动只落在 3-bet 池的翻后局面时，
     改的是 EXPECTED_THREE_BET_PROMPT_HASH——那个 fixture 才会渲染
     `_THREE_BET`；单加注翻后的 fixture 对它看不见）
"""

from __future__ import annotations

import hashlib

from poker_ocr.advice import build_messages, build_situation
from poker_ocr.hand_track import (
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds

from replay.versioning import CURRENT_ADVICE_VERSION

EXPECTED_ADVICE_VERSION = "advice-2026-11-16.2"
EXPECTED_PROMPT_HASH = "0e977f0f529b80f294c83f059af19577eeea3b8982610ea621795c5fea8b424e"
EXPECTED_THREE_BET_PROMPT_HASH = "90556a6ba17d9c49d46eb2040c40132e5fbb8e54d6059d3d5e885457da96cc06"

# 快照没有可用画像，所以「对手画像」段只有标题 + 缺口说明；
# witness 数据落地前这一段必须逐字节不变（spec §15.1）。
EXPECTED_PROFILE_SEGMENT = (
    "## 对手画像（逐座位；Hero 下注后行动顺序；优先于池子先验/全场先验）\n"
    "（本手在局对手都没有可用画像）\n"
    "无画像：CO、BB（按未知对手处理，不伪造数据）"
)


def _golden_snapshot() -> HandSnapshot:
    return HandSnapshot(
        hand_id="golden", hand_index=0, street="flop",
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
                ActionRecord("preflop", "hero", None, "BTN", "raise", 8.0, False, True),
                ActionRecord("preflop", "BB", None, "BB", "call", 8.0, False, False),
            )),
            StreetRecord("flop", 8.0, (
                ActionRecord("flop", "CO", "Villain", "CO", "bet", 4.0, False, False),
            )),
        ),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("golden", "flop", 0, 12.5, 4.0), warnings=(),
        bomb_pot=False, players_dealt=6,
    )


def test_prompt_golden_is_tied_to_advice_version():
    assert CURRENT_ADVICE_VERSION == EXPECTED_ADVICE_VERSION
    snap = _golden_snapshot()
    sit = build_situation(snap, table_dynamics="")
    system, user = build_messages(sit)
    digest = hashlib.sha256((system + "\n---\n" + user).encode()).hexdigest()
    assert digest == EXPECTED_PROMPT_HASH, (
        "提示词内容变了。若这是有意的，请 bump CURRENT_ADVICE_VERSION 并更新 "
        "EXPECTED_PROMPT_HASH；否则说明当前版本语义已经漂移。")


def test_profile_segment_is_byte_stable_without_witness():
    """无 witness 数据时，「对手画像」段必须逐字节不变（spec §15.1 第 1 条）。"""
    sit = build_situation(_golden_snapshot(), table_dynamics="")
    _, user = build_messages(sit)
    start = user.index("## 对手画像")
    end = user.index("\n\n## 数据质量", start)
    assert user[start:end] == EXPECTED_PROFILE_SEGMENT, (
        "画像段变了。witness 数据落地前这一段不应有任何变化（含图例行/缩进）。")
    assert "摊牌证据" not in user


def _three_bet_snapshot() -> HandSnapshot:
    """3-bet 池的翻后局面：只有这个 fixture 会渲染 3-bet+ 场景段。

    单加注翻后那个 fixture 不渲染 `_THREE_BET`（场景段要求 pot_type 是
    3-bet+ 池），所以 2026-11-15 的加宽（#40）改了提示词而
    `EXPECTED_PROMPT_HASH` 不变——golden 对这种改动是瞎的。这里补一个
    3-bet 池的翻后快照，把加宽后的口径钉成字节。
    """
    return HandSnapshot(
        hand_id="golden-3bet", hand_index=0, street="flop",
        board=("Kd", "7c", "2s"), hero_cards=("Kh", "Ad"), hero_position="UTG",
        hero_stack=64.0, hero_bet=0.0, hero_invested=12.0, to_call=9.0,
        second_bet=None, pot=45.0, pot_before_street=36.0, table_size=6,
        blinds=Blinds(0.5, 1.0, None, "0.5/1.0"),
        opponents=(
            OpponentSnapshot("CO", "Villain", "CO", 88.0, 9.0, False),
            OpponentSnapshot("BB", None, "BB", 100.0, 0.0, False),
        ),
        streets=(
            StreetRecord("preflop", 1.5, (
                ActionRecord("preflop", "hero", None, "UTG", "raise", 3.0, False, True),
                ActionRecord("preflop", "CO", "Villain", "CO", "raise", 12.0, False, False),
                ActionRecord("preflop", "hero", None, "UTG", "call", 12.0, False, True),
            )),
            StreetRecord("flop", 36.0, (
                ActionRecord("flop", "CO", "Villain", "CO", "bet", 9.0, False, False),
            )),
        ),
        preflop_shapes=("three-bet",), hero_is_pfr=False, cbet_spot=False,
        decision_key=("golden-3bet", "flop", 0, 45.0, 9.0),
        warnings=(), bomb_pot=False, players_dealt=6,
    )


def test_three_bet_prompt_golden_tracks_prior_width():
    sit = build_situation(_three_bet_snapshot(), table_dynamics="")
    system, user = build_messages(sit)
    digest = hashlib.sha256((system + "\n---\n" + user).encode()).hexdigest()
    assert "## 场景：3-bet+ 池" in system, "fixture 必须真的渲染 3-bet 场景段"
    assert "22+" in system and "QQ+/AK" not in system, (
        "3-bet 场景段要与加宽后的先验表一致（#40）；改了就要 bump 版本并更新下面的哈希")
    assert "同花连张" in system, "场景段的诈唬类别要带上同花连张（2026-11-15.2）"
    assert digest == EXPECTED_THREE_BET_PROMPT_HASH, (
        "3-bet 提示词内容变了。若这是有意的，请 bump CURRENT_ADVICE_VERSION 并更新 "
        "EXPECTED_THREE_BET_PROMPT_HASH；否则说明当前版本语义已经漂移。")
