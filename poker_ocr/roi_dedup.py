"""ROI 级 dhash 去重（M2.2）：每块 ROI 跟**上一帧的自己**比，没变就跳过 rec。

M2.1 实测：单帧 30 块 ROI 全量要 376ms，而 0.5s 抽帧间隔里绝大多数帧是**静止**的
（别人在思考时，画面只有计时器在动）。所以这里不比「这一帧和别的帧像不像」，
只比「同一块 ROI 和它上一帧像不像」—— 这才是抽帧场景的正确粒度。

**只有一种危险的失败模式**：画面其实变了、hash 却说没变 → 复用上一帧的读数，
于是下游拿到一个**过期的值，而且看起来和「识别器读对了」一模一样**。
（反过来「变了但 hash 说变了」只是多跑一次 rec，纯粹是慢一点。）
所以本层的偏向是**宁可漏杀不可错杀**：默认 `max_distance = 0`（逐 bit 全同才复用）。

**11 张样张两两相邻配对（150 对）的实测 + 一个合成反例**，这是全部默认值的依据：

| 判据 | 「文本变了」的最近一对 | 「文本没变」的命中(距离0) | 结论 |
|---|---|---|---|
| dhash 9x8（64 bit） | min **0** ❌ | 38/150 | 有 2 例真变化距离为 0，**不安全** |
| dhash 17x16（256 bit） | min 12 | 38/150 | 零碰撞，但见下 |
| dhash 17x16 **横切 4 片** | min **29** | 23/150 | ✅ 定为本默认 |
| MAD（逐像素） | min 1.62 | 81/150 < 0.5 | ✅ 默认开（见 3） |

  1. **整块哈希对「宽 ROI 里的小变化」是瞎的**（这条推翻了上面两行）。合成反例：
     475px 宽的盲注 ROI 只改一个字符 —— 整块 17x16 的距离是 **0**，
     跟网格粗细**无关**（那个字符只占全宽 4%，怎么平均都是没有）。
     横切 4 片 → 3 bit，切 8 片 → 7 bit。所以默认 `TILES = 4`。
     真实帧上切 4 片后「变了」的最近一对从 12 抬到 29 bit。
  2. **门槛只能取 0**：切 4 片后「没变」的最远一对仍有 105，「变了」的最近 29 ——
     两个分布**重叠**，没有可分门槛，取任何正数都会放进真变化。
     代价是「命中」要求每片逐 bit 全同，只有真·静止的 ROI 才够格 —— 这正合期望。
  3. `mad_eps` **默认 0.2**（真机视频逼出来的，见下）。它的安全值随 ROI 宽度变：
     同一个「改一个字符」，475px 宽的长 ROI 上 MAD 只有 **0.47**，窄 ROI 上能到十几 ——
     所以 eps 要按**最宽的那块 ROI** 定，不能按中位数拍脑袋（0.5 那档就正好卡在
     这个反例上，会把它放过去 → 别照抄）。
     真机视频（213 帧 / 0.5s 抽帧）实测：精确 hash 只有 **0.5%** 命中（H.264 噪声
     的中位 MAD 是 0.8，比信号还大）→ 不开 MAD 等于没去重；开 0.2 后 **19%** 命中、
     **0 次真变化被复用**（这段视频里最小的真变化是盲注秒数 0.26，余量 1.3 倍）。
     再往上调（0.5/1.0/2.0）命中涨到 40–64%，但各有 28/56/170 次真变化被复用 —— 不能要。

**调门槛之前先量**：`tools/roi_probe.py --stream --max-distance N --mad-eps M`
会打印每帧命中数和「命中里距离最大的那次」—— 门槛必须小于实测的**最接近的一次真实变化**。
复用的读数带 `reused=True`，接口和探针都看得见。

关于画面噪声：静图样张是微信转发过的 JPEG（每帧独立编码），没有时序压缩噪声，
所以在那批数据上「静止 = 距离 0」成立。线上 screencapture 也是无损的，同理。
但**压缩视频输入**完全不同（实测见 3）—— H.264 每帧都有细小的编码差异，
精确匹配几乎全灭。若出现「明明没变却一直命不中」，先看输入是不是压缩过的。
"""

from __future__ import annotations

from concurrent.futures import Executor
from dataclasses import dataclass, field as dc_field
from time import perf_counter

import cv2
import numpy as np

from .dealer import DealerDetector
from .ocr import OcrEngine
from .profile import Profile
from .roi_reader import (
    GLOBAL_FIELDS, ROI_PAD, SEAT_FIELDS, RoiFrame, RoiRead, read_frame,
)

