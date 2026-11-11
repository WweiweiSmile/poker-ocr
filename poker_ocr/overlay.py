"""屏幕浮层：把一张带 alpha 的 PNG 盖在游戏窗口上（置顶、点击穿透）。

实现用 `osascript -l JavaScript` 直接调 AppKit，不引入 pyobjc：

  · 无边框、透明、`ignoresMouseEvents` → 浮层不挡鼠标，游戏照常点；
  · level=3（floating）+ canJoinAllSpaces → 盖在游戏窗口上、切 Space 也跟着；
  · 只认一个 JSON 状态文件：版本号变了就重载 PNG；Python 每轮写一次心跳，
    JS 超过 15s 没心跳就自己退出 —— Python 被 kill 也不会留一块鬼影在屏幕上。

坐标系：`window` 是游戏窗口的**屏幕逻辑点**（左上原点，和 clicker 一致）；
浮层窗口挂在主显示器上，AppKit 用左下原点，所以内部做一次翻转。

失败一律 fail-open：没有 osascript / 不是 macOS / 窗口不可见 → `start()`
返回 False，调用方继续跑，不要因为浮层把主链路拖死。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from .bet_slider import WindowRect

STALE_SECONDS = 15.0        # 这么久没心跳，JS 自己退出
_OVERLAY_JS = r"""
ObjC.import('Cocoa');

function readText(path) {
  var s = $.NSString.stringWithContentsOfFileEncodingError(
      path, $.NSUTF8StringEncoding, null);
  if (!s || s.isNil()) return null;
  try { return s.js; } catch (e) { return null; }
}

