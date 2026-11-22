# Harrington 决策层设计（2026-11-19）

> 状态：设计稿。目标是把《哈林顿在现金桌》第 1/2 卷编译成决策层基建，
> 替换当前「规则台账 + 策略提示词」的路线。旧 LGB 20 条台账先保留、不再扩展；
> 新链路不读它，是否删除另议。

## 1. 目标与边界

用户要的链路：

```
事实 -> 书中内容 -> 偏移 -> 决策
```

- **事实**：只描述局面，不下结论。现有 `Situation` 继续作为唯一事实源。
- **书中内容**：Harrington 第 1/2 卷编译成**场景卡 + 决策树**。每张卡回答
  「这个局面，书里怎么打」，带页码和 `explicit/interpreted` 标记。
- **偏移**：桌况（ante、松紧、码深）只改**尺寸/频率**；动作由书定。
  需要改动作时，写成显式 `deviation`，单独渲染、单独记日志。
- **决策**：提示词只渲染事实 + 书计划 + 偏移 + `options`；模型只能选
  `options` 的 id，不能自创动作或尺寸。

明确不做：

- 不做原文 RAG（同一局面两次检索可能不同，无法审计）。
- 不再把书拆成互不相干的 if/then 规则堆。
- 不让 LLM 自己从书里「悟」策略；书的内容必须先编译成可检查的卡。
- 不把整本书 OCR 文本提交进仓库（版权）；只提交编译后的卡和页码。

## 2. 四层结构

```
L0 事实层   Situation -> SpotDescriptor
L1 书中层   ScenarioCard[] -> BookResolver -> BookPlan
L2 偏移层   OffsetRule[] -> apply_offsets(BookPlan, TableProfile) -> AdjustedPlan
L3 决策层   render_prompt(事实 + BookPlan + AdjustedPlan + options) -> LLM -> Advice
```

### 2.1 L0 事实层：SpotDescriptor

`SpotDescriptor` 是从 `Situation` 派生的**纯分类**，不包含任何动作。它是
场景卡的匹配键，也是「当前手牌在当前公共牌下属于什么情况」的答案。

```python
@dataclass(frozen=True)
class SpotDescriptor:
    street: str                  # preflop|flop|turn|river
    preflop_node: str            # unopened|limped|raised|three_bet|four_bet
    pot_type: str                # 现有 pot_type
    players: str                 # "2"|"3+"（单挑 / 多人；2026-11-22 起）
    hero_position: str           # 现有位置
    in_position: bool | None
    made_hand: str               # overpair|top_pair|second_pair|third_pair|
                                 # underpair|set|two_pair|straight|flush|
                                 # full_house|air
    draw: str                    # none|flush|oesd|gutshot|combo|overcards
    board: str                   # dry|wet|paired|monotone|two_tone|connected
    spr: str                     # "<3"|"3-6"|">6"|"unknown"
    facing: str                  # none|bet|raise
    commitment: str              # uncommitted|committed|unknown
    opponent: str                # unknown|loose_passive|tight_passive|aggressive
```

规则：

- 这些字段必须能从现有事实**确定性地**算出来；算不出就是 `unknown`，
  不允许模型猜。
- `made_hand` / `draw` / `board` 需要新增分类器；分类器只做牌型/牌面归类，
  不产出动作。
- `commitment` 用「已投入筹码 / 后手」或 SPR 阈值派生，阈值进
  `poker_ocr/tables/`，不写在代码里。

### 2.2 L1 书中层：ScenarioCard

```json
{
  "id": "h1.c4.top_pair.dry.ip",
  "book": "Harrington on Cash Games",
  "volume": 1,
  "chapter": "Ch4 翻牌的紧凶打法：单挑",
  "pages": [186, 190],
  "street": "flop",
  "spot": {
    "preflop_node": "raised",
    "pot_type": "single-raised",
    "players": "2",
    "made_hand": "top_pair",
    "draw": "none",
    "board": "dry",
    "in_position": true,
    "facing": "none"
  },
  "plan": {
    "action": "bet",
    "size": {"kind": "pct_pot", "value": 0.5},
    "future": {
      "turn": "未提升则过牌控池；提升则继续价值",
      "river": "按牌力位置决定价值或放弃"
    }
  },
  "why": "顶对在干燥单挑面通常领先，下注拿价值并保护",
  "confidence": "explicit",
  "priority": 100,
  "offset_policy": "size_only"
}
```

字段约束：

- `id`：`h<卷>.c<章>.<局面>.<位置>`，全局唯一。
- `pages`：**印刷页码**，不是 PDF 页码；另存 `pdf_pages` 便于回溯。
- `confidence`：`explicit` = 书里明确说；`interpreted` = 编译者从上下文归纳，
  必须由用户签字。
- `priority`：同一局面多张卡时，数值大者优先；同优先级不得有交集。
- `offset_policy`：`size_only`（默认）|`none`|`action_allowed`。
- `future`：本手后续街的计划，允许自然语言；不做自动执行，只作为下一街
  的事实/计划上下文。
- `why`：≤60 字中文转述，不抄原文。

Resolver 规则：

