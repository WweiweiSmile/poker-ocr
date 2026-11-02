"""桌况统计：本桌 VPIP/PFR/3-bet/弃牌率 + 可执行读法。

`current_table` 是本手在局对手的实测，`population` 是冷启动先验。样本太小
（<20 次机会）时不渲染 —— 十手牌算出来的「本桌 VPIP」比没有更误导。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...opponent_profile import table_exploit_zh
from ..shared.format import _fmt_num

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...opponent_profile import PooledStats


_TABLE_SMALL_OPPS = 20.0


def _table_stat_zh(label: str, value: float,
                   opportunities: float | None = None) -> str:
    """单个桌况指标；机会数 <20 时明说，避免 5 次机会的 82% 被当成硬事实。"""
    text = f"{label} {value:.0f}%"
    if opportunities is not None and opportunities < _TABLE_SMALL_OPPS:
        text += f"（仅 {_fmt_num(opportunities)} 次机会）"
    return text


def _table_stab_read_zh(stats: "PooledStats", *,
                        players_left_to_act: int | None = None,
                        in_position: bool | None = None) -> str:
    """桌况的 stab 读法：把「前面没人下注时下注 X%」翻成可执行动作。

    用户 2026-11-01 口径：只给提示，不做硬闸。高 stab 的核心含义是
    「你过牌不是免费看牌」——强牌可过牌诱导，中等牌别主动下注把空气
    打走；但 stab 只描述「下注」，不描述「加注」，加注范围仍按价值密集
    处理。低 stab 才是「过牌基本能免费看下一张」。

    `players_left_to_act == 0` 时 Hero 是最后行动，过牌就结束本街：
    此时不能说「过牌诱导 check-raise」，只能说下注后被已过牌的人反加。
    """
    stab = stats.stab
    opps = (stats.opportunities or {}).get("stab", 0.0)
    if stab is None or opps < _TABLE_SMALL_OPPS:
        return ""
    last_to_act = players_left_to_act == 0
    if stab >= 40:
        if last_to_act:
            return (f"桌况读法（程序建议）：前面没人下注时这桌爱开火（{stab:.0f}%）。"
                    "你是最后行动，过牌免费；下注后被对手 check-raise 的"
                    "风险仍在，强牌下注拿价值、中等牌控池。")
        return (f"桌况读法（程序建议）：前面没人下注时这桌爱开火（{stab:.0f}%）。"
                "你过牌不是免费看牌——强牌可以过牌诱导 check-raise，"
                "中等牌别主动下注把他们的空气打走；但他们加注范围仍偏强，"
                "别用中对跟加注。")
    if stab >= 30:
        if last_to_act:
            return (f"桌况读法（程序建议）：前面没人下注时这桌会开火（{stab:.0f}%）。"
                    "你是最后行动，过牌免费；下注后被 check-raise 要提前想好。")
        return (f"桌况读法（程序建议）：前面没人下注时这桌会开火（{stab:.0f}%）。"
                "过牌别当免费看牌；强牌可过牌诱导，中等牌下注前先想清楚"
                "被加注怎么办。")
    if stab <= 15:
        return (f"桌况读法（程序建议）：前面没人下注时这桌很少开火（{stab:.0f}%）。"
                "你过牌大概率能免费看下一张；没摊牌价值时可以在下一街"
                "主动偷池。")
    return ""


def render_table_stats_zh(stats: "PooledStats | None",
                          opponents_in_hand: int | None = None,
                          street: str | None = None,
                          suppress_cbet: bool = False,
                          players_left_to_act: int | None = None,
                          in_position: bool | None = None) -> list[str]:
    """桌况事实：`current_table` 是本手在局对手，`population` 是冷启动先验。

    替代 `.table_dy` 里「整体偏松」那类形容词。样本太小时不渲染 ——
    十手牌算出来的「本桌 VPIP」比没有更误导。

    `opponents_in_hand` 用主事实块的在局人数，而不是 `stats.total_players`：
    后者来自聚合时的活动对手列表，偶尔会和状态帧/行动线对不上；两个数字
    同时出现时模型会不知道该信谁（d3a9 出现过「本手在局 8 人」+「当前牌桌
    数据（本手在局 3 人）」）。桌况只声明「有画像的对手数」，覆盖率按主事实
    人数算。
    """
    if stats is None or stats.hands < 20:
        return []
    # 0 对手是状态帧异常（翻后只剩 Hero）。这时任何「本桌 N 名对手有画像」
    # 都会和主事实「本手在局 1 人」打架，直接不渲染桌况。
    if opponents_in_hand is not None and opponents_in_hand <= 0:
        return []
    opportunities = stats.opportunities or {}
    parts = [
        _table_stat_zh(label, value, opportunities.get(key))
        for label, value, key in (
            ("VPIP", stats.vpip, "preflop"),
            ("PFR", stats.pfr, "preflop"),
            ("3-bet", stats.threebet, "threebet"),
            ("溜入", stats.limp, "limp"),
            ("对 c-bet 弃", stats.fold_to_cbet, "fcbet"),
            ("前面没人下注时下注", stats.stab, "stab"),
        )
        if value is not None
    ]
    if not parts:
        return []
    total = 0
    if stats.scope == "current_table":
        # 以主事实块的在局人数为准；没有传时退回聚合口径。
        if opponents_in_hand is not None:
            total = max(int(opponents_in_hand), 0) + 1
        else:
            total = max(int(stats.total_players or 0), stats.players + 1)
        sample = (f"历史画像，单人最多约 {stats.hands} 手"
                  if stats.approx_hands else f"历史画像，单人最多 {stats.hands} 手")
        opponents_total = max(total - 1, 0)
        # 聚合时可能有已弃牌/已离桌的人；不能在「本手 2 人」时写 4 名对手
        # 有画像。以主事实人数封顶，缺口只算真实在局的人。
        known = min(int(stats.players), opponents_total)
        lines = [f"## 当前牌桌数据（{known} 名在局对手有画像；桌均；{sample}）"]
        unknown = max(opponents_total - known, 0)
        if unknown:
            lines.append(f"另外 {unknown} 名在局对手没有画像，"
                         "按未知对手处理，不伪造个人数据")
    else:
        lines = ["## 全场数据（全场先验，非本桌）"]
    if stats.scope == "current_table":
        lines.append("｜".join(parts))
    else:
        lines.append(f"{stats.players} 人 / {stats.hands} 手："
                     + "｜".join(parts))
    street_stats = [
        _table_stat_zh(label, value, opportunities.get(key))
        for label, value, key in (
            ("翻牌", stats.fold_to_bet_flop, "flop"),
            ("转牌", stats.fold_to_bet_turn, "turn"),
            ("河牌", stats.fold_to_bet_river, "river"),
        )
        if value is not None
    ]
    if street_stats:
        lines.append("面对下注弃：" + "｜".join(street_stats))
    stab_read = _table_stab_read_zh(
        stats, players_left_to_act=players_left_to_act,
        in_position=in_position)
    if stab_read:
        lines.append(stab_read)
    tags: list[str] = []
    if stats.loose is True:
        tags.append("松")
    elif stats.loose is False:
        tags.append("紧")
    # 「3-bet 少」不是「整体被动」：后者需要翻后 call/raise 数据，
    # 只看 3-bet 会把带 MANIAC 的桌子写成被动桌。
    if stats.passive is True:
        tags.append("3-bet 少")
    if tags:
        lines.append("整体：" + "、".join(tags))
    total_opponents = max(total - 1, 0)
    all_known = total_opponents > 0 and stats.players >= total_opponents
    # 至少一半在局对手有画像才给桌级结论；9 人桌只有 2 人有数据时，
    # 「当前桌可剥削」会盖过另外 6 个未知对手。
    enough_coverage = (stats.players >= 2
                       and stats.players * 2 >= total_opponents)
    allow_exploit = (stats.scope != "current_table"
                     or all_known or enough_coverage)
    if allow_exploit:
        exploit = table_exploit_zh(stats, street=street,
                                   suppress_cbet=suppress_cbet)
        if exploit:
            label = ("当前桌可剥削" if stats.scope == "current_table"
                     else "全场可剥削")
            lines.append(label + "（程序建议）：" + exploit)
    return lines
