// Official-class validation using a real cordis Context (read-only).
// Usage: node _validate_official.mjs <root>
import { Context } from "file:///Users/blossomx/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/cordis/lib/index.js";

const root = process.argv[2];
if (!root) { console.error("usage: node _validate_official.mjs <sessionRoot>"); process.exit(2); }

const mod = await import("file:///Users/blossomx/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js");
const Persistence = mod.default;

const ctx = new Context();
let fiber;
try {
  fiber = await ctx.plugin(Persistence, { root, compression: "zstd", packChunks: false });
} catch (e) {
  console.log("plugin mount failed:", e.message);
  process.exit(1);
}
const p = fiber.ctx.get("sessionPersistence");
if (!p) { console.log("no persistence service mounted"); process.exit(1); }

const listed = await p.list();
console.log("official list():", listed.map((m) => m.id + " cwd=" + m.cwd));
for (const meta of listed) {
  const ins = await p.inspect(meta.id);
  console.log(`inspect ${meta.id}: cursor=${ins.cursor} events=${ins.events.length}`);
}
await ctx.stop().catch(() => {});
