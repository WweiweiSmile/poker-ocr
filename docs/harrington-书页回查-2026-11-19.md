# 哈林顿在现金桌：书页逐条回查（#85）

日期：2026-11-19　范围：`poker_ocr/playbook/cards/` 下全部 live 书卡（130 张）+ 移出 1 张。

## 结论

| verdict | 张数 | 含义 |
| --- | --- | --- |
| ok | 63 | 页码、转述、confidence 均与原文一致 |
| fixed | 56 | 本轮修过页码/转述/confidence |
| compiled | 11 | 方向有原文依据，频率/尺寸为编译值 |
| removed | 1 | 无法核实，已移出 live |

机器可查台账：[`poker_ocr/playbook/book_audit.json`](../poker_ocr/playbook/book_audit.json)，按卡 id 直接检索；每张卡带卷、印刷页、PDF 页、原文短句与结论。

## 方法

- 页码口径：`pages` 是印刷页，`pdf_pages` 是 PDF 页；第 1 卷 `pdf = 印刷 + 10`，第 2 卷 `pdf = 印刷 + 6`（已用页眉目视确认）。
- 对每张卡引用的页 ±2 页做 OCR，逐句比对卡片的 plan / why；异页或过度归纳的卡改页、降级或移出。
- 不调用模型、不跑 A/B；OCR 全文只在临时目录，仓库只保留短句与页码（版权）。

## 修正清单

