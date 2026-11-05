#!/usr/bin/env python3
"""手牌识别问题倒查工具：对齐 states/events、评分、explain、导出。

    .venv/bin/python tests/test_hand_recognition_audit.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import hand_recognition_audit as audit  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def write_logs(root: Path) -> tuple[Path, Path]:
    states = root / "live.states.jsonl"
    events = root / "live.events.jsonl"
    state_rows = [
        {"type": "session_start", "session_id": "s-1",
         "started_at": "2026-11-05T22:00:00+08:00"},
        {"type": "state", "index": 1, "seq": 1, "cards_reliable": False,
         "name_warnings": ["名字抖动"]},
        {"type": "advice", "ts": "2026-11-05T22:00:01+08:00",
         "hand_id": "bad1", "street": "flop", "source": "model",
         "option": {"id": "call"}, "reason": "赔率够"},
        {"type": "hand", "ts": "2026-11-05T22:00:10+08:00",
         "hand_id": "bad1", "uid": "s01-bad1",
         "hero_cards": ["Ah"], "board": ["Ks", "7d", "2c"], "pot": 10,
         "hero_position": "BTN", "cards_unreliable": True,
         "cards_warnings": ["board 回退", "hero 只读到 1 张"],
         "streets": [{"street": "preflop", "actions": [
             {"action": "call", "inferred": True},
             {"action": "fold", "inferred": True}]}],
         "data_quality": {"status": "approx",
                          "warnings": ["board 回退", "hero 只读到 1 张"],
                          "inferred_actions": 2,
                          "cards_unreliable": True}},
        {"type": "state", "index": 2, "seq": 2},
        {"type": "hand", "ts": "2026-11-05T22:10:00+08:00",
         "hand_id": "good1", "uid": "s01-good1",
         "hero_cards": ["Ah", "Kd"], "board": ["Ks", "7d", "2c"],
         "pot": 20, "hero_position": "BTN",
         "streets": [{"street": "preflop", "actions": [
             {"action": "raise"}, {"action": "call"}]}],
         "data_quality": {"status": "exact", "warnings": [],
                          "inferred_actions": 0,
                          "cards_unreliable": False}},
    ]
    states.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                for r in state_rows) + "\n", encoding="utf-8")
    event_rows = [
        {"schema": "live.event.v1", "type": "event", "event": "frame",
         "session_id": "s-1", "data": {
             "seq": 1, "kind": "discard", "errors": ["pot:遮挡", "stack:a"],
             "cards": {"reliable": False, "warnings": ["board 回退"]},
             "hand_id": "bad1"}},
        {"schema": "live.event.v1", "type": "event", "event": "frame",
         "session_id": "s-1", "data": {
             "seq": 2, "kind": "discard", "errors": ["dealer"],
             "cards": {"reliable": True}, "hand_id": "bad1"}},
        {"schema": "live.event.v1", "type": "event", "event": "frame",
         "session_id": "s-1", "data": {
             "seq": 3, "kind": "record", "errors": [],
             "cards": {"reliable": True}, "hand_id": "good1"}},
        {"schema": "live.event.v1", "type": "event", "event": "incident",
         "session_id": "s-1", "data": {
             "hand_id": "bad1", "reason": "board 回退",
             "folder": "logs/incidents/x"}},
        {"schema": "live.event.v1", "type": "event", "event": "advice_skip",
         "session_id": "s-1", "data": {
             "hand_id": "bad1", "street": "flop",
             "reason": "cards_unreliable", "warnings": ["board 回退"]}},
    ]
    events.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                for r in event_rows) + "\n", encoding="utf-8")
    return states, events


def test_collect_and_rank() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        states, events = write_logs(root)
        report = audit.collect([states], [events], root / "no_incidents")
        check(report.sessions == 1, f"会话数错：{report.sessions}")
        check(len(report.hands) == 2, f"应有两手：{len(report.hands)}")
        by_id = {h.hand_id: h for h in report.hands}
        bad, good = by_id["bad1"], by_id["good1"]
        check(bad.score > good.score, f"坏手应排前面：{bad.score} vs {good.score}")
        check(bad.severity in ("medium", "high"), f"坏手等级：{bad.severity}")
        check(good.score == 0 and good.severity == "clean",
              f"干净手不应扣分：{good.score} {good.issues}")
        codes = {i.code for i in bad.issues}
        for code in ("approx", "cards_warnings", "inferred_actions",
                     "one_hero", "incidents", "name_warnings",
                     "advice_skip"):
            check(code in codes, f"坏手应检出 {code}：{sorted(codes)}")
        check(bad.discard_count == 2, f"丢弃帧数：{bad.discard_count}")
        check(bad.advice_skips == 1, f"跳过建议数：{bad.advice_skips}")
        check(bad.discard_reasons.get("pot") == 1,
              f"丢弃原因：{bad.discard_reasons}")
        check(len(bad.frames) == 2, "应保留逐帧记录")


def test_explain_and_exports() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        states, events = write_logs(root)
        report = audit.collect([states], [events], root / "none")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = audit.explain(report, "bad1", color=False)
        text = buf.getvalue()
        check(code == 0, "explain 应成功")
        check("s01-bad1" in text and "discard" in text,
              f"explain 应给逐帧：{text[:200]}")
        check("board 回退" in text, "explain 应给牌面警告")

        payload = root / "audit.json"
        audit.write_json(report, payload)
        data = json.loads(payload.read_text(encoding="utf-8"))
        check(data["summary"]["hands"] == 2, "JSON summary")
        check(data["hands"][0]["uid"], "JSON 应带 uid")

        csv_path = root / "audit.csv"
        audit.write_csv(report, csv_path)
        check(csv_path.exists() and "s01-bad1" in csv_path.read_text(
            encoding="utf-8"), "CSV 应含手牌 ID")

        html_path = root / "audit.html"
        audit.write_html(report, html_path)
        html = html_path.read_text(encoding="utf-8")
        check("s01-bad1" in html and "识别质量排查" in html,
              "HTML 应含手牌与标题")


def test_states_only_without_events() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        states, _events = write_logs(root)
        report = audit.collect([states], [], root / "none")
        by_id = {h.hand_id: h for h in report.hands}
        check(by_id["bad1"].score > by_id["good1"].score,
              "没有事件日志时也应按手牌质量排序")
        check(by_id["bad1"].frame_count == 1,
              f"没有事件时用 state 帧数兜底：{by_id['bad1'].frame_count}")


def main() -> int:
    tests = [test_collect_and_rank, test_explain_and_exports,
             test_states_only_without_events]
    print("── 手牌识别倒查 ──────────────────────────────")
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
    print("\n✅ 手牌识别倒查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
