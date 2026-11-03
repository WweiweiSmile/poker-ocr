"""实时牌力事实块：我有什么 → 坚果/听牌/成牌率 → 对范围强弱 → 牌面。

翻前不渲染（没有公共牌，牌力分档没有意义）。顺序按决策相关性排，听牌永远
按「听牌」渲染，不降级成 VERY SMALL。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation

from ...coach import (
    HIGH_CARD, MID_BOTTOM, ONE_PAIR, OVERPAIR, STRAIGHT, STRONG_DRAW, TPTK,
    TP_WEAK, WEAK_DRAW,
)
from ...ground_truth import card_text
from ...hand_track import PREFLOP
from ...pot_math import _can_act, call_amount, winnable_pot
from ..shared.format import _fmt_num
from ..shared.labels import _CATEGORY_EN_ZH, _RANK_ZH
from .board import (
    _board_structure_zh, _draws_zh, _street_change_zh, render_gt_warnings_zh,
)
from .ranges import render_ranges_zh


def render_hand_strength_zh(sit: Situation) -> list[str]:
    """「实时牌力」中文事实块；翻前返回空列表。

    顺序按决策相关性排：我有什么 → 坚果/听牌/成牌率 → 对范围强弱 →
    牌面会怎么变 → 牌面警告。听牌永远按「听牌」渲染，不降级成 VERY SMALL。
    """
    snap = sit.snap
    if snap.street == PREFLOP:
        return []
    if sit.abnormal_no_opponents:
        # 0 对手时 gt/落后比例是按假想对手算的；整块不渲染，交给数据质量段。
        return []
    lines = ["## 实时牌力"]
    gt = sit.ground_truth
    tier = sit.hand_tier
    is_draw = tier in (STRONG_DRAW, WEAK_DRAW)
    # 仅真实听花/听顺属于「成牌+听牌」分类；两高张等其他 outs 不算 combo。
    combo_draw = sit.hand_class == "combo"

    if is_draw:
        kind = "强听牌" if tier == STRONG_DRAW else "弱听牌"
        outs = sit.outs or (sit.ground_truth.draws.total_outs
                            if sit.ground_truth is not None else None)
        lines.append(f"牌力：{kind}" + (f"（{outs} outs）" if outs else ""))
    elif tier == "打牌面":
        # 「打牌面」在本项目指「最好的五张全靠公共牌」；公对/公三条时高张
        # 踢脚还可能参与比牌，但公顺/公花/公葫芦/公四条是整手牌都在牌面，
        # 踢脚不再参与。0c2e 的 AK 在 442 上仍有 A/K 踢脚，不能当空气；
        # 但公顺面说「踢脚参与比牌」同样是错的。
        made_gt = sit.ground_truth
        if made_gt is not None and made_gt.made.category >= STRAIGHT:
            lines.append("牌力：打牌面（公共牌自己成顺/花/葫芦/四条，人人都有；"
                         "只能用更高的同类型牌或听牌反超，踢脚不参与比牌）")
        else:
            lines.append("牌力：打牌面（只共享公对/公三条，没有自己的成牌；"
                         "高张/踢脚仍参与比牌，不等于空气）")
    elif tier == "空气":
        if sit.outs:
            lines.append("牌力：空气（无成牌、无听牌；有高张反超 outs）")
        else:
            lines.append("牌力：空气（无成牌、无听牌）")
    elif sit.hand_label:
        # 只给牌型标签；「大 / 中 / 小」是 hand_strength 的第 4 套口径，
        # 已经由 hand_tier / ground_truth / hand_ranges 的数字覆盖，不再重复。
        label = sit.hand_label
        if sit.hand_class == "combo" and gt is not None:
            label = f"成牌+听花/听顺（{label}）｜{_draws_zh(gt)}"
        lines.append(f"牌力：{label}")

    if gt is not None:
        made = gt.made.name_cn
        if gt.made.category == HIGH_CARD:
            rank = card_text(gt.made.best_five[0])[0] if gt.made.best_five else ""
            made = f"{_RANK_ZH.get(rank, rank)} 高" if rank else made
        if is_draw:
            made += "（未成牌）"
        elif sit.plays_board:
            made += "（打牌面）"
        lines.append(f"当前牌型：{made}")
        best = " ".join(card_text(c) for c in gt.made.best_five)
        lines.append(f"最好五张：{best}")
        if gt.hero_is_nuts:
            nuts = "是（没有任何对手持牌能打败你）"
            if gt.rank.tie_pct > 0:
                nuts += f"；平局 {gt.rank.tie_pct:.1%} 分池"
        elif is_draw:
            nuts = ("否（指当前成牌；听牌不代表最终输；"
                    "更大牌型存在，但不代表对手一定持有）")
        else:
            nuts = ("否（指当前成牌；更大牌型存在，"
                    "但不代表对手一定持有）")
        lines.append(f"坚果：{nuts}")
        if not gt.hero_is_nuts:
            stronger = []
            for beaten in gt.rank.beaten_by:
                if beaten.category == HIGH_CARD:
                    # 「高牌」作为类别时涵盖比英雄更小的 A 高，写成威胁会误导。
                    name = "更大的高牌"
                elif beaten.category == ONE_PAIR:
                    name = ("更大的对子（一对）"
                            if gt.made.category == ONE_PAIR else "一对")
                else:
                    name = _CATEGORY_EN_ZH.get(beaten.name, beaten.name)
                stronger.append(name)
            if stronger:
                lines.append("能打败你的牌型（理论上）："
                             + "、".join(stronger))
        lines.append(f"听牌：{_draws_zh(gt)}")
        if sit.hand_class == "combo":
            lines.append("牌况分类：成牌+听花/听顺；该分类不代表听牌强度或下注许可")
        if gt.draws.overcard_outs:
            kind = ("两高张" if gt.draws.overcard_outs >= 6 else "单高张")
            lines.append(f"反超 outs：{kind} {gt.draws.overcard_outs} 张")
        if gt.draws.total_outs:
            # 纯两高张不是「补牌成牌」，是反超当前顶对的权益；分开写，
            # 免得模型把 24% 当成「已经能跟任何下注」的胜率。
            overcard_only = (gt.draws.overcard_outs
                             and not gt.draws.flush_draw
                             and not gt.draws.straight_draw)
            label = "反超成牌率" if overcard_only else "补牌成牌率"
            basis = ("按高张 outs 估，不是对范围的河牌胜率"
                     if overcard_only else "仅全下/无后续下注时可直接比门槛")
            lines.append(f"{label}（四二法则，到河牌；{basis}）："
                         f"{gt.draws.equity_pct:.0f}%")

    hr = sit.hand_ranges
    if hr is None and gt is not None:
        behind = (f"当前成牌落后至少一家："
                  f"{gt.rank.beat_at_least_pct:.1%}（按随机牌下界")
        if is_draw:
            behind += "；当前未成牌，不代表最终胜率"
        else:
            behind += ("；这是当前五张被至少一家压过的概率，"
                       "不是输牌概率/权益")
        lines.append(behind + "）")
        if snap.to_call and snap.to_call > 0:
            lines.append("范围：不可用（只有随机牌下界，不能当对手范围；"
                         "按价格/线路/保守先验判断）")
    if hr is not None:
        modeled_players = len(hr.opponents) + 1
        random_fill = getattr(hr, "random_opponents", 0)
        total_players = modeled_players + random_fill
        opponents = (sit.opponents_in_hand
                     if sit.opponents_in_hand is not None
                     else snap.opponents_in_hand)
        actual_players = opponents + 1
        fair_share = 1.0 / actual_players if actual_players > 0 else 0.0
        equity_note = f"按河牌、未计后续下注，{total_players} 人池"
        if snap.to_call and snap.to_call > 0:
            equity_note += ("；这是对当前建模范围的估计，"
                            "不是对未建模的加注范围")
        unacted = [o for o in hr.opponents
                   if getattr(o, "mode", None) == "unacted"
                   and not getattr(o, "events", ())]
        if unacted:
            equity_note += "；对手本街未行动，按沿用翻前范围估算"
        elif any(getattr(o, "mode", None) == "unacted" for o in hr.opponents):
            # 事件模型：本街没动作 != 范围回到翻前——上一街收窄的信息还在。
            equity_note += "；对手本街未行动，沿用上一街收窄后的范围"
        if random_fill:
            equity_note += (f"；范围建模 {len(hr.opponents)} 家 + "
                            f"随机牌 {random_fill} 家")
        elif actual_players > modeled_players:
            equity_note += f"；按建模 {len(hr.opponents)} 名对手"
        if total_players != actual_players:
            equity_note += f"；实际 {actual_players} 人"
        # 权益已经包含随机牌时，拿它对实际人数公平份额做倍数才有意义；
        # 两边覆盖人数不一致时宁可不给倍数，避免再次出现「41% × 8 人池」
        # 这种分子/分母不是同一批人的算法。
        if (combo_draw and fair_share > 0
                and total_players == actual_players):
            equity_note += (f"；公平份额 {fair_share:.1%}，"
                            f"权益是它的 {hr.equity / fair_share:.1f} 倍")
        lines.append(f"对当前范围权益：{hr.equity:.0%}（{equity_note}）")
        behind = f"当前成牌落后至少一家：{hr.behind_any:.1%}"
        extra = []
        if random_fill:
            extra.append(f"含随机牌 {random_fill} 家")
        if is_draw:
            extra.append("当前未成牌，不代表最终胜率")
        else:
            extra.append("当前五张被至少一家压过的概率，"
                         "不是输牌概率/权益")
        lines.append(behind + "（" + "；".join(extra) + "）")
        if actual_players == 2:
            pool_label = "单挑"
        elif actual_players == 3:
            pool_label = "3 人池"
        else:
            pool_label = "多人池"
        multiway = (f"{pool_label}：实际 {actual_players} 人 / "
                    f"范围建模 {len(hr.opponents)} 名对手")
        if random_fill:
            multiway += f" / 随机牌 {random_fill} 家"
        multiway += f" / 公平份额 {fair_share:.1%}"
        lines.append(multiway)
        lines.extend(render_ranges_zh(sit))
    elif gt is None and sit.equity_pct is not None and is_draw:
        lines.append("补牌成牌率（四二法则，到河牌；"
                     "仅全下/无后续下注时可直接比门槛）："
                     f"{sit.equity_pct:.0f}%")

    # 听牌的即时价格：只在 SPR ≤ 2（跟注/全下基本就是最后一手）时给。
    # 关键约束：只有「跟注即全下/后面不再下注」才把四二法则印出来直接比；
    # 未全下时**不印二个数**——事实块里两个相邻的百分比（门槛 33% / 四二
    # 法则 32%）会被模型直接拿来比较，A/B P0-05 就是这么写出「赔率33%接近
    # 32%成牌率」的。门槛仍然给，判断方式交给牌型段（范围权益+隐含赔率）。
    if is_draw and snap.to_call and snap.to_call > 0 and (sit.spr or 99) <= 2.0:
        owed = call_amount(snap)
        winnable = winnable_pot(snap)
        eq = sit.equity_pct
        if eq is None and gt is not None and gt.draws.total_outs:
            eq = gt.draws.equity_pct
        if eq is not None and owed > 0 and winnable > 0:
            bep = owed / winnable * 100
            # 四二法则是「到河牌」的两街成牌率，只有跟注即全下、或所有
            # 对手都已全下（后面不再下注）时，才能和单街门槛直接比。
            call_is_all_in = (snap.hero_stack is not None
                              and owed >= snap.hero_stack - 1e-9)
            no_more_betting = not any(_can_act(o) for o in snap.opponents)
            direct = call_is_all_in or no_more_betting
            base = (f"听牌价格：跟注 {_fmt_num(owed)}BB / 可争夺底池 "
                    f"{_fmt_num(winnable)}BB = {bep:.0f}% 门槛")
            if direct:
                verdict = "够" if eq >= bep else "直接赔率不够"
                lines.append(f"{base}；四二法则 {eq:.0f}% → {verdict}"
                             "（跟注即全下/后面不再下注）")
            else:
                lines.append(
                    f"{base}；未全下/后面还会下注：四二法则不参与门槛比较，"
                    "按「范围权益 + 隐含赔率 + 对手线路」判断")

    if gt is not None:
        lines.append(f"牌面结构：{_board_structure_zh(gt)}")
        change = _street_change_zh(gt)
        if change:
            lines.append(f"转河变化：{change}")
        warnings = [w.rstrip("。；")
                    for w in render_gt_warnings_zh(gt, draw_focus=is_draw)]
        if warnings:
            lines.append("牌面警告：" + "；".join(warnings) + "。")

    # 只有标题、没有任何事实时整段省略。
    return lines if len(lines) > 1 else []
