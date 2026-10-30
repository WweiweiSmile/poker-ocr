# 中文 lean 提示词重构方案

> 状态：已实施（2026-10-29）；2026-10-30.9 后续优化见 README 与 `advice.py`
> 日期：2026-10-29  
> 后续变更：system 已拆成 core + 前注条件模块；新增五级事实优先级、指标口径、
> all-in 几何门控、缺失底池/后手不伪造下注、数据质量中文摘要、范围内部 id 下线。
> 本文件 3.1 的 system 文本是历史版本，以代码 `SYSTEM_CORE_ZH` 为准。
> 适用代码：当前 `main`（`advice.py` 的 `lean` / `lean-rules` / `skills` 三套提示词）  
> 结论：按推荐方案 **1A / 2A / 3A / 4A / 5A / 6B** 实施

---

## 1. 背景与目标

### 1.1 现状

当前实时建议有三套提示词：

| 风格 | 内容 | 主要问题 |
|---|---|---|
| `lean`（默认） | 只给状态、行动线、实时牌力、逐座位画像、桌风、选项 | 事实被删得太多；听牌被标成 `VERY SMALL`；多人池按建模人数算平均份额；没有 SPR/位置/赔率门槛/爆炸底池 |
| `lean-rules`（旧 lean） | 范围表、价值优先、`Value directive`、`Math gate`、爆炸底池、GTO 等 | 规则多且互相矛盾；模型容易抓最近/最具体的一条 |
| `skills` | 2005 年《Little Green Book》技能库，约 20k 字符 | 绝对化规则多、技能摘要互相冲突、上下文过长 |

当前实盘日志和复跑文档大多产生于 `lean-rules` 改名之前，**不能直接用来评价当前默认 `lean`**。本次重构后只保留一套中文 `lean`，旧的两套从 live 链路删除。

### 1.2 目标

1. **只保留一套中文 `lean` 提示词**：`build_messages(sit)` 不再接收 `style`。
2. **删除旧 lean 和 skill 提示词**：`lean-rules`、`skills` 的入口、技能库、CLI 参数、测试和文档。
3. **提示词中文化**：system、事实标题/标签、`reason`、选项标签全部中文；`option id`、JSON key、牌码、range id 保留英文。
4. **补回纯事实，去掉硬规则**：SPR、位置、底池类型、赔率门槛、爆炸底池、转河变化、实际人数等；不再注入 `Value directive`、`Math gate`、完整 RFI 表、GTO。
5. **修复当前 lean 的明确偏差源**：
   - 听牌被标成 `VERY SMALL` + `NOT THE NUTS`；
   - 多人池用建模人数算 `N-way avg`；
   - `Raise to X pot` 标签与实际金额语义不一致；
   - 深码/短码缺少 `all-in` 选项。
6. **保留离线分析能力**：`value_directive` / `math_gate` / `render_hand_facts` 只供 tools/replay 审计，不进入 live prompt。

### 1.3 非目标

- 不实现 GTO 求解器、翻前完整 RFI 表、对手范围求解。
- 不重做 OCR/点击器布局，只给点击器增加可选的 `allin` 按钮支持。
- 不保证旧 replay job 可重跑；旧 `skills` job 转为只读历史。
- 不保留旧 prompt 作为 live fallback；没有 `--advice-style` 切换。

---

## 2. 最终形态

### 2.1 只有一套提示词

```text
build_messages(sit) -> (中文 system, 中文 facts)
```

- `advise(sit, settings, ...)` 不再接收 `style`。
- `AdviceRunner` 不再接收 `style`。
- `live.py` 删除 `--advice-style`。
- `session_start` 不再写 `advice_style`，改为 `prompt_version`。

### 2.2 分层

| 层 | 内容 | 是否进入 prompt |
|---|---|---|
| 状态/行动线 | 桌况、盲注、位置、底牌、街道、公共牌、底池、需跟、行动线 | 是 |
| 几何事实 | 有效筹码、SPR、底池类型、位置、身后人数、短码/side pot、本手已投入 | 是 |
| 价格事实 | 赔率门槛、可争夺底池、跟注占底池比例 | 是 |
| 牌力事实 | 牌力分档、当前牌型、最好五张、坚果（当前成牌）、听牌 outs、四二法则权益、对范围权益、当前成牌落后、多人池实际/建模人数 | 是 |
| 牌面事实 | 花面/顺面/公对面、转河变化、GROUND TRUTH 警告 | 是 |
| 对手事实 | 逐座位画像、VPIP/PFR/AF、样本数、置信度、tentative | 是 |
| 桌风 | `.table_dy` 原文，明确逐座位画像优先 | 是 |
| 选项 | 中文标签 + 实际金额 + option id | 是 |
| 旧策略层 | `Value directive`、`Math gate`、RFI 全表、GTO baseline、board equity | **否** |

