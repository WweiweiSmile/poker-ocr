# 摊牌 witness 数据契约 v1

- 状态：草案，2026-11-14
- 对应：地图 #10 / 票 #18「witness 数据契约：一条证据怎么存」（契约）、#15（接入）、#16（验收）
- 上游决议：#12 族与局面、#19 准入、#17 句式。本文件只定**数据契约**，不重复族判定式、准入谓词与文案模板。
- 适用范围：poker_ocr/witness/ 投影层、profiles.json v2 的 witness 节、render_profiles_zh 的消费侧。
- 本图边界：只出 spec 与证据，不改产品代码。

## 0. 结论

witness 是**从不可变手牌文件重算出来的、有界的事件投影**，不是第二份档案：

- 一行 = 一个对手在一手牌、一条街、一个位置、一个行动下亮出的一个族。
- 只存渲染/审计需要的字段；完整动作序列用 **hand** 回查手牌 JSON，不复制。
- 只存 **admitted** 与 **tagged**；rejected 由纯函数重算 + 可选审计导出，不进缓存。
- 去重键是 **(hand, seat)**；hand 是规范手键（date-digest 优先），不是裸 uid。
- 窗口 = 最近 **5000** 手，与 seen_hands 同源；N 是窗口内计数。
- profiles.json 顶层 **version=2** + 顶层 **witness**；witness.version 或 classifier_version 失配 → 跳过渲染并提示重建。
- 渲染接入（#15 已定，见 §14）：并进逐座位画像的「摊牌证据：」行，事实优先级 3a，全场新增行 ≤600 字符。
- 验收（#16 已定，见 §15）：单测 + prompt golden 长期跑；A/B 用 WITNESS_PROMPT 开关看超额翻转率；人工 30 点由本人签字。
- 实现尚未开始：2026-11-14 时 `poker_ocr/witness/` 不存在，§14/§15 都是口径，执行在实现票之后。

## 1. 范围

### 1.1 目标

1. 固定一条证据的字段、类型、枚举、去重键、窗口、版本与身份语义。
2. 消费侧只读 witness 就能渲染 #17 的句子，不需要回扫 970 个手牌文件。
3. 任何一行都能用 hand 回查源文件，并用 witness.version + classifier_version 判断是否可采信。
4. 重建幂等：同一批手牌重跑，行集合与顺序稳定（除 updated_at）。

### 1.2 非目标

- 不做族判定式（#12）、不做准入谓词（#19）、不做句式/词表（#17/#15）。
- 不做 range 校准乘数更新、不做 LLM 标签、不做 replay-console/MySQL、不做翻前 169、不做翻牌格。
- 不改 poker_ocr/opponent_profile.py 的 _showdown 旧统计；witness 是消费侧独立投影。
- 本图不写实现代码；本文件是给实现票的契约。

## 2. 事实源与派生关系

    手牌 JSON（data/hand_outbox/sent|pending，不可变、权威）
            │  admit(hand, seat)  纯函数（#19）
            ▼
    witness.rows（本契约）
            │  按 player / family / spot 聚合
            ▼
    render_profiles_zh（只读、零 token）

- 权威事实只存在于手牌 JSON；profiles.json 是可删除、可重建的缓存。
- witness 行是投影，不是原始记录；底池、筹码、完整动作序列都留在手牌文件。
- admit() 必须是纯函数：同样的 hand + seat + 代码版本 → 同样的 Admission；重建不依赖历史随机性。
- rejected 不进入 witness.rows；它由 admit() 重算，审计时导出（见 §4.3）。#19 的「留档不丢」落为「手牌不可变 + 可重算 + 可导出」，而不是「缓存里永久留一行」。

## 3. profiles.json v2 形状

### 3.1 顶层

    {
      "version": 2,
      "pool_key": "0.5/1-9max",
      "classifier_version": "2026-...",
      "updated_at": "2026-11-14T...",
      "players": { "...": { "...": "..." } },
      "aliases": {},
      "seat_keys": {},
      "seen_hands": ["..."],
      "witness": {
        "version": "witness-2026-11-14a",
        "window_hands": 5000,
        "rows": [ ... ]
      }
    }

- players / aliases / seat_keys / seen_hands 形状不变；players[key] **不新增** witness 字段（避免玩家条目膨胀与全局裁剪分散）。
- version 从 1 → 2；旧 reader 忽略未知的 witness，新 reader 读到 v1 视为空 witness 并走迁移。
- witness 缺失或 rows 为空 = 没有可用证据，不是错误；渲染层退化为「不出 witness 行」。

### 3.2 行字段

| 字段 | 类型 | 必填 | 示例 | 语义 |
|---|---|---|---|---|
| player | string | 是 | "书麻头了wlr" | normalize_name 后的玩家键，与 players 的 key 同源；alias 合并时整行改指 |
| hand | string | 是 | "20261030-1e5ec30022" | 规范手键，见 §3.4；等于 date-digest 时的 hand_id；用于回查与去重 |
| seat | string | 是 | "upL" | 该手座位 id；与 hand 组成去重键 |
| ts | string | 是 | "2026-10-30T22:14:08.514+08:00" | 手牌时间，ISO8601 带时区，取 hand.ts |
| street | enum | 是 | "river" | turn / river；翻牌、翻前不进 v1 行 |
| action | enum | 是 | "check" | facing_bet / check / bet / unknown |
| position | enum | 是 | "ip" | ip / oop，相对 hero 的翻后行动顺序 |
| cards | string[] | 是 | ["7s","Ad"] | 亮出的牌，长度 1 或 2；admitted 必须 2，single 为 1 |
| board | string[] | 是 | ["6c","2h","As","5c","9d"] | 摊牌时公共牌，长度 3–5；用于族判定与审计 |
| family | enum/null | 是 | "weak_ace_top_pair" | 12 族之一，见 §3.3；single 为 null；派生缓存 |
| show_kind | enum | 是 | "showdown" | showdown / voluntary / single |
| status | enum | 是 | "admitted" | admitted / tagged；只有这两种进 rows |
| winner | object | 是 | {"status":"unknown","seat":null} | status: derived / unknown / single_show；seat: string/null；只作证据行旁证 |
| source | enum | 是 | "outbox" | outbox / live；同一手以 outbox 为准 |
| table | string | 是 | "20261030-67723049fc5c" | table_signature；缺失时写空串；用于审计与人工拆分同名 |

