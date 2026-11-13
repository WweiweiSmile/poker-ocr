#!/usr/bin/env python3
"""桌面自动点击的单测：坐标、配置、档位映射、执行顺序、安全闸。

    nv tests/test_clicker.py

全部是纯逻辑 + 假后端，不发送真实鼠标事件，不需要 Tk。
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import Advice, Option  # noqa: E402
from poker_ocr.clicker import (  # noqa: E402
    DEFAULT_INSET_RATIO, PRESET_IDS, Clicker, ClickerConfig,
    ClickerConfigError, DisplayInfo, DryRunBackend, Rect, choose_preset,
    choose_preset_within, plan_clicks, rect_inside_displays,
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

    def scroll(self, x: float, y: float, delta: int) -> None:
        self.events.append(("scroll", x, y, int(delta)))



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
    check(loaded.preset_tolerance == 0.05,
          f"preset_tolerance 默认应是 0.05，实际 {loaded.preset_tolerance}")


def test_config_validates_retry_bounds() -> None:
    for kwargs in ({"retry_delay_ms": -1}, {"retry_count": 6},
                   {"preset_tolerance": -0.1}, {"preset_tolerance": 1.1}):
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


def test_choose_preset_within_tolerance() -> None:
    """5% 内选快捷档位；超过 5% 返回 None（调用方再走滑杆）。"""
    presets = {"raise-1/3": 3.0, "raise-1/2": 5.0, "raise-2/3": 7.0,
               "raise-1": 10.0, "raise-1.2": 12.0}
    chosen = choose_preset_within(Option("bet", "Bet", "bet", 5.2), presets)
    check(chosen is not None and chosen[0] == "raise-1/2",
          f"5.2 对 5.0 是 4% 应选 1/2，实际 {chosen}")
    check(chosen[2] == "within_5pct", f"mapping 应为 within_5pct，实际 {chosen}")
    check(choose_preset_within(Option("bet", "Bet", "bet", 6.0), presets) is None,
          "6.0 对 5.0/7.0 都超过 5%，应返回 None")
    exact = choose_preset_within(Option("bet", "Bet", "bet", 5.0), presets)
    check(exact is not None and exact[0] == "raise-1/2",
          f"正好 5.0 应选 1/2，实际 {exact}")


def test_choose_preset_within_picks_closest() -> None:
    presets = {"raise-1/3": 5.0, "raise-1/2": 5.2}
    chosen = choose_preset_within(Option("bet", "Bet", "bet", 5.1), presets)
    check(chosen is not None and chosen[0] == "raise-1/2",
          f"5.1 离 5.2 更近，应选 1/2，实际 {chosen}")


def test_slider_prefers_preset_within_5pct() -> None:
    """推荐额在快捷档位 5% 内：点档位，不碰滑杆。"""
    class FakeSlider:
        def __init__(self) -> None:
            self.called = False

        def preset_enabled(self, _preset_id):
            return True

        def set_amount(self, _target):
            self.called = True
            return SimpleNamespace(status="set", error=None, reason="bet_slider",
                                   to_dict=lambda: {"status": "set"})

    cfg = cfg_all()
    slider = FakeSlider()
    backend = FakeBackend()
    clicker = Clicker(cfg, backend, bet_slider=slider)
    advice = adv(Option("bet", "Bet 5.2", "bet", 5.2), {"raise-1/2": 5.0})
    r = clicker.run(advice)
    check(r.status == "clicked" and r.mapping == "within_5pct"
          and r.preset == "raise-1/2",
          f"5% 内应点档位，实际 {r.to_dict()}")
    check(not slider.called, "5% 内不该走滑杆")
    check([e[0] for e in backend.events] == ["move", "down", "up"],
          f"应只点一次档位，实际 {backend.events}")


def test_slider_used_when_outside_5pct_or_button_missing() -> None:
    class FakeSlider:
        def __init__(self) -> None:
            self.targets: list[float] = []

        def set_amount(self, target):
            self.targets.append(target)
            return SimpleNamespace(status="set", error=None, reason="bet_slider",
                                   to_dict=lambda: {"status": "set"})

    slider = FakeSlider()
    cfg = cfg_all()
    clicker = Clicker(cfg, FakeBackend(), bet_slider=slider)
    clicker.run(adv(Option("bet", "Bet 6.5", "bet", 6.5), {"raise-1/2": 5.0}))
    check(slider.targets == [6.5], f"超出 5% 应走滑杆 6.5，实际 {slider.targets}")

    # 档位在 5% 内但按钮没标定 → 退回滑杆，不 skip
    cfg2 = cfg_all()
    cfg2.buttons.pop("raise-1/2")
    slider2 = FakeSlider()
    clicker2 = Clicker(cfg2, FakeBackend(), bet_slider=slider2)
    clicker2.run(adv(Option("bet", "Bet 5.2", "bet", 5.2), {"raise-1/2": 5.0}))
    check(slider2.targets == [5.2],
          f"档位按钮缺失应退回滑杆，实际 {slider2.targets}")


def test_slider_used_when_preset_disabled() -> None:
    """5% 内但档位置灰（后手不够/低于最低加注）→ 退回滑杆。"""
    class FakeSlider:
        def __init__(self) -> None:
            self.targets: list[float] = []

        def preset_enabled(self, _preset_id):
            return False

        def set_amount(self, target):
            self.targets.append(target)
            return SimpleNamespace(status="set", error=None, reason="bet_slider",
                                   to_dict=lambda: {"status": "set"})

    slider = FakeSlider()
    clicker = Clicker(cfg_all(), FakeBackend(), bet_slider=slider)
    r = clicker.run(adv(Option("bet", "Bet 5.2", "bet", 5.2),
                        {"raise-1/2": 5.0}))
    check(r.mapping == "bet_slider" and slider.targets == [5.2],
          f"档位置灰应走滑杆 5.2，实际 {r.to_dict()} {slider.targets}")


def test_slider_used_when_preset_state_unknown() -> None:
    """档位状态读不准（None）也按不可点，退回滑杆。"""
    class FakeSlider:
        def __init__(self) -> None:
            self.targets: list[float] = []

        def preset_enabled(self, _preset_id):
            return None

        def set_amount(self, target):
            self.targets.append(target)
            return SimpleNamespace(status="set", error=None, reason="bet_slider",
                                   to_dict=lambda: {"status": "set"})

    slider = FakeSlider()
    clicker = Clicker(cfg_all(), FakeBackend(), bet_slider=slider)
    clicker.run(adv(Option("bet", "Bet 5.2", "bet", 5.2),
                    {"raise-1/2": 5.0}))
    check(slider.targets == [5.2],
          f"状态未知应走滑杆 5.2，实际 {slider.targets}")


def test_force_slider_skips_presets() -> None:
    """--bet-slider-only：即使档位在 5% 内且可点，也强制走滑杆。"""
    class FakeSlider:
        def __init__(self) -> None:
            self.targets: list[float] = []

        def preset_enabled(self, _preset_id):
            return True

        def set_amount(self, target):
            self.targets.append(target)
            return SimpleNamespace(status="set", error=None, reason="bet_slider",
                                   to_dict=lambda: {"status": "set",
                                                    "target_bb": target})

    slider = FakeSlider()
    backend = FakeBackend()
    clicker = Clicker(cfg_all(), backend, bet_slider=slider,
                      force_slider=True)
    r = clicker.run(adv(Option("bet", "Bet 5.2", "bet", 5.2),
                        {"raise-1/2": 5.0}))
    check(r.mapping == "bet_slider" and slider.targets == [5.2],
          f"强制滑杆应直接到 5.2，实际 {r.to_dict()} {slider.targets}")
    check(not backend.events, "强制滑杆不该再点档位")

    r2 = clicker.run(adv(Option("bet", "Bet", "bet", None)))
    check(r2.status == "skipped" and r2.reason == "amount_unknown"
          and slider.targets == [5.2],
          f"金额未知应 skip 而不是回退档位，实际 {r2.to_dict()}")


def test_clicker_routes_allin_to_slider_top() -> None:
    """主动 allin（kind=allin）走滑杆顶端，不依赖 allin 按钮。"""
    class FakeSlider:
        def __init__(self) -> None:
            self.called = 0

        def set_all_in(self):
            self.called += 1
            return SimpleNamespace(status="set", error=None,
                                   reason="bet_slider_allin",
                                   final_bb=68.0,
                                   to_dict=lambda: {"status": "set",
                                                    "final_bb": 68.0})

    slider = FakeSlider()
    clicker = Clicker(cfg_all(), FakeBackend(), bet_slider=slider)
    r = clicker.run(adv(Option("all-in", "全下 68", "allin", 68.0)))
    check(r.status == "clicked" and r.mapping == "bet_slider"
          and slider.called == 1,
          f"allin 应走滑杆顶端，实际 {r.to_dict()} called={slider.called}")
    check(r.target_bb == 68.0, f"target 应为最终 68，实际 {r.target_bb}")


def test_click_amounts_split_advice_plan_final() -> None:
    """三个额度字段分工固定：target_bb=建议额、plan_bb=计划额、final_bb=实际额。

    以前的 `target_bb` 一条路径一个意思（滑杆=建议额、档位=档位额），
    复盘「建议 27 / 实际 26.5」会被读成「建议 26.5」。
    """
    class FakeSlider:
        def preset_enabled(self, _preset_id):
            return True

        def set_amount(self, _target):
            return SimpleNamespace(
                status="set", error=None, reason="bet_slider",
                target_bb=27.0, final_bb=28.0,
                to_dict=lambda: {"status": "set", "target_bb": 27.0,
                                 "final_bb": 28.0})

    # ① 滑杆：建议 27、客户端取整后计划 27、实际落 28（+5% 内）
    clicker = Clicker(cfg_all(), FakeBackend(), bet_slider=FakeSlider())
    r = clicker.run(adv(Option("raise", "Raise 27", "raise", 27.0)))
    check((r.target_bb, r.plan_bb, r.final_bb) == (27.0, 27.0, 28.0),
          f"滑杆三额应为 27/27/28，实际 {r.to_dict()}")

    # ② 快捷档位：建议 26.8，档位 27（5% 内）→ 计划=实际=27
    clicker2 = Clicker(cfg_all(), FakeBackend(), bet_slider=FakeSlider())
    r2 = clicker2.run(adv(Option("raise", "Raise 26.8", "raise", 26.8),
                          {"raise-1": 27.0}))
    check(r2.mapping == "within_5pct",
          f"应命中档位，实际 {r2.to_dict()}")
    check((r2.target_bb, r2.plan_bb, r2.final_bb) == (26.8, 27.0, 27.0),
          f"档位三额应为 26.8/27/27，实际 {r2.to_dict()}")
    check(r2.to_dict().get("plan_bb") == 27.0,
          f"to_dict 应输出 plan_bb，实际 {r2.to_dict()}")

    # ③ 跟注没有「选额」这一步：只有建议额，不编计划/实际
    clicker3 = Clicker(cfg_all(), FakeBackend())
    r3 = clicker3.run(adv(Option("call", "Call 3", "call", 3.0)))
    check(r3.plan_bb is None and r3.final_bb is None,
          f"跟注不该有计划/实际额，实际 {r3.to_dict()}")


def test_clicker_allin_call_stays_on_call_button() -> None:
    """别人下注 > 后手时 kind=call（跟全下），走右侧 call 按钮，不碰滑杆。"""
    class FakeSlider:
        def set_all_in(self):
            raise AssertionError("跟全下不该走滑杆")

        def set_amount(self, _target):
            raise AssertionError("跟全下不该走滑杆")

    backend = FakeBackend()
    clicker = Clicker(cfg_all(), backend, bet_slider=FakeSlider())
    r = clicker.run(adv(Option("call", "跟全下 92", "call", 92.0)))
    check(r.status == "clicked" and r.action == "call"
          and [e[0] for e in backend.events] == ["move", "down", "up"],
          f"跟全下应点 call，实际 {r.to_dict()} {backend.events}")

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

def test_plan_allin_is_optional_button() -> None:
    """all-in 没有标定按钮时 skip，绝不拿别的档位冒充全下。"""
    advice = Advice("h1", "flop", Option("all-in", "全下 94BB", "allin", 94.0),
                    "test", "model")
    plan = plan_clicks(advice, cfg_all())
    check(plan.skip_reason == "missing_button:allin",
          f"缺 allin 按钮应 skip：{plan}")
    check(not plan.steps, "缺按钮时不能有点击步骤")

    configured = cfg_all(buttons={"allin": Rect(200, 0, 80, 40)})
    plan = plan_clicks(advice, configured, rng=random.Random(3))
    check(len(plan.steps) == 1 and plan.steps[0].button == "allin",
          f"有按钮时应点 allin：{plan}")
    check(plan.action == "allin", f"action 应为 allin：{plan.action}")


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


def test_bet_slider_path_bypasses_buttons() -> None:
    """注入 bet_slider 后，下注/加注直接走滑杆，不再点档位/主按钮。"""
    class FakeSlider:
        def __init__(self) -> None:
            self.target = None

        def set_amount(self, target):
            self.target = target
            return SimpleNamespace(status="set", error=None, reason="bet_slider",
                                   to_dict=lambda: {"status": "set",
                                                    "target_bb": target})

    cfg = cfg_all()
    slider = FakeSlider()
    backend = FakeBackend()
    clicker = Clicker(cfg, backend, bet_slider=slider)
    r = clicker.run(adv(Option("bet", "Bet 5", "bet", 5.0)))
    check(r.status == "clicked" and r.mapping == "bet_slider",
          f"应走滑杆，实际 {r.to_dict()}")
    check(slider.target == 5.0, f"目标额应传给滑杆，实际 {slider.target}")
    check(not backend.events, f"滑杆路径不该再点普通按钮，实际 {backend.events}")
    check(r.detail and r.detail["slider"]["status"] == "set",
          f"明细应带滑杆结果，实际 {r.detail}")

    # 跟注不归滑杆管
    r2 = clicker.run(adv(Option("call", "Call", "call", 1.0)))
    check(r2.mapping != "bet_slider" and slider.target == 5.0,
          f"跟注不该再调滑杆，实际 {r2.to_dict()}")


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
        test_choose_preset_within_tolerance,
        test_choose_preset_within_picks_closest,
        test_slider_prefers_preset_within_5pct,
        test_slider_used_when_outside_5pct_or_button_missing,
        test_slider_used_when_preset_disabled,
        test_slider_used_when_preset_state_unknown,
        test_force_slider_skips_presets,
        test_clicker_routes_allin_to_slider_top,
        test_click_amounts_split_advice_plan_final,
        test_clicker_allin_call_stays_on_call_button,
        test_choose_preset_nearest_preflop,
        test_choose_preset_tie_breaks_by_order,
        test_choose_preset_missing_amount,
        test_plan_fold_call_check,
        test_plan_raise_is_preset_only,
        test_plan_missing_preset_skips,
        test_plan_unknown_amount_skips,
        test_plan_allin_is_optional_button,
        test_clicker_executes_move_down_up,
        test_clicker_dry_run_sends_no_events,
        test_clicker_blocks_without_accessibility,
        test_clicker_releases_mouse_on_failure,
        test_bet_slider_path_bypasses_buttons,
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
