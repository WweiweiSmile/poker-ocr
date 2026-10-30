"""手牌 outbox：把**已结束**的手牌记录落到本地队列，等手牌库来取。

设计口径：

  · **一手一个文件**（`pending/<uid>.json`），而不是一个不断增长的 JSONL。
    好处是失败只影响一手、重试幂等、人工可以直接打开看；坏处是文件多，
    但一天几十/几百手完全不是问题。
  · **原子写**：先写 `.tmp` 再 `os.replace`，手牌库不会读到半截 JSON。
  · **发送成功才移到 `sent/`**；失败留在 `pending/`，超过重试次数进 `failed/`
    并带 `.error`，避免把队列堵死。
  · live 不直接写 MySQL：手牌库离线时打牌不受影响，恢复后补发。

用法：

    outbox = HandOutbox("data/hand_outbox")
    outbox.enqueue(hand_record)          # 手结束、记录落 JSONL 之后
    for path in outbox.pending():        # tools/sync_hand_outbox.py
        ... POST /api/hands/ingest ...
        outbox.mark_sent(path)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_OUTBOX_DIR = Path("data/hand_outbox")
_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


def hand_uid(record: dict) -> str:
    """队列键：优先 `uid`，否则 `hand_id`。新记录两者通常相同。"""
    uid = str(record.get("uid") or "").strip()
    if uid:
        return uid
    return str(record.get("hand_id") or "").strip()


def _filename(uid: str) -> str:
    safe = _SAFE.sub("-", uid).strip("-")[:120]
    if not safe:
        safe = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
    return f"{safe}.json"


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层不是对象")
    return data


@dataclass
class OutboxStats:
    pending: int = 0
    sent: int = 0
    failed: int = 0


class HandOutbox:
    """本地手牌队列。`enabled=False` 时所有写操作都是空操作。"""

    def __init__(self, root: str | Path = DEFAULT_OUTBOX_DIR, *,
                 enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = bool(enabled)
        self.pending_dir = self.root / "pending"
        self.sent_dir = self.root / "sent"
        self.failed_dir = self.root / "failed"

    # ── 写 ──
    def enqueue(self, record: dict) -> Path | None:
        """写入一手完整记录；同一 uid 重复调用是幂等覆盖。"""
        if not self.enabled or not isinstance(record, dict):
            return None
        uid = hand_uid(record)
        if not uid:
            return None
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        path = self.pending_dir / _filename(uid)
        tmp = path.with_suffix(".json.tmp")
        payload = {**record, "uid": uid}
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return path

    # ── 读 ──
    def pending(self) -> list[Path]:
        if not self.enabled or not self.pending_dir.is_dir():
            return []
        return sorted(self.pending_dir.glob("*.json"))

    def load(self, path: Path) -> dict:
        return _read_json(path)

    def stats(self) -> OutboxStats:
        def count(directory: Path) -> int:
            if not directory.is_dir():
                return 0
            return sum(1 for path in directory.glob("*.json") if path.is_file())
        return OutboxStats(pending=count(self.pending_dir),
                           sent=count(self.sent_dir),
                           failed=count(self.failed_dir))

    # ── 状态迁移 ──
    def mark_sent(self, path: Path) -> Path:
        self.sent_dir.mkdir(parents=True, exist_ok=True)
        target = self.sent_dir / Path(path).name
        Path(path).with_suffix(".error").unlink(missing_ok=True)
        os.replace(path, target)
        return target

    def mark_failed(self, path: Path, error: str) -> Path:
        """把一手移到 failed/，并写同名 `.error` 文本；不删除原记录。"""
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        target = self.failed_dir / Path(path).name
        os.replace(path, target)
        target.with_suffix(".error").write_text(
            str(error)[:4000], encoding="utf-8")
        return target

    def retry_failed(self, uids: Iterable[str] | None = None) -> int:
        """把 failed/ 里的记录移回 pending/（可选只重试指定 uid）。"""
        if not self.failed_dir.is_dir():
            return 0
        wanted = {str(uid) for uid in (uids or ())}
        moved = 0
        for path in sorted(self.failed_dir.glob("*.json")):
            if wanted and path.stem not in wanted:
                continue
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            os.replace(path, self.pending_dir / path.name)
            path.with_suffix(".error").unlink(missing_ok=True)
            moved += 1
        return moved
