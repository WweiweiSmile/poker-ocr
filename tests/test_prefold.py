#!/usr/bin/env python3
"""翻前预点弃牌：恒弃集合、排除名单、预点状态机。

    nv tests/test_prefold.py

全部纯逻辑 + 假点击器 + 假时钟，不点鼠标、不读图。重点是把「点错」的反例
跑一遍：AA 绝不预弃、牌没读稳不点、上一手残影不点、我自己取消过就不补点、
看不到颜色时只点一次（第二次会把预选取消掉）、重试有上限、关掉后不再点。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.preflop_ranges import always_fold  # noqa: E402
from poker_ocr.prefold import (  # noqa: E402
    MODE_DRY, MODE_LIVE, MODE_OFF, PrefoldManager, derived_trash_keys,
    iter_hand_keys, load_exclude,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


class FakeClick:
    def __init__(self, status: str = "clicked", reason: str | None = None):
        self.status = status
        self.reason = reason


class FakeClicker:
    def __init__(self, status: str = "clicked"):
        self.pressed: list[str] = []
        self.status = status

    def press(self, button: str) -> FakeClick:
        self.pressed.append(button)
        return FakeClick(self.status)


class Clock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def gray() -> dict:
    return {"available": True, "red": 0.0, "blue": 0.0}


def red() -> dict:
    return {"available": True, "red": 0.62, "blue": 0.0}


def blind() -> dict:
    return {"available": False, "why": "profile 里没有 hero_fold_button 槽位"}


def feed(mgr: PrefoldManager, *, clock: Clock, hand_id: str = "h1",
         cards=("7h", "2c"), street: str = "preflop", reliable: bool = True,
         hero_turn: bool | None = False, fold: dict | None = None):
    return mgr.update(hand_id=hand_id, street=street, hero_cards=cards,
                      cards_reliable=reliable, hero_turn=hero_turn,
                      fold=fold if fold is not None else gray())


# ── 集合与配置 ────────────────────────────────────────────────
def test_derived_keys() -> None:
    """恒弃集合：49 手非同花；没有对子、没有同花（同花能便宜看翻牌）。

    数字是 `always_fold` 的实测结果，不是拍脑袋：这个集合变了，等于翻前
    范围变了，必须显式改这条断言 + `config/prefold_trash.json` 的说明。
    """
    keys = derived_trash_keys()
    check(len(keys) == 49, f"恒弃集合应为 49 手，实际 {len(keys)}")
    for key in keys:
        check(len(key) == 3 and key.endswith("o"), f"{key} 应该是非同花 key")
        check(key[0] != key[1], f"{key} 不该是对子")
    check("72o" in keys, "72o 应在恒弃集合里")
    check("A2o" in keys, "A2o 也在（维护者拍板全留）")
    for strong in ("AA", "KK", "AKs", "AKo", "72s", "T9s", "22"):
        check(strong not in keys, f"{strong} 不该被预弃")
    check(len(iter_hand_keys()) == 169, "169 个 key 的展开不对")
    # 集合与 always_fold 同源，不能各说各话
    for key in iter_hand_keys():
        codes = (f"{key[0]}h", f"{key[1]}{'h' if key.endswith('s') else 'c'}")
        check((key in keys) == always_fold(codes), f"{key} 与 always_fold 不一致")


def test_load_exclude() -> None:
    """排除名单：文件不存在 = 不排除；写错就 fail fast，不静默生效。"""
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.json"
        check(load_exclude(missing) == frozenset(), "文件不存在应返回空集合")

        good = Path(tmp) / "good.json"
        good.write_text(json.dumps({"version": 1, "exclude": ["a2o", "K2O"]}),
                        encoding="utf-8")
        check(load_exclude(good) == {"A2o", "K2o"}, "大小写应被归一化")

        bad_shape = Path(tmp) / "bad_shape.json"
        bad_shape.write_text(json.dumps(["A2o"]), encoding="utf-8")
        try:
            load_exclude(bad_shape)
        except ValueError:
            pass
        else:
            check(False, "顶层不是对象时应报错")

        bad_key = Path(tmp) / "bad_key.json"
        bad_key.write_text(json.dumps({"exclude": ["ZZo"]}), encoding="utf-8")
        try:
            load_exclude(bad_key)
        except ValueError:
            pass
        else:
            check(False, "非法 key 应报错")

        # 写 AA 这种「本来就不该预弃」的牌是配置错误，不能静默通过
        wrong = Path(tmp) / "aa.json"
        wrong.write_text(json.dumps({"exclude": ["AA"]}), encoding="utf-8")
        try:
            load_exclude(wrong)
        except ValueError:
            pass
        else:
            check(False, "排除一个不在恒弃集合里的 key 应报错")

        # 仓库里那份默认配置必须能读
        check(load_exclude(ROOT / "config" / "prefold_trash.json") == frozenset(),
              "config/prefold_trash.json 默认应为空名单")


# ── 状态机 ────────────────────────────────────────────────────
def test_armed_once_then_confirmed() -> None:
    """别人行动时点一次；看到红就确认，之后绝不再点。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    ev = feed(mgr, clock=clock)
    check(ev is not None and ev.kind == "armed", f"应预点一次：{ev}")
    check(clicker.pressed == ["fold"], f"应只按一次 fold：{clicker.pressed}")
    check(mgr.active("h1"), "预点后这一手应被接管（压建议）")

    ev = feed(mgr, clock=clock, fold=red())
    check(ev is not None and ev.kind == "confirmed", f"看到红应确认：{ev}")
    check(mgr.count == 1, "确认应计入 count")
    for _ in range(3):
        clock.tick(1.0)
        feed(mgr, clock=clock, fold=red())
    check(clicker.pressed == ["fold"], f"确认后不许再点：{clicker.pressed}")


