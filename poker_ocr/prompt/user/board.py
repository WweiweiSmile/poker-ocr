"""牌面事实：牌面结构、转河变化、听牌与牌面警告。

只保留**当前牌面特有**的风险（成花、公对、三条、顺子可能、花听）；
「你不是坚果 / 这是听牌」这类通用句已经在牌力段说过，不再重复。
"""

from __future__ import annotations

from ...ground_truth import GroundTruth, card_text
from ..shared.labels import _SUIT_ZH


def _board_structure_zh(gt: GroundTruth) -> str:
    b = gt.board_info
    if b.flush_made_possible:
        flush = "成花面（同花已可能）"
    elif b.flush_draw_possible:
        flush = "花听面（未成花）"
    else:
        flush = "彩虹面（无同花可能）"
    straight = "顺子可能" if b.straight_possible else "顺子不可能"
    paired = "公对面：是" if b.paired else "公对面：否"
    return "｜".join((flush, straight, paired))


def _street_change_zh(gt: GroundTruth) -> str:
    """这一街新发的牌改变了什么（成花 / 公对面成三条 / 成对）。"""
    board = gt.board
    if len(board) < 4:
        return "无（翻牌没有上一街可比）"
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
        parts.append(("坚果同花听牌" if d.nut_flush_draw else "同花听牌")
                     + f"（{d.flush_suit}，{d.flush_outs} outs）")
    if d.straight_draw:
        kind = "两头顺/双卡顺听牌" if d.oesd else "卡顺听牌"
        parts.append(f"{kind}（{d.straight_outs} outs）")
    return "；".join(parts) if parts else "无"


def render_gt_warnings_zh(gt: GroundTruth | None,
                          draw_focus: bool = False) -> list[str]:
    """把 ground_truth 的英文警告映射成中文牌面提示。

    只保留**当前牌面特有**的风险（成花、公对、三条、顺子可能、花听）。
    「你不是坚果 / 这是听牌 / 河牌没有后续牌」这类通用句已经在
    「坚果」「牌力」「街道」里说过，再渲染只会重复并放大恐惧。

    `draw_focus` 保留参数兼容旧调用；听牌的通用警告现在一律不渲染。
    """
    if gt is None:
        return []
    low_signal = (
        "You are the ABSOLUTE NUTS", "You are the NUTS",
        "You are NOT the nuts", "This is a DRAW, not a made hand",
        "River: no more cards",
    )
    out: list[str] = []
    unknown = 0
    for warning in gt.warnings:
        if warning.startswith(low_signal):
            continue
        if warning.startswith("A flush is ALREADY possible"):
            text = "牌面已成花：同花打败当前成牌，不能当坚果"
        elif warning.startswith("The board is paired"):
            text = "牌面成对：葫芦或四条可能"
        elif warning.startswith("One of your two pairs is the BOARD pair"):
            # 公对只是两对里的一对；另一对如果是自己的口袋对/手牌对，
            # 仍是真两对，能拿价值。旧文案一律写「抓诈牌」，f2f 的
            # 66 在 99T 转牌就被劝成过牌。
            hero_ranks = {r for r, _ in (gt.hero if gt is not None else ())}
            counts: dict[int, int] = {}
            for rank, _suit in (gt.made.best_five if gt is not None else ()):
                counts[rank] = counts.get(rank, 0) + 1
            pair_ranks = {r for r, n in counts.items() if n >= 2}
            # 两对里只要有一对用到了 Hero 的底牌（口袋对或手牌对），
            # 就不是纯「打牌面」；只有两对都来自公面才是抓诈牌。
            own_pair = any(r in hero_ranks for r in pair_ranks)
            text = ("两对里有一对来自公对面；另一对用到了你的底牌，仍是真两对，"
                    "可以下注拿价值，但别当坚果（更大口袋对/顶对能压过）"
                    if own_pair else
                    "两对都来自公面/你只共享公对，这是抓诈牌")
        elif warning.startswith("There are trips on the board"):
            text = "牌面有三条：四条可能"
        elif warning.startswith("An opponent can complete a straight"):
            text = "对手可以用两张牌补成顺子"
        elif warning.startswith("At most 2 of any suit are on the river board"):
            text = "河牌最多两张同花：不存在同花"
        elif warning.startswith("Only a flush DRAW is possible"):
            text = "牌面最多两张同花：只有花听，没有成花"
        elif warning.startswith("You are the nuts NOW"):
            text = "现在是坚果，但后续第三张同花可以成花"
        elif warning.startswith("Your flush draw is on a paired board"):
            text = "花听在公对面上：葫芦可能已经存在，中花未必是坚果"
        else:
            unknown += 1
            continue
        out.append(text)
    if out:
        # 去重但保留顺序：同一条牌面风险只提醒一次。
        out = list(dict.fromkeys(out))
    if unknown:
        out.append(f"另有 {unknown} 条未展开的牌面提示")
    return out