1. 按 `street` 过滤；
2. 逐字段匹配 `spot`，`null` 表示不限制；
3. 取匹配字段最多、`priority` 最高的一张；
4. 同分且不完全相同的两张 -> 编译期错误，不允许运行时随机；
5. 零匹配 -> `BookPlan.unknown=true`，prompt 明说「书中无此局面」，
   不伪造书页。

### 2.3 L2 偏移层：OffsetRule

```json
{
  "id": "ante.open_size",
  "when": {"ante_bb": {"gte": 0.5}},
  "target": "size_bb",
  "op": "add",
  "value": 0.0,
  "priority": 10,
  "reason": "0.5BB 前注不改变书里的开池尺度",
  "source": "table_profile"
}
```

约束：

- `target` 只允许 `size_bb` / `size_pct` / `freq`；**不允许 `action`**。
- 需要改动作时，另写 `DeviationRule`，必须带 `reason`，渲染成「偏离」，
  并进日志；本批不做。
- `when` 只读桌况事实（ante、table_stats、effective_stack），不读书中计划。
- 偏移结果必须保留 `baseline` 和 `deltas[]`，prompt 同时渲染两者，
  便于复盘「书说多少、桌况改成多少」。

> **#82 实现补充（2026-11-19）**：示例里的 `add 0.0` 只适用于「书中尺寸
> 本来就在本桌合法 options 里」的桌。0.5BB 前注把本桌最小合法开注抬到
> 7BB 时，最终尺寸必须落在 options 的离散金额上；因此新增 `clamp_options`
> op：把书尺寸移到最近的合法 options 金额（3BB -> 7BB），仍然只改尺寸、
> 不改动作。对应规则同时收窄为 `street=preflop` +
> `preflop_node=unopened` + `ante_bb>=0.5`，避免误伤 3-bet 等翻前加注。

### 2.4 L3 决策层：提示词重建

推倒现有 system/user 中的**策略段**，保留最小输出契约。保留/删除清单：

保留：

- `render_current_state_zh` / `render_geometry_zh` / `render_position_zh` /
  `render_price_zh` / `render_action_line_zh`（纯事实）；
- `render_hand_strength_zh` 中程序算出的 equity/beat 数据；
- `render_data_quality_zh`、`render_options_zh`；
- 输出契约（JSON 字段、只能选 option id）。

删除（新链路不再渲染）：

- `render_preflop_zh` 的建议句；
- `render_rules_zh`（旧 LGB 台账）；
- `render_attack_zh` / `render_exploit_zh` / `render_pool_prior_zh` 的结论句；
- `system/aggression.py`、`system/hand_type.py`、`system/scenario.py` 的
  策略话术；
- `render_profiles_zh` 里的动作提示（只保留描述性数据）。

新增：

- `render_book_plan_zh(BookPlan)`：局面 + 计划 + 页码 + confidence；
- `render_offsets_zh(AdjustedPlan)`：baseline -> final，逐条 delta；
- system 只留：事实优先级、书计划是基线、偏移是显式修正、只能选 options。

版本与测试：

- bump `PROMPT_VERSION`；
- 更新 `replay-console/tests/test_prompt_golden.py` 的
  `EXPECTED_ADVICE_VERSION` / `EXPECTED_PROMPT_HASH`；
- A/B 由维护者在 `tools/ab_set.json` 上跑，agent 只准备对照说明。

## 3. 编译流程

1. **OCR 目录/章节**：已确认两卷为纯扫描件（353/332 页），RapidOCR 可用；
   OCR 文本只落 `/tmp/harrington/`，不进仓库。
2. **章节地图**：
   - 第 1 卷：Ch1 基本概念；Ch2 现金桌要点；Ch3 翻前；Ch4 翻牌单挑；
     Ch5 翻牌多人。
   - 第 2 卷：Ch6 转牌；Ch7 河牌；Ch8 破绽；Ch9 松凶；Ch10 弱牌局；
     Ch11 资金/心理；Ch12 采访。
3. **抽卡**：先做 Ch4 + Ch6 的 10 张卡；每张卡两遍：
   - 第一遍从 OCR 文本抽「局面 -> 动作/尺寸 -> 后续街」；
   - 第二遍对着 PDF 页面核页码、核 `explicit/interpreted`。
4. **校验**：`validate_cards` 检查 id 唯一、页码存在、同优先级无交集、
   `offset_policy` 合法；再加一个 20 个 replay 决策点的解析测试。
5. **接 prompt**：先 shadow（只记录，不进线上），用户确认 10 张卡后再切。

## 4. 里程碑

- M0 本文档 + schema（完成）
- M1 OCR Ch4/Ch6，产出章节文本 + 页码映射（完成）
- M2 场景卡 + `validate_cards`（完成；首批 28 张，后续扩到 130 张）
- M3 Resolver + 20 个 replay 决策点（完成；444 点）
- M4 提示词重建 + golden（进行中；#80 只加不删已接入，#81 待做）
- M5 用户 A/B，决定是否扩到 Ch3/Ch5/Ch7
- M6 翻前书卡（#87，完成；Ch3 的 RFI / 面对加注 / 溜入池三桶，70 张）
- M7 转牌缺口卡（#83，完成；7 张，flop+turn 覆盖 520/591 = 88.0%）
- M8 河牌书卡（#84，完成；#106 后 12 张，river 覆盖 127/129 = 98.4%，2 点故意留空）
- M10 书页逐条回查（#85，完成；130 张 live 卡逐张台账，移出 1 张无法核实卡）