def test_no_click_when_too_strong() -> None:
    """AA/KK/AKs 这类绝不预弃 —— 点错一下就是白扔一手大牌。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    for cards in (("Ah", "Ac"), ("Kh", "Kc"), ("Ah", "Kh"), ("7h", "6h")):
        ev = feed(mgr, clock=clock, cards=cards)
        check(ev is None, f"{cards} 不该预点：{ev}")
    check(clicker.pressed == [], f"不该有任何点击：{clicker.pressed}")
    check(not mgr.active("h1"), "没预点的牌不该接管")


def test_unreliable_cards_and_wrong_street() -> None:
    """牌没读稳 / 不是翻前 / 只读到一张 → 一律不点。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    check(feed(mgr, clock=clock, reliable=False) is None, "牌不可靠不该点")
    check(feed(mgr, clock=clock, street="flop") is None, "翻后不该点")
    check(feed(mgr, clock=clock, cards=("7h",)) is None, "只读到一张不该点")
    check(clicker.pressed == [], f"不该有任何点击：{clicker.pressed}")


def test_new_hand_resets() -> None:
    """换手（hand_id 变了）应重新开始，上一手的「已点过」不能带过来。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    feed(mgr, clock=clock)
    clock.tick(0.1)
    check(feed(mgr, clock=clock, hero_turn=False) is None,
          "同一手不应该连点两次")
    clock.tick(1.0)
    ev = feed(mgr, clock=clock, hand_id="h2")
    check(ev is not None and ev.kind == "armed", f"换手后应重新预点：{ev}")
    check(clicker.pressed == ["fold", "fold"], f"两手各一次：{clicker.pressed}")


def test_manual_cancel_stops_rearm() -> None:
    """我自己取消（红→灰）之后，这一手不再补点 —— 尊重手动选择。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    feed(mgr, clock=clock)
    feed(mgr, clock=clock, fold=red())
    clock.tick(0.5)
    ev = feed(mgr, clock=clock, fold=gray())
    check(ev is not None and ev.kind == "released", f"红消失应记 released：{ev}")
    check(not mgr.active("h1"), "取消后这一手不再接管（建议要照给）")
    for _ in range(3):
        clock.tick(1.0)
        feed(mgr, clock=clock, fold=gray())
    check(clicker.pressed == ["fold"], f"取消后不许补点：{clicker.pressed}")
    # 取消后再轮到我也不许替我弃：取消就是「这一手我自己打」
    ev = feed(mgr, clock=clock, hero_turn=True, fold=gray())
    check(ev is None, f"取消后轮到我也不该自动弃：{ev}")
    check(clicker.pressed == ["fold"], f"取消后仍不许点：{clicker.pressed}")


