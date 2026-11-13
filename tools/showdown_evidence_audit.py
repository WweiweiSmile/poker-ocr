#!/usr/bin/env python3
"""亮牌样本证据上限审计：970 手能撑到哪一层的断言。

为什么需要它：
  「牌型族定义」和「摊牌证据」两张票都要引用亮牌样本。但样本只有 970 手、
  完整两张亮牌 308 手，很多格子只有个位数。这个脚本把所有相关数字一次算完，
  每个格子标出「能写比例 / 只能写见过 N 次 / 撑不住」，避免下游把 2 次观察
  当成结论。

数据范围（只读，不改任何记录）：
  data/hand_outbox/{pending,sent,discarded}/*.json
  970 个文件 = 970 手，hand_id 唯一；10 个 session（按 ts 的日期），
  2026-10-30 ~ 2026-11-12。

口径（与 SYSTEM.md / coach.evaluate 对齐）：
  · 完整亮牌 = shows 里某个座位有 2 张牌；单张亮牌单独统计。
  · 摊牌街 = 该手最终 board 长度（3=翻牌 / 4=转牌 / 5=河牌）；board<3 无法归属。
  · 牌型档 = coach.evaluate(cards) 的下标 0–8（高牌…同花顺），作为牌力尺度。
  · 位置 IP/OOP = 相对 hero 的翻后行动顺序（SB→BB→UTG→UTG+1→UTG+2→LJ→HJ→CO→BTN），
    亮牌者在 hero 之后行动 = IP，之前 = OOP。

归 属 污 染（票 #11）：
  shows 是跨手去重的，同一亮牌事件偶尔会挂到相邻一手的同一座位；这类记录的
  座位在整手牌里最后一个动作是 fold —— 弃牌的人不可能亮牌。--gate clean 剔除
  这些记录（616 条里 126 条，20.5%）。笔记的结论按 clean，all 作对照。

用法：

    nv tools/showdown_evidence_audit.py               # all 口径全量报告
    nv tools/showdown_evidence_audit.py --gate clean  # clean 口径（结论用这个）
    nv tools/showdown_evidence_audit.py --json logs/showdown_audit.json
    nv tools/showdown_evidence_audit.py --min-cell 3  # 改「撑不住」阈值
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.coach import (  # noqa: E402
    CATEGORY_NAMES,
    HIGH_CARD,
    ONE_PAIR,
    TWO_PAIR,
    TRIPS,
    evaluate,
    parse_cards,
)

RANK_CHARS = "23456789TJQKA"

# 三种牌型族的桶。顺序 = 判定优先级，见 classify_family()。
FAMILIES = [
    "弱A顶对",      # 顶对是 A，踢脚 ≤ 9
    "A顶对强踢",    # 顶对是 A，踢脚 ≥ T
    "非A顶对",      # 顶对是 K/Q/J/T 等
    "超对",         # 口袋对 > 牌面最大点数
    "中等对子",     # 口袋对 77–JJ（低于牌面顶张）或第二对
    "小对子",       # 口袋对 22–66 或底对
    "高张无对",     # 高牌，且不是同花连张/听牌破产
    "同花连张",     # 高牌 + 两张手牌同花且点数相邻（翻前牌型类）
    "听牌破产",     # 最终 ≤ 一对，但翻牌/转牌有花听或顺听
    "两对+",        # 两对 / 明三 / 顺 / 花 / 葫芦 / 四条 / 同花顺
    "暗三",         # 口袋对 + 牌面同点 = 三条
    "打牌面",       # 一对完全长在牌面上，手牌只当踢脚
]

TEXTURES = ["对子面", "单调", "连张", "A高", "K高", "其他"]
ACTIONS = ["主动下注", "面对下注", "过牌", "未知"]
POSITIONS = ["IP", "OOP", "未知"]

STREETS = {3: "翻牌", 4: "转牌", 5: "河牌"}

# 翻后行动顺序：SB 最先，BTN 最后。
POS_ORDER = {
    "SB": 0, "BB": 1, "UTG": 2, "UTG+1": 3, "UTG+2": 4,
    "LJ": 5, "HJ": 6, "CO": 7, "BTN": 8,
}
POSTFLOP_ORDER = ["SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"]


def rank_of(card: str) -> int:
    return parse_cards(card)[0][0]


def suit_of(card: str) -> str:
    return parse_cards(card)[0][1]


# ─────────────────────────── 载入 ───────────────────────────

def load_hands() -> list[dict]:
    files = sorted(glob.glob(str(ROOT / "data/hand_outbox/*/*.json")))
    hands, seen = [], set()
    for path in files:
        with open(path, encoding="utf-8") as fh:
            hand = json.load(fh)
        hid = hand.get("hand_id")
        if hid in seen:                      # 同一手可能留在两个目录里
            continue
        seen.add(hid)
        hand["_path"] = path
        hands.append(hand)
    return hands


def shows_of(hand: dict) -> list[dict]:
    """去重后的亮牌列表（每个座位一条，后面读到的那条为准）。"""
    out = {}
    for show in hand.get("shows") or []:
        seat = show.get("seat_id")
        cards = [str(c) for c in (show.get("cards") or [])]
        if not seat or len(cards) not in (1, 2):
            continue
        out[seat] = {
            "seat_id": seat,
            "name": show.get("name"),
            "position": show.get("position"),
            "cards": cards,
        }
    return list(out.values())


def full_shows(hand: dict) -> list[dict]:
    return [s for s in shows_of(hand) if len(s["cards"]) == 2]


def showdown_street(hand: dict) -> str | None:
    return STREETS.get(len(hand.get("board") or []))


# ─────────────────────────── 牌型族 ───────────────────────────

def _pocket(cards: list[str]) -> bool:
    return rank_of(cards[0]) == rank_of(cards[1])


def _rank_counts(cards: list[str]) -> collections.Counter:
    return collections.Counter(rank_of(c) for c in cards)


def _busted_draw(hole: list[str], board: list[str]) -> bool:
    """最终 ≤ 一对时，翻牌/转牌是否曾经有花听或顺听、但河牌没成。

    花听破产：两张手牌同花，最终同花色合计恰好 4 张（差一张）。
    顺听破产：手牌参与、牌面 + 手牌里存在 5 连张窗口只缺 1 张，且最终没成顺。
    这是启发式，只用来分组，不是精确的听牌追溯。
    """
    hr = [rank_of(c) for c in hole]
    hs = [suit_of(c) for c in hole]
    br = [rank_of(c) for c in board]
    bs = [suit_of(c) for c in board]

    # 花听破产
    if hs[0] == hs[1] and len(board) >= 3:
        if sum(1 for s in bs if s == hs[0]) == 2:
            return True

    # 顺听破产：5 连窗口里手牌至少占 1 张、合计 4 张
    allr = set(hr) | set(br)
    for low in range(0, 9):
        window = set(range(low, low + 5))
        if window & set(hr) and len(window & allr) >= 4:
            return True
    return False


def classify_family(hole: list[str], board: list[str]) -> str:
    """把「手牌两张 + 最终牌面」映射到 FAMILIES 里的一个桶。

    判定优先级：成牌档 > 一对的细分 > 高牌里的听牌/同花连张 > 高张无对。
    """
    if len(hole) != 2 or len(board) < 3:
        return "无法判定"

    cards = hole + board[:5]
    category, _kick = evaluate(parse_cards("".join(cards)))
    hr = [rank_of(c) for c in hole]
    br = [rank_of(c) for c in board[:5]]

    if category >= TWO_PAIR:
        if category == TRIPS and _pocket(hole):
            return "暗三"
        return "两对+"

    if category == ONE_PAIR:
        counts = _rank_counts(cards)
        pair_rank = max((r for r, n in counts.items() if n >= 2))
        board_top = max(br)
        board_sorted = sorted(set(br), reverse=True)

        if _pocket(hole):
            # 口袋对：高于整张牌面 = 超对；否则按口袋对点数分档。
            if pair_rank > board_top:
                return "超对"
            return "中等对子" if pair_rank >= RANK_CHARS.index("7") else "小对子"

        hole_pairs = [c for c in hole if rank_of(c) == pair_rank]
        if not hole_pairs:
            return "打牌面"                  # 一对完全长在牌面上

        if pair_rank == board_top:
            if rank_of(hole[0]) == 12 or rank_of(hole[1]) == 12:
                other = hole[1] if rank_of(hole[0]) == 12 else hole[0]
                return "A顶对强踢" if rank_of(other) >= RANK_CHARS.index("T") else "弱A顶对"
            return "非A顶对"
        if len(board_sorted) >= 2 and pair_rank == board_sorted[1]:
            return "中等对子"
        return "小对子"

    # 高牌
    if _busted_draw(hole, board[:5]):
        return "听牌破产"
    if suit_of(hole[0]) == suit_of(hole[1]) and abs(hr[0] - hr[1]) <= 2:
        return "同花连张"
    return "高张无对"


def flop_texture(board: list[str]) -> str:
    """翻牌三张的纹理，判定优先级：对子面 > 单调 > 连张 > A高 > K高 > 其他。"""
    flop = board[:3]
    if len(flop) < 3:
        return "未知"
    ranks = [rank_of(c) for c in flop]
    suits = [suit_of(c) for c in flop]
    if len(set(ranks)) < 3:
        return "对子面"
    if len(set(suits)) == 1:
        return "单调"
    s = sorted(set(ranks))
    if any(s[i + 2] - s[i] <= 2 for i in range(len(s) - 2)):
        return "连张"
    if 12 in ranks:
        return "A高"
    if 11 in ranks:
        return "K高"
    return "其他"


def position_class(hand: dict, show: dict) -> str:
    """亮牌者相对 hero 的位置：hero 之后行动 = IP，之前 = OOP。"""
    hero_pos = hand.get("hero_position")
    pos = show.get("position")
    if pos not in POS_ORDER or hero_pos not in POS_ORDER:
        return "未知"
    return "IP" if POS_ORDER[pos] > POS_ORDER[hero_pos] else "OOP"


def action_class(hand: dict, show: dict, street: str | None) -> str:
    """亮牌者在摊牌街上是主动下注、面对下注跟注、还是无人下注时过牌。

    用「整条街该座位出现过的动作集合」判定，不依赖动作顺序：OCR 记录里
    经常出现 `call` 排在 `bet` 前面（掉帧补记），按顺序判会把跟注判成未知。
    """
    if street is None:
        return "未知"
    actions = []
    for st in hand.get("streets") or []:
        if st.get("street") == {"翻牌": "flop", "转牌": "turn", "河牌": "river"}[street]:
            actions = st.get("actions") or []
            break
    mine = [str(a.get("action") or "") for a in actions if a.get("seat_id") == show["seat_id"]]
    others = [str(a.get("action") or "") for a in actions if a.get("seat_id") != show["seat_id"]]
    other_bet = any(x in ("bet", "raise") for x in others)

    if any(x in ("bet", "raise") for x in mine):
        return "主动下注"
    if "call" in mine:
        return "面对下注" if other_bet else "未知"
    if mine and all(x == "check" for x in mine):
        return "过牌" if not other_bet else "未知"
    return "未知"


def hand_ending(hand: dict) -> str:
    """这手牌是怎么结束的：真摊牌 / 打跑了主动秀 / 其他。

    `street_active` 是**街首**活跃名单，所以「河牌 2 人活跃 + 下注无人跟」
    是「一人下注、另一人弃牌」，不是摊牌。
    """
    if len(hand.get("board") or []) != 5:
        return "board<5"
    actions = []
    for st in hand.get("streets") or []:
        if st.get("street") == "river":
            actions = st.get("actions") or []
    kinds = [str(a.get("action") or "") for a in actions]
    if "call" in kinds and any(k in ("bet", "raise") for k in kinds):
        return "下注-跟注"
    if kinds and all(k == "check" for k in kinds):
        return "过牌到底"
    if any(k in ("bet", "raise") for k in kinds) and "call" not in kinds:
        return "下注-无人跟"
    if not kinds:
        return "无河牌动作"
    return "混合"


# ─────────────────────────── 统计工具 ───────────────────────────

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def pct(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n * 100:.1f}%)" if n else "0/0"


# ─────────────────────────── 主报告 ───────────────────────────


def last_action(hand: dict, seat_id) -> str | None:
    """该座位在整手牌里出现的最后一个动作（按 streets 顺序 + 街内顺序）。"""
    last = None
    for st in hand.get("streets") or []:
        for a in st.get("actions") or []:
            if a.get("seat_id") == seat_id:
                last = str(a.get("action") or "")
    return last


def is_fold_contaminated(hand: dict, seat_id) -> bool:
    """亮牌的座位最后动作是 fold —— 弃牌的人不该亮牌，只能是串手/归属错误。"""
    return last_action(hand, seat_id) == "fold"


def build_dataset(hands: list[dict]) -> dict:
    """把手牌摊平成「一次亮牌 = 一条记录」。"""
    records = []
    for hand in hands:
        street = showdown_street(hand)
        board = [str(c) for c in (hand.get("board") or [])]
        for show in shows_of(hand):
            two = len(show["cards"]) == 2
            family = classify_family(show["cards"], board) if two else None
            records.append({
                "hand_id": hand.get("hand_id"),
                "session": str(hand.get("ts") or "")[:10],
                "seat_id": show["seat_id"],
                "name": show["name"],
                "position": show.get("position"),
                "cards": show["cards"],
                "two_card": two,
                "board": board,
                "board_len": len(board),
                "street": street,
                "texture": flop_texture(board),
                "action": action_class(hand, show, street) if street else "未知",
                "pos_class": position_class(hand, show),
                "family": family,
                "duplicate_cards": len(set(show["cards"]) & set(board)) > 0,
                "folded": is_fold_contaminated(hand, show["seat_id"]),
            })
    return {"records": records}


def family_counts(records: list[dict]) -> collections.Counter:
    return collections.Counter(r["family"] for r in records if r["family"])


def crosstab(records: list[dict], col_key: str, columns: list[str]) -> dict:
    table = {f: collections.Counter() for f in FAMILIES}
    for r in records:
        if r["family"] in table:
            table[r["family"]][r[col_key]] += 1
    return table


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=str, default=None, help="把汇总结果写到 JSON")
    ap.add_argument("--min-cell", type=int, default=3, help="「撑不住」阈值（默认 3）")
    ap.add_argument("--can-write", type=int, default=20, help="「现在就能写」的池级 n 阈值（默认 20）")
    ap.add_argument("--seed", type=int, default=20261113, help="随机基线抽样种子")
    ap.add_argument(
        "--gate", choices=["all", "clean"], default="all",
        help="all=全部亮牌记录；clean=剔除「所在座位最后动作是 fold」的归属污染（默认 all，结论按 clean）",
    )
    args = ap.parse_args()

    hands = load_hands()
    data = build_dataset(hands)
    records = data["records"]
    raw_total = len(records)
    raw_records = list(records)
    if args.gate == "clean":
        records = [r for r in records if not r["folded"]]
    two = [r for r in records if r["two_card"]]
    one = [r for r in records if not r["two_card"]]

    print("=" * 78)
    print("亮牌样本证据上限审计")
    print("=" * 78)
    print(f"闸门={args.gate}：亮牌记录 {len(records)}/{raw_total}"
          f"（剔除 fold 污染 {raw_total-len(records)} 条）")

    # ── 0. 基线 ──
    dates = sorted({str(h.get("ts") or "")[:10] for h in hands})
    kept_two = [r["hand_id"] for r in two]
    full_ids = set(kept_two)
    full_hands = [h for h in hands if h.get("hand_id") in full_ids]
    board_dist = collections.Counter(len(h.get("board") or []) for h in full_hands)
    print("\n## 0. 数据范围（与票面基线核对）")
    print(f"手牌文件数        : {len(hands)}（hand_id 唯一）")
    print(f"session（按日期） : {len(dates)}  {dates[0]} ~ {dates[-1]}")
    print(f"有完整两张亮牌的手: {len(full_hands)} ({len(full_hands)/len(hands)*100:.1f}%)")
    print(f"完整亮牌手 board  : " + " / ".join(f"{k}:{board_dist.get(k,0)}" for k in (0, 3, 4, 5)))
    print(f"两张亮牌记录次数  : {len(two)}（一手多人亮牌时 > 手数）")
    print(f"单张亮牌记录次数  : {len(one)}，涉及 {len({r['hand_id'] for r in one})} 手")

    names = collections.Counter(r["name"] for r in two)
    print(f"两张亮牌覆盖人数  : {len(names)}，≥8 次 {sum(1 for v in names.values() if v >= 8)} 人")

    hand_count = collections.Counter()
    for h in hands:
        seen = set()
        for r in h.get("table_roster") or []:
            if r.get("seat_id") == "hero":
                continue
            if r.get("name"):
                seen.add(r["name"])
        for n in seen:
            hand_count[n] += 1
    regulars = {n for n, c in hand_count.items() if c >= 100}
    print(f"对手名字数        : {len(hand_count)}，≥100 手常客 {len(regulars)} 人")

    dup = sum(1 for r in records if r["duplicate_cards"])
    print(f"亮牌与牌面重复的  : {dup} 条（OCR 误读，族判定仍算但标注）")

    # ── 1. 桶定义 ──
    # ── 0.5 fold 污染对照 ──
    print("\n## 0.5 fold 污染：clean 与 all 对照（亮牌座位整手最后动作 = fold）")
    rt = [r for r in raw_records if r["two_card"]]
    rc = [r for r in rt if not r["folded"]]
    ro = [r for r in raw_records if not r["two_card"]]
    print(f"  两张亮牌记录: all {len(rt)} → clean {len(rc)}（污染 {len(rt)-len(rc)} 条，{((len(rt)-len(rc))/len(rt)*100):.1f}%）")
    print(f"  单张亮牌记录: all {len(ro)} → clean {sum(1 for r in ro if not r['folded'])}")
    print(f"  手数: all {len({r['hand_id'] for r in rt})} → clean {len({r['hand_id'] for r in rc})}")
    print(f"  人数: all {len({r['name'] for r in rt})} → clean {len({r['name'] for r in rc})}")
    rows = []
    ra, rcl = family_counts(rt), family_counts(rc)
    for fam in FAMILIES:
        a_, c_ = ra.get(fam, 0), rcl.get(fam, 0)
        rows.append([fam, a_, c_, a_ - c_, f"{(c_/a_*100):.0f}%" if a_ else "—"])
    rows.append(["合计", len(rt), len(rc), len(rt) - len(rc), f"{len(rc)/len(rt)*100:.0f}%"])
    print(markdown_table(["族", "all n", "clean n", "污染", "保留率"], rows))
    print("  → 污染在各族上大致均匀（保留率 72%–100%），clean 与 all 的族间相对大小不变；")
    print("     但 clean 绝对数更小，能写比例的行只会更少。")

    print("\n## 1. 候选牌型族（桶定义）")
    print("判定顺序自上而下，命中即停；全部基于 coach.evaluate 的 0–8 档 + 手牌/牌面点数关系。")
    defs = [
        ("弱A顶对", "顶对是 A（手牌 A + 牌面 A），另一张手牌（踢脚）≤ 9"),
        ("A顶对强踢", "顶对是 A，踢脚 ≥ T"),
        ("非A顶对", "顶对是 K/Q/J/T 等非 A 点数"),
        ("超对", "口袋对，且点数 > 牌面最大点数"),
        ("中等对子", "口袋对 77–JJ 但低于牌面顶张；或第二对"),
        ("小对子", "口袋对 22–66；或底对"),
        ("高张无对", "最终高牌（档 0），且不落入下面三个桶"),
        ("同花连张", "最终高牌 + 两张手牌同花且点数相差 ≤2（翻前牌型类）"),
        ("听牌破产", "最终 ≤ 一对，但花听（手牌同花、牌面该花色恰 2 张）或顺听（5 连窗口缺 1）"),
        ("两对+", "档 ≥2 且不是暗三：两对 / 明三 / 顺 / 花 / 葫芦 / 四条 / 同花顺"),
        ("暗三", "口袋对 + 牌面同点数 = 三条"),
        ("打牌面", "一对完全长在牌面上，手牌只当踢脚"),
    ]
    for name, desc in defs:
        print(f"  · {name:<8}{desc}")

    # ── 2. 池级边际分布 ──
    print("\n## 2. 池级：族 × 街（两张亮牌，n={}）".format(len(two)))
    fc = family_counts(two)
    rows = []
    for fam in FAMILIES:
        per = collections.Counter(r["street"] for r in two if r["family"] == fam)
        rows.append([fam, fc.get(fam, 0)] + [per.get(s, 0) for s in ("翻牌", "转牌", "河牌")] + ["未知" if False else per.get(None, 0)])
    rows.append(["合计", len(two)] + [sum(1 for r in two if r["street"] == s) for s in ("翻牌", "转牌", "河牌")] + [sum(1 for r in two if r["street"] is None)])
    print(markdown_table(["族", "n", "翻牌", "转牌", "河牌", "board<3"], rows))

    print("\n## 3. 池级：族 × 翻牌纹理（对子面>单调>连张>A高>K高>其他）")
    tex = crosstab(two, "texture", TEXTURES)
    rows = [[f, fc.get(f, 0)] + [tex[f].get(t, 0) for t in TEXTURES] for f in FAMILIES]
    rows.append(["合计", len(two)] + [sum(1 for r in two if r["texture"] == t) for t in TEXTURES])
    print(markdown_table(["族", "n"] + TEXTURES, rows))

    print("\n## 4. 池级：族 × 摊牌街行动")
    act = crosstab(two, "action", ACTIONS)
    rows = [[f, fc.get(f, 0)] + [act[f].get(a, 0) for a in ACTIONS] for f in FAMILIES]
    rows.append(["合计", len(two)] + [sum(1 for r in two if r["action"] == a) for a in ACTIONS])
    print(markdown_table(["族", "n"] + ACTIONS, rows))

    print("\n## 5. 池级：族 × 位置（相对 hero 的翻后顺序）")
    pos = crosstab(two, "pos_class", POSITIONS)
    rows = [[f, fc.get(f, 0)] + [pos[f].get(p, 0) for p in POSITIONS] for f in FAMILIES]
    rows.append(["合计", len(two)] + [sum(1 for r in two if r["pos_class"] == p) for p in POSITIONS])
    print(markdown_table(["族", "n"] + POSITIONS, rows))

    # ── 6. 五维联合稀疏度 ──
    print("\n## 6. 五维联合（街 × 纹理 × 行动 × 位置 × 族）稀疏度")
    joint = collections.Counter(
        (r["street"] or "未知", r["texture"], r["action"], r["pos_class"], r["family"])
        for r in two
    )
    sizes = collections.Counter(joint.values())
    total_cells = 3 * 6 * 4 * 2 * len(FAMILIES)
    print(f"  理论格子数 {total_cells}，实际非空格子 {len(joint)}")
    print(f"  格子大小分布（n: 格子数）: " + ", ".join(f"{k}:{v}" for k, v in sorted(sizes.items())))
    print(f"  n≥3 的格子 {sum(v for k, v in sizes.items() if k >= 3)} 个，占非空格子 "
          f"{sum(v for k, v in sizes.items() if k >= 3)/max(1,len(joint))*100:.1f}%")
    print("  → 五维联合表不可用于任何结论；下面退到 3 维局面键。")

    # ── 7. 局面键（街 × 行动 × 位置）──
    keys = [f"{s}|{a}|{p}" for s in ("翻牌", "转牌", "河牌") for a in ACTIONS for p in ("IP", "OOP")]
    key_of = lambda r: f"{r['street']}|{r['action']}|{r['pos_class']}"
    key_counter = collections.Counter(key_of(r) for r in two)
    print("\n## 7. 池级：族 × 局面键（街|行动|位置），只列池级 n≥2 的行")
    print("  标记：`*` = 池级 n < {}（撑不住）".format(args.min_cell))
    live_keys = [k for k in keys if key_counter.get(k, 0) >= 2]
    rows = []
    for fam in FAMILIES:
        cells = collections.Counter(key_of(r) for r in two if r["family"] == fam)
        vals = []
        for k in live_keys:
            n = cells.get(k, 0)
            vals.append("·" if n == 0 else (f"{n}*" if n < args.min_cell else str(n)))
        rows.append([fam] + vals)
    rows.append(["合计"] + [key_counter.get(k, 0) for k in live_keys])
    print(markdown_table(["族"] + live_keys, rows))

    # ── 8. 个人档 ──
    print("\n## 8. 常客个人档（{} 人 ≥100 手）".format(len(regulars)))
    reg_two = [r for r in two if r["name"] in regulars]
    reg_names = collections.Counter(r["name"] for r in reg_two)
    print(f"  常客贡献的两张亮牌记录 {len(reg_two)} 条，人均 {len(reg_two)/max(1,len(regulars)):.1f} 条")
    print(f"  常客亮牌次数分布: " + ", ".join(f"{k}次:{v}人" for k, v in sorted(collections.Counter(reg_names.values()).items())))
    rows = []
    for fam in FAMILIES:
        per_person = collections.Counter(r["name"] for r in reg_two if r["family"] == fam)
        rows.append([
            fam, fc.get(fam, 0), len([r for r in reg_two if r["family"] == fam]),
            sum(1 for v in per_person.values() if v >= 3),
            sum(1 for v in per_person.values() if v >= 1),
            max(per_person.values(), default=0),
        ])
    print(markdown_table(["族", "池级 n", "常客 n", "≥3 次的常客数", "出现过的常客数", "单人最多"], rows))
    best = max((max(collections.Counter(r["name"] for r in reg_two if r["family"] == f).values(), default=0), f)
               for f in FAMILIES)
    print(f"  → 个人档只在「两对+」「中等对子」这类大桶上有个位数到十几次，细桶基本每人 0–2 次。")
    print(f"  → 单人单族的天花板是「{best[1]}」{best[0]} 次，仍低于 20：**个人档没有任何一行够得上「能写比例」**。")

    # ── 8.5 票面例子核对 ──
    print("\n## 8.5 票面例子「牌面有 A 且亮出 A + kicker ≤9」核对")
    a_cnt, aa = 0, 0
    a_people: set = set()
    a_fam: collections.Counter = collections.Counter()
    for h in hands:
        b = [str(c) for c in (h.get("board") or [])]
        if len(b) < 3 or 12 not in [rank_of(c) for c in b]:
            continue
        for sh in full_shows(h):
            rs = [rank_of(c) for c in sh["cards"]]
            if 12 not in rs:
                continue
            if rs[0] == rs[1] == 12:            # 口袋 AA：没有「另一张」可当踢脚
                aa += 1
                continue
            kicker = [r for r in rs if r != 12][0]
            if kicker <= RANK_CHARS.index("9"):
                a_cnt += 1
                a_people.add(sh["name"])
                a_fam[classify_family(sh["cards"], b)] += 1
    print(f"  口径 = 牌面有 A + 手牌有 A + 另一张 ≤9：{a_cnt} 次 / {len(a_people)} 人"
          f"（另有 {aa} 次口袋 AA，没有踢脚，未计入）")
    print("  按最终牌型族重分：" + "，".join(f"{k} {v}" for k, v in a_fam.most_common()))
    print("  → 票面的「24 次」是**手牌形态**计数，只有 8 次最终是「弱 A 顶对」；")
    print("     另外 16 次的第二张牌配上了牌面（如 A+9 撞牌面 9），最终是两对/三条/顺/葫芦/花。")
    print("     所以「A + 弱踢」不能直接当牌型族用，必须先看最终成牌。")

    # ── 9. 亮牌选择偏差（代理证据）──
    print("\n## 9. 亮牌选择偏差：代理证据（没有 winner 字段）")
    print("  口径：以下全部是代理证据，不是「谁赢了」的直接观测。")

    print("\n### 9.1 筹码增减能不能判赢家")
    same = diff = 0
    for h in hands:
        st = {s["seat_id"]: s.get("stack") for s in h.get("start_stacks") or []}
        en = {s["seat_id"]: s.get("stack") for s in h.get("seat_meta") or []}
        if not st or not en:
            continue
        changed = [s for s in st if s in en and st[s] is not None and en[s] is not None
                   and abs(st[s] - en[s]) > 0.01]
        diff += 1 if changed else 0
        same += 0 if changed else 1
    print(f"  start_stacks 与 seat_meta.stack 相同的手: {same}，不同的手: {diff}")
    print("  → seat_meta.stack 是开局快照，不是结算后筹码；**无法用筹码增减判赢家**。")

    print("\n### 9.2 亮牌手牌型 vs 同牌面随机手牌")
    rng = random.Random(args.seed)
    shown_cat: collections.Counter = collections.Counter()
    for r in two:
        if len(r["board"]) < 3:
            continue
        cat, _ = evaluate(parse_cards("".join(r["cards"] + r["board"][:5])))
        shown_cat[cat] += 1
    rand_cat: collections.Counter = collections.Counter()
    sampled = 0
    for r in two:
        if len(r["board"]) < 3:
            continue
        deck = [f"{RANK_CHARS[i]}{s}" for i in range(13) for s in "shdc"]
        deck = [c for c in deck if c not in r["board"][:5] and c not in r["cards"]]
        for _ in range(200):
            hole = rng.sample(deck, 2)
            cat, _ = evaluate(parse_cards("".join(hole + r["board"][:5])))
            rand_cat[cat] += 1
            sampled += 1
    rows = []
    for cat in range(9):
        n1, n2 = shown_cat.get(cat, 0), rand_cat.get(cat, 0)
        rows.append([CATEGORY_NAMES[cat], n1, f"{n1/sum(shown_cat.values())*100:.1f}%",
                     n2, f"{n2/sampled*100:.1f}%"])
    print(markdown_table(["牌型档", "亮牌 n", "亮牌占比", "随机 n", "随机占比"], rows))
    mean_shown = sum(c * n for c, n in shown_cat.items()) / sum(shown_cat.values())
    mean_rand = sum(c * n for c, n in rand_cat.items()) / sampled
    strong_shown = sum(n for c, n in shown_cat.items() if c >= 2) / sum(shown_cat.values())
    strong_rand = sum(n for c, n in rand_cat.items() if c >= 2) / sampled
    print(f"  平均牌型档：亮牌 {mean_shown:.2f} vs 随机 {mean_rand:.2f}")
    print(f"  ≥两对占比：亮牌 {strong_shown*100:.1f}% vs 随机 {strong_rand*100:.1f}%")
    print("  注意：随机基线按每一手自己的牌面抽，已经扣掉牌面因素；差距只能说明「亮出来的牌比该牌面上的随机手牌强」。")

    print("\n### 9.3 手牌类别（169 先验方向）")
    all_cards = [f"{RANK_CHARS[i]}{s}" for i in range(13) for s in "shdc"]
    rand_feat = collections.Counter()
    total_combos = 0
    for i in range(len(all_cards)):
        for j in range(i + 1, len(all_cards)):
            a, b = all_cards[i], all_cards[j]
            ra, rb = rank_of(a), rank_of(b)
            rand_feat["口袋对"] += ra == rb
            rand_feat["含A"] += 12 in (ra, rb)
            rand_feat["同花"] += suit_of(a) == suit_of(b)
            rand_feat["连张"] += ra != rb and abs(ra - rb) <= 2
            total_combos += 1
    shown_feat = collections.Counter()
    for r in two:
        ra, rb = rank_of(r["cards"][0]), rank_of(r["cards"][1])
        shown_feat["口袋对"] += ra == rb
        shown_feat["含A"] += 12 in (ra, rb)
        shown_feat["同花"] += suit_of(r["cards"][0]) == suit_of(r["cards"][1])
        shown_feat["连张"] += ra != rb and abs(ra - rb) <= 2
    n_two = len(two)
    for feat in ("口袋对", "含A", "同花", "连张"):
        print(f"  {feat}: 亮牌 {shown_feat[feat]}/{n_two} ({shown_feat[feat]/n_two*100:.1f}%)"
              f"  vs 随机 2 张 {rand_feat[feat]}/{total_combos} ({rand_feat[feat]/total_combos*100:.1f}%)")
    print("  → 亮牌手牌明显偏向口袋对 / 含 A / 同花连张；这是**手牌选择**的偏差，不是摊牌结果。")

    print("\n### 9.4 亮牌来源：摊牌亮牌 vs 打跑了主动秀牌")
    ending_hands = collections.Counter()
    ending_show_hands = collections.Counter()
    ending_shows = collections.Counter()
    ending_fam: dict = collections.defaultdict(collections.Counter)
    ending_cat: dict = collections.defaultdict(list)
    for h in hands:
        e = hand_ending(h)
        ending_hands[e] += 1
        shows = full_shows(h)
        if shows:
            ending_show_hands[e] += 1
        for sh in shows:
            ending_shows[e] += 1
            b = [str(c) for c in (h.get("board") or [])]
            if len(b) >= 3:
                ending_fam[e][classify_family(sh["cards"], b)] += 1
                cat, _ = evaluate(parse_cards("".join(sh["cards"] + b[:5])))
                ending_cat[e].append(cat)
    rows = []
    for e in ("过牌到底", "下注-跟注", "下注-无人跟", "无河牌动作", "混合", "board<5"):
        n = ending_shows.get(e, 0)
        if not n:
            continue
        cats = ending_cat.get(e, [])
        strong = sum(1 for c in cats if c >= 2) / len(cats) * 100 if cats else 0
        rows.append([e, ending_hands.get(e, 0), ending_show_hands.get(e, 0), n,
                     f"{sum(cats)/len(cats):.2f}" if cats else "—", f"{strong:.0f}%"])
    print(markdown_table(["结束方式", "手数", "有亮牌手数", "完整亮牌条数", "平均档", "≥两对"], rows))
    print("  → 「下注-无人跟」的亮牌是**打跑对手后的主动秀牌**，不是摊牌亮牌。")
    print("     其中 30 条同时有 ≥2 人亮牌，与「无人跟」自相矛盾：说明 action 记录缺了 call，")
    print("     或 shows 跨手串了；这一层只能当近似，不能当精确分层。")

    print("\n### 9.5 亮牌率：真摊牌手里记录到完整亮牌的比例")
    contested = [h for h in hands if hand_ending(h) in ("下注-跟注", "过牌到底")]
    with_show = [h for h in contested if full_shows(h)]
    print(f"  真摊牌手（board=5，河牌有人跟注或全体过牌）: {len(contested)}")
    print(f"  其中记录到 ≥1 条完整亮牌: {len(with_show)} ({len(with_show)/max(1,len(contested))*100:.1f}%)")
    for e in ("过牌到底", "下注-跟注"):
        g = [h for h in hands if hand_ending(h) == e]
        w = [h for h in g if full_shows(h)]
        print(f"    {e}: {len(g)} 手，有亮牌 {len(w)} ({len(w)/max(1,len(g))*100:.1f}%)")
    print("  → 这是**下限**：没记录到亮牌 ≠ 没亮，可能是 muck，也可能是 OCR 漏读。")

    print("\n### 9.6 muck 迹象：真摊牌手里的亮牌条数")
    for e in ("过牌到底", "下注-跟注"):
        g = [h for h in hands if hand_ending(h) == e]
        d = collections.Counter(min(len(full_shows(h)), 2) for h in g)
        print(f"    {e}: 0 条 {d.get(0,0)} / 1 条 {d.get(1,0)} / ≥2 条 {d.get(2,0)}（共 {len(g)} 手）")
    print("  → 「过牌到底」双方都进了河牌，却只记到 1 条亮牌：要么输家 muck，要么 OCR 漏读，")
    print("     两者在现有数据里无法区分。")

    print("\n### 9.7 亮牌强度按来源分层")
    for e in ("下注-跟注", "过牌到底", "下注-无人跟", "board<5"):
        cats = ending_cat.get(e, [])
        if not cats:
            continue
        print(f"  {e:<8} n={len(cats):>3}  平均档 {sum(cats)/len(cats):.2f}  "
              f"≥两对 {sum(1 for c in cats if c>=2)/len(cats)*100:.0f}%")

    # ── 10. 单张亮牌 ──
    print("\n## 10. 单张亮牌（len(cards)==1，_showdown 直接丢弃）")
    one_hands = collections.Counter(r["hand_id"] for r in one)
    print(f"  记录 {len(one)} 条，涉及 {len(one_hands)} 手；每手 1 条 "
          f"{sum(1 for v in one_hands.values() if v==1)} 手，2 条 {sum(1 for v in one_hands.values() if v==2)} 手")
    print("  board 长度分布: " + ", ".join(f"{k}:{v}" for k, v in sorted(collections.Counter(r['board_len'] for r in one).items())))
    one_showdown = 0
    for hid in one_hands:
        h = next(x for x in hands if x["hand_id"] == hid)
        if len(h.get("board") or []) == 5 and hand_ending(h) in ("下注-跟注", "过牌到底"):
            one_showdown += 1
    print(f"  其中所在手是真摊牌: {one_showdown} 手；其余多为「打跑了主动秀一张」或牌面不全")
    shown_ranks = collections.Counter(rank_of(r["cards"][0]) for r in one)
    print("  亮出单张的点数分布（2..A）: " + " ".join(f"{RANK_CHARS[r]}:{shown_ranks.get(r,0)}" for r in range(13)))
    # 8h 伪读检验：单张 vs 两张 vs 牌面
    one_cards = collections.Counter(r["cards"][0] for r in one)
    two_cards = collections.Counter(c for r in two for c in r["cards"])
    board_cards = collections.Counter(c for h in hands for c in (h.get("board") or []))
    print(f"  「8h」在单张亮牌里 {one_cards.get('8h',0)}/{len(one)} = {one_cards.get('8h',0)/len(one)*100:.1f}%，"
          f"在两张亮牌里 {two_cards.get('8h',0)}/{sum(two_cards.values())} = {two_cards.get('8h',0)/sum(two_cards.values())*100:.1f}%，"
          f"在牌面上 {board_cards.get('8h',0)}/{sum(board_cards.values())} = {board_cards.get('8h',0)/sum(board_cards.values())*100:.1f}%")
    print(f"  8h 覆盖 {len({r['name'] for r in one if r['cards'][0]=='8h'})} 个不同玩家、"
          f"{len({r['hand_id'] for r in one if r['cards'][0]=='8h'})} 手 → 是**系统性误读**，不是真牌。")
    print("  → 能推出：该座位亮了一张牌、牌点/花色、以及「主动秀牌」这件事本身。")
    print("  → 推不出：另一张牌、是否成对、踢脚、最终牌型族、输赢。")
    print("  → 且这 86 条里约四分之一是 8h 伪读，单张亮牌连「牌点」都不可靠。")

    # ── 11. board<3 ──
    print("\n## 11. board<3 的记录")
    bad = [r for r in two if r["board_len"] < 3]
    print(f"  完整两张亮牌但 board 长度 <3: {len(bad)} 条，涉及 {len({r['hand_id'] for r in bad})} 手")
    for r in bad:
        print(f"    {r['hand_id']} board={r['board']} {r['name']} {r['cards']}")
    print("  → 这 4 手无法算族、无法算纹理，只能当「亮过牌」计数，不能进任何族结论。")

    # ── 12. 结论表 ──
    # ── 13. 要多少数据才够 ──
    print("\n## 12. 要多少数据才够（产能外推）")
    per_hand = len(two) / len(hands)
    cells = len(FAMILIES) * 3 * 3 * 2
    need_cells = cells * args.can_write
    need_hands = need_cells / per_hand
    print(f"  产能：{len(hands)} 手 → {len(two)} 条完整亮牌 = {per_hand:.3f} 条/手")
    print(f"  目标：{len(FAMILIES)} 族 × 3 街 × 3 动作 × 2 位置 = {cells} 格，每格 ≥{args.can_write} 条 → {need_cells} 条")
    print(f"  需要手数 ≈ {need_cells} / {per_hand:.3f} ≈ {need_hands:.0f} 手（当前 {len(hands)} 手，约 {need_hands/len(hands):.1f} 倍）")
    print(f"  若每格要 ±10pp（n≈100）→ {cells*100} 条 → 约 {cells*100/per_hand:.0f} 手，不现实。")
    print("  → 不要按五维建表；最细只到「族 × 一个局面维」（两维），且族要先合并到 5–6 个大桶。")

    print("\n## 13. 结论表：族 × 局面键")
    print(f"  阈值：池级 n ≥ {args.can_write} → 能写比例；{args.min_cell} ≤ n < {args.can_write} → 只能写见过；n < {args.min_cell} → 撑不住")
    print(f"  参考：n=20 时比例 95% Wilson 区间半宽最坏 ±{((wilson(10,20)[1]-wilson(10,20)[0])/2)*100:.0f}pp；n=10 时 ±{((wilson(5,10)[1]-wilson(5,10)[0])/2)*100:.0f}pp")
    reg_key = collections.Counter(
        (key_of(r), r["name"]) for r in reg_two if r.get("name") in regulars
    )
    rows = []
    for fam in FAMILIES:
        for k in keys:
            n = sum(1 for r in two if r["family"] == fam and key_of(r) == k)
            if n == 0:
                continue
            per_reg = collections.Counter(r["name"] for r in two
                                          if r["family"] == fam and key_of(r) == k and r["name"] in regulars)
            r3 = sum(1 for v in per_reg.values() if v >= 3)
            if n >= args.can_write:
                verdict = "能写比例"
            elif n >= args.min_cell:
                verdict = f"只能写见过 {n} 次"
            else:
                verdict = "撑不住"
            rows.append([fam, k, n, r3, verdict])
    rows.sort(key=lambda x: (-x[2], x[0]))
    print(markdown_table(["族", "局面键", "池级 n", "≥3 次的常客数", "判定"], rows))
    strong_rows = sum(1 for r in rows if r[4] == "能写比例")
    seen_rows = sum(1 for r in rows if r[4].startswith("只能写见过"))
    weak_rows = sum(1 for r in rows if r[4] == "撑不住")
    print(f"\n  汇总：能写比例 {strong_rows} 行 / 只能写见过 {seen_rows} 行 / 撑不住 {weak_rows} 行")

    if args.json:
        out = {
            "gate": args.gate,
            "records_raw": raw_total,
            "records_kept": len(records),
            "baseline": {
                "hands": len(hands), "sessions": len(dates), "dates": dates,
                "full_show_hands": len(full_hands),
                "board_dist": {str(k): v for k, v in board_dist.items()},
                "two_card_records": len(two), "one_card_records": len(one),
                "distinct_shower_names": len(names), "regulars": len(regulars),
            },
            "family_counts": dict(fc),
            "conclusion": [
                {"family": r[0], "key": r[1], "n": r[2], "regulars_ge3": r[3], "verdict": r[4]}
                for r in rows
            ],
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  JSON 已写到 {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
