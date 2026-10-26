//! gto-runner: 求解一个翻后 job，抽出翻牌/转牌的节点策略，原子落盘。
//!
//! 用法：
//!   gto-runner --job jobs/x.json --out data/raw [--iterations 250] [--threads 12] [--skip-done]
//!
//! 产物（同一目录）：
//!   <job_id>.<unix_ts>.gto.json.gz   压缩后的策略数据（唯一文件名，不覆盖）
//!   <job_id>.meta.json               元数据（sha256 / 节点数 / 版本）
//!   <job_id>.done                    完成标记（内容同 meta），最后写
//!
//! 断电：.tmp 不完整会被忽略；.done 不存在或 sha256 不匹配就重跑。

use anyhow::{anyhow, Context, Result};
use flate2::write::GzEncoder;
use flate2::Compression;
use postflop_solver::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const FORMAT_VERSION: u32 = 1;
const ENGINE_VERSION: &str = "postflop-solver@9d1509f";
const RUNNER_VERSION: &str = env!("CARGO_PKG_VERSION");

// ─────────────────────────── job ───────────────────────────

#[derive(Debug, Deserialize)]
struct Job {
    id: String,
    config_hash: String,
    flop: String,
    oop_range: String,
    ip_range: String,
    starting_pot: i32,
    effective_stack: i32,
    #[serde(default)]
    rake_rate: f64,
    #[serde(default)]
    rake_cap: f64,
    bet_sizes: BetSizes,
    raise_sizes: String,
    #[serde(default)]
    iterations: u32,
    #[serde(default)]
    add_allin_threshold: f64,
    #[serde(default = "default_force_allin")]
    force_allin_threshold: f64,
    #[serde(default)]
    merging_threshold: f64,
}

#[derive(Debug, Deserialize, Clone)]
struct BetSizes {
    flop: String,
    turn: String,
    river: String,
}

fn default_force_allin() -> f64 {
    0.15
}

// ─────────────────────────── output ───────────────────────────

#[derive(Debug, Serialize)]
struct OutFile {
    format_version: u32,
    job_id: String,
    config_hash: String,
    engine_version: String,
    runner_version: String,
    iterations: u32,
    exploitability: f32,
    starting_pot: i32,
    effective_stack: i32,
    flop: String,
    hands: Vec<String>,
    weights_oop: Vec<f32>,
    weights_ip: Vec<f32>,
    nodes: Vec<NodeOut>,
}

#[derive(Debug, Serialize)]
struct NodeOut {
    street: String,
    turn: String,
    key: String,
    actor: usize,
    pot: i32,
    actions: Vec<ActOut>,
    /// 扁平数组：action-major，长度 = actions.len() * hands.len()
    strategy: Vec<u8>,
    /// 扁平数组：action-major，单位是 chip（1 BB = 100 chip）
    ev: Vec<i32>,
    /// 该节点行动方按到达权重加权后的范围频率
    range_mix: Vec<f32>,
}

#[derive(Debug, Serialize)]
struct ActOut {
    id: String,
    amount: i32,
    frac: f32,
}

#[derive(Debug, Serialize, Deserialize)]
struct Meta {
    format_version: u32,
    job_id: String,
    file: String,
    sha256: String,
    bytes: u64,
    config_hash: String,
    engine_version: String,
    runner_version: String,
    iterations: u32,
    exploitability: f32,
    nodes: usize,
    hands: usize,
    threads: usize,
    source_host: String,
    created_at_unix: u64,
    elapsed_secs: f64,
}

// ─────────────────────────── args ───────────────────────────

struct Args {
    job: PathBuf,
    out: PathBuf,
    iterations: Option<u32>,
    threads: usize,
    skip_done: bool,
}

fn parse_args() -> Result<Args> {
    let mut job = None;
    let mut out = None;
    let mut iterations = None;
    let mut threads = 0usize;
    let mut skip_done = false;
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--job" => job = Some(PathBuf::from(it.next().ok_or_else(|| anyhow!("--job needs a value"))?)),
            "--out" => out = Some(PathBuf::from(it.next().ok_or_else(|| anyhow!("--out needs a value"))?)),
            "--iterations" => {
                iterations = Some(it.next().ok_or_else(|| anyhow!("--iterations needs a value"))?.parse()?)
            }
            "--threads" => threads = it.next().ok_or_else(|| anyhow!("--threads needs a value"))?.parse()?,
            "--skip-done" => skip_done = true,
            "-h" | "--help" => {
                println!("gto-runner --job <job.json> --out <dir> [--iterations N] [--threads N] [--skip-done]");
                std::process::exit(0);
            }
            other => return Err(anyhow!("unknown argument: {other}")),
        }
    }
    Ok(Args {
        job: job.ok_or_else(|| anyhow!("--job is required"))?,
        out: out.ok_or_else(|| anyhow!("--out is required"))?,
        iterations,
        threads,
        skip_done,
    })
}

