#!/usr/bin/env python3
"""首批翻后 9 条规则（#70）的测试。

    ./.venv/bin/python -m pytest tests/test_rules_postflop.py -q

五组用例：

  1. 台账本身：9 条 id / 页码 / status 齐全，node ≤30、why ≤40 字，前提 ≥2 条
     且含 street，verified=False、confidence=book；size 只引用 rule_params，
     规则里不出现字面尺寸数字；export_rules --check 退出 0。
  2. 命中：九条各自构造一个局面，断言命中集合里有这条 id，并把渲染出的
     两行（或一行）逐字钉住——单一命中点同时出「规则: 要不要下注」与
     「尺寸: 下多大」。
  3. 两行分支：牌面尺寸规则拿不到 hand_class 时 → 该行不渲染；intent 自带
     默认尺寸的（po.cbet.hu）→ 尺寸回落到半池，仍然只有动作行 + 尺寸行；
     intent 是过牌时不出尺寸行（不矛盾）。
  4. _size_text：size_ladder 三档、cbet 半池、block_bet 1/4 池、
     river_value_band 区间逐字断言，缺 hand_class / 未知档一律 fail open。
  5. hand_class 映射：coach 的九个牌力档全部有归属，未知档不出键。
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import coach  # noqa: E402
from poker_ocr import rules as r  # noqa: E402
from poker_ocr.advice import build_situation  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.opponent_profile import OpponentRead  # noqa: E402
from poker_ocr.options import Option  # noqa: E402
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.prompt.user import rules as render_rules  # noqa: E402
from poker_ocr.prompt.user.rules import (  # noqa: E402
    _size_text, render_rules_zh,
)
from poker_ocr.rules.facts import HAND_CLASS_BY_TIER, _hand_class  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def eq(got, want, label: str) -> None:
    check(got == want, f"{label}: 期望 {want!r}，实际 {got!r}")


def snap(*, street="flop", board=("Ks", "7d", "2c"), cards=("Kh", "Qh"),
         position="BTN", table_size=2, opps=None, pot=8.0, to_call=0.0):
    """翻后快照：默认单挑（BTN vs BB）、hero 翻前加注 4BB、对手只跟。"""
    if opps is None:
        opps = (OpponentSnapshot("BB", "乙", "BB", 96.0, 4.0, False),)
    caller = "BTN" if position == "BB" else "BB"
    streets = (
        StreetRecord("preflop", 1.5, (
            ActionRecord("preflop", "hero", None, position, "raise", 4.0,
                         False, True),
            ActionRecord("preflop", caller, "乙", caller, "call", 4.0,
                         False, False),
        )),
        StreetRecord(street, pot, ()),
    )
    return HandSnapshot(
        hand_id="h70", hand_index=70, street=street, board=board,
        hero_cards=cards, hero_position=position, hero_stack=92.0,
        hero_bet=0.0, hero_invested=4.0, to_call=to_call, second_bet=None,
        pot=pot, pot_before_street=pot, table_size=table_size,
        blinds=Blinds(sb=0.5, bb=1.0, ante=None, raw="0.5/1.0"),
        opponents=opps, streets=streets, preflop_shapes=("single-raise",),
        hero_is_pfr=True, cbet_spot=True,
        decision_key=("h70", street, 0, pot, to_call), warnings=(),
        bomb_pot=False, players_dealt=table_size,
    )


PREV = SimpleNamespace(
    street="preflop", reason="",
    option=Option("raise-4", "加注 4BB", "raise", 4.0))

READ_HIGH = OpponentRead(
    seat_id="BTN", key="k70", display_name="甲", archetype="lag",
    confidence=0.9, n=80, vpip=40.0, pfr=30.0, af=3.0,
    source="auto", note="", gate="stable", fold_to_cbet_pct=30.0)


def situation(point, reads=None):
    return build_situation(point, table_dynamics="", previous=PREV,
                           opponent_reads=reads or ())


# ─────────────────────────── 1. 台账 ───────────────────────────

# id → (页码, status)
EXPECTED = {
    "po.cbet.hu": (30, "verbatim"),
    "po.cbet.multiway": (32, "verbatim"),
    "po.size.texture": (35, "verbatim"),
    "po.draw.semi": (34, "verbatim"),
    "po.draw.block": (33, "rebuilt"),
    "po.turn.enhance": (47, "verbatim"),
    "po.turn.scary": (48, "verbatim"),
    "po.river.value": (52, "verbatim"),
    "po.river.mid.check": (53, "verbatim"),
}

SIZE_KEYS = {"table", "name", "unit", "key", "per"}


def test_ledger_nine_rules() -> None:
    rules = r.load_ledger()
    postflop = {rule.id: rule for rule in rules if rule.id.startswith("po.")}
    eq(set(postflop), set(EXPECTED), "9 条翻后规则 id")
    for rid, (page, status) in EXPECTED.items():
        rule = postflop[rid]
        check(rule.street in ("flop", "turn", "river"),
              f"{rid}.street 必须是翻后：{rule.street!r}")
        check(len(rule.node) <= 30, f"{rid}.node 超过 30 字：{rule.node!r}")
        check(len(rule.why) <= 40, f"{rid}.why 超过 40 字：{rule.why!r}")
        check(len(rule.preconditions) >= 2,
              f"{rid} 前提少于 2 条：{rule.preconditions!r}")
        check(any(p.fact == "street" for p in rule.preconditions),
              f"{rid} 缺 street 前提")
        eq(rule.verified, False, f"{rid}.verified")
        eq(rule.confidence, "book", f"{rid}.confidence")
        eq(rule.source.get("book"), "德州扑克小绿皮书", f"{rid}.source.book")
        eq(rule.source.get("page"), page, f"{rid}.source.page")
        eq(rule.source.get("status"), status, f"{rid}.source.status")
        if rule.slot == "intent":
            check(rule.action is not None, f"{rid} intent 槽必须给 action")
        if rule.slot == "size":
            check(rule.action is None, f"{rid} size 槽不该给 action")
            check(bool(rule.size), f"{rid} size 槽必须给 size 引用")
        if rule.size:
            eq(rule.size.get("table"), "rule_params", f"{rid}.size.table")
            check(set(rule.size) <= SIZE_KEYS,
                  f"{rid}.size 只许引用 rule_params：{rule.size!r}")
            check(all(isinstance(v, (str, bool)) or v is None
                      for v in rule.size.values()),
                  f"{rid}.size 里不许出现字面尺寸数字：{rule.size!r}")


def test_size_rule_coexists_both_ways() -> None:
    rules = {rule.id: rule for rule in r.load_ledger()}
    eq(rules["po.cbet.hu"].coexist_with, ("po.size.texture",)
       if isinstance(rules["po.cbet.hu"].coexist_with, tuple)
       else ["po.size.texture"], "cbet.hu → texture")
    check("po.cbet.hu" in (rules["po.size.texture"].coexist_with or ()),
          "texture 要反向声明 coexist_with（双向）")


def test_export_rules_check_ok() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools/export_rules.py"), "--check"],
        capture_output=True, text=True, cwd=str(ROOT))
    eq(proc.returncode, 0,
       f"export_rules --check 退出码（{proc.stdout}{proc.stderr}）")


# ─────────────────────────── 2. 九条命中 ───────────────────────────

def _hits(point, reads=None) -> set[str]:
    sit = situation(point, reads)
    return {rule.id for rule in r.match_rules(r.facts_snapshot(sit))}


def case_cbet_hu():
    return snap(board=("Ks", "7d", "2c"), cards=("Kh", "Qh"))


def case_cbet_multiway():
    return snap(board=("Ks", "7d", "2c"), cards=("Kh", "Qh"),
                position="SB", table_size=6,
                opps=(OpponentSnapshot("HJ", "甲", "HJ", 96.0, 4.0, False),
                      OpponentSnapshot("CO", "乙", "CO", 96.0, 4.0, False)))


def case_size_only():
    return snap(street="turn", board=("Ks", "7d", "2c", "8h"),
                cards=("Kh", "Qh"), position="HJ", table_size=6,
                opps=(OpponentSnapshot("CO", "甲", "CO", 96.0, 4.0, False),
                      OpponentSnapshot("BB", "乙", "BB", 96.0, 4.0, False)))


def case_draw_semi():
    return snap(board=("9h", "8h", "2c"), cards=("Ah", "Kh"))


def case_draw_block():
    return snap(board=("Ks", "9d", "5c"), cards=("Qh", "Jh"),
                position="BB", table_size=6,
                opps=(OpponentSnapshot("BTN", "甲", "BTN", 96.0, 4.0, False),))


def case_turn_enhance():
    return snap(street="turn", board=("Ks", "7d", "2c", "Jc"),
                cards=("Kh", "Jh"))


def case_turn_scary():
    return snap(street="turn", board=("Kh", "7h", "2c", "3h"),
                cards=("Kd", "Qc"), position="SB", table_size=6,
                opps=(OpponentSnapshot("CO", "甲", "CO", 96.0, 4.0, False),
                      OpponentSnapshot("BTN", "乙", "BTN", 96.0, 4.0, False)))


def case_river_value():
    return snap(street="river", board=("Ks", "Kd", "7c", "2h", "3s"),
                cards=("Kc", "Kh"), pot=30.0)


def case_river_mid_check():
    return snap(street="river", board=("Ks", "7d", "2c", "9h", "4s"),
                cards=("Kd", "5c"), position="BB", table_size=6, pot=20.0,
                opps=(OpponentSnapshot("BTN", "甲", "BTN", 96.0, 4.0, False),))


CASES = [
    ("po.cbet.hu", case_cbet_hu, (), [
        "规则: 单挑翻牌你先行动、对手只跟注 → 下注（对手只有约 35% 中翻牌，"
        "半池即可收下）[书 p30]",
        "尺寸: 牌越强下越小，牌越弱下越大 → 1/3 池（强牌） [书 p35]",
    ]),
    ("po.cbet.multiway", case_cbet_multiway, (), [
        "规则: 多人池你先行动，只为减少对手 → 下注（人越多越不诈唬，"
        "下注目的是打走人）[书 p32]",
        "尺寸: 牌越强下越小，牌越弱下越大 → 1/3 池（强牌） [书 p35]",
    ]),
    ("po.size.texture", case_size_only, (), [
        "尺寸: 牌越强下越小，牌越弱下越大 → 1/3 池（强牌） [书 p35]",
    ]),
    ("po.draw.semi", case_draw_semi, (), [
        "规则: 强听翻牌半诈唬：两种赢法 → 下注（先下注有两种赢法："
        "对手弃牌或我抽中）[书 p34]",
        "尺寸: 强听翻牌半诈唬：两种赢法 → 半池 [书 p34]",
    ]),
    ("po.draw.block", case_draw_block, (READ_HIGH,), [
        "规则: 无位置中等听牌：小注封锁 → 下注（小注锁住对手加注，"
        "换 5:1 看转牌）[书 p33]",
        "尺寸: 无位置中等听牌：小注封锁 → 1/4 池 [书 p33]",
    ]),
    ("po.turn.enhance", case_turn_enhance, (), [
        "规则: 转牌牌力上升：加速度做大局 → 加注（转牌变强就加速度，"
        "别让河牌吓退对手）[书 p47]",
        "尺寸: 牌越强下越小，牌越弱下越大 → 1/3 池（强牌） [书 p35]",
    ]),
    ("po.turn.scary", case_turn_scary, (), [
        "规则: 转牌成花成顺又你先行动：让出开火权 → 过牌"
        "（20% 的免费河牌不值得被加注）[书 p48]",
    ]),
    ("po.river.value", case_river_value, (), [
        "规则: 河牌坚果：下注拿价值 → 下注（价值下注别被读出，"
        "尺寸围绕对手愿付价浮动）[书 p52]",
        "尺寸: 河牌坚果：下注拿价值 → 0.66–1.0 倍池 [书 p52]",
    ]),
    ("po.river.mid.check", case_river_mid_check, (), [
        "规则: 河牌中等牌无位置：过牌控池 → 过牌（中等牌下注只被更强的跟，"
        "还丢诈唬机会）[书 p53]",
    ]),
]


def test_nine_rules_hit_and_render() -> None:
    for rid, build, reads, want in CASES:
        point = build()
        hits = _hits(point, reads)
        check(rid in hits, f"{rid} 应命中，实际命中 {sorted(hits)}")
        lines = render_rules_zh(situation(point, reads))
        eq(lines, want, f"{rid} 渲染")


def test_two_lines_same_hit_point() -> None:
    """同一个命中点：动作行 + 尺寸行，两行标题不同、内容不重复。"""
    lines = render_rules_zh(situation(case_cbet_hu()))
    eq(len(lines), 2, f"c-bet 单挑两行：{lines}")
    check(lines[0].startswith("规则: "), f"第一行是动作行：{lines[0]!r}")
    check(lines[1].startswith("尺寸: "), f"第二行是尺寸行：{lines[1]!r}")


def test_size_slot_miss_without_hand_class() -> None:
    """牌面尺寸规则拿不到 hand_class → 只出动作行（不硬凑尺寸）。"""
    point = case_cbet_multiway()

    def facts_without_class(sit):
        facts = real_facts(sit)
        facts.pop("hand_class", None)
        return facts

    real_facts = render_rules.facts_snapshot
    render_rules.facts_snapshot = facts_without_class
    try:
        lines = render_rules_zh(situation(point))
    finally:
        render_rules.facts_snapshot = real_facts
    eq(lines, ["规则: 多人池你先行动，只为减少对手 → 下注（人越多越不诈唬，"
               "下注目的是打走人）[书 p32]"], "尺寸规则未命中 → 只有动作行")


def test_size_falls_back_to_intent_default() -> None:
    """intent 自带默认尺寸（cbet 半池）→ 尺寸行退回落半池，仍只有两行。"""
    point = case_cbet_hu()

    def facts_without_class(sit):
        facts = real_facts(sit)
        facts.pop("hand_class", None)
        return facts

    real_facts = render_rules.facts_snapshot
    render_rules.facts_snapshot = facts_without_class
    try:
        lines = render_rules_zh(situation(point))
    finally:
        render_rules.facts_snapshot = real_facts
    eq(lines, [
        "规则: 单挑翻牌你先行动、对手只跟注 → 下注（对手只有约 35% 中翻牌，"
        "半池即可收下）[书 p30]",
        "尺寸: 单挑翻牌你先行动、对手只跟注 → 半池 [书 p30]",
    ], "尺寸回落到 intent 默认半池")


def test_check_action_has_no_size_line() -> None:
    """动作是过牌时不出尺寸行：否则「过牌」配「1/3 池」自相矛盾。"""
    point = case_turn_scary()
    hits = _hits(point)
    check("po.size.texture" in hits,
          f"转牌强牌也该命中尺寸槽（用来验证挡住的是渲染）：{sorted(hits)}")
    lines = render_rules_zh(situation(point))
    eq(len(lines), 1, f"过牌只出一行：{lines}")


# ─────────────────────────── 3. 尺寸文字 ───────────────────────────

def test_size_text_ladder_three_tiers() -> None:
    ref = {"table": "rule_params", "name": "size_ladder", "unit": "pot",
           "key": "hand_class"}
    eq(_size_text(ref, {"hand_class": "strong"}), "1/3 池（强牌）", "强牌 1/3")
    eq(_size_text(ref, {"hand_class": "medium"}), "2/3 池（中等牌）", "中等 2/3")
    eq(_size_text(ref, {"hand_class": "weak"}), "1 倍池（弱牌）", "弱牌 1 倍")


def test_size_text_rule_refs_verbatim() -> None:
    rules = {rule.id: rule for rule in r.load_ledger()}
    eq(_size_text(rules["po.draw.block"].size, {}), "1/4 池", "block_bet 1/4")
    eq(_size_text(rules["po.cbet.hu"].size, {}), "半池", "c-bet 半池")
    eq(_size_text(rules["po.draw.semi"].size, {}), "半池", "半诈唬半池")
    eq(_size_text(rules["po.river.value"].size, {}),
       "0.66–1.0 倍池", "河牌价值区间")
    eq(_size_text(rules["po.size.texture"].size, {"hand_class": "medium"}),
       "2/3 池（中等牌）", "尺寸槽走阶梯")


def test_size_text_fail_open() -> None:
    ref = {"table": "rule_params", "name": "size_ladder", "unit": "pot",
           "key": "hand_class"}
    check(_size_text(ref, {}) is None, "缺 hand_class → 不出尺寸")
    check(_size_text(ref, {"hand_class": "nope"}) is None,
          "未知档位 → 不出尺寸（fail open）")


# ─────────────────────────── 4. hand_class 映射 ───────────────────────────

def test_hand_class_covers_all_tiers() -> None:
    tiers = [
        coach.STRONG_MADE, coach.OVERPAIR, coach.TPTK, coach.TP_WEAK,
        coach.MID_BOTTOM, coach.STRONG_DRAW, coach.WEAK_DRAW, coach.AIR,
        coach.BOARD_PLAY,
    ]
    eq(len(tiers), 9, "coach 的九个牌力档")
    for tier in tiers:
        check(tier in HAND_CLASS_BY_TIER, f"{tier} 没有 hand_class 归属")
        check(HAND_CLASS_BY_TIER[tier] in ("strong", "medium", "weak"),
              f"{tier} 映射到非法档：{HAND_CLASS_BY_TIER[tier]!r}")
    eq(_hand_class(coach.TPTK), "strong", "顶对强踢 → strong")
    eq(_hand_class(coach.TP_WEAK), "medium", "顶对弱踢 → medium")
    eq(_hand_class(coach.STRONG_DRAW), "weak", "强听 → weak")
    check(_hand_class(None) is None, "缺档位 → None")
    check(_hand_class("未知档") is None, "未知档 → None（不出键，规则不命中）")


def test_hand_class_missing_in_facts_does_not_raise() -> None:
    sit = dataclasses.replace(situation(case_cbet_hu()), hand_tier="未知档")
    facts = r.facts_snapshot(sit)
    check("hand_class" not in facts, f"映射不出来就不写键：{facts.get('hand_class')!r}")
    ids = {rule.id for rule in r.match_rules(facts)}
    check("po.size.texture" not in ids, f"尺寸规则不该命中：{sorted(ids)}")


# ─────────────────────────── runner ───────────────────────────

def main() -> int:
    tests = [
        test_ledger_nine_rules,
        test_size_rule_coexists_both_ways,
        test_export_rules_check_ok,
        test_nine_rules_hit_and_render,
        test_two_lines_same_hit_point,
        test_size_slot_miss_without_hand_class,
        test_size_falls_back_to_intent_default,
        test_check_action_has_no_size_line,
        test_size_text_ladder_three_tiers,
        test_size_text_rule_refs_verbatim,
        test_size_text_fail_open,
        test_hand_class_covers_all_tiers,
        test_hand_class_missing_in_facts_does_not_raise,
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
    print("\n✅ 翻后 9 条规则测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
