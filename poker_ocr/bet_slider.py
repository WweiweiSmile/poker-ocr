"""下注/加注滑杆：识别 + 长按打开 + 拖/滚到目标额 + 点确定。

web 端（WPK H5）的操作路径和手机不一样：

  · 单击主按钮 = 用 5 个底池预设之一（原有 clicker 路径）；
  · **长按主按钮** = 打开一根竖向滑杆，顶部气泡显示「加注到多少 BB」，
    左侧气泡显示「占底池百分比 / 倍数」。新 UI（2026-11-10）要**按住橙色
    额度手柄上下拖**才能改额（滚轮无效），拖到目标额再点『确定』落注。

这个模块只做滑杆这一段：

  read_slider()        一帧 → 滑杆是否打开 / 当前 BB / 百分比（纯识别）
  BetSliderController  长按 → 拖动/滚动 → 读回 → 到目标 → 点确定（可注入后端/读帧）
  WindowMapper         归一化槽位 → 屏幕逻辑点（clicker 的坐系）

**安全口径**：

  · 打开与否只看**顶部金额气泡读不读得出内容**（`NBB` 或 ALLIN/全下）。
    既不看「槽位里有字」（真实关闭帧上纯 rec 会把毛毡读成 `9`/`O`），
    也不单看 track 深浅（黑帧/遮罩同样很深）。深色只用来判「可能开着但
    读不准」，这种情况下绝不重复长按主按钮。
  · 只有读到当前额 == 目标额（容差内）才点『确定』；到不了目标（超过
    all-in / 低于最小注 / 滑杆只支持整数而目标是小数）就报错并停手。
  · 全下只看客户端顶部的 **ALLIN / 全下** 字样，连续两次读到才落注；
    「额度不再变化」不能当顶端 —— 拖不动时额度同样不变。
  · 关滑杆只点右侧空白毛毡，**不发 Esc**：新 UI 不认 Esc，而且 Esc 是发给
    当前键盘焦点 App 的全局按键，窗口不在最前时会落到别的程序上。
  · 所有按钮点击都在圆盘内随机取点：半径 = 原半径 × 70%（`SLIDER_CLICK_INSET`）。
"""

from __future__ import annotations

import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

import cv2
import numpy as np

from .clicker import (ClickBackend, ClickerConfig, DEFAULT_INSET_RATIO,
                      Rect)
from .ocr import OcrEngine
from .profile import Profile, Size, Slot
from .roi_reader import crop_slot, read_slot

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*BB", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_MULT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[×xX]\s*底池")
# 拉到底时客户端把顶部气泡换成「ALL IN」/「All-in」/「全下」。
_ALLIN_RE = re.compile(r"all\s*[-_ ]?\s*in|全\s*下", re.IGNORECASE)

# 滑杆打开时 track 是深色竖条；实测 002520 = 0.915，关闭帧 = 0.000。
# **只作证据**：判定「打开」用顶部金额气泡（见 read_slider）。
TRACK_DARK_MAX = 70
TRACK_OPEN_MIN = 0.35
# 圆钮点击内缩：半径 = 原半径 × 70%（与 clicker 同一个常量），即
# 点击范围 = π(0.7r)² ≈ 49% 的按钮面积。
SLIDER_CLICK_INSET = DEFAULT_INSET_RATIO
# 连续读到这么多次 ALLIN 才落注（OCR 单帧抖动不认）。
ALLIN_CONFIRM_READS = 2

# 新 UI（2026-11-10）的额度手柄：橙色/红色圆钮，位置随额度上下移动。
# **按住它上下拖**才能改额度（滚轮在这个版本上完全无效，实测
# 2026-11-10 21:06 的 1BB 滑杆滚了 3 次数值纹丝不动，最后只能 Esc）。
HANDLE_HSV_LO = (0, 110, 110)
HANDLE_HSV_HI = (22, 255, 255)
HANDLE_MIN_AREA = 500

# 快捷档位「置灰」检测：按钮圆盘里的白色文字占比 / V99。
# 实测 000060 的 1/3（低于最低加注）= white 0.021 / V99 197；
# 同帧启用按钮 = 0.060-0.099 / V99 252-255。阈值取 0.04 / 225。
PRESET_WHITE_MIN = 0.04
PRESET_V99_MIN = 225.0
PRESET_SLOTS = {
    "raise-1/3": "bet_preset_1_3",
    "raise-1/2": "bet_preset_1_2",
    "raise-2/3": "bet_preset_2_3",
    "raise-1": "bet_preset_1",
    "raise-1.2": "bet_preset_1_2x",
}


class BetSliderError(Exception):
    """滑杆操作失败（没打开 / 读不到额 / 到不了目标）。"""


class FrameSource(Protocol):
    def capture(self) -> np.ndarray: ...


@dataclass
class BetSliderState:
    """一帧的滑杆状态。`open` 只看**顶部金额气泡**在不在，不依赖 track 深浅。"""

    open: bool
    amount_bb: float | None = None
    amount_text: str = ""
    amount_score: float = 0.0
    fraction_pct: float | None = None
    multiplier: float | None = None
    fraction_text: str = ""
    fraction_score: float = 0.0
    dark_frac: float = 0.0
    evidence: dict = field(default_factory=dict)
    error: str | None = None
    # 顶部气泡是「ALL IN / 全下」：滑杆已经拉到底。
    allin: bool = False

    @property
    def ok(self) -> bool:
        return self.open and self.amount_bb is not None

    def to_dict(self) -> dict:
        d = {
            "open": self.open,
            "amount_bb": self.amount_bb,
            "amount_text": self.amount_text,
            "amount_score": round(self.amount_score, 3),
            "fraction_pct": self.fraction_pct,
            "multiplier": self.multiplier,
            "fraction_text": self.fraction_text,
            "dark_frac": round(self.dark_frac, 3),
            "evidence": self.evidence,
        }
        if self.allin:
            d["allin"] = True
        if self.error:
            d["error"] = self.error
        return d


def parse_amount(text: str | None) -> float | None:
    """从 OCR 原文取金额；只认数字+BB，`0.5BB` / `2BB` 都行。"""
    m = _AMOUNT_RE.search(text or "")
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value if 0.0 < value < 1e6 else None


def parse_fraction(text: str | None) -> tuple[float | None, float | None]:
    """左侧气泡 → (占底池百分比, 底池倍数)。读不到就 None，不算失败。"""
    pct = _PCT_RE.search(text or "")
    mult = _MULT_RE.search(text or "")
    p = float(pct.group(1)) if pct else None
    m = float(mult.group(1)) if mult else None
    return p, m


@dataclass
class PresetState:
    """一个快捷档位按钮的启用/置灰状态。enabled=None 表示读不准。"""

    preset_id: str
    enabled: bool | None
    white_frac: float = 0.0
    v99: float = 0.0
    rect: tuple[int, int, int, int] | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = {"preset_id": self.preset_id, "enabled": self.enabled,
             "white_frac": round(self.white_frac, 3), "v99": round(self.v99, 1)}
        if self.rect:
            d["rect"] = list(self.rect)
        if self.error:
            d["error"] = self.error
        return d


