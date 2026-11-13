"""实时拉流 → 状态 JSONL（M3.1）。

    nv -m poker_ocr.live \
        --profile config/table_wpk_live.json \
        --initial-blinds 1/2 \
        --out logs/live.states.jsonl

默认链路：OBS ──虚拟摄像头──▶ 本进程（`--url cam:auto` 自动选 OBS 虚拟摄像头；
也可用 `cam:1` 手动指定，`tools/list_cameras.py` 查索引）；也可以用
`--url rtsp://127.0.0.1:8554/live` 走
OBS ──SRT──▶ MediaMTX ──RTSP──▶ 本进程。

设计要点：

  · **只保留最新帧**。reader 线程按源帧率连续 read，邮箱里永远只有一帧；
    处理不过来就覆盖旧帧，绝不让延迟越积越大。
  · **连续动作合并**。连续弃牌/下注时，中间帧的徽章还在动画里，读数既不可信
    也会写成一串「弃牌 1 个 → 2 个 → 3 个」的半成品记录。动作区静止
    `--settle` 秒后才识别，交出去的是这阵动作落定后的最新一帧；动作一直不停
    时由 `--settle-max` 兜底。盲注倒计时/头像计时环不参与「静止」判据。
  · **单处理线程**。reader 只做解码，`StateStream.ingest()` 永远只在主循环里
    调用；一帧内的多块 ROI rec 由线程池并行（`--roi-threads`，默认 4，1=串行）。
    共享同一个 OcrEngine/ONNX session，实测并发结果与串行逐字一致。
  · **默认不节流**。处理完当前帧立刻回到邮箱取最新帧，中间帧直接丢弃；
    `--every > 0` 才按固定间隔降频（调试/降压用）。
  · **断流自动重连**。重连后清空 ROI 缓存和状态机，下一帧重新当基线；
    累计计数保留，JSONL 里用 `epoch` 区分。
  · **输出 JSONL**：一行一个完整 JSON。`session_start` / `stream_info` /
    `state` / `session_end`。每条立即 flush，硬杀最多丢最后半行。
  · **每帧读牌**：`cards.recognize_cards()` 在 OCR 之前跑（纯 OpenCV，几毫秒）。
    `state` 记录按「个人手牌 → 位置 → 筹码 → 公共牌 → 全局 → 其他对手」排序；
    牌面变化也并进状态机指纹，翻牌/换手不会被判成 no_change。

延迟口径（可用 --dump-frames 实测）：

  · `queue_ms`：帧被 read 出来 → 开始 OCR 的排队时间（不含网络/解码缓冲）。
  · `proc_ms`：这一帧 OCR + 状态机 + 读牌耗时。
  · 帧内时钟到 `ts` 的差 = 传输 + 排队 + OCR。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .advice import (AISettings, AdviceRunner, PROMPT_LANGUAGE, PROMPT_VERSION,
                     build_situation, call_amount)
from .cards import (DEFAULT_CARD_SLOTS, DEFAULT_DOUBT_WINDOW, CardsResult,
                    StableCards, recognize_cards, recognize_shown_cards)
from .action_buttons import find_action_buttons
from .bet_slider import (BetSliderController, SliderSettings, WindowCapture,
                         WindowMapper, clicker_config_from_profile)
from .click_worker import InlineClickExecutor, ThreadClickExecutor
from .clicker import (DEFAULT_CONFIG_PATH as DEFAULT_CLICKER_CONFIG,
                      CGEventBackend, Clicker, ClickerConfig,
                      ClickerConfigError, ClickerUnavailable, ClickResult,
                      DryRunBackend, list_displays, rect_inside_displays)
from .env import EnvFileError, load_env
from .frame_state import action_signature
from .full_table import (DealerTracker, FullOutcome, FullStateRecorder,
                         FullTableConfig, load_full_config, observe_full)
from .hand_outbox import HandOutbox
from .hand_ingest import HandIngestWorker, IngestConfig
from .hand_track import LiveHandTracker
from .live_log import (ConsoleLog, LiveEventLog, derive_console_path,
                       derive_event_path)
from .live_web import (LiveControl, LiveWebServer, PreviewStore,
                       build_layout)
from .name_lexicon import NameLexicon, duplicate_pairs
from .ocr import OcrEngine
from .parse import parse_blinds
from .profile import Profile
from .roi_dedup import pixel_mad
from .roi_reader import hero_turn_method
from .state_stream import DEFAULT_ROI_THREADS, StateStream
from .opponent_profile import (DEFAULT_PROFILE_PATH as DEFAULT_OPPONENT_DB,
                               ProfileStore, format_read_line,
                               load_profile_store, pool_key_from_blinds)
from .table_dynamics import TableDynamics, load_table_dynamics

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "config" / "table_wpk_1080x2400.json"
# 默认直接读 OBS 虚拟摄像头（同一台 Mac、延迟最低）；走 MediaMTX 时显式传
# `--url rtsp://127.0.0.1:8554/live`。
DEFAULT_URL = "cam:auto"
DEFAULT_OUT = Path("logs/live.states.jsonl")
# 测试模式（--test）的默认日志：按实战视频演练时用，和实战记录彻底分开，
# 手牌也不进手牌库（见 apply_test_mode）。
TEST_OUT = Path("logs/live.test.jsonl")
DEFAULT_INCIDENT_DIR = Path("logs/incidents")
DEFAULT_HAND_OUTBOX = PROJECT_ROOT / "data" / "hand_outbox"
DEFAULT_BET_SLIDER_CONFIG = PROJECT_ROOT / "config" / "bet_slider_mac.json"
# 异常要持续这么久才落盘：发牌/翻牌动画里的单帧误读（1 张公共牌、hero 只读到
# 1 张）属于正常现象，稳定闸会吸收掉，不值得为每一帧存 10 张图。
INCIDENT_MIN_SECONDS = 1.0
# 同一类异常连续这么久没再出现，才算本次「疑惑」结束；否则整段持续异常只存一次。
INCIDENT_EPISODE_GAP = 10.0
# 单次会话最多落盘多少个疑惑帧目录，防止整场持续异常把磁盘写满。
DEFAULT_INCIDENT_MAX = 20
# ── 质量闸丢帧的可观测性 ──
# 只在退出汇总里报丢弃原因太晚：启动后如果画面不是牌桌（大厅、结算弹窗、
# 比例不对），一帧都过不了闸，终端上却只有 [状态] 行。这里在丢帧时按
# 「原因组合」节流打印，并落一帧样本，让「为什么没输出」当场可见。
GATE_LOG_SECONDS = 5.0       # 同一类原因最多多久打一行
GATE_DUMP_LIMIT = 3          # 单次会话最多为几类丢帧原因存样本
GATE_DUMP_MIN_GAP = 10.0     # 两次落盘至少间隔多久（秒）
ZERO_ACCEPT_WARN_AFTER = 20  # 处理这么多帧仍 0 接受就告警
# 默认不节流：处理完当前帧立刻取邮箱里的最新帧，中间帧直接丢弃。
# `--every` 仍保留给调试/降压用（>0 时按该间隔采样）。
DEFAULT_EVERY = 0.0

# 让 OpenCV 的 FFmpeg 后端用 TCP、少缓冲。必须在第一次 VideoCapture 之前设置。
FFMPEG_OPTIONS = ("rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0")


# ─────────────────────────── 基础组件 ───────────────────────────

# 控制台文本落盘：主进程启动后由 main() 挂上；None = 只打终端。
_console_sink: ConsoleLog | None = None


def set_console_sink(sink: ConsoleLog | None) -> None:
    global _console_sink
    _console_sink = sink


def _log(msg: str) -> None:
    print(msg, flush=True)
    sink = _console_sink
    if sink is not None:
        try:
            sink.write(msg)
        except OSError:
            # 日志文件写失败不能反过来拖垮识别；终端仍然有输出。
            pass


# 终端没法用 ANSI 改字号，「加大」靠加粗 + 高亮底色 + 上下留白来拉开层次。
# 只在真终端里上色：重定向到文件 / NO_COLOR 时保持纯文本，方便 grep。
_ANSI_RESET = "\033[0m"
# 建议横幅的底色按动作区分：弃牌=红、跟注/过牌=蓝、下注/加注=绿。
_ANSI_ADVICE_BAR = {
    "fold": "\033[1;97;41m",          # 粗体白字 + 红底
    "call": "\033[1;97;44m",          # 粗体白字 + 蓝底
    "check": "\033[1;97;44m",         # 过牌是被动动作，跟 call 同色
    "raise": "\033[1;30;42m",         # 粗体黑字 + 绿底
    "bet": "\033[1;30;42m",           # 下注/加注同属主动进攻
    "allin": "\033[1;30;42m",         # 全下也是主动进攻
}
_ANSI_ADVICE_BAR_DEFAULT = "\033[1;30;103m"   # 未知动作退回原来的亮黄底
_ANSI_ADVICE_TEXT = "\033[1m"         # 粗体
# 完整手牌落库提示：亮黄底黑字，和滚动中的识别日志拉开，方便事后回看这一手。
_ANSI_HAND_DONE = "\033[1;30;103m"


def _advice_bar(kind: str) -> str:
    """建议横幅的 ANSI 样式：动作类型 → 背景色。"""
    return _ANSI_ADVICE_BAR.get(kind, _ANSI_ADVICE_BAR_DEFAULT)


def _table_stats_for_active(profile_store, opponents):
    """只聚合「本手还在局」的对手；total_players 含 Hero。

    已弃牌的人不再参与当前决策；把他们算进桌况会让「本手在局 N 人」
    和实际底池人数对不上（一手 8 人发牌、5 人弃牌时会被写成 7 人桌）。
    """
    active = [o for o in opponents if not getattr(o, "folded", False)]
    current = profile_store.stats_for_opponents(active)
    return profile_store.pooled_for_stats(
        current, total_players=len(active) + 1)


def _hand_done_text(hand: dict) -> str:
    """完整手牌落库提示文案：hand_id（JSONL 里按 id 能直接搜到）。

    ID 现在是「日期 + 名单摘要」，不再有 `h7` 这样的顺序号；直接显示 ID，
    避免再维护一套序号解析。
    """
    hand_id = str(hand.get("hand_id") or "?")
    return f"完整手牌 {hand_id} 日志已经生成"


def _log_hand_done(log: Callable[[str], None], hand: dict) -> None:
    """完整手牌落库时打一条黄色横幅；重定向到文件时保持纯文本。"""
    text = f" {_hand_done_text(hand)} "
    log(f"{_ANSI_HAND_DONE}{text}{_ANSI_RESET}" if _use_ansi() else text.strip())


def _use_ansi() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


_ENV_TRUE = {"1", "true", "yes", "on"}
_ENV_FALSE = {"0", "false", "no", "off"}


def _env_flag(name: str) -> bool | None:
    """读布尔环境变量：没设返回 None，值无法识别抛 ValueError。

    显式写 0/false 是「关闭」，和没写区分开：调用方可以用命令行参数覆盖
    `.env` 里的默认值。
    """
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    if raw in _ENV_TRUE:
        return True
    if raw in _ENV_FALSE:
        return False
    raise ValueError(f"{name}={raw!r} 不是布尔值（用 1/0、true/false）")


def _confirm_key_choice(key: str) -> bool | None:
    """把一次输入映射成确认结果：只有 y/n 有效，其他返回 None（忽略）。"""
    key = (key or "").strip().lower()
    if key == "y":
        return True
    if key == "n":
        return False
    return None


def _ask_confirm_line(log: Callable[[str], None],
                      deadline: float | None = None) -> bool:
    """非 TTY（管道/重定向）下按行读确认：整行只有 y/n 才算有效。"""
    while True:
        if deadline is not None:
            remain = max(deadline - time.monotonic(), 0.0)
            try:
                import select
                stream = sys.stdin
                ready = (select.select([stream], [], [], remain)[0]
                         if stream is not None and hasattr(stream, "fileno")
                         else [stream])
            except (ImportError, OSError, ValueError):
                ready = [sys.stdin]
            if not ready:
                log("  [确认] 等待超时，按拒绝处理")
                return False
        try:
            line = input()
        except (EOFError, OSError):
            log("  [确认] 没有读到输入，按拒绝处理")
            return False
        choice = _confirm_key_choice(line)
        if choice is None:
            continue                     # 其他按键/内容不识别，继续等
        if not choice:
            log("  [确认] 已拒绝，不执行")
        return choice


def _ask_confirm(log: Callable[[str], None],
                 timeout: float | None = None) -> bool:
    """读取确认键：只识别 y / n，其他键直接忽略。

    TTY 下用 cbreak 单键读取（不需要回车），并在读取前 `tcflush` 清空终端
    里缓存的按键 —— 模型生成/用户提前敲的键不会被当成这次确认。非 TTY
    （管道、重定向、单测）退回按行读取，整行必须恰好是 y / n。
    """
    stream = sys.stdin
    if stream is None:
        log("  [确认] 标准输入不可用，按拒绝处理")
        return False
    deadline = (time.monotonic() + float(timeout)
                if timeout is not None and timeout > 0 else None)
    if not stream.isatty():
        return _ask_confirm_line(log, deadline=deadline)

    try:
        import termios
        import tty
    except ImportError:                  # pragma: no cover — Windows
        return _ask_confirm_line(log)
    try:
        fd = stream.fileno()
        old = termios.tcgetattr(fd)
    except (OSError, ValueError, termios.error):
        return _ask_confirm_line(log)

    try:
        tty.setcbreak(fd, termios.TCSANOW)
        # 切到 cbreak 后再清一次：把提示出现前/模型生成期间敲下的键丢掉，
        # 只认这条提示之后真正按下的 y / n。
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass
        while True:
            if deadline is not None:
                remain = max(deadline - time.monotonic(), 0.0)
                try:
                    import select
                    ready, _, _ = select.select([fd], [], [], remain)
                except (ImportError, OSError, ValueError):
                    ready = [fd]
                if not ready:
                    log(f"  [确认] 等待超时（{timeout:g}s），按拒绝处理")
                    return False
            try:
                raw = os.read(fd, 1)
            except OSError:
                log("  [确认] 读取键盘失败，按拒绝处理")
                return False
            if not raw:
                log("  [确认] 输入已关闭，按拒绝处理")
                return False
            choice = _confirm_key_choice(raw.decode("utf-8", "ignore"))
            if choice is None:
                continue                 # 其他按键不识别，继续等
            if not choice:
                log("  [确认] 已拒绝，不执行")
            return choice
    finally:
        # 读完再清一次：y/n 之后多按的键不会残留到下一次确认或退出后的终端。
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except termios.error:
            pass


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _sha256_file(path: Path) -> str | None:
    """文件内容哈希（相对路径溯源用）；读不到返回 None，不抛。"""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _source_kind(url: str) -> str:
    text = str(url or "")
    if text.startswith(("window:", "screen:")):
        return "window"
    if text.startswith(("cam:", "camera:")):
        return "camera"
    if text.startswith(("rtsp://", "rtsps://")):
        return "rtsp"
    if text.startswith(("http://", "https://")):
        return "stream"
    return "file"


def _resolve_source_window(url: str) -> dict | None:
    """窗口源启动时解析一次屏幕矩形（逻辑点）；解析失败返回 None。

    只给 `/api/meta` 和工作台浮层追溯用，不参与识别/点击路径 —— 失败
    fail-open，绝不能让一个读窗口的动作拖住启动。
    """
    text = str(url or "")
    if not text.startswith(("window:", "screen:")):
        return None
    _, _, raw = text.partition(":")
    try:
        from .bet_slider import resolve_window, resolve_window_info
        info = resolve_window_info(raw)
        rect = info.rect if info is not None else resolve_window(raw)
        return {"spec": raw, **rect.to_dict(),
                "window_id": (info.window_id if info is not None else 0)}
    except Exception:  # noqa: BLE001 — 窗口解析失败不影响识别
        return None


@dataclass(frozen=True)
class FramePacket:
    seq: int                 # 全局递增，跨 epoch 也不回退
    epoch: int               # 第几次成功连上 RTSP（从 1 开始）
    grab_mono: float         # read() 返回时的 monotonic 时间
    image: np.ndarray        # BGR


@dataclass
class _ClickJob:
    """一次已经通过安全闸、交给执行器去跑的点击。

    `record` 是这条建议在 JSONL 里的 dict：异步执行时结果晚到，靠它把
    `click` 补进同一个对象（手牌记录会一起带上）。
    """

    advice: object
    targets: dict | None
    record: dict | None = None
    submitted: float = 0.0
    # 点击那一刻的画面状态：0.5s 后拿它的动作指纹判「动作是否生效」。
    state: object = None
    # 结果是否要单独落一条 `click` 事件：异步执行时才需要（advice 那一行
    # 已经在点击之前写出去了）；同步执行时结果直接写进 advice 行的 click 键。
    write_event: bool = False


@dataclass
class _ClickRetry:
    """一次已点击动作的「生效确认」。

    0.5s 后拿当前帧和点击时的动作指纹比：变了 = 客户端已接受；没变 = 再点
    一次（最多 `retry_count` 次）。
    """

    advice: object
    signature: tuple
    next_at: float
    remaining: int


# 建议已经生成、但**这一帧**不能点：牌面/下注读数这一帧不可靠
# （`frame_not_clickable`），或整帧被质量闸丢了（`state_not_ok`）。
# 这两种都是瞬时的 —— 同一决策点还在、回合还没结束，下一帧恢复就该补点。
# 别的跳过原因（`stale_*` / `hero_turn_false` / 决策点不匹配）说明这条建议
# 已经作废，不能补。
DEFER_CLICK_REASONS = ("frame_not_clickable", "state_not_ok")
# 补点窗口（秒）。回合时限通常 10–15s；超过这个窗口画面还没恢复就别点了。
DEFER_CLICK_SECONDS = 3.0


@dataclass
class _DeferredClick:
    """一条已经生成、但当时没能点出去的建议，等下一帧补点。"""

    advice: object
    until: float


class LatestFrame:
    """只保留最新一帧的邮箱。处理慢时旧帧被覆盖，队列深度恒为 1。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._packet: FramePacket | None = None
        self._closed = False
        self.published = 0

    def publish(self, packet: FramePacket) -> None:
        with self._cond:
            self._packet = packet
            self.published += 1
            self._cond.notify_all()

    def wait_newer(self, last_seq: int, timeout: float = 0.5) -> FramePacket | None:
        """等到一个 seq > last_seq 的包；超时或关闭返回 None。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                # 先交付还在邮箱里的新帧；关闭后也要让最后一帧被消费掉
                if self._packet is not None and self._packet.seq > last_seq:
                    return self._packet
                if self._closed:
                    return None
                rest = deadline - time.monotonic()
                if rest <= 0:
                    return None
                self._cond.wait(rest)

    def latest(self) -> FramePacket | None:
        with self._cond:
            return self._packet

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


def throttle_delay(last_start: float, now: float, every: float) -> float:
    """距离下一次允许采样还要等多久（秒）。"""
    return max(0.0, last_start + every - now)


# ─────────────────────────── 画布裁剪 ───────────────────────────

@dataclass(frozen=True)
class CropPlan:
    """把输入画布裁成训练图片的宽高比。"""

    mode: str
    src_size: tuple[int, int]                 # (w, h)
    rect: tuple[int, int, int, int] | None    # x1, y1, x2, y2
    out_size: tuple[int, int]                 # (w, h)
    warning: str | None = None

    def apply(self, img: np.ndarray) -> np.ndarray:
        if self.rect is None:
            return img
        x1, y1, x2, y2 = self.rect
        return img[y1:y2, x1:x2]

    def to_debug(self) -> dict:
        out: dict = {
            "mode": self.mode,
            "src": list(self.src_size),
            "out": list(self.out_size),
        }
        if self.rect is not None:
            out["rect"] = list(self.rect)
        if self.warning:
            out["warning"] = self.warning
        return out


def _content_bbox(img: np.ndarray, threshold: int = 8) -> tuple[int, int, int, int] | None:
    """非黑画面的外接框。用于 auto 裁剪：OBS 把牌桌放左边、右边留黑时直接切掉。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (gray > threshold).astype(np.uint8)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


@dataclass(frozen=True)
class AspectFit:
    """把画面重采样成 profile 的参考宽高比，让 ROI 的 x/y 缩放一致。

    ROI 槽位是归一化坐标，**只有输入宽高比 = ``reference_size`` 的宽高比时**，
    x/y 才共用同一个像素缩放。流的分辨率/比例和标定图不同时（OBS 缩放、
    播放器把竖屏视频拉伸、低分辨率采集），按宽高各自归一化会让所有槽位在
    x 方向按错误比例展开 —— 越靠边的座位偏得越多，越靠下/上的槽位也会错。

    做法：**以高度为准，只改宽度**。

        out_w = round(h * ref_w / ref_h)

    这样两个轴的缩放都等于 ``h / ref_h``，槽位的相对位置和标定图一一对应。
    选高度而不是宽度，是因为低分辨率流横向像素本来就不够 —— 把 326x720
    拉成 1280x2844 只会放大噪声；保持 720 高、宽度回到 324 才是原比例。
    输入比例本来就匹配时（1080x2400、864x1920）这一步是 no-op。
    """

    src_size: tuple[int, int]                  # 处理前的 (w, h)
    out_size: tuple[int, int]                  # 重采样后的 (w, h)
    aspect: float                              # 处理前的 w / h
    reference_size: tuple[int, int]            # profile.reference_size
    reference_aspect: float = 0.0              # reference_size 的 w / h

    @property
    def resized(self) -> bool:
        return self.out_size != self.src_size

    @property
    def deviation(self) -> float:
        """处理前宽高比相对参考比例的偏差（0.01 = 1%）。"""
        if not self.reference_aspect:
            return 0.0
        return abs(self.aspect - self.reference_aspect) / self.reference_aspect

    @property
    def scale(self) -> tuple[float, float]:
        """(sx, sy)：参考图 → 处理后。正常应当 sx == sy，这正是 ROI 能对齐的前提。"""
        ref_w, ref_h = self.reference_size
        ow, oh = self.out_size
        return ow / max(1, ref_w), oh / max(1, ref_h)

    @classmethod
    def for_size(cls, size: tuple[int, int], profile: Profile) -> "AspectFit":
        w, h = size
        ref_w, ref_h = profile.reference_size
        ref_aspect = ref_w / ref_h if ref_h else 1.0
        out_w = max(1, round(h * ref_aspect)) if h else max(1, w)
        return cls((w, h), (out_w, h), w / h if h else ref_aspect,
                   (ref_w, ref_h), ref_aspect)

    def apply(self, img: np.ndarray) -> np.ndarray:
        if not self.resized:
            return img
        w, h = self.src_size
        ow, oh = self.out_size
        interp = cv2.INTER_AREA if ow < w else cv2.INTER_CUBIC
        return cv2.resize(img, (ow, oh), interpolation=interp)

    def to_debug(self) -> dict:
        d: dict = {
            "src": list(self.src_size),
            "out": list(self.out_size),
            "aspect": round(self.aspect, 6),
            "reference": list(self.reference_size),
            "reference_aspect": round(self.reference_aspect, 6),
            "deviation": round(self.deviation, 6),
            "scale": [round(s, 6) for s in self.scale],
        }
        if self.resized:
            d["resized"] = True
        return d