def test_blind_mode_clicks_at_most_once() -> None:
    """看不到按钮颜色时最多点一次：盲点第二下会把预选取消掉。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    feed(mgr, clock=clock, fold=blind())
    for _ in range(5):
        clock.tick(1.0)
        feed(mgr, clock=clock, fold=blind())
    check(clicker.pressed == ["fold"], f"看不见颜色只许点一次：{clicker.pressed}")


def test_retry_cap_and_give_up() -> None:
    """看得到灰（没挂上）时可再试一次；两次都不红就放弃并告警。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    feed(mgr, clock=clock, fold=gray())
    clock.tick(0.05)
    check(feed(mgr, clock=clock, fold=gray()) is None, "400ms 内不许连点")
    clock.tick(0.4)
    feed(mgr, clock=clock, fold=gray())
    check(len(clicker.pressed) == 2, f"应重试到两次：{clicker.pressed}")
    events = []
    for _ in range(6):
        clock.tick(1.0)
        ev = feed(mgr, clock=clock, fold=gray())
        if ev is not None:
            events.append(ev.kind)
    check("gave_up" in events, f"两次都没红应放弃：{events}")
    check(clicker.pressed == ["fold", "fold"], f"放弃后不许再点：{clicker.pressed}")


def test_on_turn_without_preselection_folds() -> None:
    """轮到我但预选没挂上（含我是第一个行动）→ 直接弃，不等建议。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    ev = feed(mgr, clock=clock, hero_turn=True)
    check(ev is not None and ev.kind == "fold_now", f"应直接弃牌：{ev}")
    check(clicker.pressed == ["fold"], f"应点一次 fold：{clicker.pressed}")


def test_on_turn_with_preselection_does_not_click() -> None:
    """预选还挂着时轮到我：客户端会自己执行，再点一下等于取消。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    feed(mgr, clock=clock)
    clock.tick(0.5)
    ev = feed(mgr, clock=clock, hero_turn=True, fold=red())
    check(ev is None, f"预选挂着时轮到我什么都不该做：{ev}")
    check(clicker.pressed == ["fold"], f"不该有第二次点击：{clicker.pressed}")


def test_confirmed_then_turn_does_not_double_click() -> None:
    """预选确认过、轮到我时红已经消失 = 客户端执行完了，别再补一下。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    feed(mgr, clock=clock)
    feed(mgr, clock=clock, fold=red())
    ev = feed(mgr, clock=clock, hero_turn=True, fold=gray())
    check(ev is None, f"已确认后轮到我且红消失，不该再点：{ev}")
    check(clicker.pressed == ["fold"], f"只许预点那一次：{clicker.pressed}")


def test_gave_up_still_folds_on_turn() -> None:
    """预点两次都没红就放弃预点；但真轮到我时仍直接弃（且只弃一次）。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    feed(mgr, clock=clock, fold=gray())
    clock.tick(0.5)
    feed(mgr, clock=clock, fold=gray())
    for _ in range(6):
        clock.tick(1.0)
        ev = feed(mgr, clock=clock, fold=gray())
        if ev is not None and ev.kind == "gave_up":
            break
    clock.tick(1.0)
    ev = feed(mgr, clock=clock, hero_turn=True, fold=gray())
    check(ev is not None and ev.kind == "fold_now", f"轮到我应直接弃：{ev}")
    clock.tick(1.0)
    ev = feed(mgr, clock=clock, hero_turn=True, fold=gray())
    check(ev is None, f"弃过之后不许再按：{ev}")
    check(len(clicker.pressed) == 3, f"两次预点 + 一次弃牌：{clicker.pressed}")


def test_excluded_key_not_armed() -> None:
    """黑名单里的手：不预点，也不接管建议。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock,
                         exclude=frozenset({"72o"}))
    check(feed(mgr, clock=clock) is None, "被排除的手不该预点")
    check(clicker.pressed == [], "不该有点击")
    ev = feed(mgr, clock=clock, cards=("3h", "2c"))
    check(ev is not None, f"没被排除的 32o 仍应预点：{ev}")


def test_dry_run_never_clicks() -> None:
    """dry-run：只记一次，一个鼠标事件都不发。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_DRY, clicker=clicker, clock=clock)
    ev = feed(mgr, clock=clock)
    check(ev is not None and ev.kind == "armed", f"dry-run 仍应记录：{ev}")
    for _ in range(3):
        clock.tick(1.0)
        feed(mgr, clock=clock)
    feed(mgr, clock=clock, hero_turn=True)
    check(clicker.pressed == [], f"dry-run 不许点：{clicker.pressed}")


