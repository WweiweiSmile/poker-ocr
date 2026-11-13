"""桌面自动点击：把 AI 建议落到客户端按钮上。

配置里是一组**屏幕坐标矩形**（逻辑点，top-left 原点），由
`tools/calibrate_clicker.py` 人工拖框标定。AI 建议返回后：

  · 弃牌  → 点 `fold`
  · 跟注 / 过牌 / 跟全下 → 点 `call`（同一个右侧按钮）
  · 下注 / 加注 → 直接点五个底池档位之一（`raise-1/3` … `raise-1.2`）；
    客户端在点档位时就完成了下注，不再点蓝色「下注/加注」确认按钮
  · 全下 → 点可选的 `allin` 按钮；配置里没标定时 skip，绝不拿档位冒充

每次点击都在矩形内缩一圈的范围里取**随机点**，并给按下时长和两步之间的间隔
加一点抖动，避免永远点同一个像素。翻前建议（`open-2.5` / `raise-3x`）没有
对应的底池档位，按金额取最接近的那个。

本模块不依赖 pyautogui / pyobjc：macOS 后端直接用 ctypes 调 CoreGraphics。
`Clicker` 的后端、随机数、sleep、时钟都可注入，单测不需要真鼠标。
"""

from __future__ import annotations

import ctypes
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, TYPE_CHECKING

if TYPE_CHECKING:                       # 只用于类型，避免运行时循环导入
    from .advice import Advice
    from .bet_slider import BetSliderController
    from .options import Option

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "clicker_local.json"

# 与 advice.OPTION_ORDER / RAISE_FRACTIONS 的档位名字一致
PRESET_IDS = ("raise-1/3", "raise-1/2", "raise-2/3", "raise-1", "raise-1.2")
# allin 是**可选**按钮：旧配置没有它也能启动，只是 all-in 建议不会自动点。
BUTTON_IDS = ("fold", "call", *PRESET_IDS, "allin")
REQUIRED_BUTTONS = ("fold", "call")
BUTTON_LABELS = {
    "fold": "弃牌（左）",
    "call": "跟注 / 让牌（右）",
    "raise-1/3": "快捷加注：底池 1/3",
    "raise-1/2": "快捷加注：底池 1/2",
    "raise-2/3": "快捷加注：底池 2/3",
    "raise-1": "快捷加注：底池 1",
    "raise-1.2": "快捷加注：底池 1.2",
    "allin": "全下（可选）",
}
PRESET_ORDER = {pid: i for i, pid in enumerate(PRESET_IDS)}
# 推荐额与客户端快捷档位的相对误差 ≤ 5% 时优先点快捷档位；
# 超过 5% 才走长按滑杆手动加注。
DEFAULT_PRESET_TOLERANCE = 0.05

# 随机点击的内缩比例：半径 = 原半径 × (1 - 0.30) = 70%
# （点击范围 = π(0.7r)² ≈ 49% 的按钮面积）。点太靠边会落到按钮外/被动画遮住，
# 点在正中心又容易被同一个像素上的遮挡物吃掉，所以留一圈边再随机取点。
DEFAULT_INSET_RATIO = 0.30
DEFAULT_CLICK_DELAY_MS = 70
DEFAULT_CONFIRM_GAP_MS = 180


class ClickerError(Exception):
    """点击器配置/后端错误。"""


class ClickerConfigError(ClickerError):
    """配置文件不合法。"""


class ClickerUnavailable(ClickerError):
    """当前平台没有可用的点击后端（目前只支持 macOS）。"""


# ─────────────────────────── 矩形与配置 ───────────────────────────