### 2.3 推荐的确认项

| 编号 | 选择 | 采用 |
|---|---|---|
| 1 | 中文范围 | **A：system + 事实 + reason + 选项标签全部中文** |
| 2 | 删除深度 | **A：从 live 移除旧 prompt、删除 skills 库；旧分析函数保留离线** |
| 3 | 旧 replay | **A：旧 `skills` job 只读，不再重跑** |
| 4 | 翻前范围 | **A：只加一行程序算的范围结论（无人入池/溜入）；面对加注不给 RFI 结论** |
| 5 | 选项标签 | **A：中文 + 实际金额** |
| 6 | `all-in` | **B：同批增加 `all-in` 选项**（点击器需可选支持） |

---

## 3. 新 system 提示词

### 3.1 最终文本

```text
你是一名德州扑克现金局实时教练。Hero 只有几秒钟，需要你从「可选动作」中选一个能直接执行的动作。

## 输出
只输出一个 JSON 对象，不要 Markdown，不要前后解释：
{"option": "<选项 id>", "reason": "<中文一句话，不超过 30 字>", "confidence": <可选 0-1>}
- option 必须严格来自「可选动作」，不得编造选项 id 或金额。
- reason 必须引用输入中的具体事实（牌力、赔率、位置、对手画像、桌风）；不要引用未提供的规则、图表、概率或“价值指令”。
- 关键事实缺失时，选最保守的合法动作，并在 reason 里说明缺什么。

## 事实优先级
1. 程序计算的事实：底池、需跟、有效筹码、SPR、赔率门槛、牌型/坚果/听牌、多人池人数。它们是权威，不要重算或矛盾。
2. 逐座位对手画像 > 桌级桌风；桌风不能覆盖计算事实。
3. 通用扑克经验只在事实不足时兜底。

## 决策顺序
- 翻前：看位置、前面行动（无人入池/溜入/单加注/3-bet）、有效筹码和范围。不要因为“便宜”用垃圾牌入池；面对加注看价格、位置和对手范围。
- 翻后：先问“哪些更差的牌会跟、哪些更好的牌会加”，再决定价值、诈唬、过牌或弃牌；然后比较权益与赔率门槛。
- 听牌：按 outs、价格、弃牌率、公对面/成花风险判断；不要因为“当前未成牌不是坚果”就否决半诈唬。
- 多人池：顶对/超对降级；没有弃牌率时不要用听牌硬下注。
- 河牌：没有未来牌，听牌等同空气；跟注看赔率和对手线路，价值下注看更差的牌会不会跟。
- 尺度只能从「可选动作」里选，金额由程序算好，不要自己算。
- all-in 是最后手段：只有赔率、筹码深度和底池几何都合理时才用，不要用它代替正常的价值下注。

## 常见错误
- 牌型名字不等于牌型排名；两对、三条、顺子都要看牌面和对手范围。
- 不要因为“已经投入”而跟注；不要因为“怕被反超”而弃掉明显领先的牌。
- 不要编造对手手牌，不要给不存在的范围表/规则找理由。
```

### 3.2 设计说明

- **不写“永远/绝不”**：旧 prompt 的“永远不要慢玩两对/三条”“强牌必须下注”会和牌面/对手冲突；新 prompt 只给决策顺序和常见错误。
- **事实优先级明确**：计算事实 > 逐座位画像 > 桌风 > 通用经验。
- **禁止引用未提供的规则**：防止模型写出“范围表说”“Value directive 说”“Math gate 说”这类不存在的依据。
- **reason 中文**：用户可读；JSON key 保持 `option` / `reason` / `confidence`，解析不变。
- **all-in 有约束**：避免模型把 all-in 当默认尺度。

---

## 4. 新事实块

### 4.1 结构