- `flop.air.oop.straight`（p221）：书只给 60/40，未给尺寸；why 补「半池为编译」。
- `flop.set.oop.straight`（p209、p210）：原引 p209；转牌 90/50 与 765 80% 在 p210，补页并改写 why。
- `turn.set.oop.deleverage`（p52、p53）：去杠杆结论在 p53，原只引 p52，已补页。
- `turn.tptk.bluff_catch`（p55）：书说应过牌抓诈唬；面对下注跟注是编译延伸，降为 interpreted。
- `turn.tptk.oop.check`（p46、p47）：过牌结论在 p47，原只引 p46，已补页。
- `flop.mw.air.check`（p294）：原引 p285（挤压牌例）；「什么都没有面对四个玩家过牌」在 p294。
- `flop.mw.air.facing_bet`（p294）：原引 p302（76s 牌例）；改 p294 马图索过牌、内格里诺弃牌，降为 interpreted。
- `flop.mw.board_play.check`（p293）：原引 p285 无依据；改 p293 弱成牌不在多人池走远，并改写 why（打牌面人人有份，不是中等牌力）。
- `flop.mw.board_play.facing_bet`（p293）：原引 p302 无依据；改 p293，并改写 why。
- `flop.mw.draw.facing_bet`（p296）：书只说多人池听牌隐含赔率高，未给跟注线；降为 interpreted。
- `flop.mw.overpair.bet`（p287、p288）：补 p288「两个跟注转牌过牌」。
- `flop.mw.strong_made.bet`（p301）：原引 p288（超对续注）；强牌下注原则在 p301。
- `flop.mw.top_pair_top.bet`（p286）：原引 p287（AA 超对）；顶对通常下注在 p286。
- `flop.mw.top_pair_weak.bet`（p284、p286）：原引 p287（AA 超对）；p284 弱踢顶对过牌受赞赏，已补对照页。
- `turn.mw.air.facing_bet`（p58、p59）：弃牌结论在 p59，原只引 p58，已补页。
- `turn.mw.strong_made.bet`（p5）：原引 p47（A8 顶对牌例）；转牌价值下注更大在 p5。
- `turn.mw.top_pair_weak.check`（p46、p47）：书例是双人底池（A8 顶对中踢，p46-47）；多人局取同一过牌线为编译，降为 interpreted。
- `preflop.bb.check.limped`（p124）：原引 p123/p129；大盲节在 p124，且过牌是规则兜底，降为 interpreted。
- `preflop.bb.check.unopened`（p124）：原引 p123（小盲节）；大盲节在 p124，且过牌是规则兜底，降为 interpreted。
- `preflop.limped.ace_broadway`（p119）：原引 p128（大盲防偷）；AJ/AT 跟注平跟池在 p119，已改页。
- `preflop.limped.other`（p105）：原引 p95-107；「其他牌从来不玩」在 p105，已改页。
- `preflop.limped.small_pair`（p112、p113）：原引 p112、p129（p129 是攻击溜入者，不含小对子）；改 p112-113。
- `preflop.limped.speculative`（p122）：书 p122 明说两个以上平跟也平跟，升为 explicit。
- `preflop.limped.trouble`（p120）：why 页标写作 p129，实为 p120，已改。
- `preflop.limped.weak_ace`（p119）：原引 p128（大盲防偷）；弱 A 不在平跟池跟注名单，主句在 p119，已改页。
- `preflop.rfi.ace_broadway.early.suited`（p118）：书是加注/平跟混合；卡只取平跟一支，降为 interpreted。
- `preflop.rfi.aq`（p116、p118）：原引 p116 讲的是面对加注；无人池加注按 p118 后位 Ax 多数加注编译，降为 interpreted。
- `preflop.rfi.other`（p105）：原引 p95-107；「其他牌从来不玩」实为 p105 示例，已改页。
- `preflop.rfi.small_pair.early`（p113）：原引 p111-112；弱局前/中/后位小对子打法在 p113，已改页并写清平跟分支。
- `preflop.rfi.small_pair.late`（p113）：原引 p112；后位多加注在 p113，已改页。
- `preflop.rfi.small_pair.middle`（p113）：原引 p112；具体位置建议在 p113，已改页。
- `preflop.rfi.speculative.late`（p122）：原引 p122-123（p123 是小盲节）；后位加注为编译，降为 interpreted。
- `preflop.rfi.trouble.early.suited`（p120）：书是加注/平跟混合；卡只取平跟一支，降为 interpreted。
- `preflop.vs_3bet.ak`（p115、p116）：书 p116 明确弃牌，原标 interpreted；升为 explicit。
- `preflop.vs_3bet.aq`（p115、p116）：书 p116 明确弃牌，原标 interpreted；升为 explicit。
- `preflop.vs_3bet.small_pair`（p111、p113、p114）：书未单列 55-22 对 3-bet；p111 原则属中口袋对，降为 interpreted 并写明编译链 p111/p113/p114。
- `preflop.vs_3bet.suited_connector`（p122）：原引 p121（单次加注节）；改 p122「再加注可能性不小就弃」。
- `preflop.vs_raise.ace_broadway`（p118、p119）：原引 p119；Ax 面对加注的主句在 p118-119，已合并。
- `preflop.vs_raise.aq`（p116）：书对 AQo/前位加注者有例外；降为 interpreted 并写明。
- `preflop.vs_raise.low_pair.ip`（p110）：原引 p110、p114；88-66 属中口袋对（p110），p114 是小对子节，已改页。
- `preflop.vs_raise.low_pair.oop`（p110）：原引 p112/114；88-66 属中口袋对（p110），已改页。
- `preflop.vs_raise.low_pair.unknown`（p110）：原引 p112/114；88-66 属中口袋对（p110），已改页。
- `preflop.vs_raise.medium_pair.ip`（p110）：原引 p114（小对子节）；中口袋对有位置跟注在 p110。why 原说「别打成再加注」过度归纳，改为默认跟注+书允许的 4x 例外。
- `preflop.vs_raise.medium_pair.oop`（p110）：原引 p112/114；书只给有位置跟注（p110），无位置按保守口径弃。
- `preflop.vs_raise.medium_pair.unknown`（p110）：原引 p112/114；书只给有位置跟注（p110），位置未知按无位置处理。
- `preflop.vs_raise.other`（p105）：原引 p122；「其他牌从来不玩」在 p105，已改页。
- `preflop.vs_raise.small_pair.ip`（p113、p114）：原引 p110/114；小对子有位置跟注在 p113，p110 是中口袋对，已改页。
- `preflop.vs_raise.small_pair.oop`（p112、p113）：原引 p110/114；改 p112-113，why 补弱局允许无位置跟注的例外。
- `preflop.vs_raise.small_pair.unknown`（p112、p113）：原引 p110/114；改 p112-113，why 补弱局例外。
- `preflop.vs_raise.suited_connector.ip`（p121）：why 原称「至少已有一个跟注者」，但 Spot 无此维度；补编译说明。
- `preflop.vs_raise.weak_ace`（p118、p119）：原引 p122；Ax 面对加注的主句在 p118-119，已改页。
- `river.bluff_catch.middle_pair.facing_bet`（p75、p85）：书是混合策略（小注跟、大注看故事）；why 写明 freq 1.0 为编译。
- `river.bluff_catch.top_pair_weak.facing_bet`（p75）：why 写明取跟注一支。
- `river.check.board_play`（p74）：why 改为「只能分池或输」的保守一支。
- `river.fold.board_play.facing_bet`（p78）：why 改为「只能分池或输」的保守一支。
- `river.value.strong_made.bet`（p71、p82）：p71 明说「认为可能最好就下注」，升为 explicit；尺寸按 p82。

## 移出 live

- `flop.draw.oop.flush`（第2卷 p25 / PDF p31）：v2 p25 是牌例一（A9/JT3）开场，不是同花听牌例；全库 OCR 未找到「65% 下注 60% 池」的原文，无法核实，移出 live。

## 已知限制

- Spot 没有「跟注者人数」「精确位置」等维度：个别卡（如先平跟再加注、AQ 全位置）只能按书上的一条分支编译，已在 why 与台账注明。
- `board_play`（打牌面）不是书上单列的概念：按「人人都有、只能分池或输」取书中最接近的弱成牌过牌/弃牌原则，标 interpreted。
- 本轮只核对 live 卡；新加卡必须同时补台账，`tests/test_playbook_book_audit.py` 会拦住漏项。

