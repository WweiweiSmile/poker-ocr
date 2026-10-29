#!/usr/bin/env python3
"""GTO 查询层测试：牌面同构映射、节点解析、手牌映射、插值、渲染、保守返回 None。

    .venv/bin/python tests/test_gto_lookup.py

用临时 SQLite + 临时 specs，不依赖 gto/data 里真实跑出来的表。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import board_equity, gto_lookup  # noqa: E402
from poker_ocr.advice import build_situation  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


BOARD = ("Ks", "7d", "2c")
CANON, PERM = gto_lookup._canonical_flop(*BOARD)
HERO = ("Ah", "Kd")
HERO_COMBO = gto_lookup._canon_hand(HERO, PERM)
assert HERO_COMBO is not None

SOLVE_ID = 1
JOB_ID = f"SRP_BTN_vs_BB_100bb_app__{CANON}__three_span"


def write_specs(d: Path) -> None:
    (d / "boards.json").write_text(json.dumps({
        "version": 1, "k": 1,
        "boards": [{"class_id": 0, "flop": CANON, "size": 1755, "features": []}],
    }), encoding="utf-8")
    (d / "board_map.json").write_text(json.dumps({"version": 1, "map": {CANON: 0}}),
                                      encoding="utf-8")


def write_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE solves (solve_id INTEGER PRIMARY KEY, job_id TEXT, flop TEXT,
      config_hash TEXT, starting_pot INTEGER, effective_stack INTEGER, iterations INTEGER,
      exploitability REAL, n_hands INTEGER, n_nodes INTEGER, engine_version TEXT,
      runner_version TEXT, created_at INTEGER, source_host TEXT, elapsed_secs REAL);
    CREATE TABLE hands (solve_id INTEGER, idx INTEGER, combo TEXT, w_oop REAL, w_ip REAL);
    CREATE TABLE nodes (solve_id INTEGER, street TEXT, turn TEXT, key TEXT, actor INTEGER,
      pot INTEGER, actions TEXT, range_mix TEXT, strategy BLOB, ev BLOB);
    """)
    conn.execute(
        "INSERT INTO solves VALUES (1,?,?, 'h',1250,9400,250,1.2,3,2,'e','r',0,'t',1.0)",
        (JOB_ID, CANON),
    )
    hands = [HERO_COMBO, "QsQh", "9h9d"]
    conn.executemany(
        "INSERT INTO hands VALUES (1,?,?,?,?)",
        [(i, h, 1.0, 1.0) for i, h in enumerate(hands)],
    )

    def add(key, actor, actions, strat_rows, ev_rows, mix):
        strat = bytes(v for row in strat_rows for v in row)
        ev = array("i", [v for row in ev_rows for v in row]).tobytes()
        conn.execute(
            "INSERT INTO nodes VALUES (1,'flop','',?,?,1250,?,?,?,?)",
            (key, actor, json.dumps(actions), json.dumps(mix), strat, ev),
        )

    add("root", 0, [{"id": "check", "amount": 0, "frac": 0},
                    {"id": "bet-33", "amount": 417, "frac": 0.33}],
        [[40, 70, 0], [60, 30, 0]],
        [[600, 500, 0], [700, 650, 0]],
        [0.5, 0.5])
    add("vs_b33", 1, [{"id": "fold", "amount": 0, "frac": 0},
                      {"id": "call", "amount": 417, "frac": 0},
                      {"id": "raise-33", "amount": 1100, "frac": 0.33}],
        [[10, 50, 0], [80, 40, 0], [10, 10, 0]],
        [[0, 0, 0], [650, 600, 0], [700, 650, 0]],
        [0.2, 0.6, 0.2])
    conn.commit()
    conn.close()


def make_sit(board=BOARD, hero=HERO, *, hero_pos="BB", ip=False, actions=(),
             second_opponent=False, to_call=0.0, pot=12.5, street="flop"):
    opponents = [OpponentSnapshot("v", "v", "BTN" if hero_pos == "BB" else "BB",
                                  94.0, 0.0, False)]
    if second_opponent:
        opponents.append(OpponentSnapshot("w", "w", "CO", 94.0, 0.0, False))
    acts = tuple(actions)
    snap = HandSnapshot(
        hand_id="h1", hand_index=1, street=street, board=board, hero_cards=hero,
        hero_position=hero_pos, hero_stack=94.0, hero_bet=0.0, hero_invested=6.0,
        to_call=to_call, second_bet=None, pot=pot, pot_before_street=12.5,
        table_size=6, blinds=None, opponents=tuple(opponents),
        streets=(StreetRecord("preflop", 1.5, ()),
                 StreetRecord("flop", 12.5, acts)),
        preflop_shapes=("single-raise",), hero_is_pfr=(hero_pos == "BTN"),
        cbet_spot=True, decision_key=("h1", street, 0, pot, to_call, hero),
    )
    return build_situation(snap, include_advanced=True)


def test_canonical_roundtrip() -> None:
    text, perm = gto_lookup._canonical_flop("Ks", "7d", "2c")
    check(text == CANON, f"canonical 不一致：{text} != {CANON}")
    check(gto_lookup._canon_hand(("Ah", "Kd"), perm) == HERO_COMBO,
          "手牌映射不一致")
    # 交换 s/d：同一个同构类
    text2, perm2 = gto_lookup._canonical_flop("Kd", "7s", "2c")
    check(text2 == CANON, f"同构翻牌应同 canonical：{text2} != {CANON}")
    check(gto_lookup._canon_hand(("Ah", "Ks"), perm2) == HERO_COMBO,
          "同构后的手牌应映射到同一 combo")