@dataclass(frozen=True)
class Rect:
    """屏幕矩形，单位是 macOS 逻辑点（top-left 原点）。

    `shape="circle"` 时把矩形的内切椭圆当作按钮区域（圆按钮拖出来的外接框
    通常不是正方形，所以按椭圆处理）：点击点取在椭圆内部，不会落到四个角
    的空白处。默认仍是 `rect`，兼容旧配置。
    """

    x: float
    y: float
    w: float
    h: float
    shape: str = "rect"              # rect | circle

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def to_list(self) -> list[float]:
        return [self.x, self.y, self.w, self.h]

    def inset_point(self, rng: random.Random,
                    ratio: float = DEFAULT_INSET_RATIO) -> tuple[float, float]:
        """在矩形内缩 `ratio` 后的范围里取随机点。

        按钮一般是圆形/圆角，点边缘容易点空；内缩后再随机。矩形太小
        （宽或高 ≤ 2pt）时退回中心点，保证总能落在一个有效坐标上。
        """
        ratio = min(max(float(ratio), 0.0), 0.45)
        if self.w <= 2 or self.h <= 2:
            return self.cx, self.cy
        pad_x = min(max(1.0, self.w * ratio), max(0.0, self.w / 2 - 1.0))
        pad_y = min(max(1.0, self.h * ratio), max(0.0, self.h / 2 - 1.0))
        lo_x, hi_x = self.x + pad_x, self.x2 - pad_x
        lo_y, hi_y = self.y + pad_y, self.y2 - pad_y
        x = self.cx if hi_x <= lo_x else rng.uniform(lo_x, hi_x)
        y = self.cy if hi_y <= lo_y else rng.uniform(lo_y, hi_y)
        return x, y

    def inset_ellipse_point(self, rng: random.Random,
                            ratio: float = DEFAULT_INSET_RATIO
                            ) -> tuple[float, float]:
        """在内切椭圆内部按面积均匀取随机点（圆形按钮用）。

        极坐标取样：角度均匀，半径取 `sqrt(U)`，这样点不会挤在圆心。
        椭圆在四个方向各内缩 `ratio`，避免点到圆形按钮的边缘。
        """
        ratio = min(max(float(ratio), 0.0), 0.45)
        if self.w <= 2 or self.h <= 2:
            return self.cx, self.cy
        pad_x = min(max(1.0, self.w * ratio), max(0.0, self.w / 2 - 1.0))
        pad_y = min(max(1.0, self.h * ratio), max(0.0, self.h / 2 - 1.0))
        ra = self.w / 2 - pad_x
        rb = self.h / 2 - pad_y
        if ra <= 0 or rb <= 0:
            return self.cx, self.cy
        theta = rng.uniform(0.0, 2.0 * math.pi)
        r = math.sqrt(rng.random())
        return self.cx + ra * r * math.cos(theta), self.cy + rb * r * math.sin(theta)

    def inset_disc_point(self, rng: random.Random,
                         ratio: float = DEFAULT_INSET_RATIO
                         ) -> tuple[float, float]:
        """圆形按钮：在**正圆**内部取样，半径取 min(w,h)/2 再内缩。

        和 `inset_ellipse_point` 的区别：槽位如果不是正方形，椭圆会把
        长边方向的点推到按钮圆外（例如 80x95 的预设钮，ry 比真实半径大）。
        圆形按钮一律用这个，保证点落在可见圆盘里。
        """
        ratio = min(max(float(ratio), 0.0), 0.45)
        if self.w <= 2 or self.h <= 2:
            return self.cx, self.cy
        radius = min(self.w, self.h) / 2.0
        radius = max(0.0, radius - radius * ratio)
        if radius <= 0:
            return self.cx, self.cy
        theta = rng.uniform(0.0, 2.0 * math.pi)
        r = math.sqrt(rng.random()) * radius
        return self.cx + r * math.cos(theta), self.cy + r * math.sin(theta)

    def sample_point(self, rng: random.Random,
                     ratio: float = DEFAULT_INSET_RATIO) -> tuple[float, float]:
        """按按钮形状取随机点：disc → 正圆，circle → 内切椭圆，其他 → 矩形内缩。"""
        if self.shape == "disc":
            return self.inset_disc_point(rng, ratio)
        if self.shape == "circle":
            return self.inset_ellipse_point(rng, ratio)
        return self.inset_point(rng, ratio)


def _rect_from_raw(raw, *, where: str = "button") -> Rect:
    try:
        if isinstance(raw, Mapping):
            shape = str(raw.get("shape") or "rect")
            rect = Rect(float(raw["x"]), float(raw["y"]),
                        float(raw["w"]), float(raw["h"]), shape)
        elif isinstance(raw, (list, tuple)) and len(raw) == 4:
            rect = Rect(*(float(v) for v in raw))
        else:
            raise ValueError("需要 {x,y,w,h} 或 [x,y,w,h]")
    except (KeyError, TypeError, ValueError) as exc:
        raise ClickerConfigError(f"{where} 坐标不合法：{raw!r}（{exc}）") from exc
    return rect


