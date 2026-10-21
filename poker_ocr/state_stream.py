"""抽帧流的状态机（M3.1）：一帧图片 → 是否记录 / 丢弃 / 无变化。

这一层原本长在 `tools/video_to_states.py` 的 `main()` 里，是离线视频专用的。
实时链路要的是**完全相同的防抖与跨帧继承**，所以原样抽出来，两边共用：

    stream = StateStream(profile, engine)
    outcome = stream.ingest(img, frame_id="live_00012", t=6.0)
    if outcome.kind == "record":
        write_jsonl(outcome.record)

**只做状态机，不做 I/O**：不读视频、不写文件、不打印。调用方负责采样节奏和落盘。

几条必须保持不变的语义（离线回归就是靠它们对拍的）：

  · `prev` 只推进到**被接受**的帧；被丢弃的帧不推进，所以 vanish 计数会跨丢弃累积。
  · `prev_dealer` / `prev_all_in` / `NameTracker` 在**每个采样帧**都更新，包括被丢弃的帧。
  · 新状态要连续出现 `stable_frames` 帧才记录；首帧立即记录当基线。
  · 签名没变不重复记录。
  · 庄位轮转（检出庄位且与上一帧不同）= 新的一手，清空全下继承。
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from .dealer import DealerResult, build_detector
from .frame_state import FrameState, NameTracker, build_state, signature
from .parse import Blinds, parse_action, parse_blinds
from .ocr import OcrEngine
from .profile import Profile
from .roi_dedup import RoiStream
from .roi_reader import read_avatars

# 一帧里通常有 30+ 块 ROI，但真正未命中缓存的只有变化的那些。4 线程在
# M2（8 核）上实测最划算（39 块全跑 475ms → 282ms）；再高会和 ONNX 的
# intra-op 线程、RTSP 解码线程抢核，反而变慢。小机器按一半核数封顶。
# `--roi-threads 1` 可整体回退到串行。
DEFAULT_ROI_THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))

# ── 一手牌内的慢变量（语义缓存）──
# 名字和盲注横幅在一手牌里不会变，但横幅里的房间号/倒计时每秒都在动，
# 按像素去重每帧都会 miss。这里把它们「钉住」：读到一次可信值后不再 OCR，
# 换手时整批解钉。庄位同理 —— 一手牌里只在换手时挪一次。
DEALER_RECHECK_SECONDS = 2.0     # 定期复核庄位，兜住漏掉的换手信号
DEALER_CONFIRM_FRAMES = 2        # 换手后多detect几帧，等轮转动画落位
NAME_PIN_MIN_SCORE = 0.8         # 低于这个分数不钉，等下帧重读到可信名字


@dataclass(frozen=True)
class FrameOutcome:
    """一帧的处理结果。

    kind:
      · record   —— 产生了可写入输出的新状态（record 字段可直接用）
      · no_change —— 状态有效，但和上一份记录一样（或还没稳定）
      · discard  —— 有字段无法精准识别 / 座位疑似消失，整帧放弃
    """

    kind: str
    state: FrameState
    errors: list[str]
    record: dict | None
    reused: int
    n_roi: int


class StateStream:
    """跨帧状态机。一个实例对应一条流/一段视频；断流重连时调用 `reset()`。"""

    def __init__(self, profile: Profile, engine: OcrEngine | None = None, *,
                 detect_fold: bool = True, dedup: bool = True,
                 stable_frames: int = 2, max_vanish: int = 3,
                 name_confirm: int = 3,
                 initial_blinds: Blinds | None = None,
                 lenient_chips: bool = False,
                 roi_threads: int = DEFAULT_ROI_THREADS) -> None:
        self.profile = profile
        self.engine = engine or OcrEngine()
        self.detect_fold = detect_fold
        # 帧内 ROI rec 的线程池：1 = 串行。线程池随 StateStream 长驻，
        # `reset()` 不清它（断流重连后继续用同一批线程）。
        self.roi_threads = max(1, int(roi_threads))
        self._roi_pool = (ThreadPoolExecutor(
            max_workers=self.roi_threads, thread_name_prefix="roi-rec")
            if self.roi_threads > 1 else None)
        self.stable_frames = max(1, int(stable_frames))
        self.max_vanish = max(0, int(max_vanish))
        self.name_confirm = max(1, int(name_confirm))
        # 标定帧里盲注横幅被挡住时，给一个已知盲注做首帧继承
        self.initial_blinds = initial_blinds
        # 固定 ROI 的筹码槽上下文明确；单位被读坏时允许取前导数字
        self.lenient_chips = bool(lenient_chips)

        detector = build_detector(profile, None, self.engine)
        self.stream = RoiStream(profile, self.engine, detector,
                                with_dealer=True, dedup=dedup,
                                executor=self._roi_pool)
        self.names = NameTracker(confirm=self.name_confirm)

        self.prev: FrameState | None = None
        self.prev_all_in: set[str] = set()
        self.prev_dealer: str | None = None
        self.vanish: dict[str, int] = defaultdict(int)

        # 一手牌内的慢变量缓存：庄位结果 + 上一帧的牌面/手牌指纹（判断换手）
        self._dealer_result: DealerResult | None = None
        self._dealer_seat: str | None = None
        self._dealer_checked_at: float | None = None
        self._dealer_confirm_left = 0
        self._board_len: int | None = None
        self._board_drop = 0
        self._hero_cards: tuple[str, ...] = ()

        self.last_sig: tuple | None = None
        self.cand_sig: tuple | None = None
        self.cand_n = 0

        # 累计计数不随 reset() 清零：断流重连后仍要汇总整个会话
        self.sampled = 0
        self.accepted = 0
        self.records = 0
        self.discarded: Counter = Counter()

    # ─────────────────────────── 主流程 ───────────────────────────

    def ingest(self, img: np.ndarray, frame_id: str, t: float,
               *, extra_signature: tuple | None = None) -> FrameOutcome:
        """喂一帧图片，返回本帧结果。调用方保证同一时刻只有一个线程调用。

        `extra_signature` 把 OCR 之外的状态并进变化指纹。实时链路用它把
        「公共牌 / hero 手牌」纳入触发条件 —— 否则翻牌只让牌面变、底池没变时，
        这一帧会被判成 no_change，JSONL 里就看不到新公共牌。

        **它也定义了一手牌的范围**：拿到它时，同一手内把名字/盲注/庄位
        钉住复用，牌面掉回 0 或 hero 换牌时整批解钉重读。离线工具不传，
        行为与以前完全一致（纯像素去重 + 每帧庄位检测）。
        """
        self.sampled += 1

        # 换手信号先于读帧：名字/盲注要在这一帧就解钉并重新识别。
        # 只有 live 会传 extra_signature（手牌/公共牌）；离线工具没有可靠的
        # 换手边界，慢变量维持原来的「按像素变化重读」，避免跨手复用旧值。
        sticky = extra_signature is not None
        new_hand = self._hand_changed(extra_signature)
        if new_hand:
            self._restart_sticky()

        # 庄位一手牌里只挪一次：没换手时直接复用上一帧结果，省掉整帧 blob 扫描。
        # 每 DEALER_RECHECK_SECONDS 兜底复核一次，防止换手信号漏掉。
        detect_dealer = (not sticky) or (
            self._dealer_result is None
            or new_hand
            or self._dealer_confirm_left > 0
            or self._dealer_checked_at is None
            or t - self._dealer_checked_at >= DEALER_RECHECK_SECONDS
        )
        frame = self.stream.read(img, frame_id=frame_id,
                                 detect_dealer=detect_dealer)
        # 先钉这一帧读到的名字/盲注；如果下面查出庄位轮转（换手），
        # _restart_sticky() 会立刻把它们解钉，下一帧重新识别。
        if sticky:
            self._pin_slow_fields(frame)
        if detect_dealer:
            self._dealer_checked_at = t
            self._dealer_confirm_left = max(0, self._dealer_confirm_left - 1)
            detected = frame.dealer
            if detected is not None and detected.status == "found":
                if (self._dealer_seat is not None
                        and detected.seat_id != self._dealer_seat):
                    # 庄位换了但牌面/手牌都没给出换手信号：按换手处理
                    new_hand = True
                    self._restart_sticky()
                self._dealer_seat = detected.seat_id
                self._dealer_result = detected
        elif self._dealer_result is not None:
            frame.dealer = self._dealer_result
            frame.ms_dealer = 0.0
        if new_hand:
            # 轮转动画可能还没落位，换手后再多读两帧
            self._dealer_confirm_left = DEALER_CONFIRM_FRAMES

        avatars = read_avatars(img, self.profile, self.engine,
                               self.stream.cache, self.detect_fold,
                               executor=self._roi_pool)

        # 庄位轮转 = 新的一手：上一手的全下状态不能带进来
        dealer_now = (frame.dealer.seat_id
                      if frame.dealer and frame.dealer.status == "found" else None)
        if dealer_now and self.prev_dealer and dealer_now != self.prev_dealer:
            self.prev_all_in = set()

        state = build_state(
            frame, self.profile,
            prev_blinds=(self.prev.blinds if self.prev is not None
                         else self.initial_blinds),
            avatar_reads=avatars,
            name_tracker=self.names,
            prev_all_in=self.prev_all_in,
            lenient_chips=self.lenient_chips,
        )
        if dealer_now:
            self.prev_dealer = dealer_now
        self.prev_all_in |= {p.seat_id for p in state.players
                             if p.in_hand and p.all_in}

        # ── 座位整座消失：先当遮挡，连续 max_vanish 帧才认定人走了 ──
        if self.prev is not None:
            for p_prev in self.prev.players:
                if not p_prev.in_hand:
                    continue
                p_now = next(p for p in state.players
                             if p.seat_id == p_prev.seat_id)
                if p_now.in_hand:
                    self.vanish[p_prev.seat_id] = 0
                else:
                    self.vanish[p_prev.seat_id] += 1
                    if self.vanish[p_prev.seat_id] <= self.max_vanish:
                        state.errors.append(f"vanished:{p_prev.seat_id}")

        if not state.ok:
            for e in state.errors:
                self.discarded[e.split(":", 1)[0]] += 1
            return FrameOutcome("discard", state, list(state.errors), None,
                                frame.n_reused, frame.n_roi)

        self.accepted += 1
        sig = signature(state)
        if extra_signature is not None:
            sig = (sig, extra_signature)
        record: dict | None = None
        if self.last_sig is None:
            record = self._payload(state, t, self.records + 1)   # 首帧：基线
        else:
            if sig == self.cand_sig:
                self.cand_n += 1
            else:
                self.cand_sig, self.cand_n = sig, 1
            # stable_frames=1 时第一次出现就记录（不再等第二帧）。
            # 旧写法把记录判断放在「重复出现」分支里，1 帧确认也要等两帧。
            if self.cand_n >= self.stable_frames and sig != self.last_sig:
                record = self._payload(state, t, self.records + 1)

        self.prev = state
        if record is not None:
            self.last_sig = sig
            self.records += 1
            return FrameOutcome("record", state, [], record,
                                frame.n_reused, frame.n_roi)
        return FrameOutcome("no_change", state, [], None,
                            frame.n_reused, frame.n_roi)

    # ─────────────────────────── 辅助 ───────────────────────────

    def _hand_changed(self, extra_signature: tuple | None) -> bool:
        """用 hero 手牌 / 公共牌判断是不是换了一手。

        live 每帧把 `(hero_cards, board_codes)` 作为 `extra_signature` 传进来；
        离线工具不传，这时不做换手推断（行为与以前一致，慢变量按需重读）。
        board 从 >0 掉回 0、或 hero 的两张牌换了一组，都是新一手。
        """
        if extra_signature is None or len(extra_signature) != 2:
            return False
        hero = tuple(extra_signature[0] or ())
        board = tuple(extra_signature[1] or ())
        new_hand = False
        if self._board_len is not None:
            # 单帧读空很常见（遮挡/动画），牌面掉回 0 必须连续 2 帧才算换手，
            # 与 LiveHandTracker._is_new_hand 的确认口径一致
            if len(board) == 0:
                if self._board_len > 0 or self._board_drop > 0:
                    self._board_drop += 1
            else:
                self._board_drop = 0
            if self._board_drop >= 2:
                new_hand = True
                self._board_drop = 0
            if (len(hero) == 2 and len(self._hero_cards) == 2
                    and hero != self._hero_cards):
                new_hand = True
        self._board_len = len(board)
        self._hero_cards = hero
        return new_hand

    def _restart_sticky(self) -> None:
        """换手：名字/盲注解钉，庄位结果作废（下一帧重新检测）。"""
        cache = self.stream.cache
        if cache is not None:
            cache.unpin_all()
        self._dealer_result = None

    def _pin_slow_fields(self, frame) -> None:
        """把这一帧读到的可信名字 / 盲注钉住，之后同手内不再 OCR。

        名字槽在部分布局里会被动作徽章盖住；看起来像动作的读数不钉，
        免得把「弃牌/跟注」冻成名字。分数太低的也留到下帧重读。
        """
        cache = self.stream.cache
        if cache is None:
            return
        for seat_roi in frame.seats:
            key = f"{seat_roi.seat_id}.name"
            if cache.is_pinned(key):
                continue
            read = seat_roi.fields.get("name")
            if (read is not None and read.text
                    and read.score >= NAME_PIN_MIN_SCORE
                    and parse_action(read.text) is None):
                cache.pin(key)
        if not cache.is_pinned("@blinds"):
            read = frame.globals.get("blinds")
            if read is not None and parse_blinds(read.text) is not None:
                cache.pin("@blinds")

    @staticmethod
    def _payload(state: FrameState, t: float, index: int) -> dict:
        payload = state.to_debug()
        payload.pop("frame_id", None)
        return {"index": index, "t": round(t, 2),
                "frame": state.frame_id, **payload}

    def reset(self) -> None:
        """断流重连 / 换桌时清掉跨帧状态。累计计数保留。"""
        self.prev = None
        self.prev_all_in = set()
        self.prev_dealer = None
        self.vanish.clear()
        self.names = NameTracker(confirm=self.name_confirm)
        self.last_sig = None
        self.cand_sig = None
        self.cand_n = 0
        self._dealer_result = None
        self._dealer_seat = None
        self._dealer_checked_at = None
        self._dealer_confirm_left = 0
        self._board_len = None
        self._board_drop = 0
        self._hero_cards = ()
        self.stream.reset()

    def close(self) -> None:
        """释放帧内 OCR 线程池。关闭后不应再调用 ingest。"""
        if self._roi_pool is not None:
            self._roi_pool.shutdown(wait=False)
            self._roi_pool = None
            # 万一关闭后还有人读帧，退回串行，别对着已 shutdown 的池 submit
            self.stream.executor = None

    def summary(self) -> dict:
        return {
            "sampled_frames": self.sampled,
            "accepted_frames": self.accepted,
            "discarded_frames": self.sampled - self.accepted,
            "discarded_reasons": dict(self.discarded),
            "record_count": self.records,
            "roi_threads": self.roi_threads,
            "roi_reuse": (self.stream.cache.to_debug()
                          if self.stream.cache is not None else {"disabled": True}),
        }
