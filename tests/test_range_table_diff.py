#!/usr/bin/env python3
"""换表重算 + diff：纯函数 `compare` / `build_report` / 过滤 / CLI 冒烟。

    nv tests/test_range_table_diff.py

验收口径来自 issue #36（规格 #33 C 段）：

  · 有差异能定位（首个分歧步骤、最大有效宽度差、top combo 位移）；
  · 无差异干净返回；
  · 记录不是这两版表产出时 `reproducible=false` + `reasons`，**仍然出 diff**；
  · 重建失败只进 `errors`（退出码 1），不抛裸栈；
  · `--hand` / `--street` / `--decision-key` 过滤，同决策点只留最新。

比较逻辑是纯函数（不 spawn、不读文件），所以除了最后一条 CLI 冒烟，
其余都在本进程里用构造载荷测。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.range_table_diff import (  # noqa: E402
    build_report, compare, exit_code, load_records,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


SHA = "ee9744743b7dc155"
PRIOR = {"id": "RFI_BTN", "version": 1, "source": "named:RFI_BTN"}


# ─────────────────────────── 构造载荷 ───────────────────────────

def tables(profile: str = "hand-tuned", variant: str = "v0",
           sha: str = SHA) -> dict:
    return {"profile": profile, "variant": variant, "sha256": sha}


def meta(*, hand="104", street="flop", actions=("bet:6",),
         ident: dict | None = None, prior: dict | None = None) -> dict:
    return {
        "hand": hand,
        "street": street,
        "board": "Ks8h3d",
        "decision_key": {"hand": str(hand), "street": street,
                         "actions": list(actions), "pot": 12.0},
        "params": {"alpha_in": 0.6, "support_floor": 0.002,
                   "tables": ident or tables()},
        "seats": [{"seat": "s1", "prior": prior or PRIOR}],
    }


def step(i: int, label: str, before: float, after: float,
         top: list[dict]) -> dict:
    return {"i": i, "street": label.split(":")[0], "label": label,
            "width_before": before, "width_after": after, "top": list(top)}


def final(width=34.0, eff=20.0, peak=0.01, combos=400,
          dropped: dict | None = None) -> dict:
    return {"width_pct": width, "weighted_width": 0.5,
            "effective_width_pct": eff, "peak": peak, "rho": 0.99,
            "combo_count": combos,
            "dropped": dropped or {"dead": 6, "zero": 0, "floor": 3}}


def seat(steps: list[dict], end: dict, top: list[dict] | None = None,
         error: str | None = None, prior: dict | None = None,
         seat_id: str = "s1") -> dict:
    return {"seat": seat_id, "pos": "CO", "archetype": "tag",
            "path": "event_model", "mode": "call",
            "bucket": "facing_bet_closing", "prior": prior or PRIOR,
            "steps": steps, "final": end, "top": list(top or ()),
            "error": error}


def payload(seats: list[dict], ident: dict | None = None,
            error: str | None = None) -> dict:
    """worker 形态（一个 profile 的子进程输出），只带一条记录。"""
    record = {"hand": "104", "street": "flop", "actions": "bet:6",
              "board": "Ks8h3d", "seats": seats, "error": None}
    return {"tables": ident or tables(), "records": [record],
            "bad_lines": [], "error": error}


def divergent_pair() -> tuple[dict, dict]:
    """A/B：宽度完全一样，第 1 步份额开始分叉，final 有效宽度差 6pt。"""
    top0 = [{"c": "AdKd", "w": 0.0100}, {"c": "KdQd", "w": 0.0080}]
    a = payload([seat(
        [step(0, "flop:bet", 44.80, 39.44, top0),
         step(1, "turn:call", 39.44, 34.00,
              [{"c": "AdKd", "w": 0.0200}, {"c": "KdQd", "w": 0.0100}])],
        final(eff=20.0, peak=0.011),
        top=[{"c": "AdKd", "w": 0.0200}, {"c": "KdQd", "w": 0.0100},
             {"c": "KdJd", "w": 0.0090}])])
    b = payload([seat(
        [step(0, "flop:bet", 44.80, 39.44, top0),
         step(1, "turn:call", 39.44, 34.00,
              [{"c": "AdKd", "w": 0.0300}, {"c": "KdQd", "w": 0.0080}])],
        final(eff=26.0, peak=0.014),
        top=[{"c": "AdKd", "w": 0.0300}, {"c": "KdQd", "w": 0.0080},
             {"c": "KdJd", "w": 0.0090}])],
        ident=tables("sourced", "v1"))
    return a, b


# ─────────────────────────── 1. 有差异定位 ───────────────────────────

def test_divergence_located() -> None:
    a, b = divergent_pair()
    report = build_report(a, b, [meta()],
                          source={"file": "x.jsonl", "lines": 1},
                          filters={})
    check(len(report["records"]) == 1, "报告应含 1 条记录")
    record = report["records"][0]
    check(record["divergent"] is True, "有差异时 divergent 应为 True")
    check(record["reproducible"] is True,
          f"两版表产出应判可复现，reasons={record['reasons']}")
    check(not record["errors"], f"不该有重建错误：{record['errors']}")

    one = record["seats"][0]
    check(one["first_divergence"] == 1,
          f"首个分歧应定位到第 1 步，实得 {one['first_divergence']}")
    check(one["steps"][0]["divergent"] is False,
          "第 0 步宽度与份额都相同，不该标分叉")
    check(one["steps"][1]["first_divergence"] is True,
          "第 1 步应带 first_divergence 标记")
    check(abs(one["steps"][1]["share_delta"] - 0.01) < 1e-9,
          f"第 1 步份额差应为 0.01，实得 {one['steps'][1]['share_delta']}")
    check(one["steps"][1]["delta"] == 0.0,
          "宽度没变，逐步宽度差应仍是 0（换表只动份额）")
    check(one["final"]["delta"]["effective_width_pct"] == 6.0,
          f"final 有效宽度差应为 6.0，实得 "
          f"{one['final']['delta']['effective_width_pct']}")

    moves = one["top_combo_moves"]
    check([row["c"] for row in moves[:2]] == ["AdKd", "KdQd"],
          f"top 位移应按 |delta| 排序，实得 {[row['c'] for row in moves]}")
    check(abs(moves[0]["delta"] - 0.01) < 1e-9,
          f"AdKd 位移应为 +0.01，实得 {moves[0]['delta']}")

    summary = report["summary"]
    check(summary["records"] == 1 and summary["seats"] == 1,
          f"summary 计数不对：{summary}")
    check(summary["divergent_records"] == 1 and summary["divergent_seats"] == 1,
          f"summary 分歧计数不对：{summary}")
    check(summary["max_effective_width_delta"] == 6.0,
          f"最大有效宽度差应为 6.0，实得 {summary['max_effective_width_delta']}")
    check((summary["top_divergence"] or {}).get("seat") == "s1",
          f"分歧最大的一处应指向 s1：{summary['top_divergence']}")
    check(exit_code(report) == 2, "检出差异应退出 2")


def test_max_delta_picks_worst() -> None:
    """两条记录时，最大有效宽度差取绝对值最大的那一处。"""
    a, b = divergent_pair()
    meta2 = meta(hand="105", street="turn")
    a_rec, b_rec = a["records"][0], b["records"][0]
    a2 = dict(a_rec, hand="105", street="turn", seats=[dict(
        a_rec["seats"][0], final=final(eff=20.0))])
    b2 = dict(b_rec, hand="105", street="turn", seats=[dict(
        b_rec["seats"][0], final=final(eff=23.0))])
    report = build_report(
        {"tables": a["tables"], "records": [a_rec, a2]},
        {"tables": b["tables"], "records": [b_rec, b2]},
        [meta(), meta2], source={"file": "x"}, filters={})
    summary = report["summary"]
    check(summary["records"] == 2, "应比较 2 条记录")
    check(summary["max_effective_width_delta"] == 6.0,
          f"应取 6.0 那处，实得 {summary['max_effective_width_delta']}")
    check((summary["top_divergence"] or {}).get("hand") == "104",
          f"最大一处应指向 hand 104：{summary['top_divergence']}")


# ─────────────────────────── 2. 无差异 ───────────────────────────

def test_no_difference() -> None:
    a, _ = divergent_pair()
    b = json.loads(json.dumps(a))          # 深拷贝，换成 B 版表身份
    b["tables"] = tables("sourced", "v1")
    report = build_report(a, b, [meta()],
                          source={"file": "x"}, filters={})
    record = report["records"][0]
    check(record["divergent"] is False, "完全相同时不该判分叉")
    one = record["seats"][0]
    check(one["first_divergence"] is None, "无差异时首个分歧点应为 null")
    check(all(not item["divergent"] for item in one["steps"]),
          "无差异时每一步都不该标分叉")
    check(one["top_combo_moves"] == [], "无差异时不该有 top 位移")
    check(all(value in (0.0, None) for value in one["final"]["delta"].values()),
          f"无差异时 final 差应为 0：{one['final']['delta']}")
    check(report["summary"]["divergent_records"] == 0, "summary 分歧数应为 0")
    check(exit_code(report) == 0, "无差异应退出 0")


def test_width_only_difference() -> None:
    """支撑集大小不同（宽度差）也算分叉，且不看份额也能定位。"""
    a = payload([seat([step(0, "flop:bet", 44.8, 39.44, [])],
                      final(width=34.0, eff=20.0))])
    b = payload([seat([step(0, "flop:bet", 44.8, 39.44, [])],
                      final(width=34.0, eff=20.0))],
                ident=tables("sourced", "v1"))
    b["records"][0]["seats"][0]["steps"][0]["width_after"] = 38.00
    report = build_report(a, b, [meta()], source={"file": "x"}, filters={})
    one = report["records"][0]["seats"][0]
    check(one["first_divergence"] == 0, "宽度差应定位到第 0 步")
    check(one["steps"][0]["delta"] == -1.44,
          f"逐步宽度差应为 -1.44，实得 {one['steps'][0]['delta']}")
    check(report["records"][0]["divergent"] is True, "宽度差应判分叉")


# ─────────────────────────── 3. 不可复现 ───────────────────────────

def test_not_reproducible_still_diffs() -> None:
    a, b = divergent_pair()
    legacy = {"profile": "legacy-2026-09", "variant": "vX",
              "sha256": "deadbeefdeadbeef"}
    record_meta = meta(ident=legacy)
    report = build_report(a, b, [record_meta],
                          source={"file": "x"}, filters={})
    record = report["records"][0]
    check(record["reproducible"] is False, "表身份对不上应判不可复现")
    joined = " | ".join(record["reasons"])
    check(len(record["reasons"]) >= 2, f"应给出多条原因，实得 {record['reasons']}")
    check("deadbeefdeadbeef" in joined, f"原因应点出记录指纹：{joined}")
    check("legacy-2026-09/vX" in joined, f"原因应点出记录表身份：{joined}")
    # 关键：仍然出两版 diff
    one = record["seats"][0]
    check(one["first_divergence"] == 1, "不可复现也要照常定位分歧")
    check(one["top_combo_moves"] != [], "不可复现也要照常给 top 位移")
    check(record["divergent"] is True, "不可复现也要照常判分叉")
    check(report["summary"]["reproducible"] is False,
          "summary 应汇总出 reproducible=false")
    check(exit_code(report) == 2,
          "不可复现本身不算失败：有差异时仍按 2 退出")


def test_not_reproducible_without_difference() -> None:
    """不可复现 + 两版一致：退出码仍是 0（正常完成），靠报告字段提醒。"""
    a, _ = divergent_pair()
    b = json.loads(json.dumps(a))
    b["tables"] = tables("sourced", "v1")
    report = build_report(a, b, [meta(ident={"profile": "other", "variant": "v0",
                                             "sha256": SHA})],
                          source={"file": "x"}, filters={})
    record = report["records"][0]
    check(record["reproducible"] is False, "表身份不同应判不可复现")
    check(record["divergent"] is False, "两版一致不该判分叉")
    check(exit_code(report) == 0, "不可复现但无差异应退出 0")


def test_missing_table_identity() -> None:
    a, b = divergent_pair()
    record_meta = meta()
    record_meta["params"]["tables"] = {}
    report = build_report(a, b, [record_meta], source={"file": "x"}, filters={})
    record = report["records"][0]
    check(record["reproducible"] is False, "缺表指纹应判不可复现")
    check(any("指纹" in reason for reason in record["reasons"]),
          f"原因应提到缺指纹：{record['reasons']}")


def test_prior_drift() -> None:
    """记录里的先验身份与当前注册表不一致 → reasons，且不吞掉 diff。"""
    a, b = divergent_pair()
    record_meta = meta(prior={"id": "RFI_BTN", "version": 2,
                              "source": "named:RFI_BTN"})
    report = build_report(a, b, [record_meta], source={"file": "x"}, filters={})
    record = report["records"][0]
    check(record["reproducible"] is False, "先验版本漂移应判不可复现")
    check(any("version" in reason for reason in record["reasons"]),
          f"原因应点出版本漂移：{record['reasons']}")


# ─────────────────────────── 4. 重建失败不抛异常 ───────────────────────────

def test_rebuild_failure_is_diagnosed() -> None:
    b = payload([seat([], final(), error=None)])
    a = payload([seat([], final(),
                      error="先验 NOT_A_PRIOR 在当前注册表里取不到")])
    try:
        report = build_report(a, b, [meta()], source={"file": "x"}, filters={})
    except Exception as exc:                       # noqa: BLE001
        check(False, f"重建失败不该抛异常，实得 {type(exc).__name__}: {exc}")
        return
    record = report["records"][0]
    check(len(record["errors"]) >= 2,
          f"A 版座位失败 + B 版无该座位，应给出诊断：{record['errors']}")
    check(any("NOT_A_PRIOR" in error for error in record["errors"]),
          f"诊断应带出具体先验 id：{record['errors']}")
    check(report["summary"]["rebuild_failures"] == 1, "summary 应记 1 条失败记录")
    check(exit_code(report) == 1, "重建失败应退出 1")


def test_missing_side_does_not_raise() -> None:
    a, _ = divergent_pair()
    report = build_report(a, None, [meta()], source={"file": "x"}, filters={})
    record = report["records"][0]
    check(any("B 版" in error for error in record["errors"]),
          f"缺一侧应给出诊断：{record['errors']}")
    check(exit_code(report) == 1, "缺一侧应退出 1")


def test_payload_error_does_not_raise() -> None:
    a, _ = divergent_pair()
    b = {"tables": tables("sourced", "v1"), "records": [], "bad_lines": [],
         "error": "FileNotFoundError: trace 文件不存在"}
    report = build_report(a, b, [meta()], source={"file": "x"}, filters={})
    record = report["records"][0]
    check(any("trace 文件不存在" in error for error in record["errors"]),
          f"记录级错误应进 errors：{record['errors']}")
    check(exit_code(report) == 1, "记录级错误应退出 1")


def test_compare_accepts_none_and_garbage() -> None:
    """compare 是纯函数：None / 空载荷 / 缺字段都只产出诊断。"""
    for bad in (None, {}, {"tables": {}, "seats": [], "error": None}):
        try:
            result = compare(bad, None, {})
        except Exception as exc:                   # noqa: BLE001
            check(False, f"compare({bad!r}) 抛了 {type(exc).__name__}: {exc}")
            continue
        check(result["divergent"] is False, "空载荷不该判分叉")
        check(bool(result["errors"]), "空载荷应给出诊断")


# ─────────────────────────── 5. 过滤参数 ───────────────────────────

def _write_trace(directory: Path) -> Path:
    path = directory / "live.range.jsonl"
    rows = []
    for hand, street, actions, ts in (
            (104, "flop", ["bet:6"], "2026-11-15T01:00:00"),
            (104, "flop", ["bet:6"], "2026-11-15T02:00:00"),   # 更新的一条
            (104, "turn", ["bet:6", "raise:20"], "2026-11-15T01:00:01"),
            (105, "flop", ["bet:6"], "2026-11-15T01:00:02"),
            (105, "river", ["check"], "2026-11-15T01:00:03")):
        rows.append(json.dumps({
            "v": "live.range.v1.2", "status": "ok", "ts": ts,
            "hand": str(hand), "street": street,
            "decision_key": {"hand": str(hand), "street": street,
                             "actions": actions},
            "params": {"tables": tables()}, "seats": [],
        }))
    rows.append(json.dumps({"v": "live.range.v1.2", "status": "skipped",
                            "hand": "104", "street": "flop"}))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_filters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_trace(Path(tmp))

        loaded = load_records(path)
        check(loaded["status_ok"] == 5,
              f"status=ok 应 5 条（skipped 不算），实得 {loaded['status_ok']}")
        check(loaded["selected"] == 4,
              f"同决策点只留最新 → 4 条，实得 {loaded['selected']}")
        keys = [(row["hand"], row["street"], row["ts"])
                for row in loaded["records"]]
        check(("104", "flop", "2026-11-15T02:00:00") in keys,
              f"重复决策点应留 ts 最新那条：{keys}")
        order = [(row["street"], row["hand"]) for row in loaded["records"]]
        check(order == sorted(order, key=lambda item: (
            ["preflop", "flop", "turn", "river"].index(item[0]),
            int(item[1]))), f"应按街序 + 手数排好：{order}")

        by_hand = load_records(path, hand=104)
        check(by_hand["selected"] == 2,
              f"--hand 104 应 2 条，实得 {by_hand['selected']}")
        check({row["hand"] for row in by_hand["records"]} == {"104"},
              "只应留下 hand 104")

        by_street = load_records(path, street="flop")
        check(by_street["selected"] == 2,
              f"--street flop 应 2 条，实得 {by_street['selected']}")

        by_key = load_records(path, decision_key="flop:bet:6")
        check(by_key["selected"] == 2,
              f"--decision-key flop:bet:6 应 2 条，实得 {by_key['selected']}")

        by_full = load_records(path, decision_key="104:turn:bet:6:raise:20")
        check(by_full["selected"] == 1,
              f"--decision-key 全写应 1 条，实得 {by_full['selected']}")
        check(by_full["records"][0]["street"] == "turn", "应选中 turn 那条")

        by_count = load_records(path, decision_key="turn:2")
        check(by_count["selected"] == 1,
              f"--decision-key turn:2（token 个数）应 1 条，实得 "
              f"{by_count['selected']}")

        empty = load_records(path, hand=999)
        check(empty["selected"] == 0, "没命中的过滤应返回空")

        try:
            load_records(path, decision_key="flopp:bet:6")
        except ValueError as exc:
            check("street" in str(exc), f"非法 street 应报清楚：{exc}")
        else:
            check(False, "非法 street 应抛 ValueError")

        # 坏行：默认失败、--lenient 跳过
        broken = Path(tmp) / "broken.jsonl"
        broken.write_text('{"status":"ok"}\n{oops\n', encoding="utf-8")
        try:
            load_records(broken)
        except ValueError as exc:
            check("不是合法 JSON" in str(exc), f"坏行诊断不对：{exc}")
        else:
            check(False, "坏行默认应抛 ValueError")
        lenient = load_records(broken, lenient=True)
        check(lenient["bad_lines"] == [2],
              f"--lenient 应记下坏行号：{lenient['bad_lines']}")

        try:
            load_records(Path(tmp) / "nope.jsonl")
        except FileNotFoundError as exc:
            check("不存在" in str(exc), f"缺文件诊断不对：{exc}")
        else:
            check(False, "缺文件应抛 FileNotFoundError")


# ─────────────────────────── 6. CLI 冒烟（真实记录） ───────────────────────────

def _real_trace(path: Path) -> dict | None:
    """用实时同一套 build 造一条 v1.2 记录，验证子进程链路。"""
    from poker_ocr import range_flow, range_trace, ranges
    from poker_ocr.coach import parse_cards

    prior = ranges.get("RFI_BTN")
    check(prior is not None, "先验 RFI_BTN 应能从注册表取到")
    if prior is None:
        return None
    events = (range_flow.ActionEvent(
        street="flop", board="Ks8h3d", seat_id="s1", action="bet",
        amount=6.0, pot=8.0, players_behind=1, spr=4.0),)
    built, trail = range_flow.build_range_traced(
        prior, events, archetype="tag",
        dead=set(parse_cards("AhQhKs8h3d")))
    check(built is not None and trail is not None, "build_range_traced 应有结果")
    if not trail:
        return None
    seat = {"seat": "s1", "pos": "CO", "archetype": "tag",
            "path": "event_model", "mode": "call",
            "bucket": range_flow.bucket_of(events[0]),
            "prior": trail["prior"], "steps": trail["steps"],
            "final": trail["final"], "detail_omitted": [], "equity": None}
    snap = SimpleNamespace(
        hand_id="20261115-cafebabe00", hand_index=104, street="flop",
        board=("Ks", "8h", "3d"), hero_cards=("Ah", "Qh"),
        hero_position="BTN", hero_stack=98.0, pot=12.0, to_call=6.0,
        table_size=6, decision_key=("h", "flop", ("bet:6",)), warnings=())
    record = range_trace.build_record(
        snap, [seat], params=range_trace.params_payload(trail["params"]),
        session="test")
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return record


def test_rebuild_matches_recorded() -> None:
    """重建必须逐位复现记录里的 final / steps（同一套 build 函数、同一版表）。

    这条是整条重建链路的自检：先验 id → ranges.get、steps[].event → ActionEvent、
    dead → 剔除、alpha/support_floor → 参数。任何一环错了这里都会露馅。
    """
    from tools.range_table_diff import rebuild_record

    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "live.range.jsonl"
        record = _real_trace(trace)
        if not record:
            check(False, "夹具没造出来，跳过重建自检")
            return
        payload = rebuild_record(record)
    check(payload["error"] is None, f"整条记录不该报错：{payload['error']}")
    seat = payload["seats"][0]
    check(seat["error"] is None, f"座位不该报错：{seat['error']}")
    recorded = record["seats"][0]["final"]
    got = seat["final"]
    for field in ("width_pct", "weighted_width", "effective_width_pct",
                  "peak", "combo_count"):
        check(got[field] == recorded[field],
              f"final.{field} 应逐位复现：记录 {recorded[field]} → 重建 {got[field]}")
    check(got["dropped"] == recorded["dropped"],
          f"final.dropped 应一致：{recorded['dropped']} → {got['dropped']}")
    check(len(seat["steps"]) == len(record["seats"][0]["steps"]),
          "步数应与记录一致")
    for mine, theirs in zip(seat["steps"], record["seats"][0]["steps"]):
        check(mine["width_after"] == theirs["width_after"],
              f"第 {mine['i']} 步 width_after 应一致："
              f"{theirs['width_after']} → {mine['width_after']}")
        check(mine["label"] == f"{theirs['street']}:"
                               f"{(theirs['event'] or {}).get('action')}",
              f"第 {mine['i']} 步标签应由事件还原：{mine['label']}")
        check(bool(mine["top"]), f"第 {mine['i']} 步应带 combo 份额")
    check(seat["prior"]["id"] == "RFI_BTN"
          and seat["prior"]["version"] == record["seats"][0]["prior"]["version"]
          and seat["prior"]["source"] == record["seats"][0]["prior"]["source"],
          f"先验四件套应对上：{seat['prior']}")


def test_ten_cards_parse() -> None:
    """牌面里的十是三字符 `10s`，不能按两位切（#37 端到端在真实牌面踩到）。"""
    from tools.range_table_diff import _board_tokens, _card, _rebuild_events

    check(_card("10s") == (8, "s"), f"_card('10s') 应解析成 (8, 's')：{_card('10s')}")
    check(_card("Ah") == (12, "h"), f"_card('Ah') 应仍是 (12, 'h')：{_card('Ah')}")
    check(_card("Xx") is None, "认不出的牌应返回 None")
    check(_board_tokens("Ks7c10d") == ["Ks", "7c", "10d"],
          f"十应按三字符切：{_board_tokens('Ks7c10d')}")
    check(_board_tokens("Ks7c10d9h") == ["Ks", "7c", "10d", "9h"],
          f"转牌同样：{_board_tokens('Ks7c10d9h')}")
    events = _rebuild_events(
        [{"street": "flop", "event": {"actor": "s1", "action": "bet",
                                      "amount": 6.0}},
         {"street": "turn", "event": {"actor": "s1", "action": "bet",
                                      "amount": 12.0}}],
        "Ks7c10d9h")
    check(events[0].board == "Ks7c10d",
          f"翻牌事件只看到 3 张：{events[0].board!r}")
    check(events[1].board == "Ks7c10d9h",
          f"转牌事件看到 4 张：{events[1].board!r}")