def plan_crop(size: tuple[int, int], profile: Profile,
              mode: str = "auto",
              image: np.ndarray | None = None) -> CropPlan:
    """把画布裁成牌桌区域。

    三种模式：
      · `auto`（默认）：找非黑内容的包围盒。OBS 左边放牌桌、右边留黑时直接切掉；
        内容铺满画布时不裁。全图路线不依赖训练比例，用这个最稳。
      · `left`：按训练图片宽高比 0.45，从左侧裁 `h * 1080 / 2400`。
      · `none`：不裁。
    """
    h, w = size
    if mode == "none":
        return CropPlan("none", (w, h), None, (w, h))

    if mode == "auto":
        if image is None:
            return CropPlan("auto", (w, h), None, (w, h), "auto 裁剪需要图片")
        bbox = _content_bbox(image)
        if bbox is None:
            return CropPlan("auto", (w, h), None, (w, h),
                            "整帧接近全黑，无法自动裁剪")
        x1, y1, x2, y2 = bbox
        # 小于画布 5% 的亮块多半是光标/图标，不当画面
        if (x2 - x1) * (y2 - y1) < w * h * 0.05:
            return CropPlan("auto", (w, h), None, (w, h),
                            "非黑区域太小，自动裁剪已跳过")
        # 内容已经贴边：不裁
        if x1 <= 2 and y1 <= 2 and x2 >= w - 2 and y2 >= h - 2:
            return CropPlan("auto", (w, h), None, (w, h))
        return CropPlan("auto", (w, h), bbox, (x2 - x1, y2 - y1))

    ref_w, ref_h = profile.reference_size
    target_w = max(1, round(h * ref_w / ref_h))
    if target_w < w:
        return CropPlan(mode, (w, h), (0, 0, target_w, h), (target_w, h))

    warning = None
    ref_aspect = ref_w / ref_h
    if h and abs(w / h - ref_aspect) / ref_aspect > 0.02:
        warning = (f"画布 {w}x{h} 比训练比例窄，无法按左侧裁出 {target_w}px；"
                   f"请把 OBS 输出改成竖屏，或让画面保持 0.45 宽高比")
    return CropPlan(mode, (w, h), None, (w, h), warning)


# ─────────────────────────── 动作区静止闸 ───────────────────────────
#
# 连续弃牌 / 连续下注时，画面会在一两秒里一帧一变。逐帧 OCR 只会得到一串
# 中间状态（弃牌 1 个 → 2 个 → 3 个），每个都被当成一条新记录；而这些帧里的
# 徽章还在动画中，本来就是最不可信的读数。用户要的是**这阵动作落定后的最新
# 一帧**。
#
# 做法：在 OCR 之前只看**动作相关区域**的像素变化。还在变 → 丢掉当前帧继续
# 等；静止 `settle` 秒 → 才把邮箱里的最新帧交给识别。动作一直不停时由
# `max_wait` 兜底，保证不会无限等下去。
#
# 哪些区域**不能**算进来：
#   · 盲注横幅（`blinds`）里有房间号和每秒跳动的倒计时；
#   · 头像只有**中心**参与：回合计时环画在头像边缘，整块拿来比永远不会静止；
#   · hero 操作区 / 预设按钮：蓝色按钮会呼吸式高亮，而且轮到我时本来就要
#     尽快出建议，不能被动画拖到 max_wait。
DEFAULT_SETTLE = 0.25
DEFAULT_SETTLE_MAX = 1.5
SETTLE_MAD = 2.0                  # 文字/数字区：下采样到 32x32 后的平均绝对差门槛
# 头像中心单独用更高的门槛：照片本身的压缩噪声比文字区大一个量级（实测
# 24fps 视频 p90 就有 2.4），而弃牌蒙层/蓝色下注钮会把平均值推到 30 以上 ——
# 门槛取 10 既不会把噪声当动作，也不会漏掉整块变暗/变蓝这种真变化。
SETTLE_AVATAR_MAD = 10.0
SETTLE_AVATAR_INSET = 0.2         # 头像只取中心 60%，躲开边缘的计时环
_SETTLE_GLOBALS = ("pot", "pot_before_street", "board", "hero_cards", "hand_label")
_SETTLE_SIZE = (32, 32)


def _shrink(rect: tuple[int, int, int, int],
            frac: float) -> tuple[int, int, int, int]:
    """按比例向内收一圈。frac=0.2 → 保留中心 60%。"""
    x1, y1, x2, y2 = rect
    dx, dy = int((x2 - x1) * frac), int((y2 - y1) * frac)
    return x1 + dx, y1 + dy, x2 - dx, y2 - dy


def build_settle_regions(profile: Profile, size: tuple[int, int], *,
                         mad: float = SETTLE_MAD,
                         avatar_mad: float = SETTLE_AVATAR_MAD,
                         avatar_inset: float = SETTLE_AVATAR_INSET
                         ) -> tuple[tuple[tuple[int, int, int, int], float], ...]:
    """一帧里参与「还在动吗」判定的区域，以及各自的 MAD 门槛。"""
    regions: list[tuple[tuple[int, int, int, int], float]] = []
    for seat in profile.seats:
        for slot in (seat.name_slot, seat.chip_slot, seat.chip_slot_alt,
                     seat.bet_slot):
            if slot is not None:
                regions.append((profile.slot_to_px(slot, size), mad))
        regions.append((_shrink(profile.slot_to_px(seat.avatar_slot, size),
                                avatar_inset), avatar_mad))
    for key in _SETTLE_GLOBALS:
        slot = profile.global_slots.get(key)
        if slot is None and key in DEFAULT_CARD_SLOTS:
            # 牌区必须参与静止判定：翻牌/转牌动画中间帧会让 board 4→3，
            # 读牌层虽然还有 StableCards 兜底，但能晚 1–2 帧读干净图更好。
            # 有些 live profile 没标 board/hero_cards，这里用与 recognize_cards
            # 相同的默认槽位补上，保证所有版式都等牌面落定。
            slot = DEFAULT_CARD_SLOTS[key]
        if slot is not None:
            regions.append((profile.slot_to_px(slot, size), mad))
    return tuple((r, th) for r, th in regions if r[2] > r[0] and r[3] > r[1])


class SettleGate:
    """动作区静止闸：一阵连续动作只放行落定后的最后一帧。

        gate = SettleGate(profile, settle=0.25, max_wait=1.5)
        if gate.should_process(img, time.monotonic()):
            process(img)

    时间参数由调用方传入（不自己读钟），单测可以直接喂合成时间轴。
    画面尺寸变了会重建区域 —— 和 ROI 缓存一样，换分辨率不能沿用旧坐标。
    """

    def __init__(self, profile: Profile, *, settle: float = DEFAULT_SETTLE,
                 max_wait: float = DEFAULT_SETTLE_MAX,
                 mad: float = SETTLE_MAD,
                 avatar_mad: float = SETTLE_AVATAR_MAD,
                 avatar_inset: float = SETTLE_AVATAR_INSET) -> None:
        if settle < 0:
            raise ValueError("settle 不能为负")
        if max_wait < settle:
            raise ValueError("max_wait 不能小于 settle")
        self.profile = profile
        self.settle = float(settle)
        self.max_wait = float(max_wait)
        self.mad = float(mad)
        self.avatar_mad = float(avatar_mad)
        self.avatar_inset = float(avatar_inset)
        self._size: tuple[int, int] | None = None
        self._regions: tuple[tuple[tuple[int, int, int, int], float], ...] = ()
        self._prev: tuple[tuple[np.ndarray, float], ...] | None = None
        # 没有「上次变化」时视为已经静止：第一帧立即放行，不白等一个窗口
        self._last_change = float("-inf")
        self._burst_start: float | None = None
        self.blocks = 0               # 合并掉的帧数（统计/调试）

    def _signature(self, img: np.ndarray) -> tuple[tuple[np.ndarray, float], ...]:
        size = (img.shape[1], img.shape[0])
        if size != self._size:
            self._size = size
            self._regions = build_settle_regions(
                self.profile, size, mad=self.mad, avatar_mad=self.avatar_mad,
                avatar_inset=self.avatar_inset)
            self._prev = None
            self._last_change = float("-inf")
            self._burst_start = None
        out = []
        for (x1, y1, x2, y2), mad in self._regions:
            roi = img[y1:y2, x1:x2]
            if roi.size == 0:
                continue
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            out.append((cv2.resize(gray, _SETTLE_SIZE,
                                   interpolation=cv2.INTER_AREA), mad))
        return tuple(out)

    def _changed(self, prev: tuple[tuple[np.ndarray, float], ...],
                 cur: tuple[tuple[np.ndarray, float], ...]) -> bool:
        if len(prev) != len(cur):
            return True
        return any(pixel_mad(a, b) > mad
                   for (a, _), (b, mad) in zip(prev, cur))

    def should_process(self, img: np.ndarray, now: float) -> bool:
        """`now` 是单调时钟。返回 True = 画面已落定，可以交给 OCR。"""
        sig = self._signature(img)
        if self._prev is not None and self._changed(self._prev, sig):
            self._last_change = now
            if self._burst_start is None:
                self._burst_start = now
        self._prev = sig

        if (now - self._last_change >= self.settle
                or (self._burst_start is not None
                    and now - self._burst_start >= self.max_wait)):
            self._burst_start = None
            return True
        self.blocks += 1
        return False


