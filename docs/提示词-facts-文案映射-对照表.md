# 提示词里范围与牌力被写成哪句话（facts → 文案映射对照表）

> 出处：map [#1 对手范围与牌力推导的可观测化](https://github.com/WweiweiSmile/poker-ocr/issues/1)
> 的决策票 [#7](https://github.com/WweiweiSmile/poker-ocr/issues/7)。
> 关联：[trace 字段契约 v1](范围推导-trace-字段契约-v1.md)（#3）、[equity 分解口径 v1](equity-与牌力分解口径-v1.md)（#5）。
>
> 本文件只回答 **"哪句话 = facts 里哪个数、由哪段代码生成"** 以及 **要让 UI 点回去需要什么映射**。
> 采集侵入面与落盘体积归 #6，看板怎么摆归 #8。

## 0. 一句话

提示词里每一个**本手数值**都在 `user` 段由具体渲染函数当场格式化出来；`system` 段除「本手人数」「本手总筹码」两处外只放规则阈值。
现在**没有任何"句子 ↔ 字段"的映射**：看板只能把 `prompt_user` 整段 dump。要支持点一句话跳到那个数，必须**在渲染期旁路产出 refs**，因为事后按 facts 反查对不上（见 §4 的 5 类缺口与 options 去重）。

## 1. 数据流与现有锚点

```
situation ──build_messages(sit)──► (system, user)  ← 唯一的文本产地
   │                                   │
   │                                   └─ prompt_sha256 = sha256(system + "\n---\n" + user)
   └──_facts_snapshot(sit, options)──► facts dict  ← 唯一的数值快照
                                        └─ 两者同帧写进 advice 行（states.jsonl / hand.advices）
```

| 环节 | 位置 | 说明 |
|---|---|---|
| 入口 | `poker_ocr/advice.py:921` `advise()` | |
| 组装 | `poker_ocr/advice.py:1022` → `poker_ocr/prompt/user/__init__.py:116` `build_messages` | 返回 `(build_system(sit), render_facts_zh(sit))` |
| 事实块顺序 | `poker_ocr/prompt/user/__init__.py:33` `render_facts_zh` | 18 段固定顺序，段间空行在 :45/:50/:56/:61 |
| system 骨架 | `poker_ocr/prompt/system/__init__.py:28` `SYSTEM_SECTIONS` + `:40` `build_system` | 恒定 8 段 → 牌型段 → 前注局 → 场景段 |
| 摘要 | `poker_ocr/advice.py:810` `_prompt_digest` | sha256(system + "\n---\n" + user) |
| 快照 | `poker_ocr/advice.py:816` `_facts_snapshot` | 扁平 dict；`hand_ranges`/`fold_eq`/`ground_truth` 三个子块，空值被剔除（:899-900） |
| 落盘 | `poker_ocr/live.py:2054` `advice.to_dict()` → `poker_ocr/live.py:2072` 写 states | **prompt 原文 + facts 在同一条 advice 记录里**；`poker_ocr/live_log.py:49-50` 只是把它们从 events 日志里剔掉 |
| 展示 | `poker_ocr/web/live/app.js:817-830`、`:906` | 纯文本原样输出 |

**关键事实**：prompt 原文从来不需要"为映射而额外落盘"——它和 facts 已经同帧同记录。缺的只是**位置索引**。

## 2. 对照表 A：范围 / 牌力 / 权益（user 段，按屏上顺序）

| # | 文案模板（屏上原样） | 生成点 | 数值来源 | 分类 | 出现条件 |
|---|---|---|---|---|---|
| 1 | `牌力：顶对Q踢` | `poker_ocr/prompt/user/strength.py:76` | `facts.hand_label` / `facts.hand_tier` | 事实（分类名） | 牌型段成立 |
| 2 | `当前牌型：一对` | `strength.py:93` | `facts.ground_truth.made` | 事实 | ground_truth 非空 |
| 3 | `最好五张：K♥Q♥…` | `strength.py:95` | `facts.ground_truth.detail` | 事实 | 同上 |
| 4 | `坚果：是/否` | `strength.py:106` | `facts.ground_truth.nuts` | 事实 | 同上 |
| 5 | `能打败你的牌型（理论上）：…` | `strength.py:120` | `facts.ground_truth.beat_pct/beat_at_least_pct` + 类别词表 | 事实 + 硬编码词表 | 同上 |
| 6 | `听牌：…（{outs} outs）` | `strength.py:122` | `facts.ground_truth.draws.*` | 事实 | 有听牌 |
| 7 | `补牌成牌率（四二法则）…` | `strength.py:137`、`:213` | `facts.equity_pct` | 事实 × 规则常数（2/4 倍） | 听牌且无范围 |
| 8 | `对当前范围权益：39%（按河牌、未计后续下注，3 人池…）` | `strength.py:188`，note 组装 `:162-187` | `facts.hand_ranges.equity` + `opponents_in_hand`/`random_opponents`/`to_call`/mode | 事实 + 条件后缀 | `hand_ranges` 非空 |
| 9 | `当前成牌落后至少一家：39.1%（…不是输牌概率/权益）` | `strength.py:189`、`:198` | `facts.hand_ranges.behind_any` | 事实 + 硬编码说明 | 同上 |
| 10 | `3 人池：实际 3 人 / 范围建模 2 名对手 / 公平份额 33.3%` | `strength.py:199-210` | `facts.opponents_in_hand` + `len(hand_ranges.opponents)` + `1/(actual+1)` | 事实 + 派生算式 | 同上 |
| 11 | `范围：CO 翻牌donk下注进2人 ≈2.0%｜BB 跟注范围 ≈13.4%` | `poker_ocr/prompt/user/ranges.py:205`、`:208`；label 生成 `:173-176` | `hand_ranges.opponents[i].width` + position + **mode/events（label）** | 事实 + 词表 | `hand_ranges` 非空 |
| 12 | `- CO：成牌 95%（一对 67%、三条 28%）；空气 5%；对手成牌领先我 28.3%（平局 18.8%；你领先 52.9%）` | `ranges.py:225-227`；分区 `_composition_zh` `:100` | `opponents[i].behind/tie` + **edge.composition** | 事实 + 词表 | 每个建模对手 |
| 13 | `- 本街还没有动作的座位：…` | `ranges.py:215-218` | **mode=="unacted" 且无 events** | 事实 | 存在未行动座位 |
| 14 | `- 连续过牌封顶：两对+ 13%、顶对 41%；单家范围你领先 61%，但 3 家至少一家领先你 39%；权益 39% 是公平份额 33.3% 的 1.2 倍` | `ranges.py:236` → `:145-170` | **composition.two_pair_plus/top_pair** + `behind_any` + `equity` | 事实 + 派生 | 连续过牌 ≥1 |
| 15 | `- 赢你的：三条、顶对、超对` | `ranges.py:255` | **edge.beaten_by**（share≥0.03，剔空气 `:250`） | 事实 + 阈值常数 | 有 beat 组 |
| 16 | `- 你赢的：一对、空气` | `ranges.py:258` | **edge.ahead_groups** | 事实 | 同上 |
| 17 | `- 范围警告（程序建议）：河牌面对全下，92% 压过你…` | `ranges.py:262-287` | **jam/composition.air/behind/draw** + 阈值 0.80/0.02/0.10 | 派生结论 | 面对全下 |
| 18 | `- 范围建模 N 家；其余 M 家按随机牌计入权益…` | `ranges.py:289-291` | `hand_ranges.random_opponents` | 事实 | M>0 |
| 19 | `- 建模 N 家，实际 M 家；未建模对手未计入范围权益` | `ranges.py:292-294` | `opponents_in_hand` | 事实 | N<M |
| 20 | `- 他的线路剩 ~178 组合/家：顶对强踢 30%、中底对 28%、顶对弱踢 21%、强成牌 16%｜全部弃牌 7%` | `poker_ocr/prompt/user/exploit.py:88-97`；构成 `_mix_zh` `:27-35` | `facts.fold_eq.combos/opponents/fold_equity` + **rep.shares** | 事实 + 词表 | fold_eq 非空 |
| 21 | `- 他的线路已经拿掉弱牌…` / `…留下宽而封顶的范围…` | `exploit.py:38-45` | `fold_eq.narrowed / observed`，阈值 +0.10 | 派生结论 + 常数 | 条件成立 |
| 22 | `- 跟注 4BB：EV +1.5BB（范围权益 39%，实现率 0.85 → 有效 34%；门槛 24%）` | `exploit.py:57-74` | `hand_ranges.equity` + pot_math + 实现率常数 | 事实 + 常数 | 面对下注 |
| 23 | `- 1/2 池：他弃 23%｜需要 25%｜EV -0.2BB（边缘）` | `exploit.py:108-112`、`:48-54` | `facts.fold_eq.lines[].{label,fold_equity,breakeven,ev}` | 事实 | 有尺寸 |
| 24 | `只建模前 N 名对手；另有 M 名未计入范围权益` | `poker_ocr/prompt/user/quality.py:92-101`；聚合 `:68` | `hand_ranges.random_opponents` | 事实（重复 #18） | M>0 |
| 25 | `- raise-1/2｜加注到 5.5BB` | `poker_ocr/prompt/user/options.py:37`（`:62-70` 去重） | `facts.options[]`（**会丢 id**） | 程序计算 | 总是 |
| 26 | `翻前范围结论（程序建议）：…` | `poker_ocr/prompt/user/preflop.py:364` | `facts.range_verdict` | 派生结论（英文长句） | 仅翻前且非空 |

## 3. 对照表 B：system 段与 user 的重复

| system 句 | 位置 | 与 user 的关系 | 风险 |
|---|---|---|---|
| `## 场景：3 人池（本手 3 人）` | `poker_ocr/prompt/system/scenario.py:94`、`:107`（`players = opponents + 1`） | **同一事实**：user `strength.py:205` 也写「3 人池：实际 3 人」 | **R1** 两个来源（`sit.opponents_in_hand` 优先、否则 `snap.opponents_in_hand`）；一处取 `sit`，一处取 `snap`，不一致时同屏两个人数 |
| `## 场景：短码（本手总筹码约 18BB）` | `poker_ocr/prompt/system/scenario.py:90`（`_hero_total_stack`） | facts 只有 `hero_stack`（**后手**），没有「后手+本街已下注」 | **R7** 这句话里的数在 facts 里查不到，refs 无家可归 |
| `- 对当前范围权益 = 对当前建模范围…未计后续下注` | `poker_ocr/prompt/system/metrics.py:12` | 口径定义，对应 user #8 的值 | 定义与值分居 system/user，改一处不改另一处 → 静默分叉 |
| `- 当前成牌落后 = …前者恒 ≥ 后者` | `metrics.py:13` | 对应 user #9（behind_any）与 #12（逐座位 behind） | **R5** 同屏两个「落后」 |
| `- 顶对强踢=顶对且踢脚 ≥J` | `poker_ocr/prompt/system/hand_type.py:23` | 定义 `facts.hand_tier` 的枚举值 | 枚举值 ↔ 规则段应同源 |
| `- 对手范围极度收缩…` | `scenario.py:59-60` | 由 `facts.pot_type` 触发 | 与 user 宽度数字互为注脚 |
| `- 河牌听牌等同空气…` | `scenario.py:68-69` | 与 `ranges.py:269-281` 的 air 分支同口径 | 两处写同一判断 |
| 阈值：`≥40%` 下注率、`14–16%` PFR、`25%/33%/40%` 弃牌率、`≤20BB`、`SPR>6/<3`、`21–40BB` | `system/aggression.py`、`system/stack.py`、`system/scenario.py:24` | 纯硬编码常数，不进 facts | 改阈值时无人能从数据侧发现 |

**结构性结论**：`system` 段除 #R1（人数）与 #R7（本手总筹码）外**不含本手数值**；本手数值 100% 在 `user` 段当场格式化。
`scenario.py:7-8` 已把「不重复事实」写成设计原则——R1 是这条原则目前唯一的破例。

## 4. facts 快照的 5 类缺口（事后反查必然失败的原因）

`_facts_snapshot`（`advice.py:816-900`）不含以下渲染输入，而它们都出现在屏上：

| 缺口 | 出现在 | facts 里有吗 | 在 trace v1 里有吗 |
|---|---|---|---|
| `composition`（成牌/听牌/空气分区、two_pair_plus/top_pair） | ranges #12 #14 | ✗ | ✗ 无现成字段；`seats[i]` 的 169 格 grid 可由 #4 的口径重算出来（M=1326·share·ΠZ），但没有直接可引用的"两对+ 百分比" |
| `beaten_by` / `ahead_groups` | ranges #15 #16 | ✗ | ✓ `seats[i].equity.beaten_by/ahead_groups` |
| `mode` / `events`（含 label 与 unacted 判定） | ranges #11 #13 | ✗ | 部分：`seats[i].mode`、`steps[]` 在，`unacted` 这个判定没有落成字段 |
| `fold_eq.shares`（可弃牌分档占比） | exploit #20 | ✗（只有 `fold_equity/observed/combos/opponents/lines`） | ✗ |
| `sit.opponent_reads`（画像行）、`_hero_total_stack` | 画像段、system #R7 | ✗（`opponent_reads` 在 `to_dict` 顶层，不在 facts） | ✗ |

另外：`render_options_zh`（`options.py:62-70`）会**丢弃**被封顶去重的 id，prompt 里的动作集合 ≠ `facts.options` —— 任何「按 facts 数组下标对回屏上第 k 行」的事后对齐都会错位。

⇒ 映射必须在渲染期产出；refs 的取值域不能只有 `facts`。

## 5. 三个候选机制（成本/回归面）

| 方案 | 做法 | 改多少 | prompt 字节 | 能否定位到"值" | 主要风险 |
|---|---|---|---|---|---|
| **A. 渲染期旁路（推荐）** | 新增 `poker_ocr/prompt/refs.py`：`contextvar` 收集器 + `fact(text, *refs, values=…)`；渲染函数把 `f"..."` 包成 `fact(f"...", "facts:hand_ranges.equity", …)`；`build_messages` 签名不变（收集器未激活时是空操作），新增 `build_messages_refs(sit) -> (system, user, refs)` | ~26 行改 + 1 个新模块 | **不变**（`str` 子类/旁路，golden hash 不动） | ✓ 行 + 值文本 | 收集器是隐式依赖，需单测守住「未激活时零行为」 |
| B. 改签名 | 每个 render 函数多收 `sink` 参数 | 35+ 函数签名 + 全部调用点/测试 | 不变 | ✓ | 改动面大，跨 replay/live 两条链 |
| C. 事后解析 | 对 prompt 文本正则/按行号配 facts | 0（只加解析器） | 不变 | ✗（options 去重、词表翻译都不可逆） | 文案一改就静默失效 |

推荐 A。理由：prompt 文本是唯一不能动的（`replay-console/tests/test_prompt_golden.py:21-22` 的 `EXPECTED_PROMPT_HASH` 是防语义漂移的红线），而 A 恰好把「文本」与「索引」解耦，且不碰任何函数签名。

## 6. refs 契约 v1（已定稿）

四个口径由 issue #7 的决议定下：**行级**（不做字符区间）、**contextvar 旁路**（prompt 字节不变）、**`Advice.prompt_refs`**（随 advice 落 `states.jsonl`）、**`facts:` + `trace:` 双命名空间**。

### 6.1 一条 ref 长什么样

```json
{
  "section": "ranges",
  "line": 27,
  "kind": "fact",
  "refs": [
    {"ns": "facts", "path": "hand_ranges.opponents[0].width", "text": "≈2.0%",
     "deep": {"ns": "trace", "path": "seats[0].final.effective_width_pct"}}
  ]
}
```

| 字段 | 语义 |
|---|---|
| `section` | 渲染段 id。user 段用模块名（`strength`/`ranges`/`exploit`/`quality`/`options`/`preflop`…）；system 段用 `role`/`output`/`priority`/`arbitration`/`decision`/`aggression`/`metrics`/`stack`/`hand_type:<class>`/`ante`/`scenario:<name>` |
| `line` | **该段内**的 1-based 行号，与 `prompt_user.split("\n")` / `prompt_system.split("\n")` 的行号一致（段内偏移，不是全文行号） |
| `kind` | `fact`（值全来自注入事实）/ `derived`（程序算出的结论，如范围警告、跟注 EV、校准话术）/ `static`（规则句或固定文案，`refs` 为空） |
| `refs[].ns` | `facts` 或 `trace`（仅此两个） |
| `refs[].path` | `facts` 里是点路径（`a.b[0].c`）；`trace` 里是 `seats[i].*` / `hero_equity.*` / `params.*` |
| `refs[].text` | 渲染后的字面量（`"≈2.0%"`、`"28.3%"`、`"三条 28%"`），UI 用它在该行内做子串高亮 |
| `refs[].deep` | **可选**下钻链接，只能指向 `trace` |
| `abs` | **实现补充**（v1 落地时新增）：该行在整段 prompt（`system` 或 `user`）里的 1-based 行号，由 `section` 段首 + 段内 `line` 算出。UI 跳行用它，避免每次都要按 section 名重新找段首；`line` 仍是段内行号，契约语义不变。 |

### 6.2 两个命名空间的职责（不要混）

- **`facts:` 是主路径，必须自足。** `facts` 是"模型当时看到了什么"的快照，随同一条 advice 记录落盘，永远可用。任何 `kind:"fact"` 的 `text` 都必须能在 `facts` 里找到对应值。
- **`trace:` 只做下钻，缺失可降级。** 它指向 `logs/live.range.jsonl`（用 `decision_key` join）。`--no-range-trace` 关闭、或看的是老记录时，`deep` 落空——**UI 降级为只显示 facts 上的值，不报错、不空屏**。

这条职责划分是被 #6 的开关逼出来的：trace 可以关，facts 不能。如果主路径指向 trace，关掉开关后整屏文案就点不回数了。

### 6.3 facts 需要补的 7 项（§4 缺口的落法）

`facts` 要自足，就得把这 7 个渲染输入补进 `_facts_snapshot`（都是小标量，估算每决策约 +200 字节）：

| 补进 facts 的路径 | 谁在用 | 备注 |
|---|---|---|
| `hand_ranges.opponents[i].composition` | ranges #12 #14 | `{made, draw, air, two_pair_plus, top_pair, …}`，就是屏上那串分区百分比 |
| `hand_ranges.opponents[i].label` | ranges #11 | 渲染后的范围名（`翻牌donk下注进2人`），不要 UI 再拼一次 |
| `hand_ranges.opponents[i].mode` | ranges #13 | `unacted` 判定要它在 |
| `hand_ranges.opponents[i].beaten_by` / `.ahead_groups` | ranges #15 #16 | 只存 `{name, share}`，combo 示例与 169 键留在 trace |
| `fold_eq.shares` | exploit #20 | `[[name, share], …]`，可弃牌分档 |
| `fold_eq.scale` | exploit #20 | 实测校准倍数，今天也没落 |
| `opponent_reads` | 画像行 | 只放 `{seat, type, conf, n, source}` 摘要（与 `to_dict` 顶层同源，不重复全量） |
| `hero_total_stack` | system 短码段（#R7） | 「后手 + 本街已下注」，今天只有 `hero_stack`（后手） |

`trace` 侧不改：v1 契约不动（`docs/范围推导-trace-字段契约-v1.md` 仍是 v1），`deep` 只用它已有的 `seats[i].final` / `equity` / `steps`。

### 6.4 落盘与版本

| 项 | 约定 |
|---|---|
| 载体 | `Advice.prompt_refs: list[dict]`（默认空），`to_dict()` **只在非空时**写 `prompt_refs`（与 `prompt_system` 等字段同一写法，`advice.py:636-655`） |
| 去向 | `states.jsonl` 的 `advice` 行 + `hand.advices`（`live.py:2054/2072/2614`）；events 日志按 `live_log.py:47-52` 追加 `"prompt_refs"` 到 drop 表，不撑大运维日志 |
| 体积 | 约 26 行 × ~120B ≈ **3KB/决策**，对比同记录已有的 ~7.6KB prompt，可接受；精确预算归 #6 |
| `PROMPT_VERSION` | **不动**。prompt 文本零变化，`EXPECTED_PROMPT_HASH` 不变（`test_prompt_golden.py:22`） |
| `ADVICE_VERSION` | **不动**。不是提示词语义变化；新键是加法，旧消费者忽略（`live.py:2608-2610` 同一约定） |

### 6.5 不做什么

- ❌ 不在 prompt 文本里插锚点/标记/不可见字符（会改字节、改模型输入）。
- ❌ 不做字符区间（文案微调即失效），只做行内子串匹配。
- ❌ 不改 `live.range.jsonl` 的 v1 契约（那是范围推导契约，不是提示词契约）。
- ❌ 不给 `derived` 行硬造 ref：程序结论（范围警告、跟注 EV）标 `derived`，refs 可空或只指向输入。

### 6.6 实现备注（#34 落地时的偏差）

落地后与上面契约的差异，全部是「让 UI 能跳行」和「不给 derived 硬造 ref」两件事，逐条记录：

1. **每条 entry 多一个 `abs`**（见 6.1 末行）：`line` 仍是段内行号，`abs` 是整段 prompt 的行号；`prompt_refs` 按 `abs` 排序。
2. **`最好五张`（§2 #3）的 facts 路径是 `ground_truth.detail`**：`detail` 是英文牌型描述（`"A-high straight"`），渲染的 `text` 是牌面串（`"Kh Ks Qh 7d 2c"`）。路径可解析、`text` 是行内子串（满足验收 3），但值语义不是同一个东西；想拿牌面串请直接用 `ground_truth.made.best_five`（facts 里没存，属已知缺口，未在本票扩契约）。
3. **`derived` 行不给 refs**：`能打败你的牌型（理论上）`、`四二法则成牌率`、`跟注 EV`、`校准话术` 只标 `kind:"derived"`，`refs` 为空——契约 6.5 禁止硬造。`3 人池：实际 N 人…`（#10）、`options` 行、`翻前范围结论`、`本手 N 人` 这几条是程序算出的展示值，`refs` 只指向输入（`opponents_in_hand` / `options` / `range_verdict`），也标 `derived`。
4. **facts 6.3 的落法**：`hand_ranges.opponents[i].label` 直接复用渲染用的 `_range_label(edge)`（UI 不用再拼）；`composition` 落 `{made, draw, air, two_pair_plus, top_pair}`；`beaten_by`/`ahead_groups` 只存 `{name, share}`；`fold_eq.shares` 存 `[[name, share], …]`（share 保留 4 位）、`fold_eq.scale` 保留 4 位；`opponent_reads` 存 `[{seat, type, conf, n, source}]`（`type` = archetype）；`hero_total_stack` = 后手 + 本街已下注。缺数据一律不写键。
5. **system 段只包了两处**：`scenario:short_stack`、`scenario:multiway`（其余 system 段是静态规则，没有可挂的 facts）。

## 7. 验收（实现时必须逐条过）

1. **文本零变化**：`build_messages_refs(sit)` 返回的 `(system, user)` 与 `build_messages(sit)` 逐字节相等，`sha256(system + "\n---\n" + user)` 等于 `EXPECTED_PROMPT_HASH`（`replay-console/tests/test_prompt_golden.py:22`）。
2. **未激活零行为**：`build_messages`（收集器未激活）无副作用、无额外分配、文本同今天。
3. **path 可解析**：每条 `kind:"fact"` 的 `refs[].ns=="facts"` 路径都能在 `_facts_snapshot` 里解析，且 `text` 是该行文本的子串。
4. **降级不崩**：`--no-range-trace` 下 refs 照常生成，`deep` 缺失不抛错。
5. **确定性**：同一 `sit` 渲染两次，`prompt_refs` 逐字节相等（无时间戳、无 set 序）。
6. **可 join**：`deep` 的 trace 路径用 `decision_key` 能定位到唯一一行（`hand+street+actions+pot+to_call+hero`）。

建议落成 `tests/test_prompt_refs.py`（6 条各一个 test，第 1 条复用 `tests/test_advice_prompt_capture.py:14` 的 `make_sit()`）。

## 8. 交接：实现顺序（不在本票范围内做）

1. `poker_ocr/prompt/refs.py`：`contextvar` 收集器 + `fact(text, *refs, kind=…)` + `section(id)` 上下文；未激活时 `fact()` 恒等返回。
2. `_facts_snapshot` 补 §6.3 的 7 项（纯加法）。
3. 26 处渲染点把 `f"…"` 包成 `fact(f"…", …)`；`render_facts_zh` 改为在 `section()` 上下文里调用各段。
4. `Advice.prompt_refs` + `to_dict` + `live_log` drop 表。
5. `tests/test_prompt_refs.py`。
6. #8 消费：点句子 → `refs[].path` → facts 上的值；有 `deep` 时再下钻 trace 的 169 格/combo。

第 3 步是唯一会碰很多文件的一步（26 个渲染点），但它只加包裹、不改文案；任何一步让 prompt 字节变了，就是走错了。
