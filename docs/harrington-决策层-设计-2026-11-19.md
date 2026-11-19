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
    players: str                 # "2"|"3"|"4+"
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
- M2 场景卡 + `validate_cards`（完成；28 张）
- M3 Resolver + 20 个 replay 决策点（完成；444 点）
- M4 提示词重建 + golden（进行中；#80 只加不删已接入，#81 待做）
- M5 用户 A/B，决定是否扩到 Ch3/Ch5/Ch7

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
  按书页回查的抽样核对仍欠着，由 #85 专门补，不阻塞 #80 的只加不删接入。

### M4 结果（2026-11-19，#80 只加不删）

- 新增 `poker_ocr/prompt/user/book.py`：把 `poker_ocr.playbook.prompt` 的
  渲染器包成 `render_book_plan_zh(sit)`，异常一律吞掉返回空（fail open）。
- `render_facts_zh` 在旧规则台账段之后、攻击/剥削段之前插入
  `## 书中计划（Harrington 现金桌）`；旧段与 `## 可选动作` 契约一字未动，
  #81 才能在同一份提示词上做原子切换对照。
- 版本 `lean-zh-2026-11-19.3` / `advice-2026-11-19.3`；3 个 golden fixture
  都是 3 人池，走零匹配「书中无此局面」，哈希同批更新。
- 新增 `tests/test_prompt_book_plan.py`（5 项）：真实 `build_situation` 路径
  命中（4+ 人池强成牌无人下注 → 下注 0.67 池 / 第 1 卷 p288）、零匹配不印
  页码、800 字硬上限（超限先丢后续/混合行）、多页来源每页带 p、渲染异常
  fail open 不阻塞主循环。

## 5. 风险与待定

- **页码映射**：第 1 卷 PDF 页码 = 印刷页码 + 10（已抽样确认）；第 2 卷
  前 6 页为封面/目录，Ch6 从 PDF 7 开始，需逐章复核。
- **牌例页**：大量手牌图是图片，OCR 读不出花色/筹码；牌例暂不进卡，
  只抽正文原则。
- **分类器覆盖**：`made_hand`/`draw` 的边界（如顶对+听牌）需要定义
  优先级；先写测试再写分类。
- **书里的模糊尺度**：Harrington 常用「足够大」「约半池」；编译成
  `pct_pot` 时标 `interpreted`，不假装是原文数字。
- **旧 LGB 台账**：不删除、不扩展；新链路不读。是否物理删除等用户拍板。

## 6. 实施票（2026-11-19）

已按 `/to-tickets` 拆成 8 张 tracer-bullet 票，全部 `ready-for-agent`：

| 票 | 标题 | 阻塞于 |
|---|---|---|
| [#80](https://github.com/WweiweiSmile/poker-ocr/issues/80) | 书计划接进 live 提示词（只加不删） | 无 |
| [#81](https://github.com/WweiweiSmile/poker-ocr/issues/81) | 推倒旧提示词，切到事实 + 书计划 + options 契约 | #80 |
| [#82](https://github.com/WweiweiSmile/poker-ocr/issues/82) | 桌况偏移层：只改尺寸与频率 | #81 |
| [#83](https://github.com/WweiweiSmile/poker-ocr/issues/83) | 转牌缺口卡 | #81 |
| [#84](https://github.com/WweiweiSmile/poker-ocr/issues/84) | 河牌书卡：价值、诈唬、抓诈唬 | #81 |
| [#85](https://github.com/WweiweiSmile/poker-ocr/issues/85) | 书页逐条回查与来源台账 | #81 |
| [#86](https://github.com/WweiweiSmile/poker-ocr/issues/86) | AB 对照准备（人工执行） | #81 |
| [#87](https://github.com/WweiweiSmile/poker-ocr/issues/87) | 翻前书卡：RFI、面对加注、溜入池 | #81、#82 |

本轮拍板的四条：

1. 书卡给计划，模型只在 `options` 里选，代码只校验（不代码硬定动作）。
2. 提示词一次原子切换；先离线构建 + 更新 golden，不调模型；保留 git revert。
3. 偏移层独立配置，只改尺寸/频率，不改动作；每条偏移必须写依据。
4. 先切提示词，再补 turn 缺口与书页回查。
