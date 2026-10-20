"""最小 `.env` 加载器（不引依赖）。

与 call-back 的 `config.LoadConfig` 保持同一套口径：

  · **已存在的环境变量优先**。`.env` 只补齐没设过的键，绝不覆盖 ——
    这样「临时 export 一个 Key 覆盖文件里的值」是可行的，也和 godotenv.Load 一致。
  · **`ENV_FILE` 可以显式指定路径**，指定了却读不到就报错，不静默退回候选列表。
    部署时进程的 CWD 未必是项目目录，靠 CWD 找 `.env` 是最常见的「配了却没生效」。
  · 没找到 `.env` 是合法状态（全靠环境变量），由调用方决定要不要告警。

刻意不支持的写法：变量展开（`$OTHER`）、多行值、行内注释。
配置项就是几个 Key/URL/模型名，支持这些只会让解析规则变得含糊。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_FILE_VAR = "ENV_FILE"

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(RuntimeError):
    """`.env` 读不到或格式不对。宁可启动失败，也不要带着半份配置跑起来。"""


@dataclass(frozen=True)
class EnvLoad:
    """一次加载的结果。只记录**键名**，不记录值 —— 日志里不该出现 Key。"""

    path: Path | None
    tried: tuple[Path, ...]
    applied: tuple[str, ...]          # 从文件写进环境变量的键
    kept: tuple[str, ...]             # 文件里有、但环境变量已存在所以没覆盖的键

    @property
    def found(self) -> bool:
        return self.path is not None


def parse_env(text: str) -> dict[str, str]:
    """解析 `.env` 文本。空行和 `#` 注释跳过，允许 `export ` 前缀与成对引号。"""
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise EnvFileError(f"第 {lineno} 行不是 KEY=VALUE：{raw!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.match(key):
            raise EnvFileError(f"第 {lineno} 行的变量名不合法：{key!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def _candidates() -> tuple[Path, ...]:
    """候选 `.env`：项目根目录优先，CWD 兜底。

    项目根目录排在前面：不管从哪里启动 `python -m poker_ocr.live`，
    应用的家都是仓库根，而 CWD 只是启动方式的副产物。
    """
    return (PROJECT_ROOT / ".env", Path(".env").resolve())


def locate_env_file(candidates: tuple[Path, ...] | None = None,
                    env: dict[str, str] | None = None) -> tuple[Path | None,
                                                               tuple[Path, ...]]:
    """定位 `.env`。返回 (命中的路径或 None, 尝试过的候选)。

    `ENV_FILE` 显式指定时**只认它**；读不到直接抛错（不退回候选列表）。
    """
    env = os.environ if env is None else env
    override = (env.get(ENV_FILE_VAR) or "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise EnvFileError(f"{ENV_FILE_VAR} 指向的 {path} 读不到")
        return path.resolve(), (path,)

    tried = candidates if candidates is not None else _candidates()
    for path in tried:
        if path.is_file():
            # 命中时 resolve：软链路径（macOS 的 /var → /private/var）在日志里
            # 会显示成两个不同路径，排查时容易以为是两个文件
            return path.resolve(), tried
    return None, tried


def load_env(candidates: tuple[Path, ...] | None = None,
             env: dict[str, str] | None = None) -> EnvLoad:
    """找到 `.env` 并补齐环境变量。没找到返回 `found=False`，不抛错。"""
    env = os.environ if env is None else env
    path, tried = locate_env_file(candidates, env)
    if path is None:
        return EnvLoad(None, tried, (), ())

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvFileError(f"读取 {path} 失败：{exc}") from exc

    values = parse_env(text)
    applied: list[str] = []
    kept: list[str] = []
    for key, value in values.items():
        if key in env:
            kept.append(key)
        else:
            env[key] = value
            applied.append(key)
    return EnvLoad(path, tried, tuple(applied), tuple(kept))
