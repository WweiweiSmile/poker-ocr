"""手牌 outbox → replay-console 的自动同步 worker。

设计口径（见 docs/手牌库-自动沉淀与AB复盘-设计.md）：

  · **先落 outbox，再发网络**：live 主循环永远不被手牌库的可用性拖住。
  · **一手一文件、成功才移走**：失败留在 pending，超过次数进 failed/。
  · **批量为快，单条为稳**：一批里有一条坏数据时，服务端返回 per-record
    errors；worker 自动降级为逐手发送，只隔离坏手。
  · **401/403 不挪文件**：token 配错时不要把整队手牌打成 failed。

这个模块只依赖标准库 + `HandOutbox`，既能被 live 进程内嵌，也能被
`tools/sync_hand_outbox.py` / `tools/hand_ingest_daemon.py` 复用。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .hand_outbox import HandOutbox


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class IngestConfig:
    mode: str = "auto"                 # off | auto | direct
    url: str = "http://127.0.0.1:8010"
    token: str = ""
    interval: float = 2.0
    batch: int = 20
    timeout: float = 20.0
    max_failures: int = 8
    backoff: tuple[float, ...] = (2, 5, 15, 60, 300)

    @classmethod
    def from_env(cls) -> "IngestConfig":
        mode = (os.environ.get("HAND_INGEST_MODE") or "auto").strip().lower()
        if mode not in ("off", "auto", "direct"):
            mode = "auto"
        token = (os.environ.get("HAND_INGEST_TOKEN")
                 or os.environ.get("REPLAY_TOKEN") or "").strip()
        raw_backoff = (os.environ.get("HAND_INGEST_BACKOFF")
                       or "2,5,15,60,300")
        try:
            backoff = tuple(float(x) for x in raw_backoff.split(",") if x.strip())
        except ValueError:
            backoff = (2, 5, 15, 60, 300)
        return cls(
            mode=mode,
            url=(os.environ.get("HAND_INGEST_URL")
                 or "http://127.0.0.1:8010").strip().rstrip("/"),
            token=token,
            interval=max(0.5, _env_float("HAND_INGEST_INTERVAL", 2.0)),
            batch=max(1, _env_int("HAND_INGEST_BATCH", 20)),
            timeout=max(1.0, _env_float("HAND_INGEST_TIMEOUT", 20.0)),
            max_failures=max(1, _env_int("HAND_INGEST_MAX_FAILURES", 8)),
            backoff=backoff,
        )


@dataclass
class IngestStats:
    sent: int = 0
    failed: int = 0
    duplicates: int = 0
    last_error: str | None = None
    last_success_at: float | None = None
    last_attempt_at: float | None = None
    attempts: int = 0
    consecutive_errors: int = 0
    batches: int = 0
    running: bool = False


class _Transient(Exception):
    """网络/5xx/429：保留 pending，稍后重试。"""


class _Unauthorized(Exception):
    """401/403：停止发送，不要挪文件。"""


class _Permanent(Exception):
    """单条坏数据/422：进 failed/。"""


def _post_json(url: str, payload: dict, token: str,
               timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"raw": raw}
    return data if isinstance(data, dict) else {"data": data}


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code)
    return None


def _post_ingest(config: IngestConfig, records: list[dict]) -> dict:
    try:
        return _post_json(
            config.url + "/api/hands/ingest",
            {"source": "live", "records": records},
            config.token, config.timeout,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 405):
            return _post_import_fallback(config, records)
        if exc.code in (401, 403):
            raise _Unauthorized(f"HTTP {exc.code}：token/权限不对") from exc
        if exc.code in (408, 429) or 500 <= exc.code < 600:
            raise _Transient(f"HTTP {exc.code}") from exc
        raise _Permanent(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Transient(f"{type(exc).__name__}: {exc}") from exc


def _post_import_fallback(config: IngestConfig, records: list[dict]) -> dict:
    """旧服务端兼容：把记录序列化成 import 文本。"""
    try:
        return _post_json(
            config.url + "/api/hands/import",
            {
                "format": "live",
                "content": json.dumps(records, ensure_ascii=False),
                "dryRun": False,
                "onlyExact": False,
                "allowInvalid": False,
            },
            config.token, config.timeout,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise _Unauthorized(f"HTTP {exc.code}：token/权限不对") from exc
        if exc.code in (408, 429) or 500 <= exc.code < 600:
            raise _Transient(f"HTTP {exc.code}") from exc
        raise _Permanent(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Transient(f"{type(exc).__name__}: {exc}") from exc


def _response_errors(result: dict) -> int:
    try:
        return int(result.get("errors") or 0)
    except (TypeError, ValueError):
        return 0


def _attempt_sidecar(path: Path) -> Path:
    return path.with_suffix(".error")


def _bump_failure(path: Path, error: str, max_failures: int,
                  outbox: HandOutbox) -> str:
    """失败先留在 pending 并记次数；达到上限才进 failed/。"""
    sidecar = _attempt_sidecar(path)
    try:
        attempts = int(sidecar.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        attempts = 0
    attempts += 1
    if attempts >= max(1, int(max_failures)):
        outbox.mark_failed(path, f"第 {attempts} 次失败：{error}")
        return f"failed（{attempts} 次）"
    sidecar.write_text(f"{attempts}\n{error}"[:4000], encoding="utf-8")
    return f"pending（{attempts}/{max_failures}）"


class HandIngestWorker:
    """后台同步线程。`start()` 后 daemon 运行，`kick()` 唤醒立即处理。"""

    def __init__(self, outbox: HandOutbox, config: IngestConfig | None = None,
                 *, log: Callable[[str], None] | None = None):
        self.outbox = outbox
        self.config = config or IngestConfig.from_env()
        self.log = log or (lambda _msg: None)
        self.stats = IngestStats()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._backoff_index = 0

    # ── 生命周期 ──
    def start(self) -> bool:
        if self.config.mode == "off":
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="hand-ingest", daemon=True)
        self._thread.start()
        self.stats.running = True
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self.stats.running = False

    def kick(self) -> None:
        self._wake.set()

    # ── 发送 ──
    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.config.interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.process_pending(limit=self.config.batch)
                self._backoff_index = 0
            except _Unauthorized as exc:
                self.stats.last_error = str(exc)
                self.stats.consecutive_errors += 1
                # token 错时不要刷屏/不要动文件；等下一次（间隔放大到 30s）
                self._stop.wait(30)
            except _Transient as exc:
                self.stats.last_error = str(exc)
                self.stats.consecutive_errors += 1
                delay = self.config.backoff[min(
                    self._backoff_index, len(self.config.backoff) - 1)]
                self._backoff_index += 1
                self._stop.wait(delay)
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self.stats.consecutive_errors += 1
                self._stop.wait(self.config.interval)
        self.stats.running = False

    def process_pending(self, limit: int = 20) -> dict:
        """处理一批 pending。返回 {sent,failed,duplicates,...}。

        这是同步入口，`tools/sync_hand_outbox.py` 和 daemon 也复用它。
        """
        paths = self.outbox.pending()[:max(0, int(limit))]
        if not paths:
            return {"sent": 0, "failed": 0, "duplicates": 0, "pending": 0}
        records: list[tuple[Path, dict]] = []
        bad: list[tuple[Path, str]] = []
        for path in paths:
            try:
                record = self.outbox.load(path)
                if not isinstance(record, dict):
                    raise ValueError("顶层不是对象")
                records.append((path, record))
            except Exception as exc:  # noqa: BLE001
                bad.append((path, f"坏文件：{type(exc).__name__}: {exc}"))

        sent = failed = duplicates = 0
        stats = self.stats
        stats.last_attempt_at = time.time()
        stats.attempts += 1
        for path, error in bad:
            detail = _bump_failure(path, error, self.config.max_failures,
                                   self.outbox)
            self.log(f"[手牌库] 坏文件 {path.name} → {detail}")
            failed += 1

        if records:
            try:
                result = _post_ingest(self.config, [r for _p, r in records])
                errors = _response_errors(result)
                duplicates += int(result.get("duplicates") or 0)
                if errors:
                    # 批量里有坏数据：逐条重发，避免一手坏牌堵住整队。
                    self.log(f"[手牌库] 批次有 {errors} 条错误，逐手重试")
                    result = None
                if result is not None:
                    for path, _record in records:
                        self.outbox.mark_sent(path)
                        sent += 1
                    stats.sent += sent
                    stats.duplicates += duplicates
                    stats.last_success_at = time.time()
                    stats.consecutive_errors = 0
                    self.log(f"[手牌库] 已入库 {sent} 手"
                             + (f"（重复 {duplicates}）" if duplicates else ""))
                    return {"sent": sent, "failed": failed,
                            "duplicates": duplicates, "pending": 0}
            except _Unauthorized:
                raise
            except _Transient:
                # 整批留在 pending，不逐个标失败
                stats.last_error = "网络/服务端暂不可用"
                raise
            except _Permanent as exc:
                self.log(f"[手牌库] 批次永久错误，逐手重试：{exc}")
                result = None

            # 逐手发送：只隔离坏手
            for path, record in records:
                try:
                    single = _post_ingest(self.config, [record])
                    if _response_errors(single):
                        raise _Permanent(
                            str((single.get("issues") or [{}])[0].get("reason")
                                or "服务端返回 errors"))
                    self.outbox.mark_sent(path)
                    sent += 1
                    stats.sent += 1
                    stats.last_success_at = time.time()
                except _Unauthorized:
                    raise
                except _Transient as exc:
                    stats.last_error = str(exc)
                    stats.consecutive_errors += 1
                    # 单条网络错误：留给下一轮，不把它打成 failed
                    self.log(f"[手牌库] {path.name} 暂缓：{exc}")
                except _Permanent as exc:
                    detail = _bump_failure(path, str(exc),
                                           self.config.max_failures,
                                           self.outbox)
                    self.log(f"[手牌库] {path.name} 失败 → {detail}")
                    failed += 1
                    stats.failed += 1
        stats.consecutive_errors = 0 if sent else stats.consecutive_errors
        return {"sent": sent, "failed": failed,
                "duplicates": duplicates,
                "pending": len(self.outbox.pending())}

    def health(self) -> dict:
        pending = len(self.outbox.pending())
        return {
            "mode": self.config.mode,
            "url": self.config.url,
            "running": bool(self._thread and self._thread.is_alive()),
            "pending": pending,
            "sent": self.stats.sent,
            "failed": self.stats.failed,
            "duplicates": self.stats.duplicates,
            "attempts": self.stats.attempts,
            "lastError": self.stats.last_error,
            "lastSuccessAt": self.stats.last_success_at,
            "lastAttemptAt": self.stats.last_attempt_at,
        }


def send_outbox_once(outbox: HandOutbox, config: IngestConfig | None = None,
                     *, limit: int = 200,
                     log: Callable[[str], None] | None = None) -> dict:
    """给工具/daemon 的一次性入口。不启动线程。"""
    worker = HandIngestWorker(outbox, config, log=log)
    try:
        return worker.process_pending(limit=limit)
    except _Unauthorized as exc:
        return {"sent": 0, "failed": 0, "duplicates": 0,
                "pending": len(outbox.pending()),
                "error": f"UNAUTHORIZED: {exc}"}
    except _Transient as exc:
        return {"sent": 0, "failed": 0, "duplicates": 0,
                "pending": len(outbox.pending()),
                "error": f"TRANSIENT: {exc}"}
