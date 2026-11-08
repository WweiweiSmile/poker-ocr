/* 实时看板前端：纯原生 JS，无构建、无 CDN。
 *
 * 数据来源只有一个：SSE `/api/events`。
 *   · 首条消息是 snapshot（整页水合）；
 *   · 之后是 frame / decision / advice / hand / status … 增量事件。
 * 所有来自日志的字符串（玩家名、理由、提示词）都走 textContent，
 * 不拼 innerHTML，避免把玩家名当成标签执行。
 */
"use strict";

const SUITS = { h: "♥", d: "♦", c: "♣", s: "♠" };
const STREET_ZH = {
  preflop: "翻前", flop: "翻牌", turn: "转牌", river: "河牌", showdown: "摊牌",
};
const ACTION_ZH = {
  fold: "弃牌", check: "过牌", call: "跟注", bet: "下注", raise: "加注",
  all_in: "全下", allin: "全下", open: "开池",
};
const STATUS_ZH = {
  playing: "在局", folded: "已弃牌", buying_in: "带入中",
  sitting_out: "暂离", away: "空位",
};

const state = {
  session: {},
  layout: null,
  stats: {},
  frame: null,
  table: null,
  pending: null,
  advice: null,
  advices: [],
  hands: [],
  feed: [],
  endedAt: null,
  connected: false,
  pendingSince: null,
  modal: null,
  feedPaused: false,
  lastCardKey: "",
};

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fmt(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  const rounded = n.toFixed(digits);
  return rounded.replace(/\.0+$/, "").replace(/(\.\d*[1-9])0+$/, "$1");
}

