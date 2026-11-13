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
import time
from dataclasses import dataclass
from typing import Callable

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
# OCR 会把单位后的 O 读成数字 0：`1BB0` / `0.5BB0`。旧正则的尾巴只允许非数字，
# 于是整块被拒成「不是金额形状」，质量闸丢帧。只放宽**单位后紧跟的 0/O**，
# 不放宽「1BB8」这类真实数字融合。
_AMOUNT_TRAILING_ZERO_RE = re.compile(
    r"^[^0-9]*(\d+(?:\.\d+)?)\s*(BB|B)\s*[0O]{1,2}$")

# 数字清洗：下注 pill 两侧的**筹码图标**会被 rec 读成数字贴在金额上 ——
# `55BB0`（尾巴 0）、`1BB8`（尾巴 8）、`26.5BB`（前导 2 是图标）。
# 前两条正则要求「单位后是非数字（≤3）」或「单位后是 0/O（≤2）」，遇到
# `1BB8` 这种就把整块拒成「不是金额形状」→ 质量闸丢整帧。2026-11-12 实盘：
# upR 的 `5.5BB` 读成 `55BBO`，一场 36 帧被这个闸拦掉。
#
# 这里只做**清洗**：认到单位为止，前导字符（≤3）和单位后尾巴（≤2）都丢掉。
# 不猜小数点、不猜倍率 —— 55BB 就是 55BB，是不是真值交给下游的
# 「下注 > 底池」闸去判，别在这一层编数字。
_AMOUNT_CORE_RE = re.compile(r"^[^0-9]{0,3}(\d+(?:\.\d+)?)\s*(BB|B)")
_AMOUNT_TAIL_NOISE_MAX = 2


@dataclass
class Amount:
    raw: str
    value: float | None
    unit: str | None
    zero_no_unit: bool = False
    error: str | None = None
    # 清洗掉的尾巴（单位后面的筹码图标/误读字符）。有值说明这个数是从
    # `55BB0` 这类带噪声的文本里洗出来的，统计时能单独数一遍。
    tail_noise: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None

    def to_debug(self) -> dict:
        d: dict = {"raw": self.raw, "value": self.value}
        if self.unit:
            d["unit"] = self.unit
        if self.zero_no_unit:
            d["zero_no_unit"] = True
        if self.tail_noise:
            d["tail_noise"] = self.tail_noise
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
        m = _AMOUNT_TRAILING_ZERO_RE.match(t)
    if not m:
        # ③ 数字清洗：两侧的筹码图标读数丢掉，只留「数字 + 单位」。
        core = _AMOUNT_CORE_RE.match(t)
        tail = t[core.end():] if core else ""
        if core and len(tail) <= _AMOUNT_TAIL_NOISE_MAX:
            return Amount(t, float(core.group(1)), core.group(2).upper(),
                          tail_noise=tail or None)
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
# **弃牌只在头像上**：名字区读到「弃牌」不是动作，见 parse_action。
ACTION_WORDS: dict[str, str] = {
    "让牌": "check",
    "跟注": "call",
    "下注": "bet",
    "加注": "raise",
    "弃牌": "fold",
}

# 名字槽能产出的动作类型（#23）。弃牌不在此列：头像弃牌走 is_fold_badge。
_NAME_ACTION_WORDS: tuple[tuple[str, str], ...] = (
    ("让牌", "check"), ("跟注", "call"), ("下注", "bet"), ("加注", "raise"))

# OCR 读残的常见形态：`下注法`、`跟注海`、`廣加注`、`跟注法`。徽章是两字词，
# 前后多/少一个字仍按「含这个词」认；但**裸单字绝不单独成动作**。
_BADGE_NOISE = " \t\r\n\u3000·・.。,，;；:：!！?？'\"“”‘’()（）[]【】<>《》_-—~～"
_BADGE_CHARS = frozenset("下加跟让注牌弃奔")


def _clean_badge_text(text: str | None) -> str:
    """去掉首尾空白与标点噪声；不动中间的字，避免把 `跟 注` 之外的读残洗掉。"""
    return (text or "").strip().strip(_BADGE_NOISE)

# 「弃牌」徽章画在**头像**上。实测 rec 会把它系统性读成形近字：
# `弃牌` → `奔牌` / `奔脚` / `奔摊` / `奔雄`（分数 0.5–0.94）。
# 只接受「弃/奔 + 一个形近字/标点」的短文本：旧口径 `^[弃奔]` 会把
# 名字「奔跑的...」「弃疗...」也判成弃牌，0c2e 的 LJ 就是这样被误伤。
# **这是 best-effort**：实测同一帧里 5 个弃牌玩家会漏掉 2 个（rec 直接返回空），
# 所以下游必须允许 fold=None。
_FOLD_RE = re.compile(r"^(?:已)?[弃奔](?:[牌脚摊雄权])?[#＃!！。.·]?$")


