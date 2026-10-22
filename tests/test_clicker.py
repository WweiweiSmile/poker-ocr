#!/usr/bin/env python3
"""桌面自动点击的单测：坐标、配置、档位映射、执行顺序、安全闸。

    .venv/bin/python tests/test_clicker.py

全部是纯逻辑 + 假后端，不发送真实鼠标事件，不需要 Tk。
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import Advice, Option  # noqa: E402
from poker_ocr.clicker import (  # noqa: E402
    DEFAULT_INSET_RATIO, PRESET_IDS, Clicker, ClickerConfig,
    ClickerConfigError, DisplayInfo, DryRunBackend, Rect, choose_preset,
    plan_clicks, rect_inside_displays,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def cfg_all(rect: Rect | None = None,
            **overrides) -> ClickerConfig:
    rect = rect or Rect(0, 0, 100, 50)
    buttons = {name: rect for name in ("fold", "call", *PRESET_IDS)}
    buttons.update(overrides.pop("buttons", {}))
    return ClickerConfig(buttons=buttons, **overrides)


def adv(option: Option, presets: dict[str, float] | None = None) -> Advice:
    return Advice("h1", "flop", option, "reason", "model",
                  preset_amounts=tuple((presets or {}).items()))


PRESETS = {"raise-1/3": 2.0, "raise-1/2": 2.25, "raise-2/3": 2.67,
           "raise-1": 3.5, "raise-1.2": 4.0}


class FakeBackend:
    def __init__(self, *, allowed: bool = True, fail_on: str | None = None) -> None:
        self.allowed = allowed
        self.fail_on = fail_on
        self.events: list[tuple[str, float, float]] = []

    def preflight(self) -> bool:
        return self.allowed

    def move(self, x: float, y: float) -> None:
        self.events.append(("move", x, y))

    def mouse_down(self, x: float, y: float) -> None:
        if self.fail_on == "down":
            raise RuntimeError("down failed")
        self.events.append(("down", x, y))

    def mouse_up(self, x: float, y: float) -> None:
        if self.fail_on == "up":
            raise RuntimeError("up failed")
        self.events.append(("up", x, y))


# ─────────────────────────── 矩形 ───────────────────────────

def test_random_point_stays_inside_inset() -> None:
    rect = Rect(100, 200, 80, 40)
    rng = random.Random(7)
    pad_x, pad_y = 80 * DEFAULT_INSET_RATIO, 40 * DEFAULT_INSET_RATIO
    for _ in range(500):
        x, y = rect.inset_point(rng)
        check(rect.x + pad_x <= x <= rect.x2 - pad_x, f"x 越界：{x}")
        check(rect.y + pad_y <= y <= rect.y2 - pad_y, f"y 越界：{y}")


def test_random_point_is_reproducible() -> None:
    rect = Rect(0, 0, 100, 100)
    a = [rect.inset_point(random.Random(3)) for _ in range(5)]
    b = [rect.inset_point(random.Random(3)) for _ in range(5)]
    check(a == b, f"同 seed 应可复现：{a} != {b}")


def test_tiny_rect_falls_back_to_center() -> None:
    rect = Rect(10, 20, 2, 2)
    x, y = rect.inset_point(random.Random(1))
    check((x, y) == (rect.cx, rect.cy), f"极小矩形应点中心：{(x, y)}")


def test_circle_point_stays_inside_inset_ellipse() -> None:
    """圆形按钮：随机点必须落在外接框的内切椭圆里，不能跑到四角。"""
    rect = Rect(0, 0, 100, 80, shape="circle")
    rng = random.Random(11)
    ratio = DEFAULT_INSET_RATIO
    ra = rect.w / 2 - rect.w * ratio
    rb = rect.h / 2 - rect.h * ratio
    for _ in range(500):
        x, y = rect.sample_point(rng)
        norm = ((x - rect.cx) / ra) ** 2 + ((y - rect.cy) / rb) ** 2
        check(norm <= 1.0 + 1e-9, f"圆形取样越界：({x:.2f}, {y:.2f}) norm={norm:.3f}")


def test_circle_point_is_reproducible() -> None:
    rect = Rect(0, 0, 100, 100, shape="circle")
    a = [rect.sample_point(random.Random(5)) for _ in range(5)]
    b = [rect.sample_point(random.Random(5)) for _ in range(5)]
    check(a == b, f"圆形取样同 seed 应可复现：{a} != {b}")


def test_plan_uses_circle_region() -> None:
    cfg = cfg_all()
    cfg.buttons["fold"] = Rect(100, 100, 80, 80, shape="circle")
    for seed in range(30):
        plan = plan_clicks(adv(Option("fold", "Fold", "fold")), cfg,
                           random.Random(seed))
        x, y = plan.steps[0].x, plan.steps[0].y
        norm = ((x - 140) / 28) ** 2 + ((y - 140) / 28) ** 2
        # 80 的框内缩 0.15（12pt）→ 半径 28
        check(norm <= 1.0 + 1e-9,
              f"圆形按钮的点击点跑出内切圆：({x:.1f}, {y:.1f})")


# ─────────────────────────── 配置 ───────────────────────────

def test_config_roundtrip() -> None:
    cfg = cfg_all(Rect(1, 2, 30, 40, "circle"), inset_ratio=0.2,
                  click_delay_ms=90, confirm_gap_ms=250,
                  retry_delay_ms=750, retry_count=2)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clicker.json"
        cfg.save(path)
        loaded = ClickerConfig.load(path)
    check(loaded.buttons["fold"] == Rect(1, 2, 30, 40, "circle"),
          f"矩形/形状应原样读回：{loaded.buttons['fold']}")
    check(loaded.inset_ratio == 0.2 and loaded.click_delay_ms == 90,
          "标量参数应原样读回")
    check(loaded.confirm_gap_ms == 250, "confirm_gap_ms 应原样读回")
    check(loaded.retry_delay_ms == 750 and loaded.retry_count == 2,
          "重试参数应原样读回")


def test_config_validates_retry_bounds() -> None:
    for kwargs in ({"retry_delay_ms": -1}, {"retry_count": 6}):
        cfg = cfg_all(**kwargs)
        try:
            cfg.validate()
        except ClickerConfigError:
            continue
        FAILS.append(f"非法重试参数应校验失败：{kwargs}")


def test_config_requires_fold_call() -> None:
    for missing in ("fold", "call"):
        cfg = cfg_all()
        cfg.buttons.pop(missing)
        try:
            cfg.validate()
        except ClickerConfigError:
            continue
        FAILS.append(f"缺 {missing} 时应校验失败")

    # confirm 已不再参与自动点击，旧配置里有/没有都可以
    cfg = cfg_all()
    cfg.buttons.pop("confirm", None)
    cfg.validate()


def test_config_rejects_bad_rect() -> None:
    for bad in (Rect(0, 0, 0, 10), Rect(0, 0, 10, 0)):
        cfg = cfg_all(bad)
        try:
            cfg.validate()
        except ClickerConfigError:
            continue
        FAILS.append(f"非法矩形 {bad} 应校验失败")


def test_config_rejects_bad_shape() -> None:
    cfg = cfg_all(Rect(0, 0, 10, 10, "triangle"))
    try:
        cfg.validate()
    except ClickerConfigError:
        return
    FAILS.append("未知 shape 应校验失败")


def test_config_accepts_list_coordinates() -> None:
    raw = {"version": 1, "buttons": {
        "fold": [1, 2, 30, 40], "call": {"x": 5, "y": 6, "w": 7, "h": 8},
        "raise-1/2": [9, 10, 11, 12]}}
    cfg = ClickerConfig.from_dict(raw)
    check(cfg.buttons["fold"] == Rect(1, 2, 30, 40), "列表坐标应支持")
    check(cfg.buttons["call"] == Rect(5, 6, 7, 8), "对象坐标应支持")
    check(cfg.buttons["fold"].shape == "rect", "缺省 shape 应是 rect")


def test_config_rejects_bad_version() -> None:
    raw = {"version": 99, "buttons": {
        "fold": [0, 0, 1, 1], "call": [0, 0, 1, 1], "confirm": [0, 0, 1, 1]}}
    try:
        ClickerConfig.from_dict(raw)
    except ClickerConfigError:
        return
    FAILS.append("未知版本应被拒绝")


# ─────────────────────────── 档位映射 ───────────────────────────

def test_choose_preset_direct() -> None:
    got = choose_preset(Option("raise-1/2", "Raise", "raise", 2.25), PRESETS)
    check(got == ("raise-1/2", 2.25, "direct"), f"直接档位映射错误：{got}")


def test_choose_preset_nearest_preflop() -> None:
    got = choose_preset(Option("open-2.5", "Open", "raise", 2.5), PRESETS)
    check(got == ("raise-2/3", 2.67, "nearest"), f"最近档位映射错误：{got}")


def test_choose_preset_tie_breaks_by_order() -> None:
    amounts = {"raise-1/3": 1.0, "raise-1/2": 2.0, "raise-2/3": 4.0,
               "raise-1": 5.0, "raise-1.2": 6.0}
    # 3.0 到 2.0 和 4.0 等距：1/2 排在 2/3 前面
    got = choose_preset(Option("open-x", "Open", "raise", 3.0), amounts)
    check(got is not None and got[0] == "raise-1/2",
          f"平局应按档位顺序：{got}")


def test_choose_preset_missing_amount() -> None:
    got = choose_preset(Option("raise-1/2", "Raise", "raise", 2.25),
                        {"raise-1/3": 2.0})
    check(got is None, f"目标档位没有金额时应返回 None：{got}")


def test_plan_fold_call_check() -> None:
    cfg = cfg_all()
    fold = plan_clicks(adv(Option("fold", "Fold", "fold")), cfg,
                       random.Random(1))
    check(fold.action == "fold" and len(fold.steps) == 1
          and fold.steps[0].button == "fold", f"弃牌计划错误：{fold}")
    for kind, oid in (("call", "call"), ("check", "call")):
        plan = plan_clicks(adv(Option(oid, "Call", kind, 2.0)), cfg,
                           random.Random(1))
        check(len(plan.steps) == 1 and plan.steps[0].button == "call",
              f"{kind} 应点右侧按钮：{plan}")


def test_plan_raise_is_preset_only() -> None:
    """点快捷档位即完成加注，不再点蓝色确认按钮。"""
    cfg = cfg_all()
    plan = plan_clicks(adv(Option("raise-1/2", "Raise", "raise", 2.25),
                           PRESETS), cfg, random.Random(1))
    check([s.button for s in plan.steps] == ["raise-1/2"],
          f"加注应只点快捷档位：{plan}")
    check(plan.mapping == "direct" and plan.preset == "raise-1/2",
          f"映射信息错误：{plan}")


def test_plan_missing_preset_skips() -> None:
    cfg = cfg_all()
    cfg.buttons.pop("raise-1/2")
    plan = plan_clicks(adv(Option("raise-1/2", "Raise", "raise", 2.25),
                           PRESETS), cfg, random.Random(1))
    check(plan.steps == () and plan.skip_reason == "missing_preset:raise-1/2",
          f"缺少目标档位应跳过而不是换档：{plan}")


def test_plan_unknown_amount_skips() -> None:
    cfg = cfg_all()
    plan = plan_clicks(adv(Option("open-x", "Open", "raise", None), PRESETS),
                       cfg, random.Random(1))
    check(plan.skip_reason == "amount_unknown", f"没有金额应跳过：{plan}")


# ─────────────────────────── 执行 ───────────────────────────

def test_clicker_executes_move_down_up() -> None:
    cfg = cfg_all(Rect(10, 20, 80, 40))
    backend = FakeBackend()
    sleeps: list[float] = []
    clicker = Clicker(cfg, backend, rng=random.Random(2),
                      sleep=sleeps.append)
    result = clicker.run(adv(Option("raise-1/2", "Raise", "raise", 2.25),
                             PRESETS))
    check(result.status == "clicked", f"应点击成功：{result}")
    check([e[0] for e in backend.events] == ["move", "down", "up"],
          f"加注只应点一次快捷档位：{backend.events}")
    check(len(sleeps) == 1 and sleeps[0] > 0,
          f"应只有按下保持的延时：{sleeps}")


def test_clicker_dry_run_sends_no_events() -> None:
    backend = FakeBackend(allowed=False)     # dry-run 不应依赖权限
    clicker = Clicker(cfg_all(), backend, dry_run=True,
                      rng=random.Random(2), sleep=lambda _s: None)
    result = clicker.run(adv(Option("fold", "Fold", "fold")))
    check(result.status == "dry_run", f"dry-run 状态错误：{result}")
    check(backend.events == [], f"dry-run 不应发事件：{backend.events}")


def test_clicker_blocks_without_accessibility() -> None:
    backend = FakeBackend(allowed=False)
    clicker = Clicker(cfg_all(), backend, rng=random.Random(2),
                      sleep=lambda _s: None)
    result = clicker.run(adv(Option("fold", "Fold", "fold")))
    check(result.status == "error"
          and result.error == "accessibility_permission_denied",
          f"无权限应报错：{result}")
    check(backend.events == [], "无权限时不应发事件")


def test_clicker_releases_mouse_on_failure() -> None:
    backend = FakeBackend(fail_on="down")
    clicker = Clicker(cfg_all(), backend, rng=random.Random(2),
                      sleep=lambda _s: None)
    result = clicker.run(adv(Option("fold", "Fold", "fold")))
    check(result.status == "error", f"后端异常应返回 error：{result}")
    check("down failed" in (result.error or ""), f"错误信息应保留：{result.error}")
    check(any(e[0] == "up" for e in backend.events),
          f"按下失败后仍应尽力抬起：{backend.events}")


# ─────────────────────────── 显示器 ───────────────────────────

def test_rect_inside_displays() -> None:
    displays = [DisplayInfo(0, 1, 0, 0, 100, 100, True)]
    check(rect_inside_displays(Rect(10, 10, 20, 20), displays), "屏内矩形应通过")
    check(not rect_inside_displays(Rect(90, 90, 20, 20), displays),
          "越界矩形应被拒绝")
    check(rect_inside_displays(Rect(0, 0, 10, 10), []), "没有显示器信息时不拦")


def main() -> int:
    tests = [
        test_random_point_stays_inside_inset,
        test_random_point_is_reproducible,
        test_tiny_rect_falls_back_to_center,
        test_circle_point_stays_inside_inset_ellipse,
        test_circle_point_is_reproducible,
        test_plan_uses_circle_region,
        test_config_roundtrip,
        test_config_validates_retry_bounds,
        test_config_requires_fold_call,
        test_config_rejects_bad_rect,
        test_config_rejects_bad_shape,
        test_config_accepts_list_coordinates,
        test_config_rejects_bad_version,
        test_choose_preset_direct,
        test_choose_preset_nearest_preflop,
        test_choose_preset_tie_breaks_by_order,
        test_choose_preset_missing_amount,
        test_plan_fold_call_check,
        test_plan_raise_is_preset_only,
        test_plan_missing_preset_skips,
        test_plan_unknown_amount_skips,
        test_clicker_executes_move_down_up,
        test_clicker_dry_run_sends_no_events,
        test_clicker_blocks_without_accessibility,
        test_clicker_releases_mouse_on_failure,
        test_rect_inside_displays,
    ]
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
    print("\n✅ 自动点击全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
