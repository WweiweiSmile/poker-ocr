"""实时 Web 看板：把 live 主循环的帧 / 建议 / 手牌事件推给浏览器。

设计约束：
  · **不引入新依赖**。用标准库 `http.server` + SSE，OCR 进程多一个轻线程，
    不碰模型、不碰点击；服务只绑定 `127.0.0.1`（默认），局域网不可见。
  · **不阻塞主链路**。`publish()` 只做内存状态更新和 `queue.put_nowait`；
    订阅者满了就丢最旧的事件，绝不反过来拖慢识别。
  · **事件与落盘同源**。同一个 `publish(kind, payload)` 既写
    `live.events.jsonl`（由调用方包一层），也推给浏览器；看板看到的就是
    日志里的事实，不是另算一份。
  · **提示词只读展示**。建议返回后 `Advice.to_dict()` 已经带 `prompt_system`
    / `prompt_user` / `facts` / `raw_response`，看板原样展示；不重新拼 prompt，
    避免「看到的提示词」和「模型实际收到的」不是同一份。

页面资源在 `poker_ocr/web/live/`：`index.html` / `app.js` / `style.css`。
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

WEB_ROOT = Path(__file__).resolve().parent / "web" / "live"
MAX_ADVICES = 30
MAX_HANDS = 30
MAX_FEED = 200
MAX_ACTIVE_EVENTS = 240
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _mid(slot) -> list[float] | None:
    if not isinstance(slot, (list, tuple)) or len(slot) != 4:
        return None
    x1, y1, x2, y2 = (float(v) for v in slot)
    return [round((x1 + x2) / 2, 4), round((y1 + y2) / 2, 4)]


def build_layout(profile) -> dict:
    """把 profile 的座位几何转换成前端坐标。

    用 `name_slot` 的中心而不是另编一套椭圆：这样看板里的座位位置和真实
    牌桌一致（hero 在下、左右两侧贴边），换 profile 不用改前端。没有
    `name_slot` 的 runtime profile 退化为按环序排一圈。
    """
    ring = list(getattr(profile, "ring_order", None) or [])
    seats_in_profile = list(getattr(profile, "seats", None) or [])
    by_id = {getattr(s, "seat_id", ""): s for s in seats_in_profile}
    if not ring:
        ring = [getattr(s, "seat_id", "") for s in seats_in_profile]
    hero = str(getattr(profile, "hero_seat_id", "") or "")
    n = max(1, len(ring))

    seats: list[dict] = []
    for i, seat_id in enumerate(ring):
        seat = by_id.get(seat_id)
        center = _mid(getattr(seat, "name_slot", None)) if seat else None
        if center is None:
            # 从 hero（底部）开始顺时针排；y 向下，角度递增即顺时针。
            import math
            angle = math.radians(90 + i * 360.0 / n)
            center = [round(0.5 + 0.42 * math.cos(angle), 4),
                      round(0.46 + 0.36 * math.sin(angle), 4)]
        seats.append({
            "seat_id": seat_id,
            "ring_index": i,
            "center": center,
            "is_hero": seat_id == hero,
        })

    reference = getattr(profile, "reference_size", None) or (0, 0)
    table_center = getattr(profile, "table_center", None) or (0.5, 0.4)
    return {
        "source": "profile" if profile is not None else "none",
        "hero_seat_id": hero,
        "ring_order": ring,
        "table_size": len(ring),
        "table_center": [round(float(table_center[0]), 4),
                         round(float(table_center[1]), 4)],
        "reference_size": [int(reference[0]), int(reference[1])],
        "seats": seats,
    }


def _feed_item(event_id: int, kind: str, payload: dict) -> dict:
    """事件流里的精简条目：不把 prompt 全文塞进每一帧的快照。"""
    keep: dict[str, Any] = {}
    if kind == "frame":
        for key in ("seq", "epoch", "frame_id", "t", "kind", "proc_ms",
                    "queue_ms", "hand_id", "street", "errors"):
            if key in payload:
                keep[key] = payload[key]
        cards = payload.get("cards") or {}
        if cards.get("warnings"):
            keep["cards_warnings"] = list(cards["warnings"])[:3]
    elif kind == "decision":
        for key in ("hand_id", "street", "pot", "to_call", "action_index"):
            if key in payload:
                keep[key] = payload[key]
        keep["options"] = len(payload.get("options") or ())
    elif kind == "advice_skip":
        for key in ("hand_id", "street", "reason", "pot"):
            if key in payload:
                keep[key] = payload[key]
        if payload.get("warnings"):
            keep["warnings"] = list(payload["warnings"])[:3]
    elif kind == "advice":
        option = payload.get("option") or {}
        keep.update({
            "hand_id": payload.get("hand_id"),
            "street": payload.get("street"),
            "action": option.get("label") or option.get("id"),
            "source": payload.get("source"),
            "duration_ms": payload.get("duration_ms"),
        })
        if payload.get("error"):
            keep["error"] = payload.get("error")
    elif kind == "hand":
        quality = payload.get("data_quality") or {}
        keep.update({
            "hand_id": payload.get("hand_id"),
            "partial": bool(payload.get("partial")),
            "status": quality.get("status"),
            "warnings": len(quality.get("warnings") or ()),
            "inferred_actions": quality.get("inferred_actions", 0),
        })
    elif kind == "incident":
        for key in ("reason", "folder", "hand_id"):
            if key in payload:
                keep[key] = payload[key]
    elif kind == "status":
        for key in ("processed", "sampled", "accepted", "records",
                    "avg_proc_ms", "reconnects", "advice_count",
                    "advice_done"):
            if key in payload:
                keep[key] = payload[key]
    elif kind == "click":
        keep.update({"hand_id": payload.get("hand_id"),
                     "click": payload.get("click")})
    return {"id": event_id, "type": kind, "ts": _now(), "summary": keep}


class LiveWebStore:
    """线程安全的内存快照 + 订阅者广播。"""

    def __init__(self, *, session: dict, layout: dict) -> None:
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()
        self._closed = False
        self._event_id = 0
        self.session = dict(session)
        self.layout = dict(layout)
        self.stats: dict[str, Any] = {
            "processed": 0, "sampled": 0, "accepted": 0, "records": 0,
            "avg_proc_ms": 0.0, "reconnects": 0,
            "advice_count": 0, "advice_done": 0,
            "incidents": 0, "fallbacks": 0,
            "cards_unreliable_skips": 0, "discarded_reasons": {},
        }
        self.latest_frame: dict | None = None
        self.table: dict | None = None
        self.pending: dict | None = None
        self.current_advice: dict | None = None
        self.advices: deque[dict] = deque(maxlen=MAX_ADVICES)
        self.hands: deque[dict] = deque(maxlen=MAX_HANDS)
        self.feed: deque[dict] = deque(maxlen=MAX_FEED)
        self.ended_at: str | None = None

    # ── 订阅 ────────────────────────────────────────────
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=MAX_ACTIVE_EVENTS)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subs = list(self._subs)
            self._subs.clear()
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    # ── 发布 ────────────────────────────────────────────
    def publish(self, kind: str, payload: dict | None = None) -> dict:
        data = payload or {}
        with self._lock:
            self._event_id += 1
            event = {"id": self._event_id, "type": kind,
                     "ts": _now(), "data": data}
            self._apply(kind, data)
            self.feed.append(_feed_item(self._event_id, kind, data))
            subs = list(self._subs)
        for q in subs:
            self._offer(q, event)
        return event

    def _apply(self, kind: str, data: dict) -> None:
        if kind == "session_start":
            self.session.update(data)
        elif kind == "status":
            self.stats.update({k: v for k, v in data.items()
                               if k in self.stats or k in {
                                   "elapsed_seconds", "frames_read"}})
        elif kind == "frame":
            self.latest_frame = data
            for key in ("processed", "sampled", "accepted", "records",
                        "avg_proc_ms", "advice_count", "advice_done"):
                if key in data:
                    self.stats[key] = data[key]
            if data.get("state"):
                self.table = {
                    "state": data["state"],
                    "frame": {k: v for k, v in data.items()
                              if k not in ("state",)},
                }
        elif kind == "decision":
            self.pending = data
        elif kind == "advice":
            self.pending = None
            self.current_advice = data
            self.advices.appendleft(data)
            if data.get("source") not in (None, "model"):
                self.stats["fallbacks"] = int(self.stats.get("fallbacks", 0)) + 1
        elif kind == "click":
            if self.current_advice is not None:
                click = data.get("click")
                if click:
                    self.current_advice = {**self.current_advice,
                                           "click": click}
        elif kind == "hand":
            self.hands.appendleft(data)
        elif kind == "incident":
            self.stats["incidents"] = int(self.stats.get("incidents", 0)) + 1
        elif kind == "session_end":
            self.ended_at = data.get("ended_at") or _now()
            summary = data.get("summary") or {}
            self.stats.update({k: v for k, v in summary.items()
                               if k in self.stats})

    @staticmethod
    def _offer(q: queue.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except queue.Full:
            # 慢客户端：丢最旧的一条，再放最新的；绝不阻塞发布线程。
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # ── 快照 ────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "type": "snapshot",
                "id": self._event_id,
                "ts": _now(),
                "session": dict(self.session),
                "layout": dict(self.layout),
                "stats": dict(self.stats),
                "frame": self.latest_frame,
                "table": self.table,
                "pending": self.pending,
                "advice": self.current_advice,
                "advices": list(self.advices),
                "hands": list(self.hands),
                "events": list(self.feed),
                "ended_at": self.ended_at,
            }


class _Handler(BaseHTTPRequestHandler):
    server_version = "PokerLiveWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 每个请求一行会刷屏；需要时由 server.log 统一打启动/错误。
        return

    @property
    def _store(self) -> LiveWebStore:
        return self.server.store  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/events":
            self._sse()
            return
        if path == "/api/snapshot":
            self._json(self._store.snapshot())
            return
        if path == "/api/health":
            self._json({"ok": True, "ts": _now()})
            return
        self._static(path)

    # ── 响应 ────────────────────────────────────────────
    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        item = _STATIC.get(path)
        if item is None:
            self.send_error(404)
            return
        name, ctype = item
        try:
            body = (WEB_ROOT / name).read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self) -> None:
        store = self._store
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        q = store.subscribe()
        try:
            self._write_sse("snapshot", store.snapshot())
            while True:
                try:
                    event = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                self._write_sse("message", event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            store.unsubscribe(q)

    def _write_sse(self, event: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        if event == "snapshot":
            head = b"retry: 3000\nid: 0\ndata: "
        else:
            head = f"id: {payload.get('id', 0)}\ndata: ".encode("utf-8")
        self.wfile.write(head + body + b"\n\n")
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LiveWebServer:
    """把 `LiveWebStore` 挂到一个本地 HTTP 端口上。"""

    def __init__(self, *, session: dict, layout: dict,
                 host: str = "127.0.0.1", port: int = 8020,
                 log: Callable[[str], None] | None = None) -> None:
        self.store = LiveWebStore(session=session, layout=layout)
        self._log = log or (lambda _msg: None)
        self._httpd = _Server((host, port), _Handler)
        self._httpd.store = self.store  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="live-web", daemon=True)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def publish(self, kind: str, payload: dict | None = None) -> dict:
        return self.store.publish(kind, payload)

    def close(self) -> None:
        self.store.close()
        # 没 start 过就别调 shutdown：BaseServer.shutdown() 会等一个永远不会
        # 被 serve_forever 置位的 Event，直接卡死退出流程。
        if self._thread.is_alive():
            try:
                self._httpd.shutdown()
            except Exception:  # noqa: BLE001 — 端口已关闭/重复关闭都无妨
                pass
        self._httpd.server_close()
        self._thread.join(timeout=2.0)