@dataclass
class ClickerConfig:
    """点击器配置。坐标是全局逻辑点，不是截图像素。"""

    buttons: dict[str, Rect] = field(default_factory=dict)
    inset_ratio: float = DEFAULT_INSET_RATIO
    click_delay_ms: int = DEFAULT_CLICK_DELAY_MS
    confirm_gap_ms: int = DEFAULT_CONFIRM_GAP_MS
    allow_fallback: bool = False
    # 动作点下去后页面没变化时的重试：0.5s 后仍无变化就再点一次。
    # retry_count=0 关闭；默认只补一次。
    retry_delay_ms: int = 500
    retry_count: int = 1
    # 推荐额与快捷档位的相对误差门槛（0.05 = 5%）。超过才走滑杆。
    preset_tolerance: float = DEFAULT_PRESET_TOLERANCE
    version: int = 1
    display: dict | None = None
    created_at: str | None = None
    source_path: Path | None = None

    # ── 读写 ──────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path | str) -> "ClickerConfig":
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ClickerConfigError(
                f"点击配置不存在：{path}。先运行 "
                f"`nv tools/calibrate_clicker.py --out {path}` 标定按钮"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ClickerConfigError(f"点击配置读取失败：{path}：{exc}") from exc
        cfg = cls.from_dict(raw)
        cfg.source_path = path
        return cfg

    @classmethod
    def from_dict(cls, raw: Mapping) -> "ClickerConfig":
        if not isinstance(raw, Mapping):
            raise ClickerConfigError("点击配置必须是 JSON 对象")
        version = raw.get("version", 1)
        if not isinstance(version, int) or version != 1:
            raise ClickerConfigError(f"不支持的点击配置版本：{version!r}")
        raw_buttons = raw.get("buttons")
        if not isinstance(raw_buttons, Mapping):
            raise ClickerConfigError("点击配置缺少 buttons 对象")
        buttons = {
            str(name): _rect_from_raw(value, where=f"buttons.{name}")
            for name, value in raw_buttons.items()
        }
        return cls(
            buttons=buttons,
            inset_ratio=float(raw.get("inset_ratio", DEFAULT_INSET_RATIO)),
            click_delay_ms=int(raw.get("click_delay_ms", DEFAULT_CLICK_DELAY_MS)),
            confirm_gap_ms=int(raw.get("confirm_gap_ms", DEFAULT_CONFIRM_GAP_MS)),
            allow_fallback=bool(raw.get("allow_fallback", False)),
            retry_delay_ms=int(raw.get("retry_delay_ms", 500)),
            retry_count=int(raw.get("retry_count", 1)),
            preset_tolerance=float(raw.get("preset_tolerance",
                                           DEFAULT_PRESET_TOLERANCE)),
            version=version,
            display=raw.get("display") if isinstance(raw.get("display"), dict) else None,
            created_at=raw.get("created_at"),
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "coordinate_space": "global_logical_points_top_left",
            "display": self.display,
            "inset_ratio": self.inset_ratio,
            "click_delay_ms": self.click_delay_ms,
            "confirm_gap_ms": self.confirm_gap_ms,
            "allow_fallback": self.allow_fallback,
            "retry_delay_ms": self.retry_delay_ms,
            "retry_count": self.retry_count,
            "preset_tolerance": self.preset_tolerance,
            "buttons": {
                name: {"x": r.x, "y": r.y, "w": r.w, "h": r.h,
                       "shape": r.shape}
                for name, r in self.buttons.items()
            },
        }

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
                        + "\n", encoding="utf-8")
        self.source_path = path
        return path

    # ── 校验 ──────────────────────────────────────────────

    def validate(self) -> None:
        missing = [name for name in REQUIRED_BUTTONS if name not in self.buttons]
        if missing:
            labels = "、".join(BUTTON_LABELS.get(n, n) for n in missing)
            raise ClickerConfigError(f"点击配置缺少必需按钮：{labels}")
        for name, rect in self.buttons.items():
            if not all(math.isfinite(v) for v in (rect.x, rect.y, rect.w, rect.h)):
                raise ClickerConfigError(f"{name} 坐标不是有限数：{rect}")
            if rect.w <= 0 or rect.h <= 0:
                raise ClickerConfigError(
                    f"{name} 矩形宽高必须大于 0：{rect.w}x{rect.h}")
            if rect.shape not in ("rect", "circle", "disc"):
                raise ClickerConfigError(
                    f"{name} 的 shape 只能是 rect / circle / disc：{rect.shape!r}")
        if not (0.0 <= self.inset_ratio <= 0.45):
            raise ClickerConfigError(
                f"inset_ratio 必须在 0–0.45 之间：{self.inset_ratio}")
        if self.click_delay_ms < 0 or self.click_delay_ms > 1000:
            raise ClickerConfigError(
                f"click_delay_ms 必须在 0–1000 之间：{self.click_delay_ms}")
        if self.confirm_gap_ms < 0 or self.confirm_gap_ms > 5000:
            raise ClickerConfigError(
                f"confirm_gap_ms 必须在 0–5000 之间：{self.confirm_gap_ms}")
        if self.retry_delay_ms < 0 or self.retry_delay_ms > 5000:
            raise ClickerConfigError(
                f"retry_delay_ms 必须在 0–5000 之间：{self.retry_delay_ms}")
        if self.retry_count < 0 or self.retry_count > 5:
            raise ClickerConfigError(
                f"retry_count 必须在 0–5 之间：{self.retry_count}")
        if not (0.0 <= self.preset_tolerance <= 1.0):
            raise ClickerConfigError(
                f"preset_tolerance 必须在 0–1 之间：{self.preset_tolerance}")

    @property
    def preset_button_ids(self) -> tuple[str, ...]:
        return tuple(pid for pid in PRESET_IDS if pid in self.buttons)


