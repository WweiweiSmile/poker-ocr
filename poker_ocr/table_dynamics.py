"""牌桌动态（`.table_dy`）：人工维护的「当前桌子大概是什么样」，注入 AI 提示词。

文件默认放在项目根目录，纯文本。以 `#` 开头的行是注释，空行忽略；其余行原样
拼成一段描述，例如：

    # 当前桌子：新手多，跟注站
    新手玩家，比较松弱，诈唬很少。

**只写桌级**：这里不写玩家名字，也不写单个玩家的风格。每个对手的类型
由画像系统按座位统计（见 `opponent_profile.py`），人工覆盖用 pin；本文件
只描述这张桌子整体的松紧、多路程度、抽注结构和默认剥削方向。换桌后
直接重写，不需要维护任何名单。

设计口径与 `.env` 类似：

  · **文件不存在是合法状态** —— 没有动态时按原来的「未知对手」标准线跑，
    不影响启动，也不改变任何已有行为。
  · **`TABLE_DY_FILE` 可以显式指定路径**；默认只找项目根目录，不按 CWD 猜。
  · **每次决策点重新读**（按 mtime+size 缓存）。换桌/换对手后直接改文件，
    下一个决策点就生效，不必重启 worker。

这里只负责「读文本」，怎么把「松弱/诈唬少」翻译成下注尺度，是
提示词里交给模型的事；确定性的 `coach.py` 不读这个文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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