def read_preset_enabled(
    img: np.ndarray,
    profile: Profile,
    preset_id: str,
    *,
    white_min: float = PRESET_WHITE_MIN,
    v99_min: float = PRESET_V99_MIN,
) -> PresetState:
    """快捷档位是否可点（未置灰）。

    WPK 在「后手不够这个档位 / 低于最低加注」时把按钮文字调灰；这里只看
    按钮圆盘里的**白色文字占比 + V99**，不看下方 BB 值（值在置灰时也可能
    保留）。返回 enabled=None 表示槽位缺失/裁剪为空，调用方应按「不可点」
    处理，退回滑杆。
    """
    key = PRESET_SLOTS.get(preset_id)
    if key is None:
        return PresetState(preset_id, None, error="unknown_preset")
    slot = profile.global_slots.get(key)
    if slot is None:
        return PresetState(preset_id, None, error=f"profile 缺少 {key}")
    size: Size = (img.shape[1], img.shape[0])
    rect, roi = crop_slot(img, profile, slot, size, pad=0.0)
    if roi.size == 0:
        return PresetState(preset_id, None, rect=rect, error="empty_crop")
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    s = hsv[:, :, 1]
    white_frac = float(((v > 190) & (s < 90)).mean())
    v99 = float(np.percentile(v, 99))
    enabled = white_frac >= white_min and v99 >= v99_min
    return PresetState(preset_id, enabled, white_frac=white_frac, v99=v99,
                       rect=rect)


def read_slider(img: np.ndarray, profile: Profile, engine: OcrEngine | None,
                *, read_fraction: bool = True,
                dark_max: int = TRACK_DARK_MAX,
                open_min: float = TRACK_OPEN_MIN) -> BetSliderState:
    """识别一帧的滑杆状态。

    **打开判据 = 顶部金额气泡读得出内容**（`数字+BB`，或 ALLIN/全下）。
    不能只看「槽位里有字」：真实关闭帧上纯 rec 会把毛毡/牌背读成单个
    `9` / `O` / `Q`（实测 logs/mac_video/hero 60/60 帧都这样），一旦把
    「有字」当打开，滑杆会永远处于打开状态。
    也不能只看深色占比：黑帧、调暗遮罩、被盖住的窗口同样很深（实测全黑帧
    dark_frac=1.0），旧口径会把关闭的滑杆判成打开。

    所以：气泡有可解析内容 → 打开；气泡读不出但**轨道是深色**（真实关闭帧
    dark_frac ≤ 0.11，打开帧 0.84–0.98）→ `open=False` 且
    `error=amount_unreadable`，提示调用方「滑杆可能开着但读不准」，
    此时绝不能再长按一次主按钮（那等于在当前额度上点一下确认）。
    """
    size: Size = (img.shape[1], img.shape[0])
    track_slot = profile.global_slots.get("hero_bet_slider")
    amount_slot = profile.global_slots.get("hero_bet_amount")
    if track_slot is None or amount_slot is None:
        return BetSliderState(
            False, error="profile 缺少 hero_bet_slider / hero_bet_amount")

    track_rect, track = crop_slot(img, profile, track_slot, size, pad=0.0)
    if track.size == 0:
        return BetSliderState(False, error="hero_bet_slider 裁剪为空")
    gray = cv2.cvtColor(track, cv2.COLOR_BGR2GRAY)
    dark_frac = float((gray < dark_max).mean())
    dark_open = dark_frac >= open_min

    state = BetSliderState(open=False, dark_frac=dark_frac,
                           evidence={"track_rect": list(track_rect),
                                     "dark_open": dark_open})

    amount_read = read_slot(img, profile, engine, amount_slot,
                            "hero_bet_amount", size, pad=0.0)
    state.amount_text = amount_read.text
    state.amount_score = amount_read.score
    state.amount_bb = parse_amount(amount_read.text)
    state.evidence.update({
        "amount_rect": list(amount_read.rect),
        "amount_parts": [(t, round(s, 3)) for t, s in amount_read.parts],
    })
    if state.amount_bb is None and dark_open:
        # 只有轨道深色（滑杆确实开着）时才值得再跑一次 det+rec：
        # 纯 rec 偶尔把 2BB 读成 2B8 / 28B；关闭帧跑 det 只会读到毛毡噪声。
        retry = read_slot(img, profile, engine, amount_slot,
                          "hero_bet_amount", size, pad=0.0, use_det=True)
        if retry.text:
            state.amount_text = retry.text
            state.amount_score = retry.score
            state.amount_bb = parse_amount(retry.text)
            state.evidence["amount_retry"] = [
                (t, round(s, 3)) for t, s in retry.parts]

    state.allin = bool(_ALLIN_RE.search(state.amount_text or ""))
    state.open = state.allin or state.amount_bb is not None
    if not state.open:
        if dark_open:
            state.error = "amount_unreadable"
        return state

    if read_fraction:
        frac_slot = profile.global_slots.get("hero_bet_fraction")
        if frac_slot is not None:
            # 左侧两行（2% / 0.5×底池）纯 rec 会把 2% 读成 0.2%，必须 det。
            frac = read_slot(img, profile, engine, frac_slot, "hero_bet_fraction",
                             size, pad=0.0, use_det=True)
            state.fraction_text = frac.text
            state.fraction_score = frac.score
            state.fraction_pct, state.multiplier = parse_fraction(frac.text)
            state.evidence["fraction_rect"] = list(frac.rect)
    return state


def find_slider_handle(img: np.ndarray, profile: Profile,
                       size: Size | None = None
                       ) -> tuple[float, float, float] | None:
    """在 `hero_bet_slider` 槽里找橙色额度手柄 → 原图 (cx, cy, r)。

    手柄位置**随额度变化**（1BB 在轨道底部、大额在顶部），所以每次拖动前都要
    重新找，不能写死一个坐标。只在滑杆槽里找，避免把旁边橙色的“加注/确定”
    按钮误认成手柄。
    """
    slot = profile.global_slots.get("hero_bet_slider")
    if slot is None:
        return None
    size = size or (img.shape[1], img.shape[0])
    x1, y1, x2, y2 = profile.slot_to_px(slot, size)
    if x2 <= x1 or y2 <= y1:
        return None
    roi = img[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, HANDLE_HSV_LO, HANDLE_HSV_HI)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            np.ones((3, 3), np.uint8))
    count, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    best: tuple[float, tuple[float, float]] | None = None
    for i in range(1, count):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if area < HANDLE_MIN_AREA:
            continue
        w = float(stats[i, cv2.CC_STAT_WIDTH])
        h = float(stats[i, cv2.CC_STAT_HEIGHT])
        if h <= 0 or not (0.55 <= w / h <= 1.8):
            continue
        if best is None or area > best[0]:
            best = (area, (float(cents[i][0]), float(cents[i][1])))
    if best is None:
        return None
    cx, cy = best[1]
    return (x1 + cx, y1 + cy, math.sqrt(best[0] / math.pi))


# ─────────────────────────── 屏幕坐标 ───────────────────────────

@dataclass(frozen=True)
class WindowRect:
    """游戏画面在屏幕上的逻辑点矩形（top-left 原点）。"""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.x, self.y, self.w, self.h)):
            raise ValueError("window 坐标必须是有限数")
        if self.w <= 0 or self.h <= 0:
            raise ValueError("window 宽高必须 > 0")

    @classmethod
    def parse(cls, text: str) -> "WindowRect":
        parts = [p.strip() for p in str(text).replace("，", ",").split(",")]
        if len(parts) != 4:
            raise ValueError(f"window 要写成 x,y,w,h，收到 {text!r}")
        return cls(*(float(p) for p in parts))

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