字段取舍（对应票面问题）：

- **局面键不单独存**：spot 由 street|action|position 拼成，避免两份真相。
- **动作序列不存**：只存该 spot 的规范 action；完整序列用 hand 回查手牌 JSON。
- **不存 pool_key**：一个 profiles 文件对应一个池；跨池合并是 §13 的未决项。
- **不存 classifier_version**：顶层统一；失配整体重建。
- **不存 display_name**：players 已有；行只存 key。
- **不存 hand_id 与 uid 两份**：hand 是规范键；旧式 sNN-hN 的 hand 就是 uid。

牌面与牌字符串保持手牌记录原样（如 "10s" 不写成 "Ts"）；大小写与花色按现有数据，比较时用统一规范化函数。

### 3.3 枚举

**family（12 个稳定 id；中文名只作展示，判定式在 #12）**

| id | 中文 | 渲染组 |
|---|---|---|
| weak_ace_top_pair | 弱A顶对 | 弱A顶对 |
| strong_ace_top_pair | A顶对强踢 | 强顶对 |
| non_ace_top_pair | 非A顶对 | 强顶对 |
| overpair | 超对 | 对子中坚 |
| middle_pair | 中等对子 | 对子中坚 |
| small_pair | 小对子 | 小对与底对 |
| air_high_card | 高张无对 | 听牌破产与高张 |
| suited_connector | 同花连张 | 听牌破产与高张 |
| busted_draw | 听牌破产 | 听牌破产与高张 |
| two_pair_plus | 两对+ | 两对+ |
| set | 暗三 | 两对+ |
| playing_board | 打牌面 | 小对与底对 |

- family 不分街：按摊牌时最终牌型判定（#12）。
- 169 类别只在证据行露具体牌，不进 family。

**street**：turn / river。翻牌、翻前不生成 v1 行。

**action**：facing_bet / check / bet / unknown。

由最后街该座位的最后动作映射：

| 源动作 | action |
|---|---|
| bet | bet |
| raise | facing_bet（面对下注后加注） |
| call | facing_bet |
| check | check |
| fold | 不生成行（admission 必拒） |
| 无动作 / 判不了 | unknown |

unknown 单独成格，不并进 check（#12）。

**position**：ip / oop。相对 hero 的翻后行动顺序；hero 座位已知但已弃牌时仍可按座位顺序判断。hero 座位缺失则跳过该行并记审计。

**status**：admitted / tagged。

**show_kind**：showdown / voluntary / single。

**winner.status**：derived / unknown / single_show（#19）。

**source**：outbox / live。仅表示这一行来自哪种载体，不是渲染来源（本人/本池/原型是 §10 的 scope）。

**rejected 原因（只进审计导出，不进 rows）**：fold_inferred / not_in_street_active / board_short / hand_mismatch。

**tagged 原因（由 show_kind 表达，不单存）**：voluntary_show → show_kind=voluntary；single_card → show_kind=single。

### 3.4 规范 hand 键

    规范键按以下顺序取第一个命中：
    1. uid 尾部是 date-digest（形如 20261030-1e5ec30022）→ 取该 date-digest；
    2. hand_id 本身是 date-digest → 取 hand_id；
    3. uid 非空 → 取 uid；
    4. 否则 → s{session:02d}-{hand_id}。

- 为什么：同一手可能同时出现为 20261101-228c7832f9 与 state:live.states.jsonl:s15-20261101-228c7832f9；只看裸 uid 会双计或漏计。
- 现状证据：profiles.json 的 seen_hands 共 1157 条，其中 488 条带 state: 前缀；这 488 条里 298 条尾部带 date-digest，其中 293 条与 outbox 的 hand_id 重合。
- 旧式 state:...:s03-h41 没有 date-digest，规范键保持 uid 原样；不同 session 的 hN 不会互相合并。
- outbox 文件名就是 date-digest；同一手以 outbox 为准。
- 迁移时 seen_hands 也要规范化为规范键并去重（见 §8）。

### 3.5 局面键

    spot = street + "|" + action + "|" + position
    例：river|check|ip

- 2 街 × 4 行动 × 2 位置 = 16 格（#17 把主动下注升为独立 action 后）。
- spot 不落盘，渲染时由三个字段拼；任何聚合都必须用同一拼接函数。
- 翻牌 / 翻前不进 v1 的 16 格。

## 4. 准入与三态

### 4.1 Admission 输出

    admit(hand, seat_id) -> Admission(
        status: admitted | tagged | rejected,
        reason: str | None,
        show_kind: showdown | voluntary | single | None,
        cards: tuple[str, ...] | None,
        winner: WinnerEvidence | None)

- 谓词、否决顺序、具体判定式属于 #19；本契约只消费输出。
- 证据侧 fail closed：判不了 ⇒ 不进 rows。主链路 fail open：不抛异常、不阻塞 OCR/手牌记录/旧画像统计。

### 4.2 入库映射

| status | show_kind | cards | family | 进 rows |
|---|---|---|---|---|
| admitted | showdown | 2 | 非 null | 是 |
| tagged | voluntary | 2 | 非 null | 是 |
| tagged | single | 1 | null | 是 |
| rejected | null/任意 | 任意 | 任意 | 否 |

- admitted 是「摊牌口径」；tagged voluntary 是「主动秀」，不进摊牌频率，但保留给将来的「见过他有这手牌」措辞。
- tagged single 只留档给范围排除，首批不渲染。

### 4.3 rejected 的去处

- 不进 witness.rows。
- 审计导出：data/opponents/witness_rejected.jsonl（可选、可删、可重建），每行包含 hand / seat / player / ts / street / action / position / cards / board / reason / show_kind。
- 导出由重建或 audit 命令生成；live 不写、不读。
- 这样 #19 的「rejected 留档不丢」由「手牌不可变 + admit() 纯函数 + 可导出」保证；不再占用 profiles 的渲染缓存。

### 4.4 投影过滤

