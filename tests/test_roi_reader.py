#!/usr/bin/env python3
"""ROI 读取器的单元测试：合成图 + 假 OCR 引擎，不依赖模型和照片。

重点覆盖三件**安静错**的事：

1. **有没有偷偷跑全图 det** —— 假引擎的 read_boxes 一调就抛异常。只跑 rec 是这一层
   速度的全部来源，如果哪天有人顺手加了 det，这里必须先炸。
2. **每块 ROI 裁的框是不是配置里那个槽位** —— 裁剪偏了不会报错，只会读成另一个东西。
   假引擎从裁剪图的像素里反解出裁剪原点，再和 slot_to_px 对一遍。
3. **老配置（没有 bet_slot/action_slot/global_slots）降级时不许崩**，要给出「这块不支持」
   的显式 note —— 静默返回空文本会被下游当成「画面里没有」。

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_roi_reader.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.profile import Profile  # noqa: E402
from poker_ocr.roi_reader import (  # noqa: E402
    GLOBAL_FIELDS, ROI_PAD, SEAT_FIELDS, RoiRead, measure_hero_turn,
    read_avatars, read_frame,
)

WPK = ROOT / "config" / "table_wpk_1080x2400.json"
LEGACY = ROOT / "config" / "table_default.json"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


# 毛毡绿（BGR），和真图实测一致；预设按钮是浅灰 → 低饱和 + 高亮度
FELT = (80, 102, 2)
DISC = (200, 200, 200)


# ─────────────────────── 合成图 + 假引擎 ───────────────────────

# 高度压到 256 以下：像素编码里 y 只占一个通道，测试图不需要真实比例
# （坐标全是归一化的，actual size 是多少都行）。
SIZE = (1080, 240)


def make_marked(size=SIZE) -> np.ndarray:
    """把每个像素的 (x, y) 编进颜色里：B=x 低 8 位、G=x 高 8 位、R=y。

    这样假引擎拿到裁剪图后能从左上角一个像素反解出「我被裁在哪儿」，
    裁剪坐标就不再是自己报自己、而是**从图里量出来的**。
    """
    w, h = size
    img = np.zeros((h, w, 3), dtype=np.uint8)
    xs = np.arange(w)
    img[:, :, 0] = xs % 256
    img[:, :, 1] = (xs // 256) % 256
    img[:, :, 2] = (np.arange(h) % 256)[:, None]
    return img


class StubEngine:
    """假 OCR：read_text 查表，read_boxes 直接炸。"""

    def __init__(self, table: dict[tuple[int, int, int, int], list[tuple[str, float]]]):
        self.table = table
        self.crops: list[tuple[int, int, int, int]] = []

    def read_text(self, roi: np.ndarray) -> list[tuple[str, float]]:
        b, g, r = (int(v) for v in roi[0, 0])
        h, w = roi.shape[:2]
        rect = (b + 256 * g, r, b + 256 * g + w, r + h)
        self.crops.append(rect)
        return self.table.get(rect, [])

    def read_boxes(self, img: np.ndarray):
        raise AssertionError("ROI 路径不许跑全图 det（read_boxes 被调用了）")


def build_table(profile: Profile, size=SIZE) -> dict:
    """把每个槽位该读出什么文本编好 —— 文本里带座位和字段名，串线了一眼能看出来。"""
    table = {}
    for seat in profile.seats:
        for key in SEAT_FIELDS:
            slot = {"name": seat.name_slot, "chip": seat.chip_slot,
                    "bet": seat.bet_slot, "action": seat.action_slot}.get(key)
            if slot is None:
                continue
            rect = profile.slot_to_px(slot, size, pad=ROI_PAD)
            table[rect] = [(f"{seat.seat_id}:{key}", 0.9)]
    for key in GLOBAL_FIELDS:
        slot = profile.global_slots.get(key)
        if slot is None:
            continue
        table[profile.slot_to_px(slot, size, pad=ROI_PAD)] = [(key, 0.8)]
    # hero_turn 的探针按 pad=0 裁（见 measure_hero_turn），表里也照这个口径
    turn_slot = profile.global_slots.get("hero_turn_probe")
    if turn_slot is not None:
        table[profile.slot_to_px(turn_slot, size, pad=0.0)] = [("15BB", 0.9)]
    return table


# ─────────────────────────── 测试 ───────────────────────────

def test_read_avatars_reads_all_nine_seats() -> None:
    """头像区按 9 个目标框裁（pad=0），文本和分数原样透传。"""
    profile = Profile.load(WPK)
    img = make_marked()
    table = {}
    for seat in profile.seats:
        rect = profile.slot_to_px(seat.avatar_slot, SIZE, pad=0.0)
        table[rect] = [(f"{seat.seat_id}:avatar", 0.9)]
    engine = StubEngine(table)

    got = read_avatars(img, profile, engine, detect_fold=True)
    check(len(got) == 9, f"应读 9 个头像，实际 {len(got)}")
    for seat in profile.seats:
        check(got.get(seat.seat_id) == (f"{seat.seat_id}:avatar", 0.9),
              f"{seat.seat_id} 的头像读数不对：{got.get(seat.seat_id)}")
    check(read_avatars(img, profile, engine, detect_fold=False) == {},
          "detect_fold=False 时不该读头像")


def test_only_rec_is_ever_called() -> None:
    """全图 det 一次都不许跑 —— read_boxes 抛异常，跑通即证明没调过。"""
    profile = Profile.load(WPK)
    img = make_marked()
    engine = StubEngine(build_table(profile))
    frame = read_frame(img, profile, engine, with_dealer=False)
    check(len(frame.seats) == 9, f"座位数应为 9，实际 {len(frame.seats)}")


def test_each_roi_is_cropped_at_the_configured_slot() -> None:
    """每块 ROI 的裁剪框必须等于配置槽位换算出的像素框（从图里量出来对账）。"""
    profile = Profile.load(WPK)
    img = make_marked()
    table = build_table(profile)
    engine = StubEngine(table)
    frame = read_frame(img, profile, engine, with_dealer=False)

    for seat in frame.seats:
        for key, read in seat.fields.items():
            check(read.rect in table,
                  f"{seat.seat_id}.{key} 裁到了 {read.rect}，不是任何配置槽位")
    for key, read in frame.globals.items():
        check(read.rect in table, f"全局 {key} 裁到了 {read.rect}，不是配置槽位")

    # 假引擎记录的裁剪框就是它实际收到的图 —— 和上报的 rect 必须一致
    reported = {r.rect for s in frame.seats for r in s.fields.values()}
    reported |= {r.rect for r in frame.globals.values()}
    turn_rect = frame.hero_turn_evidence.get("rect")
    if turn_rect:
        reported.add(tuple(turn_rect))
    check(set(engine.crops) == reported,
          f"引擎实际收到的裁剪和上报的不一致：{set(engine.crops) ^ reported}")


def test_hero_chip_alt_slot_only_when_primary_is_empty() -> None:
    """hero 没手牌时筹码胶囊贴在头像下方；主槽空才读备选槽。"""
    profile = Profile.load(WPK)
    hero = profile.seat("hero")
    check(hero.chip_slot_alt is not None, "WPK 配置应给 hero 配 chip_slot_alt")
    img = make_marked()
    alt_rect = profile.slot_to_px(hero.chip_slot_alt, SIZE, pad=ROI_PAD)

    # 主槽有读数 → 绝不碰备选槽（有手牌时筹码在最底部）
    engine = StubEngine(build_table(profile))
    read_frame(img, profile, engine, with_dealer=False)
    check(alt_rect not in engine.crops,
          "主 chip 槽读到文本时不该再读 chip_slot_alt")

    # 主槽空、备选槽有读数 → 用备选槽
    table = build_table(profile)
    primary = profile.slot_to_px(hero.chip_slot, SIZE, pad=ROI_PAD)
    table[primary] = []
    table[alt_rect] = [("182.5BB", 0.9)]
    frame = read_frame(img, profile, StubEngine(table), with_dealer=False)
    read = frame.seat("hero").fields["chip"]
    check(read.text == "182.5BB", f"应回退到备选槽，实际 {read.text!r}")
    check("chip_slot_alt" in (read.note or ""),
          f"回退时应留下 note，实际 {read.note!r}")


def test_seat_order_starts_at_hero_and_text_is_passed_through() -> None:
    """座位从 hero 起按环序（下游靠这个顺序读行动线），文本原样透传不加工。"""
    profile = Profile.load(WPK)
    engine = StubEngine(build_table(profile))
    frame = read_frame(make_marked(), profile, engine, with_dealer=False)

    order = [s.seat_id for s in frame.seats]
    check(order == [s.seat_id for s in profile.seats_in_ring_order],
          f"座位顺序不对：{order}")
    check(order[0] == profile.hero_seat_id, f"第一个应是 hero，实际 {order[0]}")

    for seat in frame.seats:
        for key in SEAT_FIELDS:
            want = f"{seat.seat_id}:{key}"
            got = seat.fields[key].text
            check(got == want, f"{seat.seat_id}.{key} 读到 {got!r}，期望 {want!r}")

    check(frame.globals["pot"].text == "pot",
          f"全局 pot 读到 {frame.globals['pot'].text!r}")


def turn_engine(profile: Profile, text: str, score: float) -> StubEngine:
    """给 hero_turn 的探针槽单独编一个读数（其他槽位照常查表）。"""
    table = build_table(profile)
    slot = profile.global_slots["hero_turn_probe"]
    table[profile.slot_to_px(slot, SIZE, pad=0.0)] = ([(text, score)] if text else [])
    return StubEngine(table)


def test_hero_turn_by_button_text() -> None:
    """轮到我：读第一个预设钮的 BB 值行，三态。

    真机视频推翻了「浅灰圆片占比」那套（表情面板误报、设备变暗漏报），
    所以现在的判据是文本：**BB 形文本 + 分数够 = True**；空 = False；
    有内容但不是 BB 形（表情面板 / 遮挡）= None，不硬判。
    """
    profile = Profile.load(WPK)
    img = make_marked()

    turn, ev = measure_hero_turn(img, profile, turn_engine(profile, "15BB", 0.97))
    check(turn is True, f"『15BB』应判 True，实际 {turn}（{ev}）")

    turn, ev = measure_hero_turn(img, profile, turn_engine(profile, "2.5BB", 0.99))
    check(turn is True, f"『2.5BB』应判 True，实际 {turn}（{ev}）")

    turn, ev = measure_hero_turn(img, profile, turn_engine(profile, "", 0.0))
    check(turn is False, f"空读数（那块没东西）应判 False，实际 {turn}（{ev}）")

    # 表情面板伪装：真有内容、但读出来不是 BB 形 → 不能当成「没轮到」
    turn, ev = measure_hero_turn(img, profile, turn_engine(profile, "Q?", 0.21))
    check(turn is None, f"非 BB 形文本应判 None，实际 {turn}（{ev}）")

    # 形状对但分数太低（设备变暗/糊）→ 也不硬判
    turn, ev = measure_hero_turn(img, profile, turn_engine(profile, "15BB", 0.3))
    check(turn is None, f"分数低于门槛应判 None，实际 {turn}（{ev}）")


def test_hero_turn_unknown_without_the_slot() -> None:
    """老配置没有 hero_turn_probe：报 None + 原因，不能猜。"""
    profile = Profile.load(LEGACY)
    turn, ev = measure_hero_turn(np.zeros((100, 100, 3), dtype=np.uint8), profile,
                                 StubEngine({}))
    check(turn is None, f"没有槽位应判 None，实际 {turn}")
    check("error" in ev, f"应给出原因，实际 {ev}")


def test_score_is_the_worst_box_and_text_is_concatenated() -> None:
    """一块 ROI 出多框时：分数取最低（和 read_name 同口径），文本拼接。"""
    read = RoiRead("pot", (0, 0, 1, 1), parts=[("总底池", 0.95), ("15BB", 0.71)])
    check(read.text == "总底池15BB", f"拼接结果 {read.text!r}")
    check(abs(read.score - 0.71) < 1e-9, f"分数应取最低 0.71，实际 {read.score}")


def test_legacy_profile_degrades_with_a_note_not_a_crash() -> None:
    """老 profile 没有 bet_slot/action_slot/global_slots：要显式说「不支持」。"""
    profile = Profile.load(LEGACY)
    engine = StubEngine({})
    frame = read_frame(make_marked(), profile, engine, with_dealer=False)

    seat = frame.seats[0]
    check("action" not in seat.fields,
          "action 不该单独读（徽章文本在 name ROI 里，理由见模块 docstring）")
    read = seat.fields["bet"]
    check(read.note is not None and "不在这个 profile" in read.note,
          f"老配置的 bet 字段应带「不支持」的 note，实际 {read.note!r}")
    check(read.text == "", "老配置的 bet 不该读出文本")
    for key in GLOBAL_FIELDS:
        check(frame.globals[key].note is not None,
              f"老配置的全局 {key} 应有 note")
    check(frame.hero_turn is None, "没有 bet_size_presets 时 hero_turn 应为 None")


def test_crops_never_leave_the_image() -> None:
    """槽位贴边（lowL 的名字槽 x 从 0.0 起）时裁剪框不能越界。"""
    profile = Profile.load(WPK)
    engine = StubEngine(build_table(profile))
    frame = read_frame(make_marked(), profile, engine, with_dealer=False)
    w, h = SIZE
    for seat in frame.seats:
        for key, read in seat.fields.items():
            x1, y1, x2, y2 = read.rect
            check(0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h,
                  f"{seat.seat_id}.{key} 裁剪框越界：{read.rect}")


def test_to_dict_and_strip() -> None:
    """调试输出：带 debug 时给 rect/ms，strip 掉之后只剩 text/score。"""
    profile = Profile.load(WPK)
    engine = StubEngine(build_table(profile))
    frame = read_frame(make_marked(), profile, engine, with_dealer=False)

    full = frame.to_dict()
    check("rect" in full["seats"][0]["fields"]["name"], "完整输出应带 rect")
    # build_table 给探针槽编的是 "15BB"/0.9 → 轮到我
    check(full["hero_turn"] is True,
          f"探针读到『15BB』应判 True，实际 {full['hero_turn']}")

    lean = frame.to_dict(include_debug=False)
    check(lean["seats"][0]["fields"]["name"].keys() == {"text", "score"},
          f"strip 后应只剩 text/score，实际 {lean['seats'][0]['fields']['name'].keys()}")


def main() -> int:
    tests = [
        test_read_avatars_reads_all_nine_seats,
        test_only_rec_is_ever_called,
        test_each_roi_is_cropped_at_the_configured_slot,
        test_hero_chip_alt_slot_only_when_primary_is_empty,
        test_seat_order_starts_at_hero_and_text_is_passed_through,
        test_hero_turn_by_button_text,
        test_hero_turn_unknown_without_the_slot,
        test_score_is_the_worst_box_and_text_is_concatenated,
        test_legacy_profile_degrades_with_a_note_not_a_crash,
        test_crops_never_leave_the_image,
        test_to_dict_and_strip,
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
    print("\n✅ ROI 读取器全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
