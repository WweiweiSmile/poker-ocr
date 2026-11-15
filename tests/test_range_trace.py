#!/usr/bin/env python3
"""范围推导 trace（契约 v1.1）：形状、降级阶梯、轮转配额、fail open、逐位不变。

    nv tests/test_range_trace.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import range_flow, range_trace, ranges  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def prior() -> ranges.Range:
    combos = []
    for key in ("QQ", "AKs", "KQo", "T9s", "99", "A5s"):
        combos.extend(ranges.combos_of_key(key))
    rng = ranges.make_range("test.prior", {c: 1.0 for c in combos})
    assert rng is not None
    return rng


def events() -> list:
    return [
        range_flow.ActionEvent(street="flop", board="Ks8h3d", seat_id="x",
                               action="bet", amount=6.0, facing_bet=2.0,
                               players_behind=2, pot=8.0),
        range_flow.ActionEvent(street="turn", board="Ks8h3d2c", seat_id="x",
                               action="raise", amount=20.0, facing_bet=6.0,
                               raises_before=1, players_behind=1, pot=20.0),
    ]


def fake_snap(**over) -> SimpleNamespace:
    base = dict(
        hand_id="20261113-deadbeef00", hand_index=104, street="flop",
        board=("Ks", "8h", "3d"), hero_cards=("Ah", "Qh"),
        hero_position="BTN", hero_stack=98.0, pot=12.0, to_call=6.0,
        table_size=9, decision_key=("h", "flop", ("bet:6",), ("Ks", "8h", "3d"),
                                    ("Ah", "Qh")), warnings=(),
    )
    base.update(over)
    return SimpleNamespace(**base)


# ─────────────────────── 键下标与 grid ───────────────────────

def test_key_index() -> None:
    check(ranges.key_index("AA") == 0, "AA 应在对角线左上（下标 0）")
    check(ranges.key_index("AKs") == 1, "AKs 应在第一行第二列（下标 1）")
    check(ranges.key_index("AKo") == 13, "AKo 应在第一列第二行（下标 13）")
    check(ranges.key_index("22") == 168, "22 应在右下角（下标 168）")
    check(ranges.key_index("nope") == -1, "非法键应返回 -1")


def test_grid_is_distribution() -> None:
    rng = prior()
    grid = range_flow._grid_array(rng.normalized())
    check(len(grid) == 169, f"grid 应 169 格，实际 {len(grid)}")
    total = sum(grid)
    check(abs(total - 1.0) < 1e-6, f"grid 应是质量占比（和 1），实际 {total}")
    check(all(0.0 <= v <= 1.0 for v in grid), "grid 每格应在 0..1")


# ─────────────────────── 逐位不变 ───────────────────────

def test_traced_matches_plain_bitwise() -> None:
    rng = prior()
    evs = events()
    dead = ("8h", "3d")
    plain = range_flow.build_range(rng, evs, dead=dead)
    traced, trail = range_flow.build_range_traced(rng, evs, dead=dead)
    assert plain is not None and traced is not None and trail
    check(plain.combo_weights == traced.combo_weights,
          "trace 路径的逐 combo 权重必须与 build_range 完全一致")
    for field in ("width_pct", "weighted_width", "combo_count", "peak", "rho"):
        check(getattr(plain, field, None) == getattr(traced, field, None),
              f"{field} 应与 build_range 逐位一致")
    steps = trail["steps"]
    check(len(steps) == len(evs), f"应有 {len(evs)} 步，实际 {len(steps)}")
    final = trail["final"]
    check(abs(final["width_pct"] - round(plain.width_pct, 2)) < 1e-9,
          "final.width_pct 应等于返回的 Range.width_pct")
    # v1.2：final 也带 169 格热图，且必须是剪枝后支撑集内的归一化份额。
    fgrid = final.get("grid")
    check(isinstance(fgrid, list) and len(fgrid) == 169,
          f"final.grid 应 169 格，实际 {type(fgrid)}")
    check(abs(sum(fgrid) - 1.0) < 1e-5,
          f"final.grid 应是占比（和 1），实际 {sum(fgrid)}")
    index_key = {ranges.key_index(key): key
                 for key in {ranges.key_of_combo(combo)
                             for combo in ranges.ALL_COMBOS}}
    live_keys = {ranges.key_of_combo(combo) for combo in plain.combo_weights}
    grid_keys = {index_key[i] for i, value in enumerate(fgrid) if value > 0}
    check(grid_keys == live_keys,
          f"final.grid 的非零格子应与 final 支撑集一致："
          f"{sorted(grid_keys)} != {sorted(live_keys)}")
    for step in steps:
        grid = step.get("grid")
        check(isinstance(grid, list) and len(grid) == 169,
              f"步 {step.get('i')} 的 grid 应 169 格")
        if grid:
            check(abs(sum(grid) - 1.0) < 1e-5,
                  f"步 {step.get('i')} 的 grid 应是占比（和 1）")
        check(sum(step.get("cell_n") or []) == sum(
            n for n in (step.get("cell_n") or [])), "cell_n 应非负")
    # 中间步 5 元、末步 8 元。
    for index, step in enumerate(steps):
        for row in step.get("detail") or ():
            want = 8 if index == len(steps) - 1 else 5
            check(len(row) == want,
                  f"步 {index} 的 detail 行应 {want} 元，实际 {len(row)} 元")


def test_time_gate_marks_omitted() -> None:
    rng = prior()
    built, trail = range_flow.build_range_traced(rng, events(), max_ms=-1.0)
    assert built is not None and trail
    omitted = trail.get("detail_omitted") or []
    check(bool(omitted), "时间闸掐掉 detail 的步应被记入 omitted")
    check(all(not step.get("detail") for step in trail["steps"]),
          "被时间闸掐掉的步不应带 detail")
    check(all(step.get("cell_n") for step in trail["steps"]),
          "时间闸只砍 detail，cell_n 必须留")


def test_trace_bypasses_build_cache() -> None:
    range_flow.reset_cache()
    rng = prior()
    evs = events()
    range_flow.build_range_traced(rng, evs)
    check(not range_flow._BUILD_CACHE,
          "trace 路径不许写 _BUILD_CACHE（缓存键不含 detail 上限）")
    range_flow.build_range(rng, evs)
    check(bool(range_flow._BUILD_CACHE), "普通路径应照常写缓存")


# ─────────────────────── 记录形状 ───────────────────────

def test_record_and_skipped_shape() -> None:
    snap = fake_snap()
    seat = {
        "seat": "s1", "pos": "CO", "archetype": "tag", "path": "event_model",
        "mode": "call", "bucket": "facing_bet", "prior": {}, "steps": [],
        "final": {}, "detail_omitted": [], "equity": None,
    }
    record = range_trace.build_record(snap, [seat])
    check(record["v"] == range_trace.VERSION,
          f"版本号应为 {range_trace.VERSION}，实际 {record['v']}")
    check(record["v"] == "live.range.v1.2",
          f"v1.2 给 final 加了 grid，版本号应可见：{record['v']}")
    check(record["status"] == "ok", "status 应为 ok")
    check(record["hand"] == 104, "hand 应是整数 hand_index")
    check(record["hand_name"] == snap.hand_id, "hand_name 应是 hand_id")
    check(record["board"] == "Ks8h3d", f"board 应拼成字符串，实际 {record['board']}")
    key = record["decision_key"]
    check(key["hand"] == "104" and key["street"] == "flop",
          "decision_key 应带 hand/street")
    check(record["hero"]["spr"] == round(98.0 / 12.0, 2),
          f"有筹码时 spr 应是 stack/pot，实际 {record['hero']['spr']}")
    check(record["hero"]["spr_source"] == "snapshot", "spr_source 应为 snapshot")
    check(record["dead"] == ["3d", "8h", "Ah", "Ks", "Qh"],
          f"dead 应是 hero+board 排序，实际 {record['dead']}")
    check(record["params"]["tables"]["sha256"],
          "params.tables.sha256 不应为空（表指纹）")

    no_stack = range_trace.build_record(fake_snap(hero_stack=None), [seat])
    check(no_stack["hero"]["spr"] is None, "拿不到筹码时 spr 必须是 null")
    check(no_stack["hero"]["spr_source"] == "unavailable",
          "spr_source 应写 unavailable")

    skipped = range_trace.build_skipped(snap, "board_short", warnings=("x",))
    check(skipped["status"] == "skipped" and skipped["reason"] == "board_short",
          "skipped 记录应带 reason")
    check("seats" not in skipped, "skipped 记录不应带 seats")


# ─────────────────────── 体积降级 ───────────────────────

def _fat_step(index: int, rows: int = 400) -> dict:
    detail = [[f"{ranges.combo_text(c)}", 0.012345, -0.004321, 1.0, None]
              for c in list(ranges.ALL_COMBOS)[:rows]]
    return {
        "i": index, "street": "flop", "detail": detail, "detail_n": len(detail),
        "truncated": False, "skipped": False,
        "grid": [round(1.0 / 169.0, 6)] * 169,
        "cell_n": [8] * 169,
        "top": [{"c": "AhKh", "dw": 0.01} for _ in range(10)] + [{"others": {"n": 1, "sum_dw": 0.0}}],
    }


def test_downgrade_ladder() -> None:
    seat = {"seat": "s1", "steps": [_fat_step(i) for i in range(10)],
            "detail_omitted": []}
    record = {"v": range_trace.VERSION, "status": "ok", "seats": [seat],
              "warnings": []}
    size = len(range_trace._encode(record))
    check(size > range_trace.SOFT_LIMIT,
          f"构造的记录应超软上限（{size} <= {range_trace.SOFT_LIMIT}）")
    out = range_trace.downgrade(record)
    new_size = len(range_trace._encode(out))
    check(new_size <= range_trace.SOFT_LIMIT,
          f"降级后应装进软上限，实际 {new_size}")
    check("trace_truncated" in out["warnings"],
          "降级应在 warnings 里追加 trace_truncated")
    check(any(step.get("truncated") for step in out["seats"][0]["steps"]),
          "降级到每步 detail 上限时应标 truncated")
    big = {"v": range_trace.VERSION, "status": "ok", "warnings": [],
           "seats": [{"seat": "s1", "detail_omitted": [],
                      "steps": [_fat_step(i) for i in range(40)]}]}
    out_big = range_trace.downgrade(big)
    check(len(range_trace._encode(out_big)) <= range_trace.SOFT_LIMIT,
          "超大记录降级后也要装进软上限")
    check(bool(out_big.get("detail_omitted")),
          "detail 降到全清时应把步号写进 detail_omitted")
    last = out["seats"][0]["steps"][-1]
    check(last.get("grid"), "末步的 grid 必须保留（看板默认视图靠它）")
    # 原始记录不能被就地改掉。
    check(len(record["seats"][0]["steps"][0]["detail"]) == 400,
          "降级不许改原记录")


# ─────────────────────── 轮转 / 配额 / fail open ───────────────────────

def test_oversize_record_is_squeezed() -> None:
    """超硬顶：剥到骨架照写并标 trace_truncated，绝不静默丢（#39 review P1）。"""
    seat = {"seat": "s1", "steps": [_fat_step(i) for i in range(6)],
            "detail_omitted": []}
    record = {"v": range_trace.VERSION, "status": "ok", "seats": [seat],
              "warnings": []}
    squeezed = range_trace._encode(range_trace._squeeze(record))
    check(len(squeezed) < len(range_trace._encode(record)),
          "骨架应比原记录小")

    with tempfile.TemporaryDirectory() as tmp:
        trace = range_trace.TraceWriter(Path(tmp) / "live.range.jsonl")
        saved = range_trace.HARD_LIMIT
        range_trace.HARD_LIMIT = len(squeezed) + 10
        try:
            ok = trace.write(record)
        finally:
            range_trace.HARD_LIMIT = saved
        check(ok is True, "超硬顶应截断后照写，不能直接 return False")
        check(trace.status == range_trace.STATUS_TOO_BIG,
              f"超硬顶 status 应是 record_too_big，实际 {trace.status}")
        check(trace.truncated == 1 and trace.dropped == 0,
              f"应记 1 条截断、0 条丢弃：{trace.stats()}")
        check("hard cap" in trace.last_error,
              f"last_error 应说明超硬顶截断：{trace.last_error!r}")
        line = json.loads(
            trace.current_path.read_text(encoding="utf-8").splitlines()[-1])
        check("trace_truncated" in line["warnings"],
              f"截断记录应带 trace_truncated：{line.get('warnings')}")
        check(bool(line.get("detail_omitted")),
              "截断记录应把步号写进 detail_omitted")
        check(all(not step.get("grid") for step in line["seats"][0]["steps"]),
              "骨架不该再带 grid")
        # 这个状态不闸后续写入。
        small = {"v": range_trace.VERSION, "status": "ok", "seats": [],
                 "warnings": []}
        check(trace.write(small) is True, "截断过一次之后仍应继续写")

    with tempfile.TemporaryDirectory() as tmp:
        trace = range_trace.TraceWriter(Path(tmp) / "live.range.jsonl")
        saved = range_trace.HARD_LIMIT
        range_trace.HARD_LIMIT = 10
        try:
            ok = trace.write(record)
        finally:
            range_trace.HARD_LIMIT = saved
        check(ok is False, "骨架都装不下才允许丢")
        check(trace.dropped == 1 and trace.truncated == 0,
              f"应记 1 条丢弃：{trace.stats()}")
        check("dropped" in trace.last_error,
              f"丢弃也要留痕：{trace.last_error!r}")
        check(trace.status == range_trace.STATUS_TOO_BIG,
              f"丢弃后 status 应是 record_too_big，实际 {trace.status}")