def rect_inside_displays(rect: Rect, displays: list["DisplayInfo"],
                         tolerance: float = 1.0) -> bool:
    """矩形是否完整落在某一个活动显示器内。"""
    if not displays:
        return True                      # 非 macOS / 查不到显示器时不拦
    return any(
        rect.x >= d.x - tolerance and rect.y >= d.y - tolerance
        and rect.x2 <= d.x + d.w + tolerance
        and rect.y2 <= d.y + d.h + tolerance
        for d in displays
    )


# ─────────────────────────── 点击计划 ───────────────────────────

@dataclass(frozen=True)
class ClickStep:
    button: str
    x: float
    y: float

    def to_dict(self) -> dict:
        return {"button": self.button, "x": round(self.x, 1),
                "y": round(self.y, 1)}


@dataclass(frozen=True)
class ClickPlan:
    action: str
    steps: tuple[ClickStep, ...] = ()
    preset: str | None = None
    mapping: str = "none"            # direct | nearest | none
    target_bb: float | None = None
    skip_reason: str | None = None


@dataclass(frozen=True)
class ClickResult:
    status: str                      # clicked | dry_run | skipped | error
    action: str | None = None
    preset: str | None = None
    mapping: str | None = None
    steps: tuple[ClickStep, ...] = ()
    reason: str | None = None
    error: str | None = None
    elapsed_ms: int = 0
    # **建议额**：AI 让下的量（`options.py` 给的 amount）。所有动作同一个口径，
    # 复盘时「模型建议多少」只认这一个字段 —— 快捷档位、滑杆、全下的实际落点
    # 都可能不同，混在 target_bb 里会让「建议 27 / 实际 26」变成「建议 26」。
    target_bb: float | None = None
    # **计划落注额**：真正准备点的那个数（滑杆取整后的 27、快捷档位的 26、
    # 全下的顶端额度）。没走到「选额」这一步（fold/call/被跳过）就是 None。
    plan_bb: float | None = None
    # **实际落注额**：屏幕上真落下去的那个数。近似落注时可能比建议多 1BB
    # 左右（只许多、不许少），复盘要能一眼看出「建议多少 / 实际多少」。
    final_bb: float | None = None
    detail: dict | None = None       # 滑杆等非按钮路径的明细

    def to_dict(self) -> dict:
        out: dict = {"status": self.status}
        if self.action is not None:
            out["action"] = self.action
        if self.preset is not None:
            out["preset"] = self.preset
        if self.mapping is not None:
            out["mapping"] = self.mapping
        if self.steps:
            out["steps"] = [s.to_dict() for s in self.steps]
        if self.reason:
            out["reason"] = self.reason
        if self.error:
            out["error"] = self.error
        if self.target_bb is not None:
            out["target_bb"] = self.target_bb
        if self.plan_bb is not None:
            out["plan_bb"] = self.plan_bb
        if self.final_bb is not None:
            out["final_bb"] = self.final_bb
        if self.detail:
            out["detail"] = self.detail
        out["elapsed_ms"] = self.elapsed_ms
        return out


def _amount_fields(option, *, plan_bb: float | None = None,
                   final_bb: float | None = None) -> dict:
    """`ClickResult` 三个额度字段的统一口径：建议额 / 计划额 / 实际额。

    建议额来自 `option.amount`，**所有动作**都是它（包括快捷档位：档位是执行
    手段，不是模型的意思）。`plan_bb`/`final_bb` 只有「选了额再点」的路径才有
    值 —— 快捷档位、滑杆、全下；fold/call 与提前返回的跳过不编数字。
    """
    return {"target_bb": getattr(option, "amount", None),
            "plan_bb": plan_bb, "final_bb": final_bb}



def choose_preset(
    option: "Option",
    preset_amounts: Mapping[str, float],
) -> tuple[str, float, str] | None:
    """给下注/加注选项选一个客户端快捷档位。

    返回 `(preset_id, target_bb, "direct"|"nearest")`，选不出来返回 None。

    `raise-1/3` … `raise-1.2` 直接按 id 对应；翻前的 `open-*` / `raise-3x` /
    `raise-4x` 等没有对应档位，按金额取最近的。平局按 1/3→1.2 的顺序。
    """
    amount = option.amount
    if amount is None or not math.isfinite(float(amount)) or amount <= 0:
        return None
    if option.id in PRESET_ORDER:
        target = preset_amounts.get(option.id)
        if target is None:
            return None
        return option.id, float(target), "direct"
    candidates = [
        (abs(float(amount) - float(target)), PRESET_ORDER[pid], pid, float(target))
        for pid, target in preset_amounts.items()
        if pid in PRESET_ORDER and math.isfinite(float(target))
    ]
    if not candidates:
        return None
    _delta, _order, pid, target = min(candidates)
    return pid, target, "nearest"