```text
## 当前局面
桌况 / 盲注 / Hero 位置 / 底牌 / 后手
街道 / 公共牌 / 底池 / 需跟
有效筹码 / SPR / 底池类型 / 位置 / 身后行动人数
赔率门槛 / 可争夺底池 / 短码与 side pot / 本手已投入

## 翻前信息（仅翻前）
翻前形态 / Hero 是否翻前加注者 / 翻前范围结论（仅无人入池或溜入）

## 实时牌力（仅翻后）
牌力分档 / 当前牌型 / 最好五张 / 坚果（当前成牌）
听牌 / 四二法则权益 / 对范围权益 / 当前成牌落后至少一家
多人池：实际人数 / 建模人数 / 公平份额
牌面结构 / 转河变化 / 牌面警告

## 对手画像（逐座位，优先于桌级桌风）
- 位置 名字：类型（n=.., VPIP.., PFR.., AF.., 置信..）[tentative]
  打法：位置开池 / c-bet / 续打 / 溜入 / 对 c-bet 弃…（低频样本带 ≈）
  可剥削：由固定阈值规则生成，只采信实测指标

## 全场数据
32 人 / 193 手：VPIP 41%｜PFR 14%｜3-bet 6%｜溜入 46%｜对 c-bet 弃 79%
整体：松、被动

## 人工观察（现场记录；统计算不出来的东西）
.table_dy 原文（只写上头/走神/合伙这类临时情况）

## 数据缺口
...

## 行动线
翻前 / 翻牌 / 转牌 / 河牌

## 可选动作
- fold｜弃牌
- call｜过牌 / 跟注 XBB / 平跟 XBB
- raise-1/3｜下注 XBB（1/3 池） / 加注到 XBB（1/3 池加注）
...
- all-in｜全下 XBB
```

### 4.2 完整示例（强听牌，面对下注）

```text
## 当前局面
桌况：6 人桌｜盲注 0.5/1.0（前注 0.5）
Hero：BB｜底牌 Ah Qh｜后手 94BB
街道：翻牌｜公共牌 9h 6h 5s｜底池 10.5BB｜需跟 4BB
有效筹码：94BB｜SPR 9.0｜底池类型：单加注池
位置：Hero 无位置｜身后行动：1 人
赔率门槛：4BB / 可争夺底池 14.5BB = 27.6%
可争夺底池：14.5BB（含我的跟注）
短码/side pot：无
本手已投入：3BB

## 实时牌力
牌力：强听牌（9 outs）
当前牌型：A 高（未成牌）
最好五张：Ah Qh 9h 6h 5s
坚果：否（指当前成牌；听牌不代表最终输）
听牌：坚果同花听牌（h，9 outs）；四二法则权益 36%
对范围权益：49%（按河牌，2 人池）
当前成牌落后至少一家：91.9%
多人池：实际 2 人 / 范围建模 2 人 / 公平份额 50%
牌面结构：花听面（未成花）；顺子可能；公对面：否
转河变化：无
牌面警告：无

## 对手画像（逐座位，优先于桌级桌风）
- BTN 老王：LAG（n=20，VPIP 38%，PFR 22%，AF 2.1，置信 0.80）

## 全场数据
32 人 / 193 手：VPIP 41%｜PFR 14%｜3-bet 6%｜溜入 46%｜对 c-bet 弃 79%
整体：松、被动

## 人工观察（现场记录；统计算不出来的东西）
- 3 号位上头了，连输两个买入，刚才用 72o 全下

## 数据缺口
无

## 行动线
翻前（街前底池 1.5BB）：BTN 加注到 3BB → Hero 跟注 3BB
翻牌（街前底池 6BB）：BTN 下注 4BB

## 可选动作
- fold｜弃牌
- call｜跟注 4BB
- raise-1/3｜加注到 8.83BB（1/3 池加注）
- raise-1/2｜加注到 11.25BB（1/2 池加注）
- raise-2/3｜加注到 13.67BB（2/3 池加注）
- raise-1｜加注到 18.5BB（满池加注）
- raise-1.2｜加注到 21.4BB（1.2 池加注）
- all-in｜全下 94BB
```

注意：

- 听牌不再出现 `VERY SMALL`；
- 听牌不再用 `NUT STATUS: NOT THE NUTS` 单独否决；
- `all-in` 标签是实际后手，不是“1.2 倍池”。

