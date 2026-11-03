"""一次决策的事实模型：把 `HandSnapshot` 整理成 `Situation`。

`Situation` 是「模型看到什么」的唯一事实源：几何、牌力、范围、画像、
桌况、数据缺口都在这里算好，提示词层（`poker_ocr.prompt`）只负责渲染。
计算顺序是有依赖的：牌面 ground truth → 对手范围 → 牌力解析 → 条件弃牌率，
每一步缺数据都 fail open，绝不阻塞主链路。

这一层不调模型、不渲染提示词；选项空间在 `poker_ocr.options`。
从 advice.py 拆出（2026-11-03 第二次拆分），`advice` 仍 re-export。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Iterable

from .coach import (
    BOARD_PLAY, MID_BOTTOM, OVERPAIR, STRONG_DRAW, STRONG_MADE, TPTK,
    TP_WEAK, WEAK_DRAW, board_texture, opponent_strength, parse_cards,
    read_hand,
)
from .ground_truth import GroundTruth, compute_ground_truth
from .hand_track import PREFLOP, HandSnapshot
from .options import (
    LOOSE_OPEN_BONUS, _abnormal_options, build_options, options_mode,
)
from .pot_math import (
    _no_opponent_anomaly, _playable_depth, _unknown_stack_opponents,
    count_limpers, hero_in_position, is_explosive_pot, players_left_to_act,
    pot_per_player, pot_type, spr,
)
from .preflop_ranges import range_verdict
from .table_dynamics import (
    is_aggressive_table, is_loose_table, is_passive_table,
)

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from .advice import Advice
    from .options import Option
    from .opponent_profile import OpponentRead, PooledStats


# ─────────────────────────── 牌况 ───────────────────────────

# 行动线里 hero 的标识。与 coach.advise 的 hero_position 口径一致
HERO_ACTOR = "hero"

# 牌面质地：coach 的中文标签 → 英文（旧英文事实块的 texture_en 字段用）。
_TEXTURE_EN = {
    "干燥": "Dry", "花面": "Flush-y", "顺面": "Straight-y",
    "花顺面": "Flush + straight", "公对面": "Paired",
}

# 牌型规则集：提示词按 hand_class 四选一注入（听牌 / 成牌+听 / 成牌 / 空气）。
# combo 按真实补牌听牌归类，不设强度门槛；听牌质量由牌力事实描述。
_DRAW_TIERS = (STRONG_DRAW, WEAK_DRAW)
_MADE_TIERS = (STRONG_MADE, OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM, BOARD_PLAY)


@dataclass(frozen=True)
class Situation:
    """一次决策的结构化事实。提示词由它渲染，规则兜底也用它。"""

    snap: HandSnapshot
    options: list[Option]
    hand_label: str | None
    hand_tier: str | None
    category: int | None
    texture: str | None
    has_draw: bool
    plays_board: bool
    opponent_strength: dict | None
    warnings: list[str] = field(default_factory=list)
    # 当前帧硬字段与行动线/形态的冲突；渲染在「数据质量」，按当前帧执行。
    conflicts: tuple[str, ...] = ()
    # 英文渲染：coach 的中文标签是 /coach/advice 的口径，给模型的提示词用英文
    hand_label_en: str | None = None
    texture_en: str | None = None
    opponent_strength_en: dict | None = None
    # 四二法则估的成牌/反超率（0-100）：花顺听 + 纯两高张的 6 张反超 outs。
    # 给提示词做「赔率 vs 胜率」比较用，但它是**成牌率**，不是对范围的河牌胜率。
    equity_pct: float | None = None
    # 人工维护的**现场观察**（根目录 .table_dy）。只写统计算不出来的东西：
    # 「3 号位上头了」「这桌有人合伙」。桌子的松紧/3-bet 频率由 table_stats 给数字。
    table_dynamics: str | None = None
    # 当前桌/全场先验统计。`scope` 决定提示词标题和口径。
    table_stats: "PooledStats | None" = None
    # 本手第几个 hero 决策点（0 起）。写进 advice 后可与 replay 的
    # DecisionPoint.action_index 对齐，解决「同一街多个决策点」无法配对。
    action_index: int = 0
    # 爆炸底池：上一街底池 ÷ 实际参与人数（BB/人）及其是否刚好 5BB。
    # 见 previous_street_pot() / is_explosive_pot()。
    pot_per_player: float | None = None
    explosive_pot: bool = False
    # 成手牌的**实际排名**：枚举对手两手牌，算多大概率比我这手大（0-100）。
    # beat_pct 是单个对手，beat_at_least_pct 是当前还在手的人数下至少一家领先。
    # 见 coach.beat_rate ——「两对」是个名字，这两个数才是强度。
    beat_pct: float | None = None
    beat_at_least_pct: float | None = None
    opponents_in_hand: int | None = None
    # 翻前：这手牌在该位置的开池范围归属（见 preflop_ranges.range_verdict）。
    # 由程序算、作为事实注入，模型只负责在事实之上做选择。
    range_verdict: str | None = None
    # 牌力事实块：最好的五张、精确坚果校验、听牌、牌面结构。
    # 由 ground_truth.compute_ground_truth 生成；None = 翻前/牌不全。
    ground_truth: GroundTruth | None = None
    # 桌风是否明确含「松浪/松凶」。混合桌也可以为 True：只要桌上有一个
    # LAG，中等成牌就不能默认做薄价值。见 table_dynamics.is_aggressive_table。
    aggressive_table: bool = False
    # 当前改良 outs（coach.Read.outs），含无花顺听时的两高张反超 outs。
    # 牌况分类用 ground_truth.draws.total_outs；策略评估再用此处 outs/牌力判断质量。
    # 两高张反超 outs 不是听牌，不能单独把空气归为 draw/combo。
    outs: int | None = None
    # 本手在局对手的画像（按 seat_id 对回具体的人）。空 = 没有画像，
    # 行为退回 `.table_dy` / aggressive_table 的旧口径。
    opponent_reads: tuple[OpponentRead, ...] = ()
    # 翻后决策的几何事实。由程序算、直接渲染进事实块：SPR 决定「顶对能不能
    # 打光」，位置/身后人数决定「能不能诈唬」，让模型自己推会推出两套口径。
    pot_type: str | None = None
    spr: float | None = None
    in_position: bool | None = None
    players_left_to_act: int | None = None
    # 上一街自己的决定（同手）。空 = 本手第一个决策点，或跨手重放。
    previous: "Advice | None" = None
    # GTO 近似表查询结果（单挑/单加注池/翻牌或转牌才有）。None = 不覆盖或查不到。
    gto: "object | None" = None
    # 牌面范围权益（Monte Carlo，多人池也能用）。None = 没表/翻前/位置不明。
    board_eq: "object | None" = None
    # 手牌 vs 对手范围（实时牌面范围 + 精确 behind + MC equity）。
    # None = 翻前/牌面不足/没有可确认的对手范围。
    hand_ranges: "object | None" = None
    # 牌力解析结论：把 hand_ranges 的 behind_any / equity 翻译成
    # 「大/中/小 + 程度 + 价值/听牌」。**只服务离线审计**：live 的
    # build_situation(include_strength=False) 不计算它，prompt 也不再渲染。
    # None = 未计算或没有 hand_ranges。
    hand_strength: "object | None" = None
    # 条件弃牌率：用本手收窄后的范围按权重算「他会不会弃」，并给出各尺寸的
    # 盈亏平衡弃牌率与 EV。见 fold_equity 模块。None = 翻前/无范围/已关闭。
    fold_eq: "object | None" = None
    # 翻后状态帧显示在局对手为 0、底池却有钱：事实自相矛盾。
    # 这种帧必须硬闸：不渲染筹码几何/牌力落后，只给过牌/弃牌。
    abnormal_no_opponents: bool = False
    # 「可选动作」是否被降级（`abnormal` / `unsized` / None=完整动作空间）。
    # 提示词的硬缺口/保守线由它派生，保证「说硬缺口」与「选项真的不全」同源；
    # 见 `options.options_mode()`。
    options_degraded: str | None = None

    @property
    def street(self) -> str:
        return self.snap.street

    @property
    def hand_class(self) -> str:
        """牌型规则集：`draw` / `combo` / `made` / `air`，互斥四类。

        · draw = 当前未成牌，但有真实可兑现听牌；
        · combo = 当前已成牌，且有真实听花或听顺（只认这两类）；
        · made = 当前已成牌，或只有其他改善/反超 outs；
        · air = 当前未成牌且没有有效听牌（河牌未成牌听牌也归空气）。

        combo 只由 `ground_truth.draws.flush_draw` 或 `straight_draw` 决定；
        不因两高张、其他反超 outs 或泛称的「可改善」归入 combo。
        分类描述牌况，不代表听牌质量或行动许可；低质量/被统治的 outs
        仍须在事实和决策中折价，不能因归 combo 自动下注。
        """
        tier = self.hand_tier
        if tier in _DRAW_TIERS:
            return "draw"
        if tier in _MADE_TIERS:
            gt = self.ground_truth
            has_combo_draw = bool(
                gt is not None and (gt.draws.flush_draw or gt.draws.straight_draw))
            return "combo" if has_combo_draw else "made"
        return "air"


def _conflict_warnings(snap: HandSnapshot) -> tuple[str, ...]:
    """当前帧硬字段与行动线/形态的冲突，供数据质量段展示。

    只检测能由快照直接对账的几类；无法确认的冲突不猜，避免把正常 OCR
    抖动写成「冲突」。所有冲突都以当前帧硬字段为准。
    """
    out: list[str] = []
    pre = snap.actions_of(PREFLOP)

    # 炸弹池横幅会串到下一手；翻前有真实动作时以动作为准。
    if snap.bomb_pot and pre:
        out.append("炸弹池标记与翻前行动冲突（按有行动处理）")

    # 本街行动线的最高下注 vs 当前帧的「本街总额」。
    street_acts = snap.actions_of(snap.street)
    bets = [float(a.amount) for a in street_acts
            if a.action in ("bet", "raise", "allin") and a.amount is not None]
    frame_max = float(snap.hero_bet or 0.0) + float(snap.to_call or 0.0)
    if bets and frame_max > 0:
        line_max = max(bets)
        if abs(line_max - frame_max) > max(1.0, frame_max * 0.25):
            out.append(
                f"行动线与当前帧下注额冲突（行动线 {line_max:g}BB / "
                f"当前帧 {frame_max:g}BB，按当前帧）")

    # 翻前形态 vs 实际加注次数。动作线为空时不判：形态可能是水位兜底。
    if pre:
        raises = sum(
            1 for a in pre
            if a.action in ("raise", "bet")
            or (a.action == "call" and a.all_in))
        shapes = set(snap.preflop_shapes or ())
        bad = False
        if "three-bet" in shapes:
            bad = raises < 2
        elif "single-raise" in shapes:
            bad = raises != 1
        elif shapes & {"unopened", "limped"}:
            bad = raises > 0
        if bad:
            shape_text = "/".join(sorted(shapes)) or "未知"
            out.append(
                f"翻前形态与行动线冲突（形态：{shape_text} / "
                f"行动线 {raises} 次加注，按当前帧）")

    # 行动线把动作记在 Hero 的位置上，却不是 Hero。
    for action in street_acts:
        if (not action.is_hero and action.position
                and action.position == snap.hero_position):
            out.append("行动线把动作记在 Hero 位置上但不是 Hero（按当前帧）")
            break

    return tuple(dict.fromkeys(out))


def _read(snap: HandSnapshot):
    """翻后读牌：返回 coach.Read 或 None（翻前 / 牌不全 / 读不出）。"""
    if snap.street == PREFLOP:
        return None
    if not snap.hero_cards or len(snap.board) < 3:
        return None
    try:
        return read_hand(parse_cards("".join(snap.hero_cards)),
                         parse_cards("".join(snap.board[:5])))
    except ValueError:
        return None


def _precise_texture_en(gt: GroundTruth | None, texture: str | None) -> str | None:
    """给模型/复盘看的牌面标签。两张同花不能再叫 "Flush + straight"。

    规则引擎的 `board_texture` 仍按历史口径（2 张同花 = 花面）用于下注尺度；
    这里只改「事实怎么说」，避免 990010 那类「只有花听被读成已成花」。
    """
    if gt is None:
        return _TEXTURE_EN.get(texture, texture) if texture else None
    b = gt.board_info
    parts: list[str] = []
    if b.flush_made_possible:
        parts.append("Flush possible")
    elif b.flush_draw_possible:
        parts.append("Two-tone (flush draw only — no made flush)")
    else:
        parts.append("Rainbow")
    if b.paired:
        parts.append("paired")
    if b.straight_possible:
        parts.append("straight possible")
    return " + ".join(parts)


def _range_conclusion_zh(snap: HandSnapshot,
                         table_dynamics: str | None,
                         loose: bool | None = None,
                         passive: bool | None = None) -> str | None:
    """翻前开池归属的一句话中文结论（只在无人入池/溜入时使用）。

    `preflop_ranges.Verdict.render()` 是英文长句；模型只需要「在/不在 +
    有溜入者时收紧后的归属」，这里只取结构化字段重新拼。
    """
    verdict = range_verdict(snap.hero_position, snap.hero_cards,
                            table_dynamics=table_dynamics,
                            limpers=count_limpers(snap),
                            stack_bb=_playable_depth(snap),
                            loose=loose, passive=passive)
    if verdict is None:
        return None
    if verdict.in_range is None:
        return (f"{verdict.hand} 在 {verdict.position} 没有开池范围"
                "（盲注位按补盲/过牌/防守处理）")
    # 小对子抽 set 是「非盲位平跟」的一个明确例外；不要把所有平跟都写死。
    small_pair = (len(verdict.hand) == 2
                  and verdict.hand[0] == verdict.hand[1])
    if verdict.limpers > 0 and verdict.isolation_ok is not None:
        iso = f"≈{verdict.isolation_position}"
        if verdict.isolation_ok:
            text = (f"{verdict.hand}：{verdict.limpers} 个溜入者后"
                    f"收紧范围（{iso}）仍包括它 → 建议加注隔离，不要平跟")
            if small_pair:
                text += ("；若后手深/桌松，可按约 10:1 隐含赔率平跟抽 set"
                         "（非盲位平跟的例外）")
        else:
            text = (f"{verdict.hand}：{verdict.limpers} 个溜入者后"
                    f"收紧范围（{iso}）已不包括它 → "
                    "建议默认弃牌，不要平跟（开池范围不等于隔离许可）")
    elif verdict.in_range:
        text = (f"{verdict.hand} 在 {verdict.position} 的开池范围内 → "
                "建议可以开池加注，不要平跟（非盲位平跟是例外）")
    else:
        text = (f"{verdict.hand} 不在 {verdict.position} 的开池范围内 → "
                "建议默认弃牌")
    if verdict.depth_note:
        text += f"（{verdict.depth_note}）"
    return text
def build_situation(snap: HandSnapshot,
                    table_dynamics: str | None = None,
                    opponent_reads: Iterable[OpponentRead] | None = None,
                    table_stats: "PooledStats | None" = None,
                    previous: "Advice | None" = None,
                    include_advanced: bool = False,
                    include_strength: bool = True
                    ) -> Situation:
    """把快照整理成一次决策的事实。

    `include_advanced=True` 才计算 GTO 近似表与 board equity（离线审计/实验
    用）。`include_strength=False` 时跳过 hand_strength：实时链路不再需要
    第 4 套牌力口径，离线审计在需要时自己懒计算。
    `table_dynamics` 是根目录 `.table_dy` 的解析文本（见 table_dynamics.py）。
    由调用方显式传入，而不是在这里读文件：单测和离线重放不会因为仓库里恰好
    有一份动态文件而改变结果。

    `opponent_reads` 是 ProfileStore.read_for() 的结果（按本手 seat_id）。
    没有画像时 `aggressive_table` 仍是 fallback，行为不会退化。
    """
    reads = tuple(opponent_reads or ())
    read = _read(snap)
    warnings: list[str] = list(snap.warnings)
    if (snap.street == PREFLOP and not snap.actions_of(PREFLOP)
            and not snap.bomb_pot):
        # 「按未开池处理」只在没人下注时成立；to_call > 1.5BB 说明有人下过注，
        # 只是动作线没抓到 —— 这时按面对加注处理，选项也会是 raise-*。
        bb = snap.blinds.bb if snap.blinds else 1.0
        warnings.append(
            "翻前动作线缺失，翻前形态/PFR 未知（本轮已有人下注，按面对加注处理）"
            if (snap.to_call or 0.0) > bb * 1.5 else
            "翻前动作线缺失，翻前形态/PFR 未知（按未开池处理：open 或弃；BB 可过牌）")
    unknown_stacks = _unknown_stack_opponents(snap)
    if unknown_stacks:
        warnings.append(
            f"{len(unknown_stacks)} 名对手筹码未读出，有效筹码/SPR 未计算")
    if snap.pot is None or snap.pot <= 0 or snap.hero_stack is None:
        warnings.append("底池/后手未读全，已隐藏下注选项，只保留过牌/跟注/弃牌")
    n_opp = sum(1 for o in snap.opponents if not o.folded)
    anomaly = _no_opponent_anomaly(snap)
    # 翻后只剩 Hero、底池却很大：状态帧把对手全标成弃牌（0c2e 的
    # 22:07:59 帧）。这种事实会自相矛盾（「本手在局 1 人」却有人全下），
    # 必须让模型看到「翻后建议不可信」，而不是按单挑打。
    if anomaly:
        warnings.append("翻后状态帧显示在局对手为 0（数据异常，翻后建议不可信）")

    # 事实块只算一次：牌面结构、最好五张、精确排名、坚果校验全从这里出。
    # 实时建议和复盘共用同一个 GroundTruth，避免「一个说听牌没成、一个说成对」。
    gt = None
    if read is not None and not anomaly:
        try:
            gt = compute_ground_truth(list(snap.hero_cards),
                                      list(snap.board[:5]), n_opp)
        except (ValueError, TypeError):
            gt = None

    texture = None
    if gt is not None:
        texture = gt.board_info.texture
    elif read is not None:
        try:
            texture, _tags = board_texture(parse_cards("".join(snap.board[:5])))
        except ValueError:
            texture = None

    opp = None
    opp_en = None
    if snap.street != PREFLOP:
        opp_read = opponent_strength(_coach_actions(snap), snap.street,
                                     HERO_ACTOR, snap.pot or 0.0)
        opp = opp_read.to_dict()
        opp_en = {"value": opp_read.value, "label": opp_read.label_en,
                  "reason": opp_read.reason_en}

    per_player = pot_per_player(snap)
    beat_pct = beat_at_least = None
    if gt is not None:
        beat_pct = round(gt.rank.beat_pct * 100, 1)
        beat_at_least = round(gt.rank.beat_at_least_pct * 100, 1)
    elif read is not None and not anomaly:
        # 兜底：gt 计算失败时仍复用 read_hand 已经枚举过的单人比例
        p = read.beat_pct
        beat_pct = round(p * 100, 1)
        beat_at_least = round((1.0 - (1.0 - p) ** max(n_opp, 1)) * 100, 1)
    # 桌子的松紧优先用**算出来的**全场统计；没有统计（第一晚、新桌）才退回
    # `.table_dy` 的关键词。`.table_dy` 现在只写现场观察，关键词多数时候匹配
    # 不到，所以这里的先后顺序很关键。
    loose_flag = (table_stats.loose
                  if table_stats is not None and table_stats.loose is not None
                  else is_loose_table(table_dynamics))
    passive_flag = (table_stats.passive
                    if table_stats is not None
                    and table_stats.passive is not None
                    else is_passive_table(table_dynamics))
    degraded = options_mode(snap, anomaly=anomaly)
    sit = Situation(
        snap=snap,
        options=(_abnormal_options(snap) if degraded == "abnormal"
                 else build_options(snap,
                                    open_bonus=(LOOSE_OPEN_BONUS
                                                if loose_flag else 0.0))),
        options_degraded=degraded,
        hand_label=read.label if read else None,
        hand_tier=read.tier if read else None,
        category=read.category if read else None,
        texture=texture,
        # 两高张有反超 outs，但不是听牌：只有真正的花顺听才算 has_draw。
        has_draw=bool(read and read.tier in (STRONG_DRAW, WEAK_DRAW)
                      and read.outs >= 4),
        plays_board=bool(read and read.plays_board),
        opponent_strength=opp,
        warnings=warnings,
        conflicts=_conflict_warnings(snap),
        hand_label_en=read.label_en if read else None,
        texture_en=_precise_texture_en(gt, texture),
        opponent_strength_en=opp_en,
        equity_pct=read.equity_pct if read else None,
        table_dynamics=(table_dynamics or "").strip() or None,
        table_stats=table_stats,
        action_index=(int(snap.decision_key[2])
                      if len(snap.decision_key) > 2 else 0),
        pot_per_player=per_player,
        explosive_pot=is_explosive_pot(snap),
        beat_pct=beat_pct,
        beat_at_least_pct=beat_at_least,
        opponents_in_hand=n_opp,
        # 翻前范围结论只在无人入池/溜入时渲染；面对加注不给 RFI 结论，
        # 否则模型会把「开池范围」当成「跟注/3-bet 范围」。
        range_verdict=(
            _range_conclusion_zh(snap, table_dynamics, loose_flag,
                                 passive_flag)
            if snap.street == PREFLOP and snap.preflop_shapes
            and set(snap.preflop_shapes) <= {"unopened", "limped"}
            else None),
        ground_truth=gt,
        aggressive_table=is_aggressive_table(table_dynamics),
        outs=read.outs if read else None,
        opponent_reads=reads,
        pot_type=pot_type(snap),
        spr=spr(snap),
        in_position=hero_in_position(snap),
        players_left_to_act=players_left_to_act(snap),
        previous=previous,
        abnormal_no_opponents=anomaly,
    )
    # GTO 表/board equity 是旧实验事实层，不再进入中文 lean 提示词。
    # 只有离线工具显式要 include_advanced=True 时才计算，且失败静默跳过。
    if include_advanced:
        try:
            from . import gto_lookup
            sit = replace(sit, gto=gto_lookup.lookup(sit))
        except Exception:  # noqa: BLE001
            pass
    # 手牌 vs 对手范围：答案在 ground truth 之后、board structure 之前。
    # 任何一步缺数据都 fail open，绝不影响主链路。
    try:
        from . import hand_vs_ranges
        hand_ranges = hand_vs_ranges.lookup(sit)
        if hand_ranges is not None:
            # 对手范围缺失/短码这类缺口要进 Data gaps，模型才知道哪些座位
            # 是 range unknown，而不是把随机牌排名当成范围。
            merged = list(dict.fromkeys([*sit.warnings,
                                         *hand_ranges.warnings]))
            sit = replace(sit, hand_ranges=hand_ranges, warnings=merged)
        elif hand_vs_ranges.enabled() and snap.street != PREFLOP \
                and len(snap.board) >= 3:
            # 全部座位都算不出范围时，也要把“range unknown”写进 Data gaps。
            from . import field_ranges
            gaps = field_ranges.resolve(snap).warnings
            if gaps:
                sit = replace(sit, warnings=list(dict.fromkeys(
                    [*sit.warnings, *gaps])))
    except Exception:  # noqa: BLE001
        pass
    if include_advanced:
        try:
            from . import board_equity
            sit = replace(sit, board_eq=board_equity.lookup(sit))
        except Exception:  # noqa: BLE001
            pass
    # 牌力解析：在 hand_ranges 之上再加一层纯判定，输出一行 Hand-strength read。
    # 依赖 hand_ranges；缺失时静默跳过，绝不猜。任何异常 fail open。
    # live 传 include_strength=False：这个结论只服务离线审计，不进 prompt。
    if include_strength:
        try:
            from . import hand_strength
            if hand_strength.enabled():
                sit = replace(sit,
                              hand_strength=hand_strength.compute_hand_strength(sit))
        except Exception:  # noqa: BLE001
            pass
    # 条件弃牌率：历史弃牌率是「全范围平均」，这手牌里对手已经用动作收窄了
    # 范围。这里用同一个收窄范围算加权弃牌率 + 各尺寸的盈亏平衡点。
    # 依赖 hand_ranges；缺失或出错时静默跳过（fail open）。
    try:
        from . import fold_equity
        report = fold_equity.lookup(sit)
        if report is not None:
            sit = replace(sit, fold_eq=report)
    except Exception:  # noqa: BLE001
        pass
    return sit


def _coach_actions(snap: HandSnapshot) -> list[dict]:
    """把快照的行动线转成 coach / call-back 的 action 字典。

    amount 口径与 coach.advise 一致：bet/raise 是「加到总额」，跟注不带金额。
    """
    out: list[dict] = []
    for rec in snap.all_actions():
        out.append({
            "actor": HERO_ACTOR if rec.is_hero else rec.seat_id,
            "action": rec.action,
            "amount": rec.amount,
            "street": rec.street,
        })
    return out

