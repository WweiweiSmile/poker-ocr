#!/usr/bin/env python3
"""tools/hand_leak_report.py 的单测：净值口径、补码识别、炸弹池判定、牌力。

    .venv/bin/python tests/test_hand_leak_report.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import hand_leak_report as report  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def hand(hand_id: str, *, stack: float | None = 0.0, pot: float = 0.0,
         hero=("Ah", "Kd"), board=(), pre_actions=(), all_ins=(),
         bomb: bool = False) -> dict:
    streets = [{"street": "preflop", "pot_start": 4.0,
                "actions": list(pre_actions)},
               {"street": "flop", "pot_start": 10.0, "actions": []}]
    return {
        "type": "hand", "ts": "2026-01-01T00:00:00+08:00", "hand_id": hand_id,
        "hero_cards": list(hero), "hero_position": "BTN",
        "board": list(board), "pot": pot, "streets": streets,
        "hero_stack_bb": stack, "all_ins": list(all_ins),
        "bomb_pot": bomb, "players_dealt": 9,
    }


def write_log(records: list[dict]) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "live.states.jsonl"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_start",
                             "started_at": "2026-01-01T19:00:00+08:00"}) + "\n")
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return tmp


def test_nets():
    # 赢的比自带筹码多：合法（对手的钱也在池里），必须算进净值
    records = [
        hand("h1", stack=90.0, pot=220.0),
        hand("h2", stack=215.0, pot=10.0),
        hand("h3", stack=210.0, pot=10.0),
    ]
    rows = report.build(report.load(write_log(records))[0])
    nets, odd = report.attach_nets(rows)
    check(nets.get(0) == 125.0, f"大胜应计入净值，实际 {nets.get(0)}")
    check(nets.get(1) == -5.0, f"小输应计入净值，实际 {nets.get(1)}")
    check(not any(o["index"] == 0 for o in odd), "合法大胜不该进异常表")


def test_topup():
    # 赢了超过底池、且这手没有全下 -> 补码，不能算成赢
    records = [
        hand("h1", stack=100.0, pot=6.0),
        hand("h2", stack=200.0, pot=6.0),
        hand("h3", stack=199.0, pot=6.0),
    ]
    rows = report.build(report.load(write_log(records))[0])
    nets, odd = report.attach_nets(rows)
    check(0 not in nets, "补码不该进净值")
    check(any(o["kind"] == "topup" and o["delta"] == 100.0 for o in odd),
          f"补码应被标出来，实际 {odd}")


def test_loss_capped_by_stack():
    records = [
        hand("h1", stack=50.0, pot=10.0),
        hand("h2", stack=10.0, pot=10.0),
        hand("h3", stack=9.0, pot=10.0),
    ]
    rows = report.build(report.load(write_log(records))[0])
    nets, _ = report.attach_nets(rows)
    # -40 没超过 50，是合法输
    check(nets.get(0) == -40.0, f"应计入 -40，实际 {nets.get(0)}")


def test_bomb():
    bomb = hand("b1", pre_actions=[])
    bomb["streets"][0]["pot_start"] = 45.0
    check(report.is_bomb(bomb), "45bb 无行动前注池应判炸弹池")
    raised = hand("b2", pre_actions=[
        {"seat_id": "lowL", "action": "call", "amount": 1.0}])
    raised["streets"][0]["pot_start"] = 45.0
    check(not report.is_bomb(raised), "有翻前行动就不是炸弹池")
    normal = hand("b3")
    normal["streets"][0]["pot_start"] = 4.0
    check(not report.is_bomb(normal), "普通池不该判成炸弹池")


def test_best_hand():
    cat = report.best_hand(report.cards(["9d", "Kd", "Ks", "Kc", "Qs", "5s", "5h"]))[0]
    check(cat == 6, f"K9 在 KKQ55 应是葫芦，实际 {report.CAT.get(cat)}")
    cat = report.best_hand(report.cards(["Kh", "Qc", "Ks", "Kc", "Qs", "5s", "5h"]))[0]
    check(cat == 6, "KQ 在 KKQ55 应是葫芦")
    # 同花 > 顺子 > 三条
    check(report.best_hand(report.cards(["As", "2s", "Ks", "Qs", "Js", "3s", "4d"]))[0] == 5,
          "A 高同花应是同花（不是同花顺）")
    check(report.best_hand(report.cards(["9s", "8s", "7s", "6s", "5s", "2d", "3c"]))[0] == 8,
          "9 高同花顺应是同花顺")
    check(report.best_hand(report.cards(["9c", "8c", "7d", "6h", "5s", "2d", "3s"]))[0] == 4,
          "9 高顺子应判顺子")


def test_string_cards():
    # 日志里会出现 "10h" 这种写法
    check(report.cards(["10h", "3C"]) == [(10, "h"), (3, "c")],
          f"10 应解析成 T，实际 {report.cards(['10h', '3C'])}")


def test_summary_shape():
    records = [
        hand("h1", stack=100.0, pot=10.0, hero=("Ah", "Ad")),
        hand("h2", stack=110.0, pot=10.0, hero=("7c", "2d")),
    ]
    hands, starts = report.load(write_log(records))
    rows = report.build(hands)
    nets, odd = report.attach_nets(rows)
    rep = report.summarize(rows, nets, odd, starts)
    check(rep["hands"] == 2, "手数")
    check(rep["net"] == 10.0, f"净额，实际 {rep['net']}")
    check("by_position" in rep and "BTN" in rep["by_position"], "按位置分组")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILS:
        for f in FAILS:
            print(f"FAIL: {f}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