- 只投影最后街为 turn / river 的手；翻牌、翻前亮牌在进入 admit 前跳过，记审计计数，不算 rejected。
- 只投影非 hero 座位；hero 的亮牌走 hero_cards，不进对手 witness。
- 同一手同一座位最多一行；同一手多个对手各一行。

## 5. 去重与替换

- 去重键：**(hand, seat)**。
- 处理一手时按「整手替换」：先删除 rows 中 hand 等于该规范键的所有行，再插入该手当前 admit 产生的行。这样 OCR 修正、状态翻转（admitted → rejected）不会残留旧行，也不会双计。
- seen_hands 仍是快速跳过：规范键已在 seen_hands 时 update_hand 直接返回（现有行为）。因此同一键的内容修正需要显式 rebuild / 清 seen 后重跑；契约要求 rebuild 幂等。
- 计数由 rows 聚合，不单独存 counters；替换后计数自动一致。
- 跨来源：outbox 优先。live 先写入的行，在 outbox 同 hand 到达时整行替换，source 变为 outbox。
- 不用内容哈希：OCR 抖动会造成漏去重；规范 hand 键才是稳定身份。

## 6. 窗口与遗忘

- window_hands = 5000，与 MAX_SEEN_HANDS 对齐。
- seen_hands 是有序列表，超过 5000 丢最旧（poker_ocr/opponent_profile.py:2155-2159）；witness 窗口 = seen_hands 保留的那批手。
- 保存时裁剪：rows 只保留 hand 在 seen_hands[-window_hands:] 里的行；再按 (ts, hand, seat) 排序，保证重建输出稳定。
- N 是**窗口内**计数，不是 lifetime。句子模板不变；在 render_profiles_zh 现有释义行（poker_ocr/prompt/user/profiles.py:48-51）追加一句「统计窗口 = 最近 5000 手」。
- 不做时间衰减、不做每人上限；#17 的每座位 ≤2 是渲染规则，不是存储规则。
- 体积估算：当前 970 手 → 616 条亮牌（约 453 admitted + 163 tagged）；5000 手约 3000 行，按 300 B/行约 0.8–1.1 MB；profiles 总计约 1.2 MB。每 10 手 / 5 秒节流的整文件写可接受。
- 若体积成为问题：调小 window_hands（契约常量），或把 rows 移 sidecar；v1 不引入每人 lifetime counters（两套数会漂移）。

## 7. 身份与 alias

- player = normalize_name 后的名字键，与 players 的 key 同源；名字全局合并，pool 不参与身份（现有行为）。
- aliases 合并时：把旧键所有行的 player 改成新键，再删旧键；行的 table / seat / ts 保留，供审计与人工拆分。
- seat_keys（session|seat → player）不参与行身份；行用 (hand, seat) 定位。
- 同名不同人：已知限制，不自动拆分；行带 table + ts，人工可查。按 table_signature 分桶会在 970 手 / 10 session 下把个人证据切碎，v1 不采用。
- temp: 键可能短暂出现在行里（亮牌时名字还没 OCR 回来）；alias 学会后整行改指；一直没恢复的 temp 行不参与具名玩家渲染。

## 8. 版本与迁移

### 8.1 版本

- profiles.version = 2（顶层形状变化）。
- witness.version = "witness-2026-11-14a"；schema、枚举、family/spot 派生语义、默认窗口变更时 bump。
- 不新增独立的 WITNESS_VERSION 顶层字段/版本层；witness 模块内用一个 WITNESS_SCHEMA 字符串与落盘值比对。
- classifier_version 留在顶层；它变或 witness.version 失配 → 视为 witness 不可采信：跳过 witness 渲染（fail open），日志提示重建，不抛异常、不阻塞 live。
- LABEL_VERSION 不引入（labels 未实现，见 §9）。

### 8.2 迁移步骤（建议）

1. 备份 data/opponents/profiles.json → profiles.json.v1.bak。
2. 枚举 data/hand_outbox/sent 与 pending 的 *.json，跳过 discarded；按规范 hand 键去重（sent 优先），按 ts 排序。
3. 推荐做一次**全量重建**：重置内存 store，逐手 update_hand，生成 players/seen_hands/witness。理由：当前 seen_hands 1157 与 970 个手牌文件不一致（306 个 sent 手不在 seen；293 条 live date-digest 与 outbox 重合），统计本身已有双计/漏计风险；全量重建让三者一致。
4. 重建前先出 diff 报告：players 数、每人 hands、VPIP/PFR、被丢弃的 live-only 键（如 s03-h41 这类无 outbox 文件的手）、witness 行数。人工确认后再替换；不要静默改写统计。
5. 若业务要求统计零变化，可退而做 witness-only 重建：保留 players/aliases/seat_keys，只规范化 seen_hands 并重建 rows；代价是 306 个未计入统计的手仍缺统计，witness 行可能引用 players 里没有的玩家（这类行跳过并记审计）。
6. 写 v2 原子落盘（沿用 save 的 tmp + os.replace）；旧 v1 保留备份。
7. 重建后校验 §12 的不变式，并跑两遍对比 rows 完全一致。

### 8.3 迁移期间的行为

- 读到 v1：witness 为空，渲染不出 witness 行；不影响现有画像段。
- 读到损坏 JSON / 未知 version：load_error 照旧；witness 视为空；live 继续。
- 读到 v2 但 witness.version 未知：跳过 witness，保留 profiles 其他部分。

## 9. 与 Label 的关系

- v1 **不生成** Label，不写 players[key].labels，不写 label_version。
- Label = (spot, direction, evidence, counter)，min_opps=8，有 gate 与冲突消解（docs/对手打法标签与剥削-设计.md §5.1 / §7.3）；witness 是**事件证据**，不是标签。
- 将来 Label 的 evidence / counter 可以读 witness 聚合，但必须自己过 gate 与 min_opps，不能直接把 witness N 当标签命中数。
- labels 落地时按原设计加 players[key].labels 与顶层 label_version；witness 仍独立，不塞进 labels 数组。

## 10. 消费侧契约

### 10.1 计数

