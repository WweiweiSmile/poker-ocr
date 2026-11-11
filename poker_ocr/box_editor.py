"""可拖拽的按钮框编辑器（Tk）。

标注模式加 `--edit` 进入：窗口铺在游戏窗口位置，背景是**游戏画面截图**，
每个可点区域一个框 —— 拖框体移动、拖四角/四边改大小，`S` 存回 profile 的
`global_slots`，`R` 重截背景（按钮出现/消失后刷新一下）。改完重启 live 生效。

为什么不做「贴着游戏窗口的透明浮层」：macOS 26 + Tk 9 会把无边框窗口加回
标题栏并下移（`overrideredirect` / `MacWindowStyle plain` 都去不掉），
所以这里做成一个普通置顶窗口显示截图，拖框坐标仍然是**窗口内逻辑点**，
和真实点击同一口径 —— 保存出来的槽位是准的。

这里同时放两层：
  · 纯几何（槽位 ↔ 画布、命中测试、拖动、保存）：Tk 之外的函数，单测直接跑；
  · `BoxEditor`：Tk 应用，`tkinter` 只在实例化时 import（无显示环境也能 import 本模块）。

坐标系：画布 = **窗口内逻辑点**（0..window.w / 0..window.h，左上原点）；
profile 槽位 = 整帧归一化（带 canvas 时相对 canvas）。两者靠
`slot_to_canvas` / `canvas_to_slot` 互转，和 `WindowMapper` 同一口径。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Mapping

import numpy as np

from .bet_slider import WindowMapper, WindowRect

# 画布上可以拖的按钮/滑杆区域（key = profile.global_slots 的键）
EDITABLE_SLOTS: tuple[tuple[str, str, str], ...] = (
    # (slot 键, 显示名, 形状) —— 形状和真实按钮一致：圆钮 disc、长条/气泡 rect
    ("hero_fold_button", "弃牌", "disc"),
    ("hero_check_button", "免费/让牌", "disc"),
    ("hero_raise_button", "主钮(加注/下注)", "disc"),
    ("hero_call_button", "跟注/让牌", "disc"),
    ("hero_allin_button", "全下", "disc"),
    ("bet_preset_1_3", "档 1/3", "disc"),
    ("bet_preset_1_2", "档 1/2", "disc"),
    ("bet_preset_2_3", "档 2/3", "disc"),
    ("bet_preset_1", "档 1", "disc"),
    ("bet_preset_1_2x", "档 1.2", "disc"),
    ("hero_bet_slider", "滑杆轨道", "rect"),
    ("hero_bet_amount", "滑杆金额", "rect"),
    ("hero_bet_confirm", "滑杆确定", "disc"),
    ("hero_bet_close", "滑杆关闭", "rect"),
)

HANDLE_PX = 5.0
MIN_SIZE = 6.0


# ─────────────────────────── 纯几何 ───────────────────────────

def slot_to_canvas(slot: tuple[float, float, float, float],
                   window: WindowRect,
                   canvas: tuple[float, float, float, float] | None = None
                   ) -> tuple[float, float, float, float]:
    """profile 槽位（相对 canvas 的 0-1）→ 窗口内逻辑点。"""
    x1, y1, x2, y2 = (float(v) for v in slot)
    if canvas is not None:
        cx1, cy1, cx2, cy2 = (float(v) for v in canvas)
        x1, x2 = cx1 + x1 * (cx2 - cx1), cx1 + x2 * (cx2 - cx1)
        y1, y2 = cy1 + y1 * (cy2 - cy1), cy1 + y2 * (cy2 - cy1)
    return (x1 * window.w, y1 * window.h, x2 * window.w, y2 * window.h)


def canvas_to_slot(rect: tuple[float, float, float, float],
                   window: WindowRect,
                   canvas: tuple[float, float, float, float] | None = None
                   ) -> tuple[float, float, float, float]:
    """窗口内逻辑点 → profile 槽位（`slot_to_canvas` 的逆）。"""
    if window.w <= 0 or window.h <= 0:
        raise ValueError(f"窗口宽高必须 > 0：{window}")
    x1, y1, x2, y2 = (float(v) for v in rect)
    x1, x2 = x1 / window.w, x2 / window.w
    y1, y2 = y1 / window.h, y2 / window.h
    if canvas is not None:
        cx1, cy1, cx2, cy2 = (float(v) for v in canvas)
        if cx2 <= cx1 or cy2 <= cy1:
            raise ValueError(f"canvas 不合法：{canvas}")
        x1, x2 = (x1 - cx1) / (cx2 - cx1), (x2 - cx1) / (cx2 - cx1)
        y1, y2 = (y1 - cy1) / (cy2 - cy1), (y2 - cy1) / (cy2 - cy1)
    return (x1, y1, x2, y2)


def handle_points(rect: tuple[float, float, float, float]
                  ) -> dict[str, tuple[float, float]]:
    x1, y1, x2, y2 = rect
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    return {"nw": (x1, y1), "n": (cx, y1), "ne": (x2, y1),
            "e": (x2, cy), "se": (x2, y2), "s": (cx, y2),
            "sw": (x1, y2), "w": (x1, cy)}


def hit_test(boxes: Mapping[str, tuple[float, float, float, float]],
             x: float, y: float, *, selected: str | None = None,
             handle_px: float = HANDLE_PX) -> tuple[str | None, str | None]:
    """点到谁：返回 `(框名, 把手)`；把手是 8 个方位或 `move`；没点到 → (None, None)。

    先测选中框的把手（小目标，优先），再测框体（后画的上层优先）。
    """
    if selected in boxes:
        for handle, (hx, hy) in handle_points(boxes[selected]).items():
            if abs(x - hx) <= handle_px and abs(y - hy) <= handle_px:
                return selected, handle
    for name in reversed(list(boxes)):
        x1, y1, x2, y2 = boxes[name]
        if x1 <= x <= x2 and y1 <= y <= y2:
            return name, "move"
    return None, None


def apply_drag(start: tuple[float, float, float, float], handle: str,
               dx: float, dy: float, bounds: tuple[float, float],
               *, min_size: float = MIN_SIZE
               ) -> tuple[float, float, float, float]:
    """从 `start` 按把手拖 `(dx, dy)` 后的新矩形（夹在窗口内、保住最小尺寸）。"""
    x1, y1, x2, y2 = start
    bw, bh = bounds
    w, h = x2 - x1, y2 - y1
    if handle == "move":
        nx1 = min(max(0.0, x1 + dx), max(0.0, bw - w))
        ny1 = min(max(0.0, y1 + dy), max(0.0, bh - h))
        return (nx1, ny1, nx1 + w, ny1 + h)
    if "w" in handle:
        x1 = min(max(0.0, x1 + dx), x2 - min_size)
    if "e" in handle:
        x2 = max(min(bw, x2 + dx), x1 + min_size)
    if "n" in handle:
        y1 = min(max(0.0, y1 + dy), y2 - min_size)
    if "s" in handle:
        y2 = max(min(bh, y2 + dy), y1 + min_size)
    return (x1, y1, x2, y2)


def save_slots(path: Path, raw: dict,
               updates: Mapping[str, tuple[float, float, float, float]],
               *, log: Callable[[str], None] | None = None) -> list[str]:
    """把槽位写回 profile JSON（先备份 .bak，原子替换）。返回改动的键。"""
    slots = raw.setdefault("global_slots", {})
    changed: list[str] = []
    for key, slot in updates.items():
        vals = [round(float(v), 6) for v in slot]
        x1, y1, x2, y2 = vals
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError(f"{key} 槽位不合法：{vals}")
        if [float(v) for v in slots.get(key, [])] != vals:
            slots[key] = vals
            changed.append(key)
    if not changed:
        return []
    path = Path(path)
    (path.parent / (path.name + ".bak")).write_text(
        path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    if log is not None:
        log(f"[编辑] 已写入 {path}（改 {len(changed)} 项，备份 "
            f"{path.name}.bak）")
    return changed


# ─────────────────────────── Tk 编辑器 ───────────────────────────

def _geometry(w: float, h: float, x: float, y: float) -> str:
    xs = f"+{int(round(x))}" if x >= 0 else f"-{abs(int(round(x)))}"
    ys = f"+{int(round(y))}" if y >= 0 else f"-{abs(int(round(y)))}"
    return f"{int(round(w))}x{int(round(h))}{xs}{ys}"


class BoxEditor:
    """显示游戏截图 + 可拖拽框的编辑窗口。

    `boxes` 的坐标是**窗口内逻辑点**；`bg0/hits0` 是窗口出现**之前**截好的
    背景和 OCR 命中（避免截到自己）；`reload_fn` 供 `R` 重新抓取。
    `reload_fn` 返回 `((BGR 图, 它对应的屏幕高度 pt), {动作: (x, y)})`，
    `None`/空表示这轮拿不到（比如摄像头断了）。
    """

    BOX_COLOR = "#00A2FF"
    SEL_COLOR = "#FFD60A"
    OCR_COLOR = "#34C759"

    def __init__(self, *, window: WindowRect, mapper: WindowMapper,
                 boxes: dict[str, tuple[float, float, float, float]],
                 labels: Mapping[str, str], profile_path: Path, raw: dict,
                 shapes: Mapping[str, str] | None = None,
                 bg0: tuple | None = None, hits0: Mapping | None = None,
                 reload_fn: Callable[[], tuple] | None = None,
                 hide_while_grab: bool = False,
                 log: Callable[[str], None] = print) -> None:
        import tkinter as tk
        from PIL import Image, ImageTk

        self.tk = tk
        self.Image = Image
        self.ImageTk = ImageTk
        self.window = window
        self.mapper = mapper
        self.canvas_rect = getattr(getattr(mapper, "profile", None),
                                   "canvas", None)
        self.boxes = dict(boxes)
        self.labels = dict(labels)
        self.shapes = dict(shapes or {})
        self.profile_path = Path(profile_path)
        self.raw = raw
        self.reload_fn = reload_fn
        self.hide_while_grab = bool(hide_while_grab)
        self.log = log

        self.selected: str | None = next(iter(self.boxes), None)
        self.drag: tuple[str, str, tuple, float, float] | None = None
        self.undo: list[dict] = []
        self.dirty = False
        self.esc_armed = False
        self.status = ""
        self._photo = None
        self._bg = None
        self._bg_src: tuple | None = None
        self._ocr_hits: dict = dict(hits0 or {})

        self.root = tk.Tk()
        self.root.title("点击标注 · 编辑")
        # 注意：别调 ::tk::unsupported::MacWindowStyle —— Tk 9 上它会让窗口
        # 停在 200x200 默认尺寸（geometry 失效），而且标题栏也不去掉。
        self.root.attributes("-topmost", True)
        self.root.geometry(_geometry(window.w, window.h, window.x, window.y))
        self.root.configure(bg="black")
        # 画布给显式宽高：否则窗口第一次 map 会按 Tk 默认画布尺寸缩成一小块
        self.canvas = tk.Canvas(self.root, bg="#101010",
                                highlightthickness=0, cursor="crosshair",
                                width=window.w, height=window.h)
        self.canvas.pack(fill="both", expand=True)
        self._build_overlay_widgets()

        self.root.bind("<Escape>", lambda _e: self._quit())
        for key in ("s", "S"):
            self.root.bind(f"<KeyPress-{key}>", lambda _e: self._save())
        for key in ("r", "R"):
            self.root.bind(f"<KeyPress-{key}>", lambda _e: self._reload())
        for key in ("z", "Z"):
            self.root.bind(f"<KeyPress-{key}>", lambda _e: self._undo())
        self.root.bind("<Tab>", lambda _e: self._cycle(1))
        self.root.bind("<Shift-Tab>", lambda _e: self._cycle(-1))
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0),
                            ("Up", 0, -1), ("Down", 0, 1)):
            self.root.bind(f"<KeyPress-{key}>",
                           lambda _e, dx=dx, dy=dy: self._nudge(dx, dy))
            self.root.bind(f"<Shift-KeyPress-{key}>",
                           lambda _e, dx=dx, dy=dy: self._nudge(dx * 10, dy * 10))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Configure>", self._on_configure)

        # update() 才真正 map；macOS 会把窗口夹进可见区，用实际画布尺寸换算
        self.root.update()
        self.cw = max(1, self.canvas.winfo_width())
        self.ch = max(1, self.canvas.winfo_height())
        self.sx = self.cw / window.w if window.w else 1.0
        self.sy = self.ch / window.h if window.h else 1.0
        if abs(self.sx - 1) > 0.01 or abs(self.sy - 1) > 0.01:
            self.log(f"[编辑] 画布被系统缩放：{self.cw}x{self.ch} "
                     f"(window {window.w:g}x{window.h:g})，已按比例换算")
        if bg0 is not None:
            self._set_bg(bg0)
        self.root.focus_force()
        self._draw()
        # 窗口第一次 map 时画布尺寸可能还没定下来（macOS 会先给个小尺寸），
        # 再对一次；之后靠 <Configure> 兜底。
        self.root.after(120, self._relayout)

    # ── 小控件 ──────────────────────────────────────────

    def _build_overlay_widgets(self) -> None:
        tk = self.tk
        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(
            self.root, textvariable=self.status_var, bg="#111111",
            fg="#FFFFFF", font=("Helvetica", 11), anchor="w", padx=8, pady=3)
        self.status_label.place(x=10, y=8)
        bar = tk.Frame(self.root, bg="#111111")
        bar.place(relx=1.0, x=-10, y=8, anchor="ne")
        for text, cmd in (("保存 S", self._save), ("重截 R", self._reload),
                          ("退出 Esc", self._quit)):
            tk.Button(bar, text=text, command=cmd, padx=6).pack(
                side="left", padx=2)

    # ── 背景 / 画 ────────────────────────────────────────

    def _set_bg(self, bg: tuple) -> None:
        self._bg_src = bg
        bgr, h_pt = bg
        if bgr is None or not getattr(bgr, "size", 0) or self.cw <= 1:
            return
        h_px = self.ch if h_pt is None else max(1, round(float(h_pt) * self.sy))
        h_px = min(h_px, self.ch)
        rgb = np.ascontiguousarray(bgr[:, :, ::-1])
        self._bg = self.Image.fromarray(rgb).resize((self.cw, h_px))

    def _relayout(self) -> None:
        """画布真正尺寸变了：重算缩放、重铺背景、重画。"""
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w <= 1 or h <= 1 or (w == self.cw and h == self.ch):
            return
        self.cw, self.ch = w, h
        self.sx = self.cw / self.window.w if self.window.w else 1.0
        self.sy = self.ch / self.window.h if self.window.h else 1.0
        if self._bg_src is not None:
            self._set_bg(self._bg_src)
        self._draw()

    def _on_configure(self, event) -> None:
        if getattr(self, "cw", None) is None:
            return
        if (event.width, event.height) == (self.cw, self.ch):
            return
        self._relayout()

    def _reload(self) -> None:
        """重截背景 + OCR（需要时先把自己的窗口藏起来，否则截到的是自己）。"""
        if self.reload_fn is None:
            return
        bg = hits = None
        try:
            if self.hide_while_grab:
                self.root.withdraw()
                self.root.update_idletasks()
            bg, hits = self.reload_fn()
        except Exception as exc:      # noqa: BLE001 — 重截失败不崩编辑器
            self.log(f"[编辑] 重截失败：{type(exc).__name__}: {exc}")
        finally:
            if self.hide_while_grab:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self.root.update_idletasks()
        if bg is not None:
            self._set_bg(bg)
        if hits is not None:
            self._ocr_hits = dict(hits)
        self._draw()

    def _to_canvas(self, rect: tuple[float, float, float, float]
                   ) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = rect
        return (x1 * self.sx, y1 * self.sy, x2 * self.sx, y2 * self.sy)

    def _to_model(self, x: float, y: float) -> tuple[float, float]:
        return (x / self.sx, y / self.sy)

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        if self._bg is not None:
            self._photo = self.ImageTk.PhotoImage(self._bg)
            c.create_image(0, 0, anchor="nw", image=self._photo)
        for name, rect in self.boxes.items():
            self._draw_box(name, rect)
        for action, (sx, sy) in self._ocr_hits.items():
            cx = (sx - self.window.x) * self.sx
            cy = (sy - self.window.y) * self.sy
            c.create_line(cx - 14, cy, cx + 14, cy, fill=self.OCR_COLOR, width=2)
            c.create_line(cx, cy - 14, cx, cy + 14, fill=self.OCR_COLOR, width=2)
            self._label(cx + 16, cy - 8, f"OCR {action}", self.OCR_COLOR)
        self._draw_status()

    def _draw_box(self, name: str, rect) -> None:
        c = self.canvas
        x1, y1, x2, y2 = self._to_canvas(rect)
        sel = name == self.selected
        color = self.SEL_COLOR if sel else self.BOX_COLOR
        width = 3 if sel else 2
        if self.shapes.get(name, "rect") in ("disc", "circle"):
            # 圆钮：画成**正圆**（半径 = 短边/2，和点击器 disc 采样一致）
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            r = min(x2 - x1, y2 - y1) / 2
            c.create_oval(cx - r, cy - r, cx + r, cy + r,
                          outline=color, width=width)
            if sel:
                # 选中时用虚线外接框 + 把手，方便拖角改大小
                c.create_rectangle(x1, y1, x2, y2, outline=color, width=1,
                                   dash=(2, 4))
        else:
            c.create_rectangle(x1, y1, x2, y2, outline=color, width=width,
                               dash=None if sel else (4, 3))
        # 标签放在框上面，别压住圆钮（压住就看不出形状了）
        label_y = y1 - 15 if y1 >= 20 else y1 + 2
        self._label(x1, label_y, self.labels.get(name, name), color)
        if sel:
            for hx, hy in handle_points((x1, y1, x2, y2)).values():
                r = HANDLE_PX
                c.create_rectangle(hx - r, hy - r, hx + r, hy + r,
                                   fill=color, outline="#000000")

    def _label(self, x: float, y: float, text: str, color: str) -> None:
        """文字 + 深色底（Tk 文字没有背景色，先创建再补矩形）。"""
        c = self.canvas
        tid = c.create_text(x, y, text=text, anchor="nw", fill=color,
                            font=("Helvetica", 10, "bold"))
        bbox = c.bbox(tid)
        if bbox:
            rid = c.create_rectangle(bbox[0] - 2, bbox[1] - 1,
                                     bbox[2] + 2, bbox[3] + 1,
                                     fill="#000000", outline="")
            c.tag_raise(tid, rid)

    def _draw_status(self) -> None:
        if self.dirty:
            self._set_status("有未保存改动（S 保存，Esc 两次退出）")
        elif self.selected:
            x1, y1, x2, y2 = self.boxes[self.selected]
            shape = self.shapes.get(self.selected, "rect")
            shape_txt = "圆钮(半径=短边/2)" if shape in ("disc", "circle") else "方框"
            self._set_status(
                f"{self.labels.get(self.selected, self.selected)}"
                f"〔{shape_txt}〕{x1:.0f},{y1:.0f} {x2 - x1:.0f}x{y2 - y1:.0f}"
                f" ｜ 拖框移动 / 拖角改大小 ｜ Tab 换框 ｜ ←→↑↓ 微调 ｜ R 重截")
        else:
            self._set_status("拖动框体移动，拖四角/四边改大小")

    def _set_status(self, text: str) -> None:
        if self.status != text:
            self.status = text
            self.status_var.set(text)

    # ── 交互 ────────────────────────────────────────────

    def _on_press(self, event) -> None:
        x, y = self._to_model(event.x, event.y)
        name, handle = hit_test(self.boxes, x, y, selected=self.selected)
        if name is None:
            return
        self.selected = name
        self.undo.append(dict(self.boxes))
        del self.undo[:-30]
        self.drag = (name, handle or "move", self.boxes[name], x, y)
        self._draw()

    def _on_motion(self, event) -> None:
        if self.drag is None:
            return
        name, handle, start, x0, y0 = self.drag
        x, y = self._to_model(event.x, event.y)
        self.boxes[name] = apply_drag(start, handle, x - x0, y - y0,
                                      (self.window.w, self.window.h))
        self.dirty = True
        self._draw()

    def _on_release(self, _event) -> None:
        if self.drag is None:
            return
        name = self.drag[0]
        self.drag = None
        x1, y1, x2, y2 = self.boxes[name]
        self.log(f"[编辑] {self.labels.get(name, name)} → "
                 f"{x1:.0f},{y1:.0f} {x2 - x1:.0f}x{y2 - y1:.0f}（未保存）")
        self._draw()

    def _cycle(self, step: int) -> None:
        names = list(self.boxes)
        if not names:
            return
        i = names.index(self.selected) if self.selected in names else 0
        self.selected = names[(i + step) % len(names)]
        self._draw()

    def _nudge(self, dx: float, dy: float) -> None:
        if self.selected is None:
            return
        self.undo.append(dict(self.boxes))
        del self.undo[:-30]
        self.boxes[self.selected] = apply_drag(
            self.boxes[self.selected], "move", dx, dy,
            (self.window.w, self.window.h))
        self.dirty = True
        self._draw()

    def _undo(self) -> None:
        if not self.undo:
            return
        self.boxes = self.undo.pop()
        self.dirty = True
        self._draw()

    def _save(self) -> None:
        updates = {key: canvas_to_slot(rect, self.window, self.canvas_rect)
                   for key, rect in self.boxes.items()}
        try:
            changed = save_slots(self.profile_path, self.raw, updates,
                                 log=self.log)
        except (OSError, ValueError) as exc:
            self.log(f"[编辑] 保存失败：{type(exc).__name__}: {exc}")
            self._set_status(f"保存失败：{exc}")
            return
        if changed:
            self.dirty = False
            self.log("[编辑] 改动：" + "、".join(changed)
                     + "（重启 live 生效）")
            self._set_status(f"已保存 {len(changed)} 项 → "
                             f"{self.profile_path.name}（重启 live 生效）")
        else:
            self._set_status("没有改动，不用保存")

    def _quit(self) -> None:
        if self.dirty and not self.esc_armed:
            self.esc_armed = True
            self._set_status("有未保存改动，再按一次 Esc 退出（或 S 保存）")
            return
        self.log("[编辑] 退出")
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0
