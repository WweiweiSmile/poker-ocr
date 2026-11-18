#!/usr/bin/env python3
"""摊牌证据行渲染（#15 §14.4 预算 / §15.1 四态 / §15.2 禁词）。

    nv tests/test_witness_render.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.witness import admission as A  # noqa: E402
from poker_ocr.witness import render as R  # noqa: E402

FAILS: list[str] = []
PREFIX = "摊牌证据："


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def row(**over):
    base = {
        "player": "p1",
        "hand": "20261101-abcdef1234",
        "seat": "s1",
        "ts": "2026-11-01T20:00:00+08:00",
        "street": "river",
        "action": "check",
        "position": "ip",
        "cards": ["7s", "Ad"],
        "board": ["6c", "2h", "As", "5c", "9d"],
        "family": "weak_ace_top_pair",
        "show_kind": "showdown",
        "status": A.STATUS_ADMITTED,
        "winner": {"status": "unknown", "seat": None, "family": None},
        "source": "outbox",
        "table": "20261101-deadbeefcafe",
    }
    base.update(over)
    return base


def rows_for(player: str, family: str, n: int, seat: str = "s1", ts0: int = 0, **over):
    out = []
    for i in range(n):
        out.append(row(player=player, seat=seat, family=family,
                       ts=f"2026-11-{1 + (ts0 + i) // 24:02d}T{(ts0 + i) % 24:02d}:00:00+08:00",
                       **over))
    return out


# ------------------------------------------------------------------ 四态


def test_appendix_a_single_line() -> None:
    path = ROOT / "data/hand_outbox/sent/20261030-1e5ec30022.json"
    if not path.exists():
        return
    hand = json.loads(path.read_text(encoding="utf-8"))
    rows = list(A.iter_rows(hand))
    seats = {"upL": "书麻头了wlr"}
    got = R.render_witness_lines(rows, seats=seats, hero_seat="hero")
    check(got == {"upL": ("摊牌证据：他在河牌IP过牌时亮过弱A顶对（1 次）",)},
          f"附录 A 单行逐字一致：{got}")


def test_empty_rows() -> None:
    check(R.render_witness_lines([], seats={"s1": "p1"}) == {}, "0 条 → 空 mapping")
    check(R.render_witness_lines(None, seats={"s1": "p1"}) == {}, "None → 空 mapping")
    check(R.render_witness_lines([row()], seats={}) == {}, "没有座位 → 空 mapping")


def test_version_mismatch() -> None:
    data = [row()]
    check(R.render_witness_lines(data, seats={"s1": "p1"}, version="witness-2025-01-01") == {},
          "数据版本失配 → 空 mapping")
    check(R.render_witness_lines({"version": "witness-2025-01-01", "rows": data},
                                 seats={"s1": "p1"}) == {}, "包装版本失配 → 空 mapping")
    check(R.render_witness_lines({"version": A.WITNESS_SCHEMA, "rows": data},
                                 seats={"s1": "p1"}) != {}, "包装版本一致 → 正常渲染")


def test_nine_seats_budget() -> None:
    seats = {f"s{i}": f"p{i}" for i in range(1, 10)}
    rows = []
    for i in range(1, 10):
        rows += rows_for(f"p{i}", "weak_ace_top_pair", 3, seat=f"s{i}", ts0=i * 10)
        rows += rows_for(f"p{i}", "two_pair_plus", 2, seat=f"s{i}", ts0=i * 10 + 5)
    got = R.render_witness_lines(rows, seats=seats, max_chars=600)
    check(len(got) == 9, f"600 下 9 个座位都出：{len(got)}")
    check(all(len(lines) == 2 for lines in got.values()), "每座位 2 条")
    total = sum(len(line) + 1 for lines in got.values() for line in lines)
    check(total <= 600, f"总预算 ≤600：{total}")
    check(list(got) == list(seats), "座位按入参顺序（近→远）")
    check(all(line.startswith(PREFIX) for lines in got.values() for line in lines),
          "每行都带前缀")

    # §15.1 的「9 人×2 条被截断」按当前模板算术不可能发生：最长行 32 + 1，18 行 ≤594。
    longest = max(len(line) for lines in got.values() for line in lines)
    check(longest * 1 + 18 * 1 <= 600, f"模板最长行 {longest}，18 行仍在 600 内")

    cost = {seat: sum(len(line) + 1 for line in lines) for seat, lines in got.items()}
    first_three = sum(cost[f"s{i}"] for i in (1, 2, 3))
    line1_4 = len(got["s4"][0]) + 1

    tight = R.render_witness_lines(rows, seats=seats, max_chars=first_three + line1_4 - 1)
    check(list(tight) == ["s1", "s2", "s3"], f"预算不够 s4 第一条 → 只出前 3 座：{list(tight)}")

    half = R.render_witness_lines(rows, seats=seats, max_chars=first_three + line1_4)
    check(list(half) == ["s1", "s2", "s3", "s4"], f"只够 s4 第一条就停：{list(half)}")
    check(len(half["s4"]) == 1 and "（3 次）" in half["s4"][0],
          "同座位第 2 条超限 → 只留 N 大的第 1 条")


def test_per_seat_limit_and_group_dedupe() -> None:
    rows = rows_for("p1", "strong_ace_top_pair", 5)
    rows += rows_for("p1", "non_ace_top_pair", 3, ts0=10)   # 同组 strong_top_pair
    got = R.render_witness_lines(rows, seats={"s1": "p1"})
    check(len(got["s1"]) == 1, f"同组只出 1 条：{got}")
    check("（5 次）" in got["s1"][0], "组内留 N 大的那条")

    rows += rows_for("p1", "two_pair_plus", 2, ts0=20)
    got = R.render_witness_lines(rows, seats={"s1": "p1"})
    check(len(got["s1"]) == 2, f"跨组补到 2 条：{got}")

    # 弱A顶对自成一族（#12 的 6 组 = 弱A顶对 + 强顶对 + 对子中坚 + 小对/底对 + 听牌/高张 + 两对+）
    weak = rows_for("p1", "weak_ace_top_pair", 1, ts0=40)
    got = R.render_witness_lines(rows + weak, seats={"s1": "p1"})
    check(len(got["s1"]) == 2, f"弱A顶对与强顶对不同组，但仍受 2 条上限：{got}")

    rows += rows_for("p1", "set", 9, ts0=30)                # 与 two_pair_plus 同组
    got = R.render_witness_lines(rows, seats={"s1": "p1"})
    check(len(got["s1"]) == 2, f"每座位上限 2：{got}")


def test_recent_spot_first() -> None:
    old = rows_for("p1", "weak_ace_top_pair", 1, ts0=0)
    new_rows = rows_for("p1", "two_pair_plus", 1, ts0=100,
                        street="turn", action="bet", position="oop")
    got = R.render_witness_lines(old + new_rows, seats={"s1": "p1"})
    check(got["s1"][0].find("转牌") > 0, f"spot 之间按最近出现倒序：{got}")


def test_pool_layer() -> None:
    seats = {"s1": "p1"}
    nineteen = []
    for i in range(19):
        nineteen += rows_for("someone", "weak_ace_top_pair", 1, seat="s9", ts0=i)
    check(R.render_witness_lines(nineteen, seats=seats) == {}, "池级 n=19 不出比例线")

    twenty = nineteen + rows_for("someone", "weak_ace_top_pair", 1, seat="s9", ts0=19)
    got = R.render_witness_lines(twenty, seats=seats)
    pool = got.get(R.POOL_KEY, ())
    check(len(pool) == 1, f"池级 n=20 出比例线：{got}")
    check("100%" in pool[0] and "20 次" in pool[0], f"比例与次数：{got}")
    check("s1" not in got, f"池级句不挂到没有个人证据的座位：{got}")

    mixed = twenty + []
    for i in range(20):
        mixed += rows_for("someone", "two_pair_plus", 1, seat="s9", ts0=100 + i)
    got = R.render_witness_lines(mixed, seats=seats)
    pool = got.get(R.POOL_KEY, ())
    check(len(pool) == 2, f"池级最多 2 条：{got}")
    check(all("50%" in line for line in pool), f"各 50%：{got}")


def test_invalid_rows_skipped() -> None:
    bad = [
        row(status=A.STATUS_TAGGED),                 # 主动秀不进提示词
        row(family="not_a_family"),
        row(cards=["7s"]),
        row(street="flop"),
        row(ts=""),
        row(player=""),
        row(action="fold"),
        row(position="unknown"),
    ]
    check(R.render_witness_lines(bad, seats={"s1": "p1"}) == {}, "坏行全跳过 → 空")
    got = R.render_witness_lines(bad + [row()], seats={"s1": "p1"})
    check(len(got.get("s1", ())) == 1, f"坏行不影响好行：{got}")


def test_hero_seat_skipped() -> None:
    rows = rows_for("p1", "weak_ace_top_pair", 1, seat="s1")
    got = R.render_witness_lines(rows, seats={"s1": "p1", "s2": "p2"}, hero_seat="s1")
    check("s1" not in got, f"hero 座位不渲染：{got}")


def test_forbidden_words_absent() -> None:
    seats = {"s1": "p1"}
    rows = rows_for("p1", "weak_ace_top_pair", 3)
    rows += rows_for("p1", "two_pair_plus", 2, ts0=10)
    rows += rows_for("p1", "busted_draw", 1, ts0=20)
    personal = R.render_witness_lines(rows, seats=seats)
    pool_rows = []
    for i in range(30):
        pool_rows += rows_for("someone", "air_high_card", 1, seat="s9", ts0=i)
    pool = R.render_witness_lines(pool_rows, seats=seats)
    lines = list(personal.get("s1", ())) + list(pool.get(R.POOL_KEY, ()))
    check(len(lines) >= 2, "禁词断言需要至少两行样本文本")
    for word in R.forbidden_words():
        check(all(word not in line for line in lines), f"禁词「{word}」不得出现在证据行")


# ------------------------------------------------------------------ 词表


def test_phrasing_file() -> None:
    table = R.load_phrasing()
    check(table["version"] == "witness-phrasing-2026-11-14a", "词表版本")
    check(table["prefix"] == PREFIX, "前缀")
    check(set(table["family"]) == set(A.FAMILIES), "12 族齐全")
    check(len(table["forbidden"]) == 19, f"禁词 19 个：{len(table['forbidden'])}")
    for key in ("street", "position", "action"):
        check(isinstance(table[key], dict) and table[key], f"词表 {key} 段存在")
    check("{family}" in table["line"] and "{count}" in table["line"], "个人模板占位符")


def test_phrasing_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        got = R.load_phrasing(broken)
        check(got["prefix"] == PREFIX and got["family"]["set"] == "暗三", "坏 JSON 回退默认值")

        missing = Path(tmp) / "missing.json"
        missing.write_text(json.dumps({"version": "x", "prefix": PREFIX}), encoding="utf-8")
        got = R.load_phrasing(missing)
        check(set(got["family"]) == set(A.FAMILIES), "缺字段也回退默认值")

        absent = Path(tmp) / "nope.json"
        check(R.load_phrasing(absent)["line"] == R.DEFAULT_PHRASING["line"],
              "文件不存在回退默认值")

    # 注入坏词表时不出行（不允许无模板静默出行）。
    got = R.render_witness_lines([row()], seats={"s1": "p1"}, phrasing={"prefix": PREFIX})
    check(got == {}, "词表不可用 → 空 mapping")


# --------------------------------------------------- §15.5 ground_truth 三条


def _snapshot():
    from poker_ocr.hand_track import (
        ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
    )
    from poker_ocr.parse import Blinds

    return HandSnapshot(
        hand_id="witness-golden", hand_index=0, street="flop",
        board=("Ks", "7d", "2c"), hero_cards=("Kh", "Qh"),
        hero_position="BTN", hero_stack=92.0, hero_bet=0.0,
        hero_invested=8.0, to_call=4.0, second_bet=None,
        pot=12.5, pot_before_street=8.0, table_size=6,
        blinds=Blinds(0.5, 1.0, None, "0.5/1.0"),
        opponents=(
            OpponentSnapshot("CO", "Villain", "CO", 100.0, 4.0, False),
            OpponentSnapshot("BB", None, "BB", 100.0, 1.0, False),
        ),
        streets=(
            StreetRecord("preflop", 1.5, (
                ActionRecord("preflop", "hero", None, "BTN", "raise", 8.0, False, True),
                ActionRecord("preflop", "BB", None, "BB", "call", 8.0, False, False),
            )),
            StreetRecord("flop", 8.0, (
                ActionRecord("flop", "CO", "Villain", "CO", "bet", 4.0, False, False),
            )),
        ),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("witness", "flop", 0, 12.5, 4.0), warnings=(),
        bomb_pot=False, players_dealt=6,
    )


def test_priority_table_puts_hero_strength_above_witness() -> None:
    """a) 事实优先级：Hero 牌力（第 2 层）在 3a 之前，3a 也在画像/池子之前。"""
    from poker_ocr.prompt.system.priority import FACTS_PRIORITY as P

    i1 = P.index("1. 硬事实")
    i2 = P.index("2. 确定性计算")
    i3a = P.index("3a. 逐座位摊牌证据")
    i3b = P.index("3b. ")
    i3c = P.index("3c. 池子先验")
    i4 = P.index("4. 现场观察")
    check(i1 < i2 < i3a < i3b < i3c < i4, f"优先级序号应递增：{P}")
    check("3a 不压 Hero 牌力事实（第 2 层）" in P,
          "必须写明摊牌证据不压 Hero 牌力事实")
    check("摊牌证据 > 逐座位画像 > 本桌 > 全场先验" in P,
          "同层「更具体」口径要含摊牌证据")


def test_weak_family_line_has_no_certainty_words() -> None:
    """b) 弱族行出现，且不带「必然/一定」这类确定性断言。"""
    rows = rows_for("p1", "weak_ace_top_pair", 2)
    got = R.render_witness_lines(rows, seats={"s1": "p1"})
    text = got.get("s1", [""])[0]
    check(text.startswith(PREFIX) and "弱A顶对" in text, f"应渲染弱族行：{text}")
    check("2 次" in text, f"次数必须写出来：{text}")
    for word in ("必然", "一定", "肯定", "铁定"):
        check(word not in text, f"弱族行不应带确定性词「{word}」：{text}")


def test_version_mismatch_disappears_byte_identically() -> None:
    """c) 版本失配 → 整段消失，且与无 witness 的 prompt 逐字节相同。"""
    from poker_ocr.advice import build_situation
    from poker_ocr.prompt.user import render_facts_zh

    snap = _snapshot()
    bad = R.render_witness_lines(
        {"version": "witness-1999-01-01a",
         "rows": rows_for("p1", "weak_ace_top_pair", 3)},
        seats={"s1": "p1"})
    check(bad == {}, f"版本失配应为空 mapping：{bad}")
    base = render_facts_zh(build_situation(snap, table_dynamics=""))
    same = render_facts_zh(build_situation(
        snap, table_dynamics="", witness_by_seat=bad))
    check(same == base, "失配后 prompt 应与无 witness 逐字节相同")
    check("摊牌证据" not in base, "无数据时不应出现摊牌证据")


def test_env_flag_disables_rendering() -> None:
    """WITNESS_PROMPT=0 时整段关闭（§14.3 A/B 开关）。"""
    import os

    old = os.environ.get("WITNESS_PROMPT")
    try:
        os.environ["WITNESS_PROMPT"] = "0"
        check(R.enabled() is False, "WITNESS_PROMPT=0 应关闭")
        os.environ["WITNESS_PROMPT"] = "1"
        check(R.enabled() is True, "WITNESS_PROMPT=1 应开启")
        os.environ.pop("WITNESS_PROMPT")
        check(R.enabled() is True, "缺省应开启")
    finally:
        if old is None:
            os.environ.pop("WITNESS_PROMPT", None)
        else:
            os.environ["WITNESS_PROMPT"] = old


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)
             and getattr(value, "__module__", "") == __name__]
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
    print("\n✅ 摊牌证据渲染全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