function pct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${Math.round(n)}%`;
}

function clamp(n, lo, hi) { return Math.min(hi, Math.max(lo, n)); }

function timeOf(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(-13, -5);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function clockOf(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const s = Math.max(0, Number(seconds));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}

function streetFromBoard(n) {
  return n === 3 ? "flop" : n === 4 ? "turn" : n === 5 ? "river" : "preflop";
}

function streetLabel(street) {
  return STREET_ZH[street] || street || "—";
}

function actionLabel(action) {
  return ACTION_ZH[action] || action || "";
}

function cardNode(code, extra = "") {
  const node = h("div", `card ${extra}`.trim());
  const raw = String(code || "").trim();
  if (!raw || raw === "?" || raw === "??") {
    node.classList.add("card-back");
    return node;
  }
  const suit = raw.slice(-1).toLowerCase();
  let rank = raw.slice(0, -1);
  if (rank === "T") rank = "10";
  const suitClass = { s: "suit-s", h: "suit-h", c: "suit-c", d: "suit-d" }[suit];
  if (suitClass) node.classList.add(suitClass);
  node.append(h("div", "rank", rank || "?"), h("div", "suit", SUITS[suit] || ""));
  return node;
}

function cardsKey(cards) { return (cards || []).join(","); }

function setCards(container, cards, extra = "") {
  container.replaceChildren();
  (cards || []).forEach((code) => container.append(cardNode(code, extra)));
}

/* ───────────────────────── 顶栏 / 统计 ───────────────────────── */

function renderSession() {
  const s = state.session || {};
  $("#conn").textContent = state.connected ? "实时" : "重连中…";
  $("#conn").className = `pill ${state.connected ? "pill-live" : "pill-off"}`;
  $("#session-id").textContent = s.session_id || s.id || "—";
  $("#profile-name").textContent = s.profile || "—";
  $("#prompt-version").textContent = s.prompt_version
    ? `prompt ${s.prompt_version}` : "prompt —";
}

function renderStats() {
  const st = state.stats || {};
  const sampled = Number(st.sampled || 0);
  const accepted = Number(st.accepted || 0);
  const discard = sampled > 0 ? (sampled - accepted) / sampled * 100 : 0;
  const adviceDone = Number(st.advice_done || 0);
  $("#stat-frames").textContent = st.processed ?? 0;
  $("#stat-accepted").textContent = accepted;
  $("#stat-discard").textContent = sampled > 0 ? pct(discard) : "—";
  $("#stat-proc").textContent = st.avg_proc_ms ? fmt(st.avg_proc_ms, 0) : "—";
  $("#stat-advice").textContent = adviceDone;
  const adviceBadge = $("#advice-source");
  if (adviceDone && st.fallbacks) {
    adviceBadge.title = `本次会话兜底 ${st.fallbacks} 次`;
  }
}

/* ───────────────────────── 牌桌 ───────────────────────── */

function tableState() { return state.table && state.table.state ? state.table.state : null; }

function heroSeatId() {
  return state.layout?.hero_seat_id || tableState()?.hero_seat_id || "";
}

function findPlayer(seatId) {
  const st = tableState();
  if (!st) return null;
  if (st.hero && st.hero.seat_id === seatId) return st.hero;
  return (st.opponents || []).find((p) => p.seat_id === seatId) || null;
}

function heroPlayer() {
  const st = tableState();
  if (!st) return null;
  return st.hero || (st.opponents || []).find((p) => p.seat_id === st.hero_seat_id) || null;
}

function seatPositions() {
  const st = tableState();
  const players = st ? [st.hero, ...(st.opponents || [])].filter(Boolean) : [];
  const layout = state.layout;
  let seats = [];
  const heroId = heroSeatId();
  if (layout && Array.isArray(layout.seats) && layout.seats.length) {
    seats = layout.seats
      // hero 已有底部大面板；环上不再画一次。除了 is_hero，还按 seat_id 兜底，
      // 防止旧布局/自定义 profile 漏标 is_hero 时出现两个 hero。
      .filter((s) => !s.is_hero && s.seat_id !== heroId)
      .map((s, i) => ({ ...s, index: i }));
  } else {
    seats = players.map((p, i) => {
      const n = Math.max(1, players.length);
      const angle = (Math.PI / 2) + (i * 2 * Math.PI / n);
      return {
        seat_id: p.seat_id,
        is_hero: p.seat_id === st.hero_seat_id,
        center: [0.5 + 0.42 * Math.cos(angle), 0.46 + 0.36 * Math.sin(angle)],
        index: i,
      };
    }).filter((s) => !s.is_hero);
  }
  // full 路线的 seat_id 不一定在 profile 里：把状态里多出来的玩家补到环上，
  // 否则他们只存在于 JSON 里、看板看不到。
  const seen = new Set(seats.map((s) => s.seat_id));
  const extras = players.filter((p) => p.seat_id !== heroId && !seen.has(p.seat_id));
  const n = Math.max(1, seats.length + extras.length);
  extras.forEach((p, i) => {
    const angle = (Math.PI / 2) + ((seats.length + i) * 2 * Math.PI / n);
    seats.push({
      seat_id: p.seat_id,
      is_hero: false,
      center: [0.5 + 0.42 * Math.cos(angle), 0.46 + 0.36 * Math.sin(angle)],
      index: seats.length,
    });
  });
  return seats;
}

function playerActionText(player) {
  let action = player.all_in ? "all_in" : player.action;
  if (!action && (player.fold || player.status === "folded")) action = "fold";
  if (player.left) return "离桌";
  if (!action) return "";
  const amount = Number(player.bet);
  const withAmount = ["call", "bet", "raise", "all_in"].includes(action)
    && Number.isFinite(amount) && amount > 0;
  return `${actionLabel(action)}${withAmount ? ` ${fmt(amount)}` : ""}`;
}

function renderSeats() {
  const root = $("#seats");
  root.replaceChildren();
  const st = tableState();
  if (!st) return;
  const dealer = st.dealer;
  const heroId = heroSeatId();
  for (const seat of seatPositions()) {
    if (seat.seat_id === heroId) continue;
    const player = findPlayer(seat.seat_id);
    const node = h("div", "seat");
    if (!player) node.classList.add("is-empty");
    if (player && (player.fold === true || player.status === "folded")) node.classList.add("is-folded");
    if (player && player.left) node.classList.add("is-left");
    if (player && (player.status === "away" || player.status === "sitting_out" || player.status === "buying_in")) {
      node.classList.add("is-empty");
    }
    if (st.hero_turn === true && player && player.seat_id === st.hero_seat_id) node.classList.add("is-turn");

    const top = h("div", "seat-top");
    top.append(h("span", "seat-name", player?.name || (player ? seat.seat_id : "空位")));
    if (player?.position) top.append(h("span", "seat-pos", player.position));
    node.append(top);

    if (player) {
      const stack = player.stack;
      // 后手和本街下注并排，少占一行；座位之间留出更多纵向空间。
      const info = h("div", "seat-info");
      info.append(h("span", "seat-stack", stack === null || stack === undefined ? "— BB" : `${fmt(stack)} BB`));
      if (player.bet) info.append(h("span", "seat-bet", `本街 ${fmt(player.bet)}`));
      node.append(info);
      const actionText = playerActionText(player);
      if (actionText) {
        const action = player.left ? "fold" : (player.all_in ? "all_in" : (player.action || "fold"));
        const cls = String(action).replace(/[^a-z_]/gi, "");
        node.classList.add(`act-${cls}`);
        node.append(h("span", `seat-action action-${cls}`, actionText));
      }
      const shown = (st.shows || []).find((x) => x.seat_id === seat.seat_id);
      if (shown && Array.isArray(shown.cards) && shown.cards.length) {
        const row = h("div", "seat-show");
        for (const code of shown.cards) row.append(cardNode(code));
        node.append(row);
      }
    } else {
      node.append(h("div", "seat-stack", "—"));
    }

    if (dealer && seat.seat_id === dealer) {
      node.append(h("span", "dealer-chip", "D"));
    }

    const rawCx = Array.isArray(seat.center) ? seat.center[0] : 0.5;
    const rawCy = Array.isArray(seat.center) ? seat.center[1] : 0.3;
    // 把座位整体向桌沿外推，给中间「底池 + 下注条 + 公共牌」留出安全区。
    // 左侧/右侧三张座位按纵向拉开；顶部两席再上移，避免压住底池。
    const cx = 0.5 + (rawCx - 0.5) * 1.12;
    let cy = rawCy;
    if (rawCy < 0.2) {
      cy = rawCy * 0.75;
    } else if (rawCy > 0.2 && rawCy < 0.75) {
      cy = 0.5 + (rawCy - 0.5) * 1.18;
    }
    node.style.left = `${clamp(cx * 100, 8, 92)}%`;
    node.style.top = `${clamp(cy * 100, 9, 78)}%`;
    root.append(node);
  }
}

function renderTable() {
  const st = tableState();
  const frame = state.frame || {};
  const empty = $("#table-empty");
  if (!st) {
    empty.hidden = false;
    $("#pot").textContent = "—";
    $("#street-bet-total").textContent = "本街 0 BB";
    $("#bet-strip").replaceChildren();
    $("#board").replaceChildren();
    $("#hero-panel").classList.add("is-empty");
    $("#hero-cards").replaceChildren();
    $("#hand-street").textContent = "等待一手牌";
    $("#hand-id").textContent = "—";
    $("#cards-badge").textContent = "牌面未知";
  } else {
    empty.hidden = true;
    $("#pot").textContent = fmt(st.pot);
    const streetBet = Number(st.street_bet_total || 0);
    $("#street-bet-total").textContent = `本街 ${fmt(streetBet)} BB`;
    const boardKey = cardsKey(st.board);
    if (boardKey !== state.lastBoardKey) {
      state.lastBoardKey = boardKey;
      setCards($("#board"), st.board);
    }
    const hero = heroPlayer();
    // 本街下注直接汇总到池子旁边：谁下了多少一眼可见，不用逐个座位找。
    const betRoot = $("#bet-strip");
    betRoot.replaceChildren();
    const bettors = [hero, ...(st.opponents || [])]
      .filter((p) => p && Number(p.bet) > 0)
      .sort((a, b) => Number(b.bet) - Number(a.bet));
    if (bettors.length) {
      for (const p of bettors) {
        const chip = h("span", `bet-chip${p.seat_id === st.hero_seat_id ? " is-hero" : ""}`);
        chip.title = `${p.name || p.position || p.seat_id} 本街已投入 ${fmt(p.bet)} BB`;
        chip.append(h("b", null, p.name || p.position || p.seat_id || "?"),
                   h("i", null, `${fmt(p.bet)}`));
        betRoot.append(chip);
      }
    } else {
      betRoot.append(h("span", "bet-empty", "本街暂无下注"));
    }
    const panel = $("#hero-panel");
    const heroCardsKey = cardsKey(st.hero_cards);
    if (heroCardsKey !== state.lastHeroCardsKey) {
      state.lastHeroCardsKey = heroCardsKey;
      setCards($("#hero-cards"), st.hero_cards);
    }
    panel.classList.toggle("is-empty", !hero);
    panel.classList.toggle("is-turn", st.hero_turn === true);
    const heroIsDealer = Boolean(st.dealer && hero && st.dealer === hero.seat_id);
    $("#hero-position").textContent = `${hero?.position || st.hero_position || "—"}${heroIsDealer ? " · D" : ""}`;
    $("#hero-name").textContent = hero?.name || "hero";
    $("#hero-turn").hidden = st.hero_turn !== true;
    $("#hero-stack").textContent = hero?.stack === null || hero?.stack === undefined
      ? "— BB" : `${fmt(hero.stack)} BB`;
    $("#hero-bet").textContent = hero?.bet ? `本街 ${fmt(hero.bet)} BB` : "";

    const blinds = st.blinds;
    $("#blinds").textContent = blinds
      ? `盲注 ${fmt(blinds.sb)}/${fmt(blinds.bb)}${blinds.ante ? ` + 前注 ${fmt(blinds.ante)}` : ""}${blinds.source === "carried" ? "（沿用）" : ""}`
      : "盲注 —";

    const street = frame.hand?.street || streetFromBoard((st.board || []).length);
    $("#hand-street").textContent = `${streetLabel(street)} · ${st.players_in_hand ?? "—"} 人在局`;
    $("#hand-id").textContent = frame.hand_id || frame.hand?.hand_id || "—";

    const cardsOk = st.cards_reliable !== false;
    const warnings = (st.cards_warnings || []).length;
    const badge = $("#cards-badge");
    badge.textContent = cardsOk ? (warnings ? `牌面 ${warnings} 条警告` : "牌面可靠") : "牌面不可靠";
    badge.className = `pill ${cardsOk ? (warnings ? "pill-warn" : "pill-good") : "pill-bad"}`;
    $("#table-foot").textContent = [
      blinds ? `盲注 ${fmt(blinds.sb)}/${fmt(blinds.bb)}` : null,
      `庄位 ${st.dealer || "—"}`,
      `本街下注 ${fmt(st.street_bet_total ?? 0)} BB`,
      st.pot_before_street !== null && st.pot_before_street !== undefined
        ? `上一街底池 ${fmt(st.pot_before_street)}` : null,
    ].filter(Boolean).join("　·　");
  }

  const f = state.frame;
  if (f) {
    const kind = f.kind || "?";
    const errs = (f.errors || []).length;
    const stamp = f.ts ? timeOf(f.ts) : clockOf(f.t);
    $("#frame-meta").textContent = `帧 #${f.seq ?? "—"} · ${kind}${errs ? ` · ${errs} 个字段错误` : ""} · ${fmt(f.proc_ms, 0)}ms · ${stamp}`;
  } else {
    $("#frame-meta").textContent = "—";
  }
}

