/* 牌桌面板「N 人在局」与座位弃牌三态：刻意抽成纯函数（table_meta.js），靠 node 断言，不需要浏览器。
 *
 * 口径来自 #121 + #123：
 *   · 人数优先读 players_in_play（确定在局），未知另报 players_fold_unknown；
 *   · 老记录回落 players_not_folded（未知算在局），再回落 players_in_hand；
 *   · 未知数只作后缀「（N 未知）」，绝不加进人数；两个都缺 ⇒ 「—」；
 *   · 0 是有效读数（不是「不知道」）；
 *   · 座位弃牌三态 seatFold()：fold 为 null 就是「未知」，不拿动作徽章或负证据补；
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

/* 9. 未知单独计数：在局人数读 players_in_play，未知跟在后缀里，不混进人数（#123） */
{
  assert.strictEqual(
    TableMeta.inHandLine("翻牌", {
      players_in_play: 3, players_not_folded: 5, players_fold_unknown: 2,
    }),
    "翻牌 · 3 人在局（2 未知）",
    "未知要单独报，且不计入在局人数",
  );
  assert.strictEqual(
    TableMeta.inHandCount({ players_in_play: 3, players_not_folded: 5, players_in_hand: 9 }), 3,
    "inHandCount 应优先读 players_in_play",
  );
  assert.strictEqual(
    TableMeta.inHandLine("转牌", { players_not_folded: 5, players_fold_unknown: 0 }),
    "转牌 · 5 人在局",
    "未知为 0 时不加后缀",
  );
  assert.strictEqual(
    TableMeta.inHandLine("转牌", { players_not_folded: 5 }),
    "转牌 · 5 人在局",
    "老记录没有未知键时照旧显示",
  );
  assert.strictEqual(
    TableMeta.inHandLine("转牌", { players_not_folded: 5, players_fold_unknown: NaN }),
    "转牌 · 5 人在局",
    "未知不是有限数字就不加后缀",
  );
  assert.strictEqual(
    TableMeta.inHandLine("转牌", { players_not_folded: 5, players_fold_unknown: 2 }),
    "转牌 · 5 人在局",
    "人数回落到 players_not_folded 时不再报未知，否则同一批人算两遍",
  );
  assert.strictEqual(
    TableMeta.inHandCount({ players_in_play: null, players_not_folded: 4 }), 4,
    "新键不可用时回落到 players_not_folded",
  );
}

/* 10. 座位弃牌三态：未知不再画成在局（#123） */
{
  assert.strictEqual(TableMeta.seatFold(null), "empty", "没有玩家是空位");
  assert.strictEqual(TableMeta.seatFold(undefined), "empty");
  assert.strictEqual(TableMeta.seatFold({ fold: true }), "folded");
  assert.strictEqual(TableMeta.seatFold({ status: "folded" }), "folded");
  assert.strictEqual(
    TableMeta.seatFold({ left: true, fold: false, status: "playing" }), "folded",
    "离桌必然已弃牌",
  );
  assert.strictEqual(
    TableMeta.seatFold({ left_uncertain: true, fold: null, status: "uncertain" }),
    "uncertain", "暂离（#124）不是弃牌，也不是在局",
  );
  assert.strictEqual(
    TableMeta.seatFold({ status: "uncertain" }), "uncertain",
    "只有 status 也要认得暂离",
  );
  assert.strictEqual(TableMeta.seatFold({ status: "away" }), "empty", "空座不是玩家状态");
  assert.strictEqual(TableMeta.seatFold({ status: "sitting_out" }), "empty");
  assert.strictEqual(TableMeta.seatFold({ status: "buying_in" }), "empty");
  assert.strictEqual(TableMeta.seatFold({ in_hand: false, fold: null, status: "playing" }), "empty",
    "本手没发到牌的座位不是「未知」，它压根不在这手里");
  assert.strictEqual(TableMeta.seatFold({ fold: false, status: "playing" }), "playing");
  assert.strictEqual(TableMeta.seatFold({ fold: null, status: "playing" }), "unknown",
    "弃牌没读到 = 未知，不许当成在局");
  assert.strictEqual(TableMeta.seatFold({ status: "playing" }), "unknown",
    "老记录缺 fold 键同样是未知，不猜");
  assert.strictEqual(
    TableMeta.seatFold({ fold: null, status: "playing", action: "raise" }), "unknown",
    "动作徽章会淡出，不拿它补弃牌状态（否则计数随动画抖动）",
  );
}

/* 11. 接线：座位渲染用三态、指纹带新计数、CSS 有未知样式 */
{
  const app = appSource();
  assert.ok(app.indexOf("TableMeta.seatFold(player)") >= 0,
    "renderSeats 应通过 TableMeta.seatFold() 判三态");
  assert.ok(app.indexOf("'未知'") >= 0, "未知座位要显示「未知」");
  assert.ok(app.indexOf("is-unknown") >= 0, "未知座位要有 is-unknown 类");

  const sigAt = app.indexOf("function tableSignature(");
  const sigEnd = app.indexOf("\n}", sigAt);
  const sig = app.slice(sigAt, sigEnd);
  assert.ok(sig.indexOf("st.players_in_play") >= 0,
    "变化指纹应包含 players_in_play，在局人数变了要重画");
  assert.ok(sig.indexOf("st.players_fold_unknown") >= 0,
    "变化指纹应包含 players_fold_unknown，未知数变了也要重画");

  const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");
  assert.ok(css.indexOf(".seat.is-unknown") >= 0, "CSS 应有 .seat.is-unknown");
  assert.ok(css.indexOf(".seat-unknown") >= 0, "CSS 应有 .seat-unknown 徽章");
}

console.log("ok table_meta: 11 groups");
