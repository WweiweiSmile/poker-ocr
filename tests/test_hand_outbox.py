#!/usr/bin/env python3
"""手牌 outbox：一手一文件、幂等、sent/failed 迁移。

    .venv/bin/python tests/test_hand_outbox.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.hand_outbox import HandOutbox, hand_uid  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def test_enqueue_is_idempotent_and_atomic() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        outbox = HandOutbox(tmp)
        record = {"uid": "20261030-abc", "hand_id": "20261030-abc",
                  "schema": "hand.v2", "hero_cards": ["Ah", "Kd"]}
        first = outbox.enqueue(record)
        second = outbox.enqueue({**record, "pot": 12.5})
        check(first == second, "同一 uid 应覆盖同一文件")
        pending = outbox.pending()
        check(len(pending) == 1, f"只应有一个待发送文件：{pending}")
        loaded = outbox.load(pending[0])
        check(loaded["pot"] == 12.5, f"后写的内容应生效：{loaded}")
        check(not list(outbox.pending_dir.glob("*.tmp")), "不应留下 .tmp")


def test_uid_falls_back_to_hand_id() -> None:
    check(hand_uid({"hand_id": "x"}) == "x", "没有 uid 时应退回 hand_id")
    check(hand_uid({"uid": "u", "hand_id": "x"}) == "u", "优先 uid")
    check(hand_uid({}) == "", "都没有时返回空")


def test_mark_sent_and_failed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        outbox = HandOutbox(tmp)
        path = outbox.enqueue({"uid": "a", "hand_id": "a"})
        path.with_suffix(".error").write_text("2\nold", encoding="utf-8")
        sent = outbox.mark_sent(path)
        check(sent.parent.name == "sent" and sent.exists(), "应移到 sent/")
        check(not path.exists(), "原 pending 文件应消失")
        check(not path.with_suffix(".error").exists(),
              "发送成功后应清掉失败计数 sidecar")

        path2 = outbox.enqueue({"uid": "b", "hand_id": "b"})
        failed = outbox.mark_failed(path2, "boom")
        check(failed.parent.name == "failed", "应移到 failed/")
        check(failed.with_suffix(".error").read_text(encoding="utf-8") == "boom",
              "应写 .error")
        check(outbox.retry_failed(["b"]) == 1, "应能把 failed 移回 pending")
        check(outbox.pending()[0].stem == "b", "重试后应回到 pending")


def test_live_writes_complete_hand_to_outbox() -> None:
    """LiveProcessor 只在完整手牌上 enqueue；partial 不写。"""
    from types import SimpleNamespace
    from unittest.mock import patch

    import poker_ocr.live as live
    from poker_ocr.frame_state import FrameState
    from poker_ocr.state_stream import FrameOutcome

    class StubStream:
        def ingest(self, img, frame_id, t, *, extra_signature=None):
            state = FrameState(frame_id=frame_id, table_seats=2,
                               players_in_hand=2, players=[], dealer="a",
                               hero_seat_id="hero", hero_turn=False)
            return FrameOutcome("no_change", state, [], None, 0, 0)

    with tempfile.TemporaryDirectory() as tmp:
        outbox = HandOutbox(tmp)
        writer = SimpleNamespace(rows=[],
                                 write=lambda row: writer.rows.append(row))
        proc = live.LiveProcessor(StubStream(), writer, quiet=True,
                                  hand_outbox=outbox, log=lambda *_: None)
        hand = {"hand_id": "20261030-abc", "table_signature": "20261030-def",
                "table_roster": [{"seat_id": "a", "name": "A"}],
                "hero_cards": ["Ah", "Kd"], "streets": []}
        proc._write_hand(hand, partial=False)
        proc._write_hand({**hand, "hand_id": "20261030-partial"}, partial=True)
        pending = outbox.pending()
        check(len(pending) == 1, f"只应同步完整手牌：{pending}")
        loaded = outbox.load(pending[0])
        check(loaded["schema"] == "hand.v2", f"应带 schema：{loaded}")
        check(loaded["uid"] == "20261030-abc", f"应带 uid：{loaded}")
        check(loaded["table_roster"], f"应带 roster：{loaded}")
        check(writer.rows[0]["uid"] == "20261030-abc",
              f"JSONL 记录也应带 uid：{writer.rows[0]}")


def test_sync_tool_dry_run() -> None:
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        outbox = HandOutbox(tmp)
        outbox.enqueue({"uid": "20261030-abc", "hand_id": "20261030-abc",
                        "hero_cards": ["Ah", "Kd"]})
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "sync_hand_outbox.py"),
             "--outbox", tmp, "--dry-run"],
            capture_output=True, text=True, timeout=30)
        check(proc.returncode == 0, f"dry-run 应成功：{proc.stderr}")
        check("将发送 20261030-abc" in proc.stdout,
              f"应打印待发送手牌：{proc.stdout}")
        check(len(outbox.pending()) == 1, "dry-run 不应移动文件")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)
             and getattr(value, "__module__", "") == __name__]
    for test in tests:
        before = len(FAILS)
        try:
            test()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{test.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {test.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print("\n✅ 手牌 outbox 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