function renderHand() {
  const frame = state.frame || {};
  const hand = frame.hand || null;
  const quality = $("#hand-quality");
  const cardsOk = tableState()?.cards_reliable !== false;
  const warnings = (tableState()?.cards_warnings || []).length;
  quality.textContent = cardsOk ? (warnings ? `牌面警告 ${warnings}` : "牌面可靠") : "牌面不可靠";
  quality.className = `pill ${cardsOk ? (warnings ? "pill-warn" : "pill-good") : "pill-bad"}`;
  $("#hand-pot").textContent = hand?.pot !== undefined && hand?.pot !== null
    ? `底池 ${fmt(hand.pot)} BB` : "";

  const root = $("#hand-actions");
  root.replaceChildren();
  if (!hand || !Array.isArray(hand.actions) || !hand.actions.length) {
    root.append(h("div", "placeholder", hand
      ? "这一手还没有抓到行动；轮到我或对手行动后会逐步出现。"
      : "还没有行动线。轮到我时，建议会出现在右侧。"));
  } else {
    const streets = ["preflop", "flop", "turn", "river", "showdown"];
    const grouped = new Map();
    for (const a of hand.actions) {
      const key = a.street || hand.street || "preflop";
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(a);
    }
    const ordered = [...streets.filter((s) => grouped.has(s)),
                     ...[...grouped.keys()].filter((s) => !streets.includes(s))];
    for (const street of ordered) {
      const block = h("div", "street-block");
      block.append(h("div", "street-label", streetLabel(street)));
      for (const a of grouped.get(street)) {
        const row = h("div", `action-row${a.is_hero ? " is-hero" : ""}`);
        const who = h("b");
        who.textContent = a.name || a.position || a.seat_id || "—";
        row.append(who);
        const label = h("span");
        label.textContent = `${actionLabel(a.action)}${a.amount ? ` ${fmt(a.amount)} BB` : ""}`;
        if (a.inferred) {
          const tag = h("span", "inferred-tag", "推断");
          label.append(tag);
        }
        row.append(label);
        row.append(h("span", "amount", a.all_in ? "ALL-IN" : ""));
        block.append(row);
      }
      root.append(block);
    }
  }

  renderHandList();
}

function renderHandList() {
  const root = $("#hand-list");
  root.replaceChildren();
  if (!state.hands.length) {
    root.append(h("div", "placeholder", "暂无已结束手牌"));
    return;
  }
  for (const hand of state.hands.slice(0, 8)) {
    const q = hand.data_quality || {};
    const status = hand.partial ? "partial" : (q.status || "exact");
    const item = h("button", "hand-item");
    item.type = "button";
    item.append(h("span", `hand-dot ${status}`, ""));
    const main = h("span");
    main.append(h("div", "hand-id-small", hand.hand_id || "—"));
    const meta = h("div", "hand-time",
      `${(hand.hero_cards || []).join(" ") || "无手牌"} · ${(hand.board || []).join(" ") || "翻前"}${q.inferred_actions ? ` · 推断 ${q.inferred_actions}` : ""}`);
    main.append(meta);
    item.append(main);
    item.append(h("span", "hand-time", status === "exact" ? "可靠" : status === "approx" ? "近似" : "截断"));
    item.addEventListener("click", () => openHand(hand));
    root.append(item);
  }
}

