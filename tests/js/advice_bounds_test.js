/* 建议卡「数据边界」段：刻意抽成纯函数（`advice_bounds.js`），靠 node 断言，不需要浏览器。
 *
 * 口径来自 #111：
 *   · 一条 warning 一行，顺序与 advice 记录里的列表一致；
 *   · 空列表 / 缺字段时整段不出现——卡片输出与改动前逐字相同（GOLDEN_CARD 是改动前
 *     b2f537f 的 adviceHtml 对同一份代表性 advice 的渲染结果，改动后仍须逐字相等）；
 *   · 换行保留在同一个条目里（靠 CSS 白空格渲染），不会被拆成两个条目；
 *   · warning 文本一律经过与 adviceHtml 相同的 esc()，不得出现裸 HTML。
 */
"use strict";

const path = require("path");
const fs = require("fs");
const assert = require("assert");

const ROOT = path.join(__dirname, "..", "..");
const STATIC = path.join(ROOT, "poker_ocr", "workbench", "static");
const AdviceBounds = require(path.join(STATIC, "advice_bounds.js"));

/* 改动前的工作台卡片：代表一条「有原因、有计划、有画像、有模型元数据、有自动点击」的建议。
 * 它刻意不带 warnings——空列表时必须与改动前逐字相同。 */
const ADVICE_NO_WARNINGS = {
  street: "turn",
  option: { id: "call", label: "跟注", amount_bb: 12 },
  reason: "对手转牌持续下注频率偏高，跟注保留位置优势。",
  reason_full: "对手转牌持续下注频率偏高（42 手样本），跟注保留位置优势；\n加注会把中等强度成牌放进更窄的范围。",
  plan: "河牌白牌则过牌-跟注；高张则收紧。",
  facts: {
    hand_label: "顶对强踢",
    pot_type: "单挑 · 有位置",
    spr: 2.5,
    equity_pct: 63.4,
    opponents_in_hand: 1,
    range_verdict: "范围占优",
  },
  opponent_reads: [{ seat_id: "seat-3", archetype: "lag", display_name: "3 号位", confidence: 0.72 }],
  meta: { model: "deepseek-v4.1", duration_ms: 4200, confidence: 0.66,
          prompt_version: "v42", prompt_tokens: 1800, completion_tokens: 220 },
  action_index: 2,
  duration_ms: 4200,
  click: { status: "clicked", reason: "按建议点跟注" },
  source: "model",
};

const GOLDEN_CARD = `<div class="advice-card kind-call">
    <div class="advice-headline">
      <div>
        <div class="advice-kicker">本次建议 · 转牌</div>
        <div class="advice-action">跟注</div>
        <div class="advice-amount">目标 12 BB</div>
      </div>
      <div class="advice-time">4.2s</div>
    </div>
    
    
    <div class="advice-reason">
      <span class="advice-label">原因</span>
      <div>对手转牌持续下注频率偏高，跟注保留位置优势。</div>
      <details><summary>完整理由</summary><div>对手转牌持续下注频率偏高（42 手样本），跟注保留位置优势；
加注会把中等强度成牌放进更窄的范围。</div></details>
    </div>
    <div class="advice-plan"><b>下一街</b> 河牌白牌则过牌-跟注；高张则收紧。</div>
    <div class="read-chips"><span class="read-chip">牌力 顶对强踢</span><span class="read-chip">池型 单挑 · 有位置</span><span class="read-chip">SPR 2.5</span><span class="read-chip">成牌率 63%</span><span class="read-chip">在局 1 人</span><span class="read-chip">范围占优</span></div>
    <div class="read-chips"><span class="read-chip">3 号位 LAG 0.72</span></div>
    <div class="advice-meta">deepseek-v4.1 · 4.2s · 置信 0.66 · v42 · 1800+220 tok · 第 2 个动作后</div>
    <div class="alert info">自动点击：clicked · 按建议点跟注</div>
  </div>`;

function cloneAdvice() {
  return JSON.parse(JSON.stringify(ADVICE_NO_WARNINGS));
}

function withWarnings(warnings) {
  const advice = cloneAdvice();
  advice.facts.warnings = warnings;
  return advice;
}

function appSource() {
  return fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
}

/* 从 app.js 里抠出渲染函数与它的依赖，在 node 里当纯函数跑（不碰 DOM）。
 * 这些函数都不含正则里的花括号，按花括号配平切片足够；切完在 new Function 里编译，
 * 切坏了会直接抛错，不会静默跑偏。 */
