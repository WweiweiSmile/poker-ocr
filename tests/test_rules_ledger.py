#!/usr/bin/env python3
"""规则台账骨架（`poker_ocr.rules`）的测试。

    nv tests/test_rules_ledger.py

票 64 是整条链路的 tracer bullet：loader → 谓词快照 → 匹配 → 校验。
四组用例逐条对应验收：

  1. 校验器：schema / 谓词白名单 / `street` 前提 / `size` 引用 / 槽位 payload /
     同槽位交集 / coexist 双向。坏规则必须报错，且错误里带**规则 id**
     ——排序时靠 id 定位，报「第 3 条」等于没报。
  2. 谓词快照：现有 fixture 局面能算出设计文档 §5 的键；缺事实一律 fail open
     （键不出现），绝不抛异常、绝不塞 None。
  3. 匹配器：空台账零命中；缺事实不命中（包括 `not_in`——没有事实就谈不上
     「不属于」）；条件齐了才命中。
  4. 工具：`export_rules --check` 空台账退出 0，坏台账退出 1 并打印 id。

期望值不是抄实现：谓词取值对着设计文档 §5 的口径写死，改这些值等于改
台账语义，应该先改设计文档。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import rules as r  # noqa: E402
from poker_ocr.advice import build_situation  # noqa: E402
from poker_ocr.coach import TPTK  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def eq(got, want, label: str) -> None:
    check(got == want, f"{label}: 期望 {want!r}，实际 {got!r}")


# ─────────────────────── 造规则 / 台账 ───────────────────────

BLINDS = Blinds(sb=0.5, bb=1.0, ante=None, raw="1/2")


def rule(**kw) -> dict:
    """一条合法规则；用例只覆盖要破坏的那个字段。"""
    base = {
        "id": "pf.test.rule",
        "version": 1,
        "slot": "intent",
        "street": "preflop",
        "node": "测试节点",
        "preconditions": [
            {"fact": "street", "eq": "preflop"},
            {"fact": "in_position", "eq": True},
        ],
        "action": "raise",
        "why": "测试用规则，只验证骨架。",
        "source": {"book": "Little Green Book", "page": 18,
                   "status": "verbatim"},
        "confidence": "book",
        "verified": False,
    }
    base.update(kw)
    return base


def write_ledger(directory: Path, *items: dict, name: str = "preflop.json") -> None:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "rules": list(items)},
                               ensure_ascii=False), encoding="utf-8")


def errors_of(*items: dict) -> str:
    """写进临时台账再加载，返回规则错误的全部文本。"""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        write_ledger(directory, *items)
        try:
            r.load_ledger(directory)
        except r.RuleError as exc:
            return "\n".join(exc.errors)
    return ""


def check_ok(*items: dict, label: str = "") -> None:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_ledger(directory, *items)
            r.load_ledger(directory)
    except r.RuleError as exc:
        FAILS.append(f"{label or '应通过'}：却报错\n" + "\n".join(exc.errors))


def check_bad(*items: dict, label: str = "", must: tuple[str, ...] = ()) -> str:
    text = errors_of(*items)
    if not text:
        FAILS.append(f"{label or '应报错'}：没有报错")
        return ""
    for token in must:
        check(token in text, f"{label or '应报错'}：错误里缺少 {token!r}：{text}")
    return text


# ─────────────────────── 1. 校验器 ───────────────────────

def test_empty_ledger_loads() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        eq(r.load_ledger(Path(tmp)), (), "空目录 = 空台账")


def test_valid_rule_loads() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        write_ledger(directory, rule())
        loaded = r.load_ledger(directory)
    eq(len(loaded), 1, "载入一条规则")
    got = loaded[0]
    eq(got.id, "pf.test.rule", "id")
    eq(got.slot, "intent", "slot")
    eq(got.street, "preflop", "street")
    eq(got.action, "raise", "action")
    eq(tuple((p.fact, p.op, p.value) for p in got.preconditions),
       (("street", "eq", "preflop"), ("in_position", "eq", True)),
       "前提解析成 (fact, op, value)")


def test_unknown_predicate_rejected() -> None:
    bad = rule(preconditions=[
        {"fact": "street", "eq": "preflop"},
        {"fact": "hero_mood", "eq": "happy"},
    ])
    check_bad(bad, label="未知谓词", must=("pf.test.rule", "hero_mood"))


def test_missing_street_rejected() -> None:
    bad = rule(preconditions=[
        {"fact": "in_position", "eq": True},
        {"fact": "limpers_before", "between": [1, 2]},
    ])
    check_bad(bad, label="缺 street 前提", must=("pf.test.rule", "street"))


def test_single_precondition_rejected() -> None:
    """至少两条：一条前提的规则等于无条件，会把整条街兜住。"""
    bad = rule(preconditions=[{"fact": "street", "eq": "preflop"}])
    check_bad(bad, label="只有一条前提", must=("pf.test.rule",))


def test_literal_size_rejected() -> None:
    bad = rule(size=3.0)
    check_bad(bad, label="size 写字面数字", must=("pf.test.rule", "size"))


def test_size_reference_unresolvable() -> None:
    bad = rule(size={"table": "no_such_table", "name": "no_such_entry"})
    check_bad(bad, label="size 引用解析不了", must=("pf.test.rule", "size"))


def test_slot_payload_checks() -> None:
    check_bad(rule(action=None), label="intent 缺 action", must=("action",))
    check_bad(rule(slot="size"), label="size 槽缺 size", must=("size",))
    check_bad(rule(slot="why", size={"table": "fold_equity", "name": "price_ref"}),
              label="why 槽带了 size", must=("why",))
    check_bad(rule(action="shove"), label="action 不在 options 词表",
              must=("action",))
    check_bad(rule(why="字" * 41), label="why 超 40 字", must=("why",))
    check_bad(rule(node="字" * 31), label="node 超 30 字", must=("node",))


def test_position_domain_follows_positions_py() -> None:
    """位置白名单必须与 `positions.py` 逐字一致：`MP` 在牌桌上不存在。"""
    from poker_ocr import positions

    for name in (positions.SB, positions.BB, positions.UTG, positions.UTG1,
                 positions.UTG2, positions.LJ, positions.HJ, positions.CO,
                 positions.BTN):
        ok = rule(id="pf.pos", preconditions=[
            {"fact": "street", "eq": "preflop"},
            {"fact": "hero_position", "eq": name},
        ])
        check_ok(ok, label=f"hero_position 接受 {name}")

    bad = rule(id="pf.pos", preconditions=[
        {"fact": "street", "eq": "preflop"},
        {"fact": "hero_position", "eq": "MP"},
    ])
    check_bad(bad, label="hero_position 不接受 MP", must=("pf.pos", "hero_position"))


def test_operator_checks() -> None:
    check_bad(rule(preconditions=[{"fact": "street", "eq": "preflop"},
                                  {"fact": "spr", "roughly": 3}]),
              label="未知算子", must=("roughly",))
    check_bad(rule(preconditions=[{"fact": "street", "eq": "flop"},
                                  {"fact": "spr", "gte": 8},
                                  {"fact": "spr", "lte": 2}]),
              label="同一谓词区间为空", must=("pf.test.rule",))


def test_source_checks() -> None:
    check_bad(rule(source={"book": "Little Green Book", "status": "verbatim"}),
              label="书来源缺页码", must=("page",))
    check_bad(rule(source={"book": "Little Green Book", "page": 18,
                           "status": "hearsay"}),
              label="status 不在 {verbatim, rebuilt}", must=("status",))


def test_duplicate_id_rejected() -> None:
    check_bad(rule(), rule(), label="id 重复", must=("pf.test.rule",))


def test_overlap_same_slot_rejected() -> None:
    """同槽位 + 前提有交集 → 同一个命中点会出两条 intent，必须静态失败。"""
    a = rule(id="pf.a")
    b = rule(id="pf.b")
    text = check_bad(a, b, label="同槽位前提相交", must=("pf.a", "pf.b"))
    check("coexist_with" in text, f"同槽位相交的报错要提示 coexist_with：{text}")


def test_coexist_must_be_bidirectional() -> None:
    a = rule(id="pf.a", coexist_with=["pf.b"])
    b = rule(id="pf.b")
    check_bad(a, b, label="单向 coexist", must=("pf.a", "pf.b"))
    ok_a = rule(id="pf.a", coexist_with=["pf.b"])
    ok_b = rule(id="pf.b", coexist_with=["pf.a"])
    check_ok(ok_a, ok_b, label="双向 coexist")


def test_disjoint_same_slot_allowed() -> None:
    a = rule(id="pf.a", preconditions=[{"fact": "street", "eq": "preflop"},
                                       {"fact": "in_position", "eq": True}])
    b = rule(id="pf.b", preconditions=[{"fact": "street", "eq": "preflop"},
                                       {"fact": "in_position", "eq": False}])
    check_ok(a, b, label="同槽位但前提互斥")


def test_other_slot_does_not_conflict() -> None:
    intent = rule(id="pf.a")
    size = rule(id="pf.b", slot="size", action=None,
                size={"table": "fold_equity", "name": "price_ref"})
    check_ok(intent, size, label="不同槽位可共存")


# ─────────────────────── 2. 谓词快照 ───────────────────────

def flop_snap() -> HandSnapshot:
    return HandSnapshot(
        hand_id="h1", hand_index=1, street="flop",
        board=("Ks", "7d", "2c"), hero_cards=("Ah", "Kd"),
        hero_position="BTN", hero_stack=97.0, hero_bet=0.0,
        hero_invested=9.0, to_call=4.0, second_bet=None,
        pot=31.0, pot_before_street=27.0, table_size=6, blinds=BLINDS,
        opponents=(
            OpponentSnapshot("CO", "老王", "CO", 88.0, 4.0, False),
            OpponentSnapshot("BB", "小李", "BB", 88.0, 0.0, False),
        ),
        streets=(
            StreetRecord("preflop", 1.5, (
                ActionRecord("preflop", "UTG", None, "UTG", "raise", 3.0,
                             False, False),
                ActionRecord("preflop", "hero", None, "BTN", "raise", 9.0,
                             False, True),
                ActionRecord("preflop", "BB", None, "BB", "call", 9.0,
                             False, False),
            )),
            StreetRecord("flop", 27.0, (
                ActionRecord("flop", "CO", "老王", "CO", "bet", 4.0,
                             False, False),
            )),
        ),
        preflop_shapes=("three-bet",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("h1", "flop", 2, 31.0, 4.0, ("Ah", "Kd")),
        players_dealt=6,
    )


def preflop_snap() -> HandSnapshot:
    return HandSnapshot(
        hand_id="h2", hand_index=2, street="preflop",
        board=(), hero_cards=("4s", "2s"),
        hero_position="CO", hero_stack=100.0, hero_bet=0.0,
        hero_invested=0.0, to_call=1.0, second_bet=None,
        pot=1.5, pot_before_street=1.5, table_size=6, blinds=BLINDS,
        opponents=(
            OpponentSnapshot("BTN", "甲", "BTN", 100.0, 0.0, False),
            OpponentSnapshot("SB", "乙", "SB", 99.5, 0.5, False),
            OpponentSnapshot("BB", "丙", "BB", 99.0, 1.0, False),
        ),
        streets=(StreetRecord("preflop", 1.5, ()),),
        preflop_shapes=("unopened",), hero_is_pfr=False, cbet_spot=False,
        decision_key=("h2", "preflop", 0, 1.5, 1.0, ("4s", "2s")),
        players_dealt=6,
    )


def test_facts_snapshot_flop() -> None:
    facts = r.facts_snapshot(build_situation(flop_snap()))
    check(set(facts) <= r.PREDICATES,
          f"快照键越出白名单：{sorted(set(facts) - r.PREDICATES)}")
    eq(facts.get("street"), "flop", "street")
    eq(facts.get("hero_position"), "BTN", "hero_position")
    eq(facts.get("in_position"), True, "in_position")
    eq(facts.get("players_in_hand"), 3, "players_in_hand（含 hero）")
    eq(facts.get("pot_bb"), 31.0, "pot_bb")
    eq(facts.get("to_call_bb"), 4.0, "to_call_bb")
    eq(facts.get("pot_type"), "3-bet+ pot", "pot_type")
    eq(facts.get("hero_hand_tier"), TPTK, "hero_hand_tier")
    eq(facts.get("board_paired"), False, "board_paired")
    eq(facts.get("hero_is_first_to_act"), False, "hero_is_first_to_act（BTN 最后）")
    check(isinstance(facts.get("board_texture"), str), "board_texture 是文本")
    eq(round(facts.get("pot_odds", -1), 6), round(4.0 / 35.0, 6),
       "pot_odds = 跟注额 / 跟注后可争夺底池（pot_math.pot_odds 口径）")
    check(facts.get("spr", 0) > 0, f"spr 应 > 0：{facts.get('spr')!r}")
    eq(facts.get("effective_stack_bb"), 88.0,
       "effective_stack_bb = min(我方总筹码, 未弃牌对手总筹码)")
    check("previous_line" not in facts, "没有上一街建议时不编 previous_line")
    check("hand_in_open_range" not in facts, "翻后不算开池范围")
    eq(facts.get("hand_label"), "顶对A踢", "hand_label")
    eq(facts.get("outs"), 0, "outs（顶对没有听牌 outs）")
    eq(facts.get("equity_pct"), 0.0, "equity_pct 走四二法则")
    eq(facts.get("has_draw"), False, "has_draw（顶对不算听牌）")
    eq(facts.get("explosive_pot"), False, "explosive_pot（底池读到了，4.5BB/人）")
    check("limpers_before" not in facts, "limpers_before 只描述翻前")
    check("raises_before" not in facts, "raises_before 只描述翻前")


def test_facts_snapshot_preflop() -> None:
    facts = r.facts_snapshot(build_situation(preflop_snap()))
    eq(facts.get("street"), "preflop", "street")
    eq(facts.get("hand_in_open_range"), False, "hand_in_open_range（42s CO 不开池）")
    eq(facts.get("hand_in_3bet_range"), False, "hand_in_3bet_range")
    check("spr" not in facts, "翻前没有 spr")
    check("hero_is_first_to_act" not in facts, "hero_is_first_to_act 只对翻后有意义")
    check("ante_bb" not in facts, "无前注时不写 ante_bb")
    check(0.0 < facts.get("pot_odds", -1) <= 1.0,
          f"翻前 facing open 也有赔率：{facts.get('pot_odds')!r}")

    # 面对一个开池时，raises_before 数的是「hero 要面对的加注」。
    facing = replace(
        preflop_snap(), to_call=3.0, pot=4.5, pot_before_street=1.5,
        streets=(StreetRecord("preflop", 1.5, (
            ActionRecord("preflop", "UTG", None, "UTG", "raise", 3.0,
                         False, False),
        )),),
        preflop_shapes=("single-raise",),
    )
    opened = r.facts_snapshot(build_situation(facing))
    eq(opened.get("raises_before"), 1, "raises_before（UTG 开池）")
    eq(opened.get("limpers_before"), 0, "limpers_before（开池前无人平跟）")


def test_facts_snapshot_opponent_reads() -> None:
    """对手画像谓词只在「这一个对手」确定时才写：多人时容易张冠李戴。"""
    from poker_ocr.opponent_profile import OpponentRead

    read = OpponentRead(
        seat_id="CO", key="老王", display_name="老王", archetype="station",
        confidence=0.7, n=40, vpip=0.52, pfr=0.08, af=0.9,
        source="pooled", note="", gate="ok", fold_to_cbet_pct=0.28,
    )
    sit = build_situation(flop_snap(), opponent_reads=(read,))
    facts = r.facts_snapshot(sit)
    eq(facts.get("opponent_archetype"), "station", "opponent_archetype")
    eq(facts.get("opp_vpip"), 0.52, "opp_vpip")
    eq(facts.get("opp_pfr"), 0.08, "opp_pfr")
    eq(facts.get("opp_fold_to_cbet"), 0.28, "opp_fold_to_cbet")

    # 第二个对手在场 → 画像不再是「对面的那个人」，全部不出面。
    other = replace(read, seat_id="BB", key="小李", archetype="nit")
    facts = r.facts_snapshot(build_situation(flop_snap(),
                                             opponent_reads=(read, other)))
    for key in ("opponent_archetype", "opp_vpip", "opp_pfr", "opp_fold_to_cbet"):
        check(key not in facts, f"两个对手画像时不该写 {key}：{facts.get(key)!r}")


def test_facts_snapshot_fail_open() -> None:
    """什么都读不到的帧：键缺席，但绝不抛异常、绝不写 None。"""
    snap = HandSnapshot(
        hand_id="h3", hand_index=3, street="preflop",
        board=(), hero_cards=(), hero_position=None, hero_stack=None,
        hero_bet=0.0, hero_invested=0.0, to_call=0.0, second_bet=None,
        pot=None, pot_before_street=0.0, table_size=6, blinds=None,
        opponents=(), streets=(StreetRecord("preflop", 0.0, ()),),
        preflop_shapes=(), hero_is_pfr=False, cbet_spot=False,
        decision_key=("h3", "preflop", 0, None, 0.0, ()),
        players_dealt=0,
    )
    try:
        facts = r.facts_snapshot(build_situation(snap, include_strength=False))
    except Exception as exc:  # noqa: BLE001 - 就是要断言这里不抛
        FAILS.append(f"缺事实时应 fail open，却抛了 {type(exc).__name__}: {exc}")
        return
    for key, value in facts.items():
        check(value is not None, f"{key} 不该是 None（缺事实就不写这个键）")
    check("pot_bb" not in facts, "没有底池就不写 pot_bb")
    check("spr" not in facts, "没有筹码就不写 spr")
    check("effective_stack_bb" not in facts, "没有筹码就不写 effective_stack_bb")
    check(set(facts) <= r.PREDICATES, "快照键越出白名单")


# ─────────────────────── 3. 匹配器 ───────────────────────

def test_match_zero_hits_on_empty_ledger() -> None:
    eq(r.match_rules({"street": "preflop"}, ()), (), "空台账零命中")
    eq(r.match_rules({}, ()), (), "空事实 + 空台账也零命中")


def test_match_requires_every_precondition() -> None:
    rules = (
        r.Rule(id="pf.a", version=1, slot="intent", street="preflop",
               node="x", action="raise", why="y",
               preconditions=(
                   r.Precondition("street", "eq", "preflop"),
                   r.Precondition("spr", "gte", 3),
               )),
    )
    eq(r.match_rules({"street": "preflop", "spr": 5}, rules)[0].id, "pf.a",
       "条件齐了命中")
    eq(r.match_rules({"street": "preflop", "spr": 2}, rules), (),
       "条件不满足不命中")
    eq(r.match_rules({"street": "preflop"}, rules), (),
       "缺事实 fail open 到不命中")


def test_missing_fact_never_matches_not_in() -> None:
    """没有事实就谈不上「不属于」——`not_in` 同样 fail open。"""
    rules = (
        r.Rule(id="po.a", version=1, slot="intent", street="flop",
               node="x", action="bet", why="y",
               preconditions=(
                   r.Precondition("street", "eq", "flop"),
                   r.Precondition("board_texture", "not_in", ("公对面",)),
               )),
    )
    eq(r.match_rules({"street": "flop"}, rules), (),
       "缺 board_texture 时 not_in 不命中")
    eq(r.match_rules({"street": "flop", "board_texture": "干燥"}, rules)[0].id,
       "po.a", "有事实且确实不属于时命中")


def test_bool_is_not_int() -> None:
    """`1 == True` 在 Python 里成立；台账里 False 不能被 0/1 蒙混。"""
    p = r.Precondition("in_position", "eq", False)
    check(not r.precondition_holds(p, {"in_position": 0}),
          "0 不应满足 eq False")
    check(r.precondition_holds(p, {"in_position": False}),
          "False 满足 eq False")


# ─────────────────────── 4. 工具 ───────────────────────

def test_enum_domain_exhausted_is_empty() -> None:
    """`not_in` 把所有取值都排除掉时，前提集合真的是空的。"""
    bad = rule(preconditions=[
        {"fact": "street", "eq": "flop"},
        {"fact": "board_texture",
         "not_in": list(r.PREDICATE_DOMAINS["board_texture"])},
    ])
    text = check_bad(bad, label="排除掉整个取值域")
    check("取值区间为空" in text, f"缺「取值区间为空」：{text}")

    # 两条规则同槽位、牌面互斥（in 一条 / not_in 另一条）→ 前提不相交，不该报冲突。
    left = rule(id="po.a", street="flop", preconditions=[
        {"fact": "street", "eq": "flop"},
        {"fact": "board_texture", "in": ["干燥"]},
    ])
    right = rule(id="po.b", street="flop", preconditions=[
        {"fact": "street", "eq": "flop"},
        {"fact": "board_texture", "not_in": ["干燥"]},
    ])
    check_ok(left, right, label="in 与 not_in 互斥的两条同槽位规则可以共存")

    # 两条都不含 street 之外的交集时仍然冲突（回归：别把交集判空了）。
    both_dry = rule(id="po.c", street="flop", preconditions=[
        {"fact": "street", "eq": "flop"},
        {"fact": "board_texture", "in": ["干燥"]},
    ])
    text = check_bad(left, both_dry, label="两条都要求干燥")
    check("po.a" in text and "po.b" not in text, f"冲突要指名到 po.a：{text}")


def test_pot_odds_matches_pot_math() -> None:
    """赔率只算一份：事实层直接吃 `pot_math.pot_odds`。"""
    from poker_ocr.pot_math import pot_odds

    sit = build_situation(flop_snap())
    eq(r.facts_snapshot(sit)["pot_odds"], pot_odds(sit.snap), "口径一致")


def test_export_rules_check_empty_ok() -> None:
    res = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "export_rules.py"), "--check"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    eq(res.returncode, 0, f"--check 空台账应退出 0：{res.stdout}{res.stderr}")


def test_export_rules_check_bad_ledger() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        write_ledger(directory, rule(id="pf.bad.street", preconditions=[
            {"fact": "in_position", "eq": True},
            {"fact": "limpers_before", "between": [1, 2]},
        ]))
        res = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "export_rules.py"),
             "--check", "--dir", str(directory)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
    eq(res.returncode, 1, f"坏台账应退出 1：{res.stdout}{res.stderr}")
    check("pf.bad.street" in (res.stdout + res.stderr),
          f"错误输出要带规则 id：{res.stdout}{res.stderr}")


# ─────────────── 5. rule_params 表与新谓词（票 68） ───────────────

def test_new_predicates_deterministic() -> None:
    """五个新谓词在现有 fixture 上的确定取值（票 68 / 设计 §5）。"""
    # hand_in_open_range / hand_in_3bet_range：AA 在 CO 必开、42s 必不开。
    strong = r.facts_snapshot(build_situation(
        replace(preflop_snap(), hero_cards=("As", "Ad"))))
    eq(strong.get("hand_in_open_range"), True,
       "hand_in_open_range（AA CO 在 RFI 表里）")
    eq(strong.get("hand_in_3bet_range"), True,
       "hand_in_3bet_range（AA 在 3-bet 宽度表里）")

    weak = r.facts_snapshot(build_situation(preflop_snap()))
    eq(weak.get("hand_in_open_range"), False,
       "hand_in_open_range（42s CO 不在 RFI 表里）")
    eq(weak.get("hand_in_3bet_range"), False,
       "hand_in_3bet_range（42s 不在 3-bet 宽度表里）")

    # board_paired：干燥面 False，公对面 True。
    dry = r.facts_snapshot(build_situation(flop_snap()))
    eq(dry.get("board_paired"), False, "board_paired（K72 干燥面）")
    paired = r.facts_snapshot(build_situation(
        replace(flop_snap(), board=("Ks", "Kd", "2c"))))
    eq(paired.get("board_paired"), True, "board_paired（KK2 公对面）")

    # hero_is_first_to_act：翻后按行动线给 bool，翻前 fail open 缺键。
    eq(dry.get("hero_is_first_to_act"), False,
       "hero_is_first_to_act（BTN 最后行动）")
    first = r.facts_snapshot(build_situation(
        replace(flop_snap(), hero_position="SB")))
    eq(first.get("hero_is_first_to_act"), True,
       "hero_is_first_to_act（SB 第一个行动）")
    check("hero_is_first_to_act" not in weak,
          "翻前不写 hero_is_first_to_act")

    # ante_bb：Blinds.ante 已经是 BB/人，事实层不再换算。
    # 口径证据：parse.py:191（字段注释）与 parse.py:219-221（筹码 / bb 筹码）。
    from poker_ocr.parse import parse_blinds
    parsed = parse_blinds("1/2(1)")
    check(parsed is not None and parsed.ante == 0.5,
          f"parse_blinds 把括号里的 1 个筹码折成 0.5BB：{parsed!r}")
    ante = r.facts_snapshot(build_situation(
        replace(preflop_snap(),
                blinds=Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)"))))
    eq(ante.get("ante_bb"), 0.5,
       "ante_bb（1/2(1) 的 1 个筹码 = 0.5BB，直接取 Blinds.ante）")
    big = r.facts_snapshot(build_situation(
        replace(preflop_snap(),
                blinds=Blinds(sb=0.5, bb=1.0, ante=5.0, raw="1/2(5)"))))
    eq(big.get("ante_bb"), 5.0, "ante_bb（5 个筹码 = 5BB，不乘 bb）")


def test_new_predicates_fail_open_without_data() -> None:
    """五个新谓词缺数据时一律不出现，且不抛异常（设计 §4.2 fail open）。"""
    snap = replace(preflop_snap(), hero_cards=(), blinds=None)
    try:
        facts = r.facts_snapshot(build_situation(snap, include_strength=False))
    except Exception as exc:  # noqa: BLE001 - 就是要断言这里不抛
        FAILS.append(f"新谓词缺数据时应 fail open，却抛了 "
                     f"{type(exc).__name__}: {exc}")
        return
    for key in ("hand_in_open_range", "hand_in_3bet_range", "ante_bb",
                "board_paired", "hero_is_first_to_act"):
        check(key not in facts, f"缺数据时不该写 {key}：{facts.get(key)!r}")


def test_rule_params_table_entries() -> None:
    """rule_params 十二个条目都能被 table() 取到，值与来源符合书页（票 68/69）。"""
    from poker_ocr.strategy_tables import table as strategy_table

    expected = {
        "open_size_bb": ("rows", {"early": 2.75, "middle": 3.25,
                                  "late": 3.75, "sb": 3.0}, "book:p19"),
        "threebet_multiplier": ("rows", {"in_position": 3.0,
                                         "out_of_position": 4.0}, "book:p26"),
        "fourbet_min_tier": ("items", ("premium",), "book:p25"),
        "cbet_freq_hu": ("value", 0.65, "book:p30"),
        "cbet_size_hu": ("value", 0.5, "book:p30"),
        "cbet_size_paired": ("value", 0.4, "book:p28"),
        "size_ladder": ("rows", {"strong": 0.33, "medium": 0.66,
                                 "weak": 1.0}, "book:p35"),
        "block_bet": ("value", 0.25, "book:p33"),
        "river_value_band": ("items", (0.66, 1.0), "book:p52"),
        "iso_size_per_limper": ("value", 1.0, "book:p23"),
        # 票 69：规则 6/8 要引用、规则里又不许写数字，所以补这两条。
        "bb_complete_raise_bb": ("value", 3.0, "book:p22"),
        "fourbet_multiplier": ("items", (2.2, 2.5), "book:p25"),
    }
    for name, (kind, want, source) in expected.items():
        entry = strategy_table("rule_params", name)
        eq(entry.source, source, f"{name}.source")
        eq(entry.verified, False, f"{name}.verified")
        check(bool(entry.note), f"{name}.note 不能为空")
        check(bool(entry.needs), f"{name}.needs 不能为空")
        if kind == "rows":
            eq(dict(entry.rows), want, f"{name}.rows")
        elif kind == "items":
            eq(tuple(entry.items), want, f"{name}.items")
        else:
            eq(entry.value, want, f"{name}.value")


def test_rule_params_size_ref_success_and_failure() -> None:
    """size 引用成功 / 引用不存在的条目失败，错误要带规则 id 与路径。"""
    good = rule(id="pf.good.size", slot="size", action=None,
                size={"table": "rule_params", "name": "cbet_size_hu"})
    check_ok(good, label="size 引用 rule_params.cbet_size_hu")

    bad = rule(id="pf.bad.size", slot="size", action=None,
               size={"table": "rule_params", "name": "cbet_size_hux"})
    text = check_bad(bad, label="size 引用 rule_params 里不存在的条目",
                     must=("pf.bad.size", "rule_params.cbet_size_hux"))
    check("解析失败" in text or "找不到" in text,
          f"错误应说明引用解析不了：{text}")


def test_rule_params_literal_size_rejected() -> None:
    """规则 JSON 写死字面数字（size: 0.5）必须校验失败。"""
    bad = rule(id="pf.literal.size", slot="size", action=None, size=0.5)
    check_bad(bad, label="size 写死字面数字 0.5",
              must=("pf.literal.size", "size"))


def test_export_rules_check_accepts_rule_params_ref() -> None:
    """export_rules --check 能解析 rule_params 引用（票 68 验收）。"""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        write_ledger(directory, rule(
            id="pf.good.size", slot="size", action=None,
            size={"table": "rule_params", "name": "cbet_size_hu"}))
        res = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "export_rules.py"),
             "--check", "--dir", str(directory)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
    eq(res.returncode, 0,
       f"--check 应接受 rule_params 引用：{res.stdout}{res.stderr}")


def test_export_rules_check_rejects_missing_rule_param() -> None:
    """--check 对 rule_params 里不存在的条目要报错并带规则 id / 路径。"""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        write_ledger(directory, rule(
            id="pf.bad.missing", slot="size", action=None,
            size={"table": "rule_params", "name": "cbet_size_hux"}))
        res = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "export_rules.py"),
             "--check", "--dir", str(directory)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
    text = res.stdout + res.stderr
    eq(res.returncode, 1, f"rule_params 缺条目应退出 1：{text}")
    check("pf.bad.missing" in text and "rule_params.cbet_size_hux" in text,
          f"错误要含规则 id 与引用路径：{text}")


def test_export_rules_check_rejects_non_rule_params_size_kind() -> None:
    """size 引用的 kind 白名单只允许 rule_params（票 68 边界）。"""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        write_ledger(directory, rule(
            id="pf.bad.kind", slot="size", action=None,
            size={"table": "fold_equity", "name": "price_ref"}))
        res = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "export_rules.py"),
             "--check", "--dir", str(directory)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
    text = res.stdout + res.stderr
    eq(res.returncode, 1, f"size 引用非 rule_params 表应退出 1：{text}")
    check("pf.bad.kind" in text and "fold_equity.price_ref" in text,
          f"错误要含规则 id 与引用路径：{text}")


def test_script_style_harness_has_no_failures() -> None:
    """pytest 桥：脚本式用例把失败记在 FAILS，这里让 pytest 也红。

    本文件历史上是「脚本 + main() 汇总」的写法（`nv tests/test_rules_ledger.py`）；
    pytest 只收集函数、看不到 FAILS，所以补这一条让两种跑法结论一致。
    故意不放进 main() 的 tests 列表，避免脚本模式自己断言自己。
    """
    assert not FAILS, "脚本式断言失败：\n" + "\n".join(FAILS)


# ─────────────────────── 入口 ───────────────────────

def main() -> int:
    tests = [
        test_empty_ledger_loads,
        test_valid_rule_loads,
        test_unknown_predicate_rejected,
        test_missing_street_rejected,
        test_single_precondition_rejected,
        test_literal_size_rejected,
        test_size_reference_unresolvable,
        test_slot_payload_checks,
        test_position_domain_follows_positions_py,
        test_operator_checks,
        test_source_checks,
        test_duplicate_id_rejected,
        test_overlap_same_slot_rejected,
        test_coexist_must_be_bidirectional,
        test_disjoint_same_slot_allowed,
        test_other_slot_does_not_conflict,
        test_facts_snapshot_flop,
        test_facts_snapshot_preflop,
        test_facts_snapshot_opponent_reads,
        test_facts_snapshot_fail_open,
        test_match_zero_hits_on_empty_ledger,
        test_match_requires_every_precondition,
        test_missing_fact_never_matches_not_in,
        test_bool_is_not_int,
        test_enum_domain_exhausted_is_empty,
        test_pot_odds_matches_pot_math,
        test_export_rules_check_empty_ok,
        test_export_rules_check_bad_ledger,
        test_new_predicates_deterministic,
        test_new_predicates_fail_open_without_data,
        test_rule_params_table_entries,
        test_rule_params_size_ref_success_and_failure,
        test_rule_params_literal_size_rejected,
        test_export_rules_check_accepts_rule_params_ref,
        test_export_rules_check_rejects_missing_rule_param,
        test_export_rules_check_rejects_non_rule_params_size_kind,
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
    print("\n✅ 规则台账骨架测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
