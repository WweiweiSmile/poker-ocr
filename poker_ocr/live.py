"""实时拉流 → 状态 JSONL（M3.1）。

    .venv/bin/python -m poker_ocr.live \
        --url rtsp://127.0.0.1:8554/live \
        --out logs/live.states.jsonl

链路：OBS ──SRT──▶ MediaMTX ──RTSP──▶ 本进程。

设计要点：

  · **只保留最新帧**。reader 线程按源帧率连续 read，邮箱里永远只有一帧；
    处理不过来就覆盖旧帧，绝不让延迟越积越大。
  · **单 OCR 线程**。onnxruntime 的 session 不保证线程安全，reader 只做解码，
    `StateStream.ingest()` 永远只在主循环里调用。
  · **0.5s 是目标节奏，不是硬承诺**。单帧实测约 0.56s，忙时自然丢帧，
    有效频率约 1.8Hz。
  · **断流自动重连**。重连后清空 ROI 缓存和状态机，下一帧重新当基线；
    累计计数保留，JSONL 里用 `epoch` 区分。
  · **输出 JSONL**：一行一个完整 JSON。`session_start` / `stream_info` /
    `state` / `session_end`。每条立即 flush，硬杀最多丢最后半行。
  · **每帧读牌**：`cards.recognize_cards()` 在 OCR 之前跑（纯 OpenCV，几毫秒）。
    `state` 记录按「个人手牌 → 位置 → 筹码 → 公共牌 → 全局 → 其他对手」排序；
    牌面变化也并进状态机指纹，翻牌/换手不会被判成 no_change。

延迟口径（可用 --dump-frames 实测）：

  · `queue_ms`：帧被 read 出来 → 开始 OCR 的排队时间（不含网络/解码缓冲）。
  · `proc_ms`：这一帧 OCR + 状态机 + 读牌耗时。
  · 帧内时钟到 `ts` 的差 = 传输 + 排队 + OCR。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .advice import AISettings, AdviceRunner, build_situation
from .cards import CardsResult, recognize_cards
from .env import EnvFileError, load_env
from .full_table import (DealerTracker, FullOutcome, FullStateRecorder,
                         FullTableConfig, load_full_config, observe_full)
from .hand_track import LiveHandTracker
from .ocr import OcrEngine
from .parse import parse_blinds
from .profile import Profile
from .state_stream import StateStream

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"
DEFAULT_URL = "rtsp://127.0.0.1:8554/live"
DEFAULT_OUT = Path("logs/live.states.jsonl")
DEFAULT_EVERY = 0.5

# 让 OpenCV 的 FFmpeg 后端用 TCP、少缓冲。必须在第一次 VideoCapture 之前设置。
FFMPEG_OPTIONS = ("rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0")


# ─────────────────────────── 基础组件 ───────────────────────────

def _log(msg: str) -> None:
    print(msg, flush=True)


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class FramePacket:
    seq: int                 # 全局递增，跨 epoch 也不回退
    epoch: int               # 第几次成功连上 RTSP（从 1 开始）
    grab_mono: float         # read() 返回时的 monotonic 时间
    image: np.ndarray        # BGR


class LatestFrame:
    """只保留最新一帧的邮箱。处理慢时旧帧被覆盖，队列深度恒为 1。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._packet: FramePacket | None = None
        self._closed = False
        self.published = 0

    def publish(self, packet: FramePacket) -> None:
        with self._cond:
            self._packet = packet
            self.published += 1
            self._cond.notify_all()

    def wait_newer(self, last_seq: int, timeout: float = 0.5) -> FramePacket | None:
        """等到一个 seq > last_seq 的包；超时或关闭返回 None。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                # 先交付还在邮箱里的新帧；关闭后也要让最后一帧被消费掉
                if self._packet is not None and self._packet.seq > last_seq:
                    return self._packet
                if self._closed:
                    return None
                rest = deadline - time.monotonic()
                if rest <= 0:
                    return None
                self._cond.wait(rest)

    def latest(self) -> FramePacket | None:
        with self._cond:
            return self._packet

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


def throttle_delay(last_start: float, now: float, every: float) -> float:
    """距离下一次允许采样还要等多久（秒）。"""
    return max(0.0, last_start + every - now)


# ─────────────────────────── 画布裁剪 ───────────────────────────

@dataclass(frozen=True)
class CropPlan:
    """把输入画布裁成训练图片的宽高比。"""

    mode: str
    src_size: tuple[int, int]                 # (w, h)
    rect: tuple[int, int, int, int] | None    # x1, y1, x2, y2
    out_size: tuple[int, int]                 # (w, h)
    warning: str | None = None

    def apply(self, img: np.ndarray) -> np.ndarray:
        if self.rect is None:
            return img
        x1, y1, x2, y2 = self.rect
        return img[y1:y2, x1:x2]

    def to_debug(self) -> dict:
        out: dict = {
            "mode": self.mode,
            "src": list(self.src_size),
            "out": list(self.out_size),
        }
        if self.rect is not None:
            out["rect"] = list(self.rect)
        if self.warning:
            out["warning"] = self.warning
        return out


def _content_bbox(img: np.ndarray, threshold: int = 8) -> tuple[int, int, int, int] | None:
    """非黑画面的外接框。用于 auto 裁剪：OBS 把牌桌放左边、右边留黑时直接切掉。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (gray > threshold).astype(np.uint8)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