def test_root_oop() -> None:
    sit = make_sit(hero_pos="BB", ip=False)
    g = sit.gto
    check(g is not None, "BB 无行动应命中 root")
    if g is None:
        return
    check(g.node == "root", f"node 应为 root：{g.node}")
    check(g.hand is not None and g.hand.in_range, "AhKd 在范围里")
    if g.hand:
        check(g.hand.freqs.get("call") == 40, f"check 频率应 40：{g.hand.freqs}")
        check(g.hand.freqs.get("raise-1/3") == 60, f"1/3 频率应 60：{g.hand.freqs}")
        check(abs(g.hand.ev.get("raise-1/3", 0) - 7.0) < 1e-6, f"EV 应 7.0：{g.hand.ev}")
        check(g.hand.best == "raise-1/3", f"best 应 1/3：{g.hand.best}")
    txt = g.render()
    check("GTO baseline" in txt and "1/3 60%" in txt, f"渲染缺内容：{txt[:160]}")
    check("EXACT" in txt, "代表牌面应标 EXACT")


def test_facing_bet_ip() -> None:
    bet = ActionRecord("flop", "v", "v", "BB", "bet", 4.17)
    sit = make_sit(hero_pos="BTN", ip=True, actions=(bet,), to_call=4.17, pot=16.67)
    g = sit.gto
    check(g is not None, "BTN 面对 1/3 下注应命中 vs_b33")
    if g is None:
        return
    check(g.node == "vs_b33", f"node 应为 vs_b33：{g.node}")
    if g.hand:
        check(g.hand.freqs.get("fold") == 10, f"fold 应 10：{g.hand.freqs}")
        check(g.hand.freqs.get("call") == 80, f"call 应 80：{g.hand.freqs}")
        check(g.hand.freqs.get("raise-1/3") == 10, f"raise 应 10：{g.hand.freqs}")


def test_out_of_range() -> None:
    sit = make_sit(hero=("5h", "4h"))
    g = sit.gto
    check(g is not None, "范围外的手牌仍应返回 range mix")
    if g is not None:
        check(g.hand is not None and not g.hand.in_range, f"应标 in_range=False：{g.hand}")
        check("OUTSIDE" in g.render(), "渲染应说明范围外")


def test_multiway_none() -> None:
    sit = make_sit(second_opponent=True)
    check(sit.gto is None, "多人池不应查表")


def test_wrong_spr_none() -> None:
    sit = make_sit(pot=40.0)  # SPR 2.35，超出 100BB 档
    check(sit.gto is None, "SPR 不在档位内应返回 None")


def test_interpolation() -> None:
    evs = {"raise-1/3": 5.0, "raise-2/3": 4.0, "raise-1.2": 2.0}
    got = gto_lookup._interpolate_ev({"33": 1 / 3, "67": 2 / 3, "120": 1.2}, evs)
    check(abs(got["raise-1/2"] - 4.5) < 1e-6, f"1/2 插值应 4.5：{got}")
    check(4.0 > got["raise-1"] > 2.0, f"1.0 插值应在 2-4 之间：{got}")
    # 只有一端有解时不外推
    got2 = gto_lookup._interpolate_ev({"33": 1 / 3, "67": 2 / 3}, {"raise-1/3": 5.0, "raise-2/3": 4.0})
    check("raise-1.2" not in got2 and "raise-1" not in got2, f"不应外推：{got2}")


def test_board_equity_multiway() -> None:
    # 用仓库真实 specs（184 类 + 7 spot），但只抽 120 次，测试要快
    os.environ["GTO_SPECS_DIR"] = str(ROOT / "gto" / "specs")
    os.environ["GTO_EQUITY_SAMPLES"] = "120"
    board_equity.reset_cache()
    sit = make_sit(hero_pos="BTN", ip=True, second_opponent=True)
    be = sit.board_eq
    check(be is not None, "3 人池也应给牌面权益")
    if be is None:
        return
    check(set(be.eq) == {1, 2, 3}, f"应有 1/2/3 人权益：{be.eq}")
    check(be.eq[1] > be.eq[2] > be.eq[3], f"人数越多权益应越低：{be.eq}")
    txt = be.render()
    check("Board structure" in txt and "Ks7d2c" in txt, f"渲染缺内容：{txt[:160]}")
    check(sit.gto is None, "多人池不应命中单挑 solver 表")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gto-test-"))
    write_specs(tmp)
    write_db(tmp / "t.sqlite")
    os.environ["GTO_TABLE_PATH"] = str(tmp / "t.sqlite")
    os.environ["GTO_SPECS_DIR"] = str(tmp)
    os.environ["GTO_ENABLED"] = "1"
    gto_lookup.reset_cache()

    tests = [
        test_canonical_roundtrip,
        test_root_oop,
        test_facing_bet_ip,
        test_out_of_range,
        test_multiway_none,
        test_wrong_spr_none,
        test_interpolation,
        test_board_equity_multiway,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ GTO 查询层全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
