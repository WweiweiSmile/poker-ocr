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
from poker_ocr.clicker import ClickResult, ClickStep  # noqa: E402
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
from poker_ocr.table_dynamics import TableDynamics  # noqa: E402

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
    check(args.auto_click is True, "自动点击默认应开启")
    check(args.url == "cam:auto",
          f"默认应自动选择虚拟摄像头，实际 {args.url}")
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


def test_auto_click_defaults_on_and_follows_advice() -> None:
    ap = live.build_parser()
    args = ap.parse_args([])
    check(args.auto_click is True, "自动点击默认应开启")
    args.ai_timeout = 20.0                 # main() 会先解析 env 默认值
    live._validate_args(ap, args)

    args = ap.parse_args(["--no-auto-click"])
    check(args.auto_click is False, "--no-auto-click 应能关闭")
    args.ai_timeout = 20.0
    live._validate_args(ap, args)

    args = ap.parse_args(["--clicker-dry-run"])
    check(args.auto_click and args.clicker_dry_run, "默认即可用 dry-run")
    args.ai_timeout = 20.0
    live._validate_args(ap, args)

    noai = ap.parse_args(["--no-ai-advice"])
    noai.ai_timeout = 20.0
    live._validate_args(ap, noai)
    check(noai.auto_click is False, "关闭 AI 建议时自动点击应跟着关闭")

    bad = ap.parse_args(["--clicker-dry-run", "--no-auto-click"])
    bad.ai_timeout = 20.0
    try:
        live._validate_args(ap, bad)
    except SystemExit:
        return
    FAILS.append("--clicker-dry-run 搭配 --no-auto-click 应拒绝启动")


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


def test_settle_gate_waits_for_card_rois() -> None:
    """profile 没标 board/hero_cards 时，静止闸也必须等默认牌区像素落定。"""
    gate = SettleGate(_GateProfile(), settle=0.2, max_wait=0.5)
    base = _gate_image()
    check(gate.should_process(base, 0.0), "首帧应立即放行")

    # 默认 board ROI：x 0.21–0.79, y 0.448–0.528（100x100 上约 x21–79/y44–52）
    check(not gate.should_process(
        _gate_image(((50, 46, 55, 50), 200)), 0.1),
        "公共牌区变化应触发静止等待")
    check(gate.should_process(
        _gate_image(((50, 46, 55, 50), 200)), 0.35),
        "牌区静止满 settle 后应放行")

    # 默认 hero_cards ROI：x 0.38–0.62, y 0.862–0.94
    check(not gate.should_process(
        _gate_image(((45, 88, 55, 92), 200)), 0.4),
        "hero 手牌区变化应触发静止等待")
    check(gate.should_process(
        _gate_image(((45, 88, 55, 92), 200)), 0.65),
        "hero 牌区静止满 settle 后应放行")


class _FlopStream:
    """轮到我、翻牌 3 张的固定状态。"""

    def ingest(self, img, frame_id: str, t: float, *,
               extra_signature: tuple | None = None) -> FrameOutcome:
        st = FrameState(
            frame_id=frame_id, table_seats=2, players_in_hand=2,
            players=[
                PlayerState("hero", name="me", position="BTN", stack=100.0,
                            bet=0.0, in_hand=True),
                PlayerState("a", name="A", position="BB", stack=99.0,
                            bet=2.0, in_hand=True),
            ],
            blinds=live.parse_blinds("1/2"), dealer="hero",
            hero_seat_id="hero", hero_turn=True, pot=7.5,
            pot_before_street=1.5)
        return FrameOutcome("no_change", st, [], None, 0, 0)

    def reset(self) -> None:      # pragma: no cover
        pass