def parse_action(name_text: str | None) -> str | None:
    """名字槽的文本如果是动作徽章，返回英文动作；否则 None。

    徽章会被 OCR 读残（`下注法` / `廣加注` / `跟注海`），所以按「含徽章词」
    认，词出现得越靠前越优先。三条硬边界：
      · 裸单字（`下`/`加`/`跟`/`让`/`注`）一律不认 —— 单字是名字残片的重灾区；
      · **名字区永不产出 fold** —— 弃牌只认头像（is_fold_badge）；
      · 只认这四类动作，`弃牌`/`奔牌` 之类留给头像路径。
    """
    t = _clean_badge_text(name_text)
    if not t or len(t) == 1:
        return None
    hits = [(t.find(word), action) for word, action in _NAME_ACTION_WORDS
            if word in t]
    return min(hits)[1] if hits else None


def looks_like_badge(text: str | None) -> bool:
    """名字槽这段文本是不是徽章（含读残），用来保护玩家名字。

    比 parse_action 宽：只要像徽章残片就算 —— 裸单字（`下`/`加`/`跟`/`让`/
    `注`/`牌`/`弃`/`奔`）和含弃牌痕迹的短文本都算。名字更新与名单钉住都要
    跳过这些帧，否则玩家名会被写成 `跟` / `加` / `下`（#23）。
    """
    t = _clean_badge_text(text)
    if not t:
        return False
    if parse_action(t) is not None:
        return True
    if len(t) == 1:
        return t in _BADGE_CHARS
    if _FOLD_RE.match(t):
        return True
    return any(word in t for word, _ in _NAME_ACTION_WORDS)


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


# 「All in」徽章画在头像上，黄色字。实测 rec 会读成 `Alin`（score 0.60–0.74），
# 所以按「去掉空格/连字符后与 allin 足够像」判，而不是要求逐字读对。
_ALL_IN_RE = re.compile(r"^(?:全下|全押|all[\s_-]*in)", re.IGNORECASE)


def is_all_in_badge(avatar_text: str | None, score: float = 0.0,
                    min_score: float = 0.4) -> bool | None:
    """头像 ROI 的读数是不是黄色的「All in」徽章。三态：True / False / None。"""
    t = (avatar_text or "").strip()
    if not t:
        return None
    if score < min_score:
        return None
    if _ALL_IN_RE.match(t):
        return True
    compact = re.sub(r"[\s_-]+", "", t).lower()
    if compact.startswith(("allin", "alin", "aiin", "ailn", "a1in", "a11in")):
        return True
    # 只对短文本做模糊匹配，避免把头像照片里读出的长句子误判成 All in
    return len(compact) <= 6 and difflib.SequenceMatcher(
        None, compact, "allin").ratio() >= 0.72


# 「人坐在桌边但这一手没被发牌」的头像文案有三种：正在带入 / 等待审核 / 等待中。
# 真机依据：`人在桌未入局.mp4`（864x1920）。hero 右手边第一位 lowR 的完整生命周期
# 都在这段视频里：1.2s 头像 `正在带入11`、筹码 100BB → 6s 起变成 `等待中`、
# 筹码 200BB（**筹码照常显示**，所以不能再用「筹码 0」当判据）。
#
# 读数有两条差异很大的路径，判据必须同时容得下：
#   · 头像区 **det+rec**（对没有手牌图标的座位才跑）读得几乎是原文：
#     `等待中` / `等待审核179` / `正在带入11`，分数 1.00；
#   · 纯 rec 只读到碎片：`#中`(0.68) / `#德中`(0.52) / `练待中`(0.54) ——
#     3 个字里常只对 1–2 个，整词匹配等于不识别。
# 所以按**命中的不同字数**判，并且只数汉字：det 会把旁边的计时数字一起框进来
# （`正在带入11`），数字不是证据。
_WAITING_PHRASES: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("buying_in", ("正", "在", "带", "入"), 3),   # 正在带入（4 字命中 3 个）
    ("waiting", ("等", "待", "审", "核"), 3),     # 等待审核（4 字命中 3 个）
    ("waiting", ("等", "待", "中"), 2),           # 等待中（3 字命中 2 个）
)
# 汉字数上限：只防头像照片里的长句噪声（实测 det 最长读到 `等待审核179`，汉字 4）。
_WAITING_MAX_LEN = 6
_WAITING_MIN_SCORE = 0.45


