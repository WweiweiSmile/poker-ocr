#!/usr/bin/env python3
"""桌况偏移层（票 #82）的验收。

    nv tests/test_playbook_offsets.py

覆盖：
  1. 独立配置可加载、字段只允许 size_bb / size_pct / freq，每条有适用条件与依据；
  2. 0.5BB 前注桌：书的 3BB 开注被抬到本桌最小合法 options 金额，动作不变；
  3. 未匹配的桌况（无前注/前注不足/没有 options）不产生偏移；
  4. 只改尺寸/频率：size_pct、freq 规则生效，action 永远不被改写；
  5. 真实提示词链路上出现「书计划 -> 偏移 -> 最终尺寸」，且 options 金额原样保留。
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import build_messages, build_situation  # noqa: E402
from poker_ocr.hand_track import HandSnapshot, OpponentSnapshot  # noqa: E402
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.playbook import prompt as book_prompt  # noqa: E402
from poker_ocr.playbook.offset import (  # noqa: E402
    OFFSET_OPS, OFFSET_TARGETS, OffsetRule, apply_offsets, load_offset_rules,
    validate_offset_rules,
)
from poker_ocr.playbook.schema import Card, Plan, Size, Spot  # noqa: E402
from poker_ocr.prompt.user.offsets import plan_offsets  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _snapshot(*, ante: float | None, street: str = "preflop",
              pot: float = 6.0, position: str = "BTN") -> HandSnapshot:
    opponents = tuple(
        OpponentSnapshot(f"p{i}", None, None, 100.0, 0.0, False)
        for i in range(8 if street == "preflop" else 3)
    )
    return HandSnapshot(
        hand_id="offset-demo", hand_index=0, street=street,
        board=() if street == "preflop" else ("Ks", "7d", "2c"),
        hero_cards=("Kh", "Kd"), hero_position=position, hero_stack=100.0,
        hero_bet=0.0, hero_invested=0.0, to_call=0.0, second_bet=None,
        pot=pot, pot_before_street=0.0, table_size=9 if street == "preflop" else 4,
        blinds=Blinds(0.5, 1.0, ante, "1/2(1)" if ante is not None else "1/2"),
        opponents=opponents,
        streets=(),
        preflop_shapes=("unopened",) if street == "preflop" else (),
        hero_is_pfr=False, cbet_spot=False,
        decision_key=("offset-demo", street, 0, pot, 0.0),
        warnings=(), bomb_pot=False,
        players_dealt=9 if street == "preflop" else 4,
    )


def _ante_sit(ante: float | None = 0.5):
    return build_situation(_snapshot(ante=ante), table_dynamics="")


def _open_card(size_bb: float = 3.0) -> Card:
    return Card(
        id="preflop.demo.open", book="Harrington", volume=1,
        chapter="demo", pages=(1,), street="preflop",
        spot=Spot(street="preflop", preflop_node="unopened"),
        plan=Plan(action="raise", size=Size("bb", size_bb)),
        why="演示卡：翻前未开池按书开注", confidence="interpreted",
    )


def _write_rules(tmp: Path, rules: list[dict]) -> Path:
    path = tmp / "offsets.json"
    path.write_text(json.dumps({"version": 1, "rules": rules}),
                    encoding="utf-8")
    return path


def _rule(**over) -> dict:
    base = {
        "id": "demo.rule",
        "when": {"street": "preflop"},
        "target": "size_bb",
        "op": "clamp_options",
        "priority": 10,
        "reason": "演示依据",
        "source": "test",
    }
    base.update(over)
    return base


# ─────────────────────────── 配置 ───────────────────────────

def test_shipped_config_is_independent_and_valid() -> None:
    """① 独立配置：只允许三种 target，每条都有条件、依据与来源。"""
    rules = load_offset_rules()
    check(bool(rules), "偏移配置不能为空")
    check(validate_offset_rules(rules) == [], f"配置校验失败：{validate_offset_rules(rules)}")
    check(set(OFFSET_TARGETS) == {"size_bb", "size_pct", "freq"},
          f"target 词表漂移：{OFFSET_TARGETS}")
    for rule in rules:
        check(rule.target in OFFSET_TARGETS, f"{rule.id}: target 越界 {rule.target}")
        check(bool(rule.when), f"{rule.id}: 缺适用条件")
        check(bool(rule.reason), f"{rule.id}: 缺依据")
        check(bool(rule.source), f"{rule.id}: 缺来源")
    check(any(r.id == "ante.open_size" for r in rules),
          "缺少 0.5BB 前注场景的第一条规则")


def test_config_rejects_targets_that_can_change_action() -> None:
    """① target=action 必须编译期拒绝，偏移层永远不碰动作。"""
    bad = [
        _rule(target="action"),
        _rule(op="delete"),
        _rule(op="add"),                    # 缺 value
        _rule(reason=""),
        _rule(when={}),
        _rule(when={"hand_strength": "strong"}),
        _rule(target="freq", op="clamp_options"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for i, row in enumerate(bad):
            row = dict(row, id=f"bad.{i}")
            path = _write_rules(Path(tmp), [row])
            try:
                load_offset_rules(path)
            except ValueError:
                continue
            FAILS.append(f"非法规则未被拒绝：{row}")

    dup = [_rule(id="same"), _rule(id="same", target="freq", op="set", value=0.8)]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            load_offset_rules(_write_rules(Path(tmp), dup))
        except ValueError:
            pass
        else:
            FAILS.append("重复 id 未被拒绝")


def test_only_size_and_frequency_ops_are_known() -> None:
    check(set(OFFSET_OPS) == {"add", "mul", "set", "clamp_options"},
          f"op 词表漂移：{OFFSET_OPS}")


# ─────────────────────────── 命中 / 未命中 ───────────────────────────

def test_ante_table_lifts_book_open_to_a_legal_option() -> None:
    """② 0.5BB 前注：书 3BB -> 本桌最小合法开注 7BB，动作仍是 raise。"""
    sit = _ante_sit(0.5)
    card = _open_card(3.0)
    deltas = apply_offsets(card, sit)
    check(len(deltas) == 1, f"应恰好一条尺寸偏移：{deltas}")
    if not deltas:
        return
    delta = deltas[0]
    check(delta.target == "size_bb", f"target 应 size_bb：{delta}")
    check(delta.baseline == "3BB", f"baseline 应保留书的 3BB：{delta}")
    check(delta.final == "7BB", f"final 应对齐本桌最小合法开注 7BB：{delta}")
    check("前注" in delta.applicability and "0.5" in delta.applicability,
          f"必须写清适用桌况：{delta.applicability}")
    check(bool(delta.reason), f"必须写清依据：{delta}")
    check(card.plan.action == "raise", "偏移不许改动作")
    raise_amounts = {o.amount for o in sit.options if o.kind == "raise"}
    check(7.0 in raise_amounts, f"final 必须落在合法 options：{raise_amounts}")


def test_unknown_or_small_ante_does_not_guess() -> None:
    """③ 没前注 / 前注不足：不产生偏移，书卡尺寸原样。"""
    card = _open_card(3.0)
    for ante in (None, 0.25):
        deltas = apply_offsets(card, _ante_sit(ante))
        check(deltas == (), f"ante={ante!r} 不该有偏移：{deltas}")
    check(card.plan.size is not None and card.plan.size.value == 3.0,
          "未命中时书卡尺寸不能变")


def test_no_options_means_no_guess() -> None:
    """③ 算不出合法 options 时 fail open，不伪造最终尺寸。"""
    sit = replace(_ante_sit(0.5), options=[])
    check(apply_offsets(_open_card(3.0), sit) == (),
          "没有 options 时偏移必须为空")


def test_already_legal_book_size_is_not_a_fake_offset() -> None:
    """③ 书尺寸已在合法范围内：不产生 7BB -> 7BB 的噪音行。"""
    check(apply_offsets(_open_card(7.0), _ante_sit(0.5)) == (),
          "没有实际改动就不该有偏移行")


def test_size_between_options_lands_on_a_legal_option() -> None:
    """④ 落在区间内但选不到的尺寸（7.2BB）也要移到最近合法档。"""
    sit = _ante_sit(0.5)
    deltas = apply_offsets(_open_card(7.2), sit)
    check(len(deltas) == 1, f"区间内非选项金额应产生偏移：{deltas}")
    if deltas:
        check(deltas[0].final == "7BB", f"应落到最近合法档 7BB：{deltas[0]}")
        check(7.0 in {o.amount for o in sit.options if o.kind == "raise"},
              "最终值必须真的出现在 options 里")


def test_table_flags_fall_back_to_table_dynamics() -> None:
    """桌风没有画像数字时，和 Situation 一样退回 .table_dy 的结论。"""
    card = Card(
        id="flop.demo.freq", book="Harrington", volume=1, chapter="demo",
        pages=(3,), street="flop", spot=Spot(street="flop"),
        plan=Plan(action="bet", size=Size("pct_pot", 0.5), freq=1.0),
        why="演示卡：翻牌下注半池", confidence="interpreted",
    )
    sit = SimpleNamespace(
        snap=_snapshot(ante=None, street="flop", pot=10.0), street="flop",
        options=[], table_stats=None, table_dynamics="松弱桌，跟注站",
    )
    rows = [_rule(id="flop.freq", when={"table_loose": True},
                  target="freq", op="set", value=0.8,
                  reason="松桌降低频率", source="test")]
    with tempfile.TemporaryDirectory() as tmp:
        rules = load_offset_rules(_write_rules(Path(tmp), rows))
    deltas = apply_offsets(card, sit, rules)
    check(len(deltas) == 1 and deltas[0].target == "freq",
          f"画像缺失时应读 .table_dy：{deltas}")


def test_offset_policy_none_disables_the_layer() -> None:
    card = replace(_open_card(3.0), offset_policy="none")
    check(apply_offsets(card, _ante_sit(0.5)) == (),
          "offset_policy=none 的卡不许被偏移")


# ─────────────────────────── 只改尺寸 / 频率 ───────────────────────────

def test_size_pct_and_freq_rules_change_only_those_fields() -> None:
    """⑤ size_pct / freq 规则生效；action 与 size.kind 不被改写。"""
    card = Card(
        id="flop.demo.bet", book="Harrington", volume=1, chapter="demo",
        pages=(2,), street="flop",
        spot=Spot(street="flop"), plan=Plan(
            action="bet", size=Size("pct_pot", 0.5), freq=1.0),
        why="演示卡：翻牌下注半池", confidence="interpreted",
    )
    snap = _snapshot(ante=None, street="flop", pot=10.0)
    sit = SimpleNamespace(snap=snap, street="flop", options=[],
                          table_stats=None)
    rows = [
        _rule(id="flop.size", when={"street": "flop"}, target="size_pct",
              op="add", value=0.1, reason="湿润面加一档", source="test"),
        _rule(id="flop.freq", when={"street": "flop"}, target="freq",
              op="set", value=0.8, reason="紧桌降低频率", source="test"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        rules = load_offset_rules(_write_rules(Path(tmp), rows))
    deltas = apply_offsets(card, sit, rules)
    by_target = {d.target: d for d in deltas}
    check(by_target.get("size_pct") is not None, f"缺 size_pct 偏移：{deltas}")
    check(by_target.get("freq") is not None, f"缺 freq 偏移：{deltas}")
    if by_target.get("size_pct"):
        d = by_target["size_pct"]
        check((d.baseline, d.final) == ("0.5 池", "0.6 池"),
              f"size_pct 口径不对：{d}")
    if by_target.get("freq"):
        d = by_target["freq"]
        check((d.baseline, d.final) == ("100%", "80%"),
              f"freq 口径不对：{d}")
    check(card.plan.action == "bet", "偏移不许改动作")
    check(card.plan.size.kind == "pct_pot", "偏移不许改尺寸类型")
    check(all(d.target in OFFSET_TARGETS for d in deltas),
          f"出现未知 target：{deltas}")


# ─────────────────────────── 真实提示词链路 ───────────────────────────

def test_prompt_shows_book_plan_then_offset_then_legal_options() -> None:
    """②④ 书计划 -> 偏移 -> 最终尺寸三段都在，options 金额原样。"""
    card = _open_card(3.0)
    with patch.object(book_prompt, "_all_cards", return_value=(card,)):
        sit = _ante_sit(0.5)
        offsets = plan_offsets(sit)
        system, user = build_messages(sit)
    check(bool(offsets), "build_situation 没有把偏移注入 Situation")
    check(user.count("## 书中计划") == 1, "书计划段应恰好一次")
    check(user.count("## 偏移") == 1, "偏移段应恰好一次")
    book = user.split("## 书中计划", 1)[1].split("\n## ", 1)[0]
    offset = user.split("## 偏移", 1)[1].split("\n## ", 1)[0]
    check("计划: 加注 3BB" in book, f"书计划应保留 3BB：{book}")
    check("size_bb：3BB -> 7BB" in offset, f"偏移行不对：{offset}")
    check("适用：" in offset and "依据：" in offset,
          f"偏移行必须写清适用桌况与依据：{offset}")
    check(user.index("## 书中计划") < user.index("## 偏移")
          < user.index("## 可选动作"),
          "顺序必须是书计划 -> 偏移 -> options")
    options = user.split("## 可选动作", 1)[1]
    for amount in ("7BB", "7.5BB", "8BB"):
        check(amount in options, f"options 的合法金额被覆盖：缺 {amount}")


def test_prompt_without_matching_table_keeps_offset_section_out() -> None:
    """③ 真实卡库没有翻前卡：0.5BB 前注也不该伪造偏移。"""
    _, user = build_messages(_ante_sit(0.5))
    check("## 偏移" not in user, "没有书卡命中时不该出现偏移段")
    check("书中无此局面" in user, "零匹配应写明书中无此局面")


def test_offset_layer_failure_fails_open() -> None:
    """偏移层异常不能阻塞主循环：Situation 仍然返回，偏移为空。"""
    import poker_ocr.playbook.offset as offset_mod
    with patch.object(offset_mod, "offsets_for_situation",
                      side_effect=RuntimeError("bad rule")):
        sit = _ante_sit(0.5)
    check(plan_offsets(sit) == (), "异常时偏移必须为空")
    check(sit.options, "异常时 options 不能被砍")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        before = len(FAILS)
        try:
            test()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{test.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {test.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print("\n✅ 桌况偏移层全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
