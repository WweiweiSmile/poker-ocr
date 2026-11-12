"""profiles 的槽位 → 分组 / 像素矩形 / 几何诊断。

**不另写一套换算**：像素矩形一律走 `Profile.slot_to_px`（它已经处理了
canvas / letterbox / pad），前端拿到的仍是归一化值，只在显示时乘内容区尺寸。

槽位 key 约定（也是标定/草稿的键）：

    global:<slot_key>                全局槽位（pot/board/hero_cards/...）
    seat:<seat_id>:<field>           座位槽位（name/chip/chip_alt/bet/action/avatar）
"""

from __future__ import annotations

from typing import Iterable

from ..profile import Profile

GROUP_LABELS = {
    "seats": "座位",
    "actions": "动作",
    "cards": "牌面",
    "pots": "底池与盲注",
    "buttons": "按钮",
    "sliders": "滑杆",
}
GROUP_ORDER = ("seats", "actions", "cards", "pots", "buttons", "sliders")

SEAT_FIELDS = {
    "name": ("名字", "seats", "name"),
    "chip": ("筹码", "seats", "chips"),
    "chip_alt": ("筹码(备)", "seats", "chips"),
    "avatar": ("头像/状态", "seats", "status"),
    "bet": ("本街下注", "actions", "bet"),
    "action": ("动作标签", "actions", "action"),
}
SEAT_FIELD_ORDER = ("name", "chip", "chip_alt", "bet", "action", "avatar")

GLOBAL_FIELDS = {
    "pot": ("底池", "pots", "pot"),
    "pot_before_street": ("本街底池", "pots", "pot"),
    "blinds": ("盲注/前注", "pots", "blinds"),
    "hero_action_zone": ("操作区", "pots", "zone"),
    "hero_turn_probe": ("轮到我探针", "pots", "probe"),
    "board": ("公共牌", "cards", "cards"),
    "hero_cards": ("我的手牌", "cards", "cards"),
    "hand_label": ("手牌标签", "cards", "label"),
    "hero_fold_button": ("弃牌按钮", "buttons", "button"),
    "hero_call_button": ("跟注按钮", "buttons", "button"),
    "hero_raise_button": ("加注/下注主钮", "buttons", "button"),
    "hero_check_button": ("过牌/让牌按钮", "buttons", "button"),
    "hero_allin_button": ("全下按钮", "buttons", "button"),
    "bet_preset_1_3": ("档 1/3", "buttons", "button"),
    "bet_preset_1_2": ("档 1/2", "buttons", "button"),
    "bet_preset_2_3": ("档 2/3", "buttons", "button"),
    "bet_preset_1": ("档 1", "buttons", "button"),
    "bet_preset_1_2x": ("档 1.2", "buttons", "button"),
    "bomb_cn": ("炸弹池(中)", "buttons", "button"),
    "bomb_en": ("炸弹池(英)", "buttons", "button"),
    "hero_bet_slider": ("滑杆轨道", "sliders", "slider"),
    "hero_bet_amount": ("滑杆金额", "sliders", "chips"),
    "hero_bet_fraction": ("滑杆比例", "sliders", "fraction"),
    "hero_bet_confirm": ("滑杆确定", "sliders", "button"),
    "hero_bet_close": ("滑杆关闭", "sliders", "button"),
}

# 标定阶段可编辑的槽位（M1 = 按钮/滑杆；M3 = 再加全部座位槽位）。
M1_EDITABLE = tuple(k for k, (_, group, _kind) in GLOBAL_FIELDS.items()
                    if group in ("buttons", "sliders"))
M3_EDITABLE_GLOBAL = tuple(GLOBAL_FIELDS)
M3_EDITABLE_SEAT = SEAT_FIELD_ORDER


def slot_key_global(name: str) -> str:
    return f"global:{name}"


def slot_key_seat(seat_id: str, field: str) -> str:
    return f"seat:{seat_id}:{field}"


def parse_slot_key(key: str) -> tuple[str, str | None, str]:
    """`global:pot` → ("global", None, "pot")；`seat:a:name` → ("seat","a","name")。"""
    parts = str(key or "").split(":")
    if len(parts) == 2 and parts[0] == "global":
        return "global", None, parts[1]
    if len(parts) == 3 and parts[0] == "seat":
        return "seat", parts[1], parts[2]
    raise ValueError(f"无法识别的槽位 key：{key!r}")


