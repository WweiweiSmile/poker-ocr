#!/usr/bin/env python3
"""LiveProcessor 的事件出口：每一帧都发 frame，丢弃帧带 errors、不更新 state。

    nv tests/test_live_events.py

不连摄像头、不加载模型：用桩 StateStream 返回 record/discard。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.frame_state import FrameState  # noqa: E402
from poker_ocr.live import FramePacket, LiveProcessor  # noqa: E402
from poker_ocr.state_stream import FrameOutcome  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


class StubStream:
    def __init__(self) -> None:
        self.sampled = 0
        self.accepted = 0
        self.records = 0
        self.discarded: dict[str, int] = {}

    def ingest(self, img, *, frame_id, t, extra_signature=None):
        self.sampled += 1
        if self.sampled == 2:
            state = FrameState(frame_id=frame_id, table_seats=9,
                               players_in_hand=3, players=[])
            state.errors = ["pot:遮挡", "stack:a"]
            self.discarded = {"pot": 1, "stack": 1}
            return FrameOutcome("discard", state, list(state.errors), None, 0, 0)
        self.accepted += 1
        self.records += 1
        state = FrameState(frame_id=frame_id, table_seats=9,
                           players_in_hand=3, players=[])
        record = {"index": self.records, "t": t, "frame": frame_id,
                  "table_seats": 9, "players_in_hand": 3,
                  "pot": 10.0, "hero_seat_id": "hero",
                  "players": [{"seat_id": "hero", "name": "me",
                               "stack": 100.0, "position": "BTN"}]}
        return FrameOutcome("record", state, [], record, 0, 1)

    def reset(self) -> None:
        pass

    def summary(self) -> dict:
        return {"sampled_frames": self.sampled, "accepted_frames": self.accepted,
                "record_count": self.records, "discarded_reasons": self.discarded}


class StubWriter:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, record: dict) -> None:
        self.rows.append(record)


def test_frame_events_for_record_and_discard() -> None:
    events: list[tuple[str, dict]] = []
    proc = LiveProcessor(StubStream(), StubWriter(), quiet=True,  # type: ignore[arg-type]
                         events=lambda kind, payload: events.append((kind, payload)))
    packet = FramePacket(seq=1, epoch=1, grab_mono=time.monotonic(),
                         image=np.zeros((4, 4, 3), np.uint8))
    proc.handle(packet)
    check(events and events[0][0] == "frame", f"应发 frame：{events}")
    payload = events[0][1]
    check(payload["kind"] == "record", f"kind 错：{payload}")
    check(payload.get("state", {}).get("pot") == 10.0,
          f"有效帧应带 state：{payload}")
    check(payload["state_index"] == 1, f"应带 state_index：{payload}")
    check(payload["cards"]["reliable"] is False,
          "没有 profile 时读牌应标不可用")

    events.clear()
    proc.handle(FramePacket(seq=2, epoch=1, grab_mono=time.monotonic(),
                            image=np.zeros((4, 4, 3), np.uint8)))
    check(events and events[0][0] == "frame", "丢弃帧也应发 frame")
    payload = events[0][1]
    check(payload["kind"] == "discard", f"kind 错：{payload}")
    check("state" not in payload, "丢弃帧不应带 state")
    check(payload["errors"] == ["pot:遮挡", "stack:a"],
          f"应带 errors：{payload}")


def test_no_events_callback_is_noop() -> None:
    # 旧调用方（单测/无看板）不传 events 时，帧处理不应依赖 sampled 等属性。
    proc = LiveProcessor(StubStream(), StubWriter(), quiet=True)  # type: ignore[arg-type]
    packet = FramePacket(seq=1, epoch=1, grab_mono=time.monotonic(),
                         image=np.zeros((4, 4, 3), np.uint8))
    check(proc.handle(packet) == "record", "没有事件回调也应正常处理")


def main() -> int:
    tests = [test_frame_events_for_record_and_discard,
             test_no_events_callback_is_noop]
    print("── live 帧事件 ──────────────────────────────")
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
    print("\n✅ live 帧事件全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
