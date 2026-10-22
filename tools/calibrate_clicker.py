#!/usr/bin/env python3
"""全屏拖框标定自动点击的虚拟按钮。

在客户端窗口上方铺一层半透明黑色窗口，依次提示每个按钮，用鼠标拖出矩形：

    Enter       确认当前外接框并进入下一个
    R           重画当前按钮
    S           跳过当前按钮（已有配置则保留）
    C           切换当前按钮为圆形 / 方形（默认圆形）
    Backspace   返回上一个按钮
    Esc         退出不保存

最后在汇总页按 Enter 保存到 `config/clicker_local.json`。

    .venv/bin/python tools/calibrate_clicker.py --list-displays
    .venv/bin/python tools/calibrate_clicker.py --out config/clicker_local.json

坐标是 macOS **逻辑点**（全局 top-left 原点），不是截图物理像素；窗口移动、
改分辨率/缩放、改显示器排列后都要重新标定。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.clicker import (  # noqa: E402
    BUTTON_IDS, BUTTON_LABELS, DEFAULT_CONFIG_PATH, CGEventBackend,
    ClickerConfig, ClickerConfigError, Rect, list_displays,
)

MIN_SIZE = 8.0          # 小于这个宽/高的框没有意义


def _geometry(w: float, h: float, x: float, y: float) -> str:
    """Tk geometry 字符串；负坐标要用 `-100` 而不是 `+-100`。"""
    xs = f"+{int(round(x))}" if x >= 0 else f"-{abs(int(round(x)))}"
    ys = f"+{int(round(y))}" if y >= 0 else f"-{abs(int(round(y)))}"
    return f"{int(round(w))}x{int(round(h))}{xs}{ys}"


def _print_displays(displays) -> None:
    if not displays:
        print("没有枚举到活动显示器（可能不是 macOS）")
        return
    for d in displays:
        mark = "  ← 主屏" if d.is_main else ""
        scale = f"，scale={d.scale:g}x" if d.scale else ""
        print(f"[{d.index}] id={d.display_id} "
              f"origin=({d.x:g}, {d.y:g}) size={d.w:g}x{d.h:g}"
              f"{scale}{mark}")


class Calibrator:
    def __init__(self, root, display, out: Path, *,
                 initial: dict[str, Rect] | None = None,
                 base: ClickerConfig | None = None,
                 inset_ratio: float = 0.15,
                 default_shape: str = "circle",
                 alpha: float = 0.35) -> None:
        import tkinter as tk

        self.tk = tk
        self.root = root
        self.display = display
        self.out = out
        self.base = base
        self.inset_ratio = inset_ratio
        self.default_shape = default_shape
        self.rects: dict[str, Rect] = dict(initial or {})
        # 每个按钮画成圆还是方。旧配置的 shape 保留；没有的用 --shape 默认值。
        self.shapes: dict[str, str] = {
            bid: self.rects[bid].shape if bid in self.rects else default_shape
            for bid in BUTTON_IDS
        }
        self.index = 0
        self.phase = "draw"           # draw | summary
        self.drag_start: tuple[float, float] | None = None
        self.preview_id: int | None = None

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", alpha)
        root.geometry(_geometry(display.w, display.h, display.x, display.y))
        root.configure(bg="black")
        root.lift()
        root.focus_force()
        self.canvas = tk.Canvas(root, bg="black", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self._build_toolbar()

        root.bind("<Return>", self._on_enter)
        root.bind("<KP_Enter>", self._on_enter)
        root.bind("<Escape>", self._on_cancel)
        root.bind("<BackSpace>", self._on_back)
        root.bind("<KeyPress-r>", self._on_redo)
        root.bind("<KeyPress-R>", self._on_redo)
        root.bind("<KeyPress-s>", self._on_skip)
        root.bind("<KeyPress-S>", self._on_skip)
        root.bind("<KeyPress-c>", self._on_toggle_shape)
        root.bind("<KeyPress-C>", self._on_toggle_shape)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        # update() 才会真正 map 窗口；只 update_idletasks() 时 winfo_rootx/y
        # 可能还是 0，几何校验会误报。
        root.update()
        # macOS 会把无边框窗口限制在菜单栏/Dock 之间的可见区域，实际窗口
        # 可能比显示器小、原点也不在 (0,0)。坐标换算必须用**实际窗口原点**，
        # 否则拖出来的框会整体偏移。
        self.origin_x = root.winfo_rootx()
        self.origin_y = root.winfo_rooty()
        self.w = root.winfo_width()
        self.h = root.winfo_height()
        self._check_geometry()
        self.redraw()

    def _build_toolbar(self) -> None:
        """窗口右上角的鼠标按钮：万一无边框窗口拿不到键盘焦点也能操作。"""
        tk = self.tk
        self.toolbar = tk.Frame(self.root, bg="#111111")
        self.toolbar.place(relx=1.0, y=10, anchor="ne", x=-16)
        self.btn_ok = tk.Button(self.toolbar, text="确认 (Enter)",
                                command=self._on_enter)
        self.btn_redo = tk.Button(self.toolbar, text="重画 (R)",
                                  command=self._on_redo)
        self.btn_skip = tk.Button(self.toolbar, text="跳过 (S)",
                                  command=self._on_skip)
        self.btn_shape = tk.Button(self.toolbar, text="圆形 (C)",
                                   command=self._on_toggle_shape)
        for btn in (self.btn_ok, self.btn_redo, self.btn_skip, self.btn_shape):
            btn.pack(side="left", padx=3)

    def _sync_toolbar(self) -> None:
        if self.phase == "summary":
            self.btn_ok.configure(text="保存 (Enter)")
            self.btn_redo.pack_forget()
            self.btn_skip.pack_forget()
            self.btn_shape.pack_forget()
            return
        self.btn_ok.configure(text="确认 (Enter)")
        shape = "圆形" if self.shapes.get(BUTTON_IDS[self.index]) == "circle" \
            else "方形"
        self.btn_shape.configure(text=f"{shape} (C)")
        for btn in (self.btn_redo, self.btn_skip, self.btn_shape):
            if not btn.winfo_manager():
                btn.pack(side="left", padx=3)

    # ── 坐标 ──────────────────────────────────────────────

    def _check_geometry(self) -> None:
        """确认窗口落在目标显示器内、尺寸足够用；不要求铺满整块屏幕。

        macOS 会把无边框窗口限制在菜单栏/Dock 之间的可见区域，所以实际
        origin/size 与显示器几何不同是正常的。只要原点在目标显示器内、
        窗口有足够的拖框面积即可；真正的坐标换算用实际窗口原点。
        """
        d = self.display
        if self.w < 200 or self.h < 200:
            raise SystemExit(
                f"标定窗口太小（{self.w}x{self.h}），无法拖框。"
                "请检查显示器缩放或改用 --display 0。")
        if not (d.x - 2 <= self.origin_x <= d.x + d.w + 2
                and d.y - 2 <= self.origin_y <= d.y + d.h + 2):
            raise SystemExit(
                "标定窗口没有落在目标显示器上："
                f"显示器 origin=({d.x:g},{d.y:g}) size={d.w:g}x{d.h:g}，"
                f"实际 origin=({self.origin_x},{self.origin_y})。\n"
                "请用 --list-displays 查看显示器索引，或改用主屏（--display 0）。")
        if (self.w < d.w - 4 or self.h < d.h - 4):
            print(f"提示：标定窗口 {self.w}x{self.h} 小于显示器 "
                  f"{d.w:g}x{d.h:g}（菜单栏/Dock 占位属正常）。"
                  "请确保客户端按钮都在这个窗口范围内。")

    def _to_global(self, x1: float, y1: float, x2: float, y2: float) -> Rect:
        gx1, gx2 = sorted((self.origin_x + x1, self.origin_x + x2))
        gy1, gy2 = sorted((self.origin_y + y1, self.origin_y + y2))
        return Rect(gx1, gy1, gx2 - gx1, gy2 - gy1)

    def _to_local(self, rect: Rect) -> tuple[float, float, float, float]:
        x1 = rect.x - self.origin_x
        y1 = rect.y - self.origin_y
        return x1, y1, x1 + rect.w, y1 + rect.h

    # ── 绘制 ──────────────────────────────────────────────

    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._sync_toolbar()
        w, h = self.w, self.h
        if self.phase == "summary":
            self._draw_summary(w, h)
            return

        button = BUTTON_IDS[self.index]
        label = BUTTON_LABELS[button]
        shape = "圆形" if self.shapes.get(button) == "circle" else "方形"
        c.create_text(
            24, 20, anchor="nw", fill="#ffe066",
            font=("Helvetica", 22, "bold"),
            text=f"[{self.index + 1}/{len(BUTTON_IDS)}] 拖框标定：{label}"
                 f"（当前 {shape}）")
        c.create_text(
            24, 58, anchor="nw", fill="#ffffff",
            font=("Helvetica", 14),
            text=("按住鼠标左键拖出按钮的外接框。"
                  "Enter 确认 / R 重画 / S 跳过 / C 切换圆形方形 / "
                  "Backspace 上一个 / Esc 退出"))

        # 已标定的其他按钮：细灰框 + 名字；当前按钮：亮绿框
        for bid, rect in self.rects.items():
            x1, y1, x2, y2 = self._to_local(rect)
            current = bid == button
            color = "#51cf66" if current else "#868e96"
            width = 3 if current else 1
            if self.shapes.get(bid) == "circle":
                c.create_oval(x1, y1, x2, y2, outline=color, width=width)
            else:
                c.create_rectangle(x1, y1, x2, y2, outline=color, width=width)
            c.create_text(x1 + 4, y1 + 4, anchor="nw", fill=color,
                          font=("Helvetica", 12),
                          text=BUTTON_LABELS.get(bid, bid))

    def _draw_summary(self, w: float, h: float) -> None:
        c = self.canvas
        c.create_text(24, 20, anchor="nw", fill="#ffe066",
                      font=("Helvetica", 22, "bold"),
                      text="标定汇总 — Enter 保存 / Backspace 返回修改 / Esc 取消")
        y = 70
        for bid in BUTTON_IDS:
            rect = self.rects.get(bid)
            if rect is None:
                text = f"{BUTTON_LABELS[bid]}：未标定"
                color = "#ff6b6b" if bid in ("fold", "call", "confirm") \
                    else "#adb5bd"
            else:
                shape = "圆形" if self.shapes.get(bid) == "circle" else "方形"
                text = (f"{BUTTON_LABELS[bid]}："
                        f"({rect.x:.0f}, {rect.y:.0f}) "
                        f"{rect.w:.0f}x{rect.h:.0f}  {shape}")
                color = "#ffffff"
            c.create_text(36, y, anchor="nw", fill=color,
                          font=("Helvetica", 14), text=text)
            y += 28
        missing = [b for b in ("fold", "call", "confirm")
                   if b not in self.rects]
        if missing:
            c.create_text(
                36, y + 12, anchor="nw", fill="#ff6b6b",
                font=("Helvetica", 15, "bold"),
                text="缺少必需按钮：" + "、".join(BUTTON_LABELS[b] for b in missing))

    # ── 鼠标 ──────────────────────────────────────────────

    def _on_press(self, event) -> None:
        if self.phase != "draw":
            return
        self.drag_start = (event.x, event.y)
        if self.preview_id is not None:
            self.canvas.delete(self.preview_id)
            self.preview_id = None

    def _on_motion(self, event) -> None:
        if self.phase != "draw" or self.drag_start is None:
            return
        x1, y1 = self.drag_start
        if self.preview_id is not None:
            self.canvas.delete(self.preview_id)
        if self.shapes.get(BUTTON_IDS[self.index]) == "circle":
            self.preview_id = self.canvas.create_oval(
                x1, y1, event.x, event.y, outline="#ffd43b", width=3)
        else:
            self.preview_id = self.canvas.create_rectangle(
                x1, y1, event.x, event.y, outline="#ffd43b", width=3)

    def _on_release(self, event) -> None:
        if self.phase != "draw" or self.drag_start is None:
            return
        x1, y1 = self.drag_start
        self.drag_start = None
        if self.preview_id is not None:
            self.canvas.delete(self.preview_id)
            self.preview_id = None
        rect = self._to_global(x1, y1, event.x, event.y)
        if rect.w < MIN_SIZE or rect.h < MIN_SIZE:
            self._flash("框太小了，请重新拖")
            return
        self.rects[BUTTON_IDS[self.index]] = rect
        self.redraw()

    # ── 键盘 ──────────────────────────────────────────────

    def _flash(self, text: str) -> None:
        item = self.canvas.create_text(
            self.w / 2, self.h - 60, fill="#ff6b6b",
            font=("Helvetica", 18, "bold"), text=text)
        self.root.after(1200, lambda: self.canvas.delete(item))

    def _on_enter(self, _event=None) -> None:
        if self.phase == "summary":
            self._save()
            return
        button = BUTTON_IDS[self.index]
        if button not in self.rects:
            self._flash("先拖一个框，或按 S 跳过")
            return
        self._advance()

    def _advance(self) -> None:
        if self.index + 1 >= len(BUTTON_IDS):
            self.phase = "summary"
        else:
            self.index += 1
        self.redraw()

    def _on_back(self, _event=None) -> None:
        if self.phase == "summary":
            self.phase = "draw"
            self.index = len(BUTTON_IDS) - 1
        elif self.index > 0:
            self.index -= 1
        self.redraw()

    def _on_redo(self, _event=None) -> None:
        if self.phase != "draw":
            return
        self.rects.pop(BUTTON_IDS[self.index], None)
        self.redraw()

    def _on_toggle_shape(self, _event=None) -> None:
        if self.phase != "draw":
            return
        bid = BUTTON_IDS[self.index]
        self.shapes[bid] = "rect" if self.shapes.get(bid) == "circle" else "circle"
        self.redraw()

    def _on_skip(self, _event=None) -> None:
        if self.phase != "draw":
            return
        self._advance()

    def _on_cancel(self, _event=None) -> None:
        print("已取消，未写入配置。")
        self.root.destroy()

    def _save(self) -> None:
        missing = [b for b in ("fold", "call", "confirm")
                   if b not in self.rects]
        if missing:
            self._flash("缺少必需按钮，Backspace 返回补标")
            return
        # 重标时保留原有非几何设置（延时、重试、allow_fallback 等）
        base = self.base or ClickerConfig()
        buttons = {}
        for bid in BUTTON_IDS:
            rect = self.rects.get(bid)
            if rect is None:
                continue
            buttons[bid] = Rect(rect.x, rect.y, rect.w, rect.h,
                                self.shapes.get(bid, rect.shape))
        cfg = ClickerConfig(
            buttons=buttons,
            inset_ratio=self.inset_ratio,
            click_delay_ms=base.click_delay_ms,
            confirm_gap_ms=base.confirm_gap_ms,
            allow_fallback=base.allow_fallback,
            retry_delay_ms=base.retry_delay_ms,
            retry_count=base.retry_count,
            display=self.display.to_dict(),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        try:
            cfg.validate()
        except ClickerConfigError as exc:
            self._flash(str(exc))
            return
        path = cfg.save(self.out)
        print(f"✅ 已保存 {path}")
        for bid, rect in cfg.buttons.items():
            shape = "圆形" if rect.shape == "circle" else "方形"
            print(f"   {BUTTON_LABELS[bid]}：({rect.x:.0f}, {rect.y:.0f}) "
                  f"{rect.w:.0f}x{rect.h:.0f}  {shape}")
        print("移动/缩放客户端窗口或改显示器排列后，需要重新标定。")
        self.root.destroy()


def _test_click(cfg: ClickerConfig) -> int:
    """逐个点击已配置按钮的中心，用来确认坐标。会操作真实 UI。"""
    backend = CGEventBackend()
    if not backend.preflight():
        print("❌ 没有「辅助功能」权限，无法发送鼠标事件。")
        print("   系统设置 → 隐私与安全性 → 辅助功能，勾上运行 Python 的 "
              "终端 / iTerm / IDE，然后完全退出并重开它。")
        print("   也可以先运行 `--request-access` 让系统弹授权框。")
        return 2
    print("⚠️  即将在真实屏幕上逐个点击这些按钮，2 秒后开始。")
    print("   确认当前前台窗口是模拟桌，且不要是真钱桌。Ctrl-C 可中断。")
    time.sleep(2)
    for bid in BUTTON_IDS:
        rect = cfg.buttons.get(bid)
        if rect is None:
            continue
        print(f"   点击 {BUTTON_LABELS[bid]}：({rect.cx:.0f}, {rect.cy:.0f})")
        backend.move(rect.cx, rect.cy)
        backend.mouse_down(rect.cx, rect.cy)
        time.sleep(0.07)
        backend.mouse_up(rect.cx, rect.cy)
        time.sleep(0.8)
    print("✅ 测试点击完成。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="拖框标定桌面自动点击按钮")
    ap.add_argument("--out", type=Path, default=DEFAULT_CONFIG_PATH,
                    help=f"输出 JSON（默认 {DEFAULT_CONFIG_PATH}）")
    ap.add_argument("--from", dest="from_path", type=Path, default=None,
                    help="预载已有配置，在其基础上重标")
    ap.add_argument("--display", type=int, default=0,
                    help="显示器索引（默认 0；用 --list-displays 查看）")
    ap.add_argument("--inset", type=float, default=None,
                    help="随机点向内缩的比例（默认沿用已有配置，否则 0.15）")
    ap.add_argument("--shape", choices=("circle", "rect"), default="circle",
                    help="新标定按钮的默认形状（默认 circle；标定时按 C 可逐个切换）")
    ap.add_argument("--alpha", type=float, default=0.35,
                    help="遮罩透明度（默认 0.35，越小越能看清下面）")
    ap.add_argument("--list-displays", action="store_true",
                    help="列出显示器几何后退出")
    ap.add_argument("--request-access", action="store_true",
                    help="请求 macOS「辅助功能」权限后退出")
    ap.add_argument("--test-click", action="store_true",
                    help="加载配置并逐个点击按钮中心（真实鼠标事件）")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    displays = list_displays()
    if args.list_displays:
        _print_displays(displays)
        return 0
    if args.request_access:
        try:
            ok = CGEventBackend.request_access()
        except Exception as exc:  # noqa: BLE001
            print(f"请求权限失败：{type(exc).__name__}: {exc}")
            return 2
        print("已请求辅助功能权限：" + ("已授权" if ok else "未授权"))
        print("若系统没有弹窗，去 系统设置 → 隐私与安全性 → 辅助功能 手动勾选，"
              "然后完全退出并重开运行 Python 的终端/IDE。")
        return 0

    if args.test_click:
        path = args.from_path or args.out
        if not path.exists():
            print(f"❌ 找不到配置：{path}")
            return 2
        try:
            cfg = ClickerConfig.load(path)
            cfg.validate()
        except ClickerConfigError as exc:
            print(f"❌ {exc}")
            return 2
        return _test_click(cfg)

    if not displays:
        print("❌ 没有枚举到活动显示器（这个工具目前只支持 macOS）")
        return 2
    if not (0 <= args.display < len(displays)):
        print(f"❌ --display {args.display} 超出范围（共 {len(displays)} 个显示器）")
        _print_displays(displays)
        return 2
    display = displays[args.display]

    base = None
    initial = None
    if args.from_path is not None:
        try:
            base = ClickerConfig.load(args.from_path)
        except ClickerConfigError as exc:
            print(f"❌ {exc}")
            return 2
        initial = base.buttons
        print(f"已预载 {args.from_path} 的 {len(initial)} 个按钮")
    inset = (args.inset if args.inset is not None
             else (base.inset_ratio if base is not None else 0.15))

    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 无法创建 Tk 窗口：{type(exc).__name__}: {exc}")
        print("   macOS 自带的 python 通常带 tkinter；如果用的是精简 Python，"
              "请换用带 Tk 的解释器。")
        return 2

    try:
        Calibrator(root, display, args.out, initial=initial, base=base,
                   inset_ratio=inset, default_shape=args.shape,
                   alpha=args.alpha)
    except SystemExit as exc:
        print(f"❌ {exc}")
        root.destroy()
        return 2
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
