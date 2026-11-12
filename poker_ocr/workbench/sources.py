"""视频源：枚举窗口/摄像头、解析源规格、抓一帧做体检。

约束（见设计 §6.3）：

  · **只检测/连接，不启动 OBS/MediaMTX/浏览器**；
  · 抓帧一律「开 → 读 → 立即 release」，不在工作台长期占着摄像头；
    启动 live 前不需要额外交接，因为工作台从不跨请求持有源。
  · 所有函数都不抛异常给 HTTP 层：返回 `{ok, ...}` 或 `(值, 原因)`。
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
from typing import Any

import cv2
import numpy as np

from ..bet_slider import (WindowCapture, WindowRect, find_windows,
                          resolve_window_info)

VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


# ─────────────────────────── 源规格 ───────────────────────────

def parse_source_spec(spec: str) -> dict:
    """把 live 的 `--url` 写法解析成 {kind, valid, detail, raw}。"""
    raw = str(spec or "").strip()
    out = {"raw": raw, "kind": None, "valid": False, "detail": ""}
    if not raw:
        out["detail"] = "视频源为空"
        return out
    if raw.startswith(("window:", "screen:")):
        out["kind"] = "window"
        inner = raw.split(":", 1)[1].strip()
        if not inner:
            out["detail"] = "window: 后要跟 title:/index:/front/x,y,w,h"
            return out
        if inner.lower() == "front":
            out["valid"] = True
            return out
        if inner.lower().startswith(("id:", "index:", "title:", "owner:")):
            out["valid"] = bool(inner.split(":", 1)[1].strip())
            if not out["valid"]:
                out["detail"] = "窗口匹配串为空"
            return out
        parts = [p.strip() for p in inner.replace("，", ",").split(",")]
        if len(parts) == 4:
            try:
                WindowRect(*(float(v) for v in parts))
                out["valid"] = True
            except ValueError:
                out["detail"] = "窗口矩形必须是 x,y,w,h 四个正数（宽高 > 0）"
            return out
        # 裸子串 = owner/title 任意匹配（live 的 WindowCapture 支持）
        out["valid"] = True
        return out
    if raw.startswith(("cam:", "camera:")):
        out["kind"] = "camera"
        inner = raw.split(":", 1)[1].strip()
        if inner.lower() in ("", "auto") or inner.isdigit():
            out["valid"] = True
        else:
            out["detail"] = "摄像头写法只有 cam:auto 或 cam:N"
        return out
    if raw.startswith(("rtsp://", "rtsps://")):
        out["kind"] = "rtsp"
        out["valid"] = bool(raw.split("://", 1)[1])
        if not out["valid"]:
            out["detail"] = "RTSP 地址缺少主机名"
        return out
    if raw.startswith(("http://", "https://")):
        out["kind"] = "file"
        out["valid"] = True
        return out
    path = Path(raw).expanduser()
    if path.suffix.lower() in VIDEO_SUFFIXES + IMAGE_SUFFIXES:
        out["kind"] = "file"
        if path.is_file():
            out["valid"] = True
        else:
            out["detail"] = f"文件不存在：{raw}"
        return out
    out["detail"] = ("无法识别的源：用 window:title:... / cam:auto / rtsp://... / "
                     "或一个视频/图片文件路径")
    return out


def normalize_window_spec(spec) -> str:
    """把 `window:title:X` / `window:index:2` 归一成 live 的 `--bet-slider-window`
    写法（不带 `window:` 前缀）。空串返回 ""。"""
    text = str(spec or "").strip()
    if text.startswith(("window:", "screen:")):
        text = text.split(":", 1)[1].strip()
    return text


def as_window_spec(spec) -> str:
    """滑杆窗口字段 → `window:` 开头的源规格（给 window_matches 用）。"""
    inner = normalize_window_spec(spec)
    return f"window:{inner}" if inner else ""


def source_label(spec: str, kind: str | None = None) -> str:
    parsed = parse_source_spec(spec)
    kind = kind or parsed.get("kind") or "?"
    raw = str(spec or "")
    if kind == "window":
        return f"窗口 {raw.split(':', 1)[1] if ':' in raw else raw}"
    if kind == "camera":
        return f"摄像头 {raw.split(':', 1)[1] or 'auto'}"
    if kind == "rtsp":
        return "RTSP 流"
    if raw:
        return f"文件 {Path(raw).name}"
    return "未选择"


# ─────────────────────────── 窗口 ───────────────────────────

def list_windows_safe() -> tuple[list[dict], str | None]:
    try:
        from ..bet_slider import list_windows
        return [w.to_dict() for w in list_windows()], None
    except Exception as exc:  # noqa: BLE001 — 需要 swift/权限时给出可读原因
        return [], f"{type(exc).__name__}: {exc}"


def window_matches(spec: str) -> tuple[list[dict], str | None]:
    """按 `window:...` 的匹配串找窗口；返回 (命中列表, 错误)。"""
    parsed = parse_source_spec(spec)
    if parsed["kind"] != "window" or not parsed["valid"]:
        return [], parsed["detail"] or "不是窗口源"
    inner = str(spec).split(":", 1)[1].strip()
    if inner.lower() == "front":
        return [], "front 无法预先枚举"
    if len([p for p in inner.replace("，", ",").split(",")]) == 4:
        try:
            rect = WindowRect(*(float(p) for p in
                                inner.replace("，", ",").split(",")))
            return ([{"index": -1, "owner": "", "title": "", "window_id": 0,
                      **rect.to_dict()}], None)
        except ValueError as exc:
            return [], str(exc)
    try:
        if inner.lower().startswith(("id:", "index:")):
            info = resolve_window_info(inner)
            return ([info.to_dict()] if info is not None else []), None
        field = "any"
        query = inner
        if ":" in inner:
            prefix, rest = inner.split(":", 1)
            if prefix.lower() in ("title", "owner", "any"):
                field, query = prefix.lower(), rest
        wins = find_windows(query, field=field)
        return [w.to_dict() for w in wins], None
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


def window_info(spec: str) -> dict | None:
    """解析窗口源 → {x,y,w,h,window_id,...}；失败返回 None（fail-open）。"""
    parsed = parse_source_spec(spec)
    if parsed["kind"] != "window" or not parsed["valid"]:
        return None
    inner = str(spec).split(":", 1)[1].strip()
    try:
        if len([p for p in inner.replace("，", ",").split(",")]) == 4:
            rect = WindowRect(*(float(p) for p in
                                inner.replace("，", ",").split(",")))
            return {"index": -1, "owner": "", "title": "", "window_id": 0,
                    **rect.to_dict()}
        if inner.lower() == "front":
            return None
        info = resolve_window_info(inner)
        return info.to_dict() if info is not None else None
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────── 摄像头 ───────────────────────────

def list_cameras() -> tuple[list[dict], str | None]:
    """macOS 摄像头名称（按系统枚举顺序），并标出 OBS 虚拟摄像头。"""
    if sys.platform != "darwin":
        return [], "非 macOS，摄像头枚举不可用"
    try:
        proc = subprocess.run(
            ["/usr/sbin/system_profiler", "SPCameraDataType", "-json"],
            capture_output=True, text=True, timeout=6)
        if proc.returncode != 0:
            return [], f"system_profiler 返回 {proc.returncode}"
        raw = json.loads(proc.stdout or "{}")
        names = [str(e.get("_name") or "") for e in
                 (raw.get("SPCameraDataType") or []) if isinstance(e, dict)]
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"
    out = [{"index": i, "name": name,
            "preferred": ("obs" in name.lower() or "virtual" in name.lower())}
           for i, name in enumerate(names)]
    return out, None


def _camera_candidates(preferred: int | None = None) -> list[int]:
    if preferred is not None:
        return [preferred]
    names, _ = list_cameras()
    first = [int(c["index"]) for c in names if c["preferred"]]
    return first + [i for i in range(6) if i not in first]


def open_camera(index: int):
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


# ─────────────────────────── 抓帧 ───────────────────────────

def grab_frame(spec: str, *, timeout: float = 8.0) -> dict:
    """开源 → 读一帧 → 释放。返回 {ok, frame, size, kind, error, elapsed_ms}。

    `frame` 是 BGR ndarray（不可 JSON 序列化）；HTTP 层需要的是 `size` 和错误。
    """
    parsed = parse_source_spec(spec)
    started = time.monotonic()
    base = {"ok": False, "frame": None, "size": None,
            "kind": parsed.get("kind"), "error": None,
            "elapsed_ms": 0.0, "spec": str(spec or "")}
    if not parsed["valid"]:
        base["error"] = parsed["detail"] or "视频源不合法"
        return base

    kind = parsed["kind"]
    cap = None
    try:
        if kind == "window":
            inner = str(spec).split(":", 1)[1].strip()
            if inner.lower() == "front":
                from ..bet_slider import front_window
                rect = front_window()
                cap = WindowCapture(f"{rect.x:g},{rect.y:g},{rect.w:g},{rect.h:g}")
            else:
                cap = WindowCapture(inner)
            frame = cap.capture()
        elif kind == "camera":
            inner = str(spec).split(":", 1)[1].strip().lower()
            candidate = None if inner in ("", "auto") else int(inner)
            cap = None
            for index in _camera_candidates(candidate):
                cap = open_camera(index)
                if cap is not None:
                    break
            if cap is None:
                base["error"] = "打不开摄像头（检查系统设置 → 隐私 → 摄像头）"
                return base
            ok, frame = _read_with_deadline(cap, started, timeout)
            if not ok:
                base["error"] = "摄像头已打开但没有出帧（可能被其他程序占用）"
                return base
        else:
            path = str(spec)
            if kind == "file" and Path(path).suffix.lower() in IMAGE_SUFFIXES:
                frame = cv2.imread(path)
                if frame is None:
                    base["error"] = f"读不出图片：{path}"
                    return base
            else:
                cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
                if not cap.isOpened():
                    cap.release()
                    cap = cv2.VideoCapture(path)
                if not cap.isOpened():
                    base["error"] = "打不开视频源（RTSP/文件）"
                    return base
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                for name, value in (("CAP_PROP_OPEN_TIMEOUT_MSEC", 5000),
                                    ("CAP_PROP_READ_TIMEOUT_MSEC", 5000)):
                    prop = getattr(cv2, name, None)
                    if prop is not None:
                        cap.set(prop, value)
                ok, frame = _read_with_deadline(cap, started, timeout)
                if not ok:
                    base["error"] = "源已打开但没有出帧（超时）"
                    return base
        if frame is None or getattr(frame, "size", 0) == 0:
            base["error"] = "抓到的帧是空的"
            return base
        h, w = frame.shape[:2]
        base.update({"ok": True, "frame": frame, "size": [int(w), int(h)]})
        return base
    except Exception as exc:  # noqa: BLE001 — 体检失败要能读，不能抛
        base["error"] = f"{type(exc).__name__}: {exc}"
        return base
    finally:
        base["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass


def _read_with_deadline(cap, started: float, timeout: float) -> tuple[bool, Any]:
    while time.monotonic() - started < max(0.5, timeout):
        ok, frame = cap.read()
        if ok and frame is not None:
            return True, frame
        time.sleep(0.05)
    return False, None


# ─────────────────────────── 调整窗口 ───────────────────────────

def _applescript_quote(text: str) -> str:
    """AppleScript 字符串字面量里的转义（标题里可能有引号/反斜杠）。"""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


_RESIZE_TEMPLATE = """set targetName to "@TITLE@"
set targetX to @X@
set targetY to @Y@
set resized to false
tell application "System Events"
  repeat with proc in (every process)
    try
      if name of proc is "@OWNER@" then
        repeat with w in (every window of proc)
          try
            set nm to (name of w)
            set pos to (position of w)
            set nx to item 1 of pos
            set ny to item 2 of pos
            set nameHit to (nm is targetName)
            set posHit to ((nx > targetX - 3) and (nx < targetX + 3) and (ny > targetY - 3) and (ny < targetY + 3))
            if nameHit or posHit then
              set position of w to {targetX, targetY}
              set size of w to {@W@, @H@}
              set resized to true
              exit repeat
            end if
          end try
        end repeat
      end if
    end try
    if resized then exit repeat
  end repeat