### M2 结果（2026-11-19）

- HU 试点卡 10 张：`poker_ocr/playbook/cards/harrington_cash.json`。
- 多人池卡 18 张：`poker_ocr/playbook/cards/harrington_cash_multiway.json`。
  原因是 replay 1548 个去重决策点里几乎全是 4+ 人池、`in_position=None`；
  HU 试点卡在真实数据上命中 0，只能作为小分支保留。
- 多人池卡按 Ch5/Ch6 的「牌力分类 + 是否面对下注」建，不绑定精确 board：
  空气 check/fold、中对 check/fold、听牌 check/call、强成牌 bet 2/3、
  超对 bet 0.64、顶对 bet 2/3、转牌顶对面对下注 fold、转牌强成牌 bet、
  转牌空气 check/fold。
- `validate_cards` 28 张全过，无同优先级交集；`tests/test_playbook_schema.py`
  + `tests/test_playbook_cards.py` + `tests/test_playbook_cards_multiway.py`
  共 18 passed。
- replay 覆盖：flop+turn 591 个决策点中 444 个命中（75.1%），0 个
  `choose_option` 失败；命中动作分布 check 229 / fold 110 / bet 83 / call 22。
- 仍未覆盖的高频缺口：转牌听牌无人下注 30、转牌打牌面无人下注 13、
  转牌中对面对下注 11、翻牌中对+听牌 7、翻牌顶对面对下注 7、
  翻牌强成牌面对下注 7。下一批优先补这些，不急着扩章节。
- 新增 `poker_ocr/playbook/prompt.py`：`render_book_plan_zh(sit)` 把 `Spot` →
  唯一卡 → 中文「局面/计划/理由/后续」行；零匹配置信地写「书中无此局面」，
  不伪造页码。已用 replay 决策点预览，长度 ≤800 字。
- 结论：多人池优先是对的；HU 卡保留但不作为主链路。M3 的 20 点验收已超额；
  按书页回查已在 #85 补成全量逐张台账（130 张），见
  `docs/harrington-书页回查-2026-11-19.md` 与 `poker_ocr/playbook/book_audit.json`。

### M4 结果（2026-11-19，#80 只加不删）

- 新增 `poker_ocr/prompt/user/book.py`：把 `poker_ocr.playbook.prompt` 的
  渲染器包成 `render_book_plan_zh(sit)`，异常一律吞掉返回空（fail open）。
- `render_facts_zh` 在旧规则台账段之后、攻击/剥削段之前插入
  `## 书中计划（Harrington 现金桌）`；旧段与 `## 可选动作` 契约一字未动，
  #81 才能在同一份提示词上做原子切换对照。
- 版本 `lean-zh-2026-11-19.3` / `advice-2026-11-19.3`；3 个 golden fixture
  都是 3 人池，走零匹配「书中无此局面」，哈希同批更新。
- 新增 `tests/test_prompt_book_plan.py`（5 项）：真实 `build_situation` 路径
  命中（4+ 人池强成牌无人下注 → 下注 0.67 池 / 第 1 卷 p301）、零匹配不印
  页码、800 字硬上限（超限先丢后续/混合行）、多页来源每页带 p、渲染异常
  fail open 不阻塞主循环。

### M6 结果（2026-11-19，#87 翻前书卡）

- 翻前没有公共牌，`situation._read` 直接返回 None，`Spot.made_hand` 恒为
  unknown。只按 `preflop_node` 建卡会对任何两张牌说同一句话（72o 也加注），
  所以给 `Spot` 补三个**确定性派生**维度，都只由事实算出、算不出为 None：
  - `hand_class`：`poker_ocr/playbook/hand_class.py` 从 `hero_cards` 派生，
    词表按书里的牌型分而且**切在书给的频率边界上**——big_pair(AA-QQ) /
    medium_pair(JJ-99) / low_pair(88-66) / small_pair(55-22) / ak / aq /
    ace_broadway(AJ、AT) / weak_ace(A9-A2) / trouble(KQ、KJ、QJ) /
    weak_trouble(KT、QT) / suited_connector(JT-54 同花) /
    speculative(非同花连张与同花隔张) / other。
    中口袋对拆两段是因为 p110 给了两套频率（JJ-99 前位 70% 加注、88-66
    30%），且 p129 攻击溜入者只把 AA-TT 算进加注名单；麻烦牌型拆两段是
    因为 p120 把 KT/QT 单列（中位直接弃）。
  - `position_bucket`：书按前位/中位/后位讲，映射成 early(UTG..UTG+2) /
    middle(LJ、HJ) / late(CO、BTN) / blind(SB、BB)。座位名从
    `poker_ocr/positions.py` 取，不重抄。
  - `hand_suited`：书反复按花色分流（前位只玩 KQs/AJs、KQo 弃），而
    `hand_class` 到「麻烦牌型」就到底了，所以同花与否单独成维；未知写 None，
    不默认非同花。
