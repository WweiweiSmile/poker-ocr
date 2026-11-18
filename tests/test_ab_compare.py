#!/usr/bin/env python3
"""tools/ab_compare.py 的判定口径测试。

    nv -m pytest tests/test_ab_compare.py -q
    nv tests/test_ab_compare.py

为什么值得单独测：这张票的结论（建议保留 / 建议回滚）完全由这里的几件事决定 ——
三档分类的边界、对齐的容差、fallback 怎么排除、分位怎么算、c-bet 子集怎么筛。
口径一松，报告就会「好看」而失真，所以每条边界都拿合成数据钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ab_compare import (  # noqa: E402
    CBET_MIN_POINTS,
    CONFLICT,
    CONFLICT_RATE_MAX,
    SAME,
    SIZE_ONLY,
    UNCLASSIFIED,
    align,
    action_family,
    action_size,
    build_report,
    classify,
    is_cbet_point,
    is_fallback,
    latency_stats,
    percentile,
    rollback_verdict,
    size_direction,
)


def row(**kw) -> dict:
    """一个最小可判读的 dump 决策点。"""
    base = {
        "hand": "20261116-aaaaaaaa",
        "street": "flop",
        "cards": ["As", "Kd"],
        "board": ["2c", "7h", "Jd"],
        "pot": 10.0,
        "to_call": 0.0,
        "n_opp": 2,
        "new": "call",
        "new_reason": "",
        "ms": 900,
        "prompt_version": "v-x",
        "model": "m",
        "fallback": False,
        "prompt_facts": "",
    }
    base.update(kw)
    return base


def pair(old_id: str, new_id: str, **kw) -> tuple[dict, dict]:
    old_kw = {k: v for k, v in kw.items() if not k.startswith("new_")}
    new_kw = {k[4:]: v for k, v in kw.items() if k.startswith("new_")}
    old = row(**{**old_kw, "new": old_id})
    new = row(**{**old_kw, **new_kw, "new": new_id})
    return old, new


# ── 1. 动作族 / 尺寸档解析 ──

def test_action_family_vocabulary() -> None:
    # 翻后词表
    assert action_family("fold") == "fold"
    assert action_family("call") == "call"
    assert action_family("check") == "call"       # 过牌与跟注是同一个物理动作
    assert action_family("raise-1/3") == "raise"
    assert action_family("raise-1.2") == "raise"
    assert action_family("all-in") == "allin"
    assert action_family("allin") == "allin"
    assert action_family("all_in") == "allin"
    assert action_family("bet-1/2") == "bet"
    # 翻前词表
    assert action_family("open-5.5") == "raise"
    assert action_family("raise-3x") == "raise"
    # 大小写/空白容错
    assert action_family("  FOLD ") == "fold"
    # 认不出来
    for bad in (None, "", "   ", 3, "reraise-2", "unknown"):
        assert action_family(bad) is None, bad


def test_action_size_extraction() -> None:
    assert action_size("raise-1/3") == "1/3"
    assert action_size("open-5.5") == "5.5"
    assert action_size("raise-3x") == "3x"
    assert action_size("bet-2/3") == "2/3"
    assert action_size("fold") is None
    assert action_size("call") is None
    assert action_size("all-in") is None


# ── 2. 三档分类边界 ──

def test_classify_same_id() -> None:
    assert classify("call", "call") == SAME
    assert classify("raise-1/3", "raise-1/3") == SAME
    assert classify(" call ", "CALL") == SAME     # 归一化后才比


def test_classify_size_only() -> None:
    assert classify("raise-1/3", "raise-1/2") == SIZE_ONLY
    assert classify("open-5.5", "open-3") == SIZE_ONLY
    assert classify("raise-2/3", "raise-1.2") == SIZE_ONLY
    # call vs check：同一动作族、无尺寸 → 仅尺度不同（不是冲突）
    assert classify("call", "check") == SIZE_ONLY


def test_classify_conflict() -> None:
    assert classify("fold", "call") == CONFLICT
    assert classify("call", "raise-1/3") == CONFLICT
    assert classify("check", "bet-1/2") == CONFLICT
    assert classify("raise-1/3", "all-in") == CONFLICT   # all-in 单列一族
    assert classify("fold", "raise-1/2") == CONFLICT


def test_classify_missing_fields() -> None:
    # 缺字段 / 未知 id 不猜，单列「无法分类」，不进三档分母
    assert classify(None, "call") == UNCLASSIFIED
    assert classify("call", None) == UNCLASSIFIED
    assert classify("", "call") == UNCLASSIFIED
    assert classify("call", "") == UNCLASSIFIED
    assert classify("mystery", "call") == UNCLASSIFIED
    assert classify(12, "call") == UNCLASSIFIED


def test_size_direction() -> None:
    assert size_direction("raise-1/3", "raise-1/2") == "变大"
    assert size_direction("raise-2/3", "raise-1/2") == "变小"
    assert size_direction("open-5.5", "open-3") == "变小"
    assert size_direction("raise-3x", "raise-4x") == "变大"
    assert size_direction("open-3", "open-3") == "不变"
    # 不同尺子不可比：5.5BB 与 1/3 池没有共同刻度
    assert size_direction("open-5.5", "raise-1/3") == "不可比"
    # 无尺寸动作 → 不变
    assert size_direction("call", "check") == "不变"


# ── 3. 对齐：键与容差 ──

def test_align_pot_tolerance() -> None:
    old = [row(pot=10.00)]
    inside = [row(pot=10.01)]      # 容差内
    outside = [row(pot=10.02)]     # 超出 0.01
    pairs, old_unmatched, new_unmatched = align(old, inside)
    assert len(pairs) == 1 and old_unmatched == 0 and new_unmatched == 0
    pairs, old_unmatched, new_unmatched = align(old, outside)
    assert pairs == [] and old_unmatched == 1 and new_unmatched == 1


def test_align_keys_must_match() -> None:
    old = [row(street="flop")]
    assert align(old, [row(street="turn")])[0] == []
    assert align(old, [row(cards=["Ah", "Kd"])])[0] == []
    assert align(old, [row(board=["2c", "7h", "Jd", "4s"])])[0] == []
    # cards 用空格字符串也认
    pairs, _, _ = align(old, [row(cards="As Kd")])
    assert len(pairs) == 1


def test_align_same_key_consumed_once() -> None:
    old = [row(pot=10.0), row(pot=10.0)]
    pairs, old_unmatched, new_unmatched = align(old, [row(pot=10.0)])
    assert len(pairs) == 1
    assert old_unmatched == 1 and new_unmatched == 0


def test_align_missing_number_skipped() -> None:
    pairs, old_unmatched, _ = align([row(pot=None)], [row()])
    assert pairs == [] and old_unmatched == 1


# ── 4. fallback ──

def test_is_fallback_detection() -> None:
    assert is_fallback(row(fallback=True)) is True
    assert is_fallback(row(new="")) is True
    assert is_fallback(row(new=None)) is True
    assert is_fallback(row(new="ERROR")) is True
    assert is_fallback(row(fallback=False, new="call")) is False


def test_fallback_rows_excluded_from_alignment() -> None:
    old = [row(pot=10.0), row(pot=20.0), row(pot=30.0)]
    new = [row(pot=10.0), row(pot=20.0, fallback=True, new=""),
           row(pot=30.0, new="call")]
    report = build_report(old, new)
    assert report["pairs"] == 3
    assert report["aligned"] == 2                    # 20.0 那对整对剔除
    assert report["excluded_fallback_pairs"] == 1
    assert report["fallback"]["new"]["n"] == 1
    assert report["fallback"]["new"]["points"][0]["hand"] == "20261116-aaaaaaaa"


def test_fallback_counted_per_side() -> None:
    old = [row(pot=10.0, fallback=True, new=""), row(pot=20.0)]
    new = [row(pot=10.0), row(pot=20.0, fallback=True, new="ERROR")]
    report = build_report(old, new)
    assert report["fallback"]["old"]["n"] == 1
    assert report["fallback"]["new"]["n"] == 1
    assert report["aligned"] == 0


# ── 5. 分位与延迟 ──

def test_percentile_nearest_rank() -> None:
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 50) == 30
    assert percentile(values, 90) == 50
    assert percentile([10, 20, 30, 40], 50) == 20    # 偶数取较低的那个（保守）
    assert percentile([7], 90) == 7
    assert percentile([], 50) is None


def test_latency_stats_p50_p90_max() -> None:
    rows = [row(ms=100), row(ms=200), row(ms=300), row(ms=9000)]
    stats = latency_stats(rows)
    assert stats["n"] == 4
    assert stats["p50"] == 200
    assert stats["p90"] == 9000
    assert stats["max"] == 9000
    assert stats["over_budget"] == 1                # > 8000ms 计 1 个
    assert latency_stats([row(ms="x")])["n"] == 0


def test_report_latency_uses_aligned_points_only() -> None:
    old = [row(pot=10.0, ms=1000), row(pot=20.0, ms=2000)]
    new = [row(pot=10.0, ms=500), row(pot=20.0, ms=4000)]
    report = build_report(old, new)
    assert report["latency"]["old"]["max"] == 2000
    assert report["latency"]["new"]["max"] == 4000
    # nearest-rank：两个样本的 p50 取较低那个，不做插值
    assert report["latency"]["new"]["p50"] == 500
    assert report["latency"]["new"]["p90"] == 4000


# ── 6. c-bet 子集筛选 ──

def test_is_cbet_point_requires_all_four() -> None:
    facts = "## 当前局面\n规则: 多人池你先行动…"
    assert is_cbet_point(row(prompt_facts=facts)) is True
    assert is_cbet_point(row(prompt_facts=facts, street="turn")) is False
    assert is_cbet_point(row(prompt_facts=facts, to_call=5.0)) is False
    assert is_cbet_point(row(prompt_facts=facts, n_opp=3)) is False
    assert is_cbet_point(row(prompt_facts="没有规则行")) is False
    assert is_cbet_point(row(prompt_facts=None)) is False


def test_report_cbet_subset() -> None:
    facts = "规则: 多人池你先行动"
    old = [
        row(pot=10.0, prompt_facts=facts, new="raise-1/3"),
        row(pot=20.0, prompt_facts=facts, new="call"),
        row(pot=30.0, prompt_facts="", new="call"),           # 没规则行 → 不进子集
        row(pot=40.0, prompt_facts=facts, n_opp=3, new="call"),  # 多人 → 不进子集
    ]
    new = [
        row(pot=10.0, prompt_facts=facts, new="raise-1/2"),
        row(pot=20.0, prompt_facts=facts, new="call"),
        row(pot=30.0, prompt_facts="", new="raise-1/3"),
        row(pot=40.0, prompt_facts=facts, n_opp=3, new="raise-1/3"),
    ]
    report = build_report(old, new)
    assert report["cbet"]["n"] == 2
    assert report["cbet"]["counts"][SIZE_ONLY] == 1
    assert report["cbet"]["counts"][SAME] == 1
    assert report["cbet"]["size_pairs"] == {"1/3→1/2": 1}
    # 宽口径 = 全部「翻牌且 to_call=0」，不看 n_opp、也不看有没有规则行 → 4 点
    assert report["cbet_wide"]["n"] == 4
    assert report["cbet_wide"]["size_pairs"] == {"1/3→1/2": 1}


# ── 7. 回滚判定：不触发 / 触发 ──

def test_verdict_keep_when_clean() -> None:
    old = [row(pot=float(i), new="call") for i in range(1, 21)]
    new = [row(pot=float(i), new="call") for i in range(1, 21)]
    report = build_report(old, new)
    assert report["conflict_rate"] == 0.0
    assert report["verdict"]["verdict"] == "建议保留"
    assert report["verdict"]["rollback"] is False
    assert report["verdict"]["triggers"] == []


def test_verdict_rollback_on_conflict_rate() -> None:
    n = 10
    old = [row(pot=float(i), new="call") for i in range(n)]
    new = [row(pot=float(i), new="raise-1/3" if i < 4 else "call")
           for i in range(n)]                     # 4/10 = 40% > 20%
    report = build_report(old, new)
    assert report["conflict_rate"] > CONFLICT_RATE_MAX
    assert report["verdict"]["verdict"] == "建议回滚"
    assert any("动作冲突率" in t for t in report["verdict"]["triggers"])


def test_verdict_rollback_on_latency() -> None:
    old = [row(pot=float(i), ms=1000) for i in range(5)]
    new = [row(pot=float(i), ms=2500) for i in range(5)]   # p50 翻 2.5 倍
    report = build_report(old, new)
    assert report["verdict"]["verdict"] == "建议回滚"
    assert any("p50" in t for t in report["verdict"]["triggers"])

    old2 = [row(pot=float(i), ms=800) for i in range(5)]
    new2 = [row(pot=float(i), ms=900) for i in range(4)] + [row(pot=4.0, ms=9001)]
    report2 = build_report(old2, new2)
    assert any("max" in t for t in report2["verdict"]["triggers"])


def test_verdict_rollback_on_cbet_subset() -> None:
    facts = "规则: 多人池你先行动"
    n = 8
    old = [row(pot=float(i), prompt_facts=facts, new="raise-1/3") for i in range(n)]
    new = [row(pot=float(i), prompt_facts=facts, new="call") for i in range(n)]
    cbet = {"n": n, "conflict_rate": 1.0}
    verdict = rollback_verdict(0.0, cbet, {"old": {"p50": 900, "max": 1000},
                                           "new": {"p50": 900, "max": 1000}}, n)
    assert verdict["verdict"] == "建议回滚"
    assert any("c-bet" in t for t in verdict["triggers"])


def test_small_cbet_subset_does_not_trigger() -> None:
    # 子集小于 CBET_MIN_POINTS 时不下结论：即使 100% 冲突也不据此回滚
    cbet = {"n": CBET_MIN_POINTS - 1, "conflict_rate": 1.0}
    verdict = rollback_verdict(0.0, cbet, {"old": {"p50": 900, "max": 1000},
                                           "new": {"p50": 900, "max": 1000}}, 20)
    assert verdict["verdict"] == "建议保留"


def test_verdict_empty_alignment() -> None:
    verdict = rollback_verdict(0.0, {"n": 0, "conflict_rate": 0.0},
                               {"old": {}, "new": {}}, 0)
    assert verdict["verdict"] == "无法判定"


def test_report_roundtrip_fields() -> None:
    old = [row(pot=10.0, new="raise-1/3", ms=700)]
    new = [row(pot=10.0, new="raise-1/2", ms=800)]
    report = build_report(old, new, old_paths=["a.json"], new_paths=["b.json"])
    assert report["schema"] == "ab_compare.v1"
    assert report["points_total"] == {"old": 1, "new": 1}
    assert report["points"][0]["size_direction"] == "变大"
    assert report["points"][0]["old_size"] == "1/3"
    assert report["points"][0]["new_size"] == "1/2"
    assert report["size"]["pairs"] == {"1/3→1/2": 1}
    assert report["inputs"]["old"] == ["a.json"]


def _all_tests() -> list:
    return [(k, v) for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = []
    for name, fn in _all_tests():
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"FAIL {name}: {exc}")
    print()
    if failures:
        print(f"❌ {len(failures)} 个用例失败：" + ", ".join(failures))
        return 1
    print(f"✅ {len(_all_tests())} 个用例全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
