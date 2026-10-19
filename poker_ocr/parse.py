"""字段解析（M2.3）：把 ROI 的**原始文本**变成数字/枚举，解析不了就说解析不了。

这一层存在的理由就是「安静错值」：OCR 会把 `11BB` 读成 `11B`、把 `01BB` 开头
的筹码图标读成 `0`、把底池读成 `总底池`、把排行榜的 `入池27%` 读进下注槽。
下面每条规则都是拿真机数据（213 帧）反推的，不是拍脑袋。

**总原则：宁可返回 None，也不返回一个看起来像真的错值。**
解析失败由调用方（frame_state）转成「这一帧丢掉」或「这个字段未知」。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# ─────────────────────────── 金额 ───────────────────────────

# 无单位但明确是 0 的写法。实测 All-in 玩家的筹码胶囊会被读成 `。`/`o`，
# 「正在带入」的玩家也显示裸 0 —— 这两种都是真实的 0，不是漏读。
_ZERO_GLYPHS = frozenset({"0", "０", "。", "o", "O", "〇"})

# 金额形状：可选的图标/噪声前缀（`C6BB`、`O0.5BB`、`01BB`），数字，
# 单位（BB 或单 B —— 实测 `11BB` 会被读成 `11B`），最多 3 个尾巴噪声
# （`9BBO`、`0.5BBO`）。
#
# 有没有单位是**硬门槛**：`入池27%`、`胜率15%`、`200`、`88.` 都能被这个正则
# 匹出数字来，但都没有单位 —— 没有单位就没有「这是 BB 金额」的证据，一律不采信。
_AMOUNT_RE = re.compile(r"^[^0-9]*(\d+(?:\.\d+)?)\s*(BB|B)?[^0-9]{0,3}$")


@dataclass
class Amount:
    raw: str
    value: float | None
    unit: str | None
    zero_no_unit: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None

    def to_debug(self) -> dict:
        d: dict = {"raw": self.raw, "value": self.value}
        if self.unit:
            d["unit"] = self.unit
        if self.zero_no_unit:
            d["zero_no_unit"] = True
        if self.error:
            d["error"] = self.error
        return d


# 宽松筹码解析：只给 live 的固定 ROI 用。全图路线也用同一套规则。
# 只接受**以数字开头**的文本（前面最多是货币符号/图标），名字里的数字不会被当筹码。
_LOOSE_NUM_RE = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]*(\d[\d,]*(?:\.\d+)?)")


def parse_chip_loose(text: str | None) -> float | None:
    """`1,234`、`$1,234`、`243DB`、`478.5B8`、`460.538` 都取前导数字。

    严格 `parse_amount` 要求 BB 单位；固定 ROI 的筹码槽上下文已经确定，
    单位丢失/误读（BB→38/DB/B8）时用这个兜底。**只在明确知道是筹码槽时用**。
    """
    t = (text or "").strip()
    if not t or "%" in t or "/" in t or ":" in t:
        return None
    m = _LOOSE_NUM_RE.match(t)
    if not m:
        return None
    rest = t[m.end():].strip()
    if len(rest) > 4:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_amount(text: str | None) -> Amount:
    """筹码 / 下注 / 底池的文本 → BB 数值。

    与 `fields.parse_chip` 的区别：那个是**筹码专用**、要求严格的 BB 后缀；
    这里要有意放宽（单 B、前导图标、尾巴噪声），因为下注 pill 实测就是会这样错。
    放宽的边界靠「必须有单位」守住。
    """
    t = (text or "").strip()
    if not t:
        return Amount(t, None, None, error="空文本")
    if t in _ZERO_GLYPHS:
        return Amount(t, 0.0, None, zero_no_unit=True)

    m = _AMOUNT_RE.match(t)
    if not m:
        return Amount(t, None, None, error="不是金额形状")
    num, unit = m.group(1), m.group(2)
    if unit is None:
        return Amount(t, None, None,
                      error="没有 BB 单位，值可能是误读（如 BB→88），不采信")
    value = float(num)
    # `01BB` / `00.5BB` 的开头 0 是左边的筹码图标；float 天然处理掉。
    return Amount(t, value, unit.upper())


# ─────────────────────────── 盲注 ───────────────────────────

# `1/2(1) 521222 00:38:31`：只取开头。括号里的前注可能缺失，也可能被读丢。
#
# 数字限位 + 后面的「不许紧跟数字」是防**融合**：实测 OCR 会把房间号粘上来 ——
# `1/20052220039`、`1/21）`、`1/20）`。限位让这类文本匹配失败（→ 继承上一帧），
# 而不是解析出一个 sb≈5e-11 的荒谬值。
_BLINDS_RE = re.compile(
    r"^\s*(\d{1,3}(?:\.\d+)?)\s*/\s*(\d{1,3}(?:\.\d+)?)(?![0-9])\s*"
    r"(?:[（(]\s*(\d{1,3}(?:\.\d+)?)\s*[）)])?"
)

# 合法盲注比：OCR 自动读数要求 bb/sb 落在 [2, 10]；真实牌桌的 1/2、2/5、5/10、
# 10/20 都在这个区间。`1/21` 之类是融合产物 → 拒绝并继承上一帧。
# 注意 1/20 这种真实高比例盲注也在区间外（2026-10-19 实测桌面），
# 所以**人工显式指定**的 `--initial-blinds` 走 parse_blinds(max_ratio=50)。
_MIN_BB_RATIO = 2.0
_MAX_BB_RATIO = 10.0


@dataclass
class Blinds:
    sb: float                # 小盲，单位 BB
    bb: float                # 大盲，单位 BB（恒为 1）
    ante: float | None       # 前注，单位 BB/人；None = 括号被读丢，未知
    raw: str

    def to_debug(self) -> dict:
        return {"sb": self.sb, "bb": self.bb, "ante": self.ante, "raw": self.raw}


def parse_blinds(text: str | None,
                 max_ratio: float = _MAX_BB_RATIO) -> Blinds | None:
    """`1/2(1)` → sb=0.5BB / bb=1BB / ante=0.5BB。

    **不猜缺斜杠的写法**（实测 OCR 会给出 `12（1）`）：`12` 既可能是 1/2 也可能
    真是 12，猜错会让整份数据的尺度错掉。解析不了 → None → 继承上一帧的盲注。

    `max_ratio` 只应给**人工显式指定**的盲注放宽（如 `--initial-blinds 1/20`）：
    OCR 自动读到的横幅仍用默认 10，避免把融合产物（`1/21）...`）解析成合法值。
    """
    t = (text or "").strip()
    m = _BLINDS_RE.match(t)
    if not m:
        return None
    sb_chips, bb_chips = float(m.group(1)), float(m.group(2))
    if sb_chips <= 0 or bb_chips <= 0:
        return None
    ratio = bb_chips / sb_chips
    if not (_MIN_BB_RATIO <= ratio <= max_ratio):
        return None
    ante_chips = float(m.group(3)) if m.group(3) else None
    # 这个 app 的盲注行是「筹码数」，1 BB = bb_chips 个筹码 → 全部折成 BB。
    return Blinds(sb=sb_chips / bb_chips, bb=1.0,
                  ante=(ante_chips / bb_chips if ante_chips is not None else None),
                  raw=t)


# ─────────────────────────── 动作 / 名字 ───────────────────────────

# app 直接渲染的动作徽章（画在名字位置，会把名字盖住）。词汇表从真机收齐。
ACTION_WORDS: dict[str, str] = {
    "让牌": "check",
    "跟注": "call",
    "下注": "bet",
    "加注": "raise",
    "弃牌": "fold",
}

# 「弃牌」徽章画在**头像**上。实测 rec 会把它系统性读成形近字：
# `弃牌` → `奔牌` / `奔脚` / `奔摊` / `奔雄`（分数 0.5–0.94）。
# 所以按「首字是 弃/奔」判，而不是等它读对。**这是 best-effort**：实测同一帧里
# 5 个弃牌玩家会漏掉 2 个（rec 直接返回空），所以下游必须允许 fold=None。
_FOLD_RE = re.compile(r"^[弃奔]")


def parse_action(name_text: str | None) -> str | None:
    """名字槽的文本如果是动作徽章，返回英文动作；否则 None。"""
    t = (name_text or "").strip()
    return ACTION_WORDS.get(t)


def is_fold_badge(avatar_text: str | None, score: float = 0.0,
                  min_score: float = 0.4) -> bool | None:
    """头像 ROI 的读数是不是「弃牌」徽章。三态：True / False / None（没读到）。

    None 与 False 要分开：没读到（头像照片、遮挡）不代表「没弃牌」。
    """
    t = (avatar_text or "").strip()
    if not t:
        return None
    if score < min_score:
        return None
    return bool(_FOLD_RE.match(t))


def names_similar(a: str | None, b: str | None, threshold: float = 0.6) -> bool:
    """两个名字像不像「同一个人被 OCR 读出了不同版本」。

    实测抖动：`YKY/YkY/YKkY`、`一只猫i/ii`、`Dragon333/Oragon333`。
    真实换人：`Dragon333→咸蛋鸡柳饭`、`YKY→一只猫ii` —— 相似度都是 0，分得开。
    **同名不同人**（两个真的都叫「陈凤」）这一层区分不了，属已知限制。
    """
    if not a or not b:
        return False
    na, nb = a.replace(" ", "").lower(), b.replace(" ", "").lower()
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def sticky_name(prev: str | None, new: str | None) -> str | None:
    """名字粘性：新读法和旧名字像同一个人，就保留旧名字（消掉 OCR 抖动）。"""
    if not new:
        return prev
    if prev and names_similar(prev, new):
        return prev
    return new


class NameTracker:
    """每座名字的稳定器：新读法要**连续确认 N 帧**才换身份。

    实测同一个座位会被读成 `YKY / YkY / YKkY / 東Yk丫`，而真正的换人
    （`Dragon333` → `咸蛋鸡柳饭`）是低频事件。所以策略是「先信旧名字，
    候选连续出现 confirm 次才换」—— 比调相似度阈值可靠：单帧/两帧的乱码
    根本进不了输出，换人也只需要 1.5s（confirm=3 @ 0.5s）就能反映出来。
    """

    def __init__(self, confirm: int = 3) -> None:
        self.confirm = max(1, int(confirm))
        self._names: dict[str, str] = {}
        self._cand: dict[str, str] = {}
        self._count: dict[str, int] = {}

    def current(self) -> dict[str, str]:
        return dict(self._names)

    def resolve(self, seat_id: str, raw: str | None,
                action: str | None = None) -> str | None:
        cur = self._names.get(seat_id)
        if action:                       # 动作徽章盖住了名字：沿用旧名字
            return cur
        if not raw:
            return cur
        if cur is None:                  # 第一次见：直接认
            self._names[seat_id] = raw
            self._clear(seat_id)
            return raw
        if names_similar(cur, raw):      # 同一人的另一个读法：保留首读
            self._clear(seat_id)
            return cur
        if self._cand.get(seat_id) == raw:
            self._count[seat_id] += 1
        else:
            self._cand[seat_id], self._count[seat_id] = raw, 1
        if self._count[seat_id] >= self.confirm:
            self._names[seat_id] = raw
            self._clear(seat_id)
            return raw
        return cur

    def _clear(self, seat_id: str) -> None:
        self._cand.pop(seat_id, None)
        self._count.pop(seat_id, None)
