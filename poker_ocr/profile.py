"""牌桌几何配置：加载、校验、归一化坐标 → 像素坐标。

核心约定：配置里所有坐标都是 0-1 的相对值，运行时乘实际宽高。
**永远不把整图 resize 到某个基准尺寸** —— 老 scan.py 的 normalize() 就是这么
把竖屏 1440x3200 压成 1920x1080 的。
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_default.json"

Slot = tuple[float, float, float, float]   # 归一化 x1,y1,x2,y2
Size = tuple[int, int]                      # 宽, 高


def _opt_slot(raw) -> Slot | None:
    """可选槽位：缺字段或不是 4 个数，一律当「本 profile 不支持」返回 None。

    不抛异常是刻意的 —— 老配置没有 bet_slot/action_slot 属于正常情况，
    不该让加载炸掉。但长度不对的（写错了）也不能被静默当成合法坐标 ——
    这里一并返回 None，由调用方按「不支持」处理，而不是拿半个框去裁剪。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))


def _slot_ok(slot: Slot) -> bool:
    """归一化矩形是否合法：0 ≤ x1 < x2 ≤ 1、0 ≤ y1 < y2 ≤ 1。

    故意**允许**边界贴到 0 和 1（lowL 的名字槽就是从 0.0 起，是实测值不是笔误）。
    """
    x1, y1, x2, y2 = slot
    return 0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0


@dataclass(frozen=True)
class Seat:
    seat_id: str
    ring_index: int
    name_slot: Slot
    chip_slot: Slot
    avatar_slot: Slot
    # 下面两个是「本街下注」与「动作标签」的区域，新管线用。
    # 老配置（table_default.json）没有，所以给默认 None —— 缺了就表示这个
    # profile 不支持该字段，而不是「这块画面什么都没有」。
    bet_slot: Slot | None = None
    action_slot: Slot | None = None
    # 备选筹码槽：主槽读不到时再试这一块。hero 的筹码胶囊会随「有没有手牌」
    # 上下移动（有牌时被牌推到最底部，没牌时贴在头像下面），见 WPK 配置。
    chip_slot_alt: Slot | None = None


