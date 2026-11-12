"""标定：草稿（撤销/重做）、试读 OCR、diff、备份、原子写。

原则（设计 §7）：

  · **只改目标键**：加载原始 JSON dict，只更新对应槽位；`_note`、未知键、
    键顺序全部保留。
  · **结构化校验**：新建 Profile 后跑 `check()`，有问题就不写、不重启。
  · **原子写 + 备份**：临时文件 + `os.replace`；备份
    `<file>.bak-YYYYmmdd-HHMMSS`，保留最近 20 份。
  · **试读 ≠ live 读数**：工作台的 OCR 不走去重/缓存/稳定闸，结果只用来判断
    「位置对不对、字读不读得出」，显式标注 `mode="trial"`。
"""

from __future__ import annotations

import base64
import cv2
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from ..action_buttons import find_action_buttons
from ..cards import recognize_cards
from ..fields import parse_chip
from ..ocr import OcrEngine
from ..parse import (parse_action, parse_amount, parse_blinds, parse_chip_loose)
from ..profile import Profile, _slot_ok
from ..roi_reader import crop_slot, read_slot
from . import geometry

MAX_BACKUPS = 20
SEAT_JSON_FIELD = {
    "name": "name_slot",
    "chip": "chip_slot",
    "chip_alt": "chip_slot_alt",
    "avatar": "avatar_slot",
    "bet": "bet_slot",
    "action": "action_slot",
}


class CalibrationError(ValueError):
    """草稿/写入校验失败；`problems` 给页面逐条显示。"""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = list(problems or [message])


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_slot(slot) -> list[float]:
    if not isinstance(slot, (list, tuple)) or len(slot) != 4:
        raise CalibrationError(f"槽位必须是 4 个数，收到 {slot!r}")
    try:
        values = [round(float(v), 6) for v in slot]
    except (TypeError, ValueError):
        raise CalibrationError(f"槽位必须是数字：{slot!r}") from None
    if not _slot_ok(tuple(values)):
        raise CalibrationError(
            f"槽位必须满足 0 ≤ x1 < x2 ≤ 1、0 ≤ y1 < y2 ≤ 1：{values}")
    return values


# ─────────────────────────── 草稿 ───────────────────────────

class Draft:
    """一个工作区的标定草稿；持久化到 `runs/.drafts/<id>.json`。"""

    def __init__(self, workspace_id: str, *, profile: str = "",
                 base_sha256: str | None = None,
                 edits: dict | None = None,
                 undo: list | None = None, redo: list | None = None,
                 created_at: str | None = None,
                 updated_at: str | None = None) -> None:
        self.workspace_id = workspace_id
        self.profile = profile
        self.base_sha256 = base_sha256
        self.edits = {k: normalize_slot(v) for k, v in (edits or {}).items()}
        self.undo_stack = list(undo or [])
        self.redo_stack = list(redo or [])
        self.created_at = created_at or _now()
        self.updated_at = updated_at or self.created_at

    # ── 编辑 ────────────────────────────────────────────
    def set(self, key: str, slot) -> list[float]:
        geometry.parse_slot_key(key)          # 提前校验 key 形状
        value = normalize_slot(slot)
        before = self.edits.get(key)
        if before == value:
            return value
        self.edits[key] = value
        self.undo_stack.append({"key": key, "before": before, "after": value})
        self.redo_stack.clear()
        self.updated_at = _now()
        return value

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        op = self.undo_stack.pop()
        if op.get("before") is None:
            self.edits.pop(op["key"], None)
        else:
            self.edits[op["key"]] = normalize_slot(op["before"])
        self.redo_stack.append(op)
        self.updated_at = _now()
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        op = self.redo_stack.pop()
        self.edits[op["key"]] = normalize_slot(op["after"])
        self.undo_stack.append(op)
        self.updated_at = _now()
        return True

    def reset(self) -> None:
        self.edits.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.updated_at = _now()

    # ── 序列化 ──────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "profile": self.profile,
            "base_sha256": self.base_sha256,
            "edits": dict(self.edits),
            "undo": list(self.undo_stack),
            "redo": list(self.redo_stack),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, doc: dict) -> "Draft":
        return cls(
            str(doc.get("workspace_id") or ""),
            profile=str(doc.get("profile") or ""),
            base_sha256=doc.get("base_sha256"),
            edits=doc.get("edits") or {},
            undo=doc.get("undo") or [],
            redo=doc.get("redo") or [],
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at"),
        )

    def diff(self, profile: Profile) -> list[dict]:
        base = {item["key"]: item for item in geometry.profile_slots(profile)}
        ref_w, ref_h = profile.reference_size
        out = []
        for key, new in self.edits.items():
            item = base.get(key) or {}
            old = item.get("slot")
            if old is None:
                out.append({"key": key, "label": key, "old": None, "new": new,
                            "dx_px": None, "dy_px": None,
                            "dw_px": None, "dh_px": None})
                continue
            out.append({
                "key": key,
                "label": item.get("label", key),
                "group": item.get("group"),
                "old": list(old),
                "new": new,
                "dx_px": round((new[0] - old[0]) * ref_w, 1),
                "dy_px": round((new[1] - old[1]) * ref_h, 1),
                "dw_px": round(((new[2] - new[0]) - (old[2] - old[0])) * ref_w, 1),
                "dh_px": round(((new[3] - new[1]) - (old[3] - old[1])) * ref_h, 1),
            })
        out.sort(key=lambda d: d["key"])
        return out


