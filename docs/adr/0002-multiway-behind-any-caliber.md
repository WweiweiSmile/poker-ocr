---
status: accepted
date: 2026-11-19
---

# 多人池「至少一家领先」的口径取舍

> 关联：父规格 #74；量化 #75（`docs/至少一家领先-双口径偏差-2026-11-19.md`）；
> 单一实现与 basis #76（`docs/equity-与牌力分解口径-v1.md` §8）；
> 冷跟按人数分桶 #77（草案）/ #78（已落地）。本文只定口径，不重复 #75 的量化表。

## Context

现场基本是 3–5 人池。「至少一家领先我」要回答的是：在**还在池中的 N 个对手**
里，至少有一家的**当前成手牌**严格大于我的概率。它不是「我输牌的概率」，也
不是「我的权益」；它只描述当前成手牌的相对位置。

这个事实在仓库里对应两套完全不同的输入：

1. **有对手范围**：每个对手的翻前先验 × 翻后动作事件收窄后的范围；要算
   「至少一家」，必须把 N 家的手牌**整组不放回**地抽出来，再逐组判断。
   这就是联合拒绝重抽，输出 0..1 的比例，生产落点是
   `poker_ocr/hand_vs_ranges.py` 的 `behind_any`。
2. **没有对手范围**：只能把对手当均匀随机牌；单挑被压比例 p1 可以精确枚举，
   多人时用闭式 `1-(1-p1)^n` 近似。函数返回 0..1，`situation` 转成 0..100
   后进 facts，生产落点是 `poker_ocr/coach.py:382 at_least_one_behind`，
   只喂提示词兜底与复盘留痕。

两个数不是同一个问题的两种精度，而是**两个问题**：一个是对建模范围的联合
估计，一个是对随机牌的下界。量纲、对手模型、语义都不同，所以不能合并成一个
数，也不能互相替换。

## Decision

- **主路**：`sit.hand_ranges is not None` 时，用 `hand_vs_ranges.lookup(sit)`
  的联合拒绝重抽。对手范围由 `poker_ocr/field_ranges.py` 解析角色/先验、
  `poker_ocr/ranges.py` 加载命名/规则先验、`poker_ocr/range_flow.py` 按翻后
  动作顺序收窄；`_monte_carlo` 整组重抽、不放回，输出 `behind_any`（0..1）
  与 `behind_i`。没有建模的对手按随机牌补位并计入 `random_opponents`，
  不静默当成已建模。
- **兜底**：`hand_ranges is None` 时，提示词只渲染
  `coach.at_least_one_behind` 的随机牌独立近似，并**显式标注**「按随机牌下界」
  （`poker_ocr/prompt/user/strength.py:163-171`）；没有数字可给时宁可不写。
- **闭式只有一处实现**：`coach.at_least_one_behind(p, n)`（`poker_ocr/coach.py:382`）。
  `rank_hands`（`poker_ocr/coach.py:413`）和 `situation` 的兜底分支
  （`poker_ocr/situation.py:456`）都调它；不变量单测
  `tests/test_behind_any_single_source.py`。不要在调用点再写一遍 `1-(1-p)**n`。
- **评分侧自证算法**：复盘 facts 给两个数各带 basis：
  `beatAtLeastBasis = "random_hands_independent_approx"`、
  `behindAnyBasis = "joint_resample_over_ranges"`
  （`replay-console/replay/decisions.py:837/846`）；控制台在
  `beatAtLeastPct` 后标「（近似）」
  （`replay-console/replay_web/src/pages/HandDetailPage.tsx:68`）。
  目前没有任何评分代码读这两个数；`hand_strength._BEHIND_DEMOTE = 0.35`
  （`poker_ocr/hand_strength.py:45`）读的是 `behind_any`，不是独立近似。
- **提示词模板不为数字来源而改**：票 #76 只加了 basis 与 UI 标注，提示词文本
  未变、`PROMPT_VERSION` / `CURRENT_ADVICE_VERSION` 未 bump，#76 那次闭式收敛
  的基线位移为 0.00pp。口径 provenance 由 facts 字段表达，不由模板文案表达。
  后续 #78 的冷跟分桶按提示词铁律另行 bump（`lean-zh-2026-11-19.1`，评审修复
  后 `lean-zh-2026-11-19.2`），与 #76 的「不 bump」不是同一件事。

## Considered Options