def choose_preset_within(
    option: "Option",
    preset_amounts: Mapping[str, float],
    tolerance: float = DEFAULT_PRESET_TOLERANCE,
) -> tuple[str, float, str] | None:
    """推荐额与某个快捷档位的**实际目标额**相对误差 ≤ tolerance 时选它。

    返回 `(preset_id, preset_target_bb, "within_5pct")`；都不在门槛内返回
    None（调用方再决定走滑杆还是跳过）。比的是金额不是档位名字：
    翻前 `open-2.5` 也可能刚好落在 1/2 池档位附近。

    多个档位同时命中时取相对误差最小的；平局按 1/3→1.2 的顺序。
    """
    amount = option.amount
    if amount is None or not math.isfinite(float(amount)) or amount <= 0:
        return None
    candidates: list[tuple[float, int, str, float]] = []
    for pid, target in preset_amounts.items():
        if pid not in PRESET_ORDER:
            continue
        try:
            value = float(target)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        rel = abs(float(amount) - value) / value
        if rel <= tolerance + 1e-12:
            candidates.append((rel, PRESET_ORDER[pid], pid, value))
    if not candidates:
        return None
    _rel, _order, pid, value = min(candidates)
    return pid, value, "within_5pct"


def _step(button: str, config: ClickerConfig,
          rng: random.Random) -> ClickStep | None:
    rect = config.buttons.get(button)
    if rect is None:
        return None
    x, y = rect.sample_point(rng, config.inset_ratio)
    return ClickStep(button, x, y)


def _target_step(button: str, targets: dict | None,
                 config: ClickerConfig, rng: random.Random) -> ClickStep | None:
    """优先用「按文字现找」的坐标；没有才退回固定槽位。

    客户端的操作行有多个版面（见 `action_buttons` 模块），固定槽位只对其中
    一种成立。现找的坐标已经落在按钮文字中心，不再做随机内缩——再缩反而
    可能偏出小圆钮。
    """
    if targets:
        hit = targets.get(button)
        if hit is not None:
            x, y = hit
            return ClickStep(button, float(x), float(y))
    return _step(button, config, rng)


def plan_clicks(advice: "Advice", config: ClickerConfig,
                rng: random.Random | None = None,
                targets: dict | None = None) -> ClickPlan:
    """把一条建议映射成 0–1 步点击。纯函数，不碰鼠标。"""
    rng = rng or random.Random()
    option = advice.option
    kind = option.kind

    if kind == "fold":
        step = _target_step("fold", targets, config, rng)
        if step is None:
            return ClickPlan("fold", skip_reason="missing_button:fold")
        return ClickPlan("fold", (step,))

    if kind in ("call", "check"):
        # 同一个右 pill 在不同状态下叫「让牌」或「免费」；两种都算过牌。
        step = None
        for name in ("check", "call"):
            step = _target_step(name, targets, config, rng)
            if step is not None:
                break
        if step is None:
            return ClickPlan(kind, skip_reason="missing_button:call")
        return ClickPlan(kind, (step,))

    if kind == "allin":
        # 可选按钮：没标定时建议照常显示，但绝不拿别的按钮代替全下。
        step = _step("allin", config, rng)
        if step is None:
            return ClickPlan("allin", skip_reason="missing_button:allin")
        return ClickPlan("allin", (step,))

    if kind not in ("raise", "bet"):
        return ClickPlan(kind, skip_reason=f"unknown_action:{kind}")

    presets = dict(advice.preset_amounts)
    chosen = choose_preset(option, presets)
    if chosen is None:
        if option.amount is None:
            return ClickPlan(kind, skip_reason="amount_unknown")
        if option.id in PRESET_ORDER:
            return ClickPlan(kind, skip_reason=f"missing_preset:{option.id}")
        return ClickPlan(kind, skip_reason="no_preset_amounts")
    preset_id, target_bb, mapping = chosen
    if preset_id not in config.buttons:
        return ClickPlan(kind, skip_reason=f"missing_preset:{preset_id}",
                         preset=preset_id, mapping=mapping,
                         target_bb=target_bb)
    preset_step = _step(preset_id, config, rng)
    assert preset_step is not None
    # 客户端点快捷档位即完成下注/加注，不再点蓝色「下注/加注」确认按钮
    return ClickPlan(kind, (preset_step,), preset=preset_id,
                     mapping=mapping, target_bb=target_bb)


# ─────────────────────────── 后端 ───────────────────────────

class ClickBackend(Protocol):
    def preflight(self) -> bool: ...
    def move(self, x: float, y: float) -> None: ...
    def mouse_down(self, x: float, y: float) -> None: ...
    def mouse_up(self, x: float, y: float) -> None: ...
    def scroll(self, x: float, y: float, delta: int) -> None: ...


