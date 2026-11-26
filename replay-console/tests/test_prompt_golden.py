"""提示词 golden test：钉住「事实 + 书计划 + 偏移 + options」契约（#81）。

改了 system / 事实块 / 书计划 / 选项规则却没 bump 版本时，这个测试会失败。
更新提示词后请同时：
  1. bump `poker_ocr/prompt/version.py` 的 PROMPT_VERSION（并在顶部写依据）；
     `replay/versioning.py` 的 CURRENT_ADVICE_VERSION 由它派生，**不要手改**
  2. 更新下面的 EXPECTED_PROMPT_HASH / EXPECTED_THREE_BET_PROMPT_HASH /
     EXPECTED_AIR_PROMPT_HASH

#81（2026-11-19.4）把旧策略段整体删掉：aggression / hand_type / scenario /
ante / metrics / arbitration / decision / stack 八段 system、attack / exploit /
pool / rules 四段 user、翻前范围结论与隔离尺寸都不再渲染。这里除了三个 hash，
还显式钉住「旧段不会回来」「书计划没被挤掉」「无匹配桌况时不出现偏移」。
旧 LGB 台账文件仍在仓库里归档（`poker_ocr/rules/ledger/`），运行时不读。
"""

from __future__ import annotations

import hashlib
import re

from poker_ocr.advice import build_messages, build_situation
from poker_ocr.hand_track import (
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds
from poker_ocr.prompt.user.offsets import (
    OFFSET_TARGETS, OffsetDelta, plan_offsets, render_offsets_zh,
)

from replay.versioning import CURRENT_ADVICE_VERSION

EXPECTED_ADVICE_VERSION = "advice-2026-11-26.1"
EXPECTED_PROMPT_HASH = "507a07078a30dfe40ee9402317aed6f2dc3d50fcd3fc7759c6482727416cc67b"
EXPECTED_THREE_BET_PROMPT_HASH = "741b499242462e68cfc73668bbbc9aeb9975df97f273cc8732444435641aa52d"
EXPECTED_AIR_PROMPT_HASH = "d670ac09e9b1e935669aac7cb6dd688bc2c38652408ad9d54fb5710d932f00b0"

# 切换前的总字数（system+user），#81 的验收要求「低于切换前」。
LEGACY_TOTALS = {
    "_golden_snapshot": 7760,
    "_three_bet_snapshot": 7972,
    "_air_snapshot": 7318,
}

# 旧策略段的段标题：任何一个回来都说明这次不是原子切换。
LEGACY_HEADERS = (
    "## 规则仲裁",
    "## 决策顺序",
    "## 指标口径",
    "## 筹码深度",
    "## 前注局",
    "## 牌型",
    "## 场景：",
    "## 攻击机会",
    "## 剥削判断",
    "## 范围弱点与弃牌率",
    "## 池子先验",
    "## 规则",
)

# 快照没有可用画像，所以「对手画像」段只有标题 + 缺口说明；
# witness 数据落地前这一段必须逐字节不变（spec §15.1）。
EXPECTED_PROFILE_SEGMENT = (
    "## 对手画像（逐座位；Hero 下注后行动顺序）\n"
    "（本手在局对手都没有可用画像）\n"
    "无画像：CO、BB（按未知对手处理，不伪造数据）"
)

OPTION_LINE = re.compile(r"^- (\S+)｜")


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


def _digest(system: str, user: str) -> str:
    return hashlib.sha256((system + "\n---\n" + user).encode()).hexdigest()


def _three_bet_snapshot() -> HandSnapshot:
    """3-bet 池的翻后局面：第二个 hash fixture（场景段已删，仍留作覆盖）。"""
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


def _air_snapshot() -> HandSnapshot:
    """单高张翻牌 miss、没人下注：第三个 hash fixture（牌型段已删）。"""
    return HandSnapshot(
        hand_id="golden-air", hand_index=0, street="flop",
        board=("Kh", "8c", "2d"), hero_cards=("Ad", "Qc"),
        hero_position="BTN", hero_stack=92.0, hero_bet=0.0,
        hero_invested=8.0, to_call=0.0, second_bet=None,
        pot=17.5, pot_before_street=17.0, table_size=6,
        blinds=Blinds(0.5, 1.0, None, "0.5/1.0"),
        opponents=(
            OpponentSnapshot("CO", None, "CO", 100.0, 8.0, False),
            OpponentSnapshot("BB", None, "BB", 100.0, 8.0, False),
        ),
        streets=(
            StreetRecord("preflop", 1.5, (
                ActionRecord("preflop", "hero", None, "BTN", "raise", 8.0, False, True),
                ActionRecord("preflop", "CO", None, "CO", "call", 8.0, False, False),
                ActionRecord("preflop", "BB", None, "BB", "call", 8.0, False, False),
            )),
            StreetRecord("flop", 17.0, (
                ActionRecord("flop", "CO", None, "CO", "check", 0.0, False, False),
                ActionRecord("flop", "BB", None, "BB", "check", 0.0, False, False),
            )),
        ),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=True,
        decision_key=("golden-air", "flop", 0, 17.5, 0.0), warnings=(),
        bomb_pot=False, players_dealt=6,
    )


def _multiway_set_snapshot() -> HandSnapshot:
    """4 人池、KK 击中暗三、没人下注：书里有页可命中的强成牌节点。"""
    return HandSnapshot(
        hand_id="golden-book", hand_index=0, street="flop",
        board=("Ks", "7d", "2c"), hero_cards=("Kh", "Kd"),
        hero_position="BTN", hero_stack=100.0, hero_bet=0.0,
        hero_invested=4.0, to_call=0.0, second_bet=None,
        pot=10.0, pot_before_street=9.0, table_size=4,
        blinds=Blinds(0.5, 1.0, None, "0.5/1.0"),
        opponents=tuple(
            OpponentSnapshot(f"p{i}", None, f"p{i}", 100.0, 0.0, False)
            for i in range(3)),
        streets=(
            StreetRecord("preflop", 1.5, (
                ActionRecord("preflop", "hero", None, "BTN", "raise", 4.0,
                             False, True),
            )),
            StreetRecord("flop", 9.0, (
                ActionRecord("flop", "hero", None, "BTN", "check", 0.0,
                             False, True),
            )),
        ),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("golden-book", "flop", 0, 10.0, 0.0), warnings=(),
        bomb_pot=False, players_dealt=4,
    )


def test_prompt_golden_is_tied_to_advice_version():
    assert CURRENT_ADVICE_VERSION == EXPECTED_ADVICE_VERSION
    system, user = build_messages(
        build_situation(_golden_snapshot(), table_dynamics=""))
    assert _digest(system, user) == EXPECTED_PROMPT_HASH, (
        "提示词内容变了。若这是有意的，请 bump PROMPT_VERSION（advice 版本由它派生）"
        "并更新 EXPECTED_PROMPT_HASH；"
        "否则说明当前版本语义已经漂移。")


def test_profile_segment_is_byte_stable_without_witness():
    """无 witness 数据时，「对手画像」段必须逐字节不变（spec §15.1 第 1 条）。"""
    _, user = build_messages(
        build_situation(_golden_snapshot(), table_dynamics=""))
    start = user.index("## 对手画像")
    end = user.index("\n\n## 数据质量", start)
    assert user[start:end] == EXPECTED_PROFILE_SEGMENT, (
        "画像段变了。witness 数据落地前这一段不应有任何变化（含图例行/缩进）。")
    assert "摊牌证据" not in user


def test_legacy_strategy_sections_do_not_come_back():
    """#81：旧策略段既不在 system，也不在 user。"""
    for snap in (_golden_snapshot(), _three_bet_snapshot(), _air_snapshot()):
        system, user = build_messages(
            build_situation(snap, table_dynamics=""))
        both = system + "\n" + user
        for header in LEGACY_HEADERS:
            assert header not in both, f"旧策略段回来了：{header}"
        for phrase in ("翻前范围结论", "limp 例外", "隔离尺寸", "默认弃牌",
                       "范围宽度", "被支配结构"):
            assert phrase not in both, f"旧建议文案回来了：{phrase}"


def test_contract_sections_are_present_and_ordered():
    """新契约：system 四段，user 书计划在牌力之后、options 在最后。"""
    system, user = build_messages(
        build_situation(_golden_snapshot(), table_dynamics=""))
    assert system.startswith("你是一名德州扑克现金局实时教练"), "system 第一段是角色"
    assert "## 决策依据" in system
    assert "## 事实优先级" in system
    assert "## 输出" in system
    assert '{"option": "<id>", "reason"' in system
    assert "严格来自「可选动作」" in system
    assert "## 书中计划（Harrington 现金桌）" in user
    assert "## 可选动作" in user
    assert user.rstrip().split("\n\n")[-1].startswith("## 可选动作"), (
        "可选动作必须是最后一段（模型最后读到的是能选什么）")
    assert user.index("## 实时牌力") < user.index("## 书中计划")
    assert user.index("## 书中计划") < user.index("## 可选动作")


ALLOWED_HEADERS = (
    "## 决策依据", "## 事实优先级", "## 输出",
    "## 当前局面", "## 行动线", "## 上一街我的建议", "## 实时牌力",
    "## 书中计划", "## 偏移", "## 对手画像", "## 人工观察",
    "## 数据质量", "## 可选动作",
)


def test_only_whitelisted_sections_can_appear():
    """AC ②：任何 fixture（含带上一街建议的）都只能出现白名单里的段。"""
    from dataclasses import replace
    from types import SimpleNamespace

    sit = build_situation(_golden_snapshot(), table_dynamics="")
    previous = SimpleNamespace(
        street="preflop",
        option=SimpleNamespace(label="开池到 5.5BB", id="raise_5.5", amount=5.5),
        reason="位置有利", plan="")
    for candidate in (sit, replace(sit, previous=previous)):
        system, user = build_messages(candidate)
        headers = [ln for ln in (system + "\n" + user).splitlines()
                   if ln.startswith("## ")]
        for header in headers:
            assert header.startswith(ALLOWED_HEADERS), f"白名单外的段：{header}"


def test_book_plan_segment_survives_on_a_matched_spot():
    """书页命中时，计划行带页码且没有被事实块挤掉（AC ③）。"""
    system, user = build_messages(
        build_situation(_multiway_set_snapshot(), table_dynamics=""))
    segment = user.split("## 书中计划", 1)[1].split("\n## ", 1)[0]
    assert "计划: 下注" in segment, "4 人池暗三应命中书页"
    assert re.search(r"（第 \d+ 卷 p\d+，(书中明确|编译归纳)）", segment), (
        "计划行必须带书页与确定性")
    assert "书中无此局面" not in segment
    assert len(system) + len(user) < 4000, "切换后不应回到 4k 以上"


def test_offsets_stay_out_without_a_matching_table():
    """#82：没有匹配的桌况时，偏移段一个字节都不加。"""
    _, user = build_messages(
        build_situation(_golden_snapshot(), table_dynamics=""))
    assert "## 偏移" not in user
    sit = build_situation(_golden_snapshot(), table_dynamics="")
    assert plan_offsets(sit) == ()
    assert render_offsets_zh([]) == []
    assert render_offsets_zh([OffsetDelta("action", "a", "b", "")]) == [], (
        "偏移层不许改动作：未知/动作 target 必须 fail open")
    assert set(OFFSET_TARGETS) == {"size_bb", "size_pct", "freq"}


def test_options_ids_are_what_the_model_may_pick():
    """契约测试：渲染出的 id 唯一、可解析，且都出现在「可选动作」里。"""
    _, user = build_messages(
        build_situation(_golden_snapshot(), table_dynamics=""))
    options = user.split("## 可选动作", 1)[1]
    lines = [ln for ln in options.splitlines() if ln.startswith("- ")]
    ids = [OPTION_LINE.match(ln).group(1) for ln in lines]
    assert ids, "至少要有一个可选动作"
    assert len(ids) == len(set(ids)), "option id 必须唯一"
    assert "fold" in ids and "call" in ids


def test_new_prompt_is_smaller_than_the_old_one():
    """AC ③：三个 fixture 的总字数都明显低于切换前。"""
    for name, legacy in LEGACY_TOTALS.items():
        snap = globals()[name]()
        system, user = build_messages(build_situation(snap, table_dynamics=""))
        assert len(system) + len(user) < legacy, f"{name} 没有变短"
        assert len(system) + len(user) < legacy * 0.6, (
            f"{name} 只短了一点：{len(system)+len(user)} vs {legacy}")


def test_three_bet_prompt_golden_stays_pinned():
    system, user = build_messages(
        build_situation(_three_bet_snapshot(), table_dynamics=""))
    assert "## 场景：3-bet+ 池" not in system, "场景段已随 #81 删除"
    assert _digest(system, user) == EXPECTED_THREE_BET_PROMPT_HASH, (
        "3-bet 提示词内容变了。若这是有意的，请 bump PROMPT_VERSION "
        "并更新 EXPECTED_THREE_BET_PROMPT_HASH。")


def test_air_prompt_golden_stays_pinned():
    system, user = build_messages(
        build_situation(_air_snapshot(), table_dynamics=""))
    assert "## 牌型" not in system, "牌型段已随 #81 删除"
    assert "单高张不套用" not in system
    assert _digest(system, user) == EXPECTED_AIR_PROMPT_HASH, (
        "空气 fixture 的提示词变了。若这是有意的，请 bump "
        "（advice 版本由它派生）并更新 EXPECTED_AIR_PROMPT_HASH。")
