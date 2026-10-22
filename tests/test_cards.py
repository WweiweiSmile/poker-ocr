#!/usr/bin/env python3
"""公共牌 / hero 手牌识别的回归测试。

用 images/card 下 16 个标注样本（15 张唯一画面 + redA）逐张校验：
公共牌、hero 手牌、以及「颜色与形状两条证据是否一致」。

这些样本覆盖了识别最容易翻车的几种状态：
  * 三/四/五张公共牌（居中对齐，位置会变）
  * hero 弃牌后牌面变灰（V≈103，不是白色）
  * 翻前没有公共牌、且被活动弹窗盖住
  * 同一手牌的多个街、同一张图的两个副本

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_cards.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poker_ocr.cards as cards_mod  # noqa: E402
from poker_ocr.cards import (  # noqa: E402
    RANKS, _classify_suit, _suit_from_shape, recognize_cards,
    recognize_cards_of_file,
)
from poker_ocr.profile import Profile  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


CARD_DIR = ROOT / "images" / "card"
PROFILE_PATH = ROOT / "config" / "table_wpk_1080x2400.json"
DEFAULT_PROFILE_PATH = ROOT / "config" / "table_default.json"
SHOW_PROFILE_PATH = ROOT / "config" / "table_wpk_live.json"

# key -> (公共牌, hero 手牌)，均按画面从左到右。
# key 取文件名里的序号；redA.jpg 单独命名。
GROUND_TRUTH: dict[str, tuple[list[str], list[str]]] = {
    "138": (["3d", "Qc", "Ad"], ["5h", "Ks"]),
    "139": (["3d", "Qc", "Ad"], ["5h", "Ks"]),
    "140": (["Ac", "6h", "4s", "10d"], ["2c", "8s"]),
    "141": (["Kc", "9s", "6c"], ["4h", "Qs"]),
    "142": (["5d", "2h", "Jc", "Kc"], ["2c", "6c"]),
    "143": (["Kc", "3s", "Qd"], ["Kh", "3h"]),
    "144": (["5h", "Qd", "Jd"], ["9d", "Qh"]),
    "145": ([], ["9d", "Qh"]),
    "146": (["9h", "8s", "Ad", "Jh", "10s"], ["Js", "5c"]),
    "147": (["9h", "8s", "Ad", "Jh"], ["Js", "5c"]),
    "148": (["9h", "8s", "Ad"], ["Js", "5c"]),
    "149": ([], ["Kd", "8h"]),
    "150": (["5s", "2d", "Kc"], ["9s", "10d"]),
    "151": (["5s", "2d", "Kc"], ["9s", "10d"]),
    "152": (["7d", "9c", "10c", "6h", "8c"], ["4c", "Ks"]),
    "redA": (["3s", "2h", "5s"], ["Ah", "Qd"]),
}


def _key(path: Path) -> str:
    if path.stem.startswith("redA"):
        return "redA"
    return path.stem.split("_")[2]


def _unique_files() -> dict[str, Path]:
    """同内容只留一份（138/139 各有一个 (1) 副本）。"""
    seen: dict[str, str] = {}
    out: dict[str, Path] = {}
    for path in sorted(CARD_DIR.glob("*.jpg")):
        # show_card.jpg 是摊牌亮牌样张，不是「公共牌 + hero」标注样本
        if not (path.stem.startswith("微信图片") or path.stem.startswith("redA")):
            continue
        key = _key(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen[digest] = key
        out[key] = path
    return out


def test_all_labeled_samples() -> None:
    """逐张比对公共牌与 hero 手牌。"""
    profile = Profile.load(PROFILE_PATH)
    files = _unique_files()
    check(set(files) == set(GROUND_TRUTH),
          f"样本集合与标注不一致：多 {set(files) - set(GROUND_TRUTH)}，"
          f"少 {set(GROUND_TRUTH) - set(files)}")

    for key, path in sorted(files.items()):
        img = cv2.imread(str(path))
        check(img is not None, f"{key}: 读不了 {path}")
        if img is None:
            continue
        result = recognize_cards(img, profile)
        exp_board, exp_hero = GROUND_TRUTH[key]
        check(result.board_codes == exp_board,
              f"{key} 公共牌：得到 {result.board_codes}，期望 {exp_board}")
        check(result.hero_codes == exp_hero,
              f"{key} hero 手牌：得到 {result.hero_codes}，期望 {exp_hero}")


def test_color_and_shape_agree() -> None:
    """所有识别出的牌，颜色判定的花色应与形状判定一致。

    这是「颜色 + 形状」双证据的核心断言：只要有一条不一致，说明形状规则或
    颜色阈值有一边漂了，应该立刻看到，而不是被平均进置信度里。
    """
    profile = Profile.load(PROFILE_PATH)
    for key, path in sorted(_unique_files().items()):
        for slot_name in ("board", "hero_cards"):
            result = recognize_cards_of_file(path, profile)
            slot = result.board if slot_name == "board" else result.hero_cards
            for card in slot.cards:
                check(card.evidence.get("shape_agrees") is True,
                      f"{key} {slot_name} {card.code}: 颜色/形状不一致 "
                      f"{card.evidence.get('shape_suit')}")


def test_suit_shape_thresholds() -> None:
    """形状判定边界：K♣/Q♣/J♣ 的 solidity 会落在 0.860 附近，不能翻成黑桃。"""
    check(_suit_from_shape(0.8604, 0.02) == "c",
          "solidity 0.8604 应判梅花（K♣/Q♣/J♣ 的实测边界）")
    check(_suit_from_shape(0.866, 0.03) == "s",
          "solidity 0.866 应判黑桃（黑桃实测下界）")
    check(_suit_from_shape(0.925, -0.10) == "h", "质心偏上应判红桃")
    check(_suit_from_shape(0.97, 0.01) == "d", "高实心度且对称应判方块")


def test_shape_disagreement_does_not_reject_color() -> None:
    """颜色是硬判据：形状只在近似校验，不能把明确的花色打到稳定闸以下。"""
    with patch.object(cards_mod, "_suit_color",
                      return_value=("c", 0.96, {})), \
         patch.object(cards_mod, "_suit_shape",
                      return_value=("s", {})):
        suit, conf, ev = _classify_suit(np.zeros((10, 10, 3), np.uint8))
    check(suit == "c", f"形状不一致时应保留颜色判据，实际 {suit}")
    check(conf >= 0.65,
          f"形状不一致不应把置信度打到稳定闸以下，实际 {conf:.3f}")
    check(ev.get("shape_agrees") is False, "应保留 shape_agrees=False 供复核")


def test_default_profile_falls_back_to_wpk_slots() -> None:
    """没有 board/hero_cards 槽位的 profile 也要能读 —— API 默认就是这种。

    api.py 默认加载 table_default.json（浏览器版式），但同一批 1080x2400 截图
    仍应能读牌；此时走 DEFAULT_CARD_SLOTS 的归一化坐标。
    """
    default_profile = Profile.load(DEFAULT_PROFILE_PATH)
    check(not default_profile.global_slots.get("board"),
          "前提不成立：table_default.json 竟然有 board 槽位")

    key = "138"
    result = recognize_cards_of_file(_unique_files()[key], default_profile)
    check(result.board_codes == GROUND_TRUTH[key][0],
          f"默认 profile 回退后公共牌：得到 {result.board_codes}")
    check(result.hero_codes == GROUND_TRUTH[key][1],
          f"默认 profile 回退后 hero：得到 {result.hero_codes}")
    check(any("默认牌区" in w for w in result.warnings),
          "回退到默认牌区时应该给出 warning")


def test_preflop_has_no_false_positive() -> None:
    """翻前（无公共牌、被活动弹窗盖住）必须返回空，不能硬凑出牌。"""
    profile = Profile.load(PROFILE_PATH)
    for key in ("145", "149"):
        result = recognize_cards_of_file(_unique_files()[key], profile)
        check(result.board_codes == [],
              f"{key} 翻前不应识别出公共牌，得到 {result.board_codes}")


def test_shown_cards_from_show_card_sample() -> None:
    """摊牌亮牌样张：midL 的 A♣Q♥ 紧贴在一起，要切开并归到该座。"""
    profile = Profile.load(SHOW_PROFILE_PATH)
    img = cv2.imread(str(CARD_DIR / "show_card.jpg"))
    check(img is not None, "读不了 show_card.jpg")
    if img is None:
        return
    result = recognize_cards(img, profile)
    check(result.show_codes == {"midL": ["Ac", "Qh"]},
          f"亮牌识别：得到 {result.show_codes}，期望 midL Ac Qh")
    check(result.hero_codes == ["Js", "Qs"],
          f"同一张图的 hero 手牌应为 JsQs，实际 {result.hero_codes}")


def test_three_eight_use_hole_topology() -> None:
    """3/8 的 NCC 分数可能只差 0.01；用闭合孔数量兜底（3=0，8=2）。"""
    profile = Profile.load(PROFILE_PATH)
    three = recognize_cards_of_file(_unique_files()["redA"], profile)
    card3 = next((c for c in three.board.cards if c.code == "3s"), None)
    check(card3 is not None, "redA 样本里应有 3s")
    if card3 is not None:
        check(card3.evidence.get("glyph_holes") == 0,
              f"3 应有 0 个闭合孔：{card3.evidence}")
        check(card3.evidence.get("topology_resolved") is True,
              f"3 vs 8 应由拓扑判出：{card3.evidence}")

    eight = recognize_cards_of_file(_unique_files()["140"], profile)
    card8 = next((c for c in eight.hero_cards.cards if c.code == "8s"), None)
    check(card8 is not None, "140 样本的 hero 应有 8s")
    if card8 is not None:
        check(card8.evidence.get("glyph_holes") == 2,
              f"8 应有 2 个闭合孔：{card8.evidence}")


def test_show_one_card_sample() -> None:
    """河牌主动只秀一张：lowR K♦、upR 6♦ 都只记一张；另一张牌背不能凑数。"""
    profile = Profile.load(SHOW_PROFILE_PATH)
    img = cv2.imread(str(CARD_DIR / "showOne"))
    check(img is not None, "读不了 showOne")
    if img is None:
        return
    result = recognize_cards(img, profile)
    check(result.show_codes.get("midL") == ["Ac", "Qh"],
          f"midL 应记录两张：{result.show_codes}")
    check(result.show_codes.get("lowR") == ["Kd"],
          f"lowR 只秀一张 K♦：{result.show_codes}")
    check(result.show_codes.get("upR") == ["6d"],
          f"upR 只秀一张 6♦：{result.show_codes}")


def test_card_back_is_classified_explicitly() -> None:
    """POKER 牌背要被显式分类成 card_back，不能只靠低置信度过滤。

    showOne 的 lowR/upR 都是「一张真牌 + 一张牌背」；牌背有白边，会被
    检成牌面矩形。这里直接看原始读数：牌背必须带 card_back=True。
    """
    profile = Profile.load(SHOW_PROFILE_PATH)
    img = cv2.imread(str(CARD_DIR / "showOne"))
    check(img is not None, "读不了 showOne")
    if img is None:
        return
    h, w = img.shape[:2]
    from poker_ocr.cards import recognize_region
    for seat_id in ("lowR", "upR"):
        seat = next(s for s in profile.seats if s.seat_id == seat_id)
        px = profile.slot_to_px(seat.action_slot, (w, h))
        raw = recognize_region(img, px, name=seat_id, slot=seat.action_slot,
                               max_cards=2, split_pairs=True)
        check(len(raw.cards) == 2, f"{seat_id} 原始读数应有 2 块牌：{raw.codes}")
        check(any(c.evidence.get("card_back") for c in raw.cards),
              f"{seat_id} 应显式识别出牌背："
              f"{[(c.code, c.evidence.get('card_back')) for c in raw.cards]}")
        check(all(not c.evidence.get("card_back") for c in
                  recognize_cards(img, profile).shows[seat_id].cards),
              f"{seat_id} 输出不能包含牌背")


def test_all_ranks_have_templates() -> None:
    from poker_ocr.cards import _load_templates
    templates = _load_templates()
    check(set(templates) == set(RANKS),
          f"模板缺项：{set(RANKS) - set(templates)}")


def main() -> int:
    tests = [
        test_all_ranks_have_templates,
        test_shown_cards_from_show_card_sample,
        test_show_one_card_sample,
        test_card_back_is_classified_explicitly,
        test_three_eight_use_hole_topology,
        test_all_labeled_samples,
        test_color_and_shape_agree,
        test_suit_shape_thresholds,
        test_shape_disagreement_does_not_reject_color,
        test_default_profile_falls_back_to_wpk_slots,
        test_preflop_has_no_false_positive,
    ]
    for t in tests:
        before = len(FAILS)
        t()
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 公共牌 / hero 手牌识别全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
