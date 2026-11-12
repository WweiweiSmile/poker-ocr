"""游戏窗口浮层：把 ROI / 按钮可点区盖在真实窗口上。

实现分两层：

  · `render_overlay()` 纯函数：profile + 窗口矩形 → RGBA PNG（可单测）；
  · `OverlayController` 线程：复用 `poker_ocr.overlay.ScreenOverlay`（透明、
    置顶、点击穿透、心跳退出），每秒重画一次、每 5s 核对窗口 rect。

浮层只显示、不接收鼠标；失败一律 fail-open —— 不影响 live，也不阻塞页面。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..bet_slider import WindowMapper, WindowRect
from ..overlay import ScreenOverlay
from . import geometry, sources

FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
)
# RGB；每组的框色（组开关时一眼能分清）
GROUP_COLORS = {
    "seats": (86, 156, 255),
    "actions": (255, 196, 84),
    "cards": (88, 214, 141),
    "pots": (196, 140, 255),
    "buttons": (0, 170, 255),
    "sliders": (255, 120, 160),
}
SELECTED_COLOR = (255, 214, 10)
DRAFT_COLOR = (255, 255, 255)


def _font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, max(9, int(size)))
            except OSError:
                continue
    return ImageFont.load_default()


def _window_rect(slot, mapper: WindowMapper, window: WindowRect
                 ) -> tuple[float, float, float, float]:
    """归一化槽位 → 窗口内逻辑点矩形（左上原点）。"""
    x1, y1 = mapper.point(slot, 0.0, 0.0)
    x2, y2 = mapper.point(slot, 1.0, 1.0)
    return x1 - window.x, y1 - window.y, x2 - window.x, y2 - window.y


def _dashed_rect(draw: ImageDraw.ImageDraw, box, color, width: int = 2,
                 dash: int = 10) -> None:
    x1, y1, x2, y2 = box
    for y in (y1, y2):
        x = x1
        while x < x2:
            seg_end = min(x2, x + dash)
            draw.line([(x, y), (seg_end, y)], fill=color, width=width)
            x = seg_end + dash
    for x in (x1, x2):
        y = y1
        while y < y2:
            seg_end = min(y2, y + dash)
            draw.line([(x, y), (x, seg_end)], fill=color, width=width)
            y = seg_end + dash


def render_overlay(profile, window: WindowRect, scale: float, *,
                   groups: list[str] | None = None,
                   selected: str | None = None,
                   drafts: dict | None = None,
                   labels: bool = False) -> np.ndarray:
    """窗口逻辑点 → RGBA 数组（尺寸 = 窗口 × scale）。"""
    scale = max(0.5, float(scale or 1.0))
    width = max(1, int(round(window.w * scale)))
    height = max(1, int(round(window.h * scale)))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font = _font(13)
    mapper = WindowMapper(profile, window)
    wanted = set(groups or list(GROUP_COLORS))
    drafts = drafts or {}

    for item in geometry.profile_slots(profile):
        if item["group"] not in wanted:
            continue
        slot = drafts.get(item["key"], item["slot"])
        x1, y1, x2, y2 = _window_rect(tuple(slot), mapper, window)
        box = [x1 * scale, y1 * scale, x2 * scale, y2 * scale]
        is_selected = item["key"] == selected
        is_draft = item["key"] in drafts
        color = (SELECTED_COLOR if is_selected else
                 DRAFT_COLOR if is_draft else
                 GROUP_COLORS.get(item["group"], (200, 200, 200)))
        alpha = 255 if (is_selected or is_draft) else 190
        line = color + (alpha,)
        w = 3 if is_selected else 2
        if is_draft:
            _dashed_rect(draw, box, line, width=w)
        elif item.get("shape") == "disc":
            draw.ellipse(box, outline=line, width=w)
        else:
            draw.rectangle(box, outline=line, width=w)
        if labels or is_selected:
            text = item.get("label") or item["key"]
            try:
                tw = draw.textlength(text, font=font)
            except Exception:  # noqa: BLE001
                tw = 8 * len(text)
            ty = max(0, box[1] - 16)
            draw.rectangle([box[0], ty, box[0] + tw + 6, ty + 15],
                           fill=(0, 0, 0, 150))
            draw.text((box[0] + 3, ty + 1), text, font=font,
                      fill=(255, 255, 255, 240))
        if is_selected:
            r = 5
            for hx, hy in ((box[0], box[1]), (box[2], box[1]),
                           (box[0], box[3]), (box[2], box[3])):
                draw.rectangle([hx - r, hy - r, hx + r, hy + r],
                               fill=SELECTED_COLOR + (255,))
    return np.asarray(image)


class OverlayController:
    """管理一个 ScreenOverlay 进程；线程只负责重画/心跳/窗口跟随。"""

    def __init__(self, log: Callable[[str], None] | None = None,
                 *, interval: float = 1.0,
                 overlay_factory: Callable[..., ScreenOverlay] = ScreenOverlay,
                 window_lookup: Callable[[str], dict | None] =
                 sources.window_info) -> None:
        self._log = log or (lambda _msg: None)
        self.interval = max(0.5, float(interval))
        self._factory = overlay_factory
        self._window_lookup = window_lookup
        self._lock = threading.RLock()
        self._overlay = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._profile = None
        self._spec = ""
        self._window: WindowRect | None = None
        self._scale = 1.0
        self._groups: list[str] = list(GROUP_COLORS)
        self._selected: str | None = None
        self._drafts: dict = {}
        self._error: str | None = None
        self._last_render = 0.0
        self._last_window_check = 0.0
        self._version = 0

    # ── 状态 ────────────────────────────────────────────
    def state(self) -> dict:
        with self._lock:
            return {
                "running": bool(self._overlay is not None
                                and getattr(self._overlay, "started", False)),
                "scale": self._scale,
                "window": (self._window.to_dict() if self._window else None),
                "groups": list(self._groups),
                "selected": self._selected,
                "drafts": len(self._drafts),
                "error": self._error,
            }

    # ── 生命周期 ────────────────────────────────────────
    def start(self, profile, spec: str, *, scale: float | None = None,
              groups: list[str] | None = None, selected: str | None = None,
              drafts: dict | None = None) -> dict:
        self.stop()
        window, error = self._resolve_window(spec)
        if window is None:
            with self._lock:
                self._error = error or "窗口不可用"
            return self.state()
        if scale is None:
            scale = self._scale_from(window, profile)
        with self._lock:
            self._profile = profile
            self._spec = spec
            self._window = window
            self._scale = float(scale)
            self._groups = list(groups or list(GROUP_COLORS))
            self._selected = selected
            self._drafts = dict(drafts or {})
            self._error = None
            self._stop.clear()
        try:
            overlay = self._factory(window, self._scale, log=self._log)
            if not overlay.start():
                with self._lock:
                    self._error = "浮层启动失败（osascript / AppKit 不可用）"
                return self.state()
            with self._lock:
                self._overlay = overlay
        except Exception as exc:  # noqa: BLE001 — fail-open
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            return self.state()
        self._render()
        self._thread = threading.Thread(target=self._loop,
                                        name="wb-overlay", daemon=True)
        self._thread.start()
        return self.state()

    def stop(self) -> dict:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.5)
        self._thread = None
        with self._lock:
            overlay, self._overlay = self._overlay, None
        if overlay is not None:
            try:
                overlay.close()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            return self.state()

    # ── 更新 ────────────────────────────────────────────
    def update(self, *, groups: list[str] | None = None,
               selected: str | None = None,
               drafts: dict | None = None,
               profile=None, spec: str | None = None,
               scale: float | None = None) -> dict:
        with self._lock:
            if groups is not None:
                self._groups = list(groups)
            if selected is not None:
                self._selected = selected or None
            if drafts is not None:
                self._drafts = dict(drafts)
            if profile is not None:
                self._profile = profile
            if spec is not None:
                self._spec = spec
            if scale is not None:
                self._scale = float(scale)
            self._version += 1
        self._render()
        return self.state()

    def _render(self) -> None:
        with self._lock:
            profile = self._profile
            window = self._window
            overlay = self._overlay
            groups = list(self._groups)
            selected = self._selected
            drafts = dict(self._drafts)
            scale = self._scale
        if profile is None or window is None or overlay is None:
            return
        try:
            rgba = render_overlay(profile, window, scale, groups=groups,
                                  selected=selected, drafts=drafts)
            if not overlay.show(rgba):
                with self._lock:
                    self._error = "浮层更新失败（进程可能已退出）"
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        self._last_render = time.monotonic()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            overlay = self._overlay
            if overlay is None:
                return
            try:
                if not overlay.started:
                    with self._lock:
                        self._error = "浮层进程已退出"
                    return
                overlay.heartbeat()
                now = time.monotonic()
                if now - self._last_window_check >= 5.0:
                    self._last_window_check = now
                    self._follow_window()
                self._render()
            except Exception as exc:  # noqa: BLE001 — 浮层线程不能死循环抛
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"

    def _follow_window(self) -> None:
        """窗口移动/缩放后重建浮层，避免框错位。"""
        with self._lock:
            spec = self._spec
            current = self._window
        window, _ = self._resolve_window(spec)
        if window is None or current is None:
            return
        if (abs(window.x - current.x) < 0.5 and abs(window.y - current.y) < 0.5
                and abs(window.w - current.w) < 0.5
                and abs(window.h - current.h) < 0.5):
            return
        with self._lock:
            old = self._overlay
            self._overlay = None
            self._window = window
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            overlay = self._factory(window, self._scale, log=self._log)
            if overlay.start():
                with self._lock:
                    self._overlay = overlay
                self._render()
        except Exception:  # noqa: BLE001
            pass

    # ── 内部 ────────────────────────────────────────────
    def _resolve_window(self, spec: str) -> tuple[WindowRect | None, str | None]:
        if not spec:
            return None, "工作区没有窗口源（浮层需要 window: 源）"
        info = None
        try:
            info = self._window_lookup(spec)
        except Exception:  # noqa: BLE001
            info = None
        if not info:
            return None, f"解析不到窗口：{spec}（浮层需要窗口源）"
        try:
            rect = WindowRect(float(info["x"]), float(info["y"]),
                              float(info["w"]), float(info["h"]))
        except (KeyError, TypeError, ValueError) as exc:
            return None, f"窗口矩形不合法：{exc}"
        return rect, None

    @staticmethod
    def _scale_from(window: WindowRect, profile) -> float:
        try:
            ref_w = float(profile.reference_size[0])
            if window.w > 0:
                return max(1.0, round(ref_w / window.w, 3))
        except Exception:  # noqa: BLE001
            pass
        return 1.0