class JsonlWriter:
    """一行一个 JSON；每条立即 flush。

    默认**追加**：每次启动不清空历史手牌，多天的记录留在同一个文件里。
    需要回到旧行为时用 `append=False`（CLI 的 --no-append）。
    """

    def __init__(self, path: Path, append: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.append = append

        # 上次被 Ctrl-C / kill 时可能停在半行。补一个换行，保证新记录不会
        # 接在坏行后面（坏行本身保留，读取方按 JSON 解析失败跳过即可）。
        needs_newline = False
        if append and path.is_file() and path.stat().st_size > 0:
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                needs_newline = fh.read(1) != b"\n"

        self._fh = open(path, "a" if append else "w",
                        encoding="utf-8", newline="\n")
        if needs_newline:
            self._fh.write("\n")

    def write(self, record: dict) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()


# ─────────────────────────── 读流 ───────────────────────────

def _state_body(record: dict, cards: CardsResult | None, recognizer: str) -> dict:
    """把状态记录重排成：个人手牌/位置/筹码 → 公共牌 → 全局 → 其他对手。

    ROI 模式的 hero 是 `players` 里 `seat_id == hero_seat_id` 的那条；
    full 模式的 hero 是 `seats` 里 `is_hero=True` 的那条。hero 从原列表摘出来
    单独放，opponents 永远排在最后 —— 这是调用方明确要求的输出顺序。

    `hero` 里保留 hero 的其余字段（名字/下注/动作/全下），避免为了重排丢信息。
    """
    rec = dict(record)
    if recognizer == "full":
        seats = rec.pop("seats", None) or []
        hero = next((s for s in seats if s.get("is_hero")), None)
        opponents = [s for s in seats if s is not hero]
    else:
        players = rec.pop("players", None) or []
        hero_id = rec.pop("hero_seat_id", None)
        hero = next((p for p in players if p.get("seat_id") == hero_id), None)
        # 空座位 / 暂离不是「对手」；in_hand 的（含已弃牌）才算
        opponents = [p for p in players
                     if p.get("seat_id") != hero_id and p.get("in_hand", True)]

    body: dict = {}
    for key in ("index", "t", "frame"):
        if key in rec:
            body[key] = rec.pop(key)

    hero = hero or {}
    body["hero_cards"] = cards.hero_codes if cards is not None else []
    body["hero_position"] = hero.get("position")
    body["hero_stack"] = hero.get("stack")
    body["board"] = cards.board_codes if cards is not None else []
    body["hero"] = hero or None
    body.update(rec)                 # 底池 / 盲注 / 庄位 / 轮到我 / 桌型……
    if cards is not None:
        body["cards_reliable"] = bool(getattr(cards, "reliable", True))
        if cards.warnings:
            body["cards_warnings"] = cards.warnings
    body["opponents"] = opponents    # 其他对手永远最后
    shows = getattr(cards, "shows", {}) if cards is not None else {}
    if shows:
        # 亮牌座位尽量补上名字/位置；full 路线的 seats 不一定有 seat_id，
        # 补不到就只写 seat_id 和牌。
        by_id = {p.get("seat_id"): p for p in opponents}
        body["shows"] = [
            {
                "seat_id": seat_id,
                "name": (by_id.get(seat_id) or {}).get("name"),
                "position": (by_id.get(seat_id) or {}).get("position"),
                "cards": read.codes,
            }
            for seat_id, read in shows.items()
        ]
    return body


def _name_tail(body: dict) -> str:
    """把名字诊断挂到状态行尾巴上：一条一行，多了只报条数。

    名字读错不会丢帧（不进质量闸），所以最容易「悄悄地错」—— 必须在控制台
    上就看得见，而不是等人翻 JSONL 才发现。
    """
    warns = body.get("name_warnings") or []
    if not warns:
        return ""
    extra = f" …另 {len(warns) - 1} 条" if len(warns) > 1 else ""
    return f"  名字⚠ {warns[0]}{extra}"


def _state_line(t: float, body: dict, tail: str = "") -> str:
    """日志行：顺序与 JSONL 一致（我 → 公共牌 → 全局 → 对手）。"""
    hero = body.get("hero") or {}
    cards = " ".join(body.get("hero_cards") or []) or "-"
    board = " ".join(body.get("board") or []) or "-"
    opponents = " ".join(
        f"{p.get('seat_id')}:{p.get('name') or '-'}={p.get('stack')}"
        f"{'/' + str(p.get('bet')) if p.get('bet') else ''}"
        f"{'(' + p.get('action') + ')' if p.get('action') else ''}"
        f"{'/离桌' if p.get('left') else ''}"
        for p in (body.get("opponents") or [])
    ) or "-"
    return (f"  [{t:7.2f}s] ✅ 记录 #{body.get('index')}  "
            f"我: {cards} 位置={hero.get('position') or '-'} "
            f"筹码={hero.get('stack')}  公共牌: {board}{tail}  "
            f"对手: {opponents}")


def _incident_category(reason: str) -> str:
    """把具体异常文本归成稳定的类别，用于「一段持续异常只存一次」。

    原始文本里常带具体牌（Kh 是牌背 / 8h 是牌背），逐帧变化会让去重失效；
    归成 board:back、hero_cards:count 这类类别后才能把整段异常合成一份。
    """
    slot, _, detail = reason.partition(":")
    if "回退" in detail:
        kind = "regress"
    elif "牌背" in detail:
        kind = "back"
    elif "遮挡" in detail:
        kind = "occluded"
    elif "读数不可靠" in detail:
        kind = "unreliable"
    elif "张数" in detail or "本帧" in detail:
        kind = "count"
    else:
        kind = "other"
    return f"{slot.strip()}:{kind}"


class RtspReader(threading.Thread):
    """持续读 RTSP，把最新帧丢进邮箱；断了就 1s 后重连。

    只做解码，**绝不调用 OCR / StateStream**。
    """

    def __init__(self, url: str, mailbox: LatestFrame, stop: threading.Event, *,
                 reconnect_delay: float = 1.0, log=_log,
                 capture_factory=None) -> None:
        super().__init__(daemon=True, name="rtsp-reader")
        self.url = url
        self.mailbox = mailbox
        self.stop_event = stop
        self.reconnect_delay = reconnect_delay
        self.log = log
        self._capture_factory = capture_factory or _open_capture

        self.seq = 0
        self.epoch = 0
        self.reconnects = 0
        self.grabbed = 0
        self.last_grab_mono = 0.0
        self.connected_once = False

    def run(self) -> None:
        while not self.stop_event.is_set():
            cap = self._capture_factory(self.url)
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                self.reconnects += 1
                self.log(f"[重连] 打不开 {self.url}，{self.reconnect_delay:.0f}s 后重试")
                self.stop_event.wait(self.reconnect_delay)
                continue

            self.epoch += 1
            self.connected_once = True
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            self.log(f"[连接] 视频源已连接 epoch={self.epoch}：{w}x{h} @ {fps:.1f}fps")

            try:
                while not self.stop_event.is_set():
                    ok, img = cap.read()
                    if not ok:
                        break
                    self.seq += 1
                    self.grabbed += 1
                    self.last_grab_mono = time.monotonic()
                    self.mailbox.publish(FramePacket(
                        seq=self.seq, epoch=self.epoch,
                        grab_mono=self.last_grab_mono, image=img))
            finally:
                cap.release()

            if not self.stop_event.is_set():
                self.reconnects += 1
                reason = getattr(cap, "last_error", None)
                detail = f"：{reason}" if reason else ""
                self.log(f"[重连] 流中断{detail}，"
                         f"{self.reconnect_delay:.0f}s 后重试")
                self.stop_event.wait(self.reconnect_delay)


def rotate_frame(img: np.ndarray, degrees: int) -> np.ndarray:
    """按 0/90/180/270 度旋转。**90 = 顺时针**（与 cv2.ROTATE_90_CLOCKWISE 一致）。

    OBS 虚拟摄像头在 macOS 上固定输出 1920x1080；竖屏画布塞进去只剩 ~486x1080，
    筹码数字太小。把 OBS 画布改成 1920x1080、手机源旋转 90° 铺满宽度，就能保留
    ~864x1920（参考尺寸的 ~88%），再用这里的 `--rotate` 把画面转回来。
    """
    table = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    if degrees not in table:
        raise ValueError(f"rotate 只支持 0/90/180/270，实际 {degrees}")
    code = table[degrees]
    return img if code is None else cv2.rotate(img, code)


def _camera_names() -> list[str]:
    """macOS 摄像头名称（按系统枚举顺序）。

    用 `system_profiler` 而不是猜索引：本机常见顺序是 FaceTime(0)、OBS
    虚拟摄像头(1)，但接外置摄像头后会变。名称里带 OBS / Virtual 的优先。
    """
    if sys.platform != "darwin":
        return []
    try:
        proc = subprocess.run(
            ["/usr/sbin/system_profiler", "SPCameraDataType", "-json"],
            capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            return []
        raw = json.loads(proc.stdout)
        entries = raw.get("SPCameraDataType") or []
        return [str(e.get("_name") or "") for e in entries if isinstance(e, dict)]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return []


def _camera_candidates() -> list[int]:
    """`cam:auto` 的候选索引：OBS 虚拟摄像头优先，其次 0..9。"""
    names = _camera_names()
    preferred = [
        i for i, name in enumerate(names)
        if "obs" in name.lower() or "virtual" in name.lower()
    ]
    return preferred + [i for i in range(10) if i not in preferred]


def _open_camera_index(index: int):
    """按索引打开摄像头：先 AVFoundation（虚拟摄像头只在这个后端暴露），
    失败再让 OpenCV 自选后端。"""
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        cap.release()
        return None
    # 摄像头后端大多忽略这个设置；读循环本身也只保留最新帧
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def _open_capture(url: str):
    """打开视频源。

    三种写法：

      · `rtsp://...` —— 走 FFmpeg 解 RTSP（OBS → SRT → MediaMTX 路线）。
      · `cam:auto` —— 自动选摄像头：名称带 OBS / Virtual 的优先，其次 0..9。
        这是默认值。
      · `cam:0` / `cam:1` —— 手动指定索引，用 `tools/list_cameras.py` 查。
      · `window:title:WePoker` / `window:index:1` / `window:x,y,w,h` ——
        直接截浏览器窗口，不经过 OBS，分辨率/构图和 profile 一致。

    `cam:` 优先 AVFoundation（macOS 虚拟摄像头只在这个后端暴露）；失败再让
    OpenCV 自选后端。
    """
    if url.startswith(("window:", "screen:")):
        _, _, raw = url.partition(":")
        try:
            cap = WindowCapture(raw)
        except (ValueError, RuntimeError) as exc:
            _log(f"[启动] 窗口源打不开：{exc}")
            return None
        # 先真截一帧：screencapture 失败（最常见是没给「屏幕录制」权限）时
        # 只会在 read() 里返回 False，上层看到的永远是「流中断」重连，
        # 根本不知道去系统设置里开权限。
        try:
            probe = cap.capture()
        except Exception as exc:        # noqa: BLE001 — 启动期给出明确原因
            _log(f"[启动] 窗口截图失败：{type(exc).__name__}: {exc}"
                 "（系统设置 → 隐私与安全性 → 屏幕录制，"
                 "勾上运行 Python 的终端，然后完全退出并重开它）")
            return None
        _log(f"[启动] 窗口源 {raw} → {cap.rect.to_dict()}"
             f"（{probe.shape[1]}x{probe.shape[0]}，screencapture 约 5-8fps）")
        return cap

    if url.startswith(("cam:", "camera:")):
        _, _, raw = url.partition(":")
        if raw.strip().lower() in ("", "auto"):
            for index in _camera_candidates():
                cap = _open_camera_index(index)
                if cap is not None:
                    names = _camera_names()
                    name = names[index] if index < len(names) else "?"
                    _log(f"[启动] cam:auto 选中摄像头 index={index}"
                         f"（{name or '未知名称'}）")
                    return cap
            return None
        try:
            index = int(raw)
        except ValueError:
            return None
        return _open_camera_index(index)

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    for name, value in (("CAP_PROP_OPEN_TIMEOUT_MSEC", 5000),
                        ("CAP_PROP_READ_TIMEOUT_MSEC", 5000)):
        prop = getattr(cv2, name, None)
        if prop is not None:
            cap.set(prop, value)
    return cap


# ─────────────────────────── 处理 ───────────────────────────

class LiveProcessor:
    """一帧包的完整处理：限速、断流重置、OCR、写 JSONL。单线程调用。"""

    def __init__(self, state_stream: StateStream | None, writer: JsonlWriter, *,
                 recognizer: str = "roi",
                 engine: OcrEngine | None = None,
                 full_config: FullTableConfig | None = None,
                 profile: Profile | None = None,
                 stable_frames: int = 2,
                 stable_seconds: float = 0.0,
                 stable_reset_on_discard: bool = True,
                 dealer_carry: int = 4,
                 every: float = DEFAULT_EVERY, max_frame_age: float = 2.0,
                 dump_dir: Path | None = None, quiet: bool = False,
                 hand_tracker: LiveHandTracker | None = None,
                 record_without_hero: bool = False,
                 advice_runner: AdviceRunner | None = None,
                 clicker: Clicker | None = None,
                 click_executor=None,
                 confirm_click: bool = False,
                 confirm_timeout: float = 6.0,
                 min_advice_confidence: float = 0.0,
                 cross_check_mode: str = "block",
                 table_dynamics_loader: Callable[[], TableDynamics] | None = None,
                 table_dynamics_text: str = "",
                 profile_store: ProfileStore | None = None,
                 hand_outbox: HandOutbox | None = None,
                 hand_ingest_worker=None,
                 incident_dir: Path | None = None,
                 incident_frames: int = 10,
                 incident_max: int = DEFAULT_INCIDENT_MAX,
                 card_confirm_frames: int = 2,
                 card_doubt_window: float = DEFAULT_DOUBT_WINDOW,
                 clock=time.monotonic, log=_log,
                 control: LiveControl | None = None,
                 preview: PreviewStore | None = None,
                 events: Callable[[str, dict], None] | None = None) -> None:
        if recognizer not in ("roi", "full"):
            raise ValueError(f"未知 recognizer：{recognizer}")
        if recognizer == "full" and (engine is None or full_config is None):
            raise ValueError("full 模式需要 engine 和 full_config")
        if recognizer == "roi" and state_stream is None:
            raise ValueError("roi 模式需要 state_stream")
        self.recognizer = recognizer
        self.state_stream = state_stream
        self.engine = engine
        self.full_config = full_config
        # 读牌只需要 profile 的 board / hero_cards 归一化槽位，不需要 OCR 引擎。
        # None 表示不读牌（单测里构造 LiveProcessor 时通常不传）。
        self.profile = profile
        self.full_recorder = (FullStateRecorder(
            stable_frames=stable_frames, stable_seconds=stable_seconds,
            stable_reset_on_discard=stable_reset_on_discard)
                              if recognizer == "full" else None)
        self.dealer_tracker = (DealerTracker(max_carry=dealer_carry)
                               if recognizer == "full" else None)
        self.writer = writer
        self.every = every
        self.max_frame_age = max_frame_age
        self.dump_dir = dump_dir
        self.quiet = quiet
        # AI 建议只在 roi 路线可用：全图路线读不到每座的「本街下注」，
        # 也没有 hero_turn，算不出跟注额与选项金额
        self.hand_tracker = hand_tracker
        # 没读到自己手牌的记录默认不写：上一手收牌后、下一手发牌前，追踪器会
        # 开一手没有 hero 手牌、也没有行动线的「空档手牌」，写下来就是每手牌
        # 后面多一条重复的空记录。旁观/观察对手时才打开。
        self.record_without_hero = bool(record_without_hero)
        self.hands_skipped_no_hero = 0
        self.advice_runner = advice_runner
        # 桌面自动点击：None = 不点击（默认）。点击器自己负责映射与随机坐标，
        # processor 只负责判断「这条建议是不是当前决策点、现在还能不能点」。
        self.clicker = clicker
        # 点击在哪个线程跑。默认 Inline：与改造前逐字节一致（测试/离线/回放）；
        # live 的 main() 会装配 ThreadClickExecutor，把 2–6s 的滑杆闭环挪出主循环。
        self.click_executor = click_executor or InlineClickExecutor()
        self._click_jobs: queue.Queue = queue.Queue()   # 工作线程 → 主循环
        self._click_inflight: _ClickJob | None = None
        self._click_wait_second = 0
        # 默认不询问（保持程序化调用/单测的旧行为）；CLI 默认开启询问，
        # 只有显式 `--no-confirm` 才直接执行。自动点击关闭时该开关无意义。
        self.confirm_click = bool(confirm_click)
        confidence = float(min_advice_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("min_advice_confidence 应在 0–1 之间")
        # 0 = 不启用。>0 时，模型给了置信度且低于阈值的建议只显示不自动点。
        self.min_advice_confidence = confidence
        # 模型动作与规则引擎严重冲突时的自动点击策略：off=不校验记录、
        # warn=只记录、block=不自动点击（默认）。
        if cross_check_mode not in ("off", "warn", "block"):
            raise ValueError(
                f"cross_check_mode 只能是 off/warn/block，收到 {cross_check_mode!r}")
        self.cross_check_mode = cross_check_mode
        # 终端确认最多等多久（秒）；0 = 一直等。默认 6s，避免把 OCR 主循环
        # 无限期挂在一次按键上（客户端回合时限通常 10–15s）。
        self.confirm_timeout = max(0.0, float(confirm_timeout))
        self._pending_decision_key: tuple | None = None
        # 一次「轮到我」只问一次模型：投递建议时上锁，直到 hero 真正行动
        # （hero_turn=False）、换街或换手才解开。拖动滑杆要好几秒，期间
        # 画面不变但回合还在；不锁就会反复投递、给出互相矛盾的建议
        # （实测 2026-11-10 22:17 同一 turn 连问「过牌」和「下注 1.33」，
        # 前一条建议还 stale，操作完全对不上）。
        self._advice_lock: tuple[str | None, str | None, int] | None = None
        self._clicked_keys: set[tuple] = set()
        self.click_count = 0
        self.click_skip_count = 0
        self.click_error_count = 0
        self.click_retry_count = 0
        self._click_retry: _ClickRetry | None = None
        # 建议已生成、但那一帧不能点（`frame_not_clickable` / `state_not_ok`）。
        # 这两种跳过是**瞬时**的：同一个决策点还在、回合也没结束，下一帧
        # 画面恢复就该补点。不补的话这条建议会被静默吞掉，hero 只能等超时
        # —— 2026-11-11 20:33 实盘：T9o 弃牌的建议正好落在 `stack:topL` 被
        # 质量闸丢掉的那一帧，`frame_not_clickable` 直接 skipped。
        self._deferred_click: _DeferredClick | None = None
        self.click_defer_count = 0
        # 返回 TableDynamics 的可调用对象；None = 不读牌桌动态（单测默认）。
        # 之所以传 loader 而不是一段固定文本：换桌后改 .table_dy 不必重启。
        self.table_dynamics_loader = table_dynamics_loader
        # 启动时已加载的文本：第一手不必再打一遍「已加载」；之后文件变化才提示。
        self._table_dynamics_text = table_dynamics_text
        self.hand_ingest_worker = hand_ingest_worker
        self._table_dynamics_error: str | None = None
        # 对手画像仓库：None = 关闭（--no-opponent-profile 或无 AI 建议）。
        self.profile_store = profile_store
        # 已结束手牌的本地队列：手牌库通过 outbox 异步取，不阻塞打牌。
        self.hand_outbox = hand_outbox
        self.profile_updates = 0
        # 决策点数（投递过几次）与真正拿到的建议数（模型/兜底都算）分开：
        # 旧 summary 的 advice_count 实际是前者，排查「投了但没回来」时会误读。
        self.advice_count = 0
        self.advice_done = 0
        # hand_id → 这一手已经给出的建议（按时间顺序）。换手 / 退出时并进
        # `hand` 记录，让「完整行动线 + 模型建议」落在同一个手牌记忆条目里，
        # 复盘时不用再按 hand_id 去 join 两条独立记录。
        self._hand_advice: dict[str, list[dict]] = {}
        # 同一手上一街给出的建议（喂回提示词的「上一街我的建议」段）。
        self._last_advice = None
        self.clock = clock
        self.log = log
        # 事件出口：main() 里接成「写 live.events.jsonl + 推 Web 看板」。
        # None = 不产生事件（单测/只跑识别时零开销）。回调自身必须吞异常，
        # 这里再包一层 _publish()，保证日志/看板永远不能拖垮 OCR。
        self.events = events
        # 工作台控制面：control 只控制「点击开关」，preview 只存最新帧引用。
        # 都为 None 时（CLI/单测默认）零开销，不改变任何识别口径。
        self.control = control
        self.preview = preview

        # 跨帧牌面稳定闸：board 只前进、hero 只接受 0/2 张、新读数 2 帧确认。
        # doubt_window > 0 时，带疑点的读数改走「疑惑帧」时间窗仲裁。
        # profile 为 None（单测桩）时不启用，_read_cards 直接返回桩结果。
        self.card_state = (StableCards(
            confirm_frames=card_confirm_frames,
            doubt_window=card_doubt_window)
            if profile is not None else None)
        # 牌区 ROI 去重：board/hero 像素没变时复用上一次的识别结果，只重读亮牌。
        self._card_roi_sig: tuple | None = None
        self._card_roi_reads: CardsResult | None = None
        # 看板最近一次收到的牌面 (hero, board)。质量闸丢帧时牌仍然可信，
        # 单独推一条 `cards` 事件，避免看板挂着上一手的牌（见 _publish_cards）。
        self._cards_published: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        # 疑惑帧环形缓冲：只存引用，不做逐帧编码；异常时才落盘。
        self.incident_dir = Path(incident_dir) if incident_dir is not None else None
        self._frame_ring: deque | None = (
            deque(maxlen=max(1, int(incident_frames)))
            if self.incident_dir is not None else None)
        # 疑惑节流：按类别记录「本次异常从什么时候开始」以及是否已经落过盘。
        # 持续几分钟的同一个异常只存一次，而不是每 5 秒重复存 10 帧。
        self.incident_max = max(0, int(incident_max))
        self.incident_count = 0
        self._incident_first_seen: dict[str, float] = {}
        self._incident_last_seen: dict[str, float] = {}
        self._incident_active: set[str] = set()
        self._incident_capped_logged = False
        self._last_cards_skip: tuple[tuple, float] | None = None
        # 不同原因的「跳过建议」节流：reason → (key, 时间)
        self._skip_logged: dict[str, tuple[tuple, float]] = {}
        self._board_anomaly_hinted = False
        self.cards_unreliable_skips = 0
        # 下注读数被几何闸降级成未知：节流打印的「错误日志」状态 + 计数
        self._last_bet_degrade: tuple[tuple, float] | None = None
        self.bet_degrade_count = 0

        # 质量闸丢帧的观测状态：同一类原因节流打印，每类最多存一帧样本。
        self._gate_sig: tuple[str, ...] | None = None
        self._gate_logged_at = 0.0
        self._gate_seen: set[tuple[str, ...]] = set()
        self._gate_dumped: set[tuple[str, ...]] = set()
        self._last_gate_dump = 0.0
        self.gate_dumps = 0
        self.last_gate_dump: str | None = None
        self._zero_accept_warned = False
        # 最近一帧原图（数组引用，不拷贝）。点击前用它按文字现找按钮位置：
        # 客户端的操作行有多个版面，固定槽位只对其中一种成立。
        self._last_image = None

        self.started_mono = clock()
        self.last_epoch = 0
        self.processed = 0
        self.total_proc_ms = 0.0
        self.last_proc_ms = 0.0
        self._stale_logged = 0.0
        self._cards_error_logged = False
        # 上一次「生成中…」心跳报到第几秒（只在等待建议时使用）
        self._wait_beat_second = 0

        if dump_dir is not None:
            dump_dir.mkdir(parents=True, exist_ok=True)

    def handle(self, packet: FramePacket) -> str:
        now = self.clock()

        # 换了一次连接：ROI 缓存和跨帧状态全部作废，下一帧重新当基线
        if packet.epoch != self.last_epoch:
            if self.last_epoch != 0:
                self.log(f"[重连] epoch {self.last_epoch} → {packet.epoch}，重置状态机")
                if self.state_stream is not None:
                    self.state_stream.reset()
                elif self.full_recorder is not None:
                    self.full_recorder.reset()
                if self.dealer_tracker is not None:
                    self.dealer_tracker.reset()
                if self.hand_tracker is not None:
                    self.hand_tracker.reset()
                if self.card_state is not None:
                    self.card_state.reset()
                self._card_roi_sig = None
                self._card_roi_reads = None
                # 重连后旧决策点/已点击记录全部作废，避免拿断流前的建议误点
                self._pending_decision_key = None
                self._clicked_keys.clear()
                self._click_retry = None
            self.last_epoch = packet.epoch

        age = now - packet.grab_mono
        if age > self.max_frame_age:
            # 邮箱里躺太久的帧（刚断流/卡顿）不处理，避免把旧画面当新状态
            if now - self._stale_logged >= 5.0:
                self.log(f"[跳过] 陈旧帧 {age * 1000:.0f}ms（上限 "
                         f"{self.max_frame_age * 1000:.0f}ms）")
                self._stale_logged = now
            if self.events is not None:
                hand = self._current_hand()
                self._publish("frame", {
                    "seq": packet.seq, "epoch": packet.epoch,
                    "frame_id": None, "ts": _iso_now(),
                    "t": round(now - self.started_mono, 2),
                    "queue_ms": round(age * 1000, 1), "proc_ms": 0.0,
                    "kind": "stale", "errors": ["stale_frame"],
                    "cards": {"reliable": False,
                              "warnings": ["stale_frame"]},
                    "hand_id": (hand or {}).get("hand_id"),
                    "street": (hand or {}).get("street"),
                    "hand": hand,
                    "processed": self.processed, "sampled": self.sampled,
                    "accepted": self.accepted, "records": self.records,
                    "avg_proc_ms": round(self.avg_proc_ms, 1),
                    "advice_count": self.advice_count,
                    "advice_done": self.advice_done,
                })
            return "stale"

        if self.dump_dir is not None:
            stamp = datetime.now().strftime("%H%M%S_%f")
            name = (self.dump_dir /
                    f"e{packet.epoch}_{packet.seq:06d}_{stamp}.jpg")
            cv2.imwrite(str(name), packet.image,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])

        t = now - self.started_mono
        frame_id = f"live_e{packet.epoch}_{packet.seq:06d}_{t:07.2f}s"
        # 只存引用，不拷贝也不编码：点击时按文字定位按钮要用同一帧。
        self._last_image = packet.image
        self._publish_preview(packet, t, frame_id)
        # 疑惑帧环形缓冲只存数组引用，不逐帧编码；异常时才落盘。
        if self._frame_ring is not None:
            self._frame_ring.append((packet.seq, round(t, 2), frame_id,
                                     packet.image))
        t0 = self.clock()
        # 每帧读一次牌。纯 OpenCV，几个毫秒；失败不致命，记一次日志后继续。
        cards = self._read_cards(packet.image)
        cards_sig = self._cards_signature(cards)
        if self.recognizer == "full":
            self._check_card_incident(packet, t, frame_id, cards)
            return self._handle_full(packet, t, frame_id, age, t0,
                                     cards, cards_sig)
        outcome = self.state_stream.ingest(
            packet.image, frame_id=frame_id, t=t,
            extra_signature=cards_sig)
        # 换手信号（庄位轮换 / 牌面归零确认 / hero 换牌 / All-in 收手）：
        # 稳定闸的 last-good 属于上一手，必须清掉并重读这一帧。否则新手会
        # 带着上一手的 board 开局，下一帧读到 0 又会被手牌追踪判成一次换手。
        if (getattr(outcome, "new_hand", False)
                and self.card_state is not None):
            self.card_state.reset()
            self._card_roi_sig = None
            self._card_roi_reads = None
            cards = self._read_cards(packet.image)
            cards_sig = self._cards_signature(cards)
        self._check_card_incident(packet, t, frame_id, cards)
        if outcome.kind == "discard":
            self._note_gate_discard(packet, outcome, t)
        proc_ms = (self.clock() - t0) * 1000
        self.processed += 1
        self.total_proc_ms += proc_ms
        self.last_proc_ms = proc_ms

        body = None
        if outcome.record is not None:
            body = _state_body(outcome.record, cards, "roi")
            self.writer.write({
                "type": "state",
                "seq": packet.seq,
                "epoch": packet.epoch,
                "ts": _iso_now(),
                "queue_ms": round(age * 1000, 1),
                "proc_ms": round(proc_ms, 1),
                **body,
            })
            if not self.quiet:
                state = outcome.state
                self.log(_state_line(
                    t, body,
                    f"  pot={state.pot} pbs={state.pot_before_street} "
                    f"轮到我={state.hero_turn}" + _name_tail(body)))
        # 丢弃 / 无变化的帧不会进 JSONL，属于「没被使用」的图片：不逐帧打印，
        # 只保留记录状态的行（丢弃原因在退出汇总里仍会打印）。
        # 注意 `_track_hand` 仍要跑：行动线/建议吃的是被接受的状态，不只是记录。

        # AI 建议：吃**每一帧被接受的状态**（不只被记录的）。签名里没有 action /
        # hero_turn，只吃 record 会漏掉大部分行动线。
        self._track_hand(outcome, cards)
        if body is None:
            # 这帧没产生记录（丢帧/无变化），但牌可能已经换了一组：
            # 牌有自己的闸（cards.reliable + 稳定帧），不该被底池/筹码的
            # 一票否决连坐（2026-11-08 实盘：换手后 164 帧里 112 帧被
            # `bet_gt_pot` 丢掉，看板 40s 都挂着上一手的牌）。
            self._publish_cards(cards)
        elif cards is not None:
            self._cards_published = (tuple(cards.hero_codes or ()),
                                     tuple(cards.board_codes or ()))
        self._publish_frame(packet, t, frame_id, age, proc_ms, outcome,
                            cards, body)
        return outcome.kind

    def _publish_preview(self, packet: FramePacket, t: float,
                         frame_id: str) -> None:
        """把最新已处理帧交给工作台预览；失败不影响识别。"""
        if self.preview is None:
            return
        try:
            self.preview.publish(packet.image, packet.seq,
                                 frame_id=frame_id, t=t)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[警告] 预览帧发布失败：{type(exc).__name__}: {exc}")

    def reset_pending_clicks(self, enabled: bool = False) -> None:
        """点击暂停时作废所有「待执行」的点击。

        暂停的含义是「从这一刻起不要动鼠标」：已经排队的补点/重试必须一起
        清掉，否则恢复前还会点出去；恢复后也不补旧决策（只在下一个新决策点
        重新生成建议）。`enabled` 只为打印一句可追溯的日志。
        """
        if enabled:
            return
        self._pending_decision_key = None
        self._deferred_click = None
        self._click_retry = None
        self.log("  [点击] 已暂停：作废待执行/补点/重试的点击，"
                 "恢复后只对新决策点生效")

    # ── 事件出口（Web / 事件日志） ─────────────────────

    def _publish(self, kind: str, payload: dict) -> None:
        """事件回调永远不能反噬识别主链路。"""
        if self.events is None:
            return
        try:
            self.events(kind, payload)
        except Exception as exc:  # noqa: BLE001 — 日志/看板故障不应中断牌局
            self.log(f"[警告] 事件发布失败：{type(exc).__name__}: {exc}")

    def _publish_cards(self, cards: CardsResult | None) -> None:
        """牌面/手牌单独推一条 `cards` 事件给看板。

        质量闸丢帧说的是「这帧的底池/筹码读不出来，牌局事实不能落库」，
        不等于牌读错了：牌有自己的一道闸（`cards.reliable` + 稳定帧确认）。
        不单独推的话，换手那几秒看板会一直挂着上一手的牌。

        只在牌真的变了才推；记录（body）已经带牌时由调用方同步
        `_cards_published`，不重复发。
        """
        if cards is None or not getattr(cards, "reliable", False):
            return
        sig = (tuple(cards.hero_codes or ()), tuple(cards.board_codes or ()))
        if sig == self._cards_published:
            return
        self._cards_published = sig
        # 不带 hand_id：丢帧时手牌追踪还停在上一手，带上反而是错的。
        self._publish("cards", {
            "hero_cards": list(sig[0]),
            "board": list(sig[1]),
            "captured_at": _iso_now(),
        })

    @staticmethod
    def _cards_event(cards: CardsResult | None) -> dict:
        if cards is None:
            return {"reliable": False, "warnings": ["cards_unavailable"],
                    "hero": [], "board": []}
        return {
            "reliable": bool(getattr(cards, "reliable", True)),
            "warnings": list(getattr(cards, "warnings", ()) or ()),
            "hero": list(getattr(cards, "hero_codes", ()) or ()),
            "board": list(getattr(cards, "board_codes", ()) or ()),
        }

    def _current_hand(self, max_actions: int = 30) -> dict | None:
        """当前这手牌的紧凑快照；只为看板/事件日志，不改变识别。"""
        hand = getattr(self.hand_tracker, "hand", None) if self.hand_tracker else None
        if hand is None:
            return None
        actions = list(getattr(hand, "actions", ()) or ())
        rows = [{
            "street": getattr(a, "street", None),
            "seat_id": getattr(a, "seat_id", None),
            "name": getattr(a, "name", None),
            "position": getattr(a, "position", None),
            "action": getattr(a, "action", None),
            "amount": getattr(a, "amount", None),
            "all_in": bool(getattr(a, "all_in", False)),
            "is_hero": bool(getattr(a, "is_hero", False)),
            "inferred": bool(getattr(a, "inferred", False)),
            "source": getattr(a, "source", None),
            "order_unknown": bool(getattr(a, "order_unknown", False)),
        } for a in actions[-max_actions:]]
        return {
            "hand_id": getattr(hand, "hand_id", None),
            "street": getattr(hand, "street", None),
            "hero_cards": list(getattr(hand, "hero_cards", ()) or ()),
            "board": list(getattr(hand, "board", ()) or ()),
            "pot": getattr(hand, "pot", None),
            "action_count": len(actions),
            "actions": rows,
        }

    def _publish_frame(self, packet: FramePacket, t: float, frame_id: str,
                       age: float, proc_ms: float, outcome, cards,
                       body: dict | None) -> None:
        """每一帧都发：看板据此显示「帧流」，state 只在有效记录时更新牌桌。"""
        if self.events is None:
            return
        hand = self._current_hand()
        payload = {
            "seq": packet.seq,
            "epoch": packet.epoch,
            "frame_id": frame_id,
            "ts": _iso_now(),
            "t": round(t, 2),
            "queue_ms": round(age * 1000, 1),
            "proc_ms": round(proc_ms, 1),
            "kind": outcome.kind,
            "errors": list(getattr(outcome, "errors", ()) or ()),
            "cards": self._cards_event(cards),
            "hand_id": (hand or {}).get("hand_id"),
            "street": (hand or {}).get("street"),
            "hand": hand,
            "state_index": (body or {}).get("index"),
            "processed": self.processed,
            "sampled": self.sampled,
            "accepted": self.accepted,
            "records": self.records,
            "avg_proc_ms": round(self.avg_proc_ms, 1),
            "advice_count": self.advice_count,
            "advice_done": self.advice_done,
        }
        if body is not None:
            payload["state"] = body
        self._publish("frame", payload)

    # ── 质量闸丢帧的现场 ─────────────────────────────────

    def _note_gate_discard(self, packet: FramePacket, outcome,
                           t: float) -> None:
        """一帧被质量闸丢掉：节流打印原因，必要时落一帧样本。

        只把原因留到退出汇总，会让「启动后只看到 [状态] 行」变成黑盒：
        用户不知道是哪几个字段没读出来。这里在丢帧当场说清楚，并把
        第一帧同类样本存进 `logs/incidents/…_gate_…`，方便直接看画面。
        """
        errors = list(outcome.errors or ())
        cats = (tuple(sorted({e.split(":", 1)[0] for e in errors}))
                or ("unknown",))
        now = self.clock()
        if (cats == self._gate_sig
                and now - self._gate_logged_at < GATE_LOG_SECONDS):
            return
        new_kind = cats not in self._gate_seen
        self._gate_seen.add(cats)
        self._gate_sig = cats
        self._gate_logged_at = now
        # 正常打牌时同类丢帧只报一次（状态行还是会提示）；一帧都没接受时
        # 每 5s 提醒一次，避免又回到「只剩 [状态] 行」的黑盒。
        if not self.quiet and (new_kind or self.accepted == 0):
            detail = "/".join(errors[:6]) if errors else "未知原因"
            if len(errors) > 6:
                detail += f" …另 {len(errors) - 6} 个"
            self.log(f"[丢弃] {detail}（累计 {self.sampled - self.accepted}"
                     f"/{self.sampled} 帧被丢）")
        if (cats not in self._gate_dumped
                and self.gate_dumps < GATE_DUMP_LIMIT
                and now - self._last_gate_dump >= GATE_DUMP_MIN_GAP):
            self._dump_gate_frame(packet, outcome, cats, t)

    def _dump_gate_frame(self, packet: FramePacket, outcome,
                         cats: tuple[str, ...], t: float) -> None:
        """把被质量闸丢掉的帧原样存下来：画面就是最直接的证据。"""
        if self.incident_dir is None:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder = self.incident_dir / f"{stamp}_gate_{'-'.join(cats)[:48]}"
        state = outcome.state
        try:
            folder.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(folder / "frame.jpg"), packet.image,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            (folder / "meta.json").write_text(json.dumps({
                "t": round(t, 2),
                "errors": list(outcome.errors or ()),
                "sampled": self.sampled,
                "accepted": self.accepted,
                "state": state.to_debug() if state is not None else None,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:          # 落盘失败不能拖垮识别
            self.log(f"[警告] 质量闸样本落盘失败：{exc}")
            return
        self._gate_dumped.add(cats)
        self._last_gate_dump = self.clock()
        self.gate_dumps += 1
        self.last_gate_dump = str(folder)

    def _track_hand(self, outcome, cards: CardsResult | None) -> None:
        """累积行动线；轮到我时把决策点投给后台建议线程。

        先取结果再投新决策点：上一回合的答案先落库，再排新的。自动点击的
        drain 放在 `observe()` 之后 —— 这时 hand_id / 决策点已经更新，能识别
        出「模型返回时这一手已经结束」的过期建议，不会拿旧建议误点。
        """
        if self.hand_tracker is None:
            # 没有手牌跟踪就没有 AI 建议；仍然把已完成的结果落库（不点击）。
            self.drain_advice(state=outcome.state, allow_click=False)
            return
        hero_codes = cards.hero_codes if cards is not None else []
        board = cards.board_codes if cards is not None else []
        shows = getattr(cards, "show_codes", {}) if cards is not None else {}
        # 亮牌不依赖 state.ok：摊牌帧常因筹码/底池被牌顶掉而整帧丢弃，
        # 但对手亮出的牌本身仍然可读。先挂到当前手牌，再做状态检查。
        # cards 为 None 表示这一帧读牌失败，不能据此清空「亮牌已消失」状态。
        if cards is not None:
            self.hand_tracker.record_shows(shows)
        if outcome.state is not None:
            self.hand_tracker.record_all_ins(outcome.state)

        # All-in 头像徽章消失 = 这手结束。主动收手后等真正的新手信号再开新手，
        # 避免把摊牌后的同一帧当新手牌开头。
        if getattr(outcome, "hand_end", False):
            self.hand_tracker.finish_current()
            # 这一手已经结束：结果只落库，绝不点击，也不再确认上一次点击。
            self._pending_decision_key = None
            self._click_retry = None
            self._deferred_click = None
            self.drain_advice(state=outcome.state, allow_click=False)

        if outcome.state is None or not outcome.state.ok:
            self._poll_click_results(state=outcome.state)
            self.drain_advice(state=outcome.state, allow_click=False)
            return
        # 先把已经完成的点击结果收回来（异步执行时它在工作线程里跑完）。
        # 放在最前面：结果一到就能落盘、挂「动作是否生效」的确认，不必等
        # 下一条建议。
        self._poll_click_results(state=outcome.state)
        # 上一次动作如果 0.5s 后画面还没变，在这里补点一次。放在 observe 之前：
        # 用刚识别出的帧判断，不受这一帧是否产生新决策点影响。
        self._service_click_retry(outcome.state)
        # 牌面不可靠（稳定结果仍出现 ?/低置信度/牌背/张数异常）时不给建议：
        # 错误手牌/公共牌上的 AI 结论比没有结论更糟。决策点不会被标记成
        # 「已建议」，等牌面恢复可靠、同一个决策点还会补投一次。
        cards_reliable = bool(getattr(cards, "reliable", True))
        cards_warnings = tuple(getattr(cards, "warnings", ()) or ())
        # 有座位的下注读到不可能的值（> 底池）时，这一帧的下注水位不可信：
        # 手牌追踪照常吃（别的字段都是好的），但**不给 AI 建议** ——
        # to_call 依赖最大下注，静默少算一位就会点错金额。
        bet_warnings = tuple(getattr(outcome.state, "bet_warnings", ()) or ())
        # 降级不是静默的：以前 `bet > 本街总额` 直接报错丢整帧，日志里只有
        # 一行「丢弃一帧 errors=[...]」；现在帧保留、只丢这一个字段，所以
        # 必须显式打一行警告，否则「这手为什么没建议」无从追查。
        if bet_warnings:
            self._log_bet_degraded(outcome.state, bet_warnings)
        # 上一次建议因为「这一帧不能点」被挂起：画面恢复了就补点。
        self._service_deferred_click(
            outcome.state, allow_click=cards_reliable and not bet_warnings)
        # 这一次「轮到我」是否已经投过建议：只有 hero 真的行动（这一街多出
        # 一条 is_hero 动作）、换街或换手才解锁。**hero_turn 单帧闪断不解锁**
        # ——拖动滑杆要好几秒，期间按钮/预设行被遮住，hero_turn 会抖；
        # 一抖就重新投递，就是 2026-11-10 22:17 那种「过牌/下注」来回改口。
        if self._advice_lock is not None:
            hand_now = self.hand_tracker.hand
            locked_hand, locked_street, locked_actions = self._advice_lock
            if hand_now is None or hand_now.hand_id != locked_hand \
                    or hand_now.street != locked_street:
                self._advice_lock = None
            else:
                try:
                    actions = hand_now.street_actions(locked_street)
                except Exception:        # noqa: BLE001 — 查不到就保持锁
                    actions = []
                hero_actions = sum(1 for a in actions if getattr(a, "is_hero", False))
                if hero_actions > locked_actions:
                    # hero 已经行动过：同一条街里只有在**面对下注**时才需要
                    # 再问一次（过牌后对手下注）。to_call=0 的「再过一次牌」
                    # 是发牌/按钮残留造成的假决策，不能重新投递。
                    hero_now = next(
                        (p for p in outcome.state.players
                         if p.seat_id == outcome.state.hero_seat_id), None)
                    max_other = max(
                        (p.bet or 0.0 for p in outcome.state.players
                         if p.seat_id != outcome.state.hero_seat_id),
                        default=0.0)
                    hero_bet = (hero_now.bet or 0.0) if hero_now else 0.0
                    if max_other - hero_bet > 0.01:
                        self._advice_lock = None
        point = self.hand_tracker.observe(
            outcome.state, hero_codes, board,
            cards_reliable=cards_reliable, cards_warnings=cards_warnings,
            allow_advice=(self.advice_runner is not None
                          and not getattr(self.advice_runner, "busy", False)
                          and self._advice_lock is None
                          and not bet_warnings))
        if (point is None and outcome.state.hero_turn is True
                and not cards_reliable):
            if self._log_cards_skip(outcome.state, cards_warnings):
                hand = self._current_hand() or {}
                self._publish("advice_skip", {
                    "hand_id": hand.get("hand_id"),
                    "street": hand.get("street"),
                    "reason": "cards_unreliable",
                    "warnings": list(cards_warnings),
                    "pot": outcome.state.pot,
                    "captured_at": _iso_now(),
                })
        elif (point is None and outcome.state.hero_turn is True
                and bet_warnings):
            if self._log_bet_skip(outcome.state, bet_warnings):
                hand = self._current_hand() or {}
                self._publish("advice_skip", {
                    "hand_id": hand.get("hand_id"),
                    "street": hand.get("street"),
                    "reason": "bet_unreadable",
                    "warnings": list(bet_warnings),
                    "pot": outcome.state.pot,
                    "captured_at": _iso_now(),
                })
        elif (point is None and outcome.state.hero_turn is True
                and self.hand_tracker.skip_reason):
            # 本街底池账单对不上（有人下注读小了 / 漏抓了行动）：to_call 会算小，
            # 宁可这一帧不给建议，也不推一条「只需跟 1BB」。
            reason = self.hand_tracker.skip_reason
            hand = self._current_hand() or {}
            key = (hand.get("hand_id"), hand.get("street"))
            if self._note_skip(reason, self.hand_tracker.skip_detail, key):
                self._publish("advice_skip", {
                    "hand_id": hand.get("hand_id"),
                    "street": hand.get("street"),
                    "reason": reason,
                    "detail": self.hand_tracker.skip_detail,
                    "pot": outcome.state.pot,
                    "captured_at": _iso_now(),
                })
        # 换手是在 observe 内部判定的：紧接着再 drain 一次，把上一手的
        # 手牌记忆（行动线 + 建议）在换手这一帧就落库，不必等下一帧。
        # 这里是**唯一允许自动点击**的 drain：state 与 hand_id 都是当前帧的，
        # 牌面不可靠 / 下注水位不可信时也不点（allow_click=False），
        # 避免错误手牌或错金额上的建议被执行。
        self.drain_advice(state=outcome.state,
                          allow_click=cards_reliable and not bet_warnings)
        if point is not None:
            self.advice_count += 1
            if self.advice_runner is not None:
                dynamics = ""
                if self.table_dynamics_loader is not None:
                    try:
                        loaded = self.table_dynamics_loader()
                    except Exception as exc:  # noqa: BLE001 — 动态文件不能拖垮主链路
                        loaded = TableDynamics(
                            error=f"{type(exc).__name__}: {exc}")
                    if loaded.error:
                        # 同一个错误只报一次，避免每个决策点刷屏
                        if loaded.error != self._table_dynamics_error:
                            self.log(f"  [建议] 牌桌动态读取失败：{loaded.error}")
                        self._table_dynamics_error = loaded.error
                    else:
                        self._table_dynamics_error = None
                        dynamics = loaded.text
                        if dynamics != self._table_dynamics_text:
                            if dynamics:
                                self.log(f"  [建议] 牌桌动态已加载/更新"
                                         f"（{len(dynamics)} 字）")
                            else:
                                self.log("  [建议] 牌桌动态已清空，"
                                         "回到「未知对手」标准线")
                            self._table_dynamics_text = dynamics
                # 本手在局对手的画像。没有画像时为空，build_situation 仍按
                # .table_dy 的 aggressive_table 走旧口径，不会退化。
                # 已弃牌的人不再参与当前决策，桌况/画像都不应再算他们。
                active_opponents = [o for o in point.opponents
                                    if not o.folded]
                reads: dict = {}
                if self.profile_store is not None:
                    try:
                        reads = self.profile_store.read_for(
                            active_opponents)
                    except Exception as exc:      # noqa: BLE001
                        self.log(f"  [对手] 画像读取失败："
                                 f"{type(exc).__name__}: {exc}")
                        reads = {}
                # 当前**这一手还在局**的对手聚合，不是整个画像库，也不是
                # 已弃牌的人。total_players 含 Hero，提示词才能说「3 人」。
                table_stats = None
                if self.profile_store is not None:
                    try:
                        table_stats = _table_stats_for_active(
                            self.profile_store, active_opponents)
                    except Exception as exc:      # noqa: BLE001
                        self.log(f"  [对手] 当前桌统计失败："
                                 f"{type(exc).__name__}: {exc}")
                # 一个坏快照不能把整条拉流链路带下去
                try:
                    previous = (self._last_advice
                                if self._last_advice is not None
                                and self._last_advice.hand_id == point.hand_id
                                else None)
                    situation = build_situation(
                        point, table_dynamics=dynamics,
                        opponent_reads=reads.values(),
                        table_stats=table_stats,
                        previous=previous,
                        include_strength=False)
                except Exception as exc:      # noqa: BLE001
                    self.log(f"  [建议] 局面构建失败：{type(exc).__name__}: {exc}")
                    return
                if self.advice_runner.submit(situation):
                    # 这一回合已经问过：上锁，直到 hero 真的行动/换街/换手。
                    try:
                        _acts = self.hand_tracker.hand.street_actions(
                            situation.street) if self.hand_tracker.hand else []
                    except Exception:        # noqa: BLE001
                        _acts = []
                    self._advice_lock = (
                        situation.snap.hand_id, situation.street,
                        sum(1 for a in _acts if getattr(a, "is_hero", False)))
                    # 只认最新投递的决策点；旧结果晚到会被 stale_decision 挡掉。
                    # 新决策点出现说明牌局已经往前走，上一次点击无需再重试。
                    self._pending_decision_key = situation.snap.decision_key
                    self._click_retry = None
                    # 看板/事件日志：先告诉前端「开始生成」，结果回来再补
                    # 完整提示词。决策点与 advice 用同一 hand_id/street 对齐。
                    self._publish("decision", {
                        "hand_id": situation.snap.hand_id,
                        "street": situation.street,
                        "hero_cards": list(situation.snap.hero_cards),
                        "board": list(situation.snap.board),
                        "pot": situation.snap.pot,
                        "to_call": round(call_amount(point), 2),
                        "action_index": situation.action_index,
                        "options": [o.to_dict() for o in situation.options],
                        "decision_key": list(situation.snap.decision_key),
                        "hand_tier": situation.hand_tier,
                        "hand_label": situation.hand_label,
                        "hand_class": situation.hand_playing_class,
                        "equity_pct": (round(situation.equity_pct, 1)
                                       if situation.equity_pct is not None
                                       else None),
                        "pot_type": situation.pot_type,
                        "spr": situation.spr,
                        "opponents_in_hand": situation.opponents_in_hand,
                        "warnings": list(situation.warnings or ()),
                        "prompt_version": PROMPT_VERSION,
                        "captured_at": _iso_now(),
                    })
                if not self.quiet:
                    pot_tag = ""
                    if situation.explosive_pot:
                        if situation.pot_per_player is not None:
                            pot_tag = (f"｜爆炸底池 "
                                       f"{situation.pot_per_player:.2f}BB/人")
                        else:
                            pot_tag = "｜爆炸底池（横幅）"
                        if point.bomb_pot:
                            pot_tag += "｜BOMB POT 横幅"
                    self.log(f"  [建议] 轮到我：{point.street} 手牌 "
                             f"{''.join(point.hero_cards) or '?'} "
                             f"牌面 {''.join(point.board) or '-'} "
                             f"需跟 {call_amount(point)}BB{pot_tag}（生成中…）")
            elif not self.quiet:
                self.log(f"  [建议] 轮到我：{point.street} 需跟 "
                         f"{call_amount(point)}BB（未开启 AI 建议）")

    def _log_cards_skip(self, state, warnings: tuple[str, ...]) -> bool:
        """牌面不可靠导致跳过建议：按手牌/街节流打印一次，并计数。

        返回 True 表示这次真的打印/计数了；调用方据此决定要不要发
        `advice_skip` 事件，避免同一段不可靠牌面每帧都刷一条。
        """
        hand = getattr(self.hand_tracker, "hand", None)
        key = (getattr(hand, "hand_id", None), round(state.pot or 0.0, 2))
        now = self.clock()
        if (self._last_cards_skip is not None
                and self._last_cards_skip[0] == key
                and now - self._last_cards_skip[1] < 5.0):
            return False
        self._last_cards_skip = (key, now)
        self.cards_unreliable_skips += 1
        reason = "；".join(warnings[:3]) or "牌面未稳定"
        self.log(f"  [建议] 跳过：牌面不可靠（{reason}）")
        return True

    def _log_bet_degraded(self, state, seats: tuple[str, ...]) -> bool:
        """有座位下注被几何闸降级成未知 → 打一行**警告**（按手/街/座位节流）。

        这是「降级」而不是「丢帧」之后的错误日志：帧仍然进手牌记录，但
        `to_call` 依赖的最大下注不可信，这一手不给建议、不自动点击。
        具体读到了什么（`55 > 本街总额 33.5`）写在 `FrameState` 的座位
        `notes` 里，这里把它原样带出来 —— 猜错 10 倍的金额必须留痕。
        """
        hand = getattr(self.hand_tracker, "hand", None)
        key = (getattr(hand, "hand_id", None),
               getattr(hand, "street", None), tuple(seats))
        now = self.clock()
        last = self._last_bet_degrade
        if last is not None and last[0] == key and now - last[1] < 5.0:
            return False
        self._last_bet_degrade = (key, now)
        self.bet_degrade_count += 1
        detail = "；".join(
            f"{p.seat_id}: {n}" for p in getattr(state, "players", ())
            if p.seat_id in seats for n in p.notes if "按未知处理" in n)
        if not detail:
            detail = f"{'/'.join(seats[:4])} 的下注超出几何上限"
        self.log(f"  [警告] 下注读数不可信，按未知处理：{detail}")
        self.log("         → 这一手不给建议、不自动点击（to_call 依赖最大下注）")
        return True

    def _log_bet_skip(self, state, seats: tuple[str, ...]) -> bool:
        """有座位下注读到不可能的值 → 跳过建议：按手牌/街节流打印一次。

        这一帧本身是有效的（别的字段都可用），跳过的是**建议/自动点击**：
        to_call 依赖最大下注，读数不可信时宁可不给，也不拿错金额去点。
        """
        hand = getattr(self.hand_tracker, "hand", None)
        key = (getattr(hand, "hand_id", None), getattr(hand, "street", None))
        who = "/".join(seats[:4]) or "未知座位"
        return self._note_skip(
            "bet_unreadable",
            f"下注读数不可信（{who} 的下注 > 底池 或 > 本街总额）", key)

    def _note_skip(self, reason: str, detail: str, key: tuple) -> bool:
        """同一手同一街同一原因的「跳过建议」只打一行、只发一条事件。"""
        now = self.clock()
        last = self._skip_logged.get(reason)
        if last is not None and last[0] == key and now - last[1] < 5.0:
            return False
        self._skip_logged[reason] = (key, now)
        self.log(f"  [建议] 跳过：{detail}")
        return True

    @property
    def waiting_for_advice(self) -> bool:
        """AI 建议正在生成：主循环应暂停识别，等结果回来再处理新帧。"""
        return self.advice_runner is not None and self.advice_runner.busy

    def log_advice_wait(self) -> None:
        """等模型时每秒打一行「生成中…」。

        等待期间主循环暂停 OCR，控制台本来会完全静默，模型返回后建议和恢复
        后的识别日志一起涌出来，看起来像「一堆一堆」。心跳把这段静默填成线性
        的时间线，也让人一眼看出慢在模型而不是识别。
        """
        runner = self.advice_runner
        if self.quiet or runner is None or not runner.busy:
            self._wait_beat_second = 0
            return
        second = int(runner.busy_seconds)
        if second < 1 or second == self._wait_beat_second:
            return
        self._wait_beat_second = second
        self.log(f"  [建议] 生成中… {second}s")

    def drain_advice(self, final: bool = False, *,
                     state=None, allow_click: bool = True) -> None:
        """把已完成的手牌/建议写进 JSONL 并打印。

        `final=True` 在退出时调用：最后一手牌等不到「换手」，也要落库。
        `state` 是当前帧的识别结果；只有 `allow_click=True` 且当前帧仍满足
        「轮到我 / 决策点未变 / 牌面可靠」时才会执行自动点击。

        顺序很重要：**先收建议、再写手牌**。换手那一帧要把这一手最后一条
        建议一起并进 `hand` 记录；反过来会漏掉同一帧刚返回的那条。
        """
        if self.advice_runner is not None:
            for advice in self.advice_runner.poll():
                data = advice.to_dict()
                self._hand_advice.setdefault(data["hand_id"], []).append(data)
                self.advice_done += 1
                self._last_advice = advice
                # **先给结论，再动手**：点击（尤其滑杆）要 2–6s，以前是先点完
                # 才 publish/log，用户看到建议时动作已经跑完或正在跑，体感就是
                # 「建议迟迟不出来、出来了又慢半拍」（2026-11-12 21:23 实测
                # 模型 1.3s、端到端 11.5s）。
                # 看板拿到的是**同一份** data（含 prompt_system/user、facts、
                # raw_response），所以展示的提示词与模型实际收到的一字不差。
                self._publish("advice", dict(data))
                self._log_advice(advice)
                # 结果由 _apply_click_result 落盘（同步路径当场补进 data["click"]，
                # 异步路径下一帧写一条 `click` 事件）。先把这一行 advice 写出去，
                # 点击结果晚到也不会顶着主循环。
                self._maybe_click(
                    advice, state=state,
                    allow_click=allow_click and not final, record=data)
                self.writer.write({"type": "advice", "ts": _iso_now(), **data})

        if self.hand_tracker is None:
            return
        finished = self.hand_tracker.take_finished()
        if finished is not None:
            self._write_hand(finished, partial=False)
            self._update_profiles(finished)
        if final:
            current = self.hand_tracker.take_current()
            if current is not None:
                self._write_hand(current, partial=True)

    def _update_profiles(self, hand: dict) -> None:
        """手牌结束后更新对手画像；终端打一行本手看到的类型。"""
        store = self.profile_store
        if store is None:
            return
        # 换手空档：没有 hero 手牌、也没有任何行动线，不是一手真实的牌。
        # 把它计入画像会把「没入池」当成一次 VPIP 机会，系统性压低 VPIP。
        if not hand.get("hero_cards") and not any(
                street.get("actions") for street in hand.get("streets") or []):
            return
        try:
            observations = store.update_hand(hand)
        except Exception as exc:                  # noqa: BLE001
            self.log(f"  [对手] 画像更新失败：{type(exc).__name__}: {exc}")
            return
        if not observations:
            return
        self.profile_updates += 1
        store.save()
        if self.quiet:
            return
        printed: set[str] = set()
        for obs in observations:
            if obs.seat_id is None or obs.seat_id in printed:
                continue
            printed.add(obs.seat_id)
            read = store.read_one(obs.display_name or obs.opponent_key,
                                  seat_id=obs.seat_id)
            if read.archetype == "unknown":
                continue
            self.log(f"  [对手] {format_read_line(read, obs.position)}")

    def _confirm_click(self, advice) -> bool:
        """在终端问一次是否执行：只认 y / n，其他按键忽略。

        默认（不加 `--no-confirm`）走这里：模型结果返回后，用户能看到建议、
        金额和耗时，再决定是否真的点。读取前会清空终端缓存的按键，输入流
        关闭/非交互环境按拒绝处理，避免自动化脚本在没有确认的情况下误点。
        """
        option = advice.option
        amount = f" {option.amount}BB" if option.amount is not None else ""
        prompt = (f"  [确认] {advice.street} 建议 {option.label}{amount}，"
                  f"是否执行？只识别 y / n（其他键忽略）：")
        self.log(prompt)
        return _ask_confirm(self.log, timeout=self.confirm_timeout)

    def _locate_click_targets(self) -> dict[str, tuple[float, float]]:
        """按当前画面里的**文字**找动作按钮 → {动作: 屏幕逻辑点}。

        客户端的操作行有多个版面（录屏实测：弃牌有时在最左 @566、有时在正中
        @736；过牌有时叫「让牌」、有时叫「免费」）。固定槽位只对其中一种成立，
        另一种会把点击送到促销位/毛毡上。这里每次点击前现找一次；
        找不到（不是操作行、没权限、OCR 失败）返回空 dict，调用方退回槽位。

        只在启用滑杆（web 桌面）时有意义：那时才有 profile 槽位和窗口映射。
        """
        slider = getattr(self.clicker, "bet_slider", None) if self.clicker else None
        mapper = getattr(slider, "mapper", None) if slider is not None else None
        engine = getattr(slider, "engine", None) if slider is not None else None
        img = self._last_image
        if mapper is None or engine is None or img is None:
            return {}
        try:
            found = find_action_buttons(img, engine)
        except Exception as exc:      # noqa: BLE001 — 定位失败只回退，不阻断
            self.log(f"  [点击] 按钮定位失败：{type(exc).__name__}: {exc}")
            return {}
        if not found:
            return {}
        h, w = img.shape[:2]
        return {name: mapper.point_from_image(cx / w, cy / h)
                for name, (cx, cy) in found.items()}

    def _sync_click_window(self) -> None:
        """窗口被拉高/拉窄后，重建按钮几何——否则所有点击按比例偏移。

        按钮槽位是**归一化**坐标，屏幕位置 = window.x + nx * window.w。
        window 只在启动时读一次；用户中途改了浏览器窗口（2026-11-11 实盘：
        标定的 838pt → 运行中 845pt → 当前 833pt）后，同一条建议会点到
        按钮外面。这里每次真正点之前校一次，变了就重建 ClickerConfig 和
        滑杆的 WindowMapper。解析失败/手填坐标时静默跳过，不阻塞主链路。
        """
        slider = getattr(self.clicker, "bet_slider", None) if self.clicker else None
        capture = getattr(slider, "frames", None) if slider is not None else None
        if capture is None or not hasattr(capture, "refresh"):
            return
        if not capture.refresh():
            return
        window = capture.effective_rect()
        profile = getattr(slider, "profile", None)
        if profile is None:
            return
        try:
            self.clicker.config = clicker_config_from_profile(
                profile, window, base=self.clicker.config)
            slider.mapper = WindowMapper(profile, window)
        except (ValueError, ClickerConfigError) as exc:
            self.log(f"  [点击] 窗口尺寸变化，但重建按钮失败：{exc}")
            return
        self.log(f"  [点击] 窗口尺寸变化，已重建按钮几何："
                 f"window={window.to_dict()}")

    def _click_paused(self) -> bool:
        """工作台是否把点击暂停了（没启用点击器时永远 False）。"""
        control = self.control
        if control is None:
            return False
        state = control.state()
        return bool(state.get("available") and not state.get("enabled"))

    def _maybe_click(self, advice, *, state, allow_click: bool,
                     record: dict | None = None):
        """执行自动点击；未启用点击器时返回 None（JSONL 不出现 click 键）。

        `confirm_click=True` 时，安全闸全部通过后还要在终端等一次 y/n；
        用户拒绝会返回 `skipped/user_rejected`，不会触碰鼠标。

        `record` 是这条建议要落盘的那份 dict：点击结果（尤其异步执行的）
        要补回同一个对象，手牌记录才会一起带上。

        返回：`ClickResult`（跳过 / 内联执行完）或 `None`（已交给工作线程，
        结果由 `_poll_click_results` 在下一帧落盘）。**所有安全闸都在这里
        同步跑完**，工作线程只负责驱动鼠标。
        """
        if self.clicker is None:
            return None
        if self._click_paused():
            result = ClickResult("skipped",
                                 action=getattr(advice.option, "kind", None),
                                 reason="click_paused")
            self._apply_click_result(advice, result, state=state, record=record)
            return result
        matched = bool(advice.decision_key) \
            and advice.decision_key == self._pending_decision_key
        reason = self._click_skip_reason(
            advice, state=state, allow_click=allow_click)
        if reason is not None:
            result = ClickResult("skipped", action=advice.option.kind,
                                 reason=reason)
            self._apply_click_result(advice, result, state=state,
                                     record=record)
            # 这一帧不能点不等于这条建议作废：牌面/下注读数抖一下、或者整帧
            # 被质量闸丢掉，下一帧就恢复正常。记下来补点，别把建议吞掉。
            if reason in DEFER_CLICK_REASONS and matched:
                # **不许清决策点**：`_service_click_retry` 用
                # `_pending_decision_key` 判断「这条挂起的建议是不是还针对
                # 同一个决策点」，清了它就等于下一帧立刻判定「决策点已变，
                # 取消补点」，挂起机制永远不生效（2026-11-11 20:33 T9o 那手
                # 就是这么把建议静默吞掉的）。
                self._defer_click(advice, reason)
            elif matched:
                self._pending_decision_key = None
            return result

        if self.confirm_click and not self._confirm_click(advice):
            result = ClickResult("skipped", action=advice.option.kind,
                                 reason="user_rejected")
            self._apply_click_result(advice, result, state=state, record=record)
            if matched:
                self._pending_decision_key = None
            return result

        # 安全闸全部过了：只有「驱动鼠标」这一段交给执行器。
        self._sync_click_window()
        job = _ClickJob(advice=advice, targets=self._locate_click_targets(),
                        record=record, submitted=self.clock(), state=state,
                        write_event=not self.click_executor.inline)
        self._click_inflight = job
        self.click_executor.submit(lambda: self._run_click_job(job))
        if matched:
            # 已经在执行了：立刻清决策点，别让下一帧重复 submit。
            self._pending_decision_key = None
        if self.click_executor.inline:
            results = self._poll_click_results(state=state)
            return results[-1] if results else None
        return None

    # ── 点击执行器 ────────────────────────────────────────────
    @property
    def click_in_flight(self) -> bool:
        """鼠标正在被驱动（滑杆打开着）。主循环据此暂停识别。"""
        return (self._click_inflight is not None
                or bool(getattr(self.click_executor, "busy", False)))

    def _run_click_job(self, job: _ClickJob) -> None:
        """工作线程里跑：只驱动鼠标，任何异常都封成 ClickResult 交回主线程。"""
        try:
            result = self.clicker.run(job.advice, targets=job.targets) \
                if self.clicker is not None \
                else ClickResult("error", reason="clicker_missing")
        except Exception as exc:        # noqa: BLE001 — 不能让工作线程死掉
            result = ClickResult(
                "error", action=getattr(job.advice.option, "kind", None),
                error=f"{type(exc).__name__}: {exc}")
        self._click_jobs.put((job, result))

    def _poll_click_results(self, *, state=None) -> list:
        """主线程取回已完成的点击结果并落盘（同步执行时当场就有一个）。"""
        results: list = []
        while True:
            try:
                job, result = self._click_jobs.get_nowait()
            except queue.Empty:
                break
            if self._click_inflight is job:
                self._click_inflight = None
            self._apply_click_result(
                job.advice, result,
                state=(job.state if job.state is not None else state),
                record=job.record, write_event=job.write_event)
            results.append(result)
        return results

    def log_click_wait(self) -> None:
        """点击在途时每秒打一行心跳，让「这段时间在干嘛」在时间线上可见。"""
        if not self.click_in_flight:
            self._click_wait_second = 0
            return
        job = self._click_inflight
        if job is None:
            return
        age = self.clock() - getattr(job, "submitted", self.clock())
        second = int(age)
        if second < 1 or second == self._click_wait_second:
            return
        self._click_wait_second = second
        action = getattr(job.advice.option, "kind", "?")
        self.log(f"  [点击] {action} 执行中… {second}s（滑杆期间不识别画面）")

    def _apply_click_result(self, advice, result, *, state=None,
                            record: dict | None = None,
                            write_event: bool = False) -> None:
        """把一次点击结果记到统计/JSONL/看板（同步与异步共用）。"""
        if record is not None:
            record["click"] = result.to_dict()
        if result.status in ("clicked", "dry_run"):
            self.click_count += 1
            if advice.decision_key:
                self._clicked_keys.add(advice.decision_key)
            if result.status == "clicked":
                if result.mapping == "bet_slider":
                    self.log("  [点击] 滑杆落注已确认关闭，不挂自动重试"
                             "（避免重复长按改额）")
                else:
                    self._arm_click_retry(advice, state)
        elif result.status == "error":
            self.click_error_count += 1
        else:
            self.click_skip_count += 1
        self._log_click(result)
        if not write_event or result.status == "skipped":
            # 同步执行时结果已经写进 advice 行的 `click`；跳过的事件不单独落盘
            # （否则每条被跳过的建议都会多一行）。
            return
        payload = {
            "type": "click",
            "ts": _iso_now(),
            "hand_id": getattr(advice, "hand_id", None),
            "decision_key": list(advice.decision_key or ()),
            **result.to_dict(),
        }
        self.writer.write(payload)
        # 看板：advice 先到、click 后到，前端把它并进当前建议（live_web 已支持）。
        self._publish("click", {"hand_id": payload["hand_id"],
                                "click": result.to_dict()})

    def _defer_click(self, advice, reason: str) -> None:
        """记住一条「这一帧不能点、下一帧补点」的建议。

        同一个决策点只挂一次，不因为连续几帧不可点而一直续命：窗口从
        第一次挂起开始算（`DEFER_CLICK_SECONDS`）。
        """
        pending = self._deferred_click
        if pending is not None and pending.advice is advice:
            return
        self._deferred_click = _DeferredClick(
            advice=advice, until=self.clock() + DEFER_CLICK_SECONDS)
        self.log(f"  [点击] 这一帧不能点（{reason}），保持待点，"
                 f"{DEFER_CLICK_SECONDS:g}s 内画面恢复就补点")

    def _service_deferred_click(self, state, *, allow_click: bool) -> None:
        """补点上一帧没能点出去的建议（只在画面恢复、仍是同一决策点时）。"""
        pending = self._deferred_click
        if pending is None:
            return
        if self._click_paused():
            self._deferred_click = None
            return
        if self.clock() >= pending.until:
            self._deferred_click = None
            self.log("  [点击] 补点窗口已过，放弃这条建议")
            return
        if state is None or not getattr(state, "ok", False):
            return
        if state.hero_turn is not True:
            self._deferred_click = None
            self.log("  [点击] 已不是我的回合，取消补点")
            return
        if pending.advice.decision_key != self._pending_decision_key:
            # 决策点已经变了（换街/换手/新的行动）：这条建议作废
            self._deferred_click = None
            self.log("  [点击] 决策点已变，取消补点")
            return
        if not allow_click:
            # 这一帧还是不能点（牌面/下注读数没恢复），下一帧再试
            return
        result = self._maybe_click(pending.advice, state=state,
                                   allow_click=True)
        if result is None:
            # 异步执行器：已经 submit 出去了。补点算成功，落到统计里；
            # 这次点击的明细由结果回到主线程时写的那条 `click` 事件承载。
            if self.click_in_flight:
                self.click_defer_count += 1
            self._deferred_click = None
            return
        if result.status in ("clicked", "dry_run"):
            self._deferred_click = None
            self.click_defer_count += 1
            self.writer.write({
                "type": "click_retry",
                "ts": _iso_now(),
                "hand_id": pending.advice.hand_id,
                "decision_key": list(pending.advice.decision_key),
                "attempt": 1,
                "deferred": True,
                "click": result.to_dict(),
            })
        elif result.status == "error" or result.reason not in DEFER_CLICK_REASONS:
            self._deferred_click = None

    def _arm_click_retry(self, advice, state) -> None:
        """点击成功后挂一个 0.5s 的「动作是否生效」确认。"""
        if self.clicker is None or self.clicker.dry_run or state is None:
            return
        cfg = self.clicker.config
        if cfg.retry_count <= 0:
            return
        self._click_retry = _ClickRetry(
            advice=advice,
            signature=action_signature(state),
            next_at=self.clock() + cfg.retry_delay_ms / 1000.0,
            remaining=cfg.retry_count,
        )
        self.log(f"  [点击] {cfg.retry_delay_ms / 1000:.1f}s 后确认动作是否生效"
                 f"（未变化最多重试 {cfg.retry_count} 次）")

    def _service_click_retry(self, state) -> None:
        """到点检查上一次点击：页面变了就收工，没变就再点一次。"""
        pending = self._click_retry
        if pending is None or state is None:
            return
        if self._click_paused():
            self._click_retry = None
            return
        now = self.clock()
        if now < pending.next_at:
            return
        if not state.ok:
            # 这一帧字段不可信，等下一帧再判；deadline 保持不动
            return
        if state.hero_turn is not True:
            self._click_retry = None
            self.log(f"  [点击] 已不是我的回合（hero_turn={state.hero_turn}），"
                     "动作已生效，取消重试")
            return
        if action_signature(state) != pending.signature:
            self._click_retry = None
            self.log("  [点击] 页面已变化，动作已生效，取消重试")
            return

        cfg = self.clicker.config
        attempt = cfg.retry_count - pending.remaining + 1
        self.log(f"  [点击] {cfg.retry_delay_ms / 1000:.1f}s 页面未变化，"
                 f"重试第 {attempt} 次")
        self._sync_click_window()
        result = self.clicker.run(pending.advice,
                                  targets=self._locate_click_targets())
        self.click_retry_count += 1
        self.writer.write({
            "type": "click_retry",
            "ts": _iso_now(),
            "hand_id": pending.advice.hand_id,
            "decision_key": list(pending.advice.decision_key),
            "attempt": attempt,
            "click": result.to_dict(),
        })
        self._log_click(result)
        remaining = pending.remaining - 1
        if result.status == "clicked" and remaining > 0:
            self._click_retry = _ClickRetry(
                advice=pending.advice,
                signature=pending.signature,
                next_at=self.clock() + cfg.retry_delay_ms / 1000.0,
                remaining=remaining,
            )
        else:
            self._click_retry = None

    def _click_skip_reason(self, advice, *, state,
                           allow_click: bool) -> str | None:
        """自动点击的安全闸。返回 None 才允许真的点。"""
        if advice.error:
            return "advice_error"
        if advice.source != "model" and not self.clicker.config.allow_fallback:
            return "fallback_advice"
        if self.min_advice_confidence > 0:
            # 没给 confidence = 无法验证，按不达标处理（fail closed）。提示词里
            # confidence 是可选字段；旧口径把「没给」当成「不受限」，这个开关
            # 对大多数回复等于没生效。
            if (advice.confidence is None
                    or advice.confidence < self.min_advice_confidence):
                return "low_confidence"
        if (self.cross_check_mode == "block"
                and getattr(advice, "cross_check", None) == "severe"):
            # 规则喊弃、模型喊加注/全下：不自动执行。这个原因**不**进
            # DEFER_CLICK_REASONS——冲突不会因为等下一帧而消失。
            return "cross_check_conflict"
        if not allow_click:
            return "frame_not_clickable"
        if state is None or not getattr(state, "ok", False):
            return "state_not_ok"
        if getattr(state, "hero_turn", None) is not True:
            return "hero_turn_false"
        if not advice.decision_key:
            return "missing_decision_key"
        if (self._pending_decision_key is None
                or advice.decision_key != self._pending_decision_key):
            return "stale_decision"
        hand = getattr(self.hand_tracker, "hand", None)
        current_hand_id = getattr(hand, "hand_id", None)
        if current_hand_id is not None and advice.hand_id != current_hand_id:
            return "stale_hand"
        # 街已经推进：preflop 的建议绝不能落到 flop 上执行。
        # 实测 2026-11-10 22:35：preflop 的建议在翻牌发出后才返回，hand_id
        # 相同、hero_turn=True，旧口径只比 decision_key，直接在 flop 点了「过牌」。
        current_street = getattr(hand, "street", None)
        if current_street and advice.street and advice.street != current_street:
            return "stale_street"
        if advice.decision_key in self._clicked_keys:
            return "already_clicked"
        return None

    def _log_click(self, result) -> None:
        if result.status == "skipped":
            reason = {"user_rejected": "用户拒绝",
                      "low_confidence": "置信度低于阈值",
                      "fallback_advice": "兜底建议",
                      "stale_street": "街已推进（旧街建议作废）"}.get(
                result.reason, result.reason)
            self.log(f"  [点击] 跳过：{reason}")
            return
        if result.status == "error":
            self.log(f"  [点击] 失败：{result.error}")
            return
        prefix = "dry-run" if result.status == "dry_run" else "已点击"
        if result.mapping == "bet_slider":
            detail = (result.detail or {}).get("slider", {})
            ev = detail.get("evidence") or {}
            if ev.get("all_in_text"):
                self.log(
                    f"  [点击] {prefix} {result.action} [bet_slider]："
                    f"顶部读到「{ev.get('allin_text') or 'ALL IN'}」，"
                    f"最后读数 {detail.get('final_bb')}BB")
                return
            changed = (f"拖动 {detail.get('drags')} 次"
                       if detail.get("control") == "drag"
                       else f"滚动 {detail.get('scrolls')} 格")
            if ev.get("requested_bb") is not None:
                changed += (f"（AI {ev['requested_bb']:g}BB → 按客户端粒度取 "
                            f"{ev.get('snapped_to_bb', result.target_bb):g}BB）")
            self.log(
                f"  [点击] {prefix} {result.action} [bet_slider]："
                f"建议 {result.target_bb}BB，实际 {detail.get('final_bb')}BB，"
                f"{changed}")
            return
        points = " → ".join(
            f"{s.button}({s.x:.1f},{s.y:.1f})" for s in result.steps)
        mapping = (f" [{result.mapping}]"
                   if result.mapping in ("direct", "nearest", "within_5pct")
                   else "")
        # 快捷档位/全下也把额度打出来：档位是 27→26.5 这种「在 5% 内」的替代，
        # 只打按钮名的话，复盘看不出实际落了多少。
        amounts = ""
        if result.final_bb is not None:
            amounts = f"（建议 {result.target_bb}BB → 实际 {result.final_bb}BB）"
        elif result.target_bb is not None:
            amounts = f"（建议 {result.target_bb}BB）"
        self.log(f"  [点击] {prefix} {result.action}{mapping}：{points}{amounts}")

    def _write_hand(self, hand: dict, *, partial: bool) -> None:
        """写一条手牌记忆：完整行动线 + 这一手给出过的全部建议。

        没有 hero 手牌的换手空档默认不写（见 `record_without_hero`）。
        """
        if not self.record_without_hero and not hand.get("hero_cards"):
            # 换手空档：上一手已经收牌、下一手还没发牌，追踪器会开一手没有
            # hero 手牌、也没有行动线的「手牌」。默认不落库，否则每手牌后面
            # 都会多一条重复的空记录；旁观/观察对手时用 --record-without-hero 打开。
            self._hand_advice.pop(hand.get("hand_id"), None)
            self.hands_skipped_no_hero += 1
            return
        advices = self._hand_advice.pop(hand.get("hand_id"), [])
        record: dict = {"type": "hand", "ts": _iso_now()}
        if partial:
            record["partial"] = True
        record.update(hand)
        # 数据质量是落库契约的一部分：复盘时先看这手能不能信，再看建议。
        inferred = sum(
            1 for street in record.get("streets") or []
            for action in street.get("actions") or []
            if action.get("inferred"))
        warnings = (list(record.get("data_warnings") or [])
                    + list(record.get("cards_warnings") or []))
        status = ("partial" if partial else
                  "approx" if (record.get("cards_unreliable")
                               or record.get("data_warnings")) else "exact")
        record.setdefault("data_quality", {
            "status": status,
            "warnings": warnings,
            "inferred_actions": inferred,
            "missing_fields": [],
            "cards_unreliable": bool(record.get("cards_unreliable")),
        })
        # 新记录带 schema/uid，手牌库可以直接按 uid 幂等入库；旧消费者
        # 会忽略多出来的键。
        record.setdefault("schema", "hand.v2")
        record.setdefault("uid", str(hand.get("hand_id") or ""))
        if advices:
            # 没有建议时连键都不出现，保持无 AI 模式下的输出与旧版一致
            record["advices"] = advices
        self.writer.write(record)
        # 看板/事件日志不需要再带一份建议全文（prompt 可能十几 KB）；
        # 建议本身已经在 advice 事件里，按 hand_id 能 join 回来。
        hand_payload = {k: v for k, v in record.items()
                        if k not in ("type", "advices")}
        hand_payload["advice_count"] = len(advices)
        self._publish("hand", hand_payload)
        if not partial and self.hand_outbox is not None:
            try:
                self.hand_outbox.enqueue(record)
                if self.hand_ingest_worker is not None:
                    self.hand_ingest_worker.kick()
            except OSError as exc:
                # 队列写失败不能拖垮牌局：JSONL 已经落盘，之后可以用
                # tools/sync_hand_outbox.py 从日志回填。
                self.log(f"  [手牌] outbox 写入失败：{exc}")
        if not partial:
            # 完整手牌落库后立刻在终端打一条黄色提示：AI 决策出问题时，
            # 用户能凭这行记住「hand_id」，再回 JSONL 按 id 定位。
            _log_hand_done(self.log, hand)

    def _log_advice(self, advice) -> None:
        option = advice.option
        amount = f" {option.amount}BB" if option.amount is not None else ""
        tail = f"（{advice.error}）" if advice.error else ""
        elapsed = f"耗时 {advice.duration_ms / 1000:.1f}s"
        reads = [r for r in getattr(advice, "opponent_reads", ())
                 if r.archetype != "unknown"]
        read_tag = ""
        if reads:
            # 全部在局对手都要露出来：只打前两家时，用户会以为其余座位没被
            # 识别（画像其实都有，是这行自己截断了），也没法核对模型看到的
            # 是不是同一批人。顺序沿用 advice.opponent_reads（快照座位序）。
            brief = "，".join(
                f"{r.display_name or r.key} {r.archetype.upper()} "
                f"{r.confidence:.2f}" for r in reads)
            read_tag = f"对手 {len(reads)} 家：{brief}"
        # 下一街计划：同一手牌下一街会回灌「My previous decision」，
        # 实时日志里也打出来，用户能看出模型打算怎么打这手牌。
        plan = (getattr(advice, "plan", "") or "").strip()
        plan_tag = f"｜计划 {plan}" if plan else ""
        source_tag = "模型" if advice.source == "model" else "兜底"
        prompt_meta = ""
        if advice.prompt_sha256:
            chars = (len(advice.prompt_system or "")
                     + len(advice.prompt_user or ""))
            prompt_meta = f"｜提示词 {advice.prompt_sha256[:8]} {chars}字"
        if advice.prompt_tokens is not None:
            prompt_meta += (f"｜tokens {advice.prompt_tokens}"
                            f"+{advice.completion_tokens or 0}")
        if not _use_ansi():
            reads_part = f"｜{read_tag}" if read_tag else ""
            self.log(f"  [建议:{source_tag}] {advice.street} ⇒ "
                     f"{option.label}{amount}"
                     f"｜{elapsed}{reads_part}{prompt_meta}"
                     f"｜{advice.reason}{plan_tag}{tail}")
            return
        # 模型结果只出现一次，别被滚动的识别日志淹没：
        # 留白 → 高亮横幅（街 + 动作 + 金额 + 耗时）→ 粗体理由 → 留白。
        bar = "═" * 60
        headline = (f" ▶ 建议  [{source_tag}] {advice.street} ⇒ "
                    f"{option.label}{amount}  ·  {elapsed} ")
        style = _advice_bar(option.kind)
        self.log("")
        self.log(f"{style}{bar}{_ANSI_RESET}")
        self.log(f"{style}{headline}{_ANSI_RESET}")
        self.log(f"{style}{bar}{_ANSI_RESET}")
        if read_tag:
            self.log(f"{_ANSI_ADVICE_TEXT} {read_tag}{_ANSI_RESET}")
        self.log(f"{_ANSI_ADVICE_TEXT} 理由：{advice.reason}{tail}{_ANSI_RESET}")
        if plan:
            self.log(f"{_ANSI_ADVICE_TEXT} 计划：{plan}{_ANSI_RESET}")
        if prompt_meta:
            self.log(f"{_ANSI_ADVICE_TEXT} {prompt_meta.lstrip('｜')}"
                     f"{_ANSI_RESET}")
        self.log("")

    def _read_cards(self, img: np.ndarray) -> CardsResult | None:
        """每帧读一次公共牌 / hero 手牌，再过跨帧稳定闸。

        没传 profile（单测）时不读。识别异常不让 worker 挂掉：记一次日志，
        这一帧按「没有牌」处理，后续帧继续尝试。

        性能：board/hero 两块 ROI 的灰度缩略图没变时，复用上一帧的识别结果，
        只重读亮牌（亮牌发生在别的 action_slot，不能一起复用）。这样动作区
        静止闸放行但牌面没动时，不再重复跑模板 NCC。
        """
        if self.profile is None:
            return None
        try:
            raw = self._recognize_cards_cached(img)
            # 仲裁窗口按真实时间走：读取慢/掉帧时窗口仍在正确的时间轴上。
            return (self.card_state.update(raw, now=self.clock())
                    if self.card_state else raw)
        except Exception as exc:  # noqa: BLE001 — 读牌失败不该中断拉流
            if not self._cards_error_logged:
                self.log(f"[警告] 读牌失败（后续帧继续尝试）：{exc}")
                self._cards_error_logged = True
            return None

    def _recognize_cards_cached(self, img: np.ndarray) -> CardsResult:
        """board/hero ROI 去重；没变时只重读亮牌。"""
        sig = self._card_roi_signature(img)
        cached = self._card_roi_reads
        if sig is not None and sig == self._card_roi_sig and cached is not None:
            shows = recognize_shown_cards(img, self.profile)
            return CardsResult(
                size=cached.size, board=cached.board,
                hero_cards=cached.hero_cards, shows=shows,
                warnings=list(cached.warnings), reliable=cached.reliable)
        raw = recognize_cards(img, self.profile)
        if sig is not None:
            self._card_roi_sig = sig
            self._card_roi_reads = raw
        return raw

    def _card_roi_signature(self, img: np.ndarray) -> tuple | None:
        """board/hero 两块牌区的灰度缩略图指纹；profile 不完整时返回 None。"""
        profile = self.profile
        if (profile is None or not hasattr(profile, "slot_to_px")
                or not hasattr(profile, "global_slots")):
            return None
        h, w = img.shape[:2]
        size = (w, h)
        slots = dict(DEFAULT_CARD_SLOTS)
        for key in ("board", "hero_cards"):
            slot = profile.global_slots.get(key)
            if slot is not None:
                slots[key] = slot
        out = []
        for key in ("board", "hero_cards"):
            x1, y1, x2, y2 = profile.slot_to_px(slots[key], size)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                return None
            gray = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            # 存 bytes 而不是 ndarray：tuple 里放 ndarray 时，`sig == prev`
            # 会触发 numpy 的「truth value ambiguous」。
            out.append(cv2.resize(gray, (32, 32),
                                  interpolation=cv2.INTER_AREA).tobytes())
        return tuple(out)

    def _check_card_incident(self, packet: FramePacket, t: float,
                             frame_id: str,
                             cards: CardsResult | None) -> None:
        """牌面回退/张数异常/牌背时，把环形缓冲里的最近帧落盘做回归样本。

        只存「持续」的异常，且同一段持续异常只存一次：
          · 异常类别（board 张数 / board 读数不可靠 / hero …）要连续出现
            `INCIDENT_MIN_SECONDS` 才落盘 —— 发牌动画里的单帧误读不存；
          · 同类异常间隔超过 `INCIDENT_EPISODE_GAP` 才算新的一次，不再每 5 秒
            重复存一份；
          · 落盘后逐帧写进日志并清空环形缓冲，下一段异常不会重复存同一批帧；
          · 整个会话最多存 `incident_max` 份（0 = 不限制）。
        """
        ring = self._frame_ring
        if ring is None or self.card_state is None:
            return
        now = self.clock()
        # 先结束已经消失足够久的异常段，允许下一次真正复发时再存一份
        for cat, last in list(self._incident_last_seen.items()):
            if now - last >= INCIDENT_EPISODE_GAP:
                self._incident_last_seen.pop(cat, None)
                self._incident_first_seen.pop(cat, None)
                self._incident_active.discard(cat)

        anomalies = list(self.card_state.last_anomalies)
        if not anomalies:
            return
        reason = anomalies[0]
        cat = _incident_category(reason)
        first = self._incident_first_seen.setdefault(cat, now)
        self._incident_last_seen[cat] = now
        if cat in self._incident_active:
            return
        if now - first < INCIDENT_MIN_SECONDS:
            return
        if self.incident_max and self.incident_count >= self.incident_max:
            if not self._incident_capped_logged:
                self._incident_capped_logged = True
                self.log(f"[疑惑帧] 本次会话已保存 {self.incident_max} 份，"
                         "后续异常不再落盘（--incident-max 调整）")
            return
        self._incident_active.add(cat)
        self.incident_count += 1

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe = "".join(ch if ch.isalnum() else "_" for ch in reason)[:48]
        folder = self.incident_dir / f"{stamp}_{safe}"
        frames = list(ring)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            meta = {
                "reason": reason,
                "anomalies": anomalies,
                "seq": packet.seq,
                "epoch": packet.epoch,
                "t": round(t, 2),
                "frame_id": frame_id,
                "cards": {
                    "board": getattr(cards, "board_codes", None),
                    "hero_cards": getattr(cards, "hero_codes", None),
                    "reliable": getattr(cards, "reliable", None),
                    "warnings": list(getattr(cards, "warnings", []) or []),
                },
                "frames": [],
            }
            for i, (seq, ft, fid, image) in enumerate(frames):
                name = f"{i:02d}_e{packet.epoch}_{seq:06d}.jpg"
                cv2.imwrite(str(folder / name), image,
                            [cv2.IMWRITE_JPEG_QUALITY, 92])
                meta["frames"].append({"file": name, "seq": seq,
                                       "t": ft, "frame_id": fid})
            (folder / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8")
            self.log(f"[疑惑帧] {reason}；已保存 {len(frames)} 帧 → {folder}")
            self._publish("incident", {
                "reason": reason,
                "anomalies": anomalies,
                "folder": str(folder),
                "hand_id": (self._current_hand() or {}).get("hand_id"),
                "t": round(t, 2),
                "frame_id": frame_id,
                "frames": len(frames),
                "cards": meta["cards"],
            })
            # 逐帧写进日志，方便只翻日志就能定位是哪几帧出的问题。
            for i, item in enumerate(meta["frames"]):
                self.log(f"[疑惑帧]   {i:02d} seq={item['seq']} "
                         f"t={item['t']:.2f}s {item['frame_id']} "
                         f"→ {item['file']}")
            # 这批帧已经落盘并记进日志：清空环形缓冲，下一段异常不会再把这
            # 批帧重复存一遍，同时及时释放它们占着的图像内存。
            ring.clear()
            if "board" in reason and not self._board_anomaly_hinted:
                self._board_anomaly_hinted = True
                self.log("  [提示] 连续出现牌面张数异常时，先确认输入画面确实是"
                         "牌桌：tools/list_cameras.py 的预览图不能是摄像头/桌面；"
                         "OBS 虚拟摄像头还要检查旋转（--rotate 90）和宽高比。")
        except Exception as exc:  # noqa: BLE001 — 落盘失败不能拖垮主链路
            self.log(f"[警告] 疑惑帧保存失败：{exc}")

    @staticmethod
    def _cards_signature(cards: CardsResult | None) -> tuple | None:
        """牌面指纹：公共牌 / hero 手牌 / 对手亮牌变化时触发一条新记录。"""
        if cards is None:
            return None
        base = (tuple(cards.hero_codes), tuple(cards.board_codes))
        shows = tuple(sorted((seat_id, tuple(read.codes))
                             for seat_id, read in getattr(cards, "shows", {}).items()))
        # 没有亮牌时保持旧指纹形状，避免所有帧的签名都多一个空维度
        return (*base, shows) if shows else base

    def _handle_full(self, packet: FramePacket, t: float, frame_id: str,
                     age: float, t0: float,
                     cards: CardsResult | None,
                     cards_sig: tuple | None) -> str:
        """全图 OCR 路线：不认识固定 profile，按文字/位置聚类。"""
        result = observe_full(packet.image, self.engine, self.full_config)
        if self.dealer_tracker is not None:
            result = self.dealer_tracker.apply(result)
        outcome: FullOutcome = self.full_recorder.push(
            result, t, frame_id, extra_signature=cards_sig)
        proc_ms = (self.clock() - t0) * 1000
        self.processed += 1
        self.total_proc_ms += proc_ms
        self.last_proc_ms = proc_ms

        body = None
        if outcome.record is not None:
            body = _state_body(outcome.record, cards, "full")
            self.writer.write({
                "type": "state",
                "seq": packet.seq,
                "epoch": packet.epoch,
                "ts": _iso_now(),
                "queue_ms": round(age * 1000, 1),
                "proc_ms": round(proc_ms, 1),
                **body,
            })
            if not self.quiet:
                carried = "（沿用）" if result.dealer_status == "carried" else ""
                self.log(_state_line(
                    t, body,
                    f"  桌型={result.table_size} "
                    f"庄位={result.dealer_seat_id}{carried}"))
        payload = {
            "seq": packet.seq, "epoch": packet.epoch, "frame_id": frame_id,
            "ts": _iso_now(), "t": round(t, 2),
            "queue_ms": round(age * 1000, 1),
            "proc_ms": round(proc_ms, 1), "kind": outcome.kind,
            "errors": list(getattr(outcome, "errors", ()) or ()),
            "cards": self._cards_event(cards),
            "hand_id": None, "street": None, "hand": None,
            "state_index": (body or {}).get("index"),
            "processed": self.processed, "sampled": self.sampled,
            "accepted": self.accepted, "records": self.records,
            "avg_proc_ms": round(self.avg_proc_ms, 1),
            "advice_count": self.advice_count,
            "advice_done": self.advice_done,
        }
        if body is not None:
            payload["state"] = body
        self._publish("frame", payload)
        if body is None:
            self._publish_cards(cards)
        elif cards is not None:
            self._cards_published = (tuple(cards.hero_codes or ()),
                                     tuple(cards.board_codes or ()))
        # 与 roi 路线一致：丢弃 / 无变化的帧不进 JSONL，不逐帧打印日志

        return outcome.kind

    @property
    def sampled(self) -> int:
        if self.full_recorder is not None:
            return self.full_recorder.sampled
        return self.state_stream.sampled  # type: ignore[union-attr]

    @property
    def accepted(self) -> int:
        if self.full_recorder is not None:
            return self.full_recorder.accepted
        return self.state_stream.accepted  # type: ignore[union-attr]

    @property
    def records(self) -> int:
        if self.full_recorder is not None:
            return self.full_recorder.records
        return self.state_stream.records  # type: ignore[union-attr]

    @property
    def discard_reasons(self) -> dict[str, int]:
        """累计丢弃原因（按字段计数，一帧可能有多个原因）。"""
        if self.full_recorder is not None:
            return dict(self.full_recorder.discarded)
        return dict(self.state_stream.discarded)  # type: ignore[union-attr]

    @property
    def idle_frames(self) -> int:
        """底池横幅显示「总底池」的手牌间隙帧（不是识别失败）。"""
        if self.full_recorder is not None:
            return int(getattr(self.full_recorder, "idle_frames", 0))
        return int(getattr(self.state_stream, "idle_frames", 0))

    def summary(self) -> dict:
        extra = {"cards_unreliable_skips": self.cards_unreliable_skips,
                 "bet_degrade_count": self.bet_degrade_count,
                 "hands_skipped_no_hero": self.hands_skipped_no_hero}
        if self.profile_store is not None:
            extra["hands_profiled"] = self.profile_updates
        if self.clicker is not None:
            extra.update({
                "clicks": self.click_count,
                "click_skips": self.click_skip_count,
                "click_errors": self.click_error_count,
                "click_retries": self.click_retry_count,
            })
        if self.full_recorder is not None:
            return {**self.full_recorder.summary(), "recognizer": "full", **extra}
        return {**self.state_stream.summary(), "recognizer": "roi",  # type: ignore[union-attr]
                **extra}

    @property
    def avg_proc_ms(self) -> float:
        return self.total_proc_ms / self.processed if self.processed else 0.0


# ─────────────────────────── CLI ───────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="实时拉虚拟摄像头/RTSP 流，按间隔识别牌桌状态并写 JSONL")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"视频源（默认 {DEFAULT_URL} = 自动选择 OBS 虚拟摄像头；"
                         "也可写 cam:1 指定索引，索引用 tools/list_cameras.py 查）。"
                         "走 MediaMTX 时写 rtsp://127.0.0.1:8554/live")
    ap.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0,
                    help="把视频源顺时针旋转多少度再识别（默认 0）。OBS 虚拟摄像头"
                         "固定 1920x1080，把 OBS 画布改成横屏、手机源转 90° 后，"
                         "这里用 --rotate 90 转回竖屏，能保住更高分辨率")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--initial-blinds", default=None,
                    help="盲注横幅读不到时的首帧盲注，如 1/2（之后走正常继承）")
    ap.add_argument("--lenient-chips", action="store_true",
                    help="筹码单位被读坏时取前导数字（roi 模式；固定筹码槽上下文下使用）")
    ap.add_argument("--recognizer", choices=("roi", "full"), default="roi",
                    help="roi=固定槽位（快）；full=全图 OCR+座位聚类（不依赖布局，慢）")
    ap.add_argument("--full-config", type=Path, default=None,
                    help="full 模式的参数 JSON（FullTableConfig 字段）")
    ap.add_argument("--every", type=float, default=DEFAULT_EVERY,
                    help="采样间隔秒（默认 0 = 不节流：处理完当前帧立即取最新帧，"
                         "中间帧丢弃）")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                    help="连续动作合并：动作区静止多少秒后才识别，只保留落定后的"
                         f"最新一帧（默认 {DEFAULT_SETTLE}；0 = 关闭，逐帧识别）")
    ap.add_argument("--settle-max", type=float, default=DEFAULT_SETTLE_MAX,
                    help="连续动作合并的最长等待秒数：动作一直不停时最多攒这么久"
                         f"就先识别一帧（默认 {DEFAULT_SETTLE_MAX}）")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"JSONL 输出（默认 {DEFAULT_OUT}；--test 时默认 "
                         f"{TEST_OUT}）")
    ap.add_argument("--append", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="追加到 --out，保留历次启动的手牌记录（默认）；"
                         "--no-append 每次启动清空")
    ap.add_argument("--test", action="store_true",
                    help="测试模式：按实战视频演练、不是实战。日志默认写 "
                         f"{TEST_OUT}（显式 --out 仍优先），手牌只留在日志里、"
                         "不写手牌 outbox（因此不会进手牌库）")
    ap.add_argument("--stable-frames", type=int, default=2,
                    help="新状态连续出现几帧才记录（默认 2：动画中间态/"
                         "单帧抖动不落库）")
    ap.add_argument("--stable-seconds", type=float, default=0.0,
                    help="新状态还要稳定多少秒才记录（默认 0 = 只看帧数；"
                         "帧率变化大时可设 0.6 固定时间窗）")
    ap.add_argument("--stable-reset-on-discard",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="质量闸丢弃的帧是否打断连续确认（默认是："
                         "否则跨动画的接受帧也会被算成连续）")
    ap.add_argument("--card-confirm-frames", type=int, default=2,
                    help="公共牌/hero 手牌新读数连续几帧一致才替换 last-good"
                         "（默认 2；牌面 4→3 回退永远不会被采用）")
    ap.add_argument("--card-doubt-window", type=float,
                    default=DEFAULT_DOUBT_WINDOW,
                    help="牌面读数带疑点（低置信度/遮挡等）又和 last-good 不同时，"
                         "开多少秒的疑惑帧仲裁窗口做多帧投票（默认 0.5；"
                         "0 = 关闭，退回 2 帧确认 + 10 帧弱读数兜底）")
    ap.add_argument("--incident-dir", type=Path, default=DEFAULT_INCIDENT_DIR,
                    help="牌面回退/张数异常/牌背时，把最近几帧存到这里做回归样本"
                         f"（默认 {DEFAULT_INCIDENT_DIR}）")
    ap.add_argument("--no-incident", action="store_true",
                    help="关闭疑惑帧留存（不占内存、不落盘）")
    ap.add_argument("--incident-frames", type=int, default=10,
                    help="疑惑帧环形缓冲保留多少帧（默认 10）")
    ap.add_argument("--incident-max", type=int, default=DEFAULT_INCIDENT_MAX,
                    help="单次会话最多落盘多少份疑惑帧（默认 "
                         f"{DEFAULT_INCIDENT_MAX}；0 = 不限制）")
    ap.add_argument("--max-vanish", type=int, default=3,
                    help="在场玩家整座空白连续几帧才认定离开（默认 3；"
                         "认定后本手保留名字/位置并按弃牌处理）")
    ap.add_argument("--name-confirm", type=int, default=3,
                    help="名字连续确认几帧才换（默认 3）")
    ap.add_argument("--dealer-carry", type=int, default=4,
                    help="full 模式 D 暂未读到时沿用上一庄位的最大帧数（默认 4；0=不沿用）")
    ap.add_argument("--max-frame-age", type=float, default=2.0,
                    help="超过多少秒的帧直接跳过（默认 2）")
    ap.add_argument("--crop", choices=("auto", "left", "none"), default=None,
                    help="画布裁剪：auto=按非黑内容自动裁（默认）；"
                         "left=按训练比例裁左侧；none=不裁")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="最多处理多少帧（调试用，0=不限）")
    ap.add_argument("--no-dedup", action="store_true", help="关掉 ROI 去重")
    ap.add_argument("--no-fold", action="store_true",
                    help="不读头像（弃牌 / All-in 徽章一起关，省 9 块 ROI）")
    ap.add_argument("--roi-threads", type=int, default=DEFAULT_ROI_THREADS,
                    help="帧内 ROI OCR 并行线程数（默认 "
                         f"{DEFAULT_ROI_THREADS}；1=串行）。"
                         "线程只并行同一帧里的多块 ROI，帧与帧之间仍串行")
    ap.add_argument("--dump-frames", type=Path, default=None,
                    help="把每个采样帧存成 JPEG，文件名带墙钟时间（量延迟用）")
    ap.add_argument("--ai-advice", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="轮到我时按当前街拼提示词并调用 AI 给一个选项"
                         "（默认开启；用 --no-ai-advice 关闭。需要 AI_SERVER_API_KEY / "
                         "AI_SERVER_BASE_URL / AI_SERVER_MODEL；仅 roi 模式）")
    ap.add_argument("--opponent-db", type=Path, default=None,
                    help="对手画像 JSON（默认读 OPPONENT_DB，否则 "
                         f"{DEFAULT_OPPONENT_DB}）。画像缺失/损坏时按无画像运行")
    ap.add_argument("--opponent-profile", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="按本手在局对手的本地历史画像调整建议（默认开启；"
                         "--no-opponent-profile 回退到 .table_dy）")
    ap.add_argument("--reset-opponents", action="store_true",
                    help="启动时清空本地对手画像（身份已全局合并，不再按池清）")
    ap.add_argument("--name-lexicon", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="用画像库里已知玩家的拼法纠正名字读数（默认开启；"
                         "--no-name-lexicon 只看当帧读数）")
    ap.add_argument("--ai-timeout", type=float, default=None,
                    help="单次 AI 建议的超时秒数（默认读 AI_SERVER_TIMEOUT，"
                         "否则 20）。实时链路建议 5–8 秒")
    ap.add_argument("--ai-confirm", type=int, default=1,
                    help="轮到我需要连续几帧确认（默认 1 = 第一帧就触发，"
                         "配合蓝色下注按钮；按钮会闪断的旧 UI 可调回 2）")
    ap.add_argument("--hand-outbox", type=Path, default=None,
                    help="已结束手牌的本地 outbox 目录（默认 "
                         f"{DEFAULT_HAND_OUTBOX}）。手牌库通过 "
                         "tools/sync_hand_outbox.py 异步取走。"
                         "--test 下不写 outbox，本参数不能与 --test 同时使用")
    ap.add_argument("--no-hand-outbox", action="store_true",
                    help="不写手牌 outbox（只保留 logs/live.states.jsonl）")
    ap.add_argument("--record-without-hero",
                    action=argparse.BooleanOptionalAction, default=None,
                    help="没读到自己手牌时也写入手牌记录（默认跳过换手空档"
                         "产生的空记录）。用于旁观/观察对手；也可用环境变量 "
                         "RECORD_WITHOUT_HERO=1 打开")
    ap.add_argument("--auto-click", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="AI 建议返回后自动点击桌面客户端的对应按钮"
                         "（默认开启；需要先跑 tools/calibrate_clicker.py。"
                         "用 --no-auto-click 关闭）")
    ap.add_argument("--no-confirm", action="store_true",
                    help="自动点击不再询问：模型建议返回后直接执行"
                         "（默认会在终端问 y 确认 / n 拒绝）")
    ap.add_argument("--clicker-config", type=Path, default=None,
                    help="自动点击的按钮坐标 JSON（默认 config/clicker_local.json）")
    ap.add_argument("--bet-slider", action="store_true",
                    help="下注/加注走「长按主按钮 → 滚动 → 确定」滑杆，"
                         "用 AI 的精确额度；需要 --auto-click 和窗口坐标")
    ap.add_argument("--bet-slider-window",
                    help="游戏画面窗口：index:N / title:... / owner:... / "
                         "x,y,w,h；先跑 tools/bet_slider.py --list-windows")
    ap.add_argument("--bet-slider-only", action="store_true",
                    help="下注/加注只走长按滑杆，不用任何快捷档位；"
                         "跳过 5%% 判定和置灰检测（需 --bet-slider）")
    ap.add_argument("--bet-slider-config", type=Path,
                    default=DEFAULT_BET_SLIDER_CONFIG,
                    help="滑杆操作参数 JSON（默认 config/bet_slider_mac.json）")
    ap.add_argument("--clicker-dry-run", action="store_true",
                    help="只打印将要点到的随机坐标，不发送真实鼠标事件")
    ap.add_argument("--confirm-timeout", type=float, default=6.0,
                    help="终端确认最多等几秒（0 = 一直等）；超时按拒绝处理，"
                         "避免把识别主循环挂在一次按键上")
    ap.add_argument("--min-advice-confidence", type=float, default=0.0,
                    help="自动点击的最低置信度（0 = 关闭）；低于该值或模型没给"
                         "confidence 时只显示建议，不自动点击")
    ap.add_argument("--cross-check", choices=("off", "warn", "block"),
                    default="block", dest="cross_check",
                    help="模型建议与规则引擎交叉校验：off=不校验、warn=只记录、"
                         "block=严重冲突（规则弃牌/模型加注全下）不自动点击"
                         "（默认 block；只影响点击，不影响落库/看板）")
    ap.add_argument("--quiet", action="store_true", help="只打状态行和最终汇总")
    # ── 日志 / Web 看板 ──
    ap.add_argument("--web", action=argparse.BooleanOptionalAction, default=True,
                    help="启动本地 Web 看板（默认开启，只绑定 127.0.0.1）。"
                         "看板每帧同步牌桌、AI 建议/原因和完整提示词；"
                         "--no-web 关闭")
    ap.add_argument("--web-host", default="127.0.0.1",
                    help="Web 看板监听地址（默认 127.0.0.1；改成 0.0.0.0 "
                         "会让同局域网的人也能看到提示词/玩家名，慎用）")
    ap.add_argument("--web-port", type=int, default=8020,
                    help="Web 看板端口（默认 8020；被占用时只告警，识别继续）")
    ap.add_argument("--web-open", action="store_true",
                    help="启动后自动用默认浏览器打开看板")
    ap.add_argument("--events-out", type=Path, default=None,
                    help="结构化事件日志路径（默认由 --out 派生："
                         "logs/live.states.jsonl → logs/live.events.jsonl）")
    ap.add_argument("--no-events", action="store_true",
                    help="不写结构化事件日志（看板仍可用）")
    # ── 工作台控制面（默认全关：不加这些参数时行为和旧版完全一致） ──
    ap.add_argument("--preview", action="store_true",
                    help="开启 GET /api/preview.jpg（最新已处理帧，给工作台看）")
    ap.add_argument("--control-token", default=None,
                    help="POST /api/control/click 的校验 token；不设则控制面关闭")
    ap.add_argument("--run-id", default=None,
                    help="工作台运行 id（写进元数据和日志，便于对账）")
    ap.add_argument("--log-file", type=Path, default=None,
                    help="控制台文本落盘路径（默认由 --out 派生："
                         "logs/live.states.jsonl → logs/live.log）")
    ap.add_argument("--no-log-file", action="store_true",
                    help="不把控制台文本写入文件")
    return ap


