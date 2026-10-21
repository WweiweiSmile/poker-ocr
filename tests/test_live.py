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
    AspectFit, FramePacket, JsonlWriter, LatestFrame, LiveProcessor, SettleGate,
    plan_crop, throttle_delay,
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
    check(args.settle == live.DEFAULT_SETTLE,
          f"动作合并默认应开启（{live.DEFAULT_SETTLE}s），实际 {args.settle}")
    check(args.settle_max == live.DEFAULT_SETTLE_MAX,
          f"动作合并上限默认 {live.DEFAULT_SETTLE_MAX}s，实际 {args.settle_max}")

    args = ap.parse_args(["--no-ai-advice", "--every", "0.5",
                          "--settle", "0"])
    check(args.ai_advice is False, "--no-ai-advice 应能关闭")
    check(args.every == 0.5, "--every 仍应能显式降频")
    check(args.settle == 0.0, "--settle 0 应能关掉动作合并")


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


class _GateSeat:
    """一份最小的槽位布局（100x100 测试图上的动作区）。"""
    name_slot = (0.10, 0.10, 0.30, 0.15)
    chip_slot = (0.10, 0.20, 0.30, 0.25)
    chip_slot_alt = None
    bet_slot = (0.10, 0.30, 0.30, 0.35)
    avatar_slot = (0.10, 0.40, 0.30, 0.60)


class _GateProfile:
    seats = [_GateSeat()]
    global_slots = {
        "pot": (0.50, 0.10, 0.70, 0.15),
        # 盲注横幅里的倒计时每秒都在跳：它必须被排除在静止判据之外
        "blinds": (0.50, 0.20, 0.70, 0.25),
    }

    @staticmethod
    def slot_to_px(slot, size):
        w, h = size
        return (int(slot[0] * w), int(slot[1] * h),
                int(slot[2] * w), int(slot[3] * h))


def _gate_image(patch=None) -> np.ndarray:
    img = np.zeros((100, 100, 3), np.uint8)
    if patch is not None:
        (x1, y1, x2, y2), value = patch
        img[y1:y2, x1:x2] = value
    return img


def test_settle_gate_coalesces_action_burst() -> None:
    """一阵连续变化期间一帧都不放行；静止 settle 秒后只放行最新帧。"""
    gate = SettleGate(_GateProfile(), settle=0.2, max_wait=0.5)
    base = _gate_image()
    check(gate.should_process(base, 0.0), "首帧应立即放行")
    check(gate.should_process(base, 0.05), "画面没变时应立即放行")

    # 名字槽（x 10–30, y 10–15）连续出现三个动作
    check(not gate.should_process(
        _gate_image(((10, 10, 30, 15), 80)), 0.10), "动作刚开始时不该放行")
    check(not gate.should_process(
        _gate_image(((10, 10, 30, 15), 160)), 0.20), "动作还在继续时不该放行")
    check(not gate.should_process(
        _gate_image(((10, 10, 30, 15), 160)), 0.35),
        "距离最后一次变化不足 settle 时不该放行")
    check(gate.should_process(
        _gate_image(((10, 10, 30, 15), 160)), 0.45), "静止满 settle 后应放行")
    check(gate.should_process(
        _gate_image(((10, 10, 30, 15), 160)), 0.50), "落定后应回到逐帧放行")
    check(gate.blocks == 3, f"三次变化应合并掉 3 帧，实际 {gate.blocks}")


def test_settle_gate_max_wait_limits_starvation() -> None:
    """动作一直不停（每次都比 settle 短）时，max_wait 到点也要放行一帧。"""
    gate = SettleGate(_GateProfile(), settle=0.3, max_wait=0.5)
    check(gate.should_process(_gate_image(), 0.0), "首帧应立即放行")
    times = [0.1, 0.2, 0.3, 0.4, 0.5]
    for i, t in enumerate(times, start=1):
        # 每次都变，永远达不到「静止 0.3s」
        img = _gate_image(((10, 10, 30, 15), min(255, i * 40)))
        allowed = gate.should_process(img, t)
        if t < 0.6:
            check(not allowed, f"{t}s 时 burst 才 {t - 0.1:.1f}s，不该放行")
    # burst_start=0.1，max_wait=0.5 → 0.6s 到点
    check(gate.should_process(
        _gate_image(((10, 10, 30, 15), 255)), 0.6),
        "等满 max_wait 后必须放行，不能无限等")


