// dsh_ingest.mjs — 把 DSH 会话记录接进 newborn 大脑（自动、增量、可重跑）
//
// 直接读 ~/.dsh/sessions 下每个会话的 session.v4.jsonl(.zstd)，用压缩文件字节数做水位。
// 只处理 user/message 与 assistant/message 的正文；工具调用、思考、系统提示都跳过。
//
// 用法：
//   node tools/dsh_ingest.mjs --once
//   node tools/dsh_ingest.mjs --loop 300
//   node tools/dsh_ingest.mjs --once --dry

import { readdirSync, readFileSync, statSync, writeFileSync, existsSync, watch } from "node:fs";
import { join, basename, dirname } from "node:path";
import { homedir } from "node:os";
import { zstdDecompressSync } from "node:zlib";
import { spawnSync } from "node:child_process";

const ROOT = join(homedir(), ".dsh", "sessions");
const STATE = join(import.meta.dirname, ".ingest-state.json");
const PY = process.env.NEWBORN_PYTHON || "python";
const BRIDGE = join(import.meta.dirname, "session_bridge.py");
const DB = process.env.NEWBORN_DB || join(import.meta.dirname, "..", "memory", "live.sqlite3");

const MAX_PER_RUN = 80;
const MIN_LEN = 4;

// 这些是宿主每轮注入的上下文，不是他说的话，不能进记忆
const SKIP_PREFIXES = [
  "Current runtime context",
  "<system-reminder>",
  "Time sampled",
  "【互动根本原则】",
  "（到谷时啦",
  "[日程提醒]",
  "This is a scheduled message",
  "You are an AI agent powered by DeepSeek Harness",
  "You are a coding agent powered by",
  "<user_message qq=",
  "记忆图 ",
  "📝 已审计",
  "dsh-memory read-hook",
  "dsh-memory write",
];

function loadState() {
  if (!existsSync(STATE)) return { files: {}, seen: [] };
  try {
    return JSON.parse(readFileSync(STATE, "utf8"));
  } catch {
    return { files: {}, seen: [] };
  }
}

function saveState(state) {
  writeFileSync(STATE, JSON.stringify(state, null, 1), "utf8");
}

function listSessionFiles() {
  const out = [];
  const walk = (dir) => {
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const p = join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.startsWith("session.v4.jsonl")) out.push(p);
    }
  };
  walk(ROOT);
  return out;
}

function scopeOf(path) {
  const name = basename(dirname(path));
  const m = name.match(/^onebot-(private|group)-([0-9]+)/);
  if (m) {
    return m[1] === "group"
      ? { kind: "group", privacy: "conversation", conversation: `qq-group-${m[2]}` }
      : { kind: "private", privacy: "private", conversation: `qq-private-${m[2]}` };
  }
  return { kind: "web", privacy: "private", conversation: `dsh-${name}` };
}

/** 从消息记录里取正文：兼容 content[] / parts[] 两种写法 */
function textOf(data) {
  const blocks = Array.isArray(data?.content) ? data.content
    : Array.isArray(data?.parts) ? data.parts : [];
  const texts = blocks
    .filter((b) => b && b.type === "text" && typeof b.text === "string")
    .map((b) => b.text.trim())
    .filter(Boolean);
  return texts.join("\n").trim();
}

/** 去掉 QQ 桥包的外壳，留下真正的原话和昵称 */
function unwrap(text) {
  const m = text.match(/^<user_message[^>]*nickname="([^"]*)"[^>]*>([\s\S]*)<\/user_message>$/);
  if (m) return { who: m[1], text: m[2].trim() };
  return { who: null, text };
}

function collect(lines, state) {
  const fresh = [];
  for (const line of lines) {
    const s = line.trim();
    if (!s) continue;
    let rec;
    try {
      rec = JSON.parse(s);
    } catch {
      continue;
    }
    // 2026-10-30：暂时只收用户消息，回复不进库（他的决定）
    if (rec.type !== "user/message") continue;
    const data = rec.data || {};
    const role = data.role || (rec.type === "user/message" ? "user" : "assistant");
    let body = textOf(data);
    if (!body) continue;
    let who = null;
    if (role === "user") {
      const u = unwrap(body);
      body = u.text;
      who = u.who;
    }
    if (!body || body.length < MIN_LEN) continue;
    if (SKIP_PREFIXES.some((p) => body.startsWith(p))) continue;
    const key = data.id || rec.id || `${rec.time}:${body.slice(0, 24)}`;
    if (state.seen.includes(key)) continue;
    fresh.push({ role, text: body, key, who });
  }
  return fresh;
}

function ingestFile(path, state, dry) {
  const size = statSync(path).size;
  const prev = state.files[path] || { offset: 0 };
  if (prev.offset >= size) return 0;
  let buf;
  try {
    const raw = readFileSync(path);
    buf = path.includes(".zstd") ? zstdDecompressSync(raw) : raw;
  } catch {
    return 0; // 压缩帧没收尾，下一轮再来
  }
  const fresh = collect(buf.toString("utf8").split("\n"), state);
  const scope = scopeOf(path);
  let wrote = 0;
  for (const m of fresh.slice(-MAX_PER_RUN)) {
    const text = m.text.slice(0, 2000);
    if (dry) {
      console.log(`[dry] ${scope.conversation} ${m.role}: ${text.slice(0, 70)}`);
    } else {
      const args = [
        BRIDGE, "--db", DB,
        "--conversation", scope.conversation,
        "--actor", m.role === "user" ? "owner" : "self",
        "--privacy", scope.privacy,
        "remember", text,
      ];
      const r = spawnSync(PY, args, { encoding: "utf8", timeout: 20000 });
      if (r.status !== 0) {
        console.error("bridge failed:", (r.stderr || "").slice(0, 300));
        return wrote;
      }
    }
    state.seen.push(m.key);
    wrote++;
  }
  state.files[path] = { offset: size };
  if (state.seen.length > 5000) state.seen = state.seen.slice(-3000);
  return wrote;
}

function runOnce(dry) {
  const state = loadState();
  let total = 0;
  for (const f of listSessionFiles()) {
    try {
      total += ingestFile(f, state, dry);
    } catch (err) {
      console.error("skip", f, String(err).slice(0, 120));
    }
  }
  if (!dry) saveState(state);
  console.log(JSON.stringify({ ingested: total, files: Object.keys(state.files).length }));
}

const args = process.argv.slice(2);
const dry = args.includes("--dry");
if (args.includes("--watch")) {
  // 事件驱动：会话日志一写就收，2 秒去抖（避免一轮里写十几次）
  runOnce(dry);
  let timer = null;
  const kick = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      try { runOnce(dry); } catch (e) { console.error(String(e).slice(0, 200)); }
    }, 2000);
  };
  watch(ROOT, { recursive: true }, (_evt, file) => {
    if (!file || !String(file).includes("session.v4.jsonl")) return;
    kick();
  });
  console.log(JSON.stringify({ mode: "watch", root: ROOT }));
  setInterval(() => runOnce(dry), 15 * 60 * 1000); // 兜底：15 分钟一次，防漏事件
} else if (args.includes("--loop")) {
  const every = Number(args[args.indexOf("--loop") + 1] || 300) * 1000;
  runOnce(dry);
  setInterval(() => runOnce(dry), every);
} else {
  runOnce(dry);
}