- 新卡文件 `poker_ocr/playbook/cards/harrington_cash_preflop.json`，70 张，
  全部带印刷页 + PDF 页；翻前卡只写动作与书里的尺寸基线（开注 3BB /
  溜入跟注 1BB / 再加注 9BB），0.5BB 前注由 `offsets.json` 的
  `ante.open_size` 夹到本桌合法档，动作不改。
- 三个主桶的卡：
  - RFI：大口袋对 90% 加注；中口袋对按位置 70%/80% 加注（书 p110 的混合
    频率写进 mix）；小口袋对前中位平跟、后位 80% 加注；AK 75% / AQ 70%
    加注；AJ-AT 与麻烦牌型前位**同花平跟、非同花弃**，中后位加注；
    弱 A 前位弃、中位同花跟/非同花弃、后位 80% 加注；KT/QT 前中位弃、
    后位加注；同花连张前位 20% 加注其余弃、中位平跟、后位加注；
    投机牌只有后位玩。
  - 面对单次加注：大口袋对再加注（书 p108-109 的 3 倍开注 = 9BB）；
    中/低/小口袋对与同花连张有位置跟注、没位置弃牌；AK/AQ 跟注；
    弱 A、AJ-AT、麻烦牌型、KT/QT、投机牌弃；位置未知时按「没位置」兜底
    （priority 90，被有位置信息的卡覆盖）。
  - 溜入池：AA-TT 与 AK/AQ 加注隔离（p129）；88-66 与更小对子、同花连张、
    投机牌、AJ-AT、KQ/KJ 平跟进池；弱 A、KT/QT 与书外牌弃牌。
  - 大盲免费看牌：两张 priority 50 的兜底 check 卡（写死 `hero_position=BB`，
    避免误伤按钮位的「不用跟注」点）。它们不在票面的三个主桶里，但去掉
    会让 44 个真实翻前点重新变成「书中无此局面」，故保留并在此注明。
- 覆盖（`nv tools/preflop_book_coverage.py`，828 个去重翻前决策点）：
  命中 744（89.9%），书中无此局面 84，坏行 0，`choose_option` 选档失败 0。
  - limped/bet 293/293、raised/bet 292/292、unopened/bet 115/115、
    unopened/none 5/5、limped/none 39/40。
  - 未覆盖：three_bet/raise 0/75、three_bet/none 0/1、raised/none 0/7，
    以及 limped/none 的 1 个按钮位怪点（facing=none 却进溜入池）。
  - `in_position` 在 828 点里 718 点为 None，所以 `.ip` 卡命中 0，
    vs_raise 的实际落点全在 priority 90 的位置未知兜底卡上；这是采集缺口
    （动作线没给出相对位置），不是卡错。
  - options 缺动作档 1 例：`preflop.rfi.big_pair` 一个 HJ KK 点，options
    只有 fold + call，没有 raise。属采集缺口，工具单独计数、不计卡错。
- 未做：3-bet / 4-bet 桶（76 点，0%）。书里 AA/KK 与 QQ 面对 3-bet 的处理
  不同（QQ 最多跟注），需要把 big_pair 再拆；本票按「三个主桶」范围不做，
  在票面写明留给后续票。
- 提示词：`_spot_line` 在翻前改渲染「位置分区 / 手牌类 / 同花状态」，翻后行
  一字未动；`PROMPT_VERSION` 随之 bump 到 `lean-zh-2026-11-19.6`
  （golden fixture 全是 flop，三个 hash 不变，只更新
  `EXPECTED_ADVICE_VERSION`）。
- 测试：`tests/test_playbook_hand_class.py`（62 项，含 169 手牌全枚举、座位
  覆盖红警、'10h' 记法的同花识别）、`tests/test_playbook_cards_preflop.py`
  （19 项，含书页频率断言：JJ 前位 0.7 加注、88 前位 0.7 平跟、TT 攻击溜入者
  加注、KQs 前位跟/KQo 弃、KT 中位弃、每张卡 choose_option 落位、前注只改
  尺寸、翻后带 hand_suited 会被 validate_cards 拒绝）。
- 书页没覆盖到的地方一律标 `interpreted` 并在 why 里写明扩展了什么，不假装是
  书里原话：99 随 TT 加注（p129 只列 AA-TT）、AKo/AQo 随 AKs/AQs 加注、
  QJ 随 KQ/KJ 在溜入池跟注、中位非同花弱 A 弃（p118 的「AJ-A7 可玩」未分
  花色，这里取保守口径）。`validate_cards` 会校验 `hand_suited` 必须是布尔
  且只出现在翻前。
- 卡面 why 里的附加条件（对手筹码 ≥ 25 倍加注额才追小对、跟加注需已有
  跟注者、前位加注且后面人多时弃 AQo）当前没有对应的事实维度，卡只按最
  保守的那一支落，条件和理由留在 why 里给模型读。

### M7 结果（2026-11-19，#83 转牌缺口卡）