@dataclass
class Profile:
    path: Path
    reference_size: Size
    table_center: tuple[float, float]
    hero_seat_id: str
    action_direction: str
    direction_verified: bool
    ring_order: list[str]
    region_gate: tuple[float, float]
    thresholds: dict
    dealer_cfg: dict
    seats: list[Seat]
    canvas: Slot | None = None
    # 不属任何座位的全局区域：pot / board / hero_cards / hand_label / pot_before_street。
    # 同样是「有才用」，键缺失即不支持。
    global_slots: dict[str, Slot] = field(default_factory=dict)
    # runtime profile：由全图识别现场生成，环序/几何不要求人工标定那套校验
    runtime: bool = False
    # 盲注横幅读丢前注时的兜底（BB）。这款 app 的 1/2 桌固定 0.5BB 前注；
    # None = 不兜底，按横幅原样。
    default_ante: float | None = None

    # ── 查询 ──────────────────────────────────────────────

    def seat(self, seat_id: str) -> Seat:
        for s in self.seats:
            if s.seat_id == seat_id:
                return s
        raise KeyError(f"配置里没有座位 {seat_id}")

    @property
    def seats_in_ring_order(self) -> list[Seat]:
        """按环序排好的座位，**从 hero 开始**绕一圈。

        旋转是算出来的，不是靠配置把 hero 写在第一位 —— 否则 --hero-seat
        覆盖就没有任何效果。
        """
        ordered = [self.seat(sid) for sid in self.ring_order]
        hero_idx = next((i for i, s in enumerate(ordered)
                         if s.seat_id == self.hero_seat_id), None)
        if hero_idx is None:
            return ordered
        return ordered[hero_idx:] + ordered[:hero_idx]

    # ── 坐标换算 ──────────────────────────────────────────

    def content_rect(self, size: Size) -> tuple[int, int, int, int]:
        """游戏画面在截图里的实际矩形（像素）。

        找不到 letterbox 黑边时就是整张图 —— 这是绝大多数情况。
        """
        w, h = size
        if self.canvas is None:
            return 0, 0, w, h
        x1, y1, x2, y2 = self.canvas
        return int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)

    def slot_to_px(self, slot: Slot, size: Size, pad: float = 0.0) -> tuple[int, int, int, int]:
        """归一化槽位 → 像素矩形。pad 是按槽位尺寸比例向外扩的量。"""
        w, h = size
        cx1, cy1, cx2, cy2 = self.content_rect(size)
        cw, ch = cx2 - cx1, cy2 - cy1
        x1, y1, x2, y2 = slot
        if pad:
            dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
            x1, y1, x2, y2 = x1 - dx, y1 - dy, x2 + dx, y2 + dy
        return (
            max(0, min(int(x1 * cw) + cx1, w)),
            max(0, min(int(y1 * ch) + cy1, h)),
            max(0, min(int(x2 * cw) + cx1, w)),
            max(0, min(int(y2 * ch) + cy1, h)),
        )

    def point_to_px(self, pt: tuple[float, float], size: Size) -> tuple[float, float]:
        w, h = size
        cx1, cy1, cx2, cy2 = self.content_rect(size)
        return (pt[0] * (cx2 - cx1) + cx1, pt[1] * (cy2 - cy1) + cy1)

    def in_region_gate(self, pt: tuple[float, float], size: Size) -> bool:
        """文本框中心是否落在桌面区内（挡掉状态栏和播放器 UI）。"""
        _, py = self.point_to_px(pt, size)
        h = size[1]
        y = py / h
        lo, hi = self.region_gate
        return lo <= y <= hi

    # ── 校验 ──────────────────────────────────────────────

    def check_ring_geometry(self) -> list[str]:
        """用几何反推环序，和手写的 ring_order 对一遍。

        图像坐标系 y 向下，所以 atan2 角度增大 = 屏幕上顺时针。

        比的是**循环序**：环没有起点，从哪个座位开始写都一样，
        所以把几何序的所有旋转都算出来看配置在不在里面。
        这不验证「行动方向」（那单帧验不了），只验证座位彼此的先后关系有没有抄错。
        """
        angles = []
        for s in self.seats:
            nx = (s.name_slot[0] + s.name_slot[2]) / 2
            ny = (s.name_slot[1] + s.name_slot[3]) / 2
            dx = nx - self.table_center[0]
            dy = ny - self.table_center[1]
            angles.append((math.degrees(math.atan2(dy, dx)) % 360, s.seat_id))
        angles.sort()

        cw = [sid for _, sid in angles]
        base = list(reversed(cw)) if self.action_direction == "ccw" else cw
        rotations = [base[i:] + base[:i] for i in range(len(base))]

        if self.ring_order not in rotations:
            return [f"环序对不上：几何算出（{self.action_direction}）{base}，"
                    f"配置写的是 {self.ring_order}"]
        return []

    def validate(self) -> None:
        problems = []
        ids = [s.seat_id for s in self.seats]
        if not (2 <= len(ids) <= 9):
            problems.append(f"座位数应在 2-9，实际 {len(ids)}")
        if len(set(ids)) != len(ids):
            problems.append(f"seat_id 有重复：{ids}")
        if sorted(ids) != sorted(self.ring_order):
            problems.append("ring_order 和 seats 的 seat_id 集合不一致")
        if self.hero_seat_id not in ids:
            problems.append(f"hero_seat_id={self.hero_seat_id} 不在 seats 里")
        if self.action_direction not in ("cw", "ccw"):
            problems.append(f"action_direction 只能是 cw/ccw，收到 {self.action_direction}")
        ring_idx = sorted(s.ring_index for s in self.seats)
        if ring_idx != list(range(len(ids))):
            problems.append(f"ring_index 应该是 0-{len(ids) - 1} 各一个，实际 {ring_idx}")

        # 新槽位（可选）也要过一遍形状检查。`_opt_slot` 只挡住了「不是 4 个数」，
        # 挡不住「写反了 / 超出 0-1」—— 那种框裁出来是空图，识别结果会静默变成
        # 「这里什么都没有」，跟本次庄位门槛写死同一个病。
        for s in self.seats:
            for label, slot in (("bet_slot", s.bet_slot), ("action_slot", s.action_slot),
                                ("chip_slot_alt", s.chip_slot_alt)):
                if slot is not None and not _slot_ok(slot):
                    problems.append(f"{s.seat_id}.{label} 不是合法的归一化矩形：{slot}")
        for gname, gslot in self.global_slots.items():
            if not _slot_ok(gslot):
                problems.append(f"global_slots.{gname} 不是合法的归一化矩形：{gslot}")

        if not self.runtime:
            problems += self.check_ring_geometry()

        if problems:
            for p in problems:
                print(f"配置有问题：{p}", file=sys.stderr)
            sys.exit(f"{self.path} 校验失败")

    # ── 加载 ──────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path | None = None) -> "Profile":
        path = Path(path or DEFAULT_PROFILE)
        if not path.exists():
            sys.exit(f"找不到配置文件 {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))

        seats = [
            Seat(
                seat_id=s["seat_id"],
                ring_index=s["ring_index"],
                name_slot=tuple(s["name_slot"]),      # type: ignore[arg-type]
                chip_slot=tuple(s["chip_slot"]),      # type: ignore[arg-type]
                avatar_slot=tuple(s["avatar_slot"]),  # type: ignore[arg-type]
                bet_slot=_opt_slot(s.get("bet_slot")),
                action_slot=_opt_slot(s.get("action_slot")),
                chip_slot_alt=_opt_slot(s.get("chip_slot_alt")),
            )
            for s in raw["seats"]
        ]

        profile = cls(
            path=path,
            reference_size=tuple(raw["reference_size"]),  # type: ignore[arg-type]
            table_center=tuple(raw["table_center"]),      # type: ignore[arg-type]
            hero_seat_id=raw["hero_seat_id"],
            action_direction=raw["action_direction"].lower(),
            direction_verified=bool(raw.get("direction_verified", False)),
            ring_order=list(raw["ring_order"]),
            region_gate=(raw["region_gate"]["y_min"], raw["region_gate"]["y_max"]),
            thresholds=dict(raw["thresholds"]),
            dealer_cfg=dict(raw.get("dealer", {})),
            seats=seats,
            canvas=tuple(raw["canvas"]) if raw.get("canvas") else None,  # type: ignore[arg-type]
            global_slots={
                k: tuple(v)  # type: ignore[arg-type]
                for k, v in (raw.get("global_slots") or {}).items()
                if isinstance(v, list) and len(v) == 4
            },
            runtime=bool(raw.get("runtime", False)),
            default_ante=(float(raw["default_ante"])
                          if raw.get("default_ante") is not None else None),
        )
        profile.validate()
        return profile


# ─────────────────────────── 画布检测 ───────────────────────────

def detect_canvas(img: np.ndarray, min_bar_frac: float = 0.02) -> Slot | None:
    """找游戏画面上下是否夹着纯色 letterbox 条。

    只在截图宽高比和参考尺寸明显不符时才调用 —— 正常铺满屏幕的截图直接返回 None。
    宁可返回 None 也不要误判：误判会让所有坐标整体偏移。
    """
    h, w = img.shape[:2]
    gray = img.mean(axis=2)
    row_std = gray.std(axis=1)

    def uniform(row_i: int) -> bool:
        return row_std[row_i] < 3.0

    top = 0
    while top < h and uniform(top):
        top += 1
    bottom = h
    while bottom > top and uniform(bottom - 1):
        bottom -= 1

    if top < h * min_bar_frac and (h - bottom) < h * min_bar_frac:
        return None                       # 上下都没有成规模的条 → 铺满，不裁

    return (0.0, top / h, 1.0, bottom / h)