def waiting_badge(text: str | None, score: float = 0.0,
                  min_score: float = _WAITING_MIN_SCORE) -> str | None:
    """头像读数是不是「在座但没入局」的文案。

    返回 `"buying_in"`（正在带入）或 `"waiting"`（等待审核 / 等待中），
    读不到、分数不够、文本不像都返回 `None` —— 与 `is_fold_badge` 同一套三态
    语义：`None` 只表示「没法判断」，调用方必须允许它。

    中文短文本，命中即认（`带入` 是「正在带入」的稳定子串）。**不做整词匹配**：
    实测 `等待中` 会被读成 `练待中` / `#待中`，整词匹配等于不识别。
    """
    t = (text or "").strip()
    if not t or score < min_score:
        return None
    # 只留汉字：det 会把计时数字（`等待审核179`）、图标字样（`POKER`）一起框进来
    compact = "".join(ch for ch in t if "\u4e00" <= ch <= "\u9fff")
    if not compact or len(compact) > _WAITING_MAX_LEN:
        return None
    if "带入" in compact:
        return "buying_in"
    for kind, chars, need in _WAITING_PHRASES:
        if sum(1 for ch in chars if ch in compact) >= need:
            return kind
    return None


# 只认**整段**就是胜率文本：`30%`、`胜率 69%`。不能 `search`，否则
# 下注槽里的 HUD 文本（`入池36%`）会被当成全下胜率，把真正的下注乱码放过去。
_PERCENT_RE = re.compile(r"^\s*(?:胜率\s*)?(\d+(?:\.\d+)?)\s*%\s*$")


def parse_percent(text: str | None) -> float | None:
    """从 `30%` / `胜率 69%` 里取百分比。不是百分数返回 None。"""
    m = _PERCENT_RE.match(text or "")
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value if 0.0 <= value <= 100.0 else None


def parse_equity(text: str | None, *, allow_bare: bool = False) -> float | None:
    """胜率读数。`allow_bare=True` 时也接受纯数字（全下胜率槽的 `%` 常被 rec 吞掉）。"""
    value = parse_percent(text)
    if value is not None:
        return value
    if not allow_bare:
        return None
    m = re.fullmatch(r"\s*(\d{1,3}(?:\.\d+)?)\s*", text or "")
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value if 0.0 <= value <= 100.0 else None


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


# 「上一次那个名字」在整座暂时没证据后还要记多久（秒）。只用来判断
# 「重新出现的读数是不是同一个人的另一种写法」，不参与输出。
NAME_RECENT_KEEP = 30.0