- 新增 7 张转牌卡进 `poker_ocr/playbook/cards/harrington_cash_multiway.json`
  （25 张，全库 105 张）：
  - `turn.mw.draw.bet`：听牌 / 无人下注 → 下注 0.67 池（半诈唬，p60）。
  - `turn.mw.board_play.check`：打牌面 / 无人下注 → 过牌控池（p21）。
  - `turn.mw.middle_pair.facing_bet`：中对 / 面对下注 → 弃牌（p45）。
  - `turn.mw.top_pair_weak.check`：顶对弱踢 / 无听牌 / 无人下注 → 过牌（p47）。
  - `turn.mw.strong_made.facing_bet`：强成牌 / 面对下注 → 加注一池（p12）。
  - `turn.mw.strong_made.facing_raise`：强成牌 / 面对加注 → 加注一池（p35）。
  - `turn.mw.board_play.facing_bet`：打牌面 / 面对下注 → 弃牌（p24）。
  全部 `priority=100`、`confidence=interpreted`、印刷页与 PDF 页差 6。
- 票面点名的三类缺口：听牌无人下注 32/32、打牌面无人下注 13/13、
  中对面对下注 11/11 现在都有卡可落。听牌卡不限制人数（Ch6 的转牌建议
  本身不按人数分流），因此 3 人池的两个残留点也一并命中。
- 覆盖（`nv tools/book_coverage.py`，flop+turn 591 个去重决策点）：
  命中 520（88.0%），书中无此局面 71，坏行 0；分街 flop 353/397、
  turn 167/194。新卡命中：draw.bet 32、board_play.check 13、
  middle_pair.facing_bet 11、strong_made.facing_bet 6、
  top_pair_weak.check 5、strong_made.facing_raise 5、
  board_play.facing_bet 4。`choose_option` 选档失败 0，缺动作档 0。
- 覆盖工具收成一份：`tools/book_coverage.py` 支持 `--streets all` 与
  `--json`，`tools/preflop_book_coverage.py` 变成 `--streets preflop` 的
  薄包装，翻前 89.9% 的旧口径不变。
- 测试：`tests/test_playbook_turn_gap_cards.py`（26 项：四类听牌都落到
  `turn.mw.draw.bet`、3 人池听牌也命中、六个缺口 Spot 各自命中、七张卡都
  落到合法 options、印刷页/PDF 页偏移、七张卡各有一条渲染断言）；多人池
  计数同步到 25 / 全库 105。
- 仍未覆盖的 flop 缺口（44 点，strong_made bet 7、top_pair_weak bet 6、
  overpair bet 5 等）留给下一批；转牌剩 27 点，主要是 overpair、
  top_pair_top、draw 面对加注这些没有对应卡的分支。

### M8 结果（2026-11-19，#84 河牌书卡）

- 新增 9 张河牌卡，独立成 `poker_ocr/playbook/cards/harrington_cash_river.json`
  （全库 114 张）。河牌卡的 `spot` 只写 `made_hand` + `facing`，**不写
  `players`**：Ch7 的河牌建议本身不按人数分流，3 人池与单挑池的河牌点
  也应命中（转牌 `turn.mw.draw.bet` 同理）。全部 `priority=100`、
  `offset_policy=size_only`、印刷页与 PDF 页差 6：
  - 价值：`river.value.strong_made.bet`，强成牌 / 无人下注 → 下注 0.67 池
    （3.3 好牌要下注 p71；4.1 越接近坚果下越大 p82）。
  - 诈唬：`river.bluff.air.bet`，空气 / 无人下注 → 下注 0.67 池、频率 50%，
    混合过牌 50%（3.7 用最弱牌诈唬 p78；4.1 诈唬尺寸匹配假装牌力 p82；
    书里标准是过牌，诈唬只在前置条件成立时）；`river.bluff.air.facing_bet`，
    空气 / 面对下注 → 弃牌（3.7 标准线 p78，唯一标 `explicit` 的一张）。
  - 抓诈唬：`river.bluff_catch.middle_pair.facing_bet` /
    `river.bluff_catch.top_pair_weak.facing_bet` → 跟注（3.5 哭泣跟注/抓诈唬
    p75；第五节「必须跟一部分」p84-85）。命名跟既有 `turn.tptk.bluff_catch`
    对齐，不再用 `river.catch.*`。
  - 摊牌：`river.check.middle_pair` / `river.check.board_play` /
    `river.check.top_pair_weak` → 过牌（3.5「一点价值以最小代价熬到亮牌」
    p74-75）。
  - 弃牌：`river.fold.board_play.facing_bet` → 弃牌（只打牌面只能分池，
    3.7 无价值弃牌 p78）。
- 覆盖（`nv tools/book_coverage.py --streets river`，129 个去重河牌决策点）：
  #106 后命中 127（98.4%），书中无此局面 2，坏行 0；选项缺动作档 0，
  `choose_option` 选档失败 0。逐卡命中：value.strong_made.bet 31、
  check.board_play 25、bluff.air.bet 22、check.middle_pair 20、
  bluff_catch.middle_pair 6、two_pair.facing_bet 6、check.top_pair_weak 5、
  trips_plus.facing_bet 4、bluff.air.facing_bet 2、trips_plus.facing_raise 2、
  fold.board_play 2、bluff_catch.top_pair_weak 2。