end tell
if resized then
  return "ok"
else
  return "not-found"
end if
"""


def _resize_script(owner: str, title: str, x: float, y: float,
                   width: int, height: int) -> str:
    """生成调整脚本：先按窗口标题匹配，失败再按左上角位置兜底。

    Chromium 的 CGWindowList 标题和 Accessibility 的 window name 偶尔不一致
    （标签页切换/多 profile），只按 name 会报「无效的索引」。位置是同一个
    窗口最稳的关联，所以两个条件任一命中就调。
    """
    return (_RESIZE_TEMPLATE
            .replace("@OWNER@", _applescript_quote(owner))
            .replace("@TITLE@", _applescript_quote(title))
            .replace("@X@", f"{x:g}")
            .replace("@Y@", f"{y:g}")
            .replace("@W@", str(int(width)))
            .replace("@H@", str(int(height))))


def _apply_window_geometry(owner: str, title: str, x: float, y: float,
                           width: int, height: int, *,
                           timeout: float = 12.0) -> tuple[bool, str]:
    """执行一次 System Events 调整；返回 (ok, 错误/空)。"""
    script = _resize_script(owner, title, x, y, width, height)
    try:
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True,
                              timeout=max(3.0, timeout))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    detail = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode == 0 and detail == "not-found":
        return False, "not-found"
    return proc.returncode == 0, detail


def resize_window(spec: str, width: int, height: int, *,
                  timeout: float = 12.0) -> dict:
    """把游戏窗口调整到指定逻辑点尺寸（一键配置窗口分辨率）。

    用 `System Events` 的 Accessibility 接口，先改位置（保持左上角）再改尺寸。
    `System Events` 的 size 语义偶发包含/不包含窗口装饰，所以读回实际尺寸后
    会用偏差再校正一次（仅当偏差在 40pt 内，避免窗口最小值/全屏时乱试）。

    只支持能定位到具体窗口的写法（title:/index:/owner:）；手填 x,y,w,h 没有
    窗口 id，无法调整。失败一律返回 `{ok: False, error, fix}`，不抛异常。
    """
    width, height = int(round(width)), int(round(height))
    info = window_info(spec)
    if not info:
        return {"ok": False, "error": f"解析不到窗口：{spec}",
                "fix": "用「枚举窗口」选 title:... / index:N"}
    owner = str(info.get("owner") or "").strip()
    title = str(info.get("title") or "").strip()
    if not owner:
        return {"ok": False,
                "error": "这个窗口没有 owner（手填 x,y,w,h 只能截图，不能调整）",
                "fix": "改用「枚举窗口」里的 title:... / index:N"}
    if sys.platform != "darwin":
        return {"ok": False, "error": "调整窗口只支持 macOS",
                "fix": "非 macOS 请手动调整窗口"}
    # 先单独查权限：AppleScript 里的 try 会把权限错误吞成 not-found。
    permitted, perm_detail = accessibility_ok()
    if permitted is False:
        return {"ok": False, "error": perm_detail,
                "fix": "系统设置 → 隐私与安全性 → 辅助功能，"
                       "勾上运行工作台的终端，完全退出后重开"}

    ok, detail = _apply_window_geometry(
        owner, title, float(info["x"]), float(info["y"]), width, height,
        timeout=timeout)
    if not ok:
        fix = "系统设置 → 隐私与安全性 → 辅助功能，勾上运行工作台的终端"
        if "assistive access" in detail or "辅助访问" in detail:
            fix = ("未授权辅助功能：系统设置 → 隐私与安全性 → 辅助功能，"
                   "勾上运行工作台的终端，完全退出后重开")
        elif (detail == "not-found" or "not found" in detail
              or "-1728" in detail or "-1719" in detail
              or "无效的索引" in detail or "invalid index" in detail):
            fix = ("窗口标题/位置已变化（切了标签页、移动或关闭）："
                   "点「枚举窗口」重选；推荐用 id:N 或 index:N")
        if (detail == "not-found" or "无效的索引" in detail
                or "invalid index" in detail or "-1719" in detail):
            detail = "没有找到目标窗口（标题/位置已变化）"
        return {"ok": False, "error": detail or "osascript 返回非 0",
                "fix": fix, "before": info}

    after = window_info(spec) or None
    if after:
        dw = width - float(after["w"])
        dh = height - float(after["h"])
        if (abs(dw) > 2 or abs(dh) > 2) and abs(dw) <= 40 and abs(dh) <= 40:
            # 窗口装饰/标题栏导致第一次差几像素：按偏差补偿一次。
            ok2, detail2 = _apply_window_geometry(
                owner, title, float(after["x"]), float(after["y"]),
                int(round(width + dw)), int(round(height + dh)),
                timeout=timeout)
            if ok2:
                after = window_info(spec) or after
            else:
                detail = detail2 or detail
    if after is None:
        return {"ok": False,
                "error": "调整后读不到窗口（可能已关闭/移动）",
                "fix": "点「枚举窗口」重新选，再试一次",
                "before": info}
    return {
        "ok": True,
        "requested": [width, height],
        "before": info,
        "after": after,
        "matched": bool(after and abs(float(after["w"]) - width) <= 2
                        and abs(float(after["h"]) - height) <= 2),
        "detail": detail,
    }


# ─────────────────────────── 权限 ───────────────────────────

def screen_recording_ok() -> tuple[bool | None, str]:
    """截 4×4 像素测「屏幕录制」权限；非 macOS 返回 None（跳过）。"""
    if sys.platform != "darwin":
        return None, "非 macOS，跳过"
    if not shutil.which("screencapture"):
        return False, "找不到 screencapture"
    fd, path = tempfile.mkstemp(suffix=".png", prefix="wb_perm_")
    os.close(fd)
    try:
        proc = subprocess.run(
            ["screencapture", "-x", "-R", "0,0,4,4", path],
            capture_output=True, text=True, timeout=10)
        ok = proc.returncode == 0 and os.path.getsize(path) > 0
        if ok:
            return True, "可截屏"
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, ("截屏被系统拒绝" + (f"：{detail[:120]}" if detail else "")
                       + "（系统设置 → 隐私与安全性 → 屏幕录制）")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def accessibility_ok() -> tuple[bool | None, str]:
    """辅助功能权限：`System Events` 的 `UI elements enabled`。"""
    if sys.platform != "darwin":
        return None, "非 macOS，跳过"
    try:
        proc = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get UI elements enabled'],
            capture_output=True, text=True, timeout=10)
        out = (proc.stdout or "").strip().lower()
        if proc.returncode == 0 and out in ("true", "1", "yes"):
            return True, "已授权辅助功能"
        return False, ("未授权辅助功能（系统设置 → 隐私与安全性 → 辅助功能，"
                       "勾上运行工作台的终端）")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def automation_ok() -> tuple[bool | None, str]:
    """Automation 权限（固定/读取窗口用）：问一次 System Events。"""
    if sys.platform != "darwin":
        return None, "非 macOS，跳过"
    try:
        proc = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process '
             'whose frontmost is true'],
            capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return True, f"可读取前台进程：{proc.stdout.strip()}"
        return False, ("未授权 Automation（系统设置 → 隐私与安全性 → "
                       "自动化，允许终端控制 System Events）")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