// ─────────────────────────── main ───────────────────────────

fn main() {
    if let Err(e) = run() {
        eprintln!("[gto-runner] error: {e:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let args = parse_args()?;
    let job_bytes = fs::read(&args.job).with_context(|| format!("read job {:?}", args.job))?;
    let job: Job = serde_json::from_slice(&job_bytes).context("parse job json")?;
    fs::create_dir_all(&args.out)?;

    let safe_id = safe_name(&job.id);
    let done_path = args.out.join(format!("{safe_id}.done"));

    if args.skip_done {
        if let Ok(txt) = fs::read_to_string(&done_path) {
            if let Ok(meta) = serde_json::from_str::<Meta>(&txt) {
                if meta.format_version == FORMAT_VERSION && meta.config_hash == job.config_hash {
                    let p = args.out.join(&meta.file);
                    if p.exists() && sha256_file(&p)? == meta.sha256 {
                        println!("[skip] {} (already done)", job.id);
                        return Ok(());
                    }
                    println!("[redo] {} (.done exists but file missing/corrupt)", job.id);
                }
            }
        }
    }

    if args.threads > 0 {
        let _ = rayon::ThreadPoolBuilder::new()
            .num_threads(args.threads)
            .build_global();
    }

    let iterations = args.iterations.unwrap_or(if job.iterations > 0 { job.iterations } else { 250 });

    println!(
        "[solve] {} flop={} pot={} stack={} iters={} threads={}",
        job.id, job.flop, job.starting_pot, job.effective_stack, iterations, args.threads
    );
    let t0 = Instant::now();

    let flop = flop_from_str(&job.flop).map_err(|e| anyhow!("bad flop {}: {e}", job.flop))?;
    let oop_range: Range = job.oop_range.parse().map_err(|e: String| anyhow!("oop range: {e}"))?;
    let ip_range: Range = job.ip_range.parse().map_err(|e: String| anyhow!("ip range: {e}"))?;

    let card_config = CardConfig {
        range: [oop_range, ip_range],
        flop,
        turn: NOT_DEALT,
        river: NOT_DEALT,
    };

    let flop_sizes = BetSizeOptions::try_from((job.bet_sizes.flop.as_str(), job.raise_sizes.as_str()))
        .map_err(|e| anyhow!("flop sizes: {e}"))?;
    let turn_sizes = BetSizeOptions::try_from((job.bet_sizes.turn.as_str(), job.raise_sizes.as_str()))
        .map_err(|e| anyhow!("turn sizes: {e}"))?;
    let river_sizes = BetSizeOptions::try_from((job.bet_sizes.river.as_str(), job.raise_sizes.as_str()))
        .map_err(|e| anyhow!("river sizes: {e}"))?;

    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: job.starting_pot,
        effective_stack: job.effective_stack,
        rake_rate: job.rake_rate,
        rake_cap: job.rake_cap,
        flop_bet_sizes: [flop_sizes.clone(), flop_sizes],
        turn_bet_sizes: [turn_sizes.clone(), turn_sizes],
        river_bet_sizes: [river_sizes.clone(), river_sizes],
        turn_donk_sizes: None,
        river_donk_sizes: None,
        add_allin_threshold: job.add_allin_threshold,
        force_allin_threshold: job.force_allin_threshold,
        merging_threshold: job.merging_threshold,
    };

    let action_tree = ActionTree::new(tree_config).map_err(|e| anyhow!("build tree: {e}"))?;
    let mut game = PostFlopGame::with_config(card_config, action_tree)
        .map_err(|e| anyhow!("init game: {e}"))?;
    game.allocate_memory(true);

    let (mem, mem_compressed) = game.memory_usage();
    println!(
        "[solve] memory {:.2} GB (compressed {:.2} GB)",
        mem as f64 / (1024.0 * 1024.0 * 1024.0),
        mem_compressed as f64 / (1024.0 * 1024.0 * 1024.0)
    );

    // target_exploitability = 0 -> 固定迭代数，跨机器可比
    let exploitability = solve(&mut game, iterations, 0.0, false);
    // 存成「底池百分比」，和日志/README 口径一致
    let exploitability_pct = 100.0 * exploitability / job.starting_pot as f32;
    let elapsed = t0.elapsed().as_secs_f64();
    println!(
        "[solve] done in {:.1}s, exploitability {:.4}% pot",
        elapsed, exploitability_pct
    );

    // ── 手牌并集 + 权重 ──
    let oop_cards = game.private_cards(0).to_vec();
    let ip_cards = game.private_cards(1).to_vec();
    let mut map: BTreeMap<String, usize> = BTreeMap::new();
    for h in oop_cards.iter().chain(ip_cards.iter()) {
        map.insert(hole_to_string(*h).map_err(|e| anyhow!("{e}"))?, 0);
    }
    let hands: Vec<String> = map.keys().cloned().collect();
    let index: BTreeMap<String, usize> = hands
        .iter()
        .cloned()
        .enumerate()
        .map(|(i, s)| (s, i))
        .collect();
    let n_hands = hands.len();

    let mut weights_oop = vec![0f32; n_hands];
    let mut weights_ip = vec![0f32; n_hands];
    for (p, cards) in [(0usize, &oop_cards), (1usize, &ip_cards)] {
        let w = game.initial_weights(p);
        let dst = if p == 0 { &mut weights_oop } else { &mut weights_ip };
        for (h, wt) in cards.iter().zip(w.iter()) {
            if let Some(&i) = index.get(&hole_to_string(*h).map_err(|e| anyhow!("{e}"))?) {
                dst[i] = *wt;
            }
        }
    }

    let ctx = Ctx {
        index: &index,
        n_hands,
    };

    // ── 抽节点 ──
    let mut nodes: Vec<NodeOut> = Vec::new();

    // 翻牌根节点（OOP 先说话）
    extract(&mut game, &ctx, "flop", "", "root", &mut nodes)?;

    let root_actions = game.available_actions();
    let root_pot = game.tree_config().starting_pot;

    // 翻牌：OOP check 之后
    if let Some(ci) = find_action(&root_actions, |a| matches!(a, Action::Check)) {
        game.apply_history(&[ci]);
        extract(&mut game, &ctx, "flop", "", "after_check", &mut nodes)?;

        let ip_actions = game.available_actions();
        let totals = game.total_bet_amount();
        let pot = root_pot + totals[0] + totals[1];
        let to_call = (totals[0] - totals[1]).max(0);
        for (j, a) in ip_actions.iter().enumerate() {
            if let Action::Bet(_) = a {
                let lab = size_id(describe(*a, pot, to_call, totals[0]).2 as f64);
                game.apply_history(&[ci, j]);
                extract(
                    &mut game,
                    &ctx,
                    "flop",
                    "",
                    &format!("after_check_vs_b{lab}"),
                    &mut nodes,
                )?;
                let resp = game.available_actions();
                if let Some(call_i) = find_action(&resp, |x| matches!(x, Action::Call)) {
                    game.apply_history(&[ci, j, call_i]);
                    extract_turn_line(
                        &mut game,
                        &ctx,
                        &[ci, j, call_i],
                        &format!("x_b{lab}c"),
                        &mut nodes,
                    )?;
                }
            }
        }
    }

    // 翻牌：OOP 直接下注
    for (i, a) in root_actions.iter().enumerate() {
        if let Action::Bet(_) = a {
            let lab = size_id(describe(*a, root_pot, 0, 0).2 as f64);
            game.apply_history(&[i]);
            extract(&mut game, &ctx, "flop", "", &format!("vs_b{lab}"), &mut nodes)?;
            let resp = game.available_actions();
            if let Some(call_i) = find_action(&resp, |x| matches!(x, Action::Call)) {
                game.apply_history(&[i, call_i]);
                extract_turn_line(
                    &mut game,
                    &ctx,
                    &[i, call_i],
                    &format!("b{lab}c"),
                    &mut nodes,
                )?;
            }
        }
    }

    println!("[extract] {} nodes, {} hands", nodes.len(), n_hands);

    // ── 写盘 ──
    let out = OutFile {
        format_version: FORMAT_VERSION,
        job_id: job.id.clone(),
        config_hash: job.config_hash.clone(),
        engine_version: ENGINE_VERSION.to_string(),
        runner_version: RUNNER_VERSION.to_string(),
        iterations,
        exploitability: exploitability_pct,
        starting_pot: job.starting_pot,
        effective_stack: job.effective_stack,
        flop: job.flop.clone(),
        hands,
        weights_oop,
        weights_ip,
        nodes,
    };

    let json = serde_json::to_vec(&out).context("serialize output")?;
    let mut enc = GzEncoder::new(Vec::new(), Compression::default());
    enc.write_all(&json)?;
    let gz = enc.finish()?;

    let now = SystemTime::now().duration_since(UNIX_EPOCH)?;
    let ts = now.as_secs();
    let file_name = format!("{safe_id}.{}.gto.json.gz", now.as_millis());
    let final_path = args.out.join(&file_name);

    write_atomic(&args.out.join(format!("{file_name}.tmp")), &final_path, &gz)?;

    let meta = Meta {
        format_version: FORMAT_VERSION,
        job_id: job.id.clone(),
        file: file_name.clone(),
        sha256: hex(&Sha256::digest(&gz)),
        bytes: gz.len() as u64,
        config_hash: job.config_hash.clone(),
        engine_version: ENGINE_VERSION.to_string(),
        runner_version: RUNNER_VERSION.to_string(),
        iterations,
        exploitability: exploitability_pct,
        nodes: out.nodes.len(),
        hands: out.hands.len(),
        threads: args.threads,
        source_host: hostname(),
        created_at_unix: ts,
        elapsed_secs: elapsed,
    };
    let meta_json = serde_json::to_vec_pretty(&meta)?;
    write_atomic(
        &args.out.join(format!("{safe_id}.meta.json.tmp")),
        &args.out.join(format!("{safe_id}.meta.json")),
        &meta_json,
    )?;
    // .done 最后写
    write_atomic(
        &args.out.join(format!("{safe_id}.done.tmp")),
        &done_path,
        &meta_json,
    )?;

    println!(
        "[done] {} -> {} ({:.2} MB, {:.1}s)",
        job.id,
        file_name,
        gz.len() as f64 / (1024.0 * 1024.0),
        elapsed
    );
    Ok(())
}

// ─────────────────────────── extract ───────────────────────────

struct Ctx<'a> {
    index: &'a BTreeMap<String, usize>,
    n_hands: usize,
}