### 4.3 关键计算口径

| 字段 | 口径 |
|---|---|
| `实际人数` | `snap.opponents_in_hand`（或非弃牌对手数） |
| `建模人数` | `len(hand_ranges.opponents)`，最多 4 |
| `公平份额` | `1 / (实际人数 + 1) × 100%` |
| `赔率门槛` | `call_amount / winnable_pot × 100%` |
| `可争夺底池` | `winnable_pot(snap)`，扣除无人能跟的超额下注 |
| `SPR` | `有效筹码 / 可争夺底池`（与赔率同口径） |
| `有效筹码` | `min(hero_stack, 所有未弃牌对手 stack)` |
| `本手已投入` | `snap.hero_invested`（start_stack − 当前 stack；未知则不写） |
| `短码` | 任一对少筹码少于 Hero 95% 时单列，并说明 side pot 未建模 |
| `爆炸底池` | `snap.bomb_pot` 或 `pot_per_player ≈ 5BB` |
| `翻前范围结论` | 仅 `preflop_shapes in ("unopened", "limped")` 时渲染；面对加注不渲染 RFI 结论 |

### 4.4 听牌渲染规则

- `HandStrengthRead.kind == "draw"` 时，牌力行写 `听牌` / `DRAW`，**不能写 `VERY SMALL`**。
- 听牌时：
  - 保留“当前牌型 / 听牌 outs / 四二法则权益 / 对范围权益”；
  - `坚果：否` 必须带“指当前成牌”的说明；
  - `当前成牌落后至少一家` 必须带“这是当前未成牌，不代表最终胜率”的说明；
  - 可隐藏 `成牌排名`/`NUT STATUS`，或降级为“当前未成牌参考”。
- 公对面花听、花面顺子等 `ground_truth` 警告必须渲染（中文映射）。

### 4.5 多人池渲染规则

- 不再出现 `5-way avg 20%` 这种“建模人数口径”。
- 8 人池必须写 `实际 8 人 / 范围建模 4 人 / 公平份额 12.5%`。
- `behind_any`/`equity` 后加括号：`按建模 4 人`。
- 如果实际人数 > 建模人数，`数据缺口` 写“只建模前 4 名对手；其余对手按随机牌计入 equity 之外”。

### 4.6 中文/英文边界

| 内容 | 语言 |
|---|---|
| system / 事实标题 / 字段标签 / reason / option 标签 | 中文 |
| option id（`fold`/`call`/`raise-1/3`/`all-in`） | 英文，保持契约 |
| JSON key（`option`/`reason`/`confidence`） | 英文，保持解析 |
| 牌码（`AhQh`/`9h6h5s`） | 英文/数字 |
| range id、mode（`RFI_BTN`/`board:bet`） | 保留英文，避免映射丢失 |
| 内部 warning | 优先中文映射；未覆盖的保留原文并加 `[EN]` |

---

## 5. 选项与动作空间

### 5.1 中文标签与金额

- 无人下注：
  - `下注 3.5BB（1/3 池）`
  - `下注 5BB（1/2 池）`
- 面对下注：
  - `加注到 8.83BB（1/3 池加注）`
  - `加注到 11.25BB（1/2 池加注）`
- 翻前：
  - `开池到 6BB`
  - `3-bet 到 18BB`
  - `4-bet 到 24BB`
- 不再使用 `Raise to 1/3 pot` 这种会让人误以为“总额是 1/3 池”的标签。

`option.id` 不变：

```text
fold / call / raise-1/3 / raise-1/2 / raise-2/3 / raise-1 / raise-1.2 / all-in
```

`Option.note` 也中文化：

- `已抬到本桌最低加注 6BB`
- `后手不足，等同全下`
- `对手下注已覆盖我的后手，跟注即全下`

### 5.2 all-in 选项

**生成规则**：

- 翻后：`hero_stack` 已知且 `hero_max = hero_bet + hero_stack` 大于当前最大档位金额时，追加：
  ```python
  Option("all-in", f"全下 {hero_max:g}BB", "allin", hero_max,
         "剩余后手全部投入")
  ```