# 差分哈希的网格。宽度 w、高度 h → (w-1)*h 个 bit。
# 默认取**细网格**（17x16 = 256 bit）：9x8 那档在实测里把 2 处真变化读成了 0
# （见模块 docstring 的表格），安全起见用细的 —— 代价只是 hash 本身慢零点几毫秒，
# 而 hash 的耗时在 12.5ms 的 rec 面前可以忽略。
HASH_W = 17
HASH_H = 16

# 每块 ROI 横向切成几片、各算一个 hash，**每一片都相同**才算「没变」。
#
# 为什么非切不可（合成反例实测）：475px 宽的盲注 ROI 里只改一个字符，
# 整块 17x16 的 dhash 距离是 **0**（那个字符只占全宽 4%，被格子一平均就没了）
# —— 也就是说**整块哈希对「宽 ROI 里的小变化」是瞎的**，这跟网格粗细无关。
# 横向切 4 片后同一个变化给出 3 bit，切 8 片给 7 bit。
# 真实帧上「文本变了」的最小距离也从 12 抬到 29（切 4 片）。
# 代价：真·静止的 ROI 照样全部命中（每片都逐 bit 相同），只是「有一片在动」
# 的 ROI 不再蒙混过关 —— 那正是我们不想复用的那些。
TILES = 4

# 门槛默认 0：每片都逐 bit 相同才复用。理由见模块 docstring 的「偏向」。
DEFAULT_MAX_DISTANCE = 0

# MAD 兜底门槛。**视频实测**：H.264 压缩噪声的中位 MAD 是 0.8，精确 hash 在连续
# 帧流上只有 0.5% 命中 —— 不开 MAD 等于没去重。开 0.2 后 19% 命中、0 次真变化被复用；
# 而这段视频里最小的真变化（盲注秒数）是 0.26，仍有 1.3 倍余量。
# 注意这是**压缩视频**的取值；线上 screencapture 是无损的，精确匹配本来就够。
DEFAULT_MAD_EPS = 0.2


def to_gray(roi: np.ndarray) -> np.ndarray:
    """转灰度。dhash 和 MAD 两条判据都要用，所以只转一次。"""
    return cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)


def dhash_gray(gray: np.ndarray, w: int = HASH_W, h: int = HASH_H) -> int:
    """差分哈希：缩到 w×h → 逐行比较相邻像素（左 > 右 记 1）。

    要求 (w-1)*h 是 8 的倍数，好让 packbits 正好填满字节（17x16 → 256 bit → 32 字节）。
    """
    if ((w - 1) * h) % 8:
        raise ValueError(f"({w}-1)*{h} 必须是 8 的倍数，才能整字节打包")
    if gray.size == 0:
        return 0
    small = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).reshape(-1)
    return int.from_bytes(np.packbits(bits).tobytes(), "big")


def dhash(roi: np.ndarray, w: int = HASH_W, h: int = HASH_H) -> int:
    """roi（BGR）的差分哈希。比均值哈希抗亮度漂移 —— 本 app 里同一个人的名字
    在他弃牌后会**变暗**，纯像素差会把这种「文字没变」判成变了。"""
    return dhash_gray(to_gray(roi), w, h)


def hamming(a: int, b: int) -> int:
    """两个 hash 差多少个 bit。"""
    return (a ^ b).bit_count()


def dhash_tiles(roi: np.ndarray, tiles: int = TILES, w: int = HASH_W,
                h: int = HASH_H) -> tuple[int, ...]:
    """横向切成 tiles 片，每片各算一个 dhash。见 TILES 处为什么非切不可。

    太窄的 ROI（不足 8px/片）不切 —— 切了每片只剩几个像素，hash 会变成噪声。
    """
    width = roi.shape[1]
    if tiles <= 1 or width < tiles * 8:
        return (dhash(roi, w, h),)
    gray = to_gray(roi)
    edges = np.linspace(0, width, tiles + 1).astype(int)
    return tuple(dhash_gray(gray[:, edges[i]:edges[i + 1]], w, h)
                 for i in range(tiles))


