"""把场景卡渲染成提示词里的「书中计划」段。

这里只做三件事：把 `Situation` 归成 `Spot`、解析出唯一的 `Card`、渲染成
中文事实行。不做偏移，不碰 options，也不让模型看见整本书。

输出示例：

    ## 书中计划（Harrington 现金桌）
    局面: 翻牌 / 3+ 人 / 强成牌 / 无人下注
    计划: 下注 0.67 池（第 1 卷 p288，书中明确）
    理由: 多人局第二类强牌直接下注约 2/3 池
    后续: 转牌继续下注约 2/3 池，河牌按牌力收价值

零匹配时只写「书中无此局面」，不伪造页码。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .schema import Card, Plan, Spot, load_cards, resolve

CARDS_DIR = Path(__file__).resolve().parent / "cards"

BOOK_PLAN_HEADER = "## 书中计划（Harrington 现金桌）"

# 这一段进用户事实块，必须有硬上限：单张卡的文案再长也不能挤掉事实与
# 可选动作。真实卡实测 ≤300 字，这个预算只防坏卡/编译事故。
BOOK_PLAN_CHAR_LIMIT = 800

ACTION_ZH = {
    "check": "过牌",
    "bet": "下注",
    "call": "跟注",
    "raise": "加注",
    "fold": "弃牌",
    "all_in": "全下",
}

STREET_ZH = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
PLAYERS_ZH = {"2": "2 人", "3+": "3+ 人", "unknown": "人数未知"}
MADE_HAND_ZH = {
    "strong_made": "强成牌",
    "overpair": "超对",
    "top_pair_top_kicker": "顶对强踢",
    "top_pair_weak_kicker": "顶对弱踢",
    "middle_or_bottom_pair": "中/底对",
    "board_play": "打牌面",
    "draw": "听牌",
    "air": "空气",
    "unknown": "未知牌力",
}
FACING_ZH = {"none": "无人下注", "bet": "面对下注", "raise": "面对加注", "unknown": "行动未知"}
# 翻前的「面对」比翻后细：同一句「面对加注」在 3-bet 池里是另一个决策（#91）。
FACING_LEVEL_ZH = {
    "three_bet": "面对 3-bet",
    "four_bet": "面对 4-bet",
    "five_bet": "面对 5-bet",
}
HERO_ROLE_ZH = {"cold": "未加注过", "open": "本方开池", "three_bet": "本方 3-bet"}
HAND_CLASS_ZH = {
    "big_pair_aa_kk": "大口袋对（AA-KK）",
    "big_pair_qq": "大口袋对（QQ）",
    "medium_pair": "中口袋对（JJ-99）",
    "low_pair": "中口袋对（88-66）",
    "small_pair": "小口袋对",
    "ak": "AK",
    "aq": "AQ",
    "ace_broadway": "AJ-AT",
    "weak_ace": "弱 A",
    "trouble": "麻烦牌型",
    "weak_trouble": "弱麻烦牌型",
    "suited_connector": "同花连张",
    "speculative": "投机牌",
    "other": "其他牌",
}
POSITION_ZH = {"early": "前位", "middle": "中位", "late": "后位", "blind": "盲注"}
SUITED_ZH = {True: "同花", False: "非同花"}
CONFIDENCE_ZH = {"explicit": "书中明确", "interpreted": "编译归纳"}


@lru_cache(maxsize=1)
def _all_cards() -> tuple[Card, ...]:
    cards: list[Card] = []
    for path in sorted(CARDS_DIR.glob("*.json")):
        cards.extend(load_cards(path))
    return tuple(cards)


def resolve_for_situation(sit) -> tuple[Spot, Card | None]:
    """把当前事实归成 Spot，并返回唯一命中的卡；零匹配返回 None。"""
    spot = Spot.from_situation(sit)
    resolution = resolve(_all_cards(), spot)
    return spot, resolution.card


def _size_zh(plan: Plan) -> str:
    if plan.size is None:
        return ""
    size = plan.size
    if size.kind == "pct_pot":
        return f"{size.value:g} 池"
    if size.kind == "bb":
        return f"{size.value:g}BB"
    if size.kind == "table":
        return f"表:{size.table}"
    return f"{size.kind}:{size.value:g}"


def _plan_line(plan: Plan) -> str:
    action = ACTION_ZH.get(plan.action, plan.action)
    size = _size_zh(plan)
    return f"{action} {size}".strip()


def _source_zh(card: Card) -> str:
    """来源行：卷 + 每个印刷页码都带 p，多页不留孤立数字。"""
    volume = f"第 {card.volume} 卷 " if card.volume else ""
    pages = " / ".join(f"p{page}" for page in card.pages)
    confidence = CONFIDENCE_ZH.get(card.confidence, card.confidence)
    return f"{volume}{pages}，{confidence}"


def _plan_meta(card: Card) -> str:
    """计划行括号里的内容：混合策略先给主行动频率，再接来源页。

    `plan` 是主行动、`mix` 是其余分支（schema.Card.mix）；只印 mix 的频率
    会让模型把主行动读成 100%（#107）。
    """
    source = _source_zh(card)
    if not card.mix and card.plan.freq == 1.0:
        return source
    return f"频率 {card.plan.freq:.0%}，{source}"


def _mix_line(mix: Plan) -> str:
    return f"混合: {_plan_line(mix)}（频率 {mix.freq:.0%}）"


def _spot_line(spot: Spot) -> str:
    parts = [
        STREET_ZH.get(spot.street, spot.street),
        PLAYERS_ZH.get(spot.players or "", spot.players or ""),
    ]
    if spot.street == "preflop":
        # 翻前没有公共牌，made_hand 恒为 unknown；书层的手牌分类与位置分区
        # 才是这段局面的信息（见 hand_class.py）。
        parts.append(POSITION_ZH.get(spot.position_bucket or "", ""))
        parts.append(HAND_CLASS_ZH.get(spot.hand_class or "", ""))
        # 书按花色分流（前位只玩 KQs/AJs），不知道就不写，别默认非同花。
        parts.append(SUITED_ZH.get(spot.hand_suited, ""))
        # 加注层级已知就照写（书只写到 3-bet，4/5-bet 也照样如实显示层级，
        # 由卡集决定有没有计划）；认不出层级时退回粗粒度「面对加注」。
        level = FACING_LEVEL_ZH.get(spot.facing_level or "", "")
        if level:
            parts.append(level)
            parts.append(HERO_ROLE_ZH.get(spot.hero_role or "", ""))
        else:
            parts.append(FACING_ZH.get(spot.facing or "", spot.facing or ""))
    else:
        parts.append(MADE_HAND_ZH.get(spot.made_hand or "", spot.made_hand or ""))
        parts.append(FACING_ZH.get(spot.facing or "", spot.facing or ""))
    return " / ".join(part for part in parts if part)


def _cap(lines: list[str]) -> list[str]:
    """把整段压进 `BOOK_PLAN_CHAR_LIMIT`；表头/局面/计划三行优先保留。

    先丢末尾的可选行（混合 -> 后续 -> 理由），真实卡不会走到丢弃分支；
    三条主干自己就超限时（坏卡/编译事故）逐行截断，仍保持「一个元素一行」
    的契约，绝不把换行塞进单个元素（`refs.py` 按行对齐）。
    """
    if len("\n".join(lines)) <= BOOK_PLAN_CHAR_LIMIT:
        return lines
    keep, optional = lines[:3], list(lines[3:])
    while optional and len("\n".join(keep + optional)) > BOOK_PLAN_CHAR_LIMIT:
        optional.pop()
    result = keep + optional
    while len("\n".join(result)) > BOOK_PLAN_CHAR_LIMIT and len(result) > 1:
        prefix_len = len("\n".join(result[:-1])) + 1
        room = BOOK_PLAN_CHAR_LIMIT - prefix_len - 1
        if room >= 8:
            result[-1] = result[-1][:room].rstrip() + "…"
            break
        result.pop()
    if len("\n".join(result)) > BOOK_PLAN_CHAR_LIMIT:
        result[0] = result[0][: BOOK_PLAN_CHAR_LIMIT - 1] + "…"
    return result


def _card_lines(card: Card, spot: Spot) -> list[str]:
    """单张书卡的「书中计划」段；渲染与验收测试共用同一入口。"""
    lines = [
        BOOK_PLAN_HEADER,
        f"局面: {_spot_line(spot)}",
        f"计划: {_plan_line(card.plan)}（{_plan_meta(card)}）",
    ]
    if card.why:
        lines.append(f"理由: {card.why}")
    for street, future in card.plan.future.items():
        lines.append(f"后续/{STREET_ZH.get(street, street)}: {future}")
    for mix in card.mix:
        lines.append(_mix_line(mix))
    return _cap(lines)


def render_book_plan_zh(sit) -> list[str]:
    """渲染「书中计划」段；零匹配返回一行说明，不返回空列表。

    返回一行说明而不是 []，是为了让模型知道「书里没有这个局面」，而不是
    把零匹配误读成「书说可以随便打」。
    """
    spot, card = resolve_for_situation(sit)
    if card is None:
        return _cap([
            BOOK_PLAN_HEADER,
            f"局面: {_spot_line(spot)}",
            "书中无此局面：不套用任何书页，按事实与可选动作保守处理。",
        ])
    return _card_lines(card, spot)


def render_book_plan_text(sit) -> str:
    return "\n".join(render_book_plan_zh(sit))


__all__ = [
    "BOOK_PLAN_CHAR_LIMIT",
    "BOOK_PLAN_HEADER",
    "CARDS_DIR",
    "render_book_plan_text",
    "render_book_plan_zh",
    "resolve_for_situation",
]
