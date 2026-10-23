"""翻后建议引擎 —— 按《小绿皮书》方法论出「一句话建议」，不含任何模型推理。

全部是确定性的判定 + 查表：牌面质地、牌力分档、行动线、赔率。

规则来源（对应 call-back/services/skills/ 下的技能文件）：

    §3 成牌尺度     postflop-made-hand.md
    §4 一对类       ★本项目引申 —— postflop-made-hand.md **没有覆盖「一对」**
                       （顶对/中对/底对/超对都不在其中），这张表是归纳出来的，
                       规则表见对话记录，用户已逐格确认。
    §5 听牌         postflop-draw.md
    §6 cbet 频率    postflop-cbet.md
    §7 多人池       postflop-multiway.md
    §8 对手形象     core-stance.md / opponent-read.md
    §9 赔率         odds-table.md

与 allin.py 同一原则：**落在覆盖范围外就返回 ok=False，绝不猜** ——
宁可不给建议，也不给一个看着像样却是错的建议。

覆盖范围（M2）：翻牌 / 转牌 / 河牌三条街；单挑与多人池；翻前动作参与判断（是否 PFR）。
规则表用户已逐条过目确认。

已知边界（别当它已经覆盖）：
  - §8 只实现了四格：对松弱/紧凶不诈唬、对松凶强牌诱捕、对紧凶下注让路。
    「紧弱→宽范围偷」没做。
  - outs 只数「下一张能改良」的补牌（顺子点数 + 同花听 9 张），是下界，
    不含两张牌的组合听法与后门听牌。河牌没有下一张，不再数 outs。
  - 成牌率用四二法则：翻牌 outs×4、转牌 outs×2、河牌 0；15+ outs 会高估。
  - **转牌隐含赔率是保守近似**（只算潜在收益的一小部分），见 _implied_need_pct。
  - **河牌价值下注的「钟形曲线」是读人的东西，机械引擎做不了**：
    退化成固定尺度档（强牌 2/3、薄价值 1/2）。抓诈唬同理，只按牌面与下注大小判。
  - **相对牌力：结构 + 频率，两个都要看**。`board_ceiling` 判「牌面上**可能**构成
    什么牌」，`beat_rate` 枚举出「**多大概率**有人的牌比我大」。只用一个会出错：
      · 只看结构 → 「公对面的同花」被判成被压制（实际只有 1.8% 的随机牌赢它），
        规则引擎就会教人把同花扔给半池下注；
      · 只看名字 → 「8822」被当成「两对 = 强成牌」，而 7 家池里 87.7% 已经落后。
    所以 `capped_by_board` = 上限更高 **且** ≥ `CAPPED_MIN_BEAT_PCT` 的随机牌赢我。
    它**不建模对手的打法范围**——技能文档明确不引入范围理论（opponent-read.md）。
    随机两手牌是下界，真实范围只会更强。所以「对手紧凶」这类信息只能经由 §8 的粗档位体现。
  - **牌面已成花/成顺时，听牌先过「坚果关」再看赔率**（用户口径）：只在这种牌面
    （3 张同花、或 567 这种 3 连 → `board_made_threat`）上，非坚果听牌一律自动弃；
    达标（坚果）才按赔率决定。2 张同花之类的"只是可能"牌面不触发，仍按赔率。
    坚果 = 同花听握着该花色 A，或顺子听补成该牌面最大顺。只管**跟注**，半诈不受限。
  - **「有牌又有听」`combo`**：成牌（一对类）+ ≥8 outs 的强听，不再只按"一对"处理 ——
    面对下注跟、过牌到我时下注（两条赢路）。修的是"判定成一对后就把听牌丢了"。
  - **「打牌面」`BOARD_PLAY`**：最好的 5 张全靠公共牌（公共牌自己就是两对/三条…）→
    人人都有，没有摊牌价值，不再当强成牌。见 `_plays_board`。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field, replace
from functools import lru_cache
from itertools import combinations

# ─────────────────────────── 牌 ───────────────────────────

RANK_CHARS = "23456789TJQKA"                       # 下标 0..12，和 evaluator.ts 一致
VALID_SUITS = "shdc"

# 花色数达到几算「花面」
FLUSH_DRAW_SUIT_COUNT = 4                          # 手里 4 张同花 = 同花听


def _parse_card(text: str) -> tuple[int, str]:
    """'Kh' / '10s' -> (点数 12, 花色 'h')。点数用 RANK_CHARS 的下标：2=0 … A=12。

    按「点数+花色」逐张扫描而不是固定切两半：识别器会给出 `10s10h` 这种
    6 字符写法（T 被写成 10），固定长度切会切错 —— 与 allin.py 同样的处理。
    """
    c = text.strip()
    if c[:2] == "10":
        rank, suit = "T", c[2:]
    else:
        rank, suit = c[0].upper(), c[1:]
    if len(suit) != 1 or suit.lower() not in VALID_SUITS:
        raise ValueError(f"认不出花色：{text!r}")
    idx = RANK_CHARS.find(rank.upper())
    if idx < 0:
        raise ValueError(f"认不出点数：{text!r}")
    return idx, suit.lower()


def parse_cards(text: str) -> list[tuple[int, str]]:
    """'KhQh' / 'Ks7d2c' / '10s10h' -> [(点数, 花色), ...]。"""
    s = text.strip().replace(" ", "")
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(s):
        step = 3 if s[i:i + 2] == "10" else 2
        chunk = s[i:i + step]
        if len(chunk) < 2 or len(chunk) < step:
            raise ValueError(f"牌串长度不对：{text!r}")
        out.append(_parse_card(chunk))
        i += step
    return out


def rank_char(idx: int) -> str:
    return RANK_CHARS[idx]


# ─────────────────────────── 成手牌评估（移植自 call-front/tools/open-size-ev/evaluator.ts） ───────────────────────────

CATEGORY_NAMES = ["高牌", "一对", "两对", "三条", "顺子", "同花", "葫芦", "四条", "同花顺"]
# 英文牌型名：只用于给 AI 模型的提示词（advice.py），/coach/advice 仍用中文。
CATEGORY_NAMES_EN = ["High card", "One pair", "Two pair", "Trips", "Straight",
                     "Flush", "Full house", "Quads", "Straight flush"]

HIGH_CARD, ONE_PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)


def _straight_high(ranks: set[int]) -> int:
    """找最大的顺子顶张（点数下标）；没有返回 -1。轮子 A2345 的顶张是 5（下标 3）。"""
    for hi in range(12, 3, -1):                    # 12..4
        if all((hi - i) in ranks for i in range(5)):
            return hi
    if {12, 0, 1, 2, 3} <= ranks:                  # A + 2345
        return 3
    return -1


def evaluate(cards: list[tuple[int, str]]) -> tuple[int, list[int]]:
    """5/6/7 张里取最好的 5 张，返回 (牌型下标, 踢脚)。比大小看牌型在前、踢脚在后。"""
    rank_count: dict[int, int] = {}
    suit_mask: dict[str, set[int]] = {}
    for rank, suit in cards:
        rank_count[rank] = rank_count.get(rank, 0) + 1
        suit_mask.setdefault(suit, set()).add(rank)

    # 同花：7 张里只可能一个花色到 5 张，命中直接返回
    for suit, mask in suit_mask.items():
        if len(mask) >= 5:
            hi = _straight_high(mask)
            if hi >= 0:
                return STRAIGHT_FLUSH, [hi]
            return FLUSH, sorted(mask, reverse=True)[:5]

    ranks = sorted(rank_count, key=lambda r: (-rank_count[r], -r))   # 张数降序、点数降序
    top = rank_count[ranks[0]]
    second = rank_count[ranks[1]] if len(ranks) > 1 else 0
    present = set(rank_count)

    if top >= 4:
        kicker = max((r for r in present if r != ranks[0]), default=0)
        return QUADS, [ranks[0], kicker]
    if top == 3 and second >= 2:
        return FULL_HOUSE, [ranks[0], ranks[1]]

    straight = _straight_high(present)
    if straight >= 0:
        return STRAIGHT, [straight]

    if top == 3:
        return TRIPS, [ranks[0], *ranks[1:3]]
    if top == 2 and second == 2:
        # 踢脚按点数找剩下最大的那张，不能按 ranks 次序（三对时次序会错）
        kicker = max((r for r in present if r not in (ranks[0], ranks[1])), default=0)
        return TWO_PAIR, [ranks[0], ranks[1], kicker]
    if top == 2:
        return ONE_PAIR, [ranks[0], *ranks[1:4]]
    return HIGH_CARD, sorted(present, reverse=True)[:5]


# ─────────────────────────── 牌面质地（§4 的列） ───────────────────────────

DRY, FLUSH_BOARD, STRAIGHT_BOARD, FLUSH_STRAIGHT_BOARD, PAIRED_BOARD = (
    "干燥", "花面", "顺面", "花顺面", "公对面")


def board_texture(board: list[tuple[int, str]]) -> tuple[str, list[str]]:
    """返回 (主标签, 全部标签)。判定阈值是本项目定的，见 §4（用户已确认）。

    - 花面：有两张或三张同花色
    - 顺面：任意两张点数相差 ≤ 2（有顺子潜力）
    - 公对面：牌面出现对子
    - 干燥：都不满足
    多个标签同时成立时的主标签优先级：公对面 > 花顺面 > 花面 > 顺面 > 干燥
    """
    suits: dict[str, int] = {}
    for _, suit in board:
        suits[suit] = suits.get(suit, 0) + 1
    ranks = sorted(r for r, _ in board)

    tags: list[str] = []
    if max(suits.values()) >= 2:
        tags.append("花面")
    if any(ranks[i + 1] - ranks[i] <= 2 for i in range(len(ranks) - 1)):
        tags.append("顺面")
    if len(set(ranks)) < len(ranks):
        tags.append("公对面")

    if not tags:
        return DRY, []
    if "公对面" in tags:
        return PAIRED_BOARD, tags
    if "花面" in tags and "顺面" in tags:
        return FLUSH_STRAIGHT_BOARD, tags
    if "花面" in tags:
        return FLUSH_BOARD, tags
    return STRAIGHT_BOARD, tags


def _suit_counts(board: list[tuple[int, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, suit in board:
        counts[suit] = counts.get(suit, 0) + 1
    return counts


def _has_run(ranks: set[int], n: int) -> bool:
    """ranks 里是否存在 n 张连续点数（A 可当 1 用，覆盖轮子）。"""
    ext = set(ranks)
    if 12 in ranks:
        ext.add(-1)                                # A 当 1
    return any(all((lo + i) in ext for i in range(n))
               for lo in range(-1, 13 - n + 1))


# 转牌「吓人牌」的三档（postflop-turn.md：吓人牌 = A、K、成花成顺牌）
BLANK, SCARE_HIGH, SCARE_FLUSH, SCARE_STRAIGHT = "blank", "高张", "成花", "成顺"


def turn_scare(board: list[tuple[int, str]]) -> str:
    """转牌（第 4 张）有多吓人 —— 决定「把开火权让给对手」还是照常下注。

    优先级：成花/成顺（牌面已经可能成牌）> 高张（转牌是 A 或 K）> blank。
    成花 = 某花色到了 3 张（公共牌三同花，任何人有 2 张该花色就成花）；
    成顺 = 4 张公共牌里有 3 连（再多一张就成顺）。
    """
    if max(_suit_counts(board).values()) >= 3:
        return SCARE_FLUSH
    if _has_run({r for r, _ in board}, 3):
        return SCARE_STRAIGHT
    if board[-1][0] >= 11:                         # 转牌是 K 或 A
        return SCARE_HIGH
    return BLANK


def board_made_threat(board: list[tuple[int, str]]) -> bool:
    """牌面是否已经「可能成花成顺」—— 河牌判「无关牌面」（抓诈唬的前提）。

    成花：某花色 ≥ 3 张；成顺：公共牌里有 3 连。任一成立就算「有关」，不抓诈唬。
    """
    if max(_suit_counts(board).values()) >= 3:
        return True
    return _has_run({r for r, _ in board}, 3)


def _board_draw_threat(board: list[tuple[int, str]]) -> bool:
    """牌面是否明显在听花或听顺 —— 转牌「凑葫芦给假赔率」的条件。

    听花：某花色 ≥ 2 张（对手手里两张该花色就是同花听）；听顺：牌面 3 连。
    """
    if max(_suit_counts(board).values()) >= 2:
        return True
    return _has_run({r for r, _ in board}, 3)


def board_ceiling(board: list[tuple[int, str]]) -> int:
    """**仅凭公共牌**就能让对手构成的最高牌型 —— 对手「可能」持有的上限。

    这是「相对牌力」的基准：我的牌型低于它，就说明牌面上存在能打败我的牌。
    只数公共牌**自己**能构成的牌型，**不把对手口袋对撞出的三条算进来**——
    否则任何牌面都"可能有三条"，所有牌都得降级，等于没判。

      - 公共牌已是三条 → 对手可能四条
      - 公共牌成对     → 对手可能葫芦（**成一对就是葫芦**，不是三条，见下）
      - 无对子         → 对手可能三条（口袋对撞三条属于「特定持牌」，不计）
      - ≥3 张同花      → 对手可能同花
      - ≥3 张连张      → 对手可能顺子

    **成一对就算葫芦，不是三条。** 7,7,8 板上对手拿 78 就是 77788；7,7,K 板上
    对手拿 7K 就是 777KK。只需要「对子那一 rank 一张 + 另一个板面 rank 一张」，
    是很普通的持牌，不是奇迹。旧实现返回 TRIPS，于是「公对面的同花/顺子」
    永远标不成被压制（同花 > 三条），这是这两类局面漏判的根因。
    """
    counts: dict[int, int] = {}
    for r, _ in board:
        counts[r] = counts.get(r, 0) + 1
    pairs = sum(1 for c in counts.values() if c >= 2)
    if any(c >= 3 for c in counts.values()):
        return QUADS
    ceiling = FULL_HOUSE if pairs >= 1 else ONE_PAIR
    if max(_suit_counts(board).values()) >= 3:
        ceiling = max(ceiling, FLUSH)
    # 这里保留「3 连」的粗判据（规则表的历史口径）；事实层用 rank_hands 的
    # 精确枚举（允许 5-7-9 这种两洞，也会算上我手里的阻断牌）。
    if _has_run({r for r, _ in board}, 3):
        ceiling = max(ceiling, STRAIGHT)
    return ceiling


@dataclass(frozen=True)
class BeatenBy:
    """一种能打败我的牌型，以及枚举出来的组合数/示例。"""

    category: int
    combos: int
    example: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class RankDetail:
    """我的成手牌在「对手任意两张暗牌」里的精确排名。

    与 `beat_rate` 的区别：不只给「多少比例比我大」，还给出**到底哪些牌型**
    比我大、牌面上最大的牌是什么。这是「坚果校验」的事实层：
      · `beaten_by` 为空 = 没有任何对手持牌能打败我（可能是分池的坚果）；
      · `best_opponent_*` = 牌面上对手能拿到的最强牌（绝对坚果基准）。
    """

    total: int
    win_pct: float                 # 对手严格小于我
    tie_pct: float
    beat_pct: float                # 对手严格大于我
    beat_at_least_pct: float       # 多人时至少一家大于我（独立近似）
    equity_pct: float              # (win + tie/2)
    beaten_by: tuple[BeatenBy, ...]
    best_opponent_category: int | None
    best_opponent_cards: tuple[tuple[int, str], ...] | None

    @property
    def hero_is_nuts(self) -> bool:
        """没有任何对手持牌能打败我。可能仍有分池（tie_pct > 0）。"""
        return self.total > 0 and not self.beaten_by


def _rank_cached(hole: tuple[tuple[int, str], ...],
                 board: tuple[tuple[int, str], ...]) -> RankDetail:
    """枚举一次，同时算胜/平/负、按牌型分组的「打败我的牌」、牌面坚果。"""
    if len(hole) + len(board) < 5:
        return RankDetail(0, 0.0, 0.0, 0.0, 0.0, 0.0, (), None, None)
    me = evaluate(list(hole) + list(board))
    used = set(hole) | set(board)
    deck = [(r, s) for r in range(len(RANK_CHARS)) for s in VALID_SUITS
            if (r, s) not in used]

    win = tie = 0
    total = 0
    groups: dict[int, list] = {}          # 牌型 → [组合数, 示例两张]
    best_key: tuple[int, list[int]] | None = None
    best_cards: tuple[tuple[int, str], ...] | None = None

    for c1, c2 in combinations(deck, 2):
        total += 1
        key = evaluate([c1, c2] + list(board))
        if key > me:
            grp = groups.setdefault(key[0], [0, None])
            grp[0] += 1
            if grp[1] is None:
                grp[1] = (c1, c2)
        elif key == me:
            tie += 1
        else:
            win += 1
        if best_key is None or key > best_key:
            best_key = key
            best_cards = (c1, c2)

    if not total:
        return RankDetail(0, 0.0, 0.0, 0.0, 0.0, 0.0, (),
                          None, None)

    beaten = tuple(
        BeatenBy(cat, grp[0], tuple(grp[1]))
        for cat, grp in sorted(groups.items())
    )
    return RankDetail(
        total=total,
        win_pct=win / total,
        tie_pct=tie / total,
        beat_pct=(total - win - tie) / total,
        beat_at_least_pct=(total - win - tie) / total,
        equity_pct=(win + tie / 2) / total,
        beaten_by=beaten,
        best_opponent_category=best_key[0] if best_key else None,
        best_opponent_cards=best_cards,
    )


def rank_hands(hole: list[tuple[int, str]], board: list[tuple[int, str]],
               opponents: int = 1) -> RankDetail:
    """精确枚举对手所有两张暗牌，返回 `RankDetail`（含坚果校验）。

    这个函数是 `beat_rate` 和 `ground_truth` 的**唯一枚举入口**：两者看到的
    胜/平/负、牌型分组、牌面坚果必须完全一致。纯 Python 一次约 5–8ms。
    """
    detail = _rank_cached(tuple(hole), tuple(board))
    n = max(int(opponents), 1)
    if n == 1:
        return detail
    return replace(detail,
                   beat_at_least_pct=1.0 - (1.0 - detail.beat_pct) ** n)


def beat_rate(hole: list[tuple[int, str]], board: list[tuple[int, str]],
              opponents: int = 1) -> tuple[float, float]:
    """枚举对手所有两手牌，算「多大概率比我这手大」，返回 0..1 的两个比例。

    返回 `(单个对手赢我的比例, opponents 家至少一家赢我的概率)`。

    为什么要有这个：`board_ceiling` 只能回答「**有没有可能**被压」（布尔），
    回答不了「**多大概率**」。而这两件事决策含义完全不同 ——

      · 8822 在 88T 上：单个对手 25.9% 赢我们，7 家 **87.7%** 至少一家领先
        → 这不是价值牌，是诈唬牌；收价值必被跟死。
      · 花面的顺子（TsJd / 9h8h7h2c）：单个对手 4.3%，5 家 19.9%
        → 仍然是该收价值的好牌，只是别打光。

    两者被 `capped_by_board` 标成同一个东西，但差了一个数量级。用布尔判断
    会把它们抹平，所以这里直接把概率算出来。

    基准是**随机两手牌**，因此是**下界**：对手既然进了池、跟了注或加了注，
    实际范围只会比随机更窄更强，真实概率只会更高。

    纯 Python 枚举一次约 5ms（47 张牌 C(47,2)=1081 组），每个决策点算一次可以接受。
    """
    detail = rank_hands(hole, board, opponents)
    return detail.beat_pct, detail.beat_at_least_pct


# ─────────────────────────── 牌力分档 ───────────────────────────

# 规则表用的分档键
STRONG_MADE = "强成牌"        # 两对及以上
OVERPAIR = "超对"
TPTK = "顶对强踢"
TP_WEAK = "顶对弱踢"
MID_BOTTOM = "中底对"
STRONG_DRAW = "强听"
WEAK_DRAW = "弱听"
AIR = "空气"
BOARD_PLAY = "打牌面"        # 最好的 5 张全靠公共牌（公共牌自己就是两对/三条…），人人都有

KICKER_STRONG_MIN = 9           # J（下标 9）及以上的踢脚算「强踢」（KJ 在 K 高面算强踢）



@dataclass(frozen=True)
class Read:
    """一手牌在某个牌面上的读牌结果。"""

    tier: str                              # 分档键，规则表按它查
    label: str                             # 人话，如「顶对顶踢」
    category: int                          # evaluate 的牌型下标
    outs: int                              # 改良 outs（顺子补牌 + 同花听 9 张）
    equity_pct: float                      # 四二法则估的成牌率（0-100）
    capped_by_board: bool                  # 牌面上限（board_ceiling）比我的牌型大 → 被牌面压制
    ceiling: int                           # 牌面允许的最高牌型下标，见 board_ceiling
    nut_draw: bool                         # 听牌命中后是不是坚果（严格口径，见 _nut_draw）
    combo: bool = False                    # 成牌 + ≥8 outs 的强听（"有牌又有听"，盲点①）
    plays_board: bool = False              # 最好的 5 张全靠公共牌（盲点②）
    label_en: str = ""                     # 英文标签，只给 AI 提示词用（advice.py）
    # 单个对手（随机两手牌）赢我的比例，0..1，见 beat_rate。
    # capped_by_board 用它做闸门：牌面上限更高**且**真有人能赢我才算被压制。
    beat_pct: float = 0.0

    def to_dict(self) -> dict:
        return {"tier": self.tier, "label": self.label,
                "category": CATEGORY_NAMES[self.category],
                "outs": self.outs, "equity_pct": round(self.equity_pct, 1),
                "relative_strength": ("被牌面压制" if self.capped_by_board else "坚果/牌面之上"),
                "board_ceiling": CATEGORY_NAMES[self.ceiling],
                "beat_by_random_pct": round(self.beat_pct * 100, 1),
                "nut_draw": self.nut_draw, "combo": self.combo,
                "plays_board": self.plays_board}


def _count_outs(hole: list[tuple[int, str]], board: list[tuple[int, str]]) -> tuple[int, bool]:
    """数改良 outs —— 是**张数**不是点数，返回 (outs, 是否同花听)。

    顺子补牌（下一张能成顺的所有牌）与同花补牌取**并集**：这样组合听牌不会重复计数，
    口径和 odds-table.md 第 4 张表对得上（花听+卡顺 = 12、花听+两头顺 = 15）。
    分开相加会多算 2 张（既成花又成顺的那两张），成牌率跟着虚高。
    """
    seen = hole + board
    used = {r for r, _ in seen}
    remaining = {(r, s) for r in range(13) for s in VALID_SUITS} - set(seen)

    straight_completers = {c for c in remaining if _straight_high(used | {c[0]}) >= 0}

    suit_counts: dict[str, int] = {}
    for _, suit in seen:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    flush_suit = next((s for s, n in suit_counts.items() if n == FLUSH_DRAW_SUIT_COUNT), None)
    flush_completers = {c for c in remaining if c[1] == flush_suit} if flush_suit else set()

    return len(straight_completers | flush_completers), flush_suit is not None


def _max_straight_high(ranks: list[int]) -> int:
    """在这个牌面上，任意两张底牌能凑出的**最大**顺子顶张（坚果顺）。"""
    best = -1
    base = set(ranks)
    for a in range(13):
        for b in range(13):
            hi = _straight_high(base | {a, b})
            if hi > best:
                best = hi
    return best


def _nut_draw(hole: list[tuple[int, str]], board: list[tuple[int, str]]) -> bool:
    """我的听牌命中后是不是**坚果**（用户口径：严格）。

      - 同花听：手里握着该花色的 A（坚果同花听）；
      - 顺子听：补成这个牌面上**最大**的顺（坚果顺）。
    任一成立即达标。不是听牌（outs 太少）时返回 False。
    """
    seen = hole + board
    used = {r for r, _ in seen}

    suit_counts: dict[str, int] = {}
    for _, s in seen:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    flush_suit = next((s for s, n in suit_counts.items() if n == FLUSH_DRAW_SUIT_COUNT), None)
    nut_flush = flush_suit is not None and (12, flush_suit) in set(hole)   # 握着该花色 A

    board_nut = _max_straight_high([r for r, _ in board])
    hero_best = max((_straight_high(used | {r}) for r in range(13) if r not in used), default=-1)
    nut_straight = hero_best >= 0 and hero_best >= board_nut

    return nut_flush or nut_straight


def _plays_board(hole: list[tuple[int, str]], board: list[tuple[int, str]]) -> bool:
    """我的最好 5 张是不是**全靠公共牌** —— 公共牌自己就是两对/三条这类牌型，人人都有。

    判据：牌型与「只看公共牌」相同时，我有没有真的贡献到**成对的那个点数**上。
      - 公共牌 `8d 5c 9s 5h 9d`（自己就是两对 9955）+ 我 KQ → 我没碰到 9/5 → 打牌面；
      - 同牌面 + 我 9h5h → 我碰到了 9 和 5 → 这是我的牌，不是打牌面。
    顺子/同花另判：看我的顶张有没有比公共牌自己构成的更高。
    """
    hero_cat, hero_tb = evaluate(hole + board)
    board_cat, board_tb = evaluate(board)
    if hero_cat != board_cat:
        return False
    if hero_cat in (STRAIGHT, FLUSH, STRAIGHT_FLUSH):
        return tuple(hero_tb) <= tuple(board_tb)

    counts: dict[int, int] = {}
    for r, _ in hole + board:
        counts[r] = counts.get(r, 0) + 1
    paired = {r for r, c in counts.items() if c >= 2}
    return not any(r in paired for r, _ in hole)


def read_hand(hole: list[tuple[int, str]], board: list[tuple[int, str]]) -> Read:
    """把「我的两张牌 + 牌面」读成 分档 + 人话标签 + outs + 成牌率。"""
    cat, _tb = evaluate(hole + board)
    hole_ranks = [r for r, _ in hole]
    board_ranks = sorted((r for r, _ in board), reverse=True)
    board_top = board_ranks[0]
    board_paired = len(set(board_ranks)) < len(board_ranks)
    pocket = hole_ranks[0] == hole_ranks[1]

    board_len = len(board)
    if board_len >= 5:                                     # 河牌：没有下一张，不数 outs
        outs, equity = 0, 0.0
    else:
        outs, _flush = _count_outs(hole, board)
        equity = min(outs * (4 if board_len == 3 else 2), 100.0)   # 四二法则

    paired_tiers = (OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM)

    # capped_by_board 是**结构判断**（牌面上可能构成更大的牌），不带频率闸门。
    # 为什么不用频率：「随机两手牌赢我的比例」对跟注站是错的基准 —— 他们不会拿
    # 随机牌跟，能跟/能加的是听牌和顶对以上。花面拿顺子只输 4.3% 的随机牌，
    # 但**跟注范围里的同花全赢我们**。所以这里保留「结构上更高 = 被压制」，
    # 由规则引擎给出「不加注 / 小注跟 / 大注弃」。
    # 频率（beat_pct）另行提供给提示词做定量判断，不参与这里的布尔判定。
    ceil = board_ceiling(board)
    beat = beat_rate(hole, board, 1)[0] if len(board) >= 3 else 0.0

    def mk(tier: str, label: str, label_en: str) -> Read:
        return Read(tier, label, cat, outs, equity, cat < ceil, ceil,
                    _nut_draw(hole, board),
                    combo=(outs >= 8 and tier in paired_tiers),   # 有牌又有听
                    plays_board=(tier == BOARD_PLAY),
                    label_en=label_en, beat_pct=beat)

    # ── 打牌面：最好的 5 张全靠公共牌（人人都有），只有更好的牌会跟 ──
    if cat >= ONE_PAIR and _plays_board(hole, board):
        if outs >= 8:                     # 打牌面 + 强听 → 价值在听牌上，不是那手"人人都有"
            return mk(STRONG_DRAW, f"打牌面+强听({outs} outs)",
                      f"Board play + strong draw ({outs} outs)")
        if outs >= 4:
            return mk(WEAK_DRAW, f"打牌面+弱听({outs} outs)",
                      f"Board play + weak draw ({outs} outs)")
        return mk(BOARD_PLAY, f"{CATEGORY_NAMES[cat]}(打牌面)",
                  f"{CATEGORY_NAMES_EN[cat]} (board play)")

    # ── 两对及以上：强成牌 ──
    if cat >= TWO_PAIR:
        if cat == TRIPS:
            is_set = pocket and hole_ranks[0] in board_ranks
            return mk(STRONG_MADE, "三条(set)" if is_set else "明三条(trips)",
                      "Set" if is_set else "Trips")
        return mk(STRONG_MADE, CATEGORY_NAMES[cat], CATEGORY_NAMES_EN[cat])

    # ── 一对 ──
    if cat == ONE_PAIR:
        if pocket and hole_ranks[0] > board_top:
            return mk(OVERPAIR, "超对", "Overpair")
        if pocket:
            tier = MID_BOTTOM
            if hole_ranks[0] > board_ranks[-1]:
                return mk(tier, "中对(口袋)", "Middle pair (pocket)")
            return mk(tier, "底对(口袋)", "Bottom pair (pocket)")

        matched = [r for r in hole_ranks if r in board_ranks]
        if matched:
            pair_rank = matched[0]
            kicker = hole_ranks[0] if hole_ranks[1] == pair_rank else hole_ranks[1]
            if pair_rank == board_top:
                if kicker >= KICKER_STRONG_MIN:
                    return mk(TPTK, f"顶对{rank_char(kicker)}踢",
                              f"Top pair {rank_char(kicker)} kicker")
                return mk(TP_WEAK, f"顶对{rank_char(kicker)}踢",
                          f"Top pair {rank_char(kicker)} kicker")
            if len(board_ranks) > 1 and pair_rank == board_ranks[1]:
                return mk(MID_BOTTOM, "中对", "Middle pair")
            return mk(MID_BOTTOM, "底对", "Bottom pair")

        # 没配上牌面的一对 = 牌面公对（借牌面）
        if board_paired:
            return mk(MID_BOTTOM, "公对(借牌面)", "Board pair (playing the board)")

    # ── 高牌：看听牌与高张 ──
    if outs >= 8:
        return mk(STRONG_DRAW, f"强听({outs} outs)", f"Strong draw ({outs} outs)")
    if outs >= 4:
        return mk(WEAK_DRAW, f"弱听({outs} outs)", f"Weak draw ({outs} outs)")
    overcards = all(r > board_top for r in hole_ranks)
    if overcards:
        return mk(AIR, "两张高牌", "Two overcards")
    return mk(AIR, "高牌/空气", "High card / air")


# ─────────────────────────── 行动线 ───────────────────────────

# 会「把钱推进底池、逼后面的人跟或弃」的动作。**allin 必须算进来** ——
# 它就是一个 bet/raise（全下），漏掉它会把「面对全下」误判成「没人下注、轮到我」。
AGGRESSIVE = ("bet", "raise", "allin")


@dataclass(frozen=True)
class Line:
    """从「前面的行动」里解出来的局面。"""

    facing_bet: bool                 # 是否轮到我面对一个下注/加注
    bet_to_call: float               # 要**额外**跟的筹码（BB）；已扣掉我本街已投入的。没面对下注为 0
    opponents: int                   # 还在场的对手数
    hero_is_pfr: bool | None         # 翻前我是不是加注者；无法判定时为 None
    checked_to_hero: bool            # 本街没人下注、轮到我行动
    warnings: list[str] = dc_field(default_factory=list)
    opponent_weak_prev: bool = False   # 上一街对手没下注/加注（且有记录）
    hero_aggressive_prev: bool = False # 上一街我下注/加注了
    hero_bet: float = 0.0              # 我本街已投入的下注/加注额（加到总额口径）
    facing_raise: bool = False         # 面对的是加注（不是我下注前的普通下注）
    pot_committed: bool = False        # 我已投入 ≥3/4 筹码（套池），不允许弃牌
    opp_strength: "OpponentRead | None" = None   # 从对手动作读出的牌力


def _num(a: dict) -> float:
    v = a.get("amount")
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def resolve_line(actions: list[dict], street: str, hero_position: str | None) -> Line:
    """把跨街的行动序列解成「我当前面对的局面」。

    amount 口径：**加到总额**（bet 4 = 投到 4；raise 12 = 加到 12）。
    pot 口径：轮到我行动时桌面上的底池总额（含我之前的下注与对手的加注）。
    所以「我下注 B、对手加到 R」时要额外跟的是 R − B，不是 R —— 这正是 §2 表的算法。
    """
    warnings: list[str] = []
    current = [a for a in actions if a.get("street") == street]
    preflop = [a for a in actions if a.get("street") == "preflop"]

    aggro = [a for a in current if a.get("action") in AGGRESSIVE]
    # 最后一个激进动作如果是我自己做的，就不是「面对下注」（没人加注我）
    last = aggro[-1] if aggro else None
    facing = last is not None and (hero_position is None or last.get("actor") != hero_position)

    hero_aggro = [a for a in aggro if a.get("actor") == hero_position] if hero_position else []
    hero_bet = _num(hero_aggro[-1]) if hero_aggro else 0.0
    if not facing:
        hero_bet = 0.0
    elif hero_position is None and hero_aggro:
        warnings.append("没给 hero_position，被加注时算不准我已投入的量")

    # 要额外跟的量 = 对手加到多少 − 我已投入；面对普通下注时 hero_bet=0，两者一致
    bet_to_call = max(_num(last) - hero_bet, 0.0) if facing else 0.0
    facing_raise = facing and last.get("action") in ("raise", "allin")

    # 对手数：优先数「本街出现过、且没弃牌」的人
    actors = {a.get("actor") for a in actions if a.get("actor") and a.get("actor") != hero_position}
    folded = {a.get("actor") for a in actions if a.get("action") == "fold"}
    opponents = len(actors - folded)

    hero_is_pfr: bool | None = None
    if hero_position is None:
        warnings.append("没给 hero_position，无法判断你是不是翻前加注者（PFR）")
    else:
        hero_is_pfr = any(a.get("action") == "raise" and a.get("actor") == hero_position
                          for a in preflop)
    if not actors and street != "preflop":
        warnings.append("前面的行动里没看到其他玩家，对手数按 1 处理")

    # 上一街的局面：判断「对手示弱」（转牌施压例外条款）与「我是否一路开火」（河牌诈唬）
    prev = {"flop": "preflop", "turn": "flop", "river": "turn"}.get(street)
    prev_actions = [a for a in actions if a.get("street") == prev] if prev else []
    opp_aggro_prev = any(a.get("action") in AGGRESSIVE and a.get("actor") != hero_position
                         for a in prev_actions)
    hero_aggro_prev = any(a.get("action") in AGGRESSIVE and a.get("actor") == hero_position
                          for a in prev_actions)

    return Line(facing_bet=facing, bet_to_call=bet_to_call,
                opponents=max(opponents, 1) if facing else opponents,
                hero_is_pfr=hero_is_pfr, checked_to_hero=not facing, warnings=warnings,
                opponent_weak_prev=bool(prev_actions) and not opp_aggro_prev,
                hero_aggressive_prev=hero_aggro_prev,
                hero_bet=hero_bet, facing_raise=facing_raise)


# ─────────────────────────── 读对手牌力 ───────────────────────────

@dataclass(frozen=True)
class OpponentRead:
    """从对手的动作推断出来的牌力（0-100）。"""

    value: int
    label: str          # 弱 / 中 / 强 / 极强
    reason: str
    label_en: str = ""  # 英文标签，只给 AI 提示词用（advice.py）
    reason_en: str = ""

    def to_dict(self) -> dict:
        return {"value": self.value, "label": self.label, "reason": self.reason}


_OPP_LABELS = ((75, "极强"), (55, "强"), (35, "中"), (0, "弱"))
_OPP_LABELS_EN = ((75, "Very strong"), (55, "Strong"), (35, "Medium"), (0, "Weak"))
_OPP_BASE = {"check": 8, "call": 20, "bet": 30, "raise": 55, "allin": 88}


def opponent_strength(actions: list[dict], street: str, hero_position: str | None,
                      pot: float) -> OpponentRead:
    """从对手的动作推断他有多强 —— 纯读牌，按**动作类型 + 下注尺度**打分。

    依据 opponent-read.md：尺度越大越强（§三 线索二「尺度大 = 有牌或想保护」）；
    主动加注比同等尺度的下注更强（§四 线索四「跟注的人通常没有再加强的牌」，反过来加注的人更强）；
    跟注大注 = 有牌或强听（不能再用"他没中"搪塞）；多街连续开火更强。

    **只看动作，不引入范围理论**（文档划的红线）。尺度比例按「他下注前的池」算
    （当前池减掉这一注），所以一笔满池注不会被读成半池。
    """
    opp = [a for a in actions if a.get("actor") and a.get("actor") != hero_position]

    def size_bonus(amount: float | None) -> int:
        if not amount or pot <= 0:
            return 0
        # 比例要按「他下注**前**的池」算：传进来的 pot 含了他这一注，
        # 直接除会把一笔满池/超池注读成半池。pot − amount ≈ 下注前的池。
        before = max(pot - amount, 0.01)
        r = amount / before
        if r >= 1.0:
            return 45
        if r >= 0.66:
            return 30
        if r >= 0.5:
            return 20
        if r >= 0.33:
            return 12
        return 5

    best = _OPP_BASE["check"]
    why = "对手尚无动作"
    why_en = "No opponent action yet"
    for a in opp:
        if a.get("street") != street:
            continue
        act = a.get("action")
        amt = _num(a) or None
        s = _OPP_BASE.get(act, 10)
        if act in ("bet", "raise", "allin", "call"):
            s += size_bonus(amt)
        if s > best:
            best = s
            why = f"对手 {act}" + (f" {amt}" if amt else "")
            why_en = f"Opponent {act}" + (f" {amt}" if amt else "")

    fired = {a.get("street") for a in opp if a.get("action") in AGGRESSIVE}
    if len(fired) >= 2:
        best += 10 * (len(fired) - 1)
        why += f"（{len(fired)} 条街开火）"
        why_en += f" (fired {len(fired)} streets)"

    value = max(0, min(100, best))
    label = next(lbl for th, lbl in _OPP_LABELS if value >= th)
    label_en = next(lbl for th, lbl in _OPP_LABELS_EN if value >= th)
    return OpponentRead(value, label, why, label_en=label_en, reason_en=why_en)


# ─────────────────────────── 尺度 ───────────────────────────

FRACTION = {"1/4": 0.25, "1/3": 1 / 3, "1/2": 0.5, "2/3": 2 / 3, "3/4": 0.75, "1": 1.0}

# 套池阈值：已投入 ≥ 这个比例的筹码后，弃牌几乎总是错误（只能跟或加）。
# **注意：这是本项目定的 3/4，与 core-stance.md §五 第 4 步写的「2/3」不同** ——
# 引擎按 3/4（用户口径），技能文档保持 2/3（那是给 AI 的提示词），这里是刻意的分歧。
POT_COMMITTED_RATIO = 0.75

# §3 成牌尺度：按牌型 × 质地取尺度分数
MADE_SIZING: dict[int, dict[str, str]] = {
    TWO_PAIR:       {DRY: "2/3", FLUSH_BOARD: "3/4", STRAIGHT_BOARD: "3/4",
                     FLUSH_STRAIGHT_BOARD: "3/4", PAIRED_BOARD: "1/2"},
    TRIPS:          {DRY: "2/3", FLUSH_BOARD: "3/4", STRAIGHT_BOARD: "3/4",
                     FLUSH_STRAIGHT_BOARD: "3/4", PAIRED_BOARD: "2/3"},
    STRAIGHT:       {DRY: "1/2", FLUSH_BOARD: "2/3", STRAIGHT_BOARD: "1/2",
                     FLUSH_STRAIGHT_BOARD: "1", PAIRED_BOARD: "2/3"},
    FLUSH:          {DRY: "1", FLUSH_BOARD: "1", STRAIGHT_BOARD: "1",
                     FLUSH_STRAIGHT_BOARD: "1", PAIRED_BOARD: "1"},
    FULL_HOUSE:     {DRY: "2/3", FLUSH_BOARD: "3/4", STRAIGHT_BOARD: "3/4",
                     FLUSH_STRAIGHT_BOARD: "3/4", PAIRED_BOARD: "2/3"},
    QUADS:          {DRY: "2/3", FLUSH_BOARD: "3/4", STRAIGHT_BOARD: "3/4",
                     FLUSH_STRAIGHT_BOARD: "3/4", PAIRED_BOARD: "2/3"},
    STRAIGHT_FLUSH: {DRY: "1", FLUSH_BOARD: "1", STRAIGHT_BOARD: "1",
                     FLUSH_STRAIGHT_BOARD: "1", PAIRED_BOARD: "1"},
}

# §4 一对类：单挑、过牌到我时的尺度（★引申，用户已确认）。
# 值为 None 表示「过牌」。
PAIR_SIZING_HU: dict[str, dict[str, str | None]] = {
    OVERPAIR:    {DRY: "2/3", FLUSH_BOARD: "2/3", STRAIGHT_BOARD: "1/2",
                  FLUSH_STRAIGHT_BOARD: "1/2", PAIRED_BOARD: "1/2"},
    TPTK:        {DRY: "2/3", FLUSH_BOARD: "1/2", STRAIGHT_BOARD: "1/2",
                  FLUSH_STRAIGHT_BOARD: "1/2", PAIRED_BOARD: "1/2"},
    TP_WEAK:     {DRY: "1/2", FLUSH_BOARD: None, STRAIGHT_BOARD: None,
                  FLUSH_STRAIGHT_BOARD: None, PAIRED_BOARD: None},
    MID_BOTTOM:  {DRY: "1/3", FLUSH_BOARD: None, STRAIGHT_BOARD: None,
                  FLUSH_STRAIGHT_BOARD: None, PAIRED_BOARD: None},
}


def _bet(pot: float, frac: str) -> float:
    return round(pot * FRACTION[frac], 2)


# ─────────────────────────── 决策 ───────────────────────────

@dataclass(frozen=True)
class Decision:
    """给一手具体牌的建议。字段与 allin.Decision 对齐（多了尺度与解读）。"""

    ok: bool
    action: str | None                    # fold | check | call | bet | raise
    sizing_bb: float | None               # 下注/加注的目标量；否则 None
    hand: Read | None
    texture: str | None
    reason: str | None
    warnings: list[str] = dc_field(default_factory=list)
    line: Line | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "sizing_bb": self.sizing_bb,
            "hand_label": self.hand.label if self.hand else None,
            "hand_tier": self.hand.tier if self.hand else None,
            "texture": self.texture,
            "opponent_strength": (self.line.opp_strength.to_dict()
                                  if self.line and self.line.opp_strength else None),
            "reason": self.reason,
            "warnings": self.warnings,
        }


def _na(reason: str, hand: Read | None = None, texture: str | None = None,
        warnings: list[str] | None = None) -> Decision:
    return Decision(False, None, None, hand, texture, reason, warnings or [])


def _checked_to_hero(hand: Read, texture: str, line: Line, pot: float,
                     read_name: str = "未知") -> Decision:
    """本街没人下注、轮到我：按 §4/§6/§7 出「下注 or 过牌 + 尺度」。"""
    card = hand.category
    multiway = line.opponents >= 2

    # ── §8 对手形象微调（先于标准线判断） ──
    if read_name == "松弱" and hand.tier in (AIR, WEAK_DRAW):
        return Decision(True, "check", None, hand, texture,
                        f"对松弱（跟注站）不诈唬（§8）：{hand.label} 过牌。",
                        line.warnings, line)
    if read_name == "紧凶" and hand.tier == AIR:
        return Decision(True, "check", None, hand, texture,
                        "对紧凶别诈唬（§8），过牌。", line.warnings, line)
    if read_name == "松凶" and hand.tier == STRONG_MADE:
        return Decision(True, "check", None, hand, texture,
                        f"对松凶诱捕（§8）：{hand.label} 过牌，让他自毁。",
                        line.warnings, line)

    # ── 打牌面：公共牌自带的牌型，人人都有，没有价值 ──
    if hand.tier == BOARD_PLAY:
        return Decision(True, "check", None, hand, texture,
                        f"{hand.label}：这是公共牌自带的牌型，人人都有 → 过牌。",
                        line.warnings, line)
    # ── 有牌又有听（盲点①）：下注，两条赢路 ──
    if hand.combo:
        return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                        f"{hand.label}：有牌又有听（{hand.outs} outs），下 1/2 池，两条赢路。",
                        line.warnings, line)

    # ── 强成牌：§3 ──
    if hand.tier == STRONG_MADE:
        frac = MADE_SIZING[card][texture]
        return Decision(True, "bet", _bet(pot, frac), hand, texture,
                        f"{hand.label}，{texture}面下 {frac} 池（§3）。"
                        f"{'多人池：尺度加大减少对手' if multiway else ''}",
                        line.warnings, line)

    # ── 一对类：§4；多人池按 §7 收紧 ──
    if hand.tier in (OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM):
        frac = PAIR_SIZING_HU[hand.tier][texture]
        if frac is None:
            return Decision(True, "check", None, hand, texture,
                            f"{hand.label}在{texture}面，按 §4 过牌控池。",
                            line.warnings, line)
        if multiway and hand.tier in (TP_WEAK, MID_BOTTOM):
            return Decision(True, "check", None, hand, texture,
                            f"{hand.label}：多人池（§7 人越多越不诈唬、一对降级）→ 过牌。",
                            line.warnings, line)
        if multiway and hand.tier == TPTK:
            frac = "1/2"
            return Decision(True, "bet", _bet(pot, frac), hand, texture,
                            f"{hand.label}：多人池降级，下小注 {frac} 池取薄价值（§7）。",
                            line.warnings, line)
        return Decision(True, "bet", _bet(pot, frac), hand, texture,
                        f"{hand.label}，{texture}面下 {frac} 池（§4）。",
                        line.warnings, line)

    # ── 强听：§5「主动下注有两条赢路」。**多人池也开火** ──
    # §7 说的「人越多越不诈唬」针对的是**诈唬**；强听有真实赢率，不是诈唬。
    if hand.tier == STRONG_DRAW:
        return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                        f"{hand.label}：强听当强牌打，下 1/2 池有两条赢路（§5）；"
                        f"多人池也开火（§7 说的是不诈唬，不是说听牌不开火）。",
                        line.warnings, line)
    if hand.tier == WEAK_DRAW:
        return Decision(True, "check", None, hand, texture,
                        f"{hand.label}：不足 8 outs，过牌（§5）。", line.warnings, line)

    # ── 空气：§6 cbet（只有翻前加注者才开火） ──
    if line.hero_is_pfr and not multiway:
        return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                        "我是翻前加注者、对手过牌示弱，按 §6 持续下注 1/2 池。",
                        line.warnings, line)
    return Decision(True, "check", None, hand, texture,
                    f"{hand.label}，过牌（§3 说不出理由的下注就是漏损）。",
                    line.warnings, line)


def _apply_pot_committed(decision: Decision, line: Line) -> Decision:
    """套池保护：已投入 ≥ POT_COMMITTED_RATIO 筹码后，弃牌几乎总是错误 → 改成跟。

    阈值取 3/4（见 POT_COMMITTED_RATIO 处的说明：与文档的 2/3 是本项目的刻意分歧）。
    只在「面对下注且原本要弃」时生效 —— 不面对下注本来就不会弃。
    """
    if not (decision.ok and line.pot_committed and decision.action == "fold"):
        return decision
    return replace(decision, action="call", sizing_bb=None,
                   reason=f"{decision.reason}（已投入 ≥3/4 筹码＝套池，弃牌几乎总是错误 → 改为跟）")


def _strong_made_dominated(hand: Read, texture: str, line: Line, pot: float) -> Decision:
    """强成牌被牌面压制（我的牌型低于 board_ceiling）→ 不加注，小注跟、大注弃。

    ★引申：文档只写了「我下注时」在湿面怎么打（postflop-made-hand.md），
    没写「我面对下注、持非坚果强牌、牌面又能构成更大的牌」。统一成一条：
    牌面上存在能打败我的牌（成花 / 成顺 / 公对面撞出三条葫芦），价值加注就是送钱。
    """
    pot_frac = line.bet_to_call / pot if pot > 0 else 99.0
    why = f"牌面可能构成{CATEGORY_NAMES[hand.ceiling]}、我的{hand.label}打不过"
    if pot_frac >= FRACTION["1/2"]:
        return Decision(True, "fold", None, hand, texture,
                        f"{why}，遇 {pot_frac:.0%} 池大注 → 弃。", line.warnings, line)
    return Decision(True, "call", None, hand, texture, f"{why} → 只跟不加注。",
                    line.warnings, line)


def _raise_tightening(hand: Read, texture: str, line: Line, pot: float) -> Decision | None:
    """被加注时的收紧：对手加注 = 范围更窄。

    odds-table §2 提醒「不能只凭那张表就多跟」，opponent-read 线索四也说
    「跟注的人通常没有再加强的牌」——反过来说加注的人范围更强。
    一对类/空气扛不住加注，这里直接给出强制建议；不需要收紧时返回 None。
    """
    if not line.facing_raise:
        return None
    if hand.combo:
        return None                    # 有牌又有听，不按"弱一对"收紧
    pot_frac = line.bet_to_call / pot if pot > 0 else 99.0
    if hand.tier in (TP_WEAK, MID_BOTTOM):
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label} 被加注：对手范围更窄 → 弃"
                        f"（odds-table §2 提醒）。", line.warnings, line)
    if hand.tier in (OVERPAIR, TPTK) and pot_frac >= FRACTION["1/2"]:
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label} 被加注（要跟 {pot_frac:.0%} 池）：加注范围窄、一对扛不住 → 弃。",
                        line.warnings, line)
    if hand.tier == AIR:
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label}：被加注，没有理由继续 → 弃。", line.warnings, line)
    return None


def _facing_bet(hand: Read, texture: str, line: Line, pot: float,
                read_name: str = "未知",
                board: list[tuple[int, str]] | None = None) -> Decision:
    """面对下注：先算赔率门槛（§9），再按牌力给 跟/加/弃。"""
    # ── 打牌面：公共牌自带的牌型，没有摊牌价值（先于一切） ──
    if hand.tier == BOARD_PLAY:
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label}：公共牌自带的牌型，人人都有 → 弃。", line.warnings, line)

    forced = _raise_tightening(hand, texture, line, pot)
    if forced is not None:
        return forced

    call = line.bet_to_call
    need_pct = call / (pot + call) * 100 if (pot + call) > 0 else 100.0   # §9 BEP
    pot_frac = call / pot if pot > 0 else 99.0

    # ── 有牌又有听（盲点①）：跟，别按"弱一对"弃。要先于「对手牌力」检查 ──
    if hand.combo and (hand.nut_draw or hand.equity_pct >= need_pct):
        return Decision(True, "call", None, hand, texture,
                        f"{hand.label}：有牌又有听（{hand.outs} outs），跟。", line.warnings, line)

    # 对手牌力「强」以上、我只有弱一对/中底对 → 弃。
    # 这把"多街开火 / 跟大注"这些**尺度之外的**信号也算了进来（不只是看这一注多大）。
    opp = line.opp_strength
    if opp and opp.value >= 55 and hand.tier in (TP_WEAK, MID_BOTTOM):
        return Decision(True, "fold", None, hand, texture,
                        f"对手牌力{opp.label}（{opp.reason}），{hand.label} 弃。",
                        line.warnings, line)

    # ── §8 紧凶：他下注就让路（翻后跟注重注＝几乎一定在慢打强牌） ──
    if read_name == "紧凶" and hand.tier != STRONG_MADE and pot_frac >= FRACTION["1/2"]:
        return Decision(True, "fold", None, hand, texture,
                        f"对紧凶，他下注就让路（§8）：{hand.label} 弃。",
                        line.warnings, line)

    # ── 强成牌：加注求价值（§3）；被牌面压制（可能成花/成顺/撞三条葫芦）就先降级 ──
    if hand.tier == STRONG_MADE:
        if hand.capped_by_board:
            return _strong_made_dominated(hand, texture, line, pot)
        target = round(pot + 2 * call, 2)          # 近似一个满池加注
        return Decision(True, "raise", target, hand, texture,
                        f"{hand.label}：加注求价值（§3 强牌玩大池）。",
                        line.warnings, line)

    # ── 听牌：牌面已成花/成顺时先过「坚果关」，其余按赔率 ──
    if hand.tier in (STRONG_DRAW, WEAK_DRAW):
        if board is not None and board_made_threat(board) and not hand.nut_draw:
            return Decision(True, "fold", None, hand, texture,
                            f"牌面已成花/成顺（可能有更大的牌），{hand.label} 不是坚果听 → 弃"
                            f"（先看听牌质量，再看赔率）。", line.warnings, line)
        if hand.equity_pct >= need_pct:
            return Decision(True, "call", None, hand, texture,
                            f"{hand.label}：约 {hand.equity_pct:.0f}% 高于门槛 "
                            f"{need_pct:.0f}%（§9）→ 跟。", line.warnings, line)
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label}：约 {hand.equity_pct:.0f}% 不够门槛 "
                        f"{need_pct:.0f}%，弃（§9）。", line.warnings, line)

    # ── 一对类：小额跟，大注+湿面弃 ──
    if hand.tier in (OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM):
        if pot_frac >= FRACTION["2/3"] and texture in (FLUSH_STRAIGHT_BOARD, STRAIGHT_BOARD,
                                                       FLUSH_BOARD):
            return Decision(True, "fold", None, hand, texture,
                            f"{hand.label} 在{texture}面遇 {pot_frac:.0%} 池的大注，"
                            f"一对降级 → 弃。", line.warnings, line)
        if hand.tier in (TP_WEAK, MID_BOTTOM) and pot_frac > FRACTION["1/2"]:
            return Decision(True, "fold", None, hand, texture,
                            f"{hand.label} 遇 {pot_frac:.0%} 池下注，弃（§4 会举证的一方是下注者，"
                            f"但弱一对扛不住大注）。", line.warnings, line)
        return Decision(True, "call", None, hand, texture,
                        f"{hand.label}：跟注。需要 {need_pct:.0f}%，"
                        f"一对在{texture}面尚可继续，但转牌再大注就让路。",
                        line.warnings, line)

    # ── 空气 ──
    return Decision(True, "fold", None, hand, texture,
                    f"{hand.label}：没有理由投入筹码，弃（§3）。", line.warnings, line)


def _implied_need_pct(call: float, pot: float, stack_bb: float | None) -> float:
    """跟注需要的胜率，含**保守**的隐含赔率（用户要求「算保守一点」）。

    显性门槛 = call / (pot + call)。隐含部分只在有效筹码够深（≥5 倍跟注额）时计入，
    且只取 min(筹码, 2×池) 的 30% —— 宁可低估潜在收益（门槛偏高、更常弃牌），
    也不高估（门槛偏低、乱跟）。读人的部分（对手河牌会不会付大注）一律不算。
    """
    if call <= 0:
        return 0.0
    implied = 0.0
    if stack_bb is not None and stack_bb >= 5 * call:
        implied = 0.3 * min(stack_bb, 2 * pot)
    return call / (pot + call + implied) * 100


def _advise_turn(hand: Read, texture: str, line: Line, pot: float,
                 stack_bb: float | None, read_name: str,
                 board: list[tuple[int, str]]) -> Decision:
    """转牌：postflop-turn.md「不是耍花招的时候」——有最好的牌就下注。"""
    scare = turn_scare(board)
    multiway = line.opponents >= 2
    if line.facing_bet:
        return _facing_bet_turn(hand, texture, line, pot, stack_bb, read_name, scare, board)

    if read_name == "松凶" and hand.tier == STRONG_MADE:
        return Decision(True, "check", None, hand, texture,
                        f"对松凶诱捕（§8）：{hand.label} 过牌。", line.warnings, line)

    if hand.tier == BOARD_PLAY:
        return Decision(True, "check", None, hand, texture,
                        f"{hand.label}：这是公共牌自带的牌型，人人都有 → 过牌。",
                        line.warnings, line)
    if hand.combo:
        return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                        f"{hand.label}：有牌又有听（{hand.outs} outs），下 1/2 池，两条赢路。",
                        line.warnings, line)

    if hand.tier == STRONG_MADE:
        # 文档原话：转牌凑出葫芦、对手在听 → 下 1/3 池给看似合适的赔率
        # （不能看 texture：凑葫芦时牌面必然是公对面，会把「在听牌」这个标签盖掉，
        #   所以用 _board_draw_threat 直接问牌面有没有花/顺听）
        if hand.category == FULL_HOUSE and _board_draw_threat(board):
            return Decision(True, "bet", _bet(pot, "1/3"), hand, texture,
                            "转牌凑出葫芦、牌面在听 → 下 1/3 池给看似合适的赔率"
                            "（postflop-turn.md）。", line.warnings, line)
        if hand.category in (TWO_PAIR, TRIPS) and scare in (SCARE_FLUSH, SCARE_STRAIGHT):
            return Decision(True, "check", None, hand, texture,
                            f"转牌成{scare}、我只有{hand.label} → 过牌-跟注控池"
                            f"（postflop-turn.md）。", line.warnings, line)
        frac = MADE_SIZING[hand.category][texture]
        return Decision(True, "bet", _bet(pot, frac), hand, texture,
                        f"有最好的牌就下注、不给免费河牌（postflop-turn.md）："
                        f"{hand.label} 下 {frac} 池。", line.warnings, line)

    if hand.tier in (OVERPAIR, TPTK):
        if scare != BLANK and line.opponent_weak_prev:
            return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                            f"转牌成{scare}，但对手翻牌示弱 → 反而施压争池"
                            f"（postflop-turn.md 例外），下 1/2 池。", line.warnings, line)
        if scare != BLANK:
            return Decision(True, "check", None, hand, texture,
                            f"转牌成{scare} → 把开火权让给对手，过牌-跟注控池"
                            f"（postflop-turn.md）。", line.warnings, line)
        return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                        f"{hand.label}，blank 转牌继续下 1/2 池。", line.warnings, line)

    if hand.tier in (TP_WEAK, MID_BOTTOM):
        if line.opponent_weak_prev and not multiway:
            return Decision(True, "bet", _bet(pot, "1/3"), hand, texture,
                            f"{hand.label}：对手两街示弱，薄价值 1/3 池。", line.warnings, line)
        return Decision(True, "check", None, hand, texture,
                        f"{hand.label}：转牌过牌。", line.warnings, line)

    if hand.tier == STRONG_DRAW:
        # 转牌强听同样开火（多人池也一样，见翻牌处对 §7 的说明）；
        # 对手上一街示弱 → 加大到 2/3 池（postflop-draw.md 半诈清单）
        if line.opponent_weak_prev:
            return Decision(True, "bet", _bet(pot, "2/3"), hand, texture,
                            f"{hand.label}：对手翻牌示弱，半诈下注 2/3 池"
                            f"（postflop-draw.md 半诈清单）。", line.warnings, line)
        return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                        f"{hand.label}：转牌强听当强牌打，下 1/2 池（§5）。", line.warnings, line)

    if hand.tier == WEAK_DRAW:
        return Decision(True, "check", None, hand, texture,
                        f"{hand.label}：outs 不足，过牌（§5）。", line.warnings, line)

    if line.hero_is_pfr and not multiway and line.opponent_weak_prev:
        return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                        "对手两街示弱、我是翻前加注者 → 开第二枪 1/2 池。", line.warnings, line)
    return Decision(True, "check", None, hand, texture, f"{hand.label}：过牌。", line.warnings, line)


def _facing_bet_turn(hand: Read, texture: str, line: Line, pot: float,
                     stack_bb: float | None, read_name: str, scare: str,
                     board: list[tuple[int, str]]) -> Decision:
    """转牌面对下注：强成牌加注；两对/三条遇成花成顺改为跟注控池；听牌看（保守）隐含赔率。"""
    forced = _raise_tightening(hand, texture, line, pot)
    if forced is not None:
        return forced

    if hand.tier == BOARD_PLAY:
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label}：公共牌自带的牌型，人人都有 → 弃。", line.warnings, line)
    if hand.combo and hand.nut_draw:
        return Decision(True, "call", None, hand, texture,
                        f"{hand.label}：有牌又有听（{hand.outs} outs）→ 跟。", line.warnings, line)

    opp = line.opp_strength
    if opp and opp.value >= 55 and hand.tier in (TP_WEAK, MID_BOTTOM):
        return Decision(True, "fold", None, hand, texture,
                        f"对手牌力{opp.label}（{opp.reason}），{hand.label} 弃。",
                        line.warnings, line)

    call = line.bet_to_call
    pot_frac = call / pot if pot > 0 else 99.0

    if read_name == "紧凶" and hand.tier != STRONG_MADE and pot_frac >= FRACTION["1/2"]:
        return Decision(True, "fold", None, hand, texture,
                        f"对紧凶，他下注就让路（§8）：{hand.label} 弃。", line.warnings, line)

    if hand.tier in (STRONG_DRAW, WEAK_DRAW):
        # 牌面已成花/成顺时先过「坚果关」，其余按赔率
        if board_made_threat(board) and not hand.nut_draw:
            return Decision(True, "fold", None, hand, texture,
                            f"牌面已成花/成顺（可能有更大的牌），{hand.label} 不是坚果听 → 弃"
                            f"（先看听牌质量，再看赔率）。", line.warnings, line)
        need = _implied_need_pct(call, pot, stack_bb)
        warns = list(line.warnings)
        if stack_bb is None:
            warns.append("没给 stack_bb，隐含赔率按 0 算（只用即时赔率）")
        if hand.equity_pct >= need:
            return Decision(True, "call", None, hand, texture,
                            f"{hand.label}（坚果听）：单街约 {hand.equity_pct:.0f}%，"
                            f"含保守隐含赔率后门槛 {need:.0f}% → 跟。", warns, line)
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label}（坚果听）：单街约 {hand.equity_pct:.0f}% < 门槛 "
                        f"{need:.0f}%（已含保守隐含赔率），弃。", warns, line)

    if hand.tier == STRONG_MADE:
        if hand.capped_by_board:
            return _strong_made_dominated(hand, texture, line, pot)
        if hand.category in (TWO_PAIR, TRIPS) and scare in (SCARE_FLUSH, SCARE_STRAIGHT):
            return Decision(True, "call", None, hand, texture,
                            f"转牌成{scare}、我只有{hand.label} → 跟注控池、不加注"
                            f"（postflop-turn.md）。", line.warnings, line)
        target = round(pot + 2 * call, 2)
        return Decision(True, "raise", target, hand, texture,
                        f"{hand.label}：转牌加注求价值（postflop-turn.md）。", line.warnings, line)

    if hand.tier in (OVERPAIR, TPTK):
        if pot_frac >= FRACTION["2/3"]:
            return Decision(True, "fold", None, hand, texture,
                            f"{hand.label} 遇 {pot_frac:.0%} 池大注，转牌让路 → 弃。",
                            line.warnings, line)
        return Decision(True, "call", None, hand, texture,
                        f"{hand.label}：转牌跟注控池（postflop-turn.md），河牌再大注就让路。",
                        line.warnings, line)

    if hand.tier in (TP_WEAK, MID_BOTTOM):
        if pot_frac > FRACTION["1/2"]:
            return Decision(True, "fold", None, hand, texture,
                            f"{hand.label} 遇 {pot_frac:.0%} 池下注，弃。", line.warnings, line)
        return Decision(True, "call", None, hand, texture,
                        f"{hand.label}：跟一注，转牌再大注就弃。", line.warnings, line)

    return Decision(True, "fold", None, hand, texture,
                    f"{hand.label}：没有理由投入筹码，弃。", line.warnings, line)


def _advise_river(hand: Read, texture: str, line: Line, pot: float,
                  read_name: str, board: list[tuple[int, str]]) -> Decision:
    """河牌：没有下一张。river-medium.md：中等牌不下注，过牌引诱诈唬。"""
    multiway = line.opponents >= 2
    blank_board = not board_made_threat(board)
    if line.facing_bet:
        return _facing_bet_river(hand, texture, line, pot, read_name, blank_board)

    if read_name == "松凶" and hand.tier == STRONG_MADE:
        return Decision(True, "check", None, hand, texture,
                        f"对松凶诱捕（§8）：{hand.label} 过牌。", line.warnings, line)

    if hand.tier == BOARD_PLAY:
        return Decision(True, "check", None, hand, texture,
                        f"{hand.label}：这是公共牌自带的牌型，人人都有 → 过牌。",
                        line.warnings, line)

    if hand.tier == STRONG_MADE:
        return Decision(True, "bet", _bet(pot, "2/3"), hand, texture,
                        f"{hand.label}：河牌价值下注 2/3 池"
                        f"（固定档；钟形曲线是读人的，引擎做不了）。", line.warnings, line)

    if hand.tier in (OVERPAIR, TPTK):        # 河牌顶对 = 中等牌（用户确认）
        if read_name == "松弱":
            return Decision(True, "bet", _bet(pot, "1/2"), hand, texture,
                            f"{hand.label}：对跟注站薄价值 1/2 池（§8）。", line.warnings, line)
        return Decision(True, "check", None, hand, texture,
                        f"河牌中等牌（{hand.label}）过牌引诱诈唬；"
                        f"无位置时过牌-跟注（river-medium.md）。", line.warnings, line)

    if hand.tier in (TP_WEAK, MID_BOTTOM):
        if line.opponents == 1 and read_name == "松弱":
            return Decision(True, "bet", _bet(pot, "1/3"), hand, texture,
                            f"{hand.label}：对跟注站薄价值 1/3 池。", line.warnings, line)
        return Decision(True, "check", None, hand, texture,
                        f"{hand.label}：中等偏弱，过牌。", line.warnings, line)

    if not multiway and read_name == "紧弱" and line.hero_aggressive_prev:
        return Decision(True, "bet", _bet(pot, "2/3"), hand, texture,
                        "单挑、对手紧弱、我一路开火 → 河牌最后诈唬一枪 2/3 池（§8 宽范围偷）。",
                        line.warnings, line)
    return Decision(True, "check", None, hand, texture,
                    f"{hand.label}：放弃，过牌。", line.warnings, line)


def _facing_bet_river(hand: Read, texture: str, line: Line, pot: float,
                      read_name: str, blank_board: bool) -> Decision:
    """河牌面对下注：强成牌价值加注 / 中等牌抓诈唬 / 弃。"""
    forced = _raise_tightening(hand, texture, line, pot)
    if forced is not None:
        return forced

    opp = line.opp_strength
    if opp and opp.value >= 55 and hand.tier in (TP_WEAK, MID_BOTTOM):
        return Decision(True, "fold", None, hand, texture,
                        f"对手牌力{opp.label}（{opp.reason}），{hand.label} 弃。",
                        line.warnings, line)

    call = line.bet_to_call
    pot_frac = call / pot if pot > 0 else 99.0

    if hand.tier == BOARD_PLAY:
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label}：公共牌自带的牌型，人人都有 → 弃。", line.warnings, line)

    if hand.tier == STRONG_MADE:
        if hand.capped_by_board:
            return _strong_made_dominated(hand, texture, line, pot)
        target = round(pot + 2 * call, 2)
        return Decision(True, "raise", target, hand, texture,
                        f"{hand.label}：河牌价值加注（§3 强牌玩大池）。", line.warnings, line)

    if hand.tier in (OVERPAIR, TPTK):
        if blank_board and pot_frac <= FRACTION["2/3"] and read_name != "紧凶":
            return Decision(True, "call", None, hand, texture,
                            f"牌面无关、对手河牌突然下注 → 中等牌（{hand.label}）抓诈唬"
                            f"（river-medium.md 反用）。", line.warnings, line)
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label}：河牌无关牌面之外的下注扛不住 → 弃。", line.warnings, line)

    if hand.tier in (TP_WEAK, MID_BOTTOM):
        if blank_board and pot_frac <= FRACTION["1/2"] and read_name != "紧凶":
            return Decision(True, "call", None, hand, texture,
                            f"{hand.label}：小注、牌面无关，跟一注抓诈唬。", line.warnings, line)
        return Decision(True, "fold", None, hand, texture,
                        f"{hand.label}：河牌弃。", line.warnings, line)

    return Decision(True, "fold", None, hand, texture,
                    f"{hand.label}：河牌没有摊牌价值，弃。", line.warnings, line)


def advise(hero_cards, board, street: str = "flop", actions: list[dict] | None = None,
           pot: float | None = None, hero_position: str | None = None,
           opponents: int | None = None, read_name: str = "未知",
           stack_bb: float | None = None,
           hero_invested_bb: float | None = None) -> Decision:
    """给一手具体牌出建议。

    hero_cards     我的底牌，'KhQh' / ['Kh','Qh'] 均可
    board          公共牌；flop 3 张、turn 4 张、river 5 张
    street         'flop' | 'turn' | 'river'
    actions        前面的行动（含翻前与本街），每项 {"actor","action","amount","street"}
                   amount 用「加到总额」口径；pot 是轮到我行动时的底池总额
    pot            当前底池（BB）。算赔率和尺度都要用，**必填**
    hero_position  我的位置名，用于判断是不是 PFR
    opponents      对手数，给了就用，不给从 actions 推
    read_name      对手形象：松弱/紧弱/松凶/紧凶/未知（§8 微调）
    stack_bb       有效筹码（BB）。转牌隐含赔率用；套池比例的分母也用；不给则按 0 算
    hero_invested_bb  我已投入底池的总筹码（BB）。≥3/4×stack_bb 即「套池」，不允许弃牌

    落在覆盖范围外（street 非法、公共牌张数不对、缺 pot、牌不合法）时 ok=False，绝不猜。
    """
    if street not in ("flop", "turn", "river"):
        return _na(f"street 只能是 flop/turn/river，收到 {street!r}。")
    if pot is None or pot <= 0:
        return _na("缺少 pot（当前底池），算不出赔率与下注尺度。")

    try:
        hole = (parse_cards(hero_cards) if isinstance(hero_cards, str)
                else [_parse_card(c) for c in hero_cards])
        brd = parse_cards(board)
    except ValueError as exc:
        return _na(f"牌面读不出来：{exc}")

    want = {"flop": 3, "turn": 4, "river": 5}[street]
    if len(hole) != 2:
        return _na(f"底牌必须两张，收到 {len(hole)} 张。")
    if len(brd) != want:
        return _na(f"{street} 的公共牌应是 {want} 张，收到 {len(brd)} 张。")
    if len({*hole, *brd}) != 2 + want:
        return _na("有重复的牌。")

    hand = read_hand(hole, brd)
    texture, _tags = board_texture(brd)
    line = resolve_line(actions or [], street, hero_position)

    committed = False
    if hero_invested_bb is not None:
        if stack_bb is None or stack_bb <= 0:
            line = replace(line, warnings=[*line.warnings,
                                           "给了 hero_invested_bb 却没给 stack_bb，套池算不出"])
        else:
            committed = hero_invested_bb >= POT_COMMITTED_RATIO * stack_bb
    if opponents is not None:
        line = replace(line, opponents=opponents)
    line = replace(line, pot_committed=committed,
                   opp_strength=opponent_strength(actions or [], street,
                                                  hero_position, pot))

    if street == "flop":
        decision = (_facing_bet(hand, texture, line, pot, read_name, brd) if line.facing_bet
                    else _checked_to_hero(hand, texture, line, pot, read_name))
    elif street == "turn":
        decision = _advise_turn(hand, texture, line, pot, stack_bb, read_name, brd)
    else:
        decision = _advise_river(hand, texture, line, pot, read_name, brd)
    return _apply_pot_committed(decision, line)