def test_unreliable_cards_skip_advice_until_recovered() -> None:
    """牌面不可靠时不给建议，也不占用决策点；恢复后同一决策点补投一次。"""
    class FakeRunner:
        busy = False

        def __init__(self) -> None:
            self.count = 0

        def submit(self, sit) -> bool:
            self.count += 1
            return True

        def poll(self):
            return []

    runner = FakeRunner()
    tracker = LiveHandTracker(confirm_frames=1)
    writer = StubWriter()
    bad = SimpleNamespace(
        hero_codes=["Ah", "Kd"], board_codes=["Ks", "7d", "2c"],
        warnings=["board: 张数 4→3 回退，不采用"], reliable=False)
    good = SimpleNamespace(
        hero_codes=["Ah", "Kd"], board_codes=["Ks", "7d", "2c"],
        warnings=[], reliable=True)
    logs: list[str] = []

    with patch.object(live, "recognize_cards", lambda img, profile: bad):
        proc = LiveProcessor(_FlopStream(), writer, quiet=False,
                             profile=SimpleNamespace(), hand_tracker=tracker,
                             advice_runner=runner, log=logs.append)
        proc.handle(packet(1))
        check(runner.count == 0,
              f"牌面不可靠时不该投建议，实际 {runner.count}")
        check(any("跳过" in line for line in logs),
              f"跳过建议应打印原因：{logs}")
        check(tracker.hand is not None and tracker.hand.cards_unreliable,
              "手牌应标记 cards_unreliable")

        with patch.object(live, "recognize_cards", lambda img, profile: good):
            proc.handle(packet(2))
        check(runner.count == 1,
              f"牌面恢复后同一决策点应补投，实际 {runner.count}")

    hand = tracker.take_current()
    check(hand is not None and hand.get("cards_unreliable") is True,
          f"手牌记录应带 cards_unreliable：{hand}")
    check(hand is not None and hand.get("cards_warnings"),
          f"手牌记录应带 cards_warnings：{hand}")


def _incident_proc(incident: Path, logs: list[str], now: list[float]):
    return live.LiveProcessor(
        StubStateStream(), StubWriter(), quiet=True,
        profile=SimpleNamespace(), incident_dir=incident,
        incident_frames=3, clock=lambda: now[0], log=logs.append)


def test_card_regression_dumps_incident_frames() -> None:
    """公共牌 4→3 回退持续超过 1 秒时，才把最近几帧落盘做回归样本。"""
    from poker_ocr.cards import Card, CardsResult, CardSlotRead

    def card(code: str) -> Card:
        return Card(rank=code[:-1], suit=code[-1], confidence=0.9,
                    rank_score=0.9, rect=(0, 0, 10, 10),
                    evidence={"card_back": False, "occluded": False,
                              "ink_pixels": 900})

    def cards4() -> CardsResult:
        return CardsResult(
            size=(100, 100),
            board=CardSlotRead(name="board", slot=(0, 0, 1, 1),
                               rect=(0, 0, 10, 10),
                               cards=[card("Ad"), card("7c"),
                                      card("2s"), card("9h")]),
            hero_cards=CardSlotRead(name="hero_cards", slot=(0, 0, 1, 1),
                                    rect=(0, 0, 10, 10),
                                    cards=[card("Kh"), card("Qs")]))

    def cards3() -> CardsResult:
        out = cards4()
        out.board.cards = out.board.cards[:3]
        return out

    with tempfile.TemporaryDirectory() as tmp:
        incident = Path(tmp) / "incidents"
        # 4→3 回退连续出现 18 帧（约 7s），跨越旧的 5 秒去重窗口，仍只落一份
        results = [cards4(), cards4()] + [cards3()] * 18
        logs: list[str] = []
        now = [time.monotonic()]
        with patch.object(live, "recognize_cards",
                          lambda img, profile: results.pop(0)):
            proc = _incident_proc(incident, logs, now)
            for seq in range(1, 21):
                now[0] += 0.4
                img = np.zeros((100, 100, 3), np.uint8)
                img[:, :, 0] = (seq * 30) % 256   # 每帧不同，避免 ROI 去重
                proc.handle(packet(seq, grab=now[0]))
        folders = [p for p in incident.iterdir() if p.is_dir()]
        check(len(folders) == 1,
              f"持续异常应只落一个事故目录，实际 {folders}")
        if folders:
            meta = json.loads((folders[0] / "meta.json").read_text("utf-8"))
            check("回退" in meta["reason"], f"事故原因应写回退：{meta}")
            check(len(meta["frames"]) == 3,
                  f"环形缓冲应保存 3 帧：{meta['frames']}")
            check(len(list(folders[0].glob("*.jpg"))) == 3,
                  f"应写出 3 张 JPEG：{list(folders[0].iterdir())}")
        check(any("事故帧" in line for line in logs),
              f"落盘应打印提示：{logs}")