class NameTracker:
    """每座名字的稳定器：新读法要**连续确认 N 帧**才改口。

    实测同一个座位会被读成 `YKY / YkY / YKkY / 東Yk丫`，而真正的换人
    （`Dragon333` → `咸蛋鸡柳饭`）是低频事件。所以策略是「先信旧名字，
    候选连续出现 confirm 次才换」。

    **2026-11-01 实盘教训：以前「相似」的读法被无条件丢弃，等于把一次错读
    锁成永久。** 21:22:51 两个座位同时被读错（`svsvbmbm`→`ssvhmbm`，
    `台风来咯咯`→`台风来吧多`），两者的 difflib 相似度分别是 0.667 / 0.600，
    都刚好过 `names_similar` 的 0.6 —— 于是之后 10 分钟、3700+ 帧的正确读数
    全被当成「同一个人的另一种写法」扔掉，直到进程重启才恢复。整手牌的
    名单、对手画像键、AI 上下文都吃的是那个错名字。

    现在「相似」不再有特权：**任何**与当前名字不同的读数都进候选计数，连读
    `confirm` 帧就改口（0.5s @6Hz）。相似度只用来决定两件事：
      · 整座重新出现时的「首读」要不要先确认（见 resolve 的 cur is None 分支）；
      · 改口/抖动时该记 `name_change`（换人）还是 `name_flicker`（同一人抖动）。
    """

    def __init__(self, confirm: int = 3, *,
                 recent_keep: float = NAME_RECENT_KEEP,
                 corrector: Callable[..., str | None] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.confirm = max(1, int(confirm))
        self.recent_keep = max(0.0, float(recent_keep))
        # 用时钟而不是帧计数：一帧会按座位调用 9 次，用次数算窗口会让
        # 「10 秒」变成「1 秒」，窗口长度还会随座位数漂。
        self._clock = clock or time.monotonic
        # 词表纠正（可选）：把读数映射到「已知玩家的规范拼写」。见 name_lexicon。
        self._corrector = corrector
        self._names: dict[str, str] = {}
        self._cand: dict[str, str] = {}
        self._count: dict[str, int] = {}
        # 座位 → 当前名字已经**逐字读对**的帧数。用来区分「确认过的名字」和
        # 「刚第一次读到的名字」—— 名单锁定/roster 修正只认前者。
        self._votes: dict[str, int] = {}
        # 座位 → (上一次的名字, 记下时的时刻)。absent() 清掉当前名字时保留，
        # 让座位重新出现时还能判断「像不像同一个人的另一次读法」。
        self._recent: dict[str, tuple[str, float]] = {}
        # 座位 → (旧名, 新名)：最近一次改口。调用方取走写进诊断。
        self._changes: dict[str, tuple[str | None, str]] = {}

    def __len__(self) -> int:
        return len(self._names)

    def current(self) -> dict[str, str]:
        return dict(self._names)

    def votes(self, seat_id: str) -> int:
        """当前名字逐字读对过多少帧（首读算 1）。"""
        return self._votes.get(seat_id, 0)

    def confirmed(self, seat_id: str) -> bool:
        """当前名字是否已经连续读对 confirm 帧 —— 名单修复只认确认过的。"""
        return seat_id in self._names and self.votes(seat_id) >= self.confirm

    def take_change(self, seat_id: str) -> tuple[str | None, str] | None:
        """取走最近一次改口（没有就返回 None）。给上层写诊断用。"""
        return self._changes.pop(seat_id, None)

    def set_corrector(self, corrector: Callable[..., str | None] | None) -> None:
        self._corrector = corrector

    def forget(self, seat_id: str) -> None:
        """**显式**清掉这个座位的名字（断流重连 / 换桌）。

        整座暂时没证据走 `absent()`：那个要保留「上一次的名字」，
        否则座位重新出现时又退化成「首读无条件采信」。
        """
        self._names.pop(seat_id, None)
        self._votes.pop(seat_id, None)
        self._recent.pop(seat_id, None)
        self._changes.pop(seat_id, None)
        self._clear(seat_id)

    def absent(self, seat_id: str) -> None:
        """整座这一帧没有「还有人」的证据：名字不再输出，但记着上一次的名字。"""
        cur = self._names.pop(seat_id, None)
        self._votes.pop(seat_id, None)
        if cur is not None:
            self._recent[seat_id] = (cur, self._clock())
        self._clear(seat_id)

    def resolve(self, seat_id: str, raw: str | None,
                action: str | None = None) -> str | None:
        cur = self._names.get(seat_id)
        if action:                       # 动作徽章盖住了名字：沿用旧名字
            return cur
        text = (raw or "").strip()
        if not text:
            return cur

        if self._corrector is not None:
            fixed = self._correct(seat_id, text)
            if fixed:
                text = fixed

        if cur is None:
            recent = self._recent_name(seat_id)
            # 座位刚重新出现、读数又「像」上一位的名字：先当候选。
            # 完全不像的（真换人）直接认，别让换人慢半秒。
            if recent is not None and recent != text and names_similar(recent, text):
                adopted = self._candidate(seat_id, text, None)
                return adopted
            self._commit(seat_id, text)
            return text

        if text == cur:
            self._votes[seat_id] = self._votes.get(seat_id, 1) + 1
            self._clear(seat_id)
            return cur

        # 与当前名字不同：**不再因为「相似」直接丢弃**，一律计票。
        adopted = self._candidate(seat_id, text, cur)
        return cur if adopted is None else adopted

    # ── 内部 ──
    def _correct(self, seat_id: str, text: str) -> str | None:
        """问一次词表。协议是 `corrector(text, seat_id)`；单参数的老写法也认。"""
        corrector = self._corrector
        if corrector is None:
            return None
        try:
            return corrector(text, seat_id)          # type: ignore[call-arg]
        except TypeError:
            try:
                return corrector(text)               # type: ignore[call-arg]
            except Exception:                        # 词表坏了不该带崩识别
                return None
        except Exception:
            return None

    def _candidate(self, seat_id: str, text: str, cur: str | None) -> str | None:
        """候选计数；连读 confirm 帧就改口，否则返回 None（保持当前名字）。"""
        if self._cand.get(seat_id) == text:
            self._count[seat_id] = self._count.get(seat_id, 0) + 1
        else:
            self._cand[seat_id], self._count[seat_id] = text, 1
        if self._count[seat_id] < self.confirm:
            return None
        self._commit(seat_id, text)
        return text

    def _commit(self, seat_id: str, text: str) -> None:
        old = self._names.get(seat_id)
        self._names[seat_id] = text
        self._votes[seat_id] = max(1, min(self.confirm, self._count.get(seat_id, 1)))
        self._recent[seat_id] = (text, self._clock())
        # 首读不算「改口」（old is None）：那是新出现的座位/新的一手，
        # 当成改口会让上层每手都报一次「名字修正」。
        if old is not None and old != text:
            self._changes[seat_id] = (old, text)
        self._clear(seat_id)

    def _recent_name(self, seat_id: str) -> str | None:
        item = self._recent.get(seat_id)
        if item is None:
            return None
        name, when = item
        if self._clock() - when > self.recent_keep:
            self._recent.pop(seat_id, None)
            return None
        return name

    def _clear(self, seat_id: str) -> None:
        self._cand.pop(seat_id, None)
        self._count.pop(seat_id, None)
