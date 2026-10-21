"""实时拉流 → 状态 JSONL（M3.1）。

    .venv/bin/python -m poker_ocr.live \
        --url rtsp://127.0.0.1:8554/live \
        --out logs/live.states.jsonl

链路：OBS ──SRT──▶ MediaMTX ──RTSP──▶ 本进程。

设计要点：

  · **只保留最新帧**。reader 线程按源帧率连续 read，邮箱里永远只有一帧；
    处理不过来就覆盖旧帧，绝不让延迟越积越大。
  · **连续动作合并**。连续弃牌/下注时，中间帧的徽章还在动画里，读数既不可信
    也会写成一串「弃牌 1 个 → 2 个 → 3 个」的半成品记录。动作区静止
    `--settle` 秒后才识别，交出去的是这阵动作落定后的最新一帧；动作一直不停
    时由 `--settle-max` 兜底。盲注倒计时/头像计时环不参与「静止」判据。
  · **单处理线程**。reader 只做解码，`StateStream.ingest()` 永远只在主循环里
    调用；一帧内的多块 ROI rec 由线程池并行（`--roi-threads`，默认 4，1=串行）。
    共享同一个 OcrEngine/ONNX session，实测并发结果与串行逐字一致。
  · **默认不节流**。处理完当前帧立刻回到邮箱取最新帧，中间帧直接丢弃；
    `--every > 0` 才按固定间隔降频（调试/降压用）。
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
from .roi_dedup import pixel_mad
from .roi_reader import hero_turn_method
from .state_stream import DEFAULT_ROI_THREADS, StateStream

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"
DEFAULT_URL = "rtsp://127.0.0.1:8554/live"
DEFAULT_OUT = Path("logs/live.states.jsonl")
# 默认不节流：处理完当前帧立刻取邮箱里的最新帧，中间帧直接丢弃。
# `--every` 仍保留给调试/降压用（>0 时按该间隔采样）。
DEFAULT_EVERY = 0.0

# 让 OpenCV 的 FFmpeg 后端用 TCP、少缓冲。必须在第一次 VideoCapture 之前设置。
FFMPEG_OPTIONS = ("rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0")


# ─────────────────────────── 基础组件 ───────────────────────────

def _log(msg: str) -> None:
    print(msg, flush=True)


# 终端没法用 ANSI 改字号，「加大」靠加粗 + 高亮底色 + 上下留白来拉开层次。
# 只在真终端里上色：重定向到文件 / NO_COLOR 时保持纯文本，方便 grep。
_ANSI_RESET = "\033[0m"
_ANSI_ADVICE_BAR = "\033[1;30;103m"   # 粗体黑字 + 亮黄底
_ANSI_ADVICE_TEXT = "\033[1m"         # 粗体


def _use_ansi() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


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


# ─────────────────────────── 动作区静止闸 ───────────────────────────
#
# 连续弃牌 / 连续下注时，画面会在一两秒里一帧一变。逐帧 OCR 只会得到一串
# 中间状态（弃牌 1 个 → 2 个 → 3 个），每个都被当成一条新记录；而这些帧里的
# 徽章还在动画中，本来就是最不可信的读数。用户要的是**这阵动作落定后的最新
# 一帧**。
#
# 做法：在 OCR 之前只看**动作相关区域**的像素变化。还在变 → 丢掉当前帧继续
# 等；静止 `settle` 秒 → 才把邮箱里的最新帧交给识别。动作一直不停时由
# `max_wait` 兜底，保证不会无限等下去。
#
# 哪些区域**不能**算进来：
#   · 盲注横幅（`blinds`）里有房间号和每秒跳动的倒计时；
#   · 头像只有**中心**参与：回合计时环画在头像边缘，整块拿来比永远不会静止；
#   · hero 操作区 / 预设按钮：蓝色按钮会呼吸式高亮，而且轮到我时本来就要
#     尽快出建议，不能被动画拖到 max_wait。
DEFAULT_SETTLE = 0.25
DEFAULT_SETTLE_MAX = 1.5
SETTLE_MAD = 2.0                  # 文字/数字区：下采样到 32x32 后的平均绝对差门槛
# 头像中心单独用更高的门槛：照片本身的压缩噪声比文字区大一个量级（实测
# 24fps 视频 p90 就有 2.4），而弃牌蒙层/蓝色下注钮会把平均值推到 30 以上 ——
# 门槛取 10 既不会把噪声当动作，也不会漏掉整块变暗/变蓝这种真变化。
SETTLE_AVATAR_MAD = 10.0
SETTLE_AVATAR_INSET = 0.2         # 头像只取中心 60%，躲开边缘的计时环
_SETTLE_GLOBALS = ("pot", "pot_before_street", "board", "hero_cards", "hand_label")
_SETTLE_SIZE = (32, 32)


def _shrink(rect: tuple[int, int, int, int],
            frac: float) -> tuple[int, int, int, int]:
    """按比例向内收一圈。frac=0.2 → 保留中心 60%。"""
    x1, y1, x2, y2 = rect
    dx, dy = int((x2 - x1) * frac), int((y2 - y1) * frac)
    return x1 + dx, y1 + dy, x2 - dx, y2 - dy


def build_settle_regions(profile: Profile, size: tuple[int, int], *,
                         mad: float = SETTLE_MAD,
                         avatar_mad: float = SETTLE_AVATAR_MAD,
                         avatar_inset: float = SETTLE_AVATAR_INSET
                         ) -> tuple[tuple[tuple[int, int, int, int], float], ...]:
    """一帧里参与「还在动吗」判定的区域，以及各自的 MAD 门槛。"""
    regions: list[tuple[tuple[int, int, int, int], float]] = []
    for seat in profile.seats:
        for slot in (seat.name_slot, seat.chip_slot, seat.chip_slot_alt,
                     seat.bet_slot):
            if slot is not None:
                regions.append((profile.slot_to_px(slot, size), mad))
        regions.append((_shrink(profile.slot_to_px(seat.avatar_slot, size),
                                avatar_inset), avatar_mad))
    for key in _SETTLE_GLOBALS:
        slot = profile.global_slots.get(key)
        if slot is not None:
            regions.append((profile.slot_to_px(slot, size), mad))
    return tuple((r, th) for r, th in regions if r[2] > r[0] and r[3] > r[1])


class SettleGate:
    """动作区静止闸：一阵连续动作只放行落定后的最后一帧。

        gate = SettleGate(profile, settle=0.25, max_wait=1.5)
        if gate.should_process(img, time.monotonic()):
            process(img)

    时间参数由调用方传入（不自己读钟），单测可以直接喂合成时间轴。
    画面尺寸变了会重建区域 —— 和 ROI 缓存一样，换分辨率不能沿用旧坐标。
    """

    def __init__(self, profile: Profile, *, settle: float = DEFAULT_SETTLE,
                 max_wait: float = DEFAULT_SETTLE_MAX,
                 mad: float = SETTLE_MAD,
                 avatar_mad: float = SETTLE_AVATAR_MAD,
                 avatar_inset: float = SETTLE_AVATAR_INSET) -> None:
        if settle < 0:
            raise ValueError("settle 不能为负")
        if max_wait < settle:
            raise ValueError("max_wait 不能小于 settle")
        self.profile = profile
        self.settle = float(settle)
        self.max_wait = float(max_wait)
        self.mad = float(mad)
        self.avatar_mad = float(avatar_mad)
        self.avatar_inset = float(avatar_inset)
        self._size: tuple[int, int] | None = None
        self._regions: tuple[tuple[tuple[int, int, int, int], float], ...] = ()
        self._prev: tuple[tuple[np.ndarray, float], ...] | None = None
        # 没有「上次变化」时视为已经静止：第一帧立即放行，不白等一个窗口
        self._last_change = float("-inf")
        self._burst_start: float | None = None
        self.blocks = 0               # 合并掉的帧数（统计/调试）

    def _signature(self, img: np.ndarray) -> tuple[tuple[np.ndarray, float], ...]:
        size = (img.shape[1], img.shape[0])
        if size != self._size:
            self._size = size
            self._regions = build_settle_regions(
                self.profile, size, mad=self.mad, avatar_mad=self.avatar_mad,
                avatar_inset=self.avatar_inset)
            self._prev = None
            self._last_change = float("-inf")
            self._burst_start = None
        out = []
        for (x1, y1, x2, y2), mad in self._regions:
            roi = img[y1:y2, x1:x2]
            if roi.size == 0:
                continue
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            out.append((cv2.resize(gray, _SETTLE_SIZE,
                                   interpolation=cv2.INTER_AREA), mad))
        return tuple(out)

    def _changed(self, prev: tuple[tuple[np.ndarray, float], ...],
                 cur: tuple[tuple[np.ndarray, float], ...]) -> bool:
        if len(prev) != len(cur):
            return True
        return any(pixel_mad(a, b) > mad
                   for (a, _), (b, mad) in zip(prev, cur))

    def should_process(self, img: np.ndarray, now: float) -> bool:
        """`now` 是单调时钟。返回 True = 画面已落定，可以交给 OCR。"""
        sig = self._signature(img)
        if self._prev is not None and self._changed(self._prev, sig):
            self._last_change = now
            if self._burst_start is None:
                self._burst_start = now
        self._prev = sig

        if (now - self._last_change >= self.settle
                or (self._burst_start is not None
                    and now - self._burst_start >= self.max_wait)):
            self._burst_start = None
            return True
        self.blocks += 1
        return False


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
            self.log(f"[连接] 视频源已连接 epoch={self.epoch}：{w}x{h} @ {fps:.1f}fps")

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


def rotate_frame(img: np.ndarray, degrees: int) -> np.ndarray:
    """按 0/90/180/270 度旋转。**90 = 顺时针**（与 cv2.ROTATE_90_CLOCKWISE 一致）。

    OBS 虚拟摄像头在 macOS 上固定输出 1920x1080；竖屏画布塞进去只剩 ~486x1080，
    筹码数字太小。把 OBS 画布改成 1920x1080、手机源旋转 90° 铺满宽度，就能保留
    ~864x1920（参考尺寸的 ~88%），再用这里的 `--rotate` 把画面转回来。
    """
    table = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    if degrees not in table:
        raise ValueError(f"rotate 只支持 0/90/180/270，实际 {degrees}")
    code = table[degrees]
    return img if code is None else cv2.rotate(img, code)


def _open_capture(url: str):
    """打开视频源。

    两种写法：

      · `rtsp://...` —— 走 FFmpeg 解 RTSP（OBS → SRT → MediaMTX 路线）。
      · `cam:0` / `cam:1` —— 直接打开本机摄像头。OBS 在**同一台机器**上时，
        用 OBS 虚拟摄像头（Start Virtual Camera）把画面直接喂给 Python，
        可以整段跳过 编码 → SRT → MediaMTX → RTSP → 解码（约 0.3–0.8s）。
        索引用 `tools/list_cameras.py` 查。

    `cam:` 优先 AVFoundation（macOS 虚拟摄像头只在这个后端暴露）；失败再让
    OpenCV 自选后端。
    """
    if url.startswith(("cam:", "camera:")):
        _, _, raw = url.partition(":")
        try:
            index = int(raw)
        except ValueError:
            return None
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            return None
        # 摄像头后端大多忽略这个设置；读循环本身也只保留最新帧
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

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
                 stable_frames: int = 1,
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
        # hand_id → 这一手已经给出的建议（按时间顺序）。换手 / 退出时并进
        # `hand` 记录，让「完整行动线 + 模型建议」落在同一个手牌记忆条目里，
        # 复盘时不用再按 hand_id 去 join 两条独立记录。
        self._hand_advice: dict[str, list[dict]] = {}
        self.clock = clock
        self.log = log

        self.started_mono = clock()
        self.last_epoch = 0
        self.processed = 0
        self.total_proc_ms = 0.0
        self.last_proc_ms = 0.0
        self._stale_logged = 0.0
        self._cards_error_logged = False
        # 上一次「生成中…」心跳报到第几秒（只在等待建议时使用）
        self._wait_beat_second = 0

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
        # 丢弃 / 无变化的帧不会进 JSONL，属于「没被使用」的图片：不逐帧打印，
        # 只保留记录状态的行（丢弃原因在退出汇总里仍会打印）。
        # 注意 `_track_hand` 仍要跑：行动线/建议吃的是被接受的状态，不只是记录。

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
        # 换手是在 observe 内部判定的：紧接着再 drain 一次，把上一手的
        # 手牌记忆（行动线 + 建议）在换手这一帧就落库，不必等下一帧。
        self.drain_advice()
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

    @property
    def waiting_for_advice(self) -> bool:
        """AI 建议正在生成：主循环应暂停识别，等结果回来再处理新帧。"""
        return self.advice_runner is not None and self.advice_runner.busy

    def log_advice_wait(self) -> None:
        """等模型时每秒打一行「生成中…」。

        等待期间主循环暂停 OCR，控制台本来会完全静默，模型返回后建议和恢复
        后的识别日志一起涌出来，看起来像「一堆一堆」。心跳把这段静默填成线性
        的时间线，也让人一眼看出慢在模型而不是识别。
        """
        runner = self.advice_runner
        if self.quiet or runner is None or not runner.busy:
            self._wait_beat_second = 0
            return
        second = int(runner.busy_seconds)
        if second < 1 or second == self._wait_beat_second:
            return
        self._wait_beat_second = second
        self.log(f"  [建议] 生成中… {second}s")

    def drain_advice(self, final: bool = False) -> None:
        """把已完成的手牌/建议写进 JSONL 并打印。

        `final=True` 在退出时调用：最后一手牌等不到「换手」，也要落库。

        顺序很重要：**先收建议、再写手牌**。换手那一帧要把这一手最后一条
        建议一起并进 `hand` 记录；反过来会漏掉同一帧刚返回的那条。
        """
        if self.advice_runner is not None:
            for advice in self.advice_runner.poll():
                data = advice.to_dict()
                self._hand_advice.setdefault(data["hand_id"], []).append(data)
                self.writer.write({"type": "advice", "ts": _iso_now(), **data})
                self._log_advice(advice)

        if self.hand_tracker is None:
            return
        finished = self.hand_tracker.take_finished()
        if finished is not None:
            self._write_hand(finished, partial=False)
        if final:
            current = self.hand_tracker.take_current()
            if current is not None:
                self._write_hand(current, partial=True)

    def _write_hand(self, hand: dict, *, partial: bool) -> None:
        """写一条手牌记忆：完整行动线 + 这一手给出过的全部建议。"""
        advices = self._hand_advice.pop(hand.get("hand_id"), [])
        record: dict = {"type": "hand", "ts": _iso_now()}
        if partial:
            record["partial"] = True
        record.update(hand)
        if advices:
            # 没有建议时连键都不出现，保持无 AI 模式下的输出与旧版一致
            record["advices"] = advices
        self.writer.write(record)

    def _log_advice(self, advice) -> None:
        option = advice.option
        amount = f" {option.amount}BB" if option.amount is not None else ""
        tail = f"（{advice.error}）" if advice.error else ""
        elapsed = f"耗时 {advice.duration_ms / 1000:.1f}s"
        if not _use_ansi():
            self.log(f"  [建议] {advice.street} ⇒ {option.label}{amount}"
                     f"｜{elapsed}｜{advice.reason}{tail}")
            return
        # 模型结果只出现一次，别被滚动的识别日志淹没：
        # 留白 → 高亮横幅（街 + 动作 + 金额 + 耗时）→ 粗体理由 → 留白。
        bar = "═" * 60
        headline = (f" ▶ 建议  {advice.street} ⇒ {option.label}{amount}"
                    f"  ·  {elapsed} ")
        self.log("")
        self.log(f"{_ANSI_ADVICE_BAR}{bar}{_ANSI_RESET}")
        self.log(f"{_ANSI_ADVICE_BAR}{headline}{_ANSI_RESET}")
        self.log(f"{_ANSI_ADVICE_BAR}{bar}{_ANSI_RESET}")
        self.log(f"{_ANSI_ADVICE_TEXT} 理由：{advice.reason}{tail}{_ANSI_RESET}")
        self.log("")

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
        # 与 roi 路线一致：丢弃 / 无变化的帧不进 JSONL，不逐帧打印日志

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
                    help=f"视频源（默认 {DEFAULT_URL}）。本机 OBS 虚拟摄像头可写 "
                         "cam:0 / cam:1（用 tools/list_cameras.py 查索引），"
                         "跳过 SRT/MediaMTX，延迟最低")
    ap.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0,
                    help="把视频源顺时针旋转多少度再识别（默认 0）。OBS 虚拟摄像头"
                         "固定 1920x1080，把 OBS 画布改成横屏、手机源转 90° 后，"
                         "这里用 --rotate 90 转回竖屏，能保住更高分辨率")
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
                    help="采样间隔秒（默认 0 = 不节流：处理完当前帧立即取最新帧，"
                         "中间帧丢弃）")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                    help="连续动作合并：动作区静止多少秒后才识别，只保留落定后的"
                         f"最新一帧（默认 {DEFAULT_SETTLE}；0 = 关闭，逐帧识别）")
    ap.add_argument("--settle-max", type=float, default=DEFAULT_SETTLE_MAX,
                    help="连续动作合并的最长等待秒数：动作一直不停时最多攒这么久"
                         f"就先识别一帧（默认 {DEFAULT_SETTLE_MAX}）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"JSONL 输出（默认 {DEFAULT_OUT}）")
    ap.add_argument("--stable-frames", type=int, default=1,
                    help="新状态连续出现几帧才记录（默认 1 = 不做多帧确认）")
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
    ap.add_argument("--roi-threads", type=int, default=DEFAULT_ROI_THREADS,
                    help="帧内 ROI OCR 并行线程数（默认 "
                         f"{DEFAULT_ROI_THREADS}；1=串行）。"
                         "线程只并行同一帧里的多块 ROI，帧与帧之间仍串行")
    ap.add_argument("--dump-frames", type=Path, default=None,
                    help="把每个采样帧存成 JPEG，文件名带墙钟时间（量延迟用）")
    ap.add_argument("--ai-advice", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="轮到我时按当前街加载技能并调用 AI 给一个选项"
                         "（默认开启；用 --no-ai-advice 关闭。需要 AI_SERVER_API_KEY / "
                         "AI_SERVER_BASE_URL / AI_SERVER_MODEL；仅 roi 模式）")
    ap.add_argument("--ai-timeout", type=float, default=20.0,
                    help="单次 AI 建议的超时秒数（默认 20）")
    ap.add_argument("--ai-confirm", type=int, default=1,
                    help="轮到我需要连续几帧确认（默认 1 = 第一帧就触发，"
                         "配合蓝色下注按钮；按钮会闪断的旧 UI 可调回 2）")
    ap.add_argument("--quiet", action="store_true", help="只打状态行和最终汇总")
    return ap


def _validate_args(ap: argparse.ArgumentParser, args) -> None:
    if args.every < 0:
        ap.error("--every 不能为负数（0 = 不节流）")
    if args.settle < 0:
        ap.error("--settle 不能为负数（0 = 关闭动作合并）")
    if args.settle_max < args.settle:
        ap.error("--settle-max 不能小于 --settle")
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
    if args.roi_threads < 1:
        ap.error("--roi-threads 至少为 1（1 = 串行）")


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
            roi_threads=args.roi_threads,
        )
        _log(f"[启动] ROI 并行线程={args.roi_threads}"
             + ("（串行）" if args.roi_threads <= 1 else ""))

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
        turn_method = hero_turn_method(profile)
        if turn_method == "blue_button":
            hero_seat = next(
                (s for s in profile.seats if s.seat_id == profile.hero_seat_id), None)
            if hero_seat is None:
                ap.error("当前 profile 没有 hero 座位，判不出蓝色下注按钮，"
                         "AI 建议永远不会触发")
        elif "hero_turn_probe" not in profile.global_slots:
            # 静默不触发是最难查的故障：直接拒绝启动，让人现在就去补槽位
            ap.error(
                "当前 profile 没有 hero_turn_probe 槽位，判不出「轮到我」，"
                "AI 建议永远不会触发。请把 thresholds.hero_turn_method 设为 "
                "blue_button（用 hero 头像槽判蓝色下注按钮），或补上 hero_turn_probe")
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

    # 动作区静止闸只用于 roi 路线：它的区域来自固定槽位；full 路线没有布局。
    settle_gate = (SettleGate(profile, settle=args.settle,
                              max_wait=args.settle_max)
                   if args.settle > 0 and args.recognizer == "roi" else None)

    _log(f"[启动] 拉流 {args.url}")
    cadence = (f"间隔 {args.every}s" if args.every > 0
               else "无节流（处理完当前帧立即取最新帧）")
    if settle_gate is not None:
        cadence += (f"；动作区静止 {args.settle}s 后识别"
                    f"（上限 {args.settle_max}s）")
    _log(f"[启动] 输出 {args.out}；{cadence}；Ctrl-C 结束")
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
            # 事件驱动：有新帧会立刻唤醒；timeout 只是「没帧时多久回来查一次」，
            # 不是采样间隔。默认不节流后把它调小，避免把空闲等待误当成延迟。
            packet = mailbox.wait_newer(last_seq, timeout=0.2)
            now = time.monotonic()
            processor.log_advice_wait()

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

            # 模型生成建议期间不识别新帧：等待时画面只会越来越旧，继续 OCR
            # 既浪费 CPU，也会让下一步决策基于过时画面。这里只把帧标记为已
            # 消费，结果回来后直接处理邮箱里最新的一帧。
            if processor.waiting_for_advice:
                last_seq = packet.seq
                continue

            if args.rotate:
                # 先转正再裁剪/缩放：OBS 虚拟摄像头输出横屏，牌桌是竖屏
                packet = FramePacket(
                    packet.seq, packet.epoch, packet.grab_mono,
                    rotate_frame(packet.image, args.rotate))

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

            # 默认不节流：处理完这一帧就回到 wait_newer，邮箱里永远只有最新一帧，
            # 中间帧已经被 reader 覆盖丢弃。--every > 0 时才保留旧的可选降频。
            if args.every > 0:
                delay = throttle_delay(last_start, now, args.every)
                if delay > 0:
                    if stop.wait(delay):
                        break
                    newer = mailbox.latest()
                    if newer is not None and newer.seq > packet.seq:
                        # 新帧同样要过裁剪/比例归一化，否则 ROI 坐标会错位
                        newer_img = plan.apply(newer.image)
                        if fit is not None:
                            newer_img = fit.apply(newer_img)
                        packet = FramePacket(newer.seq, newer.epoch,
                                             newer.grab_mono, newer_img)
                last_start = time.monotonic()

            # 动作区还在变（连续弃牌 / 下注动画）就先不识别：丢掉这一帧，
            # 等它落定后直接处理邮箱里的最新帧。这样一串动作只留最后一帧。
            if (settle_gate is not None
                    and not settle_gate.should_process(packet.image,
                                                       time.monotonic())):
                last_seq = packet.seq
                continue

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
            **({"settle_blocks": settle_gate.blocks}
               if settle_gate is not None else {}),
            **processor.summary(),
        }
        writer.write({"type": "session_end", "ended_at": _iso_now(),
                      "summary": summary})
        writer.close()
        if state_stream is not None:
            state_stream.close()

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