def test_transient_anomaly_is_not_dumped() -> None:
    """发牌/翻牌动画里的单帧回退不该落盘。"""
    from poker_ocr.cards import Card, CardsResult, CardSlotRead

    def card(code: str) -> Card:
        return Card(rank=code[:-1], suit=code[-1], confidence=0.9,
                    rank_score=0.9, rect=(0, 0, 10, 10),
                    evidence={"card_back": False, "occluded": False,
                              "ink_pixels": 900})

    def cards(n: int) -> CardsResult:
        codes = ["Ad", "7c", "2s", "9h"][:n]
        return CardsResult(
            size=(100, 100),
            board=CardSlotRead(name="board", slot=(0, 0, 1, 1),
                               rect=(0, 0, 10, 10),
                               cards=[card(c) for c in codes]),
            hero_cards=CardSlotRead(name="hero_cards", slot=(0, 0, 1, 1),
                                    rect=(0, 0, 10, 10),
                                    cards=[card("Kh"), card("Qs")]))

    with tempfile.TemporaryDirectory() as tmp:
        incident = Path(tmp) / "incidents"
        results = [cards(4), cards(4), cards(3), cards(4), cards(4)]
        logs: list[str] = []
        now = [time.monotonic()]
        with patch.object(live, "recognize_cards",
                          lambda img, profile: results.pop(0)):
            proc = _incident_proc(incident, logs, now)
            for seq in range(1, 6):
                now[0] += 0.4
                img = np.zeros((100, 100, 3), np.uint8)
                img[:, :, 0] = seq * 30
                proc.handle(packet(seq, grab=now[0]))
        folders = ([p for p in incident.iterdir() if p.is_dir()]
                   if incident.exists() else [])
        check(folders == [], f"单帧动画不该落盘事故帧：{folders}")


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
                                    "options": [o.id for o in sit.options],
                                    "table_dynamics": sit.table_dynamics})
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
                             advice_runner=runner,
                             table_dynamics_loader=lambda: TableDynamics(
                                 "松弱，诈唬少。", exists=True))
        proc.handle(packet(1))
        proc.handle(packet(2))
    check(len(runner.situations) == 1,
          f"轮到我应投一次建议，实际 {runner.situations}")
    check("open-3.5" in runner.situations[0]["options"],
          f"翻前选项应是 bb 口径的开池：{runner.situations[0]}")
    check(runner.situations[0]["table_dynamics"] == "松弱，诈唬少。",
          f"决策点应带上 .table_dy 的动态：{runner.situations[0]}")
    with patch.object(live, "recognize_cards", lambda img, profile: fake_cards):
        proc.handle(packet(2))
    check(len(runner.situations) == 1,
          f"同一个决策点不应重复投：{runner.situations}")