def hash_distance(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """两组分块 hash 的距离 = **各片距离的最大值**（有一片不同就算变了）。

    片数对不上（换了 ROI 形状）→ inf，当成「变了」。
    """
    if len(a) != len(b):
        return 1 << 30
    return max((hamming(x, y) for x, y in zip(a, b)), default=0)


def pixel_mad(a: np.ndarray, b: np.ndarray) -> float:
    """同尺寸灰度图的逐像素平均绝对差（MAD）。

    hash 是**有损投影**，损失落在最要紧的地方：一个数字变了，在 64 bit hash 上
    可能只差 1 bit。MAD 不做投影，像素真的变了它就真的变。

    ⚠️ **但它这个「变」的幅度取决于 ROI 有多大** —— 同一个「改一个字符」，
    475px 宽的长 ROI 上只有 0.47（那个字符占全图 1/475），窄 ROI 上能到十几。
    所以拿它当门槛时，必须按**最宽的那块 ROI** 定 eps，否则最宽那块会静默放行。

    另一个方向的误报也不是没有：它对**亮度变化**同样敏感（实测同一个名字
    被弃牌变暗 → MAD 12），那种「文字没变」会被它判成变了 —— 只是多跑一次 rec，
    安全，但白花时间。
    """
    if a.shape != b.shape:
        return float("inf")
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


# ─────────────────────────── 缓存 ───────────────────────────

class RoiDedupCache:
    """按 cache_key 记住「上一帧的 hash」和「上一帧的读数」。

    只跟上一帧比（不是在历史里找最像的一帧）：抽帧场景下画面是单向推进的，
    跟更早的帧比会把「中间变过又变回来」误判成「一直没变」。

    两道判据，命中任一即「没变」：

      1. 分块 dhash 的距离 ≤ max_distance（默认 0，即每片逐 bit 完全一致）；
      2. mad_eps > 0 时，逐像素平均绝对差 ≤ mad_eps。

    第 2 道是为了捞回 hash 投影掉的小变化，**默认 0.2 开着**（真机视频上不开关等于
    没去重，见模块 docstring）。传 0.0 就退回纯 dhash。它有个形状依赖的坑，见
    pixel_mad 的注释：同一个「改一个字符」的变化，在 475px 宽的长 ROI 上 MAD 只有
    0.47，在窄 ROI 上却能到十几 —— 所以 **eps 要按最宽的那块 ROI 来定**，
    别按中位数拍脑袋。
    """

    def __init__(self, max_distance: int = DEFAULT_MAX_DISTANCE,
                 mad_eps: float = DEFAULT_MAD_EPS, tiles: int = TILES,
                 hash_w: int = HASH_W, hash_h: int = HASH_H) -> None:
        self.max_distance = int(max_distance)
        self.mad_eps = float(mad_eps)
        self.tiles = int(tiles)
        self.hash_w = int(hash_w)
        self.hash_h = int(hash_h)
        self._hash: dict[str, tuple[int, ...]] = {}
        self._read: dict[str, RoiRead] = {}
        self._gray: dict[str, np.ndarray] = {}       # 只在 mad_eps > 0 时填
        self.hits = 0
        self.hits_dhash = 0
        self.hits_mad = 0
        self.misses = 0
        self.max_hit_distance: int | None = None     # 命中里距离最大的那次（看门槛余量）
        self.max_hit_mad: float | None = None

    # —— RoiCache 协议（见 roi_reader.RoiCache）——

    def check(self, key: str, roi: np.ndarray) -> tuple[RoiRead | None, float]:
        """算分块 hash 并与上一帧比。命中返回上次的读数（调用方复制再标 reused）。"""
        t0 = perf_counter()
        h = dhash_tiles(roi, self.tiles, self.hash_w, self.hash_h)
        prev = self._hash.get(key)
        prev_gray = self._gray.get(key)
        gray = None
        if self.mad_eps > 0:
            gray = to_gray(roi)
            self._gray[key] = gray
        # 不管命中与否都记下这一帧的：下一帧要跟**这一帧**比
        self._hash[key] = h
        ms = (perf_counter() - t0) * 1000

        cached = self._read.get(key)
        if prev is not None and cached is not None:
            dist = hash_distance(prev, h)
            if dist <= self.max_distance:
                self._record_hit(dist, None)
                return cached, ms
            if self.mad_eps > 0 and prev_gray is not None and gray is not None:
                d = pixel_mad(prev_gray, gray)
                if d <= self.mad_eps:
                    self._record_hit(dist, d)
                    return cached, ms

        self.misses += 1
        return None, ms

    def _record_hit(self, dist: int, mad: float | None) -> None:
        self.hits += 1
        if mad is None:
            self.hits_dhash += 1
            if self.max_hit_distance is None or dist > self.max_hit_distance:
                self.max_hit_distance = dist
        else:
            self.hits_mad += 1
            if self.max_hit_mad is None or mad > self.max_hit_mad:
                self.max_hit_mad = mad

    def store(self, key: str, read: RoiRead) -> None:
        self._read[key] = read

    # —— 维护 ——

    def reset(self) -> None:
        """丢掉全部记忆（换分辨率 / 换配置 / 换牌桌时必须调，否则会跨场景复用读数）。"""
        self._hash.clear()
        self._read.clear()
        self._gray.clear()

    def reset_stats(self) -> None:
        self.hits = self.hits_dhash = self.hits_mad = self.misses = 0
        self.max_hit_distance = None
        self.max_hit_mad = None

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def to_debug(self) -> dict:
        d: dict = {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate, 4),
            "by_dhash": self.hits_dhash,
            "max_distance": self.max_distance,
            "tiles": self.tiles,
        }
        if self.mad_eps > 0:
            d["mad_eps"] = self.mad_eps
            d["by_mad"] = self.hits_mad
        if self.max_hit_distance is not None:
            d["max_hit_distance"] = self.max_hit_distance
        if self.max_hit_mad is not None:
            d["max_hit_mad"] = round(self.max_hit_mad, 3)
        return d