function sliceFunction(text, name) {
  const sig = `function ${name}(`;
  const at = text.indexOf(sig);
  assert.ok(at >= 0, `app.js 应定义 ${name}()`);
  let i = text.indexOf("{", at);
  let depth = 0;
  for (; i < text.length; i += 1) {
    if (text[i] === "{") depth += 1;
    else if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(at, i + 1);
    }
  }
  assert.fail(`${name}() 的花括号没配平`);
}

function sliceConst(text, name) {
  const sig = `const ${name} = {`;
  const at = text.indexOf(sig);
  assert.ok(at >= 0, `app.js 应定义常量 ${name}`);
  let i = text.indexOf("{", at);
  let depth = 0;
  for (; i < text.length; i += 1) {
    if (text[i] === "{") depth += 1;
    else if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(at, i + 1);
    }
  }
  assert.fail(`常量 ${name} 的花括号没配平`);
}

function adviceRenderer(source) {
  const body = [
    sliceConst(source, "STREET_ZH"),
    sliceConst(source, "SKIP_ZH"),
    sliceFunction(source, "esc"),
    sliceFunction(source, "fmtNum"),
    sliceFunction(source, "streetZh"),
    sliceFunction(source, "optionKind"),
    sliceFunction(source, "adviceHtml"),
    "return adviceHtml;",
  ].join("\n");
  return new Function("AdviceBounds", body)(AdviceBounds);
}

/* 卡片自己的转义函数：作为「与 adviceHtml 同一套转义」的独立参照。 */
const APP_ESC = new Function(`return (${sliceFunction(appSource(), "esc")});`)();

/* 1. 空列表 / 缺字段：整段不出现，返回空串 */
{
  assert.strictEqual(AdviceBounds.warningsHtml([]), "", "空列表应整段不出现");
  assert.strictEqual(AdviceBounds.warningsHtml(undefined), "", "缺字段应整段不出现");
  assert.strictEqual(AdviceBounds.warningsHtml(null), "", "null 应整段不出现");
  assert.strictEqual(AdviceBounds.warningsHtml("不是数组"), "", "非数组按没有处理");
  assert.strictEqual(AdviceBounds.warningsHtml({ 0: "x" }), "", "对象不是列表，按没有处理");
}

/* 2. 单条 warning：一段、一行、带「数据边界」标签，文本原样 */
{
  const html = AdviceBounds.warningsHtml(["ante 是按桌配置假设来的"]);
  assert.ok(html.indexOf("数据边界") >= 0, "应出现「数据边界」标签");
  assert.ok(html.indexOf("<ul") >= 0, "应是列表结构");
  const items = html.match(/<li>[\s\S]*?<\/li>/g) || [];
  assert.strictEqual(items.length, 1, "一条 warning 应渲染一行");
  assert.strictEqual(items[0], "<li>ante 是按桌配置假设来的</li>");
}

/* 3. 多条 warning：行数 = 条数，顺序与列表一致 */
{
  const warnings = [
    "ante 是按桌配置假设来的（每人 1 BB），不是读到的",
    "有 2 家对手按随机牌补位，范围只作参考",
    "权益被降采样到 240 次蒙特卡洛，误差约 ±3%",
  ];
  const html = AdviceBounds.warningsHtml(warnings);
  const items = html.match(/<li>[\s\S]*?<\/li>/g) || [];
  assert.strictEqual(items.length, 3, "三条 warning 应渲染三行");
  warnings.forEach((w, i) => {
    assert.strictEqual(items[i], `<li>${w}</li>`, `第 ${i + 1} 行应等于第 ${i + 1} 条 warning`);
  });
  assert.ok(html.indexOf(warnings[0]) < html.indexOf(warnings[1]), "顺序应与列表一致（0 在 1 前）");
  assert.ok(html.indexOf(warnings[1]) < html.indexOf(warnings[2]), "顺序应与列表一致（1 在 2 前）");
}

/* 4. 含换行的长文案：仍是一条，换行保留在条目内（CSS pre-wrap 渲染成多视觉行） */
{
  const long = "权益被降采样：只跑了 240 次蒙特卡洛\n（换牌面重算过 3 次，误差约 ±3%，仅作参考）";
  const html = AdviceBounds.warningsHtml([long]);
  const items = html.match(/<li>[\s\S]*?<\/li>/g) || [];
  assert.strictEqual(items.length, 1, "带换行的文案仍是一条 warning 一行");
  assert.ok(items[0].indexOf("\n") >= 0, "换行应原样保留在同一个条目里，不被拆成两条");
  assert.ok(items[0].indexOf("只跑了 240 次蒙特卡洛") >= 0);
  assert.ok(items[0].indexOf("换牌面重算过 3 次") >= 0);
  const multi = AdviceBounds.warningsHtml(["第一行", "第二行\n续行", "第三行"]);
  assert.strictEqual((multi.match(/<li>/g) || []).length, 3, "混排时行数只按 warning 条数算");
}

