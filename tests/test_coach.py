#!/usr/bin/env python3
"""翻后建议引擎的测试（纯逻辑，不需要图片、不需要 fastapi）。

    .venv/bin/python tests/test_coach.py

只 import poker_ocr.coach，不碰 api —— 这样即使没有 cv2 也能跑。
所有用例的期望值都对着 call-back/services/skills/ 里的规则表：
干燥面顶对顶踢下 2/3、湿面小牌过牌、强成牌遇下注加注、听牌看赔率跟。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.coach import (  # noqa: E402
    DRY, FLUSH_STRAIGHT_BOARD, PAIRED_BOARD, STRAIGHT_BOARD,
    advise, board_texture, opponent_strength, parse_cards, read_hand,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def line(*actions: dict) -> list[dict]:
    return list(actions)


# ─────────────────────────── 牌面质地 ───────────────────────────

def test_texture() -> None:
    cases = {
        "Ks7d2c": DRY,             # 彩虹、不连、不公对
        "Ah7d2c": DRY,
        "KhQhJc": FLUSH_STRAIGHT_BOARD,   # 两同花 + 连张
        "9s8d4c": STRAIGHT_BOARD,  # 9-8 相邻
        "Ks6d6c": PAIRED_BOARD,    # 公对
        "KcQcJc": FLUSH_STRAIGHT_BOARD,   # 单色 + 连张
    }
    for board, want in cases.items():
        got, _tags = board_texture(parse_cards(board))
        check(got == want, f"质地 {board} 应 {want}，实际 {got}")


# ─────────────────────────── 牌力分档 ───────────────────────────

def test_read_hand() -> None:
    # 顶对顶踢：KQ 在 K72
    r = read_hand(parse_cards("KhQh"), parse_cards("Ks7d2c"))
    check(r.tier == "顶对强踢" and r.label == "顶对Q踢",
          f"KQ on K72 应 顶对Q踢，实际 {r.tier}/{r.label}")

    # 超对：AA 在 K72
    r = read_hand(parse_cards("AhAd"), parse_cards("Ks7d2c"))
    check(r.tier == "超对", f"AA on K72 应 超对，实际 {r.tier}")

    # 三条(set)：口袋对中牌面
    r = read_hand(parse_cards("7h7d"), parse_cards("7s2c9d"))
    check(r.tier == "强成牌" and r.label == "三条(set)",
          f"77 on 792 应 三条(set)，实际 {r.tier}/{r.label}")

    # 底对(口袋)：88 在 K72
    r = read_hand(parse_cards("8h8d"), parse_cards("Ks7d2c"))
    check(r.tier == "中底对", f"88 on K72 应 中底对，实际 {r.tier}")

    # 强听：同花听 + 两头顺 = 15 outs（并集去重，与 odds-table.md 第 4 张表一致）
    r = read_hand(parse_cards("9h8h"), parse_cards("7h6c2h"))
    check(r.tier == "强听" and r.outs == 15,
          f"9h8h on 7h6c2h 应 强听 15 outs，实际 {r.tier} {r.outs}")

    # 卡顺 = 4 张牌（不是 1 个点数）；AsTh 在 Jc8dKd 上等 Q 成百老汇
    r = read_hand(parse_cards("AsTh"), parse_cards("Jc8dKd"))
    check(r.tier == "弱听" and r.outs == 4,
          f"AsTh on Jc8dKd 应 弱听 4 outs（卡顺），实际 {r.tier} {r.outs}")


def test_relative_strength_by_board() -> None:
    """相对牌力：牌面上可能构成的牌型比我的大 → capped_by_board。"""
    # 三同花面：我的顺子被同花压制
    check(read_hand(parse_cards("AsTh"), parse_cards("Jc8dKdQcAd")).capped_by_board,
          "三同花面上的顺子应被压制")
    # 公对面（双 A）：我的两对被三条/葫芦压制
    check(read_hand(parse_cards("JhJc"), parse_cards("QcAs8dAc")).capped_by_board,
          "双 A 面上的两对应被压制")
    # 干燥面：顶对不被压制
    check(not read_hand(parse_cards("KhQh"), parse_cards("Ks7d2c")).capped_by_board,
          "干燥面上的顶对不该被压制")
    # 我自己就是同花 → 牌面上限不会超过我
    check(not read_hand(parse_cards("Ad2d"), parse_cards("Jc8dKdQc5d")).capped_by_board,
          "自己的同花不该被压制")


# ─────────────────────────── 过牌到我：下注 or 过牌 ───────────────────────────

def test_tptk_dry_bets_two_thirds() -> None:
    """用户的核心例子：顶对顶踢在干燥面拿价值 → 下注 2/3。"""
    d = advise("KhQh", "Ks7d2c", pot=6,
               actions=line({"actor": "BB", "action": "check", "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(d.ok, f"应给出建议，实际 ok=False：{d.reason}")
    check(d.action == "bet", f"应下注，实际 {d.action}（{d.reason}）")
    check(d.sizing_bb == 4.0, f"6 池的 2/3 应 4.0，实际 {d.sizing_bb}")
    check(d.texture == DRY, f"质地应 干燥，实际 {d.texture}")
    check(d.hand.label == "顶对Q踢", f"牌力应 顶对Q踢，实际 {d.hand.label}")


def test_tptk_wet_bets_half() -> None:
    """顶对顶踢在花顺面：按确认过的表仍下 1/2（不是过牌）。"""
    d = advise("AsKs", "KhQhJc", pot=10,
               actions=line({"actor": "BB", "action": "check", "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(d.texture == FLUSH_STRAIGHT_BOARD, f"质地应 花顺面，实际 {d.texture}")
    check(d.action == "bet" and d.sizing_bb == 5.0,
          f"应下注 5.0（1/2 池），实际 {d.action} {d.sizing_bb}")


def test_small_pair_wet_checks() -> None:
    """小牌在湿面：中对(口袋)过牌 —— 用户要的「小小牌更多过牌」。"""
    d = advise("ThTd", "KhQh8c", pot=10,
               actions=line({"actor": "BB", "action": "check", "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(d.texture == FLUSH_STRAIGHT_BOARD, f"质地应 花顺面，实际 {d.texture}")
    check(d.action == "check", f"小对在湿面应过牌，实际 {d.action}（{d.reason}）")


def test_strong_draw_bets_multiway() -> None:
    """强听在多人池也开火 —— §7「不诈唬」针对的是诈唬，不是有真实赢率的听牌。手 23/34 复现。"""
    d = advise("AdQd", "9d6d5s", pot=39, hero_position="LJ", opponents=4,
               actions=line({"actor": "SB", "action": "check", "street": "flop"},
                            {"actor": "BB", "action": "check", "street": "flop"}))
    check(d.hand.tier == "强听", f"应是强听，实际 {d.hand.tier}")
    check(d.action == "bet", f"多人池强听应下注，实际 {d.action}（{d.reason}）")

    # 空气在多人池仍然过牌（不诈唬那部分保留）
    d = advise("Ac9c", "Jc8dTs", pot=30, hero_position="BB", opponents=3,
               actions=line({"actor": "SB", "action": "check", "street": "flop"}))
    check(d.action == "bet", f"OESD 多人池应下注，实际 {d.action}")


def test_air_multiway_checks() -> None:
    """空气在多人池：从不诈唬，过牌。"""
    d = advise("AhKd", "Qs7c2d", pot=9,
               actions=line({"actor": "BB", "action": "check", "street": "flop"},
                            {"actor": "CO", "action": "check", "street": "flop"}),
               hero_position="BTN", opponents=2)
    check(d.action == "check", f"多人池空气应过牌，实际 {d.action}（{d.reason}）")


# ─────────────────────────── 面对下注：加 / 跟 / 弃 ───────────────────────────

def test_strong_made_raises() -> None:
    d = advise("7h7d", "7s2c9d", pot=8,
               actions=line({"actor": "BB", "action": "bet", "amount": 4, "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(d.action == "raise", f"三条遇下注应加注，实际 {d.action}（{d.reason}）")
    check(d.sizing_bb == 16.0, f"满池加注应 16.0，实际 {d.sizing_bb}")


def test_strong_draw_calls() -> None:
    """坚果听牌（握该花色 A）+ 赔率够 → 跟。"""
    d = advise("Ah9h", "Kh2h7c", pot=6,
               actions=line({"actor": "BB", "action": "bet", "amount": 3, "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(d.hand.nut_draw, "Ah9h 在 Kh2h7c 应是坚果同花听")
    check(d.action == "call", f"坚果听赔率够应跟，实际 {d.action}（{d.reason}）")


def test_nut_gate_only_when_board_threat() -> None:
    """「坚果关」只在牌面已成花/成顺时生效。

    牌面 3 张同花 → 可能有更大的牌 → 非坚果听一律弃；
    牌面只有 2 张同花（成花只是"可能"）→ 非坚果听仍按赔率走。
    """
    # 牌面 3 张红桃（已成花可能）：9h8c 是 4 张红桃的同花听、手里没 Ah → 非坚果 → 弃
    d = advise("9h8c", "Kh2h7h", pot=6,
               actions=line({"actor": "BB", "action": "bet", "amount": 3, "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(not d.hand.nut_draw, "9h8c 在 Kh2h7h 不该是坚果听")
    check(d.action == "fold", f"3 同花面上非坚果听应弃，实际 {d.action}（{d.reason}）")

    # 同一手、牌面只有 2 张红桃：坚果关不生效 → 按赔率 → 跟
    d = advise("9h8h", "Kh2h7c", pot=6,
               actions=line({"actor": "BB", "action": "bet", "amount": 3, "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(d.action == "call", f"2 同花面上非坚果听按赔率应跟，实际 {d.action}（{d.reason}）")


def test_weak_pair_folds_to_big_bet() -> None:
    d = advise("Kh3h", "Ks7d2c", pot=6,
               actions=line({"actor": "BB", "action": "bet", "amount": 5, "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(d.action == "fold", f"弱踢遇 5/6 池大注应弃，实际 {d.action}（{d.reason}）")


def test_tptk_folds_on_wet_to_big_bet() -> None:
    d = advise("AsKs", "KhQhJc", pot=8,
               actions=line({"actor": "BB", "action": "bet", "amount": 6, "street": "flop"}),
               hero_position="BTN", opponents=1)
    check(d.action == "fold", f"顶对在花顺面遇 3/4 池应弃，实际 {d.action}（{d.reason}）")


# ─────────────────────────── 我下注被加注 ───────────────────────────

def test_my_bet_uncalled_not_facing() -> None:
    """我下注、没人加注 → 不是「面对下注」，按过牌到我的逻辑走。"""
    d = advise("AhKd", "Ks7d2c", pot=10, hero_position="BTN", opponents=1,
               actions=line({"actor": "BTN", "action": "bet", "amount": 4, "street": "flop"}))
    check(d.action == "bet" and d.sizing_bb == 6.67,
          f"我下注无人加注应按价值线（2/3 池 = 6.67），实际 {d.action} {d.sizing_bb}")


def test_raised_over_my_bet_folds_top_pair() -> None:
    """我下注 4、对手加到 30 → 要跟的是 26（不是 30）；顶对被加注扛不住大注 → 弃。"""
    d = advise("AhKd", "Ks7d2c", pot=44, hero_position="BTN", opponents=1,
               actions=line({"actor": "BTN", "action": "bet", "amount": 4, "street": "flop"},
                            {"actor": "BB", "action": "raise", "amount": 30, "street": "flop"}))
    check(d.action == "fold", f"顶对被大加注应弃，实际 {d.action}（{d.reason}）")


def test_raised_over_my_bet_small_calls() -> None:
    """小加注（我投 4、对手加到 6）→ 顶对跟，且跟的量是 2 不是 6。"""
    d = advise("AhKd", "Ks7d2c", pot=18, hero_position="BTN", opponents=1,
               actions=line({"actor": "BTN", "action": "bet", "amount": 4, "street": "flop"},
                            {"actor": "BB", "action": "raise", "amount": 6, "street": "flop"}))
    check(d.action == "call", f"小加注顶对应跟，实际 {d.action}（{d.reason}）")
    check("需要 10%" in (d.reason or ""), f"应显示扣掉我已投后的门槛 10%，实际「{d.reason}」")


def test_raised_over_my_bet_draw_uses_correct_amount() -> None:
    """同花听、我下注 4 被加到 12：跟的量是 8（=12−4），门槛 24% < 36% → 跟。

    修之前会把 12 当要跟的量，门槛算成 55% → 错误弃牌。
    """
    d = advise("Ah9h", "Kh2h7c", pot=26, hero_position="BTN", opponents=1,
               actions=line({"actor": "BTN", "action": "bet", "amount": 4, "street": "flop"},
                            {"actor": "BB", "action": "raise", "amount": 12, "street": "flop"}))
    check(d.action == "call", f"听牌被加注应按 8 算门槛并跟，实际 {d.action}（{d.reason}）")
    check("24%" in (d.reason or ""), f"门槛应是 24%，实际「{d.reason}」")


# ─────────────────────────── 盲点①：有牌又有听 ───────────────────────────

def test_combo_draw_facing_bet_calls() -> None:
    """底对 + 坚果同花听（有牌又有听）→ 面对下注跟，不按"弱一对"弃。"""
    d = advise("Ah5h", "Kh5c7h", pot=12, hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "bet", "amount": 5, "street": "flop"}))
    check(d.hand.tier == "中底对", f"应是中底对，实际 {d.hand.tier}")
    check(d.hand.combo, "底对+坚果听应判为 combo")
    check(d.action == "call", f"有牌又有听应跟，实际 {d.action}（{d.reason}）")


def test_combo_draw_checked_to_bets() -> None:
    """中对 + 同花听、过牌到我 → 下注（两条赢路）。手 35 复现。"""
    d = advise("Jc6c", "Jd4cQc9d", street="turn", pot=11, hero_position="BB", opponents=1,
               actions=line({"actor": "UTG+1", "action": "check", "street": "turn"}))
    check(d.hand.combo, "中对+同花听应判为 combo")
    check(d.action == "bet", f"有牌又有听过牌到我应下注，实际 {d.action}（{d.reason}）")


# ─────────────────────────── 盲点②：打牌面 ───────────────────────────

def test_plays_board_is_not_strong() -> None:
    """公共牌自己就是两对 → 人均两对，不当强牌。手 11 复现。"""
    d = advise("KcQc", "8d5c9s5h9d", street="river", pot=42, hero_position="UTG+2",
               opponents=1,
               actions=line({"actor": "UTG+1", "action": "check", "street": "river"}))
    check(d.hand.plays_board, "KQ 在 9955 面上应是打牌面")
    check(d.hand.tier == "打牌面", f"分档应是打牌面，实际 {d.hand.tier}")
    check(d.action == "check", f"打牌面应过牌，实际 {d.action}（{d.reason}）")


def test_plays_board_folds_to_bet() -> None:
    d = advise("KcQc", "8d5c9s5h9d", street="river", pot=42, hero_position="UTG+2",
               opponents=1,
               actions=line({"actor": "UTG+1", "action": "bet", "amount": 20, "street": "river"}))
    check(d.action == "fold", f"打牌面遇下注应弃，实际 {d.action}（{d.reason}）")


def test_real_two_pair_not_downgraded() -> None:
    """我自己打出来的两对不能被误判成打牌面。"""
    r = read_hand(parse_cards("KhQh"), parse_cards("KsQd2c9h7s"))
    check(not r.plays_board and r.tier == "强成牌",
          f"KQ 在 KQ2 面上是真两对，实际 {r.tier}/{r.plays_board}")


# ─────────────────────────── 读对手牌力 ───────────────────────────

def test_opponent_strength_scales_with_size() -> None:
    """动作越大 / 越激进 → 牌力值越高。"""
    def s(act, amt=None, pot=10, street="flop", acts=None):
        a = acts or [{"actor": "BB", "action": act, "amount": amt, "street": street}]
        return opponent_strength(a, street, "BTN", pot)

    check(s("check").label == "弱", f"过牌应弱，实际 {s('check').label}")
    check(s("bet", 3).label == "中", f"1/3 池应中，实际 {s('bet', 3).label}")
    check(s("bet", 7).value >= 55, f"2/3 池应到强，实际 {s('bet', 7).value}")
    check(s("raise", 12).value >= 55, f"主动加注应强以上，实际 {s('raise', 12).value}")
    check(s("allin", 50).label == "极强", f"全下应极强，实际 {s('allin', 50).label}")
    # 多街连续开火加成
    two = s("bet", 6, pot=12, street="turn",
            acts=[{"actor": "BB", "action": "bet", "amount": 6, "street": "flop"},
                  {"actor": "BB", "action": "bet", "amount": 6, "street": "turn"}])
    one = s("bet", 6, pot=12, street="turn")
    check(two.value > one.value, f"多街开火应更高：{two.value} vs {one.value}")


def test_opponent_barreling_folds_weak_pair() -> None:
    """对手翻牌+转牌连续开火（牌力到「强」）→ 顶对弱踢弃，不只是看这一注大小。"""
    d = advise("Kh3h", "Ks7d2c9h", street="turn", pot=20, hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "bet", "amount": 5, "street": "flop"},
                            {"actor": "BB", "action": "bet", "amount": 10, "street": "turn"}))
    check(d.line.opp_strength is not None, "应算出对手牌力")
    check(d.line.opp_strength.value >= 55, f"对手牌力应到强，实际 {d.line.opp_strength.value}")
    check(d.action == "fold", f"应弃，实际 {d.action}（{d.reason}）")


# ─────────────────────────── 对手形象 ───────────────────────────

def test_lag_traps_with_strong_made() -> None:
    """对松凶：持强牌过牌诱捕，别对攻（§8）。"""
    d = advise("7h7d", "7s2c9d", pot=8,
               actions=line({"actor": "BB", "action": "check", "street": "flop"}),
               hero_position="BTN", opponents=1, read_name="松凶")
    check(d.action == "check", f"对松凶强牌应过牌诱捕，实际 {d.action}（{d.reason}）")


# ─────────────────────────── 套池（已投入 ≥3/4） ───────────────────────────

def test_pot_committed_turns_fold_into_call() -> None:
    """已投入 16/20 = 80% ≥ 3/4 → 套池：原本的弃牌改成跟。"""
    d = advise("Kh3h", "Ks7d2c", pot=6, hero_position="BTN", opponents=1,
               stack_bb=20, hero_invested_bb=16,
               actions=line({"actor": "BB", "action": "bet", "amount": 5, "street": "flop"}))
    check(d.action == "call", f"套池应改为跟，实际 {d.action}（{d.reason}）")
    check("套池" in (d.reason or ""), f"理由应说明套池，实际「{d.reason}」")


def test_not_committed_still_folds() -> None:
    """已投入 14/20 = 70% < 3/4 → 不算套池，照常弃。"""
    d = advise("Kh3h", "Ks7d2c", pot=6, hero_position="BTN", opponents=1,
               stack_bb=20, hero_invested_bb=14,
               actions=line({"actor": "BB", "action": "bet", "amount": 5, "street": "flop"}))
    check(d.action == "fold", f"未到 3/4 应照常弃，实际 {d.action}（{d.reason}）")


def test_river_pot_committed() -> None:
    d = advise("AhKd", "Ks7d2c9h3s", street="river", pot=10, hero_position="BTN",
               opponents=1, stack_bb=20, hero_invested_bb=16,
               actions=line({"actor": "BB", "action": "bet", "amount": 8, "street": "river"}))
    check(d.action == "call", f"河牌套池应改为跟，实际 {d.action}（{d.reason}）")


def test_invested_without_stack_warns() -> None:
    """给了 hero_invested_bb 却没给 stack_bb → 算不出套池，加 warning，照常弃。"""
    d = advise("Kh3h", "Ks7d2c", pot=6, hero_position="BTN", opponents=1,
               hero_invested_bb=16,
               actions=line({"actor": "BB", "action": "bet", "amount": 5, "street": "flop"}))
    check(d.action == "fold", f"算不出套池时应照常弃，实际 {d.action}")
    check(any("套池" in w for w in d.warnings), f"应有套池相关 warning，实际 {d.warnings}")


# ─────────────────────────── 转牌 ───────────────────────────

def test_turn_scare_checks() -> None:
    """转牌成花 + 顶对 → 把开火权让给对手，过牌控池（postflop-turn.md）。"""
    d = advise("AhKd", "Ks7s2c3s", street="turn", pot=10, stack_bb=30,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "bet", "amount": 3, "street": "flop"},
                            {"actor": "BTN", "action": "call", "street": "flop"},
                            {"actor": "BB", "action": "check", "street": "turn"}))
    check(d.action == "check", f"吓人转牌顶对应过牌控池，实际 {d.action}（{d.reason}）")


def test_turn_blank_bets() -> None:
    """blank 转牌 + 顶对强踢 → 继续下 1/2 池。"""
    d = advise("AhKd", "Ks7d2c9h", street="turn", pot=10, stack_bb=30,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "check", "street": "turn"}))
    check(d.action == "bet" and d.sizing_bb == 5.0,
          f"blank 转牌顶对应下 1/2 池（5.0），实际 {d.action} {d.sizing_bb}（{d.reason}）")


def test_turn_full_house_one_third() -> None:
    """转牌凑出葫芦、牌面在听 → 下 1/3 池（postflop-turn.md 原话）。"""
    d = advise("9h9d", "9s2h7h2s", street="turn", pot=12, stack_bb=30,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "check", "street": "turn"}))
    check(d.hand.category == 6, f"应是葫芦，实际 {d.hand.label}")
    check(d.action == "bet" and d.sizing_bb == 4.0,
          f"葫芦+听牌应下 1/3 池（4.0），实际 {d.action} {d.sizing_bb}（{d.reason}）")


def test_turn_draw_folds_conservative_implied() -> None:
    """转牌同花听遇 1/2 池：含保守隐含赔率后门槛仍高于 18% → 弃。"""
    d = advise("Ah9h", "Kh7h2c3s", street="turn", pot=10, stack_bb=25,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "bet", "amount": 5, "street": "turn"}))
    check(d.action == "fold", f"应弃（隐含赔率保守），实际 {d.action}（{d.reason}）")


def test_turn_draw_calls_small_bet_deep_stack() -> None:
    """转牌同花听、小注、深筹码：含保守隐含赔率后够跟。"""
    d = advise("Ah9h", "Kh7h2c3s", street="turn", pot=10, stack_bb=30,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "bet", "amount": 3, "street": "turn"}))
    check(d.action == "call", f"应跟（隐含赔率够），实际 {d.action}（{d.reason}）")


def test_turn_strong_made_raises() -> None:
    d = advise("7h7d", "7s2c9dJh", street="turn", pot=10, stack_bb=30,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "bet", "amount": 4, "street": "turn"}))
    check(d.action == "raise" and d.sizing_bb == 18.0,
          f"三条在 blank 转牌应加注 18.0，实际 {d.action} {d.sizing_bb}（{d.reason}）")


# ─────────────────────────── 河牌 ───────────────────────────

def test_river_strong_made_bets() -> None:
    """河牌强成牌 → 价值下注 2/3（固定档）。"""
    d = advise("KhQh", "KsQd2c9h7s", street="river", pot=10,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "check", "street": "river"}))
    check(d.action == "bet" and d.sizing_bb == 6.67,
          f"河牌两对应下 2/3 池（6.67），实际 {d.action} {d.sizing_bb}（{d.reason}）")


def test_river_top_pair_checks() -> None:
    """河牌顶对 = 中等牌 → 过牌引诱诈唬（river-medium.md）。"""
    d = advise("AhKd", "Ks7d2c9h3s", street="river", pot=10,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "check", "street": "river"}))
    check(d.action == "check", f"河牌顶对应过牌引诱诈唬，实际 {d.action}（{d.reason}）")


def test_river_bluff_catch() -> None:
    """牌面无关 + 对手小注 → 中等牌抓诈唬跟。"""
    d = advise("AhKd", "Ks7d2c9h3s", street="river", pot=10,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "bet", "amount": 3, "street": "river"}))
    check(d.action == "call", f"无关牌面小注应抓诈唬跟，实际 {d.action}（{d.reason}）")


def test_river_strong_made_downgrades_on_flush_board() -> None:
    """牌面三同花、我只有顺子（打不过同花）→ 不再加注，只跟。

    990009 的复现：AsTh 在 Jc8dKdQcAd 上拿坚果顺子，但 8d/Kd/Ad 三张方块，
    对手两张方块就成花反超 → 面对 40% 池下注只跟，不加注。
    """
    d = advise("AsTh", "Jc8dKdQcAd", street="river", pot=358, hero_position="BTN",
               opponents=1,
               actions=line({"actor": "UTG+2", "action": "bet", "amount": 144.5, "street": "river"}))
    check(d.hand.label == "顺子", f"应是顺子，实际 {d.hand.label}")
    check(d.action == "call", f"花面顺子应降级为跟，实际 {d.action}（{d.reason}）")


def test_river_strong_made_folds_on_flush_board_big_bet() -> None:
    d = advise("AsTh", "Jc8dKdQcAd", street="river", pot=358, hero_position="BTN",
               opponents=1,
               actions=line({"actor": "UTG+2", "action": "bet", "amount": 250, "street": "river"}))
    check(d.action == "fold", f"花面顺子遇 70% 池应弃，实际 {d.action}（{d.reason}）")


def test_paired_board_two_pair_downgrades() -> None:
    """手 4 复现：JJ 在 QcAs8dAc 上（双 A 面），两对被牌面压制 → 面对加注不加注。"""
    d = advise("JhJc", "QcAs8dAc", street="turn", pot=137, hero_position="SB", opponents=1,
               actions=line({"actor": "SB", "action": "bet", "amount": 26.5, "street": "turn"},
                            {"actor": "villain", "action": "raise", "amount": 62.5, "street": "turn"}))
    check(d.hand.label == "两对", f"应是两对，实际 {d.hand.label}")
    check(d.action != "raise", f"双 A 面两对不该加注，实际 {d.action}（{d.reason}）")


def test_river_flush_still_raises_on_flush_board() -> None:
    """我自己就是同花 → 打得过，照常加注（葫芦/四条同理）。"""
    d = advise("Ad2d", "Jc8dKdQc5d", street="river", pot=100, hero_position="BTN",
               opponents=1,
               actions=line({"actor": "UTG+2", "action": "bet", "amount": 50, "street": "river"}))
    check(d.hand.label == "同花", f"应是同花，实际 {d.hand.label}")
    check(d.action == "raise", f"同花应照常加注，实际 {d.action}（{d.reason}）")


def test_river_folds_to_big_bet() -> None:
    d = advise("AhKd", "Ks7d2c9h3s", street="river", pot=10,
               hero_position="BTN", opponents=1,
               actions=line({"actor": "BB", "action": "bet", "amount": 8, "street": "river"}))
    check(d.action == "fold", f"河牌中等牌遇大注应弃，实际 {d.action}（{d.reason}）")


# ─────────────────────────── 出界：绝不猜 ───────────────────────────

def test_out_of_scope() -> None:
    d = advise("KhQh", "Ks7d2c", street="preflop", pot=6)
    check(not d.ok and "street" in (d.reason or ""), f"非法 street 应 ok=False，实际 {d.ok}/{d.reason}")

    d = advise("KhQh", "Ks7d2c", street="turn", pot=6)
    check(not d.ok and "4 张" in (d.reason or ""), f"转牌给 3 张应 ok=False，实际 {d.ok}/{d.reason}")

    d = advise("KhQh", "Ks7d2c9h", street="river", pot=6)
    check(not d.ok and "5 张" in (d.reason or ""), f"河牌给 4 张应 ok=False，实际 {d.ok}/{d.reason}")

    d = advise("KhQh", "Ks7d2c", pot=None)
    check(not d.ok and "pot" in (d.reason or ""), f"缺 pot 应 ok=False，实际 {d.ok}/{d.reason}")

    d = advise("KhQh", "Ks7d2cQd", pot=6)
    check(not d.ok, f"翻牌位给 4 张应 ok=False，实际 {d.ok}")

    d = advise("KhKh", "Ks7d2c", pot=6)
    check(not d.ok and "重复" in (d.reason or ""), f"重复牌应 ok=False，实际 {d.reason}")

    d = advise("KhQh", "Xs7d2c", pot=6)
    check(not d.ok, f"非法牌面应 ok=False，实际 {d.ok}")


def main() -> int:
    tests = [
        test_texture,
        test_read_hand,
        test_relative_strength_by_board,
        test_tptk_dry_bets_two_thirds,
        test_tptk_wet_bets_half,
        test_small_pair_wet_checks,
        test_strong_draw_bets_multiway,
        test_air_multiway_checks,
        test_strong_made_raises,
        test_strong_draw_calls,
        test_nut_gate_only_when_board_threat,
        test_weak_pair_folds_to_big_bet,
        test_tptk_folds_on_wet_to_big_bet,
        test_my_bet_uncalled_not_facing,
        test_raised_over_my_bet_folds_top_pair,
        test_raised_over_my_bet_small_calls,
        test_raised_over_my_bet_draw_uses_correct_amount,
        test_lag_traps_with_strong_made,
        test_combo_draw_facing_bet_calls,
        test_combo_draw_checked_to_bets,
        test_plays_board_is_not_strong,
        test_plays_board_folds_to_bet,
        test_real_two_pair_not_downgraded,
        test_opponent_strength_scales_with_size,
        test_opponent_barreling_folds_weak_pair,
        test_pot_committed_turns_fold_into_call,
        test_not_committed_still_folds,
        test_river_pot_committed,
        test_invested_without_stack_warns,
        test_turn_scare_checks,
        test_turn_blank_bets,
        test_turn_full_house_one_third,
        test_turn_draw_folds_conservative_implied,
        test_turn_draw_calls_small_bet_deep_stack,
        test_turn_strong_made_raises,
        test_river_strong_made_bets,
        test_river_top_pair_checks,
        test_river_bluff_catch,
        test_river_strong_made_downgrades_on_flush_board,
        test_river_strong_made_folds_on_flush_board_big_bet,
        test_paired_board_two_pair_downgrades,
        test_river_flush_still_raises_on_flush_board,
        test_river_folds_to_big_bet,
        test_out_of_scope,
    ]
    for t in tests:
        before = len(FAILS)
        t()
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 翻后建议引擎测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
