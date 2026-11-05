#!/usr/bin/env python3
"""策略表（P0 抽表 + P1 推导）的测试：来源完整、profile 生效、接线正确、数值不变。

    .venv/bin/python tests/test_strategy_tables.py

关键不变量：

  · 每张表都有 source / note；v1 变体必须有 derivation 与 needs；
  · `hand-tuned` profile 下模块常数与 JSON 的 v0 **逐值相等**（重构没有改变行为）；
  · `sourced` profile 下只覆盖有 v1 的表，其余回落 v0 且被标记；
  · 未知 profile / 未知表 / 缺文件 → 明确报错，不静默降级成别的表；
  · v1 的 defending 单调性：强牌弃得少、空气弃得多、价格越高弃得越多。

纯逻辑，不读影像、不调模型。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr import fold_equity as fe  # noqa: E402
from poker_ocr import range_flow as rf  # noqa: E402
from poker_ocr import strategy_tables as st  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _doc(kind: str) -> dict:
    return json.loads((st.DEFAULT_DIR / f"{kind}.json").read_text(encoding="utf-8"))


# ── 结构 ──


def test_every_table_has_provenance() -> None:
    """每张表、每个变体都要有 source 与 note；v1 还要有推导与缺口。"""
    for kind in ("range_flow", "fold_equity"):
        doc = _doc(kind)
        for name, variants in doc["tables"].items():
            for variant, entry in variants.items():
                check(bool(entry.get("source")),
                      f"{kind}.{name}.{variant} 缺 source")
                check(entry.get("source") in
                      ("hand-tuned", "derived", "empirical"),
                      f"{kind}.{name}.{variant} source 非法：{entry.get('source')}")
                check(bool(entry.get("note")),
                      f"{kind}.{name}.{variant} 缺 note（来源说明）")
                check("verified" in entry,
                      f"{kind}.{name}.{variant} 缺 verified 标记")
                if variant == "v1":
                    check(bool(entry.get("derivation")),
                          f"{kind}.{name}.v1 缺 derivation（推导式）")
                    check(bool(entry.get("needs")),
                          f"{kind}.{name}.v1 缺 needs（还差什么才能验证）")


def test_v0_is_the_default() -> None:
    for kind in ("range_flow", "fold_equity"):
        check(_doc(kind)["default_variant"] == "v0",
              f"{kind} 默认变体不是 v0")
    check(st.variant_of("hand-tuned") == "v0", "hand-tuned 应解析成 v0")
    check(st.variant_of("sourced") == "v1", "sourced 应解析成 v1")


# ── 接线：模块常数真的来自 JSON ──


def test_module_constants_come_from_tables() -> None:
    check(rf._FACING_PROBS == st.rows("range_flow", "facing_probs"),
          "range_flow._FACING_PROBS 与 JSON 不一致")
    check(rf._BUCKET_TIER == st.rows("range_flow", "bucket_tier"),
          "range_flow._BUCKET_TIER 与 JSON 不一致")
    check(fe.BASE_FOLD == st.rows("fold_equity", "base_fold"),
          "fold_equity.BASE_FOLD 与 JSON 不一致")
    check(fe.SIZE_GAMMA == st.value("fold_equity", "size_gamma"),
          "fold_equity.SIZE_GAMMA 与 JSON 不一致")
    check(tuple(fe.DEFAULT_SIZES) ==
          tuple(st.items("fold_equity", "default_sizes")),
          "fold_equity.DEFAULT_SIZES 与 JSON 不一致")


def test_shape_conversion() -> None:
    """JSON 的 list/int-key 要转回代码期望的 tuple/int-key。"""
    check(isinstance(rf._FACING_PROBS["nut"], tuple),
          "facing_probs 的行应是 tuple")
    check(rf._DRAW_STREET_FACTOR == {3: 1.0, 4: 0.5, 5: 0.0},
          f"街因子应是 int 键：{rf._DRAW_STREET_FACTOR}")
    check(len(rf._TIER_PERCENTILE) == 5, "百分位表应有 5 行")
    check(all(isinstance(limit, float) and isinstance(tier, str)
              for limit, tier in rf._TIER_PERCENTILE),
          "百分位表应是 (float, str) 元组")


# ── profile 切换 ──


def test_sourced_profile_only_overrides_covered_tables() -> None:
    overridden = {"range_flow.facing_probs", "fold_equity.base_fold",
                  "fold_equity.size_gamma"}
    for kind in ("range_flow", "fold_equity"):
        for entry in st.catalog(kind, profile="sourced"):
            key = f"{kind}.{entry.name}"
            if key in overridden:
                check(entry.variant == "v1" and not entry.fell_back,
                      f"{key} 应使用 v1，实际 {entry.variant}")
            else:
                check(entry.variant == "v0" and entry.fell_back,
                      f"{key} 应回落 v0 并标记，实际 {entry.variant}")


def test_sourced_changes_the_two_tables() -> None:
    v0 = st.rows("range_flow", "facing_probs", profile="hand-tuned")
    v1 = st.rows("range_flow", "facing_probs", profile="sourced")
    check(v0 != v1, "facing_probs 的 v1 与 v0 相同，推导没生效")
    b0 = st.rows("fold_equity", "base_fold", profile="hand-tuned")
    b1 = st.rows("fold_equity", "base_fold", profile="sourced")
    check(b0 != b1, "base_fold 的 v1 与 v0 相同，推导没生效")


def test_v1_matches_its_derivation() -> None:
    """v1 必须能由模型复现：JSON 里的值 == 现在重算的值（防止手改漂移）。"""
    from tools.derive_strategy_tables import (
        REALIZATION_KIND, REALIZATION_POS, TIER_ER_9, base_fold_v1,
        facing_probs_v1, size_gamma_v1,
    )
    derived = {
        "range_flow.facing_probs": facing_probs_v1(),
        "fold_equity.base_fold": base_fold_v1(),
        "fold_equity.size_gamma": size_gamma_v1(),
    }
    got = {
        "range_flow.facing_probs": st.rows("range_flow", "facing_probs",
                                           profile="sourced"),
        "fold_equity.base_fold": st.rows("fold_equity", "base_fold",
                                         profile="sourced"),
        "fold_equity.size_gamma": st.value("fold_equity", "size_gamma",
                                           profile="sourced"),
    }
    for key, value in derived.items():
        if isinstance(value, dict):
            value = {k: tuple(v) if isinstance(v, list) else v
                     for k, v in value.items()}
        check(got[key] == value,
              f"{key} 与推导结果不一致（JSON 被手改过？）\n"
              f"   json={got[key]}\n   推导={value}")

    # 单调性必须按 e·R 排，不是按牌型名排：听牌的 e·R 可以高于中底对。
    def er(tier: str) -> float:
        equity, kind = TIER_ER_9[tier]
        return equity * REALIZATION_KIND[kind] * REALIZATION_POS

    # e·R 越低 → 越会弃，所以按 e·R 升序时弃牌率应递减。
    order = sorted(TIER_ER_9, key=er)
    values = [got["fold_equity.base_fold"][t] for t in order]
    check(values == sorted(values, reverse=True),
          f"v1 弃牌率不随 e·R 单调：{dict(zip(order, values))}")
    check(got["fold_equity.base_fold"]["空气"] > 0.85,
          f"v1 空气弃牌率过低：{got['fold_equity.base_fold']['空气']}")
    check(got["fold_equity.base_fold"]["强成牌"] < 0.05,
          f"v1 强成牌弃牌率过高：{got['fold_equity.base_fold']['强成牌']}")

    # 两条 profile 的曲线都必须随价格单调（价格越高越会弃）。
    for tier in fe.TIER_ZH:
        curve = [fe.fold_probability(tier, p) for p in (0.1, 0.25, 0.5, 1.0)]
        check(curve == sorted(curve), f"{tier} 的弃牌曲线不随价格单调：{curve}")


def test_v1_respects_clamps() -> None:
    base = st.rows("fold_equity", "base_fold", profile="sourced")
    for tier, value in base.items():
        check(0.0 < value < 1.0, f"v1 {tier} 越界：{value}")
    facing = st.rows("range_flow", "facing_probs", profile="sourced")
    for tier, row in facing.items():
        check(all(0.0 <= v <= 1.0 for v in row), f"v1 facing {tier} 越界：{row}")
        check(abs(sum(row) - 1.0) < 1e-3,
              f"v1 facing {tier} 三列不归一：{row}")


# ── 错误处理：不静默降级 ──


def test_unknown_profile_raises() -> None:
    try:
        st.variant_of("no-such-profile")
    except KeyError:
        return
    check(False, "未知 profile 应该抛 KeyError")


def test_unknown_table_raises() -> None:
    try:
        st.table("range_flow", "no_such_table")
    except KeyError:
        return
    check(False, "未知表应该抛 KeyError")


def test_missing_dir_raises() -> None:
    old = os.environ.get(st.DIR_ENV)
    os.environ[st.DIR_ENV] = "/tmp/poker-ocr-no-such-tables"
    st.reset_cache()
    try:
        st.table("range_flow", "facing_probs")
    except FileNotFoundError:
        pass
    else:
        check(False, "缺表目录应该抛 FileNotFoundError")
    finally:
        if old is None:
            os.environ.pop(st.DIR_ENV, None)
        else:
            os.environ[st.DIR_ENV] = old
        st.reset_cache()


# ── 端到端：两套表都能把范围建出来（不崩、不空） ──


def test_both_profiles_build_a_range() -> None:
    from poker_ocr import ranges
    prior = ranges.get("BB_DEF_VS_BTN")
    check(prior is not None, "取不到 BB_DEF_VS_BTN")
    if prior is None:
        return
    events = [
        rf.ActionEvent(street="flop", board="Ks7h2c", seat_id="o1",
                       action="check", facing_bet=False, players_behind=1),
    ]
    for profile in ("hand-tuned", "sourced"):
        old = os.environ.get(st.PROFILE_ENV)
        os.environ[st.PROFILE_ENV] = profile
        st.reset_cache()
        try:
            # 模块常数在 import 时已定，这里直接调底层表验证可构建，
            # 不用 reload（reload 会污染其它测试的模块状态）。
            built = rf.build_range(prior, events)
            check(built is not None, f"{profile}: build_range 返回 None")
            if built is not None:
                combos = sum(1 for w in built.combo_weights.values() if w > 0)
                check(combos > 100, f"{profile}: 收窄后组合数太少 {combos}")
        finally:
            if old is None:
                os.environ.pop(st.PROFILE_ENV, None)
            else:
                os.environ[st.PROFILE_ENV] = old
            st.reset_cache()


def main() -> int:
    tests = [v for name, v in sorted(globals().items())
             if name.startswith("test_") and callable(v)
             and getattr(v, "__module__", "") == __name__]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 策略表全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