- N(player, family, spot) = rows 中 player / family / spot 匹配且 status=admitted 的行数。
- tagged 不计入 N，也不进 #17 的「亮过 N 次」句；主动秀的措辞与降权是 §13 未决项。
- 池级：N_pool(family, street) = 所有 admitted 行中 family / street 匹配的行数；#17 的池级句用 family×street（不是 spot），n≥20 才出比例线。
- 原型级需要 archetype，v1 不实现；属 §13。

### 10.2 渲染

- 个人有 admitted 行：按 #17 句式出「他在{街}{位置} {行动}时亮过{族}（{N} 次）」；N<3 只当事例读。
- 个人无、池级 n≥20：出「这个池子在{街}的亮牌里{族}占 X%（{n} 次，本池）」。
- 个人无、池级不足、原型层 n≥20：出「这类玩家（{原型}）在{街}常亮{族}（{n} 次，{原型}类）」；v1 未实现。
- 三层都不到：不出行。
- 每座位 ≤2 条；优先级：同 spot → 最近 → 族不重复（#17）。
- 与「弃牌率高」并列，不加因果连接词，先频率后事例；词表与模板在 poker_ocr/tables/witness_phrasing.json（#15 已定结构，见 §14.6），改文案走 AGENTS.md 三步走。

### 10.3 失败语义

- 单行字段非法：跳过该行，记一次审计；不抛异常。
- 全部行非法 / witness 缺失 / 版本失配：不出 witness 行；现有画像段照常渲染。
- 渲染只读，不修改 rows，不触发 OCR，不调用 LLM（零 token）。
- 入口：poker_ocr/live.py:2083 _update_profiles 之后、poker_ocr/prompt/user/profiles.py:12 render_profiles_zh 之内；具体拼接位置见 §14.2（#15 已定）。

## 11. 字段对照表（profiles.json v1 → v2）

### 11.1 顶层

| v1 字段 | v2 字段 | 变化 |
|---|---|---|
| version | version | 1 → 2 |
| pool_key | pool_key | 不变 |
| classifier_version | classifier_version | 不变；失配使 witness 不可采信 |
| updated_at | updated_at | 不变 |
| players | players | 不变；不新增 witness 字段 |
| aliases | aliases | 不变 |
| seat_keys | seat_keys | 不变 |
| seen_hands | seen_hands | 值规范化为规范 hand 键并去重；仍是有序列表、上限 5000 |
| 无 | witness | 新增：version / window_hands / rows |

### 11.2 玩家条目

| v1 字段 | v2 | 说明 |
|---|---|---|
| display_name / pool_key / hands / sample_hands / VPIP / PFR / ... | 不变 | witness 不写玩家统计 |
| 无 | 不新增 | 证据在顶层 witness.rows，按 player 过滤 |

### 11.3 手牌源字段（只读，不新增 OCR）

| 用途 | 源字段 |
|---|---|
| 规范 hand 键 / 去重 | uid, hand_id, session |
| 时间排序 / 窗口 | ts |
| 身份 / 审计 | shows[].seat_id, shows[].name, table_signature, table_roster, seat_meta |
| 族判定 | shows[].cards, board, coach.evaluate（0–8） |
| 街 / 动作 / 位置 | streets[].street, streets[].actions[].seat_id/action, street_active, seat_meta.position |
| 摊牌 / 主动秀 / 单张 | shows[].cards 长度, street_active, 最后动作 |
| winner | shows / streets / pot（#19 的 WinnerEvidence） |

## 12. 不变式与验收

### 12.1 行级不变式

1. hand 是规范键；同一 (hand, seat) 只出现一次。
2. cards 长度：admitted 与 tagged/voluntary 为 2；tagged/single 为 1。
3. family：admitted 与 tagged/voluntary 非 null；tagged/single 为 null。
4. street ∈ {turn, river}；action ∈ 4 值；position ∈ 2 值；status ∈ {admitted, tagged}。
5. show_kind 与 status/cards 的映射符合 §4.2。
6. player 是 normalize_name 后的非空键；若 players 里没有该键，该行不参与渲染并记审计（witness-only 迁移的边界情况）。
7. table 允许空串；ts 非空。
8. rows 中没有 rejected。

### 12.2 存储级不变式

1. 所有 row.hand ∈ seen_hands；seen_hands 全是规范键且无重复。
2. 裁剪后 row 数 ≤ 窗口内手数 × 每手最多亮牌人数；无窗口外的行。
3. rows 按 (ts, hand, seat) 排序；重复重建输出一致。
4. witness.version 与代码 WITNESS_SCHEMA 一致；否则不渲染。
5. players / aliases / seat_keys / seen_hands 的旧字段语义不变；_showdown 统计不变。

### 12.3 测试

- 单元：规范 hand 键（plain date / state 前缀 date / 旧 sNN-hN / hand_id date / 缺失）；整手替换；窗口裁剪；alias 合并改指；非法行跳过。
- fixture：data/hand_outbox/sent/20261030-1e5ec30022.json 应产生 upL 的 admitted 行（family=weak_ace_top_pair，详见附录 A）；topL 也有一行，family 由 #12 判定式决定，测试不硬编码。
- 重建：同批手牌跑两遍，rows 完全一致；与手牌文件对账，admitted + tagged + rejected 覆盖所有亮牌。
- 渲染 golden：由 #15 的 witness_phrasing.json 锁；本契约只保证数据形状。
- 回归：跑 nv tests/run_all.py；不改变 _showdown 相关断言。

### 12.4 审计报告

重建/审计命令至少输出：

- 总手数、有亮牌手数、亮牌条数；
- admitted / tagged(voluntary) / tagged(single) / rejected(按 4 原因) 计数；
- 每族计数、每 spot 计数、N≥3 的玩家×族×spot 格数；
- 重复 (hand, seat) 数、窗口外行数、players 缺失的 player 数；
- 与上一版 profiles 的 diff（players/hands/VPIP）。

## 13. 未决与下一步

仍属 map 的 fog，不在本契约内拍板：

