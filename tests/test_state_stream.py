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

    def read(self, img, frame_id: str = "frame"):
        if not self.frames:
            raise AssertionError("StubRoiStream 没有待处理的帧")
        return dc_replace(self.frames.pop(0), frame_id=frame_id)

    def reset(self) -> None:
        self.resets += 1


@contextmanager
def stub_env():
    with patch.object(ss, "RoiStream", StubRoiStream), \
            patch.object(ss, "build_detector", lambda *a, **k: None), \
            patch.object(ss, "read_avatars", lambda *a, **k: {}):
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


def main() -> int:
    tests = [
        test_first_frame_is_baseline,
        test_same_signature_is_not_recorded_again,
        test_change_needs_two_stable_frames,
        test_discard_does_not_advance_prev,
        test_vanished_seat_needs_max_vanish_frames,
        test_initial_blinds_carried_when_banner_unreadable,
        test_reset_makes_next_frame_a_new_baseline,
        test_dealer_rotation_clears_all_in_carry,
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
