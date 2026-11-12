"""工作台编排层：HTTP 薄壳之下真正干活的地方（可直接单测）。

职责边界：

  · 工作区 CRUD / profile 发现（`WorkspaceStore`）
  · 体检（`preflight`）
  · 启动 / 停止 / 重启 / 暂停点击（`LiveSupervisor` + `live_client`）
  · 预览帧、ROI 布局、几何诊断
  · 标定：草稿 → 试读 → diff → 备份 → 原子写 → 自动重启（失败回滚）
  · 屏幕浮层（`OverlayController`）

所有会失败的路径都抛 `WorkbenchError`（带 HTTP 状态码和结构化 detail），
路由层只负责转 JSON。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..profile import Profile
from . import calibration, geometry, live_client, preflight, sources
from .calibration import (CalibrationEngine, CalibrationError, Draft,
                          DraftStore, apply_edits, restore_backup)
from .overlay_ctl import OverlayController
from .preflight import PreflightContext, run_preflight, summarize
from .store import (PROJECT_ROOT, WorkspaceStore, default_workspace,
                    normalize_workspace, slugify, validate_workspace,
                    _profile_defaults)
from .supervisor import LiveSupervisor, list_runs


class WorkbenchError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400,
                 detail: dict | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"ok": False, "error": str(self), **self.detail}


def _sha256(path: Path) -> str | None:
    import hashlib
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


class WorkbenchService:
    def __init__(self, *, root: Path | str = PROJECT_ROOT,
                 workspaces_dir: Path | str | None = None,
                 runs_dir: Path | str | None = None,
                 python: str | None = None,
                 spawn=None,
                 supervisor: LiveSupervisor | None = None,
                 window_resizer=None,
                 preflight_context: PreflightContext | None = None,
                 overlay: OverlayController | None = None,
                 log=None) -> None:
        self.root = Path(root)
        self.runs_dir = Path(runs_dir or (self.root / "runs"))
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir = Path(workspaces_dir
                                   or (self.root / "workspaces"))
        self.store = WorkspaceStore(self.workspaces_dir,
                                    project_root=self.root)
        self.supervisor = supervisor or LiveSupervisor(
            self.root, runs_dir=self.runs_dir, python=python,
            spawn=spawn, log=log)
        self.drafts = DraftStore(self.runs_dir / ".drafts")
        self.ocr = CalibrationEngine()
        self.overlay = overlay or OverlayController(log=log)
        # 一键调整窗口（测试注入假实现，避免真的动用户浏览器）
        self._resize_window = window_resizer or sources.resize_window
        self.ctx = preflight_context or PreflightContext(
            root=self.root, runs_dir=self.runs_dir)
        self._log = log or (lambda _msg: None)
        self._lock = threading.RLock()
        self._preflight_cache: dict[str, dict] = {}
        self._frame_cache: dict[str, dict] = {}

    # ─────────────────────────── 工作区 ───────────────────────────
    def list_workspaces(self) -> list[dict]:
        out = []
        running = self.supervisor.status().get("run")
        for doc in self.store.list_raw():
            doc = normalize_workspace(doc, root=self.root)
            ident = str(doc.get("id") or "")
            errors, warnings = validate_workspace(doc, root=self.root)
            profile, problems = self._inspect_profile(doc)
            last = self._last_run(ident)
            item = {
                **doc,
                "id": ident,
                "errors": errors,
                "warnings": warnings,
                "profile_info": self._profile_summary(profile, problems,
                                                      doc),
                "last_run": self._run_summary(last),
                "running": bool(running
                                and running.get("workspace_id") == ident
                                and running.get("active")),
            }
            out.append(item)
        return out

    def get_workspace(self, workspace_id: str) -> dict:
        try:
            doc = self.store.get(workspace_id)
        except KeyError as exc:
            raise WorkbenchError(str(exc), status=404) from exc
        doc = normalize_workspace(doc, root=self.root)
        errors, warnings = validate_workspace(doc, root=self.root)
        profile, problems = self._inspect_profile(doc)
        draft = self.drafts.get(workspace_id)
        return {
            **doc,
            "id": workspace_id,
            "errors": errors,
            "warnings": warnings,
            "profile_info": self._profile_summary(profile, problems, doc),
            "last_run": self._run_summary(self._last_run(workspace_id)),
            "runs": [self._run_summary(r) for r in
                     list_runs(self.runs_dir, workspace_id=workspace_id,
                               limit=8)],
            "draft": self._draft_info(draft, profile),
        }

    def create_workspace(self, body: dict) -> dict:
        body = dict(body or {})
        from_profile = str(body.pop("from_profile", "") or "").strip()
        explicit_id = "id" in body
        wanted_name = str(body.pop("name", "") or "").strip() or None
        if from_profile:
            profile_path = Path(from_profile)
            if not profile_path.is_absolute():
                profile_path = self.root / profile_path
            if not profile_path.is_file():
                raise WorkbenchError(f"profile 不存在：{from_profile}",
                                     status=400)
            defaults = _profile_defaults(profile_path, self.root)
            doc = default_workspace(
                from_profile, name=wanted_name, root=self.root,
                expected_size=defaults.get("expected_size"))
            for key in ("id", "source", "window", "click", "recognizer",
                        "full_config", "bet_slider_config", "runtime",
                        "debug"):
                if key not in body:
                    continue
                value = body.pop(key)
                if isinstance(value, dict) and isinstance(doc.get(key), dict):
                    doc[key] = {**doc[key], **value}
                else:
                    doc[key] = value
            doc.update(body)
        else:
            doc = body
            if wanted_name and not doc.get("name"):
                doc["name"] = wanted_name
        doc.setdefault("schema", 1)
        # 同名/同 profile 再建一个时不要撞车：自动加 -2/-3 后缀。
        if from_profile and not explicit_id:
            doc["id"] = slugify(wanted_name or Path(from_profile).stem)
        base_id = str(doc.get("id") or "").strip()
        if from_profile and base_id and not explicit_id:
            candidate, n = base_id, 2
            while self.store.exists(candidate):
                candidate = f"{base_id}-{n}"
                n += 1
            doc["id"] = candidate
        try:
            created = self.store.create(doc)
        except ValueError as exc:
            raise WorkbenchError(str(exc), status=400) from exc
        except FileExistsError as exc:
            raise WorkbenchError(str(exc), status=409) from exc
        return self.get_workspace(created["id"])

    def update_workspace(self, workspace_id: str, patch: dict) -> dict:
        try:
            self.store.update(workspace_id, patch)
        except KeyError as exc:
            raise WorkbenchError(str(exc), status=404) from exc
        except ValueError as exc:
            raise WorkbenchError(str(exc), status=400,
                                 detail={"issues": []}) from exc
        return self.get_workspace(workspace_id)

    def delete_workspace(self, workspace_id: str) -> dict:
        status = self.supervisor.status().get("run") or {}
        if status.get("workspace_id") == workspace_id and status.get("active"):
            raise WorkbenchError("工作区正在运行，先停止再删除", status=409)
        try:
            self.store.delete(workspace_id)
        except KeyError as exc:
            raise WorkbenchError(str(exc), status=404) from exc
        self.drafts.drop(workspace_id)
        self._preflight_cache.pop(workspace_id, None)
        return {"ok": True, "deleted": workspace_id}

    def duplicate_workspace(self, workspace_id: str, *, name: str | None = None
                            ) -> dict:
        try:
            doc = self.store.duplicate(workspace_id, name=name)
        except KeyError as exc:
            raise WorkbenchError(str(exc), status=404) from exc
        except ValueError as exc:
            raise WorkbenchError(str(exc), status=400) from exc
        return self.get_workspace(doc["id"])

    # ─────────────────────────── profile / 源 ───────────────────────────
    def profiles(self) -> list[dict]:
        return self.store.discover_profiles()

    def default_workspace_for(self, profile_rel: str) -> dict:
        profile_path = Path(profile_rel)
        if not profile_path.is_absolute():
            profile_path = self.root / profile_path
        if not profile_path.is_file():
            raise WorkbenchError(f"profile 不存在：{profile_rel}", status=404)
        defaults = _profile_defaults(profile_path, self.root)
        doc = default_workspace(profile_rel, root=self.root,
                                expected_size=defaults.get("expected_size"))
        return normalize_workspace(doc, root=self.root)

    def windows(self) -> dict:
        items, error = sources.list_windows_safe()
        return {"windows": items, "error": error}

    def cameras(self) -> dict:
        items, error = sources.list_cameras()
        return {"cameras": items, "error": error}

    def probe_source(self, spec: str) -> dict:
        result = sources.grab_frame(spec)
        return {"ok": bool(result.get("ok")), "size": result.get("size"),
                "kind": result.get("kind"), "error": result.get("error"),
                "elapsed_ms": result.get("elapsed_ms")}

    def pin_window(self, workspace_id: str) -> dict:
        doc = self.store.get(workspace_id)
        game = self._game_window_spec(doc)
        info = sources.window_info(game) if game else None
        if not info:
            raise WorkbenchError(
                "解析不到游戏窗口：当前源不是窗口，且工作区没填「窗口 → 游戏窗口」",
                status=400)
        rect = {"x": info["x"], "y": info["y"],
                "w": info["w"], "h": info["h"]}
        updated = self.store.update(workspace_id, {
            "window": {"expected_size": [round(info["w"]), round(info["h"])],
                       "position": [round(info["x"]), round(info["y"])],
                       "pinned_rect": rect, "pin": "manual"},
        })
        return {"ok": True, "window": rect,
                "workspace": updated,
                "note": "尺寸已记录；标定/体检会按这个 rect 核对，"
                        "live 运行中每 10s 也会重读窗口"}

    def window_state(self, workspace_id: str,
                     spec: str | None = None) -> dict:
        """当前游戏窗口预览（一键设置前确认目标没选错）。"""
        doc = self._doc(workspace_id)
        explicit = (sources.normalize_window_spec(spec)
                    if spec is not None else None)
        if explicit:
            game = sources.as_window_spec(explicit)
        elif spec is not None:
            # 界面留空 = 窗口源跟随源；非窗口源没有可跟随的窗口。
            source_spec = str((doc.get("source") or {}).get("spec") or "")
            game = (sources.as_window_spec(source_spec)
                    if source_spec.startswith(("window:", "screen:")) else "")
        else:
            game = self._game_window_spec(doc)
        if not game:
            raise WorkbenchError(
                "没有游戏窗口：先在「窗口」里填 title:... / index:N，"
                "或把源改成 window:", status=400)
        info = sources.window_info(game)
        if not info:
            raise WorkbenchError(
                f"解析不到窗口：{sources.normalize_window_spec(game)}",
                status=400,
                detail={"fix": "打开游戏窗口，或点「枚举窗口」重新选"})
        saved = doc.get("window") or {}
        return {"ok": True,
                "spec": sources.normalize_window_spec(game),
                "window": info,
                "target": {
                    "position": saved.get("position") or [0, 0],
                    "expected_size": saved.get("expected_size"),
                }}

    def configure_window(self, workspace_id: str,
                         target: list | None = None,
                         spec: str | None = None,
                         position: list | None = None) -> dict:
        """一键把游戏窗口调整为标定尺寸（显式用户动作，失败可解释）。

        `spec`：界面里当前填的游戏窗口（可能还没保存）。传了就按它调整并
        写回工作区；不传则用已保存的 `window.spec`/源。
        """
        doc = self._doc(workspace_id)
        explicit_spec = (sources.normalize_window_spec(spec)
                         if spec is not None else None)
        source_kind = str((doc.get("source") or {}).get("kind") or "")
        if spec is not None and not explicit_spec and source_kind != "window":
            # 用户把「游戏窗口」清空了：非窗口源没有可跟随的源，必须显式指定。
            raise WorkbenchError(
                "当前源不是窗口：请先选游戏窗口（不能留空）", status=400,
                detail={"fix": "点「枚举窗口」选 title:... / index:N，"
                               "或把源改成 window:"})
        if explicit_spec:
            game = sources.as_window_spec(explicit_spec)
        elif spec is not None:
            # 界面把窗口清空 = 窗口源跟随源（不是继续用已保存的旧 spec）。
            source_spec = str((doc.get("source") or {}).get("spec") or "")
            game = (sources.as_window_spec(source_spec)
                    if source_spec.startswith(("window:", "screen:")) else "")
        else:
            game = self._game_window_spec(doc)
        if not game:
            raise WorkbenchError(
                "没有游戏窗口：先在「窗口」里填 title:... / index:N，"
                "或把源改成 window:", status=400)
        profile = self._profile(doc)
        info = sources.window_info(game)
        if not info:
            raise WorkbenchError(
                f"解析不到窗口：{sources.normalize_window_spec(game)}",
                status=400,
                detail={"fix": "打开游戏窗口，或点「枚举窗口」重新选"})
        expected = (doc.get("window") or {}).get("expected_size")
        basis = "标定期望尺寸"
        if isinstance(target, (list, tuple)) and len(target) == 2:
            try:
                target_size = [int(round(float(target[0]))),
                               int(round(float(target[1])))]
            except (TypeError, ValueError):
                raise WorkbenchError("target 必须是两个数字", status=400)
            basis = "本次界面填写的尺寸"
        elif expected and len(expected) == 2:
            target_size = [int(round(float(expected[0]))),
                           int(round(float(expected[1])))]
        else:
            ref_w, ref_h = profile.reference_size
            width = int(round(float(info["w"])))
            target_size = [width, max(1, int(round(width * ref_h / ref_w)))]
            basis = "profile 参考比例（按当前窗口宽度推算）"
        if target_size[0] <= 0 or target_size[1] <= 0:
            raise WorkbenchError("目标尺寸必须 > 0", status=400)

        if isinstance(position, (list, tuple)) and len(position) == 2:
            try:
                target_position = [float(position[0]), float(position[1])]
            except (TypeError, ValueError):
                raise WorkbenchError("position 必须是两个数字", status=400)
            position_basis = "本次界面填写的位置"
        else:
            saved = (doc.get("window") or {}).get("position")
            if (isinstance(saved, (list, tuple)) and len(saved) == 2
                    and all(isinstance(v, (int, float)) for v in saved)):
                target_position = [float(saved[0]), float(saved[1])]
            else:
                target_position = [0.0, 0.0]
            position_basis = "工作区/默认 (0,0)"

        result = self._resize_window(game, target_size[0], target_size[1],
                                     x=target_position[0],
                                     y=target_position[1])
        if not result.get("ok"):
            raise WorkbenchError(
                result.get("error") or "调整窗口失败", status=502,
                detail={"fix": result.get("fix"),
                        "before": result.get("before"),
                        "requested": target_size})
        after = result.get("after") or {}
        patch = {"expected_size": target_size,
                 "position": [round(target_position[0], 3),
                              round(target_position[1], 3)],
                 "pin": "manual"}
        if explicit_spec is not None:
            # 用户在「游戏窗口」里改了但还没保存时，一键调整顺手把它落库。
            patch["spec"] = explicit_spec or None
        if after:
            patch["pinned_rect"] = {
                "x": after.get("x"), "y": after.get("y"),
                "w": after.get("w"), "h": after.get("h")}
        updated = self.store.update(workspace_id, {"window": patch})
        actual = ([round(float(after.get("w"))), round(float(after.get("h")))]
                  if after and after.get("w") and after.get("h") else target_size)
        size_matched = bool(
            after and abs(float(after.get("w") or 0) - target_size[0]) <= 2
            and abs(float(after.get("h") or 0) - target_size[1]) <= 2) \
            if after else False
        pos_matched = bool(result.get("position_matched", True))
        matched = size_matched and pos_matched
        actual_position = ([round(float(after.get("x")), 1),
                            round(float(after.get("y")), 1)]
                           if after and after.get("x") is not None
                           and after.get("y") is not None else None)
        if matched:
            note = "窗口已按目标尺寸和位置调整"
        else:
            parts = []
            if not size_matched:
                parts.append(
                    f"尺寸目标 {target_size[0]}×{target_size[1]}，"
                    f"实际 {actual[0]}×{actual[1]}")
            if not pos_matched:
                actual_pos = (f"({actual_position[0]:g}, {actual_position[1]:g})"
                              if actual_position else "未知")
                parts.append(
                    f"位置目标 ({target_position[0]:g}, "
                    f"{target_position[1]:g})，实际 {actual_pos}")
            note = "；".join(parts) + "（可能受窗口最小尺寸/权限/系统约束）"
        return {"ok": True, "requested": target_size,
                "requested_position": target_position,
                "position_basis": position_basis,
                "basis": basis, "before": result.get("before"),
                "after": after, "actual": actual,
                "actual_position": actual_position,
                "matched": matched,
                "size_matched": size_matched,
                "position_matched": pos_matched,
                "note": note, "workspace": updated}

    # ─────────────────────────── 体检 ───────────────────────────
    def preflight(self, workspace_id: str, *, probe: bool = True) -> dict:
        doc = self._doc(workspace_id)
        checks = run_preflight(doc, ctx=self.ctx, probe=probe)
        result = {"workspace_id": workspace_id, "checks": checks,
                  "summary": summarize(checks),
                  "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        with self._lock:
            self._preflight_cache[workspace_id] = result
        return result

    def cached_preflight(self, workspace_id: str) -> dict | None:
        with self._lock:
            return self._preflight_cache.get(workspace_id)

    # ─────────────────────────── 启动 / 停止 ───────────────────────────
    def start(self, workspace_id: str, *, force: bool = False,
              probe: bool = True) -> dict:
        doc = self._doc(workspace_id)
        with self._lock:
            run = self.supervisor.status().get("run") or {}
            if run.get("workspace_id") == workspace_id and run.get("active"):
                raise WorkbenchError("工作区已在运行", status=409)
            if run.get("active"):
                raise WorkbenchError(
                    f"已有另一路 live 在运行（{run.get('workspace_id')}），"
                    "v1 一次只跑一路，先停止", status=409)
            checks = run_preflight(doc, ctx=self.ctx, probe=probe)
            summary = summarize(checks)
            self._preflight_cache[workspace_id] = {
                "workspace_id": workspace_id, "checks": checks,
                "summary": summary,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            if summary["counts"]["fail"]:
                # fail 永远阻断（设计 §9）：force 只用来接受 warn。
                raise WorkbenchError(
                    "体检未通过，不能启动",
                    status=409,
                    detail={"preflight": {"checks": checks,
                                          "summary": summary}})
            if summary["counts"]["warn"] and not force:
                raise WorkbenchError(
                    "体检有警告，需要确认「仍然启动」",
                    status=409,
                    detail={"needs_confirm": True,
                            "preflight": {"checks": checks,
                                          "summary": summary}})
            self.overlay.stop()
            try:
                handle = self.supervisor.start(
                    doc, preflight={"checks": checks, "summary": summary},
                    workspace_snapshot=doc)
            except RuntimeError as exc:
                raise WorkbenchError(str(exc), status=409) from exc
        if (doc.get("debug") or {}).get("screen_overlay"):
            try:
                self.overlay.start(self._profile(doc),
                    self._game_window_spec(doc),
                    groups=list((doc.get("debug") or {}).get("overlay_groups")
                                or []),
                    drafts=self._draft_slots(workspace_id))
            except Exception as exc:  # noqa: BLE001 — 浮层失败不影响启动
                self._log(f"[工作台] 浮层启动失败：{exc}")
        return self.status(workspace_id)

    def stop(self, workspace_id: str) -> dict:
        self.overlay.stop()
        run = self.supervisor.status().get("run") or {}
        if run.get("workspace_id") not in (None, workspace_id):
            raise WorkbenchError("当前运行的不是这个工作区", status=409)
        self.supervisor.stop()
        return self.status(workspace_id)

    def restart(self, workspace_id: str, *, force: bool = True) -> dict:
        self.stop(workspace_id)
        return self.start(workspace_id, force=force, probe=False)

    # ─────────────────────────── 状态 ───────────────────────────
    def status(self, workspace_id: str) -> dict:
        state = self.supervisor.refresh()
        run = state.get("run")
        belongs = bool(run and run.get("workspace_id") == workspace_id)
        run_public = dict(run) if run else None
        if run_public:
            run_public.pop("meta", None)      # meta 单独返回，别在 run 里重复一遍
        if not belongs:
            return {
                "workspace_id": workspace_id,
                "state": "idle",
                "run": None,
                "other_run": run_public,
                "overlay": self.overlay.state(),
                "preflight": self.cached_preflight(workspace_id),
                "log_tail": [],
                "click": {"available": False, "enabled": False,
                          "paused": False},
            }
        snapshot = live_client.snapshot(run["port"]) if run.get("alive") else None
        meta = run.get("meta")
        control = (live_client.control_state(run["port"])
                   if run.get("alive") else None)
        if control:
            run["status"] = ("paused_click" if control.get("paused")
                             else "running") \
                if run.get("status") in ("running", "paused_click") else run["status"]
        state_name = run.get("status") if run else "idle"
        if run_public is not None:
            run_public["status"] = state_name
            run_public["active"] = bool(run.get("active"))
        return {
            "workspace_id": workspace_id,
            "state": state_name,
            "run": run_public,
            "meta": meta,
            "stats": (run.get("stats") or {}) if run else {},
            "click": control or (meta or {}).get("click") or {},
            "frame": (meta or {}).get("frame"),
            "snapshot": self._prune_snapshot(snapshot),
            "live_url": f"http://127.0.0.1:{run['port']}" if run else None,
            "log_tail": (self.supervisor.log_tail(limit=30).splitlines()
                         if belongs else []),
            "preflight": self.cached_preflight(workspace_id),
            "overlay": self.overlay.state(),
            "draft": (self._draft_info(self.drafts.get(workspace_id),
                                       self._inspect_profile(
                                           self._doc(workspace_id))[0])
                      if self.store.exists(workspace_id) else None),
        }

    def click(self, workspace_id: str, enabled: bool) -> dict:
        run = self.supervisor.status().get("run") or {}
        if not run or run.get("workspace_id") != workspace_id:
            raise WorkbenchError("工作区没有在运行", status=409)
        if not run.get("alive"):
            raise WorkbenchError("live 进程已退出", status=409)
        try:
            state = live_client.set_click(
                run["port"], self.supervisor.current().token, bool(enabled),
                reason="workbench")
        except live_client.LiveClientError as exc:
            raise WorkbenchError(str(exc), status=502) from exc
        if not state.get("ok") and not state.get("available"):
            raise WorkbenchError(
                state.get("detail") or "本次运行未启用自动点击", status=409)
        return state

    # ─────────────────────────── 预览 / 布局 ───────────────────────────
    def preview_jpeg(self, workspace_id: str, *, width: int,
                     full: bool = False) -> tuple[bytes, dict]:
        run = self.supervisor.status().get("run") or {}
        if not run or run.get("workspace_id") != workspace_id:
            raise WorkbenchError("工作区没有在运行", status=409)
        try:
            data = live_client.preview(run["port"], width=width, full=full)
        except live_client.LiveClientError as exc:
            raise WorkbenchError(str(exc), status=502) from exc
        return data, {"port": run["port"], "run_id": run["run_id"]}

    def frame_jpeg(self, workspace_id: str, *, max_age: float = 2.0
                   ) -> tuple[bytes, dict]:
        """标定用冻结帧：运行中走 live 预览，否则自己抓一帧（立即释放源）。"""
        run = self.supervisor.status().get("run") or {}
        if run and run.get("workspace_id") == workspace_id and run.get("alive"):
            try:
                data = live_client.preview(run["port"], full=True)
                meta = run.get("meta") or {}
                size = (meta.get("frame") or {}).get("width"), \
                    (meta.get("frame") or {}).get("height")
                return data, {"source": "live", "size": list(size),
                              "ts": time.time()}
            except live_client.LiveClientError:
                pass
        cached = self._frame_cache.get(workspace_id)
        if cached and time.time() - cached["ts"] <= max_age:
            return cached["data"], {"source": cached["source"],
                                    "size": cached["size"],
                                    "ts": cached["ts"]}
        doc = self._doc(workspace_id)
        spec = str((doc.get("source") or {}).get("spec") or "")
        result = sources.grab_frame(spec)
        if not result.get("ok"):
            raise WorkbenchError(result.get("error") or "抓不到画面",
                                 status=503)
        import cv2
        frame = self._prepare_like_live(doc, result["frame"])
        size = [int(frame.shape[1]), int(frame.shape[0])]
        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            raise WorkbenchError("预览帧编码失败", status=500)
        data = bytes(buf.tobytes())
        with self._lock:
            self._frame_cache[workspace_id] = {
                "ts": time.time(), "data": data, "size": size,
                "source": "source", "frame": frame}
        return data, {"source": "source", "size": size, "ts": time.time()}

    def _frozen_frame(self, workspace_id: str, *, max_age: float = 2.0):
        """拿 ndarray 冻结帧（试读用）；优先缓存，避免重复开摄像头。"""
        run = self.supervisor.status().get("run") or {}
        if run and run.get("workspace_id") == workspace_id and run.get("alive"):
            try:
                import cv2
                import numpy as np
                data = live_client.preview(run["port"], full=True)
                arr = np.frombuffer(data, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    size = [int(frame.shape[1]), int(frame.shape[0])]
                    with self._lock:
                        self._frame_cache[workspace_id] = {
                            "ts": time.time(), "data": data, "size": size,
                            "source": "live", "frame": frame}
                    return frame, size
            except Exception:  # noqa: BLE001
                pass
        cached = self._frame_cache.get(workspace_id)
        if cached and time.time() - cached["ts"] <= max_age and "frame" in cached:
            return cached["frame"], cached["size"]
        doc = self._doc(workspace_id)
        result = sources.grab_frame(str((doc.get("source") or {}).get("spec") or ""))
        if not result.get("ok"):
            raise WorkbenchError(result.get("error") or "抓不到画面", status=503)
        frame = self._prepare_like_live(doc, result["frame"])
        import cv2
        size = (int(frame.shape[1]), int(frame.shape[0]))
        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        data = bytes(buf.tobytes()) if ok else b""
        with self._lock:
            self._frame_cache[workspace_id] = {
                "ts": time.time(), "data": data,
                "size": list(size), "source": "source", "frame": frame}
        return frame, size

    def slots(self, workspace_id: str, *, probe: bool = False) -> dict:
        doc = self._doc(workspace_id)
        profile = self._profile(doc)
        run = self.supervisor.status().get("run") or {}
        live_meta = (run.get("meta")
                     if run.get("workspace_id") == workspace_id else None)
        frame_size = None
        source_size = None
        window = (live_meta or {}).get("source", {}).get("window")
        if live_meta:
            frame = live_meta.get("frame") or {}
            if frame.get("width"):
                frame_size = (int(frame["width"]), int(frame["height"]))
            src = (live_meta.get("source") or {}).get("frame_size")
            if src:
                source_size = tuple(src)
        if frame_size is None:
            cached = self._frame_cache.get(workspace_id)
            if cached and cached.get("size"):
                source_size = tuple(cached["size"])
                frame_size = tuple(cached["size"])
        if frame_size is None and probe:
            # 只有标定/诊断显式要求时才开源（摄像头/RTSP 可能要几秒），
            # 抓到就缓存，`frame.jpg` 随后直接用它。
            try:
                frame, size = self._frozen_frame(workspace_id, max_age=1.0)
                source_size = tuple(size)
                frame_size = (int(frame.shape[1]), int(frame.shape[0]))
            except WorkbenchError:
                pass
        if frame_size is None:
            # 不为了画框去开源：先用参考尺寸，标定页打开时会抓冻结帧并刷新。
            frame_size = tuple(int(v) for v in profile.reference_size)
        # 点击/浮层用的是「游戏窗口」，可能与识别源不同（识别走 OBS 摄像头、
        # 点击仍落在浏览器窗口）。窗口源未填时自动跟随源。
        game = self._game_window_spec(doc)
        if game:
            window = sources.window_info(game) or window
        elif window is None:
            window = sources.window_info(
                str((doc.get("source") or {}).get("spec") or ""))
        if window:
            win = {"x": window["x"], "y": window["y"],
                   "w": window["w"], "h": window["h"],
                   "expected_size": (doc.get("window") or {}).get("expected_size")}
        else:
            win = None
        layout = geometry.layout(profile, frame_size)
        draft = self.drafts.get(workspace_id)
        draft_edits = dict(draft.edits) if draft else {}
        layout.update({
            "workspace_id": workspace_id,
            "profile": str(profile.path),
            "profile_sha256": _sha256(profile.path),
            "live_profile_sha256": (live_meta or {}).get("profile", {}).get(
                "sha256"),
            "drafts": draft_edits,
            "diagnostics": geometry.diagnostics(
                profile, source_size=source_size, frame_size=frame_size,
                window=win),
            "source": (doc.get("source") or {}),
            "editable_m1": list(geometry.M1_EDITABLE),
        })
        return layout

    def diagnostics(self, workspace_id: str) -> dict:
        return self.slots(workspace_id).get("diagnostics") or {}

    # ─────────────────────────── 标定 ───────────────────────────
    def _draft_or_create(self, workspace_id: str, profile: Profile) -> Draft:
        draft = self.drafts.get(workspace_id)
        if draft is None:
            draft = Draft(
                workspace_id,
                profile=str(profile.path),
                base_sha256=_sha256(profile.path),
            )
        return draft

    def _draft_slots(self, workspace_id: str) -> dict:
        draft = self.drafts.get(workspace_id)
        return dict(draft.edits) if draft else {}

    def _draft_info(self, draft: Draft | None,
                    profile: Profile | None) -> dict | None:
        if draft is None:
            return None
        diff = draft.diff(profile) if profile is not None else []
        return {**draft.to_dict(), "diff": diff,
                "count": len(draft.edits),
                "can_undo": bool(draft.undo_stack),
                "can_redo": bool(draft.redo_stack)}

    def calibration(self, workspace_id: str) -> dict:
        doc = self._doc(workspace_id)
        profile = self._profile(doc)
        draft = self.drafts.get(workspace_id)
        layout = self.slots(workspace_id)
        return {
            "workspace_id": workspace_id,
            "profile": str(profile.path),
            "profile_sha256": _sha256(profile.path),
            "draft": self._draft_info(draft, profile),
            "layout": layout,
            "editable_m1": list(geometry.M1_EDITABLE),
            "backups": self._list_backups(Path(profile.path)),
        }

    @staticmethod
    def _list_backups(path: Path) -> list[str]:
        try:
            return [p.name for p in sorted(
                path.parent.glob(f"{path.name}.bak-*"),
                key=lambda p: p.stat().st_mtime, reverse=True)][:5]
        except OSError:
            return []

    def calibration_edit(self, workspace_id: str, key: str,
                         slot) -> dict:
        doc = self._doc(workspace_id)
        profile = self._profile(doc)
        draft = self._draft_or_create(workspace_id, profile)
        try:
            draft.set(key, slot)
        except CalibrationError as exc:
            raise WorkbenchError(str(exc), status=400,
                                 detail={"problems": exc.problems}) from exc
        self.drafts.put(draft)
        self._refresh_overlay_draft(workspace_id, draft)
        return {"ok": True, "draft": self._draft_info(draft, profile)}

    def calibration_undo(self, workspace_id: str) -> dict:
        profile = self._profile(self._doc(workspace_id))
        draft = self.drafts.get(workspace_id)
        if draft is None or not draft.undo():
            raise WorkbenchError("没有可撤销的编辑", status=400)
        self.drafts.put(draft)
        self._refresh_overlay_draft(workspace_id, draft)
        return {"ok": True, "draft": self._draft_info(draft, profile)}

    def calibration_redo(self, workspace_id: str) -> dict:
        profile = self._profile(self._doc(workspace_id))
        draft = self.drafts.get(workspace_id)
        if draft is None or not draft.redo():
            raise WorkbenchError("没有可重做的编辑", status=400)
        self.drafts.put(draft)
        self._refresh_overlay_draft(workspace_id, draft)
        return {"ok": True, "draft": self._draft_info(draft, profile)}

    def calibration_reset(self, workspace_id: str) -> dict:
        profile = self._profile(self._doc(workspace_id))
        draft = self.drafts.get(workspace_id)
        if draft is not None:
            draft.reset()
            self.drafts.put(draft)
            self._refresh_overlay_draft(workspace_id, draft)
        return {"ok": True,
                "draft": self._draft_info(draft, profile) if draft else None}

    def calibration_trial(self, workspace_id: str, key: str,
                          slot=None) -> dict:
        profile = self._profile(self._doc(workspace_id))
        frame, _size = self._frozen_frame(workspace_id, max_age=3.0)
        try:
            return self.ocr.trial(profile, frame, key, slot)
        except CalibrationError as exc:
            raise WorkbenchError(str(exc), status=400,
                                 detail={"problems": exc.problems}) from exc

    def calibration_apply(self, workspace_id: str) -> dict:
        doc = self._doc(workspace_id)
        profile = self._profile(doc)
        draft = self.drafts.get(workspace_id)
        if draft is None or not draft.edits:
            raise WorkbenchError("草稿是空的，没有要应用的改动", status=400)
        edits = dict(draft.edits)
        try:
            diff = draft.diff(profile)
        except Exception:  # noqa: BLE001
            diff = []
        run = self.supervisor.status().get("run") or {}
        was_running = bool(run.get("workspace_id") == workspace_id
                           and run.get("active"))
        # 保存前先停 live：正在跑的进程用旧配置，改文件不会热加载。
        if was_running:
            self.stop(workspace_id)
        try:
            result = apply_edits(profile.path, edits)
        except CalibrationError as exc:
            # 校验失败：不写、不重启、草稿保留
            if was_running:
                self._log("[工作台] 保存失败，按原配置重启")
                try:
                    self.start(workspace_id, force=True, probe=False)
                except WorkbenchError as exc2:
                    self._log(f"[工作台] 原配置也启动失败：{exc2}")
            raise WorkbenchError(str(exc), status=400,
                                 detail={"problems": exc.problems,
                                         "draft": self._draft_info(
                                             draft, profile)}) from exc
        restarted = False
        restart_error = None
        if was_running:
            try:
                self.start(workspace_id, force=True, probe=False)
                restarted = True
            except WorkbenchError as exc:
                restart_error = str(exc)
                # 回滚：恢复备份 + 用旧配置重启
                try:
                    restore_backup(profile.path, result["backup"])
                except CalibrationError as exc2:
                    raise WorkbenchError(
                        f"新配置启动失败且回滚失败：{exc2}",
                        status=500,
                        detail={"run_error": restart_error,
                                "diff": diff}) from exc2
                try:
                    self.start(workspace_id, force=True, probe=False)
                except WorkbenchError as exc2:
                    self._log(f"[工作台] 回滚后仍启动失败：{exc2}")
                raise WorkbenchError(
                    "新配置启动失败，已回滚并重启旧配置",
                    status=500,
                    detail={"run_error": restart_error,
                            "rollback": True,
                            "backup": result["backup"],
                            "diff": diff,
                            "draft": self._draft_info(draft, profile)})
        self.drafts.drop(workspace_id)
        self._frame_cache.pop(workspace_id, None)
        return {
            "ok": True,
            "applied": result["applied"],
            "diff": result["diff"] or diff,
            "backup": result["backup"],
            "sha256": result["sha256"],
            "restarted": restarted,
            "run": self.status(workspace_id).get("run"),
            "draft": None,
        }

    def _refresh_overlay_draft(self, workspace_id: str, draft: Draft) -> None:
        try:
            if self.overlay.state().get("running"):
                self.overlay.update(drafts=dict(draft.edits))
        except Exception:  # noqa: BLE001
            pass

    # ─────────────────────────── 浮层 ───────────────────────────
    def overlay_control(self, workspace_id: str, *, enabled: bool,
                        groups: list[str] | None = None,
                        selected: str | None = None) -> dict:
        doc = self._doc(workspace_id)
        if not enabled:
            return {"ok": True, "overlay": self.overlay.stop()}
        profile = self._profile(doc)
        spec = self._game_window_spec(doc)
        draft = self.drafts.get(workspace_id)
        state = self.overlay.start(
            profile, spec, groups=groups or
            list((doc.get("debug") or {}).get("overlay_groups") or []),
            selected=selected, drafts=dict(draft.edits) if draft else {})
        if not state.get("running"):
            raise WorkbenchError(
                state.get("error") or "浮层启动失败", status=503,
                detail={"overlay": state})
        return {"ok": True, "overlay": state}

    def overlay_update(self, workspace_id: str, *, groups: list[str] | None = None,
                       selected: str | None = None) -> dict:
        state = self.overlay.update(groups=groups, selected=selected)
        return {"ok": True, "overlay": state}

    def restore_backup(self, workspace_id: str, backup_name: str) -> dict:
        """从备份恢复 profile（M3 回滚入口）：停 live → 写回 → 重启。"""
        doc = self._doc(workspace_id)
        profile = self._profile(doc)
        name = str(backup_name or "").strip()
        if not name or "/" in name or "\\" in name or not name.startswith(
                profile.path.name + ".bak-"):
            raise WorkbenchError("非法的备份文件名", status=400)
        backup = profile.path.with_name(name)
        if not backup.is_file():
            raise WorkbenchError(f"备份不存在：{name}", status=404)
        run = self.supervisor.status().get("run") or {}
        was_running = bool(run.get("workspace_id") == workspace_id
                           and run.get("active"))
        if was_running:
            self.stop(workspace_id)
        result = restore_backup(profile.path, backup)
        self.drafts.drop(workspace_id)
        self._frame_cache.pop(workspace_id, None)
        if was_running:
            self.start(workspace_id, force=True, probe=False)
        return {"ok": True, **result, "restarted": was_running}

    # ─────────────────────────── 历史 ───────────────────────────
    def runs(self, workspace_id: str, *, limit: int = 50) -> list[dict]:
        return [self._run_summary(r) for r in
                list_runs(self.runs_dir, workspace_id=workspace_id,
                          limit=limit)]

    def manifest(self, run_id: str) -> dict:
        for run in list_runs(self.runs_dir, limit=1000):
            if str(run.get("run_id")) == run_id:
                return run
        raise WorkbenchError(f"找不到运行 {run_id}", status=404)

    # ─────────────────────────── 退出 ───────────────────────────
    def shutdown(self) -> None:
        try:
            self.overlay.stop()
        finally:
            self.supervisor.shutdown()

    def _prepare_like_live(self, doc: dict, frame):
        """把源帧按 live 的口径预处理；标定看到的就是识别看到的那一帧。

        live 主循环对每一帧做 rotate → plan_crop(auto) → AspectFit(roi)；
        工作台在 live 停止时抓的是源原图，如果直接拿来标定，就会和 live 的
        几何差一个裁剪/缩放（典型：摄像头 16:9 里放竖屏牌桌）。这里复用同一
        组函数，live 运行中则直接用它的 `/api/preview.jpg`，不重复处理。
        """
        try:
            from ..live import AspectFit, plan_crop, rotate_frame
            profile = self._profile(doc)
            advanced = (doc.get("runtime") or {}).get("advanced") or {}
            rotate = int(advanced.get("rotate") or 0)
            if rotate:
                frame = rotate_frame(frame, rotate)
            crop = str(advanced.get("crop") or "auto")
            plan = plan_crop(frame.shape[:2], profile, crop, image=frame)
            out = plan.apply(frame)
            if (doc.get("recognizer") or "roi") == "roi":
                fit = AspectFit.for_size(plan.out_size, profile)
                if fit is not None:
                    out = fit.apply(out)
            return out
        except Exception:  # noqa: BLE001 — 标定帧预处理失败就退回原图
            return frame

    # ─────────────────────────── 内部 ───────────────────────────
    @staticmethod
    def _game_window_spec(doc: dict) -> str:
        """点击/浮层用的游戏窗口（`window:` 开头的源规格）。

        `window.spec` 显式指定优先；否则窗口源跟随源。非窗口源又没有
        `window.spec` 时返回空串（调用方按「解析不到窗口」处理）。
        """
        explicit = sources.normalize_window_spec(
            (doc.get("window") or {}).get("spec"))
        if explicit:
            return sources.as_window_spec(explicit)
        source = str((doc.get("source") or {}).get("spec") or "")
        if source.startswith(("window:", "screen:")):
            return source
        return ""

    def _doc(self, workspace_id: str) -> dict:
        try:
            return normalize_workspace(self.store.get(workspace_id),
                                       root=self.root)
        except KeyError as exc:
            raise WorkbenchError(str(exc), status=404) from exc

    def _profile(self, doc: dict) -> Profile:
        path = Path(str(doc.get("profile") or ""))
        if not path.is_absolute():
            path = self.root / path
        profile, problems = Profile.inspect(path)
        if profile is None:
            raise WorkbenchError(
                "profile 加载失败：" + "；".join(problems), status=400,
                detail={"problems": problems})
        return profile

    def _inspect_profile(self, doc: dict) -> tuple[Profile | None, list[str]]:
        path = Path(str(doc.get("profile") or ""))
        if not path.is_absolute():
            path = self.root / path
        return Profile.inspect(path)

    def _profile_summary(self, profile: Profile | None, problems: list[str],
                         doc: dict) -> dict:
        path = Path(str(doc.get("profile") or ""))
        if not path.is_absolute():
            path = self.root / path
        info = {"path": str(path), "ok": profile is not None and not problems,
                "problems": problems, "reference_size": None, "seats": 0,
                "hero_seat_id": None, "aspect": None}
        if profile is not None:
            info.update({
                "reference_size": [int(v) for v in profile.reference_size],
                "seats": len(profile.seats),
                "hero_seat_id": profile.hero_seat_id,
                "aspect": round(profile.reference_size[0]
                                / max(1, profile.reference_size[1]), 6),
                "clickable": all(k in profile.global_slots for k in (
                    "hero_fold_button", "hero_call_button")),
                "slider": all(k in profile.global_slots for k in (
                    "hero_bet_slider", "hero_bet_amount",
                    "hero_bet_confirm")),
            })
        return info

    def _last_run(self, workspace_id: str) -> dict | None:
        runs = list_runs(self.runs_dir, workspace_id=workspace_id, limit=1)
        return runs[0] if runs else None

    def _run_summary(self, run: dict | None) -> dict | None:
        if not run:
            return None
        stats = run.get("stats") or {}
        processed = int(stats.get("processed") or 0)
        accepted = int(stats.get("accepted") or 0)
        discarded = max(0, processed - accepted)
        started = str(run.get("started_at") or "")
        ended = str(run.get("ended_at") or "")
        duration = None
        if started and ended:
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(started)
                t1 = datetime.fromisoformat(ended)
                duration = int((t1 - t0).total_seconds())
            except (ValueError, TypeError):
                duration = None
        return {
            "run_id": run.get("run_id"),
            "workspace_id": run.get("workspace_id"),
            "started_at": started or None,
            "ended_at": ended or None,
            "status": run.get("status"),
            "exit_code": run.get("exit_code"),
            "port": run.get("port"),
            "duration_seconds": duration,
            "processed": processed,
            "accepted": accepted,
            "discard_rate": round(discarded / processed, 3) if processed else None,
            "avg_proc_ms": stats.get("avg_proc_ms"),
            "error": run.get("error"),
            "dir": run.get("dir"),
        }

    @staticmethod
    def _prune_snapshot(snapshot: dict | None) -> dict | None:
        if not snapshot:
            return None
        frame = snapshot.get("frame") or {}
        return {
            "type": snapshot.get("type"),
            "ts": snapshot.get("ts"),
            "session": snapshot.get("session"),
            "stats": snapshot.get("stats"),
            "frame": {k: frame.get(k) for k in
                      ("seq", "frame_id", "ts", "t", "kind", "proc_ms",
                       "queue_ms", "errors", "hand_id", "street", "state",
                       "cards")},
            "table": snapshot.get("table"),
            "cards": snapshot.get("cards"),
            "advice": snapshot.get("advice"),
            "pending": snapshot.get("pending"),
            "hands": (snapshot.get("hands") or [])[:5],
            "events": (snapshot.get("events") or [])[-40:],
            "control": snapshot.get("control"),
            "ended_at": snapshot.get("ended_at"),
        }