def apply_test_mode(args) -> None:
    """把 `--test` 落成具体行为：独立日志 + 手牌不进手牌库。

    测试模式用于「按实战视频演练」，牌局本身不是实战事实：

      · 日志默认写 `logs/live.test.jsonl`（显式 `--out` 优先），事件/控制台
        日志按同一规则派生成 `live.test.events.jsonl` / `live.test.log`，
        实战的 `live.states.jsonl` 不会被测试帧污染；
      · 关掉手牌 outbox：`LiveProcessor` 不再 enqueue，进程内嵌的
        `HandIngestWorker` 也不会启动，因此这些手牌既不会自动入库，也不会
        被 `tools/sync_hand_outbox.py` 收走。测试手牌仍完整留在测试日志里，
        需要复盘时显式读 `logs/live.test.jsonl`。
    """
    args.out = args.out or (TEST_OUT if args.test else DEFAULT_OUT)
    if args.test:
        args.no_hand_outbox = True


def _validate_args(ap: argparse.ArgumentParser, args) -> None:
    if args.every < 0:
        ap.error("--every 不能为负数（0 = 不节流）")
    if args.settle < 0:
        ap.error("--settle 不能为负数（0 = 关闭动作合并）")
    if args.settle_max < args.settle:
        ap.error("--settle-max 不能小于 --settle")
    if args.max_frame_age <= 0:
        ap.error("--max-frame-age 必须大于 0")
    if args.confirm_timeout < 0:
        ap.error("--confirm-timeout 不能为负数（0 = 一直等）")
    if args.stable_frames < 1:
        ap.error("--stable-frames 至少为 1")
    if args.card_confirm_frames < 1:
        ap.error("--card-confirm-frames 至少为 1")
    if args.card_doubt_window < 0:
        ap.error("--card-doubt-window 不能为负（0 = 关闭仲裁）")
    if args.incident_frames < 1:
        ap.error("--incident-frames 至少为 1")
    if args.incident_max < 0:
        ap.error("--incident-max 不能为负（0 = 不限制）")
    if args.max_vanish < 0:
        ap.error("--max-vanish 不能为负")
    if args.name_confirm < 1:
        ap.error("--name-confirm 至少为 1")
    if args.dealer_carry < 0:
        ap.error("--dealer-carry 不能为负")
    if args.max_frames < 0:
        ap.error("--max-frames 不能为负")
    if args.ai_advice and args.recognizer != "roi":
        ap.error("--ai-advice 只支持 --recognizer roi：全图路线读不到每座本街下注和轮到我")
    if args.ai_timeout <= 0:
        ap.error("--ai-timeout 必须大于 0")
    if args.ai_confirm < 1:
        ap.error("--ai-confirm 至少为 1")
    if not args.ai_advice:
        # 没有 AI 建议就没有可执行的行动，自动点击跟着关闭
        args.auto_click = False
    if args.clicker_dry_run and not args.auto_click:
        ap.error("--clicker-dry-run 需要配合 --auto-click 使用")
    if args.clicker_config is not None and not args.auto_click:
        ap.error("--clicker-config 需要配合 --auto-click 使用")
    if args.bet_slider and not args.auto_click:
        ap.error("--bet-slider 需要配合 --auto-click 使用")
    if args.bet_slider_only and not args.bet_slider:
        ap.error("--bet-slider-only 需要配合 --bet-slider 使用")
    if not 0.0 <= args.min_advice_confidence <= 1.0:
        ap.error("--min-advice-confidence 应在 0–1 之间")
    if args.roi_threads < 1:
        ap.error("--roi-threads 至少为 1（1 = 串行）")
    if not (1 <= args.web_port <= 65535):
        ap.error("--web-port 应在 1–65535 之间")
    if not str(args.web_host).strip():
        ap.error("--web-host 不能为空")
    if args.web_open and not args.web:
        ap.error("--web-open 需要开启 --web")
    if args.preview and not args.web:
        ap.error("--preview 依赖 Web 服务，需要同时开启 --web")
    if args.control_token is not None and not args.web:
        ap.error("--control-token 依赖 Web 服务，需要同时开启 --web")
    if args.control_token is not None and len(str(args.control_token)) < 8:
        ap.error("--control-token 至少 8 个字符（建议用随机串）")
    if args.no_events and args.events_out is not None:
        ap.error("--events-out 与 --no-events 不能同时使用")
    if args.no_log_file and args.log_file is not None:
        ap.error("--log-file 与 --no-log-file 不能同时使用")
    if args.test and args.hand_outbox is not None:
        ap.error("--test 不写手牌 outbox（测试手牌不进手牌库），"
                 "不能同时指定 --hand-outbox")


