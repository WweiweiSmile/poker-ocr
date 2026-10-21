"""庄位检测。

**「9 个座位里有且仅有一个庄位」是牌局的事实，不是检测器的能力。**
所以 DealerResult 允许 seat_id 为 None、允许 status="ambiguous"，绝不用「取最大响应」
把「没找到」或「找到俩」伪装成「找到了」。

庄位在界面上的样子：**白底圆形 + 深色「D」**，画在庄家头像旁边。
判定规则用户给的：**离哪个头像最近就是谁的庄**。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

from .profile import Profile, Size

FOUND = "found"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"

# D 圆片的几何先验。实测（table-wrap-d.jpg 1440x3200）：
#   真正的 D：50x50、area≈1894、circularity 0.900
#   干扰项：下注筹码 68-73px、area≈4700（太大）；文字笔画 20-25px、area 500-700（太小）
# 尺寸差一个量级，所以面积区间卡得很死也足够稳。
BLOB_DEFAULTS = {
    "v_min": 195,           # HSV 亮度下限（近白）
    "s_max": 45,            # HSV 饱和度上限（近灰，排除彩色）
    "area_min": 1200,       # ≈ 直径 39px
    "area_max": 2600,       # ≈ 直径 57px
    "circularity_min": 0.85,
    "aspect_tol": 0.3,      # |w/h - 1| 的上限，圆片应当接近正方形
    "max_seat_dist": 1.2,   # 到最近头像框的距离，按头像宽度归一化
}

# 第一档 OCR 以这个分数以上认出「D」就直接收工，不再跑后两档重试。
# 真机实测三档都在 0.9 以上，留一点余量给压缩噪声。
CONFIDENT_READ = 0.9

# 小画面先放大再做 blob 检测。低分辨率直播（实测 344x764）里 D 圆片直径只有
# ~11px，H.264 压缩后边缘全是锯齿，圆度/面积两条几何门会静默失守：
# 同一批 10 帧在原始尺寸上 0 帧检出，放大到短边 600px 后 10/10 检出。
# 面积门槛按整图面积归一化（见 _area_range），放大不会破坏它。
MIN_DETECT_SIDE = 600
MAX_PREP_SCALE = 3.0


@dataclass
class DealerCandidate:
    seat_id: str | None
    confidence: float
    center: tuple[float, float]
    center_norm: tuple[float, float]
    reads_d: bool
    ocr_text: str | None = None
    seat_dist: float | None = None
    detail: str = ""

    def to_debug(self) -> dict:
        d = {
            "seat_id": self.seat_id,
            "confidence": round(self.confidence, 4),
            "center": [round(self.center[0], 1), round(self.center[1], 1)],
            "reads_d": self.reads_d,
        }
        if self.ocr_text is not None:
            d["ocr_text"] = self.ocr_text
        if self.seat_dist is not None:
            d["seat_dist"] = round(self.seat_dist, 3)
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class DealerResult:
    seat_id: str | None
    status: str
    method: str
    confidence: float | None = None
    candidates: list[DealerCandidate] = dc_field(default_factory=list)

    def to_debug(self) -> dict:
        d = {"seat_id": self.seat_id, "status": self.status, "method": self.method}
        if self.confidence is not None:
            d["confidence"] = round(self.confidence, 4)
        if self.candidates:
            d["candidates"] = [c.to_debug() for c in self.candidates]
        return d


@runtime_checkable
class DealerDetector(Protocol):
    name: str

    def detect(self, img: np.ndarray, profile: Profile, size: Size) -> DealerResult:
        ...


# ─────────────────────────── 手动指定 ───────────────────────────

class ManualDealerDetector:
    """人工指定庄位。自动检测不确定时的兜底，也是它上线前的唯一途径。"""

    name = "manual"

    def __init__(self, seat_id: str | None) -> None:
        self.seat_id = seat_id

    def detect(self, img: np.ndarray, profile: Profile, size: Size) -> DealerResult:
        if not self.seat_id:
            return DealerResult(None, NOT_FOUND, self.name)
        try:
            seat = profile.seat(self.seat_id)
        except KeyError:
            return DealerResult(None, NOT_FOUND, self.name)
        return DealerResult(seat.seat_id, FOUND, self.name, confidence=1.0)


# ─────────────────────────── 白底 D 圆片 ───────────────────────────

class BlobDealerDetector:
    """找白底圆形 + 认「D」+ 归给最近的头像。

    为什么不做模板匹配：matchTemplate 对缩放、抗锯齿、毛毡渐变背景都很敏感，
    换个机型密度档位就可能失效。而「近白低饱和的圆片」这个特征跨机型稳定得多。

    三重过滤，每一重都能独立挡掉一类干扰：
      1. 区域门 —— 挡掉状态栏/播放器里的白色元素
      2. 几何门 —— 面积 + 圆度 + 长宽比，挡掉下注筹码和文字笔画
      3. 识别门 —— 认出「D」才算数（默认只降权不否决，见 ocr_required）
    """

    name = "blob"

    def __init__(self, cfg: dict | None = None, engine=None) -> None:
        self.cfg = {**BLOB_DEFAULTS, **(cfg or {})}
        self.engine = engine
        # 庄位按钮一手牌里只挪一次：同一块 D 圆片的识别结果可以跨帧复用。
        # 实测 `_reads_d` 占了单帧总耗时的 60%+（每帧 3 次 rec），而它读的
        # 就是同一个白底「D」。按 (工作图尺寸, bbox) 做键、按裁剪图做签名，
        # 位置或字形一变就自然失效。
        self._reads_cache: dict[tuple[int, int, int, int, int, int],
                                tuple[bytes, tuple[bool, str | None, float]]] = {}

    # ── 几何 ──

    def _area_range(self, profile: Profile, size: Size) -> tuple[float, float]:
        """面积门按**整图面积**归一化后再用。

        `area_min`/`area_max` 是在 `profile.reference_size` 上量出来的**绝对像素**。
        换分辨率时圆片面积按 k² 缩水，绝对门槛就会**静默失守**：1440×3200 上真 D 是
        area≈1894，到 1080×2400 只剩 1046，被 1200 的门槛挡掉 —— 表现出来是
        `status=not_found`，看着像「这帧没渲染庄位」，其实是门槛写死了。

        用**面积占比**而不是按宽度缩放：圆片是二维的，得用 k²；而且实测
        1440×3200 的 1894/(1440×3200)=4.11e-4 与 1080×2400 的 1046/(1080×2400)=4.04e-4
        只差 2% —— 说明这个口径本身跨分辨率就成立，不是巧合凑上的。
        """
        ref_w, ref_h = profile.reference_size
        k2 = (size[0] * size[1]) / float(ref_w * ref_h)
        return self.cfg["area_min"] * k2, self.cfg["area_max"] * k2

    def _find_blobs(self, img: np.ndarray, profile: Profile,
                    size: Size) -> list[tuple[tuple[float, float], float, tuple[int, int, int, int]]]:
        area_min, area_max = self._area_range(profile, size)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = ((hsv[:, :, 2] > self.cfg["v_min"]) &
                (hsv[:, :, 1] < self.cfg["s_max"])).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in contours:
            area = cv2.contourArea(c)
            if not (area_min <= area <= area_max):
                continue
            perim = cv2.arcLength(c, True)
            if perim <= 0:
                continue
            # 先平滑轮廓再量圆度。**视频输入必须这么做**：H.264 会把白圆片的
            # 边缘磨出锯齿，周长被抬高 → 圆度从 0.895 掉到 0.724–0.835，
            # 被门槛 0.85 误杀（真机视频实测 171 帧里只有 1 帧过闸）。
            # 2% 周长的多边形逼近把锯齿抹掉，同一条真 D 的圆度回到 0.890–0.949，
            # 而其它小圆候选仍在 0.85 附近徘徊（实测 282 个里只有 18 个过，
            # 且还要过「读出 D」那一关）。
            smooth = cv2.approxPolyDP(c, 0.02 * perim, True)
            sp = cv2.arcLength(smooth, True)
            sa = cv2.contourArea(smooth)
            circularity = 4 * np.pi * sa / (sp * sp) if sp > 0 else 0.0
            if circularity < self.cfg["circularity_min"]:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if h == 0 or abs(w / h - 1) > self.cfg["aspect_tol"]:
                continue
            cx, cy = x + w / 2, y + h / 2
            if not profile.in_region_gate((cx / size[0], cy / size[1]), size):
                continue
            out.append(((cx, cy), circularity, (x, y, w, h)))
        return out

    # ── 识别 ──

    def _reads_d(self, img: np.ndarray,
                 bbox: tuple[int, int, int, int],
                 accepted: set[str] | None = None,
                 ) -> tuple[bool, str | None, float]:
        """按**实际外接框**裁剪，只留一点边。

        别按 area_max 固定裁一大块 —— 实测那样四周全是毛毡，白色圆片缩成一个小点，
        识别器会把它读成「·」而不是「D」。贴合裁剪才能认对（0.97）。
        """
        if self.engine is None:
            return False, None, 0.0
        x, y, w, h = bbox
        pad = max(3, int(min(w, h) * 0.15))
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(img.shape[1], x + w + pad)
        y2 = min(img.shape[0], y + h + pad)
        roi = img[y1:y2, x1:x2]
        if roi.size == 0:
            return False, None, 0.0

        # 同一个位置、同一块画面 → 上一帧已经认过的结果直接复用
        key = (img.shape[0], img.shape[1], x, y, w, h)
        sig = self._roi_signature(roi)
        cached = self._reads_cache.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]

        from .ocr import preprocess_variant

        # 圆片里就一个字母。去掉空白和分隔符后应当是 D（大小写都认）。
        # 全图路线会传入更宽的 accepted（0/O 等压缩后形近读法），
        # ROI 路线保持默认只认 D。
        accepted = accepted or {"D"}
        best_text, best_score = None, 0.0
        for upscale, mode in ((1.0, "raw"), (3.0, "raw"), (3.0, "gray")):
            try:
                hits = self.engine.read_text(preprocess_variant(roi, mode, upscale))
            except Exception:
                continue
            if not hits:
                continue
            text = "".join(t for t, _ in hits).strip()
            score = min(s for _, s in hits)
            norm = "".join(ch for ch in text if ch.isalnum()).upper()
            # 第一档（原尺寸 raw）就已经以高置信度认对时，不必再跑另外两档。
            # 实测真机三档分数 0.96/0.95/0.93，后两档只会重复读同一个 D。
            if norm in accepted and score >= CONFIDENT_READ:
                result = (True, text, score)
                self._remember(key, sig, result)
                return result
            if score > best_score:
                best_text, best_score = text, score

        norm = ("".join(ch for ch in best_text if ch.isalnum()).upper()
                if best_text else "")
        result = (norm in accepted, best_text, best_score)
        self._remember(key, sig, result)
        return result

    @staticmethod
    def _roi_signature(roi: np.ndarray) -> bytes:
        """裁剪图的粗指纹。只用来判断「还是不是同一块 D」，轻微压缩噪声不该
        造成重读，所以缩到 16×16；真换了位置/字形时字节必然不同。"""
        return cv2.resize(roi, (16, 16), interpolation=cv2.INTER_AREA).tobytes()

    def _remember(self, key: tuple, sig: bytes,
                  result: tuple[bool, str | None, float]) -> None:
        if len(self._reads_cache) >= 64:
            # 只在换桌/换分辨率时可能积累到这么大，整份丢掉最简单
            self._reads_cache.clear()
        self._reads_cache[key] = (sig, result)

    # ── 归座 ──

    def _nearest_seat(self, profile: Profile, size: Size,
                      center: tuple[float, float]) -> tuple[str | None, float]:
        """离哪个头像最近就是谁的庄。

        用**到头像框的矩形距离**而不是到中心的距离：D 贴在头像边上，
        矩形距离对「贴边」这种情况更贴合直觉，实测区分度也更好
        （庄家 0.61 个头像宽 vs 最近的邻居 1.9，差 3 倍）。
        """
        best_id, best_d = None, float("inf")
        for seat in profile.seats:
            x1, y1, x2, y2 = profile.slot_to_px(seat.avatar_slot, size)
            dx = max(x1 - center[0], 0, center[0] - x2)
            dy = max(y1 - center[1], 0, center[1] - y2)
            dist = float(np.hypot(dx, dy))
            norm = dist / max(1.0, x2 - x1)
            if norm < best_d:
                best_id, best_d = seat.seat_id, norm
        return best_id, best_d

    # ── 预处理 ──

    def _prepare(self, img: np.ndarray, size: Size) -> tuple[np.ndarray, Size, float]:
        """短边太小就放大。返回 (工作图, 工作尺寸, 缩放倍数)。

        只影响几何检测的像素分辨率；结果坐标最后会除回原尺寸，
        所以调用方拿到的 center 仍然是原图坐标。
        """
        w, h = size
        shortest = min(w, h)
        if shortest <= 0:
            return img, size, 1.0
        scale = min(MAX_PREP_SCALE, MIN_DETECT_SIDE / shortest)
        if scale <= 1.0:
            return img, size, 1.0
        work = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        return work, (work.shape[1], work.shape[0]), scale

    # ── 主入口 ──

    def detect(self, img: np.ndarray, profile: Profile, size: Size) -> DealerResult:
        work, work_size, scale = self._prepare(img, size)
        blobs = self._find_blobs(work, profile, work_size)

        candidates: list[DealerCandidate] = []
        for center, circularity, bbox in blobs:
            reads_d, text, ocr_score = self._reads_d(work, bbox)
            seat_id, seat_dist = self._nearest_seat(profile, work_size, center)
            far = seat_dist > self.cfg["max_seat_dist"]
            candidates.append(DealerCandidate(
                seat_id=None if far else seat_id,
                confidence=circularity * (ocr_score if reads_d else 0.5),
                center=(center[0] / scale, center[1] / scale),
                center_norm=(center[0] / work_size[0], center[1] / work_size[1]),
                reads_d=reads_d,
                ocr_text=text,
                seat_dist=seat_dist,
                detail="离最近头像太远，不认为是庄位" if far else "",
            ))

        if not candidates:
            return DealerResult(None, NOT_FOUND, self.name)

        # 认出「D」的优先
        verified = [c for c in candidates if c.reads_d and c.seat_id]
        if len(verified) == 1:
            return DealerResult(verified[0].seat_id, FOUND, self.name,
                                confidence=verified[0].confidence, candidates=candidates)
        if len(verified) > 1:
            # 只可能是画面里真有两个 D —— 报歧义，不猜
            return DealerResult(None, AMBIGUOUS, self.name, candidates=candidates)

        # 没有认出 D 的：几何上只有一个候选就认了，多个则报歧义
        usable = [c for c in candidates if c.seat_id]
        if len(usable) == 1:
            return DealerResult(usable[0].seat_id, FOUND, self.name,
                                confidence=usable[0].confidence * 0.6,
                                candidates=candidates)
        if len(usable) > 1:
            return DealerResult(None, AMBIGUOUS, self.name, candidates=candidates)
        return DealerResult(None, NOT_FOUND, self.name, candidates=candidates)


# ─────────────────────────── 注册与构建 ───────────────────────────

DETECTORS: dict[str, type] = {
    ManualDealerDetector.name: ManualDealerDetector,
    BlobDealerDetector.name: BlobDealerDetector,
}


def build_detector(profile: Profile, manual_seat: str | None, engine=None) -> DealerDetector:
    """挑一个检测器。

    手动指定永远优先 —— 它同时也是自动检测报 ambiguous 时的兜底。
    """
    if manual_seat:
        return ManualDealerDetector(manual_seat)

    cfg = profile.dealer_cfg or {}
    method = cfg.get("method", "manual")
    if method == "blob" and cfg.get("enabled", True):
        return BlobDealerDetector(cfg.get("blob"), engine=engine)
    return ManualDealerDetector(None)