def test_settle_gate_ignores_blind_clock_and_avatar_ring() -> None:
    """盲注倒计时和头像边缘的计时环一直在动，不能拖住识别。"""
    gate = SettleGate(_GateProfile(), settle=0.2)
    img = _gate_image()
    check(gate.should_process(img, 0.0), "首帧应立即放行")

    for i in range(1, 6):
        img = img.copy()
        img[20:25, 50:70] = i * 40            # 盲注横幅倒计时
        img[40:60, 10:13] = i * 40            # 头像左边缘（计时环）
        check(gate.should_process(img, i * 0.1),
              f"第 {i} 帧只有倒计时/计时环在动，应继续放行")

    # 头像中心（弃牌蒙层）是真实动作，必须拦住
    img = img.copy()
    img[45:55, 18:25] = 200
    check(not gate.should_process(img, 0.65), "头像中心变化应触发合并")


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


def test_jsonl_appends_by_default() -> None:
    """默认追加：重启后上一轮的手牌记录还在；--no-append 才清空。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "live.jsonl"
        w = JsonlWriter(path)
        w.write({"type": "hand", "hand_id": "h1"})
        w.close()

        w = JsonlWriter(path)
        w.write({"type": "hand", "hand_id": "h2"})
        w.close()
        parsed = [json.loads(line) for line in
                  path.read_text(encoding="utf-8").splitlines()]
        check([r["hand_id"] for r in parsed] == ["h1", "h2"],
              f"默认应追加，实际 {parsed}")

        w = JsonlWriter(path, append=False)
        w.write({"type": "hand", "hand_id": "h3"})
        w.close()
        parsed = [json.loads(line) for line in
                  path.read_text(encoding="utf-8").splitlines()]
        check([r["hand_id"] for r in parsed] == ["h3"],
              f"append=False 应覆盖，实际 {parsed}")


def test_jsonl_append_repairs_partial_last_line() -> None:
    """上次被硬杀留下的半行不能拖累新记录：新记录必须独立成行。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "live.jsonl"
        path.write_text('{"type": "hand", "hand_id": "h1"',
                        encoding="utf-8")      # 没有结尾换行
        w = JsonlWriter(path)
        w.write({"type": "hand", "hand_id": "h2"})
        w.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        check(lines[-1].startswith("{") and json.loads(lines[-1])["hand_id"] == "h2",
              f"新记录应独立成行，实际 {lines}")


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


def test_rotate_frame_clockwise() -> None:
    """--rotate 90 是顺时针；0 度原样返回，非法角度报错。"""
    img = np.zeros((2, 4, 3), np.uint8)
    img[0, 0] = (1, 2, 3)
    rotated = live.rotate_frame(img, 90)
    check(rotated.shape == (4, 2, 3), f"90° 后尺寸应 4x2，实际 {rotated.shape}")
    check(tuple(rotated[0, 1]) == (1, 2, 3),
          f"左上角应顺时针转到右上角，实际 {rotated[0, 1]}")
    check(live.rotate_frame(img, 180).shape == (2, 4, 3), "180° 尺寸应不变")
    check(live.rotate_frame(img, 270).shape == (4, 2, 3), "270° 后尺寸应 4x2")
    check(live.rotate_frame(img, 0) is img, "0° 应原样返回")
    try:
        live.rotate_frame(img, 45)
    except ValueError:
        pass
    else:
        FAILS.append("45° 应报错")


