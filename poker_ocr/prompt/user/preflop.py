"""翻前信息：形态、最后加注者、范围结论、面对加注的继续范围。

翻前只按「位置 + 行动 + 范围」判断，不给 RFI 全表；面对加注时把「这手牌
在不在继续范围里、为什么」写成事实 —— 价格不等于范围，被支配的非同花高张
翻中一对常常是第二好，直接赔率兑现不了。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...hand_track import (
    PREFLOP, ActionRecord, HandSnapshot, preflop_aggressive,
)
from ...opponent_profile import AGGRESSIVE_ARCHETYPES
from ...positions import BB, POSITIONS_BY_TABLE_SIZE, SB
from ...pot_math import (
    PREFLOP_MIN_RAISE_BB, _preflop_raise_count, behind_opponents,
    call_amount, count_limpers, winnable_pot,
)
from ..shared.format import _fmt_num
from ..shared.labels import _PREFLOP_SHAPE_ZH

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation


# 面对加注时还能继续的强牌：不管多人池多紧，这些牌都不该被范围结论劝退。
_STRONG_VS_RAISE_TOKENS = ("99+", "AQs+", "AKs", "AKo", "KQs", "AQo")
# 面对 4-bet 只剩价值 4-bet/跟注范围。
_FOUR_BET_CONTINUE_TOKENS = ("QQ+", "AKs", "AKo")


def _range_width_pct(keys: set) -> float:
    """169 键集合的组合宽度（分母 1326）。仅用于事实行里的「范围有多宽」。"""
    total = 0
    for key in keys:
        total += 6 if len(key) == 2 else (4 if key[2] == "s" else 12)
    return total / 1326 * 100


def _in_keys_range(key: str, rng) -> bool | None:
    """key 是否在 combo 级范围里；范围缺失返回 None（不猜）。"""
    if rng is None:
        return None
    from ...ranges import combos_of_key
    combos = combos_of_key(key)
    return bool(combos) and any(c in rng.combo_weights for c in combos)


def _hand_shape_zh(key: str) -> str:
    """手牌结构的一句话，用来解释「为什么这手牌跟不动」。"""
    if len(key) == 2:
        return f"（口袋对 {key}）"
    high, low, kind = key[0], key[1], key[2]
    if kind == "s":
        return f"（{key} 同花）"
    from ...preflop_ranges import RANKS
    broadway = (RANKS.index(high) >= RANKS.index("T")
                and RANKS.index(low) >= RANKS.index("T"))
    if broadway:
        return f"（{key} 非同花双高张、踢脚 {low}）"
    if high == "A":
        return f"（{key} A 高、踢脚 {low} 的非同花）"
    return f"（{key} 非同花、踢脚 {low}）"


def _raiser_read(sit: "Situation", last: ActionRecord):
    """最后一手加注者的画像；没有 seat_id 时用位置回退。"""
    seat = last.seat_id
    if not seat and last.position:
        seat = next((o.seat_id for o in sit.snap.opponents
                     if o.position == last.position), None)
    if not seat:
        return None
    return next((r for r in sit.opponent_reads if r.seat_id == seat), None)


def _fourbet_tendency(read) -> tuple[str, str]:
    """对手的 4-bet/5-bet 宽度：wide / tight / unknown。

    只看实测画像；tentative 或样本太少按 unknown（默认紧范围）。
    """
    if read is None:
        return "unknown", "没有该对手画像"
    if read.gate == "tentative" or (read.n < 10 and read.source != "pin"):
        return "unknown", f"画像样本少（n={read.n}）"
    bits = [read.archetype.upper()]
    if read.threebet_pct is not None:
        bits.append(f"3-bet {read.threebet_pct:.0f}%")
    if read.fourbet_pct is not None:
        bits.append(f"4-bet {read.fourbet_pct:.0f}%")
    desc = "、".join(bits)
    if read.archetype == "nit":
        return "tight", desc
    if read.archetype == "tag":
        if ((read.threebet_pct or 0) >= 9
                or (read.fourbet_pct or 0) >= 4):
            return "wide", desc
        return "tight", desc
    if read.archetype in ("lag", "maniac"):
        return "wide", desc
    if ((read.threebet_pct or 0) >= 9
            or (read.fourbet_pct or 0) >= 4):
        return "wide", desc
    if read.threebet_pct is not None and read.threebet_pct <= 5:
        return "tight", desc
    return "unknown", desc


def _facing_raise_zh(sit: Situation) -> list[str]:
    """翻前面对加注：这手牌在不在跟注/继续范围里，以及为什么。

    旧提示词只在「无人入池/溜入」时给范围结论，面对加注完全不给 ——
    模型只能抓「赔率门槛 21.6%」这类价格事实，于是拿 K8o/QTo 的便宜赔率
    当理由跟注。价格不等于范围：被支配的非同花高张翻中一对常常是第二好，
    直接赔率兑现不了。这里把范围归属、被支配结构、多人池/没收尾的降级
    一起算成事实，模型只负责选动作。
    """
    snap = sit.snap
    if snap.street != PREFLOP or len(snap.hero_cards) != 2:
        return []
    shapes = set(snap.preflop_shapes or ())
    if not shapes & {"single-raise", "three-bet"}:
        return []
    hero_pos = (snap.hero_position or "").strip()
    if not hero_pos:
        return []
    acts = list(snap.actions_of(PREFLOP))
    raises = preflop_aggressive(acts)
    if not raises:
        return []          # 形状说有加注、动作线却没记到：不猜

    from ... import ranges as range_mod
    from ...field_ranges import bucket
    from ...preflop_ranges import hand_key

    bb = snap.blinds.bb if snap.blinds else 1.0
    key = hand_key(snap.hero_cards)
    last = raises[-1]
    raise_pos = last.position or (hero_pos if last.is_hero else None)
    if not raise_pos:
        return []       # 加注者位置读不出来：范围先验无锚点，不猜
    raise_amt = last.amount
    n_raises = len(raises)

    # 加注前的溜入者、加注后的跟注者。只认动作线，不靠下注额猜。
    first_idx = next(i for i, a in enumerate(acts) if a is raises[0])
    last_idx = next(i for i, a in enumerate(acts) if a is last)
    before, after = acts[:first_idx], acts[last_idx + 1:]
    hero_limped = any(a.is_hero and a.action == "call"
                      and float(a.amount or 0) <= bb + 1e-9 for a in before)
    other_limpers = sum(
        1 for a in before
        if not a.is_hero and a.action == "call"
        and float(a.amount or 0) <= bb + 1e-9)
    limpers = other_limpers + (1 if hero_limped else 0)
    callers = sum(1 for a in after if a.action == "call")
    behind = behind_opponents(snap)
    tight_reasons: list[str] = []
    if limpers:
        tight_reasons.append(
            f"{limpers} 人溜入（含你）" if hero_limped
            else f"{limpers} 个溜入者")
    if callers:
        tight_reasons.append(f"{callers} 人跟注这次加注")
    if behind:
        tight_reasons.append(f"{len(behind)} 人还没响应加注")
    multiway = bool(callers or limpers or behind)

    # ── 选「继续/跟注」的参照范围；4-bet/5-bet 看最后一手加注者的画像 ──
    strong = range_mod.expand_tokens(_STRONG_VS_RAISE_TOKENS)
    profile, profile_desc = _fourbet_tendency(_raiser_read(sit, last))
    tightened = False
    five_keys: set[str] = set()
    five_range = ""
    if n_raises >= 3:
        facing_level = n_raises + 1
        if profile == "wide":
            continue_tokens = ("QQ+", "AKs", "AKo")
            five_tokens = ("AA", "KK", "AKs")
            range_desc = "QQ+/AK"
            five_range = "AA/KK/AKs"
        else:
            continue_tokens = ("KK+", "AKs")
            five_tokens = ("AA", "KK")
            range_desc = "KK/AA/AKs"
            five_range = "AA/KK"
        continue_keys = range_mod.expand_tokens(continue_tokens)
        five_keys = range_mod.expand_tokens(five_tokens)
        in_range = key in continue_keys
        width = _range_width_pct(continue_keys)
        kind = f"{facing_level}-bet"
        label = f"面对 {facing_level}-bet 的继续范围（{range_desc}）"
    elif n_raises == 2:
        kind = "3-bet"
        label = "面对 3-bet 的继续范围（TT+、AQs+、AK）"
        rng = range_mod.get("3BET_CALL_VS_MP")
        in_range = key in ("AA", "KK") or _in_keys_range(key, rng)
        width = rng.width_pct if rng is not None else None
    else:
        kind = "单加注"
        order = POSITIONS_BY_TABLE_SIZE.get(snap.table_size or 0)
        hero_ip = None
        if order and raise_pos in order and hero_pos in order:
            hero_ip = order.index(hero_pos) > order.index(raise_pos)
        if hero_pos == BB:
            base_label = "BB 单加注宽防守范围"
        elif hero_pos == SB:
            base_label = "SB 无位置跟注范围"
        else:
            base_label = (f"{hero_pos} 有位置冷跟范围" if hero_ip
                          else f"{hero_pos} 无位置冷跟范围")
        base = range_mod.get(
            f"BB_DEF_VS_{bucket(raise_pos)}" if hero_pos == BB
            else f"COLD_CALL_{'IP' if hero_ip else 'OOP'}_vs_{bucket(raise_pos)}")
        base_in = _in_keys_range(key, base)
        width = base.width_pct if base is not None else None
        label = base_label
        if key in strong:
            in_range = True
        elif hero_pos == BB and multiway:
            # BB 的「宽防守」只在收尾的单挑池里成立；有溜入者/跟注者、
            # 还有人没响应时必须收紧到无位置冷跟范围。
            tight = range_mod.get(f"COLD_CALL_OOP_vs_{bucket(raise_pos)}")
            in_range = _in_keys_range(key, tight)
            tightened = True
            label = "多人池紧防守范围"
            width = tight.width_pct if tight is not None else None
        else:
            in_range = base_in

    # ── 事实行 ──
    verb = "加注到" if n_raises == 1 else "再加注到"
    amount_text = f" {_fmt_num(raise_amt)}BB" if raise_amt else ""
    spot = f"{kind}，{raise_pos or '对手'} {verb}{amount_text}"
    detail = []
    if other_limpers:
        detail.append(f"{other_limpers} 个溜入者之后")
    if hero_limped:
        detail.append("你已溜入 1BB")
    if detail:
        spot += "（" + "；".join(detail) + "）"
    if behind:
        if len(behind) > 3:
            spot += f"；本轮还有 {len(behind)} 人要响应加注"
        else:
            names = "、".join(o.position or o.seat_id for o in behind)
            spot += f"；本轮还要响应加注：{names}"
    lines = [f"面对加注：{spot}"]

    if in_range is None:
        lines.append("对加注的牌力（程序建议）：范围表缺失（对手位置/先验不可用），"
                     "只能按牌力和价格自行判断")
        return lines
    shape = _hand_shape_zh(key)
    width_text = f"（{width:.0f}%）" if width else ""
    if in_range is False and tightened:
        lines.append(f"对加注的牌力（程序建议）：{key} 在单加注收尾的宽防守里；当前有 "
                     f"{'、'.join(tight_reasons)}，按{label}{width_text}不在内"
                     f"{shape} → 默认弃牌")
    elif in_range is False:
        lines.append(f"对加注的牌力（程序建议）：{key} 不在 {label}{width_text}内"
                     f"{shape} → 默认弃牌")
    elif tightened:
        lines.append(f"对加注的牌力（程序建议）：{key} 在 {label}{width_text}内{shape}"
                     f" → 可以跟注（多人池按边缘牌处理，别把顶对当坚果）")
    elif n_raises >= 3:
        bet = f"{n_raises + 2}-bet"
        if key in five_keys:
            lines.append(f"对加注的牌力（程序建议）：{key} 在 {label}{width_text}内{shape}"
                         f" → 价值牌，可以 {bet} 或跟注"
                         f"（{profile_desc}；{bet} 范围 {five_range}）")
        else:
            lines.append(f"对加注的牌力（程序建议）：{key} 在 {label}{width_text}内{shape}"
                         f" → 可以跟注，不 {bet}"
                         f"（{profile_desc}；{bet} 默认只用 {five_range}）")
    elif n_raises == 2:
        if key in ("AA", "KK"):
            lines.append(f"对加注的牌力（程序建议）：{key} 在 {label}{width_text}内"
                         " → 价值牌，可以 4-bet 或跟注")
        elif profile == "wide" and key in ("QQ", "AKs", "AKo"):
            lines.append(f"对加注的牌力（程序建议）：{key} 在 {label}{width_text}内"
                         f" → 可以 4-bet 或跟注（{profile_desc}；"
                         "对手会拿 JJ/QQ/AK 4-bet）")
        else:
            lines.append(f"对加注的牌力（程序建议）：{key} 在 {label}{width_text}内"
                         f" → 可以跟注，不 4-bet（{profile_desc}；"
                         "4-bet 默认只用 KK/AA）")
    elif key in range_mod.expand_tokens(("QQ+", "AKs", "AKo")):
        lines.append(f"对加注的牌力（程序建议）：{key} 在 {label}{width_text}内"
                     " → 可以 3-bet 或跟注")
    else:
        tail = " → 可以跟注或 3-bet" if key in strong else " → 可以跟注"
        lines.append(f"对加注的牌力（程序建议）：{key} 在 {label}{width_text}内{tail}")

    # 价格提醒：只在「不该跟」时出现，免得诱导模型用赔率给弱牌找理由。
    if in_range is False or tightened:
        owed = call_amount(snap)
        winnable = winnable_pot(snap)
        if owed > 0 and winnable > 0:
            lines.append(f"价格：{owed / winnable * 100:.0f}% 赔率只是直接价格，"
                         f"被支配牌实现不了这么多权益，不能单独当跟注理由")
    # 只有**实测**的松凶画像才能软化范围表结论；`.table_dy` 的关键词
    # （aggressive_table）只是现场形容词，不覆盖程序算出来的范围归属。
    aggressive_reads = [
        r for r in sit.opponent_reads
        if r.archetype in AGGRESSIVE_ARCHETYPES
        and r.gate in ("read", "stable")
    ]
    if aggressive_reads:
        names = [r.display_name or r.key for r in aggressive_reads]
        who = "、".join(names[:3])
        if len(names) > 3:
            # 只点 3 个名字会被读成「就这 3 家松凶」，表里其余松凶的座位
            # 反而被这句话藏掉了。
            who += f" 等 {len(names)} 家"
        lines.append(
            f"对手画像：{who}是松凶（实测，n≥{min(r.n for r in aggressive_reads)}）"
            "；范围表是基线，「默认弃牌」不是绝对，按对手实际加注宽度调整。")
    return lines


def _pfr_label_zh(snap: HandSnapshot) -> str:
    """最后翻前加注者的三态；`hero_is_pfr` 本身只是 bool，读不出「未知」。"""
    if snap.hero_is_pfr:
        return "是（Hero 是最后一个翻前加注者）"
    pre = snap.actions_of(PREFLOP)
    if not pre:
        return "未知（翻前动作线缺失）"
    if any(a.is_hero and a.action in ("raise", "bet") for a in pre):
        return "否（Hero 加注后被再加注）"
    return "否"


def _preflop_shape_labels(snap: HandSnapshot) -> list[str]:
    """把 ``three-bet`` 这个内部上限展开成实际级别（4-bet/5-bet）。"""
    out: list[str] = []
    for shape in (snap.preflop_shapes or ()):
        if shape != "three-bet":
            out.append(_PREFLOP_SHAPE_ZH.get(shape, shape))
            continue
        raises = _preflop_raise_count(snap)
        if raises >= 4:
            out.append(f"{raises + 1}-bet")
        elif raises == 3:
            out.append("4-bet")
        elif raises == 2:
            out.append("3-bet")
        else:
            out.append("3-bet+")
    return out


def render_preflop_zh(sit: Situation) -> list[str]:
    """「翻前信息」；只在翻前渲染。面对加注给继续范围，不给 RFI 结论。"""
    if sit.street != PREFLOP:
        return []
    snap = sit.snap
    lines = ["## 翻前信息"]
    shapes = _preflop_shape_labels(snap)
    lines.append("翻前形态：" + ("、".join(shapes) if shapes else "未知"))
    lines.append("最后翻前加注者（PFR）：" + _pfr_label_zh(snap))
    if sit.range_verdict:
        lines.append(f"翻前范围结论（程序建议）：{sit.range_verdict}")
    lines.extend(_facing_raise_zh(sit))
    limpers = count_limpers(snap)
    if limpers:
        lines.append(f"前面溜入人数：{limpers}")
        if _preflop_raise_count(snap) == 0:
            iso = PREFLOP_MIN_RAISE_BB + max(0, limpers - 1)
            if snap.hero_position == BB:
                lines.append(
                    f"隔离尺寸：从「可选动作」挑最接近 {iso:g}BB 的加注档"
                    f"（{PREFLOP_MIN_RAISE_BB:g}BB 起，每多 1 个溜入者 +1BB）；"
                    "BB 可以过牌看翻牌，要抢主动权才加注，弱牌别为证明自己而加注")
            else:
                lines.append(
                    f"隔离尺寸：从「可选动作」挑最接近 {iso:g}BB 的加注档"
                    f"（{PREFLOP_MIN_RAISE_BB:g}BB 起，每多 1 个溜入者 +1BB）；"
                    "不要选跟注/补齐档")
                if snap.hero_position == SB:
                    lines.append(
                        "你在 SB：补齐只花 0.5BB 但翻后无位置；"
                        "不在隔离范围就弃，要玩就加注——别用 23o/24s 这类牌补齐")
    raises = sorted({o.amount for o in sit.options
                     if o.kind in ("raise", "bet") and o.amount is not None})
    if raises:
        if len(raises) > 1 or raises[0] > PREFLOP_MIN_RAISE_BB + 1e-9:
            lines.append(f"本桌翻前最低加注：{PREFLOP_MIN_RAISE_BB:g}BB —— 这是牌桌"
                         "下限（更小的会被客户端拒绝）；开池尺度按底池放大，"
                         "以「可选动作」的金额为准")
        else:
            lines.append(f"本桌翻前最低加注：{PREFLOP_MIN_RAISE_BB:g}BB；"
                         "当前可选开池金额都受这个下限约束")
    return lines