function run(argv) {
  var png = argv[0], x = Number(argv[1]), top = Number(argv[2]);
  var w = Number(argv[3]), h = Number(argv[4]);
  var state = argv[5], quit = argv[6];
  var app = $.NSApplication.sharedApplication;
  app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);

  // CG 坐标（左上原点）→ AppKit 坐标（左下原点，主屏高度翻转）
  var H = $.NSScreen.mainScreen.frame.size.height;
  var win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
      $.NSMakeRect(x, H - (top + h), w, h), 0, 2, false);
  win.setOpaque(false);
  win.setBackgroundColor($.NSColor.clearColor);
  win.setIgnoresMouseEvents(true);
  win.setHasShadow(false);
  win.setLevel(3);
  win.setReleasedWhenClosed(false);
  win.setCollectionBehavior(1);            // canJoinAllSpaces
  var iv = $.NSImageView.alloc.initWithFrame($.NSMakeRect(0, 0, w, h));
  iv.setImageScaling(1);                   // 铺满视图
  win.setContentView(iv);
  win.orderFront(null);

  var fm = $.NSFileManager.defaultManager;
  var version = -1;
  var deadline = Date.now() + 3600 * 1000;
  while (Date.now() < deadline) {
    if (fm.fileExistsAtPath(quit)) break;
    var txt = readText(state);
    if (txt) {
      var st = null;
      try { st = JSON.parse(txt); } catch (e) { st = null; }
      if (st) {
        if (st.version !== version) {
          var img = $.NSImage.alloc.initWithContentsOfFile(png);
          if (img && !img.isNil()) {
            iv.setImage(img);
            version = st.version;
          }
        }
        // 心跳：Python 每轮写一次 alive；太久没写说明它挂了
        if (st.alive && Date.now() / 1000 - st.alive > 15) break;
      }
    }
    $.NSRunLoop.currentRunLoop.runUntilDate(
        $.NSDate.dateWithTimeIntervalSinceNow(0.2));
  }
  win.orderOut(null);
  return "ok";
}
"""


class ScreenOverlay:
    """把带标注的 PNG 盖在 `window` 上；用完 `close()`。"""

    def __init__(self, window: WindowRect, scale: float, *,
                 log: Callable[[str], None] | None = None,
                 stale_seconds: float = STALE_SECONDS) -> None:
        if window.w <= 0 or window.h <= 0:
            raise ValueError(f"浮层窗口宽高必须 > 0：{window}")
        self.window = window
        self.scale = max(0.5, float(scale))
        self.stale_seconds = float(stale_seconds)
        self._log = log or (lambda _m: None)
        self._tmp: Path | None = None
        self._proc: subprocess.Popen | None = None
        self._version = 0
        self._png: Path | None = None
        self._state: Path | None = None
        self._quit: Path | None = None
        self._err: Path | None = None
        self._alive = 0.0

    # ── 生命周期 ──────────────────────────────────────────

    @property
    def started(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        if self.started:
            return True
        if sys.platform != "darwin":
            self._log("[标注] 浮层只支持 macOS，已跳过")
            return False
        osascript = shutil.which("osascript")
        if not osascript:
            self._log("[标注] 找不到 osascript，浮层已跳过")
            return False
        try:
            self._tmp = Path(tempfile.mkdtemp(prefix="poker_ocr_overlay_"))
            js = self._tmp / "overlay.js"
            js.write_text(_OVERLAY_JS, encoding="utf-8")
            self._png = self._tmp / "overlay.png"
            self._state = self._tmp / "state.json"
            self._quit = self._tmp / "quit"
            self._err = self._tmp / "stderr.log"
            Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(self._png)
            self._write_state()
            err = open(self._err, "wb")
            try:
                self._proc = subprocess.Popen(
                    [osascript, "-l", "JavaScript", str(js),
                     str(self._png), f"{self.window.x:g}",
                     f"{self.window.y:g}", f"{self.window.w:g}",
                     f"{self.window.h:g}", str(self._state), str(self._quit)],
                    stdout=subprocess.DEVNULL, stderr=err)
            finally:
                err.close()
        except (OSError, ValueError) as exc:
            self._log(f"[标注] 浮层启动失败：{type(exc).__name__}: {exc}")
            self._cleanup()
            return False
        # 等窗口真正挂上（osascript 起 AppKit 约 0.3–1s）
        for _ in range(20):
            time.sleep(0.05)
            if self._proc.poll() is not None:
                self._log("[标注] 浮层进程提前退出（osascript/AppKit 不可用）"
                          + self._stderr_tail())
                self._cleanup()
                return False
        self._log(f"[标注] 浮层已就位：window={self.window.to_dict()} "
                  f"scale={self.scale:g}")
        return True

    def close(self) -> None:
        if self._quit is not None:
            try:
                self._quit.write_text("1", encoding="utf-8")
            except OSError:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        self._cleanup()

    def _stderr_tail(self, limit: int = 400) -> str:
        if self._err is None or not self._err.is_file():
            return ""
        try:
            text = self._err.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return f"：{text[-limit:]}" if text else ""

    def _cleanup(self) -> None:
        self._proc = None
        if self._tmp is not None:
            try:
                shutil.rmtree(self._tmp, ignore_errors=True)
            except OSError:
                pass
        self._tmp = None

    # ── 更新 ──────────────────────────────────────────────

    def show(self, rgba: np.ndarray) -> bool:
        """换一张标注图。`rgba` 尺寸应当是 window × scale。"""
        if not self.started or self._png is None:
            if self._proc is not None and self._proc.poll() is not None:
                self._log(f"[标注] 浮层进程已退出（code={self._proc.returncode}）"
                          + self._stderr_tail())
                self._cleanup()
            return False
        if rgba is None or getattr(rgba, "size", 0) == 0:
            return False
        try:
            tmp = self._png.with_suffix(".tmp.png")
            Image.fromarray(rgba, "RGBA").save(tmp)
            os.replace(tmp, self._png)
            self._write_state(bump=True)
            return True
        except (OSError, ValueError) as exc:
            self._log(f"[标注] 浮层更新失败：{type(exc).__name__}: {exc}")
            return False

    def heartbeat(self) -> None:
        """只刷新心跳，不重载图（图没变时用）。"""
        if self.started:
            self._write_state(bump=False)

    def _write_state(self, *, bump: bool = False) -> None:
        if self._state is None:
            return
        if bump:
            self._version += 1
        self._alive = time.time()
        payload = {"version": self._version, "alive": self._alive}
        tmp = self._state.with_suffix(".tmp.json")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._state)
        except OSError:
            pass
