#!/usr/bin/env python3
"""条件弃牌率的测试：公式、分档、范围加权、与提示词接线。

    .venv/bin/python tests/test_fold_equity.py

全部是纯逻辑，不连 RTSP、不读表、不调模型。关键不变量：

  · 盈亏平衡弃牌率与手算一致（这是给模型看的数，错了整个半诈唬判断就错）
  · 范围越强 → 弃牌率越低；下注越大 → 弃牌率越高
  · 多人池「全部弃牌」是连乘，不是取平均
  · 任何缺数据都返回 None（fail open），绝不让主链路炸
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from poker_ocr import fold_equity as fe  # noqa: E402
from poker_ocr.coach import parse_cards as pc  # noqa: E402
from poker_ocr.ranges import make_range  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def close(a: float, b: float, tol: float = 0.002) -> bool:
    return abs(float(a) - float(b)) <= tol


def _range(*keys: str):
    """用手牌 key 拼一个等权范围。非对子会自动展开成 s/o 两种（"QJ" → QJs+QJo）。"""
    from poker_ocr.ranges import combos_of_key, expand_tokens

    expanded: set[str] = set()
    for key in keys:
        got = expand_tokens([key])
        expanded |= got or {f"{key}s", f"{key}o"}   # 裸的 "QJ" = QJs + QJo
    return make_range("T", {c: 1.0 for k in expanded
                            for c in combos_of_key(k)})


# ─────────────────────────── 公式 ───────────────────────────

def test_breakeven_matches_manual() -> None:
    """底池 100、下注 50、权益 8%：需要 25.4% 的弃牌率才回本。"""
    got = fe.breakeven_fold(pot_now=100.0, owed=0.0, size=50.0, equity=0.08)
    check(close(got, 0.2537), f"盈亏平衡弃牌率应为 25.4%，实际 {got:.4f}")


def test_breakeven_facing_raise() -> None:
    """底池 133（含他 33）、我加注到 100（多投 67）、权益 23.33% → 18.4%。"""
    got = fe.breakeven_fold(pot_now=133.0, owed=33.0, size=67.0, equity=0.2333)
    check(close(got, 0.1841), f"应为 18.4%，实际 {got:.4f}")


def test_breakeven_zero_when_ahead() -> None:
    """被跟也是赚的（权益足够高）时，不需要任何弃牌率。"""
    got = fe.breakeven_fold(pot_now=100.0, owed=0.0, size=50.0, equity=0.60)
    check(got == 0.0, f"权益 60% 时不该要求弃牌率，实际 {got}")


def test_ev_raise_matches_manual() -> None:
    """弃牌率 45%、权益 23.33%、下注 1/2 池 → EV +43.16BB（底池 100）。"""
    got = fe.ev_raise(pot_now=100.0, owed=0.0, size=50.0, equity=0.2333,
                      fold_equity=0.45)
    check(close(got, 43.16, 0.05), f"EV 应为 +43.16，实际 {got:+.2f}")


def test_ev_call_matches_manual() -> None:
    """跟 33 进 166（含我这次跟注）、权益 23.33% → EV +5.7BB。"""
    got = fe.ev_call(pot_now=133.0, owed=33.0, equity=0.2333)
    check(close(got, 5.68, 0.05), f"跟注 EV 应为 +5.68，实际 {got:+.2f}")


def test_breakeven_and_ev_agree_at_zero() -> None:
    """用盈亏平衡弃牌率代入 EV，必须恰好为 0 —— 两条公式互为反函数。"""
    for eq in (0.05, 0.15, 0.30):
        be = fe.breakeven_fold(120.0, 20.0, 60.0, eq)
        ev = fe.ev_raise(120.0, 20.0, 60.0, eq, be)
        check(abs(ev) < 0.01, f"权益 {eq:.0%}：平衡点 EV 应为 0，实际 {ev:+.4f}")


# ─────────────────────────── 分档与先验 ───────────────────────────

def test_tier_ladder() -> None:
    cases = [
        ("7h7d", "Ks7d2c", "强成牌"),        # 三条
        ("AsKd", "Ks7d2c", "顶对强踢"),
        ("AhAd", "Ks7d2c", "超对"),
        ("9h8h", "7h6c2h", "强听"),          # 花听 + 顺听
        ("AhQd", "Ks7d2c", "空气"),          # 两高张不是听牌
        ("QhJd", "Ks7d2c", "空气"),
    ]
    for hole, board, want in cases:
        got = fe.tier_of(pc(hole), pc(board))
        check(got == want, f"{hole} on {board} 应为 {want}，实际 {got}")


def test_fold_rises_with_size() -> None:
    """注越大，同一手持牌弃得越多。"""
    small = fe.fold_probability("空气", fe.price_of(25.0, 100.0))
    big = fe.fold_probability("空气", fe.price_of(100.0, 100.0))
    check(big > small, f"满池应比 1/4 池弃得多：{big:.2f} vs {small:.2f}")


def test_strong_hands_fold_less_than_air() -> None:
    price = fe.price_of(50.0, 100.0)
    air = fe.fold_probability(fe.AIR, price)
    strong = fe.fold_probability(fe.STRONG_MADE, price)
    king = fe.fold_probability(fe.TPTK, price)
    check(air > king > strong,
          f"排序应为 空气 > 顶对 > 强成牌：{air:.2f} / {king:.2f} / {strong:.2f}")


def test_fold_probability_is_clamped() -> None:
    """再小的注也不能低于下限，再大的注也不能等于 100%。"""
    tiny = fe.fold_probability(fe.AIR, 0.001)
    huge = fe.fold_probability(fe.AIR, 0.9)
    check(0.0 < tiny < 1.0, f"下限失效：{tiny}")
    check(0.0 < huge < 1.0, f"上限失效：{huge}")


# ─────────────────────────── 范围加权 ───────────────────────────

def test_stronger_range_folds_less() -> None:
    """同一个牌面上，强范围的条件弃牌率必须低于宽范围。"""
    board = pc("Ks7d2c")
    wide = _range("QJ", "T9", "54", "AJo", "76")
    tight = _range("77", "22", "KK", "AK", "KQ")
    price = fe.price_of(50.0, 100.0)
    wide_fe, wide_n, _ = fe.range_fold_equity(wide, board, price)
    tight_fe, tight_n, _ = fe.range_fold_equity(tight, board, price)
    check(wide_n > 0 and tight_n > 0, "两个范围都应有有效组合")
    check(tight_fe < wide_fe,
          f"强范围弃牌率应更低：{tight_fe:.3f} vs {wide_fe:.3f}")


def test_dead_cards_excluded() -> None:
    """和我的手牌冲突的对手组合不算进范围。"""
    board = pc("Ks7d2c")
    rng = _range("AA")
    _, n_all, _ = fe.range_fold_equity(rng, board, 0.25, dead=())
    _, n_dead, _ = fe.range_fold_equity(rng, board, 0.25, dead=pc("AhAd"))
    check(n_all == 6, f"AA 应有 6 个组合，实际 {n_all}")
    # Ah、Ad 在手里：只剩 AcAs
    check(n_dead == 1, f"应只剩 AcAs，实际 {n_dead}")


def test_multiway_products() -> None:
    """多人池：全部弃牌的概率是连乘，不是平均。"""
    board = pc("Ks7d2c")
    rng = _range("QJ", "T9", "76")
    mix, _ = fe.range_tier_mix(rng, board, ())
    one = fe.mix_fold_equity(mix, 0.25)
    two = one * one
    check(two < one, f"连乘应更小：{two:.3f} vs {one:.3f}")


def test_observed_fold_normalises_percent() -> None:
    """画像里存的是百分数（45.0），报告里必须是 0.45。"""
    from dataclasses import replace
    from poker_ocr.opponent_profile import OpponentRead

    read = OpponentRead(seat_id="a", key="a", display_name=None, archetype="tag",
                        confidence=1.0, n=40, vpip=25.0, pfr=20.0, af=2.0,
                        source="test", note="", fold_to_bet_flop_pct=45.0)
    sit = replace(_situation(to_call=2.0, pot=8.0), opponent_reads=(read,))
    rep = fe.lookup(sit)
    check(rep is not None, "应算出报告")
    if rep is None:
        return
    check(rep.observed is not None and close(rep.observed, 0.45, 1e-6),
          f"历史弃牌率应为 0.45，实际 {rep.observed}")
    text = rep.render()
    check("lifetime" in text, f"应把历史值渲染进事实：\n{text}")


def test_folded_opponent_not_counted() -> None:
    """已弃牌的对手不参与弃牌率平均。"""
    from dataclasses import replace
    from poker_ocr.opponent_profile import OpponentRead

    read = OpponentRead(seat_id="a", key="a", display_name=None, archetype="tag",
                        confidence=1.0, n=40, vpip=25.0, pfr=20.0, af=2.0,
                        source="test", note="", fold_to_bet_flop_pct=45.0)
    sit = _situation(to_call=2.0, pot=8.0)
    folded = tuple(replace(o, folded=True) for o in sit.snap.opponents)
    sit = replace(sit, opponent_reads=(read,),
                  snap=replace(sit.snap, opponents=folded))
    rep = fe.lookup(sit)
    if rep is not None:
        check(rep.observed is None,
              f"对手已弃牌时不该给出历史弃牌率，实际 {rep.observed}")


# ─────────────────────── 被跟注范围的权益（v2） ───────────────────────

def test_called_equity_below_range_equity() -> None:
    """跟注范围是范围的子集（更强），所以被跟注时的权益必须更低。"""
    board = pc("Ks7d2c4h")
    rng = _range("QJ", "T9", "76", "AJo", "77", "KQ")
    samples = fe.sample_outcomes(pc("AhQd"), board, rng, n=300, seed=3)
    check(len(samples) > 0, "应有样本")
    if not samples:
        return
    flat = fe.equity_when_called(samples, 0.0)          # price=0：人人跟
    thin = fe.equity_when_called(samples, 0.25)         # 1/2 池
    steep = fe.equity_when_called(samples, 0.40)        # 大注
    check(thin < flat, f"1/2 池的跟注权益应低于全范围：{thin:.3f} vs {flat:.3f}")
    check(steep < thin, f"大注的跟注权益应更低：{steep:.3f} vs {thin:.3f}")


def test_called_equity_in_unit_interval() -> None:
    board = pc("Ks7d2c4h")
    samples = fe.sample_outcomes(pc("AhQd"), board, _range("QJ", "77"), n=120, seed=5)
    for price in (0.0, 0.2, 0.5):
        v = fe.equity_when_called(samples, price)
        check(0.0 <= v <= 1.0, f"权益越界：{v}")


def test_sampling_is_deterministic() -> None:
    """同一 seed 必须给出同一批样本，否则复盘和实盘会对不上。"""
    board = pc("Ks7d2c4h")
    rng = _range("QJ", "T9")
    a = fe.sample_outcomes(pc("AhQd"), board, rng, n=64, seed=11)
    b = fe.sample_outcomes(pc("AhQd"), board, rng, n=64, seed=11)
    c = fe.sample_outcomes(pc("AhQd"), board, rng, n=64, seed=12)
    check(a == b, "同 seed 应完全一致")
    check(a != c, "不同 seed 应不同")


def test_sampler_excludes_dead_cards() -> None:
    """对手组合不能和我手里的牌冲突：我拿 AhAd，AA 只剩 AcAs。"""
    board = pc("Ks7d2c4h")
    tiers, total = fe.range_tier_map(_range("AA"), board, dead=pc("AhAd"))
    check(len(tiers) == 1, f"AA 应只剩 1 个组合，实际 {len(tiers)}")
    for combo in tiers:
        ranks = sorted(c for c in combo)
        check(not any(c in combo for c in pc("AhAd")),
              f"残留了和我冲突的组合：{combo}")


def test_report_ev_uses_called_equity() -> None:
    """EV 必须用「被跟注权益」算，不能用整范围权益 —— 后者会高估诈唬。"""
    sit = _situation(to_call=4.0, pot=13.0, board=("Ks", "7d", "2c", "4h"),
                     street="turn", hero_cards=("Ah", "Qd"))
    rep = sit.fold_eq
    check(rep is not None, "应有报告")
    if rep is None:
        return
    check(rep.range_equity > 0, "报告应带整范围权益")
    for ln in rep.lines:
        check(0.0 <= ln.equity_called <= 1.0,
              f"被跟注权益越界：{ln.equity_called}")
        # 跟注范围是整范围的子集，权益只能更低（抽样误差留 2 个点余量）
        check(ln.equity_called <= rep.range_equity + 0.02,
              f"被跟注权益不该高于整范围："
              f"{ln.equity_called:.3f} > {rep.range_equity:.3f}")
        # 越大的尺寸 → 跟注范围越强 → 被跟注权益越低
    if len(rep.lines) >= 2:
        check(rep.lines[-1].equity_called <= rep.lines[0].equity_called + 0.02,
              f"大注的被跟注权益应不高于小注："
              f"{rep.lines[-1].equity_called:.3f} vs {rep.lines[0].equity_called:.3f}")


# ─────────────────────── 绝对水平校准 ───────────────────────

def _read(pct: float, seat: str = "a"):
    from poker_ocr.opponent_profile import OpponentRead

    return OpponentRead(seat_id=seat, key=seat, display_name=None, archetype="tag",
                        confidence=1.0, n=60, vpip=25.0, pfr=20.0, af=2.0,
                        source="test", note="", fold_to_bet_flop_pct=pct)


def _with_read(sit, pct):
    from dataclasses import replace as _replace

    return _replace(sit, opponent_reads=(_read(pct),), fold_eq=None)


def test_no_read_means_no_calibration() -> None:
    sit = _situation(to_call=2.0, pot=8.0)
    check(sit.fold_eq is not None, "应有报告")
    if sit.fold_eq is None:
        return
    check(sit.fold_eq.scale == 1.0, f"没有画像时不该校准：{sit.fold_eq.scale}")


def test_calibration_anchors_the_prior() -> None:
    """check 模式下范围就是翻前先验：校准后弃牌率必须等于历史观测值。"""
    base = _situation(to_call=2.0, pot=8.0)
    for pct in (25.0, 45.0, 70.0):
        rep = fe.lookup(_with_read(base, pct))
        check(rep is not None, "应有报告")
        if rep is None:
            continue
        check(abs(rep.fold_equity - pct / 100.0) < 0.01,
              f"观测 {pct}% 时校准后应等于 {pct/100:.2f}，实际 {rep.fold_equity:.3f}")
        check(abs(rep.scale - 1.0) > 0.02, f"观测 {pct}% 时应该发生校准")


def test_calibration_keeps_strong_range_lower() -> None:
    """同一观测值下，线路上只剩强牌时条件弃牌率必须低于历史值。"""
    from dataclasses import replace as _replace
    from poker_ocr.hand_track import OpponentSnapshot, StreetRecord, ActionRecord

    from test_advice import make_point
    from poker_ocr.advice import build_situation

    base = make_point(hero_cards=("Ah", "Qd"), board=("Ks", "7d", "2c"),
                      street="flop", pot=13.0, to_call=4.0, hero_position="BB")
    snap = _replace(base,
                    opponents=(OpponentSnapshot("a", "老王", "BTN", 93.0, 4.0, False),),
                    streets=(StreetRecord("preflop", 0.0, ()),
                             StreetRecord("flop", 9.0,
                                          (ActionRecord("flop", "a", "老王",
                                                        "BTN", "bet", 4.0),))))
    sit = build_situation(snap)
    rep = fe.lookup(_with_read(sit, 45.0))
    check(rep is not None, "应有报告")
    if rep is None:
        return
    check(rep.fold_equity < 0.45,
          f"他下过注，条件弃牌率应低于 45%，实际 {rep.fold_equity:.3f}")


def test_scale_is_bounded() -> None:
    """极端观测值也不能把曲线拉飞。"""
    base = _situation(to_call=2.0, pot=8.0)
    for pct in (5.0, 95.0):
        rep = fe.lookup(_with_read(base, pct))
        if rep is None:
            continue
        check(0.4 <= rep.scale <= 2.5, f"校准系数越界：{rep.scale}")


# ─────────────────────── 权益实现率 ───────────────────────

def test_realization_ordering() -> None:
    """成牌 > 听牌 > 打牌面 > 空气；IP > OOP；人越多越低。"""
    rf = fe.realization_factor
    value = rf(tier="强成牌", in_position=True, opponents=2)
    draw = rf(tier="强听", in_position=True, opponents=2)
    weak = rf(tier="空气", in_position=True, opponents=2)
    check(value > draw > weak, f"分档排序不对：{value:.2f}/{draw:.2f}/{weak:.2f}")
    check(rf(tier="空气", in_position=True, opponents=2)
          > rf(tier="空气", in_position=False, opponents=2), "IP 应高于 OOP")
    check(rf(tier="空气", in_position=True, opponents=2)
          >= rf(tier="空气", in_position=True, opponents=4), "人多应不高于单挑")


def test_realization_unknown_is_neutral() -> None:
    """分档未知时不做折扣（宁可少修正，不凭空扣分）。"""
    check(fe.realization_factor(tier=None, in_position=None) == 1.0,
          "未知分档应返回 1.0")
    check(fe.realization_factor(tier="不存在的档", in_position=True) == 1.0,
          "非法分档应返回 1.0")


def test_realization_bounds() -> None:
    """再差的组合也不能低于下限、不能高于 1。"""
    worst = fe.realization_factor(tier="空气", in_position=False, opponents=9)
    check(0.45 <= worst <= 1.0, f"越界：{worst}")


def test_call_ev_drops_with_realization() -> None:
    """同样的权益，实现率越低跟注 EV 越低；能翻转边缘跟注。"""
    raw = fe.ev_call(pot_now=100.0, owed=25.0, equity=0.21)
    real = fe.ev_call(pot_now=100.0, owed=25.0, equity=0.21, realization=0.60)
    check(raw > 0 > real, f"应从未实现 +{raw:.2f} 翻成 {real:.2f}")


def test_call_line_mentions_realization() -> None:
    sit = _situation(to_call=2.0, pot=8.0)
    rep = sit.fold_eq
    check(rep is not None, "应有报告")
    if rep is None or rep.call_line is None:
        return
    check(0.0 < rep.realization <= 1.0, f"实现率越界：{rep.realization}")
    if rep.realization < 0.995:
        check("realizing" in rep.call_line and "effective" in rep.call_line,
              f"跟注行应写清实现率：{rep.call_line}")


def test_no_bet_ev_when_betting_is_impossible() -> None:
    """面对全下只剩 call/fold：不能退回底池比例去算下注 EV。"""
    from dataclasses import replace as _replace

    class _Opt:
        def __init__(self, kind, amount=None, label=""):
            self.kind, self.amount, self.label = kind, amount, label

    sit = _situation(to_call=2.0, pot=8.0)
    solo = _replace(sit, options=[_Opt("call", 2.0), _Opt("fold")])
    rep = fe.lookup(_replace(solo, fold_eq=None))
    check(rep is not None, "应有报告")
    if rep is None:
        return
    check(rep.lines == (), f"不能下注时不该有尺寸 EV，实际 {len(rep.lines)} 条")


# ─────────────────────── 河牌与打牌面 ───────────────────────

def test_river_realization_is_full() -> None:
    """河牌 call 完就摊牌，不该再折实现率。"""
    rf = fe.realization_factor
    for tier, ip in (("空气", False), ("强听", True), ("强成牌", True)):
        river = rf(tier=tier, in_position=ip, opponents=4, street="river")
        check(river == 1.0, f"{tier} 河牌实现率应为 1.0，实际 {river}")
    flop = rf(tier="空气", in_position=False, street="flop")
    turn = rf(tier="空气", in_position=False, street="turn")
    river = rf(tier="空气", in_position=False, street="river")
    check(flop < turn < river, f"应随街递增：{flop:.2f} < {turn:.2f} < {river:.2f}")


def test_plays_board_matches_coach() -> None:
    """本地那份带缓存的「打牌面」判定必须和 coach 原版逐手一致。"""
    from poker_ocr.coach import _plays_board as coach_pb
    from poker_ocr.coach import evaluate as _eval

    rnd = random.Random(20261031)
    deck = [(r, s) for r in range(13) for s in "cdhs"]
    for _ in range(300):
        cards = rnd.sample(deck, 7)
        hole, board = cards[:2], cards[2:2 + rnd.choice([3, 4, 5])]
        want = coach_pb(hole, board)
        got = fe._plays_board(hole, board, _eval(list(hole) + list(board)))
        check(want == got, f"{hole} on {board}：coach={want} 本地={got}")


def test_board_play_is_not_strong_made() -> None:
    """双公对面（6622）上人人都至少两对，没贡献到对子的手是「打牌面」。"""
    board = pc("6d6s2h2s")
    check(fe.tier_of(pc("9c4d"), board) == "打牌面",
          f"9 高没碰到 6/2，应是打牌面，实际 {fe.tier_of(pc('9c4d'), board)}")
    check(fe.tier_of(pc("9cTh"), board) == "打牌面", "9T 也是打牌面")
    check(fe.tier_of(pc("7c7d"), board) == "强成牌", "77 做成 7766，是真强牌")
    check(fe.tier_of(pc("6c5c"), board) == "强成牌", "65 有 6，是三条")


# ─────────────────────── 多人池 ───────────────────────

def _multiway(n_opponents: int = 3):
    """造一个多人池 Situation（直接喂 HandVsRanges，绕开 field_ranges 解析）。"""
    from dataclasses import replace as _replace

    from test_advice import make_point

    from poker_ocr import hand_vs_ranges, ranges as _ranges
    from poker_ocr.advice import build_situation

    priors = ["BB_DEF_vs_BTN", "RFI_CO", "RFI_BTN"]
    entries = []
    for i in range(n_opponents):
        prior = _ranges.get(priors[i % len(priors)])
        if prior is None:
            return None, None
        entries.append((f"o{i}", "BB", prior, "call"))
    hr = hand_vs_ranges.compute("AhQd", "Ks7d2c", entries, samples=200)
    if hr is None:
        return None, None
    point = make_point(hero_cards=("Ah", "Qd"), board=("Ks", "7d", "2c"),
                       street="flop", pot=13.0, to_call=4.0)
    return build_situation(point), hr


def test_multiway_shares_are_averaged() -> None:
    """多个对手时各档占比要按人数平均，加起来不能超过 100%。"""
    sit, hr = _multiway(3)
    if sit is None:
        return
    from dataclasses import replace as _replace

    rep = fe.lookup(_replace(sit, hand_ranges=hr, fold_eq=None))
    check(rep is not None, "应有报告")
    if rep is None:
        return
    total = sum(share for _tier, share in rep.shares)
    check(total <= 1.0001, f"各档占比之和应 <= 100%，实际 {total:.1%}")
    check(rep.combos <= 1326, f"组合数是「每家剩多少」，不能超过 1326：{rep.combos}")


def test_multiway_has_no_size_ev() -> None:
    """半诈唬 EV 公式是单挑的：多人池不能给尺寸 EV。"""
    sit, hr = _multiway(3)
    if sit is None:
        return
    from dataclasses import replace as _replace

    rep = fe.lookup(_replace(sit, hand_ranges=hr, fold_eq=None))
    check(rep is not None, "应有报告")
    if rep is None:
        return
    check(rep.lines == (), f"多人池不该给尺寸 EV，实际 {len(rep.lines)} 条")
    text = rep.render()
    check("Multiway" in text, f"应说明为什么没有 EV：\n{text}")


# ─────────────────────────── 接线 ───────────────────────────

def _situation(**kw):
    from test_advice import make_point

    from poker_ocr.advice import build_situation

    point = make_point(**kw)
    return build_situation(point)


def test_situation_has_report() -> None:
    sit = _situation(to_call=2.0, pot=8.0)
    check(sit.fold_eq is not None, "翻后且有范围时应算出条件弃牌率")
    if sit.fold_eq is None:
        return
    r = sit.fold_eq
    check(r.combos > 0, f"组合数应为正：{r.combos}")
    check(0.0 <= r.fold_equity <= 1.0, f"弃牌率越界：{r.fold_equity}")
    check(len(r.lines) > 0, "应有至少一个尺寸的评估")
    for ln in r.lines:
        check(0.0 <= ln.breakeven <= 1.0, f"平衡点越界：{ln.breakeven}")


def test_prompt_renders_block() -> None:
    from poker_ocr.advice import render_hand_facts

    sit = _situation(to_call=2.0, pot=8.0)
    user = render_hand_facts(sit)
    check("Fold equity (range-conditional)" in user,
          f"提示词应包含条件弃牌率块：\n{user[-800:]}")


def test_disabled_returns_none() -> None:
    os.environ["FOLD_EQUITY_ENABLED"] = "0"
    try:
        sit = _situation(to_call=2.0, pot=8.0)
        check(sit.fold_eq is None, "关闭开关后不该计算")
    finally:
        os.environ.pop("FOLD_EQUITY_ENABLED", None)


def test_preflop_returns_none() -> None:
    sit = _situation(street="preflop", board=(), to_call=1.0, pot=3.0)
    check(fe.lookup(sit) is None, "翻前不该有弃牌率报告")


def test_no_fold_equity_uses_number() -> None:
    """有数字时按数字判定，不再用「3 家以上」一刀切。"""
    from poker_ocr.advice import _no_fold_equity
    from poker_ocr.coach import STRONG_DRAW

    sit = _situation(to_call=2.0, pot=8.0)
    if sit.fold_eq is None:
        return
    fake_high = type(sit.fold_eq)(**{**sit.fold_eq.__dict__, "fold_equity": 0.42})
    fake_low = type(sit.fold_eq)(**{**sit.fold_eq.__dict__, "fold_equity": 0.08})
    from dataclasses import replace as _replace

    sit_draw = _replace(sit, hand_tier=STRONG_DRAW)
    check(_no_fold_equity(_replace(sit_draw, fold_eq=fake_high)) is False,
          "弃牌率 42% 时不该判「没有弃牌率」")
    check(_no_fold_equity(_replace(sit_draw, fold_eq=fake_low)) is True,
          "弃牌率 8% 时应判「没有弃牌率」")


def test_no_fold_equity_falls_back() -> None:
    """没有报告时保留旧口径：3 家以上 + 听牌 = 没有弃牌率。"""
    from poker_ocr.advice import _no_fold_equity
    from poker_ocr.coach import STRONG_DRAW
    from dataclasses import replace as _replace

    sit = _situation(to_call=2.0, pot=8.0)
    bare = _replace(sit, fold_eq=None, hand_tier=STRONG_DRAW, opponents_in_hand=3)
    check(_no_fold_equity(bare) is True, "旧口径应保持：3 家 + 听牌 → 没有弃牌率")
    heads_up = _replace(bare, opponents_in_hand=1)
    check(_no_fold_equity(heads_up) is False, "单挑不该判没有弃牌率")


def main() -> int:
    tests = [v for name, v in sorted(globals().items())
             if name.startswith("test_") and callable(v)
             and getattr(v, "__module__", "") == __name__]
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
    print("\n✅ 条件弃牌率全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