fn extract(
    game: &mut PostFlopGame,
    ctx: &Ctx,
    street: &str,
    turn: &str,
    key: &str,
    nodes: &mut Vec<NodeOut>,
) -> Result<()> {
    if game.is_terminal_node() || game.is_chance_node() {
        return Ok(());
    }
    game.cache_normalized_weights();

    let actor = game.current_player();
    let cards = game.private_cards(actor).to_vec();
    let board = game.current_board();
    let n_local = cards.len();
    let actions = game.available_actions();
    let totals = game.total_bet_amount();
    let pot = game.tree_config().starting_pot + totals[0] + totals[1];
    let to_call = (totals[actor ^ 1] - totals[actor]).max(0);
    let prev_bet = totals[actor ^ 1];

    let strat = game.strategy();
    let evd = game.expected_values_detail(actor);
    let nw = game.normalized_weights(actor).to_vec();

    if strat.len() != actions.len() * n_local || evd.len() != actions.len() * n_local {
        return Err(anyhow!(
            "unexpected strategy/ev length at {key}: strat={} ev={} actions={} hands={}",
            strat.len(),
            evd.len(),
            actions.len(),
            n_local
        ));
    }

    let mut out = NodeOut {
        street: street.to_string(),
        turn: turn.to_string(),
        key: key.to_string(),
        actor,
        pot,
        actions: Vec::with_capacity(actions.len()),
        strategy: vec![0u8; actions.len() * ctx.n_hands],
        ev: vec![0i32; actions.len() * ctx.n_hands],
        range_mix: vec![0.0; actions.len()],
    };

    for a in &actions {
        let (id, amount, frac) = describe(*a, pot, to_call, prev_bet);
        out.actions.push(ActOut { id, amount, frac });
    }

    let mut total_w = 0.0f64;
    let mut mix = vec![0.0f64; actions.len()];
    for (h, (c1, c2)) in cards.iter().enumerate() {
        if board.contains(c1) || board.contains(c2) {
            continue;
        }
        let combo = hole_to_string((*c1, *c2)).map_err(|e| anyhow!("{e}"))?;
        let Some(&g) = ctx.index.get(&combo) else { continue };
        let w = nw[h] as f64;
        if !w.is_finite() || w <= 0.0 {
            continue;
        }
        total_w += w;

        // 先取浮点策略，再做「最大余数」修正，保证每手频率和恰好 100
        let mut raw = vec![0f64; actions.len()];
        for a in 0..actions.len() {
            let s = strat[a * n_local + h];
            raw[a] = if s.is_finite() { s.clamp(0.0, 1.0) as f64 } else { 0.0 };
        }
        let raw_sum: f64 = raw.iter().sum();
        let mut q: Vec<i32> = raw.iter().map(|x| (x * 100.0).round() as i32).collect();
        let q_sum: i32 = q.iter().sum();
        if raw_sum > 0.0 && q_sum != 100 {
            let mi = raw
                .iter()
                .enumerate()
                .max_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal))
                .map(|(i, _)| i)
                .unwrap_or(0);
            q[mi] = (q[mi] + (100 - q_sum)).max(0);
        }
        for a in 0..actions.len() {
            out.strategy[a * ctx.n_hands + g] = q[a].clamp(0, 100) as u8;
            let e = evd[a * n_local + h];
            out.ev[a * ctx.n_hands + g] = if e.is_finite() { e.round() as i32 } else { 0 };
            mix[a] += w * raw[a];
        }
    }
    if total_w > 0.0 {
        for a in 0..actions.len() {
            out.range_mix[a] = (mix[a] / total_w) as f32;
        }
    }
    nodes.push(out);
    Ok(())
}