@dataclass(frozen=True)
class AspectFit:
    """把画面重采样成 profile 的参考宽高比，让 ROI 的 x/y 缩放一致。

    ROI 槽位是归一化坐标，**只有输入宽高比 = ``reference_size`` 的宽高比时**，
    x/y 才共用同一个像素缩放。流的分辨率/比例和标定图不同时（OBS 缩放、
    播放器把竖屏视频拉伸、低分辨率采集），按宽高各自归一化会让所有槽位在
    x 方向按错误比例展开 —— 越靠边的座位偏得越多，越靠下/上的槽位也会错。

    做法：**以高度为准，只改宽度**。

        out_w = round(h * ref_w / ref_h)

    这样两个轴的缩放都等于 ``h / ref_h``，槽位的相对位置和标定图一一对应。
    选高度而不是宽度，是因为低分辨率流横向像素本来就不够 —— 把 326x720
    拉成 1280x2844 只会放大噪声；保持 720 高、宽度回到 324 才是原比例。
    输入比例本来就匹配时（1080x2400、864x1920）这一步是 no-op。
    """

    src_size: tuple[int, int]                  # 处理前的 (w, h)
    out_size: tuple[int, int]                  # 重采样后的 (w, h)
    aspect: float                              # 处理前的 w / h
    reference_size: tuple[int, int]            # profile.reference_size
    reference_aspect: float = 0.0              # reference_size 的 w / h

    @property
    def resized(self) -> bool:
        return self.out_size != self.src_size

    @property
    def deviation(self) -> float:
        """处理前宽高比相对参考比例的偏差（0.01 = 1%）。"""
        if not self.reference_aspect:
            return 0.0
        return abs(self.aspect - self.reference_aspect) / self.reference_aspect

    @property
    def scale(self) -> tuple[float, float]:
        """(sx, sy)：参考图 → 处理后。正常应当 sx == sy，这正是 ROI 能对齐的前提。"""
        ref_w, ref_h = self.reference_size
        ow, oh = self.out_size
        return ow / max(1, ref_w), oh / max(1, ref_h)

    @classmethod
    def for_size(cls, size: tuple[int, int], profile: Profile) -> "AspectFit":
        w, h = size
        ref_w, ref_h = profile.reference_size
        ref_aspect = ref_w / ref_h if ref_h else 1.0
        out_w = max(1, round(h * ref_aspect)) if h else max(1, w)
        return cls((w, h), (out_w, h), w / h if h else ref_aspect,
                   (ref_w, ref_h), ref_aspect)

    def apply(self, img: np.ndarray) -> np.ndarray:
        if not self.resized:
            return img
        w, h = self.src_size
        ow, oh = self.out_size
        interp = cv2.INTER_AREA if ow < w else cv2.INTER_CUBIC
        return cv2.resize(img, (ow, oh), interpolation=interp)

    def to_debug(self) -> dict:
        d: dict = {
            "src": list(self.src_size),
            "out": list(self.out_size),
            "aspect": round(self.aspect, 6),
            "reference": list(self.reference_size),
            "reference_aspect": round(self.reference_aspect, 6),
            "deviation": round(self.deviation, 6),
            "scale": [round(s, 6) for s in self.scale],
        }
        if self.resized:
            d["resized"] = True
        return d


def plan_crop(size: tuple[int, int], profile: Profile,
              mode: str = "auto",
              image: np.ndarray | None = None) -> CropPlan:
    """把画布裁成牌桌区域。

    三种模式：
      · `auto`（默认）：找非黑内容的包围盒。OBS 左边放牌桌、右边留黑时直接切掉；
        内容铺满画布时不裁。全图路线不依赖训练比例，用这个最稳。
      · `left`：按训练图片宽高比 0.45，从左侧裁 `h * 1080 / 2400`。
      · `none`：不裁。
    """
    h, w = size
    if mode == "none":
        return CropPlan("none", (w, h), None, (w, h))

    if mode == "auto":
        if image is None:
            return CropPlan("auto", (w, h), None, (w, h), "auto 裁剪需要图片")
        bbox = _content_bbox(image)
        if bbox is None:
            return CropPlan("auto", (w, h), None, (w, h),
                            "整帧接近全黑，无法自动裁剪")
        x1, y1, x2, y2 = bbox
        # 小于画布 5% 的亮块多半是光标/图标，不当画面
        if (x2 - x1) * (y2 - y1) < w * h * 0.05:
            return CropPlan("auto", (w, h), None, (w, h),
                            "非黑区域太小，自动裁剪已跳过")
        # 内容已经贴边：不裁
        if x1 <= 2 and y1 <= 2 and x2 >= w - 2 and y2 >= h - 2:
            return CropPlan("auto", (w, h), None, (w, h))
        return CropPlan("auto", (w, h), bbox, (x2 - x1, y2 - y1))

    ref_w, ref_h = profile.reference_size
    target_w = max(1, round(h * ref_w / ref_h))
    if target_w < w:
        return CropPlan(mode, (w, h), (0, 0, target_w, h), (target_w, h))

    warning = None
    ref_aspect = ref_w / ref_h
    if h and abs(w / h - ref_aspect) / ref_aspect > 0.02:
        warning = (f"画布 {w}x{h} 比训练比例窄，无法按左侧裁出 {target_w}px；"
                   f"请把 OBS 输出改成竖屏，或让画面保持 0.45 宽高比")
    return CropPlan(mode, (w, h), None, (w, h), warning)