- 翻前：后手 ≤ 40BB，或面对 3-bet/4-bet 时追加 all-in；100BB 的普通开池不追加，避免选项诱导。
- `to_call >= hero_stack` 时仍走现有 `Call all-in / Fold` 分支，不重复加 all-in。

**点击器**：

- `Option.kind = "allin"`。
- `poker_ocr/clicker.py` 增加可选按钮 `allin`：
  - 配置里有 `allin` 矩形 → 点 all-in；
  - 没有 → `ClickPlan("allin", skip_reason="missing_button:allin")`，**建议仍显示，但不自动点**。
- `tools/calibrate_clicker.py` 增加 `allin` 按钮标定（可选）。
- `REQUIRED_BUTTONS` 不强制包含 `allin`，旧配置继续可用。
- `allin` 不进入 `PRESET_IDS`/`preset_amounts`；`plan_clicks` 单独处理。

### 5.3 翻前 5.5BB 下限

- 保留 `PREFLOP_MIN_RAISE_BB = 5.5`。
- 选项 id 继续用 `open-2.5` 等旧 id，但标签显示实际金额：
  - `开池到 5.5BB（本桌最低加注）`
- 新增事实行：
  - `本桌翻前最低加注：5.5BB；所有加注选项已不低于该值`
- 避免模型在 reason 里建议“加到 2.5–3BB”。

---

## 6. 删除清单

### 6.1 旧提示词

`poker_ocr/advice.py`：

- [x] 删除 `_LEAN_RULES_SYSTEM`
- [x] 删除 `_build_messages_lean_rules`
- [x] 删除 `_GROUND_TRUTH_RULES`
- [x] 删除 `_build_messages_skills`
- [x] 删除 `_skill_context`
- [x] 删除 `import skills`
- [x] 删除 `PROMPT_STYLE_LEAN_RULES`、`PROMPT_STYLE_SKILLS`
- [x] `PROMPT_STYLES` 只保留 `("lean",)`，或完全删除 style 参数
- [x] `build_messages(sit)` 不再接收 `style`
- [x] `build_situation` 删除 `style` 参数与 `block_draw_bets`
- [x] `advise` 删除 `style`、skills 选择、Math gate 重问
- [x] `AdviceRunner` 删除 `style`
- [x] `Advice.skill_codes` 保留但恒为 `()`（兼容一版 JSONL）
- [x] `Advice.gate` / `gate_violation` 不再写入
- [x] `Advice.plan` 不再要求，保留字段但通常为空

### 6.2 技能库

- [x] 删除 `poker_ocr/skills.py`
- [x] 删除 `poker_ocr/skills_md/`
- [x] 删除 `poker_ocr/skills_md_en/`
- [x] 删除 `tests/test_advice.py` 中 skills 加载/路由/注入用例
- [x] 删除 `tools/compare_advice_styles.py`
- [x] 删除 `tests/test_compare_advice_styles.py`
- [x] 更新 `README`、`docs/live-stream.md` 中 skills/lean-rules 段落
- [x] 更新 `poker_ocr/opponent_profile.py` 中“供 skills 提示词共用”的注释

### 6.3 CLI / live

`poker_ocr/live.py`：

- [x] 删除 `--advice-style`
- [x] 删除 `PROMPT_STYLES` 导入
- [x] 删除 style 日志分支
- [x] `AdviceRunner(...)` 不再传 `style`
- [x] `build_situation(...)` 不再传 `style`
- [x] `session_start` 删除 `advice_style`，新增 `prompt_version`

### 6.4 离线分析函数（保留，但不进 prompt）

以下函数保留在 `advice.py`，**只给 tools/replay 使用**，并在注释标 `legacy offline only`：

- `value_directive`
- `math_gate` / `MathGate`
- `render_hand_facts`
- `_pick_size` / `_no_fold_equity` 等旧规则辅助

约束：

- `build_messages` 不调用它们；
- `advise` 不调用 `math_gate`；
- `Advice.to_dict` 不写 `math_gate`；
- 新测试只测它们作为离线工具，不测 live prompt。

### 6.5 replay / tools

