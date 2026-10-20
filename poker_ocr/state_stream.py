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

from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np

from .dealer import build_detector
from .frame_state import FrameState, NameTracker, build_state, signature
from .parse import Blinds
from .ocr import OcrEngine
from .profile import Profile
from .roi_dedup import RoiStream
from .roi_reader import read_avatars


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
                 lenient_chips: bool = False) -> None:
        self.profile = profile
        self.engine = engine or OcrEngine()
        self.detect_fold = detect_fold
        self.stable_frames = max(1, int(stable_frames))
        self.max_vanish = max(0, int(max_vanish))
        self.name_confirm = max(1, int(name_confirm))
        # 标定帧里盲注横幅被挡住时，给一个已知盲注做首帧继承
        self.initial_blinds = initial_blinds
        # 固定 ROI 的筹码槽上下文明确；单位被读坏时允许取前导数字
        self.lenient_chips = bool(lenient_chips)

        detector = build_detector(profile, None, self.engine)
        self.stream = RoiStream(profile, self.engine, detector,
                                with_dealer=True, dedup=dedup)
        self.names = NameTracker(confirm=self.name_confirm)

        self.prev: FrameState | None = None
        self.prev_all_in: set[str] = set()
        self.prev_dealer: str | None = None
        self.vanish: dict[str, int] = defaultdict(int)

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
        离线工具不传，行为与以前完全一致。
        """
        self.sampled += 1

        frame = self.stream.read(img, frame_id=frame_id)
        avatars = read_avatars(img, self.profile, self.engine,
                               self.stream.cache, self.detect_fold)

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
        self.stream.reset()

    def summary(self) -> dict:
        return {
            "sampled_frames": self.sampled,
            "accepted_frames": self.accepted,
            "discarded_frames": self.sampled - self.accepted,
            "discarded_reasons": dict(self.discarded),
            "record_count": self.records,
            "roi_reuse": (self.stream.cache.to_debug()
                          if self.stream.cache is not None else {"disabled": True}),
        }