def test_open_capture_supports_virtual_camera_index() -> None:
    """cam:N 走 AVFoundation 直接打开摄像头（OBS 虚拟摄像头超低延迟接法）。"""
    class FakeCap:
        def __init__(self, src, backend=None) -> None:
            self.src = src
            self.backend = backend
            self.opened = (src == 1 and backend == live.cv2.CAP_AVFOUNDATION)
            self.released = False

        def isOpened(self) -> bool:
            return self.opened

        def release(self) -> None:
            self.released = True

        def set(self, *_args) -> bool:
            return True

    calls: list[tuple] = []

    def factory(src, backend=None):
        calls.append((src, backend))
        return FakeCap(src, backend)

    with patch.object(live.cv2, "VideoCapture", side_effect=factory):
        cap = live._open_capture("cam:1")
    check(cap is not None and not cap.released, "cam:1 应能用 AVFoundation 打开")
    check(calls[:1] == [(1, live.cv2.CAP_AVFOUNDATION)],
          f"cam:1 应先试 AVFoundation：{calls}")

    calls.clear()
    with patch.object(live.cv2, "VideoCapture", side_effect=factory):
        check(live._open_capture("cam:9") is None, "不存在的索引应返回 None")
        check(live._open_capture("cam:abc") is None, "非法索引应返回 None")
    check(calls == [(9, live.cv2.CAP_AVFOUNDATION), (9, None)],
          f"cam:9 应依次试 AVFoundation 和默认后端：{calls}")


def test_advice_wait_logs_heartbeat_each_second() -> None:
    """等待模型时每秒一行心跳；同一秒不重复，结束后重置。"""
    class SlowRunner:
        busy = True

        def __init__(self) -> None:
            self.busy_seconds = 0.0

        def poll(self):
            return []

        def submit(self, sit) -> bool:      # pragma: no cover
            return True

    runner = SlowRunner()
    logs: list[str] = []
    proc = LiveProcessor(StubStateStream(), StubWriter(),
                         advice_runner=runner, log=logs.append)

    proc.log_advice_wait()
    check(logs == [], f"不足 1 秒不该打心跳：{logs}")
    runner.busy_seconds = 1.2
    proc.log_advice_wait()
    check(logs == ["  [建议] 生成中… 1s"], f"1 秒应打一行心跳：{logs}")
    proc.log_advice_wait()
    check(len(logs) == 1, f"同一秒不该重复：{logs}")
    runner.busy_seconds = 2.9
    proc.log_advice_wait()
    check(logs[-1].endswith("2s"), f"2 秒应再打一行：{logs}")

    # 模型返回后下一轮从 1s 重新开始
    runner.busy = False
    proc.log_advice_wait()
    check(proc._wait_beat_second == 0, "结束后心跳计数应重置")
    runner.busy = True
    runner.busy_seconds = 1.5
    proc.log_advice_wait()
    check(logs[-1].endswith("1s"), f"新一次建议应重新从 1s 报：{logs}")


def test_advice_bar_colors_by_action() -> None:
    """建议横幅：弃牌红底、跟注/过牌蓝底、下注/加注绿底。"""
    check(live._advice_bar("fold") == "\033[1;97;41m",
          f"弃牌应是红底：{live._advice_bar('fold')!r}")
    check(live._advice_bar("call") == "\033[1;97;44m",
          f"跟注应是蓝底：{live._advice_bar('call')!r}")
    check(live._advice_bar("check") == live._advice_bar("call"),
          "过牌是被动动作，应与跟注同色")
    check(live._advice_bar("raise") == "\033[1;30;42m",
          f"加注应是绿底：{live._advice_bar('raise')!r}")
    check(live._advice_bar("bet") == live._advice_bar("raise"),
          "下注与加注同属主动进攻，应同色")
    check(live._advice_bar("anything") == live._ANSI_ADVICE_BAR_DEFAULT,
          "未知动作应退回默认样式")


def main() -> int:
    tests = [
        test_defaults_are_no_throttle_and_instant_turn,
        test_mailbox_keeps_only_latest,
        test_wait_newer_wakes_up,
        test_slow_consumer_drops_frames_but_ends_at_latest,
        test_throttle_delay,
        test_settle_gate_coalesces_action_burst,
        test_settle_gate_max_wait_limits_starvation,
        test_settle_gate_ignores_blind_clock_and_avatar_ring,
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
        test_jsonl_appends_by_default,
        test_jsonl_append_repairs_partial_last_line,
        test_ai_advice_submits_on_hero_turn_once,
        test_ai_advice_waits_for_hero_cards,
        test_hand_memory_keeps_action_line_and_advices,
        test_hand_memory_flushes_on_switch_frame,
        test_processor_pauses_while_advice_is_generating,
        test_advice_wait_logs_heartbeat_each_second,
        test_advice_bar_colors_by_action,
        test_rotate_frame_clockwise,
        test_open_capture_supports_virtual_camera_index,
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