- 主动秀（tagged voluntary）的渲染措辞与降权；是否进「见过他有这手牌」、是否单独句式。
- 池级 / 原型级的精确聚合与来源标注；原型层依赖 archetype，v1 不实现。
- 跨 session / 跨盲注池合并；同名不同人的自动拆分。
- 「他亮过什么」看板（人工标注子集已由 #16 定为 30 点，见 §15.4）。
- 证据时间窗调参（5000 起步，按真实使用调）；时间衰减 v1 不做。
- range 校准乘数（out of scope，不改 range_flow）。
- #20 亮牌归属修复后，用 rejected 审计导出对账。
- 回放一致性：replay-console 用 advice.context 快照重建 prompt（replay-console/replay/engine.py:122），v1 不快照 witness 行 → 回放里看不到「摊牌证据：」。要不要把 witness_by_seat 随 context 落盘，留给后续票（本次只记下，不拍板）。

下一步：投影/存储交实现票落 poker_ocr/witness/admission.py、poker_ocr/witness/render.py；提示词接入按 §14 的 diff 清单做（先跑全量重建 diff，再改 live）；验收按 §15（单测与 golden 长期跑，A/B 与人工 30 点在实现完成后一次性过）。实现票：#38「实现 witness 投影与渲染」，按 §14.8 的 12 条 diff 与 §15.8 的 10 条清单做。

## 14. 提示词接入（#15 决议）

- 状态：已拍板 2026-11-14，票 #15「提示词落点与冲突优先级」；本节是 §10.3 里「具体拼接位置由 #15 决定」的答案。
- 只定**接入点、优先级、预算、文案与版本工序**；§3–§12 的数据形状不变，准入仍归 #19。

### 14.1 落点：并进「## 对手画像」，不新增子块

每座位块的行顺序（改动后）：

1. 类型行（n / VPIP / PFR / AF / 置信）
2. 「加注宽度：」
3. 「打法：」（render_tendencies_zh）
4. **「摊牌证据：」← 新增，插在 3 与 5 之间**
5. 「可剥削（程序建议）：」，或攻击块在场时退化的「可剥削证据：」

- 行模板沿用 #17，不改写：`摊牌证据：他在{街}{位置}{行动}时亮过{族}（{N} 次）`，一条一行，与「打法：」同缩进。
- **不加**「## 摊牌证据」独立段：拆段会让同一个座位在 prompt 里出现两处，也让「每座位 ≤2 条」失去统一的裁剪容器；模型读「这个人怎么打」时，摊牌证据就是同一座位画像里最具体的一档。
- **attack 段不独立提示**：render_attack_zh（poker_ocr/prompt/user/__init__.py:104）不新增「亮过什么」子块。攻击块将来要用同一数据，从同一 witness 聚合读，守住 poker_ocr/prompt/user/__init__.py:65-70 的「同一 prompt 只有一个能不能开火来源」。
- witness 行在 `suppress_action_hints=True`（攻击块在场或 trap）时**照常渲染**：它是既成事实，不是行动建议，不构成第二个开火来源。与 exploit 行不同——后者在攻击块在场时只留「可剥削证据：」。

### 14.2 调用链与拼接点（唯一）

    手牌 JSON ──重建──▶ witness.rows（profiles.json，§3）
                              │  render_witness_lines(rows, seats, hero_seat)  纯函数、零 IO
                              ▼
                        witness_by_seat: {seat_id: (行…)}
                              │  build_situation(witness_by_seat=…)
                              ▼
                        Situation.witness_by_seat
                              │  render_profiles_zh → render_reads_zh(witness_by_seat=…)
                              ▼
                    「## 对手画像」逐座位块，第 4 类行

- **聚合与截断在 witness/render.py，提示词层只拼接**：prompt 段不做窗口过滤、不做版本校验、不算 N。
- 拼接点只有一个：`render_reads_zh` 里「打法：」行之后、「可剥削」块之前（现 poker_ocr/opponent_profile.py:2732-2740 一带）。不放在 render_profiles_zh 里做「先渲染再插行」——那样得猜哪些行属于哪个座位，脆弱。
- `render_reads_zh` 的 `_showdown` 旧统计**不动**（§1.2）；witness 是独立投影，只是共用一个输出位置。

### 14.3 事实优先级：第 3 层拆 3a / 3b / 3c

   1   硬事实（牌 / 公共牌 / 底池 / 需跟 / 在局人数 / 后手）
   2   确定性计算（SPR、赔率、牌型、outs）
   3a  逐座位摊牌证据（witness 行：他真亮过什么）      ← 本层最具体
   3b  逐座位画像统计（类型、VPIP/PFR/AF、加注宽度、打法）
   3c  本桌统计 / 池子先验（池级亮牌比例、无条件 fold%）
   4   现场观察（.table_dy）
   5   通用经验

- 只有 3a 与 3b 同座位同一局面冲突时，3a 胜。
- witness **不抬进第 2 层**：它是统计，且摊牌样本偏强（能到摊牌的空气只有 8.7%，docs/摊牌反核-2026-11-05.md:72-73），不是「确定性计算」。
- witness **也不压 Hero 的 ground_truth**：一个是 Hero 的牌力，一个是别家的历史亮牌，不同轴；落在第 3 层已满足「不得压 1/2」。

### 14.4 预算与截断

- 600 **只算新增的 witness 行**：现有画像段与释义行的长度不动（今天 9 人桌整段画像实测 ≈1844 字符，整段 <600 已不可能；把预算下在新增部分才可执行）。
- 每行计 `len(行文本) + 1`（含行尾换行），「摊牌证据：」前缀计入。
- 每座位 ≤2 条（#17）；座位内选取顺序：同 spot → 最近 ts → 族不重复（#17 原文）。
- 跨座位按「Hero 下注后的行动顺序」由近到远填充（与 poker_ocr/prompt/user/profiles.py:19-33 的旋转顺序同源）；预算耗尽即停，更远座位不再出行 —— 即「离 Hero 越远越先丢」。
- 同座位内若第 2 条会超限，只出第 1 条；不会为了塞满预算去挑更远的短行。
- 硬上限与每座位 2 条冲突时，**硬上限优先**：预算不给就少出行，不换措辞、不缩写族名。

### 14.5 空数据与 fail open

