"""牌面事实：牌面结构、转河变化、听牌与牌面警告。

只保留**当前牌面特有**的风险（成花、公对、三条、顺子可能、花听）；
「你不是坚果 / 这是听牌」这类通用句已经在牌力段说过，不再重复。
"""

from __future__ import annotations

from ...coach import _has_run, rank_char
from ...ground_truth import GroundTruth, card_text
from ..shared.labels import _SUIT_ZH


def _board_structure_zh(gt: GroundTruth) -> str:
    """牌面结构（一行）：花色 / 顺子 / 公对 / 同花张数 / 连接 / 单张成顺。

    前三段是既有口径（成花面/花听面/彩虹面、顺子可能、公对面），文案语义不动；
    后面三段是票 70 新补的、给「按牌面选尺寸」用的轴：

      · 同花：<花色> <n> 张 —— 只在 ≥3 张（三张同花面）时出，花听已有「花听面」；
      · 连接：<n> 连       —— 公共牌最长连续点数串（3 连=顺面），A 可作 1；
      · 单张成顺（<点数>）/ 多张成顺（<n> 个点）—— 河牌没下一张牌，不渲染。

    缺数据就少一段（不编、不猜），每段自成一项，调用方拼进「牌面结构：」一行。
    """
    b = gt.board_info
    board = gt.board
    if len(board) < 3:
        return ""
    on_river = len(board) >= 5
    if b.flush_made_possible:
        flush = "成花面（同花已可能）"
    elif b.flush_draw_possible and not on_river:
        flush = "花听面（未成花）"
    elif b.flush_draw_possible:
        flush = "最多两张同花（不可能成花）"
    else:
        flush = "彩虹面（无同花可能）"
    straight = "顺子可能" if b.straight_possible else "顺子不可能"
    if not b.paired:
        paired = "公对面：否"
    elif b.quads:
        paired = "公对面：是（四条）"
    elif b.trips:
        paired = "公对面：是（三条）"
    else:
        paired = "公对面：是"
    parts = [flush, straight, paired]

    suit_run = _suit_run(board)
    if suit_run is not None and suit_run[1] >= 3:
        suit, count = suit_run
        parts.append(f"同花：{_SUIT_ZH.get(suit, suit)} {count} 张")

    ranks = {rank for rank, _suit in board}
    run = _longest_run(ranks)
    if run >= 5:
        parts.append("连接：牌面成顺")
    elif run >= 3:
        parts.append(f"连接：{run} 连")

    if not on_river:
        makers = _straight_makers(ranks)
        if len(makers) == 1:
            parts.append(f"单张成顺（{rank_char(makers[0])}）")
        elif len(makers) >= 2:
            parts.append(f"多张成顺（{len(makers)} 个点）")
    return "｜".join(parts)


def _suit_run(board) -> tuple[str, int] | None:
    """牌面最多的花色与张数；空牌面返回 None。"""
    counts: dict[str, int] = {}
    for _rank, suit in board:
        counts[suit] = counts.get(suit, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], item[0]))


def _longest_run(ranks: set[int]) -> int:
    """最长连续点数串（A 可作 1）：3=三连、5=牌面自己已经成顺。"""
    ext = set(ranks)
    if 12 in ranks:                                   # A（coach 里 A=12）当 1 用
        ext.add(-1)
    best = 0
    for low in range(-1, 13):
        length = 0
        while (low + length) in ext:
            length += 1
        best = max(best, length)
    return best


def _straight_makers(ranks: set[int]) -> list[int]:
    """再发一张什么点数会让**公共牌自己**成顺（A 可作 1 或最高张）。"""
    out: list[int] = []
    for cand in range(13):
        if cand in ranks:
            continue
        if _has_run(ranks | {cand}, 5):
            out.append(cand)
    return out


def _street_change_zh(gt: GroundTruth) -> str:
    """这一街新发的牌改变了什么（成花 / 公对面成三条 / 成对）。"""
    board = gt.board
    if len(board) < 4:
        return ""
    last, prev = board[-1], board[:-1]
    changes: list[str] = []
    suit = last[1]
    now = sum(1 for card in board if card[1] == suit)
    before = sum(1 for card in prev if card[1] == suit)
    if now >= 3 and before < 3:
        changes.append(f"{card_text(last)} 让牌面成花"
                       f"（{now} 张{_SUIT_ZH.get(suit, suit)}）")
    rank = last[0]
    same = sum(1 for card in board if card[0] == rank)
    before_same = sum(1 for card in prev if card[0] == rank)
    if same >= 4:
        changes.append(f"{card_text(last)} 让牌面出现四条")
    elif same == 3 and before_same == 2:
        changes.append(f"{card_text(last)} 让牌面出现三条")
    elif same == 2 and before_same == 1:
        changes.append(f"{card_text(last)} 让牌面成对")
    return "；".join(changes) + "。" if changes else "无"


