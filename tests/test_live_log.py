#!/usr/bin/env python3
"""实时日志底座：结构化事件 JSONL + 控制台文本落盘。

    nv tests/test_live_log.py

不启动 live / 不读摄像头：只验证落盘格式、压缩规则和派生路径。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.live_log import (  # noqa: E402
    ConsoleLog, LiveEventLog, derive_console_path, derive_event_path,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8")
            .splitlines() if line.strip()]


def test_derive_paths() -> None:
    out = Path("logs/live.states.jsonl")
    check(derive_event_path(out) == Path("logs/live.events.jsonl"),
          f"events 派生错：{derive_event_path(out)}")
    check(derive_console_path(out) == Path("logs/live.log"),
          f"console 派生错：{derive_console_path(out)}")
    check(derive_event_path(Path("x.jsonl")) == Path("x.events.jsonl"),
          "普通 jsonl 也应派生")
    check(derive_console_path(Path("x.bin")) == Path("x.bin.log"),
          "非 jsonl 应加后缀")


def test_console_log_strips_ansi_and_appends() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "live.log"
        log = ConsoleLog(path, append=True)
        log.write("\033[1;31m[警告] 测试\033[0m")
        log.close()
        text = path.read_text(encoding="utf-8")
        check("[警告] 测试" in text, f"应保留正文：{text!r}")
        check("\033[" not in text, f"应去掉 ANSI：{text!r}")
        check(text[:4].isdigit(), f"应带墙钟时间前缀：{text!r}")
        # 追加而不是覆盖
        log = ConsoleLog(path, append=True)
        log.write("第二行")
        log.close()
        check(len(path.read_text(encoding="utf-8").splitlines()) == 2,
              "追加模式应保留旧行")


def test_event_log_compacts_duplicates() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "live.events.jsonl"
        log = LiveEventLog(path, session_id="s-1", append=False)
        log.write("frame", {
            "seq": 7, "kind": "record",
            "state": {"index": 3, "pot": 10},
            "hand": {"hand_id": "h1", "actions": [{"action": "call"}]},
            "cards": {"reliable": True},
        })
        log.write("advice", {
            "hand_id": "h1", "street": "flop", "source": "model",
            "prompt_system": "S" * 10, "prompt_user": "U" * 5,
            "prompt_sha256": "abc123", "raw_response": "{}",
            "facts": {"pot": 10}, "context": {"x": 1},
            "reason": "因为",
        })
        log.write("hand", {"hand_id": "h1", "advices": [{"a": 1}, {"a": 2}],
                           "data_quality": {"status": "exact"}})
        log.close()

        rows = read_lines(path)
        check([r["event_seq"] for r in rows] == [1, 2, 3],
              f"event_seq 应连续：{[r['event_seq'] for r in rows]}")
        check(all(r["schema"] == "live.event.v1" for r in rows),
              "schema 应固定")
        check(all(r["session_id"] == "s-1" for r in rows), "session_id 应写入")

        frame = rows[0]["data"]
        check("state" not in frame and "hand" not in frame,
              f"frame 不应重复 state/hand：{frame}")
        check(frame.get("state_index") == 3, f"应补 state_index：{frame}")

        advice = rows[1]["data"]
        check("prompt_system" not in advice and "prompt_user" not in advice,
              f"advice 不应重复 prompt 原文：{advice}")
        check("raw_response" not in advice and "facts" not in advice,
              "advice 不应重复 raw/facts")
        check(advice.get("prompt_chars") == 15,
              f"prompt_chars 应为 15：{advice}")
        check(advice.get("prompt_sha256") == "abc123",
              "prompt_sha256 应保留（排查用）")

        hand = rows[2]["data"]
        check("advices" not in hand, "hand 不应重复 advices")
        check(hand.get("advice_count") == 2, f"应补 advice_count：{hand}")


def main() -> int:
    tests = [test_derive_paths, test_console_log_strips_ansi_and_appends,
             test_event_log_compacts_duplicates]
    print("── live 日志底座 ──────────────────────────────")
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ live 日志底座全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
