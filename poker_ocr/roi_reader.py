"""固定 ROI → 只跑 rec：一帧的原始读数（M2.1）。

M2 把「全图 det + 几何分配」换成「槽位裁剪 + 纯识别」。本模块只做前半段：
按 profile 的槽位裁出 ROI、逐块跑 rec，把**原始文本**原样交出去。
解析（下注宽松单位、本街下注公式）不在这层 —— 那是 M2.3。

为什么值得这么改（M1 实测）：瓶颈是 rec 的**调用次数**，不是检测图的大小。
把图缩到长边 640 再跑全图 det+rec 仍然 1.06s；固定 ROI 只 rec 是 1.11s → 18 个
ROI 共 0.25s（单 ROI 14ms）。「通过全图 det 找文本」本身就是慢的根因。

⚠️ 三条别再踩的坑（README / 配置里的回退记录 / 本模块 11 帧实测）：

1. **名字槽是「搜索区域」不是「文字紧框」** —— 按它裁不会切掉长名字的开头
   （配置的 _name_slot_comment：槽宽 0.194 ≥ 实测最长名字 0.161）。
   别为了「更精确」把它收窄到某个框上，那正是当初名字读错的根因。
2. **动作标签不单独读 ROI**。实测 91 处非空里 **0 处**和 name ROI 相同 ——
   action ROI（= 名字槽 ∪ 头像槽）在纯 rec 下读到的全是头像照片的碎片
   （`D`/`O`/`®`/`囍`/`福`，分数 0.1–0.9）。徽章文本（让牌/跟注/下注/加注）
   本来就落在 **name ROI** 里，因为它画在名字位置上。所以：
   徽章的分离在 M2.3 从 `name` 原文里做；「弃牌」盖在头像上、靠**颜色**判，也不靠文字。
   停读它省 117ms/帧（23%）。
3. **hero_turn 读「第一个预设钮的 BB 值行」，不读整块**。整块 bet_size_presets 是
   5 个圆钮排一行（「底池／1/3／15BB」三层堆叠），纯 rec 假设单行 → 读成垃圾 `e0o0e`；
   但**单个钮的 BB 值行**（`hero_turn_probe` 槽）在两个分辨率上都读得干净。
   中间试过「浅灰圆片占比」的视觉判据，在真机视频上翻车了：表情面板误报、这台设备
   按钮更暗导致漏报 —— **色彩门槛跨设备不成立，文本才成立**。详见 measure_hero_turn。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field as dc_field, replace as dc_replace
from typing import Protocol, runtime_checkable

import numpy as np

from .dealer import DealerDetector, DealerResult
from .ocr import OcrEngine, preprocess_variant
from .profile import Profile, Size, Slot

# 每个座位读哪几块。这份顺序就是最终输出顺序，改它等于改接口。
#
# **没有 action**：理由见模块 docstring 第 2 条 —— 徽章文本在 name ROI 里，
# 单独读 action ROI 只拿到头像碎片。M2.3 从 name 原文里分离。
# **没有 avatar**：那块是头像照片，rec 只会吐幻觉文本；「弃牌」靠颜色判。
SEAT_FIELDS: tuple[str, ...] = ("name", "chip", "bet")

# 全局区域。两个**故意不读**的：
#   hero_action_zone —— 配置里明说别用它判「轮到我」：那对「让或弃 / N BB 跟注」
#                       在 hero 不是回合时也会渲染出来（灰色）；
#   bet_size_presets —— 改用视觉判据（measure_hero_turn），读文本实测是垃圾。
GLOBAL_FIELDS: tuple[str, ...] = ("pot", "pot_before_street", "blinds")

# 裁剪时按槽位尺寸向外扩的比例。留白不是为了「多框点字」，是因为
# 槽位当初是按某一帧量的，换帧会有小幅位移 —— 纯 rec 没有检测来兜底。
ROI_PAD = 0.05

# 预设按钮 BB 值行的形状：`15BB` / `2.5BB`。OCR 偶尔把空格读进来，所以允许中间有空白。
_HERO_TURN_RE = re.compile(r"\d+(?:\.\d+)?\s*BB")
# 分数闸读不到时的兜底。实测正例 0.89–0.99、表情面板的伪装 0.21–0.30。
_HERO_TURN_MIN_SCORE_DEFAULT = 0.5


@dataclass
class RoiRead:
    """一块 ROI 的读数。text 为空串表示「这块没识别出文字」，不是「没有这块区域」。"""

    key: str
    rect: tuple[int, int, int, int]
    parts: list[tuple[str, float]] = dc_field(default_factory=list)
    ms: float = 0.0
    note: str | None = None
    # 这块没跑 rec，用的是上一帧的读数（M2.2 去重命中）。**一定要能看出来** ——
    # 复用错了（画面其实变了但 hash 说没变）是本层唯一危险的失败模式，
    # 而它在下游看起来和「识别器读对了」一模一样。
    reused: bool = False

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self.parts).strip()

    @property
    def score(self) -> float:
        """取**最低**的框分 —— 和 fields.read_name 一个口径：多框里有一框不可信，
        整块就不该被当成可信。"""
        return min((s for _, s in self.parts), default=0.0)

    def to_debug(self) -> dict:
        d: dict = {
            "rect": list(self.rect),
            "text": self.text,
            "score": round(self.score, 4),
            "ms": round(self.ms, 2),
        }
        # 多框才把明细摆出来：单框时 parts 和 text/score 是冗余的
        if len(self.parts) > 1:
            d["parts"] = [[t, round(s, 4)] for t, s in self.parts]
        if self.reused:
            d["reused"] = True
        if self.note:
            d["note"] = self.note
        return d


@runtime_checkable
class RoiCache(Protocol):
    """ROI 去重的接口（M2.2 实现，见 roi_dedup.RoiDedupCache）。

    刻意只有两个方法，且**由 read_frame 来调**，这样「哪块 ROI 该不该跑 rec」
    的唯一决策点留在读帧循环里，去重实现只管存与比：

        hit, hash_ms = cache.check(key, roi)   # 算 hash、记下这一帧的 hash
        hit is None → 变了（或没缓存）→ 跑 rec → cache.store(key, read)
        hit 不是 None → 没变 → 直接用它，不跑 rec

    `check` 返回的是**上一次 `store` 进去的 RoiRead**，调用方要复制一份再标
    reused，别就地改缓存里的对象。
    """

    def check(self, key: str, roi: np.ndarray) -> tuple["RoiRead | None", float]:
        ...

    def store(self, key: str, read: "RoiRead") -> None:
        ...


@dataclass
class SeatRoi:
    seat_id: str
    ring_index: int
    fields: dict[str, RoiRead] = dc_field(default_factory=dict)

    def to_debug(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "ring_index": self.ring_index,
            "fields": {k: v.to_debug() for k, v in self.fields.items()},
        }


@dataclass
class RoiFrame:
    frame_id: str
    size: Size
    hero_seat_id: str
    seats: list[SeatRoi]
    globals: dict[str, RoiRead]
    hero_turn: bool | None = None
    hero_turn_evidence: dict = dc_field(default_factory=dict)
    dealer: DealerResult | None = None
    ms_roi: float = 0.0
    ms_dealer: float = 0.0

    @property
    def n_roi(self) -> int:
        # hero_turn 的探针也是块 ROI（有一帧就 +1），算进去统计才准
        return (sum(len(s.fields) for s in self.seats) + len(self.globals)
                + (1 if "rect" in self.hero_turn_evidence else 0))

    @property
    def n_reused(self) -> int:
        """这一帧有多少块是复用上一帧的（没跑 rec）。去重的效果就看它。"""
        return (sum(r.reused for s in self.seats for r in s.fields.values())
                + sum(r.reused for r in self.globals.values())
                + (1 if self.hero_turn_evidence.get("reused") else 0))

    def seat(self, seat_id: str) -> SeatRoi:
        for s in self.seats:
            if s.seat_id == seat_id:
                return s
        raise KeyError(f"这一帧里没有座位 {seat_id}")

    def text(self, key: str, seat_id: str | None = None) -> str:
        """取原文。seat_id 为空时取全局槽 —— 探针和测试都靠它读结果。"""
        if seat_id is None:
            read = self.globals.get(key)
        else:
            read = self.seat(seat_id).fields.get(key)
        return read.text if read else ""

    @property
    def ms_total(self) -> float:
        return self.ms_roi + self.ms_dealer

    def to_dict(self, include_debug: bool = True) -> dict:
        payload: dict = {
            "frame_id": self.frame_id,
            "size": list(self.size),
            "hero_seat_id": self.hero_seat_id,
            "hero_turn": self.hero_turn,
            "hero_turn_evidence": self.hero_turn_evidence,
            "roi_slots": self.n_roi,
            "roi_reused": self.n_reused,
            "ms_roi": round(self.ms_roi, 1),
            "ms_dealer": round(self.ms_dealer, 1),
            "seats": [s.to_debug() for s in self.seats],
            "globals": {k: v.to_debug() for k, v in self.globals.items()},
        }
        if self.dealer is not None:
            payload["dealer"] = self.dealer.to_debug()
        if not include_debug:
            payload = _strip(payload)
        return payload


def _strip(payload: dict) -> dict:
    """去掉 rect/ms/parts 这些调试字段，只留 text/score。给「只想看读数」的场景。"""
    out: dict = {}
    for k, v in payload.items():
        if isinstance(v, dict):
            if "text" in v and "score" in v:
                out[k] = {"text": v["text"], "score": v["score"]}
            else:
                out[k] = _strip(v)
        elif isinstance(v, list):
            out[k] = [_strip(item) if isinstance(item, dict) else item for item in v]
        else:
            out[k] = v
    return out


# ─────────────────────────── 单块 ROI ───────────────────────────

def crop_slot(img: np.ndarray, profile: Profile, slot: Slot, size: Size,
              pad: float = ROI_PAD) -> tuple[tuple[int, int, int, int], np.ndarray]:
    """槽位 → (像素矩形, 裁剪图)。

    单独拿出来是因为 M2.2 的去重要**先拿到裁剪**算 hash，再决定要不要跑 rec ——
    如果去重层自己再算一遍 slot_to_px，两处的 pad 迟早会走偏。
    """
    x1, y1, x2, y2 = profile.slot_to_px(slot, size, pad=pad)
    return (x1, y1, x2, y2), img[y1:y2, x1:x2]


def read_slot(img: np.ndarray, profile: Profile, engine: OcrEngine, slot: Slot,
              key: str, size: Size, mode: str = "raw", upscale: float = 1.0,
              pad: float = ROI_PAD, cache: RoiCache | None = None,
              cache_key: str | None = None) -> RoiRead:
    """裁一块槽位跑纯 rec（缓存命中就一次 rec 都不跑）。

    **只调 engine.read_text（use_det=False）** —— 全图 det 是一次都不跑的，
    这是这一层速度的全部来源，也是它和 pipeline.extract 的分水岭。

    `key` 是给人看的字段名（name/chip/…），`cache_key` 必须**全场唯一**
    （座位字段传 `座位.字段`，全局传 `@字段`）—— 否则 9 个座位的 name 会互相顶掉。
    """
    rect, roi = crop_slot(img, profile, slot, size, pad=pad)
    if roi.size == 0:
        return RoiRead(key, rect, note="裁剪区为空（槽位落到了图外）")

    if cache is not None:
        hit, hash_ms = cache.check(cache_key or key, roi)
        if hit is not None:
            # 复制一份再标 reused：直接改会把缓存里那份也标上，
            # 下一帧命中时它看上去就像「当初也是复用来的」
            return dc_replace(hit, key=key, rect=rect, reused=True, ms=hash_ms)

    prepared = preprocess_variant(roi, mode, upscale)
    t0 = time.perf_counter()
    try:
        hits = engine.read_text(prepared)
    except Exception as exc:                       # 单块失败不该带崩整帧
        return RoiRead(key, rect, ms=(time.perf_counter() - t0) * 1000,
                       note=f"rec 抛异常：{type(exc).__name__}")
    ms = (time.perf_counter() - t0) * 1000

    parts = [(t.strip(), float(s)) for t, s in hits if t and t.strip()]
    read = (RoiRead(key, rect, ms=ms, note="rec 没识别出文字") if not parts
            else RoiRead(key, rect, parts=parts, ms=ms))
    if cache is not None:
        # 读不出文字也要存：下一帧同样读不出时，没必要再跑一次 rec 确认
        cache.store(cache_key or key, read)
    return read


def _seat_slot(seat, key: str) -> tuple[Slot | None, str]:
    """座位字段 → 槽位。返回 (槽位, 缺失原因)。"""
    if key == "name":
        return seat.name_slot, "name_slot 一定有"
    if key == "chip":
        return seat.chip_slot, "chip_slot 一定有"
    slot = {"bet": seat.bet_slot, "action": seat.action_slot}.get(key)
    if slot is None:
        return None, f"{key}_slot 不在这个 profile 里（老配置没有）"
    return slot, ""


def measure_hero_turn(img: np.ndarray, profile: Profile, engine: OcrEngine,
                      size: Size | None = None,
                      cache: RoiCache | None = None) -> tuple[bool | None, dict]:
    """读 `hero_turn_probe`（第一个预设钮的 BB 值行）→ 轮到 hero 了吗。三态。

    为什么是这一小块：整块 presets 是 5 个钮 × 3 行，纯 rec 读不了；单个钮的
    **BB 值行**是干净的单行文本，而且在 1080x2400 和 864x1920 两种分辨率上都成立。

    **为什么不再用「浅灰圆片占比」**（这是真机视频推翻的）：色彩/亮度门槛跨设备
    不成立 —— 同一段视频里，表情面板用 0.042 骗过了 0.03，而这台设备上的真按钮
    只有 0.0266 反而漏掉。文本不随亮度变，所以改用文本。

    三态是刻意的：
      · 读到「数字+BB」且分数够 → True（预设按钮在，轮到他）
      · 空 → False（那块什么都没有 = 没轮到）
      · 非空但不是 BB 形 / 分数太低 → None（有东西，但不是预设，不硬判）
    最后那条是为了「有东西盖住了这块」的情况 —— 把遮挡读成「没轮到」会安静地
    漏掉一整条行动线，所以宁可不知道。
    """
    slot = profile.global_slots.get("hero_turn_probe")
    if slot is None:
        return None, {"error": "profile 里没有 hero_turn_probe 槽位，判不了回合"}

    size = size or (img.shape[1], img.shape[0])
    read = read_slot(img, profile, engine, slot, "hero_turn_probe", size, pad=0.0,
                     cache=cache, cache_key="@hero_turn")
    min_score = float(profile.thresholds.get("hero_turn_min_score",
                                             _HERO_TURN_MIN_SCORE_DEFAULT))
    evidence = {
        "text": read.text,
        "score": round(read.score, 4),
        "rect": list(read.rect),
        "ms": round(read.ms, 2),
    }
    if read.reused:
        evidence["reused"] = True
    if read.text:
        if _HERO_TURN_RE.search(read.text) and read.score >= min_score:
            return True, evidence
        return None, {**evidence,
                      "why": "这块有内容但不是预设钮的 BB 值（遮挡/表情面板？），不硬判"}
    if read.note and read.note.startswith("rec 没识别出文字"):
        # 空文本 = 那块什么都没有。注意这不是「没读出文字」，是 rec 确实没找到文本。
        return False, evidence
    # 裁剪为空 / rec 抛异常 → 不知道，别把故障翻译成「没轮到他」
    return None, {**evidence, "why": read.note or "空读数但没有原因"}


# ─────────────────────────── 主流程 ───────────────────────────

def read_avatars(img: np.ndarray, profile: Profile, engine: OcrEngine,
                 cache: RoiCache | None = None,
                 detect_fold: bool = True) -> dict[str, tuple[str, float]]:
    """每座的头像区读一遍，用于弃牌 / 正在带入检测（best-effort）。

    头像不是文字，rec 只会吐碎片；这里只把原文和分数原样交出去，
    「是不是弃牌」由 `frame_state.is_fold_badge` 判。
    pad=0：头像槽本来就是照片的紧框，不需要再留白。
    """
    if not detect_fold:
        return {}
    out: dict[str, tuple[str, float]] = {}
    size = (img.shape[1], img.shape[0])
    for seat in profile.seats:
        read = read_slot(img, profile, engine, seat.avatar_slot, "avatar", size,
                         pad=0.0, cache=cache,
                         cache_key=f"{seat.seat_id}.@avatar")
        out[seat.seat_id] = (read.text, read.score)
    return out


def read_frame(img: np.ndarray, profile: Profile, engine: OcrEngine,
               frame_id: str = "frame",
               seat_fields: tuple[str, ...] = SEAT_FIELDS,
               global_fields: tuple[str, ...] = GLOBAL_FIELDS,
               mode: str = "raw", upscale: float = 1.0, pad: float = ROI_PAD,
               dealer_detector: DealerDetector | None = None,
               with_dealer: bool = True, cache: RoiCache | None = None) -> RoiFrame:
    """一帧 → 每座原始文本 + 全局原始文本。

    庄位默认开着，但它走的是**独立的全图 blob 路径**（不是 OCR det），
    耗时单独记在 ms_dealer 里 —— 别把它算进「只跑 rec」的口径。
    hero_turn 也不花 rec（视觉判据，见 measure_hero_turn），同样不占 ROI 配额。

    `cache` 是 M2.2 的 ROI 去重（见 roi_dedup）。**本函数自己不持有任何跨帧状态** ——
    缓存对象由调用方持有并传进来，所以「单帧读数」这条路永远是干净的，
    要跨帧复用才需要显式构造（推荐走 roi_dedup.RoiStream）。
    """
    size: Size = (img.shape[1], img.shape[0])
    t0 = time.perf_counter()          # 计时把座位 ROI 和全局 ROI 算在一起

    seats: list[SeatRoi] = []
    for ring_index, seat in enumerate(profile.seats_in_ring_order):
        fields: dict[str, RoiRead] = {}
        for key in seat_fields:
            slot, why = _seat_slot(seat, key)
            if slot is None:
                fields[key] = RoiRead(key, (0, 0, 0, 0), note=why)
                continue
            read = read_slot(img, profile, engine, slot, key, size,
                             mode=mode, upscale=upscale, pad=pad,
                             cache=cache, cache_key=f"{seat.seat_id}.{key}")
            # 主槽读不到时再试备选槽。只判「空」不判内容：有牌时 hero 的
            # 筹码在 alt 位置以外，主槽读到东西就绝不去碰备选块。
            if (key == "chip" and not read.text
                    and seat.chip_slot_alt is not None):
                alt = read_slot(img, profile, engine, seat.chip_slot_alt, key,
                                size, mode=mode, upscale=upscale, pad=pad,
                                cache=cache,
                                cache_key=f"{seat.seat_id}.chip_alt")
                if alt.text:
                    alt.note = "；".join(
                        n for n in (alt.note, "主 chip 槽为空，改用 chip_slot_alt")
                        if n)
                    read = alt
            fields[key] = read
        seats.append(SeatRoi(seat.seat_id, ring_index, fields))

    globals_: dict[str, RoiRead] = {}
    for key in global_fields:
        slot = profile.global_slots.get(key)
        if slot is None:
            globals_[key] = RoiRead(key, (0, 0, 0, 0),
                                    note="profile 的 global_slots 里没有这一项")
            continue
        globals_[key] = read_slot(img, profile, engine, slot, key, size,
                                  mode=mode, upscale=upscale, pad=pad,
                                  cache=cache, cache_key=f"@{key}")

    # hero_turn 也是块 ROI（只是很小），算在同一笔账里
    hero_turn, turn_evidence = measure_hero_turn(img, profile, engine, size, cache)

    # 全局槽也算在「一帧的 ROI 耗时」里 —— 调用方关心的是这帧总共花了多久
    ms_roi = (time.perf_counter() - t0) * 1000

    dealer: DealerResult | None = None
    ms_dealer = 0.0
    if with_dealer and dealer_detector is not None:
        t1 = time.perf_counter()
        dealer = dealer_detector.detect(img, profile, size)
        ms_dealer = (time.perf_counter() - t1) * 1000

    return RoiFrame(
        frame_id=frame_id,
        size=size,
        hero_seat_id=profile.hero_seat_id,
        seats=seats,
        globals=globals_,
        hero_turn=hero_turn,
        hero_turn_evidence=turn_evidence,
        dealer=dealer,
        ms_roi=ms_roi,
        ms_dealer=ms_dealer,
    )
