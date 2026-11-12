"""工作区（Workspace）存储与校验。

一个工作区 = 一次启动所需的一切：profile 路径、视频源、窗口预期、点击策略、
运行参数、调试开关。它**只引用** `config/table_*.json`，不复制几何。

文件在 `workspaces/<id>.json`（建议提交到仓库，不含密钥）。写入一律
临时文件 + `os.replace`；读取时补齐默认值并保留未知字段（向前兼容）。
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ..profile import Profile

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
DEFAULT_WORKSPACES = PROJECT_ROOT / "workspaces"

SOURCE_KINDS = ("window", "camera", "rtsp", "file")
RECOGNIZERS = ("roi", "full")
CLICK_MODES = ("profile_slots", "legacy_clicker")
WINDOW_PINS = ("manual",)
GROUPS = ("seats", "actions", "cards", "pots", "buttons", "sliders")

TOP_LEVEL_KEYS = {
    "schema", "id", "name", "profile", "source", "window", "click",
    "recognizer", "full_config", "bet_slider_config", "runtime", "debug",
    "created_at", "updated_at",
}
RUNTIME_KEYS = {
    "initial_blinds", "every", "settle", "ai_advice", "test", "advanced",
}
ADVANCED_BOOL_KEYS = {
    "stable_reset_on_discard", "record_without_hero", "no_dedup", "no_fold",
    "lenient_chips",
}
ADVANCED_KEYS = {
    "stable_frames", "stable_seconds", "stable_reset_on_discard",
    "dealer_carry", "card_confirm_frames", "card_doubt_window",
    "max_frame_age", "roi_threads", "no_dedup", "no_fold", "crop", "rotate",
    "max_vanish", "name_confirm", "incident_frames", "incident_max",
    "min_advice_confidence", "cross_check", "ai_timeout", "ai_confirm",
    "record_without_hero", "lenient_chips",
}

_BLINDS_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?\s*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    """id 用的小写 slug；中文等全部落空时退回随机后缀。"""
    base = _SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)[:48].strip("-")
    return base or f"ws-{secrets.token_hex(3)}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_size(value: Any) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_is_number(v) and float(v) > 0 for v in value))


def _issue(field: str, reason: str, level: str) -> dict:
    return {"field": field, "reason": reason, "level": level}


def default_workspace(profile_path: str | Path, *, name: str | None = None,
                      source_spec: str = "", source_kind: str = "window",
                      source_label: str = "", expected_size: Iterable | None = None,
                      root: Path = PROJECT_ROOT) -> dict:
    """从 profile 生成一份可编辑的工作区草稿（不落盘）。"""
    path = Path(profile_path)
    if not path.is_absolute():
        path = Path(root) / path
    label = str(name or path.stem.replace("table_", "").replace("_", " ")).strip()
    expected = list(expected_size) if expected_size else None
    stamp = now_iso()
    return {
        "schema": 1,
        "id": slugify(path.stem.replace("table_", "")),
        "name": label or path.stem,
        "profile": _relpath(path, Path(root)),
        "source": {"kind": source_kind, "spec": source_spec,
                   "label": source_label},
        # spec = 滑杆点击/浮层要落到的**游戏窗口**；窗口源留空时跟随源。
        # 非窗口源（camera/rtsp/file）必须显式填：OBS 只是采集画面，
        # 鼠标仍然要按屏幕坐标点到浏览器窗口上。
        "window": {"spec": None, "expected_size": expected, "pin": "manual"},
        "click": {"enabled": True, "mode": "profile_slots",
                  "dry_run": True, "confirm": True,
                  "bet_slider": True, "bet_slider_only": False},
        "recognizer": "roi",
        "runtime": {"initial_blinds": "1/2", "every": 0.5, "settle": 0.25,
                    "ai_advice": True, "test": False, "advanced": {}},
        "debug": {"preview_fps": 3, "screen_overlay": True,
                  "overlay_groups": ["buttons", "cards"]},
        "created_at": stamp,
        "updated_at": stamp,
    }


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


def _profile_defaults(profile_path: Path, root: Path) -> dict:
    """从 profile 和 bet_slider 配置推导默认值（窗口尺寸 / 盲注 / 名称）。"""
    profile, problems = Profile.inspect(profile_path)
    if profile is None:
        return {"reference_size": None, "seats": 0, "problems": problems,
                "expected_size": None}
    expected = None
    slider_cfg = Path(root) / "config" / "bet_slider_mac.json"
    try:
        raw = json.loads(slider_cfg.read_text(encoding="utf-8"))
        if str(raw.get("profile") or "").strip() and \
                Path(str(raw["profile"])).name == profile_path.name:
            window = raw.get("window") or {}
            if all(_is_number(window.get(k)) for k in ("w", "h")):
                expected = [int(window["w"]), int(window["h"])]
    except (OSError, ValueError):
        pass
    # 没有滑杆窗口可参考时**不猜**期望尺寸：reference_size 是像素，窗口是逻辑点
    # （Retina 2x 下差一倍），写错会让体检一直报窗口漂移。留给「固定窗口」记录。
    return {"reference_size": [int(v) for v in profile.reference_size],
            "seats": len(profile.seats), "problems": problems,
            "expected_size": expected}


# ─────────────────────────── 校验 ───────────────────────────

def validate_workspace(doc: dict, *, root: Path = PROJECT_ROOT
                       ) -> tuple[list[dict], list[dict]]:
    """返回 (errors, warnings)。字段路径 + 原因 + 级别，不抛异常。"""
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(field: str, reason: str) -> None:
        errors.append(_issue(field, reason, "error"))

    def warn(field: str, reason: str) -> None:
        warnings.append(_issue(field, reason, "warn"))

    if not isinstance(doc, dict):
        return [_issue("", "工作区必须是 JSON 对象", "error")], []

    if doc.get("schema") != 1:
        err("schema", f"schema 只支持 1，收到 {doc.get('schema')!r}")
    unknown = sorted(set(doc) - TOP_LEVEL_KEYS)
    if unknown:
        warn("", "未知字段（会被保留但不生效）：" + "、".join(unknown))

    ident = str(doc.get("id") or "").strip()
    if not ident:
        err("id", "id 不能为空")
    elif not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", ident):
        err("id", "id 只能用小写字母/数字/._-，最长 64")
    if not str(doc.get("name") or "").strip():
        warn("name", "名称为空，列表里不好辨认")

    profile = str(doc.get("profile") or "").strip()
    if not profile:
        err("profile", "必须引用一个 profile 路径")
    elif not profile.endswith(".json"):
        err("profile", "profile 必须是 .json 文件")
    else:
        path = Path(profile)
        if not path.is_absolute():
            path = Path(root) / path
        if not path.is_file():
            err("profile", f"profile 不存在：{profile}")

    source = doc.get("source")
    if not isinstance(source, dict):
        err("source", "source 必须是对象 {kind, spec, label}")
    else:
        kind = str(source.get("kind") or "").strip()
        if kind not in SOURCE_KINDS:
            err("source.kind", f"kind 只能是 {'/'.join(SOURCE_KINDS)}，收到 {kind!r}")
        spec = str(source.get("spec") or "").strip()
        if not spec:
            err("source.spec", "视频源不能为空")
        else:
            from .sources import parse_source_spec
            parsed = parse_source_spec(spec)
            if not parsed["valid"]:
                err("source.spec", parsed["detail"])
            elif parsed["kind"] != kind:
                warn("source.kind",
                     f"spec 看起来是 {parsed['kind']}，工作区写的是 {kind}")

    window = doc.get("window")
    if not isinstance(window, dict):
        warn("window", "缺 window 段（窗口体检会跳过）")
    else:
        spec = window.get("spec")
        if spec is not None and not isinstance(spec, str):
            err("window.spec", "spec 必须是字符串（如 title:WePoker-H5）或 null")
        elif isinstance(spec, str) and spec.strip():
            from .sources import as_window_spec, parse_source_spec
            parsed_window = parse_source_spec(as_window_spec(spec))
            if not parsed_window["valid"]:
                err("window.spec",
                    parsed_window["detail"] or "窗口写法不合法")
        elif isinstance(spec, str) and not spec.strip():
            # 空串等价于「跟随源」，但显式写空串容易让人误会，给个警告。
            warn("window.spec", "空字符串等价于 null（窗口源跟随源）")
        size = window.get("expected_size")
        if size is not None and not _is_size(size):
            err("window.expected_size", "expected_size 必须是两个正数 [w, h]")
        if window.get("pin") not in (None, *WINDOW_PINS):
            err("window.pin", f"pin 只支持 {WINDOW_PINS}")
        rect = window.get("pinned_rect")
        if rect is not None:
            if (not isinstance(rect, dict)
                    or not all(_is_number(rect.get(k))
                               for k in ("x", "y", "w", "h"))):
                err("window.pinned_rect", "pinned_rect 必须是 {x,y,w,h}")

    click = doc.get("click")
    if not isinstance(click, dict):
        err("click", "click 必须是对象")
    else:
        if not isinstance(click.get("enabled"), bool):
            err("click.enabled", "enabled 必须是 true/false")
        mode = click.get("mode", "profile_slots")
        if mode not in CLICK_MODES:
            err("click.mode", f"mode 只能是 {'/'.join(CLICK_MODES)}")
        for key in ("dry_run", "confirm", "bet_slider", "bet_slider_only"):
            if not isinstance(click.get(key), bool):
                err(f"click.{key}", f"{key} 必须是 true/false")
        if click.get("bet_slider_only") and not click.get("bet_slider"):
            err("click.bet_slider_only", "bet_slider_only 需要 bet_slider=true")


    recognizer = doc.get("recognizer", "roi")
    if recognizer not in RECOGNIZERS:
        err("recognizer", f"recognizer 只能是 {'/'.join(RECOGNIZERS)}")
    if recognizer == "full" and not str(doc.get("full_config") or "").strip():
        err("full_config", "recognizer=full 时必须指定 full 配置")

    runtime = doc.get("runtime")
    if not isinstance(runtime, dict):
        err("runtime", "runtime 必须是对象")
    else:
        blinds = runtime.get("initial_blinds")
        if blinds not in (None, "") and not _BLINDS_RE.match(str(blinds)):
            err("runtime.initial_blinds", "initial_blinds 要写成 1/2 这样的比值")
        for key in ("every", "settle"):
            value = runtime.get(key, 0)
            if not _is_number(value) or float(value) < 0:
                err(f"runtime.{key}", f"{key} 必须是非负数")
        for key in ("ai_advice", "test"):
            if not isinstance(runtime.get(key), bool):
                err(f"runtime.{key}", f"{key} 必须是 true/false")
        advanced = runtime.get("advanced") or {}
        if not isinstance(advanced, dict):
            err("runtime.advanced", "advanced 必须是对象")
        else:
            bad = sorted(set(advanced) - ADVANCED_KEYS)
            if bad:
                err("runtime.advanced", "不支持的参数：" + "、".join(bad))
            for key, value in advanced.items():
                if key in ADVANCED_BOOL_KEYS:
                    if not isinstance(value, bool):
                        err(f"runtime.advanced.{key}", "必须是 true/false")
                elif key == "rotate":
                    if value not in (0, 90, 180, 270):
                        err("runtime.advanced.rotate", "rotate 只能是 0/90/180/270")
                elif key == "crop":
                    if value not in ("auto", "left", "none"):
                        err("runtime.advanced.crop", "crop 只能是 auto/left/none")
                elif key == "cross_check":
                    if value not in ("off", "warn", "block"):
                        err("runtime.advanced.cross_check",
                            "cross_check 只能是 off/warn/block")
                elif not _is_number(value):
                    err(f"runtime.advanced.{key}", "必须是数字")

    debug = doc.get("debug")
    if not isinstance(debug, dict):
        warn("debug", "缺 debug 段（用默认值）")
    else:
        fps = debug.get("preview_fps", 3)
        if not _is_number(fps) or not 1 <= float(fps) <= 10:
            err("debug.preview_fps", "preview_fps 应在 1–10 之间")
        if not isinstance(debug.get("screen_overlay"), bool):
            err("debug.screen_overlay", "screen_overlay 必须是 true/false")
        groups = debug.get("overlay_groups") or []
        if not isinstance(groups, list) or any(g not in GROUPS for g in groups):
            err("debug.overlay_groups", "overlay_groups 只能取 " + "/".join(GROUPS))

    return errors, warnings


def normalize_workspace(doc: dict, *, root: Path = PROJECT_ROOT,
                        profile_defaults: Callable[[Path], dict] | None = None
                        ) -> dict:
    """补齐缺省字段、修正常见类型问题；未知字段原样保留。"""
    out = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    base = default_workspace(out.get("profile") or "config/table_default.json",
                             root=root)
    for key, value in base.items():
        out.setdefault(key, value)
    if not str(out.get("id") or "").strip():
        out["id"] = slugify(Path(str(out.get("profile") or "ws")).stem)
    if not str(out.get("name") or "").strip():
        out["name"] = Path(str(out.get("profile") or "ws")).stem

    profile_path = Path(str(out.get("profile")))
    if not profile_path.is_absolute():
        profile_path = Path(root) / profile_path
    defaults = (profile_defaults or (lambda p: _profile_defaults(p, Path(root))))(
        profile_path)
    expected = defaults.get("expected_size")
    window = out.get("window") or {}
    if not isinstance(window, dict):
        window = {}
    if window.get("expected_size") is None and expected:
        window["expected_size"] = expected
    window.setdefault("spec", None)
    window.setdefault("pin", "manual")
    out["window"] = window

    for section, defaults_section in (("source", base["source"]),
                                      ("click", base["click"]),
                                      ("runtime", base["runtime"]),
                                      ("debug", base["debug"])):
        if not isinstance(out.get(section), dict):
            out[section] = copy.deepcopy(defaults_section)
        else:
            for key, value in defaults_section.items():
                out[section].setdefault(key, copy.deepcopy(value))
    advanced = out["runtime"].get("advanced")
    if not isinstance(advanced, dict):
        out["runtime"]["advanced"] = {}
    # 兼容本字段落地前写过的 click.slider_window：迁移到 window.spec。
    click = out.get("click") or {}
    alias = click.pop("slider_window", None) if isinstance(click, dict) else None
    if alias and not window.get("spec"):
        window["spec"] = alias
    out.setdefault("created_at", now_iso())
    return out


# ─────────────────────────── 存储 ───────────────────────────

class WorkspaceStore:
    """`workspaces/*.json` 的读写；所有写入原子替换。"""

    def __init__(self, root: Path | str = DEFAULT_WORKSPACES, *,
                 project_root: Path | str = PROJECT_ROOT) -> None:
        self.root = Path(root)
        self.project_root = Path(project_root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ── 读 ──────────────────────────────────────────────
    def path_for(self, workspace_id: str) -> Path:
        ident = str(workspace_id or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", ident):
            raise ValueError(f"非法工作区 id：{workspace_id!r}")
        return self.root / f"{ident}.json"

    def list_raw(self) -> list[dict]:
        docs = []
        for path in sorted(self.root.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict):
                doc.setdefault("id", path.stem)
                docs.append(doc)
        docs.sort(key=lambda d: str(d.get("updated_at") or ""), reverse=True)
        return docs

    def get(self, workspace_id: str) -> dict:
        path = self.path_for(workspace_id)
        if not path.is_file():
            raise KeyError(f"工作区不存在：{workspace_id}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.setdefault("id", workspace_id)
        return doc

    def exists(self, workspace_id: str) -> bool:
        try:
            return self.path_for(workspace_id).is_file()
        except ValueError:
            return False

    # ── 写 ──────────────────────────────────────────────
    def create(self, doc: dict, *, overwrite: bool = False) -> dict:
        normalized = normalize_workspace(doc, root=self.project_root)
        errors, _ = validate_workspace(normalized, root=self.project_root)
        if errors:
            raise ValueError(_format_issues(errors))
        path = self.path_for(normalized["id"])
        if path.exists() and not overwrite:
            raise FileExistsError(f"工作区已存在：{normalized['id']}")
        normalized["created_at"] = now_iso()
        normalized["updated_at"] = now_iso()
        self._write(path, normalized)
        return normalized

    def update(self, workspace_id: str, patch: dict) -> dict:
        current = self.get(workspace_id)
        # 只做一层深合并：source/click/runtime/debug/window 各自整体替换字段。
        merged = copy.deepcopy(current)
        for key, value in (patch or {}).items():
            if key == "id":
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        # 从窗口源切到摄像头/RTSP/文件源时，把原窗口记成游戏窗口：
        # 识别源可以变，点击/浮层仍然要落到那个浏览器窗口上。
        old_source = current.get("source") or {}
        if isinstance(patch.get("source"), dict):
            from .sources import normalize_window_spec
            new_kind = str((merged.get("source") or {}).get("kind") or "")
            window = merged.get("window") or {}
            if (str(old_source.get("kind") or "") == "window"
                    and new_kind != "window"
                    and not (window.get("spec") or "").strip()):
                inner = normalize_window_spec(old_source.get("spec"))
                if inner:
                    window = {**window, "spec": inner}
                    merged["window"] = window
        merged["id"] = workspace_id
        merged = normalize_workspace(merged, root=self.project_root)
        errors, _ = validate_workspace(merged, root=self.project_root)
        if errors:
            raise ValueError(_format_issues(errors))
        merged["updated_at"] = now_iso()
        merged.pop("_errors", None)
        self._write(self.path_for(workspace_id), merged)
        return merged

    def delete(self, workspace_id: str) -> None:
        path = self.path_for(workspace_id)
        if not path.is_file():
            raise KeyError(f"工作区不存在：{workspace_id}")
        path.unlink()

    def duplicate(self, workspace_id: str, *, name: str | None = None) -> dict:
        src = self.get(workspace_id)
        clone = copy.deepcopy(src)
        clone.pop("id", None)
        clone["name"] = name or f"{src.get('name') or workspace_id} 副本"
        # 用名字生成 id；中文名会退回随机后缀，避免覆盖原工作区。
        candidate = slugify(clone["name"])
        if candidate == src.get("id") or self.exists(candidate):
            candidate = slugify(f"{candidate}-{secrets.token_hex(2)}")
        clone["id"] = candidate
        return self.create(clone)

    def _write(self, path: Path, doc: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

    # ── 发现 profile ────────────────────────────────────
    def discover_profiles(self) -> list[dict]:
        """扫描 `config/table_*.json`，能加载的列为 profile，并打兼容标签。"""
        out = []
        for path in sorted((self.project_root / "config").glob("table_*.json")):
            profile, problems = Profile.inspect(path)
            item = {
                "path": _relpath(path, self.project_root),
                "name": path.name,
                "ok": profile is not None and not problems,
                "problems": problems,
                "reference_size": None,
                "aspect": None,
                "seats": 0,
                "hero_seat_id": None,
                "runtime": False,
                "clickable": False,
                "slider": False,
                "full": False,
            }
            if profile is not None:
                item.update({
                    "reference_size": [int(v) for v in profile.reference_size],
                    "aspect": round(profile.reference_size[0]
                                    / max(1, profile.reference_size[1]), 6),
                    "seats": len(profile.seats),
                    "hero_seat_id": profile.hero_seat_id,
                    "runtime": bool(profile.runtime),
                    "clickable": all(k in profile.global_slots for k in (
                        "hero_fold_button", "hero_call_button")),
                    "slider": all(k in profile.global_slots for k in (
                        "hero_bet_slider", "hero_bet_amount",
                        "hero_bet_confirm")),
                    "full": (not profile.seats) or bool(profile.runtime),
                })
            out.append(item)
        return out


def _format_issues(issues: list[dict]) -> str:
    return "；".join(f"{i['field'] or '工作区'}：{i['reason']}" for i in issues)
