"""启动前体检：源 / 窗口 / 配置 / 权限 / 端口 / 凭据。

每条返回 `{id, status, title, detail, fix}`：

    status = pass | warn | fail | skip

`fail` 阻断启动；`warn` 需要用户勾选「仍然启动」；`skip` 表示当前平台/
配置下这一项不适用（不算通过，也不阻断）。

所有探测都 fail-open：拿不到权限/工具时给 warn/fail + 可执行修复路径，
绝不让体检本身抛异常把页面打挂。测试通过 `PreflightContext` 注入假探测。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..env import parse_env
from ..profile import Profile
from . import geometry, sources
from .store import PROJECT_ROOT, validate_workspace

LIVE_PORT_RANGE = (8020, 8040)
REQUIRED_CLICK_SLOTS = ("hero_fold_button", "hero_call_button")
DEFAULT_BET_SLIDER_CONFIG = "config/bet_slider_mac.json"


def _check(check_id: str, status: str, title: str, detail: str = "",
           fix: str = "") -> dict:
    return {"id": check_id, "status": status, "title": title,
            "detail": detail, "fix": fix}


@dataclass
class PreflightContext:
    root: Path = PROJECT_ROOT
    probe_source: Callable[..., dict] = sources.grab_frame
    window_matches: Callable[..., tuple] = sources.window_matches
    screen_permission: Callable[[], tuple] = sources.screen_recording_ok
    accessibility: Callable[[], tuple] = sources.accessibility_ok
    automation: Callable[[], tuple] = sources.automation_ok
    port_probe: Callable[..., dict] | None = None
    env_check: Callable[[], tuple[bool, str]] | None = None
    runs_dir: Path | None = None
    extra: dict = field(default_factory=dict)


# ─────────────────────────── 探测 ───────────────────────────

def port_state(port: int) -> dict:
    """端口是否空着；被占用时看看是不是另一个 live。"""
    busy = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            busy = sock.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        busy = True
    live = None
    if busy:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{int(port)}/api/health",
                    timeout=0.6) as resp:
                live = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            live = None
    return {"port": int(port), "busy": busy, "live": live}


def probe_ports(start: int = LIVE_PORT_RANGE[0],
                end: int = LIVE_PORT_RANGE[1]) -> dict:
    free = []
    for port in range(start, end + 1):
        state = port_state(port)
        if not state["busy"]:
            free.append(port)
    current = port_state(start)
    return {"start": start, "end": end, "free": free,
            "preferred": start, "preferred_busy": bool(current["busy"]),
            "preferred_live": current["live"]}


def check_ai_credentials(root: Path = PROJECT_ROOT) -> tuple[bool, str]:
    """只看 AI 凭据是否存在，不读取/不回显具体值，也不改动进程环境。"""
    if (os.environ.get("AI_SERVER_API_KEY") or "").strip():
        return True, "环境变量里已有 AI_SERVER_API_KEY"
    env_path = Path(root) / ".env"
    if not env_path.is_file():
        return False, "没有找到 AI 凭据（环境变量和 .env 都没有 AI_SERVER_API_KEY）"
    try:
        parsed = parse_env(env_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False, f".env 读不出来（{env_path}）"
    if (parsed.get("AI_SERVER_API_KEY") or "").strip():
        return True, f".env 里有 AI_SERVER_API_KEY（{env_path.name}）"
    return False, f".env 存在但没配 AI_SERVER_API_KEY（{env_path}）"


def _output_dir_ok(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".wb_write_", dir=str(path))
        os.close(fd)
        os.unlink(tmp)
        return True, str(path)
    except OSError as exc:
        return False, f"{path}：{type(exc).__name__}: {exc}"


# ─────────────────────────── 主流程 ───────────────────────────

def run_preflight(workspace: dict, *, ctx: PreflightContext | None = None,
                  probe: bool = True) -> list[dict]:
    ctx = ctx or PreflightContext()
    root = Path(ctx.root)
    checks: list[dict] = []
    errors, warnings = validate_workspace(workspace, root=root)

    # 1. 工作区 schema
    if errors:
        checks.append(_check(
            "workspace", "fail", "工作区 schema 不合法",
            "；".join(f"{e['field'] or '工作区'}：{e['reason']}" for e in errors),
            "在工作区配置里修正标红的字段"))
    elif warnings:
        checks.append(_check(
            "workspace", "warn", "工作区 schema 有未知字段",
            "；".join(f"{w['field'] or '工作区'}：{w['reason']}" for w in warnings),
            "确认这些字段是否拼错；未知字段不生效"))
    else:
        checks.append(_check("workspace", "pass", "工作区 schema 合法"))

    # 2. profile 存在/可读
    profile_ref = str(workspace.get("profile") or "").strip()
    profile_path = Path(profile_ref)
    if not profile_path.is_absolute():
        profile_path = root / profile_path
    profile = None
    problems: list[str] = []
    if not profile_ref:
        checks.append(_check("profile_file", "fail", "没有引用 profile",
                             "工作区的 profile 为空", "选一个 config/table_*.json"))
    elif not profile_path.is_file():
        checks.append(_check("profile_file", "fail", "profile 不存在",
                             str(profile_path), "换一个 profile 或恢复文件"))
    else:
        profile, problems = Profile.inspect(profile_path)
        if profile is None:
            checks.append(_check("profile_file", "fail", "profile 读不出来",
                                 "；".join(problems), "修 JSON 或换 profile"))
        else:
            checks.append(_check(
                "profile_file", "pass", "profile 可读",
                f"{profile_path.name} · {len(profile.seats)} 座 · "
                f"{profile.reference_size[0]}×{profile.reference_size[1]}"))

    # 3. profile 校验
    if profile is None:
        checks.append(_check("profile_valid", "skip", "profile 校验",
                             "profile 没有加载成功"))
    elif problems:
        checks.append(_check(
            "profile_valid", "fail", "profile 校验失败",
            "；".join(problems[:6]) + ("…" if len(problems) > 6 else ""),
            "按提示修正 profile（标定页可改槽位）"))
    else:
        checks.append(_check("profile_valid", "pass", "profile 校验通过"))

    # 4. 源 spec
    source = workspace.get("source") or {}
    spec = str(source.get("spec") or "").strip()
    parsed = sources.parse_source_spec(spec)
    image_file = (parsed["kind"] == "file"
                  and Path(spec).suffix.lower() in sources.IMAGE_SUFFIXES)
    if image_file:
        checks.append(_check(
            "source_spec", "fail", "单张图片不能做实时源",
            str(spec) + " 是图片；live 需要连续视频流",
            "换成视频文件、窗口、摄像头或 RTSP 源（图片只适合冻结帧标定）"))
    elif parsed["valid"]:
        checks.append(_check(
            "source_spec", "pass", "源规格可解析",
            f"{sources.source_label(spec)}（{parsed['kind']}）"))
    else:
        checks.append(_check("source_spec", "fail", "源规格不合法",
                             parsed["detail"] or spec, "重新选择视频源"))

    # 5/6. 源出帧 + 比例
    frame_size = None
    if not parsed["valid"]:
        checks.append(_check("source_frame", "skip", "源出帧", "源规格不合法"))
        checks.append(_check("source_aspect", "skip", "源比例 vs profile",
                             "源规格不合法"))
    elif not probe:
        checks.append(_check("source_frame", "skip", "源出帧",
                             "本次体检未探测视频源"))
        checks.append(_check("source_aspect", "skip", "源比例 vs profile",
                             "未拿到源尺寸"))
    else:
        try:
            grab = ctx.probe_source(spec, timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            grab = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if grab.get("ok"):
            frame_size = tuple(grab.get("size") or ()) or None
            status = "pass" if (grab.get("elapsed_ms") or 0) <= 4000 else "warn"
            checks.append(_check(
                "source_frame", status, "源出帧",
                f"{frame_size[0]}×{frame_size[1]}，{grab.get('elapsed_ms', 0):.0f}ms",
                "" if status == "pass" else "出帧偏慢：检查 OBS/网络/权限"))
        else:
            checks.append(_check(
                "source_frame", "fail", "源打不开",
                str(grab.get("error") or "没有出帧"),
                "检查 OBS/MediaMTX/浏览器窗口/权限；摄像头可能被其他程序占用"))
        if frame_size and profile is not None:
            report = geometry.aspect_report(frame_size, profile.reference_size)
            checks.append(_check(
                "source_aspect", report["level"], "源比例 vs profile",
                report["text"], "换源/改 OBS 画布比例，或换匹配的 profile"))
        else:
            checks.append(_check("source_aspect", "skip", "源比例 vs profile",
                                 "没有源尺寸或 profile 未加载"))

    # 7/8. 窗口解析 + 尺寸
    expected = (workspace.get("window") or {}).get("expected_size")
    resolved = None
    if parsed["kind"] != "window":
        checks.append(_check("window_resolve", "skip", "窗口解析",
                             "当前源不是窗口"))
        checks.append(_check("window_size", "skip", "窗口尺寸",
                             "当前源不是窗口"))
    elif not parsed["valid"]:
        checks.append(_check("window_resolve", "skip", "窗口解析",
                             "源规格不合法"))
        checks.append(_check("window_size", "skip", "窗口尺寸", "源规格不合法"))
    else:
        try:
            hits, err = ctx.window_matches(spec)
        except Exception as exc:  # noqa: BLE001
            hits, err = [], f"{type(exc).__name__}: {exc}"
        if err and not hits:
            checks.append(_check("window_resolve", "fail", "窗口解析失败",
                                 err, "改用 index:N 或显式 x,y,w,h"))
        elif not hits:
            checks.append(_check("window_resolve", "fail", "没找到窗口",
                                 f"匹配串 {spec}", "打开游戏窗口，或改用 index:N"))
        elif len(hits) > 1:
            checks.append(_check(
                "window_resolve", "warn", f"命中 {len(hits)} 个窗口",
                "；".join(f"[{h['index']}] {h.get('owner')} | {h.get('title')}"
                         for h in hits[:4]),
                "改成 index:N 精确指定"))
        else:
            resolved = hits[0]
            checks.append(_check(
                "window_resolve", "pass", "窗口唯一命中",
                f"[{resolved['index']}] {resolved.get('owner')} | "
                f"{resolved.get('title')} · {resolved['x']:g},"
                f"{resolved['y']:g},{resolved['w']:g},{resolved['h']:g}"))
        if resolved is None:
            checks.append(_check(
                "window_size", "skip", "窗口尺寸",
                "窗口未唯一命中，无法核对尺寸"))
        elif expected:
            dw = float(resolved["w"]) - float(expected[0])
            dh = float(resolved["h"]) - float(expected[1])
            if abs(dw) <= 2 and abs(dh) <= 2:
                checks.append(_check("window_size", "pass", "窗口尺寸符合预期",
                                     f"偏差 {dw:+.0f} / {dh:+.0f} pt"))
            else:
                checks.append(_check(
                    "window_size", "warn", "窗口尺寸与标定不一致",
                    f"期望 {expected[0]}×{expected[1]}，实际 "
                    f"{resolved['w']:g}×{resolved['h']:g}（{dw:+.0f}/{dh:+.0f} pt）",
                    "手动调整窗口到标定尺寸，或点「固定窗口」记录当前尺寸后重新标定"))
        else:
            checks.append(_check("window_size", "warn", "窗口尺寸未设置预期",
                                 "工作区没有 expected_size", "点「固定窗口」记录"))

    # 9. 屏幕录制
    if parsed["kind"] == "window" or (workspace.get("debug") or {}).get(
            "screen_overlay"):
        try:
            ok, detail = ctx.screen_permission()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok is None:
            checks.append(_check("screen_permission", "skip", "屏幕录制权限",
                                 detail))
        else:
            checks.append(_check(
                "screen_permission", "pass" if ok else "fail", "屏幕录制权限",
                detail,
                "" if ok else "系统设置 → 隐私与安全性 → 屏幕录制"))
    else:
        checks.append(_check("screen_permission", "skip", "屏幕录制权限",
                             "当前源不需要截屏"))

    # 10. 辅助功能（点击）
    click = workspace.get("click") or {}
    if click.get("enabled") and not click.get("dry_run"):
        try:
            ok, detail = ctx.accessibility()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok is None:
            checks.append(_check("accessibility", "skip", "辅助功能（点击）",
                                 detail))
        else:
            checks.append(_check(
                "accessibility", "pass" if ok else "fail", "辅助功能（点击）",
                detail,
                "" if ok else "系统设置 → 隐私与安全性 → 辅助功能"))
    else:
        checks.append(_check("accessibility", "skip", "辅助功能（点击）",
                             "未启用真实点击（dry-run 或点击关闭）"))

    # 11. Automation（窗口固定/读取）
    if parsed["kind"] == "window":
        try:
            ok, detail = ctx.automation()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok is None:
            checks.append(_check("automation", "skip", "Automation（窗口）",
                                 detail))
        else:
            checks.append(_check(
                "automation", "pass" if ok else "warn", "Automation（窗口）",
                detail, "" if ok else "允许终端控制 System Events"))
    else:
        checks.append(_check("automation", "skip", "Automation（窗口）",
                             "当前源不是窗口"))

    # 12. AI 凭据
    if (workspace.get("runtime") or {}).get("ai_advice"):
        try:
            ok, detail = ((ctx.env_check or check_ai_credentials)())
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append(_check(
            "ai_credentials", "pass" if ok else "warn", "AI 凭据",
            detail, "" if ok else "在 .env 里配 AI_SERVER_API_KEY（或关掉 AI）"))
    else:
        checks.append(_check("ai_credentials", "skip", "AI 凭据", "AI 建议已关闭"))

    # 13. live 端口
    try:
        ports = (ctx.port_probe or probe_ports)()
    except Exception as exc:  # noqa: BLE001
        ports = {"free": [], "preferred": LIVE_PORT_RANGE[0],
                 "preferred_busy": True, "error": str(exc)}
    if not ports.get("free"):
        checks.append(_check("live_port", "fail", "没有可用的 live 端口",
                             f":{LIVE_PORT_RANGE[0]}–{LIVE_PORT_RANGE[1]} 全被占用",
                             "停掉其他 live 或释放端口"))
    elif ports.get("preferred_busy"):
        live = ports.get("preferred_live") or {}
        checks.append(_check(
            "live_port", "warn", f":{ports['preferred']} 被占用",
            ("另一个 live 在跑：" + str(live.get("run_id") or live.get("session_id"))
             if live else "端口被其他程序占用")
            + f"；将改用 :{ports['free'][0]}", "停掉旧 live，或直接启动（自动换端口）"))
    else:
        checks.append(_check("live_port", "pass", "live 端口空闲",
                             f":{ports['preferred']}（备用 "
                             f":{ports['free'][0] if ports['free'] else '-'}）"))

    # 14. 输出目录
    runs_dir = Path(ctx.runs_dir or (root / "runs"))
    ok, detail = _output_dir_ok(runs_dir)
    checks.append(_check("output_dir", "pass" if ok else "fail",
                         "运行目录可写", detail,
                         "" if ok else "换一个可写目录或修权限"))

    # 15. 滑杆窗口
    if click.get("enabled") and click.get("bet_slider"):
        cfg_rel = str(workspace.get("bet_slider_config")
                      or DEFAULT_BET_SLIDER_CONFIG)
        cfg_path = Path(cfg_rel)
        if not cfg_path.is_absolute():
            cfg_path = root / cfg_path
        if not parsed["valid"]:
            # 源还没选好时无法判断滑杆窗口该跟谁；别在这里报第二个错。
            checks.append(_check("slider_window", "skip", "滑杆窗口",
                                 "先选一个可解析的视频源"))
        elif not cfg_path.is_file():
            checks.append(_check("slider_window", "fail", "滑杆配置缺失",
                                 str(cfg_path),
                                 "恢复 config/bet_slider_mac.json"))
        else:
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                checks.append(_check("slider_window", "fail", "滑杆配置读不出",
                                     f"{type(exc).__name__}: {exc}",
                                     "修 JSON 或换配置"))
            else:
                notes: list[str] = []
                status = "pass"
                conf_profile = str(raw.get("profile") or "").strip()
                if conf_profile and Path(conf_profile).name != profile_path.name:
                    status = "warn"
                    notes.append(
                        f"滑杆配置指向 {Path(conf_profile).name}（当前 "
                        f"{profile_path.name}），确认手势参数是按这块标定的")
                source_inner = sources.normalize_window_spec(
                    source.get("spec"))
                explicit = sources.normalize_window_spec(
                    (workspace.get("window") or {}).get("spec"))
                if explicit:
                    spec, origin = explicit, "工作区窗口"
                    if (parsed["kind"] == "window" and source_inner
                            and explicit != source_inner):
                        status = "warn"
                        notes.append("游戏窗口与识别源不是同一个窗口")
                elif parsed["kind"] == "window":
                    spec, origin = source_inner, "跟随窗口源"
                else:
                    # 兼容老配置：CLI 仍会读 bet_slider_mac.json 的 window。
                    cfg_window = sources.normalize_window_spec(
                        raw.get("window"))
                    spec = cfg_window
                    origin = "滑杆配置" if cfg_window else ""
                if not spec:
                    checks.append(_check(
                        "slider_window", "fail", "滑杆窗口未指定",
                        f"当前源是 {parsed['kind']}，不会自动带窗口；"
                        "滑杆点击要按屏幕坐标落到游戏浏览器窗口上"
                        + (f"（标定期望 {expected[0]}×{expected[1]} 逻辑点）"
                           if expected else ""),
                        "在工作区「窗口 → 游戏窗口」选窗口"
                        "（摄像头/RTSP/文件源必须显式指定）"))
                else:
                    try:
                        hits, err = ctx.window_matches(
                            sources.as_window_spec(spec))
                    except Exception as exc:  # noqa: BLE001
                        hits, err = [], f"{type(exc).__name__}: {exc}"
                    if not hits:
                        detail = (f"{origin} {spec} → "
                                  + (err or "没有找到窗口"))
                        checks.append(_check(
                            "slider_window", "fail", "滑杆窗口解析失败",
                            detail + ("；" + "；".join(notes) if notes else ""),
                            "打开游戏窗口，或改用 index:N / x,y,w,h"))
                    elif len(hits) > 1:
                        checks.append(_check(
                            "slider_window", "warn", f"滑杆窗口命中 {len(hits)} 个",
                            "；".join(f"[{h['index']}] {h.get('owner')} | "
                                     f"{h.get('title')}" for h in hits[:3]),
                            "改成 index:N 精确指定"))
                    else:
                        hit = hits[0]
                        detail = (
                            f"{origin} {spec} → [{hit['index']}] "
                            f"{hit.get('owner')} | {hit.get('title')} · "
                            f"{hit['w']:g}×{hit['h']:g}")
                        if expected:
                            dw = float(hit["w"]) - float(expected[0])
                            dh = float(hit["h"]) - float(expected[1])
                            if abs(dw) > 2 or abs(dh) > 2:
                                status = "warn"
                                notes.append(
                                    f"游戏窗口尺寸与标定期望差 "
                                    f"{dw:+.0f}/{dh:+.0f}pt")
                        if notes:
                            detail += "；" + "；".join(notes)
                        checks.append(_check(
                            "slider_window", status, "滑杆窗口可解析",
                            detail,
                            "" if status == "pass" else "确认滑杆窗口与标定一致"))
    else:
        checks.append(_check("slider_window", "skip", "滑杆窗口",
                             "未启用滑杆点击"))

    # 16. 点击槽位
    if click.get("enabled"):
        if profile is None:
            checks.append(_check("click_slots", "skip", "点击槽位",
                                 "profile 未加载"))
        elif click.get("mode") == "legacy_clicker":
            checks.append(_check("click_slots", "warn", "旧式 clicker 坐标",
                                 "v1 只读兼容，不做可视化编辑",
                                 "改用 profile_slots（推荐）"))
        else:
            legacy_missing = False
            if not click.get("bet_slider"):
                legacy_cfg = click.get("clicker_config") or "config/clicker_local.json"
                legacy_path = Path(legacy_cfg)
                if not legacy_path.is_absolute():
                    legacy_path = root / legacy_path
                if not legacy_path.is_file():
                    legacy_missing = True
                    checks.append(_check(
                        "click_slots", "fail", "固定档位点击缺少 clicker 配置",
                        f"{legacy_cfg} 不存在（不开滑杆时 live 用绝对坐标点击）",
                        "开「下注/加注走长按滑杆」，或先跑 "
                        "tools/calibrate_clicker.py 生成 clicker_local.json"))
            missing = [s for s in REQUIRED_CLICK_SLOTS
                       if s not in profile.global_slots]
            if legacy_missing:
                pass
            elif missing:
                checks.append(_check(
                    "click_slots", "fail", "点击槽位缺失",
                    "缺少 " + "、".join(missing), "在标定页补按钮槽位"))
            elif click.get("bet_slider") and not all(
                    k in profile.global_slots for k in
                    ("hero_bet_slider", "hero_bet_amount", "hero_bet_confirm")):
                checks.append(_check(
                    "click_slots", "warn", "滑杆槽位不全",
                    "缺 hero_bet_slider / amount / confirm 之一",
                    "关掉滑杆（用固定档位）或补滑杆槽位"))
            else:
                checks.append(_check("click_slots", "pass", "点击槽位齐全",
                                     f"{len(profile.global_slots)} 个全局槽位"))
    else:
        checks.append(_check("click_slots", "skip", "点击槽位", "自动点击已关闭"))

    # 17. full 配置
    if workspace.get("recognizer") == "full":
        full_rel = str(workspace.get("full_config") or "").strip()
        full_path = Path(full_rel)
        if not full_path.is_absolute():
            full_path = root / full_path
        if full_path.is_file():
            checks.append(_check("full_config", "pass", "full 配置可读",
                                 str(full_path)))
        else:
            checks.append(_check("full_config", "fail", "full 配置缺失",
                                 full_rel or "未填", "选一个 full 配置"))
    else:
        checks.append(_check("full_config", "skip", "full 配置",
                             "当前用 roi 识别"))

    return checks


def summarize(checks: list[dict]) -> dict:
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for c in checks:
        counts[c.get("status", "skip")] = counts.get(c.get("status", "skip"), 0) + 1
    return {"counts": counts,
            "blocking": [c["id"] for c in checks if c["status"] == "fail"],
            "warnings": [c["id"] for c in checks if c["status"] == "warn"],
            "can_start": not counts["fail"],
            "needs_confirm": bool(counts["warn"])}
