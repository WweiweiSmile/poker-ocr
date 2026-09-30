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

    # ── 几何 ──

    def _find_blobs(self, img: np.ndarray, profile: Profile,
                    size: Size) -> list[tuple[tuple[float, float], float, tuple[int, int, int, int]]]:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = ((hsv[:, :, 2] > self.cfg["v_min"]) &
                (hsv[:, :, 1] < self.cfg["s_max"])).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in contours:
            area = cv2.contourArea(c)
            if not (self.cfg["area_min"] <= area <= self.cfg["area_max"]):
                continue
            perim = cv2.arcLength(c, True)
            if perim <= 0:
                continue
            circularity = 4 * np.pi * area / (perim * perim)
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
                 bbox: tuple[int, int, int, int]) -> tuple[bool, str | None, float]:
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

        from .ocr import preprocess_variant

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
            if score > best_score:
                best_text, best_score = text, score

        if not best_text:
            return False, None, 0.0
        # 圆片里就一个字母。去掉空白和分隔符后应当是 D（大小写都认）
        norm = "".join(ch for ch in best_text if ch.isalnum())
        return norm.upper() == "D", best_text, best_score

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

    # ── 主入口 ──

    def detect(self, img: np.ndarray, profile: Profile, size: Size) -> DealerResult:
        blobs = self._find_blobs(img, profile, size)

        candidates: list[DealerCandidate] = []
        for center, circularity, bbox in blobs:
            reads_d, text, ocr_score = self._reads_d(img, bbox)
            seat_id, seat_dist = self._nearest_seat(profile, size, center)
            far = seat_dist > self.cfg["max_seat_dist"]
            candidates.append(DealerCandidate(
                seat_id=None if far else seat_id,
                confidence=circularity * (ocr_score if reads_d else 0.5),
                center=center,
                center_norm=(center[0] / size[0], center[1] / size[1]),
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