class DraftStore:
    """按工作区 id 存取草稿（内存 + 文件，进程重启不丢）。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, Draft] = {}

    def _path(self, workspace_id: str) -> Path:
        return self.root / f"{workspace_id}.json"

    def get(self, workspace_id: str) -> Draft | None:
        with self._lock:
            if workspace_id in self._cache:
                return self._cache[workspace_id]
            path = self._path(workspace_id)
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            draft = Draft.from_dict(doc)
            self._cache[workspace_id] = draft
            return draft

    def put(self, draft: Draft) -> None:
        with self._lock:
            self._cache[draft.workspace_id] = draft
            path = self._path(draft.workspace_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(draft.to_dict(), ensure_ascii=False,
                                      indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)

    def drop(self, workspace_id: str) -> None:
        with self._lock:
            self._cache.pop(workspace_id, None)
            try:
                self._path(workspace_id).unlink()
            except OSError:
                pass


# ─────────────────────────── 试读 ───────────────────────────

class CalibrationEngine:
    """懒加载的 OCR 引擎 + 单槽位试读（按钮/滑杆只做几何检查）。"""

    def __init__(self) -> None:
        self._engine: OcrEngine | None = None
        self._lock = threading.Lock()

    def _get_engine(self) -> OcrEngine:
        with self._lock:
            if self._engine is None:
                self._engine = OcrEngine()
            return self._engine

    def trial(self, profile: Profile, frame: np.ndarray, key: str,
              slot=None, *, max_width: int = 480) -> dict:
        items = {item["key"]: item for item in geometry.profile_slots(profile)}
        item = items.get(key)
        if item is None:
            raise CalibrationError(f"profile 里没有槽位 {key}")
        value = normalize_slot(slot if slot is not None else item["slot"])
        size = (int(frame.shape[1]), int(frame.shape[0]))
        rect, crop = crop_slot(frame, profile, tuple(value), size)
        started = time.monotonic()
        result = {
            "key": key, "label": item.get("label", key),
            "kind": item.get("kind"), "group": item.get("group"),
            "slot": value, "rect": list(rect),
            "crop": _data_url(crop, max_width=max_width),
            "mode": "trial",
            "text": "", "score": 0.0, "parts": [],
            "parsed": None, "ok": False, "reason": "",
            "ms": 0.0,
            "note": "试读：不走去重/缓存/稳定闸，与 live 实际读数可能不同",
        }
        kind = item.get("kind")
        try:
            if kind == "button":
                result.update(self._check_button(frame, profile, rect, value))
            elif kind == "slider":
                result.update({"ok": True, "reason": "滑杆槽位（只看几何）"})
            elif kind == "cards":
                result.update(self._check_cards(frame, profile, item))
            else:
                result.update(self._read_text(profile, frame, key, value,
                                              kind, size))
        except Exception as exc:  # noqa: BLE001 — 试读失败要能看原因
            result.update({"ok": False,
                           "reason": f"{type(exc).__name__}: {exc}"})
        result["ms"] = round((time.monotonic() - started) * 1000, 1)
        return result

    # ── 文本字段 ────────────────────────────────────────
    def _read_text(self, profile: Profile, frame: np.ndarray, key: str,
                   slot, kind: str, size: tuple[int, int]) -> dict:
        engine = self._get_engine()
        read = read_slot(frame, profile, engine, tuple(slot), key, size,
                         use_det=(kind == "status"))
        text = read.text
        score = read.score
        parts = [{"text": t, "score": round(float(s), 4)}
                 for t, s in (read.parts or ())]
        out = {"text": text, "score": round(float(score or 0.0), 4),
               "parts": parts, "parsed": None, "ok": False,
               "reason": read.note or ""}
        if not text:
            out["reason"] = out["reason"] or "没读出文字"
            return out
        parsed, ok, reason = _parse_by_kind(kind, text, score)
        out.update({"parsed": parsed, "ok": ok, "reason": reason})
        return out

    # ── 按钮 / 滑杆 ─────────────────────────────────────
    def _check_button(self, frame: np.ndarray, profile: Profile,
                      rect, slot) -> dict:
        """按钮槽位不是 OCR 字段：看几何 + 画面里能不能找到按钮中心。"""
        out = {"ok": True, "reason": "按钮槽位（只看几何）", "hit": None,
               "parsed": None, "text": "", "score": 0.0, "parts": []}
        try:
            engine = self._get_engine()
            found = find_action_buttons(frame, engine)
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"按钮定位不可用：{type(exc).__name__}: {exc}"
            out["hit"] = None
            return out
        x1, y1, x2, y2 = rect
        inside = []
        for name, (cx, cy) in (found or {}).items():
            if x1 <= float(cx) <= x2 and y1 <= float(cy) <= y2:
                inside.append(name)
        out["hit"] = bool(inside)
        out["buttons_found"] = sorted(found or {})
        out["buttons_inside"] = inside
        out["reason"] = ("框内找到按钮：" + "、".join(inside) if inside else
                         "框内没找到按钮（位置可能偏，或本帧没有操作行）")
        return out

    # ── 牌面 ────────────────────────────────────────────
    def _check_cards(self, frame: np.ndarray, profile: Profile,
                     item: dict) -> dict:
        cards = recognize_cards(frame, profile)
        field = item.get("field")
        codes = (list(getattr(cards, "hero_codes", ()) or ())
                 if field == "hero_cards"
                 else list(getattr(cards, "board_codes", ()) or ()))
        return {
            "ok": bool(codes), "parsed": codes,
            "text": " ".join(codes), "score": 1.0 if codes else 0.0,
            "parts": [], "cards_reliable": bool(getattr(cards, "reliable", False)),
            "reason": ("识别到 " + " ".join(codes)) if codes else "牌面槽位没识别到牌",
        }


def _data_url(crop: np.ndarray, *, max_width: int = 480) -> str | None:
    if crop is None or getattr(crop, "size", 0) == 0:
        return None
    img = crop
    if img.shape[1] > max_width:
        scale = max_width / float(img.shape[1])
        img = cv2.resize(img, (max_width, max(1, int(img.shape[0] * scale))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return None
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def _parse_by_kind(kind: str, text: str, score: float
                   ) -> tuple[object, bool, str]:
    """复用现有解析口径；返回 (解析值, ok, 原因)。"""
    if kind == "chips":
        chip = parse_chip(text, score)
        parsed = chip.to_debug()
        return parsed, chip.ok, chip.error or (
            f"{chip.value}{chip.unit or ''}" if chip.ok else "")
    if kind == "pot":
        amount = parse_amount(text)
        parsed = amount.to_debug()
        return parsed, amount.ok, amount.error or (
            f"{amount.value}BB" if amount.ok else "")
    if kind == "blinds":
        blinds = parse_blinds(text)
        parsed = ({"sb": blinds.sb, "bb": blinds.bb}
                  if blinds is not None else None)
        return parsed, blinds is not None, (
            f"{blinds.sb}/{blinds.bb}" if blinds is not None else "盲注解析失败")
    if kind == "bet":
        loose = parse_chip_loose(text)
        return {"value_bb": loose}, loose is not None, (
            f"{loose}BB" if loose is not None else "下注额解析失败")
    if kind == "action":
        action = parse_action(text)
        return {"action": action}, action is not None, (
            action or "动作词表里没有")
    if kind == "name":
        cleaned = "".join(text.split())
        ok = 1 <= len(cleaned) <= 20
        return {"name": cleaned, "score": round(score, 4)}, ok, (
            f"{cleaned}（{score:.2f}）" if ok else "名字形状不对")
    if kind == "fraction":
        return {"text": text}, bool(text), text
    if kind == "label":
        return {"text": text}, bool(text), text
    return {"text": text}, bool(text), text or "没读出文字"


# ─────────────────────────── diff / 写入 ───────────────────────────

def _set_edit(raw: dict, key: str, slot: list[float]) -> tuple[object, object]:
    """把一条编辑写进原始 dict，返回 (旧值, 新值)。"""
    scope, seat_id, field = geometry.parse_slot_key(key)
    if scope == "global":
        container = raw.setdefault("global_slots", {})
        if not isinstance(container, dict):
            raise CalibrationError("global_slots 不是对象，无法编辑")
        old = container.get(field)
        container[field] = list(slot)
        return old, list(slot)
    seats = raw.get("seats")
    if not isinstance(seats, list):
        raise CalibrationError("seats 不是数组，无法编辑座位槽位")
    json_field = SEAT_JSON_FIELD.get(field)
    if json_field is None:
        raise CalibrationError(f"不支持编辑座位字段 {field!r}")
    for seat in seats:
        if isinstance(seat, dict) and str(seat.get("seat_id")) == str(seat_id):
            old = seat.get(json_field)
            seat[json_field] = list(slot)
            return old, list(slot)
    raise CalibrationError(f"profile 里没有座位 {seat_id!r}")


def compute_diff(profile: Profile, edits: dict[str, list[float]]) -> list[dict]:
    draft = Draft(profile.path.stem, profile=str(profile.path),
                  edits=edits)
    return draft.diff(profile)


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


def _backup(path: Path, *, keep: int = MAX_BACKUPS) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    if backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{os.getpid()}")
    backup.write_bytes(path.read_bytes())
    siblings = sorted(path.parent.glob(f"{path.name}.bak-*"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for extra in siblings[keep:]:
        try:
            extra.unlink()
        except OSError:
            pass
    return backup


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def apply_edits(profile_path: Path | str, edits: dict[str, list[float]], *,
                keep_backups: int = MAX_BACKUPS) -> dict:
    """校验 → 备份 → 原子写；返回 {diff, backup, sha256}。

    校验失败抛 `CalibrationError`，文件一个字节都不动。
    """
    path = Path(profile_path)
    if not path.is_file():
        raise CalibrationError(f"profile 不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CalibrationError(f"profile 读不出来：{type(exc).__name__}: {exc}")
    if not isinstance(raw, dict):
        raise CalibrationError("profile 顶层必须是对象")

    for key, slot in (edits or {}).items():
        normalize_slot(slot)                 # 形状先过一遍
        _set_edit(raw, key, list(slot))
    normal_slots = {k: normalize_slot(v) for k, v in (edits or {}).items()}

    try:
        new_profile = Profile.from_raw(raw, path)
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationError(
            f"改完后结构不完整：{type(exc).__name__}: {exc}") from None
    problems = new_profile.check()
    if problems:
        raise CalibrationError("改完后 profile 校验失败", problems)

    # 原始文件里的现有槽位也保留，diff 用旧 profile 的几何算位移。
    old_profile, _ = Profile.inspect(path)
    diff = compute_diff(old_profile, normal_slots) if old_profile else []

    backup = _backup(path, keep=keep_backups)
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(path, text)
    return {
        "ok": True,
        "path": str(path),
        "backup": str(backup),
        "sha256": _sha256(path),
        "diff": diff,
        "applied": sorted(normal_slots),
    }


def restore_backup(profile_path: Path | str, backup: Path | str) -> dict:
    """把备份写回 profile（回滚用）。"""
    path = Path(profile_path)
    source = Path(backup)
    if not source.is_file():
        raise CalibrationError(f"备份不存在：{source}")
    data = source.read_text(encoding="utf-8")
    try:
        json.loads(data)
    except ValueError as exc:
        raise CalibrationError(f"备份不是合法 JSON：{exc}")
    _atomic_write(path, data)
    return {"ok": True, "path": str(path), "restored_from": str(source),
            "sha256": _sha256(path)}