def test_auto_click_after_observe_on_next_frame() -> None:
    """完整链路：第一帧投递建议，第二帧拿到结果并在 observe 之后点击。

    这条用例专门覆盖 `_track_hand` 的 drain 时机：必须在 hand_tracker 更新后
    才点，才能用当前 hand_id / 决策点判断建议是否过期。
    """
    from poker_ocr.advice import Advice, Option, preset_amounts

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

    class Runner:
        def __init__(self) -> None:
            self.snap = None
            self._done = []

        def submit(self, sit) -> bool:
            self.snap = sit.snap
            return True

        def poll(self):
            if self.snap is None or self._done:
                return []
            snap = self.snap
            self._done.append(Advice(
                snap.hand_id, snap.street,
                Option("call", "Check", "check"), "check it", "model",
                decision_key=snap.decision_key,
                preset_amounts=tuple(preset_amounts(snap).items())))
            return list(self._done)

    clicker = FakeClicker()
    tracker = LiveHandTracker(confirm_frames=1)
    writer = StubWriter()
    cards = SimpleNamespace(hero_codes=["Ah", "Kd"], board_codes=[],
                            warnings=[])
    with patch.object(live, "recognize_cards", lambda img, profile: cards):
        proc = LiveProcessor(TurnStream(), writer, quiet=True,
                             profile=SimpleNamespace(), hand_tracker=tracker,
                             advice_runner=Runner(), clicker=clicker,
                             log=lambda *_: None)
        proc.handle(packet(1))
        check(clicker.calls == [], "建议还没返回时不该点")
        proc.handle(packet(2))

    check(len(clicker.calls) == 1,
          f"建议返回后的正常帧应点击一次：{clicker.calls}")
    advice_rows = [r for r in writer.rows if r["type"] == "advice"]
    check(len(advice_rows) == 1
          and advice_rows[0]["click"]["status"] == "clicked",
          f"advice 记录应带 clicked：{advice_rows}")


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


# ─────────────────────────── 自动点击 ───────────────────────────

def _click_advice(*, kind: str = "fold", option_id: str | None = None,
                  amount: float | None = None, source: str = "model",
                  error: str | None = None, hand_id: str = "h1",
                  key: tuple | None = None):
    from poker_ocr.advice import Advice, Option
    oid = option_id or kind
    opt = Option(oid, kind.title(), kind, amount)
    return Advice(hand_id, "flop", opt, "reason", source, error=error,
                  decision_key=key or (hand_id, "flop", 0, 6.0, 0.0,
                                       ("Ah", "Kd")))


class FakeClicker:
    """记录 run() 调用；config 只需要点击器用到的字段。"""

    def __init__(self, *, allow_fallback: bool = False,
                 status: str = "clicked", retry_count: int = 1,
                 retry_delay_ms: int = 500, dry_run: bool = False) -> None:
        self.config = SimpleNamespace(
            allow_fallback=allow_fallback,
            retry_count=retry_count,
            retry_delay_ms=retry_delay_ms)
        self.status = status
        self.dry_run = dry_run
        self.calls = []

    def run(self, advice):
        self.calls.append(advice)
        return ClickResult(self.status, action=advice.option.kind,
                           steps=(ClickStep("fold", 1.0, 2.0),))


def click_state(**overrides):
    """动作指纹用的一帧状态。"""
    base = dict(
        ok=True, hero_turn=True, dealer="hero", pot=2.5,
        pot_before_street=0.0, players_in_hand=2,
        players=[
            SimpleNamespace(seat_id="hero", action=None, fold=None,
                            in_hand=True, stack=100.0, bet=0.0,
                            all_in=False, equity=None),
            SimpleNamespace(seat_id="a", action=None, fold=False,
                            in_hand=True, stack=99.0, bet=1.0,
                            all_in=False, equity=None),
        ])
    base.update(overrides)
    return SimpleNamespace(**base)


class OneShotRunner:
    def __init__(self, advices) -> None:
        self._advices = list(advices)

    def poll(self):
        out, self._advices = self._advices, []
        return out

    def submit(self, sit) -> bool:      # pragma: no cover
        return True


def _drain_click(advice, *, clicker, state=None, allow_click: bool = True,
                 pending: bool = True, current_hand: str = "h1",
                 clock=None):
    writer = StubWriter()
    tracker = SimpleNamespace(
        hand=SimpleNamespace(hand_id=current_hand),
        take_finished=lambda: None, take_current=lambda: None)
    kwargs = {"clock": clock} if clock is not None else {}
    proc = LiveProcessor(StubStateStream(), writer, quiet=True,
                         hand_tracker=tracker, clicker=clicker,
                         advice_runner=OneShotRunner([advice]),
                         log=lambda *_: None, **kwargs)
    if pending:
        proc._pending_decision_key = advice.decision_key
    if state is None:
        state = click_state()
    proc.drain_advice(state=state, allow_click=allow_click)
    return proc, writer.rows