/* ───────────────────────── 建议 ───────────────────────── */

function optionKind(option) {
  const kind = String(option?.kind || option?.id || "").toLowerCase();
  if (kind.includes("all")) return "allin";
  if (kind.includes("fold")) return "fold";
  if (kind.includes("check")) return "check";
  if (kind.includes("call")) return "call";
  if (kind.includes("bet")) return "bet";
  if (kind.includes("raise") || kind.includes("open")) return "raise";
  return "call";
}

function optionAction(option) {
  return option?.label || ACTION_ZH[option?.kind] || option?.id || "—";
}

function renderAdvice() {
  const body = $("#advice-body");
  const actions = $("#advice-actions");
  const source = $("#advice-source");
  const streetEl = $("#advice-street");
  body.replaceChildren();
  actions.hidden = true;
  streetEl.textContent = "";

  if (state.pending) {
    renderPending(body);
    source.textContent = "生成中";
    source.className = "pill pill-warn";
    streetEl.textContent = streetLabel(state.pending.street);
    return;
  }

  const a = state.advice;
  if (!a) {
    source.textContent = "等待";
    source.className = "pill pill-dim";
    const empty = h("div", "advice-empty");
    empty.append(h("div", "advice-empty-icon", "♣"));
    empty.append(h("div", null, "等待轮到我"));
    empty.append(h("p", null, "触发决策点后，建议、原因、计划与完整提示词都会在这里出现。"));
    body.append(empty);
    return;
  }

  const kind = optionKind(a.option);
  const fallback = a.source && a.source !== "model";
  source.textContent = fallback ? "兜底" : "模型";
  source.className = `pill ${fallback ? "pill-warn" : "pill-good"}`;
  streetEl.textContent = streetLabel(a.street);

  const card = h("div", "advice-card");

  const headline = h("div", `advice-headline kind-${kind}`);
  const left = h("div");
  left.append(h("div", "advice-kicker", "本次建议"));
  left.append(h("div", "advice-action", optionAction(a.option)));
  if (a.option?.amount_bb !== null && a.option?.amount_bb !== undefined) {
    left.append(h("div", "advice-amount", `目标 ${fmt(a.option.amount_bb)} BB`));
  }
  headline.append(left);
  const right = h("div", "advice-time");
  right.append(h("div", null, streetLabel(a.street)));
  right.append(h("div", null, a.duration_ms ? `${(a.duration_ms / 1000).toFixed(1)}s` : ""));
  headline.append(right);
  card.append(headline);

  if (fallback) {
    const alert = h("div", "alert");
    alert.append(h("b", null, "模型不可用，已按保守动作兜底。"));
    if (a.error) alert.append(h("span", null, a.error));
    card.append(alert);
  }
  if (a.prompt_missing) {
    const reasons = {
      no_settings: "未配置 AI_SERVER_* 凭据",
      abnormal_no_opponents: "状态帧异常（在局对手为 0），未调用模型",
    };
    card.append(h("div", "alert warn", `未调用模型：${reasons[a.prompt_missing_reason] || a.prompt_missing_reason || "未知原因"}`));
  }

  const reason = h("div", "reason-block");
  reason.append(h("div", "reason-label", "原因"));
  reason.append(h("div", "reason-text", a.reason || "（模型未给理由）"));
  if (a.reason_full && a.reason_full !== a.reason) {
    const details = h("details");
    details.append(h("summary", "reason-full", "查看完整理由"));
    details.append(h("div", "reason-full", a.reason_full));
    reason.append(details);
  }
  card.append(reason);

  if (a.plan) {
    const plan = h("div", "plan-block");
    plan.append(h("b", null, "下一街计划"));
    plan.append(h("span", null, a.plan));
    card.append(plan);
  }

  const facts = a.facts || {};
  const chips = [];
  if (facts.hand_label) chips.push(`牌力 ${facts.hand_label}`);
  if (facts.pot_type) chips.push(`池型 ${facts.pot_type}`);
  if (facts.spr !== undefined && facts.spr !== null) chips.push(`SPR ${fmt(facts.spr)}`);
  if (facts.equity_pct !== undefined && facts.equity_pct !== null) chips.push(`成牌率 ${fmt(facts.equity_pct, 1)}%`);
  if (facts.opponents_in_hand !== undefined) chips.push(`在局 ${facts.opponents_in_hand} 人`);
  if (facts.range_verdict) chips.push(facts.range_verdict);
  if (chips.length) {
    const row = h("div", "read-chips");
    for (const text of chips) row.append(h("span", "read-chip", text));
    card.append(row);
  }

  const reads = (a.opponent_reads || []).filter((r) => r.archetype && r.archetype !== "unknown");
  if (reads.length) {
    const row = h("div", "read-chips");
    for (const r of reads) {
      row.append(h("span", "read-chip",
        `${r.display_name || r.key || r.seat_id || "?"} ${String(r.archetype).toUpperCase()} ${fmt(r.confidence, 2)}`));
    }
    card.append(row);
  }

  const meta = h("div", "meta-grid");
  const items = [
    ["模型", a.model || "—"],
    ["耗时", a.duration_ms ? `${(a.duration_ms / 1000).toFixed(1)}s` : "—"],
    ["置信度", a.confidence !== undefined && a.confidence !== null ? fmt(a.confidence, 2) : "—"],
    ["提示词", a.prompt_version || "—"],
    ["tokens", a.prompt_tokens !== undefined ? `${a.prompt_tokens} + ${a.completion_tokens ?? "?"}` : "—"],
    ["决策点", a.action_index !== undefined ? `第 ${a.action_index} 个动作后` : "—"],
  ];
  for (const [label, value] of items) {
    const item = h("div", "meta-item");
    item.append(h("span", null, label), h("span", null, value));
    meta.append(item);
  }
  card.append(meta);

  if (a.click) {
    const status = a.click.status || "?";
    card.append(h("div", `alert ${status === "clicked" || status === "dry_run" ? "info" : "warn"}`,
      `自动点击：${status}${a.click.reason ? ` · ${a.click.reason}` : ""}${a.click.preset ? ` · ${a.click.preset}` : ""}`));
  }

  body.append(card);
  actions.hidden = false;
}