class DryRunBackend:
    """只记录、不真正发事件。用于 `--clicker-dry-run` 和单测。"""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def preflight(self) -> bool:
        return True

    def move(self, x: float, y: float) -> None:
        self.events.append(("move", x, y))

    def mouse_down(self, x: float, y: float) -> None:
        self.events.append(("down", x, y))

    def mouse_up(self, x: float, y: float) -> None:
        self.events.append(("up", x, y))

    def scroll(self, x: float, y: float, delta: int) -> None:
        """滚轮：delta>0 向上（加额度），<0 向下。只记录。"""
        self.events.append(("scroll", x, y, int(delta)))


# CoreGraphics 常量
kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventMouseMoved = 5
kCGHIDEventTap = 0
kCGMouseButtonLeft = 0
kCGMouseEventClickState = 1
kCGScrollEventUnitLine = 1


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


@dataclass(frozen=True)
class DisplayInfo:
    index: int
    display_id: int
    x: float
    y: float
    w: float
    h: float
    is_main: bool = False
    scale: float | None = None

    def to_dict(self) -> dict:
        return {"index": self.index, "id": self.display_id,
                "x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "main": self.is_main, "scale": self.scale}


def _load_frameworks():
    import ctypes

    if sys.platform != "darwin":
        raise ClickerUnavailable("自动点击目前只支持 macOS")
    app = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    cf = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    return ctypes, app, cf


def list_displays() -> list[DisplayInfo]:
    """枚举活动显示器（逻辑点 + 全局原点）。非 macOS 返回空列表。"""
    if sys.platform != "darwin":
        return []
    ctypes, app, _cf = _load_frameworks()
    max_displays = 16
    ids = (ctypes.c_uint32 * max_displays)()
    count = ctypes.c_uint32(0)
    app.CGGetActiveDisplayList.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32)]
    app.CGGetActiveDisplayList.restype = ctypes.c_int32
    if app.CGGetActiveDisplayList(max_displays, ids, ctypes.byref(count)) != 0:
        return []
    app.CGMainDisplayID.restype = ctypes.c_uint32
    main_id = int(app.CGMainDisplayID())
    app.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    app.CGDisplayBounds.restype = _CGRect
    app.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
    app.CGDisplayPixelsWide.restype = ctypes.c_size_t
    app.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
    app.CGDisplayPixelsHigh.restype = ctypes.c_size_t

    out: list[DisplayInfo] = []
    for i in range(int(count.value)):
        did = int(ids[i])
        rect = app.CGDisplayBounds(did)
        w, h = float(rect.size.width), float(rect.size.height)
        pixels = int(app.CGDisplayPixelsWide(did))
        scale = round(pixels / w, 2) if w > 0 and pixels > 0 else None
        out.append(DisplayInfo(i, did, float(rect.origin.x),
                               float(rect.origin.y), w, h,
                               did == main_id, scale))
    return out


class CGEventBackend:
    """用 ctypes 调 CoreGraphics 发鼠标移动/按下/抬起事件。"""

    def __init__(self) -> None:
        ctypes, app, cf = _load_frameworks()
        self._ctypes = ctypes
        self._app = app
        self._cf = cf

        app.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
        app.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        app.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        app.CGEventPost.restype = None
        if hasattr(app, "CGEventSetIntegerValueField"):
            app.CGEventSetIntegerValueField.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
            app.CGEventSetIntegerValueField.restype = None
        app.CGEventCreateScrollWheelEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32]
        app.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
        app.CGPreflightPostEventAccess.argtypes = []
        app.CGPreflightPostEventAccess.restype = ctypes.c_bool
        app.CGRequestPostEventAccess.argtypes = []
        app.CGRequestPostEventAccess.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None

    @staticmethod
    def request_access() -> bool:
        """弹系统授权框（辅助功能）。只在标定工具里显式调用。"""
        if sys.platform != "darwin":
            return False
        ctypes, app, _cf = _load_frameworks()
        app.CGRequestPostEventAccess.restype = ctypes.c_bool
        return bool(app.CGRequestPostEventAccess())

    def preflight(self) -> bool:
        return bool(self._app.CGPreflightPostEventAccess())

    def _post(self, event_type: int, x: float, y: float) -> None:
        ctypes = self._ctypes
        event = self._app.CGEventCreateMouseEvent(
            None, event_type, _CGPoint(float(x), float(y)), kCGMouseButtonLeft)
        if not event:
            raise ClickerError("CGEventCreateMouseEvent 返回空事件")
        try:
            if event_type in (kCGEventLeftMouseDown, kCGEventLeftMouseUp) \
                    and hasattr(self._app, "CGEventSetIntegerValueField"):
                self._app.CGEventSetIntegerValueField(
                    event, kCGMouseEventClickState, 1)
            self._app.CGEventPost(kCGHIDEventTap, event)
        finally:
            self._cf.CFRelease(event)

    def move(self, x: float, y: float) -> None:
        self._post(kCGEventMouseMoved, x, y)

    def mouse_down(self, x: float, y: float) -> None:
        self._post(kCGEventLeftMouseDown, x, y)

    def mouse_up(self, x: float, y: float) -> None:
        self._post(kCGEventLeftMouseUp, x, y)

    def scroll(self, x: float, y: float, delta: int) -> None:
        """滚轮：先把光标移到 (x,y)，再发 line 单位的竖向滚动。

        web 端的滑杆监听 wheel 事件；滚动事件本身没有坐标，落到**光标所在窗口**，
        所以必须先 move。delta>0 = 向上滚（加额度），<0 = 向下。
        """
        if int(delta) == 0:
            return
        self.move(x, y)
        event = self._app.CGEventCreateScrollWheelEvent(
            None, kCGScrollEventUnitLine, 1, int(delta))
        if not event:
            raise ClickerError("CGEventCreateScrollWheelEvent 返回空事件")
        try:
            self._app.CGEventPost(kCGHIDEventTap, event)
        finally:
            self._cf.CFRelease(event)