def _install_signals(stop: threading.Event) -> None:
    def handler(_signum, _frame):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _load_runtime_env(ap: argparse.ArgumentParser, args) -> object:
    """加载 .env、补齐环境变量默认值并做参数校验；返回 env_load。"""
    # `.env` 在任何配置读取之前加载：已存在的环境变量优先，文件只补齐。
    # 找不到文件不报错（全靠环境变量是合法部署方式），但 --ai-advice 下会告警 ——
    # 「.env 明明放了却没生效」是这类配置最难查的故障
    try:
        env_load = load_env()
    except EnvFileError as exc:
        ap.error(str(exc))
    if env_load.found:
        overridden = (f"，其中 {len(env_load.kept)} 项环境变量已存在、未覆盖"
                      if env_load.kept else "")
        _log(f"[启动] 已加载 .env：{env_load.path}（{len(env_load.applied)} 项{overridden}）")

    if args.ai_timeout is None:
        raw = (os.environ.get("AI_SERVER_TIMEOUT") or "").strip()
        try:
            args.ai_timeout = float(raw) if raw else 20.0
        except ValueError:
            ap.error(f"AI_SERVER_TIMEOUT 不是数字：{raw!r}")
    # 命令行没写时读 .env / 环境变量；显式 --no-record-without-hero 可以覆盖
    if args.record_without_hero is None:
        try:
            env_flag = _env_flag("RECORD_WITHOUT_HERO")
        except ValueError as exc:
            ap.error(str(exc))
        args.record_without_hero = False if env_flag is None else env_flag
    # --test 必须在 _validate_args / 派生日志路径之前落定：它决定 --out 默认值
    # 和是否写手牌 outbox（见 apply_test_mode）。
    apply_test_mode(args)
    _validate_args(ap, args)
    return env_load