fn extract_turn_line(
    game: &mut PostFlopGame,
    ctx: &Ctx,
    prefix: &[usize],
    line: &str,
    nodes: &mut Vec<NodeOut>,
) -> Result<()> {
    if !game.is_chance_node() {
        return Ok(());
    }
    let mask = game.possible_cards();
    for card in 0u8..52 {
        if mask & (1u64 << card) == 0 {
            continue;
        }
        let mut path = prefix.to_vec();
        path.push(card as usize);
        game.apply_history(&path);
        let card_str = card_to_string(card).map_err(|e| anyhow!("{e}"))?;
        extract(
            game,
            ctx,
            "turn",
            &card_str,
            &format!("turn:{card_str}:{line}:root"),
            nodes,
        )?;
        if game.is_terminal_node() || game.is_chance_node() {
            continue;
        }
        let acts = game.available_actions();
        if let Some(ci) = find_action(&acts, |a| matches!(a, Action::Check)) {
            path.push(ci);
            game.apply_history(&path);
            extract(
                game,
                ctx,
                "turn",
                &card_str,
                &format!("turn:{card_str}:{line}:after_check"),
                nodes,
            )?;
        }
    }
    Ok(())
}

// ─────────────────────────── helpers ───────────────────────────

fn describe(a: Action, pot: i32, to_call: i32, prev_bet: i32) -> (String, i32, f32) {
    match a {
        Action::Check => ("check".to_string(), 0, 0.0),
        Action::Fold => ("fold".to_string(), 0, 0.0),
        Action::Call => ("call".to_string(), to_call, 0.0),
        Action::Bet(x) => {
            let f = x as f64 / pot.max(1) as f64;
            (format!("bet-{}", size_id(f)), x, f as f32)
        }
        Action::Raise(x) => {
            let denom = (pot + to_call).max(1) as f64;
            let f = (x - prev_bet) as f64 / denom;
            (format!("raise-{}", size_id(f)), x, f as f32)
        }
        Action::AllIn(x) => ("allin".to_string(), x, 0.0),
        Action::None => ("none".to_string(), 0, 0.0),
        Action::Chance(c) => (format!("chance-{c}"), 0, 0.0),
    }
}

