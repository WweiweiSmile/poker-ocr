"""名字词表：用「已经认识的人」的拼法纠正当前读数。

## 为什么需要它

实测（`logs/incidents/20261101_2134*/frame.jpg`，生产 ROI 路径）：

    svsvbmbm    0.930
    台风来咯咯   0.869      ← 9 个座位里最低
    其余 7 席          0.986–0.998

错读 `ssvhmbm` / `台风来吧多` 落在同一个分数段里 —— RapidOCR 的 rec score 是
「这段像不像字」，不是「这个字对不对」（同样的教训见 fields.read_name 的
`稚气未酒 → 稚气未` 0.998）。所以**靠置信度阈值分不开对错**，只会先把
`0.869` 那个读对的判成不可用。

能分开它们的是**历史**：`台风来咯咯` 在画像库里已经有 68 手，而
`台风来吧多` 是一个从没见过的拼法。一个 5 字名字只错 2 个字、前缀完全一致，
几乎只可能是同一个人的另一次读法。

## 两级判据（宁可少改，不要改错）

· **自动改**：命中规范拼写，且
  - 相似度 ≥ `auto`（默认 0.80，几乎只差一个字符），或
  - 相似度 ≥ `prefix_floor`（0.60）**且公共前缀 ≥ 3 字**、名字 ≥ 5 字 ——
    实测的形态就是「前缀对、尾巴被读坏」（`台风来咯咯` → `台风来吧多`）。
    5 字名才用这条：4 字名只差 1 个字就是 0.75，`俊伯老公/俊伯老婆` 这种真正
    的两个人也会被并掉，而 5 字名的 3 字前缀冗余度高得多。
· **只告警**：相似度 ≥ `suspect`（0.62）但没到自动改的门槛。写进
  `name_warnings`，让人去画像库里加别名，而不是替人做主。

两条都要求「领先第二名」≥ `margin`，否则说明它同时像好几个人，宁可不动。

注意：词表里的键是**已知玩家的拼法**。如果两个拼法都已经入库（例如这次的
`台风来咯咯` 68 手 / `台风来吧多` 16 手），两边都是 exact match，词表不会
替你合并 —— 那种历史分裂要用 `tools/name_alias.py` 显式加别名。
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping

# 名字里允许的噪声：首尾空格、全角字符、连续空白。NFKC 之后全角变半角。
MAX_NAME_LEN = 24


def normalize_name(name: str | None) -> str | None:
    """展示用的归一化：NFKC（全角→半角）、去首尾空格、连续空白压成一个。

    大小写**不在这里改**：输出要保留原样，只有比较键（`_compare_key`）才小写。
    """
    if name is None:
        return None
    text = unicodedata.normalize("NFKC", str(name)).strip()
    text = " ".join(text.split())
    if not text:
        return None
    return text


def _compare_key(name: str) -> str:
    """真正做比较的形式：归一化 + 去掉所有空白 + 小写。

    `多读书无数` 和 `多读书 无数` 该算同一个；大小写在英文 ID 里也不该有
    区分度（实测 `sVsybmbm` / `svsvbmbm` 就是同一个人的两帧读数）。
    """
    normalized = normalize_name(name) or ""
    return normalized.replace(" ", "").lower()


def shared_prefix(a: str, b: str) -> int:
    """公共前缀长度（按比较键算）。"""
    ka, kb = _compare_key(a), _compare_key(b)
    n = 0
    for ca, cb in zip(ka, kb):
        if ca != cb:
            break
        n += 1
    return n


def duplicate_pairs(names: Iterable[str], *,
                    threshold: float = 0.6) -> list[tuple[float, str, str]]:
    """词表里互相很像的名字对（相似度降序）。

    用于两件事：启动时提示「这两条画像可能是同一个人」，以及
    `tools/name_alias.py --suspects`。**只报告，不合并** —— 运行时没有任何
    依据能判断 `俊伯老公`/`俊伯老婆` 到底是不是两个人，合并必须由人来定。
    """
    items = [n for n in dict.fromkeys(names) if n]
    out: list[tuple[float, str, str]] = []
    for i, a in enumerate(items):
        ka = _compare_key(a)
        for b in items[i + 1:]:
            kb = _compare_key(b)
            if ka == kb:
                continue
            ratio = difflib.SequenceMatcher(None, ka, kb).ratio()
            if ratio >= threshold:
                out.append((ratio, a, b))
    out.sort(reverse=True)
    return out


@dataclass(frozen=True)
class NameMatch:
    """命中结果。

    · `text`：规范拼写（要写进输出的那个）
    · `key`：画像库里的键（一般和 text 相同）
    · `score`：difflib 相似度（exact 为 1.0）
    · `kind`：exact / alias / similar / similar-prefix
    · `auto`：是否可以**直接改**；False 表示只用来告警
    """

    text: str
    key: str
    score: float
    kind: str
    auto: bool


class NameLexicon:
    """名字 → 规范拼写。线程安全与否无所谓：只在 OCR 线程里按座位调用。"""

    def __init__(self, names: Iterable[str] = (),
                 aliases: Mapping[str, str] | None = None, *,
                 auto: float = 0.80,
                 prefix_floor: float = 0.60,
                 prefix_min: int = 3,
                 prefix_name_len: int = 5,
                 suspect: float = 0.62,
                 margin: float = 0.08,
                 max_len_diff: int = 3) -> None:
        # `_canon`：比较键 → 展示拼写（输出用原来的样子）
        self._canon: dict[str, str] = {}
        # `_aliases`：比较键 → 比较键（人工/自动学到的「这其实是同一个人」）
        self._aliases: dict[str, str] = {}
        # 读数 → 结论。名字槽每秒只重读几次，但同一串文本会反复出现；
        # key 里带上 exclude 集合，否则「这桌有人叫这个名字」的判定会被串味。
        self._memo: dict[tuple, NameMatch | None] = {}
        self.auto = float(auto)
        self.prefix_floor = float(prefix_floor)
        self.prefix_min = max(1, int(prefix_min))
        self.prefix_name_len = max(1, int(prefix_name_len))
        self.suspect = float(suspect)
        self.margin = float(margin)
        self.max_len_diff = max(0, int(max_len_diff))
        self.update(names, aliases)

    # ── 维护 ──
    def update(self, names: Iterable[str] = (),
               aliases: Mapping[str, str] | None = None) -> None:
        """增补词表。已存在的拼写不覆盖（先入为主 = 先认识的算规范）。"""
        for name in names:
            key = _compare_key(name)
            display = normalize_name(name)
            if not key or not display or len(display) > MAX_NAME_LEN:
                continue
            self._canon.setdefault(key, display)
        for alias, target in (aliases or {}).items():
            ka, kt = _compare_key(alias), _compare_key(target)
            if ka and kt and ka != kt:
                self._aliases[ka] = kt
        self._memo.clear()

    def remember(self, name: str | None) -> None:
        """把本场**确认过**的新名字加进词表。

        只收「跟已知拼法都不像」的名字：只要它能对上某个已知名字（哪怕只是
        存疑档），就不入表 —— 否则一次错读会变成新的规范拼写，之后每次都
        「纠正」到错的那个上。已存在的拼写不动（先入为主）。
        """
        key = _compare_key(name)
        if not key or key in self._canon:
            return
        if self._match(key) is not None:
            return
        self.update([name])

    def __len__(self) -> int:
        return len(self._canon)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and _compare_key(name) in self._canon

    # ── 查询 ──
    def canonical(self, raw: str | None,
                  exclude: Iterable[str] = ()) -> str | None:
        """可以直接采用的规范拼写；没有把握时返回 None（调用方保留原文）。

        `exclude` 是「这张桌上别的座位已经在用的名字」：同一个名字不可能同时
        出现在两个座位上，所以候选拼写被别人占着时绝不能把读到的人算成他
        —— 这条是为了不把 `俊伯老婆` 并进 `俊伯老公`（相似度 0.75，正好落在
        前缀规则里）。
        """
        match = self.match(raw, exclude)
        return match.text if match is not None and match.auto else None

    def __call__(self, raw: str | None,
                 seat_id: str | None = None,
                 exclude: Iterable[str] = ()) -> str | None:
        """当 corrector 传给 NameTracker 用（`seat_id` 只为签名对齐，不参与判定）。"""
        return self.canonical(raw, exclude)

    def match(self, raw: str | None,
              exclude: Iterable[str] = ()) -> NameMatch | None:
        key = _compare_key(raw)
        if not key:
            return None
        excluded = frozenset(_compare_key(n) or "" for n in exclude)
        memo_key = (key, frozenset(e for e in excluded if e))
        if memo_key in self._memo:
            return self._memo[memo_key]
        match = self._match(key, excluded)
        self._memo[memo_key] = match
        return match

    # ── 内部 ──
    def _match(self, key: str, exclude: frozenset[str] = frozenset()) -> NameMatch | None:
        # 1) exact：本身就是已知拼法。**不覆盖**，这是「别把已知玩家改错」的底线。
        if key in self._canon:
            return NameMatch(self._canon[key], self._canon[key], 1.0, "exact", True)

        # 2) alias：人工/历史写过的等价关系
        target = self._aliases.get(key)
        if target and target in self._canon:
            text = self._canon[target]
            return NameMatch(text, text, 1.0, "alias", True)

        # 3) 模糊：找最像的已知拼法
        best: tuple[float, str] | None = None
        second = 0.0
        for cand_key, display in self._canon.items():
            if cand_key in exclude:
                continue                     # 这张桌上已经有人叫这个名字了
            if abs(len(cand_key) - len(key)) > self.max_len_diff:
                continue
            ratio = difflib.SequenceMatcher(None, key, cand_key).ratio()
            if best is None or ratio > best[0]:
                if best is not None:
                    second = max(second, best[0])
                best = (ratio, display)
            else:
                second = max(second, ratio)
        if best is None:
            return None

        ratio, display = best
        if ratio - second < self.margin and ratio < 1.0:
            return None                       # 同时像好几个人：不动

        prefix = shared_prefix(key, display)
        long_enough = min(len(key), len(_compare_key(display))) >= self.prefix_name_len
        if ratio >= self.auto:
            return NameMatch(display, display, ratio, "similar", True)
        if (ratio >= self.prefix_floor and long_enough
                and prefix >= self.prefix_min):
            return NameMatch(display, display, ratio, "similar-prefix", True)
        if ratio >= self.suspect:
            return NameMatch(display, display, ratio, "similar-suspect", False)
        return None