def test_ten_board_record_rebuild() -> None:
    """带十的真实记录整条重建不报错（牌面/死牌都按张切）。"""
    from tools.range_table_diff import rebuild_record

    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "live.range.jsonl"
        record = _real_trace(trace)
        if not record:
            check(False, "夹具没造出来，跳过带十重建")
            return
    record = json.loads(json.dumps(record))
    record["board"] = "10s7c2d"
    record["dead"] = ["10h", "Qs"]
    payload = rebuild_record(record)
    check(payload["error"] is None, f"记录级不该报错：{payload['error']}")
    seat = payload["seats"][0]
    check(seat["error"] is None,
          f"带十的牌面不该重建失败：{seat['error']}")
    check(payload["board"] == "10s7c2d", f"牌面应原样回传：{payload['board']}")


def test_cli_smoke() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "live.range.jsonl"
        _real_trace(trace)
        if not trace.is_file():
            check(False, "夹具没写出来，跳过 CLI 冒烟")
            return
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "range_table_diff.py"),
             "--file", str(trace), "--json", "--hand", "104"],
            cwd=str(ROOT), capture_output=True, text=True)
        check(proc.returncode in (0, 2),
              f"CLI 应正常退出（0/2），实得 {proc.returncode}："
              f"{(proc.stderr or '')[-800:]}")
        if proc.returncode not in (0, 2):
            return
        report = json.loads(proc.stdout)
        for field in ("source", "filters", "tables", "records", "summary"):
            check(field in report, f"报告缺顶层字段 {field}")
        check(report["tables"]["a"]["profile"] == "hand-tuned",
              f"A 版应是 hand-tuned：{report['tables']['a']}")
        check(report["tables"]["b"]["profile"] == "sourced",
              f"B 版应是 sourced：{report['tables']['b']}")
        check(report["filters"].get("hand") == 104,
              f"filters 应回显 --hand：{report['filters']}")
        check(len(report["records"]) == 1, "应选中 1 条记录")
        record = report["records"][0]
        for field in ("hand", "street", "decision_key", "board",
                      "reproducible", "reasons", "seats"):
            check(field in record, f"记录缺字段 {field}")
        check(record["reproducible"] is True,
              f"记录由 hand-tuned 产出，应判可复现：{record['reasons']}")
        one = record["seats"][0]
        for field in ("seat", "prior", "steps", "first_divergence",
                      "final", "top_combo_moves", "divergent"):
            check(field in one, f"座位缺字段 {field}")
        check(one["steps"] and one["steps"][0]["label"] == "flop:bet",
              f"逐步标签应来自事件：{one['steps'][:1]}")
        check(one["prior"]["a"]["id"] == "RFI_BTN",
              f"先验身份应从注册表重建：{one['prior']}")
        check(isinstance(report["summary"]["max_effective_width_delta"],
                         (int, float)), "summary 应带最大有效宽度差")


def main() -> int:
    test_divergence_located()
    test_max_delta_picks_worst()
    test_no_difference()
    test_width_only_difference()
    test_not_reproducible_still_diffs()
    test_not_reproducible_without_difference()
    test_missing_table_identity()
    test_prior_drift()
    test_rebuild_failure_is_diagnosed()
    test_missing_side_does_not_raise()
    test_payload_error_does_not_raise()
    test_compare_accepts_none_and_garbage()
    test_filters()
    test_rebuild_matches_recorded()
    test_ten_cards_parse()
    test_ten_board_record_rebuild()
    test_cli_smoke()

    if FAILS:
        print(f"✗ {len(FAILS)} 项不通过：")
        for item in FAILS:
            print(f"  · {item}")
        return 1
    print("✓ 换表重算 diff：比较/定位/不可复现/失败诊断/过滤/CLI 全通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