function renderPending(body) {
  const p = state.pending;
  const box = h("div", "pending-box");
  const head = h("div", "pending-head");
  head.append(h("span", "spinner", ""));
  head.append(h("span", "pending-title", "AI 正在思考…"));
  const elapsed = h("span", "pending-elapsed", "0s");
  elapsed.id = "pending-elapsed";
  head.append(elapsed);
  box.append(head);

  const facts = h("div", "pending-facts");
  const rows = [
    ["底池", p.pot !== undefined ? `${fmt(p.pot)} BB` : "—"],
    ["需跟", p.to_call !== undefined ? `${fmt(p.to_call)} BB` : "—"],
    ["可选动作", Array.isArray(p.options) ? `${p.options.length}` : (p.options ?? "—")],
  ];
  for (const [label, value] of rows) {
    const item = h("div", "pending-fact");
    item.append(h("b", null, value), h("span", null, label));
    facts.append(item);
  }
  box.append(facts);
  box.append(h("div", "pending-note",
    "提示词会在模型返回后展示，与本次实际调用完全一致；等待期间识别主循环会暂停，避免拿旧画面做决策。"));
  body.append(box);
  updatePendingElapsed();
}

function updatePendingElapsed() {
  const el = $("#pending-elapsed");
  if (!el || !state.pendingSince) return;
  el.textContent = `${Math.max(0, (Date.now() - state.pendingSince) / 1000).toFixed(1)}s`;
}

function renderHistory() {
  const root = $("#history-list");
  root.replaceChildren();
  $("#history-count").textContent = String(state.advices.length);
  $("#history-count").title = `${state.advices.length} 条建议`;
  if (!state.advices.length) {
    root.append(h("div", "placeholder", "本次会话还没有建议"));
    return;
  }
  for (const a of state.advices.slice(0, 20)) {
    const kind = optionKind(a.option);
    const dot = a.source && a.source !== "model" ? "fallback" : kind;
    const item = h("button", "history-item");
    item.type = "button";
    item.append(h("span", `history-dot ${dot}`, ""));
    const main = h("span", "history-main");
    main.append(h("div", "history-action", optionAction(a.option)));
    main.append(h("div", "history-sub",
      `${streetLabel(a.street)} · ${(a.hero_cards || []).join(" ") || "?"} · ${(a.board || []).join(" ") || "翻前"}`));
    item.append(main);
    item.append(h("span", "history-time", a.duration_ms ? `${(a.duration_ms / 1000).toFixed(1)}s` : "—"));
    item.title = a.reason || "";
    item.addEventListener("click", () => {
      state.pending = null;
      state.advice = a;
      renderAdvice();
    });
    root.append(item);
  }
}

/* ───────────────────────── 识别健康 / 帧流 ───────────────────────── */

function currentIssues() {
  const notes = [];
  const frame = state.frame || {};
  const st = tableState();
  const errors = frame.errors || [];
  if (errors.length) {
    notes.push({ level: "bad", text: `本帧 ${errors.length} 个字段读取失败：${errors.slice(0, 3).join("；")}` });
  }
  for (const warning of (frame.cards?.warnings || []).slice(0, 3)) {
    notes.push({ level: "warn", text: `读牌：${warning}` });
  }
  if (st?.cards_reliable === false) {
    notes.push({ level: "bad", text: "当前牌面不可靠，本帧不会给 AI 建议" });
  }
  for (const warning of (st?.cards_warnings || []).slice(0, 3)) {
    notes.push({ level: "warn", text: `牌面：${warning}` });
  }
  for (const warning of (st?.name_warnings || []).slice(0, 2)) {
    notes.push({ level: "warn", text: `名字：${warning}` });
  }
  const reasons = state.stats.discarded_reasons || {};
  const topReasons = Object.entries(reasons)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 3);
  if (topReasons.length) {
    notes.push({ level: "warn", text: `丢弃集中在：${topReasons.map(([k, v]) => `${k}=${v}`).join("  ")}` });
  }
  const hand = state.hands[0];
  const q = hand?.data_quality || {};
  if (hand) {
    for (const warning of (q.warnings || []).slice(0, 2)) {
      notes.push({ level: "warn", text: `上一手 ${hand.hand_id}：${warning}` });
    }
    if (q.inferred_actions) {
      notes.push({ level: "warn", text: `上一手有 ${q.inferred_actions} 条推断动作` });
    }
  }
  return notes;
}

function renderHealth() {
  const st = state.stats || {};
  const sampled = Number(st.sampled || 0);
  const accepted = Number(st.accepted || 0);
  const acceptRate = sampled > 0 ? accepted / sampled * 100 : 0;
  const cardsOk = tableState()?.cards_reliable !== false;
  const adviceDone = Number(st.advice_done || 0);
  const fallbacks = Number(st.fallbacks || 0);
  const modelRate = adviceDone > 0 ? Math.max(0, (adviceDone - fallbacks) / adviceDone * 100) : 0;
  const notes = currentIssues();

  const body = $("#health-body");
  body.replaceChildren();
  const rows = [
    ["接受率", acceptRate, acceptRate >= 70 ? "" : acceptRate >= 30 ? "warn" : "bad"],
    ["当前牌面", cardsOk ? 100 : 0, cardsOk ? "" : "bad"],
    ["建议模型率", adviceDone ? modelRate : 100, modelRate >= 90 ? "" : modelRate >= 60 ? "warn" : "bad"],
  ];
  for (const [label, value, cls] of rows) {
    const row = h("div", "health-row");
    row.append(h("span", null, label));
    const bar = h("div", `bar ${cls}`.trim());
    const fill = h("i");
    fill.style.width = `${clamp(value, 0, 100)}%`;
    bar.append(fill);
    row.append(bar);
    row.append(h("span", null, pct(value)));
    body.append(row);
  }
  if (notes.length) {
    const list = h("div", "health-notes");
    for (const note of notes.slice(0, 5)) {
      list.append(h("div", `health-note ${note.level === "bad" ? "bad" : ""}`.trim(), note.text));
    }
    body.append(list);
  } else {
    body.append(h("div", "placeholder", "当前没有识别告警；丢弃原因会出现在帧流里。"));
  }

  const level = $("#health-level");
  const bad = notes.some((n) => n.level === "bad") || (sampled > 0 && acceptRate < 30);
  const warn = notes.length > 0 || (sampled > 0 && acceptRate < 70);
  level.textContent = bad ? "需排查" : warn ? "有告警" : "正常";
  level.className = `pill ${bad ? "pill-bad" : warn ? "pill-warn" : "pill-good"}`;
  const dot = $("#health-dot");
  if (dot) dot.className = `tab-dot ${bad ? "bad" : warn ? "warn" : "good"}`;
}