PROFILE_BUTTON_SLOTS: dict[str, tuple[str, str]] = {
    "fold": ("hero_fold_button", "disc"),
    "call": ("hero_call_button", "disc"),
    "raise-1/3": ("bet_preset_1_3", "disc"),
    "raise-1/2": ("bet_preset_1_2", "disc"),
    "raise-2/3": ("bet_preset_2_3", "disc"),
    "raise-1": ("bet_preset_1", "disc"),
    "raise-1.2": ("bet_preset_1_2x", "disc"),
    "allin": ("hero_allin_button", "disc"),
}
# profile 算出来的圆钮内缩：半径 70%，落在真实圆盘内；椭圆采样（circle）
# 会让长边方向的点落到按钮外的毛毡/文字上（预设钮 80x95 最明显），
# 所以这些按钮一律用 disc 形状。
PROFILE_BUTTON_INSET = DEFAULT_INSET_RATIO


class WindowMapper:
    """归一化槽位 → 屏幕逻辑点。

    profile 的槽位是整帧 0-1 坐标；profile.canvas 非空时先映射到 canvas，
    再乘 window。vp 的位置/缩放变了要重标 window（和 clicker 同一口径）。
    """

    def __init__(self, profile: Profile, window: WindowRect) -> None:
        self.profile = profile
        self.window = window

    def point(self, slot: Slot, fx: float = 0.5, fy: float = 0.5) -> tuple[float, float]:
        x1, y1, x2, y2 = slot
        nx = x1 + (x2 - x1) * fx
        ny = y1 + (y2 - y1) * fy
        canvas = self.profile.canvas
        if canvas is not None:
            cx1, cy1, cx2, cy2 = canvas
            nx = cx1 + nx * (cx2 - cx1)
            ny = cy1 + ny * (cy2 - cy1)
        return (self.window.x + nx * self.window.w,
                self.window.y + ny * self.window.h)

    def point_from_image(self, nx: float, ny: float) -> tuple[float, float]:
        """**整帧**归一化坐标（截图 0-1）→ 屏幕逻辑点。

        `point()` 收的是槽位坐标（相对 canvas）；颜色定位（橙色手柄）返回的
        是整帧归一化坐标，带黑边的 profile 里两者差一个 canvas 偏移，不能
        直接把整帧坐标当槽位坐标喂给 `point()`。
        """
        canvas = self.profile.canvas
        if canvas is not None:
            cx1, cy1, cx2, cy2 = canvas
            if cx2 > cx1 and cy2 > cy1:
                nx = (nx - cx1) / (cx2 - cx1)
                ny = (ny - cy1) / (cy2 - cy1)
        return (self.window.x + nx * self.window.w,
                self.window.y + ny * self.window.h)

    def rect(self, slot: Slot, shape: str = "rect") -> Rect:
        """归一化槽位 → 屏幕逻辑点矩形（给 Clicker 的按钮用）。"""
        x1, y1 = self.point(slot, 0.0, 0.0)
        x2, y2 = self.point(slot, 1.0, 1.0)
        return Rect(x1, y1, x2 - x1, y2 - y1, shape)


# ─────────────────────────── 截屏 ───────────────────────────

class ScreenCapture:
    """用 macOS screencapture 截游戏窗口（逻辑点 → 物理像素图）。

    优先按 **window_id** 用 `screencapture -l` 截指定窗口：即使别的窗口盖在
    上面也能拿到该窗口内容。没有 id（手填坐标）才退回 `-R` 屏幕矩形截图，
    这时被遮挡会截到上面的窗口。
    """

    def __init__(self, window: WindowRect, command: str,
                 *, window_id: int = 0,
                 window_command: str | None = None) -> None:
        self.window = window
        self.command = command
        self.window_id = int(window_id or 0)
        self.window_command = (window_command
                               or "screencapture -x -o -l {id} {out}")

    def capture(self) -> np.ndarray:
        fd, path = tempfile.mkstemp(suffix=".png", prefix="bet_slider_")
        os.close(fd)
        try:
            if self.window_id:
                cmd = self.window_command.format(id=self.window_id, out=path)
            else:
                cmd = self.command.format(x=self.window.x, y=self.window.y,
                                          w=self.window.w, h=self.window.h,
                                          out=path)
            subprocess.run(cmd, shell=True, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            img = cv2.imread(path)
            if img is None:
                raise RuntimeError(f"截图为空：{path}")
            return img
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class WindowCapture:
    """把屏幕窗口截图包装成 cv2.VideoCapture 的 read/get/release 接口。

    给 live 的 `--url window:title:WePoker` 用：不经过 OBS/虚拟摄像头，
    每帧直接截浏览器窗口，分辨率/构图和 profile 完全一致，不缩放不裁黑边。
    `screencapture` 单帧约 100–200ms，实际 5–8fps，足够 OCR 的 ~1fps。
    """

    def __init__(self, spec, command: str | None = None,
                 *, window_command: str | None = None) -> None:
        info = resolve_window_info(spec)
        self.rect = info.rect if info is not None else resolve_window(spec)
        self.window_id = info.window_id if info is not None else 0
        self.owner = info.owner if info is not None else ""
        self._source = ScreenCapture(
            self.rect,
            command or "screencapture -x -R {x},{y},{w},{h} {out}",
            window_id=self.window_id, window_command=window_command)
        self._size: tuple[int, int] | None = None
        # 最近一次截图失败的原因。read() 不能把原因吞掉：没有「屏幕录制」
        # 权限时，上层只会看到「流中断」无限重连，完全不知道去开权限。
        self.last_error: str | None = None

    def capture(self) -> np.ndarray:
        img = self._source.capture()
        self._size = (img.shape[1], img.shape[0])
        self.last_error = None
        return img

    def isOpened(self) -> bool:       # noqa: N802 — 模仿 cv2.VideoCapture
        return True

    def _ensure_size(self) -> tuple[int, int]:
        if self._size is None:
            try:
                img = self.capture()
                self._size = (img.shape[1], img.shape[0])
            except Exception as exc:      # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                return (0, 0)
        return self._size

    def get(self, prop: int) -> float:  # noqa: N802
        w, h = self._ensure_size()
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(w)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(h)
        if prop == cv2.CAP_PROP_FPS:
            return 8.0
        return 0.0

    def read(self):
        try:
            img = self.capture()
        except Exception as exc:          # noqa: BLE001 — 截屏失败交给上层重连
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False, None
        return True, img

    def release(self) -> None:        # noqa: N802
        return None


def front_window() -> WindowRect:
    """取前台窗口的 position/size（逻辑点）。失败时给出 --window 的提示。"""
    script = ('tell application "System Events" to tell first process whose '
              'frontmost is true to get {position, size} of front window')
    try:
        out = subprocess.check_output(
            ["osascript", "-e", script], text=True,
            stderr=subprocess.STDOUT).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "取前台窗口失败（需要辅助功能权限）；请改用 "
            "--window x,y,w,h") from exc
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", out)]
    if len(nums) < 4:
        raise RuntimeError(f"解析前台窗口失败：{out!r}；请用 --window x,y,w,h")
    return WindowRect(nums[0], nums[1], nums[2], nums[3])


@dataclass(frozen=True)
class WindowInfo:
    """屏幕上的一个窗口（CGWindowList 的逻辑点 bounds）。"""

    index: int
    owner: str
    title: str
    rect: WindowRect
    window_id: int = 0

    def to_dict(self) -> dict:
        return {"index": self.index, "owner": self.owner,
                "title": self.title, "window_id": self.window_id,
                **self.rect.to_dict()}

    def format(self) -> str:
        title = self.title or "-"
        return (f"[{self.index}] id={self.window_id} {self.owner} | {title} | "
                f"{self.rect.x:g},{self.rect.y:g},"
                f"{self.rect.w:g},{self.rect.h:g}")