- [x] `replay-console/replay/engine.py`：`value_directive` 审计字段改为“历史版本专用”；新 lean 不依赖。
- [x] `replay-console/replay/decisions.py`：`decision_facts` 的 `directive` 字段置空或标 `legacy`。
- [x] `replay-console/replay/schemas.py`：新任务只允许 `promptStyle = "lean"`；旧 `skills` 记录只读。
- [x] `replay-console/replay/versioning.py`：bump `CURRENT_ADVICE_VERSION`。
- [x] `tools/replay_live_decisions.py`：移除 `value_directive` 对照列，改用新 facts。
- [x] `tools/replay_hand_repeat.py`：移除 `value_directive` 输出，或标 legacy。
- [x] `tools/compare_review_hands.py`：非价值下注分类改用 `hand_strength`/`ground_truth`。
- [x] `tools/ai_advice_dev_hands.py`：`render_hand_facts` 改为新 facts，或明确打印 legacy。
- [x] `tools/build_sft_dataset.py`：删除 `--style`；默认新中文 lean。

### 6.6 data / docs

- [x] `data/sft/system.txt`、`train.json`、`eval.json` 删除或标记 stale；不得用旧 prompt 训练。
- [x] `config/llamafactory/sft_lora.yaml` 的“system 必须与 data/sft/system.txt 一致”改为新流程。
- [x] `README.md`：删除 `--advice-style`，更新 AI 建议章节。
- [x] `docs/live-stream.md`：更新提示词、选项、爆炸底池、`skill_codes` 描述。
- [x] `docs/复跑控制台-设计文档.md`：`prompt_style` 只保留 lean；旧 skills 为历史。
- [x] `docs/ground-truth-事实注入-实现说明.md`：删除 lean-rules/skills 注入说明。
- [x] `docs/实时牌力解析-接入AI建议-设计.md`：更新为中文 facts，不再提 `_LEAN_SYSTEM`。
- [x] `replay-console/README.md`：更新 A/B 工具说明。
- [x] 新增本文件 `docs/中文-lean-提示词-重构方案.md`。

---

## 7. 代码改动清单

### 7.1 `poker_ocr/advice.py`

| 改动 | 说明 |
|---|---|
| 新增 `SYSTEM_ZH` | 第 3.1 节文本 |
| 新增 `render_facts_zh(sit)` | 第 4 节事实块 |
| 新增 `render_hand_strength_zh(sit)` | 牌力/听牌/多人池中文 |
| 新增 `render_options_zh(sit)` | 中文选项标签 + 金额 |
| 新增 `render_ranges_zh(sit)` | `Hand vs ranges` 中文化；不直接用英文 `render()` |
| 新增 `render_gt_warnings_zh(gt)` | 牌面警告中文映射 |
| 新增 `all_in_option(sit)` | all-in 生成规则 |
| 删除旧 prompt 函数 | 第 6.1 节 |
| `build_messages` | 只返回 `(SYSTEM_ZH, render_facts_zh(sit))` |
| `advise` | 删除 style/gate/skills；`skill_codes=()` |
| `Advice` | 保留 `skill_codes` 空；`gate`/`plan` 不再写 |
| `build_situation` | 删除 style/block_draw_bets；新增 `include_advanced=False`，lean 默认不计算 GTO/board equity |
| `OPTION_ORDER` | 末尾加 `"all-in"` |

### 7.2 `poker_ocr/live.py`

- 删除 `--advice-style`、style 日志、style 传参。
- `session_start` 增加 `prompt_version`，删除 `advice_style`。
- `build_situation`/`AdviceRunner` 调用简化。
- `advice` 日志中的 `skill_codes` 不再显示。

### 7.3 `poker_ocr/clicker.py`

- `Option.kind == "allin"` 支持。
- 可选按钮 `allin`；缺省时 skip，不误点。
- `BUTTON_LABELS` 增加 `"allin": "全下"`。
- `tools/calibrate_clicker.py` 增加 all-in 标定。
- 旧配置无 `allin` 仍能启动；只是 all-in 建议不自动点。

### 7.4 `replay-console`

- `promptStyle` 新任务只允许 `lean`。
- 旧 `skills` job 只读；重跑返回明确错误。
- `decision_facts` 删除 `directive`，或标 `legacy_directive`。
- 新 `prompt_version` 字段；旧记录允许空。
- golden test 更新 hash/version。

### 7.5 `tests`