# ─────────────────────────── 执行器 ───────────────────────────

class Clicker:
    """按配置执行点击。后端/随机数/时间函数都可注入。"""

    def __init__(self, config: ClickerConfig, backend: ClickBackend, *,
                 dry_run: bool = False,
                 rng: random.Random | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] | None = None,
                 bet_slider: "BetSliderController | None" = None,
                 force_slider: bool = False) -> None:
        config.validate()
        self.config = config
        self.backend = backend
        self.bet_slider = bet_slider
        self.force_slider = bool(force_slider)
        if bet_slider is not None and dry_run:
            bet_slider.dry_run = True
        self.dry_run = dry_run
        self.rng = rng or random.Random()
        self._sleep = sleep
        self._clock = clock
        self._log = log or (lambda _msg: None)

    def _jittered(self, base_ms: float) -> float:
        """把毫秒换成秒，并加 ±20% 的抖动；0 保持 0。"""
        if base_ms <= 0:
            return 0.0
        return max(0.0, base_ms * self.rng.uniform(0.8, 1.2) / 1000.0)

    def _run_bet_slider(self, option: "Option",
                        started: float) -> ClickResult:
        """只走长按滑杆，不用任何快捷档位。"""
        outcome = self.bet_slider.set_amount(float(option.amount))  # type: ignore[union-attr]
        status = {"set": "clicked", "dry_run": "dry_run",
                  "skipped": "skipped", "error": "error"}.get(
                      outcome.status, "error")
        return ClickResult(
            status, action=option.kind, mapping="bet_slider",
            **_amount_fields(option,
                             plan_bb=getattr(outcome, "target_bb", None),
                             final_bb=getattr(outcome, "final_bb", None)),
            reason=("bet_slider" if status in ("clicked", "dry_run")
                    else (outcome.reason or "bet_slider")),
            error=outcome.error,
            detail={"slider": outcome.to_dict()},
            elapsed_ms=int((self._clock() - started) * 1000))

    def run(self, advice: "Advice", *, targets: dict | None = None
            ) -> ClickResult:
        """执行一条建议。

        `targets`：动作名 → 屏幕逻辑点，由调用方按当前画面**现找**按钮后传入
        （见 `poker_ocr.action_buttons`）。为 None 时退回 profile 固定槽位，
        这样离线回放/没有屏幕权限时行为不变。
        """
        started = self._clock()
        option = advice.option
        plan: ClickPlan | None = None
        # 主动全下（kind=allin）也走滑杆：一直滚到顶端，不依赖 allin 按钮。
        # 面对「别人下注 > 我的后手」时 options.py 给的是 kind=call
        # （label=跟全下），仍走右侧 call 按钮，不在这里。
        if self.bet_slider is not None and option.kind == "allin":
            outcome = self.bet_slider.set_all_in()
            status = {"set": "clicked", "dry_run": "dry_run",
                      "skipped": "skipped", "error": "error"}.get(
                          outcome.status, "error")
            allin_bb = getattr(outcome, "final_bb", None)
            return ClickResult(
                status, action="allin", mapping="bet_slider",
                **_amount_fields(option, plan_bb=allin_bb,
                                 final_bb=allin_bb),
                reason=("bet_slider_allin" if status in ("clicked", "dry_run")
                        else (outcome.reason or "bet_slider_allin")),
                error=outcome.error,
                detail={"slider": outcome.to_dict()},
                elapsed_ms=int((self._clock() - started) * 1000))
        # --bet-slider-only：下注/加注无条件走滑杆，不判 5%、不看档位、
        # 不做置灰检测。档位/置灰逻辑本身有截图和 OCR，跳过它们能少两个
        # 失败点，适合「只想稳定用加注按钮」的场景。
        if (self.bet_slider is not None and self.force_slider
                and option.kind in ("bet", "raise")):
            if option.amount is None:
                return ClickResult("skipped", action=option.kind,
                                   reason="amount_unknown",
                                   elapsed_ms=int((self._clock() - started) * 1000))
            self._log("[点击] 强制滑杆：跳过快捷档位/5%/置灰检测")
            return self._run_bet_slider(option, started)
        # 下注/加注：先看推荐额是否落在某个快捷档位的 5% 内 —— 在就点档位
        # （快、稳）；不在才走长按滑杆手动加注。call/fold/allin 仍走普通按钮。
        if (self.bet_slider is not None and option.kind in ("bet", "raise")
                and option.amount is not None):
            chosen = choose_preset_within(
                option, dict(advice.preset_amounts),
                self.config.preset_tolerance)
            if chosen is not None and chosen[0] in self.config.buttons:
                preset_id, target_bb, mapping = chosen
                # 快捷档位可能因为后手不够 / 低于最低加注被置灰；置灰或
                # 读不准都不点，退回滑杆精确下注。
                enabled = None
                checker = getattr(self.bet_slider, "preset_enabled", None)
                if checker is not None:
                    try:
                        enabled = checker(preset_id)
                    except Exception:   # noqa: BLE001 — 读不准按不可点
                        enabled = None
                if enabled is True:
                    step = _step(preset_id, self.config, self.rng)
                    if step is not None:
                        plan = ClickPlan(option.kind, (step,),
                                         preset=preset_id,
                                         mapping=mapping, target_bb=target_bb)
                else:
                    self._log(f"[点击] 档位 {preset_id} 置灰/读不准，"
                              "改走滑杆")
            if plan is None:
                return self._run_bet_slider(option, started)
        if plan is None:
            plan = plan_clicks(advice, self.config, self.rng,
                               targets=targets)
        if plan.skip_reason is not None:
            return ClickResult(
                "skipped", action=plan.action, preset=plan.preset,
                mapping=plan.mapping, steps=plan.steps,
                reason=plan.skip_reason,
                **_amount_fields(option, plan_bb=plan.target_bb),
                elapsed_ms=int((self._clock() - started) * 1000))
        if not plan.steps:
            return ClickResult("skipped", action=plan.action,
                               reason="no_steps",
                               elapsed_ms=int((self._clock() - started) * 1000))

        if not self.dry_run:
            try:
                allowed = self.backend.preflight()
            except Exception as exc:        # noqa: BLE001 — 后端不可用不能拖垮主链路
                return ClickResult("error", action=plan.action, preset=plan.preset,
                                   mapping=plan.mapping, steps=plan.steps,
                                   error=f"{type(exc).__name__}: {exc}",
                                   **_amount_fields(option, plan_bb=plan.target_bb),
                                   elapsed_ms=int((self._clock() - started) * 1000))
            if not allowed:
                return ClickResult(
                    "error", action=plan.action, preset=plan.preset,
                    mapping=plan.mapping, steps=plan.steps,
                    error="accessibility_permission_denied",
                    **_amount_fields(option, plan_bb=plan.target_bb),
                    elapsed_ms=int((self._clock() - started) * 1000))

        down_at: tuple[float, float] | None = None
        try:
            for i, step in enumerate(plan.steps):
                if i > 0:
                    self._sleep(self._jittered(self.config.confirm_gap_ms))
                if self.dry_run:
                    self._log(f"[点击] dry-run {step.button} ← "
                              f"({step.x:.1f}, {step.y:.1f})")
                    continue
                self.backend.move(step.x, step.y)
                down_at = (step.x, step.y)
                self.backend.mouse_down(step.x, step.y)
                self._sleep(self._jittered(self.config.click_delay_ms))
                self.backend.mouse_up(step.x, step.y)
                down_at = None
        except Exception as exc:            # noqa: BLE001 — 点击失败只记账，不重试
            if down_at is not None and not self.dry_run:
                try:
                    self.backend.mouse_up(*down_at)
                except Exception:           # noqa: BLE001 — 收尾尽力而为
                    pass
            return ClickResult("error", action=plan.action, preset=plan.preset,
                               mapping=plan.mapping, steps=plan.steps,
                               error=f"{type(exc).__name__}: {exc}",
                               **_amount_fields(option, plan_bb=plan.target_bb),
                               elapsed_ms=int((self._clock() - started) * 1000))

        return ClickResult(
            "dry_run" if self.dry_run else "clicked",
            action=plan.action, preset=plan.preset, mapping=plan.mapping,
            steps=plan.steps,
            **_amount_fields(option, plan_bb=plan.target_bb,
                             final_bb=plan.target_bb),
            elapsed_ms=int((self._clock() - started) * 1000))