function feedText(item) {
  const s = item.summary || {};
  const type = item.type;
  if (type === "frame") {
    const kind = s.kind || "?";
    const errs = (s.errors || []);
    const tail = errs.length ? ` · ${errs[0]}${errs.length > 1 ? ` 等 ${errs.length} 项` : ""}` : "";
    return `#${s.seq ?? "—"} ${kind} · ${fmt(s.proc_ms, 0)}ms${s.hand_id ? ` · ${s.hand_id}` : ""}${tail}`;
  }
  if (type === "decision") return `决策点 · ${streetLabel(s.street)} · 底池 ${fmt(s.pot)} · 需跟 ${fmt(s.to_call)}`;
  if (type === "advice_skip") return `跳过建议 · ${s.reason || "?"}${(s.warnings || []).length ? ` · ${(s.warnings || [])[0]}` : ""}`;
  if (type === "advice") return `${streetLabel(s.street)} · ${s.action || "?"} · ${s.source || "?"}${s.error ? ` · ${s.error}` : ""}`;
  if (type === "hand") return `${s.hand_id || "?"} · ${s.status || "?"}${s.partial ? " · 截断" : ""}${s.inferred_actions ? ` · 推断 ${s.inferred_actions}` : ""}`;
  if (type === "incident") return `疑惑帧 · ${s.reason || "?"}`;
  if (type === "status") return `采样 ${s.sampled ?? "—"} · 接受 ${s.accepted ?? "—"} · ${fmt(s.avg_proc_ms, 0)}ms`;
  if (type === "click") return `点击 · ${s.click?.status || "?"}`;
  return type;
}

function renderFeed() {
  const root = $("#feed-list");
  root.replaceChildren();
  const list = state.feed.slice(-120);
  for (const item of list) {
    const row = h("div", "feed-row");
    row.title = feedText(item);
    row.append(h("span", "feed-time", timeOf(item.ts)));
    const type = item.type === "frame" ? (item.summary?.kind || "frame") : item.type;
    row.append(h("span", `feed-kind ${type}`, item.type === "frame" ? type : item.type));
    row.append(h("span", "feed-detail", feedText(item)));
    root.append(row);
  }
  if (!state.feedPaused) root.scrollTop = root.scrollHeight;
}

function pushFeed(item) {
  if (!item || state.feed.some((e) => e.id === item.id)) return;
  state.feed.push(item);
  if (state.feed.length > 150) state.feed.shift();
  const root = $("#feed-list");
  const row = h("div", "feed-row");
  row.title = feedText(item);
  row.append(h("span", "feed-time", timeOf(item.ts)));
  const type = item.type === "frame" ? (item.summary?.kind || "frame") : item.type;
  row.append(h("span", `feed-kind ${type}`, item.type === "frame" ? type : item.type));
  row.append(h("span", "feed-detail", feedText(item)));
  root.append(row);
  while (root.children.length > 120) root.firstChild.remove();
  if (!state.feedPaused) root.scrollTop = root.scrollHeight;
}

/* ───────────────────────── 弹窗 ───────────────────────── */

function promptTabs(advice) {
  const tabs = [];
  if (advice.prompt_system) tabs.push(["system", "System 提示词"]);
  if (advice.prompt_user) tabs.push(["user", "User 事实块"]);
  tabs.push(["facts", "事实快照"]);
  if (advice.raw_response) tabs.push(["raw", "模型原始返回"]);
  if (advice.model_params && Object.keys(advice.model_params).length) tabs.push(["params", "采样参数"]);
  return tabs;
}

function openPrompt(advice) {
  if (!advice) return;
  const tabs = promptTabs(advice);
  state.modal = { kind: "prompt", payload: advice, tab: tabs[0]?.[0] || "facts" };
  renderModal();
}

function handAdvices(hand) {
  return state.advices.filter((a) => a.hand_id && a.hand_id === hand.hand_id);
}

function openHand(hand) {
  state.modal = { kind: "hand", payload: hand, tab: "overview" };
  renderModal();
}

function closeModal() {
  state.modal = null;
  $("#modal").hidden = true;
}

function modalDef() {
  if (!state.modal) return null;
  if (state.modal.kind === "prompt") {
    const a = state.modal.payload;
    return {
      title: "本次建议的提示词",
      sub: `${streetLabel(a.street)} · ${a.hand_id || "—"} · ${a.prompt_sha256 ? a.prompt_sha256.slice(0, 12) : "no prompt hash"}`,
      tabs: promptTabs(a),
      foot: a.prompt_missing
        ? `未调用模型：${a.prompt_missing_reason || "unknown"}`
        : `prompt ${a.prompt_version || "—"} · ${(a.prompt_system || "").length + (a.prompt_user || "").length} 字符`,
    };
  }
  const hand = state.modal.payload;
  return {
    title: "手牌详情",
    sub: `${hand.hand_id || "—"} · ${(hand.hero_cards || []).join(" ") || "无手牌"} · ${(hand.board || []).join(" ") || "翻前"}`,
    tabs: [["overview", "概览"], ["actions", "行动线"], ["quality", "数据质量"],
           ...(hand.advices?.length ? [["advices", `建议 ${hand.advices.length}`]] : [])],
    foot: `uid ${hand.uid || hand.hand_id || "—"} · 倒查：python tools/hand_recognition_audit.py --explain ${hand.hand_id || "<hand_id>"}`,
  };
}

