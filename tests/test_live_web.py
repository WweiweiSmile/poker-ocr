#!/usr/bin/env python3
"""实时 Web 看板：内存快照、SSE 首帧、静态页面、profile → 座位坐标。

    .venv/bin/python tests/test_live_web.py

不依赖浏览器、不依赖 live 主循环：只起一个本地端口，验证协议和布局。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.live import build_parser  # noqa: E402
from poker_ocr.live_web import (  # noqa: E402
    LiveWebServer, LiveWebStore, build_layout,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def fake_profile():
    return SimpleNamespace(
        ring_order=["hero", "a", "b"],
        hero_seat_id="hero",
        table_center=(0.5, 0.35),
        reference_size=(982, 2182),
        seats=[
            SimpleNamespace(seat_id="hero",
                            name_slot=(0.40, 0.70, 0.60, 0.72)),
            SimpleNamespace(seat_id="a",
                            name_slot=(0.00, 0.30, 0.20, 0.32)),
            SimpleNamespace(seat_id="b",
                            name_slot=(0.80, 0.30, 1.00, 0.32)),
        ],
    )


def test_cli_defaults() -> None:
    args = build_parser().parse_args([])
    check(args.web is True, "Web 看板默认应开启")
    check(args.web_port == 8020, f"默认端口应 8020：{args.web_port}")
    check(args.web_host == "127.0.0.1", f"默认只绑本机：{args.web_host}")
    check(args.no_events is False, "默认应写事件日志")
    check(args.no_log_file is False, "默认应写控制台文本")
    off = build_parser().parse_args(["--no-web", "--no-events", "--no-log-file"])
    check(off.web is False and off.no_events and off.no_log_file,
          "三个 --no-* 应能关闭")


def test_layout_uses_name_slot_centers() -> None:
    layout = build_layout(fake_profile())
    seats = {s["seat_id"]: s for s in layout["seats"]}
    check(layout["hero_seat_id"] == "hero", "hero 应保留")
    check(seats["hero"]["is_hero"] is True, "hero 标记")
    check(seats["hero"]["center"] == [0.5, 0.71],
          f"hero 坐标应取 name_slot 中心：{seats['hero']}")
    check(seats["a"]["center"] == [0.1, 0.31],
          f"左侧座位坐标错：{seats['a']}")
    check(layout["table_size"] == 3, "桌型人数")


def test_store_tracks_frames_advice_hands() -> None:
    store = LiveWebStore(session={"session_id": "s-1"}, layout={})
    store.publish("frame", {
        "seq": 1, "kind": "record", "processed": 1, "sampled": 1,
        "accepted": 1, "records": 1,
        "state": {"pot": 12, "hero_cards": ["Ah", "Kd"], "opponents": []},
    })
    snap = store.snapshot()
    check(snap["table"]["state"]["pot"] == 12, f"牌桌应更新：{snap['table']}")
    check(snap["stats"]["accepted"] == 1, f"统计应更新：{snap['stats']}")
    check(snap["events"][-1]["type"] == "frame", "事件流应追加 frame")

    store.publish("decision", {"hand_id": "h1", "street": "flop",
                               "pot": 12, "to_call": 4, "options": [1, 2]})
    check(store.snapshot()["pending"]["hand_id"] == "h1", "决策点应挂起")

    store.publish("advice", {"hand_id": "h1", "street": "flop",
                             "source": "model", "option": {"label": "跟注"},
                             "reason": "赔率够"})
    snap = store.snapshot()
    check(snap["pending"] is None, "建议返回后应清掉 pending")
    check(snap["advice"]["reason"] == "赔率够", "当前建议")
    check(snap["advices"][0]["hand_id"] == "h1", "建议历史")

    store.publish("advice", {"hand_id": "h2", "street": "turn",
                             "source": "fallback", "reason": "超时"})
    check(store.snapshot()["stats"]["fallbacks"] == 1, "兜底应计数")

    store.publish("hand", {"hand_id": "h1",
                           "data_quality": {"status": "exact"}})
    check(store.snapshot()["hands"][0]["hand_id"] == "h1", "手牌历史")


def test_cards_event_updates_table_without_a_state_record() -> None:
    """质量闸丢帧时看板只收到 `cards`：牌要马上换，底池等下一份有效状态。

    2026-11-08 实盘：换手后连续 112 帧因 bet_gt_pot 被丢，看板 40s 一直
    挂着上一手的手牌。牌是另一道闸确认过的，不该被底池的一票否决连坐。
    """
    store = LiveWebStore(session={"session_id": "s-1"}, layout={})
    store.publish("frame", {
        "seq": 1, "kind": "record",
        "state": {"pot": 12, "hero_cards": ["Ah", "Kd"], "board": [],
                  "opponents": []},
    })
    store.publish("cards", {"hero_cards": ["Qc", "Qh"], "board": []})
    snap = store.snapshot()
    state = snap["table"]["state"]
    check(state["hero_cards"] == ["Qc", "Qh"],
          f"牌面应立刻换成新读数：{state}")
    check(state["pot"] == 12,
          f"底池仍是上一份有效记录的值：{state}")
    check(state["cards_reliable"] is True, f"牌面应标记为可信：{state}")
    check(snap["cards"]["hero_cards"] == ["Qc", "Qh"],
          f"快照应带最近牌面：{snap['cards']}")
    check(snap["events"][-1]["type"] == "cards", "事件流应追加 cards")


def test_http_snapshot_static_and_sse() -> None:
    server = LiveWebServer(session={"session_id": "s-1", "profile": "p.json"},
                           layout=build_layout(fake_profile()), port=0)
    server.start()
    try:
        base = server.url
        with urllib.request.urlopen(base + "/api/snapshot", timeout=3) as resp:
            snap = json.loads(resp.read().decode("utf-8"))
        check(snap["session"]["session_id"] == "s-1", "快照应带会话信息")
        check(snap["layout"]["table_size"] == 3, "快照应带布局")

        page = urllib.request.urlopen(base + "/", timeout=3).read().decode()
        check("POKER LIVE" in page, "首页应可访问")
        check('id="bet-strip"' in page, "底池旁应有本街下注汇总")
        check('class="dock-tab' in page, "右栏辅助信息应使用 tab 布局")
        js = urllib.request.urlopen(base + "/app.js", timeout=3).read().decode()
        check("EventSource" in js, "前端应通过 SSE 连接")

        server.publish("frame", {"seq": 9, "kind": "record",
                                 "state": {"pot": 3}, "processed": 1})
        with urllib.request.urlopen(base + "/api/snapshot", timeout=3) as resp:
            snap = json.loads(resp.read().decode("utf-8"))
        check(snap["table"]["state"]["pot"] == 3, "发布后快照应更新")

        # SSE 首帧必须是 snapshot；读到 data: 就关连接，避免测试挂住。
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        payload = None
        for _ in range(20):
            line = resp.fp.readline().decode("utf-8", "replace").strip()
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                break
        conn.close()
        check(payload is not None and payload.get("type") == "snapshot",
              f"SSE 首帧应是 snapshot：{payload}")
    finally:
        server.close()


def main() -> int:
    tests = [test_cli_defaults, test_layout_uses_name_slot_centers,
             test_store_tracks_frames_advice_hands,
        test_cards_event_updates_table_without_a_state_record,
             test_http_snapshot_static_and_sse]
    print("── 实时 Web 看板 ──────────────────────────────")
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
    print("\n✅ 实时 Web 看板全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