- 该座位 0 条 admitted 行 → 不渲染任何行，**不写「暂无摊牌记录」之类占位**（占位会被模型读成「他没有摊牌」这一事实）。
- 整桌 0 条 / witness 缺失 / witness.version 或 classifier_version 失配 → 整个 prompt 不出现「摊牌证据：」字样，其余画像段逐字节不变。
- 「本手谁没有画像」仍只由 poker_ocr/prompt/user/profiles.py:55-57 的 missing 名单承担，措辞不变（「按未知对手处理，不伪造数据」）。
- 单行渲染异常（字段非法、族名缺失）→ 丢该行并 log 一次，不影响同座位其它行与其它座位。
- witness_by_seat 计算异常 → live 侧 try/except 退化为空 mapping（写法同 poker_ocr/live.py:1862-1869 读画像）；不阻塞 OCR、不阻塞手牌记录、不抛给主循环。

### 14.6 文案词表 poker_ocr/tables/witness_phrasing.json

    {
      "version": "witness-phrasing-2026-11-14a",
      "prefix": "摊牌证据：",
      "line": "他在{street}{position}{action}时亮过{family}（{count} 次）",
      "street":   {"turn": "转牌", "river": "河牌"},
      "position": {"ip": "IP", "oop": "OOP"},
      "action":   {"facing_bet": "面对下注", "check": "过牌", "bet": "主动下注", "unknown": "行动不明"},
      "family":   {"weak_ace_top_pair": "弱A顶对", "…": "12 族中文名见 §3.3"}
    }

- 只有一种句式；`unknown` 单独成格并如实写「行动不明」（§3.3 不并进 check）。
- 加载失败或字段缺失 → 用模块内同一份默认常量，log 一次；不允许无模板时静默出行。
- 改文案 = 改 JSON，仍走 AGENTS.md 三步走（这份 JSON 进 prompt 文本）。

### 14.7 版本与 golden 工序

1. poker_ocr/prompt/system/priority.py:6-12 改成 3a/3b/3c 并加「逐座位摊牌证据 > 逐座位画像」一句。
2. poker_ocr/prompt/version.py:83 由 `"lean-zh-2026-11-13.1"` 改为 `"lean-zh-2026-11-14.1"`，并在文件顶部 changelog 追加（照现有格式写清为什么改、依据哪次决议）：

       2026-11-14.1：事实优先级第 3 层拆 3a/3b/3c——逐座位摊牌证据最具体。
       依据 #10 地图的 #15 决议（docs/摊牌-witness-数据契约-v1.md §14）；
       本次只改 system，user 事实块在 witness 数据落地前逐字节不变。

3. replay-console/replay/versioning.py:81 的 CURRENT_ADVICE_VERSION 由 PROMPT_VERSION 自动派生，**不手改**。
4. replay-console/tests/test_prompt_golden.py:21-22：`EXPECTED_ADVICE_VERSION` → `"advice-2026-11-14.1"`；`EXPECTED_PROMPT_HASH` 重算（:53 的 digest = sha256(system + 分隔行 + user)，system 变了 hash 必变）。该快照无画像、无 witness，所以 user 侧应逐字节不变——hash 变了但 diff 只应出现在「事实优先级」段，用这一点自检没有误改 user。
5. 释义行改动（poker_ocr/prompt/user/profiles.py:48-51 追加「统计窗口=最近 5000 手」）只影响**有画像时**的 user 文本；golden 快照无画像，所以不影响 hash，但要有单测锁这一句只在有画像时出现。

### 14.8 逐段 diff 清单（给实现票）

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | poker_ocr/witness/render.py（新） | `render_witness_lines(rows, *, seats, hero_seat, max_chars=600) -> dict[str, tuple[str, ...]]`；纯函数、零 IO、零 token | 聚合/截断/窗口可单测，prompt 层只拼接 |
| 2 | poker_ocr/tables/witness_phrasing.json（新） | §14.6 的模板与词表 | 改文案不动代码 |
| 3 | poker_ocr/situation.py:117 附近 | Situation 新增 `witness_by_seat: Mapping[str, tuple[str, ...]] = ()` | 事实随 Situation 走，与 opponent_reads 同构 |
| 4 | poker_ocr/situation.py:305-311 | `build_situation(..., witness_by_seat=None)` 透传 | 单测/离线重放可显式注入 |
| 5 | poker_ocr/live.py:1886 | build_situation 调用前算 witness_by_seat（profile_store + point），try/except 吞异常 | 唯一的实时入口；失败退化为空 |
| 6 | poker_ocr/opponent_profile.py:2683 render_reads_zh | 新增 keyword `witness_by_seat=None`；在「打法：」之后、「可剥削」之前插行 | 拼接点唯一；默认 None 时既有调用方（offline/hand_facts.py:87、replay-console）输出不变；`_showdown` 不动 |
| 7 | poker_ocr/prompt/user/profiles.py:12,35-39 | 透传 `witness_by_seat=sit.witness_by_seat`；释义行 :48-51 追加「统计窗口=最近 5000 手」 | 调用方在 prompt 层 |
| 8 | poker_ocr/prompt/system/priority.py:9-12 | 第 3 层拆 3a/3b/3c + 让位句；末句改为「逐座位摊牌证据 > 逐座位画像 > 本桌 > 全场」 | 模型要知道冲突时以谁为准 |
| 9 | poker_ocr/prompt/version.py:83 + 顶部 | bump 到 lean-zh-2026-11-14.1 + changelog 三行 | AGENTS.md 三步走 |
| 10 | replay-console/tests/test_prompt_golden.py:21-22 | 更新两个常量（hash 重算） | 三步走第三步 |
| 11 | tests/test_witness_render.py（新） | golden：附录 A 单行；空 rows → `{}`；9 人×2 条 → 总字符 ≤600；版本失配 → `{}`；截断按「远先丢 / N 小先丢」 | 锁预算与顺序 |
| 12 | tests/test_opponent_profile.py:1082 附近 | 断言「摊牌证据：」在「打法：」之后、「可剥削」之前；`witness_by_seat=None` 时整段无该前缀 | 锁拼接点 |

**不改**：profiles.json 形状与 §3–§12（#18 的契约）、admission（#19）、`_showdown` 旧统计、api.py 响应字段。

### 14.9 验收

