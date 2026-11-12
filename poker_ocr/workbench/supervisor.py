"""live 子进程管理：spawn / 停止 / 重启 / 运行目录 / manifest。

口径（设计 §4.2–4.5、§10）：

  · 一次只跑一路；同一时刻只有一个 `RunHandle`；
  · 进程组（`start_new_session=True` + `killpg`）：工作台退出时不会留下
    孤儿自动点击进程；
  · 先 SIGINT、等 10s、不退再 SIGKILL；
  · 每个 run 一个目录 `runs/<时间>-<工作区>/`，落 `live.states.jsonl`、
    `live.events.jsonl`、`live.console.log`、`workspace.json`、`preflight.json`；
    结束时写 `manifest.json`（配置哈希、窗口 rect、退出码、统计）；
  · 命令只由工作区字段拼出来（白名单），不拼接任意 shell 片段。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import live_client
from .preflight import port_state
from .store import PROJECT_ROOT, slugify

ACTIVE_STATUSES = ("starting", "running", "paused_click", "calibrating",
                   "applying", "stopping")

# 高级参数 → live CLI flag（白名单；UI 不能塞任意参数）。
ADVANCED_VALUE_FLAGS: dict[str, str] = {
    "stable_frames": "--stable-frames",
    "stable_seconds": "--stable-seconds",
    "dealer_carry": "--dealer-carry",
    "card_confirm_frames": "--card-confirm-frames",
    "card_doubt_window": "--card-doubt-window",
    "max_frame_age": "--max-frame-age",
    "roi_threads": "--roi-threads",
    "max_vanish": "--max-vanish",
    "name_confirm": "--name-confirm",
    "incident_frames": "--incident-frames",
    "incident_max": "--incident-max",
    "min_advice_confidence": "--min-advice-confidence",
    "ai_timeout": "--ai-timeout",
    "ai_confirm": "--ai-confirm",
    "rotate": "--rotate",
}
ADVANCED_CHOICE_FLAGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "crop": ("--crop", ("auto", "left", "none")),
    "cross_check": ("--cross-check", ("off", "warn", "block")),
}
ADVANCED_BOOL_FLAGS: dict[str, tuple[str, str | None]] = {
    "stable_reset_on_discard": ("--stable-reset-on-discard",
                                "--no-stable-reset-on-discard"),
    "record_without_hero": ("--record-without-hero",
                            "--no-record-without-hero"),
    "no_dedup": ("--no-dedup", None),
    "no_fold": ("--no-fold", None),
    "lenient_chips": ("--lenient-chips", None),
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _abs(path: str | Path, root: Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else Path(root) / path


def _window_spec_for_slider(workspace: dict) -> str | None:
    """滑杆点击用的窗口写法。

    优先级：工作区 `window.spec`（摄像头/RTSP/文件源必须显式填）
    > 手动固定 rect > 窗口源本身。窗口源留空时自动跟随源，和 CLI 一致。
    """
    from .sources import normalize_window_spec
    window = workspace.get("window") or {}
    explicit = normalize_window_spec(window.get("spec"))
    if explicit:
        return explicit
    rect = window.get("pinned_rect")
    if isinstance(rect, dict) and all(
            isinstance(rect.get(k), (int, float)) for k in ("x", "y", "w", "h")):
        return (f"{rect['x']:g},{rect['y']:g},"
                f"{rect['w']:g},{rect['h']:g}")
    source = workspace.get("source") or {}
    spec = str(source.get("spec") or "")
    if spec.startswith(("window:", "screen:")):
        inner = spec.split(":", 1)[1].strip()
        if inner and inner.lower() != "front":
            return inner
    return None


def build_live_command(workspace: dict, *, root: Path = PROJECT_ROOT,
                       python: str | None = None, port: int = 8020,
                       run_dir: Path, token: str, run_id: str) -> list[str]:
    """工作区 → live 命令行（纯函数，单测直接断言）。"""
    root = Path(root)
    python = python or sys.executable
    run_dir = Path(run_dir)
    profile = _abs(workspace["profile"], root)
    source = workspace.get("source") or {}
    runtime = workspace.get("runtime") or {}
    click = workspace.get("click") or {}
    advanced = runtime.get("advanced") or {}

    cmd: list[str] = [
        python, "-m", "poker_ocr.live",
        "--profile", str(profile),
        "--url", str(source.get("spec") or ""),
        "--recognizer", str(workspace.get("recognizer") or "roi"),
        "--out", str(run_dir / "live.states.jsonl"),
        "--events-out", str(run_dir / "live.events.jsonl"),
        "--log-file", str(run_dir / "live.log"),
        "--web", "--web-host", "127.0.0.1", "--web-port", str(int(port)),
        "--preview", "--control-token", token, "--run-id", run_id,
    ]
    if workspace.get("recognizer") == "full" and workspace.get("full_config"):
        cmd += ["--full-config", str(_abs(workspace["full_config"], root))]

    if runtime.get("initial_blinds"):
        cmd += ["--initial-blinds", str(runtime["initial_blinds"]).replace(" ", "")]
    cmd += ["--every", _fmt(float(runtime.get("every") or 0))]
    # settle 必须显式传：不传时 live 默认 0.25s，工作区写 0（关闭动作合并）
    # 就会静默变成 0.25。
    cmd += ["--settle", _fmt(float(runtime.get("settle") or 0))]
    cmd += ["--ai-advice" if runtime.get("ai_advice") else "--no-ai-advice"]
    if runtime.get("test"):
        cmd.append("--test")

    # 高级参数：只认白名单，值做类型/枚举检查；不合法就跳过（fail-open，
    # 由体检/校验负责提示，命令行不拼任何自由文本）。
    for key, flag in ADVANCED_VALUE_FLAGS.items():
        value = advanced.get(key)
        if value is None:
            continue
        if key == "rotate" and int(value) not in (0, 90, 180, 270):
            continue
        cmd += [flag, _fmt(value)]
    for key, (flag, choices) in ADVANCED_CHOICE_FLAGS.items():
        value = advanced.get(key)
        if value is None or str(value) not in choices:
            continue
        cmd += [flag, str(value)]
    for key, (positive, negative) in ADVANCED_BOOL_FLAGS.items():
        if key not in advanced:
            continue
        if bool(advanced[key]):
            cmd.append(positive)
        elif negative is not None:
            cmd.append(negative)

    if click.get("enabled"):
        cmd.append("--auto-click")
        if click.get("mode") == "legacy_clicker":
            legacy = click.get("clicker_config")
            if legacy:
                cmd += ["--clicker-config", str(_abs(legacy, root))]
        if click.get("bet_slider"):
            cmd.append("--bet-slider")
            if click.get("bet_slider_only"):
                cmd.append("--bet-slider-only")
            slider_window = _window_spec_for_slider(workspace)
            if slider_window:
                cmd += ["--bet-slider-window", slider_window]
            slider_cfg = workspace.get("bet_slider_config")
            if slider_cfg:
                cmd += ["--bet-slider-config",
                        str(_abs(slider_cfg, root))]
        if click.get("dry_run"):
            cmd.append("--clicker-dry-run")
        if not click.get("confirm", True):
            cmd.append("--no-confirm")
    else:
        cmd.append("--no-auto-click")
    return cmd


def redact_command(command: list[str], token: str | None) -> list[str]:
    if not token:
        return list(command)
    return ["***" if str(part) == token else str(part) for part in command]


# ─────────────────────────── 运行句柄 ───────────────────────────

@dataclass
class RunHandle:
    run_id: str
    workspace_id: str
    dir: Path
    port: int
    token: str
    started_at: str
    status: str = "starting"
    pid: int | None = None
    exit_code: int | None = None
    ended_at: str | None = None
    error: str | None = None
    command: list[str] = field(default_factory=list)
    preflight: dict | None = None
    log_path: Path | None = None
    stdout_path: Path | None = None
    stats: dict = field(default_factory=dict)
    meta: dict | None = None
    finalized: bool = False

    def public(self) -> dict:
        return {
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "dir": str(self.dir),
            "port": self.port,
            "pid": self.pid,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "error": self.error,
            "command": redact_command(self.command, self.token),
            "log_path": str(self.log_path) if self.log_path else None,
            "stats": dict(self.stats),
            "meta": self.meta,
            "active": self.status in ACTIVE_STATUSES,
        }


class LiveSupervisor:
    """单实例 live 子进程管理。所有状态变更都在 `_lock` 里。"""

    def __init__(self, root: Path | str = PROJECT_ROOT, *,
                 runs_dir: Path | str | None = None,
                 python: str | None = None,
                 log: Callable[[str], None] | None = None,
                 spawn: Callable[..., object] | None = None,
                 ready_check: Callable[..., dict] | None = None,
                 meta_get: Callable[..., dict | None] | None = None,
                 ready_timeout: float = 20.0,
                 stop_timeout: float = 10.0) -> None:
        self.root = Path(root)
        self.runs_dir = Path(runs_dir or (self.root / "runs"))
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.python = python or sys.executable
        self.log = log or (lambda _msg: None)
        self._spawn = spawn or subprocess.Popen
        self._ready_check = ready_check or live_client.wait_ready
        self._meta_get = meta_get or live_client.meta
        self.ready_timeout = float(ready_timeout)
        self.stop_timeout = float(stop_timeout)
        self._lock = threading.RLock()
        self._run: RunHandle | None = None
        self._proc = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._log_fh = None

    # ── 查询 ────────────────────────────────────────────
    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._run and self._run.status in ACTIVE_STATUSES)

    def status(self) -> dict:
        with self._lock:
            if self._run is None:
                return {"status": "idle", "run": None}
            run = self._run.public()
            run["alive"] = bool(self._proc is not None
                                and self._proc.poll() is None)
            return {"status": run["status"], "run": run}

    def current(self) -> RunHandle | None:
        with self._lock:
            return self._run

    # ── 启动 ────────────────────────────────────────────
    def start(self, workspace: dict, *, preflight: dict | None = None,
              workspace_snapshot: dict | None = None) -> dict:
        with self._lock:
            if self._run is not None and self._run.status in ACTIVE_STATUSES:
                raise RuntimeError(
                    f"已有 live 在运行（{self._run.workspace_id} / "
                    f"{self._run.run_id}），先停止再启动")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            run_id = f"{stamp}-{slugify(workspace.get('id') or 'run')}"
            run_dir = self._unique_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            token = secrets.token_urlsafe(24)
            port = self._pick_port()
            command = build_live_command(
                workspace, root=self.root, python=self.python,
                port=port, run_dir=run_dir, token=token, run_id=run_id)
            handle = RunHandle(
                run_id=run_id, workspace_id=str(workspace.get("id") or ""),
                dir=run_dir, port=port, token=token,
                started_at=now_iso(), command=command,
                log_path=run_dir / "live.log", preflight=preflight,
                stdout_path=run_dir / "live.stdout.log")
            self._write_json(run_dir / "workspace.json",
                             workspace_snapshot or workspace)
            if preflight is not None:
                self._write_json(run_dir / "preflight.json", preflight)
            self._write_json(run_dir / "run.json", {
                "run_id": run_id, "workspace_id": handle.workspace_id,
                "port": port, "pid": None, "started_at": handle.started_at,
                "command": redact_command(command, token),
                "status": "starting",
            })
            self._run = handle
            self._stopping.clear()
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            try:
                # stdout/stderr 单独一个文件：live 自己也会往 --log-file
                # 写同一份文本，两个 fd 写同一个文件会互相覆盖成乱码。
                self._log_fh = open(handle.stdout_path, "ab")
                proc = self._spawn(
                    command, cwd=str(self.root), env=env,
                    stdout=self._log_fh, stderr=subprocess.STDOUT,
                    start_new_session=True)
            except Exception as exc:  # noqa: BLE001 — spawn 失败要可解释
                handle.status = "failed"
                handle.error = f"spawn 失败：{type(exc).__name__}: {exc}"
                self._close_log()
                self._finalize(handle, workspace)
                raise RuntimeError(handle.error) from exc
            handle.pid = int(getattr(proc, "pid", 0) or 0)
            self._proc = proc
            self._write_json(run_dir / "run.json", {
                "run_id": run_id, "workspace_id": handle.workspace_id,
                "port": port, "pid": handle.pid,
                "started_at": handle.started_at,
                "command": redact_command(command, token),
                "status": "starting",
            })
            self._thread = threading.Thread(
                target=self._monitor, args=(handle, proc, workspace),
                name="live-monitor", daemon=True)
            self._thread.start()
            self.log(f"[工作台] 启动 {run_id}：:{port} pid={handle.pid}")
            return handle.public()

    def _monitor(self, handle: RunHandle, proc, workspace: dict) -> None:
        try:
            ready = self._ready_check(
                handle.port, timeout=self.ready_timeout,
                alive=lambda: proc.poll() is None)
            full_meta = None
            if ready.get("ok"):
                try:
                    full_meta = self._meta_get(handle.port, timeout=1.5)
                except Exception:  # noqa: BLE001
                    full_meta = None
            with self._lock:
                handle.meta = full_meta or ready.get("meta")
                if ready.get("ok") and not self._stopping.is_set():
                    handle.status = "running"
                    handle.error = None
                elif self._stopping.is_set():
                    pass
                else:
                    handle.status = "failed"
                    tail = self.log_tail(limit=8)
                    handle.error = (ready.get("error") or "启动失败") + (
                        f"\n{tail}" if tail else "")
                    self._kill(proc)
            code = self._wait(proc, timeout=None)
            with self._lock:
                handle.exit_code = code
                if self._stopping.is_set():
                    handle.status = "idle"
                elif handle.status == "running":
                    if code in (0, None):
                        handle.status = "idle"
                    else:
                        handle.status = "failed"
                        handle.error = handle.error or f"live 退出码 {code}"
        finally:
            with self._lock:
                self._close_log()
                self._finalize(handle, workspace)

    # ── 停止 ────────────────────────────────────────────
    def stop(self) -> dict:
        with self._lock:
            handle = self._run
            proc = self._proc
            if handle is None:
                return {"status": "idle", "run": None}
            if proc is None or proc.poll() is not None:
                self._finalize(handle, None)
                return handle.public()
            handle.status = "stopping"
            self._stopping.set()
            # 停之前尽量取一次统计/元数据：进程一退就拿不到了。
            try:
                meta = self._meta_get(handle.port, timeout=0.8)
                if meta:
                    handle.meta = meta
                    handle.stats = dict(meta.get("stats") or {})
            except Exception:  # noqa: BLE001
                pass
            self.log(f"[工作台] 停止 {handle.run_id}（pid={handle.pid}）")
            self._signal(proc, signal.SIGINT)
            code = self._wait(proc, timeout=self.stop_timeout)
            if code is None:
                self.log("[工作台] SIGINT 后 10s 未退出，SIGKILL")
                self._kill(proc)
                code = self._wait(proc, timeout=3.0)
            thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        with self._lock:
            handle.exit_code = code
            handle.status = "idle"
            self._close_log()
            self._finalize(handle, None)
            return handle.public()

    def restart(self, workspace: dict, *, preflight: dict | None = None,
                workspace_snapshot: dict | None = None) -> dict:
        self.stop()
        return self.start(workspace, preflight=preflight,
                          workspace_snapshot=workspace_snapshot)

    def shutdown(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 — 退出路径不再抛
            pass

    # ── 统计 ────────────────────────────────────────────
    def refresh(self) -> dict:
        """工作台轮询时顺手把 live 的 meta/统计同步到句柄。"""
        with self._lock:
            handle = self._run
            proc = self._proc
            if handle is None:
                return {"status": "idle", "run": None}
            alive = bool(proc is not None and proc.poll() is None)
        if alive and handle.status in ("running", "starting"):
            meta = self._meta_get(handle.port, timeout=1.2)
            if meta:
                with self._lock:
                    handle.meta = meta
                    handle.stats = dict(meta.get("stats") or {})
                    if handle.status == "starting" and (
                            (meta.get("frame") or {}).get("width") or 0) > 0:
                        handle.status = "running"
        return self.status()

    # ── 内部 ────────────────────────────────────────────
    def _pick_port(self, start: int = 8020, end: int = 8039) -> int:
        for port in range(start, end + 1):
            if not port_state(port)["busy"]:
                return port
        raise RuntimeError(f":{start}–:{end} 都被占用，没有可用的 live 端口")

    def _unique_dir(self, run_id: str) -> Path:
        base = self.runs_dir / run_id
        if not base.exists():
            return base
        for i in range(2, 100):
            candidate = self.runs_dir / f"{run_id}-{i}"
            if not candidate.exists():
                return candidate
        return self.runs_dir / f"{run_id}-{secrets.token_hex(2)}"

    def _signal(self, proc, sig) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                if sig == signal.SIGKILL:
                    proc.kill()
                else:
                    proc.send_signal(sig)
            except Exception:  # noqa: BLE001
                pass

    def _kill(self, proc) -> None:
        self._signal(proc, signal.SIGKILL)

    def _wait(self, proc, *, timeout: float | None) -> int | None:
        """等进程退出；超时返回 None。兼容测试里的假进程对象。"""
        if timeout is None:
            try:
                return int(proc.wait())
            except Exception:  # noqa: BLE001
                return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                return int(code)
            time.sleep(0.1)
        return proc.poll()

    def _close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    def log_tail(self, limit: int = 40) -> str:
        """控制台文本日志尾部；live 的 --log-file 还没写时退回 stdout。"""
        with self._lock:
            handle = self._run
        if handle is None:
            return ""
        for path in (handle.log_path, handle.stdout_path):
            if path is None:
                continue
            try:
                text = Path(path).read_text(encoding="utf-8",
                                            errors="replace")
            except OSError:
                continue
            if text.strip():
                lines = text.splitlines()
                return "\n".join(lines[-max(1, limit):])
        return ""

    def _finalize(self, handle: RunHandle, workspace: dict | None) -> None:
        if handle.finalized:
            return
        handle.finalized = True
        handle.ended_at = handle.ended_at or now_iso()
        if handle.exit_code is None:
            handle.exit_code = 0 if handle.status == "idle" else None
        try:
            snapshot = self._read_json(handle.dir / "workspace.json")
            profile_path = _abs(str((snapshot or {}).get("profile")
                                    or (workspace or {}).get("profile") or ""),
                                self.root)
            manifest = {
                "schema": 1,
                "run_id": handle.run_id,
                "workspace_id": handle.workspace_id,
                "started_at": handle.started_at,
                "ended_at": handle.ended_at,
                "status": handle.status,
                "exit_code": handle.exit_code,
                "pid": handle.pid,
                "port": handle.port,
                "error": handle.error,
                "command": redact_command(handle.command, handle.token),
                "profile": {
                    "path": str(profile_path),
                    "sha256": _sha256(profile_path),
                },
                "workspace": snapshot or workspace,
                "window": ((handle.meta or {}).get("source") or {}).get("window"),
                "click": (handle.meta or {}).get("click"),
                "stats": handle.stats,
                # 体检全文进 manifest（失败原因事后可查），摘要另存一份便于列表。
                "preflight": (handle.preflight
                              if isinstance(handle.preflight, dict) else None),
                "preflight_summary": ((handle.preflight or {}).get("summary")
                                      if isinstance(handle.preflight, dict)
                                      else None),
                "logs": {
                    "console": str(handle.dir / "live.log"),
                    "stdout": str(handle.dir / "live.stdout.log"),
                    "events": str(handle.dir / "live.events.jsonl"),
                    "states": str(handle.dir / "live.states.jsonl"),
                },
            }
            self._write_json(handle.dir / "manifest.json", manifest)
            self._write_json(handle.dir / "run.json", {
                "run_id": handle.run_id,
                "workspace_id": handle.workspace_id,
                "port": handle.port, "pid": handle.pid,
                "started_at": handle.started_at,
                "ended_at": handle.ended_at,
                "status": handle.status,
                "exit_code": handle.exit_code,
                "command": redact_command(handle.command, handle.token),
            })
        except Exception as exc:  # noqa: BLE001 — manifest 失败不该反噬退出
            self.log(f"[工作台] manifest 写入失败：{type(exc).__name__}: {exc}")

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def list_runs(runs_dir: Path | str = PROJECT_ROOT / "runs", *,
              workspace_id: str | None = None, limit: int = 50) -> list[dict]:
    """运行历史：优先 manifest，没有 manifest 的（进行中/硬杀）退回 run.json。"""
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    out = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        doc = LiveSupervisor._read_json(entry / "manifest.json") \
            or LiveSupervisor._read_json(entry / "run.json")
        if not doc:
            continue
        if workspace_id and str(doc.get("workspace_id")) != workspace_id:
            continue
        doc.setdefault("dir", str(entry))
        doc.setdefault("run_id", entry.name)
        out.append(doc)
    out.sort(key=lambda d: str(d.get("started_at") or ""), reverse=True)
    return out[:max(1, limit)]


def last_run(runs_dir: Path | str, workspace_id: str) -> dict | None:
    runs = list_runs(runs_dir, workspace_id=workspace_id, limit=1)
    return runs[0] if runs else None