def test_auto_click_current_decision() -> None:
    clicker = FakeClicker()
    advice = _click_advice()
    proc, rows = _drain_click(advice, clicker=clicker)
    check(len(clicker.calls) == 1, f"当前决策点应点击：{clicker.calls}")
    check(proc.click_count == 1 and proc.click_skip_count == 0,
          f"点击计数错误：clicks={proc.click_count} "
          f"skips={proc.click_skip_count}")
    row = rows[0]
    check(row["type"] == "advice" and row["click"]["status"] == "clicked",
          f"建议记录应带 click：{row}")
    check(proc._pending_decision_key is None, "点击后应清掉待点击决策点")


def test_auto_click_skips_when_not_hero_turn() -> None:
    clicker = FakeClicker()
    proc, rows = _drain_click(
        _click_advice(), clicker=clicker,
        state=SimpleNamespace(ok=True, hero_turn=False))
    check(clicker.calls == [], "不是轮到我时绝不能点")
    check(rows[0]["click"]["reason"] == "hero_turn_false",
          f"应记录跳过原因：{rows[0]}")
    check(proc.click_skip_count == 1, "跳过计数应 +1")


def test_auto_click_skips_stale_decision() -> None:
    clicker = FakeClicker()
    advice = _click_advice()
    writer = StubWriter()
    tracker = SimpleNamespace(
        hand=SimpleNamespace(hand_id="h1"),
        take_finished=lambda: None, take_current=lambda: None)
    proc = LiveProcessor(StubStateStream(), writer, quiet=True,
                         hand_tracker=tracker, clicker=clicker,
                         advice_runner=OneShotRunner([advice]),
                         log=lambda *_: None)
    proc._pending_decision_key = ("h1", "flop", 9, 99.0, 0.0, ())
    proc.drain_advice(state=SimpleNamespace(ok=True, hero_turn=True))
    check(clicker.calls == [], "旧决策点的建议不应点击")
    check(writer.rows[0]["click"]["reason"] == "stale_decision",
          f"应记录 stale_decision：{writer.rows[0]}")
    check(proc._pending_decision_key is not None,
          "旧建议不能清掉新决策点")


def test_auto_click_skips_fallback_and_error() -> None:
    for advice, want in (
        (_click_advice(source="fallback"), "fallback_advice"),
        (_click_advice(error="timeout"), "advice_error"),
    ):
        clicker = FakeClicker()
        proc, rows = _drain_click(advice, clicker=clicker)
        check(clicker.calls == [], f"{want} 不应点击")
        check(rows[0]["click"]["reason"] == want,
              f"应记录 {want}：{rows[0]}")


def test_auto_click_allows_fallback_when_configured() -> None:
    clicker = FakeClicker(allow_fallback=True)
    proc, rows = _drain_click(_click_advice(source="fallback"),
                              clicker=clicker)
    check(len(clicker.calls) == 1, "配置允许时 fallback 也可点击")
    check(rows[0]["click"]["status"] == "clicked", f"应点击：{rows[0]}")


def test_auto_click_skips_duplicate_and_stale_hand() -> None:
    advice = _click_advice()
    clicker = FakeClicker()
    writer = StubWriter()
    tracker = SimpleNamespace(
        hand=SimpleNamespace(hand_id="h1"),
        take_finished=lambda: None, take_current=lambda: None)
    proc = LiveProcessor(StubStateStream(), writer, quiet=True,
                         hand_tracker=tracker, clicker=clicker,
                         advice_runner=OneShotRunner([advice]),
                         log=lambda *_: None)
    proc._pending_decision_key = advice.decision_key
    proc._clicked_keys.add(advice.decision_key)
    proc.drain_advice(state=SimpleNamespace(ok=True, hero_turn=True))
    check(clicker.calls == [], "同一个决策点不能点两次")
    check(writer.rows[0]["click"]["reason"] == "already_clicked",
          f"应记录 already_clicked：{writer.rows[0]}")

    clicker2 = FakeClicker()
    proc2, rows2 = _drain_click(_click_advice(), clicker=clicker2,
                                current_hand="h2")
    check(clicker2.calls == [], "上一手的建议不能在新手牌里点")
    check(rows2[0]["click"]["reason"] == "stale_hand",
          f"应记录 stale_hand：{rows2[0]}")