## 逐卡台账

| id | 卷/印刷页 | PDF 页 | confidence | verdict | 原文关键词 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| `flop.air.oop.straight` | 1 / 221 | 231 | interpreted | fixed | 策略：60%下注，40%过牌。 | 书只给 60/40，未给尺寸；why 补「半池为编译」。 |
| `flop.overpair.oop.dry` | 1 / 204 | 214 | interpreted | ok | 策略：70%下注，30%过牌。 | 页码、转述与 confidence 一致。 |
| `flop.set.oop.dry` | 1 / 201 | 211 | interpreted | ok | 策略：30%下注，70%过牌。 | 页码、转述与 confidence 一致。 |
| `flop.set.oop.straight` | 1 / 209、210 | 219、220 | interpreted | fixed | 翻牌3：AT9 策略：下注70%，过牌30%；翻牌4：765 策略：80%下注，20%过牌。 | 原引 p209；转牌 90/50 与 765 80% 在 p210，补页并改写 why。 |
| `flop.tptk.oop.dry` | 1 / 204 | 214 | interpreted | ok | 策略：70%下注，30%过牌……建议下注一半底池。 | 页码、转述与 confidence 一致。 |
| `turn.air.oop.second_barrel` | 2 / 48 | 54 | interpreted | ok | 继续下注是在显示你真正有强牌。 | 页码、转述与 confidence 一致。 |
| `turn.set.oop.deleverage` | 2 / 52、53 | 58、59 | explicit | fixed | 应该运用去杠杆原理，在转牌过牌，然后到河牌下注。 | 去杠杆结论在 p53，原只引 p52，已补页。 |
| `turn.tptk.bluff_catch` | 2 / 55 | 61 | interpreted | fixed | 我们的牌只能抓诈唬，应该过牌。 | 书说应过牌抓诈唬；面对下注跟注是编译延伸，降为 interpreted。 |
| `turn.tptk.oop.check` | 2 / 46、47 | 52、53 | explicit | fixed | 对于顶对、中等踢脚的牌来说已经够大，所以应该过牌。 | 过牌结论在 p47，原只引 p46，已补页。 |
| `flop.mw.air.check` | 1 / 294 | 304 | explicit | fixed | 他不能在面对四个玩家时去做什么动作，他也选择过牌。 | 原引 p285（挤压牌例）；「什么都没有面对四个玩家过牌」在 p294。 |
| `flop.mw.air.facing_bet` | 1 / 294 | 304 | interpreted | fixed | 内格里诺：弃牌。布朗森也许是在诈唬，但还有三个人尚未行动，丹尼尔的这手牌太弱，位置也差。 | 原引 p302（76s 牌例）；改 p294 马图索过牌、内格里诺弃牌，降为 interpreted。 |
| `flop.mw.board_play.check` | 1 / 293 | 303 | interpreted | fixed | 中对和小于顶对的口袋对子，都不应该在多人底池中走太远。 | 原引 p285 无依据；改 p293 弱成牌不在多人池走远，并改写 why（打牌面人人有份，不是中等牌力）。 |
| `flop.mw.board_play.facing_bet` | 1 / 293 | 303 | interpreted | fixed | 中对和小于顶对的口袋对子，都不应该在多人底池中走太远。 | 原引 p302 无依据；改 p293，并改写 why。 |
| `flop.mw.draw.check` | 1 / 296 | 306 | interpreted | ok | 你经常可以不用投入而免费听牌。 | 页码、转述与 confidence 一致。 |
| `flop.mw.draw.facing_bet` | 1 / 296 | 306 | interpreted | fixed | 综合权衡，利多于弊。有一手听牌的话，与只有一两个对手相比，我更愿意在多人局中玩。 | 书只说多人池听牌隐含赔率高，未给跟注线；降为 interpreted。 |
| `flop.mw.middle_pair.check` | 1 / 289 | 299 | interpreted | ok | 要用中对子干点什么的话，必须满足以下两个条件：一个没有很多听牌的翻牌面；桌上的玩家打得很弱。 | 页码、转述与 confidence 一致。 |
| `flop.mw.middle_pair.facing_bet` | 1 / 292 | 302 | explicit | ok | 你应该弃牌。玩家C在还有三个人未行动的情况下下注，表明他一定有些什么。 | 页码、转述与 confidence 一致。 |
| `flop.mw.overpair.bet` | 1 / 287、288 | 297、298 | interpreted | fixed | 你应该下注一个正常的筹码量，比如70-100；两个跟注则转牌过牌（p288）。 | 补 p288「两个跟注转牌过牌」。 |
| `flop.mw.strong_made.bet` | 1 / 301 | 311 | interpreted | fixed | 你不能只在你形成坚果牌或者接近坚果牌时才去下注。 | 原引 p288（超对续注）；强牌下注原则在 p301。 |
| `flop.mw.top_pair_top.bet` | 1 / 286 | 296 | interpreted | fixed | 拿着超对或顶对，通常都愿意在翻牌下注。 | 原引 p287（AA 超对）；顶对通常下注在 p286。 |
| `flop.mw.top_pair_weak.bet` | 1 / 284、286 | 294、296 | interpreted | fixed | 拿着超对或顶对，通常都愿意在翻牌下注；但弱踢顶对在三人未行动时过牌也受赞赏（p284）。 | 原引 p287（AA 超对）；p284 弱踢顶对过牌受赞赏，已补对照页。 |
| `turn.mw.air.check` | 2 / 20 | 26 | interpreted | ok | 也可以简单地选择过牌，如果有人下注就弃牌。 | 页码、转述与 confidence 一致。 |
| `turn.mw.air.facing_bet` | 2 / 58、59 | 64、65 | interpreted | fixed | 稳妥的做法是弃牌……弃牌吧。 | 弃牌结论在 p59，原只引 p58，已补页。 |
| `turn.mw.board_play.check` | 2 / 21 | 27 | interpreted | ok | 你的牌有点牌力，对手没有听牌……通常你不得不在转牌过牌。 | 页码、转述与 confidence 一致。 |
| `turn.mw.board_play.facing_bet` | 2 / 24 | 30 | interpreted | compiled | 你的顶对很可能已经落后。弃牌吧。 | p24 只讲顶对落后；打牌面弃为编译。 |
| `turn.mw.draw.bet` | 2 / 60 | 66 | interpreted | compiled | 现在你肯定想下注……差不多是一个底池左右，比如15。 | 书例下注约一池（15）；卡取 2/3 为编译。 |
| `turn.mw.draw.call` | 2 / 5 | 11 | interpreted | compiled | 转牌的价值下注比翻牌的价值下注更大；听牌隐含赔率更高。 | p5 讲转牌价值下注与听牌隐含赔率；跟注线为编译。 |
| `turn.mw.middle_pair.check` | 2 / 22 | 28 | interpreted | ok | 你应该在转牌过牌，在河牌抓诈唬。 | 页码、转述与 confidence 一致。 |
| `turn.mw.middle_pair.facing_bet` | 2 / 45 | 51 | interpreted | ok | 你不能用中对来全压……弃牌吧。 | 页码、转述与 confidence 一致。 |
| `turn.mw.strong_made.bet` | 2 / 5 | 11 | interpreted | fixed | 转牌的价值下注比翻牌的价值下注更大。 | 原引 p47（A8 顶对牌例）；转牌价值下注更大在 p5。 |
| `turn.mw.strong_made.facing_bet` | 2 / 12 | 18 | interpreted | compiled | 为了河牌的全压而在转牌下注……在转牌下注来做大底池。 | p12 讲转牌下注做大底池；加注为编译。 |
| `turn.mw.strong_made.facing_raise` | 2 / 35 | 41 | interpreted | ok | 完全不要考虑扔掉你的暗三条。 | 页码、转述与 confidence 一致。 |
| `turn.mw.top_pair.facing_bet` | 2 / 24 | 30 | explicit | ok | 你的顶对很可能已经落后。弃牌吧。 | 页码、转述与 confidence 一致。 |
| `turn.mw.top_pair_weak.check` | 2 / 46、47 | 52、53 | interpreted | fixed | 对于顶对、中等踢脚的牌来说已经够大，所以应该过牌。 | 书例是双人底池（A8 顶对中踢，p46-47）；多人局取同一过牌线为编译，降为 interpreted。 |
| `preflop.bb.check.limped` | 1 / 124 | 134 | interpreted | fixed | 由于已经放进底池一些筹码，比其他玩家有更好的底池赔率（甚至有时能免费看翻牌）。 | 原引 p123/p129；大盲节在 p124，且过牌是规则兜底，降为 interpreted。 |
| `preflop.bb.check.unopened` | 1 / 124 | 134 | interpreted | fixed | 由于已经放进底池一些筹码，比其他玩家有更好的底池赔率（甚至有时能免费看翻牌）。 | 原引 p123（小盲节）；大盲节在 p124，且过牌是规则兜底，降为 interpreted。 |
| `preflop.limped.ace_broadway` | 1 / 119 | 129 | explicit | fixed | 如果前面几个人平跟，拿着AJ、AT的话我会跟注，其他的都弃牌。 | 原引 p128（大盲防偷）；AJ/AT 跟注平跟池在 p119，已改页。 |
| `preflop.limped.ak` | 1 / 129 | 139 | interpreted | compiled | AA到TT的大口袋对，AKs和AQs；非同花 AK 按同名加注（编译）。 | 书只列 AKs；非同花 AK 按同名加注为编译。 |
| `preflop.limped.aq` | 1 / 129 | 139 | interpreted | compiled | AA到TT的大口袋对，AKs和AQs；非同花 AQ 按同名加注（编译）。 | 书只列 AQs；非同花 AQ 按同名加注为编译。 |
| `preflop.limped.big_pair_aa_kk` | 1 / 129 | 139 | explicit | ok | 唯一能让我感觉可以舒舒服服加注来打击一群（三个或者更多）溜入者的牌包括：AA到TT的大口袋对，AKs和AQs。 | 页码、转述与 confidence 一致。 |
| `preflop.limped.big_pair_qq` | 1 / 129 | 139 | explicit | ok | 唯一能让我感觉可以舒舒服服加注来打击一群（三个或者更多）溜入者的牌包括：AA到TT的大口袋对，AKs和AQs。 | 页码、转述与 confidence 一致。 |
| `preflop.limped.low_pair` | 1 / 129 | 139 | interpreted | compiled | AA到TT的大口袋对，AKs和AQs；不在名单里就平跟看翻牌（编译）。 | 书只列 AA-TT/AKs/AQs；88-66 平跟为编译。 |
| `preflop.limped.medium_pair` | 1 / 129 | 139 | interpreted | compiled | 唯一能让我感觉可以舒舒服服加注来打击一群（三个或者更多）溜入者的牌包括：AA到TT的大口袋对，AKs和AQs。 | 书只列 AA-TT；99 按 p110 中口袋对归入，编译。 |
| `preflop.limped.other` | 1 / 105 | 115 | explicit | fixed | 其他牌：从来不玩 | 原引 p95-107；「其他牌从来不玩」在 p105，已改页。 |
| `preflop.limped.small_pair` | 1 / 112、113 | 122、123 | interpreted | fixed | 小口袋对本质上是个听牌，或者说投机牌。 | 原引 p112、p129（p129 是攻击溜入者，不含小对子）；改 p112-113。 |
| `preflop.limped.speculative` | 1 / 122 | 132 | explicit | fixed | 如果前面有两个或者更多的平跟人局者，那也平跟人局。 | 书 p122 明说两个以上平跟也平跟，升为 explicit。 |
| `preflop.limped.suited_connector` | 1 / 121 | 131 | explicit | ok | 前面有几个人平跟入局……理想环境是有好位置。 | 页码、转述与 confidence 一致。 |
| `preflop.limped.trouble` | 1 / 120 | 130 | interpreted | fixed | 我可能会用KQ、KJ同花或非同花去跟注。 | why 页标写作 p129，实为 p120，已改。 |
| `preflop.limped.weak_ace` | 1 / 119 | 129 | explicit | fixed | 如果前面几个人平跟，拿着AJ、AT的话我会跟注，其他的都弃牌。 | 原引 p128（大盲防偷）；弱 A 不在平跟池跟注名单，主句在 p119，已改页。 |
| `preflop.limped.weak_trouble` | 1 / 120 | 130 | explicit | ok | 如果前面已经有人加注，这些麻烦牌都弃了吧。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.ace_broadway.early.offsuit` | 1 / 118 | 128 | explicit | ok | 其他的Ax都弃牌。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.ace_broadway.early.suited` | 1 / 118 | 128 | interpreted | fixed | 在前位，我只玩AJ同花，加注和平跟混合着玩。 | 书是加注/平跟混合；卡只取平跟一支，降为 interpreted。 |
| `preflop.rfi.ace_broadway.late` | 1 / 118 | 128 | explicit | ok | 在后位，所有的Ax牌都可以玩……80%-20%或90%-10%。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.ace_broadway.middle` | 1 / 118 | 128 | explicit | ok | 中位。AJ-A7都可以玩。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.ak` | 1 / 115 | 125 | explicit | ok | 前位……75%-25%的加注、平跟比例。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.aq` | 1 / 116、118 | 126、128 | interpreted | fixed | 如果前面有人加注，又有人再加注，我会弃掉AK。 | 原引 p116 讲的是面对加注；无人池加注按 p118 后位 Ax 多数加注编译，降为 interpreted。 |
| `preflop.rfi.big_pair_aa_kk` | 1 / 108、109 | 118、119 | explicit | compiled | 无论在前位、中位还是后位，都应该加注。 | 书说永远加注、偶尔平跟；0.9 频率为编译。 |
| `preflop.rfi.big_pair_qq` | 1 / 108、109 | 118、119 | explicit | compiled | 无论在前位、中位还是后位，都应该加注。 | 书说永远加注、偶尔平跟；0.9 频率为编译。 |
| `preflop.rfi.low_pair.early` | 1 / 110 | 120 | explicit | ok | 对较小的中口袋对，即88-66，可以将比例反过来，即30%加注，70%跟注。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.low_pair.late` | 1 / 110 | 120 | explicit | ok | 继续采取进攻型的80%加注、20%跟注的打法。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.low_pair.middle` | 1 / 110 | 120 | explicit | ok | 这些中口袋对大部分情况下都可以加注……可以80%加注，20%跟注。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.medium_pair.early` | 1 / 110 | 120 | explicit | ok | 对较大的中口袋对，也就是JJ到99，采取大部分情况下加注、小部分情况下平跟的策略，可以70%加注，30%跟注。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.medium_pair.late` | 1 / 110 | 120 | explicit | ok | 继续采取进攻型的80%加注、20%跟注的打法。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.medium_pair.middle` | 1 / 110 | 120 | explicit | ok | 这些中口袋对大部分情况下都可以加注……可以80%加注，20%跟注。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.other` | 1 / 105 | 115 | explicit | fixed | 其他牌：从来不玩 | 原引 p95-107；「其他牌从来不玩」实为 p105 示例，已改页。 |
| `preflop.rfi.small_pair.early` | 1 / 113 | 123 | interpreted | fixed | 前位大部分时候平跟，偶尔加注；中间位置一样；后位多加注，偶尔平跟。 | 原引 p111-112；弱局前/中/后位小对子打法在 p113，已改页并写清平跟分支。 |
| `preflop.rfi.small_pair.late` | 1 / 113 | 123 | explicit | fixed | 前位大部分时候平跟，偶尔加注；中间位置一样；后位多加注，偶尔平跟。 | 原引 p112；后位多加注在 p113，已改页。 |
| `preflop.rfi.small_pair.middle` | 1 / 113 | 123 | interpreted | fixed | 前位大部分时候平跟，偶尔加注；中间位置一样；后位多加注，偶尔平跟。 | 原引 p112；具体位置建议在 p113，已改页。 |
| `preflop.rfi.speculative.early` | 1 / 122 | 132 | explicit | ok | 通常，玩上面列出的牌型需要很多条件：有好位置；牌桌玩得很紧，前面很多人都弃牌到你；牌桌很少甚至没有再加注。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.speculative.late` | 1 / 122 | 132 | interpreted | fixed | 如果前面有两个或者更多的平跟人局者，那也平跟人局。 | 原引 p122-123（p123 是小盲节）；后位加注为编译，降为 interpreted。 |
| `preflop.rfi.speculative.middle` | 1 / 122 | 132 | explicit | ok | 通常，玩上面列出的牌型需要很多条件：有好位置；牌桌玩得很紧，前面很多人都弃牌到你；牌桌很少甚至没有再加注。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.suited_connector.early` | 1 / 121 | 131 | interpreted | ok | 在前位，我用15%-25%的同花连牌加注，其他的弃掉。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.suited_connector.late` | 1 / 121 | 131 | explicit | ok | 在后位，基本上玩所有的同花连牌，大部分情况下加注。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.suited_connector.middle` | 1 / 121 | 131 | explicit | ok | 在中位，JTs到67s，一部分加注，一部分平跟。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.trouble.early.offsuit` | 1 / 120 | 130 | explicit | ok | 在前位我唯一愿意玩的是KQs，可能加注，也可能平跟。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.trouble.early.suited` | 1 / 120 | 130 | interpreted | fixed | 在前位我唯一愿意玩的是KQs，可能加注，也可能平跟。 | 书是加注/平跟混合；卡只取平跟一支，降为 interpreted。 |
| `preflop.rfi.trouble.late` | 1 / 120 | 130 | explicit | ok | 在后位，这五种牌型都可以加注。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.trouble.middle` | 1 / 120 | 130 | explicit | ok | 拿着KQ我喜欢玩得激进点，多数情况加注，偶尔平跟。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.weak_ace.early` | 1 / 118 | 128 | explicit | ok | 在前位……其他的Ax都弃牌。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.weak_ace.late` | 1 / 118 | 128 | explicit | ok | 在后位，所有的Ax牌都可以玩……80%-20%或90%-10%。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.weak_ace.middle.offsuit` | 1 / 118 | 128 | interpreted | ok | 中位。AJ-A7都可以玩。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.weak_ace.middle.suited` | 1 / 118、119 | 128、129 | interpreted | ok | 中位。AJ-A7都可以玩。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.weak_trouble.early` | 1 / 120 | 130 | explicit | ok | KT、QT无论同花还是非同花都直接弃掉。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.weak_trouble.late` | 1 / 120 | 130 | explicit | ok | KT、QT无论同花还是非同花都直接弃掉。 | 页码、转述与 confidence 一致。 |
| `preflop.rfi.weak_trouble.middle` | 1 / 120 | 130 | explicit | ok | KT、QT无论同花还是非同花都直接弃掉。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.ace_broadway` | 1 / 118 | 128 | interpreted | ok | 如果前面有人加注，一般要扔掉所有的Ax牌。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.ak` | 1 / 115、116 | 125、126 | explicit | fixed | 如果前面有人加注，又有人再加注，我会弃掉AK。 | 书 p116 明确弃牌，原标 interpreted；升为 explicit。 |
| `preflop.vs_3bet.aq` | 1 / 115、116 | 125、126 | explicit | fixed | 如果有人加注，又有人再加注，毫无疑问我会扔掉AQ。 | 书 p116 明确弃牌，原标 interpreted；升为 explicit。 |
| `preflop.vs_3bet.big_pair_aa_kk` | 1 / 109 | 119 | explicit | ok | 如果是AA或KK，我会继续再加注。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.big_pair_qq` | 1 / 109 | 119 | explicit | ok | 如果是QQ……这时候QQ最多只能跟注。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.low_pair` | 1 / 111 | 121 | explicit | ok | 如果前面有人加注，有人再加注，直接弃牌。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.medium_pair` | 1 / 111 | 121 | explicit | ok | 如果前面有人加注，有人再加注，直接弃牌。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.other` | 1 / 105 | 115 | interpreted | ok | 其他牌：从来不玩 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.small_pair` | 1 / 111、113、114 | 121、123、124 | interpreted | fixed | 如果前面有人加注，有人再加注，直接弃牌；强局没位置也弃（p113）；筹码不足 25 倍加注额弃（p114）。 | 书未单列 55-22 对 3-bet；p111 原则属中口袋对，降为 interpreted 并写明编译链 p111/p113/p114。 |
| `preflop.vs_3bet.speculative` | 1 / 122 | 132 | interpreted | ok | 如果再加注的可能性不小，还是直接弃掉为好。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.suited_connector` | 1 / 122 | 132 | interpreted | fixed | 如果再加注的可能性不小，还是直接弃掉为好。 | 原引 p121（单次加注节）；改 p122「再加注可能性不小就弃」。 |
| `preflop.vs_3bet.trouble` | 1 / 120 | 130 | interpreted | ok | 如果前面已经有人加注，这些麻烦牌都弃了吧。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.weak_ace` | 1 / 118 | 128 | interpreted | ok | 如果前面有人加注，一般要扔掉所有的Ax牌。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_3bet.weak_trouble` | 1 / 120 | 130 | interpreted | ok | 如果前面已经有人加注，这些麻烦牌都弃了吧。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_raise.ace_broadway` | 1 / 118、119 | 128、129 | explicit | fixed | 如果前面有人加注，一般要扔掉所有的Ax牌。 | 原引 p119；Ax 面对加注的主句在 p118-119，已合并。 |
| `preflop.vs_raise.ak` | 1 / 115、116 | 125、126 | explicit | ok | 如果前面已经加注，我会跟注。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_raise.aq` | 1 / 116 | 126 | interpreted | fixed | 如果前面只有一个加注者，我拿AQ会跟注。 | 书对 AQo/前位加注者有例外；降为 interpreted 并写明。 |
| `preflop.vs_raise.big_pair_aa_kk` | 1 / 108、109 | 118、119 | explicit | ok | 如果有人已经加注，那你就再加注。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_raise.big_pair_qq` | 1 / 108、109 | 118、119 | explicit | ok | 如果有人已经加注，那你就再加注。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_raise.low_pair.ip` | 1 / 110 | 120 | interpreted | fixed | 如果前面已经有人加注入局，翻牌后你对于加注者有位置优势的话，可以用所有的中口袋对跟注。 | 原引 p110、p114；88-66 属中口袋对（p110），p114 是小对子节，已改页。 |
| `preflop.vs_raise.low_pair.oop` | 1 / 110 | 120 | interpreted | fixed | 如果前面已经有人加注入局，翻牌后你对于加注者有位置优势的话，可以用所有的中口袋对跟注。 | 原引 p112/114；88-66 属中口袋对（p110），已改页。 |
| `preflop.vs_raise.low_pair.unknown` | 1 / 110 | 120 | interpreted | fixed | 如果前面已经有人加注入局，翻牌后你对于加注者有位置优势的话，可以用所有的中口袋对跟注。 | 原引 p112/114；88-66 属中口袋对（p110），已改页。 |
| `preflop.vs_raise.medium_pair.ip` | 1 / 110 | 120 | interpreted | fixed | 如果前面已经有人加注入局，翻牌后你对于加注者有位置优势的话，可以用所有的中口袋对跟注。 | 原引 p114（小对子节）；中口袋对有位置跟注在 p110。why 原说「别打成再加注」过度归纳，改为默认跟注+书允许的 4x 例外。 |
| `preflop.vs_raise.medium_pair.oop` | 1 / 110 | 120 | interpreted | fixed | 如果前面已经有人加注入局，翻牌后你对于加注者有位置优势的话，可以用所有的中口袋对跟注。 | 原引 p112/114；书只给有位置跟注（p110），无位置按保守口径弃。 |
| `preflop.vs_raise.medium_pair.unknown` | 1 / 110 | 120 | interpreted | fixed | 如果前面已经有人加注入局，翻牌后你对于加注者有位置优势的话，可以用所有的中口袋对跟注。 | 原引 p112/114；书只给有位置跟注（p110），位置未知按无位置处理。 |
| `preflop.vs_raise.other` | 1 / 105 | 115 | explicit | fixed | 其他牌：从来不玩 | 原引 p122；「其他牌从来不玩」在 p105，已改页。 |
| `preflop.vs_raise.small_pair.ip` | 1 / 113、114 | 123、124 | interpreted | fixed | 在弱的游戏牌局中，拿小口袋对跟注。 | 原引 p110/114；小对子有位置跟注在 p113，p110 是中口袋对，已改页。 |
| `preflop.vs_raise.small_pair.oop` | 1 / 112、113 | 122、123 | interpreted | fixed | 在弱的游戏牌局中……我即使位置不好，也会用小对子跟一个加注。 | 原引 p110/114；改 p112-113，why 补弱局允许无位置跟注的例外。 |
| `preflop.vs_raise.small_pair.unknown` | 1 / 112、113 | 122、123 | interpreted | fixed | 在弱的游戏牌局中……我即使位置不好，也会用小对子跟一个加注。 | 原引 p110/114；改 p112-113，why 补弱局例外。 |
| `preflop.vs_raise.speculative` | 1 / 122 | 132 | interpreted | ok | 如果再加注的可能性不小，还是直接弃掉为好。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_raise.suited_connector.ip` | 1 / 121 | 131 | interpreted | fixed | 跟注加注时要有好位置，至少一个玩家已经跟了加注。 | why 原称「至少已有一个跟注者」，但 Spot 无此维度；补编译说明。 |
| `preflop.vs_raise.suited_connector.oop` | 1 / 121 | 131 | interpreted | ok | 跟注加注时要有好位置，至少一个玩家已经跟了加注。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_raise.suited_connector.unknown` | 1 / 121 | 131 | interpreted | ok | 跟注加注时要有好位置，至少一个玩家已经跟了加注。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_raise.trouble` | 1 / 120 | 130 | explicit | ok | 如果前面已经有人加注，这些麻烦牌都弃了吧。 | 页码、转述与 confidence 一致。 |
| `preflop.vs_raise.weak_ace` | 1 / 118、119 | 128、129 | explicit | fixed | 如果前面有人加注，一般要扔掉所有的Ax牌。 | 原引 p122；Ax 面对加注的主句在 p118-119，已改页。 |
| `preflop.vs_raise.weak_trouble` | 1 / 120 | 130 | explicit | ok | 如果前面已经有人加注，这些麻烦牌都弃了吧。 | 页码、转述与 confidence 一致。 |
| `river.bluff.air.bet` | 2 / 78、82 | 84、88 | interpreted | compiled | 在河牌诈唬时……用最弱的牌去诈唬。 | p78 给诈唬原则；0.5 频率/0.67 尺寸为编译。 |
| `river.bluff.air.facing_bet` | 2 / 78 | 84 | explicit | ok | 如果到了河牌，你的牌仍没有任何价值，标准的打法当然是过牌，如果别人下注，你就弃牌。 | 页码、转述与 confidence 一致。 |
| `river.bluff_catch.middle_pair.facing_bet` | 2 / 75、85 | 81、91 | interpreted | fixed | 如果对手的下注很小……通常你会跟注；如果对手下了大注，你需要判断对方是否在诈唬。 | 书是混合策略（小注跟、大注看故事）；why 写明 freq 1.0 为编译。 |
| `river.bluff_catch.top_pair_weak.facing_bet` | 2 / 75 | 81 | interpreted | fixed | 如果对手的下注很小……通常你会跟注；如果对手下了大注，你需要判断对方是否在诈唬。 | why 写明取跟注一支。 |
| `river.check.board_play` | 2 / 74 | 80 | interpreted | fixed | 当你的牌有一点价值时，你希望以最小的代价熬到亮牌。 | why 改为「只能分池或输」的保守一支。 |
| `river.check.middle_pair` | 2 / 74、75 | 80、81 | interpreted | ok | 当你的牌有一点价值时，你希望以最小的代价熬到亮牌。 | 页码、转述与 confidence 一致。 |
| `river.check.top_pair_weak` | 2 / 74 | 80 | interpreted | ok | 当你的牌有一点价值时，你希望以最小的代价熬到亮牌。 | 页码、转述与 confidence 一致。 |
| `river.fold.board_play.facing_bet` | 2 / 78 | 84 | interpreted | fixed | 如果到了河牌，你的牌仍没有任何价值……如果别人下注，你就弃牌。 | why 改为「只能分池或输」的保守一支。 |
| `river.value.strong_made.bet` | 2 / 71、82 | 77、88 | explicit | fixed | 你只要认为可能已拿着最好的牌，就应该考虑下注；越接近坚果下注越大（p82）。 | p71 明说「认为可能最好就下注」，升为 explicit；尺寸按 p82。 |