- 已补（#106，2026-11-22）：书在河牌把「两对」归中等（p84）、「三条以上」归
  强牌（p85），面对下注一个跟注控池、一个加注收价值。`hand_tier` 是策略表/
  弃牌率/buckets 共用的粗档，不动；`Spot` 增派生维 `made_hand_detail`，由
  `Situation.category` 派生 `two_pair`（2）/`trips_plus`（3+），算不出 category
  时为 None（fail open）。补 3 张卡：
  `river.two_pair.facing_bet` → 跟注、
  `river.trips_plus.facing_bet` → 加注 1 池、
  `river.trips_plus.facing_raise` → 加注 1 池；尺寸与转牌强成牌加注口径一致。
  两对面对加注（0 点）不落卡，留待有数据再议。
- 仍故意留空 2 点（不写卡，渲染回「书中无此局面」，不硬塞会错一半的基线）：
  - **top_pair_top_kicker 无人下注 2 点**：p71 说「认为可能最好就下注」，
    p84 又说顶对好踢在河牌已降为中等牌力；书没有给出无位置/无下注时的
    明确分支，故不落卡。
- 尺寸只写在卡里（0.67 池），不读旧 `rule_params.json` 的 `river_value_band`，
  也不在代码里写死；`choose_option` 按本桌 options 的离散金额取最近档。
  0.67 是编译基线：书只给方向（越接近坚果下越大、诈唬尺寸匹配故事、
  对手不会跟大注时 30-50% 池），没给固定比例，卡上的 why 已写明「基线」。
- 书页与分类器的取舍（都标 `interpreted` 并在 why 里写明）：
  - **空气无人下注用 50/50 混合**：书的默认是过牌、诈唬是选择性的（3.7
    六条条件）。当前 `Spot` 没有对手类型/行动线强弱维度，无法把「对手示弱、
    故事连贯、恐怖牌」编码成匹配键；用主计划「下注 0.67 池、频率 50%」+
    混合「过牌 50%」把「不能总诈唬」落进渲染，而不是无条件下注。50/50 是
    本层编译的均衡基线，书未给频率，等 A/B 或更深的事实维度再校准。
  - **抓诈唬以跟注为基线**：书里「必须跟一部分」且按注码/故事分支（p85、
    p75）；`Spot` 没有底池赔率与对手故事维度，按最保守的「跟」落卡，
    小注/大注的差别留在 why 给模型结合现场判断。中底对与顶对弱踢属于 3.5
    的「有一点价值」，河牌跟注是这两类牌的正确基线（转牌同牌力是弃牌）。
- 未编码的书页内容：先行动时的 1/3 池阻挡下注（3.5/3.6，p75-77）——`Spot`
  的 `in_position` 在 96% 的 replay 点为 None，没有分支可用；4.2 的
  「筹码=2 倍池会套池」只影响下注后是否弃牌，不影响本层动作。
- 提示词：`_spot_line` 的河牌行原本就能渲染牌力/面对，未改渲染代码；
  `PROMPT_VERSION` bump 到 `lean-zh-2026-11-19.8`（golden fixture 全是 flop，
  三个 hash 不变，只更新 `EXPECTED_ADVICE_VERSION`）。
- 测试：`tests/test_playbook_river_cards.py`（9 张卡各自命中、3 人/单挑池
  也命中、河牌卡不漏到转牌、9 张卡都落到合法 options、尺寸显式在卡里、
  印刷页/PDF 页偏移、诈唬混合渲染、strong_made 面对下注零匹配）；
  `tests/test_playbook_cards_multiway.py` 全库计数同步到 114。


### M9 结果（2026-11-19，#91 面对 3-bet 层）

- 两个新维度都只从事实派生，落在 `poker_ocr/playbook/schema.py`（不动
  `hand_track.py` / `situation.py`）：
  - `facing_level`：数 `snap.actions_of("preflop")` 里的加注动作（bet /
    raise / all_in），2 次 = `three_bet`、3 次 = `four_bet`、4 次 =
    `five_bet`，其余 `unknown`。不用 `preflop_shapes`：shape 只说
    「three-bet 池」，分不出 4-bet/5-bet。
  - `hero_role`：英雄不在加注序列 = `cold`（冷跟/盲注），下标 0 = `open`，
    下标 1 = `three_bet`，更后或拿不到动作线 = `unknown`。
  - 读不到动作线时两者都是 unknown（fail open），`from_dict` 不改；
    `validate_cards` 校验两词表成员且只许出现在翻前卡上。
- 大口袋对按书 p109 拆两段，词表 13 -> 14：`big_pair` 拆成
  `big_pair_aa_kk`（AA-KK，面对 3-bet 继续 4-bet）与 `big_pair_qq`
  （QQ，最多跟注）。旧 3 张 big_pair 卡（RFI / 面对单加注 / 溜入池）各自
  拆成两张，计划与 priority 不变，共 6 张。
- `poker_ocr/playbook/cards/harrington_cash_preflop.json` 从 70 张到 87 张：
  新增 14 张 `preflop.vs_3bet.*`（`preflop_node=three_bet`、
  `facing_level=three_bet`、`priority=100`、`offset_policy=size_only`）：
  - AA-KK：再加注；书没给 4-bet 尺寸，卡只声明动作、尺寸交给本桌第一个
    合法加注档（`plan.size=None`），why 里写明不编数字。
  - QQ：跟注（p109「最多跟注」，弱手可再加注、强手只跟）。
  - 其余 12 类全部弃：JJ-99 与 88-66（p111）、55-22（p113-114）、AK/AQ
    （p115-116）、AJ-AT 与弱 A（p118）、KQ/KJ/QJ（p120）、KT/QT（p120）、
    同花连张（p121，书的条件「有位置且已有跟注者」里「已有跟注者」在
    `Spot` 里没有维度，凑不齐按默认弃）、投机牌与其它（p122）。
  - 明写 5 张标 `explicit`（AA-KK、QQ、JJ-99、88-66、55-22），其余
    9 张 `interpreted`，扩展口径都写在 why 里。