def profile_slots(profile: Profile) -> list[dict]:
    """profile → 槽位清单（含分组、形状、可编辑阶段），不依赖帧尺寸。"""
    out: list[dict] = []
    for key, (label, group, kind) in GLOBAL_FIELDS.items():
        slot = profile.global_slots.get(key)
        if slot is None:
            continue
        out.append({
            "key": slot_key_global(key),
            "slot": _round_slot(slot),
            "label": label,
            "group": group,
            "kind": kind,
            "seat_id": None,
            "field": key,
            "shape": "disc" if kind == "button" else "rect",
            "editable": kind != "other",
            "stage": "M1" if key in M1_EDITABLE else "M3",
        })
    for seat in profile.seats:
        for field in SEAT_FIELD_ORDER:
            label, group, kind = SEAT_FIELDS[field]
            slot = getattr(seat, f"{field}_slot", None)
            if slot is None:
                continue
            out.append({
                "key": slot_key_seat(seat.seat_id, field),
                "slot": _round_slot(slot),
                "label": f"{seat.seat_id} · {label}",
                "group": group,
                "kind": kind,
                "seat_id": seat.seat_id,
                "field": field,
                "shape": "rect",
                "editable": True,
                "stage": "M3",
            })
    # 不含在 GLOBAL_FIELDS 里的未知全局槽位也要能画出来（向前兼容）。
    for key, slot in profile.global_slots.items():
        if key in GLOBAL_FIELDS:
            continue
        out.append({
            "key": slot_key_global(key), "slot": _round_slot(slot),
            "label": key, "group": "pots", "kind": "other",
            "seat_id": None, "field": key, "shape": "rect",
            "editable": False, "stage": "—",
        })
    return out


def _round_slot(slot) -> list[float]:
    return [round(float(v), 6) for v in slot]


def slot_px(profile: Profile, frame_size: tuple[int, int], slot) -> list[int]:
    """归一化槽位 → 帧像素矩形（复用 Profile.slot_to_px）。"""
    return [int(v) for v in profile.slot_to_px(tuple(slot), frame_size)]


def layout(profile: Profile, frame_size: tuple[int, int] | None = None,
           *, groups: Iterable[str] | None = None) -> dict:
    """完整 ROI 布局：分组 + 归一化槽位 +（可选）像素矩形。"""
    wanted = set(groups or GROUP_ORDER)
    slots = []
    for item in profile_slots(profile):
        if item["group"] not in wanted:
            continue
        if frame_size:
            item = {**item, "px": slot_px(profile, frame_size, item["slot"])}
        slots.append(item)
    counts = {g: 0 for g in GROUP_ORDER}
    for item in slots:
        counts[item["group"]] += 1
    size = tuple(int(v) for v in frame_size) if frame_size else None
    return {
        "frame_size": list(size) if size else None,
        "reference_size": [int(v) for v in profile.reference_size],
        "canvas": (list(profile.canvas) if profile.canvas is not None else None),
        "content_rect": (list(profile.content_rect(size)) if size else None),
        "groups": [{"id": g, "label": GROUP_LABELS[g], "count": counts[g]}
                   for g in GROUP_ORDER],
        "slots": slots,
    }


def aspect_report(source_size: tuple[int, int] | None,
                  reference_size: tuple[int, int]) -> dict:
    """源比例 vs profile 参考比例：≤2% 绿、2–10% 黄、>10% 红。"""
    if not source_size or not all(source_size):
        return {"ok": None, "deviation": None, "level": "skip",
                "text": "未知源尺寸"}
    rw, rh = (float(reference_size[0]), float(reference_size[1]))
    sw, sh = (float(source_size[0]), float(source_size[1]))
    if rw <= 0 or rh <= 0 or sh <= 0:
        return {"ok": None, "deviation": None, "level": "skip",
                "text": "无效尺寸"}
    ref_aspect, src_aspect = rw / rh, sw / sh
    deviation = abs(src_aspect - ref_aspect) / ref_aspect
    pct = deviation * 100
    if deviation <= 0.02:
        level, text = "pass", f"比例一致（偏差 {pct:.1f}%）"
    elif deviation <= 0.10:
        level, text = "warn", (
            f"比例偏差 {pct:.1f}%：会按参考比例 AspectFit，画面可能略变形")
    else:
        level, text = "fail", (
            f"比例偏差 {pct:.1f}%：ROI 会整体错位，请换 profile 或改成等比源")
    return {
        "ok": level == "pass",
        "deviation": round(deviation, 5),
        "source_aspect": round(src_aspect, 6),
        "reference_aspect": round(ref_aspect, 6),
        "level": level,
        "text": text,
    }


