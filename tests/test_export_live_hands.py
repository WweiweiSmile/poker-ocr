#!/usr/bin/env python3
"""tools/export_live_hands.py 的单测：筛选口径、跨会话 uid、坏行容错。

    .venv/bin/python tests/test_export_live_hands.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "export_live_hands.py"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def hand(hand_id: str, *, partial: bool = False, first: str = "preflop",
         hero: tuple[str, ...] = ("Ah", "Kd"),
         actions: bool = True) -> dict:
    street = {
        "street": first,
        "pot_start": 0.0,
        "actions": ([{"seat_id": "hero", "action": "call",
                      "amount": 2.0, "is_hero": True}] if actions else []),
    }
    record = {
        "type": "hand",
        "ts": "2026-01-01T00:00:00+08:00",
        "hand_id": hand_id,
        "hero_cards": list(hero),
        "board": [],
        "streets": [street],
    }
    if partial:
        record["partial"] = True
    return record


def write_log(path: Path) -> None:
    records = [
        {"type": "session_start", "started_at": "2026-01-01T00:00:00+08:00"},
        hand("h1"),                                  # A 完整：保留
        hand("h2", partial=True),                    # B partial：默认丢
        hand("h3", first="flop"),                    # C 中途接管：默认丢
        hand("h4", hero=()),                         # D 没读到手牌：默认保留
        hand("h5", actions=False),                   # E 没有行动：默认保留
        {"type": "session_end", "ended_at": "2026-01-01T01:00:00+08:00"},
        {"type": "session_start", "started_at": "2026-01-01T02:00:00+08:00"},
        hand("h1"),                                  # F 第二个 session 的 h1
        "{ 这不是合法 JSON",                          # 坏行
    ]
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
             for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(log: Path, out: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(log), "-o", str(out), *args],
        capture_output=True, text=True)


def load(path: Path) -> list[dict]:
    return json.loads(path.read_text("utf-8"))


def test_default_filters_and_enriches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log, out = Path(tmp) / "live.states.jsonl", Path(tmp) / "hands.json"
        write_log(log)
        proc = run(log, out)
        check(proc.returncode == 0, f"应正常退出：{proc.stderr}")
        hands = load(out)
        check([h["hand_id"] for h in hands] == ["h1", "h4", "h5", "h1"],
              f"默认应保留 A/D/E/F：{[h['hand_id'] for h in hands]}")
        check(not any(h.get("partial") for h in hands),
              "默认不该包含 partial 手牌")
        check(hands[0]["session"] == 1 and hands[-1]["session"] == 2,
              f"session 编号不对：{[h['session'] for h in hands]}")
        check(hands[0]["uid"] == "s01-h1", f"uid 不对：{hands[0]['uid']}")
        check(hands[-1]["uid"] == "s02-h1",
              f"跨 session 的 h1 要能区分：{hands[-1]['uid']}")
        check(len({h["uid"] for h in hands}) == len(hands),
              "uid 应唯一")
        check(all(h.get("type") == "hand" for h in hands),
              "原字段应保留")
        check("坏数据" in proc.stdout,
              f"坏行应给出警告：{proc.stdout}")


def test_require_hero_and_actions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live.states.jsonl"
        write_log(log)

        out = Path(tmp) / "hero.json"
        proc = run(log, out, "--require-hero-cards")
        check(proc.returncode == 0, f"应正常退出：{proc.stderr}")
        check([h["hand_id"] for h in load(out)] == ["h1", "h5", "h1"],
              f"--require-hero-cards 应只去掉 D：{load(out)}")

        out = Path(tmp) / "actions.json"
        run(log, out, "--require-actions")
        check([h["hand_id"] for h in load(out)] == ["h1", "h4", "h1"],
              f"--require-actions 应只去掉 E：{load(out)}")

        out = Path(tmp) / "both.json"
        run(log, out, "--require-hero-cards", "--require-actions")
        check([h["hand_id"] for h in load(out)] == ["h1", "h1"],
              f"两个条件应同时生效：{load(out)}")


def test_partial_and_mid_hand_flags() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live.states.jsonl"
        write_log(log)

        out = Path(tmp) / "partial.json"
        run(log, out, "--include-partial")
        check([h["hand_id"] for h in load(out)] == ["h1", "h2", "h4", "h5", "h1"],
              f"--include-partial 应加回 B：{load(out)}")

        out = Path(tmp) / "all.json"
        run(log, out, "--include-partial", "--mid-hand")
        ids = [(h["session"], h["hand_id"]) for h in load(out)]
        check(ids == [(1, "h1"), (1, "h2"), (1, "h3"), (1, "h4"), (1, "h5"),
                      (2, "h1")],
              f"两个开关应得到全部 6 手：{ids}")


def test_missing_input_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = run(Path(tmp) / "nope.jsonl", Path(tmp) / "hands.json")
        check(proc.returncode == 1, "输入不存在应返回 1")
        check("找不到日志" in proc.stderr,
              f"应说明找不到日志：{proc.stderr}")


def main() -> int:
    tests = [
        test_default_filters_and_enriches,
        test_require_hero_and_actions,
        test_partial_and_mid_hand_flags,
        test_missing_input_fails,
    ]
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
    print("\n✅ 手牌导出脚本全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