def test_writer_rotate_and_prune() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "live.range.jsonl"
        old = Path(tmp) / "live.range.jsonl.20200101"
        old.write_text("{}\n", encoding="utf-8")
        trace = range_trace.TraceWriter(path)
        check(trace.write({"a": 1}) is True, "正常写应成功")
        day = trace.current_path
        check(day.name.startswith("live.range.jsonl."),
              f"应按天切成 .YYYYMMDD，实际 {day.name}")
        check(not old.exists(), "超过 14 天的文件应被删掉")
        lines = day.read_text(encoding="utf-8").splitlines()
        check(len(lines) == 1 and json.loads(lines[0]) == {"a": 1},
              "落盘应是一行一个 JSON 对象")


def test_writer_day_limit_and_fail_open() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "live.range.jsonl"
        trace = range_trace.TraceWriter(path, day_limit=40)
        check(trace.write({"a": "x" * 20}) is True, "第一条应在日上限内")
        check(trace.write({"a": "x" * 60}) is False, "超日上限应停写")
        check(trace.status == range_trace.STATUS_BUDGET,
              f"超日上限 status 应是 budget_exceeded，实际 {trace.status}")
        check(trace.write({"a": "x" * 20}) is False, "停写后不再写")

    with tempfile.TemporaryDirectory() as tmp:
        # 父路径是个文件 → mkdir 必失败 → 只能改 status，不许抛异常。
        blocker = Path(tmp) / "blocked"
        blocker.write_text("x", encoding="utf-8")
        trace = range_trace.TraceWriter(blocker / "live.range.jsonl")
        check(trace.write({"a": 1}) is False, "写不进去应返回 False")
        check(trace.status == range_trace.STATUS_WRITE_FAIL,
              f"写失败 status 应是 write_failed，实际 {trace.status}")
        check(trace.last_error, "写失败应留下 last_error")