function renderModal() {
  const modal = $("#modal");
  const def = modalDef();
  if (!def) { modal.hidden = true; return; }
  modal.hidden = false;
  $("#modal-title").textContent = def.title;
  $("#modal-sub").textContent = def.sub;
  $("#modal-foot").textContent = def.foot;
  const tabs = $("#modal-tabs");
  tabs.replaceChildren();
  for (const [key, label] of def.tabs) {
    const btn = h("button", `tab${state.modal.tab === key ? " is-active" : ""}`, label);
    btn.type = "button";
    btn.addEventListener("click", () => { state.modal.tab = key; renderModal(); });
    tabs.append(btn);
  }
  const body = $("#modal-body");
  body.replaceChildren();
  state.modal.copyText = "";
  if (state.modal.kind === "prompt") {
    renderPromptBody(body, state.modal.payload, state.modal.tab);
  } else {
    renderHandBody(body, state.modal.payload, state.modal.tab);
  }
}

function preBlock(text) {
  const pre = h("pre");
  pre.textContent = text || "（空）";
  state.modal.copyText = text || "";
  return pre;
}

function renderPromptBody(body, a, tab) {
  if (tab === "system" || tab === "user") {
    const text = tab === "system" ? a.prompt_system : a.prompt_user;
    if (!text) {
      body.append(h("div", "placeholder", "这次调用没有捕获到提示词。"));
      return;
    }
    body.append(preBlock(text));
    return;
  }
  if (tab === "raw") {
    let text = a.raw_response || "";
    try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (_) { /* 原样展示 */ }
    body.append(preBlock(text));
    return;
  }
  if (tab === "params") {
    body.append(preBlock(JSON.stringify(a.model_params || {}, null, 2)));
    return;
  }
  const facts = a.facts || {};
  body.append(preBlock(JSON.stringify(facts, null, 2)));
}

function kvBlock(pairs) {
  const dl = h("dl", "kv");
  for (const [key, value] of pairs) {
    dl.append(h("dt", null, key));
    dl.append(h("dd", null, value === null || value === undefined ? "—" : String(value)));
  }
  return dl;
}

function renderHandBody(body, hand, tab) {
  if (tab === "overview") {
    const q = hand.data_quality || {};
    state.modal.copyText = JSON.stringify(hand, null, 2);
    body.append(kvBlock([
      ["hand_id", hand.hand_id],
      ["hero", (hand.hero_cards || []).join(" ") || "—"],
      ["board", (hand.board || []).join(" ") || "翻前"],
      ["位置", hand.hero_position],
      ["底池", hand.pot !== undefined ? `${fmt(hand.pot)} BB` : "—"],
      ["状态", q.status || (hand.partial ? "partial" : "—")],
      ["推断动作", q.inferred_actions ?? 0],
      ["牌面不可靠", q.cards_unreliable ? "是" : "否"],
      ["建议数", (hand.advices || []).length],
    ]));
    return;
  }
  if (tab === "actions") {
    const lines = [];
    for (const street of hand.streets || []) {
      lines.push(`【${streetLabel(street.street)}】 起始底池 ${fmt(street.pot_start)} BB`);
      for (const a of street.actions || []) {
        lines.push(`  ${a.position || ""} ${a.name || a.seat_id || "?"}  ${actionLabel(a.action)}${a.amount ? ` ${fmt(a.amount)} BB` : ""}${a.all_in ? " ALL-IN" : ""}${a.inferred ? " [推断]" : ""}`);
      }
    }
    body.append(preBlock(lines.join("\n") || "（没有行动）"));
    return;
  }
  if (tab === "quality") {
    body.append(preBlock(JSON.stringify({
      data_quality: hand.data_quality || {},
      cards_warnings: hand.cards_warnings || [],
      shows: hand.shows || [],
      all_ins: hand.all_ins || [],
    }, null, 2)));
    return;
  }
  if (tab === "advices") {
    const root = h("div", "advice-card");
    const advices = handAdvices(hand);
    for (const a of advices) {
      const item = h("button", "history-item");
      item.type = "button";
      item.append(h("span", `history-dot ${optionKind(a.option)}`, ""));
      const main = h("span", "history-main");
      main.append(h("div", "history-action", optionAction(a.option)));
      main.append(h("div", "history-sub", `${streetLabel(a.street)} · ${a.reason || ""}`));
      item.append(main, h("span", "history-time", a.duration_ms ? `${(a.duration_ms / 1000).toFixed(1)}s` : "—"));
      item.addEventListener("click", () => openPrompt(a));
      root.append(item);
    }
    body.append(root);
  }
}

/* ───────────────────────── 事件处理 ───────────────────────── */

function hydrate(snapshot) {
  state.session = snapshot.session || {};
  state.layout = snapshot.layout || null;
  state.stats = snapshot.stats || {};
  state.frame = snapshot.frame || null;
  state.table = snapshot.table || null;
  state.pending = snapshot.pending || null;
  state.advice = snapshot.advice || null;
  state.advices = snapshot.advices || [];
  state.hands = snapshot.hands || [];
  state.feed = snapshot.events || [];
  state.endedAt = snapshot.ended_at || null;
  state.pendingSince = state.pending ? Date.now() : null;
  state.lastBoardKey = "";
  state.lastHeroCardsKey = "";
  renderAll();
}

function renderAll() {
  renderSession();
  renderStats();
  renderTable();
  renderSeats();
  renderHand();
  renderAdvice();
  renderHistory();
  renderHealth();
  renderFeed();
}