def diagnostics(profile: Profile, *, source_size: tuple[int, int] | None = None,
                frame_size: tuple[int, int] | None = None,
                window: dict | None = None) -> dict:
    """三套网格的对齐情况（§8.5）：源/内容区/profile/窗口/预计偏移。"""
    from ..click_marks import predicted_offset_pt
    from ..bet_slider import WindowRect

    rows: list[dict] = []
    verdicts: list[dict] = []

    def row(label: str, value: str, status: str = "pass",
            detail: str = "") -> None:
        rows.append({"label": label, "value": value,
                     "status": status, "detail": detail})

    src = tuple(source_size) if source_size else None
    frame = tuple(frame_size) if frame_size else None
    row("视频帧", f"{src[0]}×{src[1]}" if src else "未知",
        "pass" if src else "skip")
    if frame:
        cx1, cy1, cx2, cy2 = profile.content_rect(frame)
        row("内容区", f"{cx2 - cx1}×{cy2 - cy1}",
            "pass", "canvas 裁黑边后的有效区域" if profile.canvas else "无 letterbox")
    ref_w, ref_h = profile.reference_size
    row("profile", f"{ref_w}×{ref_h}"
        + (f"  canvas={list(profile.canvas)}" if profile.canvas else ""))
    if src:
        scale = (frame[0] / ref_w if frame else src[0] / ref_w,
                 frame[1] / ref_h if frame else src[1] / ref_h)
        row("缩放", f"{scale[0]:.4f} / {scale[1]:.4f}",
            "pass" if min(scale) >= 0.5 else "warn",
            "ROI 分辨率低于参考图 50%，筹码/庄位可能读不出"
            if min(scale) < 0.5 else "")
    if window:
        wr = WindowRect(float(window["x"]), float(window["y"]),
                        float(window["w"]), float(window["h"]))
        row("窗口", f"{wr.w:g}×{wr.h:g} 逻辑点")
        expected = window.get("expected_size")
        if expected:
            dw = wr.w - float(expected[0])
            dh = wr.h - float(expected[1])
            near = abs(dw) <= 2 and abs(dh) <= 2
            row("窗口 vs 期望",
                f"{dw:+.0f} / {dh:+.0f} pt", "pass" if near else "warn",
                "窗口尺寸漂移会让点击按比例偏移，建议手动固定窗口"
                if not near else "")
            if not near:
                verdicts.append({
                    "level": "warn", "text": "窗口高度/宽度与标定不一致，"
                    "先在「固定窗口」里记录当前 rect 再标定"})
        if frame:
            offset = predicted_offset_pt(wr, frame)
            row("预计偏移",
                f"动作行(ny≈0.85) {'偏低' if offset > 0 else '偏高'} "
                f"{abs(offset) * 0.85:.1f}pt",
                "pass" if abs(offset) <= 2 else "warn",
                f"帧高 {frame[1]}px / 窗口高 {wr.h:g}pt，差 {offset:+.1f}pt")
            if abs(offset) > 2:
                verdicts.append({
                    "level": "warn",
                    "text": f"帧与窗口高度差 {abs(offset):.1f}pt；点击会整体偏移，"
                            "先固定窗口或改用 window 源"})
    return {
        "rows": rows,
        "verdicts": verdicts,
        "aspect": aspect_report(src, profile.reference_size if src else (1, 1)),
    }


def map_errors(errors: Iterable[str], slots: list[dict]) -> dict:
    """`frame.errors`（`pot` / `stack:topL` / `bet_gt_pot:a` …）→ 槽位 key。

    映射不到的列进 `other`，页面上单独显示，不假装它属于某个框。
    """
    by_key: dict[str, list[str]] = {}
    other: list[str] = []
    active = [s for s in slots if "slot" in s]

    def add(key: str, error: str) -> None:
        by_key.setdefault(key, []).append(error)

    for error in errors or ():
        text = str(error)
        prefix, _, arg = text.partition(":")
        prefix = prefix.strip()
        if prefix in ("pot", "pbs_gt_pot", "pbs_inconsistent"):
            for s in active:
                if s["field"] in ("pot", "pot_before_street"):
                    add(s["key"], text)
            continue
        if prefix == "blinds":
            for s in active:
                if s["field"] == "blinds":
                    add(s["key"], text)
            continue
        if prefix in ("stack", "chip"):
            _add_seat_field(active, arg, ("chip", "chip_alt"), text, add, other)
            continue
        if prefix in ("bet", "bet_gt_pot"):
            _add_seat_field(active, arg, ("bet",), text, add, other)
            continue
        if prefix in ("vanished", "name", "action"):
            _add_seat_field(active, arg, ("name", "avatar"), text, add, other)
            continue
        other.append(text)
    return {"by_slot": by_key, "other": other}


def _add_seat_field(active: list[dict], seat_id: str, fields: tuple[str, ...],
                    text: str, add, other: list[str]) -> None:
    hit = False
    for s in active:
        if s.get("seat_id") == seat_id and s.get("field") in fields:
            add(s["key"], text)
            hit = True
    if not hit:
        other.append(text)
