#!/usr/bin/env python3
"""StableCards 跨帧牌面状态机的单测。

不读图片、不跑 OCR：手工拼 CardsResult。重点覆盖实测异常案例：
  · 翻牌动画把 4 张读成 3 张，不能把一手牌劈成两半；
  · hero 只读到 1 张（另一张是牌背）不能写进 hero_cards；
  · 新读数要连续 2 帧一致才替换 last-good；
  · 牌背/遮挡/低置信度即使连续两帧一致也不能采用。

    .venv/bin/python tests/test_stable_cards.py
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
         back: bool = False, occluded: bool = False,
         rank_scores: dict | None = None) -> Card:
    rank, suit = code[:-1], code[-1]
    evidence = {
        "card_back": back,
        "occluded": occluded,
        "rank_scores": rank_scores or {},
        "ink_pixels": 0 if occluded else 900,
    }
    return Card(rank=rank, suit=suit, confidence=confidence,
                rank_score=rank_score, rect=(0, 0, 10, 10),
                evidence=evidence)


def slot_read(name: str, cards: list[Card]) -> CardSlotRead:
    return CardSlotRead(name=name, slot=(0.0, 0.0, 1.0, 1.0),
                        rect=(0, 0, 10, 10), cards=cards)


def frame(board: list[Card], hero: list[Card], *,
          warnings: list[str] | None = None) -> CardsResult:
    return CardsResult(size=(1080, 2400), board=slot_read("board", board),
                       hero_cards=slot_read("hero_cards", hero),
                       warnings=list(warnings or []))


def codes(cards: list[Card]) -> list[str]:
    return [c.code for c in cards]


# ─────────────────────────── 用例 ───────────────────────────

def test_board_needs_two_equal_frames() -> None:
    s = StableCards(confirm_frames=2)
    out = s.update(frame([card("Ad")], [card("Kh"), card("Qs")]))
    check(out.board_codes == [], f"第一帧还不该采用 1 张公共牌：{out.board_codes}")

    flop = [card("Ad"), card("7c"), card("2s")]
    out = s.update(frame(flop, [card("Kh"), card("Qs")]))
    check(out.board_codes == [], f"翻牌第一帧应先等确认：{out.board_codes}")
    out = s.update(frame(flop, [card("Kh"), card("Qs")]))
    check(out.board_codes == ["Ad", "7c", "2s"],
          f"连续两帧一致应采用：{out.board_codes}")
    check(out.reliable, f"稳定后的牌面应可靠：{out.warnings}")


def test_board_regression_is_rejected() -> None:
    """4→3 / 5→4 一律不采用，哪怕连续多帧；只记异常。"""
    s = StableCards()
    flop = [card("Ad"), card("7c"), card("2s")]
    turn = flop + [card("9h")]
    s.update(frame(flop, []))
    s.update(frame(flop, []))
    s.update(frame(turn, []))
    s.update(frame(turn, []))
    s.update(frame(turn, []))          # 确认 hero 基线（空）
    check(s._board.read is not None and len(s._board.read.cards) == 4,
          "前提不成立：turn 应有 4 张")

    for i in range(5):
        out = s.update(frame(flop, []))
        check(out.board_codes == codes(turn),
              f"第 {i + 1} 帧 4→3 不该回退：{out.board_codes}")
        check(any("回退" in w for w in out.warnings),
              f"回退应写 warning：{out.warnings}")
        check(any("回退" in a for a in s.last_anomalies),
              f"回退应进 last_anomalies：{s.last_anomalies}")

    # 5→4 同理
    river = turn + [card("Qh")]
    s.update(frame(river, []))
    s.update(frame(river, []))
    for _ in range(3):
        out = s.update(frame(turn, []))
        check(out.board_codes == codes(river),
              f"5→4 不该回退：{out.board_codes}")


def test_board_zero_resets_after_confirmation() -> None:
    """0 是新一手信号，但也要连续 2 帧；确认后 hero last-good 清空。"""
    s = StableCards()
    flop = [card("Ad"), card("7c"), card("2s")]
    hero = [card("Kh"), card("Qs")]
    for _ in range(2):
        s.update(frame(flop, hero))
    out = s.update(frame(flop, hero))
    check(out.hero_codes == ["Kh", "Qs"], f"hero 应已稳定：{out.hero_codes}")

    first = s.update(frame([], hero))
    check(first.board_codes == codes(flop),
          f"公共牌归零第一帧先不采用：{first.board_codes}")
    second = s.update(frame([], hero))
    check(second.board_codes == [], f"连续两帧归零应清空：{second.board_codes}")
    check(second.hero_codes == [],
          f"新一手开始不能沿用上一手 hero 牌：{second.hero_codes}")


def test_hero_one_card_is_never_accepted() -> None:
    s = StableCards()
    two = [card("Kh"), card("Qs")]
    for _ in range(2):
        out = s.update(frame([], two))
    check(out.hero_codes == ["Kh", "Qs"], f"前提：hero 应稳定：{out.hero_codes}")

    # 只秀一张/牌背被过滤后只剩 1 张：即使是「稳定」的 1 张也不能写进 hero
    for _ in range(3):
        out = s.update(frame([], [card("Kh")]))
        check(out.hero_codes == ["Kh", "Qs"],
              f"hero 1 张不能替换 last-good：{out.hero_codes}")
        check(any("合法张数" in w for w in out.warnings),
              f"应说明张数不合法：{out.warnings}")


def test_single_frame_flip_does_not_replace_last_good() -> None:
    """翻牌 3 张稳定后，单帧读成另一组 3 张（动画）不替换。"""
    s = StableCards()
    flop = [card("Ad"), card("7c"), card("2s")]
    for _ in range(2):
        s.update(frame(flop, []))
    other = [card("Kd"), card("7c"), card("2s")]
    out = s.update(frame(other, []))
    check(out.board_codes == codes(flop),
          f"单帧变化不该替换：{out.board_codes}")
    out = s.update(frame(flop, []))
    check(out.board_codes == codes(flop), "回到原读数应继续沿用")


def test_card_back_is_never_accepted() -> None:
    """牌背读数即使连续两帧一致也不能替换 last-good。"""
    s = StableCards()
    flop = [card("Ad"), card("7c"), card("2s")]
    for _ in range(2):
        s.update(frame(flop, []))
    back = [card("8h", rank_score=0.64, confidence=0.50, back=True),
            card("7c"), card("2s")]
    for _ in range(4):
        out = s.update(frame(back, []))
        check(out.board_codes == codes(flop),
              f"牌背读数不能替换：{out.board_codes}")
        check(any("牌背" in a for a in s.last_anomalies),
              f"牌背应进 last_anomalies：{s.last_anomalies}")


def test_low_confidence_first_read_is_unreliable() -> None:
    """还没有 last-good 时读到低置信度牌：输出空、reliable=False。"""
    s = StableCards()
    weak = [card("Ad", rank_score=0.61, confidence=0.60),
            card("7c"), card("2s")]
    out = s.update(frame(weak, [card("Kh"), card("Qs")]))
    check(out.board_codes == [], f"低置信度不该被采用：{out.board_codes}")
    check(out.reliable is False,
          f"没有可靠 last-good 时应标 unreliable：{out.warnings}")


def test_hero_change_resets_board() -> None:
    """hero 从一组两张换成另一组 = 新一手，公共牌和 hero 都先清空再重读。"""
    s = StableCards()
    flop = [card("Ad"), card("7c"), card("2s")]
    first_hero = [card("Kh"), card("Qs")]
    for _ in range(2):
        s.update(frame(flop, first_hero))
    second_hero = [card("9d"), card("9c")]
    out = s.update(frame(flop, second_hero))
    check(out.hero_codes == codes(first_hero),
          f"新手牌第一帧先等待确认：{out.hero_codes}")
    out = s.update(frame(flop, second_hero))
    check(out.hero_codes == [] and out.board_codes == [],
          f"确认换手后应清空上一手牌面：hero={out.hero_codes} "
          f"board={out.board_codes}")
    # 换手确认后重新累计 hero / 公共牌确认
    out = s.update(frame(flop, second_hero))
    check(out.hero_codes == codes(second_hero),
          f"公共牌/hero 应重新确认：{out.hero_codes}")
    out = s.update(frame(flop, second_hero))
    check(out.board_codes == codes(flop),
          f"公共牌应重新确认：{out.board_codes}")


def test_unreliable_new_hero_still_breaks_deadlock() -> None:
    """新一手 hero 一直低置信度时也必须清掉上一手，不能把程序卡死。

    回归场景：旧版梅花置信度恒为 0.48，稳定闸永远不接受新牌；而换手判断
    又依赖稳定输出，于是 last-good 永远停在上一手，识别/建议/点击全部停摆。
    """
    s = StableCards(weak_accept_frames=0)
    flop = [card("Ad"), card("7c"), card("2s")]
    for _ in range(2):
        s.update(frame(flop, [card("Kh"), card("Qs")]))
    weak_hero = [card("8c", confidence=0.48), card("9c", confidence=0.48)]
    s.update(frame([], weak_hero))
    out = s.update(frame([], weak_hero))
    check(out.hero_codes == [],
          f"新 hero 不可靠时也不能沿用上一手：{out.hero_codes}")
    check(out.board_codes == [], f"上一手公共牌必须清空：{out.board_codes}")
    check(out.reliable is False,
          f"新牌未稳定时应标 unreliable：{out.warnings}")


def test_persistently_weak_reading_is_eventually_accepted() -> None:
    """读数连续多帧完全一致、只是置信度略低：兜底采用，避免永久卡住。"""
    s = StableCards(weak_accept_frames=3)
    weak = [card("Ad", confidence=0.60), card("7c")]
    for i in range(2):
        out = s.update(frame([], weak))
        check(out.hero_codes == [],
              f"前 {i + 1} 帧仍应等待：{out.hero_codes}")
    out = s.update(frame([], weak))
    check(out.hero_codes == ["Ad", "7c"],
          f"连续 3 帧一致后应兜底采用：{out.hero_codes}")
    check(any("置信度偏低" in w for w in out.warnings),
          f"兜底采用应写 warning：{out.warnings}")
    check(out.reliable, f"采用后应恢复可靠：{out.warnings}")


def test_card_back_is_not_rescued_by_weak_accept() -> None:
    """牌背属于硬问题，不能靠「连续多帧」兜底采用。"""
    s = StableCards(weak_accept_frames=2)
    back = [card("8h", confidence=0.5, back=True), card("7c")]
    for _ in range(4):
        out = s.update(frame([], back))
        check(out.hero_codes == [],
              f"牌背不能被弱读数兜底采用：{out.hero_codes}")


def test_three_frame_vote_resolves_rank_flicker() -> None:
    """连续两帧不一致时，最近 3 帧里 2 帧读到同一张牌就采用。"""
    s = StableCards(confirm_frames=2, vote_frames=3)
    s.update(frame([], []))                       # 翻前基线
    a = [card("Ad"), card("7c"), card("2s")]
    k = [card("Kd"), card("7c"), card("2s")]
    s.update(frame(a, []))                        # 第 1 帧 A
    out = s.update(frame(k, []))                  # 第 2 帧 K（与 A 不一致）
    check(out.board_codes == [], f"两帧不一致时先不采用：{out.board_codes}")
    out = s.update(frame(a, []))                  # 第 3 帧又回到 A：2/3 投票
    check(out.board_codes == ["Ad", "7c", "2s"],
          f"3 帧投票应采用多数读数：{out.board_codes}")
    check(any("投票" in w for w in out.warnings),
          f"投票应采用应写 warning：{out.warnings}")


def test_vote_requires_majority() -> None:
    """3 帧各读一个不同点数：没有多数，继续用 last-good，不硬猜。"""
    s = StableCards(confirm_frames=2, vote_frames=3)
    s.update(frame([], []))
    for rank in ("A", "K", "Q"):
        out = s.update(frame(
            [card(f"{rank}d"), card("7c"), card("2s")], []))
    check(out.board_codes == [],
          f"没有多数时不能采用任何一帧：{out.board_codes}")


def test_shows_pass_through() -> None:
    s = StableCards()
    result = frame([], [card("Kh"), card("Qs")])
    show = CardSlotRead(name="show:midL", slot=(0, 0, 1, 1),
                        rect=(0, 0, 1, 1), cards=[card("Ac"), card("Qh")])
    result.shows = {"midL": show}
    out = s.update(result)
    check(out.shows.keys() == {"midL"}, f"亮牌应原样透传：{out.shows}")


def main() -> int:
    tests = [
        test_board_needs_two_equal_frames,
        test_board_regression_is_rejected,
        test_board_zero_resets_after_confirmation,
        test_hero_one_card_is_never_accepted,
        test_single_frame_flip_does_not_replace_last_good,
        test_card_back_is_never_accepted,
        test_low_confidence_first_read_is_unreliable,
        test_hero_change_resets_board,
        test_unreliable_new_hero_still_breaks_deadlock,
        test_persistently_weak_reading_is_eventually_accepted,
        test_card_back_is_not_rescued_by_weak_accept,
        test_three_frame_vote_resolves_rank_flicker,
        test_vote_requires_majority,
        test_shows_pass_through,
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
    print("\n✅ 跨帧牌面稳定闸全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