def test_auto_click_skips_unreliable_frame() -> None:
    clicker = FakeClicker()
    proc, rows = _drain_click(_click_advice(), clicker=clicker,
                              allow_click=False)
    check(clicker.calls == [], "牌面不可靠/换手帧不应点击")
    check(rows[0]["click"]["reason"] == "frame_not_clickable",
          f"应记录 frame_not_clickable：{rows[0]}")


def test_auto_click_off_preserves_advice_shape() -> None:
    advice = _click_advice()
    writer = StubWriter()
    tracker = SimpleNamespace(
        hand=SimpleNamespace(hand_id="h1"),
        take_finished=lambda: None, take_current=lambda: None)
    proc = LiveProcessor(StubStateStream(), writer, quiet=True,
                         hand_tracker=tracker,
                         advice_runner=OneShotRunner([advice]),
                         log=lambda *_: None)
    proc.drain_advice(state=SimpleNamespace(ok=True, hero_turn=True))
    check("click" not in writer.rows[0],
          f"未启用自动点击时不该多出 click 键：{writer.rows[0]}")


def test_auto_click_counts_errors() -> None:
    clicker = FakeClicker(status="error")
    proc, rows = _drain_click(_click_advice(), clicker=clicker)
    check(proc.click_error_count == 1,
          f"错误计数错误：{proc.click_error_count}")
    check(rows[0]["click"]["status"] == "error", f"应记录 error：{rows[0]}")


def test_auto_click_retries_after_half_second_if_unchanged() -> None:
    """点完 0.5s 页面没变化 → 再点一次；有变化则不重试。"""
    now = [100.0]
    clicker = FakeClicker(retry_count=1, retry_delay_ms=500)
    state = click_state()
    proc, rows = _drain_click(_click_advice(), clicker=clicker,
                              state=state, clock=lambda: now[0])
    check(len(clicker.calls) == 1, "首次动作应点击")
    check(proc._click_retry is not None, "点击后应挂起重试确认")

    now[0] += 0.4
    proc._service_click_retry(state)
    check(len(clicker.calls) == 1, "不足 0.5s 不应重试")

    now[0] += 0.2
    proc._service_click_retry(state)
    check(len(clicker.calls) == 2, "0.5s 未变化应重试一次")
    check(proc.click_retry_count == 1, "重试计数应 +1")
    retry_rows = [r for r in proc.writer.rows if r["type"] == "click_retry"]
    check(len(retry_rows) == 1 and retry_rows[0]["attempt"] == 1,
          f"应写一条 click_retry：{retry_rows}")
    check(proc._click_retry is None, "重试次数用完后应清空")


def test_auto_click_retry_cancelled_when_page_changes() -> None:
    now = [100.0]
    clicker = FakeClicker(retry_count=1, retry_delay_ms=500)
    state = click_state()
    proc, _rows = _drain_click(_click_advice(), clicker=clicker,
                               state=state, clock=lambda: now[0])
    now[0] += 0.6

    changed = click_state(pot=5.0)
    proc._service_click_retry(changed)
    check(len(clicker.calls) == 1, "页面已变化不应重试")
    check(proc._click_retry is None, "页面变化后应清空重试")

    # hero_turn 变成 false 同样视为动作已生效
    clicker2 = FakeClicker(retry_count=1, retry_delay_ms=500)
    proc2, _ = _drain_click(_click_advice(), clicker=clicker2,
                            state=click_state(), clock=lambda: now[0])
    now[0] += 0.6
    proc2._service_click_retry(click_state(hero_turn=False))
    check(len(clicker2.calls) == 1, "回合结束不应重试")
    check(proc2._click_retry is None, "回合结束后应清空重试")