- 4-bet / 5-bet 认得出来但不落卡：渲染回「书中无此局面」，不拿 3-bet 的
  计划顶替（书里没讲这一层）。
- 覆盖（`nv tools/preflop_book_coverage.py`，828 个去重翻前决策点，桶维度
  改成 节点/加注层级/英雄角色，认不出层级的单列 unknown）：
  命中 803（97.0%，#87 时 89.9%），书中无此局面 25，坏行 0，`choose_option`
  选档失败 0，options 缺动作档 1（HJ KK 的 RFI 点，采集缺口，与 #87 同一个点）。
  - `three_bet/three_bet/cold` 42/42、`three_bet/three_bet/open` 17/17，
    即 59 个面对 3-bet 点全部可归因。
  - `three_bet/four_bet/cold` 0/11、`four_bet/three_bet` 0/3、
    `four_bet/open` 0/1、`five_bet/cold` 0/2：共 17 点显式 unknown。
- 票面验收「每张 facing_level=three_bet 卡在 59 点里至少命中 1 次」有 2 张
  做不到：59 点里没有 AA-KK（大对那 2 个点是 QQ），也没有 KQ/KJ/QJ
  （trouble）。这 2 张卡由
  `tests/test_playbook_cards_preflop.py::test_three_bet_layer_has_one_card_per_hand_class`
  用合成局面覆盖，不因「没样本」删卡——删了会让这两类变成「书中无此局面」。
- 提示词：`_spot_line` 的翻前行把 `facing_level` 渲染成「面对 3-bet /
  4-bet / 5-bet」并带英雄角色（未加注过 / 本方开池 / 本方 3-bet），认不出
  层级时退回旧的「面对加注」；手牌类映射同步成 14 键。`PROMPT_VERSION`
  bump 到 `lean-zh-2026-11-19.9`（golden fixture 全是 flop，三个 hash 不变，
  只更新 `EXPECTED_ADVICE_VERSION`）。
- 测试：`tests/test_playbook_hand_class.py` 的 AA/KK/QQ 期望改成两段；
  `tests/test_playbook_cards_preflop.py` 24 项（14 类各一张 3-bet 卡且解析
  回自己、AA-KK 加注无尺寸、QQ 跟注、其余弃、4/5-bet 无卡、提示词渲染
  层级与角色、`validate_cards` 拒绝翻后 facing_level 与非法值、每张卡
  `choose_option` 落位）；`tests/test_playbook_cards_multiway.py` 全库计数
  同步到 131。`nv tests/run_all.py` 84 个套件全过。
- 仍未做（留给后续）：书页印刷 133-171 的漏扫内容（#89 已记）；同花连张
  面对 3-bet 的「已有跟注者」维度；4-bet/5-bet 层没有书页可依，等有第二
  来源或维护者拍板再谈。


### M10 结果（2026-11-19，#85 书页逐条回查）

- 产出 `poker_ocr/playbook/book_audit.json`：130 张 live 卡逐张一条，字段
  `id / file / volume / chapter / pages / pdf_pages / confidence / verdict /
  quote / note / source`。verdict 三值：`ok` 63、`fixed` 56、`compiled` 11；
  `removed` 1 张（`flop.draw.oop.flush`）。
- 人读报告 `docs/harrington-书页回查-2026-11-19.md`：结论统计、方法、修正
  清单、移出说明、逐卡表；按卡 id 可直接查。
- 页码口径复核：第 1 卷 `pdf = 印刷 + 10`、第 2 卷 `+6`；对每张卡引用页
  ±2 页 OCR，关键页目视复核。159 组（卡页, PDF 页）页码口径全部对上。
- 二轮逐条复核（code review 后）发现首轮有 8 张卡把 OCR 文件的 PDF 页号
  当成印刷页、又加了一次偏移：`flop.mw.air.*` p304→p294、
  `flop.mw.board_play.*` p303→p293、`flop.mw.strong_made.bet` p286→p301、
  `preflop.vs_3bet.other` p122→p105；已按原文改回并同步台账与 why。
- 主要在「转述/引用」层修：翻前小对子与中口袋对串页（p110-114）、
  `bb.check.*` 误引小盲节 p123、平跟池 AJ/AT 误引大盲防偷 p128、多人池
  空气/打牌面误引 p285/p302、转牌强成牌误引 p47、河牌价值下注该为
  `explicit`。另有 2 张原 `explicit` 降级（`vs_raise.aq` 的 AQo/前位例外、
  `turn.tptk.bluff_catch` 的跟注延伸）、2 张原 `interpreted` 升级
  （`vs_3bet.ak/aq`，书 p116 明说弃）。