def _draws_zh(gt: GroundTruth) -> str:
    d = gt.draws
    parts: list[str] = []
    if d.flush_draw:
        suit = _SUIT_ZH.get(str(d.flush_suit), str(d.flush_suit))
        parts.append(("坚果同花听牌" if d.nut_flush_draw else "同花听牌")
                     + f"（{suit} {d.flush_outs} outs）")
    if d.straight_draw:
        kind = "两头顺/双卡顺听牌" if d.oesd else "卡顺听牌"
        parts.append(f"{kind}（{d.straight_outs} outs）")
    return "；".join(parts) if parts else "无"


# 通用句：已经在「坚果」「牌力」「街道」里说过，再渲染只会重复并放大恐惧。
_LOW_SIGNAL = (
    "You are the ABSOLUTE NUTS", "You are the NUTS",
    "You are NOT the nuts", "This is a DRAW, not a made hand",
    "River: no more cards",
)


def _map_gt_warning(warning: str, gt: GroundTruth) -> str | None:
    """单条英文告警 → 中文；None = 不映射（低信号或未登记）。"""
    if warning.startswith("A flush is ALREADY possible"):
        return "牌面已成花：同花打败当前成牌，不能当坚果"
    if warning.startswith("The board is paired"):
        return "牌面成对：葫芦或四条可能"
    if warning.startswith("One of your two pairs is the BOARD pair"):
        # 公对只是两对里的一对；另一对如果是自己的口袋对/手牌对，
        # 仍是真两对，能拿价值。旧文案一律写「抓诈牌」，f2f 的
        # 66 在 99T 转牌就被劝成过牌。
        hero_ranks = {r for r, _ in (gt.hero or ())}
        counts: dict[int, int] = {}
        for rank, _suit in (gt.made.best_five or ()):
            counts[rank] = counts.get(rank, 0) + 1
        pair_ranks = {r for r, n in counts.items() if n >= 2}
        # 两对里只要有一对用到了 Hero 的底牌（口袋对或手牌对），
        # 就不是纯「打牌面」；只有两对都来自公面才是抓诈牌。
        own_pair = any(r in hero_ranks for r in pair_ranks)
        return ("两对里有一对来自公对面；另一对用到了你的底牌，仍是真两对，"
                "可以下注拿价值，但别当坚果（更大口袋对/顶对能压过）"
                if own_pair else
                "两对都来自公面/你只共享公对，这是抓诈牌")
    if warning.startswith("There are trips on the board"):
        return "牌面有三条：四条可能"
    if warning.startswith("An opponent can complete a straight"):
        return "对手可以用两张牌补成顺子"
    if warning.startswith("At most 2 of any suit are on the river board"):
        return "河牌最多两张同花：不存在同花"
    if warning.startswith("Only a flush DRAW is possible"):
        return "牌面最多两张同花：只有花听，没有成花"
    if warning.startswith("You are the nuts NOW"):
        return "现在是坚果，但后续第三张同花可以成花"
    if warning.startswith("Your flush draw is on a paired board"):
        return "花听在公对面上：葫芦可能已经存在，中花未必是坚果"
    return None


def render_gt_warnings_zh(gt: GroundTruth | None,
                          draw_focus: bool = False) -> list[str]:
    """把 ground_truth 的英文警告映射成中文牌面提示。

    只保留**当前牌面特有**的风险（成花、公对、三条、顺子可能、花听）。
    「你不是坚果 / 这是听牌 / 河牌没有后续牌」这类通用句已经在
    「坚果」「牌力」「街道」里说过，再渲染只会重复并放大恐惧。

    `draw_focus` 保留参数兼容旧调用；听牌的通用警告现在一律不渲染。
    未映射的告警**不进 prompt**（模型拿到「另有 N 条」也处理不了），
    只由 `unmapped_gt_warnings()` 送去日志/JSONL，留作映射表的维护信号。
    """
    if gt is None:
        return []
    on_river = len(gt.board) >= 5
    out = []
    for warning in gt.warnings:
        # 河牌没有下一张牌：花听类警告会把死听写成活听。
        if on_river and warning.startswith((
                "Only a flush DRAW is possible",
                "Your flush draw is on a paired board",
                "You are the nuts NOW",
                "An opponent can complete a straight")):
            continue
        out.append(_map_gt_warning(warning, gt))
    # 去重但保留顺序：同一条牌面风险只提醒一次。
    return list(dict.fromkeys(text for text in out if text))


def unmapped_gt_warnings(gt: GroundTruth | None) -> list[str]:
    """未登记映射的告警原文：只服务可观测性，绝不渲染。"""
    if gt is None:
        return []
    return [w for w in gt.warnings
            if not w.startswith(_LOW_SIGNAL)
            and _map_gt_warning(w, gt) is None]
