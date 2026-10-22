#!/usr/bin/env python3
"""StateStream 的单测：防抖、丢弃、消失确认、断流重置。

不加载模型、不读图片：把 `RoiStream` / `read_avatars` / `build_detector`
换成桩，帧用 `test_frame_state.make_frame` 手工拼。真正跑 OCR 的等价性
由 `tools/video_to_states.py` 对 video1 的离线回归保证。

    .venv/bin/python tests/test_state_stream.py
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import replace as dc_replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import poker_ocr.state_stream as ss  # noqa: E402
from poker_ocr.parse import Blinds  # noqa: E402
from test_frame_state import BASE, PROFILE, make_frame  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


class StubRoiStream:
    """按顺序吐出预置的 RoiFrame；只记录 reset 次数。"""

    def __init__(self, profile, engine, detector=None, **kwargs) -> None:
        self.cache = None
        self.frames = []
        self.resets = 0

    def read(self, img, frame_id: str = "frame", *, detect_dealer: bool = True):
        if not self.frames:
            raise AssertionError("StubRoiStream 没有待处理的帧")
        return dc_replace(self.frames.pop(0), frame_id=frame_id)

    def reset(self) -> None:
        self.resets += 1


@contextmanager
def stub_env():
    with patch.object(ss, "RoiStream", StubRoiStream), \
            patch.object(ss, "build_detector", lambda *a, **k: None), \
            patch.object(ss, "read_avatars", lambda *a, **k: {}), \
            patch.object(ss, "read_slots", lambda *a, **k: []):
        yield


def new_stream(**kwargs) -> ss.StateStream:
    kwargs.setdefault("detect_fold", False)
    return ss.StateStream(PROFILE, engine=object(), **kwargs)


def feed(stream: ss.StateStream, frame, t: float = 0.0) -> ss.FrameOutcome:
    stream.stream.frames.append(frame)
    return stream.ingest(np.zeros((4, 4, 3), np.uint8), f"f{int(t * 100):04d}", t)


# ─────────────────────────── 用例 ───────────────────────────

def test_first_frame_is_baseline() -> None:
    with stub_env():
        s = new_stream()
        out = feed(s, make_frame())
        check(out.kind == "record", f"首帧应记录，实际 {out.kind}")
        check(out.record is not None and out.record["index"] == 1, "首帧 index 应为 1")
        check(out.record["pot"] == 38.0, f"底池应为 38，实际 {out.record['pot']}")


def test_same_signature_is_not_recorded_again() -> None:
    with stub_env():
        s = new_stream()
        feed(s, make_frame())
        out = feed(s, make_frame(), t=0.5)
        check(out.kind == "no_change", f"同状态应无变化，实际 {out.kind}")
        check(s.summary()["record_count"] == 1, "只应记录一次")


def test_change_needs_two_stable_frames() -> None:
    with stub_env():
        s = new_stream(stable_frames=2)
        feed(s, make_frame())
        changed = make_frame(pot="40BB")
        first = feed(s, changed, t=0.5)
        second = feed(s, changed, t=1.0)
        check(first.kind == "no_change", f"变化第一帧还不该记录，实际 {first.kind}")
        check(second.kind == "record", f"变化第二帧应记录，实际 {second.kind}")
        check(second.record["pot"] == 40.0, "记录的应是新底池")


def test_single_stable_frame_records_immediately() -> None:
    """stable_frames=1：变化第一帧就记录，不再等第二帧。"""
    with stub_env():
        s = new_stream(stable_frames=1)
        feed(s, make_frame())
        changed = make_frame(pot="40BB")
        out = feed(s, changed, t=0.5)
        check(out.kind == "record", f"1 帧确认应立即记录，实际 {out.kind}")
        check(out.record["pot"] == 40.0, "记录的应是新底池")


def test_discard_does_not_advance_prev() -> None:
    with stub_env():
        s = new_stream()
        baseline = make_frame()
        feed(s, baseline)
        bad = feed(s, make_frame(pot=""), t=0.5)
        check(bad.kind == "discard", f"底池读不出应丢弃，实际 {bad.kind}")
        check("pot" in bad.errors, f"丢弃原因应含 pot：{bad.errors}")
        # 被丢弃的帧不能推进 prev；回到原状态仍应判「无变化」
        out = feed(s, baseline, t=1.0)
        check(out.kind == "no_change", f"回到基线应无变化，实际 {out.kind}")
        check(s.summary()["discarded_reasons"].get("pot") == 1, "pot 丢弃计数应为 1")


def test_vanished_seat_needs_max_vanish_frames() -> None:
    with stub_env():
        s = new_stream(max_vanish=3)
        feed(s, make_frame())
        # 用 midL 而不是 lowR：lowR 是基线里的全下玩家，会走 all-in 继承，
        # 不会被判成离座。midL 变成「筹码 0、无下注、无动作」= sitting_out。
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["midL"] = {"name": "伯克利丁真", "chip": "0", "bet": ""}
        gone = make_frame(rows)
        outs = [feed(s, gone, t=0.5 * (i + 1)) for i in range(5)]
        check([o.kind for o in outs[:3]] == ["discard"] * 3,
              f"前 3 帧应因 vanished 丢弃：{[o.kind for o in outs]}")
        check(outs[3].kind == "no_change", f"第 4 帧应接受，实际 {outs[3].kind}")
        check(outs[4].kind == "record", f"第 5 帧应记录，实际 {outs[4].kind}")
        check(s.summary()["discarded_reasons"].get("vanished") == 3,
              f"vanished 计数应为 3：{s.summary()['discarded_reasons']}")


def test_initial_blinds_carried_when_banner_unreadable() -> None:
    with stub_env():
        s = new_stream(initial_blinds=Blinds(0.5, 1.0, None, "1/2"))
        out = feed(s, make_frame(blinds=""))
        check("blinds" not in out.errors,
              f"有初始盲注时不该因 blinds 丢弃：{out.errors}")
        check(out.state.blinds is not None and out.state.blinds.sb == 0.5,
              f"应继承初始盲注，实际 {out.state.blinds}")
        check(out.state.blinds_source == "carried",
              f"盲注来源应为 carried，实际 {out.state.blinds_source}")


def test_reset_makes_next_frame_a_new_baseline() -> None:
    with stub_env():
        s = new_stream()
        feed(s, make_frame())
        s.reset()
        out = feed(s, make_frame(), t=0.5)
        check(out.kind == "record", f"重置后首帧应记录，实际 {out.kind}")
        check(out.record["index"] == 2, "重置后 index 应继续递增")
        check(s.stream.resets == 1, "RoiStream.reset 应被调用一次")


def test_dealer_rotation_clears_all_in_carry() -> None:
    with stub_env():
        s = new_stream()
        feed(s, make_frame())
        check(s.prev_all_in == {"lowR"}, f"基线应记住 lowR 全下：{s.prev_all_in}")
        rows = {k: dict(v) for k, v in BASE.items()}
        rows.pop("lowR")
        feed(s, make_frame(rows, dealer="topR"), t=0.5)
        check(s.prev_dealer == "topR", f"prev_dealer 应更新，实际 {s.prev_dealer}")
        check(s.prev_all_in == set(),
              f"庄位轮转应清空全下继承，实际 {s.prev_all_in}")


def test_all_in_badge_disappearance_ends_hand() -> None:
    """头像 All-in 徽章连续 2 帧变成可读的非 All-in 文本 = 这手结束。"""
    reads = [{"hero": ("Alin", 0.7)}, {"hero": ("Alin", 0.7)},
             {"hero": ("弃牌", 0.9)}, {"hero": ("弃牌", 0.9)}]
    with stub_env(), patch.object(ss, "read_avatars",
                                  lambda *a, **k: reads.pop(0)):
        s = new_stream()
        first = feed(s, make_frame())
        check(not first.hand_end, "刚读到 All-in 不该判换手")
        check(s.prev_all_in_badges == {"hero"},
              f"应记住 hero 全下：{s.prev_all_in_badges}")
        second = feed(s, make_frame(), t=0.5)
        check(not second.hand_end, "徽章还在时不该判换手")
        third = feed(s, make_frame(), t=1.0)
        check(not third.hand_end, "单帧消失先不判，防遮挡/幻觉")
        fourth = feed(s, make_frame(), t=1.5)
        check(fourth.hand_end, "连续两帧消失应判这手结束")
        check(s.prev_all_in_badges == set(),
              f"换手后应清空全下徽章集合：{s.prev_all_in_badges}")


def test_all_in_badge_disappears_during_hero_turn_is_not_hand_end() -> None:
    """还轮到 hero 行动时，All-in 徽章消失不能收手（按钮/照片误读）。"""
    reads = [{"hero": ("Alin", 0.7)}, {"hero": ("Alin", 0.7)},
             {"hero": ("弃牌", 0.9)}, {"hero": ("弃牌", 0.9)}]
    with stub_env(), patch.object(ss, "read_avatars",
                                  lambda *a, **k: reads.pop(0)):
        s = new_stream()
        for i in range(4):
            out = feed(s, make_frame(hero_turn=True), t=0.5 * i)
        check(not out.hand_end,
              "hero 还在行动时不能凭 All-in 徽章消失收手")


def test_all_in_badge_end_waits_for_river() -> None:
    """全下后公共牌还没发完，All-in 徽章消失不能提前收手。"""
    reads = [{"hero": ("Alin", 0.7)}, {"hero": ("Alin", 0.7)},
             {"hero": ("弃牌", 0.9)}, {"hero": ("弃牌", 0.9)},
             {"hero": ("弃牌", 0.9)}]
    with stub_env(), patch.object(ss, "read_avatars",
                                  lambda *a, **k: reads.pop(0)):
        s = new_stream()

        def feed_board(board):
            s.stream.frames.append(make_frame(hero_turn=False))
            return s.ingest(np.zeros((4, 4, 3), np.uint8), frame_id="f", t=0.0,
                            extra_signature=(("Ah", "Kd"), tuple(board)))

        turn = ["Ks", "7d", "2c", "Qh"]
        feed_board(turn)                 # Alin
        feed_board(turn)                 # Alin
        feed_board(turn)                 # 消失第 1 帧
        out = feed_board(turn)           # 消失第 2 帧；turn 阶段不能收手
        check(not out.hand_end, "turn 阶段不能凭徽章消失收手")
        river = feed_board(turn + ["9s"])  # 河牌到齐
        check(river.hand_end, "河牌到齐后应收手")


class FakeCache:
    """只实现钉住相关的方法；StateStream 的慢变量缓存只用到这几个。"""

    def __init__(self) -> None:
        self.pinned: set[str] = set()

    def is_pinned(self, key: str) -> bool:
        return key in self.pinned

    def pin(self, key: str) -> bool:
        self.pinned.add(key)
        return True

    def unpin_all(self) -> None:
        self.pinned.clear()


def test_hand_boundaries_from_cards_and_board() -> None:
    """live 的 extra_signature：牌面掉回 0 / hero 换两张牌 = 新一手。"""
    with stub_env():
        s = new_stream()
        check(not s._hand_changed((("Ah", "Kd"), ("Ks", "7d", "2c"))),
              "首帧不该判成换手")
        check(not s._hand_changed((("Ah", "Kd"), ())),
              "牌面刚读空的第一帧先不判换手（可能是遮挡）")
        check(s._hand_changed((("Ah", "Kd"), ())),
              "连续两帧空牌面才判换手")
        check(not s._hand_changed((("Ah", "Kd"), ())), "空牌面重复不是换手")
        check(s._hand_changed((("Qs", "Jd"), ())), "hero 换了两张牌应判换手")
        check(not s._hand_changed(None), "没有 live 信号时不做换手推断")


def test_slow_fields_pinned_until_hand_switch() -> None:
    """名字/盲注读到可信值就钉住；动作徽章不钉；换手整批解钉。"""
    from poker_ocr.roi_reader import RoiFrame, RoiRead, SeatRoi

    with stub_env():
        s = new_stream()
        s.stream.cache = FakeCache()
        frame = RoiFrame(
            frame_id="f", size=(100, 100), hero_seat_id="hero",
            seats=[SeatRoi("hero", 0,
                           {"name": RoiRead("name", (0, 0, 1, 1),
                                            [("Alice", 0.95)])})],
            globals={"blinds": RoiRead("blinds", (0, 0, 1, 1),
                                       [("1/2(）30301600:24:28", 0.9)])})
        s._pin_slow_fields(frame)
        check("hero.name" in s.stream.cache.pinned,
              f"可信名字应钉住：{s.stream.cache.pinned}")
        check("@blinds" in s.stream.cache.pinned,
              f"可解析的盲注应钉住：{s.stream.cache.pinned}")

        # 动作徽章盖住名字槽时不能当名字钉住；盲注读坏也不钉
        s.stream.cache.unpin_all()
        frame.seats[0].fields["name"] = RoiRead(
            "name", (0, 0, 1, 1), [("弃牌", 0.95)])
        frame.globals["blinds"] = RoiRead("blinds", (0, 0, 1, 1), [])
        s._pin_slow_fields(frame)
        check("hero.name" not in s.stream.cache.pinned,
              f"动作徽章不该钉成名字：{s.stream.cache.pinned}")
        check("@blinds" not in s.stream.cache.pinned,
              f"读不出盲注不该钉：{s.stream.cache.pinned}")

        s.stream.cache.pinned.add("hero.name")
        s._restart_sticky()
        check(not s.stream.cache.pinned, "换手应解钉全部慢变量")


def test_bomb_banner_latches_across_frames() -> None:
    """横幅只在开局闪一下：即使横幅帧被丢弃，本手后续状态也要带标记。"""
    with stub_env():
        s = new_stream()
        feed(s, make_frame())
        # 开局下注动画帧：底池读不出 → 整帧丢弃，但横幅本身读到了
        bad = feed(s, make_frame(pot="", bomb_en="BOMB POT"), t=0.5)
        check(bad.kind == "discard", f"这帧应因底池丢弃，实际 {bad.kind}")
        check(bad.state.bomb_pot, "丢弃帧也要保留本帧的横幅读数")
        # 横幅已经消失，但同一手后面的状态仍应带标记
        out = feed(s, make_frame(), t=1.0)
        check(out.state.bomb_pot, "同一手内应一直保留爆炸标记")

        # 过几帧后正常换手（庄位轮换）→ 清空，不带到下一手
        for i in range(4):
            feed(s, make_frame(), t=1.5 + 0.5 * i)
        nxt = feed(s, make_frame(dealer="topR"), t=4.0)
        check(not nxt.state.bomb_pot, "换手后应清空爆炸标记")


def test_card_icon_fold_needs_two_absent_frames() -> None:
    """图标消失要连续 2 帧才判弃牌；亮牌座位没有图标也算在池。"""
    with stub_env():
        s = new_stream(detect_fold=True)
        img = np.zeros((4, 4, 3), np.uint8)
        with patch.object(ss, "read_card_icons",
                          lambda _img, _p: {"topR": False, "lowL": True}):
            first = s._read_icons(img)
            second = s._read_icons(img)
            check(first["topR"] is None, "第一帧没图标只能算未知")
            check(second["topR"] is False, "连续两帧没图标才判弃牌")
            check(first["lowL"] is True, "看到图标直接为 True")
        with patch.object(ss, "read_card_icons",
                          lambda _img, _p: {"topR": True}):
            back = s._read_icons(img)
            check(back["topR"] is True, "图标回来后立即恢复在池")
        shown_seats = s._shown_seats(((), (), (("topR", ("Ah", "Kd")),)))
        check(shown_seats == {"topR"}, f"应解析出亮牌座位：{shown_seats}")


def test_show_after_fold_keeps_fold() -> None:
    """河底才亮牌的人：之前已经判过弃牌，show 牌不能把它洗回「在池」。"""
    with stub_env():
        s = new_stream(detect_fold=True)
        s.stream.frames.extend([make_frame(), make_frame(), make_frame()])
        img = np.zeros((4, 4, 3), np.uint8)
        with patch.object(ss, "read_card_icons",
                          lambda _img, _p: {"lowL": False}):
            s.ingest(img, "f1", 0.0, extra_signature=((), ()))
            out2 = s.ingest(img, "f2", 0.2, extra_signature=((), ()))
        check(out2.state.player("lowL").fold is True,
              f"连续两帧没图标应判弃牌：{out2.state.player('lowL').to_debug()}")
        with patch.object(ss, "read_card_icons",
                          lambda _img, _p: {"lowL": None}):
            out3 = s.ingest(img, "f3", 0.4,
                            extra_signature=((), (), (("lowL", ("Ah", "Kd")),)))
        check(out3.state.player("lowL").fold is True,
              f"亮牌不该洗掉之前的弃牌：{out3.state.player('lowL').to_debug()}")


def main() -> int:
    tests = [
        test_first_frame_is_baseline,
        test_same_signature_is_not_recorded_again,
        test_change_needs_two_stable_frames,
        test_single_stable_frame_records_immediately,
        test_discard_does_not_advance_prev,
        test_vanished_seat_needs_max_vanish_frames,
        test_initial_blinds_carried_when_banner_unreadable,
        test_reset_makes_next_frame_a_new_baseline,
        test_dealer_rotation_clears_all_in_carry,
        test_all_in_badge_disappearance_ends_hand,
        test_all_in_badge_disappears_during_hero_turn_is_not_hand_end,
        test_all_in_badge_end_waits_for_river,
        test_hand_boundaries_from_cards_and_board,
        test_slow_fields_pinned_until_hand_switch,
        test_bomb_banner_latches_across_frames,
        test_card_icon_fold_needs_two_absent_frames,
        test_show_after_fold_keeps_fold,
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
    print("\n✅ 状态机全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
