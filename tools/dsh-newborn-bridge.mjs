/**
 * dsh-newborn-bridge —— 把 newborn（NeuroMind）大脑接进当前会话。
 *
 * 形态：只有一个 systemPrompt 片段（不注册工具、不注入记忆正文），
 * 每轮提醒模型：这轮想记住/回想，就用会话桥读写大脑库。
 * 这样缓存失效面最小，也不会把私有记忆正文塞进上下文。
 *
 * 开关：在 profile 的 cordis.patch.yml 里 insert 本文件（file:// 路径），
 * 删掉条目或 disabled: true 即可关闭，与插件系统一致。
 */

export const name = "newborn-bridge";
export const inject = ["systemPrompt"];

const DEFAULT_BRIDGE = String.raw`D:\DeepSeekHarness\newborn\tools\session_bridge.py`;
const DEFAULT_DB = String.raw`D:\DeepSeekHarness\newborn\memory\live.sqlite3`;

function text(config = {}) {
  const bridge = config.bridgePath || DEFAULT_BRIDGE;
  const db = config.dbPath || DEFAULT_DB;
  const python = config.pythonPath || "python";
  return [
    "<newborn_brain>",
    "你有一个独立实现的大脑（NeuroMind v1.1），数据在 newborn 的 SQLite 里，和 DSH 的会话记录分开。",
    "需要记住一件事：用 pwsh 跑 `" + python + " \"" + bridge + "\" --db \"" + db + "\" remember \"要记的原话\"`。",
    "需要回想：把 remember 换成 recall，后面跟关键词；看统计用 status。",
    "规则：只记他明确说过的、值得长期保留的事（偏好、决定、踩过的坑、承诺）；不记寒暄与过程。",
    "隐私：默认 private；他要求共享时才改 --privacy。",
    "不要每轮都写。没有值得记的就不写。",
    "</newborn_brain>",
  ].join("\n");
}

export function apply(ctx, config) {
  const body = text(config || {});
  ctx.effect(() =>
    ctx.systemPrompt.section({
      name: "newborn-bridge:brain",
      order: 9400,
      text: body,
    }),
  );
}