- [x] 删除 `test_advice.py` 中 lean-rules/skills 相关用例。
- [x] 新增中文 system 测试。
- [x] 新增听牌 facts 测试（无 `VERY SMALL`、无 `NOT THE NUTS` 否决）。
- [x] 新增多人池 facts 测试（8 人 / 4 建模 / 12.5%）。
- [x] 新增 SPR/位置/赔率门槛/爆炸底池/转河变化测试。
- [x] 新增选项标签/金额测试。
- [x] 新增 all-in 选项测试 + clicker skip 测试。
- [x] 更新 `test_live.py` 默认 style 断言。
- [x] 更新 `test_ground_truth.py`、`test_beat_rate.py`、`test_opponent_profile.py` 中旧 prompt 断言。
- [x] `tests/run_all.py` 增删测试清单。
- [x] `replay-console/tests/test_prompt_golden.py` 更新 hash。
- [x] `test_build_sft_dataset.py` 删除 style 参数。

---

## 8. 兼容与迁移

### 8.1 JSONL

新 `advice` 记录：

```json
{
  "type": "advice",
  "hand_id": "h1",
  "street": "flop",
  "option": {"id": "call", "label": "跟注 4BB", "kind": "call", "amount_bb": 4.0},
  "reason": "强听牌，9 张同花补牌，赔率足够跟注。",
  "source": "model",
  "model": "deepseek-flash",
  "prompt_version": "lean-zh-2026-10-29.1",
  "skill_codes": [],
  "duration_ms": 1000
}
```

- 旧字段 `math_gate` / `gate_violation` / `plan` / `advice_style` 不再写；旧日志只读兼容。
- `skill_codes` 保留空数组一版，避免旧消费者报错。
- `prompt_version` 新增，便于复盘归因。

### 8.2 `session_start`

```json
{
  "prompt_version": "lean-zh-2026-10-29.1",
  "prompt_lang": "zh",
  ...
}
```

删除 `advice_style`；旧日志中的 `advice_style` 保留原样。

### 8.3 旧 replay job

- `prompt_style=skills` / `lean-rules` 的旧 job：**只读**，不能重跑。
- 新 job：`prompt_style=lean`，`prompt_version=lean-zh-...`。
- 如果业务必须重跑旧 job，另建 `replay-console/replay/legacy_prompts.py` 冻结旧 prompt；本次不做。

### 8.4 微调

- 旧 `data/sft` 的 system 是旧英文 prompt，标签是 deepseek-flash 决策；**不得直接用于新模型**。
- 新 prompt 稳定后，用 `build_messages` 重新生成 dataset；标签至少混入人工/求解器，不要只学模型自己。

---

## 9. 测试与验收

### 9.1 自动测试

- `tests/run_all.py` 全绿。
- `replay-console` 测试全绿。
- `rg "skills|lean-rules|Coach Skill Catalog|Value directive|Math gate" poker_ocr/advice.py poker_ocr/live.py` 只允许出现在 legacy offline 注释/函数中，不允许出现在 `build_messages` 路径。
- `rg "VERY SMALL"` 在中文 facts 中不得作为听牌标签。
- `rg "5-way avg"` 不得由 8 人池产生。

### 9.2 回归用例

| 用例 | 断言 |
|---|---|
| 强听牌（AhQh / 9h6h5s / 面对 4BB） | 出现“强听牌/听牌”，不出现 `VERY SMALL`；不出现 `NUT STATUS` 单独否决；`call` 选项存在 |
| 8 人 bomb pot（KK / Ad10d6h） | 出现“实际 8 人 / 建模 4 人 / 12.5%”；出现“爆炸底池：是” |
| 3-bet 池 AQs | 出现“翻前形态：3-bet”；不出现 `Chart says RAISE` |
| 非坚果同花（Qh9h / Kh7h2h） | 标“非坚果”；不出现 `VALUE DEFAULT ... RAISE` |
| h75（22 / 88T） | 标“当前成牌落后/两对排名”；不出现“价值默认 BET” |
| h100（44 / QJ266） | 标“非坚果/公对面”；不出现“价值默认 BET” |
| h104（坚果花听 vs 全下） | 出现赔率门槛/权益；不出现“有赔率却弃牌” |
| all-in 选项 | 深码/短码场景出现；金额 = 后手；无 `allin` 按钮时 click skip |

### 9.3 人工验收