@dataclass
class _Runtime:
    """main 装配好、主循环要用的运行时对象（避免 main 继续长参数）。"""
    started_mono: object
    mailbox: object
    reader: object
    processor: object
    writer: object
    web: object
    event_log: object
    console_log: object
    settle_gate: object
    advice_runner: object
    ingest_worker: object
    profile_store: object
    state_stream: object
    publish: object
    stop: object
    profile: object

def _build_runtime(ap: argparse.ArgumentParser, args, env_load) -> _Runtime:
    """装配日志/看板/OCR/建议/点击器/处理器；不启动拉流。"""
    started_wall = _iso_now()
    started_mono = time.monotonic()
    session_id = (datetime.now().strftime("%Y%m%d-%H%M%S")
                  + "-" + uuid.uuid4().hex[:6])

    # 控制台文本落盘：先挂上，后面所有 [启动]/[警告] 都会进文件。
    console_log = None
    if not args.no_log_file:
        console_path = args.log_file or derive_console_path(args.out)
        try:
            console_log = ConsoleLog(console_path, append=args.append)
            set_console_sink(console_log)
        except OSError as exc:
            _log(f"[警告] 控制台日志文件打不开（只打终端）：{exc}")

    # 结构化事件日志：识别排查的主索引，与 states 文件分开写。
    events_path = None
    event_log = None
    if not args.no_events:
        events_path = args.events_out or derive_event_path(args.out)
        try:
            event_log = LiveEventLog(events_path, session_id=session_id,
                                     started_mono=started_mono,
                                     append=args.append)
        except OSError as exc:
            _log(f"[警告] 事件日志打不开（只推看板）：{exc}")
            events_path = None

    _log(f"[启动] 会话 {session_id}")
    if console_log is not None:
        _log(f"[启动] 控制台日志 {console_log.path}")
    if event_log is not None:
        _log(f"[启动] 事件日志 {event_log.path}")
    if args.test:
        _log(f"[测试] 测试模式：日志写 {args.out}；"
             "手牌只留在测试日志，不进手牌库")

    if args.crop is None:
        args.crop = "auto"
    initial_blinds = None
    if args.initial_blinds:
        # 人工指定的盲注允许高比例（实测桌面有 1/20）；OCR 自动读数仍用默认上限
        initial_blinds = parse_blinds(args.initial_blinds, max_ratio=50.0)
        if initial_blinds is None:
            ap.error(f"--initial-blinds 解析不了：{args.initial_blinds}")

    # 必须在创建 VideoCapture 之前设置
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", FFMPEG_OPTIONS)

    stop = threading.Event()
    _install_signals(stop)

    profile = Profile.load(args.profile)      # 配置非法时内部 sys.exit
    _log(f"[启动] profile={profile.path.name} 座位={len(profile.seats)}")

    # Web 看板：只读展示。工作台额外挂 preview/meta/control 三个最小接口
    # （`--preview` / `--control-token`），默认关闭。端口被占用时只告警，
    # 识别主链路照常跑（日志里仍能查到 URL 启动失败的原因）。
    web = None
    preview = PreviewStore() if args.preview else None
    # 点击控制面始终存在（看板显示状态用），但没开点击器时 available=False，
    # 暂停/恢复都是 no-op。
    control = LiveControl(available=bool(args.auto_click),
                          dry_run=bool(args.clicker_dry_run))
    meta_state: dict = {"processor": None,
                        "window": _resolve_source_window(args.url)}
    session_meta = {
        "session_id": session_id,
        "run_id": args.run_id,
        "started_at": started_wall,
        "profile": profile.path.name,
        "profile_path": str(profile.path),
        "profile_sha256": _sha256_file(profile.path),
        "recognizer": args.recognizer,
        "test_mode": bool(args.test),
        "url": args.url,
        "source_kind": _source_kind(args.url),
        "out": str(args.out),
        "events_out": str(events_path) if events_path else None,
        "log_file": str(console_log.path) if console_log is not None else None,
        "prompt_version": PROMPT_VERSION if args.ai_advice else None,
        "prompt_lang": PROMPT_LANGUAGE if args.ai_advice else None,
        "preview": bool(args.preview),
        "control": bool(args.control_token),
        "pid": os.getpid(),
    }

    def live_meta() -> dict:
        """工作台用的运行时元数据：几何口径全部复用 profile 自己的换算。"""
        frame = preview.info() if preview is not None else {
            "seq": 0, "frame_id": None, "width": 0, "height": 0,
            "ts": None, "t": 0.0}
        w, h = int(frame.get("width") or 0), int(frame.get("height") or 0)
        processor = meta_state.get("processor")
        stats: dict = dict(web.store.stats) if web is not None else {}
        if processor is not None:
            stats.update({
                "processed": processor.processed,
                "accepted": processor.accepted,
                "records": processor.records,
                "avg_proc_ms": round(processor.avg_proc_ms, 1),
                "advice_count": processor.advice_count,
                "advice_done": processor.advice_done,
                "click_count": processor.click_count,
                "click_skip_count": processor.click_skip_count,
                "click_error_count": processor.click_error_count,
                "click_defer_count": processor.click_defer_count,
            })
        return {
            "run_id": args.run_id,
            "session_id": session_id,
            "started_at": started_wall,
            "profile": {
                "path": str(profile.path),
                "name": profile.path.name,
                "sha256": session_meta["profile_sha256"],
                "reference_size": [int(v) for v in profile.reference_size],
                "canvas": (list(profile.canvas)
                           if profile.canvas is not None else None),
                "hero_seat_id": profile.hero_seat_id,
                "seats": len(profile.seats),
            },
            "source": {
                "spec": args.url,
                "kind": _source_kind(args.url),
                "frame_size": [w, h] if w and h else None,
                "content_rect": (list(profile.content_rect((w, h)))
                                 if w and h else None),
                "window": meta_state.get("window"),
            },
            "recognizer": args.recognizer,
            "test_mode": bool(args.test),
            "frame": frame,
            "click": control.state(),
            "stats": stats,
            "layout": build_layout(profile),
            "prompt_version": PROMPT_VERSION if args.ai_advice else None,
            "ts": _iso_now(),
        }

    if args.web:
        try:
            web = LiveWebServer(session=session_meta,
                                layout=build_layout(profile),
                                host=args.web_host, port=args.web_port,
                                log=_log,
                                control=control,
                                preview=preview,
                                meta=live_meta,
                                token=args.control_token,
                                run_id=args.run_id)
            web.start()
            _log(f"[Web] 看板已启动：{web.url}"
                 + ("（工作台预览已开启）" if preview is not None else
                    "（只读；Ctrl-C 结束）"))
            if args.web_open:
                webbrowser.open(web.url)
        except OSError as exc:
            _log(f"[Web] 看板启动失败（识别继续，可改 --web-port）：{exc}")
            web = None

    # 统一事件出口：同一份事件先落 JSONL，再推看板。任何一边失败都不能
    # 反噬识别 —— 两个 sink 都包在 try 里。
    def publish(kind: str, payload: dict) -> None:
        if event_log is not None:
            try:
                event_log.write(kind, payload)
            except Exception as exc:  # noqa: BLE001
                _log(f"[警告] 事件日志写入失败：{type(exc).__name__}: {exc}")
        if web is not None:
            try:
                web.publish(kind, payload)
            except Exception as exc:  # noqa: BLE001
                _log(f"[警告] 看板推送失败：{type(exc).__name__}: {exc}")

    _log("[启动] 加载 OCR 模型…")
    engine = OcrEngine()
    _log("[启动] OCR 就绪")

    full_config = None
    state_stream = None
    if args.recognizer == "full":
        try:
            full_config = load_full_config(args.full_config)
        except ValueError as exc:
            ap.error(str(exc))
        _log(f"[启动] recognizer=full 配置={args.full_config or '默认'}")
    else:
        state_stream = StateStream(
            profile, engine,
            detect_fold=not args.no_fold,
            dedup=not args.no_dedup,
            stable_frames=args.stable_frames,
            stable_seconds=args.stable_seconds,
            stable_reset_on_discard=args.stable_reset_on_discard,
            max_vanish=args.max_vanish,
            name_confirm=args.name_confirm,
            initial_blinds=initial_blinds,
            lenient_chips=args.lenient_chips,
            ante_fallback=profile.default_ante,
            roi_threads=args.roi_threads,
        )
        _log(f"[启动] ROI 并行线程={args.roi_threads}"
             + ("（串行）" if args.roi_threads <= 1 else ""))
    _log(f"[启动] 状态记录：连续 {args.stable_frames} 帧"
         + (f" / ≥{args.stable_seconds:g}s" if args.stable_seconds > 0 else "")
         + ("，丢弃帧打断" if args.stable_reset_on_discard else "，丢弃帧不打断")
         + "（只影响 state 日志，不拖慢建议）")

    advice_runner = None
    hand_tracker = None
    clicker = None
    profile_store = None
    hand_outbox = None
    ingest_worker = None
    table_dynamics_text = ""
    # outbox 与 AI 建议解耦：即使这局没开 AI，手牌记录也应该能进手牌库。
    # 没有 hand_tracker 时不会产生手牌，这里建 outbox 只影响配置/日志。
    if not args.no_hand_outbox:
        hand_outbox = HandOutbox(args.hand_outbox or DEFAULT_HAND_OUTBOX)
        ingest_config = IngestConfig.from_env()
        if ingest_config.mode != "off":
            ingest_worker = HandIngestWorker(hand_outbox, ingest_config,
                                             log=_log)
            ingest_worker.start()
            _log(f"[手牌] outbox {hand_outbox.root}")
            _log(f"[手牌] 自动入库 {ingest_config.url}"
                 f"（{ingest_config.mode}，每 {ingest_config.interval:g}s，"
                 f"批量 {ingest_config.batch}）")
        else:
            _log(f"[手牌] outbox {hand_outbox.root}（自动入库已关闭）")
    # 手牌跟踪不依赖模型：关掉 AI 也要沉淀手牌/画像/outbox。模型只是
    # 「额外给一条建议」，不能让它决定有没有手牌库（P2-4）。
    hand_tracker = LiveHandTracker(confirm_frames=args.ai_confirm)
    _log("[手牌] 没读到自己手牌时："
         + ("也记录（观察对手）" if args.record_without_hero
            else "默认跳过换手空档（--record-without-hero 可开启）"))
    if args.ai_advice:
        if not env_load.found:
            _log("[建议] 没找到 .env（找过 "
                 + "、".join(str(p) for p in env_load.tried)
                 + "），改用环境变量")
        source = "dotenv" if "AI_SERVER_API_KEY" in env_load.applied else "env"
        try:
            settings = AISettings.from_env(source=source)
        except ValueError as exc:
            ap.error(f"AI 采样配置有误：{exc}")
        if settings is None:
            ap.error("--ai-advice 需要 AI_SERVER_API_KEY、AI_SERVER_BASE_URL、"
                     "AI_SERVER_MODEL（写在项目根目录的 .env 里，或直接设为环境变量）")
        turn_method = hero_turn_method(profile)
        if turn_method == "blue_button":
            hero_seat = next(
                (s for s in profile.seats if s.seat_id == profile.hero_seat_id), None)
            if hero_seat is None:
                ap.error("当前 profile 没有 hero 座位，判不出蓝色下注按钮，"
                         "AI 建议永远不会触发")
        elif "hero_turn_probe" not in profile.global_slots:
            # 静默不触发是最难查的故障：直接拒绝启动，让人现在就去补槽位
            ap.error(
                "当前 profile 没有 hero_turn_probe 槽位，判不出「轮到我」，"
                "AI 建议永远不会触发。请把 thresholds.hero_turn_method 设为 "
                "blue_button（用 hero 头像槽判蓝色下注按钮），或补上 hero_turn_probe")
        advice_runner = AdviceRunner(settings, timeout=args.ai_timeout, log=_log)
        _log(f"[建议] 提示词=中文 lean {PROMPT_VERSION}"
             f"（事实优先；无范围表/价值指令）")
        dynamics = load_table_dynamics()
        table_dynamics_text = dynamics.text
        if dynamics.found:
            _log(f"[建议] 已加载牌桌动态：{dynamics.path}"
                 f"（{len(dynamics.text)} 字）")
        elif dynamics.error:
            _log(f"[建议] 牌桌动态读取失败：{dynamics.error}")
        elif dynamics.exists:
            _log(f"[建议] {dynamics.path} 为空（只有注释），按「未知对手」标准线")
        else:
            _log(f"[建议] 未找到 {dynamics.path}，按「未知对手」标准线")
        if args.opponent_profile:
            initial_pool = (pool_key_from_blinds(initial_blinds,
                                                 len(profile.seats))
                            if initial_blinds is not None else "")
            db_path = (args.opponent_db
                       or (os.environ.get("OPPONENT_DB") or "").strip()
                       or DEFAULT_OPPONENT_DB)
            profile_store = load_profile_store(db_path, initial_pool)
            if profile_store.load_error:
                _log(f"[对手] 画像读取失败，按空画像运行："
                     f"{profile_store.load_error}")
            if args.reset_opponents:
                removed = profile_store.clear()
                profile_store.save(force=True)
                _log(f"[对手] 已清空本地画像（{removed} 人）："
                     f"{profile_store.path}")
            _log(f"[对手] 画像库 {profile_store.path}"
                 f"（最近池 {profile_store.pool_key or '未定（首手盲注后确定）'}，"
                 f"玩家 {len(profile_store.players)}，"
                 f"版本 {profile_store.classifier_version}）")
            if profile_store.players:
                known = sum(1 for p in profile_store.players.values()
                            if p.preflop_opps > 0)
                _log(f"[对手] 已有样本的玩家 {known} 人；"
                     f"n<{8} 只显示不改变建议")
            # 名字词表：拿「已认识的人」的拼法纠正当帧读数。置信度分不开对错
            # （错读和读对都在 0.87–0.97），但「这个名字以前见过」能分开。
            if args.name_lexicon and state_stream is not None:
                aliases = profile_store.alias_map()
                # 只拿「至少打过一手」的名字当锚：0 手的条目多是上一帧临时
                # 建出来的键，拿它做规范拼写会把错的固化下来。
                known = profile_store.known_names(min_hands=1.0)
                lexicon = NameLexicon(known, aliases)
                state_stream.set_name_lexicon(lexicon)
                _log(f"[名字] 词表 {len(lexicon)} 个已知拼法"
                     f"（别名 {len(aliases)} 条）；"
                     "读数命中已知玩家时按已知拼写记录")
                # 已经裂成两条的拼法（OCR 错读各记了几十手）运行时分不出来 ——
                # 两边都是「精确命中」。启动时点名，人去合并。
                dupes = duplicate_pairs(known, threshold=0.6)
                if dupes:
                    shown = "；".join(f"{a!r}≈{b!r}" for _, a, b in dupes[:3])
                    more = f" 等 {len(dupes)} 组" if len(dupes) > 3 else ""
                    _log(f"[名字] 画像里疑似同一个人的拼法：{shown}{more}"
                         "（确认后用 tools/name_alias.py --add 别名=规范名字 合并）")
            elif state_stream is not None:
                _log("[名字] 词表已关闭（--no-name-lexicon），只看当帧读数")
        else:
            _log("[对手] 已关闭（--no-opponent-profile），回退到 .table_dy")
        sampling = []
        if settings.thinking is not None:
            sampling.append(f"thinking={settings.thinking}")
        if settings.reasoning is not None:
            sampling.append("reasoning="
                            + ("enabled" if settings.reasoning else "disabled"))
        if settings.reasoning_effort is not None:
            sampling.append(f"effort={settings.reasoning_effort}")
        if settings.temperature is not None:
            sampling.append(f"temperature={settings.temperature:g}")
        if settings.max_tokens is not None:
            sampling.append(f"max_tokens={settings.max_tokens}")
        _log(f"[建议] 已启用 AI 建议：{settings.model}"
             f"（凭据来源 {settings.source}，超时 {args.ai_timeout:g}s，"
             f"连续 {args.ai_confirm} 帧确认，"
             + ("，".join(sampling) if sampling else "采样参数用服务端默认")
             + "）")

    if args.auto_click:
        cfg_path = args.clicker_config or DEFAULT_CLICKER_CONFIG
        try:
            clicker_cfg = ClickerConfig.load(cfg_path)
        except ClickerConfigError as exc:
            if args.bet_slider and not cfg_path.exists():
                # 滑杆模式不依赖 clicker_local：只用它的标量默认值，
                # buttons 稍后由 profile+window 生成。
                clicker_cfg = ClickerConfig(buttons={})
            else:
                ap.error(str(exc))
        if not args.bet_slider:
            clicker_cfg.validate()
            displays = list_displays()
            outside = [name for name, rect in clicker_cfg.buttons.items()
                       if not rect_inside_displays(rect, displays)]
            if outside:
                ap.error("点击配置里这些按钮不在当前显示器内："
                         + "、".join(outside)
                         + "。移动窗口 / 改分辨率或显示器排列后请重新运行 "
                           "tools/calibrate_clicker.py")
        if args.clicker_dry_run:
            backend = DryRunBackend()
        else:
            if sys.platform != "darwin":
                ap.error("--auto-click 目前只支持 macOS；"
                         "可先用 --clicker-dry-run 验证坐标")
            try:
                backend = CGEventBackend()
            except ClickerUnavailable as exc:
                ap.error(str(exc))
            if not backend.preflight():
                ap.error(
                    "--auto-click 没有「辅助功能」权限，鼠标事件会被系统丢弃。\n"
                    "去 系统设置 → 隐私与安全性 → 辅助功能，勾上运行 Python 的"
                    "终端 / iTerm / IDE，然后完全退出并重开它。\n"
                    "只想先验证坐标可加 --clicker-dry-run")
        slider = None
        if args.bet_slider:
            try:
                raw_slider = (json.loads(args.bet_slider_config.read_text(
                    encoding="utf-8"))
                    if args.bet_slider_config.is_file() else {})
            except (OSError, json.JSONDecodeError) as exc:
                ap.error(f"读不了滑杆配置 {args.bet_slider_config}：{exc}")
            window_text = args.bet_slider_window or raw_slider.get("window")
            if not window_text:
                ap.error(
                    "启用了 --bet-slider 但没给窗口坐标：先跑 "
                    "tools/bet_slider.py --list-windows，再填 "
                    "--bet-slider-window index:N / title:... / owner:... / "
                    f"x,y,w,h，或在 {args.bet_slider_config} 里填 window")
            capture_command = str(raw_slider.get(
                "capture_command",
                "screencapture -x -R {x},{y},{w},{h} {out}"))
            window_command = str(raw_slider.get(
                "window_capture_command",
                "screencapture -x -o -l {id} {out}"))
            try:
                capture = WindowCapture(window_text, capture_command,
                                        window_command=window_command)
            except (ValueError, RuntimeError) as exc:
                ap.error(f"滑杆窗口坐标不合法：{exc}")
            # 用**实际截图像素**反推窗口高度，而不是直接用 CGWindowList 的
            # bounds：后者按逻辑点取整，可能和画面差 1–2pt；norm→屏幕 的映射
            # 一旦用偏高的 h，y 越靠下偏移越大（预设行/滑杆手柄最先中招）。
            window = capture.effective_rect()
            if (abs(window.h - capture.rect.h) >= 0.5
                    or abs(window.w - capture.rect.w) >= 0.5):
                _log(f"[点击] 窗口几何已按截图校正："
                     f"bounds={capture.rect.to_dict()} → "
                     f"有效={window.to_dict()}")
            # 启用了滑杆就说明是 web 桌面：按钮坐标直接用 profile 槽位 +
            # window 计算，不再依赖 clicker_local 的手工绝对坐标。
            try:
                clicker_cfg = clicker_config_from_profile(
                    profile, window, base=clicker_cfg)
                clicker_cfg.validate()
            except (ValueError, ClickerConfigError) as exc:
                ap.error(f"从 profile 生成点击按钮失败：{exc}")
            displays = list_displays()
            outside = [name for name, rect in clicker_cfg.buttons.items()
                       if not rect_inside_displays(rect, displays)]
            if outside:
                ap.error("profile 计算出的按钮不在当前显示器内："
                         + "、".join(outside))
            try:
                slider_settings = SliderSettings.from_config(
                    raw_slider, source=args.bet_slider_config)
            except ValueError as exc:
                ap.error(f"滑杆配置有误：{exc}")
            slider = BetSliderController(
                profile, backend, capture,
                WindowMapper(profile, window), slider_settings,
                engine=engine, dry_run=args.clicker_dry_run, log=_log)
        clicker = Clicker(clicker_cfg, backend,
                          dry_run=args.clicker_dry_run, log=_log,
                          bet_slider=slider,
                          force_slider=args.bet_slider_only)
        confirm_mode = ("直接执行（--no-confirm）" if args.no_confirm
                        else "执行前在终端询问 y/n")
        source_label = ("profile+window" if args.bet_slider else str(cfg_path))
        _log(f"[点击] 已启用自动点击：{source_label}"
             f"（{len(clicker_cfg.buttons)} 个按钮，"
             + ("dry-run，不发送真实事件" if args.clicker_dry_run else "真实点击")
             + f"；{confirm_mode}；"
             f"{clicker_cfg.retry_delay_ms / 1000:.1f}s 未变化重试 "
             f"{clicker_cfg.retry_count} 次）")
        if slider is not None:
            how = ("按住橙色手柄上下拖" if slider.settings.control == "drag"
                   else "松手后上下滚动")
            _log(f"[点击] 下注/加注走长按滑杆：window={window.to_dict()}，"
                 f"长按 {slider.settings.hold_ms}ms，{how}，"
                 f"确认前必须读到目标额")
            _log("[点击] 按钮坐标由 profile+window 计算："
                 + "、".join(sorted(clicker_cfg.buttons)))
            if args.bet_slider_only:
                _log("[点击] --bet-slider-only：下注/加注只用长按滑杆，"
                     "跳过快捷档位")

    writer = JsonlWriter(args.out, append=args.append)
    session_start_record = {
        "type": "session_start",
        "session_id": session_id,
        "started_at": started_wall,
        "url": args.url,
        "profile": profile.path.name,
        "recognizer": args.recognizer,
        # 只有测试模式才写：复盘时一眼看出这批手牌不是实战、没有进手牌库。
        **({"test_mode": True} if args.test else {}),
        "full_config": str(args.full_config) if args.full_config else None,
        "every": args.every,
        # 日志防抖口径：复盘时能知道这批 state 记录是按几帧确认出来的。
        "stable": {"frames": args.stable_frames,
                   "seconds": args.stable_seconds,
                   "reset_on_discard": args.stable_reset_on_discard},
        "ai_advice": bool(args.ai_advice),
        **({"prompt_version": PROMPT_VERSION,
            "prompt_lang": PROMPT_LANGUAGE,
            "record_without_hero": bool(args.record_without_hero)}
           if args.ai_advice else {}),
        "auto_click": bool(args.auto_click),
        **({"clicker_config": str(args.clicker_config or DEFAULT_CLICKER_CONFIG),
            "clicker_dry_run": bool(args.clicker_dry_run),
            "click_confirm": not args.no_confirm}
           if args.auto_click else {}),
        "resolution": None,
        # 本会话生效的牌桌动态原文。没有就不写这个键，保持旧记录形状。
        **({"table_dynamics": table_dynamics_text} if table_dynamics_text else {}),
        **({"opponent_db": str(profile_store.path),
            "opponent_profile": True,
            "pool_key": profile_store.pool_key}
           if profile_store is not None else {"opponent_profile": False}),
        **({"hand_outbox": str(hand_outbox.root)}
           if hand_outbox is not None else {}),
        **({"hand_ingest": ingest_worker.health()}
           if ingest_worker is not None else {}),
        "hand_schema": "hand.v2",
        "table_view_mode": "current_hand",
        # 日志/看板入口写进 session_start：翻旧日志时能立刻找到对应事件文件。
        **({"events_out": str(events_path)} if events_path else {}),
        **({"web_url": web.url} if web is not None else {}),
        **({"log_file": str(console_log.path)}
           if console_log is not None else {}),
    }
    writer.write(session_start_record)
    publish("session_start",
            {k: v for k, v in session_start_record.items() if k != "type"})

    mailbox = LatestFrame()
    reader = RtspReader(args.url, mailbox, stop)
    processor = LiveProcessor(
        state_stream, writer,
        recognizer=args.recognizer, engine=engine, full_config=full_config,
        profile=profile,
        stable_frames=args.stable_frames,
        stable_seconds=args.stable_seconds,
        stable_reset_on_discard=args.stable_reset_on_discard,
        dealer_carry=args.dealer_carry,
        every=args.every, max_frame_age=args.max_frame_age,
        dump_dir=args.dump_frames, quiet=args.quiet,
        hand_tracker=hand_tracker, advice_runner=advice_runner,
        record_without_hero=args.record_without_hero,
        clicker=clicker,
        # 点击（尤其滑杆）要 2–6s：放工作线程跑，主循环在途期间让路。
        # 没启用自动点击时不需要线程（Inline 语义与改造前一致）。
        click_executor=(ThreadClickExecutor() if clicker is not None
                        else None),
        confirm_click=not args.no_confirm,
        confirm_timeout=args.confirm_timeout,
        min_advice_confidence=args.min_advice_confidence,
        cross_check_mode=args.cross_check,
        table_dynamics_loader=load_table_dynamics if args.ai_advice else None,
        table_dynamics_text=table_dynamics_text,
        profile_store=profile_store,
        hand_outbox=hand_outbox,
        hand_ingest_worker=ingest_worker,
        incident_dir=(None if args.no_incident else args.incident_dir),
        incident_frames=args.incident_frames,
        incident_max=args.incident_max,
        card_confirm_frames=args.card_confirm_frames,
        card_doubt_window=args.card_doubt_window,
        control=control, preview=preview,
        events=(publish if (event_log is not None or web is not None)
                else None))

    # 工作台「暂停点击」：开关一变就作废待执行/补点/重试的点击。
    control.add_listener(processor.reset_pending_clicks)
    meta_state["processor"] = processor

    # 动作区静止闸只用于 roi 路线：它的区域来自固定槽位；full 路线没有布局。
    settle_gate = (SettleGate(profile, settle=args.settle,
                              max_wait=args.settle_max)
                   if args.settle > 0 and args.recognizer == "roi" else None)
    return _Runtime(
        started_mono=started_mono,
        mailbox=mailbox,
        reader=reader,
        processor=processor,
        writer=writer,
        web=web,
        event_log=event_log,
        console_log=console_log,
        settle_gate=settle_gate,
        advice_runner=advice_runner,
        ingest_worker=ingest_worker,
        profile_store=profile_store,
        state_stream=state_stream,
        publish=publish,
        stop=stop,
        profile=profile,
    )


