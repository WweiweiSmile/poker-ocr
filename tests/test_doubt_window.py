#!/usr/bin/env python3
"""疑惑帧（原「事故帧」）时间窗仲裁的单测。

不读图片、不跑 OCR：手工拼 CardsResult，并显式传 `now` 控制时间轴。
重点覆盖：

  · 带疑点但持续一致的「正确读数」能在窗口结束后被采用；
  · 动画里的单帧/短时误读会被 last-good 的多数票压下去；
  · 牌背/遮挡这类结构性疑点永远不能靠票数转正；
  · 窗口内读数恢复干净时提前解除，不用等满整个窗口；
  · 当前帧有疑点时 `reliable=False`（不能拿上一帧的旧牌继续给建议）。

    .venv/bin/python tests/test_doubt_window.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.cards import (  # noqa: E402
    Card, CardsResult, CardSlotRead, StableCards,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def card(code: str, *, rank_score: float = 0.9, confidence: float = 0.9,
         back: bool = False, occluded: bool = False) -> Card:
    rank, suit = code[:-1], code[-1]
    return Card(rank=rank, suit=suit, confidence=confidence,
                rank_score=rank_score, rect=(0, 0, 10, 10),
                evidence={"card_back": back, "occluded": occluded,
                          "rank_scores": {}, "ink_pixels": 900})


def slot_read(name: str, cards: list[Card]) -> CardSlotRead:
    return CardSlotRead(name=name, slot=(0.0, 0.0, 1.0, 1.0),
                        rect=(0, 0, 10, 10), cards=cards)


def frame(board: list[Card], hero: list[Card] | None = None) -> CardsResult:
    return CardsResult(size=(1080, 2400),
                       board=slot_read("board", board),
                       hero_cards=slot_read("hero_cards", hero or []))


def codes(cards: list[Card]) -> list[str]:
    return [c.code for c in cards]


def established(now: float = 0.0) -> StableCards:
    """先把翻牌 3 张稳定下来（两帧干净一致）。"""
    s = StableCards(doubt_window=0.5)
    flop = [card("Ad"), card("7c"), card("2s")]
    s.update(frame(flop), now=now)
    s.update(frame(flop), now=now + 0.1)
    return s


FLOP = ["Ad", "7c", "2s"]
ALT = [card("Kd", confidence=0.60), card("7c"), card("2s")]


# ─────────────────────────── 用例 ───────────────────────────

def test_flagged_correct_reading_wins_after_window() -> None:
    """带疑点、但窗口内持续一致的读数：窗口结束后按多数采用。"""
    s = established()
    out = s.update(frame(ALT), now=0.2)
    check(out.board_codes == FLOP, f"窗口刚开还应沿用 last-good：{out.board_codes}")
    check(out.reliable is False, "仲裁期间不能标可靠")

    for t in (0.4, 0.6):
        out = s.update(frame(ALT), now=t)
        check(out.board_codes == FLOP, f"窗口未结束不该采用：{out.board_codes}")
        check(out.reliable is False, "仲裁期间不能标可靠")

    out = s.update(frame(ALT), now=0.8)      # 0.2 → 0.8 已超过 0.5s
    check(out.board_codes == codes(ALT),
          f"持续一致的挑战者应被采用：{out.board_codes}")
    check(out.reliable is True, f"采用后应恢复可靠：{out.warnings}")
    check(any("疑惑帧仲裁采用" in w for w in out.warnings),
          f"应采用并写 warning：{out.warnings}")


def test_last_good_wins_when_more_frames_match() -> None:
    """窗口内多数帧更像 last-good：保留上一帧，并恢复可靠。"""
    s = established()
    weak_flop = [card("Ad", confidence=0.60), card("7c"), card("2s")]
    s.update(frame(ALT), now=0.2)            # 开窗
    s.update(frame(weak_flop), now=0.3)
    s.update(frame(ALT), now=0.4)
    s.update(frame(weak_flop), now=0.5)
    out = s.update(frame(weak_flop), now=0.8)
    check(out.board_codes == FLOP,
          f"多数帧支持 last-good 时应保留：{out.board_codes}")
    check(out.reliable is True, f"保留后应恢复可靠：{out.warnings}")
    check(any("保留上一帧" in w for w in out.warnings),
          f"应写保留 warning：{out.warnings}")


def test_no_majority_keeps_last_good_but_unreliable() -> None:
    """2 票挑战者 / 2 票 last-good / 1 票第三种：没有多数，标不可靠。"""
    s = established()
    weak_flop = [card("Ad", confidence=0.60), card("7c"), card("2s")]
    other = [card("Qs", confidence=0.60), card("7c"), card("2s")]
    s.update(frame(ALT), now=0.2)            # 开窗
    s.update(frame(weak_flop), now=0.3)
    s.update(frame(ALT), now=0.4)
    s.update(frame(weak_flop), now=0.5)
    out = s.update(frame(other), now=0.8)
    check(out.board_codes == FLOP,
          f"没有多数时应继续显示 last-good：{out.board_codes}")
    check(out.reliable is False, "没有多数时必须标不可靠")
    check(any("无多数" in w for w in out.warnings),
          f"应写无多数 warning：{out.warnings}")

    # 下一帧恢复干净：解除悬案，恢复可靠
    out = s.update(frame([card("Ad"), card("7c"), card("2s")]), now=0.9)
    check(out.board_codes == FLOP, f"恢复后仍是 last-good：{out.board_codes}")
    check(out.reliable is True, f"恢复干净后应可靠：{out.warnings}")


def test_card_back_challenger_never_wins() -> None:
    """牌背/遮挡是结构性疑点：票数再多也不能转正。"""
    s = established()
    back = [card("8h", confidence=0.50, back=True), card("7c"), card("2s")]
    for i, t in enumerate((0.2, 0.4, 0.6, 0.8)):
        out = s.update(frame(back), now=t)
        check(out.board_codes == FLOP,
              f"第 {i + 1} 帧牌背不能替换：{out.board_codes}")
    check(out.reliable is False, "牌背悬案期间必须标不可靠")
    check(any("无多数" in w for w in out.warnings),
          f"牌背不应形成有效挑战者：{out.warnings}")
    check(any("牌背" in a for a in s.last_anomalies),
          f"牌背应继续进 last_anomalies：{s.last_anomalies}")


def test_clean_frames_end_the_window_early() -> None:
    """窗口内连续两帧完全干净且一致：提前采用，不等满窗口。"""
    s = established()
    clean_alt = [card("Kd"), card("7c"), card("2s")]
    s.update(frame(ALT), now=0.2)            # 带疑点开窗
    out = s.update(frame(clean_alt), now=0.3)
    check(out.board_codes == FLOP, f"一帧干净还不够：{out.board_codes}")
    out = s.update(frame(clean_alt), now=0.4)
    check(out.board_codes == codes(clean_alt),
          f"连续两帧干净应提前采用：{out.board_codes}")
    check(out.reliable is True, f"提前采用后应可靠：{out.warnings}")
    check(any("连续 2 帧干净" in w for w in out.warnings),
          f"应写提前采用 warning：{out.warnings}")


def test_current_flagged_frame_is_unreliable() -> None:
    """当前帧读数有疑点（即使码数没变）时，不能算可靠。"""
    s = established()
    weak_same = [card("Ad", confidence=0.60), card("7c"), card("2s")]
    out = s.update(frame(weak_same), now=0.2)
    check(out.board_codes == FLOP, f"输出仍是 last-good：{out.board_codes}")
    check(out.reliable is False, "当前帧有疑点时 reliable 必须为 False")
    out = s.update(frame([card("Ad"), card("7c"), card("2s")]), now=0.3)
    check(out.reliable is True, f"下一帧干净后应恢复可靠：{out.warnings}")


def test_invalid_count_frame_is_unreliable() -> None:
    """公共牌 1 张这类动画中间态：沿用 last-good，但不能标可靠。"""
    s = established()
    out = s.update(frame([card("Ad")]), now=0.2)
    check(out.board_codes == FLOP, f"非法张数应沿用 last-good：{out.board_codes}")
    check(out.reliable is False, "非法张数帧必须标不可靠")
    out = s.update(frame([card("Ad"), card("7c"), card("2s")]), now=0.3)
    check(out.reliable is True, f"下一帧干净后应恢复可靠：{out.warnings}")


def test_pending_clean_change_keeps_reliable() -> None:
    """干净的换街读数在 2 帧确认期间不标不可靠（避免污染 cards_unreliable）。"""
    s = established()
    turn = [card("Ad"), card("7c"), card("2s"), card("9h")]
    out = s.update(frame(turn), now=0.2)
    check(out.board_codes == FLOP, f"确认前先沿用 last-good：{out.board_codes}")
    check(out.reliable is True, "干净读数的确认窗口不算异常")
    out = s.update(frame(turn), now=0.3)
    check(out.board_codes == codes(turn), f"两帧一致应采用：{out.board_codes}")
    check(out.reliable is True, f"采用后仍应可靠：{out.warnings}")


def test_zero_window_disables_arbitration() -> None:
    """--card-doubt-window 0 时退回旧行为：带疑点读数不参与替换。"""
    s = StableCards(doubt_window=0.0)
    s.update(frame([card("Ad"), card("7c"), card("2s")]), now=0.0)
    s.update(frame([card("Ad"), card("7c"), card("2s")]), now=0.1)
    for i in range(5):
        out = s.update(frame(ALT), now=0.2 + i * 0.1)
        check(out.board_codes == FLOP,
              f"关闭仲裁后带疑点读数不该被采用：{out.board_codes}")
        check(out.reliable is False, "带疑点帧仍应标不可靠")


def main() -> int:
    tests = [
        test_flagged_correct_reading_wins_after_window,
        test_last_good_wins_when_more_frames_match,
        test_no_majority_keeps_last_good_but_unreliable,
        test_card_back_challenger_never_wins,
        test_clean_frames_end_the_window_early,
        test_current_flagged_frame_is_unreliable,
        test_invalid_count_frame_is_unreliable,
        test_pending_clean_change_keeps_reliable,
        test_zero_window_disables_arbitration,
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
    print("\n✅ 疑惑帧仲裁全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
