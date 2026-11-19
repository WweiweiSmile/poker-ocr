#!/usr/bin/env python3
"""冷跟先验按入池人数分桶（票 #78）。

三件事：
1. 表落库后与草案 §2.4 的 13×13 逐格表、运行时 `ranges.get` 三方一致
   （`tools/check_cold_call_buckets.py --check` 的同一份检查）；
2. 档位选择只有一套：`cold_call_tiers.tier_from_n_way` 是唯一映射，翻前
   （prompt/user/preflop.py）与翻后（field_ranges → hand_vs_ranges）都调它；
3. 人数读不出时**不猜档**：prior 保持现状 2 人池数值，事实里标「未定」。

    nv tests/test_cold_call_buckets.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import check_cold_call_buckets as ccb  # noqa: E402
from poker_ocr import cold_call_tiers, hand_vs_ranges, ranges  # noqa: E402
from poker_ocr.advice import _facts_snapshot, build_situation  # noqa: E402
from poker_ocr.field_ranges import resolve  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402

FAILS: list[str] = []
BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")
TABLE = json.loads(cold_call_tiers.TABLE_FILE.read_text())


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def act(seat: str, pos: str | None, action: str, amount: float | None = None,
        *, street: str = "preflop", hero: bool = False):
    return ActionRecord(street=street, seat_id=seat, name=seat, position=pos,
                        action=action, amount=amount, all_in=False, is_hero=hero)


def snap(*, hero_pos="BTN", hero_cards=("Ah", "Qh"), board=("As", "Ks", "4d"),
         opponents, preflop=(), flop=(), shapes=("single-raise",),
         hero_is_pfr=True, street="flop", pot=20.0, to_call=0.0):
    return HandSnapshot(
        hand_id="h1", hand_index=1, street=street, board=tuple(board),
        hero_cards=tuple(hero_cards), hero_position=hero_pos, hero_stack=97.0,
        hero_bet=0.0, hero_invested=6.0, to_call=to_call, second_bet=None,
        pot=pot, pot_before_street=pot, table_size=9, blinds=BLINDS,
        opponents=tuple(opponents),
        streets=(StreetRecord("preflop", 0.0, tuple(preflop)),
                 StreetRecord(street, pot, tuple(flop))),
        preflop_shapes=shapes, hero_is_pfr=hero_is_pfr, cbet_spot=False,
        decision_key=("h1", street, 0, (), tuple(hero_cards)),
        players_dealt=len(opponents) + 1,
    )


def opp(seat: str, pos: str | None, *, folded: bool = False,
        bet: float = 0.0) -> OpponentSnapshot:
    return OpponentSnapshot(seat, seat, pos, 97.0, bet, folded)


def by_seat(field):
    return {s.seat_id: s for s in field.specs}


def _keys_digest(rid: str) -> tuple[int, float, str]:
    ran = ranges.get(rid)
    keys = sorted({ranges.key_of_combo(c) for c in ran.combos()})
    return (len(keys), ran.combo_count,
            hashlib.sha256(",".join(keys).encode()).hexdigest())


# ---------------------------------------------------------------- 表 ↔ 草案


def test_table_matches_draft_grid_and_runtime() -> None:
    """JSON 的 token、草案 13×13 表格、运行时读出来的范围必须是同一张表。"""
    fails = ccb.check()
    check(not fails, f"一致性检查失败：{fails}")


def test_draft_targets_are_pinned() -> None:
    """草案 §2.2 的六个目标值：档与档之间不许互相污染。"""
    want = {
        ("IP", 2): (268, 20.21, 52), ("IP", 3): (200, 15.08, 39),
        ("IP", 4): (150, 11.31, 31),
        ("OOP", 2): (222, 16.74, 45), ("OOP", 3): (170, 12.82, 34),
        ("OOP", 4): (128, 9.65, 26),
    }
    for (side, tier), (combos, width, keys) in want.items():
        entry = TABLE["tiers"][side][str(tier)]["expected"]
        check((entry["combos"], round(entry["width_pct"], 2),
               entry["keys"]) == (combos, width, keys),
              f"{side} n{tier} 表里的目标值被改了：{entry}")


def test_two_way_prior_is_bit_identical() -> None:
    """2 人池 = 现状表：combo 数、宽度、键集合逐位不变（只加人数轴，不改数值）。"""
    for side, combos, width, count, digest in (
            ("IP", 268, 268 / 1326 * 100, 52, "83a01519a314d7df"),
            ("OOP", 222, 222 / 1326 * 100, 45, "cf0973a80ec189bc")):
        ran = ranges.get(f"COLD_CALL_{side}")
        check(ran is not None, f"COLD_CALL_{side} 解析不出")
        if ran is None:
            continue
        check(ran.combo_count == combos, f"{side} 2 人池 combo 变了：{ran.combo_count}")
        check(ran.width_pct == width, f"{side} 2 人池宽度变了：{ran.width_pct}")
        n_keys, n_combos, digest_got = _keys_digest(f"COLD_CALL_{side}")
        check(n_keys == count, f"{side} 2 人池键数变了：{n_keys}")
        check(n_combos == combos, f"{side} 2 人池 combo 数变了：{n_combos}")
        check(digest_got.startswith(digest),
              f"{side} 2 人池键集合变了：{digest_got}")
        # 显式 _n2 若被接受，也只能是同一张 2 人池表（不新增数值）
        explicit = ranges.get(f"COLD_CALL_{side}_n2")
        check(explicit is None or explicit.width_pct == width,
              f"COLD_CALL_{side}_n2 应等于 2 人池宽度")
        for vs in ("EP", "MP", "LP", "SB", "BB"):
            alias = ranges.get(f"COLD_CALL_{side}_vs_{vs}")
            check(alias is not None and alias.width_pct == width,
                  f"COLD_CALL_{side}_vs_{vs} 应等于 2 人池宽度")


def test_tier_widths_land_in_draft_windows() -> None:
    """3/4 人池档只能落在草案给的窗口里（防止有人手改 token 漂移）。"""
    windows = {
        ("IP", 3): (14.5, 15.6), ("IP", 4): (10.8, 11.8),
        ("OOP", 3): (12.3, 13.3), ("OOP", 4): (9.2, 10.1),
    }
    for (side, tier), (lo, hi) in windows.items():
        ran = ranges.get(f"COLD_CALL_{side}_n{tier}")
        check(ran is not None, f"COLD_CALL_{side}_n{tier} 解析不出")
        if ran is None:
            continue
        check(lo < ran.width_pct < hi,
              f"{side} n{tier} 宽度 {ran.width_pct:.2f}% 不在 ({lo}, {hi})")


def test_tiers_are_nested_subsets() -> None:
    """只删不加：4 人池 ⊂ 3 人池 ⊂ 2 人池，否则「收紧」可能反而换进新手牌。"""
    for side in ("IP", "OOP"):
        keys = {}
        for tier in (2, 3, 4):
            rid = (f"COLD_CALL_{side}" if tier == 2 else f"COLD_CALL_{side}_n{tier}")
            ran = ranges.get(rid)
            keys[tier] = {ranges.key_of_combo(c) for c in ran.combos()}
        check(keys[4] <= keys[3] <= keys[2], f"{side} 档位不是嵌套子集")


# ------------------------------------------------------- id 形态 / 档位选择


def test_broken_table_fails_fast() -> None:
    """静态表坏了要 fail fast 并点名文件，不能静默退成「范围未知」。

    fail open 的边界是**运行期缺数据**，不是静态表本身坏了：表坏了应当在
    启动/单测就炸出来（`poker_ocr/strategy_tables.py:116-117` 同一惯例）。
    """
    import tempfile

    original = cold_call_tiers.TABLE_FILE
    bad_docs = (
        ({"kind": "cold_call_buckets", "tiers": {}}, "缺 schema"),
        ({"schema": 2, "kind": "cold_call_buckets", "tiers": {}}, "schema=2"),
        ({"schema": 1, "tiers": {}}, "缺 kind"),
        ({"schema": 1, "kind": "range_flow", "tiers": {}}, "kind 错"),
        ({"schema": 1, "kind": "cold_call_buckets"}, "缺 tiers"),
    )
    try:
        for doc, why in bad_docs:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "cold_call_buckets.json"
                path.write_text(json.dumps(doc, ensure_ascii=False),
                                encoding="utf-8")
                cold_call_tiers.TABLE_FILE = path
                cold_call_tiers.reset_cache()
                try:
                    cold_call_tiers.tokens_for("IP", 3)
                except ValueError as exc:
                    check(str(path) in str(exc),
                          f"{why}: 错误消息应点名文件：{exc}")
                else:
                    check(False, f"{why}: 坏表应抛 ValueError")
    finally:
        cold_call_tiers.TABLE_FILE = original
        cold_call_tiers.reset_cache()


def test_range_id_shapes() -> None:
    """id 形态：COLD_CALL_{IP|OOP}[_vs_{bucket}][_n{3|4}]；位置轴仍同宽。"""
    cases = {
        "COLD_CALL_IP": ("IP", None, 2),
        "COLD_CALL_IP_vs_EP": ("IP", "EP", 2),
        "COLD_CALL_IP_n3": ("IP", None, 3),
        "COLD_CALL_IP_vs_EP_n3": ("IP", "EP", 3),
        "COLD_CALL_OOP_n4": ("OOP", None, 4),
        "COLD_CALL_OOP_vs_LP_n4": ("OOP", "LP", 4),
    }
    for rid, want in cases.items():
        check(cold_call_tiers.parse_range_id(rid) == want,
              f"{rid} 解析成 {cold_call_tiers.parse_range_id(rid)}，应为 {want}")
        ran = ranges.get(rid)
        check(ran is not None, f"{rid} 解析不出范围")
        if ran is None:
            continue
        base = ranges.get(f"COLD_CALL_{want[0]}" if want[2] == 2
                          else f"COLD_CALL_{want[0]}_n{want[2]}")
        check(ran.width_pct == base.width_pct,
              f"{rid} 的位置后缀不该改变宽度")
    # 幂等：同一档反复 with_tier 不叠后缀
    rid = cold_call_tiers.with_tier("COLD_CALL_IP_vs_EP", 3)
    check(rid == "COLD_CALL_IP_vs_EP_n3", f"with_tier 结果 {rid}")
    check(cold_call_tiers.with_tier(rid, 3) == rid, "with_tier 不幂等")
    check(cold_call_tiers.with_tier("COLD_CALL_OOP", None) == "COLD_CALL_OOP",
          "未定档应保持无后缀 id")
    # 后缀放错位置 = 不认，别退回 2 人池冒充
    check(ranges.get("COLD_CALL_IP_n3_vs_EP") is None,
          "后缀放错位置时应返回 None")


def test_tier_mapping_is_clamped() -> None:
    check(cold_call_tiers.tier_from_n_way(None) is None, "None 应无档")
    check(cold_call_tiers.tier_from_n_way(0) is None, "0 人应无档")
    check(cold_call_tiers.tier_from_n_way(1) == 2, "1 人池归 2 档")
    check(cold_call_tiers.tier_from_n_way(2) == 2, "2 人池 = 2 档")
    check(cold_call_tiers.tier_from_n_way(3) == 3, "3 人池 = 3 档")
    check(cold_call_tiers.tier_from_n_way(4) == 4, "4 人池 = 4 档")
    check(cold_call_tiers.tier_from_n_way(9) == 4, "5 人以上归 4 档")


def test_tier_selection_counts_entrants_at_action_time() -> None:
    """档位按「冷跟那一刻已经进池的人数」，不是当前帧在局人数。"""
    s = snap(hero_pos="BTN", preflop=(
        act("hero", "BTN", "raise", 6.0, hero=True),
        act("co", "CO", "call", 6.0),
        act("sb", "SB", "call", 6.0),
    ), opponents=(opp("co", "CO"), opp("sb", "SB"), opp("bb", "BB")))
    check(cold_call_tiers.tier_for_seat(s, "co").tier == 2,
          "第一个冷跟者是 2 人池（开池者 + 自己）")
    check(cold_call_tiers.tier_for_seat(s, "sb").tier == 3,
          "第二个冷跟者是 3 人池")
    # BB 还没响应（behind）：不计入档位
    check(cold_call_tiers.tier_for_seat(s, "bb").tier is None,
          "没记录的座位必须无档，不能按当前帧人数猜")
    # 5 个跟注者 → 归 4 档
    many = snap(preflop=tuple([act("hero", "BTN", "raise", 6.0, hero=True)] + [
        act(f"p{i}", "CO", "call", 6.0) for i in range(5)]),
        opponents=tuple(opp(f"p{i}", "CO") for i in range(5)))
    check(cold_call_tiers.tier_for_seat(many, "p4").tier == 4, "≥5 人归 4 档")
    # 没有翻前动作线（只有当前帧）→ 未定
    no_pre = HandSnapshot(
        hand_id="h1", hand_index=1, street="flop", board=("As", "Ks", "4d"),
        hero_cards=("Ah", "Qh"), hero_position="BTN", hero_stack=97.0,
        hero_bet=0.0, hero_invested=6.0, to_call=0.0, second_bet=None,
        pot=20.0, pot_before_street=20.0, table_size=9, blinds=BLINDS,
        opponents=(opp("co", "CO"),),
        streets=(StreetRecord("flop", 20.0, ()),),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("h1", "flop", 0, (), ("Ah", "Qh")), players_dealt=2,
    )
    cold = cold_call_tiers.tier_for_seat(no_pre, "co")
    check(cold.tier is None and cold.basis == cold_call_tiers.BASIS_UNDETERMINED,
          f"没有翻前动作线时应未定，实际 {cold}")


def test_self_completion_does_not_inflate_tier() -> None:
    """SB 先补齐 0.5 再跟加注：它自己前面那次跟注不能算成「另一个入池的人」。

    回归 P0-1：旧实现拿最后一次跟注在动作线里的**下标**当人数，SB 的两次
    call 会让它从 2 人池错升 3 人池（先验从 16.74% 变成 12.82%）。
    """
    s = snap(hero_pos="BTN", preflop=(
        act("hero", "BTN", "raise", 6.0, hero=True),
        act("sb", "SB", "call", 0.5),      # SB 补齐
        act("sb", "SB", "call", 6.0),      # 面对加注再跟
    ), opponents=(opp("sb", "SB"),))
    cold = cold_call_tiers.tier_for_seat(s, "sb")
    check(cold.tier == 2 and cold.n_way == 2,
          f"同一个座位的两次跟注只算自己一次，实际 {cold}")
    prior_id = cold_call_tiers.with_tier("COLD_CALL_OOP_vs_LP", cold.tier)
    check(prior_id == "COLD_CALL_OOP_vs_LP",
          f"2 人池应无 _n 后缀，实际 {prior_id}")
    ran = ranges.get(prior_id)
    check(ran is not None and abs(ran.width_pct - 16.74) < 0.01,
          f"2 人池宽度应保持现状 16.74%，"
          f"实际 {getattr(ran, 'width_pct', None)}")
    # 去重只去自己：前面别的座位仍要计数（三人池的第二个跟注者 = 3 人池）。
    three = snap(hero_pos="BTN", preflop=(
        act("hero", "BTN", "raise", 6.0, hero=True),
        act("co", "CO", "call", 6.0),
        act("sb", "SB", "call", 0.5),
        act("sb", "SB", "call", 6.0),
    ), opponents=(opp("co", "CO"), opp("sb", "SB")))
    third = cold_call_tiers.tier_for_seat(three, "sb")
    check(third.tier == 3 and third.n_way == 3,
          f"三人池里第二个跟注者（SB 自己两次 call）应为 3 人池，实际 {third}")


def test_hero_call_is_counted_through_placeholder() -> None:
    """hero 的跟注要参与计数（占位符座位），否则 hero 之后的座位档位偏低。"""
    s = snap(hero_pos="CO", preflop=(
        act("hero", "BTN", "raise", 6.0, hero=True),
        act("hero", "CO", "call", 6.0, hero=True),
        act("sb", "SB", "call", 6.0),
    ), opponents=(opp("sb", "SB"),))
    check(cold_call_tiers.tier_for_seat(s, cold_call_tiers.HERO_SEAT).tier == 2,
          "hero 自己的跟注应算 2 人池")
    check(cold_call_tiers.tier_for_seat(s, "sb").tier == 3,
          "hero 之后跟注的人应算 3 人池")


# ------------------------------------------------ 两条链路共用同一个选择器


def _four_way_snapshot(hero_pos="BB", hero_cards=("Kd", "8h")):
    """CO 开池，SB、UTG 依次冷跟，hero BB 还没响应。

    同一手牌里三名冷跟者的档位各不相同（2 / 3 / 4 人池）——这正是「档位取
    冷跟那一刻已入池人数」而不是「当前帧在局人数」的直接证据。
    """
    return snap(
        hero_pos=hero_pos, hero_cards=hero_cards, hero_is_pfr=False,
        preflop=(
            act("co", "CO", "raise", 6.0),
            act("sb", "SB", "call", 6.0),
            act("utg", "UTG", "call", 6.0),
        ), flop=(act("co", "CO", "bet", 4.0),),
        opponents=(opp("co", "CO"), opp("sb", "SB"), opp("utg", "UTG")),
        street="flop")


def _four_way_preflop():
    """同一动作线的翻前版本（hero BB 面对 CO 开池，身后已有 2 个跟注者）。"""
    pre = (
        act("co", "CO", "raise", 6.0),
        act("sb", "SB", "call", 6.0),
        act("utg", "UTG", "call", 6.0),
    )
    return HandSnapshot(
        hand_id="h1", hand_index=1, street="preflop", board=(),
        hero_cards=("Kd", "8h"), hero_position="BB", hero_stack=97.0,
        hero_bet=1.0, hero_invested=1.0, to_call=6.0, second_bet=None,
        pot=15.0, pot_before_street=1.5, table_size=9, blinds=BLINDS,
        opponents=(opp("co", "CO"), opp("sb", "SB"), opp("utg", "UTG")),
        streets=(StreetRecord("preflop", 1.5, pre),),
        preflop_shapes=("single-raise",), hero_is_pfr=False, cbet_spot=False,
        decision_key=("h1", "preflop", 0, (), ("Kd", "8h")), players_dealt=4,
    )


def test_preflop_and_postflop_share_one_selector() -> None:
    """翻前事实与翻后先验用同一个 tier_from_n_way，同一人数必给同一档。"""
    s = _four_way_snapshot()
    field = by_seat(resolve(s))
    check(field["sb"].prior_id == "COLD_CALL_OOP_vs_LP",
          f"第一个冷跟者 = 2 人池档（无后缀），实际 {field['sb'].prior_id}")
    check(field["utg"].prior_id == "COLD_CALL_OOP_vs_LP_n3",
          f"第二个冷跟者 = 3 人池档（_n3），实际 {field['utg'].prior_id}")
    check(field["sb"].cold_call.tier == 2 and field["utg"].cold_call.tier == 3,
          f"档位应各按自己动作时的人数：{field['sb'].cold_call} "
          f"{field['utg'].cold_call}")
    # 翻后每个座位都与核心映射一致（这就是「同一套函数」的可执行断言）
    check(cold_call_tiers.tier_for_seat(s, "sb") == cold_call_tiers.tier_for_n_way(2),
          "tier_for_seat 与 tier_for_n_way 必须同映射（2 人）")
    check(cold_call_tiers.tier_for_seat(s, "utg") == cold_call_tiers.tier_for_n_way(3),
          "tier_for_seat 与 tier_for_n_way 必须同映射（3 人）")
    # 翻前：hero 面对 CO 开池、身后已有 2 个跟注者 = 第 3 个冷跟者 = 4 人池。
    # #81 删掉了翻前范围结论段（含 _facing_raise_zh），所以这里只校验选择器
    # 本身仍给同一档；翻前事实不再渲染该档宽度。
    hero_tier = cold_call_tiers.tier_for_n_way(2 + 0 + 2)
    check(hero_tier.tier == 4, f"hero 应为 4 人池档，实际 {hero_tier}")
    oop_n4 = ranges.get("COLD_CALL_OOP_n4")
    check(oop_n4 is not None, "4 人池档的范围表必须存在")
    # 翻前用的 id 与翻后同一个构造函数拼出来
    check(cold_call_tiers.with_tier("COLD_CALL_OOP_vs_LP", hero_tier.tier)
          == "COLD_CALL_OOP_vs_LP_n4", "with_tier 应拼出翻后同一形态的 id")


def test_postflop_facts_carry_tier() -> None:
    """复盘 facts 里能看出用的是哪一档、该档宽度多少。"""
    s = _four_way_snapshot()
    facts = _facts_snapshot(build_situation(s, table_dynamics=""))
    items = (facts.get("hand_ranges") or {}).get("opponents") or []
    by_pos = {(o.get("seat") or o.get("position")): o for o in items}
    cold = (by_pos.get("utg") or {}).get("cold_call") or {}
    check(cold.get("tier") == 3, f"facts 里档位应为 3：{cold}")
    check(abs(float(cold.get("prior_width", 0)) - 12.82) < 0.01,
          f"facts 里应给出该档宽度：{cold}")
    check("3 人池档" in str(cold.get("label")), f"facts 里应标档：{cold}")
    first = (by_pos.get("sb") or {}).get("cold_call") or {}
    check(first.get("tier") == 2 and "2 人池档" in str(first.get("label")),
          f"第一个冷跟者应标 2 人池档：{first}")


def test_undetermined_keeps_status_quo_and_says_so() -> None:
    """人数读不出：prior 保持现状数值（不是随机牌），事实标「未定」。"""
    # hero BTN 开池、BB 跟注都记到了，CO 的冷跟没记到 → CO 的入池序号未知
    s = snap(hero_pos="BTN", hero_cards=("Kd", "8h"), hero_is_pfr=True,
             preflop=(
                 act("hero", "BTN", "raise", 6.0, hero=True),
                 act("bb", "BB", "call", 6.0),
             ), flop=(act("co", "CO", "bet", 4.0),),
             opponents=(opp("co", "CO"), opp("bb", "BB")), street="flop")
    co = by_seat(resolve(s))["co"]
    check(co.prior_id == "COLD_CALL_OOP_vs_LP",
          f"未定档应保持无后缀 id，实际 {co.prior_id}")
    check(co.cold_call is not None and co.cold_call.tier is None,
          f"未定档要能看出来，实际 {co.cold_call}")
    ran = ranges.get(co.prior_id)
    check(ran is not None and abs(ran.width_pct - 16.74) < 0.01,
          f"未定档必须保持现状 2 人池数值，实际 {ran.width_pct if ran else None}")
    facts = _facts_snapshot(build_situation(s, table_dynamics=""))
    item = next((o for o in ((facts.get("hand_ranges") or {}).get("opponents") or [])
                 if (o.get("seat") or o.get("position")) == "co"), {})
    cold = item.get("cold_call") or {}
    check(cold.get("tier") is None, f"facts 里 tier 应为 None：{cold}")
    check("未定" in str(cold.get("label")), f"facts 里应标未定：{cold}")


def test_no_tier_suffix_leaks_into_baseline_tables() -> None:
    """别的先验表不许被人数轴污染（只有冷跟有分桶）。"""
    for rid in ("RFI_CO", "BB_DEF_vs_EP", "LIMP_LP", "3BET_VS_EP", "UNIFORM"):
        ran = ranges.get(rid)
        check(ran is not None, f"{rid} 解析不出")
    check(ranges.get("RFI_CO_n3") is None, "RFI 不该有分桶")
    check(hand_vs_ranges is not None, "hand_vs_ranges 应可导入")


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_")]
    for name, fn in tests:
        before = len(FAILS)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            FAILS.append(f"{name}: 异常 {exc!r}")
        print(f"{'✅' if len(FAILS) == before else '❌'} {name}")
    if FAILS:
        print("\n失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print(f"\n✅ 冷跟分桶 {len(tests)} 项全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