# ─────────────────────────── 抽帧入口 ───────────────────────────

@dataclass
class StreamStats:
    frames: int = 0
    roi_total: int = 0
    roi_reused: int = 0
    resets: int = 0

    def to_debug(self) -> dict:
        d = {
            "frames": self.frames,
            "roi_total": self.roi_total,
            "roi_reused": self.roi_reused,
            "reuse_rate": round(self.roi_reused / self.roi_total, 4) if self.roi_total else 0.0,
        }
        if self.resets:
            d["resets"] = self.resets
        return d


class RoiStream:
    """抽帧循环的入口：一帧一帧喂进来，没变的 ROI 直接复用上一帧的读数。

        stream = RoiStream(profile, engine, dealer_detector)
        while True:
            frame = stream.read(screencapture(), frame_id=...)

    它替调用方记三件事：ROI 缓存的记忆、累计命中率、以及**签名**。
    """

    def __init__(self, profile: Profile, engine: OcrEngine,
                 dealer_detector: DealerDetector | None = None, *,
                 max_distance: int = DEFAULT_MAX_DISTANCE, mad_eps: float = DEFAULT_MAD_EPS,
                 tiles: int = TILES, hash_w: int = HASH_W, hash_h: int = HASH_H,
                 seat_fields: tuple[str, ...] = SEAT_FIELDS,
                 global_fields: tuple[str, ...] = GLOBAL_FIELDS,
                 mode: str = "raw", upscale: float = 1.0,
                 pad: float = ROI_PAD, with_dealer: bool = True,
                 dedup: bool = True,
                 executor: Executor | None = None) -> None:
        self.profile = profile
        self.engine = engine
        # 帧内 ROI rec 的线程池（None = 串行）。由调用方持有，RoiStream 只借用
        self.executor = executor
        self.dealer_detector = dealer_detector
        self.seat_fields = tuple(seat_fields)
        self.global_fields = tuple(global_fields)
        self.mode = mode
        self.upscale = upscale
        self.pad = pad
        self.with_dealer = with_dealer
        # dedup=False 时走 read_frame(cache=None) 的无去重路径。
        # 调用方仍按原来的方式访问 self.cache，所以这里保留属性、值为 None。
        self.cache = (RoiDedupCache(max_distance, mad_eps, tiles, hash_w, hash_h)
                      if dedup else None)
        self.stats = StreamStats()
        self._signature: tuple | None = None

    def _signature_of(self, img: np.ndarray) -> tuple:
        """决定「这份缓存还能不能用」的全部输入。

        **必须包含分辨率**：换了分辨率（比如模拟器改了缩放），槽位换算出来的
        裁剪区就不是原来那块了，可 hash 照样可能撞上 → 复用一份错位的读数。
        """
        cache_sig = ((self.cache.max_distance, self.cache.mad_eps, self.cache.tiles,
                      self.cache.hash_w, self.cache.hash_h)
                     if self.cache is not None else None)
        return (str(self.profile.path), img.shape[0], img.shape[1], self.mode,
                self.upscale, self.pad, self.seat_fields, self.global_fields,
                cache_sig)

    def read(self, img: np.ndarray, frame_id: str = "frame") -> RoiFrame:
        sig = self._signature_of(img)
        if sig != self._signature:
            if self._signature is not None and self.cache is not None:
                # 不是第一帧：参数变了，旧记忆一律作废
                self.cache.reset()
                self.stats.resets += 1
            self._signature = sig

        frame = read_frame(img, self.profile, self.engine, frame_id,
                           seat_fields=self.seat_fields,
                           global_fields=self.global_fields,
                           mode=self.mode, upscale=self.upscale, pad=self.pad,
                           dealer_detector=self.dealer_detector,
                           with_dealer=self.with_dealer, cache=self.cache,
                           executor=self.executor)

        self.stats.frames += 1
        self.stats.roi_total += frame.n_roi
        self.stats.roi_reused += frame.n_reused
        return frame

    def reset(self) -> None:
        """开始新的一手/新的一张桌子时调它 —— 别让上一手的读数漏进来。"""
        if self.cache is not None:
            self.cache.reset()