function handleEvent(msg) {
  const data = msg.data || {};
  switch (msg.type) {
    case "snapshot":
      hydrate(msg.data || msg);
      break;
    case "session_start":
      state.session = { ...state.session, ...data };
      renderSession();
      break;
    case "session_end":
      state.endedAt = data.ended_at || new Date().toISOString();
      renderSession();
      break;
    case "status":
      state.stats = { ...state.stats, ...data };
      renderStats();
      renderHealth();
      break;
    case "frame": {
      state.frame = data;
      state.stats = {
        ...state.stats,
        processed: data.processed ?? state.stats.processed,
        sampled: data.sampled ?? state.stats.sampled,
        accepted: data.accepted ?? state.stats.accepted,
        records: data.records ?? state.stats.records,
        avg_proc_ms: data.avg_proc_ms ?? state.stats.avg_proc_ms,
        advice_count: data.advice_count ?? state.stats.advice_count,
        advice_done: data.advice_done ?? state.stats.advice_done,
      };
      if (data.state) {
        state.table = {
          state: data.state,
          frame: Object.fromEntries(Object.entries(data).filter(([k]) => k !== "state")),
        };
        const flash = $("#frame-flash");
        flash.classList.remove("on");
        void flash.offsetWidth;
        flash.classList.add("on");
      }
      renderStats();
      renderTable();
      renderSeats();
      renderHand();
      renderHealth();
      pushFeed({ id: msg.id, type: "frame", ts: msg.ts, summary: {
        seq: data.seq, kind: data.kind, proc_ms: data.proc_ms,
        hand_id: data.hand_id, errors: data.errors,
      } });
      break;
    }
    case "cards": {
      // 质量闸丢帧时牌面单独推：牌是另一道闸确认过的，页面要立刻换牌。
      // 底池/筹码仍是上一份有效记录的值，等这帧恢复成有效状态再补。
      const heroCards = Array.isArray(data.hero_cards) ? data.hero_cards : [];
      const board = Array.isArray(data.board) ? data.board : [];
      if (state.table && state.table.state) {
        state.table = {
          ...state.table,
          state: {
            ...state.table.state,
            hero_cards: heroCards,
            board,
            cards_reliable: true,
          },
        };
      }
      if (state.frame && state.frame.hand) {
        state.frame = {
          ...state.frame,
          hand: { ...state.frame.hand, hero_cards: heroCards, board },
        };
      }
      renderTable();
      renderHand();
      break;
    }
    case "decision":
      state.pending = data;
      state.pendingSince = Date.now();
      renderAdvice();
      pushFeed({ id: msg.id, type: "decision", ts: msg.ts, summary: {
        street: data.street, pot: data.pot, to_call: data.to_call,
      } });
      break;
    case "advice":
      state.pending = null;
      state.pendingSince = null;
      state.advice = data;
      state.advices = [data, ...state.advices.filter((a) => a !== data)].slice(0, 30);
      if (data.source && data.source !== "model") {
        state.stats.fallbacks = Number(state.stats.fallbacks || 0) + 1;
      }
      renderAdvice();
      renderHistory();
      pushFeed({ id: msg.id, type: "advice", ts: msg.ts, summary: {
        street: data.street, action: optionAction(data.option),
        source: data.source, error: data.error,
      } });
      break;
    case "hand":
      state.hands = [data, ...state.hands.filter((x) => x.hand_id !== data.hand_id)].slice(0, 30);
      renderHand();
      renderHealth();
      pushFeed({ id: msg.id, type: "hand", ts: msg.ts, summary: {
        hand_id: data.hand_id, status: data.data_quality?.status,
        partial: data.partial, inferred_actions: data.data_quality?.inferred_actions,
      } });
      break;
    case "incident":
      renderHealth();
      pushFeed({ id: msg.id, type: "incident", ts: msg.ts, summary: data });
      break;
    case "advice_skip":
      renderHealth();
      pushFeed({ id: msg.id, type: "advice_skip", ts: msg.ts, summary: data });
      break;
    case "click":
      if (state.advice && data.click) {
        state.advice = { ...state.advice, click: data.click };
        renderAdvice();
      }
      pushFeed({ id: msg.id, type: "click", ts: msg.ts, summary: data });
      break;
    default:
      pushFeed({ id: msg.id, type: msg.type, ts: msg.ts, summary: data });
  }
}

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => {
    state.connected = true;
    renderSession();
  };
  es.onerror = () => {
    state.connected = false;
    renderSession();
  };
  es.onmessage = (event) => {
    state.connected = true;
    let msg;
    try { msg = JSON.parse(event.data); } catch (err) { return; }
    if (msg && msg.type === "snapshot") {
      hydrate(msg);
      return;
    }
    if (msg && msg.type) handleEvent(msg);
  };
}

/* ───────────────────────── 交互 ───────────────────────── */

function toast(text, bad = false) {
  const el = $("#toast");
  el.textContent = text;
  el.style.borderColor = bad ? "rgba(240,125,125,.4)" : "rgba(148,190,170,.35)";
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, 2200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch (_) {
    toast("复制失败（浏览器不允许）", true);
  }
}

function activateDock(key) {
  document.querySelectorAll(".dock-tab").forEach((tab) => {
    const active = tab.dataset.dock === key;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".dock-view").forEach((view) => {
    const active = view.dataset.dockView === key;
    view.hidden = !active;
    view.classList.toggle("is-active", active);
  });
  if (key === "feed") {
    const el = $("#feed-list");
    if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }
}

function bind() {
  document.querySelectorAll(".dock-tab").forEach((tab) => {
    tab.addEventListener("click", () => activateDock(tab.dataset.dock));
  });
  $("#btn-prompt").addEventListener("click", () => openPrompt(state.advice));
  $("#btn-copy-reason").addEventListener("click", () => {
    const a = state.advice;
    if (a) copyText(`${optionAction(a.option)}\n${a.reason || ""}`);
  });
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal-copy").addEventListener("click", () => copyText(state.modal?.copyText || ""));
  $("#modal").addEventListener("click", (event) => {
    if (event.target.id === "modal") closeModal();
  });
  $("#feed-pause").addEventListener("change", (event) => {
    state.feedPaused = event.target.checked;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
    if ((event.key === "p" || event.key === "P") && !event.metaKey && !event.ctrlKey
        && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
      if (state.advice) { event.preventDefault(); openPrompt(state.advice); }
    }
  });
  setInterval(updatePendingElapsed, 250);
  renderAll();
  connect();
}

document.addEventListener("DOMContentLoaded", bind);