class JsonlWriter:
    """一行一个 JSON；每条立即 flush。默认覆盖写。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = open(path, "w", encoding="utf-8", newline="\n")

    def write(self, record: dict) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()


# ─────────────────────────── 读流 ───────────────────────────

def _state_body(record: dict, cards: CardsResult | None, recognizer: str) -> dict:
    """把状态记录重排成：个人手牌/位置/筹码 → 公共牌 → 全局 → 其他对手。

    ROI 模式的 hero 是 `players` 里 `seat_id == hero_seat_id` 的那条；
    full 模式的 hero 是 `seats` 里 `is_hero=True` 的那条。hero 从原列表摘出来
    单独放，opponents 永远排在最后 —— 这是调用方明确要求的输出顺序。

    `hero` 里保留 hero 的其余字段（名字/下注/动作/全下），避免为了重排丢信息。
    """
    rec = dict(record)
    if recognizer == "full":
        seats = rec.pop("seats", None) or []
        hero = next((s for s in seats if s.get("is_hero")), None)
        opponents = [s for s in seats if s is not hero]
    else:
        players = rec.pop("players", None) or []
        hero_id = rec.pop("hero_seat_id", None)
        hero = next((p for p in players if p.get("seat_id") == hero_id), None)
        # 空座位 / 暂离不是「对手」；in_hand 的（含已弃牌）才算
        opponents = [p for p in players
                     if p.get("seat_id") != hero_id and p.get("in_hand", True)]

    body: dict = {}
    for key in ("index", "t", "frame"):
        if key in rec:
            body[key] = rec.pop(key)

    hero = hero or {}
    body["hero_cards"] = cards.hero_codes if cards is not None else []
    body["hero_position"] = hero.get("position")
    body["hero_stack"] = hero.get("stack")
    body["board"] = cards.board_codes if cards is not None else []
    body["hero"] = hero or None
    body.update(rec)                 # 底池 / 盲注 / 庄位 / 轮到我 / 桌型……
    if cards is not None and cards.warnings:
        body["cards_warnings"] = cards.warnings
    body["opponents"] = opponents    # 其他对手永远最后
    return body


def _state_line(t: float, body: dict, tail: str = "") -> str:
    """日志行：顺序与 JSONL 一致（我 → 公共牌 → 全局 → 对手）。"""
    hero = body.get("hero") or {}
    cards = " ".join(body.get("hero_cards") or []) or "-"
    board = " ".join(body.get("board") or []) or "-"
    opponents = " ".join(
        f"{p.get('seat_id')}:{p.get('name') or '-'}={p.get('stack')}"
        f"{'/' + str(p.get('bet')) if p.get('bet') else ''}"
        f"{'(' + p.get('action') + ')' if p.get('action') else ''}"
        for p in (body.get("opponents") or [])
    ) or "-"
    return (f"  [{t:7.2f}s] ✅ 记录 #{body.get('index')}  "
            f"我: {cards} 位置={hero.get('position') or '-'} "
            f"筹码={hero.get('stack')}  公共牌: {board}{tail}  "
            f"对手: {opponents}")


class RtspReader(threading.Thread):
    """持续读 RTSP，把最新帧丢进邮箱；断了就 1s 后重连。

    只做解码，**绝不调用 OCR / StateStream**。
    """

    def __init__(self, url: str, mailbox: LatestFrame, stop: threading.Event, *,
                 reconnect_delay: float = 1.0, log=_log,
                 capture_factory=None) -> None:
        super().__init__(daemon=True, name="rtsp-reader")
        self.url = url
        self.mailbox = mailbox
        self.stop_event = stop
        self.reconnect_delay = reconnect_delay
        self.log = log
        self._capture_factory = capture_factory or _open_capture

        self.seq = 0
        self.epoch = 0
        self.reconnects = 0
        self.grabbed = 0
        self.last_grab_mono = 0.0
        self.connected_once = False

    def run(self) -> None:
        while not self.stop_event.is_set():
            cap = self._capture_factory(self.url)
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                self.reconnects += 1
                self.log(f"[重连] 打不开 {self.url}，{self.reconnect_delay:.0f}s 后重试")
                self.stop_event.wait(self.reconnect_delay)
                continue

            self.epoch += 1
            self.connected_once = True
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            self.log(f"[连接] RTSP 已连接 epoch={self.epoch}：{w}x{h} @ {fps:.1f}fps")

            try:
                while not self.stop_event.is_set():
                    ok, img = cap.read()
                    if not ok:
                        break
                    self.seq += 1
                    self.grabbed += 1
                    self.last_grab_mono = time.monotonic()
                    self.mailbox.publish(FramePacket(
                        seq=self.seq, epoch=self.epoch,
                        grab_mono=self.last_grab_mono, image=img))
            finally:
                cap.release()

            if not self.stop_event.is_set():
                self.reconnects += 1
                self.log(f"[重连] 流中断，{self.reconnect_delay:.0f}s 后重试")
                self.stop_event.wait(self.reconnect_delay)


def _open_capture(url: str):
    """打开 RTSP。优先显式 FFmpeg 后端；失败再让 OpenCV 自选。"""
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    for name, value in (("CAP_PROP_OPEN_TIMEOUT_MSEC", 5000),
                        ("CAP_PROP_READ_TIMEOUT_MSEC", 5000)):
        prop = getattr(cv2, name, None)
        if prop is not None:
            cap.set(prop, value)
    return cap


# ─────────────────────────── 处理 ───────────────────────────

class LiveProcessor:
    """一帧包的完整处理：限速、断流重置、OCR、写 JSONL。单线程调用。"""

    def __init__(self, state_stream: StateStream | None, writer: JsonlWriter, *,
                 recognizer: str = "roi",
                 engine: OcrEngine | None = None,
                 full_config: FullTableConfig | None = None,
                 profile: Profile | None = None,
                 stable_frames: int = 2,
                 dealer_carry: int = 4,
                 every: float = DEFAULT_EVERY, max_frame_age: float = 2.0,
                 dump_dir: Path | None = None, quiet: bool = False,
                 hand_tracker: LiveHandTracker | None = None,
                 advice_runner: AdviceRunner | None = None,
                 clock=time.monotonic, log=_log) -> None:
        if recognizer not in ("roi", "full"):
            raise ValueError(f"未知 recognizer：{recognizer}")
        if recognizer == "full" and (engine is None or full_config is None):
            raise ValueError("full 模式需要 engine 和 full_config")
        if recognizer == "roi" and state_stream is None:
            raise ValueError("roi 模式需要 state_stream")
        self.recognizer = recognizer
        self.state_stream = state_stream
        self.engine = engine
        self.full_config = full_config
        # 读牌只需要 profile 的 board / hero_cards 归一化槽位，不需要 OCR 引擎。
        # None 表示不读牌（单测里构造 LiveProcessor 时通常不传）。
        self.profile = profile
        self.full_recorder = (FullStateRecorder(stable_frames=stable_frames)
                              if recognizer == "full" else None)
        self.dealer_tracker = (DealerTracker(max_carry=dealer_carry)
                               if recognizer == "full" else None)
        self.writer = writer
        self.every = every
        self.max_frame_age = max_frame_age
        self.dump_dir = dump_dir
        self.quiet = quiet
        # AI 建议只在 roi 路线可用：全图路线读不到每座的「本街下注」，
        # 也没有 hero_turn，算不出跟注额与选项金额
        self.hand_tracker = hand_tracker
        self.advice_runner = advice_runner
        self.advice_count = 0
        self.clock = clock
        self.log = log

        self.started_mono = clock()
        self.last_epoch = 0
        self.processed = 0
        self.total_proc_ms = 0.0
        self.last_proc_ms = 0.0
        self._stale_logged = 0.0
        self._cards_error_logged = False

        if dump_dir is not None:
            dump_dir.mkdir(parents=True, exist_ok=True)

    def handle(self, packet: FramePacket) -> str:
        now = self.clock()

        # 换了一次连接：ROI 缓存和跨帧状态全部作废，下一帧重新当基线
        if packet.epoch != self.last_epoch:
            if self.last_epoch != 0:
                self.log(f"[重连] epoch {self.last_epoch} → {packet.epoch}，重置状态机")
                if self.state_stream is not None:
                    self.state_stream.reset()
                elif self.full_recorder is not None:
                    self.full_recorder.reset()
                if self.dealer_tracker is not None:
                    self.dealer_tracker.reset()
                if self.hand_tracker is not None:
                    self.hand_tracker.reset()
            self.last_epoch = packet.epoch

        age = now - packet.grab_mono
        if age > self.max_frame_age:
            # 邮箱里躺太久的帧（刚断流/卡顿）不处理，避免把旧画面当新状态
            if now - self._stale_logged >= 5.0:
                self.log(f"[跳过] 陈旧帧 {age * 1000:.0f}ms（上限 "
                         f"{self.max_frame_age * 1000:.0f}ms）")
                self._stale_logged = now
            return "stale"

        if self.dump_dir is not None:
            stamp = datetime.now().strftime("%H%M%S_%f")
            name = (self.dump_dir /
                    f"e{packet.epoch}_{packet.seq:06d}_{stamp}.jpg")
            cv2.imwrite(str(name), packet.image,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])

        t = now - self.started_mono
        frame_id = f"live_e{packet.epoch}_{packet.seq:06d}_{t:07.2f}s"
        t0 = self.clock()
        # 每帧读一次牌。纯 OpenCV，几个毫秒；失败不致命，记一次日志后继续。
        cards = self._read_cards(packet.image)
        cards_sig = self._cards_signature(cards)
        if self.recognizer == "full":
            return self._handle_full(packet, t, frame_id, age, t0,
                                     cards, cards_sig)
        outcome = self.state_stream.ingest(
            packet.image, frame_id=frame_id, t=t,
            extra_signature=cards_sig)
        proc_ms = (self.clock() - t0) * 1000
        self.processed += 1
        self.total_proc_ms += proc_ms
        self.last_proc_ms = proc_ms

        if outcome.record is not None:
            body = _state_body(outcome.record, cards, "roi")
            self.writer.write({
                "type": "state",
                "seq": packet.seq,
                "epoch": packet.epoch,
                "ts": _iso_now(),
                "queue_ms": round(age * 1000, 1),
                "proc_ms": round(proc_ms, 1),
                **body,
            })
            if not self.quiet:
                state = outcome.state
                self.log(_state_line(
                    t, body,
                    f"  pot={state.pot} pbs={state.pot_before_street} "
                    f"轮到我={state.hero_turn}"))
        elif outcome.kind == "discard":
            if not self.quiet:
                self.log(f"  [{t:7.2f}s] 丢弃 {','.join(outcome.errors)}"
                         f"  (复用 {outcome.reused}/{outcome.n_roi})")
        elif not self.quiet:
            self.log(f"  [{t:7.2f}s] 无变化 (复用 {outcome.reused}/{outcome.n_roi})")

        # AI 建议：吃**每一帧被接受的状态**（不只被记录的）。签名里没有 action /
        # hero_turn，只吃 record 会漏掉大部分行动线。
        self._track_hand(outcome, cards)
        return outcome.kind

    def _track_hand(self, outcome, cards: CardsResult | None) -> None:
        """累积行动线；轮到我时把决策点投给后台建议线程。

        先取结果再投新决策点：上一回合的答案先落库，再排新的。
        """
        self.drain_advice()

        if self.hand_tracker is None or outcome.state is None or not outcome.state.ok:
            return
        hero_codes = cards.hero_codes if cards is not None else []
        board = cards.board_codes if cards is not None else []
        point = self.hand_tracker.observe(outcome.state, hero_codes, board)
        if point is not None:
            self.advice_count += 1
            if self.advice_runner is not None:
                # 一个坏快照不能把整条拉流链路带下去
                try:
                    situation = build_situation(point)
                except Exception as exc:      # noqa: BLE001
                    self.log(f"  [建议] 局面构建失败：{type(exc).__name__}: {exc}")
                    return
                self.advice_runner.submit(situation)
                if not self.quiet:
                    self.log(f"  [建议] 轮到我：{point.street} 手牌 "
                             f"{''.join(point.hero_cards) or '?'} "
                             f"牌面 {''.join(point.board) or '-'} "
                             f"需跟 {point.to_call}BB（生成中…）")
            elif not self.quiet:
                self.log(f"  [建议] 轮到我：{point.street} 需跟 {point.to_call}BB"
                         f"（未开启 AI 建议）")

    def drain_advice(self, final: bool = False) -> None:
        """把已完成的手牌/建议写进 JSONL 并打印。

        `final=True` 在退出时调用：最后一手牌等不到「换手」，也要落库。
        """
        if self.hand_tracker is not None:
            finished = self.hand_tracker.take_finished()
            if finished is not None:
                self.writer.write({"type": "hand", "ts": _iso_now(), **finished})
            if final:
                current = self.hand_tracker.take_current()
                if current is not None:
                    self.writer.write({"type": "hand", "ts": _iso_now(),
                                       "partial": True, **current})

        if self.advice_runner is None:
            return
        for advice in self.advice_runner.poll():
            self.writer.write({"type": "advice", "ts": _iso_now(),
                               **advice.to_dict()})
            self._log_advice(advice)

    def _log_advice(self, advice) -> None:
        option = advice.option
        amount = f" {option.amount}BB" if option.amount is not None else ""
        tail = f"（{advice.error}）" if advice.error else ""
        self.log(f"  [建议] {advice.street} ⇒ {option.label}{amount}"
                 f"｜{advice.reason}{tail}")

    def _read_cards(self, img: np.ndarray) -> CardsResult | None:
        """每帧读一次公共牌 / hero 手牌。

        没传 profile（单测）时不读。识别异常不让 worker 挂掉：记一次日志，
        这一帧按「没有牌」处理，后续帧继续尝试。
        """
        if self.profile is None:
            return None
        try:
            return recognize_cards(img, self.profile)
        except Exception as exc:  # noqa: BLE001 — 读牌失败不该中断拉流
            if not self._cards_error_logged:
                self.log(f"[警告] 读牌失败（后续帧继续尝试）：{exc}")
                self._cards_error_logged = True
            return None

    @staticmethod
    def _cards_signature(cards: CardsResult | None) -> tuple | None:
        """牌面指纹：公共牌或 hero 手牌变化时触发一条新记录。"""
        if cards is None:
            return None
        return (tuple(cards.hero_codes), tuple(cards.board_codes))

    def _handle_full(self, packet: FramePacket, t: float, frame_id: str,
                     age: float, t0: float,
                     cards: CardsResult | None,
                     cards_sig: tuple | None) -> str:
        """全图 OCR 路线：不认识固定 profile，按文字/位置聚类。"""
        result = observe_full(packet.image, self.engine, self.full_config)
        if self.dealer_tracker is not None:
            result = self.dealer_tracker.apply(result)
        outcome: FullOutcome = self.full_recorder.push(
            result, t, frame_id, extra_signature=cards_sig)
        proc_ms = (self.clock() - t0) * 1000
        self.processed += 1
        self.total_proc_ms += proc_ms
        self.last_proc_ms = proc_ms

        if outcome.record is not None:
            body = _state_body(outcome.record, cards, "full")
            self.writer.write({
                "type": "state",
                "seq": packet.seq,
                "epoch": packet.epoch,
                "ts": _iso_now(),
                "queue_ms": round(age * 1000, 1),
                "proc_ms": round(proc_ms, 1),
                **body,
            })
            if not self.quiet:
                carried = "（沿用）" if result.dealer_status == "carried" else ""
                self.log(_state_line(
                    t, body,
                    f"  桌型={result.table_size} "
                    f"庄位={result.dealer_seat_id}{carried}"))
        elif outcome.kind == "discard":
            if not self.quiet:
                self.log(f"  [{t:7.2f}s] 丢弃 座位={result.table_size} "
                         f"庄位={result.dealer_status}")
        elif not self.quiet:
            self.log(f"  [{t:7.2f}s] 无变化 ({result.table_size} 座)")

        return outcome.kind

    @property
    def sampled(self) -> int:
        if self.full_recorder is not None:
            return self.full_recorder.sampled
        return self.state_stream.sampled  # type: ignore[union-attr]

    @property
    def accepted(self) -> int:
        if self.full_recorder is not None:
            return self.full_recorder.accepted
        return self.state_stream.accepted  # type: ignore[union-attr]

    def summary(self) -> dict:
        if self.full_recorder is not None:
            return {**self.full_recorder.summary(), "recognizer": "full"}
        return {**self.state_stream.summary(), "recognizer": "roi"}  # type: ignore[union-attr]

    @property
    def avg_proc_ms(self) -> float:
        return self.total_proc_ms / self.processed if self.processed else 0.0


# ─────────────────────────── CLI ───────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="实时拉 RTSP 流，按间隔识别牌桌状态并写 JSONL")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"RTSP 地址（默认 {DEFAULT_URL}）")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--initial-blinds", default=None,
                    help="盲注横幅读不到时的首帧盲注，如 1/2（之后走正常继承）")
    ap.add_argument("--lenient-chips", action="store_true",
                    help="筹码单位被读坏时取前导数字（roi 模式；固定筹码槽上下文下使用）")
    ap.add_argument("--recognizer", choices=("roi", "full"), default="roi",
                    help="roi=固定槽位（快）；full=全图 OCR+座位聚类（不依赖布局，慢）")
    ap.add_argument("--full-config", type=Path, default=None,
                    help="full 模式的参数 JSON（FullTableConfig 字段）")
    ap.add_argument("--every", type=float, default=DEFAULT_EVERY,
                    help="采样间隔秒（默认 0.5）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"JSONL 输出（默认 {DEFAULT_OUT}）")
    ap.add_argument("--stable-frames", type=int, default=2,
                    help="新状态连续出现几帧才记录（默认 2）")
    ap.add_argument("--max-vanish", type=int, default=3,
                    help="在场玩家整座空白连续几帧才认定离开（默认 3）")
    ap.add_argument("--name-confirm", type=int, default=3,
                    help="名字连续确认几帧才换（默认 3）")
    ap.add_argument("--dealer-carry", type=int, default=4,
                    help="full 模式 D 暂未读到时沿用上一庄位的最大帧数（默认 4；0=不沿用）")
    ap.add_argument("--max-frame-age", type=float, default=2.0,
                    help="超过多少秒的帧直接跳过（默认 2）")
    ap.add_argument("--crop", choices=("auto", "left", "none"), default=None,
                    help="画布裁剪：auto=按非黑内容自动裁（默认）；"
                         "left=按训练比例裁左侧；none=不裁")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="最多处理多少帧（调试用，0=不限）")
    ap.add_argument("--no-dedup", action="store_true", help="关掉 ROI 去重")
    ap.add_argument("--no-fold", action="store_true", help="不读头像（省 9 块 ROI）")
    ap.add_argument("--dump-frames", type=Path, default=None,
                    help="把每个采样帧存成 JPEG，文件名带墙钟时间（量延迟用）")
    ap.add_argument("--ai-advice", action="store_true",
                    help="轮到我时按当前街加载技能并调用 AI 给一个选项"
                         "（需要 AI_SERVER_API_KEY / AI_SERVER_BASE_URL / "
                         "AI_SERVER_MODEL；仅 roi 模式）")
    ap.add_argument("--ai-timeout", type=float, default=20.0,
                    help="单次 AI 建议的超时秒数（默认 20）")
    ap.add_argument("--ai-confirm", type=int, default=2,
                    help="轮到我需要连续几帧确认（默认 2；真机按钮会闪断）")
    ap.add_argument("--quiet", action="store_true", help="只打状态行和最终汇总")
    return ap


def _validate_args(ap: argparse.ArgumentParser, args) -> None:
    if args.every <= 0:
        ap.error("--every 必须大于 0")
    if args.max_frame_age <= 0:
        ap.error("--max-frame-age 必须大于 0")
    if args.stable_frames < 1:
        ap.error("--stable-frames 至少为 1")
    if args.max_vanish < 0:
        ap.error("--max-vanish 不能为负")
    if args.name_confirm < 1:
        ap.error("--name-confirm 至少为 1")
    if args.dealer_carry < 0:
        ap.error("--dealer-carry 不能为负")
    if args.max_frames < 0:
        ap.error("--max-frames 不能为负")
    if args.ai_advice and args.recognizer != "roi":
        ap.error("--ai-advice 只支持 --recognizer roi：全图路线读不到每座本街下注和轮到我")
    if args.ai_timeout <= 0:
        ap.error("--ai-timeout 必须大于 0")
    if args.ai_confirm < 1:
        ap.error("--ai-confirm 至少为 1")


def _install_signals(stop: threading.Event) -> None:
    def handler(_signum, _frame):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    _validate_args(ap, args)

    # `.env` 在任何配置读取之前加载：已存在的环境变量优先，文件只补齐。
    # 找不到文件不报错（全靠环境变量是合法部署方式），但 --ai-advice 下会告警 ——
    # 「.env 明明放了却没生效」是这类配置最难查的故障
    try:
        env_load = load_env()
    except EnvFileError as exc:
        ap.error(str(exc))
    if env_load.found:
        overridden = (f"，其中 {len(env_load.kept)} 项环境变量已存在、未覆盖"
                      if env_load.kept else "")
        _log(f"[启动] 已加载 .env：{env_load.path}（{len(env_load.applied)} 项{overridden}）")

    if args.crop is None:
        args.crop = "auto"
    initial_blinds = None
    if args.initial_blinds:
        # 人工指定的盲注允许高比例（实测桌面有 1/20）；OCR 自动读数仍用默认上限
        initial_blinds = parse_blinds(args.initial_blinds, max_ratio=50.0)
        if initial_blinds is None:
            ap.error(f"--initial-blinds 解析不了：{args.initial_blinds}")

    # 必须在创建 VideoCapture 之前设置
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", FFMPEG_OPTIONS)

    started_wall = _iso_now()
    started_mono = time.monotonic()
    stop = threading.Event()
    _install_signals(stop)

    profile = Profile.load(args.profile)      # 配置非法时内部 sys.exit
    _log(f"[启动] profile={profile.path.name} 座位={len(profile.seats)}")
    _log("[启动] 加载 OCR 模型…")
    engine = OcrEngine()
    _log("[启动] OCR 就绪")

    full_config = None
    state_stream = None
    if args.recognizer == "full":
        try:
            full_config = load_full_config(args.full_config)
        except ValueError as exc:
            ap.error(str(exc))
        _log(f"[启动] recognizer=full 配置={args.full_config or '默认'}")
    else:
        state_stream = StateStream(
            profile, engine,
            detect_fold=not args.no_fold,
            dedup=not args.no_dedup,
            stable_frames=args.stable_frames,
            max_vanish=args.max_vanish,
            name_confirm=args.name_confirm,
            initial_blinds=initial_blinds,
            lenient_chips=args.lenient_chips,
        )

    advice_runner = None
    hand_tracker = None
    if args.ai_advice:
        if not env_load.found:
            _log("[建议] 没找到 .env（找过 "
                 + "、".join(str(p) for p in env_load.tried)
                 + "），改用环境变量")
        source = "dotenv" if "AI_SERVER_API_KEY" in env_load.applied else "env"
        settings = AISettings.from_env(source=source)
        if settings is None:
            ap.error("--ai-advice 需要 AI_SERVER_API_KEY、AI_SERVER_BASE_URL、"
                     "AI_SERVER_MODEL（写在项目根目录的 .env 里，或直接设为环境变量）")
        if "hero_turn_probe" not in profile.global_slots:
            # 静默不触发是最难查的故障：直接拒绝启动，让人现在就去补槽位
            ap.error(
                "当前 profile 没有 hero_turn_probe 槽位，判不出「轮到我」，"
                "AI 建议永远不会触发。table_wpk_live.json 由 refit 生成时没有保留"
                "该槽位；请补上（可参考 table_wpk_1080x2400.json 的坐标并在真机帧上"
                "验证），或改用带该槽位的 profile")
        advice_runner = AdviceRunner(settings, timeout=args.ai_timeout, log=_log)
        hand_tracker = LiveHandTracker(confirm_frames=args.ai_confirm)
        _log(f"[建议] 已启用 AI 建议：{settings.model}"
             f"（凭据来源 {settings.source}，超时 {args.ai_timeout}s，"
             f"连续 {args.ai_confirm} 帧确认）")

    writer = JsonlWriter(args.out)
    writer.write({
        "type": "session_start",
        "started_at": started_wall,
        "url": args.url,
        "profile": profile.path.name,
        "recognizer": args.recognizer,
        "full_config": str(args.full_config) if args.full_config else None,
        "every": args.every,
        "ai_advice": bool(args.ai_advice),
        "resolution": None,
    })

    mailbox = LatestFrame()
    reader = RtspReader(args.url, mailbox, stop)
    processor = LiveProcessor(
        state_stream, writer,
        recognizer=args.recognizer, engine=engine, full_config=full_config,
        profile=profile,
        stable_frames=args.stable_frames, dealer_carry=args.dealer_carry,
        every=args.every, max_frame_age=args.max_frame_age,
        dump_dir=args.dump_frames, quiet=args.quiet,
        hand_tracker=hand_tracker, advice_runner=advice_runner)

    _log(f"[启动] 拉流 {args.url}")
    _log(f"[启动] 输出 {args.out}；间隔 {args.every}s；Ctrl-C 结束")
    reader.start()

    last_seq = -1
    last_start = 0.0
    last_status = time.monotonic()
    last_shape: tuple[int, int] | None = None
    plan: CropPlan | None = None
    fit: AspectFit | None = None
    last_warn = 0.0

    try:
        while not stop.is_set():
            packet = mailbox.wait_newer(last_seq, timeout=0.5)
            now = time.monotonic()

            if packet is None:
                # 流断了 / 还没连上：定期提醒，不要每 0.5s 刷屏
                if (reader.connected_once and reader.last_grab_mono > 0
                        and now - reader.last_grab_mono > 3.0
                        and now - last_warn > 5.0):
                    _log(f"[警告] {now - reader.last_grab_mono:.1f}s 没有新帧"
                         f"（重连 {reader.reconnects} 次）")
                    last_warn = now
                if now - last_status >= 10.0:
                    _log_status(processor, reader, started_mono)
                    last_status = now
                continue

            shape = packet.image.shape[:2]          # (h, w)
            if shape != last_shape:
                h, w = shape
                plan = plan_crop(shape, profile, args.crop, image=packet.image)
                # ROI 坐标前提是「画面宽高比 = profile 参考宽高比」。
                # 裁剪后的画面再按参考比例重采样一次，x/y 缩放就一致了。
                fit = (AspectFit.for_size(plan.out_size, profile)
                       if args.recognizer == "roi" else None)
                last_shape = shape
                writer.write({"type": "stream_info", "ts": _iso_now(),
                              "recognizer": args.recognizer,
                              "resolution": [w, h],
                              "aspect": round(w / h, 6) if h else None,
                              "reference_aspect":
                                  round(profile.reference_size[0]
                                        / profile.reference_size[1], 6),
                              "crop": plan.to_debug(),
                              "roi_fit": fit.to_debug() if fit else None})
                if plan.rect is not None:
                    _log(f"[裁剪] {w}x{h} → {plan.out_size[0]}x{plan.out_size[1]}"
                         f"（{plan.mode}）")
                if plan.warning:
                    _log(f"[警告] {plan.warning}")
                if fit is not None:
                    _log(f"[比例] 流 {w}x{h}（{w / h:.4f}）→ 画面 "
                         f"{plan.out_size[0]}x{plan.out_size[1]}"
                         f"（{fit.aspect:.4f}）→ ROI "
                         f"{fit.out_size[0]}x{fit.out_size[1]}"
                         f"（参考 {fit.reference_aspect:.4f}，"
                         f"缩放 {fit.scale[0]:.4f}/{fit.scale[1]:.4f}）")
                    if fit.deviation > 0.02:
                        _log(f"[警告] 画面宽高比与 profile 参考 "
                             f"{profile.reference_size[0]}x{profile.reference_size[1]} "
                             f"偏差 {fit.deviation * 100:.1f}%；已按参考比例重采样。"
                             f"若画面被 OBS 横向拉伸，请改回等比缩放")
                    low = min(fit.scale)
                    if low < 0.5:
                        _log(f"[警告] ROI 分辨率只有参考图的 {low * 100:.0f}%"
                             f"（{fit.out_size[0]}x{fit.out_size[1]} vs "
                             f"{profile.reference_size[0]}x{profile.reference_size[1]}）。"
                             f"筹码数字/庄位 D 会小到读不出，"
                             f"请把 OBS 输出分辨率提到 "
                             f"{profile.reference_size[0]}x{profile.reference_size[1]}")

            img = plan.apply(packet.image)
            if fit is not None:
                img = fit.apply(img)
            if img is not packet.image:
                packet = FramePacket(packet.seq, packet.epoch,
                                     packet.grab_mono, img)

            delay = throttle_delay(last_start, now, args.every)
            if delay > 0:
                if stop.wait(delay):
                    break
                newer = mailbox.latest()
                if newer is not None and newer.seq > packet.seq:
                    packet = newer

            last_start = time.monotonic()
            result = processor.handle(packet)
            last_seq = packet.seq

            if args.max_frames and processor.processed >= args.max_frames:
                break
            if time.monotonic() - last_status >= 10.0:
                _log_status(processor, reader, started_mono)
                last_status = time.monotonic()
    finally:
        stop.set()
        mailbox.close()
        reader.join(timeout=2.0)

        # 关掉建议线程前先把已完成的结果落库；正在跑的那一次不再等
        if advice_runner is not None:
            advice_runner.close()
            processor.drain_advice(final=True)

        elapsed = time.monotonic() - started_mono
        summary = {
            "elapsed_seconds": round(elapsed, 1),
            "frames_read": reader.grabbed,
            "processed_frames": processor.processed,
            "avg_proc_ms": round(processor.avg_proc_ms, 1),
            "eff_hz": round(processor.processed / elapsed, 2) if elapsed else 0,
            "reconnects": reader.reconnects,
            "advice_count": processor.advice_count,
            **processor.summary(),
        }
        writer.write({"type": "session_end", "ended_at": _iso_now(),
                      "summary": summary})
        writer.close()

        _log(f"\n会话结束：读帧 {reader.grabbed}，处理 {processor.processed}，"
             f"记录 {summary['record_count']} 条，重连 {reader.reconnects} 次；"
             f"平均 {summary['avg_proc_ms']:.0f}ms/帧，"
             f"有效 {summary['eff_hz']:.2f}Hz")
        if summary["discarded_reasons"]:
            _log("丢弃原因：" + "  ".join(
                f"{k}={v}" for k, v in
                sorted(summary["discarded_reasons"].items(), key=lambda kv: -kv[1])))
        _log(f"→ {args.out}")

    return 0


def _log_status(processor: LiveProcessor, reader: RtspReader,
                started_mono: float) -> None:
    elapsed = time.monotonic() - started_mono
    eff = processor.processed / elapsed if elapsed else 0.0
    _log(f"[状态] 采样={processor.sampled} 处理={processor.processed} "
         f"读帧={reader.grabbed} 丢弃帧={processor.sampled - processor.accepted} "
         f"平均={processor.avg_proc_ms:.0f}ms 有效={eff:.2f}Hz "
         f"重连={reader.reconnects}")


if __name__ == "__main__":
    sys.exit(main())
