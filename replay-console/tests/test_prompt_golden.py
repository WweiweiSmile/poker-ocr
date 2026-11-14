"""提示词 golden test。

改了系统/用户提示词、事实块或选项生成规则却没 bump ADVICE_VERSION 时，
这个测试会失败。更新提示词后请同时：
  1. bump replay/versioning.py 的 CURRENT_ADVICE_VERSION
  2. 更新下面的 EXPECTED_PROMPT_HASH
"""

from __future__ import annotations

import hashlib

from poker_ocr.advice import build_messages, build_situation
from poker_ocr.hand_track import (
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds

from replay.versioning import CURRENT_ADVICE_VERSION

EXPECTED_ADVICE_VERSION = "advice-2026-11-14.1"
EXPECTED_PROMPT_HASH = "5acf3bc972653d5186f806c64e1b313310ef3111e5979b48e68c002abd7365e7"

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
