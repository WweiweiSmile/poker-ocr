#!/usr/bin/env python3
"""人工锚点 + 错误段证据的单元测试（spec #93 / #94 / #95）。

只测新增的纯逻辑：
  · `LiveProcessor.anchor_snapshot` 的取帧与降级语义；
  · `AnchorCapture.trigger` 落盘 pre 帧、meta 骨架、后 3 秒收集、±10s 事件；
  · 错误段第 3 帧落截图、evidence 挂到 frame 事件、单场上限；
  · `ConsoleLog` / `LiveEventLog` 的 recent_between 时间窗。

不跑 OCR、不连摄像头：LiveProcessor 用 `__new__` 造一个只带所需属性的
壳，帧用 numpy 小图，时间用真实 clock（后 3 秒窗口调成 0.15s）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poker_ocr.live import (  # noqa: E402
    ANCHOR_SCHEMA, ERROR_DUMP_LIMIT, AnchorCapture, FramePacket,
    LiveProcessor, _anchor_advice)
from poker_ocr.live_log import ConsoleLog, LiveEventLog  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)
        print(f"  ❌ {msg}")


def _frame(seq: int, epoch: int = 1) -> tuple[int, int, float, str,
                                              np.ndarray]:
    return (seq, epoch, float(seq), f"f{seq}",
            np.full((8, 8, 3), seq % 255, np.uint8))


def _bare_processor() -> LiveProcessor:
    """只带锚点环的 LiveProcessor 壳（不跑 __init__，不碰 OCR）。"""
    proc = LiveProcessor.__new__(LiveProcessor)
    proc._anchor_ring = deque(maxlen=20)
    proc._anchor_lock = threading.Lock()
    return proc


def _fill(proc: LiveProcessor, seqs) -> None:
    with proc._anchor_lock:
        for seq in seqs:
            proc._anchor_ring.append(_frame(seq))


class _FakeEventLog:
    def __init__(self, rows):
        self.rows = rows
        self.windows = []

    def recent_between(self, start, end):
        self.windows.append((start, end))
        return self.rows


class _FakeConsole:
    def __init__(self, lines):
        self.lines = lines

    def recent_between(self, start, end):
        return self.lines


# ── anchor_snapshot ─────────────────────────────────────────────


def test_anchor_snapshot_degrades_gracefully() -> None:
    proc = _bare_processor()
    frames, degraded = proc.anchor_snapshot(None)
    check(frames == [] and degraded is False, f"空环应返回空且不降级：{frames} {degraded}")
    frames, degraded = proc.anchor_snapshot(5)
    check(frames == [] and degraded is False, f"空环给 seq 也应空：{frames} {degraded}")

    _fill(proc, range(100, 105))
    frames, degraded = proc.anchor_snapshot(None)
    check([f[0] for f in frames] == [100, 101, 102, 103, 104],
          f"无 seq 应取全部：{[f[0] for f in frames]}")
    check(degraded is False, "无 seq 不该降级")

    frames, degraded = proc.anchor_snapshot(102)
    check([f[0] for f in frames] == [100, 101, 102],
          f"应取 ≤ seq 的帧：{[f[0] for f in frames]}")
    check(degraded is False, "命中中心帧不该降级")

    frames, degraded = proc.anchor_snapshot(99)
    check([f[0] for f in frames] == [100], f"比最旧还旧应取最旧：{frames}")
    check(degraded is True, "比最旧还旧必须标降级")

    frames, degraded = proc.anchor_snapshot(999)
    check(len(frames) == 5, f"超前 seq 应取全部：{len(frames)}")
    check(degraded is True, "seq 超前（还没到这一帧）必须标降级")

    _fill(proc, range(105, 130))
    frames, _ = proc.anchor_snapshot(None)
    check(len(frames) == 20, f"环应封顶 20 帧：{len(frames)}")
    check(frames[-1][0] == 129, f"应保留最新 20 帧：{frames[-1][0]}")


def test_anchor_advice_strips_prompt() -> None:
    payload = {"option": {"id": "call"}, "reason": "赔率够",
               "prompt_system": "很长", "prompt_user": "也很长",
               "raw_response": "{}"}
    got = _anchor_advice(payload)
    check(got is not None and got["option"] == {"id": "call"},
          f"应保留建议主体：{got}")
    check(all(k not in got for k in
              ("prompt_system", "prompt_user", "raw_response")),
          f"提示词原文不该进锚点：{got}")
    check(_anchor_advice(None) is None, "None 应原样返回 None")


# ── AnchorCapture ───────────────────────────────────────────────


def test_anchor_capture_writes_pre_and_post() -> None:
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        proc = _bare_processor()
        _fill(proc, range(100, 105))
        events: list[tuple[str, dict]] = []
        event_log = _FakeEventLog([{"event": "advice", "t": 1.0}])
        console = _FakeConsole(["100 行日志"])
        capture = AnchorCapture(
            run_dir=run_dir,
            pre_frames=proc.anchor_snapshot,
            frames=proc.anchor_frames,
            publish=lambda kind, payload: events.append((kind, payload)),
            session_id="s-1", run_id="r-1",
            event_log=event_log, console_log=console,
            current_hand=lambda: {"hand_id": "h-9", "street": "flop"},
            last_advice=lambda: {"option": {"id": "raise"}, "reason": "值",
                                 "prompt_system": "别存我"},
            table=lambda: {"pot": 12.0},
            post_seconds=0.15, poll_seconds=0.02)

        result = capture.trigger(None)
        check(result["ok"] is True, f"应成功：{result}")
        check(result["center_seq"] == 104, f"中心帧应是最新：{result}")
        check(result["degraded"] is False, f"不该降级：{result}")
        folder = Path(result["folder"])
        check(folder.is_dir(), f"锚点目录应存在：{folder}")
        check(folder.parent == run_dir / "anchors", f"应落在 anchors/：{folder}")
        jpgs = sorted((folder / "frames").glob("*.jpg"))
        check(len(jpgs) == 5, f"pre 帧应写 5 张：{len(jpgs)}")

        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        check(meta["schema"] == ANCHOR_SCHEMA, f"schema：{meta['schema']}")
        check(meta["kind"] == "manual", f"kind：{meta['kind']}")
        check(meta["session_id"] == "s-1" and meta["run_id"] == "r-1",
              f"session/run：{meta['session_id']} {meta['run_id']}")
        check(meta["hand_id"] == "h-9" and meta["street"] == "flop",
              f"手牌上下文：{meta['hand_id']} {meta['street']}")
        check(meta["pre_frames"] == 5, f"pre_frames：{meta['pre_frames']}")
        check(meta["note"] == "", "note 初始应为空")
        check(meta["advice"]["option"] == {"id": "raise"}, f"建议：{meta['advice']}")
        check("prompt_system" not in meta["advice"], "提示词不该进 meta")
        check(meta["table"] == {"pot": 12.0}, f"牌桌快照：{meta['table']}")

        # 点击后 3 秒：新帧进环，后台线程应补收。
        _fill(proc, [105, 106])
        capture.wait(5)
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        check(meta["post_frames"] == 2, f"应收到 2 帧 post：{meta['post_frames']}")
        check(len(meta["frames"]) == 7, f"帧总数应为 7：{len(meta['frames'])}")
        check(meta["frames"][-1]["seq"] == 106,
              f"最后一帧应是 106：{meta['frames'][-1]}")
        check(len(list((folder / "frames").glob("*.jpg"))) == 7,
              "post 帧也应落盘")
        check(meta["events"] == [{"event": "advice", "t": 1.0}],
              f"事件窗：{meta['events']}")
        check(meta["console_log"] == ["100 行日志"], f"控制台窗：{meta['console_log']}")
        check("window" in meta and "start" in meta["window"], "应写 ±10s 窗口")
        check("finished_at" in meta, "应写 finished_at")
        start, end = event_log.windows[-1]
        check(abs((end - start) - 20.0) < 0.01,
              f"事件窗应 ±10s：{end - start}")
        check([e[1]["done"] for e in events] == [False, True],
              f"应发开始与完成两条 anchor 事件：{events}")


def test_anchor_capture_degraded_and_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        proc = _bare_processor()
        capture = AnchorCapture(run_dir=Path(td),
                                pre_frames=proc.anchor_snapshot,
                                frames=proc.anchor_frames,
                                post_seconds=0.0, poll_seconds=0.01)
        result = capture.trigger(None)
        check(result["ok"] is False, f"空环应明确失败而不是落空目录：{result}")

        _fill(proc, range(100, 104))
        result = capture.trigger(50)
        check(result["ok"] is True and result["degraded"] is True,
              f"seq 不在环里应降级成功：{result}")
        check(result["center_seq"] == 100,
              f"降级应取最旧帧：{result['center_seq']}")
        capture.wait(5)
        meta = json.loads((Path(result["folder"]) / "meta.json").read_text(
            encoding="utf-8"))
        check(meta["degraded"] is True, "meta 应记 degraded")
        check(meta["center_seq"] == 100, f"meta 中心帧：{meta['center_seq']}")


# ── 错误段证据 ─────────────────────────────────────────────────


def _evidence_processor(tmp: Path) -> LiveProcessor:
    proc = LiveProcessor.__new__(LiveProcessor)
    proc.error_dir = tmp / "errors"
    proc.incident_dir = None
    proc.quiet = True
    proc.full_recorder = None
    proc.state_stream = SimpleNamespace(sampled=1, accepted=0, records=0)
    proc.gate_dumps = 0
    proc._gate_dumped = set()
    proc._gate_seen = set()
    proc._gate_sig = None
    proc._gate_logged_at = 0.0
    proc._last_gate_dump = 0.0
    proc.last_gate_dump = None
    proc._discard_streak = 0
    proc._discard_start_seq = None
    proc._discard_cats = ()
    proc._error_dumps = 0
    proc._frame_evidence = None
    proc.clock = lambda: 100.0
    proc.log = lambda _msg: None
    proc._current_hand = lambda: {"hand_id": "h-9", "street": "turn"}
    proc.events = lambda kind, payload: None
    proc.processed = 0
    proc.total_proc_ms = 0.0
    proc.advice_count = 0
    proc.advice_done = 0
    return proc


def _discard(seq: int) -> tuple[FramePacket, SimpleNamespace]:
    packet = FramePacket(seq=seq, epoch=1, grab_mono=1.0,
                         image=np.zeros((8, 8, 3), np.uint8))
    outcome = SimpleNamespace(kind="discard", errors=["pot", "stack:3"],
                              state=None)
    return packet, outcome


def test_error_segment_dumps_on_third_frame() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _evidence_processor(tmp)
        for seq in (7, 8):
            packet, outcome = _discard(seq)
            proc._note_gate_discard(packet, outcome, 1.0, None)
        check(proc._frame_evidence is None, "前两帧不该落盘")
        check(not (tmp / "errors").exists(), "前两帧不该建目录")

        packet, outcome = _discard(9)
        proc._note_gate_discard(packet, outcome, 1.0, None)
        evidence = proc._frame_evidence
        check(evidence is not None, "连续第 3 帧应落盘并挂 evidence")
        check(evidence["segment_start_seq"] == 7,
              f"段起点应是第 1 帧：{evidence}")
        check(evidence["frames"] == 3, f"段长应是 3：{evidence}")
        folder = Path(evidence["folder"])
        check((folder / "frame.jpg").is_file(), "应有现场图")
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        check(meta["schema"] == "live.error.v1", f"schema：{meta['schema']}")
        check(meta["kind"] == "discard_segment", f"kind：{meta['kind']}")
        check(meta["segment_start_seq"] == 7 and meta["segment_frames"] == 3,
              f"段信息：{meta['segment_start_seq']} {meta['segment_frames']}")
        check(meta["cats"] == ["pot", "stack"], f"类别：{meta['cats']}")
        check(meta["hand_id"] == "h-9", f"手牌：{meta['hand_id']}")

        # 同一段继续丢帧：不再重复落盘。
        packet, outcome = _discard(10)
        proc._note_gate_discard(packet, outcome, 1.0, None)
        check(len(list((tmp / "errors").iterdir())) == 1,
              "同一段只落一张")

        # frame 事件挂 evidence 后清空，只挂一次。
        published: list[dict] = []
        proc.events = lambda kind, payload: published.append(payload)
        packet, outcome = _discard(9)
        proc._publish_frame(packet, 1.0, "f9", 0.01, 3.0, outcome, None, None)
        check(published[-1].get("evidence", {}).get("segment_start_seq") == 7,
              f"frame 事件应带 evidence：{published[-1].get('evidence')}")
        check(proc._frame_evidence is None, "evidence 只挂一次")
        proc._publish_frame(packet, 1.0, "f9", 0.01, 3.0, outcome, None, None)
        check("evidence" not in published[-1], "下一帧不该重复挂 evidence")


def test_error_dump_respects_cap() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _evidence_processor(tmp)
        proc._error_dumps = ERROR_DUMP_LIMIT
        for seq in (1, 2, 3):
            packet, outcome = _discard(seq)
            proc._note_gate_discard(packet, outcome, 1.0, None)
        check(proc._frame_evidence is None, "达到上限后不该再落盘")
        check(not (tmp / "errors").exists(), "达到上限后不该建目录")


# ── recent_between ─────────────────────────────────────────────


def test_console_recent_between() -> None:
    with tempfile.TemporaryDirectory() as td:
        log = ConsoleLog(Path(td) / "console.log", append=False)
        before = time.time() - 0.5
        log.write("第一行")
        log.write("第二行")
        after = time.time() + 0.5
        got = log.recent_between(before, after)
        check(len(got) == 2, f"窗口内应有 2 行：{got}")
        check("第一行" in got[0], f"内容：{got[0]}")
        check(log.recent_between(0, before - 0.1) == [], "窗口外应为空")
        log.close()


def test_event_log_recent_between() -> None:
    with tempfile.TemporaryDirectory() as td:
        log = LiveEventLog(Path(td) / "events.jsonl", session_id="s-1",
                           append=False)
        before = time.time() - 0.5
        log.write("advice", {"option": {"id": "call"}})
        log.write("frame", {"seq": 7})
        after = time.time() + 0.5
        got = log.recent_between(before, after)
        check([r["event"] for r in got] == ["advice", "frame"],
              f"事件顺序：{got}")
        check(log.recent_between(0, before - 0.1) == [], "窗口外应为空")
        log.close()


def main() -> int:
    tests = [test_anchor_snapshot_degrades_gracefully,
             test_anchor_advice_strips_prompt,
             test_anchor_capture_writes_pre_and_post,
             test_anchor_capture_degraded_and_empty,
             test_error_segment_dumps_on_third_frame,
             test_error_dump_respects_cap,
             test_console_recent_between,
             test_event_log_recent_between]
    print("── 锚点 / 错误段证据 ──────────────────────────")
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
    print("\n✅ 锚点 / 错误段证据全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