- 用 20–30 个历史决策点跑新 prompt，检查：
  - 中文可读；
  - 不再出现“引用不存在的规则”；
  - 强听牌不再被“当前未成牌”压死；
  - 多人池不再低估风险；
  - 选项标签与实际金额一致。
- 不把“单次输赢”当验收标准；看决策是否符合事实。

---

## 10. 实施顺序

建议拆 4 个提交，降低回归风险：

### Commit 1：中文 lean + 事实层

- 新增 `SYSTEM_ZH`、`render_facts_zh`、`render_hand_strength_zh`、`render_options_zh`。
- 修听牌标签、多人池人数、SPR/位置/赔率门槛/爆炸底池/转河变化。
- 保留旧 prompt 代码但不再被 `build_messages` 调用。
- 新增中文 prompt 测试。

### Commit 2：all-in 选项 + 点击器

- `Option(kind="allin")`、`OPTION_ORDER`。
- `clicker` 可选 `allin` 按钮。
- 测试；旧配置不误点。

### Commit 3：删除旧 prompt 与 skills

- 删除 `lean-rules`/`skills` 入口、`skills.py`、`skills_md*`。
- 删除 `--advice-style`、session_start `advice_style`。
- 更新 replay/tools/tests/docs。
- 旧 replay job 只读。

### Commit 4：文档与 SFT

- 更新 README/docs。
- `data/sft` 标记 stale 或删除。
- 更新 golden/version。

---

## 11. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| 中文 prompt 对旧模型不友好 | 决策质量下降 | 当前统一走 OpenAI 兼容 chat；模型切换用 `.env`，不再按模型名切协议 |
| `max_tokens=160` 截断中文 reason | JSON 不完整 → fallback | 中文 reason 限 30 字；必要时把 `AI_SERVER_MAX_TOKENS` 提到 220 |
| 删除旧 prompt 导致旧 replay 不能重跑 | 历史任务不可复现 | 旧 job 只读；如必须重跑，另存 frozen legacy |
| all-in 没有点击器按钮 | 建议显示但无法自动点 | 可选 `allin` 标定；缺省 skip，不误点 |
| 中文标签影响日志/消费者 | 下游解析失败 | 只改 `label`/`reason` 文本，`id`/`amount_bb`/JSON key 不变；更新文档 |
| 翻前范围一行仍可能误导 | 面对加注误用 RFI | 只在 `unopened`/`limped` 渲染；面对加注不渲染；system 明确“范围结论只是默认” |
| 旧 `value_directive` 仍在代码里 | 误被调用 | 注释 `legacy offline only`；`build_messages`/`advise` 不引用；测试覆盖 |
| SFT 数据旧 prompt | 训练/推理不同构 | 删除/重建；不要用旧 `system.txt` |
| 模型仍编造规则 | reason 不可信 | system 明确禁止；新 facts 提供足够依据；复盘看事实不看理由 |

---

## 12. 变更前后对照

### 12.1 听牌

**现在（问题）**

```text
Hand-strength read: VERY SMALL Strong draw (9 outs); behind now 92%, equity 49% ...
NUT STATUS: NOT THE NUTS — better hands exist: High card (12), One pair (396), ...
```

**重构后**

```text
牌力：强听牌（9 outs）
当前牌型：A 高（未成牌）
听牌：坚果同花听牌（h，9 outs）；四二法则权益 36%
对范围权益：49%（按河牌，2 人池）
当前成牌落后至少一家：91.9%（当前未成牌，不代表最终胜率）
赔率门槛：27.6%
```

### 12.2 多人池

**现在（问题）**

```text
Hand-strength read: ... equity 42% vs 5-way avg 20%
```

**重构后**

```text
多人池：实际 8 人 / 范围建模 4 人 / 公平份额 12.5%
对范围权益：42%（按建模 4 人；其余对手未建模）
```

### 12.3 选项

**现在（问题）**

```text
- raise-1/3 — Raise to 1/3 pot, amount 8.83BB
```

**重构后**

```text
- raise-1/3 — 加注到 8.83BB（1/3 池加注）
```

---

## 13. 一句话

**这次不是给模型加更多规则，而是把“算好的事实”用中文完整地交给它，同时删掉两套会互相打架的旧 prompt。** 默认只保留一套中文 `lean`：事实优先、决策框架简洁、选项金额明确、all-in 可选；旧 lean 和 skills 从 live 链路彻底下线。
