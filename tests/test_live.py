#!/usr/bin/env python3
"""实时 worker 的单测：邮箱不积压、限速、断流重置、JSONL 完整性。

不连 RTSP、不加载模型。真正的推流链路靠 OBS 手工验收。

    .venv/bin/python tests/test_live.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poker_ocr.live as live  # noqa: E402
from poker_ocr.full_table import (  # noqa: E402
    FullTableConfig, FullTableResult, SeatObservation,
)
from poker_ocr.live import (  # noqa: E402
    AspectFit, FramePacket, JsonlWriter, LatestFrame, LiveProcessor, plan_crop,
    throttle_delay,
)
from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import LiveHandTracker  # noqa: E402
from poker_ocr.state_stream import FrameOutcome  # noqa: E402

FAILS: list[str] = []
IMG = np.zeros((4, 4, 3), np.uint8)


def test_defaults_are_no_throttle_and_instant_turn() -> None:
    """用户口径：不节流、不做 2 帧确认、轮到我第一帧就调用模型。"""
    ap = live.build_parser()
    args = ap.parse_args([])
    check(args.every == 0.0, f"默认应不节流，实际 --every={args.every}")
    check(args.stable_frames == 1,
          f"默认应取消 2 帧稳定确认，实际 {args.stable_frames}")
    check(args.ai_confirm == 1,
          f"默认应第一帧就认定轮到我，实际 {args.ai_confirm}")
    check(args.ai_advice is True, "AI 建议默认应开启")

    args = ap.parse_args(["--no-ai-advice", "--every", "0.5"])
    check(args.ai_advice is False, "--no-ai-advice 应能关闭")
    check(args.every == 0.5, "--every 仍应能显式降频")


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def packet(seq: int, epoch: int = 1, grab: float | None = None) -> FramePacket:
    return FramePacket(seq=seq, epoch=epoch,
                       grab_mono=time.monotonic() if grab is None else grab,
                       image=IMG)


class StubStateStream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.extra_signatures: list[tuple | None] = []
        self.resets = 0

    def ingest(self, img, frame_id: str, t: float, *,
               extra_signature: tuple | None = None) -> FrameOutcome:
        self.calls.append((frame_id, t))
        self.extra_signatures.append(extra_signature)
        return FrameOutcome("no_change", None, [], None, 0, 0)  # type: ignore[arg-type]

    def reset(self) -> None:
        self.resets += 1


class StubWriter:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, record: dict) -> None:
        self.rows.append(record)


# ─────────────────────────── 用例 ───────────────────────────

def test_mailbox_keeps_only_latest() -> None:
    mb = LatestFrame()
    mb.publish(packet(1))
    mb.publish(packet(2))
    mb.publish(packet(3))
    got = mb.wait_newer(0, 0.1)
    check(got is not None and got.seq == 3, f"应拿到最新帧 3，实际 {got}")
    check(mb.wait_newer(3, 0.05) is None, "没有新帧时应返回 None")


def test_wait_newer_wakes_up() -> None:
    mb = LatestFrame()

    def publish_later() -> None:
        time.sleep(0.05)
        mb.publish(packet(1))

    th = threading.Thread(target=publish_later)
    th.start()
    got = mb.wait_newer(0, 1.0)
    th.join()
    check(got is not None and got.seq == 1, f"应被唤醒并拿到帧 1，实际 {got}")


def test_slow_consumer_drops_frames_but_ends_at_latest() -> None:
    """消费慢于生产：中间帧被覆盖，但最后一定处理到最新帧。"""
    mb = LatestFrame()

    def produce() -> None:
        for i in range(1, 11):
            mb.publish(packet(i))
            time.sleep(0.02)
        mb.close()

    th = threading.Thread(target=produce)
    th.start()
    consumed: list[int] = []
    last = 0
    while True:
        got = mb.wait_newer(last, 0.3)
        if got is None:
            break
        consumed.append(got.seq)
        last = got.seq
        time.sleep(0.1)                # 比生产慢 5 倍
    th.join()

    check(consumed[-1] == 10, f"最后应消费到最新帧 10，实际 {consumed}")
    check(len(consumed) < 10, f"应丢掉中间帧，实际消费了 {consumed}")
    check(consumed == sorted(set(consumed)), f"seq 不应重复/回退：{consumed}")


def test_throttle_delay() -> None:
    check(abs(throttle_delay(10.0, 10.2, 0.5) - 0.3) < 1e-9,
          "应再等 0.3s")
    check(throttle_delay(10.0, 11.0, 0.5) == 0.0,
          "已经超过间隔就不该再等")
    check(throttle_delay(0.0, 0.0, 0.5) == 0.5,
          "首帧按 last_start=0 计算时等满一个间隔（主循环会先处理首帧）")


def test_epoch_change_resets_state_machine() -> None:
    stub = StubStateStream()
    writer = StubWriter()
    proc = LiveProcessor(stub, writer, every=0.5, quiet=True)  # type: ignore[arg-type]
    proc.handle(packet(1, epoch=1))
    check(stub.resets == 0, "第一次连接不该 reset")
    proc.handle(packet(2, epoch=2))
    check(stub.resets == 1, f"换 epoch 应 reset 一次，实际 {stub.resets}")
    check(len(stub.calls) == 2, "两帧都应进入状态机")


def test_stale_frame_is_skipped() -> None:
    stub = StubStateStream()
    writer = StubWriter()
    proc = LiveProcessor(stub, writer, every=0.5, max_frame_age=2.0,
                         quiet=True)  # type: ignore[arg-type]
    result = proc.handle(packet(1, grab=time.monotonic() - 10.0))
    check(result == "stale", f"超龄帧应跳过，实际 {result}")
    check(stub.calls == [], "超龄帧不该调用状态机")


def test_crop_left_uses_training_aspect_and_left_anchor() -> None:
    profile = SimpleNamespace(reference_size=(1080, 2400))
    plan = plan_crop((720, 1280), profile, "left")     # (h, w)
    check(plan.rect == (0, 0, 324, 720), f"应裁左侧 324px，实际 {plan.rect}")
    check(plan.out_size == (324, 720), f"输出尺寸不对：{plan.out_size}")
    cropped = plan.apply(np.zeros((720, 1280, 3), np.uint8))
    check(cropped.shape == (720, 324, 3), f"裁剪后形状不对：{cropped.shape}")


def test_crop_left_noop_when_already_training_aspect() -> None:
    profile = SimpleNamespace(reference_size=(1080, 2400))
    for shape in ((720, 324), (2400, 1080)):
        plan = plan_crop(shape, profile, "left")
        check(plan.rect is None and plan.out_size == (shape[1], shape[0]),
              f"{shape} 本来就匹配，不该裁：{plan}")


def test_crop_left_warns_when_canvas_is_too_narrow() -> None:
    profile = SimpleNamespace(reference_size=(1080, 2400))
    plan = plan_crop((720, 300), profile, "left")
    check(plan.rect is None, "宽度不足时不能裁")
    check(plan.warning is not None, "宽度不足时应该给出警告")


def test_crop_none_keeps_frame() -> None:
    profile = SimpleNamespace(reference_size=(1080, 2400))
    plan = plan_crop((720, 1280), profile, "none")
    check(plan.rect is None and plan.out_size == (1280, 720), f"none 不该裁：{plan}")


def test_crop_auto_finds_left_content() -> None:
    profile = SimpleNamespace(reference_size=(1080, 2400))
    img = np.zeros((720, 1280, 3), np.uint8)
    img[:, :324] = 120                       # 左侧 324x720 是画面，右侧全黑
    plan = plan_crop((720, 1280), profile, "auto", image=img)
    check(plan.rect == (0, 0, 324, 720), f"auto 应裁左侧 324px：{plan.rect}")
    check(plan.out_size == (324, 720), f"auto 输出尺寸不对：{plan.out_size}")
    check(plan.apply(img).shape == (720, 324, 3), "auto 裁剪结果形状不对")


def test_crop_auto_keeps_full_frame() -> None:
    profile = SimpleNamespace(reference_size=(1080, 2400))
    img = np.full((720, 1280, 3), 80, np.uint8)   # 画面铺满，没有黑边
    plan = plan_crop((720, 1280), profile, "auto", image=img)
    check(plan.rect is None and plan.out_size == (1280, 720),
          f"铺满画面不该裁：{plan}")


def test_crop_auto_all_black_warns() -> None:
    profile = SimpleNamespace(reference_size=(1080, 2400))
    img = np.zeros((720, 1280, 3), np.uint8)
    plan = plan_crop((720, 1280), profile, "auto", image=img)
    check(plan.rect is None and plan.warning is not None,
          f"全黑应跳过并告警：{plan}")


def test_aspect_fit_noop_when_ratio_already_matches() -> None:
    profile = SimpleNamespace(reference_size=(1080, 2400))
    for size in ((1080, 2400), (864, 1920), (324, 720)):
        fit = AspectFit.for_size(size, profile)
        check(not fit.resized, f"{size} 本来比例就匹配，不该重采样：{fit.to_debug()}")
        check(fit.out_size == size, f"{size} 输出尺寸应保持不变：{fit.out_size}")


def test_aspect_fit_restores_reference_ratio() -> None:
    """326x720 是 OBS 把竖屏画面按高度缩到 720 后的实际尺寸（2026-10-19 流）。"""
    profile = SimpleNamespace(reference_size=(1080, 2400))
    fit = AspectFit.for_size((326, 720), profile)
    check(fit.resized, "326x720（0.4528）与参考 0.45 不一致，应重采样")
    check(fit.out_size == (324, 720), f"应保持 720 高、宽度回到 324：{fit.out_size}")
    sx, sy = fit.scale
    check(abs(sx - sy) < 1e-9, f"重采样后 x/y 缩放必须一致：{fit.scale}")
    check(abs(fit.aspect - 0.452778) < 1e-4 and fit.deviation < 0.01,
          f"应算出流的宽高比和偏差：{fit.to_debug()}")
    img = np.zeros((720, 326, 3), np.uint8)
    check(fit.apply(img).shape == (720, 324, 3), "apply 后应是 324x720")


def test_aspect_fit_handles_landscape_capture() -> None:
    """画面被横向拉伸成 1280x720 时，按参考比例还原成 324x720。"""
    profile = SimpleNamespace(reference_size=(1080, 2400))
    fit = AspectFit.for_size((1280, 720), profile)
    check(fit.out_size == (324, 720), f"横屏拉伸应还原成 324x720：{fit.out_size}")
    check(abs(fit.scale[0] - fit.scale[1]) < 1e-9, "两个轴应使用同一缩放")


def test_full_recognizer_writes_state() -> None:
    res = FullTableResult(
        size=(1280, 720), table_center=(0.5, 0.45),
        seats=[
            SeatObservation("s0", (0.5, 0.8), 80.0, name="A", stack=100,
                            position="BTN", is_dealer=True, is_hero=True),
            SeatObservation("s1", (0.1, 0.5), 160.0, name="B", stack=50,
                            position="SB"),
        ],
        dealer_seat_id="s0", dealer_status="found", dealer_center=(0.5, 0.6),
        pot=12.0, blinds=None, direction="cw", warnings=[], debug={})
    writer = StubWriter()
    with patch.object(live, "observe_full", lambda img, engine, cfg: res):
        proc = LiveProcessor(None, writer, recognizer="full", engine=object(),
                             full_config=FullTableConfig(), stable_frames=1)
        kind = proc.handle(packet(1))
    check(kind == "record", f"全图模式首帧应记录，实际 {kind}")
    row = writer.rows[0]
    check(row["type"] == "state" and row["table_size"] == 2,
          f"记录结构不对：{row}")
    check(row["dealer"] == "s0", f"庄位没写进记录：{row}")
    check(row["hero"]["seat_id"] == "s0" and row["hero_position"] == "BTN",
          f"hero 没单独放到前面：{row}")
    check([s["seat_id"] for s in row["opponents"]] == ["s1"],
          f"opponents 应只剩非 hero 座位：{row}")
    check(list(row)[-1] == "opponents", f"opponents 应排最后：{list(row)}")


def test_state_payload_orders_hero_board_opponents() -> None:
    """JSONL 字段顺序：个人手牌/位置/筹码 → 公共牌 → 全局 → 对手（最后）。"""
    class RecordStateStream(StubStateStream):
        def ingest(self, img, frame_id: str, t: float, *,
                   extra_signature: tuple | None = None) -> FrameOutcome:
            self.extra_signatures.append(extra_signature)
            state = SimpleNamespace(pot=10.0, pot_before_street=4.0, hero_turn=True)
            record = {
                "index": 1, "t": round(t, 2), "frame": frame_id,
                "table_seats": 3, "players_in_hand": 2, "blinds": None,
                "dealer": "h", "hero_seat_id": "h", "hero_turn": True,
                "pot": 10.0, "pot_before_street": 4.0, "street_bet_total": 6.0,
                "players": [
                    {"seat_id": "h", "position": "BTN", "name": "me",
                     "stack": 100.0, "bet": 0, "action": None, "fold": None,
                     "in_hand": True, "all_in": False, "status": "playing"},
                    {"seat_id": "a", "position": "SB", "name": "A",
                     "stack": 50.0, "bet": 2, "action": "call", "fold": None,
                     "in_hand": True, "all_in": False, "status": "playing"},
                    {"seat_id": "b", "position": "BB", "name": "B",
                     "stack": None, "bet": None, "action": None, "fold": None,
                     "in_hand": False, "all_in": False, "status": "away"},
                ],
            }
            return FrameOutcome("record", state, [], record, 0, 0)  # type: ignore[arg-type]

    stub = RecordStateStream()
    writer = StubWriter()
    fake_cards = SimpleNamespace(
        hero_codes=["Ah", "Qd"], board_codes=["3s", "2h", "5s"], warnings=[])
    with patch.object(live, "recognize_cards", lambda img, profile: fake_cards):
        proc = LiveProcessor(stub, writer, every=0.5, quiet=True,
                             profile=SimpleNamespace())
        proc.handle(packet(1))

    check(stub.extra_signatures == [(("Ah", "Qd"), ("3s", "2h", "5s"))],
          f"牌面没并进变化指纹：{stub.extra_signatures}")
    row = writer.rows[0]
    check(row["hero_cards"] == ["Ah", "Qd"], f"hero_cards 不对：{row}")
    check(row["hero_position"] == "BTN", f"hero_position 不对：{row}")
    check(row["hero_stack"] == 100.0, f"hero_stack 不对：{row}")
    check(row["board"] == ["3s", "2h", "5s"], f"board 不对：{row}")
    check([p["seat_id"] for p in row["opponents"]] == ["a"],
          f"空座位不该算对手：{row}")
    check(list(row)[-1] == "opponents", f"opponents 应排最后：{list(row)}")
    keys = list(row)
    check(keys.index("hero_cards") < keys.index("board") < keys.index("opponents"),
          f"字段顺序不对：{keys}")


def test_jsonl_is_one_complete_object_per_line() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "live.jsonl"
        w = JsonlWriter(path)
        w.write({"type": "state", "pot": 38.0, "name": "柚子"})
        w.write({"type": "state", "pot": 40.0})
        w.close()

        lines = path.read_text(encoding="utf-8").splitlines()
        check(len(lines) == 2, f"应有两行，实际 {len(lines)}")
        parsed = [json.loads(line) for line in lines]
        check(parsed[0]["name"] == "柚子", "中文应原样保留")
        check(parsed[1]["pot"] == 40.0, "第二行应完整")


def test_ai_advice_submits_on_hero_turn_once() -> None:
    """轮到我时投一次决策点；同一个回合的后续帧不重复投。"""
    class TurnStream(StubStateStream):
        def ingest(self, img, frame_id: str, t: float, *,
                   extra_signature: tuple | None = None) -> FrameOutcome:
            self.extra_signatures.append(extra_signature)
            st = FrameState(
                frame_id=frame_id, table_seats=3, players_in_hand=3,
                players=[
                    PlayerState("hero", name="me", position="BTN", stack=100.0,
                                bet=0.0, in_hand=True),
                    PlayerState("a", name="A", position="SB", stack=99.5,
                                bet=1.0, action="call", in_hand=True),
                    PlayerState("b", name="B", position="BB", stack=99.0,
                                bet=1.0, in_hand=True),
                ],
                blinds=live.parse_blinds("1/2"), dealer="hero",
                hero_seat_id="hero", hero_turn=True, pot=2.5,
                pot_before_street=0.0)
            return FrameOutcome("no_change", st, [], None, 0, 0)

    class FakeRunner:
        def __init__(self) -> None:
            self.situations: list[dict] = []

        def submit(self, sit) -> bool:
            self.situations.append({"street": sit.street,
                                    "options": [o.id for o in sit.options]})
            return True

        def poll(self):
            return []

    runner = FakeRunner()
    tracker = LiveHandTracker(confirm_frames=1)
    fake_cards = SimpleNamespace(hero_codes=["Ah", "Kd"], board_codes=[],
                                 warnings=[])
    with patch.object(live, "recognize_cards", lambda img, profile: fake_cards):
        proc = LiveProcessor(TurnStream(), StubWriter(), quiet=True,
                             profile=SimpleNamespace(), hand_tracker=tracker,
                             advice_runner=runner)
        proc.handle(packet(1))
        proc.handle(packet(2))
    check(len(runner.situations) == 1,
          f"轮到我应投一次建议，实际 {runner.situations}")
    check("open-3.5" in runner.situations[0]["options"],
          f"翻前选项应是 bb 口径的开池：{runner.situations[0]}")
    with patch.object(live, "recognize_cards", lambda img, profile: fake_cards):
        proc.handle(packet(2))
    check(len(runner.situations) == 1,
          f"同一个决策点不应重复投：{runner.situations}")


def test_ai_advice_waits_for_hero_cards() -> None:
    """读不到手牌时不给建议（宁可晚一帧，也不给一个不知道手牌的答案）。"""
    class TurnStream(StubStateStream):
        def ingest(self, img, frame_id: str, t: float, *,
                   extra_signature: tuple | None = None) -> FrameOutcome:
            st = FrameState(
                frame_id=frame_id, table_seats=2, players_in_hand=2,
                players=[
                    PlayerState("hero", name="me", position="BTN", stack=100.0,
                                bet=0.0, in_hand=True),
                    PlayerState("a", name="A", position="BB", stack=99.0,
                                bet=1.0, in_hand=True),
                ],
                blinds=live.parse_blinds("1/2"), dealer="hero",
                hero_seat_id="hero", hero_turn=True, pot=1.5,
                pot_before_street=0.0)
            return FrameOutcome("no_change", st, [], None, 0, 0)

    class FakeRunner:
        def __init__(self) -> None:
            self.count = 0

        def submit(self, sit) -> bool:
            self.count += 1
            return True

        def poll(self):
            return []

    runner = FakeRunner()
    tracker = LiveHandTracker(confirm_frames=1)
    empty = SimpleNamespace(hero_codes=[], board_codes=[], warnings=[])
    with patch.object(live, "recognize_cards", lambda img, profile: empty):
        proc = LiveProcessor(TurnStream(), StubWriter(), quiet=True,
                             profile=SimpleNamespace(), hand_tracker=tracker,
                             advice_runner=runner)
        proc.handle(packet(1))
    check(runner.count == 0, "没读到手牌不该投建议")

    cards = SimpleNamespace(hero_codes=["Ah", "Kd"], board_codes=[], warnings=[])
    with patch.object(live, "recognize_cards", lambda img, profile: cards):
        proc.handle(packet(2))
    check(runner.count == 1, f"读到手牌后应补投一次，实际 {runner.count}")


def test_hand_memory_keeps_action_line_and_advices() -> None:
    """换手/退出时，hand 记录（手牌记忆）要同时带上完整行动线和这一手的建议。"""
    from poker_ocr.advice import Advice, Option

    class FakeTracker:
        def __init__(self) -> None:
            self.finished: dict | None = None
            self.current: dict | None = None

        def take_finished(self):
            hand, self.finished = self.finished, None
            return hand

        def take_current(self):
            hand, self.current = self.current, None
            return hand

    class FakeRunner:
        busy = False

        def __init__(self, advices) -> None:
            self._advices = list(advices)

        def poll(self):
            out, self._advices = self._advices, []
            return out

        def submit(self, sit) -> bool:      # pragma: no cover
            return True

    def advice(hand_id: str, reason: str) -> Advice:
        return Advice(hand_id=hand_id, street="flop",
                      option=Option("call", "Call 4BB", "call"),
                      reason=reason, source="model", model="jev-1.14",
                      duration_ms=1800, pot=10.0, to_call=4.0,
                      hero_cards=("Ah", "Kd"), board=("Ks", "7d", "2c"))

    flop = {"street": "flop", "pot_start": 6.0,
            "actions": [{"seat_id": "a", "action": "bet", "amount": 4.0,
                         "is_hero": False}]}
    tracker = FakeTracker()
    tracker.finished = {"hand_id": "h1", "streets": [flop]}
    writer = StubWriter()
    proc = LiveProcessor(StubStateStream(), writer, quiet=True,
                         hand_tracker=tracker,
                         advice_runner=FakeRunner([advice("h1", "pot odds")]),
                         log=lambda *_: None)
    proc.drain_advice()

    kinds = [r["type"] for r in writer.rows]
    check(kinds == ["advice", "hand"],
          f"应先写建议再写手牌记忆，实际 {kinds}")
    hand_row = writer.rows[1]
    check(hand_row["hand_id"] == "h1" and hand_row["streets"] == [flop],
          f"手牌记忆应保留完整行动线：{hand_row}")
    advices = hand_row.get("advices") or []
    check(len(advices) == 1 and advices[0]["hand_id"] == "h1"
          and advices[0]["option"]["id"] == "call"
          and advices[0]["reason"] == "pot odds"
          and advices[0]["duration_ms"] == 1800,
          f"手牌记忆应带上这一手的建议：{advices}")

    # 退出时最后一手还没换手：partial 记录同样带建议
    tracker.current = {"hand_id": "h2", "streets": [flop]}
    writer2 = StubWriter()
    proc2 = LiveProcessor(StubStateStream(), writer2, quiet=True,
                          hand_tracker=tracker,
                          advice_runner=FakeRunner([advice("h2", "exit")]),
                          log=lambda *_: None)
    proc2.drain_advice(final=True)
    partial = next(r for r in writer2.rows if r["type"] == "hand")
    check(partial.get("partial") is True and partial["hand_id"] == "h2",
          f"最后一手应标 partial：{partial}")
    check([a["reason"] for a in partial.get("advices", [])] == ["exit"],
          f"partial 手牌记忆也要带建议：{partial}")

    # 没有建议时保持旧输出，不出现 advices 键
    tracker.current = {"hand_id": "h3", "streets": [flop]}
    writer3 = StubWriter()
    proc3 = LiveProcessor(StubStateStream(), writer3, quiet=True,
                          hand_tracker=tracker, log=lambda *_: None)
    proc3.drain_advice(final=True)
    row3 = next(r for r in writer3.rows if r["type"] == "hand")
    check("advices" not in row3, f"没有建议时不该多出 advices 键：{row3}")


def test_hand_memory_flushes_on_switch_frame() -> None:
    """换手是在 observe 内部判定的：同一帧就要把上一手落库，不等下一帧。"""
    class SwitchStream(StubStateStream):
        def __init__(self) -> None:
            super().__init__()
            self.dealers = ["h", "a"]

        def ingest(self, img, frame_id: str, t: float, *,
                   extra_signature: tuple | None = None) -> FrameOutcome:
            dealer = self.dealers.pop(0) if self.dealers else "a"
            st = FrameState(
                frame_id=frame_id, table_seats=2, players_in_hand=2,
                players=[
                    PlayerState("hero", name="me", position="BTN", stack=100.0,
                                bet=0.0, in_hand=True),
                    PlayerState("a", name="A", position="BB", stack=99.0,
                                bet=1.0, in_hand=True),
                ],
                blinds=live.parse_blinds("1/2"), dealer=dealer,
                hero_seat_id="hero", hero_turn=False, pot=1.5,
                pot_before_street=0.0)
            return FrameOutcome("no_change", st, [], None, 0, 0)

    tracker = LiveHandTracker(confirm_frames=1)
    writer = StubWriter()
    cards = SimpleNamespace(hero_codes=["Ah", "Kd"], board_codes=[],
                            warnings=[])
    with patch.object(live, "recognize_cards", lambda img, profile: cards):
        proc = LiveProcessor(SwitchStream(), writer, quiet=True,
                             profile=SimpleNamespace(), hand_tracker=tracker,
                             log=lambda *_: None)
        proc.handle(packet(1))
        check(not any(r["type"] == "hand" for r in writer.rows),
              f"第一帧不该产出上一手记录：{writer.rows}")
        proc.handle(packet(2))

    hands = [r for r in writer.rows if r["type"] == "hand"]
    check(len(hands) == 1 and hands[0]["hand_id"] == "h1",
          f"换手那一帧就应落库上一手：{hands}")


def test_processor_pauses_while_advice_is_generating() -> None:
    """模型返回前 waiting_for_advice=True，主循环据此跳过识别。"""
    class BusyRunner:
        busy = True

        def poll(self):
            return []

        def submit(self, sit) -> bool:      # pragma: no cover — 不应被调用
            raise AssertionError("等待期间不该投新决策点")

    proc = LiveProcessor(StubStateStream(), StubWriter(), quiet=True,
                         advice_runner=BusyRunner())
    check(proc.waiting_for_advice, "生成建议期间应暂停识别")

    class IdleRunner(BusyRunner):
        busy = False

    proc = LiveProcessor(StubStateStream(), StubWriter(), quiet=True,
                         advice_runner=IdleRunner())
    check(not proc.waiting_for_advice, "结果返回后应恢复识别")

    proc = LiveProcessor(StubStateStream(), StubWriter(), quiet=True)
    check(not proc.waiting_for_advice, "没开 AI 建议时不应暂停")


def main() -> int:
    tests = [
        test_defaults_are_no_throttle_and_instant_turn,
        test_mailbox_keeps_only_latest,
        test_wait_newer_wakes_up,
        test_slow_consumer_drops_frames_but_ends_at_latest,
        test_throttle_delay,
        test_epoch_change_resets_state_machine,
        test_stale_frame_is_skipped,
        test_crop_left_uses_training_aspect_and_left_anchor,
        test_crop_left_noop_when_already_training_aspect,
        test_crop_left_warns_when_canvas_is_too_narrow,
        test_crop_none_keeps_frame,
        test_crop_auto_finds_left_content,
        test_crop_auto_keeps_full_frame,
        test_crop_auto_all_black_warns,
        test_aspect_fit_noop_when_ratio_already_matches,
        test_aspect_fit_restores_reference_ratio,
        test_aspect_fit_handles_landscape_capture,
        test_full_recognizer_writes_state,
        test_state_payload_orders_hero_board_opponents,
        test_jsonl_is_one_complete_object_per_line,
        test_ai_advice_submits_on_hero_turn_once,
        test_ai_advice_waits_for_hero_cards,
        test_hand_memory_keeps_action_line_and_advices,
        test_hand_memory_flushes_on_switch_frame,
        test_processor_pauses_while_advice_is_generating,
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
    print("\n✅ 实时 worker 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
