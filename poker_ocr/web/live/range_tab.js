/* 手牌详情第 5 个 tab：范围推导（trace 只读消费端）。
 *
 * 数据来源只有 GET /api/range?hand=<hand_index>：
 *   · 一次返回这手牌全部决策点（每条含全部座位），切座位/切步不再请求；
 *   · 记录形状见 docs/范围推导-trace-字段契约-v1.md（v1.2）；
 *   · 空态文案见 docs/看板-范围推导-信息架构.md §6。
 *
 * 所有来自日志的字符串都走 textContent，不拼 innerHTML。
 * 深链 #/range?hand=&street=&seat=&step=&cell=，交互走 replaceState。
 */
"use strict";

(function () {
  var RANKS = ["A", "K", "Q", "J", "T", "9", "8", "7", "6", "5", "4", "3", "2"];
  var SUITS = { h: "♥", d: "♦", c: "♣", s: "♠" };
  var STREET_ZH = { preflop: "翻前", flop: "翻牌", turn: "转牌", river: "河牌", showdown: "摊牌" };
  var ACTION_ZH = { fold: "弃牌", check: "过牌", call: "跟注", bet: "下注", raise: "加注", all_in: "全下", allin: "全下" };
  var GROUP_ZH = { value: "价值", semi_bluff: "半诈", bluff: "诈唬" };
  var MADE_ZH = { nut: "坚果", strong: "强", medium: "中", weak: "弱", air: "空气" };
  var DRAW_ZH = { combo: "组合听", flush: "花听", oesd: "两头顺", gutshot: "卡顺", backdoor: "后门", none: "无" };
  var REASON_ZH = {
    disabled: "这次 live 在决策点上关掉了 trace",
    board_short: "公共牌不足 3 张，没有翻后牌面",
    no_hero_cards: "没读到自己的底牌",
    no_specs: "没有可推的对手（都弃牌或不在局）",
    error: "内部异常，已 fail open",
  };
  var CUT_ZH = { dead: "删 dead", zero: "归零", floor: "剪除" };

  /* ───────── 状态 ───────── */

  var S = null;

  function newState(hand) {
    return {
      key: hand.uid || hand.hand_id || "",
      hand: hand,
      token: 0,
      phase: "loading",
      status: "",
      detail: "",
      records: [],
      idx: 0,
      seatId: null,
      step: -1,
      cell: "",
      want: null,
      pushed: false,
      loaded: false,
    };
  }

  function ensure(hand) {
    var key = hand.uid || hand.hand_id || "";
    if (!S || S.key !== key) S = newState(hand);
    return S;
  }

  function record() {
    if (!S || !S.records.length) return null;
    return S.records[S.idx] || null;
  }

  function seatRec() {
    var rec = record();
    var seats = (rec && rec.seats) || [];
    for (var i = 0; i < seats.length; i++) {
      if (seats[i].seat === S.seatId) return seats[i];
    }
    return seats[0] || null;
  }

  function actionsOf(rec) {
    return rec && rec.decision_key ? rec.decision_key.actions : null;
  }

  /* ───────── 小工具 ───────── */

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function num(value, digits) {
    if (value === null || value === undefined || value === "") return "—";
    var n = Number(value);
    if (!isFinite(n)) return String(value);
    return n.toFixed(digits === undefined ? 2 : digits);
  }

  function pct(value, digits) {
    var text = num(value, digits === undefined ? 1 : digits);
    return text === "—" ? text : text + "%";
  }

  function streetName(key) { return STREET_ZH[key] || key || "—"; }

  function posOf(rec, seat) {
    var seats = (rec && rec.seats) || [];
    for (var i = 0; i < seats.length; i++) {
      if (seats[i].seat === seat) return seats[i].pos || seat;
    }
    return seat || "";
  }

  function hasPostflop(hand) {
    var streets = (hand && hand.streets) || [];
    for (var i = 0; i < streets.length; i++) {
      var name = streets[i].street;
      if (name === "flop" || name === "turn" || name === "river" || name === "showdown") return true;
    }
    return false;
  }

  /* 169 键 ↔ 平铺下标：A K Q J T 9 8 7 6 5 4 3 2；
   * 对子在对角，同花在右上（行=高牌），非同花在左下（列=高牌）。 */
  function keyOfIndex(index) {
    if (index < 0 || index >= 169) return "";
    var row = Math.floor(index / 13);
    var col = index % 13;
    if (row === col) return RANKS[row] + RANKS[col];
    if (row < col) return RANKS[row] + RANKS[col] + "s";
    return RANKS[col] + RANKS[row] + "o";
  }

  function indexOfKey(key) {
    if (!key || key.length < 2) return -1;
    var a = RANKS.indexOf(key.charAt(0));
    var b = RANKS.indexOf(key.charAt(1));
    if (a < 0 || b < 0) return -1;
    if (key.length === 2 && a === b) return a * 13 + a;
    var kind = key.charAt(2);
    if (kind === "s" && a < b) return a * 13 + b;
    if (kind === "o" && a > b) return a * 13 + b;
    return -1;
  }

  function keyOfCombo(text) {
    var raw = String(text || "");
    if (raw.length < 4) return "";
    var r1 = raw.charAt(0).toUpperCase();
    var r2 = raw.charAt(2).toUpperCase();
    var s1 = raw.charAt(1).toLowerCase();
    var s2 = raw.charAt(3).toLowerCase();
    var i1 = RANKS.indexOf(r1);
    var i2 = RANKS.indexOf(r2);
    if (i1 < 0 || i2 < 0) return "";
    if (r1 === r2) return r1 + r2;
    var hi = i1 < i2 ? r1 : r2;
    var lo = i1 < i2 ? r2 : r1;
    return hi + lo + (s1 === s2 ? "s" : "o");
  }

  function cardChips(cards) {
    var wrap = el("span", "rt-cards");
    (cards || []).forEach(function (code) {
      var raw = String(code || "");
      var suit = raw.slice(-1).toLowerCase();
      var rank = raw.slice(0, -1).toUpperCase();
      if (rank === "10") rank = "T";
      var cls = "rt-card suit-" + suit;
      var chip = el("span", cls, rank + (SUITS[suit] || ""));
      wrap.append(chip);
    });
    return wrap;
  }

  function boardChips(text) {
    var cards = [];
    var raw = String(text || "");
    for (var i = 0; i + 1 < raw.length; i += 2) cards.push(raw.slice(i, i + 2));
    return cardChips(cards);
  }

  /* ───────── 取数 ───────── */

  function render(body, hand) {
    var s = ensure(hand);
    paint(body, hand);
    if (!s.loaded && s.phase === "loading") load(hand);
    if (!s.pushed) {
      s.pushed = true;
      if (!isRangeHash(location.hash)) {
        history.pushState(null, "", hashOf());
      }
    }
  }

  function load(hand) {
    var s = S;
    var token = ++s.token;
    s.phase = "loading";
    s.loaded = false;
    if (hand.hand_index === undefined || hand.hand_index === null) {
      s.loaded = true;
      s.phase = "empty";
      s.status = "none";
      paintModal();
      return;
    }
    if (!hasPostflop(hand)) {
      s.loaded = true;
      s.phase = "empty";
      s.status = "preflop";
      paintModal();
      return;
    }
    var url = "/api/range?hand=" + encodeURIComponent(hand.hand_index);
    fetch(url, { cache: "no-store" })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (token !== s.token) return;
        s.loaded = true;
        s.status = (data && data.status) || "missing";
        s.detail = (data && data.detail) || "";
        var records = (data && data.records) || [];
        if (records.length) {
          s.records = records;
          s.phase = "ready";
          pickDefault(s);
          applyWant(s);
        } else {
          s.phase = "empty";
        }
        paintModal();
      })
      .catch(function (err) {
        if (token !== s.token) return;
        s.loaded = true;
        s.phase = "error";
        s.detail = String((err && err.message) || err || "请求失败");
        paintModal();
      });
  }

  function pickDefault(s) {
    var idx = s.records.length - 1;
    for (var i = s.records.length - 1; i >= 0; i--) {
      var candidate = s.records[i];
      if (candidate && candidate.status === "ok" && candidate.seats && candidate.seats.length) {
        idx = i;
        break;
      }
    }
    s.idx = idx;
    var rec = s.records[idx];
    var seats = (rec && rec.seats) || [];
    var seat = null;
    for (var j = 0; j < seats.length; j++) {
      if (seats[j].steps && seats[j].steps.length) { seat = seats[j]; break; }
    }
    if (!seat) seat = seats[0] || null;
    s.seatId = seat ? seat.seat : null;
    s.step = -1;
    s.cell = "";
  }

  function applyWant(s) {
    var want = s.want;
    if (!want) return;
    s.want = null;
    if (want.street) {
      for (var i = 0; i < s.records.length; i++) {
        var candidate = s.records[i];
        if (!candidate || candidate.street !== want.street) continue;
        if (want.actions !== undefined && want.actions !== null && want.actions !== "") {
          if (String(actionsOf(candidate)) !== String(want.actions)) continue;
        }
        s.idx = i;
      }
    }
    var rec = s.records[s.idx];
    var seats = (rec && rec.seats) || [];
    var seat = null;
    if (want.seat) {
      for (var k = 0; k < seats.length; k++) {
        if (seats[k].seat === want.seat) { seat = seats[k]; }
      }
    }
    if (!seat) {
      for (var m = 0; m < seats.length; m++) {
        if (seats[m].steps && seats[m].steps.length) { seat = seats[m]; break; }
      }
    }
    if (!seat) seat = seats[0] || null;
    s.seatId = seat ? seat.seat : null;
    var maxStep = (seat && seat.steps) ? seat.steps.length : 0;
    var step = want.step === undefined || want.step === null || want.step === "" ? -1 : parseInt(want.step, 10);
    if (isNaN(step) || step < -1 || step > maxStep) step = -1;
    s.step = step;
    s.cell = want.cell && indexOfKey(want.cell) >= 0 ? want.cell : "";
  }

  function paintModal() {
    var body = document.getElementById("modal-body");
    if (!body || !S) return;
    if (!state.modal || state.modal.kind !== "hand" || state.modal.tab !== "range") return;
    paint(body, S.hand);
    syncHash(false);
  }

  function repaint() {
    paintModal();
  }

  function retry() {
    if (!S) return;
    S.loaded = false;
    S.phase = "loading";
    paintModal();
    load(S.hand);
  }

  /* ───────── 主渲染 ───────── */

  function paint(body, hand) {
    var s = ensure(hand);
    body.replaceChildren();
    var root = el("div", "rt");
    body.append(root);
    state.modal.copyText = "";
    if (s.phase === "loading") {
      root.append(el("div", "rt-note", "正在读取这手牌的推导记录…"));
      return;
    }
    if (s.phase === "error") {
      root.append(emptyBox("读取失败", s.detail || "接口没有返回有效数据。", retry));
      return;
    }
    if (s.phase === "empty") {
      root.append(emptyBoxFor(s));
      return;
    }
    var rec = record();
    if (!rec) {
      root.append(emptyBoxFor(s));
      return;
    }
    root.append(decisionBar(rec));
    if (rec.status !== "ok") {
      root.append(emptyBox(
        "这条决策点被跳过",
        REASON_ZH[rec.reason] || ("trace 记为 skipped：" + (rec.reason || "未知原因")),
        null,
        "事实视图（概览 / 行动线 / 数据质量）不受影响。"
      ));
      return;
    }
    root.append(adviceBar(hand, rec));
    var seats = rec.seats || [];
    if (!seats.length) {
      root.append(emptyBox(
        "这条记录没有座位范围",
        "trace 只写了决策点，没有可推的对手范围。",
        null,
        "事实视图不受影响。"
      ));
      return;
    }
    if (rec.hero_equity) root.append(equityBar(rec));
    root.append(mainGrid(rec));
    root.append(metaStrip(rec));
    alignRight();
  }

  /* 右栏两列：热图定宽、组合表吃剩余宽度；把表格 panel 的高度拉平到热图高度，
     表格内部滚动，底边与热图底边对齐（与已批准原型的 alignRight 一致）。 */
  function alignRight() {
    var right = document.querySelector(".rt-right");
    if (!right) return;
    var panels = right.children;
    if (!panels || panels.length < 2) return;
    var second = panels[1];
    if (window.innerWidth <= 1200) {
      second.style.height = "";
      second.style.maxHeight = "";
      return;
    }
    var first = panels[0].getBoundingClientRect().height;
    if (!first) return;
    second.style.height = Math.round(first) + "px";
    second.style.maxHeight = Math.round(first) + "px";
  }

  function emptyBox(title, text, retryFn, hint) {
    var box = el("div", "rt-empty");
    box.append(el("div", "rt-empty-title", title));
    box.append(el("div", "rt-empty-text", text || ""));
    if (hint) box.append(el("div", "rt-empty-hint", hint));
    if (retryFn) {
      var btn = el("button", "btn btn-small rt-empty-btn", "重试");
      btn.type = "button";
      btn.addEventListener("click", retryFn);
      box.append(btn);
    }
    return box;
  }

  function emptyBoxFor(s) {
    var status = s.status || "missing";
    if (status === "off") {
      return emptyBox("范围推导未开启", "本次 live 以 --no-range-trace 启动，没有采集逐步推导。", null,
        "事实视图（概览 / 行动线 / 数据质量 / 建议）不受影响。");
    }
    if (status === "budget_exceeded") {
      return emptyBox("已停写（超当日预算）", "今日 trace 超过 200 MB 日上限，之后的决策点不再落盘。", null,
        "重启或次日恢复。之前的记录仍可在日志里按 14 天保留。");
    }
    if (status === "write_failed") {
      return emptyBox("写盘失败", "trace 写入磁盘出错，已 fail open，不影响识别与建议。", null,
        "请检查 logs/ 目录权限与磁盘空间。");
    }
    if (status === "none") {
      return emptyBox("无推导（记录早于 trace）", "这条手牌没有 hand_index，无法与 trace 对应。", null,
        "旧记录不自动回填；事实视图保持不变。");
    }
    if (status === "preflop") {
      return emptyBox("翻前不做范围计算", "翻前由先验覆盖，trace 只记录翻后决策点。", null,
        "可以先看事实视图里的建议与行动线。");
    }
    return emptyBox("找不到这条推导记录", s.detail || "可能是 trace 上线前的旧记录，或日志已按 14 天 / 1 GB 轮转清掉。", retry,
      "事实视图不受影响。");
  }

  /* ───────── 顶部：决策点 / 建议 / 权益 ───────── */

  function decisionBar(rec) {
    var bar = el("div", "rt-bar");
    if (S.records.length > 1) {
      var chips = el("div", "rt-chips");
      S.records.forEach(function (item, index) {
        var label = streetName(item.street);
        var act = actionsOf(item);
        if (act !== null && act !== undefined) label += " · " + act + " 动作后";
        var chip = el("button", "rt-chip" + (index === S.idx ? " is-active" : ""), label);
        chip.type = "button";
        chip.title = (item.ts || "") + " · 底池 " + num(item.decision_key && item.decision_key.pot, 1) + " BB";
        chip.addEventListener("click", function () {
          S.idx = index;
          pickDefault(S);
          repaint();
        });
        chips.append(chip);
      });
      bar.append(chips);
    }
    var meta = el("div", "rt-bar-meta");
    var street = streetName(rec.street);
    meta.append(el("b", null, street));
    var key = rec.decision_key || {};
    if (key.actions !== null && key.actions !== undefined) meta.append(el("span", null, key.actions + " 个动作后"));
    meta.append(el("span", null, "底池 " + num(key.pot, 1) + " BB"));
    meta.append(el("span", null, "需跟 " + num(key.to_call, 1) + " BB"));
    if (rec.board) meta.append(el("span", "rt-bar-board"), boardChips(rec.board));
    var hero = rec.hero || {};
    if (hero.pos) meta.append(el("span", null, "我 " + hero.pos));
    if (hero.spr !== null && hero.spr !== undefined) meta.append(el("span", null, "SPR " + num(hero.spr, 2)));
    bar.append(meta);
    return bar;
  }

  function adviceFor(hand, rec) {
    var list = (state.advices || []).filter(function (a) {
      if (!a || a.hand_id !== hand.hand_id) return false;
      if (rec.street && a.street !== rec.street) return false;
      return true;
    });
    if (!list.length) return null;
    var key = rec.decision_key || {};
    var wantPot = Number(key.pot);
    var wantCall = Number(key.to_call);
    var best = null;
    var bestScore = Infinity;
    list.forEach(function (a) {
      var score = 0;
      if (isFinite(wantPot) && Number.isFinite(Number(a.pot))) score += Math.abs(Number(a.pot) - wantPot);
      if (isFinite(wantCall) && Number.isFinite(Number(a.to_call))) score += Math.abs(Number(a.to_call) - wantCall) * 2;
      if (rec.board && a.board && a.board.join("") !== rec.board) score += 5;
      if (score < bestScore) { bestScore = score; best = a; }
    });
    return best;
  }

  /* 「提示词 →」：优先跳到范围段第一行，没有范围段就跳弱点段；trace 只用于 deep 下钻。 */
  function promptFocus(advice, rec) {
    var refs = (advice && advice.prompt_refs) || [];
    var hit = null;
    for (var i = 0; i < refs.length; i++) {
      if (refs[i].section === "ranges") { hit = refs[i]; break; }
    }
    if (!hit) {
      for (var j = 0; j < refs.length; j++) {
        if (refs[j].section === "exploit") { hit = refs[j]; break; }
      }
    }
    if (!hit) return null;
    var trace = null;
    if (rec && (rec.seats || rec.hero_equity)) {
      trace = { seats: rec.seats || [], hero_equity: rec.hero_equity };
    }
    return { section: hit.section, line: hit.line, abs: hit.abs, trace: trace };
  }

  function adviceBar(hand, rec) {
    var box = el("div", "rt-advice");
    var advice = adviceFor(hand, rec);
    var head = el("div", "rt-advice-head");
    if (advice) {
      head.append(el("div", "rt-advice-label", optionAction(advice.option)));
      var reason = el("div", "rt-advice-reason", String(advice.reason || "").replace(/["'」]+$/, ""));
      head.append(reason);
      var meta = el("div", "rt-advice-meta", (advice.model || "model") + " · " + num(advice.duration_ms, 0) + " ms");
      if (advice.source && advice.source !== "model") meta.textContent += " · " + advice.source;
      head.append(meta);
      var jump = el("button", "btn btn-small rt-advice-jump", "提示词 →");
      jump.type = "button";
      jump.addEventListener("click", function () { openPrompt(advice, promptFocus(advice, rec)); });
      head.append(jump);
    } else {
      head.append(el("div", "rt-advice-label", "没有对应的建议记录"));
      head.append(el("div", "rt-advice-reason", "trace 在决策点写入；看板只保留本会话最近 30 条建议，更早的已被裁掉。"));
      var params = rec.params || {};
      head.append(el("div", "rt-advice-meta", "prompt " + (params.prompt_version || "—") + " · trace " + (rec.v || "—")));
    }
    box.append(head);
    return box;
  }

  function equityBar(rec) {
    var eq = rec.hero_equity || {};
    var box = el("div", "rt-panel rt-eq");
    var head = el("div", "rt-eq-head");
    var value = Number(eq.eq);
    var tie = Number(eq.now && eq.now.tie_any);
    if (!isFinite(tie)) tie = 0;
    var win = isFinite(value) ? value - tie / 2 : 0;
    if (win < 0) win = 0;
    var lose = isFinite(value) ? 1 - win - tie : 0;
    if (lose < 0) lose = 0;
    head.append(el("div", "rt-eq-big", pct(isFinite(value) ? value * 100 : null)));
    var side = el("div", "rt-eq-side");
    var ci = eq.ci95 || [];
    if (ci.length === 2) {
      side.append(el("span", null, "±" + num((Number(ci[1]) - Number(ci[0])) / 2 * 100, 1) + " pp · 95% CI"));
    }
    side.append(el("span", null, "平局 " + pct(tie * 100)));
    if (eq.now && eq.now.behind_any !== undefined) {
      side.append(el("span", "rt-eq-warn", "被至少一家压过 " + pct(Number(eq.now.behind_any) * 100)));
    }
    side.append(el("span", null, "MC " + num(eq.n_valid, 0) + " 次" + (eq.cache_hit ? " · 缓存命中" : "")));
    head.append(side);
    box.append(head);

    var bar = el("div", "rt-eqbar");
    var segs = [
      ["lose", lose],
      ["tie", tie],
      ["win", win],
    ];
    segs.forEach(function (pair) {
      var seg = el("div", "rt-eqseg " + pair[0]);
      seg.style.width = Math.max(0, Math.min(100, pair[1] * 100)) + "%";
      seg.title = pair[0] === "win" ? "赢 " + pct(pair[1] * 100) : pair[0] === "tie" ? "平局 " + pct(pair[1] * 100) : "输 " + pct(pair[1] * 100);
      bar.append(seg);
    });
    if (ci.length === 2) {
      [Number(ci[0]), Number(ci[1])].forEach(function (edge) {
        if (!isFinite(edge)) return;
        var whisk = el("div", "rt-whisk");
        whisk.style.left = Math.max(0, Math.min(100, edge * 100)) + "%";
        bar.append(whisk);
      });
    }
    box.append(bar);
    var legend = el("div", "rt-eq-legend");
    legend.append(el("span", null, "输 " + pct(lose * 100)));
    legend.append(el("span", null, "平 " + pct(tie * 100)));
    legend.append(el("span", null, "赢 " + pct(win * 100)));
    if (eq.samples_requested) legend.append(el("span", "rt-dim", "计划 " + eq.samples_requested + " 次采样"));
    box.append(legend);

    var seat = seatRec();
    if (seat && seat.equity) {
      var ex = el("div", "rt-eq-seat");
      ex.append(el("span", null, "对 " + (seat.pos || seat.seat) + "："));
      ex.append(el("span", null, "被压过 " + pct(Number(seat.equity.behind) * 100)));
      ex.append(el("span", null, "平局 " + pct(Number(seat.equity.tie) * 100)));
      if (seat.equity.removed) ex.append(el("span", "rt-dim", "删 " + seat.equity.removed + " 个已死组合"));
      box.append(ex);
    }
    return box;
  }

  /* ───────── 主网格 ───────── */

  function mainGrid(rec) {
    var grid = el("div", "rt-grid");
    var left = el("div", "rt-left");
    var right = el("div", "rt-right");
    left.append(timelinePanel(rec));
    left.append(overviewPanel(rec));
    right.append(heatmapPanel(rec));
    right.append(tablePanel(rec));
    grid.append(left, right);
    return grid;
  }

  function stepSelected(step) {
    return S.step === step;
  }

  function timelinePanel(rec) {
    var seat = seatRec();
    var panel = el("div", "rt-panel rt-tl-panel");
    var head = el("div", "rt-phead");
    var title = el("h4", null, "推导时间线");
    var sub = el("div", "rt-sub");
    var steps = (seat && seat.steps) || [];
    var params = rec.params || {};
    sub.textContent = steps.length ? steps.length + " 步 · α " + num(params.alpha_eff, 2) : "无逐步留痕";
    head.append(title, sub);
    panel.append(head);
    if (!seat) return panel;
    var list = el("div", "rt-tl");

    var prior = seat.prior || {};
    list.append(stepCard({
      step: 0,
      lab: "先验范围",
      right: "step 0",
      meta: (prior.id || "prior") + " · 宽度 " + pct(prior.width_pct) + " · " + (prior.combo_count || 0) + " 个组合" +
        (prior.source ? " · " + prior.source : ""),
      title: "翻前先验（before 任何翻后动作）",
    }));

    steps.forEach(function (step, index) {
      list.append(eventStepCard(rec, step, index));
    });

    if (seat.final) {
      list.append(finalCard(seat));
    }
    panel.append(list);
    if (seat.path === "board_range") {
      panel.append(el("div", "rt-hmnote", "这家走 board_range 旧口径：一条街一次过滤，没有「第 k 个事件」可拆；上面只是终局。"));
    }
    if (seat.detail_omitted && seat.detail_omitted.length) {
      panel.append(el("div", "rt-hmnote", "第 " + seat.detail_omitted.map(function (i) { return i + 1; }).join("、") + " 步的逐条明细因性能预算省略（宽度与热图仍然完整）。"));
    }
    return panel;
  }

  function stepCard(opts) {
    var card = el("div", "rt-step" + (stepSelected(opts.step) ? " is-active" : ""));
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.title = opts.title || "";
    var top = el("div", "rt-step-top");
    top.append(el("span", "rt-step-lab", opts.lab));
    top.append(el("span", "rt-step-z", opts.right));
    card.append(top);
    var meta = el("div", "rt-step-meta", opts.meta);
    card.append(meta);
    if (opts.extra) card.append(opts.extra);
    function choose() {
      S.step = opts.step;
      S.cell = "";
      repaint();
    }
    card.addEventListener("click", choose);
    card.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
    });
    return card;
  }

  function eventStepCard(rec, step, index) {
    var ev = step.event || {};
    var actor = posOf(rec, ev.actor) || ev.actor || "";
    var amount = (ev.amount === null || ev.amount === undefined) ? "" : " " + num(ev.amount, 1) + " BB";
    var allIn = ev.all_in ? " ALL-IN" : "";
    var lab = streetName(step.street) + " · " + actor + " " + (ACTION_ZH[ev.action] || ev.action || "") + amount + allIn;
    var bits = [];
    if (step.skipped) {
      bits.push("该步无牌面分类，跳过");
    } else {
      bits.push("宽度 " + pct(step.width_before) + " → " + pct(step.width_after));
    }
    var mass = Number(step.mass_after);
    if (isFinite(mass) && !step.skipped) bits.push("质量 " + num(mass, 3));
    var extra = droppedBar(step.dropped);
    return stepCard({
      step: index + 1,
      lab: lab,
      right: step.z_k === null || step.z_k === undefined ? "—" : "Z " + num(step.z_k, 2),
      meta: bits.join(" · "),
      extra: extra,
      title: "底池 " + num(ev.pot_before, 1) + " BB · 本街动作前 SPR " + num(ev.spr_before, 2) +
        " · α " + num(step.alpha_k, 2) + " · bucket " + (step.bucket || "—"),
    });
  }

  function finalCard(seat) {
    var fin = seat.final || {};
    var bits = ["宽度 " + pct(fin.width_pct), "有效 " + pct(fin.effective_width_pct)];
    if (fin.combo_count !== undefined) bits.push(fin.combo_count + " 个组合");
    if (fin.peak !== undefined) bits.push("峰值 " + num(fin.peak, 3));
    return stepCard({
      step: -1,
      lab: "留存量",
      right: "final",
      meta: bits.join(" · "),
      extra: droppedBar(fin.dropped),
      title: "留存量：所有事件之后、dead 与支撑集下限剪枝之后的范围（ρ " + num(fin.rho, 2) + "）",
    });
  }

  function droppedBar(dropped) {
    if (!dropped) return null;
    var dead = Number(dropped.dead) || 0;
    var zero = Number(dropped.zero) || 0;
    var floor = Number(dropped.floor) || 0;
    var total = dead + zero + floor;
    if (total <= 0) return null;
    var wrap = el("div", "rt-dropwrap");
    var bar = el("div", "rt-dbar");
    [["dead", dead], ["zero", zero], ["floor", floor]].forEach(function (pair) {
      if (!pair[1]) return;
      var seg = el("div", "rt-dseg " + pair[0]);
      seg.style.width = (pair[1] / total * 100) + "%";
      seg.title = CUT_ZH[pair[0]] + " " + pair[1];
      bar.append(seg);
    });
    wrap.append(bar);
    var legend = el("div", "rt-dlegend", "本步删 " + total + " 个组合：dead " + dead + " · 归零 " + zero + " · 剪除 " + floor);
    wrap.append(legend);
    return wrap;
  }

  function overviewPanel(rec) {
    var seats = rec.seats || [];
    var panel = el("div", "rt-panel rt-ov-panel");
    var head = el("div", "rt-phead");
    head.append(el("h4", null, "各家终局宽度"));
    head.append(el("div", "rt-sub", "横条=支撑集宽度，竖线=各步宽度"));
    panel.append(head);
    var scale = 0;
    seats.forEach(function (seat) {
      var width = Number(seat.final && seat.final.width_pct);
      if (isFinite(width) && width > scale) scale = width;
    });
    if (scale <= 0) scale = 1;
    var list = el("div", "rt-ov");
    seats.forEach(function (seat) {
      var row = el("div", "rt-ovrow" + (seat.seat === S.seatId ? " is-active" : ""));
      row.tabIndex = 0;
      var name = el("div", "rt-ovname", seat.pos || seat.seat || "?");
      var track = el("div", "rt-ovtrack");
      var width = Number(seat.final && seat.final.width_pct);
      var fill = el("div", "rt-ovfill" + (seat.path === "board_range" ? " legacy" : ""));
      fill.style.width = (isFinite(width) ? width / scale * 100 : 0) + "%";
      track.append(fill);
      (seat.steps || []).forEach(function (step, index) {
        var w = Number(step.width_after);
        if (!isFinite(w)) return;
        var tick = el("div", "rt-ovtick");
        tick.style.left = Math.max(0, Math.min(100, w / scale * 100)) + "%";
        tick.title = "第 " + (index + 1) + " 步 " + pct(w);
        track.append(tick);
      });
      var value = el("div", "rt-ovvalue", pct(width));
      if (seat.final && seat.final.effective_width_pct !== undefined) {
        value.title = "有效宽度 " + pct(seat.final.effective_width_pct) + " · 组合 " + (seat.final.combo_count || 0);
      }
      if (seat.path === "board_range") value.classList.add("legacy");
      row.append(name, track, value);
      row.addEventListener("click", function () {
        S.seatId = seat.seat;
        S.step = -1;
        S.cell = "";
        repaint();
      });
      list.append(row);
    });
    panel.append(list);
    var axis = el("div", "rt-ovax");
    axis.append(el("span", null, "0"));
    axis.append(el("span", null, pct(scale / 2)));
    axis.append(el("span", null, pct(scale)));
    panel.append(axis);
    return panel;
  }

  /* ───────── 当前视图（先验 / 第 k 步 / 留存量） ───────── */

  function currentView() {
    var seat = seatRec();
    if (!seat) return null;
    if (S.step === 0) {
      return {
        kind: "prior",
        label: "先验",
        grid: (seat.prior && seat.prior.grid) || [],
        width: seat.prior && seat.prior.width_pct,
        effective: seat.prior && seat.prior.weighted_width,
        comboCount: seat.prior && seat.prior.combo_count,
        step: null,
      };
    }
    if (S.step === -1) {
      return {
        kind: "final",
        label: "留存量",
        grid: (seat.final && seat.final.grid) || [],
        width: seat.final && seat.final.width_pct,
        effective: seat.final && seat.final.effective_width_pct,
        comboCount: seat.final && seat.final.combo_count,
        step: null,
      };
    }
    var step = (seat.steps || [])[S.step - 1];
    if (!step) return null;
    return {
      kind: "step",
      label: "第 " + S.step + " 步",
      grid: step.grid || [],
      width: step.width_after,
      effective: null,
      comboCount: null,
      step: step,
      stepIndex: S.step - 1,
    };
  }

  function cellShare(view) {
    var index = indexOfKey(S.cell);
    if (index < 0 || !view || !view.grid) return null;
    var value = Number(view.grid[index]);
    return isFinite(value) ? value : null;
  }

  /* ───────── 热图 ───────── */

  function heatmapPanel(rec) {
    var seat = seatRec();
    var view = currentView();
    var panel = el("div", "rt-panel rt-hm-panel");
    var head = el("div", "rt-phead");
    head.append(el("h4", null, "范围热图"));
    var sub = el("div", "rt-sub", (seat ? (seat.pos || seat.seat) : "—") + " · " + (view ? view.label : "—"));
    head.append(sub);
    panel.append(head);
    if (!view || !view.grid || !view.grid.length) {
      panel.append(el("div", "rt-note", "这一步没有热图（该步被跳过或没有留痕）。"));
      return panel;
    }
    var max = 0;
    for (var i = 0; i < view.grid.length; i++) {
      var v = Number(view.grid[i]);
      if (isFinite(v) && v > max) max = v;
    }
    var matrix = el("div", "rt-matrix");
    for (var row = 0; row < 13; row++) {
      for (var col = 0; col < 13; col++) {
        var index = row * 13 + col;
        var value = Number(view.grid[index]) || 0;
        var key = keyOfIndex(index);
        var cell = el("div", "rt-cell");
        if (value <= 0) {
          cell.className = "rt-cell zero";
          cell.title = key + " · 0%";
        } else {
          var t = max > 0 ? Math.sqrt(value / max) : 0;
          cell.style.background = "hsl(" + (18 + (1 - t) * 200) + ", " + (28 + t * 45) + "%, " + (12 + t * 46) + "%)";
          if (t > 0.12) cell.append(el("span", "rt-cell-lab", key));
          cell.title = key + " · 份额 " + pct(value * 100, 2);
          cell.tabIndex = 0;
          cell.addEventListener("click", function () {
            S.cell = S.cell === key ? "" : key;
            repaint();
          });
        }
        if (key === S.cell) cell.classList.add("on");
        matrix.append(cell);
      }
    }
    panel.append(matrix);

    var scale = el("div", "rt-scale");
    scale.append(el("span", null, "0"));
    scale.append(el("div", "rt-scale-bar"));
    scale.append(el("span", null, pct(max * 100, 1)));
    panel.append(scale);

    if (S.cell) {
      var share = cellShare(view);
      var chip = el("div", "rt-cellchip");
      var text = "格子 " + S.cell;
      if (share !== null) text += " · 占本步 " + pct(share * 100, 2);
      var count = cellCount(view);
      if (count !== null) text += " · 本格 " + count + " 个组合";
      chip.append(el("span", null, text));
      var clear = el("button", "rt-clear", "清除 ✕");
      clear.type = "button";
      clear.addEventListener("click", function () { S.cell = ""; repaint(); });
      chip.append(clear);
      panel.append(chip);
    }
    panel.append(el("div", "rt-hmnote", heatNote(view)));
    return panel;
  }

  function cellCount(view) {
    if (view.kind === "step" && view.step && view.step.cell_n) {
      var index = indexOfKey(S.cell);
      var n = Number(view.step.cell_n[index]);
      return isFinite(n) ? n : null;
    }
    return null;
  }

  function heatNote(view) {
    if (view.kind === "final") {
      return "留存量只落 169 格聚合（契约 v1.2）：份额在剪枝后的支撑集内归一化。逐 combo 明细看最后一步。";
    }
    if (view.kind === "prior") {
      return "先验范围（翻前表 + board_range 构造），还没有吃任何翻后动作。";
    }
    return "份额在「本步存活支撑集」内归一化；宽度变化见左栏时间线。";
  }

  /* ───────── 组合表 ───────── */

  function aggregateRows(grid) {
    var rows = [];
    for (var i = 0; i < 169; i++) {
      var value = Number(grid[i]) || 0;
      if (value > 0) rows.push({ key: keyOfIndex(i), share: value });
    }
    rows.sort(function (a, b) { return b.share - a.share; });
    return rows;
  }

  function stepRowMap(step) {
    var map = {};
    var order = [];
    (step.top || []).forEach(function (item) {
      if (!item || !item.c) return;
      map[item.c] = {
        c: item.c, key: keyOfCombo(item.c), dw: item.dw, w1: item.w1,
        l: item.l, m: item.m, d: item.d, group: item.group,
        made: item.made, draw: item.draw, cut: item.cut, share: null,
      };
      order.push(item.c);
    });
    (step.detail || []).forEach(function (line) {
      if (!line || !line.length) return;
      var combo = line[0];
      var item = map[combo];
      if (!item) {
        item = { c: combo, key: keyOfCombo(combo) };
        map[combo] = item;
        order.push(combo);
      }
      item.share = line[1];
      item.dw = line[2];
      item.l = line[3];
      item.cut = line[4];
      if (line.length >= 8) {
        item.group = line[5];
        item.made = line[6];
        item.draw = line[7];
      }
    });
    return order.map(function (combo) { return map[combo]; });
  }

  function changedCount(step) {
    var direct = 0;
    var others = 0;
    (step.top || []).forEach(function (item) {
      if (item && item.c) direct += 1;
      else if (item && item.others) others += Number(item.others.n) || 0;
    });
    return direct + others;
  }

  function tablePanel(rec) {
    var seat = seatRec();
    var view = currentView();
    var panel = el("div", "rt-panel rt-tb-panel");
    var head = el("div", "rt-phead");
    var title = el("h4", null, "组合明细");
    var meta = el("div", "rt-sub");
    head.append(title, meta);
    panel.append(head);
    if (!seat || !view) {
      panel.append(el("div", "rt-note", "没有可展示的明细。"));
      return panel;
    }
    if (view.kind === "prior") {
      var priorRows = aggregateRows(view.grid);
      meta.textContent = "先验 169 格聚合 · 按份额降序" + (S.cell ? " · 格子 " + S.cell : "");
      panel.append(renderAggregateTable(filterAggregate(priorRows), "先验只落 169 格，没有逐 combo 明细。"));
      return panel;
    }
    if (view.kind === "final") {
      if (S.cell) {
        var last = (seat.steps || [])[(seat.steps || []).length - 1];
        var rows = last ? stepRowMap(last) : [];
        if (last && last.detail && last.detail.length) {
          rows = rows.filter(function (row) { return row.share !== null && row.share !== undefined; });
        }
        var filtered = rows.filter(function (row) { return row.key === S.cell; });
        filtered.sort(function (a, b) { return Math.abs(Number(b.dw) || 0) - Math.abs(Number(a.dw) || 0); });
        meta.textContent = "格子 " + S.cell + " · 占留存量 " + pct((cellShare(view) || 0) * 100, 2) + " · 明细来自最后一步";
        panel.append(renderStepTable(filtered, false));
        panel.append(el("div", "rt-hmnote", "final 只落 169 格；这里的逐 combo 明细来自最后一步，floor 剪枝没有逐条落盘。"));
        return panel;
      }
      meta.textContent = "留存量 169 格聚合 · 按份额降序（逐 combo 明细看最后一步）";
      panel.append(renderAggregateTable(filterAggregate(aggregateRows(view.grid))));
      return panel;
    }
    var step = view.step;
    var all = stepRowMap(step);
    if (S.cell && step.detail && step.detail.length) {
      // 点格子问的是「这一格还剩谁」：口径要与 cell_n / 筛选 pill 一致（存活支撑集）。
      // top 榜里那几条归零 / 待删行是「本步怎么变的」，不是「本格还有谁」，不进筛选结果。
      all = all.filter(function (row) { return row.share !== null && row.share !== undefined; });
    }
    var cellFiltered = S.cell ? all.filter(function (row) { return row.key === S.cell; }) : all;
    var showAll = !!S.cell;
    var display = cellFiltered;
    if (!showAll) {
      var byCombo = {};
      (step.top || []).forEach(function (item) {
        if (item && item.c) byCombo[item.c] = true;
      });
      display = all.filter(function (row) { return byCombo[row.c]; });
      display = orderByDelta(display);
    } else {
      display = orderByDelta(cellFiltered);
    }
    if (step.truncated) {
      meta.textContent = (S.cell ? "格子 " + S.cell + " 的 " + display.length + " 条组合 · 占本步 " + pct((cellShare(view) || 0) * 100, 2) : "按 |Δw| 降序 · 共 " + changedCount(step) + " 个组合有变化") + " · 明细截断（每步 400 条）";
    } else if (S.cell) {
      meta.textContent = "格子 " + S.cell + " 的 " + display.length + " 条组合 · 占本步 " + pct((cellShare(view) || 0) * 100, 2);
    } else {
      meta.textContent = "按 |Δw| 降序 · 共 " + changedCount(step) + " 个组合有变化";
    }
    panel.append(renderStepTable(display, true));
    if (!S.cell) {
      panel.append(el("div", "rt-hmnote", "只列变化最大的组合。点热图格子可以看该格全部组合（含 L / M / D 乘数）。"));
    }
    if (step.detail_n === 0 && step.truncated === false) {
      panel.append(el("div", "rt-hmnote", "这一步的逐条明细因性能预算省略；变化的 top 榜仍在。"));
    }
    return panel;
  }

  function filterAggregate(rows) {
    if (!S.cell) return rows;
    return rows.filter(function (row) { return row.key === S.cell; });
  }

  function orderByDelta(rows) {
    var copy = rows.slice();
    copy.sort(function (a, b) {
      return Math.abs(Number(b.dw) || 0) - Math.abs(Number(a.dw) || 0);
    });
    return copy;
  }

  function renderAggregateTable(rows, note) {
    var wrap = el("div", "rt-tblwrap");
    var table = el("table", "rt-table");
    var thead = el("thead");
    var hr = el("tr");
    ["格子", "份额"].forEach(function (label) { hr.append(el("th", null, label)); });
    thead.append(hr);
    table.append(thead);
    var tbody = el("tbody");
    rows.slice(0, 169).forEach(function (row) {
      var tr = el("tr");
      tr.append(el("td", "rt-mono", row.key));
      var td = el("td", "num");
      td.textContent = pct(row.share * 100, 2);
      tr.append(td);
      tbody.append(tr);
    });
    table.append(tbody);
    wrap.append(table);
    if (note) wrap.append(el("div", "rt-hmnote", note));
    return wrap;
  }

  function renderStepTable(rows, allowExpand) {
    var wrap = el("div", "rt-tblwrap");
    if (!rows.length) {
      wrap.append(el("div", "rt-note", S.cell ? "这一格在这步没有留痕的组合。" : "这一步没有逐 combo 明细。"));
      return wrap;
    }
    var table = el("table", "rt-table");
    var thead = el("thead");
    var hr = el("tr");
    var columns = ["组合", "169", "成牌", "听牌", "权重", "Δw", "L", "M/D", "处置"];
    columns.forEach(function (label) { hr.append(el("th", null, label)); });
    thead.append(hr);
    table.append(thead);
    var tbody = el("tbody");
    rows.slice(0, 400).forEach(function (row) {
      var tr = el("tr");
      tr.append(el("td", "rt-mono", row.c));
      tr.append(el("td", "rt-mono rt-dim", row.key));
      tr.append(el("td", null, MADE_ZH[row.made] || row.made || "—"));
      tr.append(el("td", null, DRAW_ZH[row.draw] || row.draw || "—"));
      var share = row.share !== null && row.share !== undefined ? pct(Number(row.share) * 100, 2) : (row.w1 !== undefined ? pct(Number(row.w1) * 100, 2) : "—");
      tr.append(el("td", "num", share));
      tr.append(el("td", "num", row.dw === null || row.dw === undefined ? "—" : (Number(row.dw) * 100).toFixed(2)));
      tr.append(el("td", "num", row.l === null || row.l === undefined ? "—" : num(row.l, 2)));
      var md = (row.m === null || row.m === undefined ? "—" : num(row.m, 2)) + " / " + (row.d === null || row.d === undefined ? "—" : num(row.d, 2));
      tr.append(el("td", "num", md));
      var cut = row.cut ? el("span", "rt-tag cut-" + row.cut, CUT_ZH[row.cut] || row.cut) : el("span", "rt-dim", "—");
      var cutCell = el("td");
      cutCell.append(cut);
      tr.append(cutCell);
      tr.title = "组 " + (GROUP_ZH[row.group] || row.group || "—") + " · 权重 " + num(row.w1, 6);
      if (allowExpand) {
        tr.classList.add("rt-clickrow");
        tr.addEventListener("click", function () { toggleRowDetail(tr, row, columns.length); });
      }
      tbody.append(tr);
    });
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  function toggleRowDetail(tr, row, cols) {
    var next = tr.nextElementSibling;
    if (next && next.classList.contains("rt-xrow")) {
      next.remove();
      return;
    }
    var x = el("tr", "rt-xrow");
    var td = el("td");
    td.colSpan = cols;
    td.textContent = JSON.stringify(row);
    x.append(td);
    tr.after(x);
  }

  /* ───────── 记录元信息 ───────── */

  function metaStrip(rec) {
    var strip = el("div", "rt-meta");
    var params = rec.params || {};
    var tables = params.tables || {};
    var bits = [];
    bits.push("trace " + (rec.v || "—"));
    bits.push("会话 " + String(rec.session || "—").slice(0, 12));
    if (rec.ts) bits.push(rec.ts);
    if (tables.profile) bits.push("profile " + tables.profile + (tables.variant ? "/" + tables.variant : ""));
    if (params.alpha_eff !== undefined) bits.push("α " + num(params.alpha_eff, 3));
    if (params.support_floor !== undefined) bits.push("floor " + num(params.support_floor, 4));
    if (params.prompt_version) bits.push("prompt " + params.prompt_version);
    if (rec.omitted) bits.push("省略 " + rec.omitted + " 步明细");
    strip.textContent = bits.join(" · ");
    var warnings = rec.warnings || [];
    if (warnings.length) {
      var line = el("div", "rt-warn", "warnings: " + warnings.join(" · "));
      var box = el("div", "rt-meta-wrap");
      box.append(strip, line);
      return box;
    }
    return strip;
  }

  /* ───────── 深链 ───────── */

  function isRangeHash(hash) {
    return typeof hash === "string" && hash.indexOf("#/range") === 0;
  }

  function hashOf() {
    if (!S || !S.hand) return "";
    var parts = ["#/range?hand=" + encodeURIComponent(S.hand.hand_index === undefined ? "" : S.hand.hand_index)];
    var rec = record();
    if (rec && rec.street) parts.push("street=" + encodeURIComponent(rec.street));
    if (S.seatId) parts.push("seat=" + encodeURIComponent(S.seatId));
    parts.push("step=" + S.step);
    if (S.cell) parts.push("cell=" + encodeURIComponent(S.cell));
    return parts.join("&");
  }

  function syncHash(push) {
    if (!S || !S.loaded || !record()) return;
    var want = hashOf();
    if (!want || want === location.hash) return;
    if (push) history.pushState(null, "", want);
    else history.replaceState(null, "", want);
  }

  function parseHash(hash) {
    if (!isRangeHash(hash)) return null;
    var raw = hash.slice("#/range".length);
    if (raw.charAt(0) === "?") raw = raw.slice(1);
    var out = {};
    raw.split("&").forEach(function (pair) {
      if (!pair) return;
      var index = pair.indexOf("=");
      if (index < 0) return;
      out[decodeURIComponent(pair.slice(0, index))] = decodeURIComponent(pair.slice(index + 1));
    });
    return out;
  }

  function restore() {
    var info = parseHash(location.hash);
    if (!info || !info.hand) return false;
    var hands = state.hands || [];
    var found = null;
    for (var i = 0; i < hands.length; i++) {
      if (String(hands[i].hand_index) === String(info.hand)) { found = hands[i]; break; }
    }
    if (!found) return false;
    S = newState(found);
    S.want = info;
    state.modal = { kind: "hand", payload: found, tab: "range" };
    renderModal();
    return true;
  }

  window.addEventListener("hashchange", function () {
    if (isRangeHash(location.hash)) {
      restore();
    } else if (state.modal && state.modal.kind === "hand" && state.modal.tab === "range") {
      closeModal();
    }
  });

  window.addEventListener("resize", function () {
    if (S && S.phase === "ready") alignRight();
  });

  window.RangeTab = { render: render, restore: restore, isRangeHash: isRangeHash };
})();