- **把独立近似 `1-(1-p)^n` 当主算法**：否决。它把 N 家当成独立同分布、
  有放回的组合，而真实发牌是同一副牌不放回；「至少一家」不是独立事件的可加
  组合。票 #75 在同一随机牌对手模型下测得独立近似系统性偏低（独立 − 联合，
  3/4/5 人池均值 −0.42 / −0.66 / −0.84pp，最坏 −1.66 / −2.18 / −2.51pp，
  4× 复核后的最坏 |差| 0.99 / 1.77 / 2.31pp，人数越多偏得越多）。更关键的是
  它同时丢掉对手范围：票 #75 口径 B 的独立/随机 vs 联合/范围均值差 −4.58pp，
  范围收窄才是大头。把它当主算法还会把量纲从 0..1 变成 0..100、语义从
  「联合估计」降成「随机牌下界」，等于把下界冒充成对手范围估计。
- **用 solver / GTO 表替代**：否决。`gto/` 已于 2026-11-05 整体移除；历史实现
  也硬门槛 HU（`opponents_in_hand != 1 → None`），输出 range mix / 单手 EV，
  不生产 3–5 人范围，也不回答「我领先什么牌」；N 人均衡解不是这个目标，
  松被动池也不做频率调整。重开 solver 等于换问题。
- **把两套数相加或互相替换**：否决。量纲不同（0..1 比例 vs 0..100 百分数），
  对手模型不同（范围 vs 随机牌），语义不同（联合估计 vs 下界）。相加没有
  意义；替换会同时改提示词与复盘两条链路的输入口径。票 #76 的 §8 已把这条
  写成字段级规则，本文只引用、不重复。
- **评分侧继续静默使用近似值**：否决。票 #76 加 basis 的理由就是让读的人
  能分辨算法；没有 basis 时，`beatAtLeastPct` 看起来像「对范围」的数，实际是
  随机牌独立近似。评分当前不消费它，但未来任何数值消费者都必须先看 basis；
  静默使用会把口径错误带进判定。
- **兜底分支也跑 MC**：否决/暂缓。兜底的前提就是没有范围；对随机牌跑联合
  重抽只能修正「独立 vs 不放回」的几 pp（票 #75 口径 A），收益上界有限，
  而成本与线上延迟**未评估，需重开时评估**。在拿到兜底路径的实测成本/收益
  前，不为它加 MC。

## 何时重开

以下任一条成立，就重开本 ADR，而不是直接改数值：

| 触发条件 | 该做什么 |
|---|---|
| 3/4/5 人池任一池 \|均值差\| ≥ 5pp，或最坏 \|差\| ≥ 10pp（票 #75 的 `CRITERION_MEAN_PP` / `CRITERION_WORST_PP`） | 重跑 `tools/behind_any_caliber_report.py`，确认样本与复核；再决定是否把联合重抽推广到兜底或改阈值 |
| 差值或主路数值跨过下游判定线：`hand_strength._BEHIND_DEMOTE = 0.35`，或新增任何读 `beatAtLeastPct` 的数值消费者/阈值 | 先定该消费者该读哪个口径；跨线前不得上线 |
| 6–9 人池决策点占比上升，或现场常驻 ≥6 人池 | 扩样本重跑票 #75（当前 11 点、均值 −2.01pp、最坏 −5.50pp，4× 复核最大 \|差\| 5.18pp），单独校准大池 |
| 范围模型换版：`ranges.RANGE_VERSION` 变更、先验表/事件模型/profile（v0→v1）变更、新增数据源 | 重跑票 #75 并重设基线；主路数值会随范围变化，旧结论不再可比 |
| 票 #77 的冷跟按人数分桶（#78 已落地：`poker_ocr/tables/cold_call_buckets.json` + `poker_ocr/cold_call_tiers.py`，2/3/4 人池三档） | 已完成接入；先验分布已变，按本条重跑票 #75，重看主路/兜底覆盖率与差值 |
| 提示词或评分新增「至少一家」的数值用法（阈值、排序、EV 输入） | 先重开本 ADR，明确口径与 basis；不得直接拿现有字段当判定输入 |

## Consequences

- 读这两个数的人先看字段名与 basis：`handStrength.behindAny`（0..1，联合
  重抽，`joint_resample_over_ranges`）和 `beatAtLeastPct`（0..100，随机牌
  独立近似，`random_hands_independent_approx`）；UI 在后者后标「（近似）」。
- 不能相加、平均、互相替换，也不能拿 `beatAtLeastPct` 当「对对手范围的
  领先比例」或任何数值判定线；`_BEHIND_DEMOTE` 只读 `behind_any`。
- 没有范围时，提示词会明说「按随机牌下界」；复盘旧记录没有 basis 字段时，
  按同一口径读（`beatAtLeastPct` 仍是独立近似），不要按新口径反推。
- 主路数值会随范围先验/事件模型/profile 变化；兜底数值只随闭式实现与人数
  变化。改范围先验不是「改提示词」，要按上面的触发条件重跑量化。
- 新消费者必须持久化 basis，并在代码/文档里链接本 ADR；否则下一次口径
  变更无法判断历史记录属于哪套算法。
