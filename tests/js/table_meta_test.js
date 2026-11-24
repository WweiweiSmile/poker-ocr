/* 牌桌面板「N 人在局」：刻意抽成纯函数（table_meta.js），靠 node 断言，不需要浏览器。
 *
 * 口径来自 #121：
 *   · 优先读 players_not_folded（还没弃牌的人数，含 hero）；
 *   · 只有缺这个键的老记录才回落到 players_in_hand——旧口径显示，绝不覆盖新键；
 *   · 两个都缺 ⇒ 「—」，与改动前 st.players_in_hand ?? '—' 一致；
 *   · 0 是有效读数（不是「不知道」）；
 *   · 面板渲染点与变化指纹都必须接上这个模块，否则数字变了也不重画。
 */
"use strict";

const path = require("path");
const fs = require("fs");
const assert = require("assert");

const ROOT = path.join(__dirname, "..", "..");
const STATIC = path.join(ROOT, "poker_ocr", "workbench", "static");
const TableMeta = require(path.join(STATIC, "table_meta.js"));

function appSource() {
  return fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
}

/* 1. 新键优先：六家弃牌时面板要显示 hero + 2 = 3，而不是发牌人数 9 */
{
  assert.strictEqual(
    TableMeta.inHandLine("翻牌", { street: "flop", players_not_folded: 3, players_in_hand: 9 }),
    "翻牌 · 3 人在局",
    "应读 players_not_folded，不读发牌人数",
  );
  assert.strictEqual(
    TableMeta.inHandCount({ players_not_folded: 3, players_in_hand: 9 }), 3,
    "inHandCount 也应取在局人数",
  );
}

/* 2. 老记录（没有新键）才回落到 players_in_hand */
{
  assert.strictEqual(
    TableMeta.inHandLine("转牌", { players_in_hand: 6 }),
    "转牌 · 6 人在局",
    "缺新键时回落到旧字段",
  );
  assert.strictEqual(TableMeta.inHandCount({ players_in_hand: 6 }), 6);
}

/* 3. 两个都缺 ⇒ 「—」，与改动前一致 */
{
  assert.strictEqual(TableMeta.inHandLine("翻牌", {}), "翻牌 · — 人在局");
  assert.strictEqual(TableMeta.inHandLine("翻牌", null), "翻牌 · — 人在局");
  assert.strictEqual(TableMeta.inHandLine("翻牌", undefined), "翻牌 · — 人在局");
  assert.strictEqual(TableMeta.inHandCount(null), null);
  assert.strictEqual(TableMeta.inHandCount(undefined), null);
}

/* 4. 0 是有效读数，不是「不知道」 */
{
  assert.strictEqual(TableMeta.inHandLine("河牌", { players_not_folded: 0 }),
    "河牌 · 0 人在局");
  assert.strictEqual(TableMeta.inHandCount({ players_not_folded: 0 }), 0);
}

/* 5. 非数字（字符串 / NaN / 布尔）都当「不知道」，不渲染 "NaN 人在局" */
{
  assert.strictEqual(TableMeta.inHandCount({ players_not_folded: "3" }), null,
    "字符串不是读数");
  assert.strictEqual(TableMeta.inHandCount({ players_not_folded: NaN }), null, "NaN 不是读数");
  assert.strictEqual(
    TableMeta.inHandCount({ players_not_folded: null, players_in_hand: 4 }), 4,
    "新键存在但不可用时回落到旧字段",
  );
}

/* 6. 街道名可以为空（渲染调用方已保证有值，纯函数不编造街道） */
{
  assert.strictEqual(TableMeta.inHandLine("", { players_not_folded: 2 }), "2 人在局");
  assert.strictEqual(TableMeta.inHandLine(null, { players_not_folded: 2 }), "2 人在局");
}

/* 7. 接线：面板渲染点用这个模块，变化指纹带上新字段，index.html 先加载模块 */
{
  const app = appSource();
  assert.ok(app.indexOf("TableMeta.inHandLine(tblStreet(street), st)") >= 0,
    "app.js 的 hand-street 渲染应调用 TableMeta.inHandLine()");
  assert.ok(app.indexOf("st.players_in_hand ?? '—'") < 0,
    "app.js 不应再自己拼 players_in_hand 的人数文案");

  const sigAt = app.indexOf("function tableSignature(");
  assert.ok(sigAt >= 0, "app.js 应有 tableSignature()");
  const sigEnd = app.indexOf("\n}", sigAt);
  const sig = app.slice(sigAt, sigEnd);
  assert.ok(sig.indexOf("st.players_not_folded") >= 0,
    "变化指纹应包含 players_not_folded，人数变了要重画");

  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const metaAt = html.indexOf("/static/table_meta.js");
  const appAt = html.indexOf("/app.js");
  assert.ok(metaAt >= 0, "index.html 应加载 table_meta.js");
  assert.ok(metaAt < appAt, "table_meta.js 必须在 app.js 之前加载");
}

/* 8. 浏览器分支：没有 CommonJS 时挂到 window.TableMeta（node 测试走的是另一条分支） */
{
  const vm = require("vm");
  const sandbox = { window: {} };
  vm.runInNewContext(fs.readFileSync(path.join(STATIC, "table_meta.js"), "utf8"), sandbox);
  assert.strictEqual(typeof sandbox.window.TableMeta, "object",
    "浏览器里应挂到 window.TableMeta");
  assert.strictEqual(
    sandbox.window.TableMeta.inHandLine("翻牌", { players_not_folded: 3, players_in_hand: 9 }),
    "翻牌 · 3 人在局",
  );
}

console.log("ok table_meta: 8 groups");
