"""按文字定位动作按钮的单元测试。

坐标全部取自 2026-11-11 现场录屏（1472x1666）的 OCR 实测值，覆盖两种版面。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.action_buttons import (  # noqa: E402
    action_for_option, find_action_buttons, resolve_target,
)
from poker_ocr.ocr import OcrBox  # noqa: E402


def check(cond, msg=""):
    assert cond, msg


class FakeEngine:
    """返回预设文本框（坐标相对**送进来的裁剪图**，和真引擎一致）。"""

    def __init__(self, boxes):
        self.boxes = boxes
        self.calls = []

    def read_boxes(self, img):
        self.calls.append(img.shape)
        return [OcrBox(text=t, score=s, x1=x - 30, y1=y - 14,
                       x2=x + 30, y2=y + 14, polygon=[])
                for t, s, x, y in self.boxes]


W, H = 1472, 1666
BAND_TOP = int(H * 0.775)          # 1291


def box(text, x, y, score=0.95):
    """整帧坐标 → 裁剪图坐标。"""
    return (text, score, x, y - BAND_TOP)


def blank():
    return np.zeros((H, W, 3), np.uint8)


def test_layout_a_three_buttons() -> None:
    """版面 A：弃牌在左 @566，主按钮 @736，过牌叫「让牌」@906。"""
    eng = FakeEngine([
        box("弃牌", 566, 1396),
        box("加注", 736, 1344),
        box("让牌", 906, 1397),
        box("免费", 194, 1354),        # 左下装饰位，不是按钮
        box("自动让牌", 907, 1398),    # 忽略：开关
    ])
    got = find_action_buttons(blank(), eng)
    check(abs(got["fold"][0] - 566) < 1, f"fold 错：{got}")
    check(abs(got["raise"][0] - 736) < 1, f"raise 错：{got}")
    check(abs(got["check"][0] - 906) < 1,
          f"check 应命中右侧「让牌」，不该是左下的「免费」：{got}")
    check("call" not in got, f"没有「跟注」不该出现 call：{got}")


def test_layout_b_two_buttons_moves_fold_to_center() -> None:
    """版面 B：只剩弃牌+免费，弃牌挪到正中 @736，过牌叫「免费」@907。

    固定槽位在这里会把弃牌点到 (563,1405) 的促销位上 —— 这正是录制视频里
    「弃牌点空白」的那 11 秒。
    """
    eng = FakeEngine([
        box("弃牌", 736, 1344),
        box("免费", 907, 1388),
        box("偷偷看牌", 564, 1349),
        box("发发看", 906, 1349),
    ])
    got = find_action_buttons(blank(), eng)
    check(abs(got["fold"][0] - 736) < 1, f"版面 B 的弃牌应在正中：{got}")
    check(abs(got["fold"][1] - 1344) < 1, f"版面 B 的弃牌 y：{got}")
    check(abs(got["check"][0] - 907) < 1, f"免费就是过牌：{got}")


def test_call_pill_replaces_check_when_facing_bet() -> None:
    eng = FakeEngine([box("弃牌", 566, 1396), box("加注", 736, 1344),
                      box("跟注", 906, 1411)])
    got = find_action_buttons(blank(), eng)
    check(abs(got["call"][0] - 906) < 1, f"call 定位错：{got}")
    check("check" not in got, f"面对下注不该有 check：{got}")
    # check/call 是同一颗右 pill，互相兜底
    check(resolve_target(got, "check") == got["call"], "check 应回退到 call")
    check(resolve_target(got, "call") == got["call"], "call 直接用自己")


def test_confirm_and_bet_labels() -> None:
    eng = FakeEngine([box("弃牌", 566, 1396), box("确定", 736, 1344),
                      box("让牌", 906, 1397)])
    got = find_action_buttons(blank(), eng)
    check(abs(got["confirm"][0] - 736) < 1, f"确定定位错：{got}")

    eng = FakeEngine([box("弃牌", 566, 1396), box("下注", 736, 1344),
                      box("让牌", 906, 1397)])
    got = find_action_buttons(blank(), eng)
    check(abs(got["bet"][0] - 736) < 1, f"下注定位错：{got}")


def test_ignores_low_score_and_empty() -> None:
    eng = FakeEngine([box("弃牌", 566, 1396, score=0.3)])
    check(find_action_buttons(blank(), eng) == {}, "低分不该采信")
    eng = FakeEngine([box("   ", 566, 1396)])
    check(find_action_buttons(blank(), eng) == {}, "空文本不该采信")
    check(find_action_buttons(np.zeros((0, 0, 3), np.uint8), eng) == {},
          "空图应返回空")


def test_action_for_option_mapping() -> None:
    check(action_for_option("fold") == "fold")
    check(action_for_option("call", to_call=0.0) == "check", "不需跟注时是过牌")
    check(action_for_option("call", to_call=2.0) == "call")
    check(action_for_option("raise") == "raise")
    check(action_for_option("bet") == "bet")
    check(action_for_option("call", is_confirm=True) == "confirm")


def test_plan_clicks_prefers_dynamic_targets() -> None:
    """有现找坐标时必须用它，不能被固定槽位覆盖。"""
    import random

    from poker_ocr.advice import Advice, Option
    from poker_ocr.clicker import ClickerConfig, Rect, plan_clicks

    cfg = ClickerConfig(buttons={"fold": Rect(500, 1350, 105, 105, "disc"),
                                 "call": Rect(850, 1350, 105, 105, "disc")})
    advice = Advice(hand_id="h", street="flop", option=Option("call", "过牌", "check"),
                    reason="", source="model")
    plan = plan_clicks(advice, cfg, random.Random(0),
                       targets={"check": (906.0, 1388.0)})
    check(len(plan.steps) == 1, f"应一步：{plan}")
    check(abs(plan.steps[0].x - 906.0) < 1e-6 and abs(plan.steps[0].y - 1388.0) < 1e-6,
          f"应点现找的坐标：{plan.steps[0]}")

    # 没有现找坐标时退回槽位（在槽位矩形内）
    plan2 = plan_clicks(advice, cfg, random.Random(0))
    st = plan2.steps[0]
    check(850 <= st.x <= 955 and 1350 <= st.y <= 1455, f"应退回槽位：{st}")
