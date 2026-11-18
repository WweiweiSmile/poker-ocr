#!/usr/bin/env python3
"""首批翻前 8 条规则（#69）的测试。

    nv tests/test_rules_preflop.py

四组用例：

  1. 台账本身：8 条 id 齐全、node ≤30 / why ≤40 字、前提 ≥2 条且含 street、
     source 页码是整数、export_rules --check 退出 0。
  2. 渲染：翻前 Situation 命中 intent + 尺寸行，尺寸文字逐字断言；零命中是空列表。
  3. _size_text 纯函数：三种 unit（pot/bb/raise）× 三种载体（value/rows/items），
     以及缺谓词/坏值一律 fail open。
  4. witness 池级证据整段只出一次（#38 方案 A）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import rules as r  # noqa: E402
from poker_ocr.advice import build_situation  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.opponent_profile import (  # noqa: E402
    OpponentRead, render_reads_zh,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.prompt.user.rules import (  # noqa: E402
    _size_text, render_rules_zh,
)
from poker_ocr.witness import admission as A  # noqa: E402
from poker_ocr.witness import render as W  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def eq(got, want, label: str) -> None:
    check(got == want, f"{label}: 期望 {want!r}，实际 {got!r}")


def snap(*, position="BTN", cards=("Ah", "Kh"), ante=0.0,
         opponents=None, streets=(), shapes=("unopened",),
         to_call=1.0, pot=1.5) -> HandSnapshot:
    if opponents is None:
        opponents = (
            OpponentSnapshot("CO", "甲", "CO", 100.0, 0.0, False),
            OpponentSnapshot("SB", "乙", "SB", 99.5, 0.5, False),
            OpponentSnapshot("BB", "丙", "BB", 99.0, 1.0, False),
        )
    return HandSnapshot(
        hand_id="h69", hand_index=69, street="preflop", board=(),
        hero_cards=cards, hero_position=position, hero_stack=100.0,
        hero_bet=0.0, hero_invested=0.0, to_call=to_call, second_bet=None,
        pot=pot, pot_before_street=1.5, table_size=6,
        blinds=Blinds(sb=0.5, bb=1.0, ante=ante, raw="1/2"),
        opponents=opponents,
        streets=(StreetRecord("preflop", 1.5, streets),),
        preflop_shapes=shapes, hero_is_pfr=False, cbet_spot=False,
        decision_key=("h69", "preflop", 0, 1.5, to_call, cards),
        players_dealt=6,
    )


def situation(point, **kw):
    return build_situation(point, table_dynamics="", **kw)


# ─────────────────────────── 1. 台账 ───────────────────────────

EXPECTED_IDS = {
    "pf.rfi.first_in",
    "pf.rfi.overlimp",
    "pf.iso.limp",
    "pf.sb.complete.no_ante",
    "pf.sb.ante",
    "pf.bb.vs_complete",
    "pf.threebet.size",
    "pf.fourbet.premium",
}


def test_ledger_eight_rules() -> None:
    rules = r.load_ledger()
    by_id = {rule.id: rule for rule in rules}
    eq(set(by_id), EXPECTED_IDS, "8 条翻前规则 id")
    for rule in rules:
        eq(rule.street, "preflop", f"{rule.id}.street")
        check(len(rule.node) <= 30, f"{rule.id}.node 超过 30 字：{rule.node!r}")
        check(len(rule.why) <= 40, f"{rule.id}.why 超过 40 字：{rule.why!r}")
        check(len(rule.preconditions) >= 2,
              f"{rule.id} 前提少于 2 条：{rule.preconditions!r}")
        check(any(p.fact == "street" for p in rule.preconditions),
              f"{rule.id} 缺 street 前提")
        eq(rule.verified, False, f"{rule.id}.verified")
        eq(rule.confidence, "book", f"{rule.id}.confidence")
        check(isinstance(rule.source.get("page"), int),
              f"{rule.id}.source.page 必须是整数：{rule.source!r}")
        check(rule.source.get("status") in ("verbatim", "rebuilt"),
              f"{rule.id}.source.status：{rule.source!r}")


def test_export_rules_check_ok() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools/export_rules.py"), "--check"],
        capture_output=True, text=True, cwd=str(ROOT))
    eq(proc.returncode, 0,
       f"export_rules --check 退出码（{proc.stdout}{proc.stderr}）")


# ─────────────────────────── 2. 渲染 ───────────────────────────

def test_render_first_in_early() -> None:
    """UTG 无人入池、无前注 → 开池规则 + 早位尺寸。"""
    lines = render_rules_zh(situation(snap(position="UTG", cards=("Ah", "Ad"))))
    eq(len(lines), 2, f"intent + 尺寸两行：{lines}")
    eq(lines[0],
       "规则: 第一个入池 → 加注（第一个入池只加注，减少对手并让翻后好判断）[书 p18]",
       "首个入池 intent 行")
    eq(lines[1], "尺寸: 第一个入池 → 2.75BB（早位） [书 p18]", "早位开池尺寸")


def test_render_iso_limper() -> None:
    """BTN 面对 1 个溜入者、手牌在 open range → 隔离 + 每溜入者递增。"""
    limper = ActionRecord("preflop", "UTG", None, "UTG", "call", 1.0, False, False)
    point = snap(position="BTN", cards=("Ah", "Ad"), streets=(limper,),
                 to_call=1.0, pot=2.5)
    lines = render_rules_zh(situation(point))
    eq(len(lines), 2, f"intent + 尺寸两行：{lines}")
    check(lines[0].startswith("规则: 隔离 1–2 个溜入者 → 加注（有位置惩罚溜入者")
          and lines[0].endswith("[书 p23]"), f"隔离 intent 行：{lines[0]!r}")
    eq(lines[1], "尺寸: 隔离 1–2 个溜入者 → 每多一个溜入者 +1BB [书 p23]",
       "按溜入者递增的尺寸")


def test_render_sb_no_ante() -> None:
    """SB、无前注、没人入池、唯一对手 PFR 8% → SB 规则（why 带 25% 平跟）。"""
    read = OpponentRead(
        seat_id="BB", key="k", display_name="某丙", archetype="tag",
        confidence=0.8, n=60, vpip=22.0, pfr=8.0, af=1.5,
        source="auto", note="", gate="stable", fold_to_cbet_pct=40.0)
    point = snap(position="SB", cards=("Jh", "9h"),
                 opponents=(OpponentSnapshot("BB", "丙", "BB", 99.0, 1.0, False),))
    lines = render_rules_zh(situation(point, opponent_reads=(read,)))
    eq(len(lines), 1, f"无尺寸引用 → 只有 intent 行：{lines}")
    check("规则: SB 无前注 → 加注" in lines[0], f"SB intent 行：{lines[0]!r}")
    check("平跟（25%）" in lines[0], f"25% 平跟混合要写进 why：{lines[0]!r}")


def test_render_threebet_size() -> None:
    """面对一个加注、手牌在 3-bet 范围 → 只有尺寸行（尺寸槽规则）。"""
    raise_act = ActionRecord("preflop", "UTG", None, "UTG", "raise", 3.0,
                             False, False)
    point = snap(position="BTN", cards=("Ah", "Ad"), streets=(raise_act,),
                 shapes=("single-raise",), to_call=3.0, pot=4.5)
    lines = render_rules_zh(situation(point))
    eq(lines, ["尺寸: 3-bet 尺寸 → 对手加注的 3 倍（有位置） [书 p25]"],
       "3-bet 尺寸行（有位置）")


def test_render_zero_hits() -> None:
    """72o UTG 不在开池范围 → 零命中，空列表（不写占位）。"""
    eq(render_rules_zh(situation(snap(position="UTG", cards=("7s", "2d")))),
       [], "零命中返回空列表")


# ─────────────────────────── 3. _size_text ───────────────────────────

def test_size_text_value_units() -> None:
    eq(_size_text({"table": "rule_params", "name": "cbet_size_hu"}, {}),
       "半池", "pot 标量走 §6 档位表")
    eq(_size_text({"table": "rule_params", "name": "cbet_size_hu",
                   "unit": "bb"}, {}), "0.5BB", "bb 标量")
    eq(_size_text({"table": "rule_params", "name": "cbet_size_hu",
                   "unit": "raise"}, {}), "对手加注的 0.5 倍", "raise 标量")


def test_size_text_rows_units() -> None:
    eq(_size_text({"table": "rule_params", "name": "open_size_bb",
                   "unit": "bb", "key": "position_group"},
                  {"hero_position": "UTG"}), "2.75BB（早位）", "早位开池")
    eq(_size_text({"table": "rule_params", "name": "open_size_bb",
                   "unit": "bb", "key": "position_group"},
                  {"hero_position": "CO"}), "3.75BB（晚位）", "晚位开池")
    eq(_size_text({"table": "rule_params", "name": "threebet_multiplier",
                   "unit": "raise", "key": "in_position"},
                  {"in_position": True}), "对手加注的 3 倍（有位置）", "有位置 3-bet")
    eq(_size_text({"table": "rule_params", "name": "threebet_multiplier",
                   "unit": "raise", "key": "in_position"},
                  {"in_position": False}), "对手加注的 4 倍（无位置）", "无位置 3-bet")


def test_size_text_items_units() -> None:
    eq(_size_text({"table": "rule_params", "name": "fourbet_multiplier",
                   "unit": "raise"}, {}), "对手加注的 2.2–2.5 倍", "raise 区间")
    eq(_size_text({"table": "rule_params", "name": "river_value_band"}, {}),
       "0.66–1.0 倍池", "pot 区间")
    check(_size_text({"table": "rule_params", "name": "fourbet_min_tier",
                      "unit": "raise"}, {}) is None,
          "items 不是数字（premium）不出尺寸")


def test_size_text_per_limpers() -> None:
    ref = {"table": "rule_params", "name": "iso_size_per_limper",
           "unit": "bb", "per": "limpers_before"}
    eq(_size_text(ref, {}), "每多一个溜入者 +1BB", "per + bb（缺 facts 也出）")
    ref["unit"] = "pot"
    eq(_size_text(ref, {}), "每多一个溜入者 +1 倍池", "per + pot")


def test_size_text_fail_open() -> None:
    check(_size_text({"table": "rule_params", "name": "nope"}, {}) is None,
          "不存在的表名 → None")
    check(_size_text({"table": "rule_params", "name": "open_size_bb",
                      "key": "position_group"}, {}) is None,
          "缺 hero_position → 不出行")
    check(_size_text({"table": "rule_params", "name": "open_size_bb",
                      "key": "position_group"},
                     {"hero_position": "MP"}) is None, "未知位置分组 → 不出行")
    check(_size_text({"table": "rule_params", "name": "threebet_multiplier",
                      "key": "in_position"}, {}) is None,
          "缺 in_position → 不出行")
    check(_size_text({"table": "rule_params", "name": "open_size_bb",
                      "key": "position_group", "unit": "furlong"},
                     {"hero_position": "BTN"}) is None, "未知 unit → 不出行")
    check(_size_text({"table": "rule_params", "name": "cbet_size_hu",
                      "per": "pot_bb"}, {}) is None, "未知 per → 不出行")
    check(_size_text("0.5", {}) is None, "size 不是字典 → 不出行")


# ─────────────────────────── 4. witness 池级一次 ───────────────────────────

def _row(**over) -> dict:
    base = {
        "player": "p1", "hand": "20261101-abcdef1234", "seat": "s1",
        "ts": "2026-11-01T20:00:00+08:00", "street": "river",
        "action": "check", "position": "ip", "cards": ["7s", "Ad"],
        "board": ["6c", "2h", "As", "5c", "9d"],
        "family": "weak_ace_top_pair", "show_kind": "showdown",
        "status": A.STATUS_ADMITTED,
        "winner": {"status": "unknown", "seat": None, "family": None},
        "source": "outbox", "table": "20261101-deadbeefcafe",
    }
    base.update(over)
    return base


def _rows(player: str, family: str, n: int, seat: str, ts0: int = 0) -> list[dict]:
    return [_row(player=player, seat=seat, family=family,
                 ts=f"2026-11-{1 + (ts0 + i) // 24:02d}T{(ts0 + i) % 24:02d}:00:00+08:00")
            for i in range(n)]


def test_pool_evidence_once_per_prompt() -> None:
    """3 个座位都没有个人证据：池级句只能出现 1 次，且不挂在任何座位下。"""
    pool_rows = _rows("someone", "weak_ace_top_pair", 20, "s9")
    seats = {"s1": "p1", "s2": "p2", "s3": "p3"}
    got = W.render_witness_lines(pool_rows, seats=seats)
    joined = "\n".join(line for lines in got.values() for line in lines)
    eq(joined.count("这个池子"), 1, f"池级句出现次数：{got}")
    check(set(got) == {W.POOL_KEY}, f"池级不挂到具体座位：{got}")

    # 9 个座位全无个人证据：改造前会出 9×2=18 行、两两重复；改造后只剩 2 行。
    nine = {f"s{i}": f"p{i}" for i in range(1, 10)}
    got9 = W.render_witness_lines(pool_rows, seats=nine)
    emitted = [line for lines in got9.values() for line in lines]
    eq(len(emitted), len(set(emitted)), f"多座位不得有重复行：{got9}")
    check(set(got9) == {W.POOL_KEY}, f"9 座位仍只出一份池级：{set(got9)}")

    # 有个人证据的座位照旧缩进拼接，池级另起一段、整段只一次。
    personal_rows = pool_rows + _rows("p1", "two_pair_plus", 1, "s1", ts0=40)
    got = W.render_witness_lines(personal_rows, seats=seats)
    joined = "\n".join(line for lines in got.values() for line in lines)
    eq(joined.count("这个池子"), 1, f"有个人证据时仍只一次：{got}")
    check("s1" in got and W.POOL_KEY in got, f"个人与池级并存：{got}")


def test_pool_paragraph_not_indented() -> None:
    """接到提示词里：池级句独立成段，行首不是两空格缩进。"""
    pool_rows = _rows("someone", "weak_ace_top_pair", 20, "s9")
    pool = W.render_witness_lines(pool_rows, seats={"s1": "p1"})[W.POOL_KEY]
    reads = (
        OpponentRead(seat_id="s1", key="k1", display_name="某甲",
                     archetype="tag", confidence=0.8, n=60, vpip=22.0,
                     pfr=8.0, af=1.5, source="auto", note="", gate="stable"),
        OpponentRead(seat_id="s2", key="k2", display_name="某乙",
                     archetype="station", confidence=0.8, n=60, vpip=45.0,
                     pfr=5.0, af=0.7, source="auto", note="", gate="stable"),
    )
    positions = {"s1": "CO", "s2": "BTN"}
    lines = render_reads_zh(reads, positions,
                            witness_by_seat={W.POOL_KEY: pool})
    text = "\n".join(lines)
    eq(text.count("这个池子"), 1, f"提示词里池级句只出现一次：\n{text}")
    pool_lines = [line for line in lines if "这个池子" in line]
    check(pool_lines and not pool_lines[0].startswith("  "),
          f"池级句不缩进、不挂座位：{pool_lines!r}")

    # 没有任何座位画像时，池级段也能单独出。
    only_pool = render_reads_zh((), {}, witness_by_seat={W.POOL_KEY: pool})
    check(sum("这个池子" in line for line in only_pool) == 1,
          f"无座位画像时池级仍出一次：{only_pool!r}")


# ─────────────────────────── runner ───────────────────────────

def main() -> int:
    tests = [
        test_ledger_eight_rules,
        test_export_rules_check_ok,
        test_render_first_in_early,
        test_render_iso_limper,
        test_render_sb_no_ante,
        test_render_threebet_size,
        test_render_zero_hits,
        test_size_text_value_units,
        test_size_text_rows_units,
        test_size_text_items_units,
        test_size_text_per_limpers,
        test_size_text_fail_open,
        test_pool_evidence_once_per_prompt,
        test_pool_paragraph_not_indented,
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
    print("\n✅ 翻前 8 条规则测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
