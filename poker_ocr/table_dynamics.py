"""牌桌动态（`.table_dy`）：人工维护的「当前桌子大概是什么样」，注入 AI 提示词。

文件默认放在项目根目录，纯文本。以 `#` 开头的行是注释，空行忽略；其余行原样
拼成一段描述，例如：

    # 当前桌子：新手多，跟注站
    新手玩家，比较松弱，诈唬很少。

**按座位匹配**：桌面上的名单每次入桌都可能换人，所以这里不做持久化的
「昵称 → 座位」映射。每次决策时拿当前座位的 OCR 名字，去和笔记段落里的
名字做归一化 + 模糊匹配（全角/半角、空格、大小写、OCR 少一两个字都能对上），
由 `advice.render_table_notes()` 把「这个座位对应哪段笔记」写进事实块；
**名单里没有的人明确标成 unknown**，不套用整桌的默认标签。

匹配单位是**段落**（空行分隔）；一行一个玩家的写法（`名字: 描述`）会自动
按行拆开。所以换桌后直接改文件即可，不需要维护任何映射表。

设计口径与 `.env` 类似：

  · **文件不存在是合法状态** —— 没有动态时按原来的「未知对手」标准线跑，
    不影响启动，也不改变任何已有行为。
  · **`TABLE_DY_FILE` 可以显式指定路径**；默认只找项目根目录，不按 CWD 猜。
  · **每次决策点重新读**（按 mtime+size 缓存）。换桌/换对手后直接改文件，
    下一个决策点就生效，不必重启 worker。

这里只负责「读文本 + 匹配名字」，怎么把「松弱/诈唬少」翻译成下注尺度，是
提示词里交给模型的事；确定性的 `coach.py` 不读这个文件。
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .env import PROJECT_ROOT

TABLE_DY_NAME = ".table_dy"
TABLE_DY_VAR = "TABLE_DY_FILE"

# 提示词预算：这张表是给人写随手笔记的，正常十几行就够。设个上限，
# 避免误把一整篇复盘粘进来把每次决策的提示词撑爆。
MAX_CHARS = 2000

# 桌风关键词。只用于把自由文本归成几个**保守**的布尔倾向，不代替模型理解：
# 命中「松弱/紧弱/很少诈唬/尊重加注」时，说明对手的多街 aggression 更可能是
# 价值，边际成牌不该再按「未知对手」的默认线去抓诈。
_VALUE_HEAVY_WORDS = (
    "松弱", "紧弱", "跟注站", "新手", "顶对以上",
    "诈唬很少", "很少诈唬", "不诈唬", "几乎不诈唬", "诈唬少",
    "尊重", "连续下注", "持续下注", "多街",
    "loose-passive", "loose passive", "calling station", "tight-passive",
    "novice", "nit", "rarely bluffs", "few bluffs", "never bluffs",
    "respect", "barrels", "continued bets", "multi-street",
)
# 明确「爱诈唬」的描述不能误判成价值线；但如果同一段又写了「很少诈唬」，
# 以更具体的「很少诈唬」为准（用户可能是在区分不同对手）。
_AGGRESSIVE_WORDS = ("松凶", "爱诈唬", "经常诈唬", "很多诈唬", "maniac",
                     "loose-aggressive", "bluffs a lot")
_RARELY_BLUFFS_WORDS = ("诈唬很少", "很少诈唬", "不诈唬", "rarely bluffs",
                        "never bluffs")


# 「松」桌的判断只用于开池尺度：松桌跟注多，开池要放大收价值；
# 紧桌不在这里放大（紧弱/尊重加注不是「松」）。
_LOOSE_WORDS = (
    "松弱", "松凶", "偏松", "很松", "松的", "松散", "跟注站",
    "loose-passive", "loose passive", "loose-aggressive", "loose aggressive",
    "calling station", "新手", "鱼",
)


def is_loose_table(text: str | None) -> bool:
    """桌风是否明确是「松」桌（松弱/松凶/跟注站/新手鱼）。

    影响两处：翻前开池尺度（`advice.build_options` 的 `open_bonus`），以及
    `preflop_ranges.range_verdict` 对「默认线」的解释（松桌允许更宽、
    小对子可以 over-limp）。没有桌风、或写的是紧弱/尊重加注，都不算松。
    """
    t = (text or "").lower()
    return bool(t) and any(w in t for w in _LOOSE_WORDS)


def value_heavy_aggression(text: str | None) -> bool:
    """桌风是否明确说「对手的加注/连续下注以价值为主」。

    这是给 `advice.value_directive()` 用的保守判据：只有用户写清楚了
    松弱/紧弱/很少诈唬/尊重持续下注中的任意一类，才把边际成牌从「跟注」
    改成「弃牌」。没有桌风、或写的是松凶/爱诈唬，都不触发。
    """
    t = (text or "").lower()
    if not t:
        return False
    if any(w in t for w in _AGGRESSIVE_WORDS) and not any(
            w in t for w in _RARELY_BLUFFS_WORDS):
        return False
    return any(w.lower() in t for w in _VALUE_HEAVY_WORDS)


def is_aggressive_table(text: str | None) -> bool:
    """桌风里是否有明确的「松浪/松凶」玩家。

    与 `value_heavy_aggression()` 的区别：混合桌可以**同时**有松弱和松浪，
    前者返回 False（不能假设全桌价值线），但后者仍返回 True——只要桌上
    有松浪，`value_directive` 就不该对中等成牌默认做薄价值。

    只看显式标签，不靠「爱诈唬」这类泛化词：避免把「松凶但很少诈唬」
    误判成松浪。宁可漏判，也不要把被动桌当成激进桌。
    """
    t = (text or "").lower()
    if not t:
        return False
    # 「松浪」是明确的爱开火标签，出现即算；「松凶」可能被用户补一句
    # 「但很少诈唬」，那就不能按松浪处理。
    if any(w in t for w in ("松浪", "maniac", "loose-aggressive",
                            "loose aggressive", "spewy")):
        return True
    if "松凶" in t:
        return not any(w in t for w in _RARELY_BLUFFS_WORDS)
    return False


# ─────────────── 昵称 → 笔记段落：每次决策按当前座位名匹配 ───────────────
#
# 不做持久化的「昵称 → 座位」映射：每次入桌的人都可能不同，映射表一定会过期。
# 这里只做无状态的文本匹配 —— 当前座位叫什么名字，就去笔记里找提到这个名字
# 的段落；找不到就是 unknown，绝不把整桌标签套到一个陌生人身上。

# 匹配分数门槛。0.8 能容纳 OCR 少一两个字，又不至于把两个无关的短名字拼上。
NAME_MATCH_THRESHOLD = 0.8

# 归一化 / 分词共用：中英文标点、括号、斜杠、空白全部当分隔符。
_SEP_RE = re.compile(
    r"[\s\u3000·・,，。.、;；:：!！?？'\"“”‘’()（）\[\]【】<>《》"
    r"_\-—–~～/／\\|｜]+")


def normalize_player_name(name: str | None) -> str:
    """名字归一化：全角→半角、大小写、去空格/标点/符号。

    OCR 经常把全角括号、空格、emoji 混进名字；笔记是人手写的，两边写法
    不一致时**必须在比较前消掉这些差异**，否则 3 个字符的差别就匹配不上。
    """
    text = unicodedata.normalize("NFKC", name or "").casefold()
    return _SEP_RE.sub("", text)


# 段内的分组标记：`A. ` / `B) ` / `1、` / `三. `。只在句末或行首出现时才算，
# 避免把「6.5BB」这种数字拆开。
_LIST_MARK_RE = re.compile(
    r"(?:(?<=[。；;！!？?\n：:])|^)\s*"
    r"(?=(?:[A-Za-z]|[0-9]+|[一二三四五六七八九十]+)[.、)）](?=\s|$))",
    re.MULTILINE)


def split_table_notes(text: str | None) -> list[str]:
    """把桌风拆成「一段 = 一个匹配单位」。

    空行分段（一组玩家写一段）；同一段里出现 `A.` / `B.` 这类分组标记时
    再按标记拆；如果某段是多行且没有项目符号（`名字: 描述` 一行一个玩家
    的写法），按行再拆。三种写法都支持。
    """
    blocks: list[str] = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        para = para.strip()
        if not para:
            continue
        parts = [part.strip() for part in _LIST_MARK_RE.split(para)
                 if part.strip()]
        if len(parts) > 1:
            blocks.extend(parts)
            continue
        lines = [line.strip() for line in para.splitlines() if line.strip()]
        if len(lines) > 1 and not any(line[0] in "-*·•" for line in lines):
            blocks.extend(lines)
        else:
            blocks.append(para)
    return blocks


_GROUP_HEADER_RE = re.compile(
    r"^[A-Za-z0-9一二三四五六七八九十]+[.、)）]\s*(.*)$")


def named_players(text: str | None) -> set[str]:
    """笔记里以分组标题（`A. ...（名字、名字）：`）列出的名字，归一化后返回。

    只用于判断「一段笔记是不是在说具体的人」，不参与类型分类：像
    `C. 混合：...（伯克利丁真、阿坝干嘛）：` 这种没有统一类型标签的分组
    也要能识别出来，否则它会被当成「桌级背景」重复注入。
    """
    names: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _GROUP_HEADER_RE.match(line):
            continue
        for group in re.findall(r"[（(]([^）)]*)[）)]", line):
            for token in re.split(r"[、,，/|]+", group):
                normalized = normalize_player_name(token)
                if len(normalized) >= 2:
                    names.add(normalized)
    return names


@dataclass(frozen=True)
class NoteMatch:
    """一个座位的名字匹配到的笔记段落。"""

    block: str
    matched: str          # 段落里被匹配到的那个名字（原文写法）
    score: float


def _name_score(name: str, token: str) -> float:
    """归一化后的两个写法有多像。"""
    if name == token:
        return 1.0
    short, long_ = (name, token) if len(name) <= len(token) else (token, name)
    if len(short) >= 3 and short in long_:
        return 0.92
    # OCR 常把长名字截断（「八六」对「八六上青山」）：前缀相同且只差 1-2 字。
    if (len(short) >= 2 and long_.startswith(short)
            and len(long_) - len(short) <= 2):
        return 0.85
    return SequenceMatcher(None, name, token).ratio()


@lru_cache(maxsize=64)
def _match_table_notes_cached(text: str,
                              names: tuple[str, ...]) -> dict[str, NoteMatch]:
    blocks = split_table_notes(text)
    if not blocks:
        return {}
    # 每段的候选名字只切一次：决策点可能每街都来，别为每个名字重切全文。
    words_by_block: list[tuple[str, list[str]]] = []
    for block in blocks:
        words = [w for w in _SEP_RE.split(block)
                 if len(normalize_player_name(w)) >= 2]
        words_by_block.append((block, words))

    out: dict[str, NoteMatch] = {}
    for raw in names:
        normalized = normalize_player_name(raw)
        if len(normalized) < 2:
            continue
        best: NoteMatch | None = None
        for block, words in words_by_block:
            for word in words:
                score = _name_score(normalized, normalize_player_name(word))
                if best is None or score > best.score:
                    best = NoteMatch(block, word, score)
        if best is not None and best.score >= NAME_MATCH_THRESHOLD:
            out[raw] = best
    return out


def match_table_notes(text: str | None,
                      names: Iterable[str | None]) -> dict[str, NoteMatch]:
    """把当前在座的 OCR 名字匹配到笔记段落。

    返回 `{原始名字: NoteMatch}`；**没匹配上的名字不会出现在结果里**，
    调用方据此把它们显式标成 unknown（换桌后名单里没有的人不能套整桌标签）。

    结果按（文本, 名字集合）缓存：同一桌、同一手牌会被渲染/价值指令
    调用两三次，而桌风文本只在换桌时变。
    """
    key = tuple(sorted({name for name in names if name}))
    if not key:
        return {}
    # 返回副本：调用方不该被缓存里的 dict 串改
    return dict(_match_table_notes_cached(text or "", key))


@dataclass(frozen=True)
class TableDynamics:
    """一次读取的结果。`text` 为空表示没有可用的动态。"""

    text: str = ""
    path: Path | None = None
    error: str | None = None
    # 文件是否存在（存在但只有注释时 text 为空、found 为 False）
    exists: bool = False

    @property
    def found(self) -> bool:
        return bool(self.text)


# path → (mtime_ns, size, parsed_text)。文件很小，但决策点可能每手/每街都来，
# 按 mtime+size 缓存可以避免每次都重新解析；编辑文件后 mtime 变化会自动失效。
_CACHE: dict[Path, tuple[int, int, str]] = {}


def parse_table_dynamics(text: str, *, max_chars: int = MAX_CHARS) -> str:
    """去掉整行注释和空行，保留其余内容，并做长度上限。

    只认**整行**注释：行内的 `#` 可能是牌面/文字的一部分，不擅自截断。
    """
    lines: list[str] = []
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    out = "\n".join(lines)
    if max_chars > 0 and len(out) > max_chars:
        out = out[:max_chars].rstrip() + "…"
    return out


def locate_table_dynamics(path: str | Path | None = None,
                          env: dict[str, str] | None = None) -> Path:
    """定位 `.table_dy`。显式 `path` > `TABLE_DY_FILE` > 项目根目录。"""
    if path is not None:
        return Path(path).expanduser().resolve()
    env = os.environ if env is None else env
    override = (env.get(TABLE_DY_VAR) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (PROJECT_ROOT / TABLE_DY_NAME).resolve()


def load_table_dynamics(path: str | Path | None = None,
                        env: dict[str, str] | None = None) -> TableDynamics:
    """读取并解析 `.table_dy`。不存在 → 空动态；读/解析失败 → `error` 非空。

    默认路径不存在是合法的（没有动态）；但显式传 `path` 或 `TABLE_DY_FILE`
    却读不到，会在 `error` 里说明 —— 避免「配了却没生效」静默通过。
    """
    env_map = os.environ if env is None else env
    explicit = path is not None or bool((env_map.get(TABLE_DY_VAR) or "").strip())
    file_path = locate_table_dynamics(path, env_map)
    try:
        stat = file_path.stat()
    except FileNotFoundError:
        _CACHE.pop(file_path, None)
        return TableDynamics(path=file_path,
                             error=f"{file_path} 不存在" if explicit else None)
    except OSError as exc:
        return TableDynamics(path=file_path, error=f"读取 {file_path} 失败：{exc}")

    key = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(file_path)
    if cached is not None and cached[:2] == key:
        return TableDynamics(cached[2], file_path, exists=True)

    try:
        # utf-8-sig：Windows 记事本保存的文件可能带 BOM
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return TableDynamics(path=file_path, error=f"读取 {file_path} 失败：{exc}")
    except UnicodeDecodeError as exc:
        return TableDynamics(path=file_path,
                             error=f"{file_path} 不是 UTF-8 文本：{exc}")

    parsed = parse_table_dynamics(text)
    _CACHE[file_path] = (stat.st_mtime_ns, stat.st_size, parsed)
    return TableDynamics(parsed, file_path, exists=True)
