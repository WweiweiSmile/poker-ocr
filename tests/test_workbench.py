#!/usr/bin/env python3
"""工作台：工作区 schema、体检、子进程、标定写入、HTTP 路由。

    nv tests/test_workbench.py

不连摄像头/窗口/OCR 模型：体检、spawn、OCR 引擎都注入假实现，测试快且
可在无权限环境跑；真正要权限的部分由人工验收（见 docs/工作台-实现说明.md）。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from poker_ocr.workbench import geometry  # noqa: E402
from poker_ocr.workbench.calibration import (  # noqa: E402
    CalibrationEngine, CalibrationError, Draft, apply_edits, restore_backup)
from poker_ocr.workbench.preflight import (  # noqa: E402
    PreflightContext, run_preflight, summarize)
from poker_ocr.workbench.service import WorkbenchError, WorkbenchService  # noqa: E402
from poker_ocr.workbench.store import (  # noqa: E402
    WorkspaceStore, default_workspace, normalize_workspace,
    validate_workspace)
from poker_ocr.workbench.supervisor import (  # noqa: E402
    LiveSupervisor, build_live_command, redact_command)

PROFILE = ROOT / "config" / "table_wpk_mac.json"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="wb_test_"))


def _workspace(**over) -> dict:
    doc = default_workspace(PROFILE, name="测试工作区",
                            expected_size=[736, 838])
    doc["source"] = {"kind": "window", "spec": "window:title:WePoker-H5",
                     "label": "Edge"}
    doc.update(over)
    return normalize_workspace(doc, root=ROOT)


# ─────────────────────────── schema / store ───────────────────────────

def test_workspace_validation() -> None:
    doc = _workspace()
    errors, warnings = validate_workspace(doc, root=ROOT)
    check(not errors, f"合法工作区不应报错：{errors}")
    check(not warnings, f"合法工作区不应报警告：{warnings}")

    bad = copy.deepcopy(doc)
    bad["schema"] = 2
    bad["source"] = {"kind": "magic", "spec": ""}
    bad["click"]["mode"] = "nope"
    bad["recognizer"] = "full"
    bad["runtime"]["advanced"] = {"whatever": 1}
    errors, _ = validate_workspace(bad, root=ROOT)
    fields = {e["field"] for e in errors}
    for field in ("schema", "source.kind", "source.spec", "click.mode",
                  "full_config", "runtime.advanced"):
        check(field in fields, f"应报错 {field}：{fields}")

    bad2 = copy.deepcopy(doc)
    bad2["window"]["expected_size"] = [0, -1]
    bad2["click"]["bet_slider_only"] = True
    bad2["click"]["bet_slider"] = False
    errors, _ = validate_workspace(bad2, root=ROOT)
    fields = {e["field"] for e in errors}
    check("window.expected_size" in fields, "非法窗口尺寸应报错")
    check("click.bet_slider_only" in fields, "只用滑杆但没开滑杆应报错")

    bad_window = copy.deepcopy(doc)
    bad_window["window"]["spec"] = 123
    errors, _ = validate_workspace(bad_window, root=ROOT)
    check(any(e["field"] == "window.spec" for e in errors),
          "window.spec 非字符串应报错")
    bad_window = copy.deepcopy(doc)
    bad_window["window"]["position"] = [1, 2, 3]
    errors, _ = validate_workspace(bad_window, root=ROOT)
    check(any(e["field"] == "window.position" for e in errors),
          "window.position 形状不对应报错")

    unknown = copy.deepcopy(doc)
    unknown["mystery"] = 1
    _, warnings = validate_workspace(unknown, root=ROOT)
    check(any("mystery" in w["reason"] for w in warnings),
          f"未知字段应给警告：{warnings}")


def test_workspace_crud() -> None:
    tmp = _tmp()
    store = WorkspaceStore(tmp / "ws", project_root=ROOT)
    doc = store.create(_workspace())
    check(doc["id"] == "wpk-mac", f"id 应来自 profile：{doc['id']}")
    check((tmp / "ws" / "wpk-mac.json").is_file(), "工作区应落盘")
    check(len(store.list_raw()) == 1, "列表应有 1 个")

    updated = store.update("wpk-mac", {"name": "改名", "runtime": {"every": 0.8}})
    check(updated["name"] == "改名", "名称应更新")
    check(updated["runtime"]["every"] == 0.8, "runtime 应合并")
    check(updated["runtime"]["settle"] == 0.25, "runtime 未覆盖的字段应保留")
    check(updated["source"]["spec"].startswith("window:"), "source 应保留")

    # 窗口源 → 摄像头源：原窗口要自动留作滑杆/浮层窗口
    migrated = store.update("wpk-mac", {
        "source": {"kind": "camera", "spec": "cam:auto"},
    })
    check(migrated["window"]["spec"] == "title:WePoker-H5",
          f"切换源应保留游戏窗口：{migrated['window']}")

    clone = store.duplicate("wpk-mac")
    check(clone["id"] != "wpk-mac", "复制应换 id")
    check(len(store.list_raw()) == 2, "复制后应有 2 个")

    store.delete(clone["id"])
    check(len(store.list_raw()) == 1, "删除后应有 1 个")

    try:
        store.create(_workspace(), overwrite=False)
    except FileExistsError:
        pass
    else:
        check(False, "重复 id 应拒绝覆盖")

    try:
        store.path_for("../../etc/passwd")
    except ValueError:
        pass
    else:
        check(False, "非法 id 必须被拒绝（路径穿越）")


def test_profile_discovery() -> None:
    tmp = _tmp()
    store = WorkspaceStore(tmp / "ws", project_root=ROOT)
    profiles = {p["name"]: p for p in store.discover_profiles()}
    check("table_wpk_mac.json" in profiles, f"应发现 wpk_mac：{list(profiles)}")
    mac = profiles["table_wpk_mac.json"]
    check(mac["ok"] and mac["clickable"] and mac["slider"],
          f"wpk_mac 应可点可滑杆：{mac}")
    check(mac["aspect"] == round(1472 / 1676, 6), "比例应算对")


# ─────────────────────────── geometry ───────────────────────────

def test_geometry_layout_and_errors() -> None:
    profile, problems = __import__("poker_ocr.profile",
                                   fromlist=["Profile"]).Profile.inspect(PROFILE)
    check(profile is not None and not problems, f"profile 应可读：{problems}")
    slots = geometry.profile_slots(profile)
    by_key = {s["key"]: s for s in slots}
    check(by_key["global:hero_raise_button"]["group"] == "buttons", "主钮分组")
    check(by_key["global:hero_raise_button"]["stage"] == "M1", "M1 可编辑")
    check(by_key["global:pot"]["stage"] == "M3", "底池属于 M3")
    check(by_key["seat:hero:name"]["seat_id"] == "hero", "座位槽位 key")
    check(len([s for s in slots if s["group"] == "buttons"]) >= 10,
          "按钮槽位数量")

    layout = geometry.layout(profile, (1472, 1676))
    hero = next(s for s in layout["slots"] if s["key"] == "global:hero_raise_button")
    expect = list(profile.slot_to_px(tuple(hero["slot"]), (1472, 1676)))
    check(hero["px"] == expect, "像素矩形必须复用 slot_to_px")
    check(layout["content_rect"] == [0, 0, 1472, 1676], "无 canvas 时整帧")

    report = geometry.aspect_report((1472, 1676), (1472, 1676))
    check(report["level"] == "pass", f"同比例应 pass：{report}")
    check(geometry.aspect_report((1400, 1676), (1472, 1676))["level"] == "warn",
          "5% 偏差应 warn")
    check(geometry.aspect_report((1000, 1676), (1472, 1676))["level"] == "fail",
          "20% 偏差应 fail")

    mapped = geometry.map_errors(
        ["pot", "stack:topL", "bet_gt_pot:a", "blinds", "dealer"], slots)
    check(mapped["by_slot"].get("global:pot") == ["pot"], "pot 应映射到底池")
    check("global:blinds" in mapped["by_slot"], "blinds 应映射")
    top = by_key["seat:topL:chip"]["key"]
    check(top in mapped["by_slot"], f"stack:topL 应映射到 {top}")
    check("dealer" in mapped["other"], "dealer 映射不到槽位应进 other")

    diag = geometry.diagnostics(profile, source_size=(1472, 1676),
                                frame_size=(1472, 1676),
                                window={"x": 0, "y": 33, "w": 736, "h": 838,
                                        "expected_size": [736, 838]})
    labels = [r["label"] for r in diag["rows"]]
    check("窗口" in labels and "预计偏移" in labels, f"诊断行：{labels}")
    check(diag["aspect"]["level"] == "pass", "诊断里的比例应 pass")


# ─────────────────────────── 体检 ───────────────────────────

def _ctx(tmp: Path, **over) -> PreflightContext:
    window = [{"index": 0, "owner": "Microsoft Edge", "title": "WePoker-H5",
               "window_id": 2354, "x": 0, "y": 33, "w": 736, "h": 838}]
    base = dict(
        root=ROOT,
        probe_source=lambda spec, timeout=8.0: {
            "ok": True, "size": [1472, 1676], "kind": "window",
            "elapsed_ms": 120.0},
        window_matches=lambda spec: (window, None),
        screen_permission=lambda: (True, "可截屏"),
        accessibility=lambda: (True, "已授权"),
        automation=lambda: (True, "可读取前台进程"),
        port_probe=lambda: {"free": [8020, 8021], "preferred": 8020,
                            "preferred_busy": False, "preferred_live": None},
        env_check=lambda: (True, ".env 里有 AI_SERVER_API_KEY"),
        runs_dir=tmp / "runs",
    )
    base.update(over)
    return PreflightContext(**base)


def test_preflight_pass_and_warn() -> None:
    tmp = _tmp()
    ws = _workspace()
    checks = run_preflight(ws, ctx=_ctx(tmp))
    ids = {c["id"]: c for c in checks}
    check(ids["workspace"]["status"] == "pass", "schema 应 pass")
    check(ids["profile_file"]["status"] == "pass", "profile 应 pass")
    check(ids["profile_valid"]["status"] == "pass", "profile 校验应 pass")
    check(ids["source_frame"]["status"] == "pass", "源出帧应 pass")
    check(ids["source_aspect"]["status"] == "pass", "比例应 pass")
    check(ids["window_resolve"]["status"] == "pass", "窗口应唯一命中")
    check(ids["window_size"]["status"] == "pass", "窗口尺寸应匹配")
    check(ids["live_port"]["status"] == "pass", "端口应空闲")
    summary = summarize(checks)
    check(summary["can_start"], f"应可启动：{summary}")
    check(not summary["needs_confirm"], f"不应需要确认：{summary}")

    # 源打不开、窗口找不到、端口全占：三条 fail
    checks = run_preflight(ws, ctx=_ctx(
        tmp,
        probe_source=lambda spec, timeout=8.0: {"ok": False,
                                                "error": "没有出帧"},
        window_matches=lambda spec: ([], None),
        port_probe=lambda: {"free": [], "preferred": 8020,
                            "preferred_busy": True, "preferred_live": None},
    ))
    ids = {c["id"]: c for c in checks}
    check(ids["source_frame"]["status"] == "fail", "源失败应 fail")
    check(ids["window_resolve"]["status"] == "fail", "窗口找不到应 fail")
    check(ids["live_port"]["status"] == "fail", "端口全占应 fail")
    summary = summarize(checks)
    check(not summary["can_start"], "有 fail 时不能启动")
    check(set(summary["blocking"]) >= {"source_frame", "window_resolve",
                                      "live_port"}, "blocking 应列出三项")

    # 不探测源：source_frame/aspect 是 skip，不是 pass
    checks = run_preflight(ws, ctx=_ctx(tmp), probe=False)
    ids = {c["id"]: c for c in checks}
    check(ids["source_frame"]["status"] == "skip", "未探测应 skip")
    check(ids["source_aspect"]["status"] == "skip", "未探测比例应 skip")


def test_preflight_click_config_without_slider() -> None:
    """不开滑杆时必须能找到 clicker_local；缺了要 fail，但不能吞掉后面的检查。"""
    tmp = _tmp()
    ws = _workspace()
    ws["click"]["bet_slider"] = False
    checks = run_preflight(ws, ctx=_ctx(tmp))
    ids = {c["id"]: c for c in checks}
    check("click_slots" in ids and "full_config" in ids,
          f"17 项检查必须齐全：{sorted(ids)}")
    check(ids["click_slots"]["status"] in ("pass", "fail"),
          f"clicker 配置存在时应 pass：{ids['click_slots']}")

    ws["click"]["clicker_config"] = "config/does-not-exist.json"
    checks = run_preflight(ws, ctx=_ctx(tmp))
    ids = {c["id"]: c for c in checks}
    check(ids["click_slots"]["status"] == "fail",
          "缺 clicker 配置应 fail")
    check(ids.get("full_config", {}).get("status") == "skip",
          "后续 full_config 检查不能被吞掉")


def test_window_id_spec_is_stable_across_titles() -> None:
    """`id:N` 解析：标题变了也能找到同一个窗口（工作台枚举窗口默认用它）。"""
    from poker_ocr import bet_slider
    from poker_ocr.bet_slider import WindowInfo, WindowRect
    from poker_ocr.workbench import sources

    fake = WindowInfo(2, "Microsoft Edge", "Poker Replay Console",
                      WindowRect(0, 33, 736, 838), 2354)
    original = bet_slider.list_windows
    bet_slider.list_windows = lambda: [fake]
    try:
        info = bet_slider.resolve_window_info("id:2354")
        check(info is not None and info.title == "Poker Replay Console",
              f"id 应解析到窗口：{info}")
        check(sources.parse_source_spec("window:id:2354")["valid"],
              "window:id:N 应可解析")
        hits, err = sources.window_matches("window:id:2354")
        check(len(hits) == 1 and hits[0]["window_id"] == 2354,
              f"window_matches 应命中 id：{hits} {err}")
        resolved = sources.window_info("window:id:2354")
        check(resolved and resolved["w"] == 736,
              f"window_info 应返回 rect：{resolved}")
        try:
            bet_slider.resolve_window_info("id:9999")
        except ValueError as exc:
            check("没找到 window id=9999" in str(exc),
                  f"不存在的 id 应报错：{exc}")
        else:
            check(False, "不存在的 id 必须报错")
    finally:
        bet_slider.list_windows = original


def test_slider_window_follows_source_or_workspace() -> None:
    """滑杆窗口：窗口源自动跟随源；非窗口源必须显式指定，且不读共享配置的 window。"""
    tmp = _tmp()
    ws = _workspace()          # window:title:WePoker-H5
    checks = {c["id"]: c for c in run_preflight(ws, ctx=_ctx(tmp))}
    check(checks["slider_window"]["status"] == "pass",
          f"窗口源应自动跟随：{checks['slider_window']}")
    check("跟随窗口源" in checks["slider_window"]["detail"],
          checks["slider_window"]["detail"])

    # camera 源：没有窗口可跟随 → fail，且修复动作指向工作区而不是共享配置
    ws_cam = _workspace()
    ws_cam["source"] = {"kind": "camera", "spec": "cam:auto", "label": ""}
    checks = {c["id"]: c for c in run_preflight(ws_cam, ctx=_ctx(tmp))}
    check(checks["slider_window"]["status"] == "fail",
          f"摄像头源必须显式指定滑杆窗口：{checks['slider_window']}")
    check("滑杆窗口未指定" in checks["slider_window"]["title"],
          checks["slider_window"]["title"])
    check("工作区" in checks["slider_window"]["fix"],
          checks["slider_window"]["fix"])

    # 显式指定（可带 window: 前缀）→ pass，并显示命中的窗口
    ws_cam["window"]["spec"] = "window:title:WePoker-H5"
    checks = {c["id"]: c for c in run_preflight(ws_cam, ctx=_ctx(tmp))}
    check(checks["slider_window"]["status"] == "pass",
          f"显式滑杆窗口应可解析：{checks['slider_window']}")
    check("工作区窗口" in checks["slider_window"]["detail"],
          checks["slider_window"]["detail"])

    # 老配置兜底：bet_slider_mac.json 里有 window 时直接用它
    cfg = tmp / "bet_slider_mac.json"
    raw = json.loads((ROOT / "config" / "bet_slider_mac.json").read_text(
        encoding="utf-8"))
    raw["window"] = "title:WePoker-H5"
    cfg.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    ws_old = _workspace()
    ws_old["source"] = {"kind": "camera", "spec": "cam:auto", "label": ""}
    ws_old["bet_slider_config"] = str(cfg)
    checks = {c["id"]: c for c in run_preflight(ws_old, ctx=_ctx(tmp))}
    check(checks["slider_window"]["status"] == "pass",
          f"老配置的 window 应兜底：{checks['slider_window']}")
    check("滑杆配置" in checks["slider_window"]["detail"],
          checks["slider_window"]["detail"])

    # 源还没选好：滑杆窗口 skip，不叠加第二个错误
    ws_empty = _workspace()
    ws_empty["source"] = {"kind": "window", "spec": "", "label": ""}
    checks = {c["id"]: c for c in run_preflight(ws_empty, ctx=_ctx(tmp))}
    check(checks["slider_window"]["status"] == "skip",
          f"未选源时应 skip：{checks['slider_window']}")


def test_build_live_command_slider_window() -> None:
    base = dict(root=ROOT, python="/py", port=8021,
                run_dir=ROOT / "runs" / "r1", token="t0ken-1234",
                run_id="r1")
    cmd = build_live_command(_workspace(), **base)
    check(cmd[cmd.index("--bet-slider-window") + 1] == "title:WePoker-H5",
          f"窗口源应复用源作为滑杆窗口：{cmd}")

    ws = _workspace()
    ws["source"] = {"kind": "camera", "spec": "cam:auto", "label": ""}
    ws["window"]["spec"] = "window:index:2"
    cmd = build_live_command(ws, **base)
    check(cmd[cmd.index("--bet-slider-window") + 1] == "index:2",
          f"显式滑杆窗口应去掉 window: 前缀：{cmd}")


def test_preflight_window_and_permissions() -> None:
    tmp = _tmp()
    ws = _workspace()
    ws["window"]["expected_size"] = [800, 900]
    ws["click"]["dry_run"] = False
    hits = [{"index": 0, "owner": "Edge", "title": "WePoker-H5",
             "window_id": 1, "x": 0, "y": 0, "w": 736, "h": 838},
            {"index": 1, "owner": "Edge", "title": "WePoker-B", "window_id": 2,
             "x": 0, "y": 0, "w": 736, "h": 838}]
    checks = run_preflight(ws, ctx=_ctx(
        tmp, window_matches=lambda spec: (hits, None),
        screen_permission=lambda: (False, "没有屏幕录制权限"),
        accessibility=lambda: (False, "没有辅助功能权限")))
    ids = {c["id"]: c for c in checks}
    check(ids["window_resolve"]["status"] == "warn", "多个命中应 warn")
    check(ids["window_size"]["status"] == "skip",
          "窗口不唯一时无法核对尺寸（skip，不冒充通过）")
    single = run_preflight(ws, ctx=_ctx(
        tmp, window_matches=lambda spec: (hits[:1], None)))
    single_ids = {c["id"]: c for c in single}
    check(single_ids["window_size"]["status"] == "warn",
          "唯一命中但尺寸不符应 warn")
    check(ids["screen_permission"]["status"] == "fail", "无截屏权限应 fail")
    check(ids["accessibility"]["status"] == "fail", "无点击权限应 fail")
    check(ids["accessibility"]["fix"], "fail 项必须有修复动作")

    ws["click"]["dry_run"] = True
    checks = run_preflight(ws, ctx=_ctx(tmp))
    ids = {c["id"]: c for c in checks}
    check(ids["accessibility"]["status"] == "skip", "dry-run 不需要辅助功能")


# ─────────────────────────── 子进程 / 命令 ───────────────────────────

def test_build_live_command() -> None:
    ws = _workspace(runtime={
        "initial_blinds": "1/2", "every": 0.5, "settle": 0.25,
        "ai_advice": True, "test": False,
        "advanced": {"stable_frames": 3, "rotate": 90, "crop": "left",
                     "no_dedup": True, "unknown": 1}})
    cmd = build_live_command(ws, root=ROOT, python="/py", port=8021,
                             run_dir=ROOT / "runs" / "r1",
                             token="secret-token-123", run_id="r1")
    check(cmd[0] == "/py" and cmd[1:3] == ["-m", "poker_ocr.live"], "入口")
    check("--preview" in cmd, "必须请求预览")
    check(cmd[cmd.index("--control-token") + 1] == "secret-token-123",
          "控制 token")
    check(cmd[cmd.index("--web-port") + 1] == "8021", "端口")
    check("--bet-slider" in cmd, "滑杆")
    check("--clicker-dry-run" in cmd, "dry-run")
    check("--no-confirm" not in cmd, "默认要确认")
    check(cmd[cmd.index("--stable-frames") + 1] == "3", "高级参数白名单")
    check("--rotate" in cmd and cmd[cmd.index("--rotate") + 1] == "90",
          "rotate 白名单")
    check("--crop" in cmd and cmd[cmd.index("--crop") + 1] == "left", "crop")
    check("--no-dedup" in cmd, "布尔高级参数")
    check("--unknown" not in cmd, "未知高级参数不能进命令行")
    check("--ai-advice" in cmd and "--test" not in cmd, "AI/测试开关")
    check(str(PROFILE) in cmd, "profile 应用绝对路径")
    redacted = redact_command(cmd, "secret-token-123")
    check("***" in redacted and "secret-token-123" not in redacted,
          "manifest 里 token 必须打码")


class FakeProc:
    def __init__(self) -> None:
        self.pid = 999999
        self.returncode = None
        self.signals = []
        self._event = threading.Event()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if timeout is None:
            self._event.wait(5.0)
        else:
            self._event.wait(timeout)
        if self.returncode is None and self._event.is_set():
            self.returncode = 0
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if int(sig) == 2:                     # SIGINT：真实进程会自行退出
            self.returncode = 0
            self._event.set()

    def kill(self):
        self.returncode = -9
        self._event.set()

    def finish(self, code=0):
        self.returncode = code
        self._event.set()


def test_supervisor_lifecycle_and_manifest() -> None:
    tmp = _tmp()
    procs = []

    def spawn(command, **kwargs):
        proc = FakeProc()
        procs.append(proc)
        return proc

    sup = LiveSupervisor(ROOT, runs_dir=tmp / "runs", python="/py",
                         spawn=spawn,
                         ready_check=lambda port, **kw: {
                             "ok": True, "meta": {"frame": {"width": 1472}},
                             "error": None},
                         meta_get=lambda port, **kw: {
                             "stats": {"processed": 10, "accepted": 9},
                             "source": {"window": {"x": 0, "y": 0,
                                                   "w": 736, "h": 838}},
                             "click": {"enabled": True}})
    sup._pick_port = lambda *a, **k: 8123
    ws = _workspace()
    handle = sup.start(ws, preflight={
        "summary": {"counts": {"pass": 1}, "can_start": True,
                    "blocking": []},
        "checks": [{"id": "workspace", "status": "pass"}]})
    check(handle["port"] == 8123, "应使用选定端口")
    check(handle["status"] in ("starting", "running"), "启动后应 running")
    for _ in range(40):
        state = sup.status()
        if state["status"] == "running":
            break
        time.sleep(0.05)
    check(state["status"] == "running", f"应进入 running：{state['status']}")

    procs[0].finish(0)
    for _ in range(40):
        if sup.status()["run"] and sup.status()["run"]["status"] != "running":
            break
        time.sleep(0.05)
    run = sup.status()["run"]
    check(run["status"] == "idle", f"进程退出后应 idle：{run['status']}")
    check(run["exit_code"] == 0, f"退出码：{run['exit_code']}")
    manifest_path = Path(run["dir"]) / "manifest.json"
    check(manifest_path.is_file(), "必须写 manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check(manifest["exit_code"] == 0, "manifest 退出码")
    check(manifest["profile"]["sha256"], "manifest 应有 profile 哈希")
    check(manifest["preflight_summary"]["can_start"],
          "manifest 应记录体检结论")
    check(manifest["preflight"]["checks"], "manifest 应记录体检全文")
    check(manifest["window"]["w"] == 736, "manifest 应记录窗口 rect")
    check("***" in " ".join(manifest["command"]), "命令里的 token 要打码")
    check((Path(run["dir"]) / "workspace.json").is_file(), "应存工作区快照")


def test_supervisor_rejects_second_run() -> None:
    tmp = _tmp()
    proc = FakeProc()
    sup = LiveSupervisor(tmp, runs_dir=tmp / "runs", spawn=lambda *a, **k: proc,
                         ready_check=lambda port, **kw: {"ok": True, "meta": {}},
                         stop_timeout=0.3)
    sup._pick_port = lambda *a, **k: 8124
    sup.start(_workspace())
    try:
        sup.start(_workspace())
    except RuntimeError as exc:
        check("已有 live 在运行" in str(exc), f"第二次启动应被拒：{exc}")
    else:
        check(False, "第二次启动必须被拒绝")
    sup.stop()


# ─────────────────────────── 标定 / 写入 ───────────────────────────

def test_draft_undo_redo_diff() -> None:
    profile, _ = __import__("poker_ocr.profile",
                            fromlist=["Profile"]).Profile.inspect(PROFILE)
    draft = Draft("ws")
    draft.set("global:hero_raise_button", [0.50, 0.80, 0.55, 0.85])
    draft.set("global:hero_fold_button", [0.10, 0.80, 0.15, 0.85])
    check(len(draft.edits) == 2, "应有 2 条编辑")
    check(draft.undo(), "应能撤销")
    check("global:hero_fold_button" not in draft.edits, "撤销后应移除")
    check(draft.redo(), "应能重做")
    check("global:hero_fold_button" in draft.edits, "重做后应恢复")
    diff = draft.diff(profile)
    check(len(diff) == 2, "diff 应有 2 条")
    check(all(d["dx_px"] is not None for d in diff), "diff 应有像素位移")
    draft.reset()
    check(not draft.edits and not draft.undo_stack, "重置应清空")

    try:
        draft.set("global:pot", [0.9, 0.9, 0.1, 0.1])
    except CalibrationError:
        pass
    else:
        check(False, "倒置槽位必须被拒绝")


def test_apply_edits_backup_and_preserve_notes() -> None:
    tmp = _tmp()
    cfg = tmp / "config"
    cfg.mkdir()
    path = cfg / "table_wpk_mac.json"
    shutil.copy2(PROFILE, path)
    original_text = path.read_text(encoding="utf-8")
    raw = json.loads(original_text)
    original_order = list(raw)
    old_slot = raw["global_slots"]["hero_raise_button"]

    result = apply_edits(path, {
        "global:hero_raise_button": [0.50, 0.80, 0.55, 0.85],
        "seat:hero:name": [0.40, 0.70, 0.60, 0.72],
    })
    check(result["ok"], "写入应成功")
    check(Path(result["backup"]).is_file(), "必须生成备份")
    check(Path(result["backup"]).read_text(encoding="utf-8") == original_text,
          "备份应是原文件逐字节")
    new_raw = json.loads(path.read_text(encoding="utf-8"))
    hero_index = next(i for i, seat in enumerate(new_raw["seats"])
                      if seat["seat_id"] == "hero")
    check(list(new_raw) == original_order, "顶层键顺序必须保留")
    check(new_raw["_comment"] == raw["_comment"], "_note/_comment 必须保留")
    check(new_raw["global_slots"]["hero_raise_button"] ==
          [0.50, 0.80, 0.55, 0.85], "槽位应更新")
    check(new_raw["seats"][hero_index]["name_slot"] ==
          [0.40, 0.70, 0.60, 0.72], "座位槽位应更新")
    check(old_slot == raw["global_slots"]["hero_raise_button"], "内存副本不变")
    check(result["diff"], "应返回 diff")

    # 校验失败：文件一个字节都不动
    before = path.read_bytes()
    try:
        apply_edits(path, {"global:pot": [0.5, 0.5, 0.4, 0.6]})
    except CalibrationError as exc:
        check(bool(exc.problems), "校验失败应带问题列表")
    else:
        check(False, "非法槽位必须被拒绝")
    check(path.read_bytes() == before, "校验失败不能改文件")

    # 回滚
    restore_backup(path, result["backup"])
    check(path.read_text(encoding="utf-8") == original_text, "回滚应恢复原文件")


def test_calibration_trial_with_fake_engine() -> None:
    profile, _ = __import__("poker_ocr.profile",
                            fromlist=["Profile"]).Profile.inspect(PROFILE)
    engine = CalibrationEngine()

    class FakeOcr:
        def read_text(self, img):
            return [("加注", 0.93)]

        def read_boxes(self, img):
            return []

    engine._engine = FakeOcr()
    frame = np.zeros((1676, 1472, 3), dtype=np.uint8)
    result = engine.trial(profile, frame, "global:hero_bet_amount")
    check(result["mode"] == "trial", "结果必须标注试读")
    check(result["text"] == "加注", f"应读到文本：{result}")
    check("试读" in result["note"], "必须说明和 live 读数的区别")
    check(result["crop"] and result["crop"].startswith("data:image/png"),
          "应返回裁剪图")
    check(result["rect"][2] > result["rect"][0], "裁剪矩形应有效")
    check(result["ms"] >= 0, "应有耗时")

    try:
        engine.trial(profile, frame, "global:nope")
    except CalibrationError:
        pass
    else:
        check(False, "未知槽位应报错")


# ─────────────────────────── service / HTTP ───────────────────────────

def _service(tmp: Path, *, spawn=None) -> WorkbenchService:
    return WorkbenchService(root=ROOT, workspaces_dir=tmp / "ws",
                            runs_dir=tmp / "runs", spawn=spawn,
                            preflight_context=_ctx(tmp))


def test_service_workspace_and_status() -> None:
    tmp = _tmp()
    svc = _service(tmp)
    doc = svc.create_workspace({"from_profile": str(PROFILE),
                                "name": "服务测试",
                                "source": {"kind": "window",
                                           "spec": "window:title:WePoker-H5"}})
    check(doc["id"] and doc["profile"] == "config/table_wpk_mac.json",
          f"服务创建：{doc['id']}")
    check(svc.get_workspace(doc["id"])["profile_info"]["ok"], "profile 摘要")
    status = svc.status(doc["id"])
    check(status["state"] == "idle", "没有运行时应 idle")
    check(status["run"] is None, "没有 run")
    slots = svc.slots(doc["id"])
    check(len(slots["slots"]) > 50, "应返回全量槽位")
    check(slots["diagnostics"]["rows"], "应有几何诊断")

    try:
        svc.get_workspace("missing")
    except WorkbenchError as exc:
        check(exc.status == 404, "缺工作区应 404")
    else:
        check(False, "缺工作区应报错")


def test_service_calibration_apply_no_run() -> None:
    tmp = _tmp()
    cfg = tmp / "config"
    cfg.mkdir()
    profile = cfg / "table_wpk_mac.json"
    shutil.copy2(PROFILE, profile)
    svc = _service(tmp)
    doc = svc.create_workspace({"from_profile": str(profile),
                                "source": {"kind": "window",
                                           "spec": "window:title:WePoker-H5"}})
    svc.calibration_edit(doc["id"], "global:hero_raise_button",
                         [0.50, 0.80, 0.55, 0.85])
    check(svc.calibration(doc["id"])["draft"]["count"] == 1, "草稿应有 1 条")
    result = svc.calibration_apply(doc["id"])
    check(result["ok"] and result["applied"] == ["global:hero_raise_button"],
          f"应用结果：{result}")
    check(Path(result["backup"]).is_file(), "应用应备份")
    check(svc.calibration(doc["id"])["draft"] is None, "成功后应清草稿")
    raw = json.loads(profile.read_text(encoding="utf-8"))
    check(raw["global_slots"]["hero_raise_button"] ==
          [0.50, 0.80, 0.55, 0.85], "profile 应写入新值")


def test_service_restore_and_overlay_update() -> None:
    tmp = _tmp()
    cfg = tmp / "config"
    cfg.mkdir()
    profile = cfg / "table_wpk_mac.json"
    shutil.copy2(PROFILE, profile)
    original = profile.read_text(encoding="utf-8")
    svc = _service(tmp)
    doc = svc.create_workspace({"from_profile": str(profile),
                                "source": {"kind": "window",
                                           "spec": "window:title:WePoker-H5"}})
    svc.calibration_edit(doc["id"], "global:hero_raise_button",
                         [0.50, 0.80, 0.55, 0.85])
    applied = svc.calibration_apply(doc["id"])
    backup_name = Path(applied["backup"]).name
    check(profile.read_text(encoding="utf-8") != original, "应用后文件应变化")

    # 浮层没启动时 update 是安全的 no-op 状态（fail-open）
    state = svc.overlay_update(doc["id"], groups=["buttons"], selected="global:pot")
    check(state["ok"] and not state["overlay"]["running"], f"浮层状态：{state}")

    result = svc.restore_backup(doc["id"], backup_name)
    check(result["ok"], f"恢复结果：{result}")
    check(profile.read_text(encoding="utf-8") == original, "恢复应回到原文件")
    check(svc.calibration(doc["id"])["draft"] is None, "恢复后草稿清空")

    try:
        svc.restore_backup(doc["id"], "../../etc/passwd")
    except WorkbenchError as exc:
        check(exc.status == 400, "非法备份名应 400")
    else:
        check(False, "非法备份名必须被拒绝")


def test_slots_probe_uses_live_geometry() -> None:
    """标定帧必须和 live 一样过 crop/AspectFit，而不是源原图。"""
    tmp = _tmp()
    svc = _service(tmp)
    doc = svc.create_workspace({
        "from_profile": "config/table_wpk_mac.json",
        "name": "Probe",
        "source": {"kind": "file", "spec": "table-8p.jpg"}})
    layout = svc.slots(doc["id"], probe=True)
    w, h = layout["frame_size"]
    check(w != 1280 or h != 2844,
          f"probe 后应是处理后的帧，不是源原图：{layout['frame_size']}")
    check(abs(w / h - 1472 / 1676) < 0.01,
          f"处理后的比例应与 profile 一致：{w}×{h}")
    labels = [r["label"] for r in layout["diagnostics"]["rows"]]
    check("缩放" in labels, f"诊断应带缩放行：{labels}")
    check(layout["diagnostics"]["aspect"]["level"] == "pass",
          "处理后的比例应与 profile 一致（pass）")


def test_configure_window_one_click() -> None:
    tmp = _tmp()
    calls = []

    def fake_resize(spec, width, height, *, x=None, y=None):
        calls.append((spec, width, height, x, y))
        return {"ok": True, "matched": True, "position_matched": True,
                "before": {"x": 0, "y": 33, "w": 736, "h": 841},
                "after": {"x": x or 0, "y": y or 0,
                          "w": width, "h": height}}

    svc = WorkbenchService(
        root=ROOT, workspaces_dir=tmp / "ws", runs_dir=tmp / "runs",
        preflight_context=_ctx(tmp), window_resizer=fake_resize)
    doc = svc.create_workspace({
        "from_profile": str(PROFILE),
        "source": {"kind": "window", "spec": "window:0,33,736,841"},
        "window": {"spec": "0,33,736,841", "expected_size": [700, 800]},
    })
    res = svc.configure_window(doc["id"], target=[736, 838])
    check(res["ok"] and res["requested"] == [736, 838], f"调整结果：{res}")
    check(calls[-1][1:] == (736, 838, 0.0, 0.0),
          f"应调用 resize（默认位置 0,0）：{calls[-1]}")
    updated = svc.get_workspace(doc["id"])
    check(updated["window"]["expected_size"] == [736, 838],
          f"应记录期望尺寸：{updated['window']}")
    check(updated["window"]["pinned_rect"]["w"] == 736,
          f"应记录实际 rect：{updated['window']}")
    check(updated["window"]["position"] == [0, 0],
          f"应记录位置：{updated['window']}")

    # 预览：显式 spec 和「空 spec=跟随窗口源」都要解析对
    state = svc.window_state(doc["id"], spec="0,33,736,838")
    check(state["window"]["w"] == 736,
          f"预览应返回窗口 rect：{state}")
    state = svc.window_state(doc["id"], spec="")
    check(state["window"]["w"] == 736 and state["window"]["x"] == 0,
          f"空 spec 应跟随窗口源：{state}")
    state = svc.window_state(doc["id"], spec="0,44,700,800")
    check(state["window"]["x"] == 0 and state["window"]["y"] == 44,
          f"预览应使用界面 spec：{state}")

    # 界面上改了窗口但还没保存：一键调整按新窗口来，并顺手落库
    res = svc.configure_window(doc["id"], target=[720, 820],
                               position=[120, 60],
                               spec="window:0,33,720,820")
    check(calls[-1][0] == "window:0,33,720,820",
          f"应按界面窗口调整：{calls[-1]}")
    check(calls[-1][3:] == (120.0, 60.0),
          f"应把位置传给 resize：{calls[-1]}")
    check(res["requested_position"] == [120.0, 60.0],
          f"返回应带目标位置：{res}")
    check(svc.get_workspace(doc["id"])["window"]["spec"] == "0,33,720,820",
          "应把界面窗口写回工作区")

    # 不传 target 时用工作区期望尺寸（上一步已写成 720×820）
    res = svc.configure_window(doc["id"])
    check(res["requested"] == [720, 820] and "标定期望" in res["basis"],
          f"应读工作区期望：{res}")
    res = svc.configure_window(doc["id"], target=[736, 838])
    check(res["requested"] == [736, 838], f"再按新目标调整：{res}")

    # 期望尺寸为空：按 profile 参考比例 + 当前窗口宽度推算（736 → 838）
    doc2 = svc.create_workspace({
        "from_profile": str(PROFILE), "name": "无期望尺寸",
        "source": {"kind": "window", "spec": "window:title:WePoker-H5"},
        "window": {"spec": "0,33,736,841", "expected_size": None},
    })
    res = svc.configure_window(doc2["id"])
    check(res["requested"] == [736, 838],
          f"应按参考比例推算目标：{res}")

    # 失败要带权限修复提示
    def fail_resize(spec, width, height, *, x=None, y=None):
        return {"ok": False, "error": "not allowed assistive access",
                "fix": "系统设置 → 隐私与安全性 → 辅助功能"}

    svc_fail = WorkbenchService(
        root=ROOT, workspaces_dir=tmp / "ws2", runs_dir=tmp / "runs2",
        preflight_context=_ctx(tmp / "runs2"), window_resizer=fail_resize)
    doc3 = svc_fail.create_workspace({
        "from_profile": str(PROFILE),
        "source": {"kind": "window", "spec": "window:title:WePoker-H5"},
        "window": {"spec": "0,33,736,841", "expected_size": [736, 838]},
    })
    try:
        svc_fail.configure_window(doc3["id"])
    except WorkbenchError as exc:
        check(exc.status == 502, f"失败应 502：{exc.status}")
        check("辅助功能" in str((exc.detail or {}).get("fix", "")),
              f"应带修复提示：{exc.detail}")
    else:
        check(False, "调整失败必须报错")


def test_configure_window_reports_position_mismatch() -> None:
    """尺寸 1px 在容忍内，但位置没到位时必须说清是位置问题，不能只报尺寸。"""
    tmp = _tmp()

    def offset_resize(spec, width, height, *, x=None, y=None):
        return {"ok": True, "matched": False, "position_matched": False,
                "before": {"x": 122, "y": 78, "w": 736, "h": 839},
                "after": {"x": 122, "y": 78, "w": 736, "h": 839,
                          "owner": "Microsoft Edge", "title": "知乎"}}

    svc = WorkbenchService(
        root=ROOT, workspaces_dir=tmp / "ws", runs_dir=tmp / "runs",
        preflight_context=_ctx(tmp), window_resizer=offset_resize)
    doc = svc.create_workspace({
        "from_profile": str(PROFILE),
        "source": {"kind": "window", "spec": "window:title:WePoker-H5"},
        "window": {"spec": "id:2354", "expected_size": [736, 838],
                   "position": [0, 33]},
    })
    res = svc.configure_window(doc["id"])
    check(res["size_matched"] is True, f"1px 尺寸差应在容忍内：{res}")
    check(res["position_matched"] is False, f"位置应判不匹配：{res}")
    check(res["matched"] is False, "整体不应算匹配")
    check("位置目标" in res["note"] and "(122, 78)" in res["note"],
          f"note 应说清位置差异：{res['note']}")
    check(res["after"]["title"] == "知乎", "应带回实际窗口标题")


def test_resize_script_sets_size_before_position() -> None:
    from poker_ocr.workbench import sources
    script = sources._resize_script("Microsoft Edge", "WePoker-H5", 0, 33,
                                    736, 838)
    check(script.index("set size of w") < script.index("set position of w"),
          "应先 resize 再 set position（避免 resize 把位置重置）")


def test_service_start_requires_force_on_warn() -> None:
    tmp = _tmp()
    svc = _service(tmp)
    doc = svc.create_workspace({"from_profile": str(PROFILE),
                                "source": {"kind": "window",
                                           "spec": "window:title:WePoker-H5"}})
    # 制造一条警告：窗口尺寸与期望不一致
    svc.update_workspace(doc["id"], {"window": {"expected_size": [800, 900]}})
    try:
        svc.start(doc["id"], probe=False)
    except WorkbenchError as exc:
        check(exc.status == 409, f"警告应 409：{exc.status}")
        check(exc.detail.get("needs_confirm"), "应要求确认")
    else:
        check(False, "有警告时必须先确认")


def test_http_routes_and_token() -> None:
    from fastapi.testclient import TestClient

    tmp = _tmp()
    svc = _service(tmp)
    from poker_ocr.workbench.app import create_app
    app = create_app(service=svc, token="test-token-123")
    client = TestClient(app)

    health = client.get("/api/health").json()
    check(health.get("ok") is True, "health")
    check(health.get("stale") is False, f"刚启动不应提示 stale：{health}")
    page = client.get("/")
    check(page.status_code == 200 and "window.__WB_TOKEN__" in page.text,
          "首页应可访问并注入 token")
    check("test-token-123" in page.text, "token 应注入页面")

    body = {"from_profile": str(PROFILE),
            "source": {"kind": "window", "spec": "window:title:WePoker-H5"}}
    resp = client.post("/api/workspaces", json=body)
    check(resp.status_code == 403, f"无 token 写操作应 403：{resp.status_code}")

    resp = client.post("/api/workspaces", json=body,
                       headers={"X-Workbench-Token": "test-token-123"})
    check(resp.status_code == 200, f"带 token 应成功：{resp.text[:200]}")
    ws = resp.json()

    resp = client.post("/api/workspaces", json=body,
                       headers={"X-Workbench-Token": "test-token-123",
                                "Origin": "http://evil.example"})
    check(resp.status_code == 403, "跨源写请求应被拒绝")

    listed = client.get("/api/workspaces").json()["workspaces"]
    check(len(listed) == 1, "列表应有 1 个")
    status = client.get(f"/api/workspaces/{ws['id']}/status").json()
    check(status["state"] == "idle", "status idle")
    slots = client.get(f"/api/workspaces/{ws['id']}/slots").json()
    check(slots["slots"], "slots 接口")

    resp = client.post(f"/api/workspaces/{ws['id']}/calibration/edit",
                       json={"key": "global:pot", "slot": [0.1, 0.2, 0.3, 0.4]},
                       headers={"X-Workbench-Token": "test-token-123"})
    check(resp.status_code == 200, f"编辑草稿：{resp.text[:200]}")
    cal = client.get(f"/api/workspaces/{ws['id']}/calibration").json()
    check(cal["draft"]["count"] == 1, "草稿计数")

    resp = client.delete(f"/api/workspaces/{ws['id']}",
                         headers={"X-Workbench-Token": "test-token-123"})
    check(resp.status_code == 200, "删除工作区")


def main() -> int:
    tests = [
        test_workspace_validation, test_workspace_crud, test_profile_discovery,
        test_geometry_layout_and_errors,
        test_preflight_pass_and_warn, test_preflight_click_config_without_slider,
        test_window_id_spec_is_stable_across_titles,
        test_slider_window_follows_source_or_workspace,
        test_build_live_command_slider_window,
        test_preflight_window_and_permissions,
        test_build_live_command, test_supervisor_lifecycle_and_manifest,
        test_supervisor_rejects_second_run,
        test_draft_undo_redo_diff, test_apply_edits_backup_and_preserve_notes,
        test_calibration_trial_with_fake_engine,
        test_service_workspace_and_status, test_service_calibration_apply_no_run,
        test_service_restore_and_overlay_update,
        test_slots_probe_uses_live_geometry,
        test_configure_window_one_click,
        test_configure_window_reports_position_mismatch,
        test_resize_script_sets_size_before_position,
        test_service_start_requires_force_on_warn, test_http_routes_and_token,
    ]
    print("── 工作台 ────────────────────────────────────")
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 工作台全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
