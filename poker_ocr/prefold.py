"""翻前提前弃牌（预点「让/弃」）。

牌桌上头像左边是「让/弃」、右边是「让/跟」。拿到「哪个位置都不开池」的垃圾牌
（27o 这类）时，没必要等轮到自己再走一遍「看建议 → 点弃牌」：在**别人还在
行动**时点一下左边那颗灰按钮，客户端把它记成预选（按钮背景灰→红），轮到我
时自动执行；有人加注就自动弃牌。省掉的是最后那几秒。

边界（2026-11-16 维护者逐条拍板）：

  · 只做**翻前**，只预点**弃牌**；不预点跟注/加注，不碰翻后。
  · 大盲也预点：没人加注时预选解析成「让牌」，照样免费看翻牌；有人加注才
    变成弃牌。
  · 轮到我了还没预选上（例如我是第一个行动）→ **直接点弃牌**，不问建议。
  · fail closed：牌没读全/不稳定、手牌还没换、没有「让/弃」按钮槽位 → 不点。
    点错一下比晚一秒弃牌贵得多。
  · 预选一旦被确认（按钮变红）就**不再点**：再点一次会取消预选。
  · 看到过红、红又消失（我手工取消，或者客户端已经执行）→ 这一手不再补点。
  · 看不到按钮颜色时**最多只点一次**：盲点第二次可能把刚挂上的预选取消掉。

这一层只做判定与状态机；鼠标、日志、事件由调用方注入（`clicker.press`、
`log`），所以测试里用假点击器和假时钟就能把每条反例跑一遍。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .hand_track import PREFLOP
from .preflop_ranges import always_fold, hand_key

MODE_OFF = "off"
MODE_DRY = "dry"
MODE_LIVE = "live"
MODES: tuple[str, ...] = (MODE_OFF, MODE_DRY, MODE_LIVE)

# 两次尝试之间至少间隔多久（秒）。一次点击的鼠标事件 ~几十 ms，400ms 足够让
# 客户端把预选画出来，又不会拖到下一个行动。
BETWEEN_TRIES_S = 0.4
# 每手最多点几次。**这只是上限**：看到红色确认就收手，看不到颜色时只点一次。
MAX_TRIES = 2
# 点了这么多次仍看不到红色 → 放弃并告警，不再和客户端抢这一手。
GIVE_UP_S = 2.0
# 「让/弃」按钮被判成「已选中」的红占比门槛（灰按钮实测 0.00）。
RED_MIN = 0.15

DEFAULT_EXCLUDE_PATH = "config/prefold_trash.json"

# 手牌 key 的合法形状：两张不同点数的牌 + 是否同花。对子不带 s/o。
_KEY_RANKS = "23456789TJQKA"


def iter_hand_keys() -> list[str]:
    """全部 169 个起手牌 key（对子 / 同花 / 非同花各一套）。"""
    keys: list[str] = []
    for i, hi in enumerate(_KEY_RANKS):
        for lo in _KEY_RANKS[:i + 1]:
            if hi == lo:
                keys.append(hi + lo)
            else:
                keys.append(f"{hi}{lo}s")
                keys.append(f"{hi}{lo}o")
    return keys


def derived_trash_keys() -> frozenset[str]:
    """`always_fold` 派生的恒弃集合（当前 49 个非同花 key）。"""
    return frozenset(k for k in iter_hand_keys() if always_fold(_cards_of(k)))


def _cards_of(key: str) -> tuple[str, str]:
    """把 'KJo' 还原成两张具体牌：'Kh', 'Jc'（同花用同一花色）。"""
    hi, lo = key[0], key[1]
    if len(key) == 2:                 # 对子
        return (f"{hi}h", f"{lo}c")
    if key[2] == "s":                 # 同花：两张同花色
        return (f"{hi}h", f"{lo}h")
    return (f"{hi}h", f"{lo}c")       # 非同花


def load_exclude(path: str | Path | None) -> frozenset[str]:
    """读 `config/prefold_trash.json` 的排除名单；文件不存在返回空集合。

    文件格式 `{"version": 1, "exclude": ["A2o", "K2o"]}`。**fail fast**：
    形状不对、key 不合法、或者该 key 本来就不在恒弃集合里（写错了/拼错了）
    都直接抛 `ValueError`，不让它静默地什么都不排除。
    """
    if path is None:
        return frozenset()
    file = Path(path)
    if not file.exists():
        return frozenset()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"读不了 {file}：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{file} 顶层必须是对象")
    raw = data.get("exclude") or []
    if not isinstance(raw, list):
        raise ValueError(f"{file} 的 exclude 必须是字符串数组")
    valid = derived_trash_keys()
    out: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{file} 的 exclude 里有非字符串：{item!r}")
        norm = _normalize_key(item.strip())
        if norm is None:
            raise ValueError(f"{file} 里的 {item!r} 不是合法的起手牌 key")
        if norm not in valid:
            raise ValueError(
                f"{file} 里的 {item!r} 不在恒弃集合里（它本来就不该被预弃）")
        out.add(norm)
    return frozenset(out)


def _normalize_key(key: str) -> str | None:
    """'kjo' / 'KJo' / 'kjO' → 'KJo'；不合法返回 None。"""
    text = key.upper()
    if len(text) == 2:
        hi, lo = text[0], text[1]
        if hi not in _KEY_RANKS or lo not in _KEY_RANKS or hi != lo:
            return None
        return hi + lo
    if len(text) == 3:
        hi, lo, tail = text[0], text[1], text[2].lower()
        if (hi not in _KEY_RANKS or lo not in _KEY_RANKS
                or hi == lo or _KEY_RANKS.index(hi) < _KEY_RANKS.index(lo)):
            return None
        if tail not in ("s", "o"):
            return None
        return f"{hi}{lo}{tail}"
    return None


@dataclass(frozen=True)
class PrefoldEvent:
    """一次预点动作的结果，调用方拿去打日志 / 发事件。"""

    kind: str            # armed / confirmed / released / fold_now / gave_up / mode
    hand_id: str | None
    hand: str = ""
    detail: str = ""
    tries: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "hand_id": self.hand_id,
            "hand": self.hand,
            "detail": self.detail,
            "tries": self.tries,
        }


class PrefoldManager:
    """一手牌内的预点状态机。

    帧从识别主循环喂（`update`），开关从 Web 线程切（`set_mode`）：两者都
    拿同一把锁串行化，免得「一边取消、一边预选」落到客户端变成两次相反动作。
    锁内只做判定与一次点击（点击本身几十 ms），不会长期占住主循环。
    """

    def __init__(self, *, mode: str = MODE_OFF,
                 clicker: Any = None,
                 clock: Callable[[], float] = time.monotonic,
                 exclude: frozenset[str] = frozenset(),
                 log: Callable[[str], None] | None = None,
                 red_min: float = RED_MIN,
                 min_gap: float = BETWEEN_TRIES_S,
                 max_tries: int = MAX_TRIES,
                 give_up: float = GIVE_UP_S) -> None:
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
        self.mode = mode
        self.clicker = clicker
        self.exclude = frozenset(exclude)
        self.red_min = float(red_min)
        self.min_gap = float(min_gap)
        self.max_tries = int(max_tries)
        self.give_up = float(give_up)
        self._clock = clock
        self._log = log or (lambda _msg: None)
        self._hand_id: str | None = None
        self._status = "idle"     # idle/armed/confirmed/spent/released/gave_up/retiring
        self._hand = ""
        self._tries = 0
        self._last_at = float("-inf")
        self._seen_red = False
        self._dry_logged = False
        self._retired_once = False
        self._retries = 0
        # 开关从 Web 线程切、帧从主循环喂：状态机与「点一下」必须串行，
        # 否则可能一边取消一边预选，落到客户端就是两次相反的动作。
        self._lock = threading.RLock()
        self.count = 0            # 累计真正预选上的手数
        self.clicks = 0           # 累计按下的次数

    # ── 外部状态 ───────────────────────────────────────────────
    def set_mode(self, mode: str) -> list[str]:
        """切换开关；返回需要调用方显示的告警（切 off 时预选还挂着）。"""
        with self._lock:
            return self._set_mode_locked(mode)

    def _set_mode_locked(self, mode: str) -> list[str]:
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
        warnings: list[str] = []
        if mode == MODE_OFF and self._status in ("armed", "confirmed"):
            if self._status == "confirmed":
                # 确认过「已挂上」，再点一次就是取消。**不在这里点**：开关是
                # 从 Web 线程切的，鼠标事件统一留给主循环下一帧做（`_retire_step`），
                # 免得和正在执行的点击撞在一起。
                self._status = "retiring"
                self._retries = 0
                self._retired_once = False
                warnings.append("预选还挂着：下一帧会点一次取消，结果见日志")
            else:
                warnings.append(
                    "预选状态未知（点过但没看到红色）：不盲点取消，请自己看一眼"
                    "那颗按钮是不是红的")
        self.mode = mode
        return warnings

    @property
    def retiring(self) -> bool:
        """切回 off 之后还在等「取消那一按」的结果。"""
        return self._status == "retiring"

    def active(self, hand_id: str | None) -> bool:
        """这一手是否由预点接管（接管期间不产生建议行）。"""
        if self.mode == MODE_OFF or hand_id is None:
            return False
        if hand_id != self._hand_id:
            return False
        # released（我自己取消）之后不再接管：这一手我要自己打，建议照给。
        return self._status in ("armed", "confirmed", "spent", "gave_up")

    def reset(self) -> None:
        self._hand_id = None
        self._status = "idle"
        self._hand = ""
        self._tries = 0
        self._last_at = float("-inf")
        self._seen_red = False
        self._dry_logged = False
        self._retired_once = False
        self._retries = 0

    # ── 主判定 ─────────────────────────────────────────────────
    def update(self, *, hand_id: str | None, street: str | None,
               hero_cards: Any, cards_reliable: bool,
               hero_turn: bool | None, fold: dict | None
               ) -> PrefoldEvent | None:
        """喂一帧；需要动作就动作，返回发生了什么（None = 没事）。"""
        with self._lock:
            return self._update_locked(hand_id=hand_id, street=street,
                                       hero_cards=hero_cards,
                                       cards_reliable=cards_reliable,
                                       hero_turn=hero_turn, fold=fold)

    def _update_locked(self, *, hand_id: str | None, street: str | None,
                       hero_cards: Any, cards_reliable: bool,
                       hero_turn: bool | None, fold: dict | None
                       ) -> PrefoldEvent | None:
        if hand_id is not None and hand_id != self._hand_id:
            self.reset()
            self._hand_id = hand_id
        if self._status == "retiring":
            return self._retire_step(hand_id, fold)

        if self.mode == MODE_OFF or self.clicker is None:
            return None
        if street != PREFLOP or not cards_reliable:
            return None
        cards = tuple(hero_cards or ())
        if len(cards) != 2:
            return None
        key = self._key_of(cards)
        if key is None or key in self.exclude:
            return None
        if not always_fold(cards):
            return None

        red = self._is_red(fold)
        if hero_turn is True:
            return self._on_turn(hand_id, key, red)
        return self._waiting(hand_id, key, red)

    # ── 各状态 ─────────────────────────────────────────────────
    def _on_turn(self, hand_id: str | None, key: str, red: bool | None
                 ) -> PrefoldEvent | None:
        """真轮到我了：预选没挂上（或客户端没吃进去）→ 直接弃，不问建议。"""
        if self._status == "released":
            # 我自己把预选取消了：这一手我要自己打。取消动作本身就是投票，
            # 别再替我弃一次（否则「取消」等于没取消）。
            return None
        if self._status == "spent":
            # 已经替自己弃过了：回合可能还亮着几帧，别按第二下。
            return None
        if red is True:
            # 预选还挂着，客户端马上会执行；这时候再点等于取消。
            return None
        if self._status == "confirmed":
            # 确认过「预选挂上」，现在红没了 = 客户端已经执行完了（或正把按钮
            # 淡出）。再点一下会落到已经变的界面上 —— 什么都不做最安全。
            self._status = "spent"
            return None
        if self.mode == MODE_DRY:
            if not self._dry_logged:
                self._dry_logged = True
                return PrefoldEvent("armed", hand_id, key,
                                    detail="dry-run：轮到我时会直接弃牌，没有真的点")
            return None
        if self._tries >= self.max_tries:
            return None
        now = self._clock()
        if now - self._last_at < self.min_gap:
            return None
        self._status = "spent"
        ok = self._press("轮到我但预选没挂上，直接弃牌")
        self._tries += 1
        self._last_at = now
        return PrefoldEvent("fold_now", hand_id, key,
                            detail="" if ok else "点击失败", tries=self._tries)

    def _waiting(self, hand_id: str | None, key: str,
                 red: bool | None) -> PrefoldEvent | None:
        """还没轮到我：预选 / 确认 / 被取消 / 放弃。"""
        if self._status in ("spent", "released", "gave_up"):
            return None
        if red is True:
            self._seen_red = True
            self._last_at = self._clock()
            if self._status != "confirmed":
                self._status = "confirmed"
                self._hand = key
                self.count += 1
                return PrefoldEvent("confirmed", hand_id, key,
                                    detail="按钮已变红，等客户端执行",
                                    tries=self._tries)
            return None
        if self._seen_red and red is False:
            self._status = "released"
            return PrefoldEvent("released", hand_id, key,
                                detail="红色消失（已取消或已执行），这一手不再补点",
                                tries=self._tries)

        # 看不到颜色时最多一次：盲目点第二下会把刚挂上的预选取消掉。
        limit = self.max_tries if red is not None else 1
        if self._tries >= limit:
            if (self._tries and red is not None
                    and self._clock() - self._last_at >= self.give_up
                    and self._status != "gave_up"):
                self._status = "gave_up"
                # 预点放弃 ≠ 不弃牌：真轮到我时仍要直接弃，给它一份新预算。
                self._tries = 0
                return PrefoldEvent("gave_up", hand_id, key,
                                    detail="点了没看到红色，放弃预点；轮到我时直接弃",
                                    tries=self._tries)
            return None
        now = self._clock()
        if now - self._last_at < self.min_gap:
            return None
        if self.mode == MODE_DRY:
            if not self._dry_logged:
                self._dry_logged = True
                self._hand = key
                self._status = "armed"
                return PrefoldEvent("armed", hand_id, key,
                                    detail="dry-run：没有真的点")
            return None
        self._status = "armed"
        self._hand = key
        ok = self._press(f"预点让/弃（第 {self._tries + 1} 次）")
        self._tries += 1
        self._last_at = now
        return PrefoldEvent("armed" if ok else "error", hand_id, key,
                            detail="" if ok else "点击失败", tries=self._tries)

    def _retire_step(self, hand_id: str | None,
                     fold: dict | None) -> PrefoldEvent | None:
        """切回 off 之后，把还挂着的预选取消掉（最多再点一次）。"""
        red = self._is_red(fold)
        if red is False:
            self._status = "idle"
            return PrefoldEvent("mode", hand_id, self._hand,
                                detail="预选已取消，预点已关闭")
        if not self._retired_once:
            self._retired_once = True
            self._press("取消预选（切回 off）")
            return PrefoldEvent("mode", hand_id, self._hand,
                                detail="已点一次取消预选")
        if red is True and self._retries < 1:
            self._retries = 1
            self._press("再点一次取消预选")
            return PrefoldEvent("mode", hand_id, self._hand,
                                detail="预选仍是红的，已再点一次取消")
        if red is True:
            self._status = "idle"
            return PrefoldEvent("mode", hand_id, self._hand,
                                detail="取消失败：按钮还是红的，请手动点一下")
        # 看不见颜色：不能确认取消成功，也不能再点（可能又挂上）。如实告警。
        self._status = "idle"
        return PrefoldEvent("mode", hand_id, self._hand,
                            detail="看不见按钮颜色，无法确认预选已取消，请手动看一眼")

    # ── 小工具 ─────────────────────────────────────────────────
    def _is_red(self, fold: dict | None) -> bool | None:
        """按钮是不是红的。None = 看不见（没有槽位/读数不可用）。"""
        if not fold or not fold.get("available"):
            return None
        return float(fold.get("red") or 0.0) >= self.red_min

    def _key_of(self, cards: tuple[str, ...]) -> str | None:
        try:
            return hand_key(cards)
        except (TypeError, ValueError, IndexError):
            return None

    def _press(self, why: str) -> bool:
        """按一下「让/弃」。失败只记一行，不抛 —— 预点不能拖垮识别。"""
        try:
            result = self.clicker.press("fold")
        except Exception as exc:  # noqa: BLE001 — 点击后端故障不中断主循环
            self._log(f"  [预点] 点击失败：{type(exc).__name__}: {exc}")
            return False
        status = getattr(result, "status", None)
        self.clicks += 1
        if status not in ("clicked", "dry_run"):
            self._log(f"  [预点] {why}：没点上（{status}"
                      f"{'/' + str(getattr(result, 'reason', '')) if getattr(result, 'reason', None) else ''}）")
            return False
        self._log(f"  [预点] {why}")
        return True