- `nv tests/run_all.py` 全绿；golden 的 hash 变化只来自「事实优先级」段。
- 附录 A 的行注入后，那一段输出是：`摊牌证据：他在河牌IP过牌时亮过弱A顶对（1 次）`。
- 不注入 witness（或版本失配）时，「## 对手画像」整段与今天逐字节相同。
- 9 人桌、每人 2 条构造下，witness 行总字符 ≤600，且第一个被丢的是离 Hero 最远座位的 N 最小那条。

## 15. 验收口径（#16 决议）

**前提事实**：到 2026-11-14 为止 `poker_ocr/witness/` 目录不存在，`render.py`、`witness_phrasing.json` 都没有——投影层与渲染一行未实现。所以本节所有验收项（单测、A/B、人工标注）**全部排在实现票之后**，本节只定口径，不产生可执行结果。

### 15.1 golden 与单测：两条线

- **线 1（纯函数）**：`tests/test_witness_render.py`，fixture 驱动 `render_witness_lines`，覆盖四态——附录 A 单行（输出 `摊牌证据：他在河牌IP过牌时亮过弱A顶对（1 次）`）、0 条 → `{}`、版本失配 → `{}`、9 人 × 2 条 → 总字符 ≤600 且每座位 ≤2 条。
  - 更正（#38 实测）：9 人 × 2 条在当前模板下**不会**触发 600 截断——最长个人行 = 前缀 5 + 「他在」2 + 街 2 + 位 3 + 行动 4 + 「时亮过」3 + 族 5 + 「（1234 次）」8 = 32 字，+1 换行 = 33，18 行 = 594 ≤ 600。600 仍是生产硬上限；淘汰顺序（远座位先丢、同座位 N 小先丢）改在更小的 `max_chars` 下断言，语义不变。
- **线 2（prompt 层）**：`replay-console/tests/test_prompt_golden.py` 两条断言——(a) 默认路径（无 witness、`witness_by_seat=None`）整段 prompt 的 hash 等于 §14.7 更新的 `EXPECTED_PROMPT_HASH`，且「## 对手画像」段与今天逐字节相同；(b) 注入 fixture 后 prompt 含该座位那一行、不含其它座位的行。
- **不做**：含 witness 的整段 golden 快照。理由：文案微调就要重算，且一个快照只覆盖一个局面。

### 15.2 预算与禁词的断言层级

- **预算两层**：`render_witness_lines` 单测卡死 600 字符、每座位 ≤2 条、淘汰顺序（远先丢、同座位 N 小先丢）；prompt 层留一条端到端兜底——生成整段 user prompt，抽出所有以「摊牌证据：」开头的行，断言总字符 ≤600 且座位顺序符合 `poker_ocr/prompt/user/profiles.py:19-33` 的旋转顺序。只测纯函数的话，拼接处多一个换行、或重复调用一次都不会被发现。
- **禁词作用域**：只断言以「摊牌证据：」开头的行。仓库现状没有禁词测试，且「经常」在 `poker_ocr/prompt/user/attack.py:11,34,394` 是合法用法 → 不能全局禁。
- **禁词清单**（落成 `witness_phrasing.json` 的 `forbidden` 列表，单测逐条断言）：
  - 频率副词：经常、频繁、总是、往往、通常、一贯、老是——N 是事例数，写成频率就是把事例偷偷升格成倾向。
  - 确定性词：必然、一定、肯定、铁定。
  - 建议词：建议、应该、可以、必须、最好。
  - 断言式持有：对手有、对手拿着、他手里是——只允许「亮过」。

### 15.3 A/B 怎么跑、看什么

- **开关**：`WITNESS_PROMPT`，默认 1，取值 0 关闭渲染；读法同 `poker_ocr/hand_strength.py:91`（`os.environ.get`）。不用两个 git checkout，避免混入无关差异。
- **同一份输入**：同一份代码、同一份 `data/opponents/profiles.json` 快照、同一手牌集；两遍 `nv tools/replay_live_decisions.py --model --hands hands.json --log logs/live.states.jsonl`，再 `tools/ab_diff.py --json-out` 对比。
- **手牌集不人工挑**：脚本自动筛「至少一个在局对手落在 witness 覆盖窗口（最近 5000 手内、有 admitted 行）」的决策点，全量入选；与 `docs/AB测试-手牌集.md` 的 ready 14 条做交叉核对。
- **归因补到行级**：`tools/ab_diff.py` 现在按行首 `## ` 切段，witness 行落在「## 对手画像」段内部 → 段级会把「n 涨了 1」和「多了一条证据行」混为一谈。实现票要给它加段内逐行 diff，以「摊牌证据：」前缀归一，报告「段内新增行数」与「事实块变化是否只来自 witness 行」。
- **主指标**：翻转率差值 = 「事实块因 witness 行而变化」子集的动作翻转率 −「事实块未变」子集翻转率（ab_diff 现成的超额口径），要求变了的那批 n≥30 才读比例。
- **不做**：胜率 / 权益指标——单次摊牌结果方差太大、覆盖点太少；也**不把「攻击后被打回」当主指标**——回放数据里没有这个标签，它只能靠人工判读，归 §15.4。

### 15.4 人工标注：30 点

- **构成**：20 个「有 witness 行」的决策点 + 10 个同场「无 witness 行」的对照点。
- **标注项**：3 个布尔 + 1 条备注——(a) 动作是否被证据推动（推动 / 无关 / 反被推歪）；(b) 证据解读是否正确（次数当事例 / 被读成频率 / 未被引用）；(c) 是否压过硬事实（压过即算失败案例）。
- **落盘**：`docs/AB测试-手牌集.md` 新增一节，或独立 csv 挂实现票。
- **为什么不标 50–100 手**：那是「让统计可信」的规模，属另一张票；#14 的 n≥20 已管样本量，本口径只需判「有没有被误用」。成本：单人 0.5–1 小时。
- **2026-11-16 修订（#38 实测）**：v1 数据凑不出 10 个「无 witness 行」的对照点——witness 覆盖饱和，121 个决策点里 120 点都带证据行，只剩 1 点干净。
  实际构成改为 **6 翻转 + 14 个人证据 + 9「只有池级」对照 + 1「无证据」对照 = 30**，CSV 的「证据层级」列标清每点属于哪层。
  代价说清：这 9 点仍是 treatment（也出证据行），不是真正的对照，所以 (c)「压过硬事实」在对照侧没有样本，只能看 treatment 侧有没有压过；真正的无证据对照要等 #39（投影落盘 + 覆盖率可控）之后补。
  这一条是**未完全满足** §15.4 的原始构成，票上如实写，别当成满足。