/* 5. 不注入 HTML：warning 文本一律走 esc()，与 adviceHtml 其余字段同一套转义 */
{
  const evil = '<img src=x onerror="alert(1)"> & \'引号\' <script>alert(2)</script>';
  const html = AdviceBounds.warningsHtml([evil]);
  assert.ok(html.indexOf("<img") < 0, "不得出现裸 <img>");
  assert.ok(html.indexOf("<script") < 0, "不得出现裸 <script>");
  assert.ok(html.indexOf("&lt;img") >= 0, "尖括号应被转义");
  assert.ok(html.indexOf("&lt;script&gt;") >= 0, "script 标签应被整体转义");
  assert.ok(html.indexOf("&quot;") >= 0, "双引号应被转义");
  assert.ok(html.indexOf("&#39;") >= 0, "单引号应被转义");
  assert.ok(html.indexOf("&amp;") >= 0, "& 应被转义");

  // 默认转义必须与 app.js 的 esc 同一套规则：不传 esc 与传入 esc 的输出逐字相同。
  ["", "纯文本", "a&b<c>d\"e'f", evil].forEach((value) => {
    assert.strictEqual(AdviceBounds.warningsHtml([value]), AdviceBounds.warningsHtml([value], APP_ESC),
      `默认转义应与 app.js 的 esc 一致：${value}`);
  });
}

/* 6. 卡片级：空 / 缺 warning 列表时，输出与改动前逐字相同 */
{
  const render = adviceRenderer(appSource());
  assert.strictEqual(render(ADVICE_NO_WARNINGS), GOLDEN_CARD,
    "缺 warnings 字段时卡片应与改动前逐字相同");
  assert.strictEqual(render(withWarnings([])), GOLDEN_CARD,
    "warnings 为空列表时卡片应与改动前逐字相同");
  assert.strictEqual(render(ADVICE_NO_WARNINGS).indexOf("数据边界"), -1,
    "空列表时不应出现「数据边界」段");
}

/* 7. 卡片级：N 条 warning 渲染 N 行，位置在 advice-meta 之后、自动点击之前 */
{
  const render = adviceRenderer(appSource());
  const warnings = [
    "ante 是按桌配置假设来的（每人 1 BB），不是读到的",
    "有 2 家对手按随机牌补位，范围只作参考",
    "权益被降采样：只跑了 240 次蒙特卡洛\n（误差约 ±3%）",
    "在座不足 7 人，弃牌率未校准",
  ];
  const card = render(withWarnings(warnings));
  const items = card.match(/<li>[\s\S]*?<\/li>/g) || [];
  assert.strictEqual(items.length, warnings.length, "N 条 warning 应出现 N 行");
  warnings.forEach((w, i) => {
    assert.strictEqual(items[i], `<li>${APP_ESC(w)}</li>`,
      `第 ${i + 1} 行应等于第 ${i + 1} 条 warning`);
  });
  assert.ok(card.indexOf("数据边界") > card.indexOf("advice-meta"),
    "「数据边界」段应排在既有字段（advice-meta）之后");
  assert.ok(card.indexOf("数据边界") < card.indexOf("自动点击："),
    "「数据边界」段不得挤到自动点击提示之后");
  // 既有字段逐字不变：把新增段切掉，应与空列表的渲染完全相同。
  const start = card.indexOf('<div class="advice-bounds">');
  const end = card.indexOf("</ul></div>") + "</ul></div>".length;
  const withoutBounds = card.slice(0, start) + card.slice(end);
  assert.strictEqual(withoutBounds, GOLDEN_CARD,
    "去掉「数据边界」段后，卡片其余部分应与改动前逐字相同");
}

/* 8. 浏览器分支：没有 CommonJS 时挂到 window.AdviceBounds（node 测试走的是另一条分支） */
{
  const vm = require("vm");
  const sandbox = { window: {} };
  vm.runInNewContext(fs.readFileSync(path.join(STATIC, "advice_bounds.js"), "utf8"), sandbox);
  assert.strictEqual(typeof sandbox.window.AdviceBounds, "object",
    "浏览器里应挂到 window.AdviceBounds");
  assert.ok(sandbox.window.AdviceBounds.warningsHtml(["边界"]).indexOf("<li>边界</li>") >= 0,
    "浏览器分支拿到的函数应能正常渲染");
}

console.log("ok advice_bounds: 8 groups");