def _run_live(args, rt: _Runtime) -> int:
    """主循环与退出清理；顺序与拆分前保持逐行一致。"""
    started_mono = rt.started_mono
    mailbox = rt.mailbox
    reader = rt.reader
    processor = rt.processor
    writer = rt.writer
    web = rt.web
    event_log = rt.event_log
    console_log = rt.console_log
    settle_gate = rt.settle_gate
    advice_runner = rt.advice_runner
    ingest_worker = rt.ingest_worker
    profile_store = rt.profile_store
    state_stream = rt.state_stream
    publish = rt.publish
    stop = rt.stop
    profile = rt.profile

    _log(f"[启动] 拉流 {args.url}")
    cadence = (f"间隔 {args.every}s" if args.every > 0
               else "无节流（处理完当前帧立即取最新帧）")
    if settle_gate is not None:
        cadence += (f"；动作区静止 {args.settle}s 后识别"
                    f"（上限 {args.settle_max}s）")
    mode = "追加" if args.append else "覆盖"
    _log(f"[启动] 输出 {args.out}（{mode}）；{cadence}；Ctrl-C 结束")
    reader.start()

    def publish_status() -> None:
        """每 10s 把累计计数推给看板/事件日志（与终端 [状态] 行同源）。"""
        publish("status", {
            "elapsed_seconds": round(time.monotonic() - started_mono, 1),
            "frames_read": reader.grabbed,
            "processed": processor.processed,
            "sampled": processor.sampled,
            "accepted": processor.accepted,
            "records": processor.records,
            "avg_proc_ms": round(processor.avg_proc_ms, 1),
            "reconnects": reader.reconnects,
            "discarded_reasons": processor.discard_reasons,
            "idle_frames": processor.idle_frames,
            "advice_count": processor.advice_count,
            "advice_done": processor.advice_done,
            "cards_unreliable_skips": processor.cards_unreliable_skips,
            "bet_degrade_count": processor.bet_degrade_count,
        })

    last_seq = -1
    last_start = 0.0
    last_status = time.monotonic()
    status_base: dict[str, int] = {}
    last_shape: tuple[int, int] | None = None
    plan: CropPlan | None = None
    fit: AspectFit | None = None
    last_warn = 0.0

    try:
        while not stop.is_set():
            # 事件驱动：有新帧会立刻唤醒；timeout 只是「没帧时多久回来查一次」，
            # 不是采样间隔。默认不节流后把它调小，避免把空闲等待误当成延迟。
            packet = mailbox.wait_newer(last_seq, timeout=0.2)
            now = time.monotonic()
            processor.log_advice_wait()

            if packet is None:
                # 流断了 / 还没连上：定期提醒，不要每 0.5s 刷屏
                if (reader.connected_once and reader.last_grab_mono > 0
                        and now - reader.last_grab_mono > 3.0
                        and now - last_warn > 5.0):
                    _log(f"[警告] {now - reader.last_grab_mono:.1f}s 没有新帧"
                         f"（重连 {reader.reconnects} 次）")
                    last_warn = now
                if now - last_status >= 10.0:
                    status_base = _log_status(processor, reader, started_mono,
                                              status_base)
                    publish_status()
                    last_status = now
                continue

            if args.rotate:
                # 先转正再裁剪/缩放：OBS 虚拟摄像头输出横屏，牌桌是竖屏
                packet = FramePacket(
                    packet.seq, packet.epoch, packet.grab_mono,
                    rotate_frame(packet.image, args.rotate))

            shape = packet.image.shape[:2]          # (h, w)
            if shape != last_shape:
                h, w = shape
                plan = plan_crop(shape, profile, args.crop, image=packet.image)
                # ROI 坐标前提是「画面宽高比 = profile 参考宽高比」。
                # 裁剪后的画面再按参考比例重采样一次，x/y 缩放就一致了。
                fit = (AspectFit.for_size(plan.out_size, profile)
                       if args.recognizer == "roi" else None)
                last_shape = shape
                writer.write({"type": "stream_info", "ts": _iso_now(),
                              "recognizer": args.recognizer,
                              "resolution": [w, h],
                              "aspect": round(w / h, 6) if h else None,
                              "reference_aspect":
                                  round(profile.reference_size[0]
                                        / profile.reference_size[1], 6),
                              "crop": plan.to_debug(),
                              "roi_fit": fit.to_debug() if fit else None})
                if plan.rect is not None:
                    _log(f"[裁剪] {w}x{h} → {plan.out_size[0]}x{plan.out_size[1]}"
                         f"（{plan.mode}）")
                if plan.warning:
                    _log(f"[警告] {plan.warning}")
                if fit is not None:
                    _log(f"[比例] 流 {w}x{h}（{w / h:.4f}）→ 画面 "
                         f"{plan.out_size[0]}x{plan.out_size[1]}"
                         f"（{fit.aspect:.4f}）→ ROI "
                         f"{fit.out_size[0]}x{fit.out_size[1]}"
                         f"（参考 {fit.reference_aspect:.4f}，"
                         f"缩放 {fit.scale[0]:.4f}/{fit.scale[1]:.4f}）")
                    if fit.deviation > 0.02:
                        hint = ""
                        if args.url.startswith(("cam:", "camera:")):
                            hint = ("。虚拟摄像头固定输出 1920x1080：若 OBS 里手机源"
                                    "旋转了 90°，加 --rotate 90；否则请把 OBS 画布"
                                    "调成与 profile 同比例（"
                                    f"{profile.reference_size[0]}:"
                                    f"{profile.reference_size[1]}）")
                        _log(f"[警告] 画面宽高比与 profile 参考 "
                             f"{profile.reference_size[0]}x{profile.reference_size[1]} "
                             f"偏差 {fit.deviation * 100:.1f}%；已按参考比例重采样。"
                             f"若画面被 OBS 横向拉伸，请改回等比缩放{hint}")
                    low = min(fit.scale)
                    if low < 0.5:
                        hint = ("；虚拟摄像头请改用 RTSP 或让 OBS 画布与 profile "
                                "同比例" if args.url.startswith(("cam:", "camera:"))
                                else "")
                        _log(f"[警告] ROI 分辨率只有参考图的 {low * 100:.0f}%"
                             f"（{fit.out_size[0]}x{fit.out_size[1]} vs "
                             f"{profile.reference_size[0]}x{profile.reference_size[1]}）。"
                             f"筹码数字/庄位 D 会小到读不出，"
                             f"请把 OBS 输出分辨率提到 "
                             f"{profile.reference_size[0]}x{profile.reference_size[1]}"
                             f"{hint}")

            img = plan.apply(packet.image)
            if fit is not None:
                img = fit.apply(img)
            if img is not packet.image:
                packet = FramePacket(packet.seq, packet.epoch,
                                     packet.grab_mono, img)

            # 默认不节流：处理完这一帧就回到 wait_newer，邮箱里永远只有最新一帧，
            # 中间帧已经被 reader 覆盖丢弃。--every > 0 时才保留旧的可选降频。
            if args.every > 0:
                delay = throttle_delay(last_start, now, args.every)
                if delay > 0:
                    if stop.wait(delay):
                        break
                    newer = mailbox.latest()
                    if newer is not None and newer.seq > packet.seq:
                        # 新帧同样要过裁剪/比例归一化，否则 ROI 坐标会错位
                        newer_img = plan.apply(newer.image)
                        if fit is not None:
                            newer_img = fit.apply(newer_img)
                        packet = FramePacket(newer.seq, newer.epoch,
                                             newer.grab_mono, newer_img)
                last_start = time.monotonic()

            # 动作区还在变（连续弃牌 / 下注动画）就先不识别：丢掉这一帧，
            # 等它落定后直接处理邮箱里的最新帧。这样一串动作只留最后一帧。
            if (settle_gate is not None
                    and not settle_gate.should_process(packet.image,
                                                       time.monotonic())):
                last_seq = packet.seq
                continue

            # 自动点击正握着鼠标（滑杆打开着）时先不识别：滑杆轨道/气泡会
            # 盖住公共牌第三张，继续 OCR 只会读出「board 2 张」的假警报。
            # 这几帧**不算丢弃** —— 它们只是让给了点击，画面回来立刻恢复。
            if processor.click_in_flight:
                # 先把已经跑完的点击结果收回来：不收的话 _click_inflight 永远是
                # 真，主循环会一直让路（死锁）。收完再判一次要不要继续让路。
                processor._poll_click_results()
                if processor.click_in_flight:
                    last_seq = packet.seq
                    processor.log_click_wait()
                    continue

            result = processor.handle(packet)
            last_seq = packet.seq

            if args.max_frames and processor.processed >= args.max_frames:
                break
            if time.monotonic() - last_status >= 10.0:
                status_base = _log_status(processor, reader, started_mono,
                                          status_base)
                publish_status()
                last_status = time.monotonic()
    finally:
        stop.set()
        mailbox.close()
        reader.join(timeout=2.0)

        # 关掉建议线程前先把已完成的结果落库；正在跑的那一次不再等
        if advice_runner is not None:
            advice_runner.close()
            processor.drain_advice(final=True)
        # 点击线程最后收尾：在途的那一下不等（鼠标动作已经发出去了），
        # 只把已经回来的结果落盘，别让 session_end 之后还有 write。
        processor._poll_click_results()
        try:
            processor.click_executor.close()
        except Exception:            # noqa: BLE001 — 收尾失败不能影响退出
            pass
        if ingest_worker is not None:
            ingest_worker.stop()

        elapsed = time.monotonic() - started_mono
        summary = {
            "elapsed_seconds": round(elapsed, 1),
            "frames_read": reader.grabbed,
            "processed_frames": processor.processed,
            "avg_proc_ms": round(processor.avg_proc_ms, 1),
            "eff_hz": round(processor.processed / elapsed, 2) if elapsed else 0,
            "reconnects": reader.reconnects,
            "advice_count": processor.advice_count,
            "advice_done": processor.advice_done,
            **({"settle_blocks": settle_gate.blocks}
               if settle_gate is not None else {}),
            **processor.summary(),
        }
        session_end_record = {"type": "session_end", "ended_at": _iso_now(),
                              "summary": summary}
        writer.write(session_end_record)
        writer.close()
        publish("session_end", {k: v for k, v in session_end_record.items()
                                if k != "type"})
        if state_stream is not None:
            state_stream.close()
        if profile_store is not None:
            ok = profile_store.save(force=True)
            _log(f"[对手] 画像已落盘：{profile_store.path}"
                 f"（{len(profile_store.players)} 人）"
                 + ("" if ok else f"；失败：{profile_store.last_save_error}"))

        _log(f"\n会话结束：读帧 {reader.grabbed}，处理 {processor.processed}，"
             f"接受 {summary['accepted_frames']}，"
             f"记录 {summary['record_count']} 条，重连 {reader.reconnects} 次；"
             f"平均 {summary['avg_proc_ms']:.0f}ms/帧，"
             f"有效 {summary['eff_hz']:.2f}Hz")
        if summary.get("idle_frames"):
            _log(f"手牌间隙帧（底池横幅显示「总底池」）："
                 f"{summary['idle_frames']}（不是识别失败）")
        if summary["discarded_reasons"]:
            _log("丢弃原因：" + "  ".join(
                f"{k}={v}" for k, v in
                sorted(summary["discarded_reasons"].items(), key=lambda kv: -kv[1])))
        if summary.get("hands_skipped_no_hero"):
            _log(f"手牌：跳过 {summary['hands_skipped_no_hero']} 条没有自己手牌的"
                 f"空档记录（--record-without-hero 可保留）")
        _log(f"→ {args.out}")

        # 先关看板（浏览器会收到 session_end 后断开），再关事件日志和控制台
        # 文件，保证最后一条事件/最后一行文本都落盘。
        if web is not None:
            web.close()
        if event_log is not None:
            event_log.close()
        set_console_sink(None)
        if console_log is not None:
            console_log.close()

    return 0