fn size_id(f: f64) -> String {
    const FR: [f64; 5] = [1.0 / 3.0, 0.5, 2.0 / 3.0, 1.0, 1.2];
    const ID: [&str; 5] = ["33", "50", "67", "100", "120"];
    if !f.is_finite() {
        return "na".to_string();
    }
    let mut best = 0usize;
    let mut bd = f64::MAX;
    for (i, x) in FR.iter().enumerate() {
        let d = (f - x).abs();
        if d < bd {
            bd = d;
            best = i;
        }
    }
    if bd <= 0.06 {
        ID[best].to_string()
    } else {
        format!("{:.0}", f * 100.0)
    }
}

fn find_action<F: Fn(&Action) -> bool>(actions: &[Action], f: F) -> Option<usize> {
    actions.iter().position(f)
}

fn safe_name(s: &str) -> String {
    s.chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.' { c } else { '_' })
        .collect()
}

fn hostname() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "unknown".to_string())
}

fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{x:02x}"));
    }
    s
}

fn sha256_file(p: &Path) -> Result<String> {
    let bytes = fs::read(p)?;
    Ok(hex(&Sha256::digest(&bytes)))
}

fn write_atomic(tmp: &Path, dst: &Path, bytes: &[u8]) -> Result<()> {
    {
        let mut f = fs::File::create(tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    if dst.exists() {
        // 目标已存在：先删再 rename（Windows 上 rename 不覆盖）。
        // 这里的目标名带时间戳/固定名，删除窗口很短；.done 的 sha256 会兜底。
        fs::remove_file(dst).ok();
    }
    fs::rename(tmp, dst)?;
    Ok(())
}