### 15.5 ground_truth 护栏：3 条 fixture + 1 条软测试

- **(a)** Hero 坚果 + 对手亮过强族 → 断言 `poker_ocr/prompt/system/priority.py` 里 Hero 牌力事实的权重高于 3a witness（动作类断言要调模型、不稳定，改断优先级表）。
- **(b)** 对手亮过弱族 + Hero 中等 → 断言该行出现，且 reason 层不出现「必然 / 一定」。
- **(c)** witness 版本失配 → 整段消失，prompt 与无 witness 时逐字节相同（fail open 一致性）。
- **软测试**：有 `OPENAI_API_KEY` 时跑一次真实模型，断言不出现「因为对手亮过 X 所以弃坚果」这类组合；无 key 则 skip（本机 `.env` 目前没有该 key）。

### 15.6 失败阈值与处置

- **判失败三条，任一命中即是失败**：
  1. 超额翻转率 ≥10 个百分点（且 n≥30）；
  2. 人工 30 点里「反被推歪」≥2 例；
  3. 出现 1 例「压过硬事实」——零容忍。
- 第 1 条的 10pp 是**先验**，首轮实测后可按分布调整；调整时在实现票留一行「为什么改」。
- **处置**：代码保留，`WITNESS_PROMPT` 默认改回 0，修文案 / 阈值后再开。不回滚删代码（投影层已单独验证）；不只调优先级权重（治不了「把事例写成频率」这类文案病）。

### 15.7 长期回归范围与签字

- **长期**：`tests/test_witness_render.py` 与 prompt golden 进 `nv tests/run_all.py`，每次跑。
- **一次性**：A/B + 人工 30 点只做 v1 一次；之后改词表 / 权重，只有「新增一族」或「改阈值」才重跑。
- **签字**：机器三项（单测、golden、A/B 阈值）齐 → 自动过；人工 30 点由本人读，在票上留一行「已读，X 例误用」，再关票。机器只能证明「没坏」，证明不了「有用」。

### 15.8 检查清单（实现票收尾时逐条勾）

- [x] `poker_ocr/witness/render.py`、`poker_ocr/tables/witness_phrasing.json` 落地（§14.8 第 1–2 条）
- [x] `tests/test_witness_render.py` 四态通过（§15.1 线 1）
- [x] prompt golden 两断言通过：无 witness 时 hash / 段字节不变、注入后有行（§15.1 线 2）
- [x] 预算两层断言通过（§15.2）
- [x] 禁词清单生效，且只作用于「摊牌证据：」行（§15.2）
- [x] ground_truth 三条 fixture 通过；软测试未跑（本机无 `OPENAI_API_KEY`）

提示词接入（2026-11-15 落）：`PROMPT_VERSION=lean-zh-2026-11-14.1`，golden
`EXPECTED_ADVICE_VERSION=advice-2026-11-14.1`、`EXPECTED_PROMPT_HASH=5acf3bc972653d5186f806c64e1b313310ef3111e5979b48e68c002abd7365e7`；
与上一版对比 user 事实块逐字节相同，system 只差「事实优先级」第 3 层三行与末句。
新增一层多出约 60 字，`tests/test_advice.py::test_system_prompt_worst_case_stays_bounded`
的最坏局面上界从 7000 放到 7100（实测 7026），仍是同一量级的延迟预算。
- [x] `tools/ab_diff.py` 行级归因可用（§15.3）——按「摊牌证据：」前缀归一（前缀每次从 `witness_phrasing.json` 读；只有导入失败/词表不可读才回退内置常量，回退时往 stderr 打一行警告，不会静默算错），报段内新增/删除行、witness 行数、其它行数、是否「只因 witness 行而变」（2026-11-16）。
  `--json-out` 现在写 `{summary, flips}`：summary 带 n / 分街 / 翻转率 / `baseline_reliable`，结论可离线复核；基线子集 n<30 时工具不再打印「超额」，只打 `—` 加一行警告。合并分批复跑用 `tools/ab_merge.py`（`--witness-only` 即 §15.3 的自动筛选）。
- [x] A/B 跑出 n 与超额翻转率，结果写进实现票（§15.3）——n=121，翻转 6/121=5.0%，同配置重跑噪声 8.3%，超额 −3.3pp；对照子集塌成 n=1 的偏差与替代口径见 `docs/AB测试-手牌集.md` §6
- [ ] 人工 30 点标完，结果与「已读，X 例误用」签字进票（§15.4 / §15.7）
- [x] `nv tests/run_all.py` 全绿（2026-11-16 本轮改动后复跑，65 个套件）

## 附录 A：一条真实证据

手牌 data/hand_outbox/sent/20261030-1e5ec30022.json：

- ts 2026-10-30T22:14:08.514+08:00；table_signature 20261030-67723049fc5c；board 6c 2h As 5c 9d。
- shows：topL（JJ21:38）6h 10s；upL（书麻头了wlr）7s Ad。
- hero LJ；upL 在河牌最后行动为 check，相对 hero 为 IP。
- #17 的例子：他在河牌 IP 过牌时亮过弱A顶对（1 次）。

对应行（winner 示例留 unknown；实际由 admit() 填）：

    {
      "player": "书麻头了wlr",
      "hand": "20261030-1e5ec30022",
      "seat": "upL",
      "ts": "2026-10-30T22:14:08.514+08:00",
      "street": "river",
      "action": "check",
      "position": "ip",
      "cards": ["7s", "Ad"],
      "board": ["6c", "2h", "As", "5c", "9d"],
      "family": "weak_ace_top_pair",
      "show_kind": "showdown",
      "status": "admitted",
      "winner": {"status": "unknown", "seat": null},
      "source": "outbox",
      "table": "20261030-67723049fc5c"
    }

同手 topL 也有一行（6h 10s）；family 由 #12 判定式决定，本契约不硬编码。