def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    env_load = _load_runtime_env(ap, args)
    return _run_live(args, _build_runtime(ap, args, env_load))


def _format_reasons(reasons: dict[str, int], limit: int = 8) -> str:
    """把 `{原因: 次数}` 排成一行：次数多的在前，超出的只报类数。"""
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    text = "  ".join(f"{k}={v}" for k, v in top)
    if len(reasons) > limit:
        text += f"  …另 {len(reasons) - limit} 类"
    return text


def _log_status(processor: LiveProcessor, reader: RtspReader,
                started_mono: float,
                since: dict[str, int] | None = None) -> dict[str, int]:
    """打一行状态 + 本段丢弃原因；返回累计原因，供下次算增量。

    `接受=0` 是排查的关键信号：采样/处理都在涨、接受一直是 0，说明画面
    根本没有通过质量闸 —— 这时状态行必须同时给出「哪里没读出来」，
    否则终端看起来就只剩一串 [状态]。
    """
    elapsed = time.monotonic() - started_mono
    eff = processor.processed / elapsed if elapsed else 0.0
    reasons = processor.discard_reasons
    # 间隙帧（底池横幅显示「总底池」= 已收池/还没开手）不算识别失败，
    # 单独报，免得把 20% 的丢弃率全算到 OCR 头上。
    idle = processor.idle_frames
    _log(f"[状态] 采样={processor.sampled} 处理={processor.processed} "
         f"接受={processor.accepted} 记录={processor.records} "
         f"读帧={reader.grabbed} "
         f"丢弃帧={processor.sampled - processor.accepted - idle} "
         f"间隙帧={idle} "
         f"平均={processor.avg_proc_ms:.0f}ms 有效={eff:.2f}Hz "
         f"重连={reader.reconnects}")
    since = since or {}
    delta = {k: v - since.get(k, 0) for k, v in reasons.items()
             if v > since.get(k, 0)}
    # 正常打牌时每 10s 重复一遍原因太吵：只在出现新原因时提示；一帧都没
    # 接受时则每次都提示（这正是需要盯着的状态）。
    new_keys = {k for k in delta if k not in since}
    if delta and (new_keys or processor.accepted == 0):
        tail = (f"｜累计 {_format_reasons(reasons)}"
                if len(reasons) > len(delta) else "")
        _log(f"[丢弃] 本段 {_format_reasons(delta)}{tail}")
    if (processor.accepted == 0
            and processor.processed >= ZERO_ACCEPT_WARN_AFTER
            and not processor._zero_accept_warned):
        processor._zero_accept_warned = True
        sample = (f"；样本已存 {processor.last_gate_dump}"
                  if processor.last_gate_dump else "")
        _log(f"[警告] 已处理 {processor.processed} 帧、接受 0 帧：画面里读不到"
             f"底池/庄位/筹码。先确认视频源是牌桌画面（大厅、结算弹窗、"
             f"比例不对都会让整帧丢弃）{sample}")
    return reasons


if __name__ == "__main__":
    sys.exit(main())
