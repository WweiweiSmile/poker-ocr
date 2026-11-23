#!/usr/bin/env python3
"""9 人权益的 2 秒时间预算：超预算只降抽样，对手数一个不减（ticket #117）。

    nv tests/test_equity_budget.py

锁定五件事：

  1. 预算人为调低时（`HAND_VS_RANGES_BUDGET_MS=1`）会降采样：结果仍非空、
     出现含「降采样」的中文事实 warning、`samples < samples_requested`；
  2. 降采样不减少对手：逐家范围 + 随机补位仍然等于 9；
  3. 预算充足（或环境变量不设）时**逐位不变**：不出现降采样 warning，
     equity / behind_any 与不设环境变量的一次运行完全相同；
  4. 降采样只影响抽样次数与 warning，不影响选项集；warning 在
     `sit.warnings` / 提示词 user 段 / advice facts 三处都可见；
  5. 独立基准 `tools/equity_benchmark.py` 可手动跑：9 家全进抽样，
     报告 p90 与每次耗时，不设阈值、不抛断言。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import hand_vs_ranges, ranges  # noqa: E402
from poker_ocr.advice import AISettings, advise  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.prompt.user import build_messages  # noqa: E402
from poker_ocr.situation import build_situation  # noqa: E402

FAILS: list[str] = []

BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")
SETTINGS = AISettings(api_key="k", base_url="https://example.invalid", model="m")
BUDGET_ENV = "HAND_VS_RANGES_BUDGET_MS"
# 9 家局面的两个有范围对手（与 test_equity_all_opponents 同构）。
POSITIONS = ("BB", "CO")


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


@contextmanager
def budget(value):
    """临时设/清预算环境变量，并清掉模块级 MC 缓存。"""
    old = os.environ.get(BUDGET_ENV)
    try:
        if value is None:
            os.environ.pop(BUDGET_ENV, None)
        else:
            os.environ[BUDGET_ENV] = str(value)
        hand_vs_ranges._CACHE.clear()
        yield
    finally:
        if old is None:
            os.environ.pop(BUDGET_ENV, None)
        else:
            os.environ[BUDGET_ENV] = old
        hand_vs_ranges._CACHE.clear()


def opp(seat: str, pos: str | None = None, *,
        stack: float | None = 97.0) -> OpponentSnapshot:
    return OpponentSnapshot(seat, seat, pos, stack, 0.0, False)


def snap(*, opponents, hero_cards=("Ah", "Qh"), board=("As", "Ks", "4d"),
         street="flop", hero_pos="BTN", hero_stack=97.0, pot=20.0,
         table_size=9) -> HandSnapshot:
    """最小可用的翻后快照（与 test_field_ranges / test_equity_all_opponents 同构）。"""
    return HandSnapshot(
        hand_id="h1", hand_index=1, street=street, board=tuple(board),
        hero_cards=tuple(hero_cards), hero_position=hero_pos,
        hero_stack=hero_stack, hero_bet=0.0, hero_invested=2.0, to_call=0.0,
        second_bet=None, pot=pot, pot_before_street=pot,
        table_size=table_size, blinds=BLINDS,
        opponents=tuple(opponents),
        streets=(StreetRecord("preflop", 0.0, ()),
                 StreetRecord(street, pot, ())),
        preflop_shapes=("limped",), hero_is_pfr=False, cbet_spot=False,
        decision_key=("h1", street, 0, (), tuple(hero_cards)),
        players_dealt=len(opponents) + 1,
    )


def make_range(rid: str, keys) -> ranges.Range:
    combos = []
    for key in keys:
        combos.extend(ranges.combos_of_key(key))
    built = ranges.make_range(rid, {c: 1.0 for c in combos})
    assert built is not None
    return built


R_A = make_range("A", ("KK", "44", "AKs", "AKo", "KJo"))
R_B = make_range("B", ("QQ", "JJ", "KQs", "KJs", "QJs"))


def nine_opponent_compute():
    """2 家有范围 + 7 家缺证 = 9 个在局对手；走同一次联合重抽。"""
    return hand_vs_ranges.compute(
        ("Ah", "Qh"), ("As", "Ks", "4d"),
        [("a", POSITIONS[0], R_A), ("b", POSITIONS[1], R_B)],
        random_opponents=7, seed_key="h1")


def test_low_budget_downsamples_without_dropping_opponents() -> None:
    """a + c：预算 1ms → 降采样 warning，对手数仍是 9，结果仍可用。"""
    with budget(1):
        low = nine_opponent_compute()
    check(low is not None, "低预算应仍能算出结果（fail open，不是 None）")
    if low is None:
        return
    check(any("降采样" in w for w in low.warnings),
          f"低预算应出现降采样 warning：{list(low.warnings)}")
    check(len(low.opponents) == 2 and low.random_opponents == 7,
          f"降采样不能减对手：{len(low.opponents)}+{low.random_opponents}")
    check(len(low.opponents) + low.random_opponents == 9,
          "逐家 + 补位必须仍是 9")
    check(low.samples < low.samples_requested,
          f"降采样应让实际样本数 < 目标数：{low.samples}/{low.samples_requested}")
    check(0.0 < low.equity < 1.0, f"降采样后权益仍应在 (0,1)：{low.equity}")
    # 同一次联合重抽：9 家的 behind_any 必须严格大于任何单家 behind_i。
    check(low.behind_any > max((e.behind for e in low.opponents), default=0.0),
          f"降采样后 behind_any 退化成逐家近似了：{low.behind_any} vs "
          f"{[e.behind for e in low.opponents]}")


def test_ample_budget_bit_identical_and_no_warning() -> None:
    """b：充足预算 / 不设环境变量 → 无降采样 warning，数值逐位相等。"""
    with budget(60000):
        ample = nine_opponent_compute()
    with budget(None):
        default = nine_opponent_compute()
    check(ample is not None and default is not None, "两次运行都应有结果")
    if ample is None or default is None:
        return
    check(not any("降采样" in w for w in ample.warnings),
          f"充足预算不应降采样：{list(ample.warnings)}")
    check(ample.equity == default.equity,
          f"充足预算 equity 应与默认逐位相等："
          f"{ample.equity} != {default.equity}")
    check(ample.behind_any == default.behind_any,
          f"充足预算 behind_any 应逐位相等："
          f"{ample.behind_any} != {default.behind_any}")
    check(ample.samples == default.samples,
          "充足预算实际样本数应与默认一致")
    check(len(ample.opponents) + ample.random_opponents == 9,
          f"充足预算的对手数也应是 9：{len(ample.opponents)}+"
          f"{ample.random_opponents}")
    with budget(None):
        check(hand_vs_ranges._budget_ms() == 2000,
              f"预算默认值应为 2000ms：{hand_vs_ranges._budget_ms()}")
    with budget("not-a-number"):
        check(hand_vs_ranges._budget_ms() == 2000,
              "预算解析失败应按默认 2000ms，不抛异常")
    with budget(0):
        check(hand_vs_ranges._budget_ms() == 0, "预算 0 表示不限时")


def test_samples_requested_unchanged() -> None:
    """f：不超预算时抽样次数不额外增加。"""
    with budget(60000):
        ample = nine_opponent_compute()
    check(ample is not None, "充足预算应有结果")
    if ample is None:
        return
    expected = hand_vs_ranges._samples_for(9)
    check(expected == 1500, f"9 家默认目标样本数应为 1500：{expected}")
    check(ample.samples_requested == expected,
          f"目标样本数被改了：{ample.samples_requested} != {expected}")
    check(ample.samples <= ample.samples_requested,
          "实际样本数不能超过目标数")


def test_options_unchanged_and_warning_visible_in_three_places() -> None:
    """d：降采样只影响抽样与 warning，不影响选项集；三处可见。"""
    opponents = [opp("o0", POSITIONS[0]), opp("o1", POSITIONS[1])]
    opponents += [opp(f"o{i}", None, stack=None) for i in range(2, 9)]

    with budget(1):
        low = build_situation(snap(opponents=opponents))
    with budget(60000):
        ample = build_situation(snap(opponents=opponents))

    low_ids = [o.id for o in low.options]
    ample_ids = [o.id for o in ample.options]
    check(low_ids == ample_ids,
          f"降采样改动了选项集：{low_ids} != {ample_ids}")
    check(low.hand_ranges is not None, "低预算应有范围权益块")
    if low.hand_ranges is None:
        return
    check(low.hand_ranges.samples < low.hand_ranges.samples_requested,
          "低预算的 build_situation 应真的降采样")
    line = next((w for w in low.warnings if "降采样" in w), "")
    check(bool(line), f"sit.warnings 缺降采样行：{low.warnings}")
    if not line:
        return
    _system, user = build_messages(low)
    check(line in user, f"提示词 user 段缺降采样行：{user}")
    adv = advise(low, SETTINGS,
                 complete=lambda *a, **k: {"option": "call", "reason": "x"})
    facts = adv.to_dict().get("facts") or {}
    check(line in (facts.get("warnings") or []),
          f"advice facts 缺降采样 warning：{facts.get('warnings')}")
    check(not any("降采样" in w for w in ample.warnings),
          f"充足预算的 sit.warnings 不应出现降采样：{ample.warnings}")


def test_benchmark_tool_reports_nine_opponents() -> None:
    """e：独立基准可手动跑；9 家全进抽样，报告 p90 与每次耗时。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "equity_benchmark.py"),
         "--runs", "2", "--samples", "200", "--json"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    check(proc.returncode == 0,
          f"基准退出码应为 0，实际 {proc.returncode}：{proc.stderr[-800:]}")
    if proc.returncode != 0:
        return
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        FAILS.append(f"基准 --json 输出不是 JSON：{exc}：{proc.stdout[:800]}")
        return
    check(report.get("opponents_in_sample") == 9,
          f"基准应证明 9 家全进抽样：{report.get('opponents_in_sample')}")
    check(report.get("all_opponents_sampled") is True,
          "基准应逐次检查在局人数")
    check(report.get("p90_ms") is not None and report["p90_ms"] >= 0,
          f"基准应报出 p90：{report.get('p90_ms')}")
    per_run = report.get("per_run_ms")
    check(isinstance(per_run, list) and len(per_run) == 2,
          f"基准应报出每次耗时：{per_run}")
    check(report.get("samples_requested") == 200,
          f"基准应报出目标样本数：{report.get('samples_requested')}")
    check(report.get("downsampled") is False,
          f"2000ms 预算下 200 次抽样不应降采样：{report.get('downsampled')}")


def main() -> int:
    tests = [
        test_low_budget_downsamples_without_dropping_opponents,
        test_ample_budget_bit_identical_and_no_warning,
        test_samples_requested_unchanged,
        test_options_unchanged_and_warning_visible_in_three_places,
        test_benchmark_tool_reports_nine_opponents,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print("\n❌ " + "\n❌ ".join(FAILS))
        return 1
    print("\n✅ 权益时间预算全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