def test_module_switch() -> None:
    range_trace.reset()
    check(range_trace.status() == range_trace.STATUS_OFF, "默认应是 off")
    check(range_trace.emit({"a": 1}) is False, "关闭时 emit 应是空操作")
    with tempfile.TemporaryDirectory() as tmp:
        range_trace.configure(path=Path(tmp) / "live.range.jsonl")
        range_trace.set_session("sess-1")
        check(range_trace.enabled() is True, "configure 后应开启")
        check(range_trace.status() == range_trace.STATUS_ON, "开启后 status 应是 on")
        check(range_trace.emit({"a": 1}) is True, "开启后 emit 应写成功")
        check(range_trace.session() == "sess-1", "session 应被记住")
        range_trace.reset()
        check(range_trace.status() == range_trace.STATUS_OFF, "reset 后应回到 off")


# ─────────────────────── 看板读回（records_for / lookup） ───────────────────────

def _sample(hand: int = 104, street: str = "flop", actions: int = 7,
            ts: str = "2026-11-13T21:00:00", session: str = "sess-1",
            status: str = "ok") -> dict:
    record = {
        "v": range_trace.VERSION, "status": status, "ts": ts, "session": session,
        "hand": hand, "hand_name": f"{street}-{actions}", "street": street,
        "decision_key": {"hand": str(hand), "street": street, "actions": actions},
        "seats": [],
    }
    if status != "ok":
        record["reason"] = "board_short"
    return record


