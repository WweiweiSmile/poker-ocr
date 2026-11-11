#!/usr/bin/env python3
"""下注滑杆：识别口径 + 长按/滚动/确定的闭环。

    nv tests/test_bet_slider.py

全部用合成图/假后端/假读帧，不发送真实鼠标事件，不依赖视频。
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import Advice, Option  # noqa: E402
from poker_ocr.bet_slider import (  # noqa: E402
    BetSliderController, BetSliderState, SliderSettings, WindowMapper,
    WindowRect, clicker_config_from_profile, parse_amount, parse_fraction,
    plan_slider, read_preset_enabled, read_slider, resolve_window,
)
from poker_ocr.clicker import (  # noqa: E402
    Clicker, ClickerConfig, Rect, plan_clicks,
)
from poker_ocr.profile import Profile  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


PROFILE = Profile.load(ROOT / "config" / "table_wpk_mac.json")


# ─────────────────────────── 纯解析 ───────────────────────────

def test_parse_amount() -> None:
    for text, want in (("2BB", 2.0), ("2.5 BB", 2.5), ("12bb", 12.0)):
        check(parse_amount(text) == want, f"{text!r} 应解析成 {want}，实际 {parse_amount(text)}")
    for bad in ("", "BB", "2B", "abc"):
        check(parse_amount(bad) is None, f"{bad!r} 不该解析成金额")


def test_parse_fraction() -> None:
    pct, mult = parse_fraction("2%0.5×底池")
    check(pct == 2.0 and mult == 0.5, f"应得 (2, 0.5)，实际 {(pct, mult)}")
    pct, mult = parse_fraction("12.5% 1.2x底池")
    check(pct == 12.5 and mult == 1.2, f"应得 (12.5, 1.2)，实际 {(pct, mult)}")
    check(parse_fraction("") == (None, None), "空串应返回 (None, None)")


def test_plan_direction() -> None:
    up = plan_slider(2.0, 5.0)
    check(up["direction"] == 1 and up["estimated_notches"] == 3,
          f"2→5 应向上 3 格，实际 {up}")
    down = plan_slider(5.0, 2.0)
    check(down["direction"] == -1, f"5→2 应向下，实际 {down}")
    same = plan_slider(5.0, 5.0)
    check(same["direction"] == 0 and same["reason"] == "already_at_target",
          f"同额应无需动作，实际 {same}")


# ─────────────────────────── 单帧识别 ───────────────────────────

class FakeEngine:
    """只读金额槽；返回固定 `text`。"""

    def __init__(self, text: str = "5BB") -> None:
        self.text = text

    def read_text(self, _img):
        return [(self.text, 0.99)] if self.text else []


def _felt() -> np.ndarray:
    return np.full((1666, 1468, 3), (80, 102, 2), dtype=np.uint8)


def test_read_slider_open_by_amount_bubble() -> None:
    """打开判据 = 顶部金额气泡有字，不是 track 深不深。"""
    img = _felt()
    # 1) 气泡没有字 → 关闭（哪怕 track 是黑的也算关闭）
    state = read_slider(img, PROFILE, FakeEngine(""), read_fraction=False)
    check(not state.open and state.amount_bb is None,
          f"气泡没字时应 closed，实际 {state.to_dict()}")

    # 2) 全黑帧（滑杆关闭、画面被遮暗）：旧口径 dark_frac=1.0 会误判打开
    black = np.zeros_like(img)
    state = read_slider(black, PROFILE, FakeEngine(""), read_fraction=False)
    check(not state.open,
          f"黑帧不能仅凭深色判成滑杆打开，实际 {state.to_dict()}")
    check(state.dark_frac > 0.9 and state.evidence.get("dark_open") is True,
          f"深色占比应只进证据，实际 {state.to_dict()}")

    # 3) 气泡有字（track 不深也一样）→ 打开 + 读出额
    state = read_slider(img, PROFILE, FakeEngine("5BB"), read_fraction=False)
    check(state.open and state.amount_bb == 5.0,
          f"气泡有字应 open/5，实际 {state.to_dict()}")

    # 4) 深色 track + 气泡 → 同样打开，dark_frac 记在证据里
    slot = PROFILE.global_slots["hero_bet_slider"]
    x1, y1 = int(slot[0] * 1468), int(slot[1] * 1666)
    x2, y2 = int(slot[2] * 1468), int(slot[3] * 1666)
    img[y1:y2, x1:x2] = (20, 20, 20)
    state = read_slider(img, PROFILE, FakeEngine("5BB"), read_fraction=False)
    check(state.open and state.amount_bb == 5.0 and state.dark_frac > 0.9,
          f"深色 track + 5BB 应 open/5，实际 {state.to_dict()}")


def test_read_slider_open_but_amount_unreadable() -> None:
    """轨道深色 + 气泡读不出金额：open=False + amount_unreadable，调用方必须停手。

    真实数据：纯 rec 在关闭帧上会把毛毡读成单个 `9`/`O`/`Q`，所以「有字」
    不能当打开；只有深色轨道 + 读不出额才提示「可能开着但读不准」。
    """
    img = _felt()
    slot = PROFILE.global_slots["hero_bet_slider"]
    x1, y1 = int(slot[0] * 1468), int(slot[1] * 1666)
    x2, y2 = int(slot[2] * 1468), int(slot[3] * 1666)
    img[y1:y2, x1:x2] = (20, 20, 20)
    state = read_slider(img, PROFILE, FakeEngine("O"), read_fraction=False)
    check(not state.open and state.amount_bb is None
          and state.error == "amount_unreadable",
          f"深色轨道 + 读不出额应报 amount_unreadable，实际 {state.to_dict()}")

    # 浅色毛毡上读到单个噪声字符：不能判成打开（真实关闭帧就是这样）
    felt = _felt()
    state = read_slider(felt, PROFILE, FakeEngine("O"), read_fraction=False)
    check(not state.open and state.error is None,
          f"浅色毛毡上的噪声字符不能判打开，实际 {state.to_dict()}")


def test_read_slider_detects_allin_text() -> None:
    """顶部气泡是 ALLIN / 全下 时，滑杆算是打开且已到顶。"""
    img = _felt()
    for text in ("ALL IN", "ALLIN", "All-in", "全下"):
        state = read_slider(img, PROFILE, FakeEngine(text), read_fraction=False)
        check(state.open and state.allin and state.amount_bb is None,
              f"{text!r} 应判 allin，实际 {state.to_dict()}")
        check(state.error is None,
              f"allin 不该报 amount_unreadable，实际 {state.to_dict()}")
    state = read_slider(img, PROFILE, FakeEngine("5BB"), read_fraction=False)
    check(not state.allin, f"普通金额不能判 allin，实际 {state.to_dict()}")


# ─────────────────────────── 坐标映射 ───────────────────────────

def test_window_mapper() -> None:
    mapper = WindowMapper(PROFILE, WindowRect(100, 50, 734, 833))
    slot = (0.4, 0.2, 0.6, 0.4)
    x, y = mapper.point(slot)
    check(abs(x - (100 + 0.5 * 734)) < 1e-6, f"x 映射错：{x}")
    check(abs(y - (50 + 0.3 * 833)) < 1e-6, f"y 映射错：{y}")


# ─────────────────────────── 闭环 ───────────────────────────

class FakeTable:
    def __init__(self, amount: float = 2.0, step: float = 1.0,
                 low: float = 2.0, high: float = 68.0,
                 allin: bool = True) -> None:
        self.amount = amount
        self.step = step
        self.low = low
        self.high = high
        self.allin = allin          # 到顶时顶部气泡是否显示 ALL IN
        self.open = False
        self.confirmed: float | None = None
        self.cancelled = False


def _mapper() -> WindowMapper:
    return WindowMapper(PROFILE, WindowRect(0, 0, 734, 833))


class FakeBackend:
    """滑杆后端：长按打开、点确定落注、点右侧空白关掉。

    坐标按 profile 槽位算，点和 clicker 一样的判定半径；确认/取消点都改成
    随机取点后，这里的容差必须覆盖「圆盘半径 × 70%」的抖动。
    """

    def __init__(self, table: FakeTable) -> None:
        self.table = table
        self.events: list[tuple] = []
        mapper = _mapper()
        self.confirm_xy = mapper.point(PROFILE.global_slots["hero_bet_confirm"])
        self.close_xy = mapper.point(PROFILE.global_slots["hero_bet_close"])

    def preflight(self) -> bool:
        return True

    def move(self, x: float, y: float) -> None:
        self.events.append(("move", round(x, 1), round(y, 1)))

    def _near(self, xy: tuple[float, float], point: tuple[float, float],
              tolerance: float = 45.0) -> bool:
        return abs(point[0] - xy[0]) <= tolerance \
            and abs(point[1] - xy[1]) <= tolerance

    def mouse_down(self, x: float, y: float) -> None:
        self.events.append(("down", round(x, 1), round(y, 1)))
        if not self.table.open:
            self.table.open = True                 # 长按打开
            return
        if self._near(self.confirm_xy, (x, y), 30):
            self.table.confirmed = self.table.amount  # 点确定
            self.table.open = False
            return
        if self._near(self.close_xy, (x, y)):
            self.table.open = False                # 点右侧空白关闭
            self.table.cancelled = True

    def mouse_up(self, x: float, y: float) -> None:
        self.events.append(("up", round(x, 1), round(y, 1)))

    def scroll(self, x: float, y: float, delta: int) -> None:
        self.events.append(("scroll", int(delta)))
        if self.table.open:
            self.table.amount = max(self.table.low,
                                    min(self.table.high,
                                        self.table.amount + delta * self.table.step))


def _reader_for(table: FakeTable):
    """假读帧：到顶且开启 allin 时气泡显示 ALL IN（和真客户端一致）。"""
    def reader(_img, _profile, _engine, **_kwargs):
        if not table.open:
            return BetSliderState(open=False, amount_text="", dark_frac=0.0)
        at_top = table.allin and abs(table.amount - table.high) <= 1e-9
        if at_top:
            return BetSliderState(
                open=True, amount_bb=None, amount_text="ALL IN",
                amount_score=0.99, dark_frac=0.8, allin=True)
        return BetSliderState(
            open=True,
            amount_bb=table.amount,
            amount_text=f"{table.amount:g}BB",
            amount_score=0.99,
            dark_frac=0.8,
        )
    return reader


def _controller(table: FakeTable, backend: FakeBackend, **settings):
    cfg = SliderSettings(hold_ms=0, click_ms=0, scroll_interval_ms=0,
                         settle_ms=0, poll_ms=1, **settings)
    return BetSliderController(
        PROFILE, backend, lambda: np.zeros((10, 10, 3), np.uint8),
        WindowMapper(PROFILE, WindowRect(0, 0, 734, 833)), cfg,
        reader=_reader_for(table), sleep=lambda _s: None)


def test_set_amount_scrolls_and_confirms() -> None:
    table = FakeTable(amount=2.0)
    backend = FakeBackend(table)
    result = _controller(table, backend).set_amount(5.0)
    check(result.status == "set", f"应 set，实际 {result.to_dict()}")
    check(table.confirmed == 5.0 and not table.open,
          f"应以 5BB 落注并关闭，实际 confirmed={table.confirmed} open={table.open}")
    check(result.start_bb == 2.0 and result.final_bb == 5.0,
          f"起止额应 2→5，实际 {result.start_bb}→{result.final_bb}")
    check(any(e[0] == "scroll" for e in backend.events), "应发过 scroll")


def test_batch_jump_still_lands_exact() -> None:
    """第一次滚动后学到 1BB/格，下一次会一次补 2 格；仍要精确落在目标。"""
    table = FakeTable(amount=2.0)
    backend = FakeBackend(table)
    result = _controller(table, backend).set_amount(7.0)
    check(result.status == "set" and table.confirmed == 7.0,
          f"应精确到 7，实际 {result.to_dict()} confirmed={table.confirmed}")


def test_unreachable_target_does_not_confirm() -> None:
    """目标超过后手：滑杆到顶显示 ALL IN，报错停手、绝不在 all-in 额上落注。"""
    table = FakeTable(amount=2.0, high=4.0)
    backend = FakeBackend(table)
    result = _controller(table, backend).set_amount(10.0)
    check(result.status == "error"
          and "target_exceeds_allin" in (result.error or ""),
          f"超过上限应报 target_exceeds_allin，实际 {result.to_dict()}")
    check(table.confirmed is None, f"到不了目标绝不能点确定，实际 {table.confirmed}")
    check(table.cancelled and result.evidence.get("cancel_closed") is True,
          f"失败后应点右侧空白关掉滑杆，实际 cancelled={table.cancelled} "
          f"evidence={result.evidence}")


def test_stuck_slider_does_not_confirm() -> None:
    """滚轮没反应（H5 不认滚轮 / 焦点不对）：必须报 amount_not_changing 停手。"""
    table = FakeTable(amount=2.0, step=0.0)
    backend = FakeBackend(table)
    result = _controller(table, backend).set_amount(5.0)
    check(result.status == "error" and "amount_not_changing" in (result.error or ""),
          f"额度不动应报 amount_not_changing，实际 {result.to_dict()}")
    check(table.confirmed is None, f"额度不动绝不能点确定，实际 {table.confirmed}")
    check(table.cancelled, f"失败后应点右侧空白关掉滑杆，实际 {table.cancelled}")


def test_oscillating_target_does_not_confirm() -> None:
    """3BB/格、目标 4BB：2→5→2→5 反复，必须停手，不能近似落注。"""
    table = FakeTable(amount=2.0, step=3.0)
    backend = FakeBackend(table)
    result = _controller(table, backend).set_amount(4.0)
    check(result.status == "error" and "oscillating" in (result.error or ""),
          f"应报 oscillating，实际 {result.to_dict()}")
    check(table.confirmed is None, f"振荡时绝不能点确定，实际 {table.confirmed}")


def test_keep_held_releases_before_confirm() -> None:
    table = FakeTable(amount=2.0)
    backend = FakeBackend(table)
    result = _controller(table, backend, release_after_open=False).set_amount(3.0)
    check(result.status == "set", f"应 set，实际 {result.to_dict()}")
    scroll_i = next(i for i, e in enumerate(backend.events) if e[0] == "scroll")
    up_after = [i for i, e in enumerate(backend.events)
                if e[0] == "up" and i > scroll_i]
    downs = [i for i, e in enumerate(backend.events) if e[0] == "down"]
    check(up_after and downs and up_after[0] < downs[-1],
          f"按住模式必须先松手再点确定，实际 {backend.events}")


def test_dry_run_sends_no_events() -> None:
    table = FakeTable(amount=2.0)
    table.open = True
    backend = FakeBackend(table)
    cfg = SliderSettings(hold_ms=0, click_ms=0, scroll_interval_ms=0,
                         settle_ms=0, poll_ms=1)
    controller = BetSliderController(
        PROFILE, backend, lambda: np.zeros((10, 10, 3), np.uint8),
        WindowMapper(PROFILE, WindowRect(0, 0, 734, 833)), cfg,
        reader=_reader_for(table), sleep=lambda _s: None, dry_run=True)
    result = controller.set_amount(5.0)
    check(result.status == "dry_run", f"应 dry_run，实际 {result.to_dict()}")
    check(not backend.events, f"dry-run 不该发事件，实际 {backend.events}")
    check(result.evidence["plan"]["direction"] == 1,
          f"计划应向上，实际 {result.evidence}")



def test_clicker_uses_slider_for_bet() -> None:
    """Clicker 注入滑杆后，下注建议走滑杆而不是快捷档位。"""
    table = FakeTable(amount=2.0)
    backend = FakeBackend(table)
    controller = _controller(table, backend)
    cfg = ClickerConfig(buttons={"fold": Rect(0, 0, 10, 10),
                                 "call": Rect(10, 10, 10, 10)})
    clicker = Clicker(cfg, backend, bet_slider=controller,
                      sleep=lambda _s: None)
    advice = Advice("h1", "flop", Option("bet", "Bet 5", "bet", 5.0),
                    "reason", "model")
    result = clicker.run(advice)
    check(result.status == "clicked" and result.mapping == "bet_slider",
          f"应走滑杆，实际 {result.to_dict()}")
    check(result.detail["slider"]["final_bb"] == 5.0,
          f"滑杆明细应到 5BB，实际 {result.detail}")
    check(table.confirmed == 5.0, f"应真的落注 5BB，实际 {table.confirmed}")


def test_read_preset_enabled_white_vs_gray() -> None:
    img = _felt()
    slot = PROFILE.global_slots["bet_preset_1_3"]
    x1, y1 = int(slot[0] * 1468), int(slot[1] * 1666)
    x2, y2 = int(slot[2] * 1468), int(slot[3] * 1666)
    # 白色文字块 → 可点
    img[y1 + 10:y2 - 10, x1 + 10:x2 - 10] = (255, 255, 255)
    st = read_preset_enabled(img, PROFILE, "raise-1/3")
    check(st.enabled is True, f"白字应判可点，实际 {st.to_dict()}")
    # 灰色块 → 置灰
    img[y1 + 10:y2 - 10, x1 + 10:x2 - 10] = (150, 150, 150)
    st = read_preset_enabled(img, PROFILE, "raise-1/3")
    check(st.enabled is False, f"灰字应判置灰，实际 {st.to_dict()}")
    check(read_preset_enabled(img, PROFILE, "raise-9").enabled is None,
          "未知档位应返回 None")


def test_resolve_window_coords_and_dict() -> None:
    check(resolve_window("-1,34,736,833") == WindowRect(-1, 34, 736, 833),
          "x,y,w,h 应直接解析")
    check(resolve_window({"x": 10, "y": 20, "w": 300, "h": 400})
          == WindowRect(10, 20, 300, 400), "dict 应直接解析")


def test_clicker_config_from_profile_maps_slots() -> None:
    """按钮坐标由 profile 槽位 + window 计算，不依赖 clicker_local。"""
    window = WindowRect(100, 50, 734, 833)
    cfg = clicker_config_from_profile(PROFILE, window)
    cfg.validate()
    check(set(cfg.buttons) == {"fold", "call", "raise-1/3", "raise-1/2",
                               "raise-2/3", "raise-1", "raise-1.2"},
          f"应生成 fold/call/5 档位，实际 {sorted(cfg.buttons)}")
    fold = cfg.buttons["fold"]
    check(fold.x > 100 and fold.y > 50 and fold.x2 < 834 and fold.y2 < 883,
          f"fold 应落在 window 内，实际 {fold}")
    # plan_clicks 用派生按钮时应产出窗口内的点
    advice = Advice("h1", "flop", Option("fold", "Fold", "fold"),
                    "reason", "model")
    plan = plan_clicks(advice, cfg)
    check(len(plan.steps) == 1 and plan.steps[0].button == "fold",
          f"应计划点 fold，实际 {plan}")
    point = plan.steps[0]
    check(100 <= point.x <= 834 and 50 <= point.y <= 883,
          f"点击点应在 window 内，实际 {(point.x, point.y)}")
    # 标量参数从 base 继承
    base = ClickerConfig(buttons={"fold": Rect(0, 0, 1, 1),
                                  "call": Rect(0, 0, 1, 1)},
                         inset_ratio=0.2, preset_tolerance=0.03)
    cfg2 = clicker_config_from_profile(PROFILE, window, base=base)
    check(cfg2.inset_ratio == 0.30 and cfg2.preset_tolerance == 0.03,
          "圆钮内缩应改成 0.30，base 的 tolerance 应保留")
    check(all(r.shape == "disc" for r in cfg2.buttons.values()),
          f"按钮应为 disc 形状，实际 {[(k, r.shape) for k, r in cfg2.buttons.items()]}")


def test_disc_point_stays_inside_circle() -> None:
    """圆形按钮即使槽位不是正方形，取样也必须落在正圆内。"""
    rect = Rect(0, 0, 80, 95, shape="disc")
    rng = random.Random(9)
    for _ in range(1000):
        x, y = rect.sample_point(rng, 0.30)
        dist = ((x - rect.cx) ** 2 + (y - rect.cy) ** 2) ** 0.5
        check(dist <= 80 / 2 * (1 - 0.30) + 1e-9,
              f"disc 取样跑出圆：dist={dist:.2f}")


def test_set_all_in_scrolls_to_top_and_confirms() -> None:
    """set_all_in 一直向上滚到顶部气泡出现 ALL IN，然后确认。"""
    table = FakeTable(amount=2.0, step=3.0, high=68.0)
    backend = FakeBackend(table)
    result = _controller(table, backend).set_all_in()
    check(result.status == "set", f"应 set，实际 {result.to_dict()}")
    check(table.confirmed == 68.0 and not table.open,
          f"应在 68BB 顶端落注，实际 confirmed={table.confirmed} open={table.open}")
    check(result.evidence.get("allin_text") == "ALL IN",
          f"证据里应留下 ALL IN 字样，实际 {result.evidence}")
    check(result.final_bb == 62.0,
          f"final_bb 是 ALL IN 前最后一次读到的额（62：62 → 一批滚到顶），"
          f"实际 {result.final_bb}")


def test_unreadable_bubble_does_not_press_again() -> None:
    """深色轨道 + 气泡读不出额：不能再长按主按钮（等于按当前额确认），
    但要把可能开着的滑杆点空白关掉。"""
    table = FakeTable(amount=2.0)
    table.open = True                      # 上一轮失败留下的滑杆
    backend = FakeBackend(table)
    controller = _controller(table, backend)
    controller.reader = lambda *_a, **_k: BetSliderState(
        open=False, amount_bb=None, amount_text="2B8", dark_frac=0.8,
        error="amount_unreadable")
    result = controller.set_amount(5.0)
    check(result.status == "error" and "amount_unreadable" in (result.error or ""),
          f"应报 amount_unreadable，实际 {result.to_dict()}")
    check(table.confirmed is None,
          f"读不出额绝不能按当前额确认，实际 confirmed={table.confirmed}")
    check(table.cancelled and result.evidence.get("cancel_closed") is True,
          f"应点右侧空白把滑杆关掉，实际 cancelled={table.cancelled} "
          f"evidence={result.evidence}")


def test_allin_requires_the_client_text() -> None:
    """额度停在顶端但气泡没有 ALL IN 字样：不能当 all-in 落注。

    这正是旧口径的 bug：拉不动/到顶都表现为「额度不再变化」，旧代码直接点
    确定，在 2BB 这种中间额上下注却记成全下。
    """
    table = FakeTable(amount=2.0, step=0.0, high=68.0, allin=False)
    backend = FakeBackend(table)
    result = _controller(table, backend).set_all_in()
    check(result.status == "error" and "allin_not_reached" in (result.error or ""),
          f"没有 ALLIN 字样应报 allin_not_reached，实际 {result.to_dict()}")
    check(table.confirmed is None,
          f"没读到 ALLIN 绝不能点确定，实际 {table.confirmed}")
    check(table.cancelled, f"失败后应点右侧空白关掉滑杆，实际 {table.cancelled}")


def test_allin_needs_two_consecutive_reads() -> None:
    """只闪一帧 ALL IN 不算：连续两次读到才落注。"""
    table = FakeTable(amount=2.0, step=3.0, high=68.0)
    backend = FakeBackend(table)
    controller = _controller(table, backend)
    base_reader = controller.reader
    calls = {"n": 0}

    def flaky(img, profile, engine, **kwargs):
        state = base_reader(img, profile, engine, **kwargs)
        if state.allin:
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一次读到 ALL IN 后立刻回退成普通读数（模拟 OCR 抖动）
                return BetSliderState(open=True, amount_bb=table.high,
                                      amount_text=f"{table.high:g}BB",
                                      amount_score=0.99, dark_frac=0.8)
        return state

    controller.reader = flaky
    result = controller.set_all_in()
    check(result.status == "set" and table.confirmed == 68.0,
          f"抖动后仍应读到两次 ALL IN 落注，实际 {result.to_dict()} "
          f"confirmed={table.confirmed}")
    check(calls["n"] >= 2, f"ALL IN 至少要读到两次，实际 {calls['n']}")




# ─────────────────────────── 拖手柄（新 UI） ───────────────────────────

class DragTable:
    """合成滑杆：按住橙色手柄上下拖 → 额度变化；滚轮无效。"""

    def __init__(self, amount: float = 1.0, low: float = 1.0,
                 high: float = 98.5, px_per_bb: float = 6.0,
                 allin: bool = True) -> None:
        self.amount = amount
        self.low = low
        self.high = high
        self.px_per_bb = px_per_bb
        self.allin = allin          # 到顶时顶部气泡是否显示 ALL IN
        self.open = False
        self.confirmed: float | None = None
        self.cancelled = False
        self.scrolls = 0


class FakeDragBackend:
    def __init__(self, table: DragTable, confirm_xy, close_xy) -> None:
        self.table = table
        self.confirm_xy = confirm_xy
        self.close_xy = close_xy
        self.events: list[tuple] = []
        self._down: tuple[float, float] | None = None

    def preflight(self) -> bool:
        return True

    def move(self, x: float, y: float) -> None:
        self.events.append(("move", round(x, 1), round(y, 1)))

    def mouse_down(self, x: float, y: float) -> None:
        self.events.append(("down", round(x, 1), round(y, 1)))
        if not self.table.open:
            self.table.open = True
            return
        if abs(x - self.confirm_xy[0]) <= 30 and abs(y - self.confirm_xy[1]) <= 30:
            self.table.confirmed = self.table.amount
            self.table.open = False
            return
        if abs(x - self.close_xy[0]) <= 45 and abs(y - self.close_xy[1]) <= 45:
            self.table.open = False
            self.table.cancelled = True
            return
        self._down = (x, y)

    def mouse_up(self, x: float, y: float) -> None:
        self.events.append(("up", round(x, 1), round(y, 1)))
        if self._down is None:
            return
        dy = y - self._down[1]
        self.table.amount = max(
            self.table.low,
            min(self.table.high,
                self.table.amount - dy / self.table.px_per_bb))
        self._down = None

    def scroll(self, x: float, y: float, delta: int) -> None:
        self.table.scrolls += 1


class StuckDragBackend(FakeDragBackend):
    """拖了但额度不变：模拟 H5 不认拖动 / 橙色手柄位置找错。"""

    def mouse_up(self, x: float, y: float) -> None:
        self.events.append(("up", round(x, 1), round(y, 1)))
        self._down = None            # 不改额度


def _mk_drag(table, **settings):
    mapper = WindowMapper(PROFILE, WindowRect(0, 0, 734, 833))
    confirm_xy = mapper.point(PROFILE.global_slots["hero_bet_confirm"])
    close_xy = mapper.point(PROFILE.global_slots["hero_bet_close"])
    backend = FakeDragBackend(table, confirm_xy, close_xy)
    cfg = SliderSettings(hold_ms=0, click_ms=0, scroll_interval_ms=0,
                         settle_ms=0, poll_ms=1, control="drag", **settings)
    controller = BetSliderController(
        PROFILE, backend, lambda: np.zeros((10, 10, 3), np.uint8), mapper, cfg,
        reader=_reader_for(table), sleep=lambda _s: None,
        handle_locator=lambda _img: (0.5, 0.75))
    return controller, backend


def test_drag_reaches_target_and_confirms() -> None:
    table = DragTable(amount=1.0, high=98.0)
    controller, backend = _mk_drag(table)
    result = controller.set_amount(5.5)
    check(result.status == "set", f"应 set，实际 {result.to_dict()}")
    check(table.confirmed == 5.5, f"应以 5.5BB 落注，实际 {table.confirmed}")
    check(result.drags >= 1 and table.scrolls == 0,
          f"应只拖手柄、不发滚轮，实际 drags={result.drags} scrolls={table.scrolls}")
    check(any(e[0] == "down" for e in backend.events)
          and any(e[0] == "up" for e in backend.events),
          f"拖动手势应包含按下/抬起，实际 {backend.events}")


def test_drag_overshoot_then_corrects() -> None:
    """初始 px/BB 估大了：第一把拖过头，必须反向拖回来。"""
    table = DragTable(amount=1.0, high=98.0, px_per_bb=6.0)
    controller, _ = _mk_drag(table, drag_px_per_bb=24.0)
    result = controller.set_amount(5.5)
    check(result.status == "set", f"应 set，实际 {result.to_dict()}")
    check(table.confirmed == 5.5, f"过冲后应回到 5.5，实际 {table.confirmed}")
    check(result.drags >= 2, f"应至少拖两次（上去再下来），实际 {result.drags}")


def test_drag_unreachable_does_not_confirm_and_closes_outside() -> None:
    # allin=False：客户端到顶也不显示 ALL IN，只能靠「额度不再变化」判停手
    table = DragTable(amount=1.0, high=4.0, allin=False)
    controller, _ = _mk_drag(table)
    result = controller.set_amount(10.0)
    check(result.status == "error" and "amount_not_changing" in (result.error or ""),
          f"到不了上限应报 amount_not_changing，实际 {result.to_dict()}")
    check(table.confirmed is None, f"到不了目标绝不能落注，实际 {table.confirmed}")
    check(table.cancelled and result.evidence.get("cancel_closed") is True,
          f"Esc 无效后应点右侧空白关掉，实际 cancelled={table.cancelled} "
          f"evidence={result.evidence}")


def test_drag_handle_missing_closes_outside() -> None:
    table = DragTable(amount=1.0)
    mapper = WindowMapper(PROFILE, WindowRect(0, 0, 734, 833))
    backend = FakeDragBackend(
        table, mapper.point(PROFILE.global_slots["hero_bet_confirm"]),
        mapper.point(PROFILE.global_slots["hero_bet_close"]))
    controller = BetSliderController(
        PROFILE, backend, lambda: np.zeros((10, 10, 3), np.uint8), mapper,
        SliderSettings(hold_ms=0, click_ms=0, scroll_interval_ms=0,
                       settle_ms=0, poll_ms=1, control="drag"),
        reader=_reader_for(table), sleep=lambda _s: None,
        handle_locator=lambda _img: None)
    result = controller.set_amount(5.0)
    check(result.status == "error" and "handle_not_found" in (result.error or ""),
          f"找不到手柄应报 handle_not_found，实际 {result.to_dict()}")
    check(table.confirmed is None, f"找不到手柄绝不能落注，实际 {table.confirmed}")
    check(table.cancelled and result.evidence.get("cancel_closed") is True,
          f"应点右侧空白关掉滑杆，实际 cancelled={table.cancelled}")


def test_drag_all_in_stops_at_top() -> None:
    table = DragTable(amount=2.0, high=30.0)
    controller, _ = _mk_drag(table)
    result = controller.set_all_in()
    check(result.status == "set", f"应 set，实际 {result.to_dict()}")
    check(table.confirmed == 30.0,
          f"应拖到顶端 30BB 再落注，实际 {table.confirmed}")


def test_drag_all_in_stuck_does_not_confirm() -> None:
    """拖不动（H5 不认拖动/手柄找错）：没有 ALL IN 字样就绝不落注。"""
    table = DragTable(amount=2.0, high=30.0)
    mapper = WindowMapper(PROFILE, WindowRect(0, 0, 734, 833))
    backend = StuckDragBackend(
        table, mapper.point(PROFILE.global_slots["hero_bet_confirm"]),
        mapper.point(PROFILE.global_slots["hero_bet_close"]))
    controller = BetSliderController(
        PROFILE, backend, lambda: np.zeros((10, 10, 3), np.uint8), mapper,
        SliderSettings(hold_ms=0, click_ms=0, scroll_interval_ms=0,
                       settle_ms=0, poll_ms=1, control="drag"),
        reader=_reader_for(table), sleep=lambda _s: None,
        handle_locator=lambda _img: (0.5, 0.75))
    result = controller.set_all_in()
    check(result.status == "error" and "allin_not_reached" in (result.error or ""),
          f"拖不动应报 allin_not_reached，实际 {result.to_dict()}")
    check(table.confirmed is None,
          f"拖不动绝不能点确定，实际 {table.confirmed}")
    check(table.cancelled, f"失败后应点右侧空白关掉滑杆，实际 {table.cancelled}")


def _button_controller(rng_seed: int = 7) -> BetSliderController:
    table = FakeTable()
    return BetSliderController(
        PROFILE, FakeBackend(table), lambda: np.zeros((10, 10, 3), np.uint8),
        _mapper(), SliderSettings(), reader=_reader_for(table),
        sleep=lambda _s: None, rng=random.Random(rng_seed))


def test_random_click_points_stay_in_disc() -> None:
    """按钮点击点：圆盘内随机，半径 = 原半径 × 70%（不是几何中心）。"""
    controller = _button_controller()
    slot = PROFILE.global_slots["hero_bet_confirm"]
    rect = _mapper().rect(slot, shape="disc")
    radius = min(rect.w, rect.h) / 2.0 * 0.7
    points = [controller._button_point("hero_bet_confirm") for _ in range(200)]
    for x, y in points:
        dist = math.hypot(x - rect.cx, y - rect.cy)
        check(dist <= radius + 1e-6,
              f"点击点超出 70% 半径：{dist:.2f} > {radius:.2f}")
    check(len({(round(x, 3), round(y, 3)) for x, y in points}) > 20,
          "点击点应该是随机的，不能每次都在同一点")


def test_settings_reject_bad_config() -> None:
    """配置文件逐个校验：类型/范围/枚举写错都直接报错，不静默退化。"""
    raw = json.loads((ROOT / "config" / "bet_slider_mac.json")
                     .read_text(encoding="utf-8"))
    settings = SliderSettings.from_config(raw, source="bet_slider_mac.json")
    check(settings.control == "drag" and settings.total_timeout_ms > 0,
          f"仓库自带的滑杆配置应能加载，实际 {settings}")
    bad_cases = [({"control": "dragg"}, "control"),
                 ({"hold_ms": "650ms"}, "hold_ms"),
                 ({"release_after_open": "false"}, "release_after_open"),
                 ({"tolerance_bb": -1}, "tolerance_bb"),
                 ({"max_steps": 0}, "max_steps"),
                 ({"close_slot": ""}, "close_slot")]
    for bad, needle in bad_cases:
        try:
            SliderSettings.from_config(bad)
        except ValueError as exc:
            check(needle in str(exc),
                  f"{bad} 的错误信息应提到 {needle}，实际：{exc}")
        else:
            check(False, f"{bad} 应该报错（不能静默默认）")


def main() -> int:
    tests = [
        test_parse_amount, test_parse_fraction, test_plan_direction,
        test_read_slider_open_by_amount_bubble,
        test_read_slider_open_but_amount_unreadable,
        test_read_slider_detects_allin_text,
        test_window_mapper, test_resolve_window_coords_and_dict,
        test_clicker_config_from_profile_maps_slots,
        test_disc_point_stays_inside_circle,
        test_random_click_points_stay_in_disc,
        test_settings_reject_bad_config,
        test_set_amount_scrolls_and_confirms, test_batch_jump_still_lands_exact,
        test_unreachable_target_does_not_confirm,
        test_stuck_slider_does_not_confirm,
        test_oscillating_target_does_not_confirm,
        test_keep_held_releases_before_confirm, test_dry_run_sends_no_events,
        test_clicker_uses_slider_for_bet,
        test_set_all_in_scrolls_to_top_and_confirms,
        test_unreadable_bubble_does_not_press_again,
        test_allin_requires_the_client_text,
        test_allin_needs_two_consecutive_reads,
        test_drag_reaches_target_and_confirms, test_drag_overshoot_then_corrects,
        test_drag_unreachable_does_not_confirm_and_closes_outside,
        test_drag_handle_missing_closes_outside, test_drag_all_in_stops_at_top,
        test_drag_all_in_stuck_does_not_confirm,
        test_drag_rounds_target_to_client_grid,
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
    print("\n✅ 下注滑杆全部通过")
    return 0





def test_drag_rounds_target_to_client_grid() -> None:
    """AI 的 1.33BB 落不到整数 BB 的滑杆上：先取到最近档，避免 1↔2 振荡。"""
    table = DragTable(amount=1.0, high=98.0)
    controller, _ = _mk_drag(table, round_to_bb=1.0)
    result = controller.set_amount(1.33)
    check(result.status == "set", f"应 set，实际 {result.to_dict()}")
    check(table.confirmed == 1.0,
          f"应落 1BB（最近的整 BB），实际 {table.confirmed}")
    check(result.evidence.get("requested_bb") == 1.33,
          f"证据里应保留 AI 原始目标，实际 {result.evidence}")
    check(result.evidence.get("snapped_to_bb") == 1.0,
          f"证据里应记录取整后的目标，实际 {result.evidence}")


if __name__ == "__main__":
    sys.exit(main())