- 移出 `flop.draw.oop.flush`：原引 v2 p25 是牌例一开场，全库找不到
  「65% 下注 60% 池」的原文；按票面「无法核实不进 live」处理。
- 测试 `tests/test_playbook_book_audit.py`（6 项）：台账覆盖每张 live 卡且
  不重复、removed 不在 live 且有理由、每张卡有页码/来源/verdict、台账与卡
  的 pages/pdf_pages/confidence/file 一致、`pdf = 印刷 + 偏移`、explicit 卡
  必须有 ≥6 字的可定位原文。计数同步：单挑 9、全库 130。
- 不调用模型、不跑 A/B；OCR 全文不入库，仓库只留短句与页码。

### M11 结果（2026-11-22，#101–#104 补卡）

- `players` 桶从「单挑 / 3 人 / 4+」收成「单挑 / 3+」：书 Ch5 的多路池建议对
  3 人池与 4+ 是同一条线，再分只会让 3 人池翻后无卡（replay 里 3 人池 flop
  0/9 命中）。24 张多人卡由 `"4+"` 改挂 `"3+"`。
- 翻后 `facing` 改为只看本街：本街一次加注 = 「面对下注」，两次及以上激进
  动作 = 「面对加注」。此前 `facing=raise` 来自翻前 3-bet 池，本街真被加注
  反而记成 `bet`（#102）；拿不到动作线时兜底 `bet`，绝不猜 `raise`。
- 新增 21 张缺口卡（多人池）：翻牌成牌面对下注 4 张、翻牌被加注 8 张、
  转牌顶对强踢/超对 9 张；全部 `interpreted` + `compiled` 台账。全库 151 张。
- 覆盖（1548 个去重决策点）：1438 → **1483（95.8%）**；flop 88.9%→**97.2%**、
  turn 86.1%→**92.3%**、preflop 97.0%、river 89.1%。3 人池 25 点 13→**25/25**。
  余下 65 点：翻前 4-bet/5-bet 与认不出层级 25（故意）、河牌强成牌面对下注
  12（#106，2026-11-22 已补；总覆盖 1520/1548=98.2%，river 127/129）、
  成牌+听牌交叉 11+15（#105）、零散 2。
- 版本 `lean-zh-2026-11-22.10` / `advice-2026-11-22.10`；3 人池 golden
  fixture 现在命中书卡，三个 prompt hash 同批更新。

### M12 结果（2026-11-22，#107 混合频率渲染）

- 书计划段此前只印 `mix` 的频率，主行动（`plan.freq`）按缺省 1.0 渲染，
  模型把「70% 加注 / 30% 跟注」读成「加注 100% + 跟注 30%」。26 张混合卡
  的数据在 #85 已补齐，缺的是渲染与校验。
- `prompt.py`：混合卡的 `计划:` 行括号先写 `频率 70%` 再接来源页；`混合:`
  行统一成 `混合: 跟注 1BB（频率 30%）`。频率为 100% 的纯卡渲染不变。
- `schema.validate_cards`：`plan + mix` 的 freq 合计必须为 1（容差 0.001），
  超出与不足都报错；`tests/test_playbook_prompt.py` 逐张检查 26 张混合卡，
  `tests/test_playbook_schema.py` 覆盖合计不足与容差。
- 版本 `lean-zh-2026-11-22.11` / `advice-2026-11-22.11`；三个 golden fixture
  都不含混合卡，prompt hash 不变。

### M13 结果（2026-11-22，#106 强成牌细档）

- 拆分落点：只在 playbook 层加 `Spot.made_hand_detail`（`two_pair` / `trips_plus`），
  由 `Situation.category` 派生（2 / ≥3），只对 `made_hand=strong_made` 生效；
  `hand_tier`、策略表、fold_equity、buckets 全不动。category 缺失时返回 None，
  渲染回「书中无此局面」，不猜。
- `validate_cards`：`made_hand_detail` 只允许翻后、必须与
  `made_hand=strong_made` 同用、值必须在词表内；否则编译期报错，防止写下
  永不匹配的死卡。
- 补 3 张河牌卡（均 `interpreted` + `compiled`，pages 见台账）：
  `river.two_pair.facing_bet`（跟注，p84/85）、
  `river.trips_plus.facing_bet`（加注 1 池，p85）、
  `river.trips_plus.facing_raise`（加注 1 池，p85 的编译延伸）。
  卡 id 不带 `.mw.`：河牌卡本身不按人数分流，单挑与多人池同卡命中。
- 覆盖：river 115→**127/129（98.4%）**，`strong_made/none/bet` 10/10、
  `strong_made/none/raise` 2/2；全库 #108 后 1508→**1520/1548（98.2%）**。
  剩余 28 点：翻前 25（4-bet/5-bet 与层级不明，故意）、转牌炸弹池 1（#109）、
  河牌 TPTK 无人下注 2。
- 测试：`tests/test_playbook_river_cards.py` 覆盖派生、三张卡的匹配/选档/渲染
  与 category 缺失 fail open；零匹配 fixture 改成 TPTK 无人下注；
  `tests/test_prompt_book_plan.py`、`tests/test_playbook_book_audit.py` 全绿。
- 纯卡集 + 派生维，不改提示词文本；`PROMPT_VERSION` / advice version 不动。