def test_off_mode_is_inert() -> None:
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_OFF, clicker=clicker, clock=clock)
    check(feed(mgr, clock=clock) is None, "off 不该预点")
    check(feed(mgr, clock=clock, hero_turn=True) is None, "off 不该直接弃")
    check(clicker.pressed == [], "off 不该有任何点击")
    check(not mgr.active("h1"), "off 不该接管")


def test_switch_off_cancels_preselection() -> None:
    """运行中关掉预点：已确认挂上的要按一下取消，取消失败要告警。"""
    clock = Clock()
    clicker = FakeClicker()
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    feed(mgr, clock=clock)
    feed(mgr, clock=clock, fold=red())
    check(clicker.pressed == ["fold"], "确认时不该再点")

    warnings = mgr.set_mode(MODE_OFF)
    check(len(warnings) == 1 and "取消" in warnings[0],
          f"已确认时应提示会取消：{warnings}")
    # 开关是 Web 线程切的，鼠标事件留给主循环下一帧，避免和点击撞车
    check(clicker.pressed == ["fold"], f"切 off 本身不该点鼠标：{clicker.pressed}")
    ev = feed(mgr, clock=clock, fold=red())
    check(ev is not None and ev.kind == "mode" and "取消" in ev.detail,
          f"下一帧应补点取消：{ev}")
    check(len(clicker.pressed) == 2, f"应点一次取消：{clicker.pressed}")
    ev = feed(mgr, clock=clock, fold=gray())
    check(ev is not None and "已取消" in ev.detail, f"看到灰应收尾：{ev}")
    check(mgr._status == "idle", f"结束后应回 idle：{mgr._status}")

    # 只点过、没看到红 → 不盲点取消，但要告诉用户
    clock2 = Clock()
    clicker2 = FakeClicker()
    mgr2 = PrefoldManager(mode=MODE_LIVE, clicker=clicker2, clock=clock2)
    feed(mgr2, clock=clock2)
    warnings = mgr2.set_mode(MODE_OFF)
    check(len(warnings) == 1, f"状态未知应告警：{warnings}")
    check(clicker2.pressed == ["fold"], f"状态未知不许再点：{clicker2.pressed}")


def test_clicker_failure_is_not_fatal() -> None:
    """点击器故障：记一行、返回 error，不抛异常、不拖垮主循环。"""
    clock = Clock()
    clicker = FakeClicker(status="error")
    mgr = PrefoldManager(mode=MODE_LIVE, clicker=clicker, clock=clock)
    ev = feed(mgr, clock=clock)
    check(ev is not None and ev.kind == "error", f"点不上应报 error：{ev}")

    class Boom:
        def press(self, button):
            raise RuntimeError("backend down")

    mgr2 = PrefoldManager(mode=MODE_LIVE, clicker=Boom(), clock=Clock())
    ev = feed(mgr2, clock=Clock())
    check(ev is not None and ev.kind == "error", f"抛异常也要变成 error：{ev}")


def test_mode_validation() -> None:
    for bad in ("on", "", "live2", None):
        try:
            PrefoldManager(mode=bad, clicker=FakeClicker())
        except ValueError:
            pass
        else:
            check(False, f"非法模式 {bad!r} 应报错")


def main() -> int:
    for fn in (test_derived_keys, test_load_exclude,
               test_armed_once_then_confirmed, test_no_click_when_too_strong,
               test_unreliable_cards_and_wrong_street, test_new_hand_resets,
               test_manual_cancel_stops_rearm, test_blind_mode_clicks_at_most_once,
               test_retry_cap_and_give_up,
               test_on_turn_without_preselection_folds,
               test_on_turn_with_preselection_does_not_click,
               test_confirmed_then_turn_does_not_double_click,
               test_gave_up_still_folds_on_turn,
               test_excluded_key_not_armed, test_dry_run_never_clicks,
               test_off_mode_is_inert, test_switch_off_cancels_preselection,
               test_clicker_failure_is_not_fatal, test_mode_validation):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} 项不通过：")
        for item in FAILS:
            print(f"  ✗ {item}")
        return 1
    print("\n全部通过（预点弃牌）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
