"""运行配置。

控制台是一个独立项目，但复用 poker-ocr 的重跑引擎（`poker_ocr.advice`）。
配置按下面的优先级合并，**已存在的环境变量永远优先**：

1. 进程环境变量
2. `replay-console/.env`（本项目的本地配置）
3. `poker-ocr/.env`（AI_SERVER_* 凭据与采样参数）
4. `call-back/.env`（DB_*；设计文档要求复用同一个 MySQL 实例）

第 4 项是内部工具最省事的默认值。生产/CI 可以用 `REPLAY_SKIP_CALLBACK_ENV=1`
关掉它，或者用 `REPLAY_CALLBACK_ENV` 指向另一份文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# replay-console/replay/config.py -> replay-console/
PROJECT_DIR = Path(__file__).resolve().parent.parent
# poker-ocr/
REPO_ROOT = PROJECT_DIR.parent

# 允许直接 `python replay_api.py` 时也能 import poker_ocr
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from poker_ocr.env import parse_env  # noqa: E402


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int, low: int | None = None,
         high: int | None = None) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        out = int(value)
    except ValueError:
        return default
    if low is not None:
        out = max(low, out)
    if high is not None:
        out = min(high, out)
    return out


def _judge_timeout(value: str | None) -> float:
    """REPLAY_JUDGE_TIMEOUT：配错直接炸，不静默退回默认。"""
    if value is None or not value.strip():
        return 30.0
    try:
        timeout = float(value)
    except ValueError:
        raise ValueError(f"REPLAY_JUDGE_TIMEOUT 不是数字：{value!r}") from None
    if timeout <= 0:
        raise ValueError("REPLAY_JUDGE_TIMEOUT 必须大于 0")
    return timeout


def _float(value: str | None, default: float | None) -> float | None:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _price_table(raw: str | None) -> dict[str, dict[str, float]]:
    """`REPLAY_PRICE_TABLE` → {model: {"in": 元/百万 token, "out": ...}}。

    配错就直接不启动（fail fast）：成本估算宁可报错，也不要静默显示「未知」
    让人以为没配过。空值合法，表示没配单价。
    """
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"REPLAY_PRICE_TABLE 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("REPLAY_PRICE_TABLE 必须是 {model: {in, out}} 对象")
    table: dict[str, dict[str, float]] = {}
    for model, prices in parsed.items():
        if not isinstance(prices, dict):
            raise ValueError(
                f"REPLAY_PRICE_TABLE[{model!r}] 必须是 {{in, out}}")
        entry: dict[str, float] = {}
        for key in ("in", "out"):
            try:
                value = float(prices.get(key))
            except (TypeError, ValueError):
                raise ValueError(
                    f"REPLAY_PRICE_TABLE[{model!r}].{key} 必须是数字") from None
            if value < 0:
                raise ValueError(
                    f"REPLAY_PRICE_TABLE[{model!r}].{key} 不能为负")
            entry[key] = value
        table[str(model)] = entry
    return table


def _load_env_file(path: Path, applied: list[Path]) -> None:
    """把文件里的键补进环境变量。文件不存在是合法状态。"""
    if not path.is_file():
        return
    try:
        values = parse_env(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
    applied.append(path)


def load_environment() -> list[Path]:
    """加载分层 .env，返回实际读到的文件（只用于启动日志）。"""
    applied: list[Path] = []
    explicit = (os.environ.get("REPLAY_ENV_FILE") or "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    else:
        candidates.append(PROJECT_DIR / ".env")
        candidates.append(REPO_ROOT / ".env")
        if not _truthy(os.environ.get("REPLAY_SKIP_CALLBACK_ENV")):
            callback = (os.environ.get("REPLAY_CALLBACK_ENV") or "").strip()
            candidates.append(
                Path(callback).expanduser() if callback
                else REPO_ROOT.parent / "call-back" / ".env")
    for path in candidates:
        _load_env_file(path, applied)
    return applied


def git_state(repo: Path = REPO_ROOT) -> tuple[str | None, bool]:
    """当前 poker-ocr 的 commit 与 dirty 状态。只用于追溯，不进 config_hash。"""
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip() or None
        porcelain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout
        return commit, bool(porcelain.strip())
    except (OSError, subprocess.SubprocessError):
        return None, False


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8010
    token: str = ""
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5173",
                                     "http://localhost:5173")

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = ""
    db_password: str = ""
    db_name: str = "call_game_dev"
    db_connect_timeout: int = 5
    db_pool_size: int = 6

    hand_user_id: int = 0
    console_user_id: int = 0

    worker_enabled: bool = True
    worker_id: str = ""
    unit_workers: int = 2
    model_timeout: float = 30.0
    transient_retries: int = 2
    rate_limit_per_second: float = 5.0
    max_calls_per_job: int = 2000
    max_queue_depth: int = 3
    # 成本估算单价（元/百万 token）：REPLAY_PRICE_TABLE，启动时校验
    price_table: dict[str, dict[str, float]] = field(default_factory=dict)
    heartbeat_seconds: int = 10
    stale_seconds: int = 120
    auto_migrate: bool = True

    # judge（#58）：独立模型 + 温度 0；型号进 judge_version，随 run 冻结。
    judge_model: str = "deepseek-v4-pro"
    judge_base_url: str = ""
    judge_api_key_ref: str = "AI_SERVER_API_KEY"
    judge_timeout: float = 30.0
    judge_thinking: str = "disabled"

    store_prompts: bool = True
    retention_days: int = 365
    default_repeat: int = 1
    default_page_size: int = 50
    log_file: Path = field(default_factory=lambda: PROJECT_DIR / "logs" / "replay-console.log")
    static_dir: Path = field(default_factory=lambda: PROJECT_DIR / "replay_web" / "dist")
    loaded_env_files: tuple[Path, ...] = ()

    @classmethod
    def from_env(cls, *, load: bool = True) -> "Settings":
        files = load_environment() if load else []
        callback_default = REPO_ROOT.parent / "call-back" / ".env"
        db_host = os.environ.get("REPLAY_DB_HOST") or os.environ.get("DB_HOST") or "127.0.0.1"
        db_port = _int(os.environ.get("REPLAY_DB_PORT") or os.environ.get("DB_PORT"), 3306, 1, 65535)
        db_user = os.environ.get("REPLAY_DB_USER") or os.environ.get("DB_USER") or ""
        db_password = (os.environ.get("REPLAY_DB_PASSWORD")
                       if os.environ.get("REPLAY_DB_PASSWORD") is not None
                       else os.environ.get("DB_PASSWORD", ""))
        db_name = os.environ.get("REPLAY_DB_NAME") or os.environ.get("DB_NAME") or "call_game_dev"

        origins = [o.strip() for o in
                   (os.environ.get("REPLAY_CORS_ORIGINS")
                    or "http://127.0.0.1:5173,http://localhost:5173").split(",")
                   if o.strip()]
        log_file = Path(os.environ.get("REPLAY_LOG_FILE") or
                        PROJECT_DIR / "logs" / "replay-console.log").expanduser()
        static_dir = Path(os.environ.get("REPLAY_STATIC_DIR") or
                          PROJECT_DIR / "replay_web" / "dist").expanduser()
        worker_id = (os.environ.get("REPLAY_WORKER_ID")
                     or f"replay-{os.getpid()}")
        settings = cls(
            host=os.environ.get("REPLAY_HOST", "127.0.0.1"),
            port=_int(os.environ.get("REPLAY_PORT"), 8010, 1, 65535),
            token=(os.environ.get("REPLAY_TOKEN") or "").strip(),
            cors_origins=tuple(origins),
            db_host=db_host, db_port=db_port, db_user=db_user,
            db_password=db_password, db_name=db_name,
            db_connect_timeout=_int(os.environ.get("REPLAY_DB_CONNECT_TIMEOUT"), 5, 1, 60),
            db_pool_size=_int(os.environ.get("REPLAY_DB_POOL"), 6, 1, 20),
            hand_user_id=_int(os.environ.get("REPLAY_HAND_USER_ID"), 0, 0),
            console_user_id=_int(os.environ.get("REPLAY_CONSOLE_USER_ID"), 0, 0),
            worker_enabled=_truthy(os.environ.get("REPLAY_WORKER_ENABLED"), True),
            worker_id=worker_id,
            unit_workers=_int(os.environ.get("REPLAY_WORKERS"), 2, 1, 8),
            model_timeout=_float(os.environ.get("REPLAY_TIMEOUT"), 30.0) or 30.0,
            transient_retries=_int(os.environ.get("REPLAY_RETRIES"), 2, 0, 5),
            rate_limit_per_second=_float(os.environ.get("REPLAY_RATE_LIMIT"), 5.0) or 0.0,
            max_calls_per_job=_int(os.environ.get("REPLAY_MAX_CALLS_PER_JOB"), 2000, 1),
            max_queue_depth=_int(os.environ.get("REPLAY_MAX_QUEUE_DEPTH"), 3, 1),
            price_table=_price_table(os.environ.get("REPLAY_PRICE_TABLE")),
            heartbeat_seconds=_int(os.environ.get("REPLAY_HEARTBEAT_SECONDS"), 10, 2, 120),
            stale_seconds=_int(os.environ.get("REPLAY_STALE_SECONDS"), 120, 10),
            auto_migrate=_truthy(os.environ.get("REPLAY_AUTO_MIGRATE"), True),
            judge_model=(os.environ.get("REPLAY_JUDGE_MODEL")
                         or "deepseek-v4-pro").strip(),
            judge_base_url=(os.environ.get("REPLAY_JUDGE_BASE_URL")
                            or os.environ.get("AI_SERVER_BASE_URL")
                            or "").strip(),
            judge_api_key_ref=(os.environ.get("REPLAY_JUDGE_API_KEY_REF")
                               or "AI_SERVER_API_KEY").strip(),
            judge_timeout=_judge_timeout(os.environ.get("REPLAY_JUDGE_TIMEOUT")),
            judge_thinking=(os.environ.get("REPLAY_JUDGE_THINKING")
                            or "disabled").strip(),
            store_prompts=_truthy(os.environ.get("REPLAY_STORE_PROMPTS"), True),
            retention_days=_int(os.environ.get("REPLAY_RETENTION_DAYS"), 365, 0),
            default_repeat=_int(os.environ.get("REPLAY_DEFAULT_REPEAT"), 1, 1, 5),
            default_page_size=_int(os.environ.get("REPLAY_PAGE_SIZE"), 50, 1, 200),
            log_file=log_file, static_dir=static_dir,
            loaded_env_files=tuple(files),
        )
        # judge 配置错了必须启动就炸：静默退回默认会让整批评审口径静默变化。
        if not settings.judge_model:
            raise ValueError("REPLAY_JUDGE_MODEL 不能为空")
        if not settings.judge_api_key_ref:
            raise ValueError("REPLAY_JUDGE_API_KEY_REF 不能为空")
        if settings.judge_thinking not in ("", "enabled", "disabled"):
            raise ValueError("REPLAY_JUDGE_THINKING 只能是 enabled / disabled / 空")
        # 便于启动日志说明 DB 配置从哪来（不打印密码）
        settings.callback_env_path = callback_default
        return settings

    def public(self) -> dict:
        """给 `/api/meta` / `/api/health` 的安全视图，绝不返回 Key/密码。"""
        return {
            "host": self.host,
            "port": self.port,
            "db": {
                "host": self.db_host,
                "port": self.db_port,
                "name": self.db_name,
                "user": self.db_user,
                "configured": bool(self.db_user),
                "poolSize": self.db_pool_size,
            },
            "handUserId": self.hand_user_id,
            "consoleUserId": self.console_user_id,
            "workerEnabled": self.worker_enabled,
            "workers": self.unit_workers,
            "timeoutSeconds": self.model_timeout,
            "maxCallsPerJob": self.max_calls_per_job,
            "maxQueueDepth": self.max_queue_depth,
            "judgeModel": self.judge_model,
            "judgeTimeoutSeconds": self.judge_timeout,
            "storePrompts": self.store_prompts,
            "retentionDays": self.retention_days,
            "defaultRepeat": self.default_repeat,
            "loadedEnvFiles": [str(p) for p in self.loaded_env_files],
        }
