#!/usr/bin/env python3
"""`.env` 加载器的测试：解析、环境变量优先、ENV_FILE 显式指定。

    .venv/bin/python tests/test_env.py

全是纯逻辑，不读项目根的真实 .env（用例都传临时目录）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.env import (  # noqa: E402
    EnvFileError, load_env, locate_env_file, parse_env,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ─────────────────────────── 解析 ───────────────────────────

def test_parse_basic() -> None:
    got = parse_env(
        "# 注释\n"
        "\n"
        "AI_SERVER_API_KEY=sk-123\n"
        "  AI_SERVER_BASE_URL = https://api.deepseek.com  \n"
        "export AI_SERVER_MODEL=deepseek-flash\n"
        "QUOTED='a b c'\n"
        'DOUBLE="x=y"\n'
        "EMPTY=\n"
        "HASH=value#not-comment\n"
    )
    check(got["AI_SERVER_API_KEY"] == "sk-123", f"基础键值错：{got}")
    check(got["AI_SERVER_BASE_URL"] == "https://api.deepseek.com",
          f"值的空白应裁掉：{got}")
    check(got["AI_SERVER_MODEL"] == "deepseek-flash", f"export 前缀应支持：{got}")
    check(got["QUOTED"] == "a b c", f"单引号应剥掉：{got}")
    check(got["DOUBLE"] == "x=y", f"双引号应剥掉且只按第一个 = 切分：{got}")
    check(got["EMPTY"] == "", f"空值应允许：{got}")
    # 刻意不支持行内注释：# 是值的一部分
    check(got["HASH"] == "value#not-comment", f"不应猜行内注释：{got}")


def test_parse_rejects_malformed() -> None:
    for text, why in (("NO_EQUALS\n", "缺 ="),
                      ("1BAD=x\n", "变量名以数字开头"),
                      ("A B=x\n", "变量名含空格")):
        try:
            parse_env(text)
        except EnvFileError as exc:
            check("第 1 行" in str(exc), f"{why} 的报错应带行号：{exc}")
        else:
            FAILS.append(f"{why} 应抛 EnvFileError")


# ─────────────────────────── 加载 ───────────────────────────

def test_load_env_does_not_override_existing() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td) / ".env",
                     "AI_SERVER_API_KEY=from-file\n"
                     "AI_SERVER_BASE_URL=https://file.example\n"
                     "AI_SERVER_MODEL=file-model\n")
        env = {"AI_SERVER_API_KEY": "from-env"}
        loaded = load_env(candidates=(path,), env=env)

        # macOS 的临时目录是 /var → /private/var 的软链，命中的路径会被 resolve
        check(loaded.found and loaded.path == path.resolve(),
              f"应命中 .env：{loaded}")
        check(env["AI_SERVER_API_KEY"] == "from-env",
              f"已存在的环境变量不能被覆盖：{env}")
        check(env["AI_SERVER_BASE_URL"] == "https://file.example",
              f"缺失的键应从文件补齐：{env}")
        check(env["AI_SERVER_MODEL"] == "file-model", f"模型名应补齐：{env}")
        check(loaded.kept == ("AI_SERVER_API_KEY",),
              f"应记录被保留的键：{loaded.kept}")
        check(set(loaded.applied) == {"AI_SERVER_BASE_URL", "AI_SERVER_MODEL"},
              f"应记录从文件生效的键：{loaded.applied}")


def test_load_env_missing_is_not_an_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / ".env"
        loaded = load_env(candidates=(missing,), env={})
        check(not loaded.found and loaded.path is None, f"没有 .env 应返回未命中：{loaded}")
        check(loaded.tried == (missing,), f"应列出找过的路径：{loaded.tried}")


def test_env_file_override_wins_and_fails_loudly() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        chosen = write(td / "custom.env", "A=1\n")
        other = write(td / ".env", "A=2\n")

        env = {"ENV_FILE": str(chosen)}
        path, tried = locate_env_file(candidates=(other,), env=env)
        check(path == chosen.resolve(), f"ENV_FILE 应优先于候选列表：{path}")
        check(tried == (chosen,), f"显式指定时只应尝试它：{tried}")

        env = {"ENV_FILE": str(td / "nope.env")}
        try:
            locate_env_file(candidates=(other,), env=env)
        except EnvFileError as exc:
            check("读不到" in str(exc), f"报错应说清读不到：{exc}")
        else:
            FAILS.append("ENV_FILE 指向不存在的文件应抛 EnvFileError")


def test_candidates_use_first_existing() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        first = write(td / "first.env", "A=1\n")
        second = write(td / "second.env", "A=2\n")
        path, tried = locate_env_file(candidates=(first, second), env={})
        check(path == first.resolve(), f"应命中第一个存在的文件：{path}")
        check(tried == (first, second), f"应原样返回候选顺序：{tried}")


def main() -> int:
    tests = [
        test_parse_basic,
        test_parse_rejects_malformed,
        test_load_env_does_not_override_existing,
        test_load_env_missing_is_not_an_error,
        test_env_file_override_wins_and_fails_loudly,
        test_candidates_use_first_existing,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ .env 加载器全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