_SWIFT_LIST_WINDOWS = r"""
import CoreGraphics
let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] ?? []
for w in list {
    let id = w[kCGWindowNumber as String] as? Int ?? 0
    let owner = w[kCGWindowOwnerName as String] as? String ?? ""
    let title = w[kCGWindowName as String] as? String ?? ""
    let layer = w[kCGWindowLayer as String] as? Int ?? 0
    guard layer == 0 else { continue }
    guard let b = w[kCGWindowBounds as String] as? [String: Any] else { continue }
    let x = b["X"] as? Double ?? 0
    let y = b["Y"] as? Double ?? 0
    let width = b["Width"] as? Double ?? 0
    let height = b["Height"] as? Double ?? 0
    if width < 100 || height < 100 { continue }
    print("\(id)\t\(owner)\t\(title)\t\(Int(x)),\(Int(y)),\(Int(width)),\(Int(height))")
}
"""


def list_windows() -> list[WindowInfo]:
    """枚举屏幕上的普通窗口（layer 0），不依赖「谁在前台」。

    用 CoreGraphics 的 CGWindowListCopyWindowInfo；在终端里执行时终端自己
    是不是前台都无所谓。返回的逻辑点 bounds 可直接填给 window/--window。
    """
    swift = shutil.which("swift")
    if not swift:
        raise RuntimeError(
            "找不到 swift（需要 Xcode Command Line Tools）；"
            "请改用手工 x,y,w,h 或 --window front")
    try:
        proc = subprocess.run(
            [swift, "-e", _SWIFT_LIST_WINDOWS],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"枚举窗口失败：{exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"枚举窗口失败：{(proc.stderr or proc.stdout).strip()[:300]}")
    out: list[WindowInfo] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        raw_id, owner, title, bounds = parts[0], parts[1], parts[2], parts[3]
        nums = bounds.split(",")
        if len(nums) != 4:
            continue
        try:
            x, y, w, h = (float(v) for v in nums)
            rect = WindowRect(x, y, w, h)
            window_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        out.append(WindowInfo(len(out), owner, title, rect, window_id))
    return out


def find_windows(query: str, *, field: str = "any") -> list[WindowInfo]:
    """按 owner/title/any 子串找窗口（大小写不敏感）。"""
    q = query.strip().lower()
    if not q:
        return []
    hits = []
    for win in list_windows():
        owner = win.owner.lower()
        title = win.title.lower()
        if field == "owner" and q in owner:
            hits.append(win)
        elif field == "title" and q in title:
            hits.append(win)
        elif field == "any" and (q in owner or q in title):
            hits.append(win)
    return hits


def resolve_window_info(spec) -> WindowInfo | None:
    """按 index/title/owner 找窗口；坐标/dict/front 返回 None（没有 window_id）。"""
    if isinstance(spec, WindowInfo):
        return spec
    if isinstance(spec, WindowRect) or isinstance(spec, Mapping):
        return None
    text = str(spec).strip()
    if not text or text.lower() == "front":
        return None
    if text.lower().startswith("index:") or text.startswith("#"):
        raw = text.split(":", 1)[1] if ":" in text else text[1:]
        idx = int(raw)
        wins = list_windows()
        if not (0 <= idx < len(wins)):
            raise ValueError(f"窗口索引 {idx} 超出范围（共 {len(wins)} 个）")
        return wins[idx]
    field = "any"
    query = text
    if ":" in text:
        prefix, rest = text.split(":", 1)
        if prefix.lower() in ("title", "owner", "any"):
            field, query = prefix.lower(), rest
    if field == "any":
        nums = [p.strip() for p in text.replace("，", ",").split(",")]
        if len(nums) == 4:
            try:
                WindowRect(*(float(v) for v in nums))
                return None
            except ValueError:
                pass
    hits = find_windows(query, field=field)
    if not hits:
        raise ValueError(
            f"没找到窗口 {query!r}；先运行 --list-windows 看 id/owner/title")
    if len(hits) > 1:
        lines = "\n".join(w.format() for w in hits)
        raise ValueError(
            f"{query!r} 命中 {len(hits)} 个窗口，请改用 index:N：\n{lines}")
    return hits[0]


def resolve_window(spec) -> WindowRect:
    """把 --window / 配置里的 window 解析成屏幕逻辑点矩形。

    支持：
      · {x,y,w,h} 或 "x,y,w,h"
      · "front"（前台进程的 front window；终端是前台时会拿到终端，尽量别用）
      · "index:2" / "#2"（先用 --list-windows 看索引）
      · "title:WePoker" / "owner:Google Chrome" / 裸子串（owner 或 title 匹配）
    """
    if isinstance(spec, WindowRect):
        return spec
    if isinstance(spec, Mapping):
        return WindowRect(float(spec["x"]), float(spec["y"]),
                          float(spec["w"]), float(spec["h"]))
    info = resolve_window_info(spec)
    if info is not None:
        return info.rect
    text = str(spec).strip()
    if text.lower() == "front":
        return front_window()
    nums = [p.strip() for p in text.replace("，", ",").split(",")]
    if len(nums) == 4:
        return WindowRect(*(float(v) for v in nums))
    raise ValueError(f"无法解析 window：{spec!r}")


def clicker_config_from_profile(profile: Profile, window: WindowRect,
                                base: ClickerConfig | None = None
                                ) -> ClickerConfig:
    """用 profile 的归一化槽位 + window 直接生成点击按钮，免手标 clicker_local。

    `fold / call / 5 个快捷档位` 的屏幕矩形由
    `window.x + slot.x * window.w` 计算；按钮形状默认圆形。`allin` 只有
    profile 里存在 `hero_allin_button` 才生成，否则保持「不建议自动全下」。
    """
    mapper = WindowMapper(profile, window)
    buttons: dict[str, Rect] = {}
    missing: list[str] = []
    for name, (slot_key, shape) in PROFILE_BUTTON_SLOTS.items():
        slot = profile.global_slots.get(slot_key)
        if slot is None:
            if name in ("fold", "call"):
                missing.append(slot_key)
            continue
        buttons[name] = mapper.rect(slot, shape=shape)
    if missing:
        raise ValueError(
            "profile 缺少必需按钮槽位：" + "、".join(missing))
    if base is None:
        return ClickerConfig(buttons=buttons,
                             inset_ratio=PROFILE_BUTTON_INSET)
    # 保留 clicker 的 retry/tolerance 等标量，只替换 buttons 和圆钮内缩。
    from dataclasses import replace as _replace
    return _replace(base, buttons=buttons,
                    inset_ratio=PROFILE_BUTTON_INSET)


# ─────────────────────────── 操作 ───────────────────────────

@dataclass(frozen=True)
class SliderSettings:
    hold_ms: int = 650
    release_after_open: bool = True
    click_ms: int = 60
    scroll_interval_ms: int = 160
    settle_ms: int = 140
    poll_ms: int = 100
    open_timeout_ms: int = 1500
    max_steps: int = 80
    tolerance_bb: float = 0.01
    max_no_change: int = 3
    scroll_batch_max: int = 5
    invert_scroll: bool = False
    read_fraction: bool = False   # 闭环只读金额；左侧百分比是证据，开它会每次多跑一次 det
    confirm_timeout_ms: int = 1200
    # 整个闭环（打开→改额→确认）的总时限：客户端回合通常只有 10–15s，
    # max_steps × 每步耗时（截图+settle+拖动）能轻松超过它。
    total_timeout_ms: int = 8000
    notch_bb: float = 1.0          # 只用于 dry-run 估算，闭环不依赖它
    dark_max: int = TRACK_DARK_MAX  # 深色轨道像素阈值（灰阶 < 它算深色）
    open_min: float = TRACK_OPEN_MIN  # 深色占比 ≥ 它 → evidence.dark_open（不参与 open 判定）
    preset_white_min: float = PRESET_WHITE_MIN
    preset_v99_min: float = PRESET_V99_MIN
    # ── 改额方式 ──────────────────────────────────────────────
    # drag  = 按住橙色手柄上下拖（2026-11-10 的新 UI，滚轮无效）
    # wheel = 老 UI 的滚轮（保留兼容）
    control: str = "wheel"
    drag_px_per_bb: float = 6.0    # 初始估计；第一次拖动后按实测更新
    drag_min_px: float = 6.0       # 单次拖动的最小像素，防抖
    drag_max_px: float = 120.0     # 单次拖动的最大像素
    drag_steps: int = 8            # 拖动过程中的 move 事件数（H5 要看到中间移动）
    drag_step_ms: int = 12
    drag_hold_ms: int = 80         # 按下后先停一下再拖（H5 要「按住拖动」）
    # 客户端滑杆的可达粒度（BB）：AI 给的 1/3 池常是 1.33BB，而滑杆只能落在
    # 整数 BB 上 —— 不先取整会 1↔2 来回拖、最后振荡失败。0 = 不取整。
    round_to_bb: float = 0.0
    close_slot: str = "hero_bet_close"  # 取消时点的「右侧空白处」槽位

    def __post_init__(self) -> None:
        """配置值一律 fail fast：新配置项静默默认比报错危险得多。"""
        def need(ok: bool, msg: str) -> None:
            if not ok:
                raise ValueError(msg)

        for name in ("hold_ms", "click_ms", "scroll_interval_ms", "settle_ms",
                     "poll_ms", "drag_step_ms", "drag_hold_ms"):
            value = getattr(self, name)
            need(isinstance(value, int) and value >= 0,
                 f"{name} 必须是非负整数：{value!r}")
        need(self.open_timeout_ms > 0,
             f"open_timeout_ms 必须 > 0：{self.open_timeout_ms}")
        need(self.confirm_timeout_ms > 0,
             f"confirm_timeout_ms 必须 > 0：{self.confirm_timeout_ms}")
        need(self.total_timeout_ms > 0,
             f"total_timeout_ms 必须 > 0：{self.total_timeout_ms}")
        need(self.max_steps >= 1, f"max_steps 至少为 1：{self.max_steps}")
        need(self.max_no_change >= 1,
             f"max_no_change 至少为 1：{self.max_no_change}")
        need(self.scroll_batch_max >= 1,
             f"scroll_batch_max 至少为 1：{self.scroll_batch_max}")
        need(math.isfinite(self.tolerance_bb) and self.tolerance_bb >= 0,
             f"tolerance_bb 必须 ≥ 0：{self.tolerance_bb}")
        need(math.isfinite(self.notch_bb) and self.notch_bb > 0,
             f"notch_bb 必须 > 0：{self.notch_bb}")
        need(0 <= self.dark_max <= 255,
             f"dark_max 必须在 0–255 之间：{self.dark_max}")
        need(0.0 <= self.open_min <= 1.0,
             f"open_min 必须在 0–1 之间：{self.open_min}")
        need(0.0 <= self.preset_white_min <= 1.0,
             f"preset_white_min 必须在 0–1 之间：{self.preset_white_min}")
        need(0.0 <= self.preset_v99_min <= 255.0,
             f"preset_v99_min 必须在 0–255 之间：{self.preset_v99_min}")
        need(self.control in ("drag", "wheel"),
             f"control 只能是 drag 或 wheel：{self.control!r}")
        need(math.isfinite(self.drag_px_per_bb) and self.drag_px_per_bb > 0,
             f"drag_px_per_bb 必须 > 0：{self.drag_px_per_bb}")
        need(math.isfinite(self.drag_min_px) and self.drag_min_px > 0,
             f"drag_min_px 必须 > 0：{self.drag_min_px}")
        need(math.isfinite(self.drag_max_px)
             and self.drag_max_px >= self.drag_min_px,
             f"drag_max_px 必须 ≥ drag_min_px：{self.drag_max_px}")
        need(self.drag_steps >= 2,
             f"drag_steps 至少为 2：{self.drag_steps}")
        need(math.isfinite(self.round_to_bb) and self.round_to_bb >= 0,
             f"round_to_bb 必须 ≥ 0：{self.round_to_bb}")
        need(isinstance(self.close_slot, str) and bool(self.close_slot.strip()),
             f"close_slot 不能为空：{self.close_slot!r}")

    @classmethod
    def from_config(cls, raw: Mapping | None,
                    *, source: str | Path | None = None) -> "SliderSettings":
        """从 `config/bet_slider_mac.json` 这样的 dict 构造，并逐个校验。

        以前是在 live.py / tools 里各写一遍 `int(raw.get(...))`：配置写错
        （`hold_ms: "650ms"`、`control: "dragg"`、`"release_after_open": "false"`）
        要么抛原始 traceback，要么静默退化成另一个行为。这里统一收口：
        类型不对、取值越界都直接报错，`source` 只用于把文件名带进错误信息。
        """
        raw = raw if isinstance(raw, Mapping) else {}
        base = cls()
        where = f"（{source}）" if source is not None else ""

        def pick_int(key: str) -> int:
            if key not in raw:
                return getattr(base, key)
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(f"{key} 必须是非负整数{where}：{value!r}")
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{key} 必须是非负整数{where}：{value!r}") from exc

        def pick_float(key: str) -> float:
            if key not in raw:
                return getattr(base, key)
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(f"{key} 必须是数字{where}：{value!r}")
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{key} 必须是数字{where}：{value!r}") from exc

        def pick_bool(key: str) -> bool:
            if key not in raw:
                return getattr(base, key)
            value = raw[key]
            if not isinstance(value, bool):
                raise ValueError(
                    f"{key} 必须是 true/false{where}：{value!r}")
            return value

        def pick_str(key: str) -> str:
            if key not in raw:
                return getattr(base, key)
            value = raw[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} 必须是非空字符串{where}：{value!r}")
            return value

        return cls(
            hold_ms=pick_int("hold_ms"),
            release_after_open=pick_bool("release_after_open"),
            click_ms=pick_int("click_ms"),
            scroll_interval_ms=pick_int("scroll_interval_ms"),
            settle_ms=pick_int("settle_ms"),
            poll_ms=pick_int("poll_ms"),
            open_timeout_ms=pick_int("open_timeout_ms"),
            max_steps=pick_int("max_steps"),
            tolerance_bb=pick_float("tolerance_bb"),
            max_no_change=pick_int("max_no_change"),
            scroll_batch_max=pick_int("scroll_batch_max"),
            invert_scroll=pick_bool("invert_scroll"),
            read_fraction=pick_bool("read_fraction"),
            confirm_timeout_ms=pick_int("confirm_timeout_ms"),
            total_timeout_ms=pick_int("total_timeout_ms"),
            notch_bb=pick_float("notch_bb"),
            dark_max=pick_int("dark_max"),
            open_min=pick_float("open_min"),
            preset_white_min=pick_float("preset_white_min"),
            preset_v99_min=pick_float("preset_v99_min"),
            control=pick_str("control").strip().lower(),
            drag_px_per_bb=pick_float("drag_px_per_bb"),
            drag_min_px=pick_float("drag_min_px"),
            drag_max_px=pick_float("drag_max_px"),
            drag_steps=pick_int("drag_steps"),
            drag_step_ms=pick_int("drag_step_ms"),
            drag_hold_ms=pick_int("drag_hold_ms"),
            round_to_bb=pick_float("round_to_bb"),
            close_slot=pick_str("close_slot"),
        )


@dataclass
class SliderResult:
    status: str                    # set | dry_run | skipped | error
    target_bb: float | None = None
    start_bb: float | None = None
    final_bb: float | None = None
    steps: int = 0
    scrolls: int = 0
    drags: int = 0
    reason: str | None = None
    error: str | None = None
    evidence: dict = field(default_factory=dict)
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        d = {
            "status": self.status,
            "target_bb": self.target_bb,
            "start_bb": self.start_bb,
            "final_bb": self.final_bb,
            "steps": self.steps,
            "scrolls": self.scrolls,
            "drags": self.drags,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.reason:
            d["reason"] = self.reason
        if self.error:
            d["error"] = self.error
        if self.evidence:
            d["evidence"] = self.evidence
        return d


def plan_slider(current_bb: float | None, target_bb: float,
                notch_bb: float = 1.0) -> dict:
    """不开滑杆时的静态估算（CLI --dry-run 用）。方向 +1 = 向上加额。"""
    if current_bb is None or not math.isfinite(current_bb) \
            or not math.isfinite(target_bb) or target_bb <= 0:
        return {"direction": 0, "estimated_notches": 0, "reason": "invalid"}
    diff = target_bb - current_bb
    if abs(diff) <= 1e-9:
        return {"direction": 0, "estimated_notches": 0, "reason": "already_at_target"}
    notches = max(1, int(math.ceil(abs(diff) / max(0.01, notch_bb))))
    return {"direction": 1 if diff > 0 else -1,
            "estimated_notches": notches, "reason": "estimate_only"}


class BetSliderController:
    """长按主按钮 → 改额 → 读回 → 到目标 → 点确定。

    两种改额方式（`SliderSettings.control`）：

      · `drag` —— 2026-11-10 的新 UI：按住橙色额度手柄上下拖。手柄位置随额度
        移动，所以每次拖前都用颜色重新找（见 `find_slider_handle`）；
      · `wheel` —— 老 UI 的滚轮，保留兼容。

    `frames` 是零参函数（返回一帧 BGR）或带 `.capture()` 的对象。点击后端
    复用 `clicker.ClickBackend`。`reader` 默认真实 OCR，测试可注入假读帧；
    `handle_locator` 也可注入（返回归一化手柄坐标），测试不用真实截图。
    所有按钮点击都在圆盘内随机取点（`SLIDER_CLICK_INSET`，半径 70%），
    `rng` 可注入以便测试复现。
    """

    def __init__(self, profile: Profile, backend: ClickBackend,
                 frames: Callable[[], np.ndarray] | FrameSource,
                 mapper: WindowMapper, settings: SliderSettings | None = None,
                 *, engine: OcrEngine | None = None,
                 reader: Callable[..., BetSliderState] = read_slider,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] | None = None,
                 dry_run: bool = False,
                 rng: random.Random | None = None,
                 handle_locator: Callable[[np.ndarray],
                                          tuple[float, float] | None] | None = None,
                 ) -> None:
        self.profile = profile
        self.backend = backend
        self.frames = frames
        self.mapper = mapper
        self.settings = settings or SliderSettings()
        self.engine = engine
        self.reader = reader
        self._sleep = sleep
        self._clock = clock
        self._log = log or (lambda _msg: None)
        self.dry_run = dry_run
        self.rng = rng or random.Random()
        self._handle_locator = handle_locator
        self._held: tuple[float, float] | None = None
        self._opened = False

    # ── 基础动作 ──
    def _capture(self) -> np.ndarray:
        if callable(self.frames):
            return self.frames()
        return self.frames.capture()

    def read(self, img: np.ndarray | None = None) -> BetSliderState:
        img = self._capture() if img is None else img
        return self.reader(img, self.profile, self.engine,
                           read_fraction=self.settings.read_fraction,
                           dark_max=self.settings.dark_max,
                           open_min=self.settings.open_min)

    def preset_enabled(self, preset_id: str) -> bool | None:
        """快捷档位是否可点；None = 读不准（按不可点处理，退回滑杆）。"""
        try:
            state = read_preset_enabled(
                self._capture(), self.profile, preset_id,
                white_min=self.settings.preset_white_min,
                v99_min=self.settings.preset_v99_min)
        except Exception as exc:        # noqa: BLE001 — 读帧失败按不可点
            self._log(f"[滑杆] 档位 {preset_id} 状态读取失败："
                      f"{type(exc).__name__}: {exc}")
            return None
        self._log(f"[滑杆] 档位 {preset_id} "
                  f"{'可点' if state.enabled else '置灰/读不准'}"
                  f"（white={state.white_frac:.3f}, v99={state.v99:.0f}）")
        return state.enabled

    def _slot_point(self, key: str, fx: float = 0.5,
                    fy: float = 0.5) -> tuple[float, float]:
        slot = self.profile.global_slots.get(key)
        if slot is None:
            raise BetSliderError(f"profile 缺少 {key}")
        return self.mapper.point(slot, fx, fy)

    def _button_point(self, key: str) -> tuple[float, float]:
        """按钮点击点：圆盘内随机取，半径 = 原半径 × 70%。

        和 clicker 的按钮采样同一口径（避免每次都点几何中心 + 落在圆心
        那个最容易被动画/拇指遮挡的点）。槽位不是正方形时用**正圆**
        （`Rect.inset_disc_point`），椭圆采样会把长边方向的点推到圆外。
        """
        slot = self.profile.global_slots.get(key)
        if slot is None:
            raise BetSliderError(f"profile 缺少 {key}")
        rect = self.mapper.rect(slot, shape="disc")
        return rect.inset_disc_point(self.rng, SLIDER_CLICK_INSET)

    def _deadline(self) -> float:
        return self._clock() + self.settings.total_timeout_ms / 1000.0

    def _check_deadline(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise BetSliderError("slider_timeout")

    def _preflight(self) -> None:
        try:
            allowed = self.backend.preflight()
        except Exception as exc:        # noqa: BLE001 — 后端不可用只报错
            raise BetSliderError(f"点击后端不可用：{type(exc).__name__}: {exc}") from exc
        if not allowed:
            raise BetSliderError("accessibility_permission_denied")

    def _click(self, point: tuple[float, float]) -> None:
        if self.dry_run:
            self._log(f"[滑杆] dry-run click ({point[0]:.1f}, {point[1]:.1f})")
            return
        self.backend.move(*point)
        self.backend.mouse_down(*point)
        self._sleep(max(0.0, self.settings.click_ms) / 1000.0)
        self.backend.mouse_up(*point)

    def _press(self, point: tuple[float, float], hold_ms: int,
               release: bool) -> None:
        if self.dry_run:
            self._log(f"[滑杆] dry-run long-press ({point[0]:.1f}, {point[1]:.1f}) "
                      f"{hold_ms}ms")
            return
        self.backend.move(*point)
        self.backend.mouse_down(*point)
        self._sleep(max(0.0, hold_ms) / 1000.0)
        if release:
            self.backend.mouse_up(*point)

    def _release_held(self) -> None:
        if self._held is None:
            return
        point, self._held = self._held, None
        if not self.dry_run:
            self.backend.mouse_up(*point)

    def _scroll(self, point: tuple[float, float], delta: int) -> None:
        if self.dry_run:
            self._log(f"[滑杆] dry-run scroll ({point[0]:.1f}, {point[1]:.1f}) "
                      f"delta={delta:+d}")
            self._sleep(self.settings.scroll_interval_ms / 1000.0)
            return
        self.backend.move(*point)
        self.backend.scroll(point[0], point[1], delta)
        self._sleep(self.settings.scroll_interval_ms / 1000.0)

    # ── 改额：拖动手柄（新 UI） ──
    def _handle_point(self, img: np.ndarray | None = None
                      ) -> tuple[float, float] | None:
        """橙色额度手柄的屏幕逻辑点；找不到返回 None。"""
        img = self._capture() if img is None else img
        if img is None or img.size == 0:
            return None
        if self._handle_locator is not None:
            norm = self._handle_locator(img)
            if norm is None:
                return None
            nx, ny = norm
        else:
            found = find_slider_handle(
                img, self.profile, (img.shape[1], img.shape[0]))
            if found is None:
                return None
            cx, cy, _r = found
            nx, ny = cx / img.shape[1], cy / img.shape[0]
        return self.mapper.point_from_image(nx, ny)

    def _drag_handle(self, dy: float) -> None:
        """按住额度手柄上下拖 dy 像素（dy<0 = 向上加额）。"""
        point = self._handle_point()
        if point is None:
            raise BetSliderError("handle_not_found")
        x, y = round(point[0], 1), round(point[1], 1)
        if self.dry_run:
            self._log(f"[滑杆] dry-run drag ({x:.1f}, {y:.1f}) dy={dy:+.1f}")
            return
        self.backend.move(x, y)
        self.backend.mouse_down(x, y)
        # 记成 held：拖动中途抛异常时 cancel() 的 _release_held 也能抬起左键，
        # 不会把鼠标按钮卡在按下状态。
        self._held = (x, y)
        try:
            self._sleep(max(0, self.settings.drag_hold_ms) / 1000.0)
            # 每一步至少 1px：H5 会忽略亚像素移动，小额度时别把一步拆成 0.75px。
            steps = max(2, min(int(self.settings.drag_steps), int(abs(dy)) or 2))
            for i in range(1, steps + 1):
                self.backend.move(round(x, 1), round(y + dy * i / steps, 1))
                self._sleep(max(0, self.settings.drag_step_ms) / 1000.0)
            self.backend.mouse_up(round(x, 1), round(y + dy, 1))
        finally:
            self._held = None

    # ── 打开 ──
    def open_slider(self) -> BetSliderState:
        """已打开直接返回；否则长按主按钮并轮询。失败抛 BetSliderError。"""
        state = self.read()
        if state.open:
            self._opened = True
            return state
        if state.error == "amount_unreadable":
            # 轨道是深色但金额气泡读不出：滑杆可能已经开着。此时**不能**再
            # 长按主按钮（那等于在当前额度上点一下确认），直接停手；标记
            # _opened 让上层失败时负责点右侧空白把它关掉（否则滑杆会一直
            # 挂着盖住公共牌）。
            self._opened = True
            raise BetSliderError("amount_unreadable")
        point = self._button_point("hero_raise_button")
        self._preflight()
        self._press(point, self.settings.hold_ms,
                    release=self.settings.release_after_open)
        if not self.settings.release_after_open:
            self._held = point
        deadline = self._clock() + self.settings.open_timeout_ms / 1000.0
        while self._clock() < deadline:
            self._sleep(max(1, self.settings.poll_ms) / 1000.0)
            state = self.read()
            if state.open:
                self._opened = True
                return state
        self._release_held()
        raise BetSliderError("slider_did_not_open")

    def cancel(self) -> bool:
        """关掉滑杆：点「右侧空白毛毡」，返回是否已关闭。

        2026-11-10 的新 UI **不认 Esc**（实测滑杆一直挂着，把公共牌第三张
        盖住，直接引出 board 2 张的误报），而且 Esc 是发给当前键盘焦点 App 的
        全局按键 —— 窗口不在最前时会落到别的程序上。所以这里只保留 H5 的
        「点外部关闭」：在 `close_slot`（右侧空白毛毡）里随机点一下。
        """
        self._release_held()
        if self.dry_run:
            self._log("[滑杆] dry-run cancel（点右侧空白）")
            return True
        if self.profile.global_slots.get(self.settings.close_slot) is None:
            return False
        try:
            self._click(self._button_point(self.settings.close_slot))
            self._sleep(max(1, self.settings.poll_ms) / 1000.0)
            return not self.read().open
        except Exception:                # noqa: BLE001
            return False

    # ── 主流程 ──
    def _confirm_slider(self) -> BetSliderState:
        """点确定并确认滑杆关闭；失败抛 BetSliderError。"""
        self._release_held()
        confirm = self._button_point("hero_bet_confirm")
        self._click(confirm)
        deadline = self._clock() + self.settings.confirm_timeout_ms / 1000.0
        state = self.read()
        while self._clock() < deadline:
            self._sleep(max(1, self.settings.poll_ms) / 1000.0)
            state = self.read()
            if not state.open:
                break
        if state.open:
            raise BetSliderError("confirm_not_effective")
        return state

    def set_amount(self, target_bb: float | None) -> SliderResult:
        started = self._clock()
        target = float(target_bb) if target_bb is not None else None
        result = SliderResult("error", target_bb=target)
        if target is None or not math.isfinite(target) or target <= 0:
            result.error = "invalid_target"
            return result

        control = str(self.settings.control or "wheel").lower()
        requested = target
        if control == "drag" and self.settings.round_to_bb > 0:
            grid = float(self.settings.round_to_bb)
            snapped = max(grid, round(target / grid) * grid)
            if abs(snapped - target) > 1e-9:
                self._log(f"[滑杆] 目标 {target:g}BB 不在客户端 {grid:g}BB "
                          f"粒度上，按最近档取 {snapped:g}BB")
                target = snapped
                result.target_bb = target
                result.evidence["requested_bb"] = requested
                result.evidence["snapped_to_bb"] = snapped

        if self.dry_run:
            state = self.read()
            plan = plan_slider(state.amount_bb, target, self.settings.notch_bb)
            result.status = "dry_run" if state.open else "skipped"
            result.start_bb = state.amount_bb
            result.final_bb = state.amount_bb
            result.reason = plan.get("reason")
            result.evidence = {"slider": state.to_dict(), "plan": plan,
                               "control": control}
            result.elapsed_ms = int((self._clock() - started) * 1000)
            return result

        try:
            state = self.open_slider()
            result.start_bb = state.amount_bb
            result.final_bb = state.amount_bb
            if state.amount_bb is None:
                raise BetSliderError(
                    "target_exceeds_allin" if state.allin
                    else "amount_unreadable")

            deadline = self._deadline()
            point = self._slot_point("hero_bet_slider", fx=0.5, fy=0.55)
            amount = state.amount_bb
            per_notch: float | None = None
            px_per_bb = max(0.5, float(self.settings.drag_px_per_bb))
            no_change = 0
            recent: list[float] = []
            for _ in range(max(1, self.settings.max_steps)):
                self._check_deadline(deadline)
                if abs(amount - target) <= self.settings.tolerance_bb:
                    break
                diff = target - amount
                before = amount
                if control == "drag":
                    dy = -diff * px_per_bb
                    mag = min(max(self.settings.drag_min_px, abs(dy)),
                              self.settings.drag_max_px)
                    dy = mag if dy >= 0 else -mag
                    self._drag_handle(dy)
                    result.drags += 1
                else:
                    direction = 1 if diff > 0 else -1
                    if self.settings.invert_scroll:
                        direction = -direction
                    n = 1
                    if per_notch and per_notch > 0:
                        n = int(round(abs(diff) / per_notch))
                        n = max(1, min(max(1, self.settings.scroll_batch_max), n))
                    for _ in range(n):
                        self._scroll(point, direction)
                        result.scrolls += 1
                self._sleep(max(0, self.settings.settle_ms) / 1000.0)
                state = self.read()
                result.steps += 1
                if not state.open:
                    raise BetSliderError("slider_closed_during_scroll")
                if state.allin:
                    # 滑杆已经到顶（顶部显示 ALL IN）：目标额度超过后手，
                    # 到不了 —— 报错停手，绝不在 all-in 额度上落一个近似注。
                    raise BetSliderError(
                        f"target_exceeds_allin(target={target:g})")
                if state.amount_bb is None:
                    raise BetSliderError("amount_unreadable_after_scroll")
                amount = state.amount_bb
                result.final_bb = amount
                if control == "drag":
                    self._log(f"[滑杆] 拖动 {dy:+.0f}px → {amount:g}BB"
                              f"（目标 {target:g}）")
                if abs(amount - before) <= 1e-9:
                    no_change += 1
                    per_notch = None
                    if no_change >= max(1, self.settings.max_no_change):
                        raise BetSliderError(
                            f"amount_not_changing(at {amount:g}BB)")
                else:
                    no_change = 0
                    if control == "drag":
                        px_per_bb = max(0.2, abs(dy)
                                        / max(1e-9, abs(amount - before)))
                    else:
                        per_notch = abs(amount - before) / max(1, n)
                recent.append(round(amount, 2))
                if (len(recent) >= 4 and recent[-1] == recent[-3]
                        and recent[-2] == recent[-4]
                        and recent[-1] != recent[-2]
                        and abs(amount - target) > self.settings.tolerance_bb):
                    raise BetSliderError(
                        f"amount_oscillating({recent[-2]:g}↔{recent[-1]:g})")

            if abs(amount - target) > self.settings.tolerance_bb:
                raise BetSliderError(
                    f"target_unreachable(target={target:g}, final={amount:g})")

            # 目标额已到位，才允许落注。
            state = self._confirm_slider()
            result.status = "set"
            result.final_bb = target
            result.evidence = {**result.evidence,
                               "slider": state.to_dict(),
                               "control": control,
                               "px_per_bb": round(px_per_bb, 2)}
            self._opened = False
        except BetSliderError as exc:
            result.status = "error"
            result.error = str(exc)
            if self._opened:
                closed = self.cancel()
                result.evidence["cancel_closed"] = closed
                if not closed:
                    result.error += "；滑杆未关闭，请手动点右侧空白"
                self._opened = False
        except Exception as exc:        # noqa: BLE001 — 操作失败不能拖垮主链路
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            if self._opened:
                closed = self.cancel()
                result.evidence["cancel_closed"] = closed
                if not closed:
                    result.error += "；滑杆未关闭，请手动点右侧空白"
                self._opened = False
        result.elapsed_ms = int((self._clock() - started) * 1000)
        return result

    def set_all_in(self) -> SliderResult:
        """滑杆拉到最上面 → 全下。

        **判据只看客户端顶部的 ALLIN / 全下字样**（连续 `ALLIN_CONFIRM_READS`
        次读到才落注）。不能用「额度不再变化」猜顶端：拖动没生效、手柄找错、
        H5 改版不认拖动时，额度同样一直不变 —— 旧口径会把「拖不动」当成
        「已经到顶」，然后在 2BB 这种中间额度上点确定。到不了 ALLIN 就报错、
        点右侧空白关掉滑杆，绝不在中间额度落注。
        """
        started = self._clock()
        result = SliderResult("error", reason="bet_slider_allin")
        control = str(self.settings.control or "wheel").lower()
        if self.dry_run:
            state = self.read()
            result.status = "dry_run" if state.open else "skipped"
            result.start_bb = state.amount_bb
            result.final_bb = state.amount_bb
            result.target_bb = state.amount_bb
            result.evidence = {"slider": state.to_dict(),
                               "plan": "all_in_text",
                               "allin": state.allin,
                               "control": control}
            result.elapsed_ms = int((self._clock() - started) * 1000)
            return result

        try:
            state = self.open_slider()
            result.start_bb = state.amount_bb
            result.final_bb = state.amount_bb
            if not state.open:
                raise BetSliderError("slider_did_not_open")
            deadline = self._deadline()
            point = self._slot_point("hero_bet_slider", fx=0.5, fy=0.55)
            last_bb = state.amount_bb
            allin_text = state.amount_text if state.allin else ""
            streak = 1 if state.allin else 0
            reached = streak >= ALLIN_CONFIRM_READS
            for _ in range(max(1, self.settings.max_steps)):
                if reached:
                    break
                self._check_deadline(deadline)
                if not state.allin:
                    # 还没到顶：继续往上拖/滚。已经出现过 ALLIN 就不动，只复核。
                    if control == "drag":
                        self._drag_handle(
                            -max(self.settings.drag_min_px,
                                 self.settings.drag_max_px * 0.5))
                        result.drags += 1
                    else:
                        direction = -1 if self.settings.invert_scroll else 1
                        for _ in range(max(1, self.settings.scroll_batch_max)):
                            self._scroll(point, direction)
                            result.scrolls += 1
                self._sleep(max(0, self.settings.settle_ms) / 1000.0)
                state = self.read()
                result.steps += 1
                if state.amount_bb is not None:
                    last_bb = state.amount_bb
                    result.final_bb = last_bb
                if not state.open:
                    raise BetSliderError("slider_closed_during_scroll")
                if state.allin:
                    streak += 1
                    allin_text = state.amount_text
                    reached = streak >= ALLIN_CONFIRM_READS
                else:
                    streak = 0
                    if state.amount_bb is None:
                        raise BetSliderError("amount_unreadable_after_scroll")
            if not reached:
                raise BetSliderError(
                    f"allin_not_reached(at {last_bb if last_bb is not None else '?'}BB)")

            state = self._confirm_slider()
            result.status = "set"
            result.final_bb = last_bb
            result.target_bb = last_bb
            result.evidence = {"slider": state.to_dict(), "all_in_text": True,
                               "allin_text": allin_text, "control": control}
            self._opened = False
        except BetSliderError as exc:
            result.status = "error"
            result.error = str(exc)
            if self._opened:
                closed = self.cancel()
                result.evidence["cancel_closed"] = closed
                if not closed:
                    result.error += "；滑杆未关闭，请手动点右侧空白"
                self._opened = False
        except Exception as exc:        # noqa: BLE001 — 操作失败不能拖垮主链路
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            if self._opened:
                closed = self.cancel()
                result.evidence["cancel_closed"] = closed
                if not closed:
                    result.error += "；滑杆未关闭，请手动点右侧空白"
                self._opened = False
        result.elapsed_ms = int((self._clock() - started) * 1000)
        return result