def test_auto_click_retry_respects_count_and_dry_run() -> None:
    now = [100.0]

    # retry_count=2：连续两次未变化 → 共点 3 次
    clicker = FakeClicker(retry_count=2, retry_delay_ms=500)
    state = click_state()
    proc, _ = _drain_click(_click_advice(), clicker=clicker,
                           state=state, clock=lambda: now[0])
    now[0] += 0.6
    proc._service_click_retry(state)
    check(len(clicker.calls) == 2, "第一次重试应点击")
    check(proc._click_retry is not None, "还有剩余次数应继续挂起")
    now[0] += 0.6
    proc._service_click_retry(state)
    check(len(clicker.calls) == 3, "第二次重试应点击")
    check(proc.click_retry_count == 2, "重试计数应为 2")
    check(proc._click_retry is None, "次数用完后应清空")

    # dry-run 不挂重试（没有真实点击，页面永远不会变）
    dry = FakeClicker(status="dry_run", dry_run=True, retry_count=2)
    proc2, _ = _drain_click(_click_advice(), clicker=dry,
                            state=click_state(), clock=lambda: now[0])
    check(proc2._click_retry is None, "dry-run 不应挂重试")

    # retry_count=0 关闭重试
    off = FakeClicker(retry_count=0)
    proc3, _ = _drain_click(_click_advice(), clicker=off,
                            state=click_state(), clock=lambda: now[0])
    check(proc3._click_retry is None, "retry_count=0 不应挂重试")


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

    # cam:auto：优先 OBS 候选索引；打不开就继续下一个
    calls.clear()
    with patch.object(live, "_camera_candidates", lambda: [2, 0, 1]), \
         patch.object(live, "_camera_names",
                      lambda: ["FaceTime", "OBS Virtual Camera", "Other"]), \
         patch.object(live.cv2, "VideoCapture", side_effect=factory):
        cap = live._open_capture("cam:auto")
    check(cap is not None and cap.src == 1,
          f"cam:auto 应跳过打不开的候选，选中可用的 1：{calls}")
    check(calls[:3] == [(2, live.cv2.CAP_AVFOUNDATION), (2, None),
                        (0, live.cv2.CAP_AVFOUNDATION)],
          f"cam:auto 候选顺序不对：{calls}")


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
        test_auto_click_defaults_on_and_follows_advice,
        test_mailbox_keeps_only_latest,
        test_wait_newer_wakes_up,
        test_slow_consumer_drops_frames_but_ends_at_latest,
        test_throttle_delay,
        test_settle_gate_coalesces_action_burst,
        test_settle_gate_max_wait_limits_starvation,
        test_settle_gate_ignores_blind_clock_and_avatar_ring,
        test_settle_gate_waits_for_card_rois,
        test_card_regression_dumps_incident_frames,
        test_transient_anomaly_is_not_dumped,
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
        test_auto_click_after_observe_on_next_frame,
        test_ai_advice_waits_for_hero_cards,
        test_unreliable_cards_skip_advice_until_recovered,
        test_hand_memory_keeps_action_line_and_advices,
        test_hand_memory_flushes_on_switch_frame,
        test_auto_click_current_decision,
        test_auto_click_skips_when_not_hero_turn,
        test_auto_click_skips_stale_decision,
        test_auto_click_skips_fallback_and_error,
        test_auto_click_allows_fallback_when_configured,
        test_auto_click_skips_duplicate_and_stale_hand,
        test_auto_click_skips_unreliable_frame,
        test_auto_click_off_preserves_advice_shape,
        test_auto_click_counts_errors,
        test_auto_click_retries_after_half_second_if_unchanged,
        test_auto_click_retry_cancelled_when_page_changes,
        test_auto_click_retry_respects_count_and_dry_run,
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