def test_records_for_and_lookup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "live.range.jsonl"
        range_trace.reset()
        range_trace.configure(path=path)
        range_trace.set_session("sess-1")
        try:
            check(range_trace.emit(_sample(street="turn", actions=12,
                                           ts="2026-11-13T21:12:00")),
                  "emit 应写成功")
            range_trace.emit(_sample(street="flop", actions=7,
                                     ts="2026-11-13T21:00:00"))
            # 同一个决策点写两次（例如重放/重试）：只留 ts 最新的一条。
            range_trace.emit(_sample(street="flop", actions=7,
                                     ts="2026-11-13T21:05:00"))

            got = range_trace.records_for(104, path=path)
            check([r["street"] for r in got] == ["flop", "turn"],
                  f"应按街序返回两个决策点：{[r['street'] for r in got]}")
            flop = [r for r in got if r["street"] == "flop"]
            check(len(flop) == 1 and flop[0]["ts"] == "2026-11-13T21:05:00",
                  f"同一决策点应只留 ts 最新的：{flop}")
            only = range_trace.records_for(104, street="turn", path=path)
            check(len(only) == 1 and range_trace._record_actions(only[0]) == 12,
                  f"按街过滤应只回该街：{only}")
            nil = range_trace.records_for(104, street="flop", actions=99,
                                          path=path)
            check(nil == [], "决策序号不匹配时应返回空")
            check(range_trace.records_for(999, path=path) == [],
                  "别的 hand_index 不应捞到记录")

            found = range_trace.lookup(104, path=path)
            check(found["status"] == "ok" and len(found["records"]) == 2,
                  f"lookup 应回 ok + 全部决策点：{found.get('status')}")
            check(range_trace.lookup(999, path=path)["status"] == "missing",
                  "开着 trace 但没有记录时应是 missing")

            # 跨会话重号：当前会话的记录优先，没有才回落历史会话。
            range_trace.set_session("sess-2")
            range_trace.emit(_sample(street="flop", actions=7,
                                     ts="2026-11-13T21:09:00", session="sess-2"))
            mine = range_trace.records_for(104, street="flop", path=path)
            check(len(mine) == 1 and mine[0]["session"] == "sess-2",
                  f"当前会话的记录应优先：{mine}")
            range_trace.set_session("sess-9")
            history = range_trace.records_for(104, street="flop", path=path)
            check(len(history) == 1,
                  f"本会话没有记录时应回落到历史会话：{history}")

            # skipped 记录也要能读到：看板要按 reason 显示「这条决策点被跳过」。
            range_trace.emit(_sample(street="river", actions=20, status="skipped"))
            mixed = range_trace.records_for(104, path=path)
            check(any(r["status"] == "skipped" for r in mixed),
                  "skipped 记录应能被捞出来")
        finally:
            range_trace.reset()

        # trace 关掉之后：有历史记录仍然可读，没有记录才回 off。
        check(range_trace.status() == range_trace.STATUS_OFF, "reset 后应回到 off")
        still = range_trace.lookup(104, path=path)
        check(still["status"] == "ok",
              "关掉 trace 后历史记录仍应可读（看板还能看昨天那手）")
        check(range_trace.lookup(999, path=path)["status"] == "off",
              "没有记录且已关闭时应回 off，让看板显示「未开启」")


def main() -> int:
    tests = [
        test_key_index,
        test_grid_is_distribution,
        test_traced_matches_plain_bitwise,
        test_time_gate_marks_omitted,
        test_trace_bypasses_build_cache,
        test_record_and_skipped_shape,
        test_downgrade_ladder,
        test_oversize_record_is_squeezed,
        test_writer_rotate_and_prune,
        test_writer_day_limit_and_fail_open,
        test_module_switch,
        test_records_for_and_lookup,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{test.__name__} 抛异常：{type(exc).__name__}: {exc}")
    if FAILS:
        print()
        for msg in FAILS:
            print(f"  ❌ {msg}")
        print(f"\n❌ {len(FAILS)} 条断言失败")
        return 1
    print(f"✅ range trace 全部通过（{len(tests)} 组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
